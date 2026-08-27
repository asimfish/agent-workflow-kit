"""Regression coverage for task-id derivation and overwrite protection.

A stale board must never let one task silently re-register or overwrite
another: fresh ids have to respect task documents (live and archived),
live session claims, and unreleased worktree leases, and explicit creation
against a taken id has to fail loudly.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


KIT = Path(__file__).resolve().parents[1]
IDENTITY_ENV = (
    "CODEX_THREAD_ID",
    "CLAUDE_CODE_SESSION_ID",
    "CURSOR_CONVERSATION_ID",
    "WHALENT_AGENT_ID",
    "WHALENT_CODEX_INSTANCE_ID",
    "WHALENT_COMPOSER_ID",
    "WHALENT_FORK_SOURCE_AGENT_ID",
    "AGENT_SESSION_ID",
    "TERM_SESSION_ID",
    "AGENT_WORKFLOW_SESSION_ID",
    "AGENT_WORKFLOW_SESSION_KEY",
    "AGENT_WORKFLOW_SESSION_OWNER_RUNTIME",
    "AGENT_WORKFLOW_SESSION_INSTANCE_ID",
    "AGENT_WORKFLOW_PARENT_SESSION_KEY",
    "AGENT_WORKFLOW_SESSION_ISOLATION_ERROR",
)


class TaskIdentityWorkflowRegressionTest(unittest.TestCase):
    def setUp(self):
        self.temp = Path(tempfile.mkdtemp(prefix="awk-task-id-regress-"))
        self.addCleanup(shutil.rmtree, self.temp, ignore_errors=True)
        self.root = self.temp / "project"
        self.root.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True, timeout=60)
        installed = subprocess.run(
            [sys.executable, str(KIT / "tools" / "agentctl.py"),
             "init", str(self.root)],
            cwd=KIT, text=True, capture_output=True, timeout=120,
        )
        self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)

    def env(self, session):
        env = os.environ.copy()
        for name in IDENTITY_ENV:
            env.pop(name, None)
        env["AGENT_WORKFLOW_SESSION_ID"] = session
        return env

    def agentctl(self, session, *args, expect=0):
        proc = subprocess.run(
            [sys.executable, "tools/agentctl.py", *args],
            cwd=self.root, env=self.env(session), text=True,
            capture_output=True, timeout=120,
        )
        self.assertEqual(proc.returncode, expect, proc.stdout + proc.stderr)
        return proc

    def board_path(self):
        return self.root / ".agent" / "board.json"

    def drop_board_entry(self, task):
        board = json.loads(self.board_path().read_text(encoding="utf-8"))
        board["tasks"].pop(task, None)
        self.board_path().write_text(
            json.dumps(board, indent=2, ensure_ascii=False), encoding="utf-8",
        )

    def drop_session_records(self):
        sessions_dir = self.root / ".git" / "agent-workflow" / "sessions"
        if sessions_dir.is_dir():
            for record in sessions_dir.glob("session-*.json"):
                record.unlink()

    def test_create_refuses_existing_task_document_when_board_is_stale(self):
        self.agentctl(
            "alpha", "work", "--agent", "alpha", "--auto-create",
            "--new-id", "T-DUP-A", "--title", "original task",
            "--scope", "docs/alpha/", "--type", "docs",
        )
        doc = self.root / ".agent" / "tasks" / "T-DUP-A.md"
        original = doc.read_text(encoding="utf-8")
        self.drop_board_entry("T-DUP-A")
        self.drop_session_records()

        rejected = self.agentctl(
            "beta", "task", "create", "--id", "T-DUP-A",
            "--title", "impostor task", "--owner", "beta",
            "--scope", "docs/beta/", "--type", "docs",
            expect=1,
        )
        self.assertIn("board is likely out of date", rejected.stderr)
        self.assertEqual(doc.read_text(encoding="utf-8"), original)
        board = json.loads(self.board_path().read_text(encoding="utf-8"))
        self.assertNotIn("T-DUP-A", board["tasks"])

    def test_create_refuses_id_claimed_by_live_session(self):
        self.agentctl(
            "alpha", "work", "--agent", "alpha", "--auto-create",
            "--new-id", "T-CLAIM-1", "--title", "claimed task",
            "--scope", "docs/alpha/", "--type", "docs",
        )
        # Simulate a board rollback that lost both the entry and the doc
        # while alpha's session still claims the task.
        self.drop_board_entry("T-CLAIM-1")
        (self.root / ".agent" / "tasks" / "T-CLAIM-1.md").unlink()

        rejected = self.agentctl(
            "beta", "task", "create", "--id", "T-CLAIM-1",
            "--title", "impostor task", "--owner", "beta",
            "--scope", "docs/beta/", "--type", "docs",
            expect=1,
        )
        self.assertIn("already claimed", rejected.stderr)

    def test_create_refuses_id_owned_by_archived_task(self):
        self.agentctl(
            "alpha", "work", "--agent", "alpha", "--auto-create",
            "--new-id", "T-ARCH-1", "--title", "archived task",
            "--scope", "docs/alpha/", "--type", "docs",
        )
        self.drop_session_records()
        self.drop_board_entry("T-ARCH-1")
        doc = self.root / ".agent" / "tasks" / "T-ARCH-1.md"
        archive_dir = self.root / ".agent" / "archive" / "tasks"
        archive_dir.mkdir(parents=True)
        doc.rename(archive_dir / "T-ARCH-1.md")

        rejected = self.agentctl(
            "beta", "task", "create", "--id", "T-ARCH-1",
            "--title", "impostor task", "--owner", "beta",
            "--scope", "docs/beta/", "--type", "docs",
            expect=1,
        )
        self.assertIn("already claimed", rejected.stderr)

    def test_auto_ids_skip_documents_missing_from_the_board(self):
        first = self.agentctl(
            "alpha", "work", "--agent", "alpha", "--auto-create",
            "--title", "first auto task",
            "--scope", "docs/alpha/", "--type", "docs",
        )
        created = [
            line for line in (first.stdout + first.stderr).splitlines()
            if "created" in line
        ]
        self.assertTrue(created, first.stdout + first.stderr)
        first_id = created[0].split("created", 1)[1].strip().split()[0]
        self.assertRegex(first_id, r"^T[0-9A-F]+-\d{3}$")

        # Simulate a board that lost the entry (e.g. an unsynced checkout)
        # while the task document survives; the session record is also gone.
        self.drop_board_entry(first_id)
        self.drop_session_records()

        second = self.agentctl(
            "alpha", "work", "--agent", "alpha", "--auto-create",
            "--title", "second auto task",
            "--scope", "docs/alpha2/", "--type", "docs",
        )
        created = [
            line for line in (second.stdout + second.stderr).splitlines()
            if "created" in line
        ]
        self.assertTrue(created, second.stdout + second.stderr)
        second_id = created[0].split("created", 1)[1].strip().split()[0]
        self.assertNotEqual(second_id, first_id)
        prefix, first_num = first_id.rsplit("-", 1)
        second_prefix, second_num = second_id.rsplit("-", 1)
        self.assertEqual(prefix, second_prefix)
        self.assertGreater(int(second_num), int(first_num))
        # The surviving document was never touched.
        doc = self.root / ".agent" / "tasks" / f"{first_id}.md"
        self.assertIn("first auto task", doc.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
