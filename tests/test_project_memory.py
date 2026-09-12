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

    def test_claude_must_import_rules(self):
        self.write("CLAUDE.md", "# duplicated policies\n")
        self.assertTrue(any("must import" in e for e in check(self.root)))

    def test_skill_metadata(self):
        self.write(".agents/skills/bidradar-handoff/SKILL.md", "# no metadata\n")
        self.assertTrue(any("front matter" in e for e in check(self.root)))

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
