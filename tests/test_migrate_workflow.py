"""Regression coverage for upgrading already-running workflow sessions."""

import hashlib
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


class MigrationWorkflowRegressionTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="awk-migrate-regress-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True, timeout=60)
        installed = subprocess.run(
            [sys.executable, str(KIT / "tools" / "agentctl.py"), "init", str(self.root)],
            cwd=KIT,
            env=self.clean_env(),
            text=True,
            capture_output=True,
            timeout=120,
        )
        self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)

    def clean_env(self):
        env = os.environ.copy()
        for name in IDENTITY_ENV:
            env.pop(name, None)
        return env

    def env(self, session):
        env = self.clean_env()
        env["AGENT_WORKFLOW_SESSION_ID"] = session
        return env

    def agentctl_env(self, env, *args, expect=0):
        proc = subprocess.run(
            [sys.executable, "tools/agentctl.py", *args],
            cwd=self.root,
            env=env,
            text=True,
            capture_output=True,
            timeout=120,
        )
        self.assertEqual(proc.returncode, expect, proc.stdout + proc.stderr)
        return proc

    def start(self, env, task, scope):
        return self.agentctl_env(
            env,
            "work",
            "--agent",
            "codex",
            "--auto-create",
            "--new-id",
            task,
            "--title",
            f"migration fixture {task}",
            "--scope",
            scope,
        )

    def migrate(self, env, expect=0):
        proc = self.agentctl_env(env, "migrate", "--json", expect=expect)
        return json.loads(proc.stdout)

    def session_records(self):
        common = subprocess.run(
            ["git", "rev-parse", "--git-common-dir"],
            cwd=self.root,
            text=True,
            capture_output=True,
            check=True,
            timeout=60,
        ).stdout.strip()
        common_path = Path(common)
        if not common_path.is_absolute():
            common_path = self.root / common_path
        return sorted((common_path.resolve() / "agent-workflow" / "sessions").glob("*.json"))

    def test_healthy_conversation_can_continue_without_state_writes(self):
        env = self.env("healthy-conversation")
        self.start(env, "T-301", "src/healthy/")
        records = self.session_records()
        self.assertEqual(len(records), 1)
        before = records[0].read_bytes()

        report = self.migrate(env)

        self.assertTrue(report["ok"])
        self.assertEqual(report["action"], "continue")
        self.assertTrue(report["installation"]["ok"])
        self.assertEqual(report["current_session"]["source"], "conversation")
        self.assertEqual(report["current_session"]["task"], "T-301")
        self.assertEqual(records[0].read_bytes(), before)

    def test_changed_project_documents_require_a_real_refresh(self):
        env = self.env("refresh-conversation")
        self.start(env, "T-302", "src/refresh/")
        plan = self.root / ".agent" / "PROJECT_PLAN.md"
        plan.write_text(plan.read_text(encoding="utf-8") + "\nHuman direction changed.\n", encoding="utf-8")

        report = self.migrate(env, expect=1)

        self.assertFalse(report["ok"])
        self.assertEqual(report["action"], "refresh")
        self.assertIn(".agent/PROJECT_PLAN.md", report["current_session"]["changed_documents"])
        self.agentctl_env(env, "refresh")
        refreshed = self.migrate(env)
        self.assertEqual(refreshed["action"], "continue")

    def test_incomplete_fork_requires_session_restart_and_keeps_parent(self):
        parent_env = self.env("parent-workflow-session")
        parent_env.update({
            "CODEX_THREAD_ID": "parent-thread",
            "WHALENT_AGENT_ID": "parent-agent",
        })
        self.start(parent_env, "T-303", "src/parent/")
        record = self.session_records()[0]
        before = record.read_bytes()

        child_env = parent_env.copy()
        child_env.update({
            "CODEX_THREAD_ID": "child-thread",
            "WHALENT_AGENT_ID": "child-agent",
            "WHALENT_FORK_SOURCE_AGENT_ID": "parent-agent",
        })
        report = self.migrate(child_env, expect=1)

        self.assertEqual(report["action"], "restart")
        self.assertEqual(report["current_session"]["source"], "untrusted_fork")
        self.assertTrue(any("SessionStart" in reason for reason in report["reasons"]))
        self.assertEqual(record.read_bytes(), before)

    def test_missing_conversation_identity_requires_restart(self):
        report = self.migrate(self.clean_env(), expect=1)

        self.assertEqual(report["action"], "restart")
        self.assertEqual(report["current_session"]["key"], "default")
        self.assertEqual(report["current_session"]["source"], "untrusted_identity")

    def test_identifiable_stale_peer_is_advisory_and_never_auto_released(self):
        stale_env = self.env("stale-conversation")
        self.start(stale_env, "T-304", "src/stale/")
        record = self.session_records()[0]
        payload = json.loads(record.read_text(encoding="utf-8"))
        payload["heartbeat_ns"] = 1
        record.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        before = record.read_bytes()

        report = self.migrate(self.env("replacement-conversation"))

        self.assertTrue(report["ok"])
        self.assertEqual(report["action"], "continue")
        self.assertEqual([row["task"] for row in report["sessions"]["stale"]], ["T-304"])
        self.assertTrue(any("T-304" in warning for warning in report["warnings"]))
        self.assertTrue(any("sessions list" in step for step in report["next_steps"]))
        self.assertEqual(record.read_bytes(), before)
        self.assertNotEqual(json.loads(record.read_text(encoding="utf-8"))["presence_status"], "released")

        self.start(
            self.env("replacement-conversation"),
            "T-309",
            "src/replacement/",
        )

    def test_managed_install_drift_or_legacy_manifest_requires_repair(self):
        managed = self.root / "tools" / "agent_workflow_hook.py"
        original = managed.read_text(encoding="utf-8")
        managed.write_text(original + "\n# incompatible local drift\n", encoding="utf-8")

        drift = self.migrate(self.env("repair-conversation"), expect=1)

        self.assertEqual(drift["action"], "repair_install")
        self.assertFalse(drift["installation"]["ok"])
        self.assertTrue(any("managed installation files changed" in item for item in drift["reasons"]))

        managed.write_text(original, encoding="utf-8")
        (self.root / ".agent" / "install-manifest.json").unlink()
        legacy = self.migrate(self.env("legacy-install-conversation"), expect=1)
        self.assertEqual(legacy["action"], "repair_install")
        self.assertEqual(legacy["installation"]["manifest"], "legacy_missing")

    def test_matching_legacy_singleton_is_reported_without_being_moved(self):
        env = self.clean_env()
        env["CODEX_THREAD_ID"] = "legacy-codex-conversation"
        runtime_digest = hashlib.sha256(
            b"CODEX_THREAD_ID=legacy-codex-conversation"
        ).hexdigest()
        runtime_identity = f"host-runtime:{runtime_digest[:32]}"
        state_dir = self.root / ".agent" / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        legacy = state_dir / "current_session.json"
        legacy.write_text(json.dumps({
            "task": "T-305",
            "agent": "codex",
            "scope": ["src/legacy/"],
            "runtime_identities": [runtime_identity],
            "notes": [],
            "doc_hashes": {},
        }, indent=2) + "\n", encoding="utf-8")
        before = legacy.read_bytes()

        report = self.migrate(env, expect=1)

        self.assertEqual(report["action"], "refresh")
        self.assertEqual(report["current_session"]["source"], "singleton_legacy")
        self.assertEqual(legacy.read_bytes(), before)
        self.agentctl_env(env, "status", "--json")
        self.assertFalse(legacy.exists())

    def test_released_record_returns_to_work_selection_without_mutation(self):
        env = self.env("released-conversation")
        self.start(env, "T-306", "src/released/")
        record = self.session_records()[0]
        payload = json.loads(record.read_text(encoding="utf-8"))
        payload["presence_status"] = "released"
        record.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        before = record.read_bytes()

        report = self.migrate(env)

        self.assertEqual(report["action"], "continue")
        self.assertEqual(report["current_session"]["status"], "released")
        self.assertTrue(any("agentctl work" in step for step in report["next_steps"]))
        self.assertEqual(record.read_bytes(), before)

    def test_current_pre_identity_record_requires_refresh_before_continue(self):
        env = self.env("legacy-current-conversation")
        self.start(env, "T-307", "src/legacy-current/")
        record = self.session_records()[0]
        payload = json.loads(record.read_text(encoding="utf-8"))
        payload.pop("identity_source", None)
        record.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        before = record.read_bytes()

        report = self.migrate(env, expect=1)

        self.assertEqual(report["action"], "refresh")
        self.assertTrue(any("identity metadata" in reason for reason in report["reasons"]))
        self.assertEqual(record.read_bytes(), before)
        self.agentctl_env(env, "refresh")
        self.assertEqual(self.migrate(env)["action"], "continue")
        self.assertEqual(
            json.loads(record.read_text(encoding="utf-8"))["identity_source"],
            "session_start",
        )

    def test_active_unknown_peer_requires_inspection_before_handoff(self):
        old_env = self.env("old-conversation")
        self.start(old_env, "T-308", "src/old/")
        record = self.session_records()[0]
        payload = json.loads(record.read_text(encoding="utf-8"))
        payload.pop("identity_source", None)
        record.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        before = record.read_bytes()

        report = self.migrate(self.env("replacement-conversation"), expect=1)

        self.assertEqual(report["action"], "inspect_sessions")
        self.assertEqual(report["sessions"]["active"][0]["task"], "T-308")
        self.assertEqual(record.read_bytes(), before)

    def test_upgrade_refreshes_managed_entry_without_replacing_project_plan(self):
        plan = self.root / ".agent" / "PROJECT_PLAN.md"
        plan.write_text(plan.read_text(encoding="utf-8") + "\nProject-owned history.\n", encoding="utf-8")
        agents = self.root / "AGENTS.md"
        old_entry = (
            "<!-- agent-workflow-kit:start -->\n"
            "Old managed workflow entry.\n"
            "<!-- agent-workflow-kit:end -->"
        )
        agents.write_text(re.sub(
            r"<!-- agent-workflow-kit:start -->.*?<!-- agent-workflow-kit:end -->",
            old_entry,
            agents.read_text(encoding="utf-8"),
            flags=re.S,
        ), encoding="utf-8")

        upgraded = subprocess.run(
            [sys.executable, str(KIT / "tools" / "agentctl.py"), "init", str(self.root)],
            cwd=KIT,
            env=self.clean_env(),
            text=True,
            capture_output=True,
            timeout=120,
        )

        self.assertEqual(upgraded.returncode, 0, upgraded.stdout + upgraded.stderr)
        self.assertIn("agentctl.py migrate", agents.read_text(encoding="utf-8"))
        self.assertIn("Project-owned history.", plan.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
