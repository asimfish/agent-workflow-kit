"""Installed-project task restarts keep durable views and receipts consistent."""

import json
import re

from tests.test_task_contract import _ContractTestCase


class TaskLifecycleViewsTest(_ContractTestCase):
    def setUp(self):
        super().setUp()
        self.register_reviewer("reviewer", name="lifecycle-reviewer")
        self.agentctl(
            "work", "--agent", "lifecycle-reviewer", "--auto-create",
            "--title", "review lifecycle work", "--scope", ".agent/gates/",
            "--type", "review", "--done", "record an independent decision",
            session="reviewer",
        )

    def complete_task(self, *, session="worker", scope="src/worker/"):
        task = self.open_task(
            session, "reopenable work", scope, "--done", "deliver the fixture",
        )
        self.agentctl("finish", "--summary", "first delivery", session=session)
        self.agentctl(
            "gate", "approve", "--task", task, "--by", "lifecycle-reviewer",
            "--note", "fixture independently checked", session="reviewer",
        )
        self.assert_views(task, "done", "codex")
        return task

    def assert_views(self, task, status, owner):
        board = json.loads((self.root / ".agent/board.json").read_text(encoding="utf-8"))
        self.assertEqual(board["tasks"][task]["status"], status)
        self.assertEqual(board["tasks"][task]["owner"], owner)
        index = (self.root / ".agent/TASKS.md").read_text(encoding="utf-8")
        row = next(line for line in index.splitlines() if line.startswith(f"| {task} |"))
        self.assertTrue(row.startswith(f"| {task} | {status} | {owner} |"), row)
        doc = self.doc(task)
        self.assertIn(f"\nStatus: {status}\n", doc)
        self.assertIn(f"\nOwner: {owner}\n", doc)
        self.assertIn(f"\nAgent: {owner}\n", doc)
        plan = (self.root / ".agent/PROJECT_PLAN.md").read_text(encoding="utf-8")
        match = re.search(rf"^- \[([ x])\] {re.escape(task)} - (.*)$", plan, re.M)
        self.assertIsNotNone(match, plan)
        self.assertEqual(match.group(1), "x" if status == "done" else " ", match.group(0))
        self.assertTrue(match.group(2).endswith(f"(owner: {owner})"), match.group(0))

    def test_work_reopens_done_task_without_manual_view_repair(self):
        task = self.complete_task()
        previous_record = self.completion(task)
        self.agentctl("work", "--agent", "codex", "--task", task, session="worker")
        self.assert_views(task, "in_progress", "codex")
        self.assertEqual(self.completion(task), previous_record)
        self.agentctl("check", "--mode", "manual", session="worker")
        self.agentctl("note", "second phase started", session="worker")
        self.agentctl("finish", "--summary", "second delivery", session="worker")
        self.assert_views(task, "review", "codex")
        self.agentctl(
            "gate", "approve", "--task", task, "--by", "lifecycle-reviewer",
            "--note", "second delivery checked", session="reviewer",
        )
        self.assert_views(task, "done", "codex")
        self.agentctl("check", "--mode", "manual", session="reviewer")

    def test_start_reopens_done_task_for_a_new_owner(self):
        task = self.complete_task()
        self.agentctl("agents", "add", "--id", "cursor", session="reviewer")
        self.agentctl(
            "start", "--agent", "cursor", "--task", task, session="new-worker",
        )
        self.assert_views(task, "in_progress", "cursor")
        self.agentctl("check", "--mode", "manual", session="new-worker")
        self.agentctl("note", "new owner has its own claim", session="new-worker")
        self.agentctl("finish", "--summary", "new delivery", session="new-worker")
        self.assert_views(task, "review", "cursor")

    def test_review_task_can_return_to_work_without_refresh(self):
        task = self.open_task(
            "worker", "first review needs revision", "src/worker/",
            "--done", "deliver the fixture",
        )
        self.agentctl("finish", "--summary", "first delivery", session="worker")
        self.assert_views(task, "review", "codex")
        self.agentctl("work", "--agent", "codex", "--task", task, session="worker")
        self.assert_views(task, "in_progress", "codex")
        self.agentctl("note", "revision started", session="worker")
        self.agentctl("finish", "--summary", "revised delivery", session="worker")
        self.assert_views(task, "review", "codex")

    def test_reopening_preserves_human_plan_text_and_peer_receipt(self):
        peer = self.open_task("peer", "unrelated work", "src/peer/", "--done", "peer delivery")
        task = self.complete_task()
        plan = self.root / ".agent/PROJECT_PLAN.md"
        instruction = "Keep peer outputs intact. \u4fdd\u7559\u4eba\u7684\u65b9\u5411\u3002"
        plan.write_text(
            plan.read_text(encoding="utf-8").replace(
                "## Task Board\n", f"## Task Board\n\n{instruction}\n\n", 1,
            ),
            encoding="utf-8",
        )
        self.agentctl("refresh", session="peer")
        before = json.loads(self.agentctl("status", "--json", session="peer").stdout)
        peer_doc = self.doc(peer)
        self.agentctl("work", "--agent", "codex", "--task", task, session="worker")
        self.assert_views(task, "in_progress", "codex")
        self.assertIn(instruction, plan.read_text(encoding="utf-8"))
        self.assertEqual(self.doc(peer), peer_doc)
        after = json.loads(self.agentctl("status", "--json", session="peer").stdout)
        self.assertEqual(after["doc_hashes"], before["doc_hashes"])
        self.assertEqual(after["claim_id"], before["claim_id"])
        self.agentctl("note", "peer can continue without refreshing", session="peer")
        self.agentctl("note", "reopened worker can continue", session="worker")
        self.agentctl("check", "--mode", "manual", session="worker")

    def test_restart_changes_only_the_exact_task_row_and_is_idempotent(self):
        task = self.complete_task()
        peer = task + "-PEER"
        self.agentctl(
            "task", "create", "--id", peer, "--title", "longer peer identifier",
            "--owner", "cursor", "--scope", "src/peer/", session="reviewer",
        )
        plan = self.root / ".agent/PROJECT_PLAN.md"
        peer_row = next(line for line in plan.read_text(encoding="utf-8").splitlines()
                        if line.startswith(f"- [ ] {peer} -"))
        peer_doc = self.doc(peer)
        self.agentctl("work", "--agent", "codex", "--task", task, session="worker")
        self.assert_views(task, "in_progress", "codex")
        self.assertIn(peer_row, plan.read_text(encoding="utf-8"))
        self.assertEqual(self.doc(peer), peer_doc)
        before = json.loads(self.agentctl("status", "--json", session="worker").stdout)
        plan_bytes, doc_bytes = plan.read_bytes(), self.doc(task)
        self.agentctl("work", "--agent", "codex", "--task", task, session="worker")
        after = json.loads(self.agentctl("status", "--json", session="worker").stdout)
        self.assertEqual(plan.read_bytes(), plan_bytes)
        self.assertEqual(self.doc(task), doc_bytes)
        self.assertEqual(after["claim_id"], before["claim_id"])
        self.assertEqual(after["doc_hashes"], before["doc_hashes"])
        self.agentctl("note", "same holder continues after retry", session="worker")
        self.agentctl("check", "--mode", "manual", session="worker")

    def test_competing_restart_does_not_mutate_durable_views(self):
        task = self.open_task("worker", "owned work", "src/worker/", "--done", "fixture")
        paths = [
            ".agent/board.json", ".agent/TASKS.md", ".agent/PROJECT_PLAN.md",
            f".agent/tasks/{task}.md", ".agent/logs/progress.md",
        ]
        before = {path: (self.root / path).read_bytes() for path in paths}
        refused = self.agentctl(
            "work", "--agent", "codex", "--task", task, expect=1, session="competitor",
        )
        self.assertRegex(refused.stdout + refused.stderr, r"session conflict|claimed|locked|takeover")
        self.assertEqual({path: (self.root / path).read_bytes() for path in paths}, before)
        self.agentctl("note", "holder continues after refusal", session="worker")
        self.assert_views(task, "in_progress", "codex")
