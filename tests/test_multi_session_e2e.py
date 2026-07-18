"""Four-conversation end-to-end storm mirroring the reported ChemBench layout.

Conversation A collects on machine liyufeng_4090, B collects on bjxy_5090,
C runs full-pipeline validation, and D reproduces long_horizon — all in one
checkout, all at the same time. The suite asserts the exact user-facing
guarantees: no cross-task contamination, hostile moves refused with zero
writes, and one task finishing without disturbing the other three.
"""

import concurrent.futures
import hashlib
import json
import os
import random
import subprocess
import sys
import shutil
import tempfile
import unittest
from pathlib import Path


KIT = Path(__file__).resolve().parents[1]
PROVIDER_ENV = (
    "CODEX_THREAD_ID",
    "CLAUDE_CODE_SESSION_ID",
    "CURSOR_CONVERSATION_ID",
    "WHALENT_AGENT_ID",
    "WHALENT_CODEX_INSTANCE_ID",
    "WHALENT_COMPOSER_ID",
    "WHALENT_FORK_SOURCE_AGENT_ID",
    "AGENT_SESSION_ID",
    "TERM_SESSION_ID",
)
WORKFLOW_ENV = (
    "AGENT_WORKFLOW_SESSION_ID",
    "AGENT_WORKFLOW_SESSION_KEY",
    "AGENT_WORKFLOW_SESSION_OWNER_RUNTIME",
    "AGENT_WORKFLOW_SESSION_INSTANCE_ID",
    "AGENT_WORKFLOW_PARENT_SESSION_KEY",
    "AGENT_WORKFLOW_SESSION_ISOLATION_ERROR",
)

CONVERSATIONS = {
    "A": {"task": "T-4090", "agent": "codex", "scope": "collect/liyufeng_4090/",
          "title": "collect liyufeng_4090 batch"},
    "B": {"task": "T-5090", "agent": "cursor", "scope": "collect/bjxy_5090/",
          "title": "collect bjxy_5090 batch"},
    "C": {"task": "T-PIPE", "agent": "claude", "scope": "pipeline_test/",
          "title": "full pipeline validation"},
    "D": {"task": "T-LH", "agent": "codex", "scope": "long_horizon/",
          "title": "reproduce long_horizon"},
}


