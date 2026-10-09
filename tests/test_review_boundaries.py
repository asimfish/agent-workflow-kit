"""Executable regressions for the independent isolation review findings."""

import json
import subprocess
from unittest import mock

from tools import agentctl
from tests.test_milestones import _MilestoneTestCase


class ReviewBoundaryTest(_MilestoneTestCase):
    def start(self, name="a", task="T-101"):
        self.agentctl("work", "--agent", "codex", "--auto-create", "--type", "docs",
                      "--new-id", task, "--title", task, "--scope", f"docs/{name}/",
                      "--done", "complete", session=name)

    def revoke(self):
        board = self.board()
        board["tasks"]["T-101"]["claim_id"] = "replacement-claim"
        (self.root / ".agent/board.json").write_text(json.dumps(board) + "\n")

    def test_releasing_revoked_session_does_not_restore_loop_authority(self):
        self.start()
        self.revoke()
        self.agentctl("loop", "run", "doc-hygiene", "--once", session="a", expect=1)
        self.agentctl("sessions", "release", "--reason", "revoked holder stopped", session="a")
        reports = set((self.root / ".agent/loops/runs").glob("*.md"))
        self.agentctl("loop", "run", "doc-hygiene", "--once", session="a", expect=1)
        self.agentctl("loop", "auto", "--checkpoint", "pre-finish", "--once", session="a", expect=1)
        self.assertEqual(set((self.root / ".agent/loops/runs").glob("*.md")), reports)

    def test_revoked_holder_can_release_own_resource_but_peer_cannot(self):
        self.start()
        self.start("b", "T-102")
        self.agentctl("resource", "acquire", "gpu:0", session="a")
        resources = json.loads(self.agentctl("resource", "status", "--json").stdout)["resources"]
        lease = next(row["id"] for row in resources if row["task"] == "T-101")
        self.agentctl("resource", "release", lease, "--reason", "not my lease", session="b", expect=1)
        self.revoke()
        self.agentctl("resource", "acquire", "gpu:1", session="a", expect=1)
        self.agentctl("resource", "release", lease, "--reason", "revoked holder cleanup", session="a")
        resources = json.loads(self.agentctl("resource", "status", "--json").stdout)["resources"]
        self.assertEqual(next(row["status"] for row in resources if row["id"] == lease), "released")

    def test_loop_history_never_resolves_a_peer_failure(self):
        for task, status in (("T-A", "failed"), ("T-B", "success")):
            with mock.patch.object(agentctl, "_load_session", return_value={"task": task, "agent": "codex"}):
                result = agentctl._attach_previous_run(self.root, "doc-hygiene", {"status": status})
                self.assertNotIn("previous", result)
                self.assertFalse(any("resolved" in message for message in result.get("feedback", [])))
                agentctl._write_loop_report(self.root, "doc-hygiene", "regression", result)
        with mock.patch.object(agentctl, "_load_session", return_value={"task": "T-A", "agent": "codex"}):
            result = agentctl._attach_previous_run(self.root, "doc-hygiene", {"status": "success"})
        self.assertEqual(result["previous"]["status"], "failed")
        self.assertTrue(any("resolved" in message for message in result["feedback"]))
        state = json.loads((self.root / ".agent/loops/state.json").read_text())
        self.assertEqual({row["task"] for row in state["loops"].values()}, {"T-A", "T-B"})

    def test_fresh_session_cannot_overwrite_an_unmerged_board(self):
        path = self.root / ".agent/board.json"
        before = path.read_bytes()
        sha = self.git("hash-object", "-w", str(path)).stdout.strip()
        stages = "".join(f"100644 {sha} {stage}\t.agent/board.json\n" for stage in (1, 2, 3))
        result = subprocess.run(["git", "update-index", "--index-info"], input=stages, cwd=self.root,
                                text=True, capture_output=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.agentctl("task", "create", "--id", "T-201", "--title", "fresh task", "--scope", "docs/",
                      session="fresh", expect=1)
        self.agentctl("work", "--agent", "codex", "--auto-create", "--title", "fresh task",
                      "--scope", "docs/", session="fresh", expect=1)
        self.assertEqual(path.read_bytes(), before)
        self.assertNotIn("T-201", self.board()["tasks"])

    def test_declared_marker_file_is_checked_without_scanning_siblings(self):
        directory = self.root / ".agent-artifacts/T-A"
        directory.mkdir(parents=True)
        error = directory / "ERROR"
        error.touch()
        peer = directory / "peer"
        peer.mkdir()
        (peer / "ERROR").touch()
        leases = [{"kind": "run", "task": "T-A", "outputs": [str(error)]}]
        with mock.patch.object(agentctl, "_load_session", return_value={"task": "T-A", "scope": ["src/a/"]}), \
                mock.patch.object(agentctl, "_load_runtime_leases", return_value={"leases": leases}):
            result = agentctl._loop_experiment_monitor(self.root)
        self.assertEqual(result["status"], "partial")
        self.assertIn("ERROR markers: 1", result["feedback"])
        self.assertIn(".agent-artifacts/T-A/ERROR", result["read"])
