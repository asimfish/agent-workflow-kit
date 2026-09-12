"""Regression oracles for archived children and concurrent milestone edits."""
import contextlib
import copy
import io
import json
import tempfile
import unittest
from pathlib import Path

from tools import agentctl


class MilestoneBoundaryTest(unittest.TestCase):
    def merge(self, base, ours, theirs):
        text, conflict = agentctl._merge_ledger_file(
            "board.json", *(json.dumps({"tasks": x}) for x in (base, ours, theirs)))
        self.assertFalse(conflict)
        return json.loads(text)

    def test_concurrent_additions_survive_both_orders(self):
        base = {"M-1": {"type": "milestone", "status": "todo", "deps": []}}
        ours, theirs = copy.deepcopy(base), copy.deepcopy(base)
        ours["M-1"]["deps"] = ["E-1"]
        theirs["M-1"]["deps"] = ["E-2"]
        ours["E-1"] = {"status": "todo"}
        theirs["E-2"] = {"status": "done"}
        for left, right in ((ours, theirs), (theirs, ours)):
            merged = self.merge(base, left, right)
            self.assertEqual(set(merged["tasks"]["M-1"]["deps"]), {"E-1", "E-2"})
            self.assertEqual(agentctl._milestone_children(merged, "M-1"), (["E-2"], ["E-1"]))

    def test_close_racing_addition_is_conflict(self):
        base = {"M-1": {"type": "milestone", "status": "todo", "deps": ["E-1"]}}
        ours, theirs = copy.deepcopy(base), copy.deepcopy(base)
        ours["M-1"]["status"] = "done"
        theirs["M-1"]["deps"].append("E-2")
        for left, right in ((ours, theirs), (theirs, ours)):
            with self.assertRaisesRegex(ValueError, "milestone"):
                self.merge(base, left, right)

    def test_combined_cycle_is_conflict(self):
        base = {x: {"type": "milestone", "status": "todo", "deps": []} for x in ("M-1", "M-2")}
        ours, theirs = copy.deepcopy(base), copy.deepcopy(base)
        ours["M-1"]["deps"] = ["M-2"]
        theirs["M-2"]["deps"] = ["M-1"]
        with self.assertRaisesRegex(ValueError, "cycle"):
            self.merge(base, ours, theirs)

    def test_removal_racing_addition_is_conflict(self):
        base = {"M-1": {"type": "milestone", "status": "todo", "deps": ["E-1"]}}
        ours, theirs = copy.deepcopy(base), copy.deepcopy(base)
        ours["M-1"]["deps"] = []
        theirs["M-1"]["deps"].append("E-2")
        with self.assertRaisesRegex(ValueError, "removal"):
            self.merge(base, ours, theirs)

    def test_uncontested_removal_survives(self):
        base = {"M-1": {"type": "milestone", "status": "todo", "deps": ["E-1"]}}
        ours = copy.deepcopy(base)
        ours["M-1"]["deps"] = []
        self.assertEqual(self.merge(base, ours, base)["tasks"]["M-1"]["deps"], [])

    def test_reopen_racing_update_is_conflict(self):
        base = {"M-1": {"type": "milestone", "status": "done", "deps": ["E-1"]}}
        ours, theirs = copy.deepcopy(base), copy.deepcopy(base)
        ours["M-1"]["status"] = "todo"
        theirs["M-1"]["title"] = "updated title"
        with self.assertRaisesRegex(ValueError, "reopen"):
            self.merge(base, ours, theirs)

    def test_live_reopen_overrides_archive_and_missing_stays_open(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / ".agent/archive"
            archive.mkdir(parents=True)
            (archive / "board.json").write_text(json.dumps({"tasks": {"E-1": {"status": "done"}}}))
            board = {"tasks": {
                "M-1": {"type": "milestone", "status": "todo", "deps": ["E-1", "E-2"]},
                "E-1": {"status": "todo"},
            }}
            self.assertEqual(agentctl._cascade_milestones(root, board), [])
            resolved = agentctl._milestone_board(root, board)
            self.assertEqual(agentctl._milestone_children(resolved, "M-1"), ([], ["E-1", "E-2"]))

    def test_archived_child_closes_nested_parent_and_renders(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / ".agent/archive"
            archive.mkdir(parents=True)
            (archive / "board.json").write_text(json.dumps({"tasks": {"E-1": {"status": "done"}}}))
            tasks = root / ".agent/tasks"
            tasks.mkdir()
            for tid in ("M-1", "M-2"):
                (tasks / f"{tid}.md").write_text(f"# {tid}\n\nStatus: todo\n")
            board = {"tasks": {
                "M-1": {"type": "milestone", "status": "todo", "deps": ["E-1", "E-2"]},
                "M-2": {"type": "milestone", "status": "todo", "deps": ["M-1"]},
                "E-2": {"status": "done"},
            }}
            self.assertEqual(agentctl._cascade_milestones(root, board), ["M-1", "M-2"])
            self.assertIn("E-1, E-2", (tasks / "M-1.md").read_text())
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                agentctl._print_board_tree(board, root=root)
            self.assertIn("E-1 [done]", output.getvalue())
            self.assertNotIn("[missing]", output.getvalue())


if __name__ == "__main__":
    unittest.main()
