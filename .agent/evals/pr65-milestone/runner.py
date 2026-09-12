"""Supervisor-owned positive oracles; read candidate code, write only temp fixtures.

No task gate or synthetic runtime identity is used. Done/archive states are
explicit storage fixtures, not claims of independently approved live work.
"""
import argparse
import contextlib
import copy
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile

sys.dont_write_bytecode = True
FAILURES = []


def require(condition, label, observed=None):
    if not condition:
        FAILURES.append(label)
        print("FAIL:", label, "observed=", repr(observed))


def entry(task, status="todo", deps=(), milestone=False, timestamp="2026-01-01 00:00:00"):
    return {"title": task, "type": "milestone" if milestone else "generic",
            "status": status, "deps": list(deps), "scope": [".agent/"],
            "owner": None, "created_at": timestamp, "updated_at": timestamp}


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def fixture(root, tasks, archived=None):
    board = {"version": 1, "tasks": copy.deepcopy(tasks)}
    write_json(root / ".agent/board.json", board)
    for folder, rows in (("tasks", tasks), ("archive/tasks", archived or {})):
        for task, row in rows.items():
            path = root / ".agent" / folder / (task + ".md")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                "# " + task + "\n\nStatus: " + row["status"]
                + "\n\n## Task Contract\n\n- Definition of Done: all declared children done"
                + "\n\n## Completion Record\n\n- Summary: fixture\n",
                encoding="utf-8",
            )
    (root / ".agent/PROJECT_PLAN.md").write_text(
        "# Plan\n\n## Task Board\n" + "".join(
            "- [" + ("x" if row["status"] == "done" else " ") + "] "
            + task + " - " + task + "\n" for task, row in tasks.items()
        ) + "\n## Risks\n\nNone.\n", encoding="utf-8",
    )
    if archived is not None:
        write_json(root / ".agent/archive/board.json", {"version": 1, "tasks": archived})
    return board


def cli_board(target, root, tree=False):
    command = [sys.executable, "-B", str(target / "tools/agentctl.py"), "board"]
    if tree:
        command.append("--tree")
    result = subprocess.run(command, cwd=root, text=True, capture_output=True, timeout=30)
    require(result.returncode == 0, "board CLI exits 0", result.stderr)
    return result.stdout


def completion(root, task):
    return (root / ".agent/tasks" / (task + ".md")).read_text(encoding="utf-8")


def cascade(module, root, board):
    with contextlib.redirect_stdout(io.StringIO()):
        module._cascade_and_report(root, board)
    return json.loads((root / ".agent/board.json").read_text(encoding="utf-8"))


def archive_correctness(module, target):
    with tempfile.TemporaryDirectory(prefix="pr65-eval-archive-") as directory:
        root = Path(directory)
        tasks = {"M-0": entry("M-0", deps=["M-1"], milestone=True),
                 "M-1": entry("M-1", deps=["E-1", "E-2"], milestone=True),
                 "E-2": entry("E-2", "done")}
        board = fixture(root, tasks, {"E-1": entry("E-1", "done")})
        archive_before = (root / ".agent/archive/board.json").read_bytes()
        board = cascade(module, root, board)
        states = {task: row["status"] for task, row in board["tasks"].items()}
        require(states["M-1"] == states["M-0"] == "done",
                "CR001 archived done child closes both ancestor levels", states)
        for task, children in (("M-1", ["E-1", "E-2"]), ("M-0", ["M-1"])):
            record = completion(root, task)
            require("Status: done" in record and "- Completed-at:" in record
                    and all(child in record.split("## Completion Record")[-1] for child in children),
                    "CR001 completion record names children: " + task, record)
        flat, tree = cli_board(target, root), cli_board(target, root, tree=True)
        require("2/2 children done" in flat, "CR001 flat board counts archived child", flat)
        require("E-1 [missing]" not in tree and re.search(r"E-1 \[[^\]]*\bdone\b[^\]]*\]", tree),
                "CR001 tree resolves archived done leaf", tree)
        require((root / ".agent/archive/board.json").read_bytes() == archive_before,
                "CR001 cascade preserves archived evidence")
        plan = (root / ".agent/PROJECT_PLAN.md").read_text(encoding="utf-8")
        require("- [x] M-0" in plan and "- [x] M-1" in plan,
                "CR001 recursive completion checks plan boxes", plan)


def merge(module, base, ours, theirs):
    return module._merge_ledger_file(
        "board.json", *(json.dumps(value) for value in (base, ours, theirs)))


