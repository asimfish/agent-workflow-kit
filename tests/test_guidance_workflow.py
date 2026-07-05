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


if __name__ == "__main__":
    unittest.main()
