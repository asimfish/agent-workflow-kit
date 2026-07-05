"""Regression tests for the loop feedback chain in fresh installed projects.

Each test installs the kit into a temporary Git repository and replays the
T-008 dogfood scenario: a failing custom loop creates a follow-up packet,
repeated failures escalate, escalation blocks check/finish, fixing the loop
auto-closes the packet, and --ack-escalations records a deliberate override.
"""

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

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

    def inbox_packets(self):
        return sorted((self.root / ".agent" / "bus" / "inbox").rglob("*.json"))

    def guidance_packets(self):
        proc = self.agentctl("guidance", "list", "--json", expect=0)
        return json.loads(proc.stdout)["guidance"]

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
        self.assertIn("loop cycle finished with 2/2 failing cycle(s)", combined)

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
