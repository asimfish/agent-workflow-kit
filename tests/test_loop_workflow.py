"""Regression tests for the loop feedback chain in fresh installed projects.

Each test installs the kit into a temporary Git repository and replays the
T-008 dogfood scenario: a failing custom loop creates a follow-up packet,
repeated failures escalate, escalation blocks check/finish, fixing the loop
auto-closes the packet, and --ack-escalations records a deliberate override.
"""

import importlib.util
import errno
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

KIT = Path(__file__).resolve().parents[1]

LOOP_CONTRACT = """# Loop: {loop_id}

Purpose: regression custom loop for the loop feedback chain.

## Trigger

- Mode: manual
- Checkpoint: {checkpoint}

## Execute

- Agent: codex
- Task: active task
- Allowed Writes:
  - .agent/loops/runs/

## Check

```loop-check
timeout: 30
max-output: 1000
$ {command}
```

## Feedback

- Failure should create or update one loop follow-up packet.

## Memory

- Write reports under `.agent/loops/runs/` and update `.agent/loops/state.json`.

## Next

- Stop after one run.
"""


class LoopWorkflowRegressionTest(unittest.TestCase):
    def setUp(self):
        identity = mock.patch.dict(
            os.environ, {"AGENT_WORKFLOW_SESSION_ID": "loop-regression-session"},
        )
        identity.start()
        self.addCleanup(identity.stop)
        self.root = Path(tempfile.mkdtemp(prefix="awk-loop-regress-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        git = subprocess.run(["git", "init", "-q"], cwd=str(self.root),
                             text=True, capture_output=True, timeout=60)
        self.assertEqual(git.returncode, 0, git.stdout + git.stderr)
        install = subprocess.run(
            [sys.executable, str(KIT / "tools" / "agentctl.py"), "init", str(self.root)],
            cwd=str(KIT), text=True, capture_output=True, timeout=120)
        self.assertEqual(install.returncode, 0, install.stdout + install.stderr)

    def agentctl(self, *args, expect=None):
        proc = subprocess.run(
            [sys.executable, "tools/agentctl.py", *args],
            cwd=str(self.root), text=True, capture_output=True, timeout=120)
        if expect is not None:
            self.assertEqual(
                proc.returncode, expect,
                f"agentctl {' '.join(args)} rc={proc.returncode}\n{proc.stdout}\n{proc.stderr}")
        return proc

    def write_loop(self, loop_id, checkpoint, command):
        loop_path = self.root / ".agent" / "loops" / f"{loop_id}.md"
        loop_path.write_text(
            LOOP_CONTRACT.format(loop_id=loop_id, checkpoint=checkpoint, command=command),
            encoding="utf-8")

    def test_checkpoint_debounce_is_bypassed_when_coordination_docs_change(self):
        """The plan-triage contract says 'do not rerun ... unless the plan
        changed'. Time-only debounce silently skips a genuinely-changed plan;
        a coordination-doc change must bypass the window."""
        self.agentctl("work", "--agent", "codex", "--auto-create",
                      "--title", "debounce", "--scope", "src/", expect=0)
        # A checkpoint with a real debounce window.
        self.write_loop("dbnc", "dbnc", "true")
        policy_path = self.root / ".agent" / "loops" / "checkpoints.json"
        policy = json.loads(policy_path.read_text(encoding="utf-8"))
        policy["checkpoints"]["dbnc"] = {
            "loops": ["dbnc"], "strict": False,
            "debounce_minutes": 30, "escalate_after": 3,
        }
        policy_path.write_text(json.dumps(policy, indent=2) + "\n", encoding="utf-8")
        self.agentctl("refresh", expect=0)

        first = self.agentctl("loop", "auto", "--checkpoint", "dbnc", "--once", expect=0)
        self.assertNotIn("skipped", (first.stdout + first.stderr).lower())
        # No change -> within 30m -> debounced.
        second = self.agentctl("loop", "auto", "--checkpoint", "dbnc", "--once", expect=0)
        self.assertIn("skipped", (second.stdout + second.stderr).lower())
        # Change a coordination doc -> must run despite the window.
        plan = self.root / ".agent" / "PROJECT_PLAN.md"
        plan.write_text(plan.read_text(encoding="utf-8") + "\nNEW PRIORITY\n", encoding="utf-8")
        third = self.agentctl("loop", "auto", "--checkpoint", "dbnc", "--once", expect=0)
        self.assertNotIn("skipped", (third.stdout + third.stderr).lower(),
                         "debounce suppressed a run after the plan changed")
        # Unchanged again -> debounced once more.
        fourth = self.agentctl("loop", "auto", "--checkpoint", "dbnc", "--once", expect=0)
        self.assertIn("skipped", (fourth.stdout + fourth.stderr).lower())

    def test_loop_command_cannot_write_into_a_peer_scope(self):
        """A loop Check command must not become a cross-session write channel.

        loop run/auto/cycle execute an arbitrary shell command outside the
        PreToolUse guard. With a live peer sharing the checkout, a loop command
        writing into the peer's scope must be refused. (Solo sessions keep full
        freedom, consistent with the opaque/contamination policy.)
        """
        (self.root / "src" / "one").mkdir(parents=True, exist_ok=True)
        (self.root / "src" / "two").mkdir(parents=True, exist_ok=True)
        victim = self.root / "src" / "two" / "victim.txt"
        victim.write_text("precious\n", encoding="utf-8")
        # Peer B (distinct session) owns src/two.
        peer_env = os.environ.copy()
        peer_env["AGENT_WORKFLOW_SESSION_ID"] = "loop-scope-peer"
        peer = subprocess.run(
            [sys.executable, "tools/agentctl.py", "work", "--agent", "cursor",
             "--auto-create", "--new-id", "T-PEER", "--title", "peer",
             "--scope", "src/two/"],
            cwd=str(self.root), env=peer_env, text=True, capture_output=True, timeout=120)
        self.assertEqual(peer.returncode, 0, peer.stdout + peer.stderr)
        # A (the default test session) owns src/one and defines loops.
        self.agentctl("work", "--agent", "codex", "--auto-create",
                      "--title", "loop scope", "--scope", "src/one/,.agent/loops/",
                      expect=0)

        self.write_loop("evil", "evil", "echo pwned > src/two/victim.txt")
        self.add_checkpoint("evil", "evil", escalate_after=3)
        run = self.agentctl("loop", "run", "evil", "--once")
        self.assertEqual(victim.read_text(encoding="utf-8"), "precious\n",
                         "loop command wrote into a peer's scope")
        self.assertNotEqual(run.returncode, 0, run.stdout + run.stderr)

        # A traversal target into the peer scope is also refused.
        self.write_loop("evil2", "evil2", "echo x > src/one/../two/sneak.txt")
        self.add_checkpoint("evil2", "evil2", escalate_after=3)
        self.agentctl("loop", "run", "evil2", "--once")
        self.assertFalse((self.root / "src" / "two" / "sneak.txt").exists())

        # An in-scope command still runs.
        self.write_loop("good", "good", "echo ok > src/one/loopout.txt")
        self.add_checkpoint("good", "good", escalate_after=3)
        self.agentctl("loop", "run", "good", "--once", expect=0)
        self.assertEqual((self.root / "src" / "one" / "loopout.txt").read_text().strip(), "ok")

    def test_solo_loop_command_is_not_scope_restricted(self):
        """A session alone in its checkout keeps full loop freedom (efficiency)."""
        self.agentctl("work", "--agent", "codex", "--auto-create",
                      "--title", "solo loop", "--scope", "src/one/,.agent/loops/",
                      expect=0)
        (self.root / "out").mkdir(parents=True, exist_ok=True)
        self.write_loop("solo", "solo", "echo ok > out/result.txt")
        self.add_checkpoint("solo", "solo", escalate_after=3)
        self.agentctl("loop", "run", "solo", "--once", expect=0)
        self.assertEqual((self.root / "out" / "result.txt").read_text().strip(), "ok")

    def add_checkpoint(self, name, loop_id, escalate_after):
        policy_path = self.root / ".agent" / "loops" / "checkpoints.json"
        policy = json.loads(policy_path.read_text(encoding="utf-8"))
        policy["checkpoints"][name] = {
            "loops": [loop_id],
            "strict": True,
            "debounce_minutes": 0,
            "escalate_after": escalate_after,
        }
        policy_path.write_text(json.dumps(policy, indent=2) + "\n", encoding="utf-8")
        self.agentctl("refresh", expect=0)

    def inbox_packets(self):
        return sorted((self.root / ".agent" / "bus" / "inbox").rglob("*.json"))

    def guidance_packets(self):
        proc = self.agentctl("guidance", "list", "--json", expect=0)
        return json.loads(proc.stdout)["guidance"]

    def load_agentctl_module(self, name):
        spec = importlib.util.spec_from_file_location(name, KIT / "tools" / "agentctl.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def set_cycle_runtime(self, **overrides):
        state_path = self.root / ".agent" / "loops" / "state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        runtime = {
            "version": 1,
            "id": "cycle-test-runtime",
            "checkpoint": "resume-check",
            "status": "running",
            "requested_cycles": 3,
            "completed_cycles": 1,
            "failures": 0,
            "first_failure_code": 0,
            "max_failures": 3,
            "interval_seconds": 0,
            "trigger": "regression",
            "strict": True,
            "force": True,
            "continue_on_failure": True,
            "owner_pid": 99999999,
            "owner_host": "",
            "started_at": "2026-01-01 00:00:00",
            "updated_at": "2026-01-01 00:00:00",
            "finished_at": None,
            "last_cycle_at": "2026-01-01 00:00:00",
            "last_return_code": 0,
            "last_reports": [],
            "stop_reason": None,
            "inflight_cycle": None,
            "active_command": None,
            "resume_safe": True,
            "events": [],
        }
        runtime.update(overrides)
        state["cycle_runtime"] = runtime
        state_path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
        return runtime

    def test_fail_escalate_block_fix_autoclose(self):
        self.agentctl("work", "--agent", "codex", "--auto-create",
                      "--title", "loop regression", "--scope", ".agent/,src/", expect=0)
        healthy = self.agentctl("doctor", "--json", expect=0)
        self.assertTrue(json.loads(healthy.stdout)["ok"])

        self.write_loop("regress-fail", "regress-check", "exit 7")
        self.add_checkpoint("regress-check", "regress-fail", escalate_after=2)

        self.agentctl("loop", "auto", "--checkpoint", "regress-check", "--once",
                      "--trigger", "fail-1", expect=1)
        packets = self.inbox_packets()
        self.assertEqual(len(packets), 1, packets)
        pkt = json.loads(packets[0].read_text(encoding="utf-8"))
        self.assertEqual(pkt["occurrences"], 1)
        self.assertFalse(pkt.get("escalated"))

        self.agentctl("loop", "auto", "--checkpoint", "regress-check", "--once",
                      "--trigger", "fail-2", expect=1)
        self.assertEqual(self.inbox_packets(), packets, "repeat failure must update, not add")
        pkt = json.loads(packets[0].read_text(encoding="utf-8"))
        self.assertEqual(pkt["occurrences"], 2)
        self.assertTrue(pkt.get("escalated"))

        check = self.agentctl("check", "--mode", "manual", expect=1)
        self.assertIn("escalated loop follow-up", (check.stdout + check.stderr).lower())
        doctor = self.agentctl("doctor", "--json", expect=1)
        doctor_report = json.loads(doctor.stdout)
        self.assertFalse(doctor_report["ok"])
        self.assertTrue(
            any("escalated loop follow-up" in problem for problem in doctor_report["problems"]),
            doctor_report["problems"])

        blocked = self.agentctl("complete", "--summary", "should block",
                                "--tests", "n/a", expect=1)
        self.assertIn("finish blocked", blocked.stdout + blocked.stderr)

        self.write_loop("regress-fail", "regress-check", "true")
        fixed = self.agentctl("loop", "auto", "--checkpoint", "regress-check", "--once",
                              "--trigger", "fixed", expect=0)
        self.assertIn("auto-closed", fixed.stdout + fixed.stderr)
        self.assertEqual(self.inbox_packets(), [])
        done_packets = sorted((self.root / ".agent" / "bus" / "done").glob("*.json"))
        self.assertTrue(done_packets, "auto-closed packet must be archived in bus/done")
        done = json.loads(done_packets[0].read_text(encoding="utf-8"))
        self.assertEqual(done["status"], "done")
        self.assertTrue(done.get("escalated"))

        self.agentctl("check", "--mode", "manual", expect=0)
        self.agentctl("finish", "--summary", "loop regression passed",
                      "--tests", "fail/escalate/block/fix/auto-close", expect=0)

    def test_ack_escalations_records_override(self):
        self.agentctl("work", "--agent", "codex", "--auto-create",
                      "--title", "ack regression", "--scope", ".agent/", expect=0)
        self.write_loop("regress-ack", "ack-check", "exit 9")
        self.add_checkpoint("ack-check", "regress-ack", escalate_after=1)

        first = self.agentctl("loop", "auto", "--checkpoint", "ack-check", "--once",
                              "--trigger", "fail", expect=1)
        self.assertIn("escalated", (first.stdout + first.stderr).lower())

        acked = self.agentctl("complete", "--summary", "ack allowed",
                              "--tests", "n/a", "--ack-escalations", expect=0)
        self.assertIn("escalation acknowledged", acked.stdout + acked.stderr)
        packets = self.inbox_packets()
        self.assertEqual(len(packets), 1, packets)
        pkt = json.loads(packets[0].read_text(encoding="utf-8"))
        self.assertEqual(pkt.get("acknowledged_by"), "codex")

    def test_cycle_accumulates_feedback_and_autocloses(self):
        self.agentctl("work", "--agent", "codex", "--auto-create",
                      "--title", "cycle regression", "--scope", ".agent/", expect=0)
        self.write_loop("regress-cycle", "cycle-check", "exit 5")
        self.add_checkpoint("cycle-check", "regress-cycle", escalate_after=2)

        cycled = self.agentctl(
            "loop", "cycle", "--checkpoint", "cycle-check",
            "--cycles", "2", "--continue-on-failure",
            "--trigger", "cycle-regress", expect=1)
        combined = cycled.stdout + cycled.stderr
        self.assertIn("loop cycle 1/2", combined)
        self.assertIn("loop cycle 2/2", combined)
        self.assertIn("loop cycle blocked", combined)

        status = json.loads(self.agentctl("loop", "status", "--json", expect=0).stdout)
        self.assertEqual(status["status"], "blocked")
        self.assertEqual(status["runtime"]["completed_cycles"], 2)

        packets = self.inbox_packets()
        self.assertEqual(len(packets), 1, packets)
        pkt = json.loads(packets[0].read_text(encoding="utf-8"))
        self.assertEqual(pkt["occurrences"], 2)
        self.assertTrue(pkt.get("escalated"))

        self.write_loop("regress-cycle", "cycle-check", "true")
        fixed = self.agentctl(
            "loop", "cycle", "--checkpoint", "cycle-check",
            "--cycles", "1", "--trigger", "cycle-fixed", expect=0)
        self.assertIn("auto-closed", fixed.stdout + fixed.stderr)
        self.assertEqual(self.inbox_packets(), [])

    def test_cycle_runtime_records_completion_and_status(self):
        self.agentctl("work", "--agent", "codex", "--auto-create",
                      "--title", "runtime status regression", "--scope", ".agent/", expect=0)
        self.write_loop("regress-runtime", "runtime-check", "true")
        self.add_checkpoint("runtime-check", "regress-runtime", escalate_after=3)

        cycled = self.agentctl(
            "loop", "cycle", "--checkpoint", "runtime-check",
            "--cycles", "2", "--trigger", "runtime-regress", expect=0)
        self.assertIn("loop runtime started", cycled.stdout)
        self.assertIn("loop cycle finished successfully (2/2)", cycled.stdout)

        payload = json.loads(self.agentctl("loop", "status", "--json", expect=0).stdout)
        runtime = payload["runtime"]
        self.assertEqual(payload["status"], "completed")
        self.assertEqual(runtime["completed_cycles"], 2)
        self.assertEqual(runtime["failures"], 0)
        self.assertIsNone(runtime["owner_pid"])
        self.assertIsNone(runtime["inflight_cycle"])
        self.assertIsNone(runtime["active_command"])
        self.assertTrue(runtime["resume_safe"])
        self.assertEqual(
            [event["event"] for event in runtime["events"]].count("cycle_finished"), 2)

    def test_orphaned_cycle_is_detected_and_resumes_remaining_work(self):
        self.agentctl("work", "--agent", "codex", "--auto-create",
                      "--title", "runtime resume regression", "--scope", ".agent/", expect=0)
        self.write_loop("regress-resume", "resume-check", "true")
        self.add_checkpoint("resume-check", "regress-resume", escalate_after=3)
        self.set_cycle_runtime()

        interrupted = json.loads(
            self.agentctl("loop", "status", "--json", expect=0).stdout)
        self.assertEqual(interrupted["status"], "interrupted")
        self.assertIn("no longer running", interrupted["runtime"]["stop_reason"])

        resumed = self.agentctl("loop", "resume", expect=0)
        combined = resumed.stdout + resumed.stderr
        self.assertIn("resumed", combined)
        self.assertIn("loop cycle 2/3", combined)
        self.assertIn("loop cycle 3/3", combined)

        completed = json.loads(
            self.agentctl("loop", "status", "--json", expect=0).stdout)
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["runtime"]["completed_cycles"], 3)
        self.assertTrue(any(
            event["event"] == "resumed" for event in completed["runtime"]["events"]))

    def test_dead_owner_with_terminal_predicate_is_finalized_not_stranded(self):
        self.agentctl("work", "--agent", "codex", "--auto-create",
                      "--title", "terminal recovery regression", "--scope", ".agent/", expect=0)
        self.set_cycle_runtime(requested_cycles=1, completed_cycles=1, max_failures=1)

        completed = json.loads(
            self.agentctl("loop", "status", "--json", expect=0).stdout)
        self.assertEqual(completed["status"], "completed")

        self.set_cycle_runtime(
            requested_cycles=3, completed_cycles=2, failures=2,
            first_failure_code=4, max_failures=2)
        failed = json.loads(
            self.agentctl("loop", "status", "--json", expect=0).stdout)
        self.assertEqual(failed["status"], "failed")
        self.assertIn("failure budget exhausted", failed["runtime"]["stop_reason"])

    def test_live_runtime_rejects_concurrency_and_accepts_cooperative_stop(self):
        self.agentctl("work", "--agent", "codex", "--auto-create",
                      "--title", "runtime stop regression", "--scope", ".agent/", expect=0)
        self.write_loop("regress-resume", "resume-check", "true")
        self.add_checkpoint("resume-check", "regress-resume", escalate_after=3)
        runtime = self.set_cycle_runtime(owner_pid=os.getpid(), completed_cycles=0)

        refused = self.agentctl(
            "loop", "cycle", "--checkpoint", "resume-check", "--cycles", "1", expect=2)
        self.assertIn("unfinished loop runtime", refused.stdout + refused.stderr)

        stopped = self.agentctl(
            "loop", "stop", "--reason", "regression stop", expect=0)
        self.assertIn("stop requested", stopped.stdout)
        waiting = json.loads(
            self.agentctl("loop", "status", "--json", expect=0).stdout)
        self.assertEqual(waiting["status"], "stop_requested")

        module = self.load_agentctl_module("agentctl_stop_cas_test")
        stale_update = module._cycle_runtime_update(
            self.root,
            runtime["id"],
            lambda current: current.update(status="running"),
            expected_statuses={"running"},
            expected_owner_pid=os.getpid(),
        )
        self.assertIsNone(stale_update, "a stale runner must not erase stop_requested")

        self.set_cycle_runtime(
            id=runtime["id"], owner_pid=99999999, status="stop_requested",
            completed_cycles=0, stop_reason="regression stop")
        settled = json.loads(
            self.agentctl("loop", "status", "--json", expect=0).stdout)
        self.assertEqual(settled["status"], "stopped")
        self.assertEqual(settled["runtime"]["stop_reason"], "regression stop")

    def test_running_cycle_honors_cooperative_stop_between_cycles(self):
        self.agentctl("work", "--agent", "codex", "--auto-create",
                      "--title", "cooperative stop regression", "--scope", ".agent/", expect=0)
        self.write_loop("regress-stop", "stop-check", "true")
        self.add_checkpoint("stop-check", "regress-stop", escalate_after=3)

        runner = subprocess.Popen(
            [sys.executable, "tools/agentctl.py", "loop", "cycle",
             "--checkpoint", "stop-check", "--cycles", "3", "--interval", "30"],
            cwd=str(self.root), text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.addCleanup(lambda: runner.poll() is None and runner.kill())
        state_path = self.root / ".agent" / "loops" / "state.json"
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            state = json.loads(state_path.read_text(encoding="utf-8"))
            runtime = state.get("cycle_runtime") or {}
            if runtime.get("status") == "running" and runtime.get("completed_cycles", 0) >= 1:
                break
            time.sleep(0.05)
        else:
            runner.kill()
            self.fail("cycle runner did not reach its cooperative sleep window")

        request = self.agentctl(
            "loop", "stop", "--reason", "test requested stop", expect=0)
        self.assertIn("stop requested", request.stdout)
        stdout, stderr = runner.communicate(timeout=10)
        self.assertEqual(runner.returncode, 0, stdout + stderr)
        self.assertIn("stopped at 1/3", stdout + stderr)

        stopped = json.loads(
            self.agentctl("loop", "status", "--json", expect=0).stdout)
        self.assertEqual(stopped["status"], "stopped")
        self.assertEqual(stopped["runtime"]["completed_cycles"], 1)

        refused = self.agentctl("loop", "resume", expect=2)
        self.assertIn("terminal (stopped)", refused.stdout + refused.stderr)
        still_stopped = json.loads(
            self.agentctl("loop", "status", "--json", expect=0).stdout)
        self.assertEqual(still_stopped["status"], "stopped")

    @unittest.skipUnless(os.name == "posix", "process-group reconciliation requires POSIX")
    def test_killed_runner_does_not_resume_an_unknown_inflight_command(self):
        self.agentctl("work", "--agent", "codex", "--auto-create",
                      "--title", "inflight recovery regression", "--scope", ".agent/", expect=0)
        self.write_loop(
            "regress-inflight",
            "inflight-check",
            # The orphan must outlive the kill -> status -> refusal assertions
            # even on a heavily loaded machine; 2s lost the race in practice.
            "echo start >> starts.txt; sleep 6; echo done >> dones.txt",
        )
        self.add_checkpoint("inflight-check", "regress-inflight", escalate_after=3)

        runner = subprocess.Popen(
            [sys.executable, "tools/agentctl.py", "loop", "cycle",
             "--checkpoint", "inflight-check", "--cycles", "1"],
            cwd=str(self.root), text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.addCleanup(lambda: runner.poll() is None and runner.kill())
        state_path = self.root / ".agent" / "loops" / "state.json"
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            state = json.loads(state_path.read_text(encoding="utf-8"))
            runtime = state.get("cycle_runtime") or {}
            active = runtime.get("active_command") or {}
            if active.get("pid") and (self.root / "starts.txt").is_file():
                break
            time.sleep(0.05)
        else:
            runner.kill()
            self.fail("cycle runner did not persist its active command")

        runner.kill()
        runner.wait(timeout=5)
        runner.stdout.close()
        runner.stderr.close()
        interrupted = json.loads(
            self.agentctl("loop", "status", "--json", expect=0).stdout)
        self.assertEqual(interrupted["status"], "interrupted")
        self.assertFalse(interrupted["runtime"]["resume_safe"])
        self.assertIsNotNone(interrupted["runtime"]["inflight_cycle"])
        self.assertIsNotNone(interrupted["runtime"]["active_command"])

        refused_resume = self.agentctl("loop", "resume", expect=2)
        self.assertIn("cannot be resumed", refused_resume.stdout + refused_resume.stderr)
        refused_stop = self.agentctl(
            "loop", "stop", "--ack-inflight", "--reason", "not yet reconciled", expect=2)
        self.assertIn("still has a live", refused_stop.stdout + refused_stop.stderr)

        deadline = time.monotonic() + 20
        module = self.load_agentctl_module("agentctl_inflight_test")
        while time.monotonic() < deadline:
            runtime = json.loads(state_path.read_text(encoding="utf-8"))["cycle_runtime"]
            if (self.root / "dones.txt").is_file() and not module._cycle_active_command_alive(runtime):
                break
            time.sleep(0.05)
        else:
            self.fail("orphaned command did not finish and release its process group")

        missing_ack = self.agentctl(
            "loop", "stop", "--reason", "inspected command output", expect=2)
        self.assertIn("--ack-inflight", missing_ack.stdout + missing_ack.stderr)
        reconciled = self.agentctl(
            "loop", "stop", "--ack-inflight",
            "--reason", "command exited; side effects inspected", expect=0)
        self.assertIn("marked stopped", reconciled.stdout)
        self.assertEqual((self.root / "starts.txt").read_text().splitlines(), ["start"])
        self.assertEqual((self.root / "dones.txt").read_text().splitlines(), ["done"])

    @unittest.skipUnless(os.name == "posix", "signal race regression requires POSIX")
    def test_command_waits_for_persisted_pid_before_launch(self):
        self.agentctl("work", "--agent", "codex", "--auto-create",
                      "--title", "launch handshake regression", "--scope", ".agent/", expect=0)
        self.write_loop("regress-handshake", "handshake-check", "echo run >> runs.txt")
        self.add_checkpoint("handshake-check", "regress-handshake", escalate_after=3)

        harness = r'''import importlib.util
import os
import signal

spec = importlib.util.spec_from_file_location("agentctl_launch_race", "tools/agentctl.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
# Wide enough that the first `loop stop` below lands INSIDE the pending
# window even on a heavily loaded machine (0.5s starved python startup),
# yet short enough that waiting out the deadline stays cheap.
module.LOOP_COMMAND_LAUNCH_TIMEOUT = 8.0
original = module._cycle_runtime_update

def crash_before_pid(root, runtime_id, updater, **kwargs):
    if getattr(updater, "__name__", "") == "mark_pid":
        os.kill(os.getpid(), signal.SIGKILL)
    return original(root, runtime_id, updater, **kwargs)

module._cycle_runtime_update = crash_before_pid
raise SystemExit(module.main([
    "loop", "cycle", "--checkpoint", "handshake-check", "--cycles", "1", "--force"
]))
'''
        runner = subprocess.Popen(
            [sys.executable, "-c", harness],
            cwd=str(self.root), text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        runner.wait(timeout=10)
        runner.stdout.close()
        runner.stderr.close()
        self.assertNotEqual(runner.returncode, 0)

        interrupted = json.loads(
            self.agentctl("loop", "status", "--json", expect=0).stdout)["runtime"]
        self.assertEqual(interrupted["status"], "interrupted")
        self.assertFalse(interrupted["resume_safe"])
        self.assertEqual(interrupted["active_command"]["launch_state"], "pending")
        self.assertIsNone(interrupted["active_command"]["pid"])
        self.assertFalse((self.root / "runs.txt").exists())

        still_arming = self.agentctl(
            "loop", "stop", "--ack-inflight", "--reason", "launch not reconciled", expect=2)
        self.assertIn("still has a live", still_arming.stdout + still_arming.stderr)
        # Wait out the pending-launch deadline (plus its 1s tolerance).
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            reconciled = self.agentctl(
                "loop", "stop", "--ack-inflight", "--reason",
                "launch gate expired before command execution")
            if reconciled.returncode == 0:
                break
            time.sleep(0.5)
        else:
            self.fail("pending launch never expired into a reconcilable state")
        self.assertFalse((self.root / "runs.txt").exists())

        completed = self.agentctl(
            "loop", "cycle", "--checkpoint", "handshake-check", "--cycles", "1", "--force",
            expect=0)
        self.assertIn("finished successfully", completed.stdout)
        self.assertEqual((self.root / "runs.txt").read_text().splitlines(), ["run"])

    def test_active_cycle_blocks_one_shot_execution_entry_points(self):
        self.agentctl("work", "--agent", "codex", "--auto-create",
                      "--title", "execution lease regression", "--scope", ".agent/", expect=0)
        self.write_loop(
            # The lease must stay held through both blocked assertions even on
            # a heavily loaded machine; 2s lost that race in practice.
            "regress-lease", "lease-check", "echo run >> runs.txt; sleep 6")
        self.add_checkpoint("lease-check", "regress-lease", escalate_after=3)

        runner = subprocess.Popen(
            [sys.executable, "tools/agentctl.py", "loop", "cycle",
             "--checkpoint", "lease-check", "--cycles", "1", "--force"],
            cwd=str(self.root), text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.addCleanup(lambda: runner.poll() is None and runner.kill())
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if (self.root / "runs.txt").is_file():
                break
            time.sleep(0.05)
        else:
            runner.kill()
            self.fail("cycle command did not start")

        blocked_auto = self.agentctl(
            "loop", "auto", "--checkpoint", "lease-check", "--once", "--force", expect=2)
        self.assertIn("non-owner loop execution is blocked", blocked_auto.stdout + blocked_auto.stderr)
        blocked_run = self.agentctl(
            "loop", "run", "regress-lease", "--once", expect=2)
        self.assertIn("non-owner loop execution is blocked", blocked_run.stdout + blocked_run.stderr)

        stdout, stderr = runner.communicate(timeout=10)
        self.assertEqual(runner.returncode, 0, stdout + stderr)
        self.assertEqual((self.root / "runs.txt").read_text().splitlines(), ["run"])
        state = json.loads(
            (self.root / ".agent" / "loops" / "state.json").read_text(encoding="utf-8"))
        self.assertNotIn("execution_lease", state)

    @unittest.skipUnless(os.name == "posix", "one-shot process recovery requires POSIX")
    def test_killed_one_shot_requires_reconciliation_before_replay(self):
        self.agentctl("work", "--agent", "codex", "--auto-create",
                      "--title", "one-shot recovery regression", "--scope", ".agent/", expect=0)
        self.agentctl(
            "loop", "cycle", "--checkpoint", "pre-finish", "--cycles", "1", "--force",
            expect=0,
        )
        self.write_loop(
            "regress-one-shot",
            "one-shot-check",
            "echo start >> starts.txt; sleep 2; echo done >> dones.txt",
        )

        runner = subprocess.Popen(
            [sys.executable, "tools/agentctl.py", "loop", "run", "regress-one-shot", "--once"],
            cwd=str(self.root), text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if (self.root / "starts.txt").is_file():
                break
            time.sleep(0.05)
        else:
            runner.kill()
            self.fail("one-shot command did not start")
        runner.kill()
        runner.wait(timeout=5)
        runner.stdout.close()
        runner.stderr.close()

        status = json.loads(self.agentctl("loop", "status", "--json", expect=0).stdout)
        self.assertEqual(status["status"], "execution_interrupted")
        self.assertEqual(status["runtime"]["status"], "completed")
        self.assertEqual(status["execution_lease"]["status"], "interrupted")
        blocked_live = self.agentctl(
            "loop", "run", "regress-one-shot", "--once", expect=2)
        self.assertIn("execution lease", blocked_live.stdout + blocked_live.stderr)

        deadline = time.monotonic() + 10
        module = self.load_agentctl_module("agentctl_one_shot_recovery_test")
        while time.monotonic() < deadline:
            lease = json.loads(
                (self.root / ".agent" / "loops" / "state.json").read_text(encoding="utf-8")
            )["execution_lease"]
            if (self.root / "dones.txt").is_file() and not module._active_command_alive(
                    lease.get("active_command")):
                break
            time.sleep(0.05)
        else:
            self.fail("orphaned one-shot command did not finish")

        blocked_unknown = self.agentctl(
            "loop", "run", "regress-one-shot", "--once", expect=2)
        self.assertIn("interrupted", blocked_unknown.stdout + blocked_unknown.stderr)
        missing_ack = self.agentctl("loop", "stop", "--reason", "inspected", expect=2)
        self.assertIn("--ack-inflight", missing_ack.stdout + missing_ack.stderr)
        self.agentctl(
            "loop", "stop", "--ack-inflight",
            "--reason", "orphaned command completed; side effects inspected", expect=0)

        replayed = self.agentctl("loop", "run", "regress-one-shot", "--once", expect=0)
        self.assertIn("-> success", replayed.stdout)
        self.assertEqual((self.root / "starts.txt").read_text().splitlines(), ["start", "start"])
        self.assertEqual((self.root / "dones.txt").read_text().splitlines(), ["done", "done"])

    @unittest.skipUnless(os.name == "posix", "process-group timeout regression requires POSIX")
    def test_timeout_kills_descendants_after_the_shell_leader_exits(self):
        self.agentctl("work", "--agent", "codex", "--auto-create",
                      "--title", "descendant timeout regression", "--scope", ".agent/", expect=0)
        self.write_loop(
            "regress-descendant", "descendant-check",
            "(sleep 3; echo leaked > leaked.txt) &",
        )
        path = self.root / ".agent" / "loops" / "regress-descendant.md"
        path.write_text(
            path.read_text(encoding="utf-8").replace("timeout: 30", "timeout: 1"),
            encoding="utf-8",
        )
        self.agentctl("refresh", expect=0)

        started = time.monotonic()
        timed_out = self.agentctl("loop", "run", "regress-descendant", "--once", expect=1)
        self.assertIn("-> failed", timed_out.stdout + timed_out.stderr)
        report = max((self.root / ".agent" / "loops" / "runs").glob("*-regress-descendant.md"))
        self.assertIn("timeout after 1s", report.read_text(encoding="utf-8"))
        self.assertLess(time.monotonic() - started, 5)
        time.sleep(2.5)
        self.assertFalse((self.root / "leaked.txt").exists())

    def test_windows_pid_probe_is_non_destructive(self):
        module = self.load_agentctl_module("agentctl_windows_pid_test")

        class Kernel32:
            def __init__(self):
                self.wait_result = 0x00000102
                self.open_handle = 42
                self.last_error = 0
                self.raise_probe_error = False
                self.closed = []

            def OpenProcess(self, _access, _inherit, _pid):
                if self.raise_probe_error:
                    raise RuntimeError("simulated ctypes failure")
                return self.open_handle

            def WaitForSingleObject(self, _handle, _timeout):
                return self.wait_result

            def CloseHandle(self, handle):
                self.closed.append(handle)

            def GetLastError(self):
                return self.last_error

        kernel32 = Kernel32()
        fake_ctypes = types.SimpleNamespace(
            windll=types.SimpleNamespace(kernel32=kernel32),
            wintypes=types.SimpleNamespace(DWORD=object(), BOOL=object(), HANDLE=object()),
        )
        with mock.patch.object(module.os, "name", "nt"), \
                mock.patch.dict(sys.modules, {"ctypes": fake_ctypes}), \
                mock.patch.object(module.os, "kill") as destructive_probe:
            self.assertTrue(module._pid_alive(1234))
            kernel32.wait_result = 0
            self.assertFalse(module._pid_alive(1234))
            kernel32.open_handle = 0
            kernel32.last_error = 87
            self.assertFalse(module._pid_alive(1234))
            kernel32.last_error = 5
            self.assertTrue(module._pid_alive(1234))
            kernel32.raise_probe_error = True
            self.assertTrue(module._pid_alive(1234))
        destructive_probe.assert_not_called()
        self.assertEqual(kernel32.closed, [42, 42])

    @unittest.skipUnless(os.name == "posix", "permission probe regression requires POSIX")
    def test_posix_pid_probe_treats_permission_denied_as_alive(self):
        module = self.load_agentctl_module("agentctl_posix_permission_test")
        denied = PermissionError(errno.EPERM, "operation not permitted")
        with mock.patch.object(module.os, "kill", side_effect=denied):
            self.assertTrue(module._pid_alive(1234))
        with mock.patch.object(module.os, "kill", side_effect=OverflowError("pid out of range")):
            self.assertFalse(module._pid_alive(2 ** 62))

    @unittest.skipUnless(os.name == "posix", "process-group probe regression requires POSIX")
    def test_posix_process_group_probe_handles_permission_and_range_failures(self):
        module = self.load_agentctl_module("agentctl_posix_group_permission_test")
        denied = PermissionError(errno.EPERM, "operation not permitted")
        with mock.patch.object(module.os, "killpg", side_effect=denied):
            self.assertTrue(module._posix_process_group_exists(1234))
        with mock.patch.object(module.os, "killpg", side_effect=OverflowError("pgid out of range")):
            self.assertFalse(module._posix_process_group_exists(2 ** 62))

    def test_windows_process_birth_marker_uses_creation_time_and_degrades_safely(self):
        import ctypes

        module = self.load_agentctl_module("agentctl_windows_process_birth_test")

        class FakeFunction:
            def __init__(self, implementation):
                self.implementation = implementation

            def __call__(self, *args):
                return self.implementation(*args)

        class Kernel32:
            def __init__(self):
                self.closed = []
                self.raise_argument_error = False
                self.OpenProcess = FakeFunction(lambda _access, _inherit, _pid: 42)
                self.GetProcessTimes = FakeFunction(self.get_process_times)
                self.CloseHandle = FakeFunction(self.close_handle)

            def get_process_times(self, _handle, created, _exited, _kernel, _user):
                if self.raise_argument_error:
                    raise ctypes.ArgumentError("simulated ABI mismatch")
                created._obj.low = 0x89ABCDEF
                created._obj.high = 0x12345678
                return 1

            def close_handle(self, handle):
                self.closed.append(handle)
                return 1

        kernel32 = Kernel32()
        fake_windll = types.SimpleNamespace(kernel32=kernel32)
        with mock.patch.object(module.os, "name", "nt"), \
                mock.patch.object(ctypes, "windll", fake_windll, create=True):
            self.assertEqual(
                module._process_birth_marker(1234),
                "windows:1311768467177459183",
            )
            kernel32.raise_argument_error = True
            self.assertIsNone(module._process_birth_marker(1234))
        self.assertEqual(kernel32.closed, [42, 42])

    def test_process_birth_marker_is_stable_for_current_process(self):
        module = self.load_agentctl_module("agentctl_process_birth_test")
        with mock.patch.dict(os.environ, {"TZ": "UTC"}):
            utc_marker = module._process_birth_marker(os.getpid())
        with mock.patch.dict(os.environ, {"TZ": "Asia/Seoul"}):
            local_marker = module._process_birth_marker(os.getpid())

        self.assertIsNotNone(utc_marker)
        self.assertEqual(local_marker, utc_marker)
        self.assertEqual(module._process_birth_marker(os.getpid()), utc_marker)
        self.assertTrue(module._same_process(os.getpid(), utc_marker))
        if sys.platform == "darwin":
            kind, seconds, microseconds = utc_marker.split(":")
            self.assertEqual(kind, "darwin")
            self.assertGreater(int(seconds), 0)
            self.assertGreaterEqual(int(microseconds), 0)
            self.assertLess(int(microseconds), 1_000_000)

    def test_non_finite_pid_values_degrade_without_crashing(self):
        module = self.load_agentctl_module("agentctl_non_finite_pid_test")
        for pid in (float("inf"), float("-inf")):
            self.assertFalse(module._pid_alive(pid))
            self.assertIsNone(module._process_birth_marker(pid))

    def test_linux_process_birth_marker_parses_non_utf8_comm(self):
        module = self.load_agentctl_module("agentctl_linux_process_birth_test")
        fields = [b"S"] + [str(index).encode("ascii") for index in range(1, 19)] + [b"4242"]

        class FakeProcPath:
            def __init__(self, path):
                self.path = str(path)

            def is_file(self):
                return True

            def read_bytes(self):
                if self.path.endswith("/stat"):
                    return b"123 (name-\xff-with-) paren) " + b" ".join(fields) + b"\n"
                return b"boot-id\n"

        with mock.patch.object(module.sys, "platform", "linux"), \
                mock.patch.object(module, "Path", FakeProcPath):
            self.assertEqual(
                module._process_birth_marker(123),
                "linux:boot-id:4242",
            )

    def test_linux_process_birth_marker_degrades_when_procfs_is_inaccessible(self):
        module = self.load_agentctl_module("agentctl_linux_proc_permission_test")

        class InaccessibleProcPath:
            def __init__(self, _path):
                pass

            def is_file(self):
                raise PermissionError(errno.EACCES, "permission denied")

        with mock.patch.object(module.sys, "platform", "linux"), \
                mock.patch.object(module, "Path", InaccessibleProcPath):
            self.assertIsNone(module._process_birth_marker(123))

    def test_reused_pid_does_not_preserve_runtime_or_execution_ownership(self):
        module = self.load_agentctl_module("agentctl_reused_pid_test")
        state_path = self.root / ".agent" / "loops" / "state.json"
        runtime = self.set_cycle_runtime(
            owner_pid=os.getpid(),
            owner_host=module._cycle_host_id(),
            owner_birth_marker="old-process-birth",
        )
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["execution_lease"] = {
            "token": "reused-pid-lease",
            "operation": "loop run reused-pid",
            "status": "running",
            "owner_pid": os.getpid(),
            "owner_birth_marker": "old-process-birth",
            "owner_host": module._cycle_host_id(),
            "cycle_runtime_id": None,
            "active_command": None,
            "started_at": "2026-01-01 00:00:00",
            "finished_at": None,
            "stop_reason": None,
        }
        state_path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")

        with mock.patch.object(module, "_pid_alive", return_value=True), \
                mock.patch.object(
                    module, "_process_birth_marker", return_value="new-process-birth"):
            recovered_runtime = module._cycle_runtime(self.root)
            recovered_lease = module._loop_execution_lease(self.root)
            active = dict(runtime.get("active_command") or {})
            active.update({
                "pid": os.getpid(),
                "birth_marker": "old-process-birth",
                "host": module._cycle_host_id(),
                "launch_state": "armed",
            })
            self.assertFalse(module._active_command_alive(active))
            active["process_group"] = 1234
            with mock.patch.object(module, "_posix_process_group_exists", return_value=True):
                self.assertEqual(module._active_command_state(active), "unverifiable")

        self.assertEqual(recovered_runtime["status"], "interrupted")
        self.assertTrue(recovered_runtime["resume_safe"])
        self.assertIsNone(recovered_runtime["owner_pid"])
        self.assertIsNone(recovered_runtime["owner_birth_marker"])
        self.assertEqual(recovered_lease["status"], "interrupted")
        self.assertIsNone(recovered_lease["owner_pid"])
        self.assertIsNone(recovered_lease["owner_birth_marker"])

    @unittest.skipUnless(os.name == "posix", "process-group recovery requires POSIX")
    def test_ack_reconciles_an_unverifiable_leaderless_process_group(self):
        module = self.load_agentctl_module("agentctl_reused_process_group_test")
        active = {
            "token": "reused-group-command",
            "pid": 99999999,
            "birth_marker": "old-process-birth",
            "process_group": os.getpgrp(),
            "host": module._cycle_host_id(),
            "launch_state": "armed",
        }
        self.assertEqual(module._active_command_state(active), "unverifiable")
        self.assertTrue(module._active_command_alive(active))
        legacy_active = dict(active)
        legacy_active.pop("birth_marker")
        self.assertEqual(module._active_command_state(legacy_active), "live")
        self.set_cycle_runtime(
            status="interrupted",
            owner_pid=None,
            owner_birth_marker=None,
            inflight_cycle={"number": 2},
            active_command=active,
            resume_safe=False,
        )

        missing_ack = self.agentctl(
            "loop", "stop", "--reason", "inspected unrelated reused group", expect=2)
        self.assertIn("--ack-inflight", missing_ack.stdout + missing_ack.stderr)
        reconciled = self.agentctl(
            "loop", "stop", "--ack-inflight",
            "--reason", "recorded leader exited; process group identity is no longer verifiable",
            expect=0,
        )
        self.assertIn("marked stopped", reconciled.stdout)

    def test_cycle_rejects_non_finite_intervals(self):
        self.agentctl("work", "--agent", "codex", "--auto-create",
                      "--title", "interval validation regression", "--scope", ".agent/", expect=0)
        self.write_loop("regress-interval", "interval-check", "true")
        self.add_checkpoint("interval-check", "regress-interval", escalate_after=3)

        for value in ("nan", "inf", "-inf"):
            refused = self.agentctl(
                "loop", "cycle", "--checkpoint", "interval-check", "--cycles", "1",
                f"--interval={value}", expect=2)
            self.assertIn("--interval must be finite", refused.stdout + refused.stderr)
        payload = json.loads(self.agentctl("loop", "status", "--json", expect=0).stdout)
        self.assertEqual(payload["status"], "idle")

    def test_cooperative_stop_preserves_a_prior_failure_exit_code(self):
        self.agentctl("work", "--agent", "codex", "--auto-create",
                      "--title", "failed stop regression", "--scope", ".agent/", expect=0)
        self.write_loop("regress-failed-stop", "failed-stop-check", "exit 4")
        self.add_checkpoint("failed-stop-check", "regress-failed-stop", escalate_after=10)

        runner = subprocess.Popen(
            [sys.executable, "tools/agentctl.py", "loop", "cycle",
             "--checkpoint", "failed-stop-check", "--cycles", "3", "--interval", "30",
             "--continue-on-failure", "--max-failures", "3"],
            cwd=str(self.root), text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.addCleanup(lambda: runner.poll() is None and runner.kill())
        state_path = self.root / ".agent" / "loops" / "state.json"
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            runtime = json.loads(state_path.read_text(encoding="utf-8")).get("cycle_runtime") or {}
            if (runtime.get("status") == "running"
                    and runtime.get("completed_cycles") == 1
                    and runtime.get("failures") == 1):
                break
            time.sleep(0.05)
        else:
            runner.kill()
            self.fail("cycle runner did not enter its post-failure sleep window")

        self.agentctl("loop", "stop", "--reason", "stop after observed failure", expect=0)
        stdout, stderr = runner.communicate(timeout=10)
        self.assertEqual(runner.returncode, 1, stdout + stderr)
        self.assertIn("stopped at 1/3", stdout + stderr)
        stopped = json.loads(
            self.agentctl("loop", "status", "--json", expect=0).stdout)["runtime"]
        self.assertEqual(stopped["status"], "stopped")
        self.assertEqual(stopped["failures"], 1)
        self.assertEqual(stopped["first_failure_code"], 1)

    def test_concurrent_resume_claims_runtime_once(self):
        self.agentctl("work", "--agent", "codex", "--auto-create",
                      "--title", "resume claim regression", "--scope", ".agent/", expect=0)
        self.write_loop("regress-claim", "claim-check", "sleep 0.2")
        self.add_checkpoint("claim-check", "regress-claim", escalate_after=3)
        self.set_cycle_runtime(
            checkpoint="claim-check", status="interrupted", requested_cycles=2,
            completed_cycles=0, owner_pid=None, finished_at="2026-01-01 00:00:00")

        command = [sys.executable, "tools/agentctl.py", "loop", "resume"]
        runners = [
            subprocess.Popen(command, cwd=str(self.root), text=True,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            for _ in range(2)
        ]
        results = []
        for runner in runners:
            stdout, stderr = runner.communicate(timeout=15)
            results.append((runner.returncode, stdout + stderr))
        self.assertEqual(sorted(code for code, _output in results), [0, 2], results)

        state = json.loads(
            (self.root / ".agent" / "loops" / "state.json").read_text(encoding="utf-8"))
        runtime = state["cycle_runtime"]
        self.assertEqual(runtime["status"], "completed")
        self.assertEqual(runtime["completed_cycles"], 2)
        self.assertEqual(
            [event.get("cycle") for event in runtime["events"]
             if event["event"] == "cycle_started"],
            [1, 2],
        )

    def test_failure_budget_stops_before_requested_cycle_count(self):
        self.agentctl("work", "--agent", "codex", "--auto-create",
                      "--title", "failure budget regression", "--scope", ".agent/", expect=0)
        self.write_loop("regress-budget", "budget-check", "exit 4")
        self.add_checkpoint("budget-check", "regress-budget", escalate_after=10)

        failed = self.agentctl(
            "loop", "cycle", "--checkpoint", "budget-check", "--cycles", "5",
            "--continue-on-failure", "--max-failures", "2", expect=1)
        combined = failed.stdout + failed.stderr
        self.assertIn("loop cycle 1/5", combined)
        self.assertIn("loop cycle 2/5", combined)
        self.assertNotIn("loop cycle 3/5", combined)
        self.assertIn("failure budget exhausted (2/2)", combined)

        status = json.loads(self.agentctl("loop", "status", "--json", expect=0).stdout)
        self.assertEqual(status["status"], "failed")
        self.assertEqual(status["runtime"]["completed_cycles"], 2)
        self.assertEqual(status["runtime"]["failures"], 2)

        ambiguous = self.agentctl(
            "loop", "cycle", "--checkpoint", "budget-check", "--cycles", "3",
            "--max-failures", "2", expect=2)
        self.assertIn("requires --continue-on-failure", ambiguous.stdout + ambiguous.stderr)

    def test_atomic_json_replace_preserves_previous_snapshot_on_failure(self):
        module = self.load_agentctl_module("agentctl_atomic_test")
        path = self.root / "snapshot.json"
        path.write_text('{"version": 1}\n', encoding="utf-8")

        with mock.patch.object(module.os, "replace", side_effect=OSError("simulated crash")):
            with self.assertRaises(OSError):
                module._save_json(path, {"version": 2})

        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"version": 1})
        self.assertEqual(list(path.parent.glob(f".{path.name}.*.tmp")), [])

    def test_persistent_loop_state_lock_is_acquired_without_stale_timeout(self):
        module = self.load_agentctl_module("agentctl_dead_lock_test")
        lock = module._loop_state_lock_path(self.root)
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text(
            f"pid=99999999 host={module._cycle_host_id()} acquired_at=2026-01-01 00:00:00\n",
            encoding="utf-8",
        )

        started = time.monotonic()
        state = module._update_loop_state(
            self.root, lambda payload: payload.update(dead_lock_recovered=True))
        self.assertLess(time.monotonic() - started, 1)
        self.assertTrue(state["dead_lock_recovered"])
        self.assertTrue(lock.exists())

    def test_loop_state_advisory_lock_serializes_concurrent_writers(self):
        state_path = self.root / ".agent" / "loops" / "state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["concurrent_counter"] = 0
        state_path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
        gate = self.root / "start-concurrent-writers"
        harness = r'''import importlib.util
import sys
import time
from pathlib import Path

root = Path(sys.argv[1])
gate = Path(sys.argv[2])
spec = importlib.util.spec_from_file_location("agentctl_concurrent_writer", root / "tools/agentctl.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
while not gate.exists():
    time.sleep(0.01)

def increment(state):
    value = int(state.get("concurrent_counter") or 0)
    time.sleep(0.05)
    state["concurrent_counter"] = value + 1

module._update_loop_state(root, increment)
'''
        writers = [
            subprocess.Popen(
                [sys.executable, "-c", harness, str(self.root), str(gate)],
                cwd=str(self.root), text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            for _ in range(8)
        ]
        gate.write_text("go\n", encoding="utf-8")
        results = [writer.communicate(timeout=15) for writer in writers]
        self.assertEqual([writer.returncode for writer in writers], [0] * 8, results)
        final = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(final["concurrent_counter"], 8)

    def test_supervisor_guidance_surfaces_blocks_and_acknowledges(self):
        self.agentctl("work", "--agent", "codex", "--auto-create",
                      "--title", "guided implementation", "--scope", ".agent/", expect=0)
        task = json.loads(self.agentctl("status", "--json", expect=0).stdout)["task"]

        created = self.agentctl(
            "guidance", "create",
            "--from-agent", "fable",
            "--to-agent", "codex",
            "--task", task,
            "--summary", "tighten implementation stages",
            "--plan", "Read the task doc, keep the patch scoped, and add a regression test.",
            expect=0)
        self.assertIn("finish gate", created.stdout)
        packets = self.guidance_packets()
        self.assertEqual(len(packets), 1, packets)
        packet_id = packets[0]["id"]

        focus = self.agentctl("focus", expect=0)
        combined = focus.stdout + focus.stderr
        self.assertIn("[Supervisor Guidance]", combined)
        self.assertIn("tighten implementation stages", combined)
        self.assertIn("Required before finish", combined)

        check = self.agentctl("check", "--mode", "manual", expect=1)
        self.assertIn("pending supervisor guidance", check.stdout + check.stderr)
        blocked = self.agentctl("finish", "--summary", "should block",
                                "--tests", "n/a", expect=1)
        self.assertIn("finish blocked", blocked.stdout + blocked.stderr)

        acked = self.agentctl("guidance", "ack", packet_id, "--by", "codex",
                              "--note", "incorporated into the implementation plan", expect=0)
        self.assertIn("acknowledged by codex", acked.stdout)
        self.assertEqual(self.inbox_packets(), [])
        done = self.guidance_packets()
        self.assertEqual(done[0]["status"], "done")
        self.assertEqual(done[0]["acknowledged_by"], "codex")

        self.agentctl("check", "--mode", "manual", expect=0)
        self.agentctl("finish", "--summary", "guidance workflow passed",
                      "--tests", "guidance create/focus/block/ack", expect=0)


if __name__ == "__main__":
    unittest.main()
