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


KIT = Path(__file__).resolve().parents[1]


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

    def agentctl(self, *args, expect=0):
        proc = subprocess.run(
            [sys.executable, "tools/agentctl.py", *args],
            cwd=self.root,
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

    def agentctl(self, *args, expect=0, runtime="worker-runtime"):
        env = os.environ.copy()
        env["CODEX_THREAD_ID"] = runtime
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
            runtime="worker-finish-runtime",
        )
        same_runtime = self.agentctl(
            "gate", "approve", "--task", "T-101", "--by", "supervisor",
            expect=1, runtime="worker-finish-runtime",
        )
        self.assertIn("participated in the worker task", same_runtime.stderr)
        self_review = self.agentctl(
            "gate", "approve", "--task", "T-102", "--by", "supervisor",
            expect=1, runtime="worker-finish-runtime",
        )
        self.assertIn("cannot own the task", self_review.stderr)

        self.agentctl("refresh", runtime="reviewer-runtime")
        self.agentctl(
            "gate", "approve", "--task", "T-101", "--by", "supervisor",
            runtime="reviewer-runtime",
        )
        board = json.loads(self.agentctl("board", "--json").stdout)
        self.assertEqual(board["tasks"]["T-101"]["status"], "done")
        gate = (self.root / ".agent" / "gates" / "T-101.md").read_text(encoding="utf-8")
        self.assertIn("Reviewer task: T-102", gate)
        self.assertIn("Reviewer runtime: host-runtime:", gate)
        self.agentctl("check", "--mode", "manual")


if __name__ == "__main__":
    unittest.main()
