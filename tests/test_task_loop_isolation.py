"""A worker's feedback, checks, and memory belong to its task, not its peers."""

import json
import os
import shlex
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from unittest import mock

from tools import agentctl
from tests.test_milestones import _MilestoneTestCase
from tests.test_loop_workflow import LOOP_CONTRACT


class TaskLoopIsolationTest(_MilestoneTestCase):
    def start(self, name):
        self.agentctl(
            "work", "--agent", "codex", "--auto-create", "--type", "docs",
            "--new-id", f"T-{name.upper()}", "--title", f"docs {name}",
            "--scope", f"docs/{name}/", "--done", "documentation complete",
            session=name,
        )

    def test_follow_up_failure_and_success_cannot_touch_another_task(self):
        with mock.patch.object(agentctl, "_load_session", return_value={"task": "T-A", "agent": "codex"}):
            a, _, _ = agentctl._create_loop_follow_up(self.root, "experiment-check", "failed", ["a-error"], True, 2)
        with mock.patch.object(agentctl, "_load_session", return_value={"task": "T-B", "agent": "codex"}):
            b, created, escalated = agentctl._create_loop_follow_up(self.root, "experiment-check", "failed", ["b-error"], True, 2)
            self.assertNotEqual(a, b)
            self.assertTrue(created)
            self.assertFalse(escalated)
            self.assertEqual(agentctl._close_loop_follow_ups(self.root, "experiment-check", "B fixed"), [b])
        packets = agentctl._loop_follow_up_packets(self.root)
        self.assertEqual(len(packets), 1)
        packet = packets[0][1]
        self.assertEqual((packet["id"], packet["to_task"], packet["occurrences"], packet["artifacts"]),
                         (a, "T-A", 1, ["a-error"]))

    def test_unrelated_malformed_document_does_not_block_finish(self):
        self.start("a")
        self.start("b")
        (self.root / ".agent/tasks/T-B.md").write_text("# T-B\nStatus: in_progress\n")
        self.agentctl("finish", "--summary", "A complete", "--tests", "isolated check", session="a")
        self.assertEqual(self.status("T-A"), "review")
        self.agentctl("refresh", session="b")
        result = self.agentctl("finish", "--summary", "B complete", "--tests", "isolated check",
                               "--done", "documentation complete", session="b", expect=1)
        self.assertIn("doc-hygiene", result.stdout + result.stderr)
        self.assertEqual(self.status("T-B"), "in_progress")

    def test_escalation_for_a_does_not_block_b_manual_check(self):
        self.start("a")
        self.start("b")
        with mock.patch.object(agentctl, "_load_session", return_value={"task": "T-A", "agent": "codex"}):
            agentctl._create_loop_follow_up(self.root, "experiment-check", "failed", ["a-error"], True, 1)
        self.agentctl("check", "--mode", "manual", session="a", expect=1)
        self.agentctl("check", "--mode", "manual", session="b")
        # A project-wide CI audit still reports unresolved escalations.
        self.agentctl("check", "--mode", "ci", expect=1)

    def test_checkpoint_debounce_and_doc_change_are_task_local(self):
        self.start("a")
        self.start("b")
        path = self.root / ".agent/loops/checkpoints.json"
        policy = json.loads(path.read_text())
        policy["checkpoints"]["local-docs"] = {"loops": ["doc-hygiene"], "strict": True,
                                               "debounce_minutes": 30}
        path.write_text(json.dumps(policy) + "\n")
        for session in ("a", "b"):
            self.agentctl("refresh", session=session)
            first = self.agentctl("loop", "auto", "--checkpoint", "local-docs", "--once", session=session)
            self.assertNotIn("skipped", first.stdout)
            second = self.agentctl("loop", "auto", "--checkpoint", "local-docs", "--once", session=session)
            self.assertIn("skipped", second.stdout)
        doc = self.root / ".agent/tasks/T-A.md"
        doc.write_text(doc.read_text() + "\nExtra task evidence.\n")
        self.agentctl("refresh", session="a")
        again = self.agentctl("loop", "auto", "--checkpoint", "local-docs", "--once", session="a")
        self.assertNotIn("skipped", again.stdout)
        peer = self.agentctl("loop", "auto", "--checkpoint", "local-docs", "--once", session="b")
        self.assertIn("skipped", peer.stdout)

    def test_running_cycle_stops_before_writing_after_claim_transfer(self):
        self.start("a")
        runner = subprocess.Popen(
            [sys.executable, "tools/agentctl.py", "loop", "cycle",
             "--checkpoint", "pre-finish", "--cycles", "2", "--interval", "2", "--force"],
            cwd=self.root, env=self.env("a"), text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        try:
            state_path = self.root / ".agent/loops/state.json"
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                runtime = json.loads(state_path.read_text()).get("cycle_runtime") or {}
                if runtime.get("status") == "running" and runtime.get("completed_cycles") == 1:
                    break
                self.assertIsNone(runner.poll(), "runner ended before its first interval")
                time.sleep(0.05)
            else:
                self.fail("runner did not reach its first interval")

            reports = set((self.root / ".agent/loops/runs").glob("*.md"))
            board_path = self.root / ".agent/board.json"
            board = self.board()
            board["tasks"]["T-A"].update(claim_id="replacement-claim", owner="peer")
            board_path.write_text(json.dumps(board) + "\n")
            stdout, stderr = runner.communicate(timeout=20)
            self.assertNotEqual(runner.returncode, 0, stdout + stderr)
            self.assertIn("claim authority changed", stdout + stderr)
            runtime = json.loads(state_path.read_text())["cycle_runtime"]
            self.assertEqual((runtime["status"], runtime["completed_cycles"]), ("blocked", 1))
            self.assertIsNone(runtime["inflight_cycle"])
            self.assertEqual(set((self.root / ".agent/loops/runs").glob("*.md")), reports)
            self.assertEqual(self.board()["tasks"]["T-A"]["claim_id"], "replacement-claim")
        finally:
            if runner.poll() is None:
                runner.kill()
                runner.communicate()

    def test_peer_cannot_resume_interrupted_cycle_but_owner_can(self):
        self.start("a")
        self.start("b")
        runner = subprocess.Popen(
            [sys.executable, "tools/agentctl.py", "loop", "cycle", "--checkpoint",
             "pre-finish", "--cycles", "2", "--interval", "30", "--force"],
            cwd=self.root, env=self.env("a"), text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        try:
            state_path = self.root / ".agent/loops/state.json"
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                runtime = json.loads(state_path.read_text()).get("cycle_runtime") or {}
                if runtime.get("status") == "running" and runtime.get("completed_cycles") == 1:
                    break
                self.assertIsNone(runner.poll(), "runner ended before its first interval")
                time.sleep(0.05)
            else:
                self.fail("runner did not reach its first interval")
            refused_stop = self.agentctl("loop", "stop", "--reason", "peer stop", session="b", expect=2)
            self.assertIn("different task/session claim", refused_stop.stdout + refused_stop.stderr)
            self.assertEqual(json.loads(state_path.read_text())["cycle_runtime"], runtime)
            runner.terminate()
            runner.communicate(timeout=20)
            normalized = json.loads(self.agentctl("loop", "status", "--json", session="a").stdout)
            self.assertEqual(normalized["status"], "interrupted")
            before = json.loads(state_path.read_text())["cycle_runtime"]
            reports = set((self.root / ".agent/loops/runs").glob("*.md"))
            refused = self.agentctl("loop", "resume", session="b", expect=2)
            self.assertIn("different task/session claim", refused.stdout + refused.stderr)
            self.assertEqual(json.loads(state_path.read_text())["cycle_runtime"], before)
            self.assertEqual(set((self.root / ".agent/loops/runs").glob("*.md")), reports)
            self.agentctl("loop", "resume", session="a")
            runtime = json.loads(state_path.read_text())["cycle_runtime"]
            self.assertEqual((runtime["status"], runtime["completed_cycles"]), ("completed", 2))
            for report in runtime["last_reports"]:
                self.assertIn("- Task: T-A", (self.root / report).read_text())
        finally:
            if runner.poll() is None:
                runner.kill()
                runner.communicate()

    def test_revoked_cycle_holder_can_stop_but_peer_cannot(self):
        self.start("a")
        self.start("b")
        with mock.patch.dict(os.environ, self.env("a"), clear=True):
            session = agentctl._load_session(self.root)
        runtime = {"id": "interrupted-owner-cycle", "status": "interrupted", "owner_pid": None,
                   "task": session["task"], "claim_id": session["claim_id"],
                   "workflow_session_key": session["workflow_session_key"], "resume_safe": True}
        state_path = self.root / ".agent/loops/state.json"
        state = json.loads(state_path.read_text())
        state["cycle_runtime"] = runtime
        state_path.write_text(json.dumps(state) + "\n")
        board_path = self.root / ".agent/board.json"
        board = self.board()
        board["tasks"]["T-A"].update(claim_id="replacement-claim", owner="peer")
        board_path.write_text(json.dumps(board) + "\n")
        self.agentctl("loop", "stop", "--reason", "not the holder", session="b", expect=2)
        self.assertEqual(json.loads(state_path.read_text())["cycle_runtime"], runtime)
        self.agentctl("loop", "stop", "--reason", "revoked holder cleanup", session="a")
        self.assertEqual(json.loads(state_path.read_text())["cycle_runtime"]["status"], "stopped")
        self.assertEqual(self.board()["tasks"]["T-A"]["claim_id"], "replacement-claim")

    def test_legacy_unbound_cycle_requires_exclusive_explicit_reconciliation(self):
        self.start("a")
        self.start("b")
        runtime = {"id": "legacy-cycle", "status": "interrupted", "owner_pid": None, "resume_safe": True}
        state_path = self.root / ".agent/loops/state.json"
        state = json.loads(state_path.read_text())
        state["cycle_runtime"] = runtime
        state_path.write_text(json.dumps(state) + "\n")
        self.agentctl("loop", "resume", session="a", expect=2)
        self.agentctl("loop", "stop", "--reason", "not explicitly reconciled", session="a", expect=2)
        args = ("loop", "stop", "--ack-inflight", "--reason", "verified legacy process and outputs")
        self.agentctl(*args, session="a", expect=2)
        self.assertEqual(json.loads(state_path.read_text())["cycle_runtime"], runtime)
        self.agentctl("sessions", "release", "--reason", "peer finished", session="b")
        self.agentctl(*args, session="a")
        self.assertEqual(json.loads(state_path.read_text())["cycle_runtime"]["status"], "stopped")

    def test_interrupted_one_shot_only_original_holder_can_reconcile(self):
        self.agentctl("work", "--agent", "codex", "--auto-create", "--type", "docs",
                      "--new-id", "T-A", "--title", "one-shot A", "--scope", "docs/a/,.agent/loops/",
                      "--done", "fixture complete", session="a")
        artifacts = self.root / ".agent-artifacts/T-A"
        artifacts.mkdir(parents=True)
        child = ("from pathlib import Path; import time; p=Path('.agent-artifacts/T-A'); "
                 "(p/'starts').touch(); time.sleep(2); (p/'dones').touch()")
        argv = [sys.executable, "-c", child]
        command = subprocess.list2cmdline(argv) if os.name == "nt" else shlex.join(argv)
        (self.root / ".agent/loops/owned-one-shot.md").write_text(
            LOOP_CONTRACT.format(loop_id="owned-one-shot", checkpoint="manual", command=command))
        self.agentctl("refresh", session="a")
        runner = subprocess.Popen(
            [sys.executable, "tools/agentctl.py", "loop", "run", "owned-one-shot", "--once"],
            cwd=self.root, env=self.env("a"), text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        state_path = self.root / ".agent/loops/state.json"
        try:
            deadline = time.monotonic() + 15
            while not (artifacts / "starts").exists():
                self.assertIsNone(runner.poll(), "one-shot runner ended before child start")
                self.assertLess(time.monotonic(), deadline, "one-shot child did not start")
                time.sleep(0.05)
            runner.terminate()
            runner.communicate(timeout=20)
            status = json.loads(self.agentctl("loop", "status", "--json", session="a").stdout)
            self.assertEqual(status["execution_lease"]["status"], "interrupted")
            deadline = time.monotonic() + 15
            while True:
                lease = json.loads(state_path.read_text())["execution_lease"]
                if (artifacts / "dones").exists() and not agentctl._active_command_alive(lease.get("active_command")):
                    break
                self.assertLess(time.monotonic(), deadline, "one-shot child did not exit")
                time.sleep(0.05)
            self.start("b")
            state = json.loads(state_path.read_text())
            args = ("loop", "stop", "--ack-inflight", "--reason", "verified child result")
            self.agentctl(*args, session="b", expect=2)
            self.assertEqual(json.loads(state_path.read_text()), state)
            board_path = self.root / ".agent/board.json"
            board = self.board()
            board["tasks"]["T-A"].update(claim_id="replacement-claim", owner="peer")
            board_path.write_text(json.dumps(board) + "\n")
            self.agentctl(*args, session="a")
            state = json.loads(state_path.read_text())
            self.assertNotIn("execution_lease", state)
            self.assertEqual(state["execution_history"][-1]["token"], lease["token"])
            self.assertEqual(self.board()["tasks"]["T-A"]["claim_id"], "replacement-claim")
        finally:
            if runner.poll() is None:
                runner.kill()
                runner.communicate()

    def test_experiment_monitor_does_not_scan_peer_results(self):
        for name, marker in (("a", "DONE"), ("b", "ERROR")):
            output = self.root / "results" / name
            output.mkdir(parents=True)
            (output / marker).touch()
        with mock.patch.object(agentctl, "_load_session", return_value={"task": "T-A", "scope": ["results/a/"]}):
            result = agentctl._loop_experiment_monitor(self.root)
        self.assertEqual(result["status"], "success", result)
        self.assertIn("ERROR markers: 0", result["feedback"])

    def test_experiment_monitor_checks_only_own_declared_run_outputs(self):
        leases = []
        for task, marker in (("T-A", "DONE"), ("T-B", "ERROR")):
            output = self.root / ".agent-artifacts" / task
            output.mkdir(parents=True)
            (output / marker).touch()
            leases.append({"task": task, "kind": "run", "outputs": [str(output)]})
        with mock.patch.object(agentctl, "_load_session", return_value={"task": "T-A", "scope": ["src/a/"]}), \
                mock.patch.object(agentctl, "_load_runtime_leases", return_value={"leases": leases}):
            result = agentctl._loop_experiment_monitor(self.root)
        self.assertEqual(result["status"], "success", result)
        self.assertIn("DONE markers: 1", result["feedback"])
        self.assertIn("ERROR markers: 0", result["feedback"])

    def test_experiment_monitor_does_not_rescan_nested_owned_outputs(self):
        output = self.root / "results/a"
        output.mkdir(parents=True)
        marker = output / "DONE"
        marker.touch()
        leases = [{"task": "T-A", "kind": "run", "outputs": [str(output), str(marker)]}]
        with mock.patch.object(agentctl, "_load_session", return_value={
                "task": "T-A", "scope": ["results/a/", "results/a/nested/"]}), \
                mock.patch.object(agentctl, "_load_runtime_leases", return_value={"leases": leases}):
            result = agentctl._loop_experiment_monitor(self.root)
        self.assertIn("DONE markers: 1", result["feedback"])

    def test_experiment_result_changes_bypass_debounce_and_close_own_failure(self):
        self.agentctl("work", "--agent", "codex", "--auto-create", "--type", "docs",
                      "--new-id", "T-A", "--title", "monitor A", "--scope", "results/a/",
                      "--done", "monitor complete", session="a")
        path = self.root / ".agent/loops/checkpoints.json"
        policy = json.loads(path.read_text())
        policy["checkpoints"]["experiment-check"]["strict"] = True
        path.write_text(json.dumps(policy) + "\n")
        self.agentctl("refresh", session="a")
        output = self.root / "results/a"
        output.mkdir(parents=True)
        (output / "DONE").touch()
        args = ("loop", "auto", "--checkpoint", "experiment-check", "--once")
        self.agentctl(*args, session="a")
        (output / "ERROR").touch()
        failure = self.agentctl(*args, session="a", expect=1)
        self.assertNotIn("skipped", failure.stdout)
        self.assertEqual(len(agentctl._loop_follow_up_packets(self.root)), 1)
        (output / "ERROR").unlink()
        recovery = self.agentctl(*args, session="a")
        self.assertIn("auto-closed", recovery.stdout)
        self.assertEqual(agentctl._loop_follow_up_packets(self.root), [])

    def test_cycle_records_only_current_task_reports_not_legacy_or_peer_history(self):
        self.start("a")
        args = ("loop", "cycle", "--checkpoint", "pre-finish", "--cycles", "1", "--force")
        state_path = self.root / ".agent/loops/state.json"
        for seed_legacy in (False, True):
            if seed_legacy:
                state = json.loads(state_path.read_text())
                state["checkpoints"]["pre-finish"] = {"last_reports": ["legacy/report.md"]}
                state["checkpoints"]["pre-finish:T-B"] = {"last_reports": ["peer/report.md"]}
                state_path.write_text(json.dumps(state) + "\n")
            self.agentctl(*args, session="a")
            state = json.loads(state_path.read_text())
            expected = state["checkpoints"]["pre-finish:T-A"]["last_reports"]
            self.assertTrue(expected)
            self.assertEqual(state["cycle_runtime"]["last_reports"], expected)

    def test_external_owned_outputs_detect_errors_without_peers_and_invalidate_cache(self):
        self.agentctl("work", "--agent", "codex", "--auto-create", "--type", "docs",
                      "--new-id", "T-A", "--title", "external monitor", "--scope", "results/a/",
                      "--done", "monitor complete", session="a")
        checkpoint_path = self.root / ".agent/loops/checkpoints.json"
        checkpoints = json.loads(checkpoint_path.read_text())
        checkpoints["checkpoints"]["experiment-check"]["strict"] = True
        checkpoint_path.write_text(json.dumps(checkpoints) + "\n")
        self.agentctl("refresh", session="a")
        internal = self.root / "results/a"
        internal.mkdir(parents=True)
        (internal / "DONE").touch()
        with tempfile.TemporaryDirectory(prefix="awk-owned-external-") as directory:
            external = Path(directory).resolve()
            policy_path = self.root / ".agent/runtime-policy.json"
            policy = json.loads(policy_path.read_text())
            policy["artifact_roots"] = [str(external)]
            policy_path.write_text(json.dumps(policy) + "\n")
            own, peer = external / "T-A", external / "T-B"
            own.mkdir()
            peer.mkdir()
            (peer / "ERROR").touch()
            args = ("loop", "auto", "--checkpoint", "experiment-check", "--once")
            for declared in (own, own / "ERROR"):
                with self.subTest(output=declared.name):
                    outputs, problems = agentctl._validate_run_outputs(
                        self.root, "T-A", ["results/a/"], [str(declared)])
                    self.assertFalse(problems)
                    agentctl._save_runtime_leases(self.root, {"leases": [
                        {"id": "run-own", "task": "T-A", "kind": "run", "status": "succeeded",
                         "outputs": outputs},
                        {"id": "run-peer", "task": "T-B", "kind": "run", "status": "succeeded",
                         "outputs": [str(peer)]},
                    ]})
                    self.agentctl(*args, "--force", session="a")
                    cached = self.agentctl(*args, session="a")
                    self.assertIn("skipped", cached.stdout)
                    (own / "ERROR").touch()
                    failure = self.agentctl(*args, session="a", expect=1)
                    self.assertNotIn("skipped", failure.stdout)
                    state = json.loads((self.root / ".agent/loops/state.json").read_text())
                    reports = state["checkpoints"]["experiment-check:T-A"]["last_reports"]
                    text = "\n".join((self.root / path).read_text() for path in reports)
                    self.assertIn("ERROR markers: 1", text)
                    self.assertIn(str(own / "ERROR"), text)
                    self.assertNotIn(str(peer), text)
                    (own / "ERROR").unlink()
                    recovery = self.agentctl(*args, session="a")
                    self.assertIn("auto-closed", recovery.stdout)
                    self.assertEqual(agentctl._loop_follow_up_packets(self.root), [])


class CICommitRangeTest(_MilestoneTestCase):
    def commit_object(self, subject, parent=None):
        tree = self.git("mktree").stdout.strip()
        args = ["commit-tree", tree, "-m", subject]
        if parent:
            args += ["-p", parent]
        return self.git(*args).stdout.strip()

    def test_ci_rejects_bad_commit_and_invalid_range(self):
        base = self.commit_object("fixture baseline")
        bad = self.commit_object("message without convention or task", base)
        result = self.agentctl("check", "--mode", "ci", "--commit-range", f"{base}..{bad}", expect=1)
        self.assertIn("commit not Conventional", result.stdout)
        self.assertIn("commit missing task ID", result.stdout)
        invalid = self.agentctl("check", "--mode", "ci", "--commit-range", "missing..HEAD", expect=1)
        self.assertIn("commit range", invalid.stdout.lower())
        self.agentctl("check", "--mode", "ci", "--commit-range=--max-count=0", expect=1)

    def test_ci_accepts_valid_commits_without_local_session_or_exclusivity(self):
        self.agentctl("work", "--agent", "codex", "--auto-create", "--new-id", "T-101",
                      "--title", "valid CI", "--scope", "src/a/", "--done", "complete", session="a")
        self.agentctl("work", "--agent", "codex", "--auto-create", "--new-id", "T-102",
                      "--title", "unrelated", "--scope", "src/b/", "--done", "complete", session="b")
        self.agentctl("finish", "--summary", "A complete", "--tests", "fixture", session="a")
        base = self.commit_object("fixture baseline")
        valid = self.commit_object("fix(test): verified task\n\nRefs: T-101", base)
        self.agentctl("check", "--mode", "ci", "--commit-range", f"{base}..{valid}")
