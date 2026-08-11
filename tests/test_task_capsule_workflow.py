"""Regression coverage for bounded task startup capsules."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


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


class TaskCapsuleWorkflowRegressionTest(unittest.TestCase):
    def setUp(self):
        self.temp = Path(tempfile.mkdtemp(prefix="awk-capsule-regress-"))
        self.addCleanup(shutil.rmtree, self.temp, ignore_errors=True)
        self.root = self.temp / "project"
        self.root.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True, timeout=60)
        installed = subprocess.run(
            [sys.executable, str(KIT / "tools" / "agentctl.py"),
             "init", str(self.root)],
            cwd=KIT, text=True, capture_output=True, timeout=120,
        )
        self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)

    def env(self, session):
        env = os.environ.copy()
        for name in IDENTITY_ENV:
            env.pop(name, None)
        env["AGENT_WORKFLOW_SESSION_ID"] = session
        return env

    def agentctl(self, session, *args, expect=0):
        proc = subprocess.run(
            [sys.executable, "tools/agentctl.py", *args],
            cwd=self.root, env=self.env(session), text=True,
            capture_output=True, timeout=120,
        )
        self.assertEqual(proc.returncode, expect, proc.stdout + proc.stderr)
        return proc

    def test_capsule_is_bounded_and_exposes_live_coordination_state(self):
        first = self.agentctl(
            "alpha", "work", "--agent", "alpha", "--auto-create",
            "--new-id", "T-CAP-A", "--title", "capsule producer",
            "--scope", "docs/alpha/", "--type", "docs",
        )
        self.assertIn("[Runtime Capsule]", first.stdout)
        self.agentctl("alpha", "note", "first durable checkpoint")
        self.agentctl(
            "beta", "work", "--agent", "beta", "--auto-create",
            "--new-id", "T-CAP-B", "--title", "parallel peer",
            "--scope", "docs/beta/", "--type", "docs",
        )

        rendered = self.agentctl("alpha", "capsule", "--json").stdout
        self.assertLess(len(rendered.encode("utf-8")), 8192)
        capsule = json.loads(rendered)
        self.assertEqual(capsule["task"], "T-CAP-A")
        self.assertEqual(capsule["type"], "docs")
        self.assertEqual(capsule["isolation"], "shared")
        self.assertEqual(capsule["protocol_epoch"], 2)
        self.assertEqual(len(capsule["documents_digest"]), 12)
        self.assertTrue(any(
            row["task"] == "T-CAP-B" and row["status"] == "active"
            for row in capsule["peers"]
        ))
        self.assertTrue(any(
            row["note"] == "first durable checkpoint"
            for row in capsule["recent_notes"]
        ))
        self.assertTrue(capsule["remaining_todos"])
        self.assertEqual(capsule["next_action"], capsule["remaining_todos"][0])


if __name__ == "__main__":
    unittest.main()