class FourConversationEndToEndTest(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="awk-e2e-chembench-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True, timeout=60)
        for rel in ("collect/liyufeng_4090", "collect/bjxy_5090",
                    "pipeline_test", "long_horizon"):
            (self.root / rel).mkdir(parents=True)
        install = subprocess.run(
            [sys.executable, str(KIT / "tools" / "agentctl.py"), "init", str(self.root)],
            cwd=KIT, text=True, capture_output=True, timeout=120,
        )
        self.assertEqual(install.returncode, 0, install.stdout + install.stderr)

    def env(self, session):
        env = os.environ.copy()
        for name in (*PROVIDER_ENV, *WORKFLOW_ENV):
            env.pop(name, None)
        env["AGENT_WORKFLOW_SESSION_ID"] = f"chembench-conv-{session}"
        return env

    def bare_env(self, extra=None):
        env = os.environ.copy()
        for name in (*PROVIDER_ENV, *WORKFLOW_ENV):
            env.pop(name, None)
        env.update(extra or {})
        return env

    def run_ctl(self, session, *args):
        return subprocess.run(
            [sys.executable, "tools/agentctl.py", *args], cwd=self.root,
            env=self.env(session), text=True, capture_output=True, timeout=180,
        )

    def agentctl(self, *args, session="A", expect=0):
        proc = self.run_ctl(session, *args)
        self.assertEqual(proc.returncode, expect, proc.stdout + proc.stderr)
        return proc

    def claim_all(self):
        for label, spec in CONVERSATIONS.items():
            self.agentctl(
                "work", "--agent", spec["agent"], "--auto-create",
                "--new-id", spec["task"], "--title", spec["title"],
                "--scope", spec["scope"], session=label,
            )
        for label in CONVERSATIONS:
            self.agentctl("refresh", session=label)

    def sessions_by_task(self):
        proc = self.agentctl("sessions", "list", "--json", session="A")
        return {row.get("task"): row for row in json.loads(proc.stdout)["sessions"]}

    def agent_state_snapshot(self):
        digest = hashlib.sha256()
        base = self.root / ".agent"
        for path in sorted(base.rglob("*")):
            if path.is_file():
                digest.update(str(path.relative_to(self.root)).encode())
                digest.update(path.read_bytes())
        return digest.hexdigest()

    def test_four_conversations_storm_without_interference(self):
        self.claim_all()

        def storm(label):
            spec = CONVERSATIONS[label]
            rng = random.Random(label)
            outcomes = []
            for step in range(5):
                op = rng.choice(["note", "resume", "refresh"])
                if op == "note":
                    proc = self.run_ctl(label, "note", f"[{label}] step{step}")
                    if proc.returncode != 0:
                        self.run_ctl(label, "refresh")
                        proc = self.run_ctl(label, "note", f"[{label}] step{step}")
                    outcomes.append(("note", proc.returncode))
                elif op == "resume":
                    proc = self.run_ctl(label, "work", "--agent", spec["agent"])
                    outcomes.append(
                        ("resume", proc.returncode, spec["task"] in proc.stdout)
                    )
                else:
                    proc = self.run_ctl(label, "refresh")
                    outcomes.append(("refresh", proc.returncode))
            return label, outcomes

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            results = dict(
                pool.map(storm, CONVERSATIONS)  # noqa: C417 - order irrelevant
            )
        for label, outcomes in results.items():
            for entry in outcomes:
                self.assertEqual(entry[1], 0, f"{label}: {outcomes}")
                if entry[0] == "resume":
                    self.assertTrue(entry[2], f"{label} resumed a foreign task: {outcomes}")

        rows = self.sessions_by_task()
        expected_tasks = {spec["task"] for spec in CONVERSATIONS.values()}
        self.assertEqual(set(rows) & expected_tasks, expected_tasks)
        keys = [rows[spec["task"]]["workflow_session_key"] for spec in CONVERSATIONS.values()]
        self.assertEqual(len(set(keys)), 4, keys)

        board = json.loads((self.root / ".agent" / "board.json").read_text(encoding="utf-8"))
        for spec in CONVERSATIONS.values():
            self.assertEqual(
                board["tasks"][spec["task"]]["owner"], spec["agent"], spec["task"],
            )
        for label, spec in CONVERSATIONS.items():
            doc = (self.root / ".agent" / "tasks" / f"{spec['task']}.md").read_text(encoding="utf-8")
            self.assertIn(spec["title"], doc)
            for other_label, other in CONVERSATIONS.items():
                if other_label != label:
                    self.assertNotIn(f"[{other_label}] step", doc)
        progress = (self.root / ".agent" / "logs" / "progress.md").read_text(encoding="utf-8")
        for label in CONVERSATIONS:
            self.assertIn(f"[{label}] step", progress)

    def test_hostile_moves_are_refused_with_zero_untracked_writes(self):
        self.claim_all()
        # Stealing another conversation's task.
        steal = self.run_ctl("B", "work", "--agent", "cursor", "--task", "T-4090")
        self.assertNotEqual(steal.returncode, 0, steal.stdout)
        # Claiming a task whose scope overlaps two live sessions.
        self.agentctl(
            "task", "create", "--id", "T-OVER", "--title", "hostile overlap",
            "--owner", "codex", "--scope", "collect/", session="A",
        )
        claim = self.run_ctl("E", "work", "--agent", "codex", "--task", "T-OVER")
        self.assertNotEqual(claim.returncode, 0, claim.stdout)
        # A terminal-only ghost conversation cannot mutate workflow state.
        before = self.agent_state_snapshot()
        ghost = subprocess.run(
            [sys.executable, "tools/agentctl.py", "task", "create", "--id", "T-GHOST",
             "--title", "ghost", "--owner", "ghost", "--scope", "ghost/"],
            cwd=self.root, env=self.bare_env({"TERM_SESSION_ID": "shared-terminal"}),
            text=True, capture_output=True, timeout=120,
        )
        self.assertNotEqual(ghost.returncode, 0, ghost.stdout)
        self.assertEqual(before, self.agent_state_snapshot())
        self.assertFalse((self.root / ".agent" / "tasks" / "T-GHOST.md").exists())

    def test_one_completion_does_not_disturb_the_other_three(self):
        self.claim_all()
        self.agentctl(
            "complete", "--summary", "pipeline validation finished",
            "--tests", "fixture drill", session="C",
        )
        # The other three conversations keep working with no forced refresh:
        # T-PIPE's completion only touched its own rows and the change log.
        for label in ("A", "B", "D"):
            note = self.agentctl(
                "note", f"[{label}] continues untouched", session=label,
            )
            self.assertIn("progress recorded", note.stdout)
        resume = self.agentctl("work", "--agent", "codex", session="A")
        self.assertIn("T-4090", resume.stdout)
        rows = self.sessions_by_task()
        for label in ("A", "B", "D"):
            task = CONVERSATIONS[label]["task"]
            self.assertIn(task, rows)
            self.assertEqual(rows[task]["observed_status"], "active", task)


if __name__ == "__main__":
    unittest.main()
