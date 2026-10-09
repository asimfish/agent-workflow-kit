"""A worker's feedback, checks, and memory belong to its task, not its peers."""

import json
from unittest import mock

from tools import agentctl
from tests.test_milestones import _MilestoneTestCase


class TaskLoopIsolationTest(_MilestoneTestCase):
    def start(self, name):
        self.agentctl(
            "work", "--agent", "codex", "--auto-create", "--type", "docs",
            "--new-id", f"T-{name.upper()}", "--title", f"docs {name}",
            "--scope", f"docs/{name}/", "--done", "documentation complete",
            session=name,
        )

    def test_follow_up_failure_and_success_cannot_touch_another_task(self):
        with mock.patch.object(agentctl, "_load_session", return_value={"task": "T-A", "agent": "codex"}):
            a, _, _ = agentctl._create_loop_follow_up(self.root, "experiment-check", "failed", ["a-error"], True, 2)
        with mock.patch.object(agentctl, "_load_session", return_value={"task": "T-B", "agent": "codex"}):
            b, created, escalated = agentctl._create_loop_follow_up(self.root, "experiment-check", "failed", ["b-error"], True, 2)
            self.assertNotEqual(a, b)
            self.assertTrue(created)
            self.assertFalse(escalated)
            self.assertEqual(agentctl._close_loop_follow_ups(self.root, "experiment-check", "B fixed"), [b])
        packets = agentctl._loop_follow_up_packets(self.root)
        self.assertEqual(len(packets), 1)
        packet = packets[0][1]
        self.assertEqual((packet["id"], packet["to_task"], packet["occurrences"], packet["artifacts"]),
                         (a, "T-A", 1, ["a-error"]))

    def test_unrelated_malformed_document_does_not_block_finish(self):
        self.start("a")
        self.start("b")
        (self.root / ".agent/tasks/T-B.md").write_text("# T-B\nStatus: in_progress\n")
        self.agentctl("finish", "--summary", "A complete", "--tests", "isolated check", session="a")
        self.assertEqual(self.status("T-A"), "review")
        self.agentctl("refresh", session="b")
        result = self.agentctl("finish", "--summary", "B complete", "--tests", "isolated check",
                               "--done", "documentation complete", session="b", expect=1)
        self.assertIn("doc-hygiene", result.stdout + result.stderr)
        self.assertEqual(self.status("T-B"), "in_progress")

    def test_escalation_for_a_does_not_block_b_manual_check(self):
        self.start("a")
        self.start("b")
        with mock.patch.object(agentctl, "_load_session", return_value={"task": "T-A", "agent": "codex"}):
            agentctl._create_loop_follow_up(self.root, "experiment-check", "failed", ["a-error"], True, 1)
        self.agentctl("check", "--mode", "manual", session="a", expect=1)
        self.agentctl("check", "--mode", "manual", session="b")
        # A project-wide CI audit still reports unresolved escalations.
        self.agentctl("check", "--mode", "ci", expect=1)

    def test_checkpoint_debounce_and_doc_change_are_task_local(self):
        self.start("a")
        self.start("b")
        path = self.root / ".agent/loops/checkpoints.json"
        policy = json.loads(path.read_text())
        policy["checkpoints"]["local-docs"] = {"loops": ["doc-hygiene"], "strict": True,
                                               "debounce_minutes": 30}
        path.write_text(json.dumps(policy) + "\n")
        for session in ("a", "b"):
            self.agentctl("refresh", session=session)
            first = self.agentctl("loop", "auto", "--checkpoint", "local-docs", "--once", session=session)
            self.assertNotIn("skipped", first.stdout)
            second = self.agentctl("loop", "auto", "--checkpoint", "local-docs", "--once", session=session)
            self.assertIn("skipped", second.stdout)
        doc = self.root / ".agent/tasks/T-A.md"
        doc.write_text(doc.read_text() + "\nExtra task evidence.\n")
        self.agentctl("refresh", session="a")
        again = self.agentctl("loop", "auto", "--checkpoint", "local-docs", "--once", session="a")
        self.assertNotIn("skipped", again.stdout)
        peer = self.agentctl("loop", "auto", "--checkpoint", "local-docs", "--once", session="b")
        self.assertIn("skipped", peer.stdout)

    def test_experiment_monitor_does_not_scan_peer_results(self):
        for name, marker in (("a", "DONE"), ("b", "ERROR")):
            output = self.root / "results" / name
            output.mkdir(parents=True)
            (output / marker).touch()
        with mock.patch.object(agentctl, "_load_session", return_value={"task": "T-A", "scope": ["results/a/"]}):
            result = agentctl._loop_experiment_monitor(self.root)
        self.assertEqual(result["status"], "success", result)
        self.assertIn("ERROR markers: 0", result["feedback"])

    def test_experiment_monitor_checks_only_own_declared_run_outputs(self):
        leases = []
        for task, marker in (("T-A", "DONE"), ("T-B", "ERROR")):
            output = self.root / ".agent-artifacts" / task
            output.mkdir(parents=True)
            (output / marker).touch()
            leases.append({"task": task, "kind": "run", "outputs": [str(output)]})
        with mock.patch.object(agentctl, "_load_session", return_value={"task": "T-A", "scope": ["src/a/"]}), \
                mock.patch.object(agentctl, "_load_runtime_leases", return_value={"leases": leases}):
            result = agentctl._loop_experiment_monitor(self.root)
        self.assertEqual(result["status"], "success", result)
        self.assertIn("DONE markers: 1", result["feedback"])
        self.assertIn("ERROR markers: 0", result["feedback"])

    def test_experiment_monitor_does_not_rescan_nested_owned_outputs(self):
        output = self.root / "results/a"
        output.mkdir(parents=True)
        marker = output / "DONE"
        marker.touch()
        leases = [{"task": "T-A", "kind": "run", "outputs": [str(output), str(marker)]}]
        with mock.patch.object(agentctl, "_load_session", return_value={
                "task": "T-A", "scope": ["results/a/", "results/a/nested/"]}), \
                mock.patch.object(agentctl, "_load_runtime_leases", return_value={"leases": leases}):
            result = agentctl._loop_experiment_monitor(self.root)
        self.assertIn("DONE markers: 1", result["feedback"])

    def test_experiment_result_changes_bypass_debounce_and_close_own_failure(self):
        self.agentctl("work", "--agent", "codex", "--auto-create", "--type", "docs",
                      "--new-id", "T-A", "--title", "monitor A", "--scope", "results/a/",
                      "--done", "monitor complete", session="a")
        path = self.root / ".agent/loops/checkpoints.json"
        policy = json.loads(path.read_text())
        policy["checkpoints"]["experiment-check"]["strict"] = True
        path.write_text(json.dumps(policy) + "\n")
        self.agentctl("refresh", session="a")
        output = self.root / "results/a"
        output.mkdir(parents=True)
        (output / "DONE").touch()
        args = ("loop", "auto", "--checkpoint", "experiment-check", "--once")
        self.agentctl(*args, session="a")
        (output / "ERROR").touch()
        failure = self.agentctl(*args, session="a", expect=1)
        self.assertNotIn("skipped", failure.stdout)
        self.assertEqual(len(agentctl._loop_follow_up_packets(self.root)), 1)
        (output / "ERROR").unlink()
        recovery = self.agentctl(*args, session="a")
        self.assertIn("auto-closed", recovery.stdout)
        self.assertEqual(agentctl._loop_follow_up_packets(self.root), [])


