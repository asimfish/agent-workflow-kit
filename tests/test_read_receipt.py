"""Regression coverage for mandatory workflow-document read receipts."""

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


KIT = Path(__file__).resolve().parents[1]


class ReadReceiptRegressionTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="awk-receipt-regress-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        git = subprocess.run(
            ["git", "init", "-q"],
            cwd=str(self.root),
            text=True,
            capture_output=True,
            timeout=60,
        )
        self.assertEqual(git.returncode, 0, git.stdout + git.stderr)
        install = subprocess.run(
            [sys.executable, str(KIT / "tools" / "agentctl.py"), "init", str(self.root)],
            cwd=str(KIT),
            text=True,
            capture_output=True,
            timeout=120,
        )
        self.assertEqual(install.returncode, 0, install.stdout + install.stderr)

    def agentctl(self, *args, expect):
        proc = subprocess.run(
            [sys.executable, "tools/agentctl.py", *args],
            cwd=str(self.root),
            text=True,
            capture_output=True,
            timeout=120,
        )
        self.assertEqual(
            proc.returncode,
            expect,
            f"agentctl {' '.join(args)} rc={proc.returncode}\n{proc.stdout}\n{proc.stderr}",
        )
        return proc

    def test_note_and_finish_require_explicit_refresh_after_instruction_change(self):
        self.agentctl(
            "work", "--agent", "codex", "--auto-create",
            "--title", "receipt regression", "--scope", ".agent/",
            expect=0,
        )
        session = json.loads(self.agentctl("status", "--json", expect=0).stdout)
        required = {
            "AGENTS.md",
            ".agent/WORKFLOW_ENTRY.md",
            ".agent/PROJECT_PLAN.md",
            ".agent/TASKS.md",
            ".agent/agents.json",
            ".agent/rules/agent-operating-rules.md",
            ".agent/rules/github-standards.md",
            ".agent/loops/checkpoints.json",
        }
        self.assertTrue(required.issubset(session["doc_hashes"]), session["doc_hashes"])

        plan = self.root / ".agent" / "PROJECT_PLAN.md"
        plan.write_text(
            plan.read_text(encoding="utf-8")
            + "\n- Human direction changed after the worker started.\n",
            encoding="utf-8",
        )

        note = self.agentctl("note", "must not erase the changed receipt", expect=1)
        self.assertIn("progress blocked", note.stdout + note.stderr)
        finish = self.agentctl(
            "finish", "--summary", "must not finish", "--tests", "not run",
            expect=1,
        )
        self.assertIn("finish blocked", finish.stdout + finish.stderr)

        self.agentctl("refresh", expect=0)
        self.agentctl("note", "re-read human direction and refreshed", expect=0)
        finished = self.agentctl(
            "finish", "--summary", "receipt enforcement verified",
            "--tests", "read receipt regression",
            expect=0,
        )
        self.assertIn("-> review", finished.stdout + finished.stderr)


if __name__ == "__main__":
    unittest.main()
