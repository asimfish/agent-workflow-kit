"""Milestones: the plan as a DAG that closes itself from the leaves up.

A milestone is a task whose Definition of Done is "every task it depends on
is done". Nobody claims it; the tool closes it the moment its last child
closes, the way a proof-sketch is proved once every lemma it imports is.
These tests pin the rules:

- created with `task create --type milestone --deps ...`, or grown by
  children naming `--parent`; scope defaults to `.agent/`
- `work`/`start` refuse to claim it and name its open children; the
  auto-selector skips it
- it cascades to done on every path a task reaches done: the gate, a review
  task's own finish, the closure sweep, a merge-back import, a sync pull
- a dependency that would form a cycle is refused
- `board` shows children done over total; `board --tree` renders the DAG
"""

import json
import os
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


class _MilestoneTestCase(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="awk-milestone-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.git("init", "-q")
        self.git("config", "user.email", "agent@example.com")
        self.git("config", "user.name", "Agent Test")
        install = subprocess.run(
            [sys.executable, str(KIT / "tools" / "agentctl.py"), "init", str(self.root)],
            cwd=KIT, text=True, capture_output=True, timeout=120,
        )
        self.assertEqual(install.returncode, 0, install.stdout + install.stderr)

    def git(self, *args, cwd=None, check=True):
        proc = subprocess.run(
            ["git", *args], cwd=str(cwd or self.root), text=True, capture_output=True, timeout=60,
        )
        if check:
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return proc

    def env(self, session=None, **extra):
        env = os.environ.copy()
        for name in IDENTITY_ENV:
            env.pop(name, None)
        env["AGENT_WORKFLOW_RESOURCE_LOCK_DIR"] = str(self.root / ".resource-locks")
        if session:
            env["AGENT_WORKFLOW_SESSION_ID"] = session
            env["CODEX_THREAD_ID"] = f"thread-{session}"
        env.update(extra)
        return env

    def agentctl(self, *args, expect=0, session=None, cwd=None, **extra):
        proc = subprocess.run(
            [sys.executable, "tools/agentctl.py", *args],
            cwd=str(cwd or self.root), env=self.env(session, **extra), text=True,
            capture_output=True, timeout=180,
        )
        self.assertEqual(
            proc.returncode, expect,
            f"args={args}\nstdout={proc.stdout}\nstderr={proc.stderr}",
        )
        return proc

    def board(self, root=None):
        return json.loads(((root or self.root) / ".agent" / "board.json").read_text(encoding="utf-8"))

    def status(self, task, root=None):
        return self.board(root)["tasks"][task]["status"]

    def milestone(self, task_id, title, deps=""):
        args = ["task", "create", "--id", task_id, "--type", "milestone", "--title", title]
        if deps:
            args += ["--deps", deps]
        self.agentctl(*args, session="supervisor")

    def child(self, task_id, parent, session, title=None):
        self.agentctl(
            "work", "--agent", "codex", "--auto-create", "--new-id", task_id,
            "--title", title or task_id, "--scope", f"src/{task_id.lower()}/",
            "--done", f"{task_id} done", "--parent", parent, session=session,
        )

    def finish(self, session):
        self.agentctl("finish", "--summary", "done", "--tests", "unit", session=session)

    def reviewer(self, name="reviewer", scope=".agent/gates/"):
        self.agentctl("agents", "add", "--id", name, "--role", "review", session=name)
        self.agentctl(
            "work", "--agent", name, "--auto-create", "--type", "review",
            "--title", f"review by {name}", "--scope", scope, session=name,
        )
        self.agentctl("refresh", session=name)
        return name

    def approve(self, task, reviewer="reviewer"):
        self.agentctl("refresh", session=reviewer)
        return self.agentctl("gate", "approve", "--task", task, "--by", reviewer, "--note", "ok", session=reviewer)


class MilestoneShapeTest(_MilestoneTestCase):
    def test_created_with_deps_or_grown_by_parent(self):
        self.milestone("M-1", "paper: all experiments")
        entry = self.board()["tasks"]["M-1"]
        self.assertEqual(entry["type"], "milestone")
        self.assertEqual(entry["scope"], [".agent/"])
        self.assertEqual(entry["deps"], [])
        self.assertIn("every task this milestone depends on is done", agentctl._task_contract(self.root, "M-1")["done"])

        self.child("E-1", "M-1", "a")
        self.child("E-2", "M-1", "b")
        self.assertEqual(self.board()["tasks"]["M-1"]["deps"], ["E-1", "E-2"])

        self.milestone("M-2", "the whole paper", deps="M-1")
        self.assertEqual(self.board()["tasks"]["M-2"]["deps"], ["M-1"])

    def test_parent_must_be_an_open_milestone(self):
        self.milestone("M-1", "m")
        refused = self.agentctl(
            "work", "--agent", "codex", "--auto-create", "--new-id", "E-9", "--title", "x",
            "--scope", "src/x/", "--done", "x", "--parent", "M-9", expect=2, session="a",
        )
        self.assertIn("is not on the board", refused.stderr)
        self.agentctl(
            "work", "--agent", "codex", "--auto-create", "--new-id", "E-1", "--title", "leaf",
            "--scope", "src/e1/", "--done", "x", session="a",
        )
        refused = self.agentctl(
            "task", "create", "--id", "E-2", "--title", "x", "--scope", "src/e2/",
            "--parent", "E-1", expect=2, session="supervisor",
        )
        self.assertIn("not a milestone", refused.stderr)
        self.assertNotIn("E-2", self.board()["tasks"])

    def test_a_milestone_cannot_be_claimed_and_is_never_auto_selected(self):
        self.milestone("M-1", "m")
        self.child("E-1", "M-1", "a")
        refused = self.agentctl("work", "--agent", "codex", "--task", "M-1", expect=1, session="c")
        self.assertIn("is a milestone; it is not worked", refused.stderr)
        self.assertIn("Open children: E-1", refused.stderr)
        refused = self.agentctl("start", "--task", "M-1", "--agent", "codex", expect=1, session="c")
        self.assertIn("is a milestone", refused.stderr)
        # The only todo tasks are T-000 (scope .agent/, owner supervisor) and
        # the milestone; a fresh codex session must not be handed the milestone.
        selected = self.agentctl("work", "--agent", "codex", expect=1, session="d")
        self.assertIn("no ready/todo task assigned", selected.stdout)

    def test_cycles_are_refused(self):
        self.milestone("M-1", "m")
        self.child("E-1", "M-1", "a")
        # E-1 is a child of M-1; a task that depends on M-1 cannot also be its child.
        refused = self.agentctl(
            "task", "create", "--id", "E-2", "--title", "x", "--scope", "src/e2/",
            "--deps", "M-1", "--parent", "M-1", expect=2, session="supervisor",
        )
        self.assertIn("cannot be both a child and a dependency", refused.stderr)
        # M-2 depends on M-1; making M-2 a child of M-1 would close the loop.
        self.milestone("M-2", "m2", deps="M-1")
        refused = self.agentctl(
            "task", "create", "--id", "M-3", "--type", "milestone", "--title", "m3",
            "--deps", "M-2", "--parent", "M-1", expect=2, session="supervisor",
        )
        self.assertIn("would form a cycle", refused.stderr)
        self.assertNotIn("M-3", self.board()["tasks"])
        self.assertIsNone(agentctl._dependency_cycle(self.board(), "M-1", ["E-1"]))
        self.assertEqual(agentctl._dependency_cycle(self.board(), "E-1", ["M-2"]), ["E-1", "M-2", "M-1", "E-1"])


class CascadeTest(_MilestoneTestCase):
    def test_closes_on_the_last_gate_approval_recursively(self):
        self.milestone("M-1", "experiments")
        self.milestone("M-0", "the paper", deps="M-1")
        self.child("E-1", "M-1", "a")
        self.child("E-2", "M-1", "b")
        self.finish("a")
        self.finish("b")
        self.reviewer()
        first = self.approve("E-1")
        self.assertNotIn("milestone", first.stdout)
        self.assertEqual(self.status("M-1"), "todo")
        second = self.approve("E-2")
        self.assertIn("milestone M-1 closed: all children done", second.stdout)
        self.assertIn("milestone M-0 closed: all children done", second.stdout)
        self.assertEqual(self.status("M-1"), "done")
        self.assertEqual(self.status("M-0"), "done")
        doc = (self.root / ".agent" / "tasks" / "M-1.md").read_text(encoding="utf-8")
        self.assertIn("Status: done", doc)
        self.assertIn("- Summary: all 2 children done: E-1, E-2", doc)
        self.assertIn("- Completed-at:", doc)
        self.assertEqual(agentctl._completion_record_problem(doc), "")
        plan = (self.root / ".agent" / "PROJECT_PLAN.md").read_text(encoding="utf-8")
        self.assertIn("- [x] M-1 - experiments", plan)
        self.assertIn("- [x] M-0 - the paper", plan)
        self.assertEqual(self.agentctl("check", "--mode", "ci", session="supervisor").returncode, 0)

    def test_a_childless_milestone_never_closes_itself(self):
        self.milestone("M-1", "m")
        self.milestone("M-0", "root", deps="M-1")
        self.agentctl(
            "work", "--agent", "codex", "--auto-create", "--new-id", "E-1", "--title", "e",
            "--scope", "src/e1/", "--done", "x", session="a",
        )
        self.finish("a")
        self.reviewer()
        self.approve("E-1")
        self.assertEqual(self.status("M-1"), "todo")
        self.assertEqual(self.status("M-0"), "todo")

    def attach(self, milestone, task):
        board_path = self.root / ".agent" / "board.json"
        board = self.board()
        board["tasks"][milestone]["deps"].append(task)
        board_path.write_text(json.dumps(board, indent=2) + "\n", encoding="utf-8")

    def test_closes_when_a_review_task_finishes_the_last_child(self):
        # A review task is a child too; its own finish (closing on its recorded
        # decision) is a done-transition the cascade must see.
        self.milestone("M-1", "m")
        self.child("E-1", "M-1", "a")
        self.finish("a")
        reviewer = self.reviewer()
        review_task = next(t for t, e in self.board()["tasks"].items() if e.get("type") == "review")
        self.attach("M-1", review_task)
        self.approve("E-1")
        self.assertEqual(self.status("M-1"), "todo")  # the review task is still open
        finished = self.agentctl("finish", "--summary", "reviewed", session=reviewer)
        self.assertIn("-> done", finished.stdout)
        self.assertIn("milestone M-1 closed", finished.stdout)
        self.assertEqual(self.status("M-1"), "done")

    def test_closes_through_the_closure_sweep(self):
        self.milestone("M-1", "m")
        self.child("E-1", "M-1", "a")
        self.finish("a")
        reviewer = self.reviewer()
        review_task = next(t for t, e in self.board()["tasks"].items() if e.get("type") == "review")
        self.attach("M-1", review_task)
        self.approve("E-1")
        self.assertEqual(self.status("M-1"), "todo")
        # Park the review task in `review` with its decision recorded and let
        # the sweep close it: the sweep's done must cascade too.
        board = self.board()
        board["tasks"][review_task]["status"] = "review"
        (self.root / ".agent" / "board.json").write_text(json.dumps(board, indent=2) + "\n", encoding="utf-8")
        agentctl._set_task_doc_status(self.root, review_task, "review")
        self.agentctl("refresh", session=reviewer)
        swept = self.agentctl("reconcile", "close-decided-reviews", session=reviewer)
        self.assertIn(f"{review_task} -> done", swept.stdout)
        self.assertIn("milestone M-1 closed", swept.stdout)
        self.assertEqual(self.status("M-1"), "done")


class MergeBackAndSyncCascadeTest(_MilestoneTestCase):
    """The last child may close on a branch or another machine."""

    def adopt_and_commit(self, message="chore(agent): adopt agent-workflow-kit"):
        human = self.env(TERM_SESSION_ID="w0t0p0")
        subprocess.run(["git", "add", *agentctl.ADOPTION_COMMIT_ADD_PATHS], cwd=str(self.root), env=human, check=True, capture_output=True)
        committed = subprocess.run(["git", "commit", "-q", "-m", message], cwd=str(self.root), env=human, text=True, capture_output=True)
        self.assertEqual(committed.returncode, 0, committed.stdout + committed.stderr)

    def ledger_commit(self, root, message):
        subprocess.run(["git", "add", "-A", ".agent"], cwd=str(root), check=True, capture_output=True)
        subprocess.run(
            ["git", "-c", "core.hooksPath=/dev/null", "commit", "-q", "-m", message],
            cwd=str(root), check=True, capture_output=True,
        )

    def test_merge_back_of_the_last_child_closes_the_milestone(self):
        self.adopt_and_commit()
        self.milestone("M-1", "m")
        self.child("E-1", "M-1", "a")
        self.ledger_commit(self.root, "chore(ledger): milestone and child\n\nRefs: M-1, E-1")
        # The child is finished and approved on a branch, as a worktree task would be.
        self.git("checkout", "-q", "-b", "feature/E-1")
        self.finish("a")
        reviewer = self.reviewer()
        self.approve("E-1")
        self.assertEqual(self.status("M-1"), "done")  # cascaded on the branch too
        # Rewind the milestone on the branch so the planning side has to cascade on import.
        self.ledger_commit(self.root, "chore(ledger): E-1 done\n\nRefs: E-1, M-1")
        self.git("checkout", "-q", "-")
        self.assertEqual(self.status("M-1"), "todo")
        self.assertEqual(self.status("E-1"), "in_progress")
        self.agentctl("agents", "add", "--id", "planner", "--role", "planning", session="planner")
        self.agentctl(
            "work", "--agent", "planner", "--auto-create", "--type", "review",
            "--title", "import", "--scope", ".agent/handoffs/", session="planner",
        )
        self.agentctl("refresh", session="planner")
        imported = self.agentctl("reconcile", "merge-back", "--from-ref", "feature/E-1", "--task", "E-1", session="planner")
        self.assertIn("merged back E-1", imported.stdout)
        self.assertIn("milestone M-1 closed", imported.stdout)
        self.assertEqual(self.status("M-1"), "done")
        self.assertEqual(self.status("E-1"), "done")

    def test_sync_pull_of_the_last_child_closes_the_milestone(self):
        self.adopt_and_commit()
        origin = Path(tempfile.mkdtemp(prefix="awk-milestone-origin-"))
        self.addCleanup(shutil.rmtree, origin, ignore_errors=True)
        subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
        subprocess.run(["git", "-C", str(origin), "symbolic-ref", "HEAD", "refs/heads/main"], check=True)
        branch = self.git("branch", "--show-current").stdout.strip()
        self.git("remote", "add", "origin", str(origin))
        self.git("push", "-q", "-u", "origin", f"{branch}:main")
        self.milestone("M-1", "m")
        self.child("E-1", "M-1", "a")
        self.agentctl("sync", "--branch", "main", session="a")
        # A second machine finishes and approves the child and syncs.
        other = Path(tempfile.mkdtemp(prefix="awk-milestone-other-"))
        self.addCleanup(shutil.rmtree, other, ignore_errors=True)
        subprocess.run(["git", "clone", "-q", "-b", "main", str(origin), str(other)], check=True, capture_output=True)
        for key, value in (("user.email", "b@example.com"), ("user.name", "B")):
            subprocess.run(["git", "-C", str(other), "config", key, value], check=True)
        self.agentctl("work", "--agent", "codex", "--task", "E-1", "--takeover", "--reason", "moved", session="b", cwd=other)
        self.agentctl("finish", "--summary", "done", "--tests", "unit", session="b", cwd=other)
        self.agentctl("agents", "add", "--id", "rev", "--role", "review", session="rev", cwd=other)
        self.agentctl(
            "work", "--agent", "rev", "--auto-create", "--type", "review", "--title", "r",
            "--scope", ".agent/gates/", session="rev", cwd=other,
        )
        self.agentctl("refresh", session="rev", cwd=other)
        self.agentctl("gate", "approve", "--task", "E-1", "--by", "rev", "--note", "ok", session="rev", cwd=other)
        self.assertEqual(self.status("M-1", other), "done")
        # Rewind the milestone there and push with plain git (that side's own
        # sync would close it again) so the pulling side has to cascade.
        board = self.board(other)
        board["tasks"]["M-1"]["status"] = "todo"
        (other / ".agent" / "board.json").write_text(json.dumps(board, indent=2) + "\n", encoding="utf-8")
        agentctl._set_task_doc_status(other, "M-1", "todo")
        self.ledger_commit(other, "chore(ledger): child done elsewhere\n\nRefs: E-1, M-1")
        subprocess.run(["git", "-C", str(other), "-c", "core.hooksPath=/dev/null", "push", "-q", "origin", "HEAD:main"], check=True, capture_output=True)
        self.assertEqual(self.status("M-1"), "todo")
        synced = self.agentctl("sync", "--branch", "main", session="a")
        self.assertIn("milestone M-1 closed", synced.stdout)
        self.assertEqual(self.status("M-1"), "done")
        self.assertEqual(self.status("E-1"), "done")
        log = self.git("log", "--oneline", "-3").stdout
        self.assertIn("close milestone(s) M-1", log)


class BoardRenderingTest(_MilestoneTestCase):
    def test_board_and_tree_show_progress(self):
        self.milestone("M-1", "experiments")
        self.milestone("M-0", "the paper", deps="M-1")
        self.child("E-1", "M-1", "a")
        self.child("E-2", "M-1", "b")
        # A plain task with a plain dependency, under no milestone: both
        # must still appear, the dependency under the task that waits on it.
        self.agentctl("task", "create", "--id", "P-1", "--title", "prep", session="supervisor")
        self.agentctl("task", "create", "--id", "P-2", "--title", "uses prep", "--deps", "P-1", session="supervisor")
        flat = self.agentctl("board", TERM_SESSION_ID="w0t0p0").stdout
        self.assertIn("[milestone: 0/2 children done]", flat)
        self.assertIn("[milestone: 0/1 children done]", flat)
        tree = self.agentctl("board", "--tree", TERM_SESSION_ID="w0t0p0").stdout
        lines = [ln.rstrip() for ln in tree.splitlines()]
        self.assertIn("  M-0 [todo, 0/1 done] the paper", lines)
        self.assertIn("    - M-1 [todo, 0/2 done] experiments", lines)
        self.assertIn("      - E-1 [in_progress] E-1", lines)
        self.assertIn("      - E-2 [in_progress] E-2", lines)
        self.assertIn("  (not under any milestone)", lines)
        self.assertTrue(any("T-000 [todo]" in ln for ln in lines))
        self.assertIn("    - P-2 [todo] uses prep", lines)
        self.assertIn("      - P-1 [todo] prep", lines)
        self.assertFalse(any(ln.startswith("    - P-1 ") for ln in lines))
        # M-1 is a child of M-0, so it is not a root.
        self.assertFalse(any(ln.startswith("  M-1 ") for ln in lines))


if __name__ == "__main__":
    unittest.main()
