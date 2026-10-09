"""A durable claim, not an agent label or an old local session, owns a task."""

import importlib.util
import json
import subprocess
import sys
import unittest
from pathlib import Path

from tools import agentctl


KIT = Path(__file__).resolve().parents[1]
_fixture_spec = importlib.util.spec_from_file_location(
    "claim_sync_fixtures", KIT / "tests" / "test_multi_checkout_sync.py",
)
sync_fixtures = importlib.util.module_from_spec(_fixture_spec)
_fixture_spec.loader.exec_module(sync_fixtures)


class ClaimLedgerMergeTest(unittest.TestCase):
    def row(self, claim, *, owner="codex", status="in_progress", stamp="2026-01-01 00:00:00", **extra):
        return {
            "title": "contested task", "owner": owner, "status": status,
            "claim_id": claim, "updated_at": stamp, **extra,
        }

    def merge(self, base, ours, theirs):
        sides = [json.dumps({"version": 1, "tasks": {"T-1": row}}) for row in (base, ours, theirs)]
        merged = agentctl._merge_ledger_json(*sides, "tasks", agentctl._resolve_board_entry)
        return json.loads(merged)["tasks"]["T-1"]

    def test_independent_claims_conflict_despite_owner_rank_or_timestamp(self):
        base = {"title": "contested task", "status": "todo", "owner": "codex"}
        for owner in ("codex", "cursor"):
            for status in ("in_progress", "review"):
                ours = self.row("claim-a", note="local update")
                theirs = self.row("claim-b", owner=owner, status=status, stamp="2026-01-02 00:00:00")
                for left, right in ((ours, theirs), (theirs, ours)):
                    with self.subTest(owner=owner, status=status, ours=left["claim_id"]):
                        with self.assertRaises(ValueError, msg="different claims must not pick a winner"):
                            self.merge(base, left, right)

    def test_takeover_racing_an_original_claim_update_conflicts(self):
        base = self.row("original-claim")
        original_update = self.row("original-claim", status="review", note="old holder finished")
        takeover = self.row("replacement-claim", stamp="2026-01-02 00:00:00")
        for ours, theirs in ((original_update, takeover), (takeover, original_update)):
            with self.subTest(ours=ours["claim_id"]):
                with self.assertRaises(ValueError):
                    self.merge(base, ours, theirs)

    def test_same_claim_notes_keep_the_newer_update(self):
        base = self.row("claim-a")
        older = self.row("claim-a", stamp="2026-01-02 00:00:00", note="older")
        newer = self.row("claim-a", stamp="2026-01-03 00:00:00", note="newer")
        for ours, theirs in ((older, newer), (newer, older)):
            with self.subTest(ours=ours["note"]):
                self.assertEqual(self.merge(base, ours, theirs), newer)

    def test_same_claim_status_can_advance_even_with_an_older_timestamp(self):
        base = self.row("claim-a")
        note = self.row("claim-a", stamp="2026-01-03 00:00:00", note="still working")
        reviewed = self.row("claim-a", status="review", stamp="2026-01-02 00:00:00")
        for ours, theirs in ((note, reviewed), (reviewed, note)):
            with self.subTest(ours=ours["status"]):
                self.assertEqual(self.merge(base, ours, theirs), reviewed)

    def test_one_sided_takeover_is_not_a_competing_claim(self):
        base = self.row("original-claim")
        takeover = self.row("replacement-claim", owner="cursor", stamp="2026-01-02 00:00:00")
        self.assertEqual(self.merge(base, base, takeover), takeover)
        self.assertEqual(self.merge(base, takeover, base), takeover)


