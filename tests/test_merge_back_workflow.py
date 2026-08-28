"""Regression coverage for reconcile merge-back and its supporting fixes.

Covers the tooled worktree merge-back path (import per-task ledger records
from a branch), the multi-hyphen task-id plan-row regression, the pre-push
archive lookup, and the explicit-create intent guard (B-1).
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tools import agentctl

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


class _KitRepoTestCase(unittest.TestCase):
    """Shared scaffolding: a temp repo with the kit installed."""

    session_id = "merge-back-session"

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="awk-merge-back-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.git("init", "-q")
        self.git("config", "user.email", "agent@example.com")
        self.git("config", "user.name", "Agent Test")
        install = subprocess.run(
            [sys.executable, str(KIT / "tools" / "agentctl.py"), "init", str(self.root)],
            cwd=KIT, text=True, capture_output=True, timeout=120,
        )
        self.assertEqual(install.returncode, 0, install.stdout + install.stderr)

    def git(self, *args, check=True):
        proc = subprocess.run(
            ["git", *args], cwd=str(self.root), text=True,
            capture_output=True, timeout=60,
        )
        if check:
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return proc.stdout.strip()

    def env(self, session=None):
        env = os.environ.copy()
        for name in IDENTITY_ENV:
            env.pop(name, None)
        env["AGENT_WORKFLOW_SESSION_ID"] = session or self.session_id
        return env

    def agentctl(self, *args, expect=0, cwd=None, session=None):
        proc = subprocess.run(
            [sys.executable, "tools/agentctl.py", *args],
            cwd=str(cwd or self.root), env=self.env(session), text=True,
            capture_output=True, timeout=120,
        )
        self.assertEqual(
            proc.returncode, expect,
            f"args={args}\nstdout={proc.stdout}\nstderr={proc.stderr}",
        )
        return proc

    def commit_all(self, message):
        self.git("add", "-A")
        self.git("-c", "core.hooksPath=", "commit", "-q", "-m", message)
        return self.git("rev-parse", "HEAD")


class MergeBackWorkflowTest(_KitRepoTestCase):
    def _start_session(self):
        self.agentctl(
            "work", "--agent", "planner", "--auto-create",
            "--title", "planning ledger upkeep", "--scope", "docs/plan.md",
        )

    def _branch_with_reviewed_task(self, tid="W-100", status="review"):
        """Create a side worktree branch carrying task `tid` at `status`."""
        wt = self.root.parent / f"{self.root.name}-sidewt"
        self.addCleanup(shutil.rmtree, wt, ignore_errors=True)
        self.git("worktree", "add", "-q", "-b", "feature-side", str(wt))
        create = subprocess.run(
            [sys.executable, "tools/agentctl.py", "task", "create",
             "--id", tid, "--title", "side worktree change",
             "--owner", "worker", "--scope", "src/side.py"],
            cwd=str(wt), env=self.env("side-session"), text=True,
            capture_output=True, timeout=120,
        )
        self.assertEqual(create.returncode, 0, create.stdout + create.stderr)
        board_path = wt / ".agent" / "board.json"
        board = json.loads(board_path.read_text(encoding="utf-8"))
        board["tasks"][tid]["status"] = status
        board_path.write_text(json.dumps(board, indent=2), encoding="utf-8")
        doc_path = wt / ".agent" / "tasks" / f"{tid}.md"
        doc_path.write_text(
            doc_path.read_text(encoding="utf-8").replace(
                "Status: todo", f"Status: {status}",
            ),
            encoding="utf-8",
        )
        render = subprocess.run(
            [sys.executable, "tools/agentctl.py", "reconcile", "render"],
            cwd=str(wt), env=self.env("side-session"), text=True,
            capture_output=True, timeout=120,
        )
        self.assertEqual(render.returncode, 0, render.stdout + render.stderr)
        subprocess.run(["git", "add", "-A"], cwd=str(wt), check=True, timeout=60)
        subprocess.run(
            ["git", "-c", "core.hooksPath=", "commit", "-q", "-m",
             "side branch ledger"],
            cwd=str(wt), check=True, timeout=60,
        )
        return tid

    def test_merge_back_imports_reviewed_task_and_refuses_regression(self):
        self._start_session()
        self.commit_all("planning baseline")
        tid = self._branch_with_reviewed_task()

        # dry-run reports the plan without writing anything
        dry = self.agentctl(
            "reconcile", "merge-back", "--from-ref", "feature-side", "--dry-run",
        )
        self.assertIn(f"would merge back {tid} (import as review)", dry.stdout)
        board = json.loads((self.root / ".agent" / "board.json").read_text("utf-8"))
        self.assertNotIn(tid, board["tasks"])

        # real run imports the board entry, the task doc, and the views
        run = self.agentctl("reconcile", "merge-back", "--from-ref", "feature-side")
        self.assertIn(f"merged back {tid} (import as review)", run.stdout)
        board = json.loads((self.root / ".agent" / "board.json").read_text("utf-8"))
        self.assertEqual(board["tasks"][tid]["status"], "review")
        self.assertTrue((self.root / ".agent" / "tasks" / f"{tid}.md").is_file())
        tasks_md = (self.root / ".agent" / "TASKS.md").read_text("utf-8")
        self.assertIn(tid, tasks_md)
        self.agentctl("reconcile", "check")

        # idempotent: a second run has nothing to do
        again = self.agentctl("reconcile", "merge-back", "--from-ref", "feature-side")
        self.assertIn("nothing to merge back", again.stdout)

        # never regress: local status ahead of the source branch stays put
        board_path = self.root / ".agent" / "board.json"
        board = json.loads(board_path.read_text("utf-8"))
        board["tasks"][tid]["status"] = "done"
        board_path.write_text(json.dumps(board, indent=2), encoding="utf-8")
        doc_path = self.root / ".agent" / "tasks" / f"{tid}.md"
        doc_path.write_text(
            doc_path.read_text("utf-8").replace("Status: review", "Status: done"),
            encoding="utf-8",
        )
        self.agentctl("reconcile", "render")
        auto = self.agentctl("reconcile", "merge-back", "--from-ref", "feature-side")
        self.assertIn("nothing to merge back", auto.stdout)
        explicit = self.agentctl(
            "reconcile", "merge-back", "--from-ref", "feature-side",
            "--task", tid, expect=1,
        )
        self.assertIn("ahead of source", explicit.stdout + explicit.stderr)
        board = json.loads(board_path.read_text("utf-8"))
        self.assertEqual(board["tasks"][tid]["status"], "done")

    def test_merge_back_requires_session_and_valid_ref(self):
        # no active session yet
        no_session = self.agentctl(
            "reconcile", "merge-back", "--from-ref", "HEAD", expect=1,
        )
        self.assertIn("requires an active session", no_session.stderr)
        self._start_session()
        self.commit_all("planning baseline")
        bad_ref = self.agentctl(
            "reconcile", "merge-back", "--from-ref", "no-such-branch", expect=2,
        )
        self.assertIn("cannot resolve git ref", bad_ref.stderr)
        unknown = self.agentctl(
            "reconcile", "merge-back", "--from-ref", "HEAD",
            "--task", "NOPE-001", expect=2,
        )
        self.assertIn("not on the HEAD board", unknown.stderr)

    def test_gate_points_to_merge_back_when_task_is_missing(self):
        self._start_session()
        missing = self.agentctl(
            "gate", "approve", "--task", "GHOST-001", "--by", "reviewer",
            expect=2,
        )
        self.assertIn("not found on board", missing.stderr)
        self.assertIn("reconcile merge-back", missing.stderr)

    def test_code_task_refusal_names_the_bare_work_invocation(self):
        self._start_session()
        self.agentctl(
            "task", "create", "--id", "C-900", "--title", "code change",
            "--owner", "worker", "--scope", "src/x.py", "--type", "code",
        )
        refusal = self.agentctl(
            "work", "--agent", "worker", "--task", "C-900",
            session="other-session", expect=1,
        )
        self.assertIn("WITHOUT --task", refusal.stderr)


class ExplicitCreateIntentTest(_KitRepoTestCase):
    session_id = "intent-session"

    def test_explicit_create_does_not_silently_resume_other_work(self):
        first = self.agentctl(
            "work", "--agent", "w1", "--auto-create",
            "--title", "first work", "--scope", "docs/a.md",
        )
        self.assertIn("first work", first.stdout)
        board = json.loads((self.root / ".agent" / "board.json").read_text("utf-8"))
        active = next(
            tid for tid, t in board["tasks"].items()
            if t.get("title") == "first work"
        )

        # a different creation request must not silently resume the active task
        refused = self.agentctl(
            "work", "--agent", "w1", "--auto-create",
            "--title", "second work", "--scope", "docs/b.md", expect=1,
        )
        self.assertIn("refusing to silently resume", refused.stderr)
        self.assertIn(active, refused.stderr)

        # a matching creation request may resume (idempotent retry)
        resumed = self.agentctl(
            "work", "--agent", "w1", "--auto-create",
            "--title", "first work", "--scope", "docs/a.md",
        )
        self.assertIn("resuming active task", resumed.stdout)


class PlanRowRegexTest(_KitRepoTestCase):
    session_id = "plan-row-session"

    def test_multi_hyphen_task_id_round_trips_through_views(self):
        rows = agentctl._plan_task_rows(
            "- [x] TR024-REVIEW-001 - independent review (owner: rev)\n"
        )
        self.assertIn("TR024-REVIEW-001", rows)
        self.assertEqual(rows["TR024-REVIEW-001"]["title"], "independent review")

        # end to end: a rendered row for a multi-hyphen id must reconcile
        self.agentctl(
            "task", "create", "--id", "TR900-REVIEW-001",
            "--title", "review with a multi-hyphen id",
            "--owner", "rev", "--scope", ".agent/",
        )
        self.agentctl("reconcile", "check")


class PrePushArchiveLookupTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="awk-prepush-archive-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True, timeout=60)
        subprocess.run(
            ["git", "config", "user.email", "agent@example.com"],
            cwd=self.root, check=True, timeout=60,
        )
        subprocess.run(
            ["git", "config", "user.name", "Agent Test"],
            cwd=self.root, check=True, timeout=60,
        )
        (self.root / ".agent").mkdir()
        (self.root / ".agent" / "board.json").write_text(
            json.dumps({"version": 1, "tasks": {}}), encoding="utf-8",
        )

    def _commit(self, name, message):
        (self.root / name).write_text("x\n", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=self.root, check=True, timeout=60)
        subprocess.run(
            ["git", "commit", "-q", "-m", message],
            cwd=self.root, check=True, timeout=60,
        )
        return subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=self.root, check=True,
            capture_output=True, text=True, timeout=60,
        ).stdout.strip()

    def _write_archive(self, status):
        archive = self.root / ".agent" / "archive"
        archive.mkdir(exist_ok=True)
        (archive / "board.json").write_text(
            json.dumps({"version": 1, "tasks": {"T-777": {"status": status}}}),
            encoding="utf-8",
        )

    def test_archived_done_reference_is_accepted(self):
        base = self._commit("base.txt", "chore(test): baseline\n\nRefs: T-000")
        head = self._commit(
            "change.txt", "chore(test): archive ledger\n\nRefs: T-777",
        )
        commit_range = f"{base}..{head}"

        # without an archive entry the reference is refused
        problems = "\n".join(agentctl._check_prepush(self.root, commit_range))
        self.assertIn("T-777", problems)
        self.assertIn("archive", problems)

        # an archived done task is a legitimate reference
        self._write_archive("done")
        problems = "\n".join(agentctl._check_prepush(self.root, commit_range))
        self.assertNotIn("T-777", problems)

        # an archived entry that never reached a pushable status still fails
        self._write_archive("todo")
        problems = "\n".join(agentctl._check_prepush(self.root, commit_range))
        self.assertIn("T-777", problems)


if __name__ == "__main__":
    unittest.main()
