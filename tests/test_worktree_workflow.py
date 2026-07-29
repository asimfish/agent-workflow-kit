"""Fresh-install regression tests for managed Git worktree leases."""

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


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


def path_is_relative_to(path, parent):
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


class WorktreeWorkflowRegressionTest(unittest.TestCase):
    def setUp(self):
        test_env = os.environ.copy()
        for name in IDENTITY_ENV:
            test_env.pop(name, None)
        test_env["AGENT_WORKFLOW_SESSION_ID"] = "worktree-regression-session"
        identity = mock.patch.dict(
            os.environ, test_env, clear=True,
        )
        identity.start()
        self.addCleanup(identity.stop)
        self.temp = Path(tempfile.mkdtemp(prefix="awk-worktree-regress-"))
        self.addCleanup(shutil.rmtree, self.temp, ignore_errors=True)
        self.root = self.temp / "project"
        self.root.mkdir()
        self.worktree = self.temp / "worker"
        self.git("init", "-q")
        self.git("config", "user.email", "agent@example.com")
        self.git("config", "user.name", "Agent Test")
        installed = subprocess.run(
            [sys.executable, str(KIT / "tools" / "agentctl.py"), "init", str(self.root)],
            cwd=str(KIT),
            text=True,
            capture_output=True,
            timeout=120,
        )
        self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
        self.commit("chore(agent): install workflow\n\nRefs: T-024")

    def git(self, *args, cwd=None, expect=0):
        proc = subprocess.run(
            ["git", *args],
            cwd=str(cwd or self.root),
            text=True,
            capture_output=True,
            timeout=120,
        )
        self.assertEqual(proc.returncode, expect, proc.stdout + proc.stderr)
        return proc.stdout.strip()

    def commit(self, message):
        self.git("add", "-A")
        self.git("commit", "--no-verify", "-q", "-m", message)

    def agentctl(self, *args, cwd=None, expect=None, env=None):
        proc = subprocess.run(
            [sys.executable, "tools/agentctl.py", *args],
            cwd=str(cwd or self.root),
            text=True,
            capture_output=True,
            env=env,
            timeout=120,
        )
        if expect is not None:
            self.assertEqual(
                proc.returncode,
                expect,
                f"agentctl {' '.join(args)} rc={proc.returncode}\n{proc.stdout}\n{proc.stderr}",
            )
        return proc

    def failing_worktree_list_env(self):
        if os.name == "nt":
            self.skipTest("POSIX Git shim")
        real_git = shutil.which("git")
        self.assertIsNotNone(real_git)
        fake_bin = self.temp / "fake-bin"
        fake_bin.mkdir(exist_ok=True)
        shim = fake_bin / "git"
        shim.write_text(
            "#!/bin/sh\n"
            "if [ \"$1\" = -C ]; then\n"
            "  command_name=$3\n"
            "  subcommand=$4\n"
            "else\n"
            "  command_name=$1\n"
            "  subcommand=$2\n"
            "fi\n"
            "if [ \"$command_name\" = worktree ] && [ \"$subcommand\" = list ]; then\n"
            "  echo 'simulated worktree enumeration failure' >&2\n"
            "  exit 86\n"
            "fi\n"
            f"exec {shlex.quote(real_git)} \"$@\"\n",
            encoding="utf-8",
        )
        shim.chmod(0o755)
        env = os.environ.copy()
        env["PATH"] = str(fake_bin) + os.pathsep + env.get("PATH", "")
        return env

    def create_committed_task(self, task="T-101", agent="worker"):
        self.agentctl(
            "task", "create", "--id", task, "--title", "parallel worker phase",
            "--owner", agent, "--scope", "src/", expect=0,
        )
        self.commit(f"chore(agent): plan parallel worker\n\nRefs: {task}")

    def create_lease(self, path=None, task="T-101", agent="worker"):
        args = ["worktree", "create", "--task", task, "--agent", agent]
        if path is not None:
            args.extend(["--path", str(path)])
        self.agentctl(*args, expect=0)
        return json.loads(
            self.agentctl("worktree", "list", "--json", expect=0).stdout
        )["worktrees"][-1]

    def hook(self, event, payload, cwd=None, env=None):
        return subprocess.run(
            [sys.executable, "tools/agent_workflow_hook.py", event],
            cwd=str(cwd or self.root), env=env, input=json.dumps(payload),
            text=True, capture_output=True, timeout=120,
        )

    def test_code_auto_create_bootstraps_an_isolated_worktree(self):
        created = self.agentctl(
            "work", "--agent", "codex", "--auto-create",
            "--new-id", "T-401", "--title", "isolated code task",
            "--scope", "src/code/", "--type", "code", expect=0,
        )
        match = re.search(r"^  path=(.+)$", created.stdout, re.MULTILINE)
        self.assertIsNotNone(match, created.stdout)
        worker = Path(match.group(1))
        self.addCleanup(shutil.rmtree, worker, ignore_errors=True)

        base_board = json.loads(
            (self.root / ".agent" / "board.json").read_text(encoding="utf-8")
        )
        self.assertNotIn("T-401", base_board["tasks"])
        worker_board = json.loads(
            (worker / ".agent" / "board.json").read_text(encoding="utf-8")
        )
        self.assertEqual(worker_board["tasks"]["T-401"]["type"], "code")
        self.assertEqual(worker_board["tasks"]["T-401"]["status"], "todo")

        self.agentctl(
            "work", "--agent", "codex", "--task", "T-401",
            cwd=worker, expect=0,
        )
        status = json.loads(
            self.agentctl("status", "--json", cwd=worker, expect=0).stdout
        )
        self.assertEqual(status["task"], "T-401")
        self.assertEqual(status["isolation"], "worktree")

    def test_docs_auto_create_uses_the_shared_checkout_fast_path(self):
        self.agentctl(
            "work", "--agent", "writer", "--auto-create",
            "--new-id", "T-402", "--title", "shared docs task",
            "--scope", "docs/", "--type", "docs", expect=0,
        )
        status = json.loads(self.agentctl("status", "--json", expect=0).stdout)
        self.assertEqual(status["task"], "T-402")
        self.assertEqual(status["isolation"], "shared")
        board = json.loads(
            (self.root / ".agent" / "board.json").read_text(encoding="utf-8")
        )
        self.assertEqual(board["tasks"]["T-402"]["type"], "docs")

    def test_worktree_flow_lets_a_blocked_script_run_after_relocation(self):
        """End-to-end escape hatch: opaque command blocked beside a peer, then
        runnable in the leased worktree after the worker starts its task."""
        # Peer session A holds the main checkout.
        self.agentctl(
            "task", "create", "--id", "T-4090", "--title", "collect 4090",
            "--owner", "codex", "--scope", "collect/m4090/", expect=0,
        )
        env_a = os.environ.copy()
        env_a["AGENT_WORKFLOW_SESSION_ID"] = "sess-a"
        self.agentctl("work", "--agent", "codex", "--task", "T-4090", env=env_a, expect=0)

        # Session B has an active planning task and is blocked from running an
        # opaque collector in the shared checkout.
        env_b = os.environ.copy()
        env_b["AGENT_WORKFLOW_SESSION_ID"] = "sess-b"
        self.agentctl(
            "task", "create", "--id", "T-PLAN", "--title", "b planning",
            "--owner", "cursor", "--scope", "collect/plan/", env=env_b, expect=0,
        )
        self.agentctl("work", "--agent", "cursor", "--task", "T-PLAN", env=env_b, expect=0)
        blocked = self.hook(
            "pre-tool-use",
            {"tool_name": "Bash", "tool_input": {"command": "python3 collect.py"}},
            env=env_b,
        )
        self.assertEqual(json.loads(blocked.stdout).get("decision"), "block")
        self.assertIn("worktree create", json.loads(blocked.stdout)["reason"])

        # B follows the guidance: create the collection task (left todo), commit
        # its plan, allocate a worktree, and start the task inside it.
        self.agentctl(
            "task", "create", "--id", "T-5090", "--title", "collect 5090",
            "--owner", "cursor", "--scope", "collect/m5090/", env=env_b, expect=0,
        )
        self.commit("chore(agent): plan collectors\n\nRefs: T-5090")
        wt = self.temp / "b-worktree"
        created = self.agentctl(
            "worktree", "create", "--task", "T-5090", "--agent", "cursor",
            "--path", str(wt), env=env_b, expect=0,
        )
        self.assertIn("cd ", created.stdout)  # actionable next step printed
        self.agentctl(
            "sessions", "release", "--reason", "move planning work to task worktree",
            env=env_b, expect=0,
        )
        self.agentctl("work", "--agent", "cursor", "--task", "T-5090",
                      cwd=wt, env=env_b, expect=0)

        # In the worktree B is exclusive: the opaque collector now runs.
        freed = self.hook(
            "pre-tool-use",
            {"tool_name": "Bash", "tool_input": {"command": "python3 collect.py"}},
            cwd=wt, env=env_b,
        )
        self.assertNotIn('"decision": "block"', freed.stdout)
        write = self.hook(
            "pre-tool-use",
            {"tool_name": "Bash", "tool_input": {"command": "touch collect/m5090/out.json"}},
            cwd=wt, env=env_b,
        )
        self.assertNotIn('"decision": "block"', write.stdout)

    def test_worktree_lease_lifecycle_is_shared_and_non_destructive(self):
        self.agentctl(
            "task", "create", "--id", "T-101", "--title", "parallel worker phase",
            "--owner", "worker", "--scope", "src/", expect=0,
        )
        uncommitted = self.agentctl(
            "worktree", "create", "--task", "T-101", "--agent", "worker",
            "--path", str(self.worktree), expect=1,
        )
        self.assertIn("not committed", uncommitted.stdout + uncommitted.stderr)
        self.commit("chore(agent): plan parallel worker\n\nRefs: T-101")

        instructions = self.root / "AGENTS.md"
        original_instructions = instructions.read_text(encoding="utf-8")
        instructions.write_text(original_instructions + "\nlocal steering\n", encoding="utf-8")
        dirty = self.agentctl(
            "worktree", "create", "--task", "T-101", "--agent", "worker",
            "--path", str(self.worktree), expect=1,
        )
        self.assertIn("clean committed baseline", dirty.stdout + dirty.stderr)
        instructions.write_text(original_instructions, encoding="utf-8")

        created = self.agentctl(
            "worktree", "create", "--task", "T-101", "--agent", "worker",
            "--path", str(self.worktree), expect=0,
        )
        self.assertIn("worktree lease", created.stdout)
        root_rows = json.loads(
            self.agentctl("worktree", "list", "--json", expect=0).stdout
        )["worktrees"]
        self.assertEqual(len(root_rows), 1)
        lease = root_rows[0]
        self.assertEqual(lease["observed_status"], "active")
        self.assertEqual(Path(lease["path"]), self.worktree.resolve())

        worker_rows = json.loads(
            self.agentctl("worktree", "list", "--json", cwd=self.worktree, expect=0).stdout
        )["worktrees"]
        self.assertEqual(worker_rows[0]["id"], lease["id"])

        duplicate = self.agentctl(
            "worktree", "create", "--task", "T-101", "--agent", "worker",
            "--path", str(self.temp / "duplicate"), expect=1,
        )
        self.assertIn("already exists", duplicate.stdout + duplicate.stderr)

        current = self.agentctl(
            "worktree", "release", lease["id"], cwd=self.worktree, expect=1,
        )
        self.assertIn("current directory", current.stdout + current.stderr)

        dirty_file = self.worktree / "uncommitted.txt"
        dirty_file.write_text("unfinished\n", encoding="utf-8")
        refused = self.agentctl("worktree", "release", lease["id"], expect=1)
        self.assertIn("is dirty", refused.stdout + refused.stderr)
        dirty_file.unlink()

        released = self.agentctl("worktree", "release", lease["id"], expect=0)
        self.assertIn("branch", released.stdout)
        self.assertIn("preserved", released.stdout)
        self.assertFalse(self.worktree.exists())
        self.git("show-ref", "--verify", f"refs/heads/{lease['branch']}")

        final_rows = json.loads(
            self.agentctl("worktree", "list", "--json", expect=0).stdout
        )["worktrees"]
        self.assertEqual(final_rows[0]["observed_status"], "released")

    def test_same_task_cannot_start_in_two_linked_worktrees(self):
        self.agentctl(
            "task", "create", "--id", "T-601", "--title", "single authority",
            "--owner", "codex", "--scope", "src/shared/", "--type", "generic",
            expect=0,
        )
        self.commit("chore(agent): plan single authority\n\nRefs: T-601")
        first = self.temp / "authority-a"
        second = self.temp / "authority-b"
        self.git("worktree", "add", "-q", "-b", "test/authority-a", str(first), "HEAD")
        self.git("worktree", "add", "-q", "-b", "test/authority-b", str(second), "HEAD")
        env_a = os.environ.copy()
        env_a["AGENT_WORKFLOW_SESSION_ID"] = "authority-a"
        env_b = os.environ.copy()
        env_b["AGENT_WORKFLOW_SESSION_ID"] = "authority-b"

        self.agentctl(
            "work", "--agent", "codex", "--task", "T-601",
            cwd=first, env=env_a, expect=0,
        )
        blocked = self.agentctl(
            "work", "--agent", "codex", "--task", "T-601",
            cwd=second, env=env_b, expect=1,
        )
        self.assertIn("already claimed", blocked.stderr)

    def test_exclusive_maintenance_blocks_sessions_in_other_worktrees(self):
        created = self.agentctl(
            "work", "--agent", "maintainer", "--auto-create",
            "--new-id", "T-611", "--title", "exclusive maintenance",
            "--scope", "maintenance/", "--type", "maintenance", expect=0,
        )
        match = re.search(r"^  path=(.+)$", created.stdout, re.MULTILINE)
        self.assertIsNotNone(match, created.stdout)
        worker = Path(match.group(1))
        env_maintenance = os.environ.copy()
        env_maintenance["AGENT_WORKFLOW_SESSION_ID"] = "maintenance-owner"
        env_docs = os.environ.copy()
        env_docs["AGENT_WORKFLOW_SESSION_ID"] = "docs-peer"
        self.agentctl(
            "work", "--agent", "maintainer", "--task", "T-611",
            cwd=worker, env=env_maintenance, expect=0,
        )
        blocked = self.agentctl(
            "work", "--agent", "writer", "--auto-create",
            "--new-id", "T-612", "--title", "parallel docs",
            "--scope", "docs/parallel/", "--type", "docs",
            env=env_docs, expect=1,
        )
        self.assertIn("exclusive", blocked.stderr)
        board = json.loads((self.root / ".agent" / "board.json").read_text())
        self.assertNotIn("T-612", board["tasks"])
        self.assertFalse((self.root / ".agent" / "tasks" / "T-612.md").exists())
        self.assertNotIn(
            "T-612", (self.root / ".agent" / "TASKS.md").read_text(),
        )
        self.assertNotIn(
            "T-612", (self.root / ".agent" / "PROJECT_PLAN.md").read_text(),
        )

    def test_stale_session_with_missing_checkout_is_audited_but_not_authoritative(self):
        self.agentctl(
            "task", "create", "--id", "T-631", "--title", "old checkout",
            "--owner", "codex", "--scope", "src/old/", "--type", "generic",
            expect=0,
        )
        self.commit("chore(agent): plan orphan checkout\n\nRefs: T-631")
        old_checkout = self.temp / "orphaned-worker"
        self.git(
            "worktree", "add", "-q", "-b", "test/orphaned-worker",
            str(old_checkout), "HEAD",
        )
        reused_env = os.environ.copy()
        reused_env["AGENT_WORKFLOW_SESSION_ID"] = "reused-after-orphan"
        self.agentctl(
            "work", "--agent", "codex", "--task", "T-631",
            cwd=old_checkout, env=reused_env, expect=0,
        )
        common_raw = self.git("rev-parse", "--git-common-dir")
        common = Path(common_raw)
        if not common.is_absolute():
            common = (self.root / common).resolve()
        session_paths = list(
            (common / "agent-workflow" / "sessions").glob("session-*.json"),
        )
        old_record = next(
            path for path in session_paths
            if json.loads(path.read_text()).get("task") == "T-631"
        )
        record = json.loads(old_record.read_text())
        record["heartbeat_ns"] = 1
        record["heartbeat_at"] = "1970-01-01 00:00:00"
        old_record.write_text(
            json.dumps(record, indent=2) + "\n", encoding="utf-8",
        )
        self.git("worktree", "remove", "--force", str(old_checkout))

        rows = json.loads(
            self.agentctl(
                "sessions", "list", "--json", env=reused_env, expect=0,
            ).stdout
        )["sessions"]
        orphaned = next(row for row in rows if row["task"] == "T-631")
        self.assertEqual(orphaned["observed_status"], "orphaned")

        started = self.agentctl(
            "work", "--agent", "writer", "--auto-create",
            "--new-id", "T-632", "--title", "new checkout work",
            "--scope", "docs/new/", "--type", "docs",
            env=reused_env, expect=0,
        )
        self.assertIn("started T-632", started.stdout)

    def test_experiment_finish_requires_successful_declared_output(self):
        created = self.agentctl(
            "work", "--agent", "researcher", "--auto-create",
            "--new-id", "T-621", "--title", "experiment output contract",
            "--scope", "experiments/T-621/", "--type", "experiment", expect=0,
        )
        match = re.search(r"^  path=(.+)$", created.stdout, re.MULTILINE)
        self.assertIsNotNone(match, created.stdout)
        worker = Path(match.group(1))
        env = os.environ.copy()
        env["AGENT_WORKFLOW_SESSION_ID"] = "experiment-owner"
        self.agentctl(
            "work", "--agent", "researcher", "--task", "T-621",
            cwd=worker, env=env, expect=0,
        )
        blocked = self.agentctl(
            "finish", "--summary", "no output yet", "--tests", "none",
            cwd=worker, env=env, expect=1,
        )
        self.assertIn("successful run with existing declared output", blocked.stderr)

        started = self.agentctl(
            "run", "start", "--output", ".agent-artifacts/T-621/result.txt", "--",
            sys.executable, "-c",
            "from pathlib import Path; p=Path('.agent-artifacts/T-621/result.txt'); "
            "p.parent.mkdir(parents=True, exist_ok=True); p.write_text('ok', encoding='utf-8')",
            cwd=worker, env=env, expect=0,
        )
        run_id = re.search(r"\b(run-[0-9a-f]{16})\b", started.stdout).group(1)
        self.agentctl(
            "run", "wait", run_id, "--timeout", "20",
            cwd=worker, env=env, expect=0,
        )
        self.agentctl(
            "finish", "--summary", "experiment output captured",
            "--tests", "run succeeded", cwd=worker, env=env, expect=0,
        )

    def test_release_refuses_a_worktree_moved_outside_the_registry(self):
        self.create_committed_task()
        lease = self.create_lease(self.worktree)
        moved = self.temp / "moved-worker"
        self.git("worktree", "move", str(self.worktree), str(moved))
        self.git("switch", "--detach", "-q", cwd=moved)

        rows = json.loads(
            self.agentctl("worktree", "list", "--json", expect=0).stdout
        )["worktrees"]
        self.assertEqual(rows[0]["observed_status"], "moved")
        refused = self.agentctl("worktree", "release", lease["id"], expect=1)
        self.assertIn("is moved", refused.stdout + refused.stderr)
        self.assertTrue(moved.is_dir())

    def test_moved_managed_worktree_cannot_start_another_task(self):
        self.create_committed_task(task="T-101", agent="worker")
        self.agentctl(
            "task", "create", "--id", "T-102", "--title", "unrelated task",
            "--owner", "worker", "--scope", "other/", expect=0,
        )
        self.commit("chore(agent): plan unrelated worker\n\nRefs: T-102")
        self.create_lease(self.worktree)
        moved = self.temp / "moved-worker"
        self.git("worktree", "move", str(self.worktree), str(moved))

        refused = self.agentctl(
            "start", "--task", "T-102", "--agent", "worker", cwd=moved, expect=1,
        )
        self.assertIn("managed worktree lease is moved", refused.stdout + refused.stderr)
        board = json.loads((moved / ".agent" / "board.json").read_text(encoding="utf-8"))
        self.assertEqual(board["tasks"]["T-102"]["status"], "todo")

    def test_git_worktree_enumeration_failure_is_fail_closed(self):
        self.create_committed_task()
        lease = self.create_lease(self.worktree)
        common = Path(self.git("rev-parse", "--git-common-dir"))
        if not common.is_absolute():
            common = self.root / common
        registry = common.resolve() / "agent-workflow" / "worktree-leases.json"
        before = registry.read_bytes()
        env = self.failing_worktree_list_env()

        listed = self.agentctl("worktree", "list", "--json", expect=2, env=env)
        self.assertIn("unable to inspect Git worktrees", listed.stdout + listed.stderr)
        started = self.agentctl(
            "start", "--task", "T-101", "--agent", "worker",
            cwd=self.worktree, expect=2, env=env,
        )
        self.assertIn("unable to inspect Git worktrees", started.stdout + started.stderr)
        diagnosed = self.agentctl("doctor", expect=1, env=env)
        self.assertIn("worktree leases", diagnosed.stdout + diagnosed.stderr)
        self.assertIn("unable to inspect Git worktrees", diagnosed.stdout + diagnosed.stderr)
        self.assertNotIn("Traceback", diagnosed.stdout + diagnosed.stderr)
        released = self.agentctl(
            "worktree", "release", lease["id"], "--ack-missing", expect=2, env=env,
        )
        self.assertIn("unable to inspect Git worktrees", released.stdout + released.stderr)
        duplicate = self.agentctl(
            "worktree", "create", "--task", "T-101", "--agent", "worker",
            "--path", str(self.temp / "duplicate"), expect=2, env=env,
        )
        self.assertIn("unable to inspect Git worktrees", duplicate.stdout + duplicate.stderr)
        self.assertEqual(registry.read_bytes(), before)
        self.assertTrue(self.worktree.is_dir())
        board = json.loads((self.worktree / ".agent" / "board.json").read_text(encoding="utf-8"))
        self.assertEqual(board["tasks"]["T-101"]["status"], "todo")

    def test_release_cleans_prunable_git_metadata(self):
        self.create_committed_task()
        lease = self.create_lease(self.worktree)
        shutil.rmtree(self.worktree)

        rows = json.loads(
            self.agentctl("worktree", "list", "--json", expect=0).stdout
        )["worktrees"]
        self.assertEqual(rows[0]["observed_status"], "prunable")
        refused = self.agentctl("worktree", "release", lease["id"], expect=1)
        self.assertIn("--ack-missing", refused.stdout + refused.stderr)
        self.agentctl(
            "worktree", "release", lease["id"], "--ack-missing", expect=0,
        )
        registered = self.git("worktree", "list", "--porcelain")
        self.assertNotIn(str(self.worktree), registered)

    def test_create_rejects_task_state_a_fresh_worker_cannot_claim(self):
        self.create_committed_task()
        self.agentctl("start", "--task", "T-101", "--agent", "worker", expect=0)
        self.commit("chore(agent): mark worker task active\n\nRefs: T-101")

        refused = self.agentctl(
            "worktree", "create", "--task", "T-101", "--agent", "worker",
            "--path", str(self.worktree), expect=1,
        )
        self.assertIn("requires a todo or ready task", refused.stdout + refused.stderr)
        self.assertFalse(self.worktree.exists())

    def test_create_rejects_an_agent_that_does_not_own_the_task(self):
        self.create_committed_task(agent="alice")
        refused = self.agentctl(
            "worktree", "create", "--task", "T-101", "--agent", "bob",
            "--path", str(self.worktree), expect=1,
        )
        self.assertIn("cannot claim it", refused.stdout + refused.stderr)
        self.assertFalse(self.worktree.exists())

    def test_create_rejects_unresolved_task_dependencies(self):
        self.agentctl(
            "task", "create", "--id", "T-100", "--title", "dependency",
            "--owner", "alice", "--scope", "dep/", expect=0,
        )
        self.agentctl(
            "task", "create", "--id", "T-101", "--title", "dependent worker",
            "--owner", "worker", "--scope", "src/", "--deps", "T-100", expect=0,
        )
        self.commit("chore(agent): plan dependent worker\n\nRefs: T-100 T-101")
        refused = self.agentctl(
            "worktree", "create", "--task", "T-101", "--agent", "worker",
            "--path", str(self.worktree), expect=1,
        )
        self.assertIn("unresolved dependencies", refused.stdout + refused.stderr)
        self.assertFalse(self.worktree.exists())

    def test_create_rejects_active_write_scope_conflicts(self):
        self.agentctl(
            "task", "create", "--id", "T-100", "--title", "active phase",
            "--owner", "alice", "--scope", "src/", expect=0,
        )
        self.agentctl("start", "--task", "T-100", "--agent", "alice", expect=0)
        self.agentctl(
            "task", "create", "--id", "T-101", "--title", "parallel phase",
            "--owner", "worker", "--scope", "src/", expect=0,
        )
        self.commit("chore(agent): plan conflicting workers\n\nRefs: T-100 T-101")
        refused = self.agentctl(
            "worktree", "create", "--task", "T-101", "--agent", "worker",
            "--path", str(self.worktree), expect=1,
        )
        self.assertIn("write-scope conflict", refused.stdout + refused.stderr)
        self.assertFalse(self.worktree.exists())

    def test_create_rejects_scope_conflicts_from_an_active_shared_lease(self):
        self.agentctl(
            "task", "create", "--id", "T-100", "--title", "first worker",
            "--owner", "alice", "--scope", "src/", expect=0,
        )
        self.agentctl(
            "task", "create", "--id", "T-101", "--title", "second worker",
            "--owner", "bob", "--scope", "src/", expect=0,
        )
        self.commit("chore(agent): plan parallel workers\n\nRefs: T-100 T-101")
        first = self.create_lease(self.worktree, task="T-100", agent="alice")
        self.agentctl(
            "start", "--task", "T-100", "--agent", "alice",
            cwd=self.worktree, expect=0,
        )
        self.git("add", "-A", cwd=self.worktree)
        self.git(
            "commit", "--no-verify", "-q", "-m",
            "chore(agent): claim first worker\n\nRefs: T-100", cwd=self.worktree,
        )

        refused = self.agentctl(
            "worktree", "create", "--task", "T-101", "--agent", "bob",
            "--path", str(self.temp / "bob"), expect=1,
        )
        self.assertIn(first["id"], refused.stdout + refused.stderr)
        self.assertIn("write-scope conflict", refused.stdout + refused.stderr)

    def test_create_rejects_overlapping_leases_for_the_same_agent_id(self):
        self.agentctl(
            "task", "create", "--id", "T-100", "--title", "first codex",
            "--owner", "codex", "--scope", "src/", expect=0,
        )
        self.agentctl(
            "task", "create", "--id", "T-101", "--title", "second codex",
            "--owner", "codex", "--scope", "src/", expect=0,
        )
        self.commit("chore(agent): plan codex workers\n\nRefs: T-100 T-101")
        first = self.create_lease(self.worktree, task="T-100", agent="codex")
        refused = self.agentctl(
            "worktree", "create", "--task", "T-101", "--agent", "codex",
            "--path", str(self.temp / "second"), expect=1,
        )
        self.assertIn(first["id"], refused.stdout + refused.stderr)
        self.assertIn("write-scope conflict", refused.stdout + refused.stderr)

    def test_managed_worker_cannot_change_its_leased_scope(self):
        self.create_committed_task()
        self.create_lease(self.worktree)
        refused = self.agentctl(
            "start", "--task", "T-101", "--agent", "worker",
            "--scope", "src/expanded/", cwd=self.worktree, expect=1,
        )
        self.assertIn("cannot change a managed worktree lease", refused.stdout + refused.stderr)
        self.agentctl(
            "start", "--task", "T-101", "--agent", "worker",
            cwd=self.worktree, expect=0,
        )

    def test_create_rejects_empty_agent_identity(self):
        self.create_committed_task(agent="any")
        refused = self.agentctl(
            "worktree", "create", "--task", "T-101", "--agent", "",
            "--path", str(self.worktree), expect=2,
        )
        self.assertIn("non-empty", refused.stdout + refused.stderr)

    def test_missing_registration_requires_explicit_release_acknowledgement(self):
        self.create_committed_task()
        lease = self.create_lease(self.worktree)
        moved = self.temp / "raw-moved-worker"
        shutil.move(self.worktree, moved)
        unfinished = moved / "unfinished.txt"
        unfinished.write_text("important\n", encoding="utf-8")
        self.git("worktree", "prune", "--expire", "now")

        rows = json.loads(
            self.agentctl("worktree", "list", "--json", expect=0).stdout
        )["worktrees"]
        self.assertEqual(rows[0]["observed_status"], "missing")
        refused = self.agentctl("worktree", "release", lease["id"], expect=1)
        self.assertIn("--ack-missing", refused.stdout + refused.stderr)
        self.assertTrue(unfinished.is_file())
        self.agentctl(
            "worktree", "release", lease["id"], "--ack-missing", expect=0,
        )
        self.assertTrue(unfinished.is_file())

    def test_interrupted_creation_recovery_backfills_stable_identity(self):
        self.create_committed_task()
        lease = self.create_lease(self.worktree)
        common = Path(self.git("rev-parse", "--git-common-dir"))
        if not common.is_absolute():
            common = self.root / common
        registry = common.resolve() / "agent-workflow" / "worktree-leases.json"
        data = json.loads(registry.read_text(encoding="utf-8"))
        data["leases"][0]["status"] = "creating"
        data["leases"][0].pop("git_dir", None)
        registry.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

        rows = json.loads(
            self.agentctl("worktree", "list", "--json", expect=0).stdout
        )["worktrees"]
        self.assertEqual(rows[0]["observed_status"], "active")
        recovered = json.loads(registry.read_text(encoding="utf-8"))["leases"][0]
        self.assertEqual(recovered["status"], "active")
        self.assertTrue(recovered.get("git_dir"))

        moved = self.temp / "recovered-moved-worker"
        self.git("worktree", "move", str(self.worktree), str(moved))
        self.git("switch", "--detach", "-q", cwd=moved)
        rows = json.loads(
            self.agentctl("worktree", "list", "--json", expect=0).stdout
        )["worktrees"]
        self.assertEqual(rows[0]["id"], lease["id"])
        self.assertEqual(rows[0]["observed_status"], "moved")

    def test_default_path_uses_checkout_root_with_separate_git_dir(self):
        separate_root = self.temp / "separate-project"
        separate_git = self.temp / "metadata" / "project.git"
        separate_root.mkdir()
        separate_git.parent.mkdir()
        self.git("init", "-q", "--separate-git-dir", str(separate_git), str(separate_root))
        self.git("config", "user.email", "agent@example.com", cwd=separate_root)
        self.git("config", "user.name", "Agent Test", cwd=separate_root)
        installed = subprocess.run(
            [sys.executable, str(KIT / "tools" / "agentctl.py"), "init", str(separate_root)],
            cwd=str(KIT), text=True, capture_output=True, timeout=120,
        )
        self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
        self.git("add", "-A", cwd=separate_root)
        self.git(
            "commit", "--no-verify", "-q", "-m",
            "chore(agent): install workflow\n\nRefs: T-024", cwd=separate_root,
        )
        self.agentctl(
            "task", "create", "--id", "T-201", "--title", "separate metadata",
            "--owner", "worker", "--scope", "src/", cwd=separate_root, expect=0,
        )
        self.git("add", "-A", cwd=separate_root)
        self.git(
            "commit", "--no-verify", "-q", "-m",
            "chore(agent): plan separate worker\n\nRefs: T-201", cwd=separate_root,
        )

        nested = self.agentctl(
            "worktree", "create", "--task", "T-201", "--agent", "worker",
            "--path", str(separate_root / "worker"), cwd=separate_root, expect=1,
        )
        self.assertIn("overlaps registered checkout", nested.stdout + nested.stderr)

        self.agentctl(
            "worktree", "create", "--task", "T-201", "--agent", "worker",
            cwd=separate_root, expect=0,
        )
        rows = json.loads(
            self.agentctl("worktree", "list", "--json", cwd=separate_root, expect=0).stdout
        )["worktrees"]
        expected_pool = separate_root.parent / "separate-project-worktrees"
        self.assertEqual(Path(rows[0]["path"]).parent, expected_pool.resolve())
        self.assertFalse(path_is_relative_to(Path(rows[0]["path"]), separate_git))

    def test_create_rejects_a_path_nested_in_an_existing_checkout(self):
        self.create_committed_task()
        nested = self.root / "worker"
        refused = self.agentctl(
            "worktree", "create", "--task", "T-101", "--agent", "worker",
            "--path", "worker", expect=1,
        )
        self.assertIn("overlaps registered checkout", refused.stdout + refused.stderr)
        self.assertFalse(nested.exists())
        self.assertEqual(self.git("status", "--porcelain", "--untracked-files=all"), "")


if __name__ == "__main__":
    unittest.main()
