"""Clean Git state does not mean that a managed checkout has no writers."""

import json
import re
import subprocess
import sys
import time
import unittest
from pathlib import Path

from tests.test_task_contract import _ContractTestCase
from tools import agentctl


class WorktreeRetirementTest(_ContractTestCase):
    def setUp(self):
        super().setUp()
        self.git("add", *agentctl.ADOPTION_COMMIT_ADD_PATHS)
        self.git("commit", "-q", "-m", agentctl.ADOPTION_COMMIT_MESSAGE)
        self.agentctl(
            "work", "--agent", "codex", "--auto-create", "--new-id", "T-701",
            "--type", "code", "--title", "synthetic writer", "--scope", ".agent-artifacts/T-701/",
            "--done", "Declared synthetic output exists", session="planner",
        )
        self.lease = self.leases()[0]
        self.worker = Path(self.lease["path"])
        self.addCleanup(self.cleanup_worker)

    def cleanup_worker(self):
        if self.worker.exists():
            self.ok(self.worker, "writer", "sessions", "release", "--reason", "test cleanup")
            self.ok(self.root, "planner", "worktree", "release", self.lease["id"])
        if self.worker.parent.exists() and not any(self.worker.parent.iterdir()):
            self.worker.parent.rmdir()

    def cleanup_run(self, run):
        state = json.loads(self.ok(self.root, "planner", "run", "show", run, "--json").stdout)["runs"][0]
        if state["status"] in {"cancelled", "succeeded", "failed"}:
            self.wait_for_run_exit(run)
            return
        if not self.worker.exists():
            self.git("worktree", "add", str(self.worker), self.lease["branch"])
        self.ok(self.worker, "writer", "run", "stop", run, "--reason", "test cleanup")
        self.wait_for_run_exit(run)

    def wait_for_run_exit(self, run):
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            state = json.loads(self.ok(self.root, "planner", "run", "show", run, "--json").stdout)["runs"][0]
            if state["status"] in {"cancelled", "succeeded", "failed"} and not any(
                agentctl._runtime_process_alive(process)
                for process in [*state.get("processes", []), state.get("supervisor_process")]
            ):
                return
            time.sleep(0.1)
        self.fail("run did not fully exit: " + repr(state))

    def ctl(self, checkout, session, *args):
        return subprocess.run(
            [sys.executable, str(self.root / "tools/agentctl.py"), *args],
            cwd=str(checkout), env=self.env(session), text=True,
            capture_output=True, timeout=120,
        )

    def ok(self, checkout, session, *args):
        result = self.ctl(checkout, session, *args)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def leases(self):
        return json.loads(self.ok(self.root, "planner", "worktree", "list", "--json").stdout)["worktrees"]

    def claim_clean_worker(self):
        self.ok(self.worker, "writer", "work", "--agent", "codex", "--task", "T-701")
        for args in (
            ("add", ".agent"),
            ("commit", "-q", "-m", "chore(agent): checkpoint writer claim\n\nRefs: T-701"),
        ):
            result = subprocess.run(
                ["git", *args], cwd=str(self.worker), env=self.env("writer"),
                text=True, capture_output=True, timeout=120,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assert_clean_worker()

    def assert_clean_worker(self):
        result = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=all"],
            cwd=str(self.worker), text=True, capture_output=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "", result.stdout)

    def refuse_retirement(self, blocker):
        before_board = (self.worker / ".agent/board.json").read_bytes()
        result = self.ctl(self.root, "planner", "worktree", "release", self.lease["id"])
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn(blocker, result.stdout + result.stderr)
        self.assertTrue(self.worker.is_dir())
        self.assertEqual((self.worker / ".agent/board.json").read_bytes(), before_board)
        self.assertEqual(self.leases()[0]["observed_status"], "active")

    def release_idle_worker(self):
        self.ok(self.worker, "writer", "sessions", "release", "--reason", "writer drained")
        self.ok(self.root, "planner", "worktree", "release", self.lease["id"])
        self.assertFalse(self.worker.exists())
        self.assertEqual(self.leases()[0]["observed_status"], "released")
        self.git("show-ref", "--verify", "refs/heads/" + self.lease["branch"])

    def test_clean_released_worktree_can_be_retired(self):
        self.claim_clean_worker()
        self.ok(self.worker, "writer", "sessions", "release", "--reason", "writer drained")
        self.ok(self.root, "planner", "worktree", "release", self.lease["id"])
        self.assertFalse(self.worker.exists())
        self.git("show-ref", "--verify", "refs/heads/" + self.lease["branch"])

    def test_clean_active_checkout_is_not_retired_and_peer_keeps_working(self):
        self.claim_clean_worker()
        self.open_task("peer", "independent peer", ".agent-artifacts/peer/")
        self.refuse_retirement("session")
        self.ok(self.root, "peer", "note", "Peer still progresses after refused retirement")
        self.release_idle_worker()
        self.ok(self.root, "peer", "note", "Peer still progresses after lawful retirement")

    def test_clean_stale_checkout_requires_explicit_session_release(self):
        self.claim_clean_worker()
        common = Path(self.git("rev-parse", "--git-common-dir").stdout.strip())
        if not common.is_absolute():
            common = self.root / common
        rows = json.loads(self.ok(self.worker, "writer", "sessions", "list", "--json").stdout)["sessions"]
        key = next(row["workflow_session_key"] for row in rows if row["task"] == "T-701")
        record = next(common.rglob(key + "-*.json"))
        state = json.loads(record.read_text(encoding="utf-8"))
        state["heartbeat_ns"] = time.time_ns() - int((agentctl.SESSION_STALE_SECONDS + 60) * 1e9)
        record.write_text(json.dumps(state), encoding="utf-8")
        self.refuse_retirement("stale")
        self.release_idle_worker()

    def test_released_session_does_not_hide_a_live_supervised_run(self):
        self.claim_clean_worker()
        output = ".agent-artifacts/T-701/ready.txt"
        script = (
            "from pathlib import Path; import time; "
            "p=Path(" + repr(output) + "); p.parent.mkdir(parents=True, exist_ok=True); "
            "p.write_text('ready'); time.sleep(60)"
        )
        started = self.ok(self.worker, "writer", "run", "start", "--output", output, "--", sys.executable, "-c", script)
        run = re.search(r"\brun-[0-9a-f]{16}\b", started.stdout).group()
        self.addCleanup(self.cleanup_run, run)
        deadline = time.monotonic() + 15
        while not (self.worker / output).exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertTrue((self.worker / output).exists(), started.stdout)
        self.ok(self.worker, "writer", "sessions", "release", "--reason", "conversation exits before its run")
        self.assert_clean_worker()
        self.refuse_retirement(run)
        self.ok(self.worker, "writer", "run", "stop", run, "--reason", "run drained before retirement")
        self.wait_for_run_exit(run)
        self.ok(self.root, "planner", "worktree", "release", self.lease["id"])
        self.assertFalse(self.worker.exists())

    @unittest.skipIf(sys.platform == "win32", "run adopt cwd probes are POSIX-only")
    def test_adopted_run_remains_a_blocker_until_its_unknown_exit_is_reconciled(self):
        self.claim_clean_worker()
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            cwd=str(self.worker), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        self.addCleanup(self.cleanup_process, process)
        adopted = self.ok(
            self.worker, "writer", "run", "adopt", "--pid", str(process.pid),
            "--cwd", str(self.worker), "--output", ".agent-artifacts/T-701/adopted.txt",
        )
        run = re.search(r"\brun-[0-9a-f]{16}\b", adopted.stdout).group()
        self.ok(self.worker, "writer", "sessions", "release", "--reason", "adopted run retains ownership")
        self.refuse_retirement(run)
        self.ok(self.worker, "writer", "run", "stop", run, "--reason", "stop adopted fixture")
        process.wait(timeout=15)
        self.refuse_retirement("exited_unknown")
        self.ok(self.worker, "writer", "run", "finish", run, "--status", "cancelled", "--reason", "verified fixture exited")
        self.ok(self.root, "planner", "worktree", "release", self.lease["id"])
        self.assertFalse(self.worker.exists())

    def cleanup_process(self, process):
        if process.poll() is None:
            process.terminate()
        process.wait(timeout=15)

    def test_run_admission_and_retirement_use_the_same_coordination_lock(self):
        self.claim_clean_worker()
        lock = agentctl._session_coordination_lock_path(self.root)
        fd = agentctl._acquire_lock_file(lock)
        commands = (
            (self.root, "planner", ("worktree", "release", self.lease["id"])),
            (self.worker, "writer", ("run", "start", "--output", ".agent-artifacts/T-701/race.txt", "--", sys.executable, "-c", "import time; time.sleep(60)")),
        )
        processes = []
        try:
            for checkout, session, args in commands:
                process = subprocess.Popen(
                    [sys.executable, str(self.root / "tools/agentctl.py"), *args],
                    cwd=str(checkout), env=self.env(session), text=True,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                )
                processes.append(process)
                self.addCleanup(self.cleanup_process, process)
            for process in processes:
                with self.assertRaises(subprocess.TimeoutExpired):
                    process.communicate(timeout=0.4)
        finally:
            agentctl._release_lock_file(lock, fd)
        results = [process.communicate(timeout=120) for process in processes]
        self.assertEqual(processes[0].returncode, 1, repr(results[0]))
        self.assertEqual(processes[1].returncode, 0, repr(results[1]))
        run = re.search(r"\brun-[0-9a-f]{16}\b", results[1][0]).group()
        self.addCleanup(self.cleanup_run, run)
        self.assertTrue(self.worker.is_dir())
        self.ok(self.root, "planner", "worktree", "list", "--json")
