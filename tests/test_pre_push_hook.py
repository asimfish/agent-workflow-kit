"""Regression tests for the pre-push hook commit range."""

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

KIT = Path(__file__).resolve().parents[1]
ZERO_SHA = "0000000000000000000000000000000000000000"


class PrePushHookRangeTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="awk-pre-push-range-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.git("init", "-q")
        self.git("config", "user.email", "agent@example.com")
        self.git("config", "user.name", "Agent Test")
        (self.root / "tools").mkdir()
        (self.root / ".githooks").mkdir()
        (self.root / "tools" / "agentctl.py").write_text(
            """#!/usr/bin/env python3
import pathlib
import sys

args = sys.argv[1:]
range_value = args[args.index("--commit-range") + 1]
path = pathlib.Path(".agentctl-ranges")
old = path.read_text(encoding="utf-8") if path.exists() else ""
path.write_text(old + range_value + "\\n", encoding="utf-8")
print("agentctl check (pre-push): OK")
""",
            encoding="utf-8",
        )
        shutil.copy2(KIT / "hooks" / "pre-push", self.root / ".githooks" / "pre-push")

    def git(self, *args):
        proc = subprocess.run(
            ["git", *args],
            cwd=str(self.root),
            text=True,
            capture_output=True,
            timeout=60,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return proc.stdout.strip()

    def commit_file(self, name, content, message):
        path = self.root / name
        path.write_text(content, encoding="utf-8")
        self.git("add", name)
        self.git("commit", "-q", "-m", message)
        return self.git("rev-parse", "HEAD")

    def run_hook(self, local_sha, remote_sha):
        line = f"refs/heads/feature {local_sha} refs/heads/feature {remote_sha}\n"
        proc = subprocess.run(
            [".githooks/pre-push", "origin", "unused-url"],
            cwd=str(self.root),
            input=line,
            text=True,
            capture_output=True,
            timeout=60,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return (self.root / ".agentctl-ranges").read_text(encoding="utf-8").splitlines()[-1]

    def test_new_remote_branch_uses_origin_main_merge_base(self):
        base = self.commit_file("base.txt", "base\n", "legacy message without task id")
        self.git("update-ref", "refs/remotes/origin/main", base)
        self.git("switch", "-q", "-c", "feature/new-range")
        head = self.commit_file("feature.txt", "feature\n", "feat(test): add feature\n\nRefs: T-015")

        seen_range = self.run_hook(head, ZERO_SHA)

        self.assertEqual(seen_range, f"{base}..{head}")

    def test_existing_remote_branch_uses_remote_sha_range(self):
        base = self.commit_file("base.txt", "base\n", "legacy message without task id")
        self.git("update-ref", "refs/remotes/origin/main", base)
        self.git("switch", "-q", "-c", "feature/existing-range")
        remote_sha = self.commit_file("remote.txt", "remote\n", "feat(test): remote\n\nRefs: T-015")
        head = self.commit_file("local.txt", "local\n", "feat(test): local\n\nRefs: T-015")

        seen_range = self.run_hook(head, remote_sha)

        self.assertEqual(seen_range, f"{remote_sha}..{head}")


if __name__ == "__main__":
    unittest.main()
