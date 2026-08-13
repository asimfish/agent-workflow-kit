"""Regression coverage for safe adoption and native lifecycle hook contracts."""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlparse


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
    "AGENT_WORKFLOW_SESSION_ID",
    "AGENT_WORKFLOW_SESSION_KEY",
    "AGENT_WORKFLOW_SESSION_OWNER_RUNTIME",
    "AGENT_WORKFLOW_SESSION_INSTANCE_ID",
    "AGENT_WORKFLOW_PARENT_SESSION_KEY",
    "AGENT_WORKFLOW_SESSION_ISOLATION_ERROR",
)


class InstallAndHookRegressionTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="awk-install-regress-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True, timeout=60)

    def init(self, *args, expect=0):
        proc = subprocess.run(
            [sys.executable, str(KIT / "tools" / "agentctl.py"), "init", str(self.root), *args],
            cwd=KIT,
            text=True,
            capture_output=True,
            timeout=120,
        )
        self.assertEqual(proc.returncode, expect, proc.stdout + proc.stderr)
        return proc

    def clean_env(self):
        env = os.environ.copy()
        for name in IDENTITY_ENV:
            env.pop(name, None)
        env["AGENT_WORKFLOW_SESSION_ID"] = "install-hook-session"
        return env

    def agentctl(self, *args, expect=0):
        proc = subprocess.run(
            [sys.executable, "tools/agentctl.py", *args],
            cwd=self.root,
            env=self.clean_env(),
            text=True,
            capture_output=True,
            timeout=120,
        )
        self.assertEqual(proc.returncode, expect, proc.stdout + proc.stderr)
        return proc

    def hook(self, event, payload):
        return subprocess.run(
            [sys.executable, "tools/agent_workflow_hook.py", event],
            cwd=self.root,
            env=self.clean_env(),
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            timeout=120,
        )

    def test_existing_project_merge_repeat_upgrade_and_atomic_conflict(self):
        (self.root / "AGENTS.md").write_text("# Existing rules\n\nKeep this.\n", encoding="utf-8")
        for rel in (".codex/hooks.json", ".claude/settings.json", ".cursor/hooks.json"):
            path = self.root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            event = "SessionStart" if "cursor" not in rel else "sessionStart"
            hooks = {event: [{"command": "echo existing"}]}
            if "cursor" not in rel:
                hooks["PreToolUse"] = [{
                    "matcher": "Bash|Edit|Write",
                    "hooks": [
                        {"type": "command", "command": "tools/agent_workflow_hook.py pre-tool-use"},
                        {"type": "command", "command": "echo keep-nested"},
                    ],
                }]
            path.write_text(json.dumps({
                "custom": {"preserved": True},
                "hooks": hooks,
            }), encoding="utf-8")
        (self.root / ".gitignore").write_text("build/\n", encoding="utf-8")
        pr_template = self.root / ".github" / "PULL_REQUEST_TEMPLATE.md"
        pr_template.parent.mkdir(parents=True, exist_ok=True)
        pr_template.write_text("# Existing PR checks\n\nKeep this too.\n", encoding="utf-8")

        self.init()
        agents = (self.root / "AGENTS.md").read_text(encoding="utf-8")
        self.assertIn("Keep this.", agents)
        self.assertEqual(agents.count("<!-- agent-workflow-kit:start -->"), 1)
        merged_pr = pr_template.read_text(encoding="utf-8")
        self.assertIn("Keep this too.", merged_pr)
        self.assertEqual(merged_pr.count("<!-- agent-workflow-kit:pr-start -->"), 1)
        for rel, event in (
            (".codex/hooks.json", "SessionStart"),
            (".claude/settings.json", "SessionStart"),
            (".cursor/hooks.json", "sessionStart"),
        ):
            data = json.loads((self.root / rel).read_text(encoding="utf-8"))
            self.assertTrue(data["custom"]["preserved"])
            self.assertTrue(any(row.get("command") == "echo existing" for row in data["hooks"][event]))
            self.assertEqual(
                sum("agent_workflow_hook.py" in json.dumps(row) for row in data["hooks"][event]), 1,
            )
            if "cursor" not in rel:
                self.assertTrue(any(
                    "echo keep-nested" in json.dumps(row) for row in data["hooks"]["PreToolUse"]
                ))

        plan = self.root / ".agent" / "PROJECT_PLAN.md"
        plan.write_text(plan.read_text(encoding="utf-8") + "\nHuman-owned plan change.\n", encoding="utf-8")
        before = {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        self.init()
        after = {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        self.assertEqual(before, after)

        codex_config = self.root / ".codex" / "hooks.json"
        codex_data = json.loads(codex_config.read_text(encoding="utf-8"))
        codex_data["hooks"].pop("PreToolUse")
        codex_config.write_text(json.dumps(codex_data, indent=2) + "\n", encoding="utf-8")
        managed_rule = self.root / ".cursor" / "rules" / "agent-workflow.mdc"
        managed_rule.write_text("local incompatible edit\n", encoding="utf-8")
        codex_before = codex_config.read_bytes()
        drift = json.loads(self.agentctl("doctor", "--json", expect=1).stdout)
        self.assertTrue(any("managed installation files changed" in p for p in drift["problems"]))
        self.assertTrue(any("native hook configuration invalid" in p for p in drift["problems"]))
        failed = self.init(expect=1)
        self.assertIn("aborted before writing", failed.stderr)
        self.assertEqual(codex_config.read_bytes(), codex_before)

        self.init("--force-managed")
        self.assertIn("Human-owned plan change.", plan.read_text(encoding="utf-8"))
        self.assertIn("work --agent cursor", managed_rule.read_text(encoding="utf-8"))
        repaired = json.loads(codex_config.read_text(encoding="utf-8"))
        self.assertIn("PreToolUse", repaired["hooks"])
        doctor = json.loads(self.agentctl("doctor", "--json").stdout)
        self.assertTrue(doctor["ok"], doctor)

        repaired["hooks"]["PreToolUse"][-1]["matcher"] = "Read"
        codex_config.write_text(json.dumps(repaired, indent=2) + "\n", encoding="utf-8")
        ineffective = json.loads(self.agentctl("doctor", "--json", expect=1).stdout)
        self.assertTrue(any("differs from the shipped contract" in p for p in ineffective["problems"]))
        self.init()
        self.agentctl("doctor")

    def test_init_warns_before_overriding_existing_git_hooks(self):
        """init repoints core.hooksPath to .githooks; it must not do so silently
        when the project already has hooks, or its own pre-commit/lint/tests
        stop firing without notice."""
        # Case A: existing custom hooksPath (husky-style).
        (self.root / ".husky").mkdir()
        (self.root / ".husky" / "pre-commit").write_text("#!/bin/sh\nexit 0\n")
        subprocess.run(["git", "config", "core.hooksPath", ".husky"],
                       cwd=self.root, check=True, timeout=60)
        proc = self.init()
        out = proc.stdout + proc.stderr
        self.assertIn(".husky", out, out)
        self.assertTrue(
            "hooks" in out.lower() and ("warn" in out.lower() or "replac" in out.lower()
                                        or "overrid" in out.lower() or "no longer" in out.lower()),
            f"no warning about replacing an existing hooksPath:\n{out}",
        )

    def test_init_warns_about_default_git_hooks_being_bypassed(self):
        hooks = subprocess.run(
            ["git", "rev-parse", "--git-path", "hooks"], cwd=self.root,
            text=True, capture_output=True, timeout=60).stdout.strip()
        hp = self.root / hooks
        hp.mkdir(parents=True, exist_ok=True)
        (hp / "pre-commit").write_text("#!/bin/sh\nexit 0\n")
        (hp / "pre-commit").chmod(0o755)
        proc = self.init()
        out = proc.stdout + proc.stderr
        self.assertTrue(
            "pre-commit" in out.lower() and ("warn" in out.lower() or "bypass" in out.lower()
                                             or "no longer" in out.lower()),
            f"no warning that the default .git/hooks/pre-commit will be bypassed:\n{out}",
        )

    def test_unknown_managed_file_conflict_does_not_partially_install(self):
        path = self.root / ".github" / "workflows" / "agent-workflow-check.yml"
        path.parent.mkdir(parents=True)
        path.write_text("name: project workflow\n", encoding="utf-8")
        failed = self.init(expect=1)
        self.assertIn("agent-workflow-check.yml", failed.stderr)
        self.assertFalse((self.root / ".agent").exists())
        self.assertEqual(path.read_text(encoding="utf-8"), "name: project workflow\n")

    def test_doctor_accepts_crlf_managed_text(self):
        self.init()
        managed = self.root / "tools" / "agentctl.py"
        managed.write_bytes(managed.read_bytes().replace(b"\n", b"\r\n"))

        doctor = json.loads(self.agentctl("doctor", "--json").stdout)
        self.assertTrue(doctor["ok"], doctor)
        self.init()

    def test_provider_payloads_block_without_session_and_allow_after_work_entry(self):
        self.init()
        fixtures = [
            {"tool_name": "Bash", "tool_input": {"command": "touch blocked-codex"}},
            {"tool_name": "Bash", "tool_input": {"command": "touch blocked-claude"}},
            {"tool": "Shell", "command": "touch blocked-cursor"},
        ]
        for payload in fixtures:
            blocked = self.hook("pre-tool-use", payload)
            self.assertEqual(blocked.returncode, 0, blocked.stderr)
            decision = json.loads(blocked.stdout)
            self.assertEqual(decision["decision"], "block")
            self.assertEqual(decision["permission"], "deny")
            self.assertEqual(
                decision["hookSpecificOutput"]["permissionDecision"], "deny",
            )

        read_only = self.hook(
            "pre-tool-use", {"tool_name": "Bash", "tool_input": {"command": "git status"}},
        )
        self.assertEqual(read_only.returncode, 0)
        self.assertEqual(read_only.stdout, "")

        self.agentctl(
            "work", "--agent", "codex", "--auto-create", "--new-id", "T-101",
            "--title", "hook contract", "--scope", "src/",
        )
        allowed = self.hook(
            "pre-tool-use", {"tool_name": "Write", "tool_input": {"file_path": "src/new.py"}},
        )
        self.assertEqual(allowed.returncode, 0, allowed.stderr)
        self.assertEqual(allowed.stdout, "")

        session = self.hook("session-start", {"source": "startup"})
        context = json.loads(session.stdout)
        self.assertIn("WORKFLOW_ENTRY.md", context["additional_context"])
        stopped = self.hook("stop", {})
        self.assertIn("task session is still active", json.loads(stopped.stdout)["additional_context"])
        self.agentctl("finish", "--summary", "hook contract complete", "--tests", "fixture checks")
        after_finish = self.hook(
            "pre-tool-use", {"tool_name": "Write", "tool_input": {"file_path": "src/late.py"}},
        )
        self.assertIn("already been completed", json.loads(after_finish.stdout)["reason"])

    def test_shipped_provider_hooks_cover_all_tools_mcp_and_clear(self):
        self.init()
        codex = json.loads((self.root / ".codex/hooks.json").read_text(encoding="utf-8"))
        claude = json.loads((self.root / ".claude/settings.json").read_text(encoding="utf-8"))
        cursor = json.loads((self.root / ".cursor/hooks.json").read_text(encoding="utf-8"))

        for config in (codex, claude):
            self.assertEqual(
                config["hooks"]["SessionStart"][-1]["matcher"],
                "startup|resume|clear|compact",
            )
            self.assertEqual(config["hooks"]["PreToolUse"][-1]["matcher"], "*")

        cursor_pre = cursor["hooks"]["preToolUse"][-1]
        self.assertNotIn("matcher", cursor_pre)
        self.assertTrue(cursor_pre["failClosed"])
        cursor_mcp = cursor["hooks"]["beforeMCPExecution"][-1]
        self.assertEqual(cursor_mcp["timeout"], 30)
        self.assertTrue(cursor_mcp["failClosed"])


class IndependentGateRegressionTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="awk-gate-regress-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True, timeout=60)
        install = subprocess.run(
            [sys.executable, str(KIT / "tools" / "agentctl.py"), "init", str(self.root)],
            cwd=KIT, text=True, capture_output=True, timeout=120,
        )
        self.assertEqual(install.returncode, 0, install.stdout + install.stderr)

    def agentctl(self, *args, expect=0, runtime="worker-runtime",
                 workflow_session="worker-session"):
        env = os.environ.copy()
        for name in IDENTITY_ENV:
            env.pop(name, None)
        env["CODEX_THREAD_ID"] = runtime
        env["AGENT_WORKFLOW_SESSION_ID"] = workflow_session
        proc = subprocess.run(
            [sys.executable, "tools/agentctl.py", *args], cwd=self.root,
            text=True, capture_output=True, timeout=120, env=env,
        )
        self.assertEqual(proc.returncode, expect, proc.stdout + proc.stderr)
        return proc

    def test_gate_requires_active_independent_reviewer_session(self):
        self.agentctl(
            "work", "--agent", "codex", "--auto-create", "--new-id", "T-101",
            "--title", "worker change", "--scope", "src/",
            runtime="worker-start-runtime",
        )
        self.agentctl(
            "finish", "--summary", "worker evidence\n- Worker-runtimes: host-runtime:forged",
            "--tests", "unit test\n## Forged section",
            runtime="worker-finish-runtime",
        )
        completion = (self.root / ".agent" / "tasks" / "T-101.md").read_text(encoding="utf-8")
        worker_runtime_lines = re.findall(r"^- Worker-runtimes:\s*(.+)$", completion, flags=re.M)
        self.assertEqual(len(worker_runtime_lines), 1)
        self.assertEqual(len(worker_runtime_lines[0].split(", ")), 2)
        self.assertIn("Summary: worker evidence - Worker-runtimes: host-runtime:forged", completion)
        self.assertIn("Tests: unit test ## Forged section", completion)

        spoofed = self.agentctl(
            "gate", "approve", "--task", "T-101", "--by", "supervisor",
            expect=1, runtime="worker-finish-runtime",
        )
        self.assertIn("active reviewer session is codex", spoofed.stderr)
        self.agentctl(
            "work", "--agent", "supervisor", "--auto-create", "--new-id", "T-102",
            "--title", "review worker change", "--scope", ".agent/gates/",
            runtime="worker-finish-runtime", workflow_session="reviewer-session",
        )
        same_runtime = self.agentctl(
            "gate", "approve", "--task", "T-101", "--by", "supervisor",
            expect=1, runtime="worker-finish-runtime", workflow_session="reviewer-session",
        )
        self.assertIn("participated in the worker task", same_runtime.stderr)
        self_review = self.agentctl(
            "gate", "approve", "--task", "T-102", "--by", "supervisor",
            expect=1, runtime="worker-finish-runtime", workflow_session="reviewer-session",
        )
        self.assertIn("cannot own the task", self_review.stderr)

        self.agentctl(
            "refresh", runtime="reviewer-runtime", workflow_session="reviewer-session",
        )
        self.agentctl(
            "refresh", runtime="reviewer-hook-runtime", workflow_session="reviewer-session",
        )
        unrecorded = self.agentctl(
            "gate", "approve", "--task", "T-101", "--by", "supervisor",
            expect=1, runtime="unrecorded-reviewer-runtime",
            workflow_session="reviewer-session",
        )
        self.assertIn("not bound to the current host runtime", unrecorded.stderr)
        self.agentctl(
            "gate", "approve", "--task", "T-101", "--by", "supervisor",
            runtime="reviewer-runtime", workflow_session="reviewer-session",
        )
        board = json.loads(self.agentctl("board", "--json").stdout)
        self.assertEqual(board["tasks"]["T-101"]["status"], "done")
        gate = (self.root / ".agent" / "gates" / "T-101.md").read_text(encoding="utf-8")
        self.assertIn("Reviewer task: T-102", gate)
        self.assertIn("Reviewer runtime: host-runtime:", gate)
        self.assertIn("- Note: none\n", gate)
        self.assertNotRegex(gate, r" +$")
        self.agentctl(
            "check", "--mode", "manual", runtime="reviewer-runtime",
            workflow_session="reviewer-session",
        )

    def test_decided_review_task_closes_on_finish_and_backlog_reconciles(self):
        self.agentctl(
            "work", "--agent", "codex", "--auto-create", "--new-id", "T-201",
            "--title", "worker change", "--scope", "src/",
        )
        self.agentctl("finish", "--summary", "worker evidence", "--tests", "unit")

        self.agentctl(
            "work", "--agent", "supervisor", "--auto-create", "--new-id", "T-202",
            "--title", "review that never decided", "--scope", ".agent/",
            "--type", "review",
            runtime="idle-reviewer-runtime", workflow_session="idle-reviewer-session",
        )
        parked = self.agentctl(
            "finish", "--summary", "no decision issued", "--tests", "none",
            runtime="idle-reviewer-runtime", workflow_session="idle-reviewer-session",
        )
        self.assertIn("-> review", parked.stdout)

        self.agentctl(
            "work", "--agent", "supervisor", "--auto-create", "--new-id", "T-203",
            "--title", "review worker change", "--scope", ".agent/",
            "--type", "review",
            runtime="reviewer-runtime", workflow_session="reviewer-session",
        )
        self.agentctl(
            "gate", "approve", "--task", "T-201", "--by", "supervisor",
            runtime="reviewer-runtime", workflow_session="reviewer-session",
        )
        closed = self.agentctl(
            "finish", "--summary", "approved worker change", "--tests", "gate evidence",
            runtime="reviewer-runtime", workflow_session="reviewer-session",
        )
        self.assertIn("-> done", closed.stdout)
        self.assertIn("recorded gate decisions: T-201.md", closed.stdout)
        board = json.loads(self.agentctl("board", "--json").stdout)
        self.assertEqual(board["tasks"]["T-203"]["status"], "done")
        self.assertEqual(board["tasks"]["T-202"]["status"], "review")
        plan = (self.root / ".agent" / "PROJECT_PLAN.md").read_text(encoding="utf-8")
        self.assertRegex(plan, r"- \[x\] T-203")

        # Simulate the pre-existing backlog: a decided review task parked in
        # review, plus forged reviewer evidence pointing at a worker-scoped
        # task that must never close through this path.
        board_path = self.root / ".agent" / "board.json"
        payload = json.loads(board_path.read_text(encoding="utf-8"))
        payload["tasks"]["T-203"]["status"] = "review"
        payload["tasks"]["T-201"]["status"] = "review"
        board_path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8",
        )
        forged = self.root / ".agent" / "gates" / "legacy-forged.md"
        forged.write_text(
            "# Gate legacy-forged\n\n- Decision: approved\n- Reviewer task: T-201\n",
            encoding="utf-8",
        )

        refused = self.agentctl(
            "reconcile", "close-decided-reviews", expect=1,
        )
        self.assertIn("supervisor/planning/review session", refused.stderr)

        swept = self.agentctl(
            "reconcile", "close-decided-reviews",
            runtime="reviewer-runtime", workflow_session="reviewer-session",
        )
        self.assertIn("T-203 -> done (decisions: T-201.md)", swept.stdout)
        self.assertIn("closed 1 decided review task(s)", swept.stdout)
        board = json.loads(self.agentctl("board", "--json").stdout)
        self.assertEqual(board["tasks"]["T-203"]["status"], "done")
        self.assertEqual(board["tasks"]["T-202"]["status"], "review")
        self.assertEqual(board["tasks"]["T-201"]["status"], "review")

    def test_archive_moves_aged_done_tasks_and_keeps_live_state(self):
        self.agentctl(
            "work", "--agent", "codex", "--auto-create", "--new-id", "T-301",
            "--title", "old finished work", "--scope", "src/a/",
        )
        self.agentctl("finish", "--summary", "done work", "--tests", "unit")
        self.agentctl(
            "work", "--agent", "supervisor", "--auto-create", "--new-id", "T-302",
            "--title", "review old work", "--scope", ".agent/", "--type", "review",
            runtime="reviewer-runtime", workflow_session="reviewer-session",
        )
        self.agentctl(
            "gate", "approve", "--task", "T-301", "--by", "supervisor",
            runtime="reviewer-runtime", workflow_session="reviewer-session",
        )

        refused = self.agentctl("reconcile", "archive", expect=1)
        self.assertIn("supervisor/planning/review session", refused.stderr)

        none_yet = self.agentctl(
            "reconcile", "archive",
            runtime="reviewer-runtime", workflow_session="reviewer-session",
        )
        self.assertIn("no done tasks older", none_yet.stdout)

        board_path = self.root / ".agent" / "board.json"
        payload = json.loads(board_path.read_text(encoding="utf-8"))
        payload["tasks"]["T-301"]["updated_at"] = "2026-01-01 00:00:00"
        board_path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

        swept = self.agentctl(
            "reconcile", "archive", "--days", "30",
            runtime="reviewer-runtime", workflow_session="reviewer-session",
        )
        self.assertIn("archived T-301", swept.stdout)
        board = json.loads(self.agentctl("board", "--json").stdout)
        self.assertNotIn("T-301", board["tasks"])
        self.assertIn("T-302", board["tasks"])
        self.assertFalse((self.root / ".agent" / "tasks" / "T-301.md").exists())
        self.assertTrue(
            (self.root / ".agent" / "archive" / "tasks" / "T-301.md").is_file()
        )
        self.assertTrue(
            (self.root / ".agent" / "archive" / "gates" / "T-301.md").is_file()
        )
        archived = json.loads(
            (self.root / ".agent" / "archive" / "board.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertIn("T-301", archived["tasks"])
        tasks_view = (self.root / ".agent" / "TASKS.md").read_text(encoding="utf-8")
        self.assertNotIn("T-301", tasks_view)
        plan_view = (self.root / ".agent" / "PROJECT_PLAN.md").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("T-301", plan_view)
        self.agentctl("reconcile", "check")


class GithubMergeGateRegressionTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="awk-github-gate-regress-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True, timeout=60)
        subprocess.run(
            ["git", "config", "user.email", "agent@example.com"],
            cwd=self.root, check=True, capture_output=True, text=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Agent Test"],
            cwd=self.root, check=True, capture_output=True, text=True,
        )
        subprocess.run(
            ["git", "remote", "add", "origin", "https://github.com/example/project.git"],
            cwd=self.root, check=True, capture_output=True, text=True,
        )
        install = subprocess.run(
            [sys.executable, str(KIT / "tools" / "agentctl.py"), "init", str(self.root)],
            cwd=KIT, text=True, capture_output=True, timeout=120,
        )
        self.assertEqual(install.returncode, 0, install.stdout + install.stderr)
        self.env = os.environ.copy()
        for name in IDENTITY_ENV:
            self.env.pop(name, None)
        self.env["AGENT_WORKFLOW_SESSION_ID"] = "github-gate-session"
        fake_bin = self.root / "fake-bin"
        fake_bin.mkdir()
        if os.name == "nt":
            script = fake_bin / "gh.py"
            script.write_text(
                "import os, sys\n"
                "if len(sys.argv) > 1 and sys.argv[1] == 'api':\n"
                "    expected = os.environ.get('FAKE_GH_EXPECT_HOST', '')\n"
                "    if expected and ('--hostname' not in sys.argv or sys.argv[sys.argv.index('--hostname') + 1] != expected):\n"
                "        print('wrong API host', file=sys.stderr); raise SystemExit(2)\n"
                "    if 'graphql' not in sys.argv or '--paginate' not in sys.argv:\n"
                "        print('file evidence is not GraphQL-paginated', file=sys.stderr); raise SystemExit(2)\n"
                "    print(os.environ.get('FAKE_GH_PR_FILES', ''))\n"
                "else:\n"
                "    expected_repo = os.environ.get('FAKE_GH_EXPECT_REPO', '')\n"
                "    if expected_repo and ('--repo' not in sys.argv or sys.argv[sys.argv.index('--repo') + 1] != expected_repo):\n"
                "        print('wrong PR repository', file=sys.stderr); raise SystemExit(2)\n"
                "    print(os.environ['FAKE_GH_PR_JSON'])\n",
                encoding="utf-8",
            )
            wrapper = fake_bin / "gh.cmd"
            wrapper.write_text(
                f'@"{sys.executable}" -X utf8 "%~dp0gh.py" %*\n', encoding="utf-8",
            )
        else:
            wrapper = fake_bin / "gh"
            wrapper.write_text(
                f"#!{sys.executable}\nimport os, sys\n"
                "if len(sys.argv) > 1 and sys.argv[1] == 'api':\n"
                "    expected = os.environ.get('FAKE_GH_EXPECT_HOST', '')\n"
                "    if expected and ('--hostname' not in sys.argv or sys.argv[sys.argv.index('--hostname') + 1] != expected):\n"
                "        print('wrong API host', file=sys.stderr); raise SystemExit(2)\n"
                "    if 'graphql' not in sys.argv or '--paginate' not in sys.argv:\n"
                "        print('file evidence is not GraphQL-paginated', file=sys.stderr); raise SystemExit(2)\n"
                "    print(os.environ.get('FAKE_GH_PR_FILES', ''))\n"
                "else:\n"
                "    expected_repo = os.environ.get('FAKE_GH_EXPECT_REPO', '')\n"
                "    if expected_repo and ('--repo' not in sys.argv or sys.argv[sys.argv.index('--repo') + 1] != expected_repo):\n"
                "        print('wrong PR repository', file=sys.stderr); raise SystemExit(2)\n"
                "    print(os.environ['FAKE_GH_PR_JSON'])\n",
                encoding="utf-8",
            )
            wrapper.chmod(0o755)
        self.env["PATH"] = str(fake_bin) + os.pathsep + self.env.get("PATH", "")

    def agentctl(self, *args, expect=0):
        proc = subprocess.run(
            [sys.executable, "tools/agentctl.py", *args], cwd=self.root,
            text=True, capture_output=True, timeout=120, env=self.env,
        )
        self.assertEqual(proc.returncode, expect, proc.stdout + proc.stderr)
        return proc

    def set_pr(self, *, state="MERGED", files=None, merged_by="project-owner", oid=None,
               url="https://github.com/example/project/pull/7", expected_repo=""):
        files = files or []
        self.env["FAKE_GH_PR_FILES"] = "\n".join(files)
        self.env["FAKE_GH_EXPECT_HOST"] = urlparse(url).netloc
        self.env["FAKE_GH_EXPECT_REPO"] = expected_repo
        self.env["FAKE_GH_PR_JSON"] = json.dumps({
            "state": state,
            "mergedAt": "2026-07-16T00:00:00Z" if state == "MERGED" else None,
            "mergeCommit": {"oid": oid or "0" * 40},
            "mergedBy": {"login": merged_by},
            "url": url,
            "baseRefName": "main",
            "files": [{"path": path} for path in files[:100]],
        })

    def test_reconcile_github_requires_authoritative_merge_evidence(self):
        self.agentctl(
            "work", "--agent", "codex", "--auto-create", "--new-id", "T-101",
            "--title", "merged worker task", "--scope", "src/",
        )
        self.agentctl("finish", "--summary", "worker complete", "--tests", "unit tests")
        subprocess.run(["git", "add", "-A"], cwd=self.root, check=True)
        subprocess.run(
            ["git", "commit", "--no-verify", "-q", "-m", "test merged state"],
            cwd=self.root, check=True,
        )
        merge_oid = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=self.root, text=True,
        ).strip()
        self.agentctl(
            "work", "--agent", "supervisor", "--auto-create", "--new-id", "T-102",
            "--title", "reconcile merge", "--scope", ".agent/",
        )

        self.set_pr(state="OPEN", files=[".agent/tasks/T-101.md"], oid=merge_oid)
        self.agentctl(
            "gate", "reconcile-github", "--task", "T-101", "--by", "project-owner",
            "--pr", "7", expect=1,
        )
        self.set_pr(files=["README.md"], oid=merge_oid)
        self.agentctl(
            "gate", "reconcile-github", "--task", "T-101", "--by", "project-owner",
            "--pr", "7", expect=1,
        )
        self.set_pr(files=[".agent/tasks/T-101.md"], oid=merge_oid)
        mismatch = self.agentctl(
            "gate", "reconcile-github", "--task", "T-101", "--by", "another-user",
            "--pr", "7", expect=1,
        )
        self.assertIn("does not match GitHub mergedBy", mismatch.stderr)
        self.set_pr(
            files=[".agent/tasks/T-101.md"], oid=merge_oid,
            url="https://github.com/untrusted/fork/pull/7",
        )
        wrong_repo = self.agentctl(
            "gate", "reconcile-github", "--task", "T-101", "--by", "project-owner",
            "--pr", "7", expect=1,
        )
        self.assertIn("does not match the checkout origin", wrong_repo.stderr)
        board = json.loads(self.agentctl("board", "--json").stdout)
        self.assertEqual(board["tasks"]["T-101"]["status"], "review")

        self.set_pr(files=[".agent/tasks/T-101.md"], oid=merge_oid)
        self.agentctl(
            "gate", "reconcile-github", "--task", "T-101", "--by", "project-owner",
            "--pr", "7",
        )
        board = json.loads(self.agentctl("board", "--json").stdout)
        self.assertEqual(board["tasks"]["T-101"]["status"], "done")
        gate = (self.root / ".agent" / "gates" / "T-101.md").read_text(encoding="utf-8")
        self.assertIn("Source: github-merge", gate)
        self.assertIn(f"Merge commit: {merge_oid}", gate)
        self.assertIn("By: project-owner", gate)
        gate_bytes = (self.root / ".agent" / "gates" / "T-101.md").read_bytes()
        repeated = self.agentctl(
            "gate", "reconcile-github", "--task", "T-101", "--by", "project-owner",
            "--pr", "7",
        )
        self.assertIn("already reconciled", repeated.stdout)
        self.assertEqual(
            (self.root / ".agent" / "gates" / "T-101.md").read_bytes(), gate_bytes,
        )

        independent_gate = (
            "# Gate T-101\n\n- Decision: approved\n- Source: runtime-review\n"
            "- By: independent-reviewer\n"
        ).encode()
        (self.root / ".agent" / "gates" / "T-101.md").write_bytes(independent_gate)
        protected = self.agentctl(
            "gate", "reconcile-github", "--task", "T-101", "--by", "project-owner",
            "--pr", "7", expect=1,
        )
        self.assertIn("existing gate evidence will not be overwritten", protected.stderr)
        self.assertEqual(
            (self.root / ".agent" / "gates" / "T-101.md").read_bytes(), independent_gate,
        )

    def test_reconcile_uses_completion_evidence_from_merge_commit(self):
        self.agentctl(
            "work", "--agent", "codex", "--auto-create", "--new-id", "T-201",
            "--title", "worker with forged evidence", "--scope", "src/",
        )
        self.agentctl("finish", "--summary", "worker complete", "--tests", "unit tests")
        task_doc = self.root / ".agent" / "tasks" / "T-201.md"
        valid_body = task_doc.read_text(encoding="utf-8")
        invalid_body = valid_body.replace("- Summary: worker complete", "- Summary:")
        invalid_body = invalid_body.replace("- Tests: unit tests", "- Tests: not run")
        task_doc.write_text(invalid_body, encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=self.root, check=True)
        subprocess.run(
            ["git", "commit", "--no-verify", "-q", "-m", "test incomplete merged state"],
            cwd=self.root, check=True,
        )
        merge_oid = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=self.root, text=True,
        ).strip()
        task_doc.write_text(valid_body, encoding="utf-8")
        self.agentctl(
            "work", "--agent", "supervisor", "--auto-create", "--new-id", "T-202",
            "--title", "reconcile forged merge", "--scope", ".agent/",
        )
        self.set_pr(files=[".agent/tasks/T-201.md"], oid=merge_oid)
        rejected = self.agentctl(
            "gate", "reconcile-github", "--task", "T-201", "--by", "project-owner",
            "--pr", "7", expect=1,
        )
        self.assertIn("task completion summary is missing", rejected.stderr)
        self.assertIn("task verification evidence is missing", rejected.stderr)
        board = json.loads(self.agentctl("board", "--json").stdout)
        self.assertEqual(board["tasks"]["T-201"]["status"], "review")

    def test_reconcile_reads_task_path_beyond_first_file_page(self):
        self.agentctl(
            "work", "--agent", "codex", "--auto-create", "--new-id", "T-301",
            "--title", "large merged worker task", "--scope", "src/",
        )
        self.agentctl("finish", "--summary", "large worker complete", "--tests", "unit tests")
        subprocess.run(["git", "add", "-A"], cwd=self.root, check=True)
        subprocess.run(
            ["git", "commit", "--no-verify", "-q", "-m", "test large merged state"],
            cwd=self.root, check=True,
        )
        merge_oid = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=self.root, text=True,
        ).strip()
        self.agentctl(
            "work", "--agent", "supervisor", "--auto-create", "--new-id", "T-302",
            "--title", "reconcile large merge", "--scope", ".agent/",
        )
        files = [f"src/generated-{index:03d}.txt" for index in range(100)]
        files.append(".agent/tasks/T-301.md")
        self.set_pr(files=files, oid=merge_oid)
        self.agentctl(
            "gate", "reconcile-github", "--task", "T-301", "--by", "project-owner",
            "--pr", "7",
        )
        board = json.loads(self.agentctl("board", "--json").stdout)
        self.assertEqual(board["tasks"]["T-301"]["status"], "done")

    def test_reconcile_supports_enterprise_host_and_legacy_completion(self):
        subprocess.run(
            ["git", "remote", "set-url", "origin", "git@git.example.test:team/project.git"],
            cwd=self.root, check=True,
        )
        self.agentctl(
            "work", "--agent", "codex", "--auto-create", "--new-id", "T-401",
            "--title", "legacy enterprise task", "--scope", "src/",
        )
        self.agentctl("finish", "--summary", "legacy worker complete", "--tests", "unit tests")
        task_doc = self.root / ".agent" / "tasks" / "T-401.md"
        body = task_doc.read_text(encoding="utf-8")
        task_doc.write_text(
            re.sub(r"^- Completed-at-ns:.*\n", "", body, flags=re.M), encoding="utf-8",
        )
        subprocess.run(["git", "add", "-A"], cwd=self.root, check=True)
        subprocess.run(
            ["git", "commit", "--no-verify", "-q", "-m", "test legacy enterprise state"],
            cwd=self.root, check=True,
        )
        merge_oid = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=self.root, text=True,
        ).strip()
        self.agentctl(
            "work", "--agent", "supervisor", "--auto-create", "--new-id", "T-402",
            "--title", "reconcile legacy enterprise merge", "--scope", ".agent/",
        )
        self.set_pr(
            files=[".agent/tasks/T-401.md"], oid=merge_oid,
            url="https://git.example.test/team/project/pull/7",
            expected_repo="git.example.test/team/project",
        )
        self.agentctl(
            "gate", "reconcile-github", "--task", "T-401", "--by", "project-owner",
            "--pr", "7", "--repo", "team/project",
        )
        board = json.loads(self.agentctl("board", "--json").stdout)
        self.assertEqual(board["tasks"]["T-401"]["status"], "done")


if __name__ == "__main__":
    unittest.main()
