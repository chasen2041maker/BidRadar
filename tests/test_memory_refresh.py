"""用临时文件与临时 Git 仓库验证摘要刷新；不碰项目业务数据或外部服务。"""
import contextlib
import io
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scripts.refresh_project_memory import INDEX, INPUTS, SECTIONS, load_notes, main, render_index, require_note_update, runtime_report


class MemoryRefreshTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'docs/learning').mkdir(parents=True)
        for name in INPUTS:
            (self.root / name).write_text('# 状态\n', encoding='utf-8')
        self.note = self.root / 'docs/learning/DOC-001.md'
        self.note.write_text(self.note_text(), encoding='utf-8')

    def note_text(self):
        return '# DOC-001\n\n摘要：只记录可核查知识，不冒称已经学会。\n\n' + '\n'.join('## ' + h + '\n待阅读或参见证据。\n' for h in SECTIONS)

    def cli(self, *args):
        with patch('sys.argv', ['memory', '--root', str(self.root), *args]), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return main()

    def git(self, *args):
        return subprocess.run(['git', '-C', str(self.root), *args], capture_output=True, text=True, check=True).stdout.strip()

    def init_git(self):
        self.git('init', '-q')
        self.git('config', 'user.email', 'fixture@example.invalid')
        self.git('config', 'user.name', 'Isolated test')
        self.commit()
        return self.git('rev-parse', 'HEAD')

    def commit(self):
        self.git('add', '.')
        self.git('commit', '-qm', 'test fixture')

    def test_refresh_is_deterministic_and_idempotent(self):
        self.assertEqual(self.cli('--write'), 0)
        first = (self.root / INDEX).read_bytes()
        self.assertEqual(self.cli('--write'), 0)
        self.assertEqual(first, (self.root / INDEX).read_bytes())
        self.assertEqual(self.cli('--check'), 0)

    def test_state_change_makes_index_stale(self):
        self.cli('--write')
        (self.root / 'PROJECT_STATE.md').write_text('# changed\n', encoding='utf-8')
        self.assertEqual(self.cli('--check'), 1)
        self.assertEqual(self.cli('--write'), 0)
        self.assertEqual(self.cli('--check'), 0)

    def test_note_change_makes_index_stale(self):
        self.cli('--write')
        self.note.write_text(self.note_text() + '\n新证据。\n', encoding='utf-8')
        self.assertEqual(self.cli('--check'), 1)

    def test_empty_summary_rejected(self):
        self.note.write_text(self.note_text().replace('摘要：只记录可核查知识，不冒称已经学会。', '摘要：'), encoding='utf-8')
        self.assertEqual(self.cli('--write'), 1)

    def test_missing_or_empty_section_rejected(self):
        for text in (self.note_text().replace('## 我的参与', '## other'), self.note_text().replace('## 我的参与\n待阅读或参见证据。', '## 我的参与\n')):
            with self.subTest(text=text):
                self.note.write_text(text, encoding='utf-8')
                self.assertEqual(self.cli('--write'), 1)

    def test_bad_utf8_and_missing_input_fail(self):
        self.note.write_bytes(b'\xff')
        self.assertEqual(self.cli('--write'), 1)
        self.note.write_text(self.note_text(), encoding='utf-8')
        (self.root / 'AGENTS.md').unlink()
        self.assertEqual(self.cli('--write'), 1)

    def test_no_notes_and_invalid_names_fail(self):
        self.note.unlink()
        self.assertEqual(self.cli('--write'), 1)
        (self.root / 'docs/learning/not-a-task.md').write_text(self.note_text(), encoding='utf-8')
        self.assertEqual(self.cli('--write'), 1)

    def test_symlink_input_cannot_escape(self):
        with tempfile.TemporaryDirectory() as outside:
            source = Path(outside) / 'private.md'
            source.write_text('private', encoding='utf-8')
            self.note.unlink()
            self.note.symlink_to(source)
            self.assertEqual(self.cli('--write'), 1)

    def test_symlink_output_cannot_escape(self):
        with tempfile.TemporaryDirectory() as outside:
            target = Path(outside) / 'private.md'
            target.write_text('unchanged', encoding='utf-8')
            (self.root / INDEX).symlink_to(target)
            self.assertEqual(self.cli('--write'), 1)
            self.assertEqual(target.read_text(), 'unchanged')

    def test_text_is_escaped_without_changing_learning_status(self):
        self.note.write_text(self.note_text().replace('只记录可核查知识，不冒称已经学会。', '<script>x</script> [fake](x) | 待阅读'), encoding='utf-8')
        text = render_index(self.root)
        self.assertNotIn('<script>', text)
        self.assertNotIn('[fake]', text)
        self.assertIn('待阅读', text)
        self.assertNotIn('已学会', text)

    def test_changes_without_note_fail_then_note_update_passes(self):
        base = self.init_git()
        (self.root / 'example.py').write_text('value = 1\n', encoding='utf-8')
        self.commit()
        with self.assertRaises(ValueError):
            require_note_update(self.root, base)
        self.note.write_text(self.note_text() + '\n说明实现和验证的变化。\n', encoding='utf-8')
        self.commit()
        require_note_update(self.root, base)

    def test_deleting_note_is_not_memory_update(self):
        other = self.root / 'docs/learning/DOC-002.md'
        other.write_text(self.note_text(), encoding='utf-8')
        base = self.init_git()
        other.unlink()
        (self.root / 'example.py').write_text('value = 2\n', encoding='utf-8')
        self.commit()
        with self.assertRaises(ValueError):
            require_note_update(self.root, base)

    def test_bad_ref_is_rejected_without_git(self):
        for ref in ('HEAD', '--help', '0' * 40, '$(touch bad)'):
            with self.subTest(ref=ref), self.assertRaises(ValueError):
                require_note_update(self.root, ref)

    def test_runtime_uses_actual_head_and_never_infers_review(self):
        head = self.init_git()
        with patch.dict(os.environ, {'TESTS_OUTCOME': 'success', 'MEMORY_OUTCOME': 'failure', 'GITHUB_EVENT_NAME': 'push'}):
            report = runtime_report(self.root)
        self.assertIn(head, report)
        self.assertIn('MEMORY_OUTCOME: failure', report)
        self.assertIn('本脚本不判定', report)
        self.assertNotIn('审查通过', report)

    def test_index_is_bounded_without_deleting_sources(self):
        for number in range(2, 40):
            (self.root / f"docs/learning/DOC-{number:03d}.md").write_text(self.note_text(), encoding="utf-8")
        text = render_index(self.root)
        self.assertNotIn("[DOC-001]", text)
        self.assertIn("[DOC-039]", text)
        self.assertEqual(len(load_notes(self.root)), 39)

    def test_runtime_has_no_write_side_effect(self):
        self.init_git()
        self.assertEqual(self.cli('--report'), 0)
        self.assertEqual(self.git('status', '--porcelain'), '')


if __name__ == '__main__':
    unittest.main()
