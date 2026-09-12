"""生成可重建的学习索引；只整理文件和 Git 事实，不猜测业务进度或执行记录中的命令。"""
from __future__ import annotations

import argparse
import hashlib
import html
import os
from pathlib import Path
import re
import subprocess
import sys

INDEX = "docs/learning/INDEX.md"
INPUTS = ("AGENTS.md", "PROJECT_STATE.md", "CURRENT_TASK.md")
SECTIONS = ("本次改变", "阅读顺序", "数据流与失败边界", "验证与未验证", "我的参与", "值得保留的决定与坑")
NOTE_NAME = re.compile(r"[A-Z][A-Z0-9]*-[0-9]+\.md")
SHA = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")


def read_text(root: Path, name: str) -> str:
    """只读仓库内的小型 UTF-8 文件；拒绝逃逸符号链接，避免把机器私有文件写进摘要。"""
    path = (root / name).resolve()
    if not path.is_relative_to(root.resolve()) or not path.is_file():
        raise ValueError(f"缺失或越界文件: {name}")
    if path.stat().st_size > 32768:
        raise ValueError(f"文件超过 32 KiB，请拆分或归档: {name}")
    return path.read_text(encoding="utf-8")


def load_notes(root: Path) -> list[tuple[str, str, str]]:
    """约定笔记格式而不是解析任意 Markdown：任务标题、单行摘要和六个非空章节。"""
    result = []
    folder = root / "docs/learning"
    for path in sorted(folder.glob("*.md")):
        if path.name in ("README.md", "INDEX.md"):
            continue
        if not NOTE_NAME.fullmatch(path.name):
            raise ValueError(f"学习笔记须使用任务编号命名: {path.name}")
        name = path.relative_to(root).as_posix()
        text = read_text(root, name)
        lines = text.splitlines()
        if not lines or not lines[0].startswith("# "):
            raise ValueError(f"缺少任务标题: {name}")
        summaries = [s[3:].strip() for s in lines if s.startswith("摘要：")]
        if len(summaries) != 1 or not 1 <= len(summaries[0]) <= 200:
            raise ValueError(f"须提供唯一的 1–200 字单行摘要: {name}")
        for section in SECTIONS:
            marker = f"## {section}"
            if marker not in lines:
                raise ValueError(f"缺少章节 {section}: {name}")
            start = lines.index(marker) + 1
            end = next((i for i in range(start, len(lines)) if lines[i].startswith("## ")), len(lines))
            if not any(line.strip() for line in lines[start:end]):
                raise ValueError(f"空章节 {section}: {name}")
        result.append((name, summaries[0], text))
    if not result:
        raise ValueError("没有学习/变更笔记")
    return result


def safe_text(text: str) -> str:
    """摘要按普通文字展示；阻止笔记中的 HTML/Markdown 被拼成可执行或误导性的页面。"""
    return html.escape(text).replace("|", "&#124;").replace("[", "&#91;").replace("]", "&#93;").replace("`", "&#96;")


def render_index(root: Path) -> str:
    """相同输入产生相同输出；不写时间/HEAD，避免提交摘要又改变自身版本的无限循环。"""
    notes = load_notes(root)
    sources = [(name, read_text(root, name)) for name in INPUTS]
    sources += [(name, text) for name, _, text in notes]
    # 文件名与正文之间加分隔，防止不同文件的拼接边界产生相同指纹。
    digest = hashlib.sha256()
    for name, text in sources:
        digest.update(name.encode("utf-8") + b"\0" + text.encode("utf-8") + b"\0")
    lines = ["# 项目学习与记忆索引（自动生成）", "", "由 scripts/refresh_project_memory.py 从状态文件和任务笔记生成；不要手工编辑。", "", "先读 [规则](../../AGENTS.md)、[状态](../../PROJECT_STATE.md)、[当前任务](../../CURRENT_TASK.md)，再按需读下列记录。", "", f"来源指纹：`{digest.hexdigest()}`", "", f"## 已记录任务（共 {len(notes)} 条，按编号倒序显示最多 20 条）", "", "| 任务 | 有用摘要 |", "| --- | --- |"]
    # 索引不复制长日志；全部知识保留在按任务拆分的源笔记中。
    for name, summary, _ in reversed(notes[-20:]):
        lines.append(f"| [{Path(name).stem}]({Path(name).name}) | {safe_text(summary)} |")
    lines += ["", "实现、测试、独立审查、合并与本人学习进度是不同事实；该索引不自动给任何一项盖章。", "全部源笔记留在本目录，不因索引截取而删除。每次推送/PR 更新的实际 SHA 与检查结果见 Actions 运行摘要。", ""]
    return "\n".join(lines)


