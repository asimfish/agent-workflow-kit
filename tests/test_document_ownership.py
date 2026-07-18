"""Document ownership and scope-aware receipt regressions.

Contract under test:

1. Controller-generated workflow files (board, task index, state views, shared
   progress log, gate records, loop runtime/reports, install manifest) can never
   be edited through agent tool calls, even when the active task scope covers
   `.agent/`.
2. A business-scoped worker (no `.agent/` in scope) may still edit its own task
   document; every other `.agent` document stays read-only for it.
3. Read receipts are scope-aware: another task's lifecycle (create/finish) does
   not invalidate this conversation's receipt, while plan-body edits, this
   task's own index row, and this task's own document still do.
"""

import json
import os
import re
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


class DocumentOwnershipRegressionTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="awk-doc-ownership-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True, timeout=60)
        install = subprocess.run(
            [sys.executable, str(KIT / "tools" / "agentctl.py"), "init", str(self.root)],
            cwd=KIT, text=True, capture_output=True, timeout=120,
        )
        self.assertEqual(install.returncode, 0, install.stdout + install.stderr)

    def env(self, session):
        env = os.environ.copy()
        for name in (*PROVIDER_ENV, *WORKFLOW_ENV):
            env.pop(name, None)
        env["AGENT_WORKFLOW_SESSION_ID"] = session
        return env

    def agentctl(self, *args, session="one", expect=0):
        proc = subprocess.run(
            [sys.executable, "tools/agentctl.py", *args], cwd=self.root,
            env=self.env(session), text=True, capture_output=True, timeout=120,
        )
        self.assertEqual(proc.returncode, expect, proc.stdout + proc.stderr)
        return proc

    def hook(self, event, payload, session="one"):
        return subprocess.run(
            [sys.executable, "tools/agent_workflow_hook.py", event], cwd=self.root,
            env=self.env(session), input=json.dumps(payload), text=True,
            capture_output=True, timeout=120,
        )

    def start(self, session, task, scope, agent="codex"):
        return self.agentctl(
            "work", "--agent", agent, "--auto-create", "--new-id", task,
            "--title", f"work for {session}", "--scope", scope,
            session=session,
        )

    def guard(self, path, session="one", expect=0):
        return self.agentctl(
            "sessions", "guard", "--path", path, session=session, expect=expect,
        )

    def test_controller_generated_files_reject_direct_writes_even_with_agent_scope(self):
        self.start("one", "T-201", ".agent/,src/")
        self.agentctl("refresh", session="one")
        for rel in (
            ".agent/board.json",
            ".agent/TASKS.md",
            ".agent/agents.json",
            ".agent/state/SESSIONS.md",
            ".agent/logs/progress.md",
            ".agent/gates/T-201.md",
            ".agent/loops/state.json",
            ".agent/loops/runs/20990101-000000-example.md",
            ".agent/bus/inbox/packet.json",
            ".agent/handoffs/guidance-a-to-b.md",
            ".agent/evals/runs/20990101-000000/result.json",
            ".agent/evals/decisions/decision.json",
            ".agent/evals/eval-hmac.key",
            ".agent/install-manifest.json",
        ):
            blocked = self.guard(rel, session="one", expect=1)
            self.assertIn("controller-generated", blocked.stderr, rel)
            self.assertIn("agentctl", blocked.stderr, rel)
        # The same denial must reach tool calls through the PreToolUse hook.
        hook_blocked = self.hook(
            "pre-tool-use",
            {"tool_name": "Write", "tool_input": {"file_path": ".agent/board.json"}},
            session="one",
        )
        self.assertIn(
            "controller-generated", json.loads(hook_blocked.stdout)["reason"],
        )
        # Editable project-owned docs stay writable for an `.agent/`-scoped task.
        self.guard(".agent/PROJECT_PLAN.md", session="one")
        self.guard(".agent/tasks/T-201.md", session="one")
        self.guard(".agent/rules/agent-operating-rules.md", session="one")
        # Policy files stay scope-based: checkpoint and eval-suite definitions.
        self.guard(".agent/loops/checkpoints.json", session="one")
        self.guard(".agent/evals/suites.json", session="one")

    def test_business_scoped_worker_can_edit_only_its_own_task_doc(self):
        self.start("one", "T-211", "src/one/")
        self.start("two", "T-212", "src/two/")
        self.agentctl("refresh", session="one")
        # Own task document is auto-included in the effective write scope.
        self.guard(".agent/tasks/T-211.md", session="one")
        own_doc_hook = self.hook(
            "pre-tool-use",
            {"tool_name": "Edit", "tool_input": {"file_path": ".agent/tasks/T-211.md"}},
            session="one",
        )
        self.assertNotIn("block", own_doc_hook.stdout)
        # Everything else in .agent/ stays out of reach for a business scope.
        peer_doc = self.guard(".agent/tasks/T-212.md", session="one", expect=1)
        self.assertIn("outside active task scope", peer_doc.stderr)
        plan = self.guard(".agent/PROJECT_PLAN.md", session="one", expect=1)
        self.assertIn("outside active task scope", plan.stderr)
        board = self.guard(".agent/board.json", session="one", expect=1)
        self.assertIn("controller-generated", board.stderr)
        # Business scope itself keeps working.
        self.guard("src/one/module.py", session="one")

    def test_unrelated_task_lifecycle_does_not_invalidate_receipts(self):
        self.start("one", "T-221", "src/one/")
        self.agentctl("refresh", session="one")
        # A sibling conversation creates, works, and completes a disjoint task.
        self.start("two", "T-222", "src/two/")
        self.agentctl("refresh", session="two")
        self.agentctl("note", "sibling progress before completion", session="two")
        self.agentctl(
            "complete", "--summary", "sibling task done",
            "--tests", "not applicable (docs-only fixture)", session="two",
        )
        # T-222's create + complete changed TASKS.md rows, the plan Task Board
        # checkbox, and the plan Change Log. None of that concerns T-221.
        note = self.agentctl("note", "must land without refresh", session="one")
        self.assertIn("progress recorded", note.stdout)

    def test_plan_body_edits_and_own_row_changes_still_invalidate(self):
        self.start("one", "T-231", "src/one/")
        self.agentctl("refresh", session="one")
        plan = self.root / ".agent" / "PROJECT_PLAN.md"
        text = plan.read_text(encoding="utf-8")
        # 1. A real plan-body change (before the Change Log) must block writes.
        plan.write_text(
            text.replace("## Task Board", "New global priority statement.\n\n## Task Board", 1),
            encoding="utf-8",
        )
        blocked = self.agentctl("note", "blocked by plan body", session="one", expect=1)
        self.assertIn("documents changed", blocked.stderr)
        self.agentctl("refresh", session="one")
        # 2. A Change-Log-only append must NOT block writes.
        with plan.open("a", encoding="utf-8") as fh:
            fh.write("- 2099-01-01 00:00:00 - other-agent - unrelated completion note.\n")
        note = self.agentctl("note", "changelog appends are irrelevant", session="one")
        self.assertIn("progress recorded", note.stdout)
        # 3. Editing this task's own index row must block writes.
        tasks_md = self.root / ".agent" / "TASKS.md"
        rows = tasks_md.read_text(encoding="utf-8")
        self.assertIn("| T-231 |", rows)
        tasks_md.write_text(
            re.sub(r"^(\| T-231 \|[^\n]*)$", r"\1 (edited)", rows, count=1, flags=re.M),
            encoding="utf-8",
        )
        blocked = self.agentctl("note", "blocked by own row", session="one", expect=1)
        self.assertIn("documents changed", blocked.stderr)
        self.agentctl("refresh", session="one")
        # 4. Editing this task's own document must still block until refresh.
        own_doc = self.root / ".agent" / "tasks" / "T-231.md"
        own_doc.write_text(
            own_doc.read_text(encoding="utf-8") + "\nHuman-added clarification.\n",
            encoding="utf-8",
        )
        blocked = self.agentctl("note", "blocked by own doc", session="one", expect=1)
        self.assertIn("documents changed", blocked.stderr)
        self.agentctl("refresh", session="one")
        self.agentctl("note", "clean after refresh", session="one")


if __name__ == "__main__":
    unittest.main()
