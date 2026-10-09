"""Independent review probes. Exit 0 means the two reported defects reproduced.

Uses the real inherited runtime for CLI commands. Done states below are explicit
test fixture states, not independent approvals or forged worker identities.
"""
import contextlib
import copy
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools import agentctl


def run(root, *args):
    result = subprocess.run(
        [sys.executable, str(ROOT / "tools/agentctl.py"), *args],
        cwd=root, text=True, capture_output=True, timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout


with tempfile.TemporaryDirectory(prefix="pr65-archive-probe-") as directory:
    root = Path(directory)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    run(ROOT, "init", str(root))
    run(root, "work", "--agent", "archive-probe-reviewer", "--auto-create",
        "--type", "review", "--title", "Isolated archive acceptance fixture",
        "--scope", ".agent/", "--done", "Observe archive and cascade")
    run(root, "agents", "add", "--id", "archive-probe-reviewer", "--role", "review")
    run(root, "refresh")
    run(root, "task", "create", "--id", "M-1", "--type", "milestone", "--title", "Parent")
    run(root, "task", "create", "--id", "E-1", "--title", "First", "--parent", "M-1")
    run(root, "task", "create", "--id", "E-2", "--title", "Last", "--parent", "M-1")
    board = agentctl._load_board(root)
    board["tasks"]["E-1"].update(status="done", updated_at="2000-01-01 00:00:00")
    agentctl._save_board(root, board)
    agentctl._set_task_doc_status(root, "E-1", "done")
    agentctl._render_task_views(root, board)
    run(root, "refresh")
    print(run(root, "reconcile", "archive", "--days", "30"))
    archived = json.loads((root / ".agent/archive/board.json").read_text())
    assert archived["tasks"]["E-1"]["status"] == "done"
    board = agentctl._load_board(root)
    assert "E-1" not in board["tasks"]
    # Feed precisely the same done-state board consumed by all done-path callers.
    board["tasks"]["E-2"]["status"] = "done"
    agentctl._set_task_doc_status(root, "E-2", "done")
    agentctl._save_board(root, board)
    closed = agentctl._cascade_and_report(root, board)
    tree = run(root, "board", "--tree")
    print(tree)
    print("archive: closed=", closed, "parent=", board["tasks"]["M-1"]["status"],
          "children=", agentctl._milestone_children(board, "M-1"))
    assert closed == [] and board["tasks"]["M-1"]["status"] == "todo"
    assert "E-1 [missing]" in tree
    assert agentctl._milestone_children(board, "M-1") == (["E-2"], ["E-1"])

base = {"version": 1, "tasks": {"M-1": {
    "type": "milestone", "status": "todo", "deps": [], "updated_at": "2026-09-01 00:00:00",
}}}
ours, theirs = copy.deepcopy(base), copy.deepcopy(base)
ours["tasks"]["M-1"].update(deps=["E-1"], updated_at="2026-09-01 00:00:01")
theirs["tasks"]["M-1"].update(deps=["E-2"], updated_at="2026-09-01 00:00:02")
ours["tasks"]["E-1"] = {"type": "generic", "status": "todo", "deps": []}
theirs["tasks"]["E-2"] = {"type": "generic", "status": "done", "deps": []}
merged_text, conflict = agentctl._merge_ledger_file(
    "board.json", *(json.dumps(value) for value in (base, ours, theirs)))
merged = json.loads(merged_text)
print("concurrent-parent: conflict=", conflict, "deps=", merged["tasks"]["M-1"]["deps"],
      "tasks=", sorted(merged["tasks"]))
assert not conflict and merged["tasks"]["M-1"]["deps"] == ["E-2"]
assert merged["tasks"]["E-1"]["status"] == "todo"
with tempfile.TemporaryDirectory(prefix="pr65-merge-probe-") as directory:
    closed = agentctl._cascade_milestones(Path(directory), merged)
    print("concurrent-parent: closed=", closed, "while E-1=", merged["tasks"]["E-1"]["status"])
    assert closed == ["M-1"]
print("REPRODUCED: archive strands milestone; concurrent parent merge drops child and closes early")
