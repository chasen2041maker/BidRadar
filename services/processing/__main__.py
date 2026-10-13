"""规范化的命令行工具：读入采集服务导出的证据包，输出给目录导入用的规范化观察。

用法：python -m services.processing evidence.json > normalized.json
只处理传进来的 JSON，不去读采集服务的数据库。
"""

import argparse
import json
from pathlib import Path
import sys

from services.processing.normalize import MAX_JSON_BYTES, normalize_bundle


def main(argv=None):
    """读取证据包（文件或标准输入），输出规范化结果。成功返回 0，失败返回 4。"""
    parser = argparse.ArgumentParser(description="证据包→规范观察JSON；默认从stdin读取")
    parser.add_argument("input", nargs="?", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.input:
            with args.input.open("rb") as stream:
                data = stream.read(MAX_JSON_BYTES + 1)
        else:
            data = sys.stdin.buffer.read(MAX_JSON_BYTES + 1)
        if len(data) > MAX_JSON_BYTES:
            raise ValueError("bundle_too_large")
        result = normalize_bundle(json.loads(data.decode("utf-8-sig")))
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (ValueError, TypeError, OSError, RecursionError):
        # 输入里可能有个人信息，出错时不打印原始内容或文件路径。
        print(json.dumps({"error": "invalid_evidence_bundle"}))
        return 4


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
