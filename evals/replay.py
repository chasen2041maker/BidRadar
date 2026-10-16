"""评测时的模型调用：录制和重放，加一个内存账本。

录制：每次真实调用的响应按"请求指纹"存进 recordings/<模型>.jsonl。指纹只看请求内容
（用途、提示词、输入、输出上限、温度），不看 call_key：call_key 里有运行 ID，每次评测都不一样。
重放：同样的请求直接取录下的响应，不发送、不花钱。改了提示词，指纹就变了，只有变了的那些调用才需要重新录。

录制文件可以提交进仓库：内容只来自虚构的招标文件和虚构档案，CI 和别人都能免费重放。
"""

import json
from pathlib import Path

from bidradar.agent.llm import (
    BudgetExceeded,
    CallOutcomeUnknown,
    LLMClient,
    LLMRequest,
    LLMResponse,
    Pricing,
    Usage,
)
from bidradar.infra.ids import fingerprint

RECORDINGS = Path(__file__).parent / "recordings"


class MissingRecording(Exception):
    """重放模式下遇到了没录过的请求。"""

    def __init__(self, purpose: str) -> None:
        super().__init__(
            f"没有录过这个 {purpose} 请求（可能改了提示词或规则）。"
            "要重新录，用 --llm live --enable-model（会花钱，先确认费用上限）。"
        )


def request_key(request: LLMRequest) -> str:
    return fingerprint([request.purpose, request.system, request.user, request.max_output_tokens, request.temperature])


class RecordingLLM:
    """inner 为 None 时只重放；否则没录过的请求交给 inner，并把响应录下来。"""

    def __init__(self, path: Path, inner: LLMClient | None = None, pricing: Pricing | None = None) -> None:
        self.path, self.inner, self.pricing = path, inner, pricing or Pricing()
        self.recorded: dict[str, dict] = {}
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    row = json.loads(line)
                    self.recorded[row["key"]] = row
        self.calls = 0
        self.replayed = 0
        self.cost_micro = 0  # 按录制时的用量折算：重放不花钱，这个数用来比较"一份报告大约要多少钱"

    def complete(self, request: LLMRequest) -> LLMResponse:
        self.calls += 1
        key = request_key(request)
        row = self.recorded.get(key)
        if row is not None:
            self.replayed += 1
            usage = Usage(**row["usage"]) if row.get("usage") else None
            response = LLMResponse(text=row["text"], usage=usage, model=row.get("model", ""), replayed=True)
        else:
            if self.inner is None:
                raise MissingRecording(request.purpose)
            response = self.inner.complete(request)
            row = {
                "key": key,
                "purpose": request.purpose,
                "text": response.text,
                "usage": response.usage.model_dump() if response.usage else None,
                "model": response.model,
            }
            self.recorded[key] = row
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as file:
                file.write(json.dumps(row, ensure_ascii=False) + "\n")
        if response.usage is not None:
            self.cost_micro += self.pricing.cost(response.usage)
        return response


class MemoryLedger:
    """评测用的账本：只有一个总上限，进程结束就没了。规则和线上的 research 账本一样：先预留、后结算。"""

    def __init__(self, cap_micro: int) -> None:
        self.cap = cap_micro
        self.rows: dict[str, dict] = {}

    @property
    def spent(self) -> int:
        return sum(
            row["charged"] if row["state"] == "settled" else row["reserved"]
            for row in self.rows.values()
            if row["state"] != "released"
        )

    def lookup(self, call_key: str) -> LLMResponse | None:
        row = self.rows.get(call_key)
        if row is None or row["state"] == "released":
            return None
        if row["state"] != "settled":
            raise CallOutcomeUnknown()
        return row["response"]

    def reserve(self, request: LLMRequest, amount: int) -> None:
        if self.spent + amount > self.cap:
            raise BudgetExceeded()
        self.rows[request.call_key] = {"state": "pending", "reserved": amount, "charged": 0}

    def settle(self, request: LLMRequest, response: LLMResponse, cost: int) -> None:
        self.rows[request.call_key].update(state="settled", charged=cost, response=response)

    def mark_unknown(self, request: LLMRequest) -> None:
        self.rows[request.call_key]["state"] = "unknown"

    def release(self, request: LLMRequest) -> None:
        self.rows[request.call_key].update(state="released", reserved=0)
