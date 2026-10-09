"""Legacy claim migration must be explicit and must not restore a revoked holder."""

import json
import subprocess
import sys
from unittest import mock

from tools import agentctl
from tests.test_milestones import KIT, _MilestoneTestCase


class ClaimUpgradeTest(_MilestoneTestCase):
    def start(self):
        self.agentctl("work", "--agent", "codex", "--auto-create", "--type", "docs",
                      "--new-id", "T-101", "--title", "legacy task", "--scope", "docs/a/",
                      "--done", "task complete", session="a")

    def legacy_claim(self):
        board = self.board()
        board["tasks"]["T-101"].pop("claim_id")
        (self.root / ".agent/board.json").write_text(json.dumps(board) + "\n")
        with mock.patch.dict("os.environ", self.env("a"), clear=True):
            path = agentctl._session_path(self.root)
        session = json.loads(path.read_text())
        session.pop("claim_id")
        path.write_text(json.dumps(session) + "\n")
        return path, session

    def test_legacy_refresh_cannot_grant_authority_but_explicit_start_binds_claim(self):
        self.start()
        self.legacy_claim()
        report = json.loads(self.agentctl("migrate", "--json", session="a", expect=1).stdout)
        self.assertEqual(report["action"], "inspect_sessions")
        self.assertTrue(any("work --agent codex --task T-101" in step for step in report["next_steps"]))
        self.agentctl("refresh", session="a", expect=1)
        self.agentctl("work", "--agent", "codex", "--task", "T-101", session="a")
        row = json.loads(self.agentctl("status", "--json", session="a").stdout)
        self.assertEqual(row["claim_id"], self.board()["tasks"]["T-101"]["claim_id"])
        self.agentctl("note", "explicit legacy binding complete", session="a")

    def test_revocation_cannot_be_rebound_and_migrate_does_not_recommend_refresh(self):
        self.start()
        board = self.board()
        board["tasks"]["T-101"]["claim_id"] = "foreign-claim"
        (self.root / ".agent/board.json").write_text(json.dumps(board) + "\n")
        self.agentctl("upgrade", "rebind", session="a", expect=1)
        report = json.loads(self.agentctl("migrate", "--json", session="a", expect=1).stdout)
        self.assertEqual(report["action"], "inspect_sessions")
        self.assertIn("claim authority changed", " ".join(report["reasons"]))

    def test_protocol_two_unbound_session_drains_without_losing_task_history(self):
        self.start()
        path, session = self.legacy_claim()
        session["protocol_epoch"] = 2
        path.write_text(json.dumps(session) + "\n")
        manifest_path = self.root / ".agent/install-manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["protocol_epoch"] = 2
        manifest_path.write_text(json.dumps(manifest) + "\n")
        def install(expected):
            result = subprocess.run([sys.executable, str(KIT / "tools/agentctl.py"), "init", str(self.root)],
                                    cwd=KIT, text=True, capture_output=True, timeout=120)
            self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        install(1)
        self.agentctl("work", "--agent", "codex", "--task", "T-101", session="a", expect=1)
        self.agentctl("sessions", "release", "--reason", "verified old worker stopped", session="a")
        install(0)
        self.assertEqual(self.board()["tasks"]["T-101"]["title"], "legacy task")
        self.assertEqual(json.loads(manifest_path.read_text())["protocol_epoch"], 3)
        self.agentctl("upgrade", "rebind", session="a")
        self.agentctl("work", "--agent", "codex", "--task", "T-101", session="a")
        self.agentctl("note", "upgraded with preserved history", session="a")
