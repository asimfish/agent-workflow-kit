"""Regression tests for supervisor guidance packets.

The tests install the kit into a fresh Git repository and verify the intended
Fable -> Codex flow: a stronger planning agent writes a durable plan packet,
Codex sees it at work start, and task completion is blocked until Codex
acknowledges that the guidance was incorporated.
"""

import json
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
            "--to-model", "gpt5.5xhigh",
            "--to-session", "xxx",
            "--task", "T-102",
            "--summary", "Plan for the target high-effort Codex session",
            "--plan", "Only session xxx should receive and ack this plan.",
            expect=0)
        self.agentctl(
            "guidance", "create",
            "--from-agent", "fable",
            "--to-agent", "codex",
            "--to-model", "gpt5.5xhigh",
            "--to-session", "yyy",
            "--task", "T-102",
            "--summary", "Plan for a different Codex session",
            "--plan", "Session xxx must not see or be blocked by this plan.",
            expect=0)

        work = self.agentctl(
            "work", "--agent", "codex",
            "--model", "gpt5.5xhigh",
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
            "--model", "gpt5.5xhigh",
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
        self.assertIn("model=gpt5.5xhigh session=xxx", created.stdout)

        work = self.agentctl("work", "--agent", "codex-gpt55xhigh", expect=0)
        combined = work.stdout + work.stderr
        self.assertIn("model=gpt5.5xhigh session=xxx", combined)
        self.assertIn("Profile metadata should target session xxx", combined)


if __name__ == "__main__":
    unittest.main()