class CICommitRangeTest(_MilestoneTestCase):
    def commit_object(self, subject, parent=None):
        tree = self.git("mktree").stdout.strip()
        args = ["commit-tree", tree, "-m", subject]
        if parent:
            args += ["-p", parent]
        return self.git(*args).stdout.strip()

    def test_ci_rejects_bad_commit_and_invalid_range(self):
        base = self.commit_object("fixture baseline")
        bad = self.commit_object("message without convention or task", base)
        result = self.agentctl("check", "--mode", "ci", "--commit-range", f"{base}..{bad}", expect=1)
        self.assertIn("commit not Conventional", result.stdout)
        self.assertIn("commit missing task ID", result.stdout)
        invalid = self.agentctl("check", "--mode", "ci", "--commit-range", "missing..HEAD", expect=1)
        self.assertIn("commit range", invalid.stdout.lower())
        self.agentctl("check", "--mode", "ci", "--commit-range=--max-count=0", expect=1)

    def test_ci_accepts_valid_commits_without_local_session_or_exclusivity(self):
        self.agentctl("work", "--agent", "codex", "--auto-create", "--new-id", "T-101",
                      "--title", "valid CI", "--scope", "src/a/", "--done", "complete", session="a")
        self.agentctl("work", "--agent", "codex", "--auto-create", "--new-id", "T-102",
                      "--title", "unrelated", "--scope", "src/b/", "--done", "complete", session="b")
        self.agentctl("finish", "--summary", "A complete", "--tests", "fixture", session="a")
        base = self.commit_object("fixture baseline")
        valid = self.commit_object("fix(test): verified task\n\nRefs: T-101", base)
        self.agentctl("check", "--mode", "ci", "--commit-range", f"{base}..{valid}")
