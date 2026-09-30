"""整链验收日志也需可校验：重复或失败的调用不能污染上次成功证据。"""
from contextlib import redirect_stdout
from hashlib import sha256
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scripts.run_public_chain import main


class PublicChainRunnerTests(unittest.TestCase):
    def test_each_invocation_has_independent_manifest_without_self_hash(self):
        calls = {"collect": 0}
        fail_processing = False

        def run(command, **kwargs):
            if command[:2] == ["git", "rev-parse"]:
                output = "a" * 40
            elif command[:2] == ["git", "status"]:
                output = ""
            elif "collect-public" in command:
                calls["collect"] += 1
                output = json.dumps({"created": calls["collect"] == 1}) + "\n" + json.dumps(
                    {"run": {"id": "same-run", "status": "succeeded"}, "captures": []})
            elif "services.processing" in command:
                if fail_processing:
                    return subprocess.CompletedProcess(command, 4, "{}", "")
                output = json.dumps({"observations": []})
            elif "import" in command:
                output = json.dumps({"added": 0})
            else:
                output = "{}"
            return subprocess.CompletedProcess(command, 0, output, "")

        with tempfile.TemporaryDirectory() as directory, patch("subprocess.run", side_effect=run):
            args = ["--root", directory, "--source", "ccgp", "--category", "zygg/jzxcs", "--key", "same",
                    "--policy", str(Path(directory) / "policy.json")]
            with redirect_stdout(io.StringIO()):
                self.assertEqual(0, main(args))
                self.assertEqual(0, main(args))
                fail_processing = True
                with self.assertRaises(RuntimeError):
                    main(args)
            root = Path(directory) / "runs" / "same-run"
            self.assertEqual(3, len(list(root.iterdir())))
            manifests = list(root.glob("*/manifest.json"))
            self.assertEqual(2, len(manifests))  # 第三次失败没有冒充完成清单。
            for path in manifests:
                manifest = json.loads(path.read_text(encoding="utf-8"))
                self.assertNotIn("manifest.json", manifest["files"])
                for name, digest in manifest["files"].items():
                    self.assertEqual(digest, sha256((path.parent / name).read_bytes()).hexdigest())
                self.assertIn("reused_existing_run", manifest)


if __name__ == "__main__":
    unittest.main()