class ClaimAuthorityCheckoutTest(unittest.TestCase):
    # Reuse the real installed two-clone fixture without inheriting its tests.
    setUp = sync_fixtures.TwoCheckoutsOneRemoteTest.setUp
    clone = sync_fixtures.TwoCheckoutsOneRemoteTest.clone
    git = sync_fixtures.TwoCheckoutsOneRemoteTest.git
    env = sync_fixtures.TwoCheckoutsOneRemoteTest.env
    agentctl = sync_fixtures.TwoCheckoutsOneRemoteTest.agentctl
    git_as = sync_fixtures.TwoCheckoutsOneRemoteTest.git_as
    task_id = sync_fixtures.TwoCheckoutsOneRemoteTest.task_id
    board = sync_fixtures.TwoCheckoutsOneRemoteTest.board
    open_task = sync_fixtures.TwoCheckoutsOneRemoteTest.open_task

    def run_cli(self, root, session, *args):
        return subprocess.run(
            [sys.executable, "tools/agentctl.py", *args], cwd=root,
            env=self.env(session), text=True, capture_output=True, timeout=120,
        )

    def session(self, root, session):
        return json.loads(self.agentctl(root, session, "status", "--json").stdout)

    def session_path(self, root, state):
        paths = []
        for path in agentctl._session_runtime_dir(root).glob("*.json"):
            row = json.loads(path.read_text(encoding="utf-8"))
            if (row.get("workflow_session_key") == state["workflow_session_key"]
                    and row.get("task") == state["task"]):
                paths.append(path)
        self.assertEqual(len(paths), 1, paths)
        return paths[0]

    def bind_fixture_claim(self, root, session, task, fallback):
        """Backfill only pre-claim-id baselines so denial paths still execute."""
        board_path = root / ".agent" / "board.json"
        board = json.loads(board_path.read_text(encoding="utf-8"))
        state = self.session(root, session)
        path = self.session_path(root, state)
        claim = board["tasks"][task].get("claim_id") or fallback
        board["tasks"][task]["claim_id"] = claim
        if not state.get("claim_id"):
            state["claim_id"] = claim
        board_path.write_text(json.dumps(board, indent=2) + "\n", encoding="utf-8")
        path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
        self.assertEqual(state["claim_id"], claim)
        return claim, state, path

    def local_claim(self):
        task = self.open_task(self.a, "conv-a", "codex", "claim authority", "docs/claim/")
        claim, state, path = self.bind_fixture_claim(self.a, "conv-a", task, "fixture-claim-a")
        return task, claim, state, path

    def import_competing_claim(self, task, owner):
        # A refreshed Git ledger can replace the board while the private local
        # session remains. Re-reading documents must not rebind its authority.
        path = self.a / ".agent" / "board.json"
        board = json.loads(path.read_text(encoding="utf-8"))
        board["tasks"][task].update({
            "claim_id": "imported-claim-b", "owner": owner,
            "status": "in_progress", "updated_at": "2026-01-03 00:00:00",
        })
        path.write_text(json.dumps(board, indent=2) + "\n", encoding="utf-8")

    def protected_state(self, task):
        return {
            relative: (self.a / relative).read_bytes()
            for relative in (
                ".agent/board.json", ".agent/TASKS.md", ".agent/PROJECT_PLAN.md",
                f".agent/tasks/{task}.md", ".agent/logs/progress.md",
            )
        }

    def assert_loser_denied(self, *args):
        task, claim, original, session_path = self.local_claim()
        for owner in ("cursor", "codex"):
            with self.subTest(imported_owner=owner):
                self.import_competing_claim(task, owner)
                # Isolate authority from the ordinary stale-document gate.
                original["doc_hashes"] = agentctl._hash_docs(self.a, task)
                session_path.write_text(json.dumps(original, indent=2) + "\n", encoding="utf-8")
                before = self.protected_state(task)
                command = [task if arg == "<task>" else arg for arg in args]
                proc = self.run_cli(self.a, "conv-a", *command)
                self.assertNotEqual(
                    proc.returncode, 0,
                    f"lost {claim} to imported-claim-b but accepted {command}:\n{proc.stdout}{proc.stderr}",
                )
                self.assertRegex((proc.stdout + proc.stderr).lower(), r"claim|takeover")
                self.assertEqual(self.protected_state(task), before, "denial must not change the ledger")
                after = json.loads(session_path.read_text(encoding="utf-8"))
                for key in ("claim_id", "doc_hashes", "notes", "claimed_files"):
                    self.assertEqual(after.get(key), original.get(key), key)

    def test_new_claim_is_durable_and_bound_to_its_local_session(self):
        task = self.open_task(self.a, "conv-a", "codex", "mint a claim", "docs/claim/")
        entry = self.board(self.a)[task]
        state = self.session(self.a, "conv-a")
        claim = entry.get("claim_id")
        self.assertIsInstance(claim, str, entry)
        self.assertTrue(claim.strip(), "start must mint a nonempty claim_id")
        self.assertEqual(state.get("claim_id"), claim)
        self.agentctl(self.a, "conv-a", "work", "--agent", "codex", "--task", task)
        self.assertEqual(self.board(self.a)[task]["claim_id"], claim, "resume must not mint another claim")
        self.assertEqual(self.session(self.a, "conv-a")["claim_id"], claim)

    def test_loser_cannot_refresh_away_the_claim_mismatch(self):
        self.assert_loser_denied("refresh")

    def test_loser_cannot_resume_from_its_old_local_session(self):
        self.assert_loser_denied("work", "--agent", "codex")

    def test_loser_cannot_note_even_after_rereading_current_documents(self):
        self.assert_loser_denied("note", "unauthorized old-claim note")

    def test_loser_cannot_register_an_in_scope_file_write(self):
        self.assert_loser_denied("sessions", "guard", "--path", "docs/claim/result.md")

    def test_loser_cannot_explicitly_start_using_historical_local_presence(self):
        self.assert_loser_denied("start", "--task", "<task>", "--agent", "codex")

    def test_force_is_not_a_claim_takeover(self):
        self.assert_loser_denied("start", "--task", "<task>", "--agent", "codex", "--force")

    def test_takeover_requires_a_reason_even_with_an_old_local_session(self):
        self.assert_loser_denied("work", "--task", "<task>", "--agent", "codex", "--takeover")

    def test_explicit_reasoned_takeover_binds_a_new_claim_and_records_it(self):
        task, old_claim, _, _ = self.local_claim()
        self.import_competing_claim(task, "codex")
        reason = "fixture: remote holder abandoned work; notes and outputs inspected"
        self.agentctl(
            self.a, "conv-a", "work", "--agent", "codex", "--task", task,
            "--takeover", "--reason", reason,
        )
        entry = self.board(self.a)[task]
        state = self.session(self.a, "conv-a")
        self.assertTrue(entry.get("claim_id"), entry)
        self.assertNotIn(entry["claim_id"], (old_claim, "imported-claim-b"))
        self.assertEqual(state.get("claim_id"), entry["claim_id"])
        self.assertEqual(entry.get("takeover_reason"), reason)
        self.assertEqual(entry.get("taken_over_from"), "codex")
        for relative in (f".agent/tasks/{task}.md", ".agent/logs/progress.md"):
            text = (self.a / relative).read_text(encoding="utf-8")
            self.assertIn(reason, text)
            self.assertIn("taken over", text)
        self.agentctl(self.a, "conv-a", "note", "new claim is authorized")
        self.assertEqual(self.board(self.a)[task]["claim_id"], entry["claim_id"])

    def test_same_agent_name_on_another_checkout_does_not_own_the_claim(self):
        task, claim, _, _ = self.local_claim()
        self.agentctl(self.a, "conv-a", "sync")
        self.git(self.b, "pull", "-q", "--ff-only", "origin", "main")
        before = (self.b / ".agent" / "board.json").read_bytes()
        proc = self.run_cli(self.b, "conv-b", "start", "--task", task, "--agent", "codex")
        self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("--takeover", proc.stderr)
        self.assertEqual((self.b / ".agent" / "board.json").read_bytes(), before)
        self.assertEqual(self.board(self.b)[task]["claim_id"], claim)

    def test_matching_claim_allows_refresh_resume_note_guard_and_status_update(self):
        task, claim, _, _ = self.local_claim()
        self.agentctl(self.a, "conv-a", "refresh")
        self.agentctl(self.a, "conv-a", "work", "--agent", "codex")
        self.agentctl(self.a, "conv-a", "note", "authorized same-claim note")
        self.agentctl(self.a, "conv-a", "sessions", "guard", "--path", "docs/claim/result.md")
        state = self.session(self.a, "conv-a")
        self.assertEqual(state["claim_id"], claim)
        self.assertIn("docs/claim/result.md", state["claimed_files"])
        self.assertEqual(state["notes"][-1]["note"], "authorized same-claim note")
        self.agentctl(
            self.a, "conv-a", "finish", "--done", "fixture claim authority contract",
            "--summary", "same claim completed", "--tests", "fixture: no production code",
        )
        self.assertEqual(self.board(self.a)[task]["status"], "review")
        self.assertEqual(self.board(self.a)[task]["claim_id"], claim)
        self.assertEqual(self.session(self.a, "conv-a")["claim_id"], claim)

    def test_real_sync_does_not_resolve_two_claims_of_the_same_task(self):
        task = "T-CLAIM-1"
        self.agentctl(
            self.a, "publisher", "task", "create", "--id", task, "--type", "docs",
            "--title", "shared unclaimed task", "--scope", "docs/claim/",
        )
        self.open_task(self.a, "publisher", "coordinator", "publish fixture baseline", "docs/publisher/")
        self.agentctl(self.a, "publisher", "sync")
        self.agentctl(self.a, "publisher", "sessions", "release", "--reason", "fixture baseline published")
        self.git(self.b, "pull", "-q", "--ff-only", "origin", "main")
        claims = []
        for root, session in ((self.a, "conv-a"), (self.b, "conv-b")):
            self.agentctl(root, session, "start", "--task", task, "--agent", "codex")
            claim, _, _ = self.bind_fixture_claim(root, session, task, f"fixture-claim-{root.name}")
            claims.append(claim)
            self.agentctl(root, session, "note", f"independent claim from {root.name}")
        self.assertNotEqual(claims[0], claims[1])
        # Keep Markdown content identical so a note/timestamp conflict cannot
        # mask the board driver's silent resolution of competing claims.
        task_doc = Path(".agent") / "tasks" / f"{task}.md"
        (self.b / task_doc).write_bytes((self.a / task_doc).read_bytes())
        self.agentctl(self.b, "conv-b", "refresh")
        self.agentctl(self.a, "conv-a", "sync")
        rejected = self.run_cli(self.b, "conv-b", "sync")
        self.assertNotEqual(rejected.returncode, 0, rejected.stdout + rejected.stderr)
        self.assertIn(".agent/board.json", self.git(self.b, "diff", "--name-only", "--diff-filter=U"))
        stage_two = json.loads(self.git(self.b, "show", ":2:.agent/board.json"))
        self.assertEqual(self.board(self.b), stage_two["tasks"], "driver must preserve Git's ours for reconciliation")
        remote_board = json.loads(self.git(self.b, "show", "origin/main:.agent/board.json"))
        self.assertEqual(remote_board["tasks"][task]["claim_id"], claims[0], "conflicting claim must not be pushed")
        local = self.session(self.b, "conv-b")
        self.assertEqual(local["claim_id"], claims[1])
        # Rebase's ours is upstream. Give this fixture a matching binding so
        # only the unmerged index, not a claim mismatch, can block the guard.
        local["claim_id"] = stage_two["tasks"][task]["claim_id"]
        self.session_path(self.b, local).write_text(json.dumps(local, indent=2) + "\n", encoding="utf-8")
        board_before = (self.b / ".agent" / "board.json").read_bytes()
        guard = self.run_cli(
            self.b, "conv-b", "sessions", "guard", "--path", "docs/claim/conflicted-result.md",
        )
        self.assertNotEqual(guard.returncode, 0, guard.stdout + guard.stderr)
        self.assertRegex((guard.stdout + guard.stderr).lower(), r"unresolved|unmerged|conflict")
        self.assertEqual((self.b / ".agent" / "board.json").read_bytes(), board_before)
        after = self.session(self.b, "conv-b")
        self.assertEqual(after["claim_id"], local["claim_id"])
        self.assertEqual(after["claimed_files"], local["claimed_files"], "unmerged board must block new file claims")


if __name__ == "__main__":
    unittest.main()

