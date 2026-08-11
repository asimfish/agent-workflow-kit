"""Canonical task-state and generated-view regression coverage."""

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


class ReconcileWorkflowRegressionTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="awk-reconcile-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True, timeout=60)
        install = subprocess.run(
            [sys.executable, str(KIT / "tools" / "agentctl.py"), "init", str(self.root)],
            cwd=KIT, text=True, capture_output=True, timeout=120,
        )
        self.assertEqual(install.returncode, 0, install.stdout + install.stderr)

    def env(self):
        env = os.environ.copy()
        for name in IDENTITY_ENV:
            env.pop(name, None)
        env["AGENT_WORKFLOW_SESSION_ID"] = "reconcile-session"
        return env

    def agentctl(self, *args, expect=0):
        proc = subprocess.run(
            [sys.executable, "tools/agentctl.py", *args],
            cwd=self.root, env=self.env(), text=True, capture_output=True, timeout=120,
        )
        self.assertEqual(proc.returncode, expect, proc.stdout + proc.stderr)
        return proc

    def create(self, task, title):
        return self.agentctl(
            "task", "create", "--id", task, "--title", title,
            "--owner", "codex", "--scope", f"src/{task}/",
        )

    def test_reverse_consistency_detects_task_missing_from_canonical_board(self):
        self.create("T-201", "canonical task")
        self.create("T-202", "task that merge dropped")
        board_path = self.root / ".agent" / "board.json"
        board = json.loads(board_path.read_text(encoding="utf-8"))
        board["tasks"].pop("T-202")
        board_path.write_text(json.dumps(board, indent=2) + "\n", encoding="utf-8")

        checked = self.agentctl("reconcile", "check", expect=1)
        self.assertIn("T-202", checked.stdout)
        self.assertIn("missing from canonical board", checked.stdout)
        manual = self.agentctl("check", "--mode", "manual", expect=1)
        self.assertIn("T-202", manual.stdout)

    def test_legacy_migration_restores_board_then_render_rebuilds_views(self):
        self.create("T-211", "kept task")
        self.create("T-212", "lost task")
        board_path = self.root / ".agent" / "board.json"
        board = json.loads(board_path.read_text(encoding="utf-8"))
        board["tasks"].pop("T-212")
        board_path.write_text(json.dumps(board, indent=2) + "\n", encoding="utf-8")

        self.agentctl("reconcile", "migrate")
        migrated = json.loads(board_path.read_text(encoding="utf-8"))
        self.assertEqual(migrated["tasks"]["T-212"]["title"], "lost task")
        self.assertEqual(migrated["tasks"]["T-212"]["scope"], ["src/T-212/"])

        tasks = self.root / ".agent" / "TASKS.md"
        tasks.write_text(
            tasks.read_text(encoding="utf-8").replace(
                "| T-212 | todo | codex |",
                "| T-212 | failed | somebody-else |",
            ),
            encoding="utf-8",
        )
        plan = self.root / ".agent" / "PROJECT_PLAN.md"
        plan.write_text(
            plan.read_text(encoding="utf-8").replace(
                "- [ ] T-212 - lost task",
                "- [x] T-212 - stale view",
            ),
            encoding="utf-8",
        )
        drift = self.agentctl("reconcile", "check", expect=1)
        self.assertIn("TASKS.md status failed differs from canonical status todo", drift.stdout)
        self.assertIn("PROJECT_PLAN.md title differs", drift.stdout)

        self.agentctl("reconcile", "render")
        self.agentctl("reconcile", "check")
        self.assertIn("| T-212 | todo | codex |", tasks.read_text(encoding="utf-8"))
        self.assertIn("- [ ] T-212 - lost task", plan.read_text(encoding="utf-8"))

    def test_render_refuses_to_drop_noncanonical_task_evidence(self):
        self.create("T-221", "visible task")
        board_path = self.root / ".agent" / "board.json"
        board = json.loads(board_path.read_text(encoding="utf-8"))
        board["tasks"].pop("T-221")
        board_path.write_text(json.dumps(board, indent=2) + "\n", encoding="utf-8")

        refused = self.agentctl("reconcile", "render", expect=1)
        self.assertIn("would drop task evidence", refused.stderr)
        self.assertIn("reconcile migrate", refused.stderr)

    def test_task_scope_rejects_globs_that_cannot_prove_write_ownership(self):
        refused = self.agentctl(
            "task", "create", "--id", "T-231", "--title", "ambiguous scope",
            "--owner", "codex", "--scope", "pipeline/auto_*gpu*",
            expect=2,
        )
        self.assertIn("uses a glob", refused.stderr)
        board = json.loads(
            (self.root / ".agent" / "board.json").read_text(encoding="utf-8")
        )
        self.assertNotIn("T-231", board["tasks"])


if __name__ == "__main__":
    unittest.main()
