"""Launcher tests use an isolated fake uv, never an actual server or credentials."""

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


class StartTests(unittest.TestCase):
    def test_launch_from_another_directory_and_forward_arguments(self):
        source = Path(__file__).resolve().parents[1] / "start.sh"
        with tempfile.TemporaryDirectory(prefix="opencode-ci-start-", dir=Path.home() / ".cache") as temporary:
            root = Path(temporary)
            checkout = root / "checkout with spaces"
            checkout.mkdir()
            script = checkout / "start.sh"
            shutil.copyfile(source, script)
            binary = root / "bin"
            binary.mkdir()
            uv = binary / "uv"
            uv.write_text(
                "#!/usr/bin/env python3\n"
                "import json, os, sys\n"
                "with open(os.environ['START_TEST_CAPTURE'], 'w') as f:\n"
                "    json.dump({'args': sys.argv[1:], 'no_bytecode': os.environ.get('PYTHONDONTWRITEBYTECODE')}, f)\n"
            )
            uv.chmod(0o755)
            capture = root / "captured.json"
            env = {**os.environ, "PATH": str(binary) + os.pathsep + os.environ.get("PATH", ""),
                   "START_TEST_CAPTURE": str(capture)}
            subprocess.run([shutil.which("bash"), str(script), "--port", "9876"], cwd=root, env=env, check=True, capture_output=True)
            result = json.loads(capture.read_text())
            self.assertEqual(result["args"], ["run", "--no-project", "--directory", str(checkout),
                             "--with", str(checkout), "python", "-B", "-m", "opencode_ci", "serve", "--port", "9876"])
            self.assertEqual(result["no_bytecode"], "1")
            self.assertEqual([p.name for p in checkout.iterdir()], ["start.sh"])

    def test_clear_error_when_uv_is_missing(self):
        script = Path(__file__).resolve().parents[1] / "start.sh"
        with tempfile.TemporaryDirectory(prefix="opencode-ci-no-uv-", dir=Path.home() / ".cache") as directory:
            result = subprocess.run([shutil.which("bash"), str(script)], env={**os.environ, "PATH": directory}, capture_output=True, text=True)
        self.assertEqual(result.returncode, 127)
        self.assertIn("uv is required", result.stderr)


if __name__ == "__main__":
    unittest.main()
