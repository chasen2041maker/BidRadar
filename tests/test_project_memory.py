"""Isolated positive and negative tests; never touch project or production data."""
from pathlib import Path
import tempfile
import unittest

from scripts.check_project_memory import HEADINGS, LIMITS, REQUIRED, check


class ProjectMemoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        for name in (*LIMITS, *REQUIRED):
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            text = "# Test\n" + "".join(h + "\n" for h in HEADINGS.get(name, ()))
            path.write_text(text, encoding="utf-8")
        self.write("CLAUDE.md", "@AGENTS.md\n")
        self.write(".agents/skills/bidradar-handoff/SKILL.md", "---\nname: bidradar-handoff\ndescription: Test handoff\n---\n")

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, name, text):
        (self.root / name).write_text(text, encoding="utf-8")

    def test_valid(self):
        self.assertEqual(check(self.root), [])

    def test_missing_file(self):
        (self.root / "CURRENT_TASK.md").unlink()
        self.assertTrue(any("missing: CURRENT_TASK.md" in e for e in check(self.root)))

    def test_chinese_bytes_not_characters(self):
        self.write("AGENTS.md", "中" * (LIMITS["AGENTS.md"] // 3 + 1) + "\n")
        self.assertTrue(any("oversize" in e for e in check(self.root)))

    def test_missing_heading(self):
        self.write("PROJECT_STATE.md", "# empty\n")
        self.assertTrue(any("missing heading" in e for e in check(self.root)))

    def test_broken_link(self):
        self.write("README.md", "[missing](does-not-exist.md)\n")
        self.assertTrue(any("broken local file link" in e for e in check(self.root)))

    def test_path_escape(self):
        self.write("README.md", "[outside](../outside.md)\n")
        self.assertTrue(any("escapes repository" in e for e in check(self.root)))

    def test_links_in_fences_ignored(self):
        self.write("README.md", "```text\n[example](not-a-file.md)\n```\n")
        self.assertEqual(check(self.root), [])

    def test_external_and_anchor_links_not_validated(self):
        self.write("README.md", "[external](https://example.invalid) [anchor](#x)\n")
        self.assertEqual(check(self.root), [])

    def test_unclosed_fence(self):
        self.write("README.md", "```text\nunclosed\n")
        self.assertTrue(any("unclosed" in e for e in check(self.root)))

    def test_info_string_is_not_a_closing_fence(self):
        self.write("README.md", "```text\n```python\n")
        self.assertTrue(any("unclosed" in e for e in check(self.root)))

    def test_links_after_false_closer_remain_in_code(self):
        self.write("README.md", "```text\n```python\n[example](missing.md)\n```\n")
        self.assertEqual(check(self.root), [])

    def test_tilde_info_string_is_not_a_closing_fence(self):
        self.write("README.md", "~~~text\n~~~python\n")
        self.assertTrue(any("unclosed" in e for e in check(self.root)))

    def test_closing_fence_may_have_trailing_space_or_tab(self):
        self.write("README.md", "```text\n[example](missing.md)\n``` \t\n")
        self.assertEqual(check(self.root), [])

    def test_deeply_indented_fence_does_not_close(self):
        for indent in ("    ", "        ", "\t", " \t"):
            with self.subTest(indent=repr(indent)):
                self.write("README.md", "```text\n" + indent + "```\n")
                self.assertTrue(any("unclosed" in e for e in check(self.root)))

    def test_deeply_indented_marker_does_not_open(self):
        for indent in ("    ", "        ", "\t", " \t"):
            with self.subTest(indent=repr(indent)):
                self.write("README.md", "# Title\n\n" + indent + "```text\n")
                self.assertEqual(check(self.root), [])

    def test_zero_to_three_spaces_allow_fences(self):
        for opening in range(4):
            for closing in range(4):
                with self.subTest(opening=opening, closing=closing):
                    self.write("README.md", " " * opening + "```text\n[code](missing.md)\n" + " " * closing + "```\n")
                    self.assertEqual(check(self.root), [])

    def test_deep_false_closer_keeps_links_inside_code(self):
        for indent in ("    ", "\t"):
            with self.subTest(indent=repr(indent)):
                self.write("README.md", "```text\n" + indent + "```\n[code](missing.md)\n```\n")
                self.assertEqual(check(self.root), [])

    def test_backtick_info_cannot_contain_backtick(self):
        self.write("README.md", "```lang`invalid\n")
        self.assertEqual(check(self.root), [])

    def test_invalid_backtick_fence_checks_same_line_link(self):
        self.write("README.md", "```lang`invalid [missing](does-not-exist.md)\n")
        self.assertTrue(any("broken local file link" in e for e in check(self.root)))

    def test_invalid_backtick_fence_allows_valid_same_line_link(self):
        self.write("README.md", "```lang`invalid [state](PROJECT_STATE.md)\n")
        self.assertEqual(check(self.root), [])

    def test_invalid_backtick_fence_checks_same_line_escape(self):
        self.write("README.md", "```lang`invalid [outside](../outside.md)\n")
        self.assertTrue(any("escapes repository" in e for e in check(self.root)))

    def test_tilde_info_may_contain_backtick(self):
        self.write("README.md", "~~~lang`valid\n[code](missing.md)\n~~~\n")
        self.assertEqual(check(self.root), [])

    def test_other_fence_type_does_not_close(self):
        self.write("README.md", "```text\n~~~\n")
        self.assertTrue(any("unclosed" in e for e in check(self.root)))

    def test_shorter_fence_does_not_close(self):
        self.write("README.md", "````text\n```\n")
        self.assertTrue(any("unclosed" in e for e in check(self.root)))

    def test_longer_fence_can_close(self):
        self.write("README.md", "```text\n[code](missing.md)\n````\n")
        self.assertEqual(check(self.root), [])

    def test_claude_must_import_rules(self):
        self.write("CLAUDE.md", "# duplicated policies\n")
        self.assertTrue(any("must import" in e for e in check(self.root)))

    def test_skill_metadata(self):
        self.write(".agents/skills/bidradar-handoff/SKILL.md", "# no metadata\n")
        self.assertTrue(any("front matter" in e for e in check(self.root)))

    def test_skill_metadata_accepts_lf_and_crlf(self):
        # 明确写入字节，让 Linux CI 也覆盖 Windows 行尾，不依赖运行平台。
        path = self.root / ".agents/skills/bidradar-handoff/SKILL.md"
        for newline in (b"\n", b"\r\n"):
            with self.subTest(newline=newline):
                path.write_bytes(newline.join((b"---", b"name: bidradar-handoff",
                                              b"description: Test handoff", b"---", b"")))
                self.assertEqual(check(self.root), [])

    def test_skill_metadata_rejects_invalid_description(self):
        # 接受 CRLF 不应让空描述、裸 CR 或额外换行冒充非空单行元数据。
        path = self.root / ".agents/skills/bidradar-handoff/SKILL.md"
        for description in (b"", b"Test\rinjected", b"Test\r", b"Test\r\ninjected"):
            with self.subTest(description=description):
                path.write_bytes(b"---\r\nname: bidradar-handoff\r\ndescription: "
                                 + description + b"\r\n---\r\n")
                self.assertTrue(any("front matter" in e for e in check(self.root)))

    def test_crlf_safety_checks_remain_active(self):
        cases = (
            (b"# Title\r\n\x00\r\n", "NUL character"),
            (b"<<<<<<< HEAD\r\nconflict\r\n", "merge conflict"),
            (b"[outside](../outside.md)\r\n", "escapes repository"),
            (b"```text\r\nunclosed\r\n", "unclosed code fence"),
        )
        for content, expected in cases:
            with self.subTest(expected=expected):
                (self.root / "README.md").write_bytes(content)
                self.assertTrue(any(expected in e for e in check(self.root)))

    def test_byte_limit_includes_crlf_bytes(self):
        # 按实际 UTF-8 字节计数：边界处通过，多一个字节也必须拒绝。
        path = self.root / "AGENTS.md"
        limit = LIMITS["AGENTS.md"]
        for newline in (b"\n", b"\r\n"):
            with self.subTest(newline=newline):
                content = "中".encode("utf-8") + b"x" * (limit - 3 - len(newline)) + newline
                path.write_bytes(content)
                self.assertEqual(check(self.root), [])
                path.write_bytes(b"x" + content)
                self.assertIn(f"oversize: AGENTS.md: {limit + 1} > {limit} bytes", check(self.root))

    def test_crlf_invalid_utf8_remains_rejected(self):
        (self.root / "README.md").write_bytes(b"# Title\r\n\xff\r\n")
        self.assertTrue(any("unreadable UTF-8" in e for e in check(self.root)))

    def test_crlf_missing_final_newline_remains_rejected(self):
        (self.root / "README.md").write_bytes(b"# Title\r\nno final newline")
        self.assertTrue(any("missing final newline" in e for e in check(self.root)))

    def test_conflict_marker(self):
        self.write("README.md", "<<<<<<< HEAD\nconflict\n")
        self.assertTrue(any("merge conflict" in e for e in check(self.root)))

    def test_invalid_utf8(self):
        (self.root / "README.md").write_bytes(b"\xff")
        self.assertTrue(any("unreadable UTF-8" in e for e in check(self.root)))

    def test_valid_relative_file_link(self):
        self.write("README.md", "[state](PROJECT_STATE.md)\n")
        self.assertEqual(check(self.root), [])


if __name__ == "__main__":
    unittest.main()
