"""Validate the small, portable project handoff (Python standard library only).

Checks structure, local Markdown file links and size; it does not verify business
correctness, remote URLs, Markdown anchors, permissions, or independent review.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import re
import sys
from urllib.parse import unquote, urlsplit

LIMITS = {"AGENTS.md": 12288, "PROJECT_STATE.md": 6144, "CURRENT_TASK.md": 5120}
REQUIRED = (
    "README.md", "CLAUDE.md", "docs/20-development-standard.md",
    "docs/21-context-management.md", "docs/22-cms-chinese-acceptance.md",
    ".agents/skills/bidradar-handoff/SKILL.md",
)
HEADINGS = {
    "PROJECT_STATE.md": ("## 当前阶段", "## 已确认", "## 待验证", "## 证据入口"),
    "CURRENT_TASK.md": ("## 目标", "## 范围", "## 验收", "## 未执行与阻塞", "## 下一步"),
}
LINK = re.compile(r"!?\[[^\]\n]*\]\(([^\s)]+)(?:\s+\"[^\"]*\")?\)")
FENCE = re.compile(r"^\s*(`{3,}|~{3,})")


def check(root: Path) -> list[str]:
    """Return errors without writing files or making network requests."""
    root = root.resolve()
    errors: list[str] = []
    for name in (*LIMITS, *REQUIRED):
        if not (root / name).is_file():
            errors.append(f"missing: {name}")
    texts: dict[str, str] = {}
    # Only our maintained handoff files are size-gated; other docs remain on-demand.
    for name in (*LIMITS, *REQUIRED):
        path = root / name
        if not path.is_file():
            continue
        try:
            data = path.read_bytes()
            text = data.decode("utf-8")
        except (OSError, UnicodeError) as exc:
            errors.append(f"unreadable UTF-8: {name}: {type(exc).__name__}")
            continue
        texts[name] = text
        if name in LIMITS and len(data) > LIMITS[name]:
            errors.append(f"oversize: {name}: {len(data)} > {LIMITS[name]} bytes")
        if not text.endswith("\n"):
            errors.append(f"missing final newline: {name}")
        if "\x00" in text:
            errors.append(f"NUL character: {name}")
        if re.search(r"^(<<<<<<< |=======\s*$|>>>>>>> )", text, re.M):
            errors.append(f"merge conflict marker: {name}")
        for heading in HEADINGS.get(name, ()):
            if heading not in text.splitlines():
                errors.append(f"missing heading: {name}: {heading}")
        fence_char = None
        fence_len = 0
        for line in text.splitlines():
            match = FENCE.match(line)
            if match:
                token = match.group(1)
                if fence_char is None:
                    fence_char, fence_len = token[0], len(token)
                elif (
                    token[0] == fence_char
                    and len(token) >= fence_len
                    and not line[match.end():].strip(" \t")
                ):
                    # A closing fence cannot have an info string or other text.
                    fence_char = None
                continue
            if fence_char is not None:
                continue
            for raw in LINK.findall(line):
                parsed = urlsplit(raw)
                if parsed.scheme or parsed.netloc or not parsed.path:
                    continue
                target = (path.parent / unquote(parsed.path)).resolve()
                if not target.is_relative_to(root):
                    errors.append(f"link escapes repository: {name}: {raw}")
                elif not target.exists():
                    errors.append(f"broken local file link: {name}: {raw}")
        if fence_char is not None:
            errors.append(f"unclosed code fence: {name}")
    if "CLAUDE.md" in texts and "@AGENTS.md" not in texts["CLAUDE.md"].splitlines():
        errors.append("CLAUDE.md must import @AGENTS.md rather than duplicate rules")
    skill = texts.get(".agents/skills/bidradar-handoff/SKILL.md")
    if skill is not None and not re.match(r"\A---\nname: bidradar-handoff\ndescription: [^\n]+\n---\n", skill):
        errors.append("handoff skill needs name and description front matter")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    errors = check(args.root)
    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 1
    print("PASS: handoff structure, UTF-8, file links and size limits; not business validation")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