def parent_merge_correctness(module, target):
    del target
    base = {"version": 1, "tasks": {
        "M-1": entry("M-1", deps=["E-0"], milestone=True),
        "E-0": entry("E-0", "done"),
    }}
    ours, theirs = copy.deepcopy(base), copy.deepcopy(base)
    ours["tasks"]["M-1"].update(deps=["E-0", "E-1"], updated_at="2026-01-01 00:00:01")
    theirs["tasks"]["M-1"].update(deps=["E-0", "E-2"], updated_at="2026-01-01 00:00:02")
    ours["tasks"]["E-1"] = entry("E-1")
    theirs["tasks"]["E-2"] = entry("E-2", "done")
    for label, left, right in (("ours-first", ours, theirs), ("theirs-first", theirs, ours)):
        text, conflict = merge(module, base, left, right)
        require(not conflict, "CR002 additive merge is conflict-free: " + label, text)
        if conflict:
            continue
        merged = json.loads(text)
        deps = merged["tasks"]["M-1"]["deps"]
        require(set(deps) == {"E-0", "E-1", "E-2"} and len(deps) == 3,
                "CR002 both additions and base edge survive: " + label, deps)
        with tempfile.TemporaryDirectory(prefix="pr65-eval-union-") as directory:
            root = Path(directory)
            board = fixture(root, merged["tasks"])
            board = cascade(module, root, board)
            require(board["tasks"]["M-1"]["status"] == "todo",
                    "CR002 parent waits for unfinished child: " + label, board["tasks"]["M-1"])
            board["tasks"]["E-1"]["status"] = "done"
            write_json(root / ".agent/board.json", board)
            board = cascade(module, root, board)
            require(board["tasks"]["M-1"]["status"] == "done",
                    "CR002 last child closes parent: " + label, board["tasks"]["M-1"])
            record = completion(root, "M-1")
            require(all(child in record.split("## Completion Record")[-1]
                        for child in ("E-0", "E-1", "E-2")),
                    "CR002 completion preserves all child evidence: " + label, record)

    # These safety oracles describe the announced conflict contract, not an
    # implementation-specific exception type or merge algorithm.
    scenarios = []
    deletion = copy.deepcopy(ours)
    deletion["tasks"]["M-1"]["deps"] = ["E-1"]
    scenarios.append(("concurrent-deletion", base, deletion, theirs))
    closing = copy.deepcopy(base)
    closing["tasks"]["M-1"].update(status="done", updated_at="2026-01-01 00:00:03")
    scenarios.append(("concurrent-closure", base, closing, theirs))
    done_base = copy.deepcopy(base)
    done_base["tasks"]["M-1"]["status"] = "done"
    reopened = copy.deepcopy(done_base)
    reopened["tasks"]["M-1"].update(status="todo", updated_at="2026-01-01 00:00:04")
    done_edit = copy.deepcopy(done_base)
    done_edit["tasks"]["M-1"].update(deps=["E-0", "E-2"], updated_at="2026-01-01 00:00:05")
    done_edit["tasks"]["E-2"] = entry("E-2")
    scenarios.append(("concurrent-reopen", done_base, reopened, done_edit))
    cycle_base = {"version": 1, "tasks": {"M-A": entry("M-A", milestone=True),
                                           "M-B": entry("M-B", milestone=True)}}
    cycle_a, cycle_b = copy.deepcopy(cycle_base), copy.deepcopy(cycle_base)
    cycle_a["tasks"]["M-A"]["deps"] = ["M-B"]
    cycle_b["tasks"]["M-B"]["deps"] = ["M-A"]
    scenarios.append(("merged-cycle", cycle_base, cycle_a, cycle_b))
    for label, common, left, right in scenarios:
        for order, first, second in (("ab", left, right), ("ba", right, left)):
            try:
                _, conflict = merge(module, common, first, second)
            except ValueError:
                conflict = True
            require(conflict, "CR002 unsafe merge reports conflict: " + label + "-" + order)


def live_precedence_compatibility(module, target):
    with tempfile.TemporaryDirectory(prefix="pr65-eval-heldout-") as directory:
        root = Path(directory)
        tasks = {"M-LIVE": entry("M-LIVE", deps=["E-LIVE"], milestone=True),
                 "E-LIVE": entry("E-LIVE", "todo", timestamp="2026-02-01 00:00:00"),
                 "M-EMPTY": entry("M-EMPTY", milestone=True),
                 "M-MISSING": entry("M-MISSING", deps=["E-ABSENT"], milestone=True)}
        board = fixture(root, tasks, {"E-LIVE": entry("E-LIVE", "done")})
        archived_before = (root / ".agent/archive/board.json").read_bytes()
        board = cascade(module, root, board)
        require(all(row["status"] == "todo" for row in board["tasks"].values()),
                "held_out live reopen overrides stale archive; empty/missing stay open", board)
        tree = cli_board(target, root, tree=True)
        require(re.search(r"E-LIVE \[[^\]]*\btodo\b[^\]]*\]", tree) is not None,
                "held_out tree uses live reopened status", tree)
        require("E-ABSENT [missing]" in tree, "held_out true missing remains visible", tree)
        require((root / ".agent/archive/board.json").read_bytes() == archived_before,
                "held_out archive remains immutable")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("case", choices=["archive", "parent-merge", "live-precedence"])
    parser.add_argument("--sha256", required=True)
    args = parser.parse_args()
    actual = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    if actual != args.sha256:
        print("Verifier integrity mismatch", file=sys.stderr)
        return 2
    target = Path.cwd().resolve()
    module_path = target / "tools/agentctl.py"
    spec = importlib.util.spec_from_file_location("reviewed_agentctl", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    {"archive": archive_correctness, "parent-merge": parent_merge_correctness,
     "live-precedence": live_precedence_compatibility}[args.case](module, target)
    print(args.case + ": " + ("FAIL " + str(len(FAILURES)) + " correctness assertions"
                               if FAILURES else "PASS all correctness assertions"))
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
