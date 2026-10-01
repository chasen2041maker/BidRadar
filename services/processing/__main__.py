"""processing本地JSON接口；只接收显式输入，不打开ingestion存储。"""
import argparse
import json
from pathlib import Path
import sys

from services.processing.normalize import MAX_JSON_BYTES, normalize_bundle


def main(argv=None):
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
        # 输入可能夹带个人信息，不把原始JSON或系统路径打印进错误日志。
        print(json.dumps({"error": "invalid_evidence_bundle"}))
        return 4


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
