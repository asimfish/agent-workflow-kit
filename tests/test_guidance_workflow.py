"""Regression tests for supervisor guidance packets.

The tests install the kit into a fresh Git repository and verify the intended
Fable -> Codex flow: a stronger planning agent writes a durable plan packet,
Codex sees it at work start, and task completion is blocked until Codex
acknowledges that the guidance was incorporated.
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

KIT = Path(__file__).resolve().parents[1]


class GuidanceWorkflowRegressionTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="awk-guidance-regress-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.env = os.environ.copy()
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
            cwd=str(self.root), text=True, capture_output=True, timeout=120,
            env=self.env)
        if expect is not None:
            self.assertEqual(
                proc.returncode, expect,
                f"agentctl {' '.join(args)} rc={proc.returncode}\n{proc.stdout}\n{proc.stderr}")
        return proc

    def install_fake_codex(self, exit_code=0):
        fake_bin = self.root / "fake-bin"
        fake_bin.mkdir(parents=True, exist_ok=True)
        executable = fake_bin / "codex"
        executable.write_text(
            """#!/usr/bin/env python3
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

args = sys.argv[1:]
Path(os.environ["FAKE_CODEX_RECORD"]).write_text(json.dumps(args), encoding="utf-8")
if os.environ.get("FAKE_CODEX_SLEEP"):
    time.sleep(float(os.environ["FAKE_CODEX_SLEEP"]))
if os.environ.get("FAKE_CODEX_ACK") == "1":
    packet = re.search(r"guidance packet `([^`]+)`", args[-1]).group(1)
    acknowledged = subprocess.run(
        [sys.executable, "tools/agentctl.py", "guidance", "ack", packet, "--by", "codex"],
        text=True,
        capture_output=True,
    )
    if acknowledged.returncode:
        print(acknowledged.stdout + acknowledged.stderr, file=sys.stderr)
        raise SystemExit(acknowledged.returncode)