def git(root: Path, *args: str) -> str:
    """只运行固定 Git 查询，参数以列表传递；绝不执行学习笔记或提交消息里的命令。"""
    run = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, timeout=20, check=True)
    return run.stdout


def require_note_update(root: Path, base: str) -> None:
    """CI 防漏记：有非索引变更，就要求同一 PR 更新至少一份任务笔记；不评价语义质量。"""
    if not SHA.fullmatch(base) or set(base) == {"0"}:
        raise ValueError("--base 必须是有效的完整提交 SHA")
    ancestor = git(root, "merge-base", base, "HEAD").strip()
    changed = set(git(root, "diff", "--name-only", "--no-renames", "-z", ancestor, "HEAD").split("\0")) - {""}
    # 删除旧笔记不能冒充补充记忆；只接受当前仍存在且通过格式检查的笔记。
    notes = {name for name, _, _ in load_notes(root)}
    meaningful = changed - {INDEX} - notes
    if meaningful and not (changed & notes):
        raise ValueError("存在变更但没有更新任务笔记；请运行交接 Skill，说明决定/学习点或明确无新增知识的原因")


def runtime_report(root: Path) -> str:
    """合并后的提交号不回写 main：从当前 checkout 生成运行摘要，避免机器人递归提交。"""
    head = git(root, "rev-parse", "HEAD").strip()
    if not SHA.fullmatch(head):
        raise ValueError("无法确认实际 HEAD")
    rows = ["# 本次项目记忆快照", "", f"实际 checkout：`{head}`", f"事件：{safe_text(os.environ.get('GITHUB_EVENT_NAME', 'local'))}", "", "以下为各检查步骤的实际 outcome，不等于产品验收或独立审查："]
    for key in ("STRUCTURE_OUTCOME", "TESTS_OUTCOME", "MEMORY_OUTCOME", "CLEAN_OUTCOME", "DIFF_OUTCOME"):
        value = os.environ.get(key, "未提供")
        if value not in ("success", "failure", "cancelled", "skipped", "未提供"):
            value = "未知"
        rows.append(f"- {key}: {value}")
    rows += ["", "## 任务记录（按编号倒序，最多五条）"]
    for name, summary, _ in load_notes(root)[-5:][::-1]:
        rows.append(f"- {Path(name).stem}: {safe_text(summary)}")
    rows += ["", "独立审查、合并授权和个人掌握程度：本脚本不判定，请回读相应证据。", ""]
    return "\n".join(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--write", action="store_true")
    modes.add_argument("--check", action="store_true")
    modes.add_argument("--report", action="store_true")
    parser.add_argument("--base", help="可选：核对当前提交相对基线是否同步记笔记")
    args = parser.parse_args()
    root = args.root.resolve()
    try:
        if args.report:
            print(runtime_report(root))
            return 0
        expected = render_index(root)
        if args.base:
            require_note_update(root, args.base)
        if args.write:
            target = root / INDEX
            # 即使是生成文件，也不能通过符号链接覆盖仓库外的文件。
            if target.is_symlink() or not target.resolve().is_relative_to(root):
                raise ValueError("拒绝向越界索引或符号链接写入")
            target.write_text(expected, encoding="utf-8")
        elif read_text(root, INDEX) != expected:
            raise ValueError("记忆索引已陈旧：先更新任务笔记/状态，再运行 --write")
        print("PASS: 记忆索引一致；不代表业务或学习验收")
        return 0
    except (ValueError, OSError, UnicodeError, subprocess.SubprocessError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
