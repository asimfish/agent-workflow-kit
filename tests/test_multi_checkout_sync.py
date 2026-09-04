"""Several checkouts, one remote: the ledger has to merge, travel, and refuse silent takeover.

Sessions and locks never leave a machine, so between machines the only
channel is Git. Before this coverage existed, a claim could not be pushed
until the task reached review (the hook refused in_progress references), a
second machine could take over an in_progress task without a word, and two
machines that each finished a task conflicted on board.json, TASKS.md and
PROJECT_PLAN.md with no automatic resolution.

These tests run two independently installed clones against one bare
repository and pin the fixed behavior: ledger-only commits push while the
task is in_progress, code commits still wait for review, the merge driver
combines concurrent ledger changes without conflict, a foreign in_progress
claim needs an explicit --takeover --reason, and `agentctl sync` does the
commit / pull / push round trip.
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


class TwoCheckoutsOneRemoteTest(unittest.TestCase):
    """Machine A and machine B share a bare origin; each has the kit installed."""

    def setUp(self):
        self.base = Path(tempfile.mkdtemp(prefix="awk-multi-checkout-"))
        self.addCleanup(shutil.rmtree, self.base, ignore_errors=True)
        self.locks = self.base / "locks"
        self.origin = self.base / "origin.git"
        subprocess.run(["git", "init", "-q", "--bare", str(self.origin)], check=True, timeout=60)
        seed = self.base / "seed"
        seed.mkdir()
        self.git(seed, "init", "-q")
        self.git(seed, "config", "user.email", "seed@example.com")
        self.git(seed, "config", "user.name", "Seed")
        self.git(seed, "commit", "-q", "--allow-empty", "-m", "chore(init): seed\n\nRefs: T-000")
        install = subprocess.run(
            [sys.executable, str(KIT / "tools" / "agentctl.py"), "init", str(seed)],
            cwd=KIT, text=True, capture_output=True, timeout=600,
        )
        self.assertEqual(install.returncode, 0, install.stdout + install.stderr)
        self.git(seed, "add", "-A")
        self.git(seed, "-c", "core.hooksPath=", "commit", "-q", "-m", "chore(init): install kit\n\nRefs: T-000")
        self.git(seed, "-c", "core.hooksPath=", "push", "-q", str(self.origin), "HEAD:main")
        # The bare repo's default branch name follows the host's git config
        # (master on CI, main here); point HEAD at what was pushed so clones
        # check it out instead of coming up empty.
        subprocess.run(
            ["git", "-C", str(self.origin), "symbolic-ref", "HEAD", "refs/heads/main"],
            check=True, timeout=60,
        )
        self.a = self.clone("a")
        self.b = self.clone("b")

    def clone(self, name):
        root = self.base / name
        subprocess.run(
            ["git", "clone", "-q", "-b", "main", str(self.origin), str(root)],
            check=True, timeout=60,
        )
        self.git(root, "config", "user.email", f"{name}@example.com")
        self.git(root, "config", "user.name", f"Machine {name.upper()}")
        # Every machine installs the kit into its own clone (idempotent on the
        # committed files); this is what registers the merge driver per clone.
        install = subprocess.run(
            [sys.executable, str(KIT / "tools" / "agentctl.py"), "init", str(root)],
            cwd=KIT, text=True, capture_output=True, timeout=600,
        )
        self.assertEqual(install.returncode, 0, install.stdout + install.stderr)
        self.assertEqual(self.git(root, "status", "--porcelain"), "", "init must not dirty a fresh clone")
        return root

    def git(self, root, *args, check=True):
        proc = subprocess.run(
            ["git", *args], cwd=str(root), text=True, capture_output=True, timeout=120,
        )
        if check:
            self.assertEqual(proc.returncode, 0, f"git {' '.join(args)}\n{proc.stdout}{proc.stderr}")
        return proc.stdout.strip() if check else proc

    def env(self, session):
        env = os.environ.copy()
        for name in IDENTITY_ENV:
            env.pop(name, None)
        env["AGENT_WORKFLOW_SESSION_ID"] = session
        env["CODEX_THREAD_ID"] = f"thread-{session}"
        env["AGENT_WORKFLOW_RESOURCE_LOCK_DIR"] = str(self.locks)
        return env

    def agentctl(self, root, session, *args, expect=0):
        proc = subprocess.run(
            [sys.executable, "tools/agentctl.py", *args],
            cwd=str(root), env=self.env(session), text=True, capture_output=True, timeout=600,
        )
        self.assertEqual(
            proc.returncode, expect,
            f"[{root.name}] agentctl {' '.join(args)}\nrc={proc.returncode}\n{proc.stdout}{proc.stderr}",
        )
        return proc

    def git_as(self, root, session, *args, expect=0):
        proc = subprocess.run(
            ["git", *args], cwd=str(root), env=self.env(session), text=True,
            capture_output=True, timeout=300,
        )
        self.assertEqual(
            proc.returncode, expect,
            f"[{root.name}] git {' '.join(args)}\nrc={proc.returncode}\n{proc.stdout}{proc.stderr}",
        )
        return proc

    def task_id(self, root, title):
        board = json.loads((root / ".agent" / "board.json").read_text(encoding="utf-8"))
        return next(t for t, e in board["tasks"].items() if e.get("title") == title)

    def board(self, root):
        return json.loads((root / ".agent" / "board.json").read_text(encoding="utf-8"))["tasks"]

    def open_task(self, root, session, agent, title, scope):
        self.agentctl(
            root, session, "work", "--agent", agent, "--auto-create", "--type", "docs",
            "--title", title, "--scope", scope,
        )
        return self.task_id(root, title)

    # --- tests ---------------------------------------------------------------------

    def test_install_registers_the_merge_driver_and_attributes(self):
        for root in (self.a, self.b):
            self.assertIn("merge-driver", self.git(root, "config", "--get", "merge.agent-ledger.driver"))
            attributes = (root / ".gitattributes").read_text(encoding="utf-8")
            for entry in agentctl.GITATTRIBUTES_MANAGED_ENTRIES:
                self.assertIn(entry, attributes)
        report = json.loads(self.agentctl(self.a, "conv-a", "doctor", "--json").stdout)
        driver = next(c for c in report["checks"] if c["name"] == "ledger merge driver")
        self.assertEqual(driver["status"], "ok", driver)

    def test_claim_is_pushable_while_in_progress_but_code_is_not(self):
        task = self.open_task(self.a, "conv-a", "codex", "collect on 4090", "collect/a/")
        self.git_as(self.a, "conv-a", "add", "--", ".agent")
        self.git_as(self.a, "conv-a", "commit", "-q", "-m", f"chore(ledger): open {task}\n\nRefs: {task}")
        # The ledger-only commit publishes the claim although the task is in_progress.
        self.git_as(self.a, "conv-a", "push", "-q", "origin", "HEAD:main")
        # Machine B sees the claim after a pull.
        self.git(self.b, "pull", "-q", "--rebase", "origin", "main")
        self.assertEqual(self.board(self.b)[task]["status"], "in_progress")
        # A code change referencing the same in_progress task still cannot leave the machine.
        (self.a / "collect" / "a").mkdir(parents=True)
        (self.a / "collect" / "a" / "run.py").write_text("print('x')\n", encoding="utf-8")
        self.agentctl(self.a, "conv-a", "note", "first run script")
        self.git_as(self.a, "conv-a", "add", "collect/a/run.py", ".agent")
        self.git_as(self.a, "conv-a", "commit", "-q", "-m", f"feat(collect): first run\n\nRefs: {task}")
        refused = self.git_as(self.a, "conv-a", "push", "-q", "origin", "HEAD:main", expect=1)
        self.assertIn(
            "must be review/approved/done before pushing commits that change anything but ledger data under .agent/",
            refused.stdout + refused.stderr,
        )

    def test_behavior_changing_files_under_agent_are_not_ledger_data(self):
        # Loop contracts are executed (their check lines run through a shell)
        # and checkpoints.json wires them to work-start, so a change to them
        # is code for the purpose of the push rule and waits for review.
        task = self.open_task(self.a, "conv-a", "codex", "collect on 4090", "collect/a/")
        (self.a / ".agent" / "loops" / "evil.md").write_text(
            "# Loop evil\n\n## Trigger\nx\n## Execute\n```loop-check\n$ echo evil\n```\n"
            "## Check\nx\n## Feedback\nx\n## Memory\nx\n## Next\nx\n",
            encoding="utf-8",
        )
        self.agentctl(self.a, "conv-a", "note", "adding a loop")
        self.git_as(self.a, "conv-a", "-c", "core.hooksPath=", "add", "--", ".agent")
        self.git_as(
            self.a, "conv-a", "-c", "core.hooksPath=", "commit", "-q", "-m",
            f"chore(loops): add loop\n\nRefs: {task}",
        )
        refused = self.git_as(self.a, "conv-a", "push", "-q", "origin", "HEAD:main", expect=1)
        self.assertIn("must be review/approved/done", refused.stdout + refused.stderr)
        for path, expected in (
            (".agent/board.json", True), (".agent/tasks/T-1.md", True), (".agent/logs/progress.md", True),
            (".agent/loops/runs/x.md", True), (".agent/loops/state.json", True), (".agent/archive/board.json", True),
            (".agent/loops/evil.md", False), (".agent/loops/checkpoints.json", False),
            (".agent/rules/x.md", False), (".agent/evals/suites.json", False),
            (".agent/runtime-policy.json", False), (".agent/WORKFLOW_ENTRY.md", False),
            ("tools/agentctl.py", False),
        ):
            self.assertEqual(agentctl._is_ledger_data_path(path), expected, path)

    def test_sync_stages_ledger_data_only(self):
        task = self.open_task(self.a, "conv-a", "codex", "collect on 4090", "collect/a/")
        # A rule file changes agent behavior; it is under .agent/ but not ledger data.
        (self.a / ".agent" / "rules" / "local-note.md").write_text("# local rule\n", encoding="utf-8")
        synced = self.agentctl(self.a, "conv-a", "sync", "--no-push")
        self.assertIn("committed ledger changes", synced.stdout)
        self.assertIn("left these .agent/ changes unstaged", synced.stderr)
        self.assertIn(".agent/rules/local-note.md", synced.stderr)
        self.assertEqual(
            self.git(self.a, "status", "--porcelain", "--", ".agent/rules/local-note.md"),
            "?? .agent/rules/local-note.md",
        )
        shown = self.git(self.a, "show", "--name-only", "--format=", "HEAD")
        self.assertIn(".agent/board.json", shown)
        self.assertNotIn("local-note.md", shown)
        self.assertIn(task, self.git(self.a, "log", "-1", "--format=%B"))

    def test_archive_stands_when_the_other_side_only_touched_a_done_entry(self):
        base = json.dumps({"version": 1, "tasks": {"T-1": {"status": "done", "updated_at": "2026-01-01 00:00:00"}}})
        archived = json.dumps({"version": 1, "tasks": {}})
        touched = json.dumps({"version": 1, "tasks": {"T-1": {"status": "done", "updated_at": "2026-01-02 00:00:00"}}})
        merged = json.loads(agentctl._merge_ledger_json(base, archived, touched, "tasks", agentctl._resolve_board_entry))
        self.assertEqual(merged["tasks"], {}, "a done task archived on one side is not resurrected by a touch")
        reopened = json.dumps({"version": 1, "tasks": {"T-1": {"status": "in_progress", "updated_at": "2026-01-02 00:00:00"}}})
        merged2 = json.loads(agentctl._merge_ledger_json(base, archived, reopened, "tasks", agentctl._resolve_board_entry))
        self.assertEqual(merged2["tasks"]["T-1"]["status"], "in_progress", "a real status change survives the archive")
        # Theirs-only top-level keys survive; ours wins on shared ones.
        merged3 = json.loads(agentctl._merge_ledger_json(
            base, json.dumps({"version": 1, "tasks": {}, "x": "ours"}),
            json.dumps({"version": 1, "tasks": {}, "x": "theirs", "y": "theirs"}), "tasks", agentctl._resolve_board_entry,
        ))
        self.assertEqual((merged3["x"], merged3["y"]), ("ours", "theirs"))

    def test_foreign_in_progress_claim_needs_an_explicit_takeover(self):
        task = self.open_task(self.a, "conv-a", "codex", "collect on 4090", "collect/a/")
        self.git_as(self.a, "conv-a", "add", "--", ".agent")
        self.git_as(self.a, "conv-a", "commit", "-q", "-m", f"chore(ledger): open {task}\n\nRefs: {task}")
        self.git_as(self.a, "conv-a", "push", "-q", "origin", "HEAD:main")
        self.git(self.b, "pull", "-q", "--rebase", "origin", "main")

        refused = self.agentctl(self.b, "conv-b", "start", "--task", task, "--agent", "claude", expect=1)
        self.assertIn("in_progress for codex according to the board", refused.stderr)
        self.assertIn("another checkout or machine", refused.stderr)
        self.assertIn("--takeover --reason", refused.stderr)
        self.assertEqual(self.board(self.b)[task]["owner"], "codex")

        no_reason = self.agentctl(
            self.b, "conv-b", "work", "--agent", "claude", "--task", task, "--takeover", expect=1,
        )
        self.assertIn("requires --reason", no_reason.stderr)

        taken = self.agentctl(
            self.b, "conv-b", "work", "--agent", "claude", "--task", task,
            "--takeover", "--reason", "machine A was decommissioned; notes and outputs checked",
        )
        self.assertIn("taken over from codex by claude", taken.stderr)
        entry = self.board(self.b)[task]
        self.assertEqual(entry["owner"], "claude")
        self.assertEqual(entry["taken_over_from"], "codex")
        self.assertIn("decommissioned", entry["takeover_reason"])
        doc = (self.b / ".agent" / "tasks" / f"{task}.md").read_text(encoding="utf-8")
        self.assertIn("taken over from codex by claude: machine A was decommissioned", doc)
        log = (self.b / ".agent" / "logs" / "progress.md").read_text(encoding="utf-8")
        self.assertIn(f"[{task}] taken over from codex by claude", log)

        # Resuming a task this checkout already holds is not a takeover.
        self.agentctl(self.b, "conv-b", "work", "--agent", "claude", "--task", task)

    def test_concurrent_ledger_changes_merge_without_conflict(self):
        task_a = self.open_task(self.a, "conv-a", "codex", "collect on 4090", "collect/a/")
        task_b = self.open_task(self.b, "conv-b", "cursor", "collect on 5090", "collect/b/")
        for root, session, task in ((self.a, "conv-a", task_a), (self.b, "conv-b", task_b)):
            self.agentctl(root, session, "note", f"working on {task}")
        self.agentctl(self.a, "conv-a", "finish", "--summary", "a done", "--tests", "n/a")
        # A publishes first; B's push is rejected and its rebase must merge the ledgers.
        self.git_as(self.a, "conv-a", "add", "--", ".agent")
        self.git_as(self.a, "conv-a", "commit", "-q", "-m", f"chore(ledger): {task_a} to review\n\nRefs: {task_a}")
        self.git_as(self.a, "conv-a", "push", "-q", "origin", "HEAD:main")

        self.git_as(self.b, "conv-b", "add", "--", ".agent")
        self.git_as(self.b, "conv-b", "commit", "-q", "-m", f"chore(ledger): open {task_b}\n\nRefs: {task_b}")
        self.git_as(self.b, "conv-b", "push", "-q", "origin", "HEAD:main", expect=1)
        rebase = self.git_as(self.b, "conv-b", "pull", "--rebase", "origin", "main")
        self.assertNotIn("CONFLICT", rebase.stdout + rebase.stderr)
        self.assertEqual(self.git(self.b, "diff", "--name-only", "--diff-filter=U"), "")

        merged = self.board(self.b)
        self.assertEqual(merged[task_a]["status"], "review")
        self.assertEqual(merged[task_b]["status"], "in_progress")
        index = (self.b / ".agent" / "TASKS.md").read_text(encoding="utf-8")
        self.assertIn(f"| {task_a} | review |", index)
        self.assertIn(f"| {task_b} | in_progress |", index)
        plan = (self.b / ".agent" / "PROJECT_PLAN.md").read_text(encoding="utf-8")
        self.assertEqual(plan.count(f"] {task_a} - "), 1)
        self.assertEqual(plan.count(f"] {task_b} - "), 1)
        self.assertNotIn("<<<<<<<", plan)
        log = (self.b / ".agent" / "logs" / "progress.md").read_text(encoding="utf-8")
        self.assertIn(f"working on {task_a}", log)
        self.assertIn(f"working on {task_b}", log)
        self.assertEqual(self.agentctl(self.b, "conv-b", "reconcile", "check").returncode, 0)
        self.git_as(self.b, "conv-b", "push", "-q", "origin", "HEAD:main")

    def test_same_task_advanced_on_both_sides_resolves_to_the_later_status(self):
        # The board rule for a genuinely competing edit of one entry.
        base = json.dumps({"version": 1, "tasks": {"T-1": {"status": "in_progress", "owner": "codex", "updated_at": "2026-01-01 00:00:00"}}})
        ours = json.dumps({"version": 1, "tasks": {"T-1": {"status": "in_progress", "owner": "codex", "updated_at": "2026-01-02 00:00:00", "note": "ours"}}})
        theirs = json.dumps({"version": 1, "tasks": {"T-1": {"status": "review", "owner": "codex", "updated_at": "2026-01-01 12:00:00"}, "T-2": {"status": "todo"}}})
        merged = json.loads(agentctl._merge_ledger_json(base, ours, theirs, "tasks", agentctl._resolve_board_entry))
        self.assertEqual(merged["tasks"]["T-1"]["status"], "review")
        self.assertEqual(merged["tasks"]["T-2"]["status"], "todo")
        # Same rank: the newer update wins; an archived (deleted) entry stays deleted
        # unless the other side advanced it.
        ours2 = json.dumps({"version": 1, "tasks": {"T-1": {"status": "review", "updated_at": "2026-01-03 00:00:00", "note": "ours"}}})
        theirs2 = json.dumps({"version": 1, "tasks": {"T-1": {"status": "review", "updated_at": "2026-01-02 00:00:00", "note": "theirs"}}})
        merged2 = json.loads(agentctl._merge_ledger_json(base, ours2, theirs2, "tasks", agentctl._resolve_board_entry))
        self.assertEqual(merged2["tasks"]["T-1"]["note"], "ours")
        deleted_ours = json.dumps({"version": 1, "tasks": {}})
        merged3 = json.loads(agentctl._merge_ledger_json(base, deleted_ours, base, "tasks", agentctl._resolve_board_entry))
        self.assertEqual(merged3["tasks"], {})
        merged4 = json.loads(agentctl._merge_ledger_json(base, deleted_ours, theirs, "tasks", agentctl._resolve_board_entry))
        self.assertEqual(merged4["tasks"]["T-1"]["status"], "review", "a deletion racing an advance keeps the advance")

    def test_malformed_ledger_side_is_a_conflict_not_a_deletion(self):
        base = json.dumps({"version": 1, "tasks": {"T-1": {"status": "in_progress"}}})
        theirs = json.dumps({"version": 1, "tasks": {"T-1": {"status": "in_progress"}, "T-2": {"status": "todo"}}})
        with self.assertRaises(ValueError):
            agentctl._merge_ledger_json(base, "{not json", theirs, "tasks", agentctl._resolve_board_entry)
        with self.assertRaises(ValueError):
            agentctl._merge_ledger_json(base, "[1, 2]", theirs, "tasks", agentctl._resolve_board_entry)
        # Through the driver entry point: exit 1, ours left untouched.
        tmp = self.base / "driver"
        tmp.mkdir()
        (tmp / "base").write_text(base, encoding="utf-8")
        (tmp / "ours").write_text("{not json", encoding="utf-8")
        (tmp / "theirs").write_text(theirs, encoding="utf-8")
        proc = subprocess.run(
            [sys.executable, "tools/agentctl.py", "merge-driver", "--base", str(tmp / "base"),
             "--ours", str(tmp / "ours"), "--theirs", str(tmp / "theirs"), "--path", ".agent/board.json"],
            cwd=str(self.a), env=self.env("conv-a"), text=True, capture_output=True, timeout=120,
        )
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("not merged", proc.stderr)
        self.assertEqual((tmp / "ours").read_text(encoding="utf-8"), "{not json")
        # An empty side is a legitimate empty ledger, not damage.
        merged = json.loads(agentctl._merge_ledger_json("", "", theirs, "tasks", agentctl._resolve_board_entry))
        self.assertEqual(set(merged["tasks"]), {"T-1", "T-2"})

    def test_loop_run_reports_do_not_collide_across_checkouts(self):
        # Two checkouts running the same loop in the same second must not
        # produce two different files with one name (an add/add conflict).
        self.assertNotEqual(agentctl._loop_report_nonce(self.a), agentctl._loop_report_nonce(self.b))
        self.assertRegex(agentctl._loop_report_nonce(self.a), r"^[0-9a-f]{6}$")
        result = {"status": "success", "trigger": "test", "execute": [], "check": [], "feedback": [], "memory": [], "next": []}
        paths = [agentctl._write_loop_report(root, "daily-plan-triage", "test", result) for root in (self.a, self.b)]
        self.assertNotEqual(paths[0].name, paths[1].name)
        for root, path in zip((self.a, self.b), paths):
            self.assertRegex(path.name, r"^\d{8}-\d{6}-daily-plan-triage-[0-9a-f]{6}(-\d+)?\.md$")
            self.assertTrue(path.name.startswith(path.name[:15]), path)
            self.assertIn(agentctl._loop_report_nonce(root), path.name)

    def test_plan_prose_conflicts_are_still_reported(self):
        base = "# Plan\n\nGoal: x\n\n## Task Board\n- [ ] T-1 - one\n\n## Notes\nkeep\n"
        ours = base.replace("Goal: x", "Goal: ours")
        theirs = base.replace("Goal: x", "Goal: theirs").replace("- [ ] T-1 - one", "- [ ] T-1 - one\n- [ ] T-2 - two")
        merged, conflicted = agentctl._merge_project_plan(base, ours, theirs)
        self.assertTrue(conflicted)
        self.assertIn("<<<<<<<", merged)
        self.assertIn("- [ ] T-2 - two", merged, "task rows still merge even when prose conflicts")
        merged_ok, conflicted_ok = agentctl._merge_project_plan(base, base.replace("keep", "keep more"), theirs)
        self.assertFalse(conflicted_ok)
        self.assertIn("keep more", merged_ok)
        self.assertIn("Goal: theirs", merged_ok)

    def test_sync_round_trip(self):
        task_a = self.open_task(self.a, "conv-a", "codex", "collect on 4090", "collect/a/")
        synced = self.agentctl(self.a, "conv-a", "sync")
        self.assertIn("committed ledger changes", synced.stdout)
        self.assertIn("pushed main to origin", synced.stdout)
        self.assertEqual(self.git(self.a, "status", "--porcelain"), "")

        task_b = self.open_task(self.b, "conv-b", "cursor", "collect on 5090", "collect/b/")
        synced_b = self.agentctl(self.b, "conv-b", "sync")
        self.assertIn("pulled origin/main", synced_b.stdout)
        self.assertIn("pushed main to origin", synced_b.stdout)
        board_b = self.board(self.b)
        self.assertEqual({task_a, task_b} <= set(board_b), True)

        # A staged code file never rides along with a ledger sync.
        (self.a / "collect" / "a").mkdir(parents=True)
        (self.a / "collect" / "a" / "run.py").write_text("print('x')\n", encoding="utf-8")
        self.git_as(self.a, "conv-a", "add", "collect/a/run.py")
        refused = self.agentctl(self.a, "conv-a", "sync", expect=1)
        self.assertIn("ledger data only", refused.stderr)
        self.git_as(self.a, "conv-a", "reset", "-q", "collect/a/run.py")

        # Nothing new locally: sync still pulls B's claim.
        synced_again = self.agentctl(self.a, "conv-a", "sync")
        self.assertIn("no ledger changes to commit", synced_again.stdout)
        self.assertIn(task_b, self.board(self.a))
        self.assertEqual(self.agentctl(self.a, "conv-a", "reconcile", "check").returncode, 0)


if __name__ == "__main__":
    unittest.main()
