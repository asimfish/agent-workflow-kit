"""Fresh-install regression coverage for protocol upgrade barriers."""

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
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


class UpgradeBarrierWorkflowRegressionTest(unittest.TestCase):
    def setUp(self):
        self.temp = Path(tempfile.mkdtemp(prefix="awk-upgrade-regress-"))
        self.addCleanup(shutil.rmtree, self.temp, ignore_errors=True)
        self.root = self.temp / "project"
        self.root.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True, timeout=60)
        subprocess.run(
            ["git", "config", "user.email", "agent@example.com"],
            cwd=self.root, check=True, timeout=60,
        )
        subprocess.run(
            ["git", "config", "user.name", "Agent Test"],
            cwd=self.root, check=True, timeout=60,
        )
        self.source_init()
        subprocess.run(
            ["git", "add", "-A"], cwd=self.root, check=True, timeout=60,
        )
        subprocess.run(
            ["git", "commit", "--no-verify", "-q", "-m",
             "chore(agent): install workflow\n\nRefs: T-000"],
            cwd=self.root, check=True, timeout=60,
        )

    def env(self, session):
        env = os.environ.copy()
        for name in IDENTITY_ENV:
            env.pop(name, None)
        env["AGENT_WORKFLOW_SESSION_ID"] = session
        return env

    def source_init(self, expect=0):
        proc = subprocess.run(
            [sys.executable, str(KIT / "tools" / "agentctl.py"),
             "init", str(self.root)],
            cwd=KIT, text=True, capture_output=True, timeout=120,
        )
        self.assertEqual(proc.returncode, expect, proc.stdout + proc.stderr)
        return proc

    def agentctl(self, *args, session="admin", expect=0):
        proc = subprocess.run(
            [sys.executable, "tools/agentctl.py", *args],
            cwd=self.root, env=self.env(session), text=True,
            capture_output=True, timeout=120,
        )
        self.assertEqual(
            proc.returncode, expect,
            f"agentctl {' '.join(args)} rc={proc.returncode}\n"
            f"{proc.stdout}\n{proc.stderr}",
        )
        return proc

    def session_record(self, session):
        status = json.loads(
            self.agentctl("status", "--json", session=session).stdout
        )
        key = status["workflow_session_key"]
        common = subprocess.run(
            ["git", "rev-parse", "--git-common-dir"], cwd=self.root,
            check=True, text=True, capture_output=True, timeout=60,
        ).stdout.strip()
        common_path = Path(common)
        if not common_path.is_absolute():
            common_path = self.root / common_path
        matches = list(
            (common_path.resolve() / "agent-workflow" / "sessions").glob(
                f"{key}-*.json"
            )
        )
        self.assertEqual(len(matches), 1)
        return matches[0]

    def make_legacy_manifest(self):
        path = self.root / ".agent" / "install-manifest.json"
        manifest = json.loads(path.read_text(encoding="utf-8"))
        manifest["version"] = 1
        manifest.pop("kit_version", None)
        manifest.pop("source_commit", None)
        manifest.pop("updated_at", None)
        manifest["protocol_epoch"] = 1
        tool = self.root / "tools" / "agentctl.py"
        manifest["managed_files"]["tools/agentctl.py"] = hashlib.sha256(
            tool.read_bytes()
        ).hexdigest()
        path.write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    def install_legacy_entrypoint(self):
        legacy = subprocess.run(
            ["git", "show", "origin/main:tools/agentctl.py"],
            cwd=KIT, check=True, capture_output=True, timeout=60,
        ).stdout
        self.assertNotEqual(legacy, (KIT / "tools" / "agentctl.py").read_bytes())
        (self.root / "tools" / "agentctl.py").write_bytes(legacy)

    def test_manifest_records_version_schema_source_and_protocol(self):
        manifest = json.loads(
            (self.root / ".agent" / "install-manifest.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(manifest["version"], 2)
        self.assertEqual(manifest["kit_version"], "0.5.0")
        self.assertEqual(manifest["protocol_epoch"], 2)
        self.assertTrue(manifest["source_commit"])
        validation = json.loads(
            self.agentctl("upgrade", "validate", "--json").stdout
        )
        self.assertTrue(validation["ok"], validation)

    def test_upgrade_drains_sessions_before_install_and_requires_rebind(self):
        self.agentctl(
            "work", "--agent", "writer", "--auto-create",
            "--new-id", "T-UP", "--title", "upgrade fixture",
            "--scope", "docs/", "--type", "docs", session="old",
        )
        record_path = self.session_record("old")
        record = json.loads(record_path.read_text(encoding="utf-8"))
        record["protocol_epoch"] = 1
        record_path.write_text(
            json.dumps(record, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        self.install_legacy_entrypoint()
        self.make_legacy_manifest()
        tool_before = (self.root / "tools" / "agentctl.py").read_bytes()

        blocked = self.source_init(expect=1)
        self.assertIn("upgrade barrier entered draining state", blocked.stderr)
        self.assertNotEqual(
            (self.root / "tools" / "agentctl.py").read_bytes(), tool_before
        )
        self.assertEqual(
            (self.root / "tools" / "agentctl.py").read_bytes(),
            (KIT / "tools" / "agentctl.py").read_bytes(),
        )
        refused_new_work = self.agentctl(
            "work", "--agent", "intruder", "--auto-create",
            "--new-id", "T-INTRUDER", "--title", "must wait for upgrade",
            "--scope", "tmp/intruder/", "--type", "docs",
            session="intruder", expect=1,
        )
        self.assertIn("workflow upgrade is draining", refused_new_work.stderr)
        guarded = self.agentctl(
            "sessions", "guard", "--path", "docs/change.md",
            session="old", expect=1,
        )
        self.assertIn("upgrade is draining", guarded.stderr)
        self.agentctl("note", "safe drain note", session="old")
        self.agentctl(
            "sessions", "release", "--reason", "upgrade drain", session="old"
        )

        self.source_init()
        status = json.loads(
            self.agentctl("upgrade", "status", "--json").stdout
        )
        self.assertEqual(status["state"], "steady")
        self.assertEqual(status["installed_epoch"], 2)

        mismatch = self.agentctl(
            "note", "must not write before rebind", session="old", expect=1,
        )
        self.assertIn("upgrade rebind", mismatch.stderr)
        self.agentctl("upgrade", "rebind", session="old")
        self.agentctl(
            "work", "--agent", "writer", "--task", "T-UP", session="old"
        )
        self.agentctl("note", "writes restored after rebind", session="old")

    def test_no_blocker_upgrade_bootstraps_barrier_before_validation_work(self):
        self.install_legacy_entrypoint()
        self.make_legacy_manifest()
        hook = self.root / ".githooks" / "pre-commit"
        hook.write_text(
            hook.read_text(encoding="utf-8") + "\n# project conflict\n",
            encoding="utf-8",
        )

        conflicted = self.source_init(expect=1)
        self.assertIn("installation aborted before writing", conflicted.stderr)
        self.assertEqual(
            (self.root / "tools" / "agentctl.py").read_bytes(),
            (KIT / "tools" / "agentctl.py").read_bytes(),
        )
        status = json.loads(
            self.agentctl("upgrade", "status", "--json").stdout
        )
        self.assertEqual(status["state"], "validating")
        self.assertEqual(
            set(status["barrier_entrypoints"]),
            {"tools/agentctl.py", "tools/agent_workflow_hook.py"},
        )

        refused = self.agentctl(
            "work", "--agent", "late-writer", "--auto-create",
            "--new-id", "T-LATE", "--title", "must wait for validation",
            "--scope", "docs/late/", "--type", "docs",
            session="late", expect=1,
        )
        self.assertIn("workflow upgrade is validating", refused.stderr)
        board = json.loads(
            (self.root / ".agent" / "board.json").read_text(encoding="utf-8")
        )
        self.assertNotIn("T-LATE", board["tasks"])

    def test_managed_run_can_finish_while_next_upgrade_is_draining(self):
        self.agentctl(
            "work", "--agent", "runner", "--auto-create",
            "--new-id", "T-RUN", "--title", "daemon fixture",
            "--scope", "jobs/", "--type", "docs", session="runner",
        )
        started = self.agentctl(
            "run", "start", "--output", ".agent-artifacts/T-RUN/result.txt",
            "--", sys.executable, "-c",
            "import time; time.sleep(0.5)",
            session="runner",
        )
        match = re.search(r"run lease (run-[0-9a-f]+) started", started.stdout)
        self.assertIsNotNone(match, started.stdout)
        lease = match.group(1)
        self.agentctl(
            "sessions", "release", "--reason", "admin upgrade",
            session="runner",
        )
        self.agentctl(
            "upgrade", "begin", "--target-epoch", "3",
            "--target-version", "0.6.0", session="admin",
        )
        waiting = self.agentctl(
            "run", "wait", lease, "--timeout", "10", session="admin",
        )
        self.assertIn("succeeded", waiting.stdout)
        state = json.loads(
            self.agentctl("upgrade", "status", "--json").stdout
        )
        self.assertEqual(state["state"], "draining")
        self.assertEqual(state["blocking_sessions"], [])


if __name__ == "__main__":
    unittest.main()
