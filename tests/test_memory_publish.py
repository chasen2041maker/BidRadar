"""发布测试仅模拟 GitHub API；证明边界处理，不冒充真实 Issue 写入。"""
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from scripts.publish_project_memory import MARKER, REPO, publish


class PublishTests(unittest.TestCase):
    def setUp(self):
        self.head = 'a' * 40
        self.env = {'GITHUB_REPOSITORY': REPO, 'GITHUB_EVENT_NAME': 'push', 'GITHUB_REF': 'refs/heads/main', 'GITHUB_SHA': self.head, 'GH_TOKEN': 'fixture-not-a-real-token', 'GITHUB_RUN_ID': '123'}
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, self.env, clear=True).start()
        self.git = patch('scripts.publish_project_memory.git', return_value=self.head).start()
        patch('scripts.publish_project_memory.runtime_report', return_value='测试报告\n').start()
        self.api = patch('scripts.publish_project_memory.api').start()
        self.api.side_effect = [{'object': {'sha': self.head}}, {'state': 'open', 'body': MARKER}, {}]

    def test_main_can_update_only_fixed_issue(self):
        self.assertTrue(publish(Path('.')))
        self.assertEqual(self.api.call_args.args[:2], ('PATCH', f'/repos/{REPO}/issues/4'))
        self.assertIn(self.head, self.api.call_args.args[3]['body'])

    def test_pr_or_other_branch_cannot_publish(self):
        for field, value in (('GITHUB_EVENT_NAME', 'pull_request'), ('GITHUB_REF', 'refs/heads/work'), ('GITHUB_REPOSITORY', 'other/repository')):
            with self.subTest(field=field), patch.dict(os.environ, {field: value}), self.assertRaises(ValueError):
                publish(Path('.'))
        self.api.assert_not_called()

    def test_wrong_checkout_and_missing_token_refused(self):
        for field, value in (('GITHUB_SHA', 'b' * 40), ('GH_TOKEN', '')):
            with self.subTest(field=field), patch.dict(os.environ, {field: value}), self.assertRaises(ValueError):
                publish(Path('.'))
        self.api.assert_not_called()

    def test_stale_job_does_not_overwrite(self):
        self.api.side_effect = [{'object': {'sha': 'b' * 40}}]
        self.assertFalse(publish(Path('.')))
        self.assertEqual(self.api.call_count, 1)

    def test_closed_unmarked_or_pr_target_refused(self):
        for issue in ({'state': 'closed', 'body': MARKER}, {'state': 'open', 'body': 'ordinary'}, {'state': 'open', 'body': MARKER, 'pull_request': {}}):
            with self.subTest(issue=issue):
                self.api.reset_mock()
                self.api.side_effect = [{'object': {'sha': self.head}}, issue]
                with self.assertRaises(ValueError):
                    publish(Path('.'))
                self.assertEqual(self.api.call_count, 2)

    def test_invalid_remote_version_refused(self):
        self.api.side_effect = [{"object": {}}]
        with self.assertRaises(ValueError):
            publish(Path("."))
        self.assertEqual(self.api.call_count, 1)

    def test_missing_run_id_refused(self):
        with patch.dict(os.environ, {"GITHUB_RUN_ID": "not-a-number"}), self.assertRaises(ValueError):
            publish(Path("."))
        self.assertEqual(self.api.call_count, 2)

    def test_api_failure_not_reported_as_success(self):
        self.api.side_effect = OSError('simulated unavailable')
        with self.assertRaises(OSError):
            publish(Path('.'))


if __name__ == '__main__':
    unittest.main()