if "--output-last-message" in args:
    output = Path(args[args.index("--output-last-message") + 1])
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("fake Codex completed the dispatched turn", encoding="utf-8")
print("fake Codex stdout")
print("fake Codex stderr", file=sys.stderr)
raise SystemExit(int(os.environ.get("FAKE_CODEX_EXIT", "0")))
""",
            encoding="utf-8",
        )
        executable.chmod(0o755)
        record = self.root / ".agent" / "state" / "fake-codex-args.json"
        self.env["PATH"] = str(fake_bin) + os.pathsep + self.env.get("PATH", "")
        self.env["FAKE_CODEX_RECORD"] = str(record)
        self.env["FAKE_CODEX_EXIT"] = str(exit_code)
        return record

    def test_fable_guidance_surfaces_to_codex_and_blocks_until_ack(self):
        self.agentctl(
            "task", "create",
            "--id", "T-101",
            "--title", "guided implementation",
            "--owner", "codex",
            "--scope", "src/",
            expect=0)

        created = self.agentctl(
            "guidance", "create",
            "--from-agent", "fable",
            "--to-agent", "codex",
            "--task", "T-101",
            "--summary", "Use a three-phase implementation plan",
            "--plan", "1. Inspect the target code.\n2. Implement narrowly.\n3. Run verification.",
            expect=0)
        match = re.search(r"guidance packet created: (\S+)", created.stdout)
        self.assertIsNotNone(match, created.stdout)
        packet_id = match.group(1)

        listed = self.agentctl("guidance", "list", "--agent", "codex", "--json", expect=0)
        guidance = json.loads(listed.stdout)["guidance"]
        self.assertEqual(len(guidance), 1, guidance)
        self.assertEqual(guidance[0]["from_agent"], "fable")
        self.assertEqual(guidance[0]["to_agent"], "codex")
        self.assertEqual(guidance[0]["task"], "T-101")

        work = self.agentctl("work", "--agent", "codex", expect=0)
        combined = work.stdout + work.stderr
        self.assertIn("Supervisor Guidance", combined)
        self.assertIn("Use a three-phase implementation plan", combined)
        self.assertIn("Required before finish", combined)

        blocked = self.agentctl(
            "finish", "--summary", "should be blocked",
            "--tests", "not run",
            expect=1)
        self.assertIn("pending supervisor guidance", blocked.stdout + blocked.stderr)

        acked = self.agentctl(
            "guidance", "ack", packet_id,
            "--by", "codex",
            "--note", "incorporated into task plan",
            expect=0)
        self.assertIn("acknowledged by codex", acked.stdout)
        self.assertEqual(
            sorted((self.root / ".agent" / "bus" / "inbox").rglob("*.json")),
            [])

        finished = self.agentctl(
            "finish", "--summary", "guided work complete",
            "--tests", "guidance regression",
            expect=0)
        self.assertIn("T-101 -> review", finished.stdout + finished.stderr)

        done_packets = sorted((self.root / ".agent" / "bus" / "done").glob("*.json"))
        self.assertTrue(done_packets)
        packet = json.loads(done_packets[0].read_text(encoding="utf-8"))
        self.assertEqual(packet["status"], "done")
        self.assertEqual(packet["acknowledged_by"], "codex")

    def test_session_scoped_guidance_only_blocks_matching_worker_session(self):
        self.agentctl(
            "task", "create",
            "--id", "T-102",
            "--title", "session routed implementation",
            "--owner", "codex",
            "--scope", ".agent/",
            expect=0)

        self.agentctl(
            "guidance", "create",
            "--from-agent", "fable",
            "--to-agent", "codex",
            "--to-model", "gpt-5.5",
            "--to-reasoning-effort", "xhigh",
            "--to-session", "xxx",
            "--task", "T-102",
            "--summary", "Plan for the target high-effort Codex session",
            "--plan", "Only session xxx should receive and ack this plan.",
            expect=0)
        self.agentctl(
            "guidance", "create",
            "--from-agent", "fable",
            "--to-agent", "codex",
            "--to-model", "gpt-5.5",
            "--to-reasoning-effort", "xhigh",
            "--to-session", "yyy",
            "--task", "T-102",
            "--summary", "Plan for a different Codex session",
            "--plan", "Session xxx must not see or be blocked by this plan.",
            expect=0)

        work = self.agentctl(
            "work", "--agent", "codex",
            "--model", "gpt-5.5",
            "--reasoning-effort", "xhigh",
            "--session-id", "xxx",
            expect=0)
        combined = work.stdout + work.stderr
        self.assertIn("Plan for the target high-effort Codex session", combined)
        self.assertIn("session=xxx", combined)
        self.assertNotIn("Plan for a different Codex session", combined)

        visible = self.agentctl(
            "guidance", "list",
            "--agent", "codex",
            "--session-id", "xxx",
            "--json",
            expect=0)
        visible_packets = json.loads(visible.stdout)["guidance"]
        self.assertEqual(len(visible_packets), 1, visible_packets)
        self.assertEqual(visible_packets[0]["to_session"], "xxx")
        packet_id = visible_packets[0]["id"]

        blocked = self.agentctl("check", "--mode", "manual", expect=1)
        self.assertIn("pending supervisor guidance", blocked.stdout + blocked.stderr)

        self.agentctl(
            "guidance", "ack", packet_id,
            "--by", "codex",
            "--note", "session xxx incorporated the supervisor plan",
            expect=0)
        self.agentctl("check", "--mode", "manual", expect=0)

        finished = self.agentctl(
            "finish",
            "--summary", "session scoped guidance complete",
            "--tests", "session routing regression",
            expect=0)
        self.assertIn("T-102 -> review", finished.stdout + finished.stderr)

        remaining = self.agentctl(
            "guidance", "list",
            "--agent", "codex",
            "--session-id", "yyy",
            "--status", "ready",
            "--json",
            expect=0)
        remaining_packets = json.loads(remaining.stdout)["guidance"]
        self.assertEqual(len(remaining_packets), 1, remaining_packets)
        self.assertEqual(remaining_packets[0]["to_session"], "yyy")

    def test_agent_profile_session_metadata_routes_guidance_by_default(self):
        self.agentctl(
            "agents", "add",
            "--id", "codex-gpt55xhigh",
            "--role", "implementation worker",
            "--backend", "codex",
            "--model", "gpt-5.5",
            "--reasoning-effort", "xhigh",
            "--session-id", "xxx",
            expect=0)
        self.agentctl(
            "task", "create",
            "--id", "T-103",
            "--title", "profile routed implementation",
            "--owner", "codex-gpt55xhigh",
            "--scope", ".agent/",
            expect=0)
        created = self.agentctl(
            "guidance", "create",
            "--from-agent", "fable",
            "--to-agent", "codex-gpt55xhigh",
            "--task", "T-103",
            "--summary", "Profile metadata should target session xxx",
            "--plan", "Use the registered worker model and session metadata.",
            expect=0)
        self.assertIn("model=gpt-5.5 reasoning=xhigh session=xxx", created.stdout)

        work = self.agentctl("work", "--agent", "codex-gpt55xhigh", expect=0)
        combined = work.stdout + work.stderr
        self.assertIn("model=gpt-5.5 reasoning=xhigh session=xxx", combined)
        self.assertIn("Profile metadata should target session xxx", combined)

    def test_dispatch_dry_run_builds_resume_command_without_starting_codex(self):
        created = self.agentctl(
            "guidance", "create",
            "--from-agent", "fable",
            "--to-agent", "codex",
            "--to-model", "gpt-5.5",
            "--to-reasoning-effort", "xhigh",
            "--to-session", "session-dry-run",
            "--task", "T-201",
            "--summary", "Dry-run the worker dispatch",
            "--plan", "Inspect, implement, verify.",
            expect=0)
        packet_id = re.search(r"guidance packet created: (\S+)", created.stdout).group(1)

        dispatched = self.agentctl(
            "guidance", "dispatch", packet_id,
            "--dry-run",
            expect=0)
        combined = dispatched.stdout + dispatched.stderr
        self.assertIn("exec resume", combined)
        self.assertIn("--model gpt-5.5", combined)
        self.assertIn("model_reasoning_effort", combined)
        self.assertIn("session-dry-run", combined)
        self.assertIn("<guidance-prompt>", combined)
        self.assertIn("no Codex process started", combined)

        packet = json.loads(
            self.agentctl("guidance", "show", packet_id, "--json", expect=0).stdout)
        self.assertNotIn("dispatch", packet)
        self.assertEqual(packet["to_reasoning_effort"], "xhigh")

    def test_create_dispatch_invokes_target_session_and_records_receipt(self):
        record = self.install_fake_codex(exit_code=0)
        self.env["FAKE_CODEX_ACK"] = "1"
        created = self.agentctl(
            "guidance", "create",
            "--from-agent", "fable",
            "--to-agent", "codex",
            "--to-model", "gpt-5.5",
            "--to-reasoning-effort", "xhigh",
            "--to-session", "session-success",
            "--task", "T-202",
            "--summary", "Implement the bounded worker phase",
            "--plan", "1. Read the task.\n2. Implement.\n3. Test and finish.",
            "--dispatch",
            expect=0)
        packet_id = re.search(r"guidance packet created: (\S+)", created.stdout).group(1)
        self.assertIn("dispatched successfully", created.stdout)
        self.assertIn("fake Codex completed the dispatched turn", created.stdout)

        args = json.loads(record.read_text(encoding="utf-8"))
        self.assertEqual(args[:2], ["exec", "resume"])
        self.assertEqual(args[args.index("--model") + 1], "gpt-5.5")
        self.assertEqual(
            args[args.index("--config") + 1],
            'model_reasoning_effort="xhigh"')
        self.assertEqual(args[-2], "session-success")
        prompt = args[-1]
        self.assertIn(packet_id, prompt)
        self.assertIn("T-202", prompt)
        self.assertIn("Implement the bounded worker phase", prompt)
        self.assertIn("Do not steal another live task lock", prompt)

        packet = json.loads(
            self.agentctl("guidance", "show", packet_id, "--json", expect=0).stdout)
        self.assertEqual(packet["status"], "done")
        self.assertEqual(packet["dispatch"]["status"], "succeeded")
        self.assertEqual(packet["dispatch"]["attempts"], 1)
        self.assertEqual(packet["dispatch"]["exit_code"], 0)
        self.assertEqual(packet["dispatch"]["reasoning_effort"], "xhigh")
        receipt = self.root / ".agent" / "state" / "dispatch" / f"{packet_id}.json"
        self.assertTrue(receipt.is_file())
        self.assertEqual(json.loads(receipt.read_text(encoding="utf-8"))["status"], "succeeded")
        self.assertEqual(sorted((self.root / ".agent" / "bus" / "inbox").rglob("*.json")), [])

    def test_failed_dispatch_keeps_guidance_ready_and_can_retry(self):
        self.install_fake_codex(exit_code=9)
        failed = self.agentctl(
            "guidance", "create",
            "--from-agent", "fable",
            "--to-agent", "codex",
            "--to-session", "session-retry",
            "--task", "T-203",
            "--summary", "Retry a failed dispatch",
            "--plan", "Run the worker phase once transport is healthy.",
            "--dispatch",
            expect=1)
        packet_id = re.search(r"guidance packet created: (\S+)", failed.stdout).group(1)
        packet = json.loads(
            self.agentctl("guidance", "show", packet_id, "--json", expect=0).stdout)
        self.assertEqual(packet["status"], "ready")
        self.assertEqual(packet["dispatch"]["status"], "failed")
        self.assertEqual(packet["dispatch"]["exit_code"], 9)
        self.assertEqual(packet["dispatch"]["attempts"], 1)

        self.env["FAKE_CODEX_EXIT"] = "0"
        retried = self.agentctl("guidance", "dispatch", packet_id, expect=0)
        self.assertIn("dispatched successfully", retried.stdout)
        packet = json.loads(
            self.agentctl("guidance", "show", packet_id, "--json", expect=0).stdout)
        self.assertEqual(packet["status"], "ready")
        self.assertEqual(packet["dispatch"]["status"], "succeeded")
        self.assertEqual(packet["dispatch"]["attempts"], 2)

    def test_dispatch_timeout_records_bounded_failure(self):
        self.install_fake_codex(exit_code=0)
        self.env["FAKE_CODEX_SLEEP"] = "2"
        failed = self.agentctl(
            "guidance", "create",
            "--from-agent", "fable",
            "--to-agent", "codex",
            "--to-session", "session-timeout",
            "--task", "T-204",
            "--summary", "Bound a slow worker turn",
            "--plan", "Stop and preserve a retryable packet if transport times out.",
            "--dispatch",
            "--timeout", "1",
            expect=1)
        packet_id = re.search(r"guidance packet created: (\S+)", failed.stdout).group(1)
        packet = json.loads(
            self.agentctl("guidance", "show", packet_id, "--json", expect=0).stdout)
        self.assertEqual(packet["status"], "ready")
        self.assertEqual(packet["dispatch"]["status"], "failed")
        self.assertEqual(packet["dispatch"]["exit_code"], 124)
        self.assertIn("timed out after 1s", packet["dispatch"]["failure"])

    def test_dispatch_refuses_source_session_recursion(self):
        created = self.agentctl(
            "guidance", "create",
            "--from-agent", "fable",
            "--from-session", "same-session",
            "--to-agent", "codex",
            "--to-session", "same-session",
            "--task", "T-205",
            "--summary", "Do not recurse into the supervisor session",
            "--plan", "This packet must remain file-only.",
            expect=0)
        packet_id = re.search(r"guidance packet created: (\S+)", created.stdout).group(1)
        blocked = self.agentctl(
            "guidance", "dispatch", packet_id,
            "--dry-run",
            expect=1)
        self.assertIn("source session", blocked.stdout + blocked.stderr)


if __name__ == "__main__":
    unittest.main()
