"""Regression coverage for the README day-one walkthrough gaps.

A verbatim replay of the README on a blank project found places where the
prose and the tool disagreed. These tests pin the fixed behavior:

- `doctor` is a read-only diagnostic and must run from a plain terminal
  with no agent conversation identity, while mutating commands stay refused.
- `init` gitignores the default artifact root and a re-run appends only the
  entries an older install is missing.
- The `finish` hint and the gate refusal name the reviewer registration
  command instead of leaving the reviewer to guess.
"""

import glob
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
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


class _KitRepoTestCase(unittest.TestCase):
    """Shared scaffolding: a temp repo with the kit installed."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="awk-day-one-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.git("init", "-q")
        self.git("config", "user.email", "agent@example.com")
        self.git("config", "user.name", "Agent Test")
        self.init()

    def init(self):
        install = subprocess.run(
            [sys.executable, str(KIT / "tools" / "agentctl.py"), "init", str(self.root)],
            cwd=KIT, text=True, capture_output=True, timeout=120,
        )
        self.assertEqual(install.returncode, 0, install.stdout + install.stderr)
        return install

    def git(self, *args, check=True):
        proc = subprocess.run(
            ["git", *args], cwd=str(self.root), text=True,
            capture_output=True, timeout=60,
        )
        if check:
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return proc

    def env(self, session=None, **extra):
        env = os.environ.copy()
        for name in IDENTITY_ENV:
            env.pop(name, None)
        # Resource locks are machine-wide by design (a GPU is a GPU no matter
        # which checkout claims it); keep the suite hermetic.
        env["AGENT_WORKFLOW_RESOURCE_LOCK_DIR"] = str(self.root / ".resource-locks")
        if session:
            env["AGENT_WORKFLOW_SESSION_ID"] = session
            # A real Codex/Claude/Cursor conversation also carries a host
            # thread id; the review gate fingerprints it as the runtime.
            env["CODEX_THREAD_ID"] = f"thread-{session}"
        env.update(extra)
        return env

    def agentctl(self, *args, expect=0, session=None, **extra):
        proc = subprocess.run(
            [sys.executable, "tools/agentctl.py", *args],
            cwd=str(self.root), env=self.env(session, **extra), text=True,
            capture_output=True, timeout=120,
        )
        self.assertEqual(
            proc.returncode, expect,
            f"args={args}\nstdout={proc.stdout}\nstderr={proc.stderr}",
        )
        return proc


class DoctorWithoutIdentityTest(_KitRepoTestCase):
    def test_doctor_runs_from_a_plain_terminal(self):
        # README step 5: the human checks in from any terminal. Terminal.app
        # exports TERM_SESSION_ID, which the controller classifies as a
        # non-unique identity and refuses for anything that touches sessions.
        proc = self.agentctl("doctor", TERM_SESSION_ID="w0t0p0")
        self.assertIn("agentctl doctor:", proc.stdout)
        self.assertIn("agent sessions", proc.stdout)
        self.assertIn("resource interlocks", proc.stdout)
        self.assertNotIn("conversation identity is unavailable", proc.stderr)

        report = json.loads(
            self.agentctl("doctor", "--json", TERM_SESSION_ID="w0t0p0").stdout
        )
        self.assertTrue(report["ok"], report)
        self.assertEqual(report["problems"], [])

        # No identity at all (cron, CI shell) works too.
        self.agentctl("doctor")

        # The exemption is narrow: claiming work still needs a real identity.
        refused = self.agentctl(
            "work", "--agent", "human", "--auto-create",
            "--title", "no identity", "--scope", "src/",
            expect=2, TERM_SESSION_ID="w0t0p0",
        )
        self.assertIn("conversation identity is unavailable", refused.stderr)

    def test_doctor_still_reports_interlocks_without_identity(self):
        # A conversation leases gpu:0 and then dies; the human should see
        # the stuck card and the recovery command from a plain terminal.
        self.agentctl(
            "work", "--agent", "codex", "--auto-create",
            "--title", "train", "--scope", "src/train/", session="conv-a",
        )
        self.agentctl("resource", "acquire", "gpu:0", session="conv-a")
        aged = 0
        for path in glob.glob(str(self.root / ".git" / "agent-workflow" / "sessions" / "*.json")):
            state = json.loads(Path(path).read_text(encoding="utf-8"))
            state["heartbeat_ns"] = time.time_ns() - 2 * 3600 * 10**9
            Path(path).write_text(json.dumps(state), encoding="utf-8")
            aged += 1
        self.assertEqual(aged, 1)

        proc = self.agentctl("doctor", TERM_SESSION_ID="w0t0p0")
        self.assertIn("stuck without a live holder", proc.stdout)
        self.assertIn("gpu:0", proc.stdout)
        self.assertIn("resource release", proc.stdout)
        self.assertIn("--force-stale", proc.stdout)
        self.assertIn("stale agent session claim", proc.stdout)

    def test_identity_free_set_is_shared_with_the_hook(self):
        from tools import agent_workflow_hook as workflow_hook

        self.assertIn(("doctor",), agentctl.IDENTITY_FREE_COMMAND_PATHS)
        self.assertIn(("doctor",), workflow_hook.IDENTITY_FREE_COMMAND_PATHS)
        self.assertFalse(
            workflow_hook.command_requires_agentctl_identity("python3 tools/agentctl.py doctor")
        )
        template = (KIT / "templates" / "project" / "tools" / "agent_workflow_hook.py").read_text(
            encoding="utf-8"
        )
        self.assertEqual(
            template, (KIT / "tools" / "agent_workflow_hook.py").read_text(encoding="utf-8"),
            "the distributed hook template must match tools/agent_workflow_hook.py",
        )


class GitignoreManagedEntriesTest(_KitRepoTestCase):
    def test_init_ignores_the_default_artifact_root(self):
        text = (self.root / ".gitignore").read_text(encoding="utf-8")
        for entry in agentctl.GITIGNORE_MANAGED_ENTRIES:
            self.assertIn(entry, text.splitlines())
        self.assertIn(".agent-artifacts/", agentctl.GITIGNORE_MANAGED_ENTRIES)
        # `run start --output .agent-artifacts/<task>/` is where checkpoints
        # land; Git must not offer them for staging.
        probe = self.git("check-ignore", "-q", ".agent-artifacts/T-001/ckpt.bin", check=False)
        self.assertEqual(probe.returncode, 0, probe.stdout + probe.stderr)

    def test_rerun_appends_only_missing_entries_inside_the_managed_block(self):
        gitignore = self.root / ".gitignore"
        gitignore.write_text(
            "build/\n\n# Agent Workflow Kit local state\n.agent/state/\n.agent/tmp/\n\n*.pyc\n",
            encoding="utf-8",
        )
        self.init()
        lines = gitignore.read_text(encoding="utf-8").splitlines()
        self.assertEqual(
            lines,
            ["build/", "", "# Agent Workflow Kit local state", ".agent/state/",
             ".agent/tmp/", ".agent-artifacts/", "", "*.pyc"],
        )
        self.init()
        self.assertEqual(lines, gitignore.read_text(encoding="utf-8").splitlines())

    def test_user_spelling_of_an_entry_is_respected(self):
        gitignore = self.root / ".gitignore"
        gitignore.write_text("/.agent-artifacts/\n", encoding="utf-8")
        self.init()
        text = gitignore.read_text(encoding="utf-8")
        self.assertEqual(text.count("agent-artifacts"), 1, text)
        self.assertIn(".agent/state/", text)


class ReviewerRegistrationHintTest(_KitRepoTestCase):
    def test_finish_hint_and_gate_refusal_name_the_registration_command(self):
        self.agentctl(
            "work", "--agent", "codex", "--auto-create",
            "--title", "fix the data loader", "--scope", "src/data/", session="worker",
        )
        board = json.loads((self.root / ".agent" / "board.json").read_text(encoding="utf-8"))
        task = next(t for t, e in board["tasks"].items() if e.get("title") == "fix the data loader")
        finished = self.agentctl(
            "finish", "--summary", "done", "--tests", "pytest: 1 passed", session="worker",
        )
        self.assertIn("agentctl agents add --id <reviewer> --role review", finished.stdout)
        self.assertIn(f"gate approve --task {task} --by <reviewer>", finished.stdout)

        # An unregistered reviewer follows the README but skips registration.
        self.agentctl(
            "work", "--agent", "reviewer-x", "--auto-create", "--type", "review",
            "--title", f"review {task}", "--scope", ".agent/", session="reviewer",
        )
        refused = self.agentctl(
            "gate", "approve", "--task", task, "--by", "reviewer-x", "--note", "lgtm",
            expect=1, session="reviewer",
        )
        self.assertIn("independent gate decision rejected", refused.stderr)
        self.assertIn("agentctl agents add --id reviewer-x --role review", refused.stderr)

        # Registering, refreshing the read receipt, and deciding then works.
        self.agentctl("agents", "add", "--id", "reviewer-x", "--role", "review", session="reviewer")
        self.agentctl("refresh", session="reviewer")
        approved = self.agentctl(
            "gate", "approve", "--task", task, "--by", "reviewer-x", "--note", "lgtm",
            session="reviewer",
        )
        self.assertIn("approved -> done", approved.stdout)


if __name__ == "__main__":
    unittest.main()
