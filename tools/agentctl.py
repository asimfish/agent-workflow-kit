#!/usr/bin/env python3
"""agentctl: task / session / board controller for the Agent Workflow Kit.

Dependency-free (stdlib only), Python 3.8+.

Commands:
  init       scaffold workflow files; distribute agentctl.py + git hooks
  work       resume current work, claim the next assigned task, or auto-create one
  start      begin a task session: read receipt + lock + board -> in_progress
  focus      print the active task focus (goal/scope/todo) -- re-read anytime
  note       shorthand progress note for the active task
  progress   append a progress note to the active task
  finish     shorthand complete command for the active task
  complete   move the active task to review (write completion record, free lock)
  gate       approve/reject a task in review (-> done / blocked)
  guidance   create/list/show/ack/dispatch/verify supervisor guidance packets
  handoff    create/list/show/close cross-agent task packets
  worktree   create/list/release task-scoped worktree leases
  lease      list the normalized conversation/worktree/run/resource leases
  run        start/adopt/list/wait/finish/stop durable background runs
  resource   acquire/status/release project and remote resource leases
  eval       run and compare deterministic baseline/candidate verifier suites
  loop       run/status/resume/stop bounded project loops and checkpoints
  sessions   list/heartbeat/guard/release concurrent conversation claims
  refresh    re-record doc hashes after plan/rules/task docs changed
  board      print the task board (human or --json)
  task       create / show task documents and board entries
  agents     add / list agent profiles
  check      verify workflow state (--mode manual|pre-commit|commit-msg|pre-push|ci)
  status     print this conversation's current session (human or --json)

Exit codes: 0 = ok, 1 = violations found, 2 = usage/internal error, 3 = no session.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import errno
import hashlib
import hmac
import json
import math
import os
import platform
import re
import secrets
import signal
import shlex
import shutil
import string
import subprocess
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path
from typing import Protocol
from urllib.parse import urlparse

WORKFLOW_DIR = ".agent"
STATE_DIR = "state"
SESSION_FILE = "current_session.json"
SESSION_RUNTIME_DIR = "sessions"
SESSION_COORDINATION_LOCK = "sessions.lock"
SESSION_VIEW_FILE = "SESSIONS.md"
SESSION_STALE_SECONDS = 30 * 60
SESSION_ID_ENV = "AGENT_WORKFLOW_SESSION_ID"
SESSION_OWNER_RUNTIME_ENV = "AGENT_WORKFLOW_SESSION_OWNER_RUNTIME"
SESSION_INSTANCE_ENV = "AGENT_WORKFLOW_SESSION_INSTANCE_ID"
PARENT_SESSION_KEY_ENV = "AGENT_WORKFLOW_PARENT_SESSION_KEY"
SESSION_ISOLATION_ERROR_ENV = "AGENT_WORKFLOW_SESSION_ISOLATION_ERROR"
COMMAND_ACTION_ATTRS = {
    "task": "task_action",
    "reconcile": "reconcile_action",
    "agents": "agents_action",
    "handoff": "handoff_action",
    "worktree": "worktree_action",
    "lease": "lease_action",
    "run": "run_action",
    "resource": "resource_action",
    "eval": "eval_action",
    "guidance": "guidance_action",
    "loop": "loop_action",
    "sessions": "sessions_action",
    "upgrade": "upgrade_action",
}
IDENTITY_FREE_COMMAND_PATHS = frozenset({
    ("init",),
    ("focus",),
    ("capsule",),
    ("board",),
    ("task", "show"),
    ("reconcile", "check"),
    ("lease", "list"),
    ("run", "list"),
    ("run", "show"),
    ("run", "wait"),
    ("resource", "status"),
    ("agents", "list"),
    ("handoff", "list"),
    ("handoff", "show"),
    ("eval", "list"),
    ("eval", "show"),
    ("eval", "compare"),
    ("guidance", "list"),
    ("guidance", "show"),
    ("loop", "list"),
    ("loop", "show"),
    ("check",),
    ("doctor",),
    ("merge-driver",),
    ("migrate",),
    ("sessions", "list"),
    ("upgrade", "status"),
    ("upgrade", "validate"),
    ("status",),
})
LOCKS_DIR = "locks"
BOARD_FILE = "board.json"
AGENTS_FILE = "agents.json"
ADOPTION_FILE = "adoption.json"
INSTALL_MANIFEST_FILE = "install-manifest.json"
KIT_VERSION = "0.5.0"
INSTALL_SCHEMA_VERSION = 2
PROTOCOL_EPOCH = 2
LEGACY_PROTOCOL_EPOCH = 1
UPGRADE_STATE_FILE = "upgrade-state.json"
PLAN_FILE = "PROJECT_PLAN.md"
TASKS_FILE = "TASKS.md"
TASKS_DIR = "tasks"
RULES_DIR = "rules"
LOG_DIR = "logs"
PROGRESS_LOG = "progress.md"
GATES_DIR = "gates"
BUS_DIR = "bus"
BUS_INBOX = "inbox"
BUS_OUTBOX = "outbox"
BUS_DONE = "done"
BUS_FAILED = "failed"
LOOPS_DIR = "loops"
LOOP_RUNS_DIR = "runs"
LOOP_STATE_FILE = "state.json"
LOOP_CHECKPOINTS_FILE = "checkpoints.json"
LOOP_FOLLOW_UP_KIND = "loop-follow-up"
GUIDANCE_KIND = "supervisor-guidance"
LOOP_COMMAND_FENCE = "loop-check"
LOOP_BUILTIN_IDS = ("daily-plan-triage", "doc-hygiene", "experiment-monitor")
LOOP_COMMAND_TIMEOUT_DEFAULT = 120
LOOP_COMMAND_TIMEOUT_MAX = 3600
LOOP_COMMAND_OUTPUT_CAP_DEFAULT = 2000
LOOP_ESCALATE_AFTER_DEFAULT = 3
LOOP_CYCLE_MAX = 100
LOOP_CYCLE_HISTORY_LIMIT = 20
LOOP_CYCLE_EVENT_LIMIT = 100
LOOP_COMMAND_LAUNCH_TIMEOUT = 15.0
_CYCLE_ANY_OWNER = object()
GUIDANCE_DISPATCH_TIMEOUT_DEFAULT = 7200
GUIDANCE_DISPATCH_TIMEOUT_MAX = 86400
GUIDANCE_DISPATCH_PROMPT_MAX = 24000
GUIDANCE_DISPATCH_OUTPUT_CAP = 4000
GUIDANCE_SIGNING_KEY_FILE = "guidance-hmac.key"
GUIDANCE_ACCEPTANCE_DIR = "acceptance"
REASONING_EFFORT_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
WORKTREE_LEASES_DIR = "agent-workflow"
WORKTREE_LEASES_FILE = "worktree-leases.json"
WORKTREE_LEASES_LOCK = "worktree-leases.lock"
RUNTIME_LEASES_FILE = "execution-leases.json"
RUNTIME_LEASES_LOCK = "execution-leases.lock"
RUNTIME_RUNS_DIR = "runs"
SUBMISSION_REQUESTS_DIR = "requests"
SUBMISSION_REQUEST_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
# git worktree add checks out the whole tree; on a loaded machine that can
# take minutes, so it gets a dedicated, overridable budget instead of the
# default subprocess timeout.
WORKTREE_GIT_TIMEOUT_ENV = "AGENT_WORKTREE_GIT_TIMEOUT"
WORKTREE_GIT_TIMEOUT_DEFAULT = 900
RUNTIME_POLICY_FILE = "runtime-policy.json"
RESOURCE_LOCK_ENV = "AGENT_WORKFLOW_RESOURCE_LOCK_DIR"
RESOURCE_REMOTE_PREFIX = "ssh://"
RUN_ID_ENV = "AGENT_WORKFLOW_RUN_ID"
RUN_HEARTBEAT_SECONDS = 2.0
# signal.SIGKILL does not exist on Windows; referencing it there raises
# AttributeError at call time, which broke every run stop. The numeric value
# routes to taskkill /F on Windows and to the real SIGKILL elsewhere.
PORTABLE_SIGKILL = getattr(signal, "SIGKILL", 9)
TASK_TYPES = {"code", "experiment", "docs", "review", "maintenance", "generic"}
ISOLATION_MODES = {"auto", "shared", "worktree", "read-only", "exclusive"}
TASK_ID_NAMESPACE_FILE = "task-id-namespace.key"
TASK_ID_NAMESPACE_BYTES = 32
TASK_ID_SHARD_HEX_LENGTH = 16
EVALS_DIR = "evals"
EVAL_SUITES_FILE = "suites.json"
EVAL_RUNS_DIR = "runs"
EVAL_DECISIONS_DIR = "decisions"
EVAL_SIGNING_KEY_FILE = "eval-hmac.key"
EVAL_OUTPUT_CAP = 4000
EVAL_TIMEOUT_DEFAULT = 120
EVAL_TIMEOUT_MAX = 3600

LOOP_REQUIRED_SECTIONS = ("Trigger", "Execute", "Check", "Feedback", "Memory", "Next")
DEFAULT_LOOP_CHECKPOINTS = {
    "version": 1,
    "checkpoints": {
        "work-start": {
            "loops": ["daily-plan-triage"],
            "strict": False,
            "debounce_minutes": 30,
        },
        "pre-finish": {
            "loops": ["doc-hygiene"],
            "strict": True,
            "debounce_minutes": 0,
        },
        "post-finish": {
            "loops": ["doc-hygiene"],
            "strict": False,
            "debounce_minutes": 0,
        },
        "experiment-check": {
            "loops": ["experiment-monitor"],
            "strict": False,
            "debounce_minutes": 30,
        },
    },
}

STATUSES = ["todo", "ready", "in_progress", "blocked", "review", "approved", "done", "failed"]
ACTIVE_STATUSES = {"in_progress"}
PUSHABLE_STATUSES = {"review", "approved", "done"}
COMMIT_TYPES = ("feat", "fix", "docs", "refactor", "test", "chore", "perf", "ci", "build")

CONVENTIONAL_RE = re.compile(r"^(?:" + "|".join(COMMIT_TYPES) + r")(?:\([^)]+\))?!?: .+")
# A task id is not preceded by a letter, digit, or hyphen ("non-UTF-8" and
# "x-T-1" are prose; "T-1" and "(T-1)" are references) and may carry
# middle segments ("TR024-REVIEW-001"), which the old `\b` boundary split
# into a bogus "REVIEW-001".
TASK_ID_RE = re.compile(r"(?<![A-Za-z0-9-])[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)*-\d+\b")
REFS_LINE_RE = re.compile(r"^\s*refs?\s*:\s*(.+)$", re.IGNORECASE)
TASK_RECORD_ID_RE = re.compile(r"[A-Z][A-Z0-9]*-[A-Z0-9][A-Z0-9._-]*")
SECRET_RES = [
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"ghp_[0-9A-Za-z]{30,}"),
    re.compile(r"xox[baprs]-[0-9A-Za-z-]{10,}"),
    re.compile(r"(?i)(?:api[_-]?key|secret|token|password|passwd)\s*[:=]\s*['\"][^'\"]{8,}['\"]"),
]


# ---------- helpers ----------

def _now() -> str:
    return _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _repo_root(start: Path | None = None) -> Path:
    cur = (start or Path.cwd()).resolve()
    for cand in [cur, *cur.parents]:
        if (cand / WORKFLOW_DIR).is_dir():
            return cand
        if (cand / ".git").is_dir():
            return cand
    return cur


def _kit_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _load_json(path: Path, default):
    if path.is_file():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return default
    return default


def _save_json(path: Path, data) -> None:
    """Replace a JSON snapshot atomically so concurrent readers never see a partial file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.parent / f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp"
    try:
        with temp.open("w", encoding="utf-8") as handle:
            handle.write(json.dumps(data, indent=2, ensure_ascii=False, allow_nan=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


def _git(root: Path, *args: str) -> str:
    try:
        out = subprocess.check_output(["git", "-C", str(root), *args], stderr=subprocess.DEVNULL)
        return out.decode("utf-8", "replace").strip()
    except Exception:
        return ""


def _git_common_dir(root: Path) -> Path | None:
    raw = _git(root, "rev-parse", "--git-common-dir")
    if not raw:
        return None
    path = Path(raw)
    if not path.is_absolute():
        path = root / path
    return path.resolve()


def _git_dir(root: Path) -> Path | None:
    raw = _git(root, "rev-parse", "--git-dir")
    if not raw:
        return None
    path = Path(raw)
    if not path.is_absolute():
        path = root / path
    return path.resolve()


def _git_process(root: Path, *args: str, timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        text=True,
        capture_output=True,
        timeout=timeout,
    )


GITIGNORE_HEADER = "# Agent Workflow Kit local state"
# Per-entry so a re-run on an older install appends only what is missing.
# The default artifact root is where `run start --output` puts checkpoints and
# logs; keeping it out of Git by default avoids staging gigabytes by accident.
GITIGNORE_MANAGED_ENTRIES = (".agent/state/", ".agent/tmp/", ".agent-artifacts/")


def _ensure_gitignore(root: Path) -> None:
    path = root / ".gitignore"
    text = _read(path)
    lines = text.splitlines()
    present = {line.strip().lstrip("/") for line in lines}
    missing = [entry for entry in GITIGNORE_MANAGED_ENTRIES if entry not in present]
    if not missing:
        return
    if GITIGNORE_HEADER in lines:
        idx = lines.index(GITIGNORE_HEADER) + 1
        while idx < len(lines) and lines[idx].strip().lstrip("/") in GITIGNORE_MANAGED_ENTRIES:
            idx += 1
        lines[idx:idx] = missing
        _write(path, "\n".join(lines) + "\n")
        return
    while lines and not lines[-1].strip():
        lines.pop()
    block = [GITIGNORE_HEADER, *missing]
    if lines:
        _write(path, "\n".join([*lines, "", *block]) + "\n")
    else:
        _write(path, "\n".join(block) + "\n")


# ---------- ledger merge across checkouts ----------
#
# The plan, the task board, and the task index are files in Git, and every
# checkout on every machine commits to them. Git's line merge cannot combine
# two checkouts that each added a task, so without help every concurrent
# ledger change ends in a conflict on board.json. These files are keyed by
# task id, which makes a deterministic three-way merge possible: entries are
# merged per id, a genuinely competing edit of the same entry resolves to the
# one further along the task lifecycle, the progress log is append-only, and
# loop runtime state stays with the checkout that owns it.

LEDGER_MERGE_DRIVER = "agent-ledger"
GITATTRIBUTES_HEADER = "# Agent Workflow Kit ledger merge"
GITATTRIBUTES_MANAGED_ENTRIES = (
    ".agent/board.json merge=agent-ledger",
    ".agent/TASKS.md merge=agent-ledger",
    ".agent/PROJECT_PLAN.md merge=agent-ledger",
    ".agent/agents.json merge=agent-ledger",
    ".agent/loops/state.json merge=agent-ledger",
    ".agent/archive/board.json merge=agent-ledger",
    ".agent/logs/progress.md merge=union",
)
LEDGER_STATUS_RANK = {
    "todo": 0, "ready": 0, "in_progress": 1, "blocked": 1,
    "review": 2, "approved": 3, "done": 4, "failed": 4,
}


def _ensure_gitattributes(root: Path) -> None:
    path = root / ".gitattributes"
    text = _read(path)
    lines = text.splitlines()
    present = {" ".join(line.split()) for line in lines}
    missing = [entry for entry in GITATTRIBUTES_MANAGED_ENTRIES if entry not in present]
    if not missing:
        return
    if GITATTRIBUTES_HEADER in lines:
        idx = lines.index(GITATTRIBUTES_HEADER) + 1
        while idx < len(lines) and " ".join(lines[idx].split()) in GITATTRIBUTES_MANAGED_ENTRIES:
            idx += 1
        lines[idx:idx] = missing
        _write(path, "\n".join(lines) + "\n")
        return
    while lines and not lines[-1].strip():
        lines.pop()
    block = [GITATTRIBUTES_HEADER, *missing]
    _write(path, "\n".join([*lines, "", *block] if lines else block) + "\n")


def _ledger_merge_driver_command() -> str:
    return (
        "python3 tools/agentctl.py merge-driver --base %O --ours %A --theirs %B --path %P"
    )


def _configure_ledger_merge_driver(root: Path) -> None:
    """Register the merge driver in this clone's Git config (per clone, like core.hooksPath)."""
    _git(root, "config", f"merge.{LEDGER_MERGE_DRIVER}.name",
         "agent workflow ledger (per-task three-way merge)")
    _git(root, "config", f"merge.{LEDGER_MERGE_DRIVER}.driver", _ledger_merge_driver_command())


def _ledger_merge_driver_configured(root: Path) -> bool:
    return _git(root, "config", "--get", f"merge.{LEDGER_MERGE_DRIVER}.driver").strip() != ""


def _three_way_entries(base: dict, ours: dict, theirs: dict, resolve,
                       deletion_wins=None) -> dict:
    """Merge dicts keyed by id, one entry at a time.

    Unchanged on one side takes the other side's version (including a
    deletion). A deletion racing a modification keeps the modification,
    because archiving a task someone else just advanced must not lose the
    advance -- unless `deletion_wins(base_entry, survivor)` says the
    modification changed nothing that matters (both still `done`, for the
    board), in which case the archive stands. Both sides changed
    differently -> `resolve(key, ours, theirs)`. Ours' key order is kept;
    theirs' new keys follow in theirs' order.
    """
    merged: dict = {}
    for key in [*ours, *[k for k in theirs if k not in ours], *[k for k in base if k not in ours and k not in theirs]]:
        b, o, t = base.get(key), ours.get(key), theirs.get(key)
        if o == t:
            if o is not None:
                merged[key] = o
            continue
        if o == b:
            if t is not None:
                merged[key] = t
            continue
        if t == b:
            if o is not None:
                merged[key] = o
            continue
        if o is None or t is None:
            survivor = t if o is None else o
            if deletion_wins is not None and b is not None and deletion_wins(b, survivor):
                continue
            merged[key] = survivor
        else:
            merged[key] = resolve(key, o, t)
    return merged


def _board_deletion_wins(base_entry: dict, survivor: dict) -> bool:
    """An archived task stays archived when the other side only touched a done entry."""
    return (
        isinstance(base_entry, dict) and isinstance(survivor, dict)
        and str(base_entry.get("status") or "") == "done"
        and str(survivor.get("status") or "") == "done"
    )


def _resolve_board_entry(_key: str, ours: dict, theirs: dict) -> dict:
    """Competing edits of one task: the entry further along the lifecycle wins."""
    if not isinstance(ours, dict) or not isinstance(theirs, dict):
        return ours
    o_rank = LEDGER_STATUS_RANK.get(str(ours.get("status") or ""), 0)
    t_rank = LEDGER_STATUS_RANK.get(str(theirs.get("status") or ""), 0)
    if t_rank > o_rank:
        return theirs
    if o_rank > t_rank:
        return ours
    if str(theirs.get("updated_at") or "") > str(ours.get("updated_at") or ""):
        return theirs
    return ours


def _merge_ledger_json(base_text: str, ours_text: str, theirs_text: str,
                       collection: str, resolve) -> str:
    """Merge two JSON ledgers by entry; raises ValueError on a side that is not a JSON object.

    A side that fails to parse must not be read as "deleted everything": that
    would turn a damaged board on one machine into deletions everywhere. The
    driver reports it as a conflict for a human instead.
    """
    def load(label: str, text: str) -> dict:
        if not text.strip():
            return {}
        try:
            data = json.loads(text)
        except ValueError as exc:
            raise ValueError(f"{label} side is not valid JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise ValueError(f"{label} side is not a JSON object")
        return data

    base, ours, theirs = load("base", base_text), load("ours", ours_text), load("theirs", theirs_text)
    merged = {**theirs, **ours}  # top-level fields: ours wins, theirs-only keys survive
    merged[collection] = _three_way_entries(
        base.get(collection) or {}, ours.get(collection) or {}, theirs.get(collection) or {},
        resolve,
        deletion_wins=_board_deletion_wins if collection == "tasks" else None,
    )
    stamps = [str(d.get("updated") or "") for d in (ours, theirs) if d.get("updated")]
    if stamps:
        merged["updated"] = max(stamps)
    return json.dumps(merged, indent=2, ensure_ascii=False) + "\n"


_TASK_INDEX_ROW_RE = re.compile(
    r"^\|\s*(?P<id>[A-Za-z][A-Za-z0-9]*-[\w.-]+)\s*\|\s*(?P<status>[a-z_]*)\s*\|"
)
_PLAN_ROW_RE = re.compile(
    r"^- \[(?P<check>[ xX])\]\s+(?P<id>[A-Za-z][A-Za-z0-9]*-[\w.-]+)\s+-\s+"
)


def _merge_keyed_lines(base_lines: list[str], ours_lines: list[str], theirs_lines: list[str],
                       key_re, better) -> tuple[list[str], list[str]]:
    """Merge lists of lines keyed by the regex's `id` group; returns (prefix, merged rows).

    Lines that do not match the key pattern (headers) come from ours.
    """
    def split(lines: list[str]) -> tuple[list[str], dict]:
        prefix, rows = [], {}
        for line in lines:
            match = key_re.match(line)
            if match:
                rows[match.group("id")] = line
            elif not rows:
                prefix.append(line)
        return prefix, rows

    o_prefix, o_rows = split(ours_lines)
    _b_prefix, b_rows = split(base_lines)
    _t_prefix, t_rows = split(theirs_lines)
    merged = _three_way_entries(b_rows, o_rows, t_rows, lambda _k, o, t: better(o, t))
    return o_prefix, list(merged.values())


def _better_index_row(ours: str, theirs: str) -> str:
    def rank(line: str) -> int:
        match = _TASK_INDEX_ROW_RE.match(line)
        return LEDGER_STATUS_RANK.get(match.group("status") if match else "", 0)
    return theirs if rank(theirs) > rank(ours) else ours


def _better_plan_row(ours: str, theirs: str) -> str:
    def checked(line: str) -> bool:
        match = _PLAN_ROW_RE.match(line)
        return bool(match and match.group("check").lower() == "x")
    return theirs if checked(theirs) and not checked(ours) else ours


def _merge_task_index(base_text: str, ours_text: str, theirs_text: str) -> str:
    prefix, rows = _merge_keyed_lines(
        base_text.splitlines(), ours_text.splitlines(), theirs_text.splitlines(),
        _TASK_INDEX_ROW_RE, _better_index_row,
    )
    return "\n".join([*prefix, *rows]) + "\n"


def _split_plan(text: str) -> tuple[str, list[str], str]:
    """(before, task-board lines, after) for PROJECT_PLAN.md; board lines exclude the heading."""
    heading = "## Task Board"
    start = text.find(heading)
    if start < 0:
        return text, [], ""
    content_start = start + len(heading)
    rest = text[content_start:]
    nxt = re.search(r"^##\s+", rest, flags=re.M)
    section = rest[: nxt.start()] if nxt else rest
    after = rest[nxt.start():] if nxt else ""
    return text[:content_start], [l for l in section.splitlines() if l.strip()], after


def _git_merge_file(base: str, ours: str, theirs: str) -> tuple[str, bool]:
    """Three-way text merge via `git merge-file -p`; returns (text, conflicted)."""
    with tempfile.TemporaryDirectory(prefix="awk-merge-") as tmp:
        paths = []
        for name, text in (("ours", ours), ("base", base), ("theirs", theirs)):
            path = Path(tmp) / name
            path.write_text(text, encoding="utf-8")
            paths.append(str(path))
        proc = subprocess.run(
            ["git", "merge-file", "-p", "-L", "ours", "-L", "base", "-L", "theirs", *paths],
            text=True, capture_output=True, timeout=60,
        )
    # merge-file exits with the number of conflicts (negative on error)
    return proc.stdout, proc.returncode != 0


def _merge_project_plan(base_text: str, ours_text: str, theirs_text: str) -> tuple[str, bool]:
    b_before, b_rows, b_after = _split_plan(base_text)
    o_before, o_rows, o_after = _split_plan(ours_text)
    t_before, t_rows, t_after = _split_plan(theirs_text)
    before, conflict_before = _git_merge_file(b_before, o_before, t_before)
    after, conflict_after = _git_merge_file(b_after, o_after, t_after)
    _prefix, rows = _merge_keyed_lines(b_rows, o_rows, t_rows, _PLAN_ROW_RE, _better_plan_row)
    if not before.endswith("\n"):
        before += "\n"
    body = before + "\n".join(rows) + ("\n\n" if rows else "\n") + after.lstrip("\n")
    return body, conflict_before or conflict_after


def _git_run(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(root), *args], text=True, capture_output=True, timeout=300,
    )


def cmd_sync(args: argparse.Namespace) -> int:
    """Publish this checkout's ledger and pick up everyone else's.

    Stages only `.agent/`, commits with a `Refs:` trailer for the active
    task, makes sure the ledger merge driver is registered, pulls with
    rebase, re-renders the views if the merge left them behind the board,
    and pushes. Code changes are never swept in: a staged path outside
    `.agent/` is refused so an unreviewed edit cannot ride along with a
    claim.
    """
    root = _repo_root()
    if not (root / ".git").exists():
        print("agentctl: sync needs a Git checkout", file=sys.stderr)
        return 2
    session = _require_session(root)
    task = str(session.get("task") or "")
    staged = [
        line.strip() for line in _git(root, "diff", "--cached", "--name-only").splitlines()
        if line.strip()
    ]
    outside = [path for path in staged if not _is_ledger_data_path(path)]
    if outside:
        print(
            "agentctl: sync commits ledger data only; unstage these first: "
            + ", ".join(outside[:5]) + (" ..." if len(outside) > 5 else ""),
            file=sys.stderr,
        )
        return 1
    if not _ledger_merge_driver_configured(root):
        _configure_ledger_merge_driver(root)
    attributes = _read(root / ".gitattributes")
    if GITATTRIBUTES_MANAGED_ENTRIES[0] not in attributes:
        print(
            "agentctl: WARNING .gitattributes does not route ledger files to the merge "
            "driver; run 'agentctl init .' and commit .gitattributes so concurrent "
            "ledger changes merge instead of conflicting",
            file=sys.stderr,
        )
    git_dir = _git_dir(root)
    if git_dir is not None and ((git_dir / "rebase-merge").exists() or (git_dir / "rebase-apply").exists()):
        print(
            "agentctl: sync stopped: a rebase is in progress in this checkout; resolve it "
            "('git status' lists the files), then 'git rebase --continue' or 'git rebase --abort'",
            file=sys.stderr,
        )
        return 1
    branch = args.branch or _git(root, "rev-parse", "--abbrev-ref", "HEAD").strip()
    if not branch or branch == "HEAD":
        print("agentctl: sync needs a branch checked out (detached HEAD)", file=sys.stderr)
        return 1

    def commit_ledger(subject: str) -> tuple[bool, str]:
        # Stage ledger data only (see LEDGER_DATA_PATHS). Loop contracts,
        # rules, evals, and policy files under .agent/ change behavior and
        # travel with reviewed work, never with a claim.
        data_paths = [entry.rstrip("/") for entry in LEDGER_DATA_PATHS if (root / entry.rstrip("/")).exists()]
        if data_paths:
            add = _git_run(root, "add", "-A", "--", *data_paths)
            if add.returncode:
                return False, add.stderr.strip() or add.stdout.strip()
        skipped = [
            line[3:].strip() for line in _git(root, "status", "--porcelain", "--", WORKFLOW_DIR).splitlines()
            if line[:2].strip() and not _is_ledger_data_path(line[3:].strip())
        ]
        if skipped:
            print(
                "agentctl: sync left these .agent/ changes unstaged because they are not ledger "
                "data (commit them with reviewed work): " + ", ".join(sorted(skipped)[:6])
                + (" ..." if len(skipped) > 6 else ""),
                file=sys.stderr,
            )
        pending = [
            line.strip() for line in _git(root, "diff", "--cached", "--name-only").splitlines()
            if line.strip()
        ]
        if not pending:
            return False, ""
        touched = sorted({
            match.group(1) for path in pending
            for match in [re.search(rf"^{WORKFLOW_DIR}/tasks/([^/]+)\.md$", path)] if match
        })
        refs = [tid for tid in [task, *touched] if tid]
        if not refs:
            return False, (
                "no task id to reference: start or resume a task with 'agentctl work' first"
            )
        body = f"{subject}\n\nRefs: {', '.join(dict.fromkeys(refs))}\n"
        commit = _git_run(root, "commit", "-q", "-m", body)
        if commit.returncode:
            return False, commit.stderr.strip() or commit.stdout.strip()
        return True, ""

    committed, error = commit_ledger(args.message or f"chore(ledger): sync {task or 'ledger'}")
    if error:
        print(f"agentctl: sync could not commit the ledger: {error}", file=sys.stderr)
        return 1
    if committed:
        print(f"agentctl: sync committed ledger changes ({_git(root, 'rev-parse', '--short', 'HEAD')})")
    else:
        print("agentctl: sync found no ledger changes to commit")

    pull = _git_run(root, "pull", "--rebase", "--quiet", args.remote, branch)
    if pull.returncode:
        conflicted = [
            line.strip() for line in _git(root, "diff", "--name-only", "--diff-filter=U").splitlines()
            if line.strip()
        ]
        print(
            f"agentctl: sync stopped: 'git pull --rebase {args.remote} {branch}' failed",
            file=sys.stderr,
        )
        if conflicted:
            print(
                "  conflicts in: " + ", ".join(conflicted) + "; hand-written text needs a human, "
                "then 'git add' those files and 'git rebase --continue'",
                file=sys.stderr,
            )
        else:
            print("  " + (pull.stderr.strip() or pull.stdout.strip()), file=sys.stderr)
        return 1
    print(f"agentctl: sync pulled {args.remote}/{branch}")

    # A merge that combined two boards can leave the rendered views a step
    # behind (ordering, a title edited on one side); the board is canonical.
    if _check_board_consistency(root):
        _render_task_views(root, _load_board(root))
        committed_views, error = commit_ledger("chore(ledger): re-render views after merge")
        if error:
            print(f"agentctl: sync could not commit re-rendered views: {error}", file=sys.stderr)
            return 1
        if committed_views:
            print("agentctl: sync re-rendered task views from the merged board")

    if args.no_push:
        return 0
    push = _git_run(root, "push", "--quiet", args.remote, f"HEAD:{branch}")
    if push.returncode:
        print(f"agentctl: sync stopped: push to {args.remote}/{branch} failed", file=sys.stderr)
        print("  " + (push.stderr.strip() or push.stdout.strip()), file=sys.stderr)
        return 1
    print(f"agentctl: sync pushed {branch} to {args.remote}")
    return 0


def _merge_ledger_file(name: str, base: str, ours: str, theirs: str) -> tuple[str, bool]:
    """Dispatch on the ledger file name; returns (merged text, conflicted).

    Raises ValueError when a JSON side cannot be parsed.
    """
    if name == "board.json":
        return _merge_ledger_json(base, ours, theirs, "tasks", _resolve_board_entry), False
    if name == "agents.json":
        return _merge_ledger_json(base, ours, theirs, "agents", lambda _k, o, _t: o), False
    if name == "TASKS.md":
        return _merge_task_index(base, ours, theirs), False
    if name == "PROJECT_PLAN.md":
        return _merge_project_plan(base, ours, theirs)
    if name == "state.json":
        return ours, False  # loop runtime bookkeeping belongs to this checkout
    return _git_merge_file(base, ours, theirs)


def cmd_merge_driver(args: argparse.Namespace) -> int:
    """Git merge driver entry point: %O %A %B %P -> merged result written to %A."""
    base_text = _read(Path(args.base))
    ours_text = _read(Path(args.ours))
    theirs_text = _read(Path(args.theirs))
    try:
        merged, conflicted = _merge_ledger_file(Path(args.path).name, base_text, ours_text, theirs_text)
    except ValueError as exc:
        # Leave ours in place and let git record the conflict; a damaged
        # ledger needs a human, not a guess.
        print(f"agentctl merge-driver: {args.path} not merged: {exc}", file=sys.stderr)
        return 1
    Path(args.ours).write_text(merged, encoding="utf-8")
    if conflicted:
        print(f"agentctl merge-driver: {args.path} merged with conflicts in hand-written text", file=sys.stderr)
        return 1
    return 0


def _state_dir(root: Path) -> Path:
    return root / WORKFLOW_DIR / STATE_DIR


def _private_identity_key(prefix: str, value: str, length: int = 24) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        return ""
    if re.fullmatch(rf"{re.escape(prefix)}-[0-9a-f]{{{length}}}", normalized):
        return normalized
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:length]
    return f"{prefix}-{digest}"


def _workflow_session_instance_key() -> str:
    return _private_identity_key("instance", os.environ.get(SESSION_INSTANCE_ENV, ""))


def _workflow_parent_session_key() -> str:
    explicit = _private_identity_key(
        "lineage", os.environ.get(PARENT_SESSION_KEY_ENV, ""),
    )
    if explicit:
        return explicit
    return _private_identity_key(
        "lineage", os.environ.get("WHALENT_FORK_SOURCE_AGENT_ID", ""),
    )


def _workflow_session_isolation_error() -> str:
    inherited = (os.environ.get(SESSION_ISOLATION_ERROR_ENV) or "").strip()
    if inherited:
        return inherited
    if not _workflow_parent_session_key() or _workflow_session_instance_key():
        return ""
    forced = (os.environ.get("AGENT_WORKFLOW_SESSION_KEY") or "").strip()
    inherited_session = bool(
        (os.environ.get("AGENT_WORKFLOW_SESSION_ID") or "").strip()
        or forced == "default"
        or re.fullmatch(r"session-[0-9a-f]{24}", forced)
    )
    if not inherited_session:
        return ""
    return (
        "forked conversation instance is missing or untrusted; refusing to use "
        "the inherited parent workflow session until SessionStart establishes "
        "an isolated instance"
    )


def _workflow_session_identity_source() -> str:
    forced = (os.environ.get("AGENT_WORKFLOW_SESSION_KEY") or "").strip()
    if forced == "default":
        return "default"
    if re.fullmatch(r"session-[0-9a-f]{24}", forced):
        return "forced_key"

    explicit = (os.environ.get(SESSION_ID_ENV) or "").strip()
    runtime = _runtime_identity()
    owner_runtime = (os.environ.get(SESSION_OWNER_RUNTIME_ENV) or "").strip()
    if explicit and runtime and owner_runtime and owner_runtime != runtime:
        source = "host_runtime"
    elif explicit:
        source = "session_start"
    elif runtime:
        source = "host_runtime"
    elif (os.environ.get("WHALENT_COMPOSER_ID") or "").strip():
        source = "whalent_composer"
    elif (os.environ.get("AGENT_SESSION_ID") or "").strip():
        source = "agent_session"
    elif (os.environ.get("TERM_SESSION_ID") or "").strip():
        source = "terminal"
    else:
        source = "default"
    if _workflow_session_instance_key():
        return "session_instance"
    return source


def _workflow_session_identity_error() -> str:
    isolation_error = _workflow_session_isolation_error()
    if isolation_error:
        return isolation_error
    source = _workflow_session_identity_source()
    if source not in {"terminal", "default"}:
        return ""
    if source == "terminal":
        detail = "TERM_SESSION_ID is terminal-scoped and may be shared by multiple agents"
    else:
        detail = "the client supplied no conversation, runtime, or SessionStart identity"
    return (
        "unique conversation identity is unavailable: " + detail + "; refusing "
        "to load or mutate task-session state. Restart this conversation so the "
        "project SessionStart hook runs, then retry"
    )


def _agentctl_command_path(args: argparse.Namespace) -> tuple[str, ...]:
    command = str(getattr(args, "cmd", "") or "")
    action_attr = COMMAND_ACTION_ATTRS.get(command)
    if action_attr:
        return command, str(getattr(args, action_attr, "") or "")
    return (command,)


def _command_requires_trusted_identity(args: argparse.Namespace) -> bool:
    return _agentctl_command_path(args) not in IDENTITY_FREE_COMMAND_PATHS


def _workflow_session_key() -> str:
    forced = (os.environ.get("AGENT_WORKFLOW_SESSION_KEY") or "").strip()
    if forced == "default" or re.fullmatch(r"session-[0-9a-f]{24}", forced):
        return forced
    explicit = (os.environ.get("AGENT_WORKFLOW_SESSION_ID") or "").strip()
    runtime = _runtime_identity()
    owner_runtime = (os.environ.get(SESSION_OWNER_RUNTIME_ENV) or "").strip()
    inherited_explicit = bool(
        explicit
        and runtime
        and owner_runtime
        and owner_runtime != runtime
    )
    identity = runtime if inherited_explicit else (explicit or runtime)
    if not identity:
        for name in ("WHALENT_COMPOSER_ID", "AGENT_SESSION_ID", "TERM_SESSION_ID"):
            value = (os.environ.get(name) or "").strip()
            if value:
                identity = f"{name}={value}"
                break
    instance_key = _workflow_session_instance_key()
    if instance_key:
        identity = f"{identity or 'fork'}\n{SESSION_INSTANCE_ENV}={instance_key}"
    if not identity:
        return "default"
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
    return f"session-{digest}"


def _session_runtime_dir(root: Path) -> Path:
    common = _git_common_dir(root)
    if common is not None:
        return common / WORKTREE_LEASES_DIR / SESSION_RUNTIME_DIR
    return _state_dir(root) / SESSION_RUNTIME_DIR


def _session_coordination_lock_path(root: Path) -> Path:
    common = _git_common_dir(root)
    if common is not None:
        return common / WORKTREE_LEASES_DIR / SESSION_COORDINATION_LOCK
    return _state_dir(root) / LOCKS_DIR / SESSION_COORDINATION_LOCK


def _checkout_fingerprint(root: Path) -> str:
    return hashlib.sha256(str(root.resolve()).encode("utf-8")).hexdigest()[:12]


def _session_path(root: Path, session_key: str | None = None) -> Path:
    key = session_key or _workflow_session_key()
    if key == "default":
        return _state_dir(root) / SESSION_FILE
    return _session_runtime_dir(root) / f"{key}-{_checkout_fingerprint(root)}.json"


def _legacy_shared_session_path(root: Path, session_key: str) -> Path:
    return _session_runtime_dir(root) / f"{session_key}.json"


def _session_matches_current_runtime(st: dict, session_key: str) -> bool:
    if st.get("workflow_session_key") == session_key:
        return True
    runtime = _runtime_identity()
    return bool(runtime and runtime in (st.get("runtime_identities") or []))


def _load_session(root: Path) -> dict:
    if _workflow_session_identity_error():
        return {}
    key = _workflow_session_key()
    path = _session_path(root, key)
    st = _load_json(path, {})
    if st or key == "default":
        return st if isinstance(st, dict) else {}
    shared_legacy_path = _legacy_shared_session_path(root, key)
    shared_legacy = _load_json(shared_legacy_path, {})
    if isinstance(shared_legacy, dict) and shared_legacy.get("task"):
        legacy_checkout = shared_legacy.get("checkout")
        if ((legacy_checkout and _same_checkout(root, shared_legacy))
                or (not legacy_checkout and _session_matches_current_runtime(shared_legacy, key))):
            shared_legacy["workflow_session_key"] = key
            _save_session(root, shared_legacy)
            try:
                shared_legacy_path.unlink()
            except FileNotFoundError:
                pass
            _render_sessions_view(root)
            return shared_legacy
    legacy = _load_json(_session_path(root, "default"), {})
    if not isinstance(legacy, dict) or not legacy.get("task"):
        return {}
    if not _session_matches_current_runtime(legacy, key):
        return {}
    legacy["workflow_session_key"] = key
    _save_session(root, legacy)
    try:
        _session_path(root, "default").unlink()
    except FileNotFoundError:
        pass
    _render_sessions_view(root)
    return legacy


def _save_session(root: Path, st: dict) -> None:
    key = st.get("workflow_session_key") or _workflow_session_key()
    st["workflow_session_key"] = key
    parent_key = _workflow_parent_session_key()
    if parent_key:
        st["parent_session_key"] = parent_key
    instance_key = _workflow_session_instance_key()
    if instance_key:
        st["session_instance_key"] = instance_key
    st["identity_source"] = _workflow_session_identity_source()
    st["checkout"] = str(root.resolve())
    st["branch"] = _git(root, "branch", "--show-current")
    st["heartbeat_at"] = _now()
    st["heartbeat_ns"] = time.time_ns()
    st["revision"] = int(st.get("revision") or 0) + 1
    if st.get("status") in {"review", "approved", "done", "released"}:
        st["presence_status"] = st.get("status")
    else:
        st["presence_status"] = "working"
    _save_json(_session_path(root, key), st)
    _render_sessions_view(root)


def _clear_session(root: Path) -> None:
    p = _session_path(root)
    if p.is_file():
        p.unlink()
    _render_sessions_view(root)


def _session_age_seconds(st: dict) -> float | None:
    try:
        heartbeat_ns = int(st.get("heartbeat_ns") or 0)
    except (TypeError, ValueError, OverflowError):
        return None
    if heartbeat_ns <= 0:
        return None
    return max(0.0, (time.time_ns() - heartbeat_ns) / 1_000_000_000)


def _session_observed_status(st: dict) -> str:
    presence = st.get("presence_status") or st.get("status") or "working"
    if presence in {"review", "approved", "done", "released"}:
        return presence
    age = _session_age_seconds(st)
    if age is None or age > SESSION_STALE_SECONDS:
        return "stale"
    return "active"


def _session_checkout_is_orphaned(st: dict) -> bool:
    if _session_observed_status(st) != "stale":
        return False
    checkout = str(st.get("checkout") or "").strip()
    if not checkout:
        return False
    path = Path(checkout)
    if not path.is_absolute():
        return False
    try:
        path.stat()
    except (FileNotFoundError, NotADirectoryError):
        return True
    except OSError:
        # Permission and transient filesystem failures remain fail-closed.
        return False
    return False


def _session_rows_unlocked(root: Path) -> list[dict]:
    rows: list[dict] = []
    seen: set[tuple[str, str]] = set()
    paths = sorted(_session_runtime_dir(root).glob("session-*.json"))
    legacy = _session_path(root, "default")
    if legacy.is_file():
        paths.append(legacy)
    for path in paths:
        st = _load_json(path, {})
        if not isinstance(st, dict) or not st.get("task"):
            continue
        row = dict(st)
        key = row.get("workflow_session_key") or (
            "default" if path == legacy else path.stem
        )
        checkout = str(row.get("checkout") or root.resolve())
        identity = (str(key), checkout)
        if identity in seen:
            continue
        seen.add(identity)
        row["workflow_session_key"] = key
        observed_status = _session_observed_status(row)
        if observed_status == "stale" and _session_checkout_is_orphaned(row):
            observed_status = "orphaned"
        row["observed_status"] = observed_status
        age = _session_age_seconds(row)
        row["heartbeat_age_seconds"] = None if age is None else round(age, 3)
        row["_record_path"] = str(path)
        rows.append(row)
    rows.sort(key=lambda row: (
        str(row.get("checkout") or ""), str(row.get("task") or ""),
        str(row.get("workflow_session_key") or ""),
    ))
    return rows


def _public_session_row(row: dict) -> dict:
    return {key: value for key, value in row.items() if not key.startswith("_")}


def _is_kit_source_checkout(root: Path) -> bool:
    return (root / "templates" / "project").is_dir() and _kit_root() == root


def _installed_protocol_epoch(root: Path) -> int:
    manifest = _load_json(root / WORKFLOW_DIR / INSTALL_MANIFEST_FILE, {})
    if isinstance(manifest, dict) and manifest:
        try:
            return int(manifest.get("protocol_epoch") or LEGACY_PROTOCOL_EPOCH)
        except (TypeError, ValueError, OverflowError):
            return LEGACY_PROTOCOL_EPOCH
    if _is_kit_source_checkout(root):
        return PROTOCOL_EPOCH
    return LEGACY_PROTOCOL_EPOCH


def _session_protocol_epoch(root: Path, session: dict) -> int:
    try:
        return int(session.get("protocol_epoch") or (
            PROTOCOL_EPOCH if _is_kit_source_checkout(root) else LEGACY_PROTOCOL_EPOCH
        ))
    except (TypeError, ValueError, OverflowError):
        return LEGACY_PROTOCOL_EPOCH


def _upgrade_state_path(root: Path) -> Path:
    common = _git_common_dir(root)
    if common is not None:
        return common / WORKTREE_LEASES_DIR / UPGRADE_STATE_FILE
    return root / WORKFLOW_DIR / STATE_DIR / UPGRADE_STATE_FILE


def _load_upgrade_state(root: Path) -> dict:
    installed = _installed_protocol_epoch(root)
    state = _load_json(_upgrade_state_path(root), {})
    if not isinstance(state, dict) or not state:
        return {
            "state": "steady",
            "installed_epoch": installed,
            "target_epoch": installed,
        }
    state.setdefault("state", "steady")
    state.setdefault("installed_epoch", installed)
    state.setdefault("target_epoch", installed)
    return state


def _upgrade_blocking_sessions(root: Path) -> list[dict]:
    return [
        row for row in _session_rows_unlocked(root)
        if row.get("observed_status") in {"active", "stale"}
    ]


_UPGRADE_DRAIN_ALLOWED = frozenset({
    ("note",),
    ("complete",),
    ("finish",),
    ("refresh",),
    ("focus",),
    ("capsule",),
    ("board",),
    ("task", "show"),
    ("reconcile", "check"),
    ("lease", "list"),
    ("run", "list"),
    ("run", "show"),
    ("run", "wait"),
    ("run", "finish"),
    ("run", "stop"),
    ("_run-supervise",),
    ("resource", "status"),
    ("resource", "release"),
    ("worktree", "list"),
    ("worktree", "release"),
    ("loop", "status"),
    ("loop", "stop"),
    ("check",),
    ("doctor",),
    ("merge-driver",),
    ("migrate",),
    ("sessions", "list"),
    ("sessions", "heartbeat"),
    ("sessions", "release"),
    ("status",),
    ("upgrade", "begin"),
    ("upgrade", "status"),
    ("upgrade", "validate"),
    ("upgrade", "complete"),
    ("upgrade", "rebind"),
})
_PROTOCOL_MISMATCH_ALLOWED = frozenset({
    ("focus",),
    ("capsule",),
    ("board",),
    ("task", "show"),
    ("lease", "list"),
    ("run", "list"),
    ("run", "show"),
    ("run", "wait"),
    ("run", "finish"),
    ("run", "stop"),
    ("_run-supervise",),
    ("resource", "status"),
    ("resource", "release"),
    ("worktree", "list"),
    ("worktree", "release"),
    ("loop", "status"),
    ("loop", "stop"),
    ("check",),
    ("doctor",),
    ("merge-driver",),
    ("migrate",),
    ("sessions", "list"),
    ("sessions", "release"),
    ("status",),
    ("upgrade", "status"),
    ("upgrade", "validate"),
    ("upgrade", "rebind"),
})


def _upgrade_command_error(root: Path, args: argparse.Namespace) -> str:
    path = _agentctl_command_path(args)
    if path in {("init",), ("upgrade", "status"), ("upgrade", "validate"),
                ("upgrade", "begin"), ("upgrade", "complete"),
                ("upgrade", "rebind")}:
        return ""
    state = _load_upgrade_state(root)
    if state.get("state") in {"draining", "validating"} and path not in _UPGRADE_DRAIN_ALLOWED:
        return (
            f"workflow upgrade is {state.get('state')} "
            f"(epoch {state.get('installed_epoch')} -> {state.get('target_epoch')}); "
            "new work and repository writes are blocked until active sessions release "
            "and `agentctl upgrade complete` succeeds"
        )
    _key, session, _source, _record_path, _identity_error = (
        _migration_current_record(root)
    )
    if not session.get("task"):
        return ""
    installed = _installed_protocol_epoch(root)
    observed = _session_protocol_epoch(root, session)
    if observed != installed and path not in _PROTOCOL_MISMATCH_ALLOWED:
        return (
            f"this conversation is bound to workflow protocol epoch {observed}, "
            f"but the checkout uses epoch {installed}; re-read the plan and task "
            "document, then run `python3 tools/agentctl.py upgrade rebind`"
        )
    return ""


def _write_atomic_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.parent / f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp"
    try:
        with temp.open("w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


def _render_sessions_view(root: Path, rows: list[dict] | None = None) -> None:
    rows = rows if rows is not None else _session_rows_unlocked(root)
    lines = [
        "# Agent Sessions",
        "",
        f"Generated: {_now()}",
        "",
        "This local, gitignored view is generated from per-conversation records. ",
        "Do not edit it; use the task documents for durable project decisions.",
        "",
        "| Session | Fork lineage | State | Agent | Task | Scope | Branch | Checkout | Heartbeat |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for row in rows:
        values = [
            row.get("workflow_session_key") or "default",
            row.get("parent_session_key") or "-",
            row.get("observed_status") or "unknown",
            row.get("agent") or "-",
            row.get("task") or "-",
            ", ".join(row.get("scope") or []) or "-",
            row.get("branch") or "-",
            row.get("checkout") or "-",
            row.get("heartbeat_at") or "-",
        ]
        escaped = [str(value).replace("|", "\\|").replace("\n", " ") for value in values]
        lines.append("| " + " | ".join(escaped) + " |")
    if not rows:
        lines.append("| - | - | none | - | - | - | - | - | - |")
    lines.extend([
        "",
        "States `active` and `stale` keep their task/file claims. A stale claim must be ",
        "inspected and explicitly released before overlapping work starts.",
        "",
    ])
    _write_atomic_text(_state_dir(root) / SESSION_VIEW_FILE, "\n".join(lines))


def _require_session(root: Path) -> dict:
    st = _load_session(root)
    if not st.get("task"):
        print("agentctl: no active task. run 'agentctl work --agent <name>' first.", file=sys.stderr)
        sys.exit(3)
    return st


def _board_path(root: Path) -> Path:
    return root / WORKFLOW_DIR / BOARD_FILE


def _load_board(root: Path) -> dict:
    return _load_json(_board_path(root), {"version": 1, "updated_at": "", "tasks": {}})


def _save_board(root: Path, board: dict) -> None:
    board["updated_at"] = _now()
    _save_json(_board_path(root), board)


def _agents_path(root: Path) -> Path:
    return root / WORKFLOW_DIR / AGENTS_FILE


def _adoption_path(root: Path) -> Path:
    return root / WORKFLOW_DIR / ADOPTION_FILE


def _install_manifest_path(root: Path) -> Path:
    return root / WORKFLOW_DIR / INSTALL_MANIFEST_FILE


def _save_upgrade_state(root: Path, state: dict) -> None:
    state["updated_at"] = _now()
    _save_json(_upgrade_state_path(root), state)


def _upgrade_barrier_plan(root: Path, kit: Path,
                          force_managed: bool) -> list[tuple[str, Path, str]]:
    manifest = _load_json(_install_manifest_path(root), {})
    old_hashes = manifest.get("managed_files") or {}
    plan = []
    for rel in ("tools/agentctl.py", "tools/agent_workflow_hook.py"):
        source = kit / rel
        if not source.is_file():
            raise ValueError(f"upgrade barrier source is missing: {rel}")
        destination = root / rel
        desired = _read(source)
        desired_hash = hashlib.sha256(desired.encode("utf-8")).hexdigest()
        current = _read(destination)
        current_hash = hashlib.sha256(current.encode("utf-8")).hexdigest()
        recorded_hash = str(old_hashes.get(rel) or "")
        if (destination.exists() and current_hash != desired_hash
                and recorded_hash and current_hash != recorded_hash
                and not force_managed):
            raise ValueError(
                f"{rel} differs from its recorded managed version; inspect it "
                "or rerun init with --force-managed before upgrading"
            )
        plan.append((rel, destination, desired))
    return plan


def _bootstrap_upgrade_barrier(
        plan: list[tuple[str, Path, str]]) -> list[str]:
    installed = []
    for rel, destination, desired in plan:
        if _read(destination) != desired:
            _write_atomic_text(destination, desired)
        try:
            os.chmod(destination, 0o755)
        except OSError:
            pass
        installed.append(rel)
    return installed


def _prepare_install_upgrade(root: Path, kit: Path,
                             force_managed: bool = False) -> tuple[dict | None, list[dict]]:
    manifest_path = _install_manifest_path(root)
    installed_before = (
        manifest_path.is_file()
        or (root / "tools" / "agentctl.py").is_file()
        or (root / WORKFLOW_DIR / PLAN_FILE).is_file()
    )
    if not installed_before or _is_kit_source_checkout(root):
        return None, []
    from_epoch = _installed_protocol_epoch(root)
    if from_epoch > PROTOCOL_EPOCH:
        raise ValueError(
            f"refusing workflow downgrade from protocol epoch {from_epoch} "
            f"to {PROTOCOL_EPOCH}"
        )
    if from_epoch == PROTOCOL_EPOCH:
        return None, []
    state = {
        "state": "draining",
        "installed_epoch": from_epoch,
        "target_epoch": PROTOCOL_EPOCH,
        "target_version": KIT_VERSION,
        "started_at": _now(),
        "initiated_by": "agentctl init",
    }
    blockers = _upgrade_blocking_sessions(root)
    state["blocking_sessions"] = [
        {
            "session": row.get("workflow_session_key"),
            "task": row.get("task"),
            "status": row.get("observed_status"),
            "checkout": row.get("checkout"),
        }
        for row in blockers
    ]
    if not blockers:
        state["state"] = "validating"
        state["drained_at"] = _now()
    barrier_plan = _upgrade_barrier_plan(root, kit, force_managed)
    _save_upgrade_state(root, state)
    state["barrier_entrypoints"] = _bootstrap_upgrade_barrier(barrier_plan)
    state["barrier_bootstrapped_at"] = _now()
    _save_upgrade_state(root, state)
    return state, blockers


def _finish_install_upgrade(root: Path, context: dict | None) -> None:
    if context is None:
        return
    _save_upgrade_state(root, {
        "state": "steady",
        "installed_epoch": PROTOCOL_EPOCH,
        "target_epoch": PROTOCOL_EPOCH,
        "target_version": KIT_VERSION,
        "started_at": context.get("started_at"),
        "completed_at": _now(),
        "initiated_by": context.get("initiated_by") or "agentctl init",
    })


def _load_agents(root: Path) -> dict:
    return _load_json(_agents_path(root), {"version": 1, "agents": {}})


def _save_agents(root: Path, data: dict) -> None:
    _save_json(_agents_path(root), data)


def _agent_profile(root: Path, agent: str | None) -> dict:
    if not agent:
        return {}
    return (_load_agents(root).get("agents") or {}).get(agent) or {}


def _resolve_worker_metadata(root: Path, agent: str | None, args: argparse.Namespace | None = None) -> dict:
    profile = _agent_profile(root, agent)
    session_id = ""
    model = ""
    reasoning_effort = ""
    if args is not None:
        session_id = getattr(args, "session_id", "") or ""
        model = getattr(args, "model", "") or ""
        reasoning_effort = getattr(args, "reasoning_effort", "") or ""
    return {
        "session_id": session_id or os.environ.get("AGENT_SESSION_ID", "") or profile.get("session_id", "") or "",
        "model": model or os.environ.get("AGENT_MODEL", "") or profile.get("model", "") or "",
        "reasoning_effort": (
            reasoning_effort
            or os.environ.get("AGENT_REASONING_EFFORT", "")
            or profile.get("reasoning_effort", "")
            or ""
        ),
    }


def _runtime_identity() -> str:
    """Fingerprint host-issued agent/session identifiers without persisting raw IDs."""
    values = []
    for name in (
        "CODEX_THREAD_ID",
        "CLAUDE_CODE_SESSION_ID",
        "CURSOR_CONVERSATION_ID",
        "WHALENT_AGENT_ID",
        "WHALENT_CODEX_INSTANCE_ID",
    ):
        value = (os.environ.get(name) or "").strip()
        if value:
            values.append(f"{name}={value}")
    if not values:
        return ""
    digest = hashlib.sha256("\n".join(values).encode("utf-8")).hexdigest()
    return f"host-runtime:{digest[:32]}"


def _record_runtime_identity(st: dict) -> str:
    current = _runtime_identity()
    identities = st.get("runtime_identities")
    if not isinstance(identities, list):
        identities = []
    identities = [str(item) for item in identities if str(item).strip()]
    if current and current not in identities:
        identities.append(current)
    st["runtime_identity"] = current
    st["runtime_identities"] = identities
    return current


def _record_adoption_baseline(root: Path) -> None:
    """Record the pre-install HEAD so old history is not retroactively gated."""
    path = _adoption_path(root)
    if path.is_file():
        return
    head = _git(root, "rev-parse", "HEAD").strip()
    if not head:
        return
    _save_json(path, {
        "version": 1,
        "created_at": _now(),
        "ignore_commits_through": head,
        "policy": "pre-push checks apply only to commits after this adoption baseline",
    })


def _bus_dir(root: Path, kind: str) -> Path:
    return root / WORKFLOW_DIR / BUS_DIR / kind


def _loops_dir(root: Path) -> Path:
    return root / WORKFLOW_DIR / LOOPS_DIR


def _loop_runs_dir(root: Path) -> Path:
    return _loops_dir(root) / LOOP_RUNS_DIR


def _loop_state_path(root: Path) -> Path:
    return _loops_dir(root) / LOOP_STATE_FILE


def _loop_checkpoints_path(root: Path) -> Path:
    return _loops_dir(root) / LOOP_CHECKPOINTS_FILE


def _loop_state_lock_path(root: Path) -> Path:
    return _state_dir(root) / LOCKS_DIR / "loop-state.lock"


def _lock_path(root: Path, task: str) -> Path:
    return _state_dir(root) / LOCKS_DIR / f"{task}.lock"


def _pid_alive(pid) -> bool:
    try:
        pid = int(pid)
    except (OverflowError, TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.windll.kernel32
            try:
                kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
                kernel32.OpenProcess.restype = wintypes.HANDLE
                kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
                kernel32.WaitForSingleObject.restype = wintypes.DWORD
                kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
                kernel32.CloseHandle.restype = wintypes.BOOL
                kernel32.GetLastError.argtypes = []
                kernel32.GetLastError.restype = wintypes.DWORD
            except (AttributeError, TypeError):
                pass
            synchronize = 0x00100000
            wait_object_0 = 0x00000000
            error_invalid_parameter = 87
            handle = kernel32.OpenProcess(synchronize, False, pid)
            if not handle:
                try:
                    return int(kernel32.GetLastError()) != error_invalid_parameter
                except Exception:
                    return True
            try:
                wait_result = kernel32.WaitForSingleObject(handle, 0)
                if wait_result == wait_object_0:
                    return False
                return True
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            return True
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except ProcessLookupError:
        return False
    except OSError as exc:
        return exc.errno == errno.EPERM
    except (OverflowError, ValueError, TypeError):
        return False
    # kill(pid, 0) succeeds for a zombie: the process has exited but its
    # parent has not reaped it yet. Treat that as dead where the kernel
    # exposes the state, otherwise a supervisor that crashes at startup
    # looks alive for as long as `run start` itself is running.
    return not _posix_process_is_zombie(pid)


def _posix_process_is_zombie(pid: int) -> bool:
    if sys.platform.startswith("linux"):
        try:
            _head, separator, tail = Path(f"/proc/{pid}/stat").read_bytes().rpartition(b")")
            fields = tail.split()
            return bool(separator) and bool(fields) and fields[0] == b"Z"
        except OSError:
            return False
    return False


def _darwin_process_birth_marker(pid: int) -> str | None:
    """Read Darwin's microsecond-resolution process start time via libproc."""
    try:
        import ctypes

        class ProcBsdInfo(ctypes.Structure):
            _fields_ = [
                ("pbi_flags", ctypes.c_uint32),
                ("pbi_status", ctypes.c_uint32),
                ("pbi_xstatus", ctypes.c_uint32),
                ("pbi_pid", ctypes.c_uint32),
                ("pbi_ppid", ctypes.c_uint32),
                ("pbi_uid", ctypes.c_uint32),
                ("pbi_gid", ctypes.c_uint32),
                ("pbi_ruid", ctypes.c_uint32),
                ("pbi_rgid", ctypes.c_uint32),
                ("pbi_svuid", ctypes.c_uint32),
                ("pbi_svgid", ctypes.c_uint32),
                ("rfu_1", ctypes.c_uint32),
                ("pbi_comm", ctypes.c_char * 16),
                ("pbi_name", ctypes.c_char * 32),
                ("pbi_nfiles", ctypes.c_uint32),
                ("pbi_pgid", ctypes.c_uint32),
                ("pbi_pjobc", ctypes.c_uint32),
                ("e_tdev", ctypes.c_uint32),
                ("e_tpgid", ctypes.c_uint32),
                ("pbi_nice", ctypes.c_int32),
                ("pbi_start_tvsec", ctypes.c_uint64),
                ("pbi_start_tvusec", ctypes.c_uint64),
            ]

        libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        libproc.proc_pidinfo.argtypes = [
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_uint64,
            ctypes.c_void_p,
            ctypes.c_int,
        ]
        libproc.proc_pidinfo.restype = ctypes.c_int
        info = ProcBsdInfo()
        size = ctypes.sizeof(info)
        used = libproc.proc_pidinfo(pid, 3, 0, ctypes.byref(info), size)
        if used != size or int(info.pbi_pid) != pid:
            return None
        return f"darwin:{int(info.pbi_start_tvsec)}:{int(info.pbi_start_tvusec)}"
    except Exception:
        return None


def _process_birth_marker(pid) -> str | None:
    """Return a stable marker that distinguishes reused process IDs."""
    try:
        pid = int(pid)
    except (OverflowError, TypeError, ValueError):
        return None
    if pid <= 0:
        return None
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            class FILETIME(ctypes.Structure):
                _fields_ = [("low", wintypes.DWORD), ("high", wintypes.DWORD)]

            kernel32 = ctypes.windll.kernel32
            try:
                kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
                kernel32.OpenProcess.restype = wintypes.HANDLE
                kernel32.GetProcessTimes.argtypes = [
                    wintypes.HANDLE,
                    ctypes.POINTER(FILETIME),
                    ctypes.POINTER(FILETIME),
                    ctypes.POINTER(FILETIME),
                    ctypes.POINTER(FILETIME),
                ]
                kernel32.GetProcessTimes.restype = wintypes.BOOL
                kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
                kernel32.CloseHandle.restype = wintypes.BOOL
            except (AttributeError, TypeError):
                pass
            query_limited_information = 0x00001000
            handle = kernel32.OpenProcess(query_limited_information, False, pid)
            if not handle:
                return None
            try:
                created = FILETIME()
                exited = FILETIME()
                kernel = FILETIME()
                user = FILETIME()
                if not kernel32.GetProcessTimes(
                        handle, ctypes.byref(created), ctypes.byref(exited),
                        ctypes.byref(kernel), ctypes.byref(user)):
                    return None
                ticks = (int(created.high) << 32) | int(created.low)
                return f"windows:{ticks}"
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            return None
    if sys.platform == "darwin":
        return _darwin_process_birth_marker(pid)
    proc_stat = Path(f"/proc/{pid}/stat")
    boot_id_path = Path("/proc/sys/kernel/random/boot_id")
    try:
        if proc_stat.is_file() and boot_id_path.is_file():
            _head, separator, tail = proc_stat.read_bytes().rpartition(b")")
            fields = tail.strip().split()
            boot_id = boot_id_path.read_bytes().strip()
            if separator and len(fields) > 19 and boot_id:
                return f"linux:{boot_id.decode('ascii')}:{fields[19].decode('ascii')}"
    except Exception:
        return None
    return None


def _same_process(pid, expected_birth_marker) -> bool:
    """Match a live PID to its persisted birth marker, conservatively on probe failure."""
    if not _pid_alive(pid):
        return False
    if not expected_birth_marker:
        return True
    current = _process_birth_marker(pid)
    return current is None or current == expected_birth_marker


def _scopes_overlap(a, b) -> bool:
    for x in a:
        for y in b:
            xn = x.strip().strip("/").rstrip("*").rstrip("/")
            yn = y.strip().strip("/").rstrip("*").rstrip("/")
            if not xn or not yn:
                continue
            if xn == yn or xn.startswith(yn + "/") or yn.startswith(xn + "/"):
                return True
    return False


def _same_checkout(root: Path, st: dict) -> bool:
    try:
        return Path(st.get("checkout") or root).resolve() == root.resolve()
    except (OSError, TypeError, ValueError):
        return False


def _blocking_session_rows(root: Path, current_key: str | None = None) -> list[dict]:
    rows = []
    for row in _session_rows_unlocked(root):
        if current_key and row.get("workflow_session_key") == current_key:
            continue
        if not _same_checkout(root, row):
            continue
        if row.get("observed_status") not in {"active", "stale"}:
            continue
        rows.append(row)
    return rows


def _session_start_conflicts(root: Path, session_key: str, task: str,
                             scope: list[str], isolation: str = "shared") -> list[str]:
    conflicts = []
    for row in _session_rows_unlocked(root):
        if row.get("observed_status") not in {"active", "stale"}:
            continue
        other_key = row.get("workflow_session_key") or "default"
        other_task = row.get("task") or "unknown"
        same_checkout = _same_checkout(root, row)
        other_isolation = str(row.get("isolation") or "shared")
        if other_key == session_key:
            if other_task != task:
                conflicts.append(
                    f"this conversation already owns active task {other_task}; finish or release it first"
                )
            elif not same_checkout:
                conflicts.append(
                    f"this conversation already owns task {task} in checkout "
                    f"{row.get('checkout')}; release it before changing checkouts"
                )
            continue
        if other_task == task:
            conflicts.append(
                f"task {task} is already claimed by session {other_key} ({row.get('observed_status')})"
            )
        elif isolation == "exclusive" or other_isolation == "exclusive":
            conflicts.append(
                f"exclusive task ownership conflicts with session {other_key} "
                f"task={other_task} isolation={other_isolation} "
                f"({row.get('observed_status')})"
            )
        elif not same_checkout:
            continue
        elif not scope or not (row.get("scope") or []):
            conflicts.append(
                f"write scopes cannot be proven disjoint from session {other_key} "
                f"task={other_task}; every concurrent task needs a bounded scope"
            )
        elif _scopes_overlap(scope, row.get("scope") or []):
            conflicts.append(
                f"write scope overlaps session {other_key} task={other_task} "
                f"scope={row.get('scope') or []} ({row.get('observed_status')})"
            )
    return conflicts


def _normalize_claim_path(root: Path, value: str) -> tuple[str | None, str | None]:
    raw = str(value or "").strip()
    if not raw:
        return None, None
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        relative = candidate.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return None, f"path is outside the task checkout: {raw}"
    rel = relative.as_posix()
    if rel in {"", "."}:
        return None, f"path must name a file or directory inside the checkout: {raw}"
    return rel, None


def _scope_entry_base(scope_entry: str) -> str:
    return scope_entry.strip().strip("/").rstrip("*").rstrip("/")


def _scope_entry_error(scope_entry: str) -> str | None:
    value = str(scope_entry or "").strip()
    if not value:
        return "scope entries cannot be empty"
    if any(char in value for char in "*?["):
        return (
            f"scope entry '{value}' uses a glob; declare the containing directory "
            "instead so ownership is deterministic"
        )
    normalized = value.replace("\\", "/")
    if normalized.startswith("/") or re.match(r"^[A-Za-z]:/", normalized):
        return f"scope entry '{value}' must be relative to the project checkout"
    parts = [part for part in normalized.strip("/").split("/") if part]
    if not parts or parts == ["."] or ".." in parts:
        return f"scope entry '{value}' must name a bounded project path"
    return None


def _scope_errors(scope: list[str]) -> list[str]:
    return [
        error for error in (_scope_entry_error(item) for item in scope) if error
    ]


def _dir_within_scope_entry(path: str, scope_entry: str) -> bool:
    """True only when `path` is the scope entry or a descendant of it.

    Directional containment: unlike `_scopes_overlap`, an ANCESTOR of the
    scope entry is not "in scope". Writing `src` or `src/*` when the task
    scope is `src/one/` must be rejected because it also reaches sibling
    `src/two/`.
    """
    p = path.strip().strip("/")
    s = _scope_entry_base(scope_entry)
    if not p or not s:
        return False
    return p == s or p.startswith(s + "/")


def _path_in_scope(path: str, scope: list[str]) -> bool:
    """Whether a single write target is fully contained in the task scope.

    A glob only stays in scope when the directory being expanded is itself
    inside a scope entry, so every possible match lands in scope too. A
    non-glob path must be the scope entry or a descendant.
    """
    raw = str(path).strip()
    star = raw.find("*")
    if star != -1:
        prefix = raw[:star]
        slash = prefix.rfind("/")
        globbed_dir = prefix[:slash] if slash != -1 else ""
        if not globbed_dir:
            # Bare glob at the checkout root (e.g. `*` or `src*`) can escape.
            return False
        return any(_dir_within_scope_entry(globbed_dir, item) for item in scope)
    return any(_dir_within_scope_entry(raw, item) for item in scope)


def _controller_owned_paths() -> tuple[tuple[str, str], ...]:
    """Workflow files agents must never edit directly, with the owning command."""
    return (
        (f"{WORKFLOW_DIR}/{BOARD_FILE}", "agentctl task/work/finish"),
        (f"{WORKFLOW_DIR}/{TASKS_FILE}", "agentctl task/work/finish"),
        (f"{WORKFLOW_DIR}/{AGENTS_FILE}", "agentctl agents"),
        (f"{WORKFLOW_DIR}/{STATE_DIR}/", "agentctl sessions/status"),
        (f"{WORKFLOW_DIR}/{LOG_DIR}/{PROGRESS_LOG}", "agentctl note/progress"),
        (f"{WORKFLOW_DIR}/{GATES_DIR}/", "agentctl gate"),
        (f"{WORKFLOW_DIR}/{LOOPS_DIR}/{LOOP_STATE_FILE}", "agentctl loop"),
        (f"{WORKFLOW_DIR}/{LOOPS_DIR}/{LOOP_RUNS_DIR}/", "agentctl loop"),
        (f"{WORKFLOW_DIR}/{BUS_DIR}/", "agentctl guidance"),
        (f"{WORKFLOW_DIR}/handoffs/", "agentctl handoff/guidance"),
        (f"{WORKFLOW_DIR}/{EVALS_DIR}/{EVAL_RUNS_DIR}/", "agentctl eval run"),
        (f"{WORKFLOW_DIR}/{EVALS_DIR}/{EVAL_DECISIONS_DIR}/", "agentctl eval gate"),
        (f"{WORKFLOW_DIR}/{EVALS_DIR}/{EVAL_SIGNING_KEY_FILE}", "agentctl eval"),
        (f"{WORKFLOW_DIR}/{INSTALL_MANIFEST_FILE}", "agentctl init"),
    )


def _controller_owned_claim_error(path: str) -> str | None:
    for owned, command in _controller_owned_paths():
        if owned.endswith("/"):
            if path == owned.rstrip("/") or path.startswith(owned):
                return (
                    f"path {path} is controller-generated; use '{command}' "
                    "instead of editing it directly"
                )
        elif path == owned:
            return (
                f"path {path} is controller-generated; use '{command}' "
                "instead of editing it directly"
            )
    return None


def _session_effective_scope(st: dict) -> list[str]:
    """Declared scope plus the active task's own document.

    Business-scoped workers keep their task doc writable without needing a
    blanket `.agent/` scope; scope-conflict checks still use the declared scope.
    """
    scope = [str(item) for item in (st.get("scope") or []) if str(item)]
    task = st.get("task")
    if task:
        own_doc = f"{WORKFLOW_DIR}/{TASKS_DIR}/{task}.md"
        if own_doc not in scope:
            scope.append(own_doc)
    return scope


def _workspace_contamination(root: Path, rows: list[dict]) -> list[str]:
    """Tracked files modified outside every live session's effective scope.

    Static command inspection cannot enumerate what interpreters or project
    scripts write, so the guard reconciles the working tree instead: a tracked
    modification that no live session scope covers is evidence that a write
    escaped the guards, and further mutating actions stop until it is
    reconciled. Installer-managed dotfiles and workflow documents are exempt;
    untracked files are judged at commit time by the pre-commit scope check.
    """
    status = _git(
        root, "status", "--porcelain", "-z", "--untracked-files=no",
        "--no-renames", "--ignore-submodules=all",
    )
    if not status:
        return []
    scopes: list[list[str]] = [_session_effective_scope(row) for row in rows]
    # Installer/client-managed configuration; upgraded by `init`, not by tasks.
    managed_config_prefixes = (
        ".githooks/", ".github/", ".claude/", ".codex/", ".cursor/", ".vscode/",
    )
    contaminated = []
    for entry in status.split("\0"):
        # `_git` strips leading whitespace from the first entry, so parse the
        # two status letters and separator instead of using fixed offsets.
        match = re.match(r"^\s*[MADRCUT? !]{1,2}\s(.*)$", entry)
        if not match:
            continue
        path = match.group(1).strip().strip('"')
        if not path or path in {"AGENTS.md", ".gitignore"}:
            continue
        if path.startswith(managed_config_prefixes):
            continue
        if path.startswith(f"{WORKFLOW_DIR}/"):
            # Workflow documents are governed by their own layers: controller-
            # generated files deny direct edits, human-owned plan/rule/policy
            # docs are receipt-governed, and task documents are unreachable
            # cross-session because path guards refuse them and opaque writers
            # cannot run beside live peers at all.
            continue
        if any(_path_in_scope(path, scope) for scope in scopes if scope):
            continue
        contaminated.append(path)
    return sorted(set(contaminated))


def _session_awareness(root: Path, current_key: str | None = None) -> str:
    current_key = current_key or _workflow_session_key()
    rows = _blocking_session_rows(root, current_key)
    if not rows:
        return ""
    lines = ["Other active/stale conversations in this checkout:"]
    for row in rows:
        lines.append(
            f"  {row.get('workflow_session_key')} {row.get('observed_status')} "
            f"task={row.get('task')} agent={row.get('agent')} "
            f"scope={','.join(row.get('scope') or []) or '-'}"
        )
    lines.append(f"Live view: {WORKFLOW_DIR}/{STATE_DIR}/{SESSION_VIEW_FILE}")
    return "\n".join(lines)


def _peer_session_snapshot(rows: list[dict]) -> str:
    payload = [
        {
            "session": row.get("workflow_session_key"),
            "state": row.get("observed_status"),
            "task": row.get("task"),
            "agent": row.get("agent"),
            "scope": row.get("scope") or [],
            "claimed_files": row.get("claimed_files") or [],
        }
        for row in rows
    ]
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _doc_hash_targets(root: Path, task: str | None):
    targets = [
        root / "AGENTS.md",
        root / WORKFLOW_DIR / "WORKFLOW_ENTRY.md",
        root / WORKFLOW_DIR / PLAN_FILE,
        root / WORKFLOW_DIR / TASKS_FILE,
        root / WORKFLOW_DIR / AGENTS_FILE,
        root / WORKFLOW_DIR / RULES_DIR / "agent-operating-rules.md",
        root / WORKFLOW_DIR / RULES_DIR / "github-standards.md",
        root / WORKFLOW_DIR / LOOPS_DIR / LOOP_CHECKPOINTS_FILE,
    ]
    if task:
        targets.append(root / WORKFLOW_DIR / TASKS_DIR / f"{task}.md")
        try:
            st = _load_session(root)
            if st.get("task") == task:
                targets.extend(
                    path for path, _pkt in _open_guidance_packets(
                        root,
                        to_agent=st.get("agent"),
                        task=task,
                        session_id=st.get("session_id") or "",
                        model=st.get("model") or "",
                        reasoning_effort=st.get("reasoning_effort") or "",
                    )
                )
            else:
                targets.extend(path for path, _pkt in _open_guidance_packets(root, task=task))
        except NameError:
            pass
    return targets


_TASKS_INDEX_ROW_RE = re.compile(r"^\|\s*([A-Za-z][A-Za-z0-9]*-[\w.]+)\s*\|")
_PLAN_TASK_ROW_RE = re.compile(r"^- \[[ x]\]\s+([A-Za-z][A-Za-z0-9]*-[\w.]+)\b")


def _receipt_view(rel: str, data: bytes, task: str) -> bytes:
    """Receipt-relevant content of a shared doc for one task's session.

    Other tasks' index rows, other tasks' plan checklist rows, and the plan
    Change Log body churn on every unrelated lifecycle event; they carry no
    instruction for this task, so they are excluded from its read receipt.
    Everything else - headers, goals, rules, this task's own rows - still
    invalidates the receipt when it changes.
    """
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return data
    lines = []
    in_change_log = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("## "):
            in_change_log = stripped.lower() == "## change log"
            lines.append(line)
            continue
        if in_change_log and stripped.startswith("- "):
            continue
        if rel == f"{WORKFLOW_DIR}/{TASKS_FILE}":
            row = _TASKS_INDEX_ROW_RE.match(line)
            if row and row.group(1) != task:
                continue
        if rel == f"{WORKFLOW_DIR}/{PLAN_FILE}":
            row = _PLAN_TASK_ROW_RE.match(line)
            if row and row.group(1) != task:
                continue
        lines.append(line)
    return "\n".join(lines).encode("utf-8")


def _hash_docs(root: Path, task: str | None) -> dict:
    scoped_views = {
        f"{WORKFLOW_DIR}/{PLAN_FILE}",
        f"{WORKFLOW_DIR}/{TASKS_FILE}",
    }
    hashes = {}
    for d in _doc_hash_targets(root, task):
        if d.is_file():
            rel = str(d.relative_to(root))
            data = d.read_bytes()
            if task and rel in scoped_views:
                data = _receipt_view(rel, data, task)
            hashes[rel] = hashlib.sha256(data).hexdigest()[:12]
    return hashes


def _extract_section(text: str, header: str) -> str:
    lines = text.splitlines()
    out = []
    capturing = False
    for ln in lines:
        if ln.strip().startswith("## "):
            if capturing:
                break
            capturing = ln.strip().lower() == header.lower()
            continue
        if capturing:
            out.append(ln)
    return "\n".join(out).strip()


def _task_word_re(task: str) -> str:
    # \b alone is not enough: 'T-005' is a literal suffix of 'AGENT-005'.
    return rf"(?<![\w-]){re.escape(task)}\b"


def _plan_checked(root: Path, task: str) -> bool:
    text = _read(root / WORKFLOW_DIR / PLAN_FILE)
    return re.search(rf"- \[x\][^\n]*{_task_word_re(task)}", text) is not None


def _plan_has_task_row(root: Path, task: str) -> bool:
    text = _read(root / WORKFLOW_DIR / PLAN_FILE)
    return re.search(rf"^- \[[ x]\]\s+{_task_word_re(task)}", text, flags=re.M) is not None


def _check_plan_box(root: Path, task: str) -> None:
    plan = root / WORKFLOW_DIR / PLAN_FILE
    text = _read(plan)
    new = re.sub(rf"- \[ \](\s+{_task_word_re(task)}[^\n]*)", r"- [x]\1", text, count=1)
    if new != text:
        _write(plan, new)


def _set_task_doc_status(root: Path, task: str, status: str) -> None:
    path = root / WORKFLOW_DIR / TASKS_DIR / f"{task}.md"
    text = _read(path)
    if not text:
        return
    new = re.sub(r"^Status: .*$", f"Status: {status}", text, count=1, flags=re.M)
    if new != text:
        _write(path, new)


def _format_scope(scope) -> str:
    if isinstance(scope, str):
        return scope or "-"
    return ", ".join(scope or []) or "-"


def _update_tasks_index(root: Path, task: str, *, status: str | None = None,
                        owner: str | None = None, scope=None, title: str | None = None) -> None:
    path = root / WORKFLOW_DIR / TASKS_FILE
    text = _read(path)
    if not text:
        return
    lines = text.splitlines()
    updated = False
    out = []
    for line in lines:
        if line.startswith(f"| {task} |"):
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if len(cells) >= 6:
                if status is not None:
                    cells[1] = status
                if owner is not None:
                    cells[2] = owner or "-"
                if scope is not None:
                    cells[3] = f"`{_format_scope(scope)}`"
                if title is not None:
                    cells[5] = title
                line = "| " + " | ".join(cells) + " |"
                updated = True
        out.append(line)
    if updated:
        _write(path, "\n".join(out) + "\n")


def _task_status(root: Path, task: str) -> str:
    board = _load_board(root)
    return (board.get("tasks", {}).get(task) or {}).get("status") or ""


def _deps_satisfied(board: dict, task: dict) -> bool:
    tasks = board.get("tasks", {})
    for dep in task.get("deps") or []:
        if (tasks.get(dep) or {}).get("status") not in {"approved", "done"}:
            return False
    return True


def _agent_can_take(agent: str, task: dict) -> bool:
    owner = task.get("owner")
    if not owner or owner in {"-", "unassigned", "any"}:
        return True
    if owner == agent:
        return True
    return agent in {"supervisor", "human"}


def _select_next_task(root: Path, agent: str) -> str | None:
    board = _load_board(root)
    candidates = []
    priority = {"ready": 0, "todo": 1}
    for tid, task in board.get("tasks", {}).items():
        status = task.get("status")
        if status not in priority:
            continue
        if not _agent_can_take(agent, task):
            continue
        if not _deps_satisfied(board, task):
            continue
        candidates.append((priority[status], task.get("created_at") or "", tid))
    if not candidates:
        return None
    candidates.sort()
    return candidates[0][2]


def _task_isolation(root: Path, task: dict, requested: str | None = None) -> str:
    if requested and requested != "auto":
        return requested
    task_type = str(task.get("type") or "generic")
    policy = _runtime_policy(root)
    configured = ((policy.get("task_types") or {}).get(task_type) or {}).get("isolation")
    if configured in ISOLATION_MODES:
        return str(configured)
    return "worktree" if task_type in {"code", "experiment", "maintenance"} else "shared"


def _task_id_namespace_key(root: Path) -> bytes:
    git_dir = _git_dir(root)
    base = git_dir / WORKTREE_LEASES_DIR if git_dir is not None else _state_dir(root)
    path = base / TASK_ID_NAMESPACE_FILE
    try:
        key = path.read_bytes()
    except FileNotFoundError:
        path.parent.mkdir(parents=True, exist_ok=True)
        generated = secrets.token_bytes(TASK_ID_NAMESPACE_BYTES)
        try:
            key = generated if _create_binary_secret(path, generated) else path.read_bytes()
        except OSError:
            key = b""
    except OSError:
        key = b""
    if len(key) != TASK_ID_NAMESPACE_BYTES:
        fallback = f"{platform.node()}\0{root.resolve()}".encode("utf-8")
        return hashlib.sha256(fallback).digest()
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return key


def _claimed_task_ids(root: Path) -> set[str]:
    """Every task id this checkout can prove is already taken.

    The board alone can lag behind reality: a task claimed by a live session
    or leased to an unmerged worktree may not be on this checkout's board
    yet, and task documents (including archived ones) keep owning their
    identifiers after board entries move on. Deriving fresh ids from the
    board alone is how one task can silently collide with -- and later
    overwrite -- another, so collect ids from every durable source.
    """
    claimed: set[str] = set(_load_board(root).get("tasks", {}))
    for base in (
        root / WORKFLOW_DIR / TASKS_DIR,
        root / WORKFLOW_DIR / "archive",
    ):
        if base.is_dir():
            claimed.update(doc.stem for doc in base.rglob("*.md"))
    for row in _session_rows_unlocked(root):
        task = str(row.get("task") or "")
        if task:
            claimed.add(task)
    # Read the lease registry without taking its lock: task creation can run
    # inside a bootstrap subprocess while the parent already holds the lock,
    # and an unreconciled (possibly stale) claim only makes this set a safe
    # superset of the truly taken ids.
    for lease in _load_worktree_leases(root).get("leases") or []:
        if not isinstance(lease, dict) or lease.get("status") == "released":
            continue
        task = str(lease.get("task") or "")
        if task:
            claimed.add(task)
    return claimed


def _foreign_task_claim(root: Path, task: str) -> str | None:
    """Describe who else already claims this task id, if anyone.

    Claims made by this very session or by this checkout's own worktree
    lease are not foreign: worktree bootstrap legitimately creates the task
    record inside the freshly leased checkout, and a session may recreate a
    record it already owns.
    """
    session_key = _workflow_session_key()
    for row in _session_rows_unlocked(root):
        if str(row.get("task") or "") != task:
            continue
        if str(row.get("workflow_session_key") or "") == session_key:
            continue
        return f"live session {row.get('workflow_session_key')}"
    archive_base = root / WORKFLOW_DIR / "archive"
    if archive_base.is_dir() and any(archive_base.rglob(f"{task}.md")):
        return "an archived task document"
    current = str(root.resolve())
    for lease in _load_worktree_leases(root).get("leases") or []:
        if not isinstance(lease, dict) or lease.get("status") == "released":
            continue
        if str(lease.get("task") or "") != task:
            continue
        if str(Path(lease.get("path") or "").resolve()) == current:
            continue
        return f"worktree lease {lease.get('id')}"
    return None


def _next_task_id(root: Path, prefix: str = "T") -> str:
    session_key = _workflow_session_key()
    shard = hashlib.sha256(
        _task_id_namespace_key(root) + b"\0" + session_key.encode("utf-8")
    ).hexdigest()[:TASK_ID_SHARD_HEX_LENGTH].upper()
    namespaced_prefix = f"{prefix}{shard}"
    max_num = 0
    pattern = re.compile(rf"^{re.escape(namespaced_prefix)}-(\d+)$")
    for tid in _claimed_task_ids(root):
        m = pattern.match(tid)
        if m:
            max_num = max(max_num, int(m.group(1)))
    return f"{namespaced_prefix}-{max_num + 1:03d}"


# ---------- commands ----------

_AGENTS_BLOCK_START = "<!-- agent-workflow-kit:start -->"
_AGENTS_BLOCK_END = "<!-- agent-workflow-kit:end -->"
_PR_BLOCK_START = "<!-- agent-workflow-kit:pr-start -->"
_PR_BLOCK_END = "<!-- agent-workflow-kit:pr-end -->"
_MERGED_TEMPLATE_PATHS = {
    "AGENTS.md",
    ".github/PULL_REQUEST_TEMPLATE.md",
    ".codex/hooks.json",
    ".claude/settings.json",
    ".cursor/hooks.json",
    ".gitignore",
}


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _managed_markdown_text(existing: str, desired: str, start: str, end: str) -> str:
    block = f"{start}\n{desired.strip()}\n{end}"
    pattern = re.compile(
        re.escape(start) + r".*?" + re.escape(end),
        flags=re.S,
    )
    if pattern.search(existing):
        return pattern.sub(block, existing).rstrip() + "\n"
    if not existing.strip():
        return block + "\n"
    return existing.rstrip() + "\n\n" + block + "\n"


def _managed_hook_config(existing: str, desired: str, rel: str) -> str:
    try:
        current = json.loads(existing) if existing.strip() else {}
        managed = json.loads(desired)
    except json.JSONDecodeError as exc:
        raise ValueError(f"cannot merge {rel}: invalid JSON ({exc})") from exc
    if not isinstance(current, dict) or not isinstance(managed, dict):
        raise ValueError(f"cannot merge {rel}: top-level JSON value must be an object")
    current_hooks = current.setdefault("hooks", {})
    desired_hooks = managed.get("hooks") or {}
    if not isinstance(current_hooks, dict) or not isinstance(desired_hooks, dict):
        raise ValueError(f"cannot merge {rel}: hooks must be an object")
    for event, wanted in desired_hooks.items():
        prior = current_hooks.get(event, [])
        if not isinstance(prior, list) or not isinstance(wanted, list):
            raise ValueError(f"cannot merge {rel}: hooks.{event} must be an array")
        preserved = []
        for item in prior:
            cleaned = _remove_managed_hook_command(item)
            if cleaned is not None:
                preserved.append(cleaned)
        current_hooks[event] = preserved + wanted
    if "version" in managed:
        current.setdefault("version", managed["version"])
    return json.dumps(current, indent=2, ensure_ascii=False) + "\n"


def _remove_managed_hook_command(item):
    if not isinstance(item, dict):
        return item
    command = item.get("command")
    if isinstance(command, str) and "agent_workflow_hook.py" in command:
        return None
    cleaned = dict(item)
    nested = item.get("hooks")
    if isinstance(nested, list):
        remaining = []
        for child in nested:
            child_cleaned = _remove_managed_hook_command(child)
            if child_cleaned is not None:
                remaining.append(child_cleaned)
        if not remaining:
            return None
        cleaned["hooks"] = remaining
    return cleaned


def _hook_row_contains(observed, expected) -> bool:
    """Return True when observed preserves every effective field in expected."""
    if not isinstance(observed, dict) or not isinstance(expected, dict):
        return observed == expected
    for key, wanted in expected.items():
        actual = observed.get(key)
        if key == "hooks" and isinstance(wanted, list):
            if not isinstance(actual, list):
                return False
            if not all(any(_hook_row_contains(row, child) for row in actual) for child in wanted):
                return False
        elif actual != wanted:
            return False
    return True


def _init_install_plan(root: Path, kit: Path, src: Path, *, force_managed: bool) -> tuple[dict, list[str], int]:
    """Build a complete write plan before init mutates the target."""
    manifest = _load_json(_install_manifest_path(root), {})
    old_hashes = manifest.get("managed_files") if isinstance(manifest, dict) else {}
    if not isinstance(old_hashes, dict):
        old_hashes = {}
    writes: dict[Path, str] = {}
    managed: dict[str, str] = {}
    conflicts: list[str] = []
    seeded = 0

    template_files: dict[str, str] = {}
    for dirpath, _dirs, files in os.walk(src):
        for fn in files:
            source = Path(dirpath) / fn
            rel = str(source.relative_to(src)).replace(os.sep, "/")
            template_files[rel] = _read(source)

    exact = {
        rel: text for rel, text in template_files.items()
        if not rel.startswith(f"{WORKFLOW_DIR}/") and rel not in _MERGED_TEMPLATE_PATHS
    }
    exact["tools/agentctl.py"] = _read(kit / "tools" / "agentctl.py")
    hooks_src = kit / "hooks"
    if hooks_src.is_dir():
        for hook in sorted(hooks_src.iterdir()):
            if hook.is_file():
                exact[f".githooks/{hook.name}"] = _read(hook)

    for rel, desired in exact.items():
        destination = root / rel
        current = _read(destination) if destination.exists() else ""
        desired_hash = _sha256_text(desired)
        current_hash = _sha256_text(current) if destination.exists() else ""
        old_hash = str(old_hashes.get(rel) or "")
        if destination.exists() and current_hash != desired_hash:
            if not ((old_hash and current_hash == old_hash) or force_managed):
                conflicts.append(
                    f"{rel} differs from the last managed version; inspect it or rerun with --force-managed"
                )
                continue
        if current_hash != desired_hash:
            writes[destination] = desired
        managed[rel] = desired_hash

    for rel, desired in template_files.items():
        destination = root / rel
        if rel.startswith(f"{WORKFLOW_DIR}/"):
            if not destination.exists():
                writes[destination] = desired
                seeded += 1
            continue
        if rel == "AGENTS.md":
            merged = _managed_markdown_text(
                _read(destination), desired, _AGENTS_BLOCK_START, _AGENTS_BLOCK_END,
            )
            if _read(destination) != merged:
                writes[destination] = merged
        elif rel == ".github/PULL_REQUEST_TEMPLATE.md":
            merged = _managed_markdown_text(
                _read(destination), desired, _PR_BLOCK_START, _PR_BLOCK_END,
            )
            if _read(destination) != merged:
                writes[destination] = merged
        elif rel in {".codex/hooks.json", ".claude/settings.json", ".cursor/hooks.json"}:
            try:
                merged = _managed_hook_config(_read(destination), desired, rel)
            except ValueError as exc:
                conflicts.append(str(exc))
                continue
            if _read(destination) != merged:
                writes[destination] = merged

    source_commit = _git(kit, "rev-parse", "HEAD") or "uncommitted"
    manifest_changed = (
        manifest.get("version") != INSTALL_SCHEMA_VERSION
        or manifest.get("kit_version") != KIT_VERSION
        or manifest.get("protocol_epoch") != PROTOCOL_EPOCH
        or manifest.get("source_commit") != source_commit
        or old_hashes != managed
    )
    manifest_text = json.dumps({
        "version": INSTALL_SCHEMA_VERSION,
        "kit_version": KIT_VERSION,
        "protocol_epoch": PROTOCOL_EPOCH,
        "source_commit": source_commit,
        "installed_at": manifest.get("installed_at") or _now(),
        "updated_at": (
            _now() if manifest_changed else manifest.get("updated_at") or _now()
        ),
        "managed_files": managed,
        "managed_hooks": {
            rel: json.loads(template_files[rel])
            for rel in (".codex/hooks.json", ".claude/settings.json", ".cursor/hooks.json")
        },
        "policy": "project state is preserved; managed files upgrade only from recorded hashes",
    }, indent=2, ensure_ascii=False) + "\n"
    writes[_install_manifest_path(root)] = manifest_text
    return writes, conflicts, seeded

def cmd_init(args: argparse.Namespace) -> int:
    root = Path(args.path).resolve()
    kit = _kit_root()
    src = kit / "templates" / "project"
    if not src.is_dir():
        print(f"agentctl: template dir not found: {src}", file=sys.stderr)
        return 2
    try:
        upgrade_context, upgrade_blockers = _prepare_install_upgrade(
            root, kit, force_managed=bool(args.force_managed),
        )
    except ValueError as exc:
        print(f"agentctl: {exc}", file=sys.stderr)
        return 1
    if upgrade_blockers:
        print(
            "agentctl: upgrade barrier entered draining state; managed enforcement "
            "entrypoints were upgraded, but project templates and state were not "
            "migrated because active or stale sessions still hold write authority:",
            file=sys.stderr,
        )
        for row in upgrade_blockers:
            print(
                f"  - {row.get('workflow_session_key')} task={row.get('task')} "
                f"status={row.get('observed_status')} checkout={row.get('checkout')}",
                file=sys.stderr,
            )
        print(
            "agentctl: have each live session finish/release; inspect stale sessions "
            "before explicit release, then rerun init",
            file=sys.stderr,
        )
        return 1
    writes, conflicts, copied = _init_install_plan(
        root, kit, src, force_managed=bool(args.force_managed),
    )
    if conflicts:
        print("agentctl: installation aborted before writing because managed files conflict:", file=sys.stderr)
        for conflict in conflicts:
            print(f"  - {conflict}", file=sys.stderr)
        return 1
    for destination, content in writes.items():
        _write(destination, content)
    self_dst = root / "tools" / "agentctl.py"
    if self_dst.exists():
        try:
            os.chmod(self_dst, 0o755)
        except OSError:
            pass
    hook_bridge = root / "tools" / "agent_workflow_hook.py"
    if hook_bridge.exists():
        try:
            os.chmod(hook_bridge, 0o755)
        except OSError:
            pass
    # distribute git hooks into versioned .githooks/
    installed = []
    hooks_src = kit / "hooks"
    if hooks_src.is_dir():
        for h in sorted(hooks_src.iterdir()):
            if h.is_file():
                dst = root / ".githooks" / h.name
                try:
                    os.chmod(dst, 0o755)
                except OSError:
                    pass
                installed.append(h.name)
    # seed shared runtime files
    if not _board_path(root).is_file():
        _save_board(root, {"version": 1, "updated_at": "", "tasks": {}})
    if not _agents_path(root).is_file():
        _save_agents(root, {"version": 1, "agents": {
            "supervisor": {"role": "planning, task split, final review",
                           "backend": "any", "write_scope": [".agent/", "docs/"],
                           "tools": [], "model": "", "reasoning_effort": ""},
            "fable": {"role": "advanced planning supervisor; writes guidance packets for codex",
                      "backend": "claude", "write_scope": [".agent/", "docs/"],
                      "tools": ["agentctl guidance create", "agentctl task create"],
                      "model": "fable", "reasoning_effort": ""},
            "codex": {"role": "implementation worker; reads guidance packets and executes tasks",
                      "backend": "codex", "write_scope": [],
                      "tools": ["agentctl work", "agentctl guidance ack", "agentctl finish"],
                      "model": "", "reasoning_effort": ""}}})
    _record_adoption_baseline(root)
    _ensure_gitignore(root)
    _ensure_gitattributes(root)
    for kind in (BUS_INBOX, BUS_OUTBOX, BUS_DONE, BUS_FAILED):
        _bus_dir(root, kind).mkdir(parents=True, exist_ok=True)
    wired = False
    hook_warnings: list[str] = []
    if (root / ".git").exists() and installed:
        # Repointing core.hooksPath silently bypasses hooks the project already
        # relies on (husky, pre-commit framework, or default .git/hooks). Warn
        # so the operator can chain them into .githooks instead of losing them.
        prior_hooks_path = _git(root, "config", "--get", "core.hooksPath").strip()
        if prior_hooks_path and prior_hooks_path != ".githooks":
            hook_warnings.append(
                f"core.hooksPath was '{prior_hooks_path}' and is being repointed to "
                "'.githooks'; hooks under the old path will no longer run. Chain "
                "them from .githooks/* (call the old scripts at the end) if you "
                "still need them."
            )
        elif not prior_hooks_path:
            default_hooks = _git_common_dir(root)
            hooks_dir = (default_hooks / "hooks") if default_hooks else (root / ".git" / "hooks")
            existing = []
            if hooks_dir.is_dir():
                existing = [
                    h.name for h in sorted(hooks_dir.iterdir())
                    if h.is_file() and os.access(h, os.X_OK)
                    and not h.name.endswith(".sample")
                ]
            if existing:
                hook_warnings.append(
                    "existing default git hooks will be bypassed once core.hooksPath "
                    f"points to '.githooks': {', '.join(existing)}. Move or chain them "
                    "into .githooks/* to keep them running."
                )
        _git(root, "config", "core.hooksPath", ".githooks")
        _configure_ledger_merge_driver(root)
        wired = True
    print(f"agentctl: initialized workflow ({copied} project seed files, {len(writes)} managed writes) at {root}")
    print(f"agentctl: distributed agentctl.py + {len(installed)} git hooks into .githooks/")
    for warning in hook_warnings:
        print(f"agentctl: WARNING {warning}", file=sys.stderr)
    if wired:
        print("agentctl: git core.hooksPath -> .githooks")
        print(f"agentctl: git merge driver '{LEDGER_MERGE_DRIVER}' -> ledger files under .agent/")
    elif installed:
        print("agentctl: NOTE not a git repo; after 'git init' run: git config core.hooksPath .githooks")
    _finish_install_upgrade(root, upgrade_context)
    return 0


def _task_capsule(root: Path, task: str, session: dict | None = None) -> dict:
    session = session if isinstance(session, dict) else _load_session(root)
    entry = (_load_board(root).get("tasks") or {}).get(task) or {}
    task_doc = root / WORKFLOW_DIR / TASKS_DIR / f"{task}.md"
    body = _read(task_doc)
    contract = _extract_section(body, "## Task Contract").strip()
    stage_plan = _extract_section(body, "## Stage Plan")
    stage_log = _extract_section(body, "## Stage Log")
    todos = [
        match.group(1).strip()
        for match in re.finditer(r"(?m)^-\s*\[\s\]\s+(.+)$", stage_plan)
    ][:12]
    recent_log = [
        line.strip()
        for line in stage_log.splitlines()
        if line.strip().startswith("-") and "No updates yet" not in line
    ][-5:]
    dependencies = []
    board_tasks = _load_board(root).get("tasks") or {}
    for dependency in entry.get("deps") or []:
        dep = board_tasks.get(dependency) or {}
        dependencies.append({
            "task": dependency,
            "status": dep.get("status") or "missing",
        })
    current_key = session.get("workflow_session_key") or _workflow_session_key()
    peers = [
        {
            "session": row.get("workflow_session_key"),
            "task": row.get("task"),
            "agent": row.get("agent"),
            "status": row.get("observed_status"),
            "scope": row.get("scope") or [],
            "checkout": row.get("checkout"),
        }
        for row in _session_rows_unlocked(root)
        if row.get("workflow_session_key") != current_key
        and row.get("observed_status") in {"active", "stale"}
    ]
    leases = []
    for row in _execution_lease_rows(root):
        if row.get("task") != task or row.get("kind") == "conversation":
            continue
        leases.append({
            "id": row.get("id"),
            "kind": row.get("kind"),
            "status": row.get("status"),
            "resources": row.get("resources") or [],
            "processes": row.get("processes") or [],
        })
    doc_hashes = _hash_docs(root, task)
    digest = hashlib.sha256(
        json.dumps(doc_hashes, sort_keys=True).encode("utf-8")
    ).hexdigest()[:12]
    return {
        "version": 1,
        "generated_at": _now(),
        "task": task,
        "title": entry.get("title") or task,
        "status": entry.get("status") or "unknown",
        "owner": entry.get("owner") or session.get("agent") or "",
        "type": entry.get("type") or session.get("task_type") or "generic",
        "isolation": session.get("isolation") or _task_isolation(root, entry, "auto"),
        "scope": entry.get("scope") or session.get("scope") or [],
        "contract": contract[:1200],
        "remaining_todos": todos,
        "next_action": todos[0] if todos else "",
        "recent_log": recent_log,
        "recent_notes": list(session.get("notes") or [])[-3:],
        "dependencies": dependencies,
        "peers": peers,
        "leases": leases,
        "protocol_epoch": _installed_protocol_epoch(root),
        "documents_digest": digest,
    }


def _print_capsule(capsule: dict) -> None:
    print("[Runtime Capsule]")
    print(
        f"task={capsule['task']} status={capsule['status']} "
        f"type={capsule['type']} isolation={capsule['isolation']} "
        f"docs={capsule['documents_digest']} epoch={capsule['protocol_epoch']}"
    )
    if capsule.get("next_action"):
        print(f"next={capsule['next_action']}")
    if capsule.get("peers"):
        print("peers=" + "; ".join(
            f"{row['session']}:{row['task']}[{row['status']}]"
            for row in capsule["peers"]
        ))
    if capsule.get("leases"):
        print("leases=" + "; ".join(
            f"{row['id']}[{row['status']}]" for row in capsule["leases"]
        ))


def _print_focus(root: Path, task: str, agent: str | None = None,
                 session_id: str = "", model: str = "",
                 reasoning_effort: str = "") -> None:
    task_doc = root / WORKFLOW_DIR / TASKS_DIR / f"{task}.md"
    print(f"\n=== FOCUS [{task}] — re-read before continuing ===")
    if task_doc.is_file():
        body = _read(task_doc)
        for header, label in (("## Task Contract", "Contract"),
                              ("## Work Scope", "Scope"),
                              ("## Stage Plan", "Stage Plan / TODO")):
            sec = _extract_section(body, header)
            if sec:
                print(f"[{label}]\n{sec}\n")
    else:
        print(f"(no task doc at {WORKFLOW_DIR}/{TASKS_DIR}/{task}.md)")
    _print_capsule(_task_capsule(root, task))
    if agent:
        _print_guidance_focus(
            root, agent, task, session_id=session_id, model=model,
            reasoning_effort=reasoning_effort,
        )
    print("Required reading: AGENTS.md, .agent/PROJECT_PLAN.md, and the task doc above.")
    print("=== end focus ===")


def cmd_capsule(args: argparse.Namespace) -> int:
    root = _repo_root()
    session = _load_session(root)
    task = args.task or session.get("task")
    if not task:
        print(
            "agentctl: no active task; pass --task or start work first",
            file=sys.stderr,
        )
        return 2
    capsule = _task_capsule(root, task, session)
    if args.json:
        print(json.dumps(capsule, indent=2, ensure_ascii=False))
    else:
        _print_capsule(capsule)
    return 0


def cmd_work(args: argparse.Namespace) -> int:
    root = _repo_root()
    identity_error = _workflow_session_identity_error()
    if identity_error:
        print(f"agentctl: {identity_error}", file=sys.stderr)
        return 2
    agent = args.agent or os.environ.get("AGENT_NAME", "unknown")
    if args.task:
        meta = _resolve_worker_metadata(root, agent, args)
        start_args = argparse.Namespace(task=args.task, agent=agent, scope=args.scope, force=args.force,
                                        session_id=meta["session_id"], model=meta["model"],
                                        reasoning_effort=meta["reasoning_effort"],
                                        takeover=getattr(args, "takeover", False),
                                        reason=getattr(args, "reason", ""))
        return cmd_start(start_args)
    st = _load_session(root)
    active = st.get("task")
    if active and _task_status(root, active) == "in_progress" and not args.force:
        if args.auto_create and (args.new_id or args.title):
            # An explicit creation request names new work; resuming the
            # session's existing task instead would silently discard that
            # intent (B-1). Refuse unless the request describes the task the
            # session already owns.
            entry = (_load_board(root).get("tasks") or {}).get(active) or {}
            requested_id = str(args.new_id or "").strip()
            requested_title = str(args.title or "").strip()
            active_title = str(entry.get("title") or "").strip()
            if (requested_id and requested_id != active) or (
                not requested_id
                and requested_title
                and requested_title != active_title
            ):
                wanted = requested_id or f"'{requested_title}'"
                print(
                    f"agentctl: this session already owns in_progress task "
                    f"{active} ('{active_title}'); refusing to silently "
                    f"resume it for a different creation request ({wanted})",
                    file=sys.stderr,
                )
                print(
                    f"  finish or release {active} first, or resume it "
                    f"explicitly with 'agentctl work --agent {agent} "
                    f"--task {active}'",
                    file=sys.stderr,
                )
                return 1
        try:
            coordination_fd = _acquire_lock_file(_session_coordination_lock_path(root))
        except TimeoutError as exc:
            print(f"agentctl: {exc}", file=sys.stderr)
            return 2
        resumed = False
        try:
            st = _load_session(root)
            active = st.get("task")
            if active and _task_status(root, active) == "in_progress":
                session_key = st.get("workflow_session_key") or _workflow_session_key()
                conflicts = _session_start_conflicts(
                    root, session_key, active, st.get("scope") or [],
                    str(st.get("isolation") or "shared"),
                )
                if conflicts:
                    for conflict in conflicts:
                        print(f"agentctl: session conflict: {conflict}", file=sys.stderr)
                    print(
                        "agentctl: this task was transferred to another conversation; "
                        "select different work instead of reviving the old session.",
                        file=sys.stderr,
                    )
                    return 1
                resume_agent = st.get("agent") or agent
                resume_meta = _resolve_worker_metadata(root, resume_agent, args)
                effective_meta = {
                    key: resume_meta[key] or st.get(key) or ""
                    for key in ("session_id", "model", "reasoning_effort")
                }
                runtime_before = list(st.get("runtime_identities") or [])
                current_runtime_before = st.get("runtime_identity")
                _record_runtime_identity(st)
                if (any(st.get(key, "") != value for key, value in effective_meta.items())
                        or runtime_before != st.get("runtime_identities")
                        or current_runtime_before != st.get("runtime_identity")):
                    st.update(effective_meta)
                    st["doc_hashes"] = _hash_docs(root, active)
                st["presence_status"] = "working"
                st.pop("released_at", None)
                st.pop("release_reason", None)
                _save_session(root, st)
                resumed = True
        finally:
            _release_lock_file(_session_coordination_lock_path(root), coordination_fd)
        if resumed:
            print(f"agentctl: resuming active task {active} (agent={resume_agent})")
            _print_focus(root, active, resume_agent, **effective_meta)
            awareness = _session_awareness(root, st.get("workflow_session_key"))
            if awareness:
                print("\n" + awareness)
            _run_loop_checkpoint(root, "work-start", once=True, trigger="work-resume", strict=False)
            return 0
    meta = _resolve_worker_metadata(root, agent, args)
    # An explicit creation request names the work this conversation was asked
    # to do; it must never be silently swapped for another queued task that
    # happens to share the agent name.
    explicit_create = bool(args.auto_create and (args.new_id or args.title))
    task = None if explicit_create else _select_next_task(root, agent)
    if not task:
        if args.auto_create:
            if not args.title:
                print("agentctl: --title is required with --auto-create", file=sys.stderr)
                return 2
            if not args.scope:
                print("agentctl: --scope is required with --auto-create so the task has a safe write boundary", file=sys.stderr)
                return 2
            request_token = (getattr(args, "request_id", "") or "").strip()
            request_fd = None
            if request_token:
                token_error = _submission_request_error(request_token)
                if token_error:
                    print(f"agentctl: {token_error}", file=sys.stderr)
                    return 2
                intent_digest = _submission_intent_digest({
                    "kind": "auto-create",
                    "agent": agent,
                    "title": str(args.title),
                    "scope": sorted(
                        item.strip() for item in str(args.scope).split(",")
                        if item.strip()
                    ),
                    "type": str(args.task_type or "generic"),
                    "new_id": str(args.new_id or ""),
                    "deps": str(args.deps or ""),
                })

                def _resolve_created_task(record: dict):
                    allocated = str((record.get("result") or {}).get("task") or "")
                    if allocated and allocated in (_load_board(root).get("tasks") or {}):
                        return {"task": allocated}
                    return None

                action, record, request_fd = _submission_request_begin(
                    root, request_token, intent_digest, "auto-create",
                    _resolve_created_task,
                )
                if action == "replay":
                    confirmed = str((record.get("result") or {}).get("task") or "")
                    print(
                        f"agentctl: request {request_token} already created task "
                        f"{confirmed}; not creating it again"
                    )
                    print(
                        f"  continue: python3 tools/agentctl.py work "
                        f"--agent {agent} --task {confirmed}"
                    )
                    return 0
                if action == "replay-reject":
                    print(
                        f"agentctl: request {request_token} was already rejected: "
                        f"{record.get('error') or 'no recorded reason'}",
                        file=sys.stderr,
                    )
                    return 2
                if action == "conflict":
                    print(
                        f"agentctl: request {request_token} was already used with a "
                        "different creation intent; pick a new request id",
                        file=sys.stderr,
                    )
                    return 2
                if action == "blocked":
                    print(
                        f"agentctl: request {request_token} has an interrupted "
                        "attempt with no confirmed task; inspect worktree leases "
                        "and branches for leftovers, then retry with a new "
                        "request id",
                        file=sys.stderr,
                    )
                    return 1
            task = args.new_id or _next_task_id(root, args.prefix or "T")
            if request_fd is not None:
                _submission_request_note(root, request_token, {"task": task})
            create_args = argparse.Namespace(
                id=task,
                title=args.title,
                owner=agent,
                scope=args.scope,
                deps=args.deps or "",
                task_type=args.task_type or "generic",
                force=args.force,
            )
            isolation = _task_isolation(
                root, {"type": create_args.task_type}, args.isolation,
            )
            if isolation in {"worktree", "exclusive"}:
                bootstrap_rc = _worktree_bootstrap_task(
                    root, create_args, agent, isolation,
                )
                if request_fd is not None:
                    if bootstrap_rc == 0:
                        _submission_request_settle(
                            root, request_token, request_fd,
                            "confirmed", {"task": task},
                        )
                    elif bootstrap_rc == 1:
                        # Admission rejection: nothing durable was created, so
                        # the token stays reusable after the caller fixes the
                        # conflict.
                        _submission_request_abandon(
                            root, request_token, request_fd,
                        )
                    else:
                        # Execution failure past the creation boundary: keep
                        # "preparing" so a retry converges from durable state
                        # instead of relaunching blindly.
                        _submission_request_settle(
                            root, request_token, request_fd,
                            "preparing",
                            error=f"bootstrap exited {bootstrap_rc}",
                        )
                return bootstrap_rc
            if not (root / WORKFLOW_DIR / PLAN_FILE).is_file():
                print(
                    f"agentctl: missing {WORKFLOW_DIR}/{PLAN_FILE}. "
                    "run 'agentctl init' first.",
                    file=sys.stderr,
                )
                if request_fd is not None:
                    _submission_request_abandon(root, request_token, request_fd)
                return 2
            try:
                coordination_fd = _acquire_lock_file(
                    _session_coordination_lock_path(root),
                )
            except TimeoutError as exc:
                print(f"agentctl: {exc}", file=sys.stderr)
                if request_fd is not None:
                    _submission_request_abandon(root, request_token, request_fd)
                return 2
            try:
                session_key = _workflow_session_key()
                scope = [
                    item.strip() for item in (args.scope or "").split(",")
                    if item.strip()
                ]
                presence_conflicts = _session_start_conflicts(
                    root, session_key, task, scope, isolation,
                )
                if presence_conflicts:
                    for conflict in presence_conflicts:
                        print(
                            f"agentctl: session conflict: {conflict}",
                            file=sys.stderr,
                        )
                    print(
                        "agentctl: auto-create was rejected before task state "
                        "was written; inspect sessions or choose disjoint work.",
                        file=sys.stderr,
                    )
                    if request_fd is not None:
                        _submission_request_abandon(
                            root, request_token, request_fd,
                        )
                        request_fd = None
                    return 1
                try:
                    managed_lease = _managed_worktree_lease(root)
                except RuntimeError as exc:
                    print(f"agentctl: {exc}", file=sys.stderr)
                    if request_fd is not None:
                        _submission_request_abandon(
                            root, request_token, request_fd,
                        )
                        request_fd = None
                    return 2
                if managed_lease:
                    print(
                        "agentctl: this managed worktree is already leased to "
                        f"task={managed_lease.get('task')} "
                        f"agent={managed_lease.get('agent')}; create new work "
                        "from the planning checkout",
                        file=sys.stderr,
                    )
                    if request_fd is not None:
                        _submission_request_abandon(
                            root, request_token, request_fd,
                        )
                        request_fd = None
                    return 1
                board = _load_board(root)
                if scope and not args.force:
                    for tid, other_task in (board.get("tasks") or {}).items():
                        if other_task.get("status") not in ACTIVE_STATUSES:
                            continue
                        if _scopes_overlap(scope, other_task.get("scope") or []):
                            print(
                                f"agentctl: write-scope conflict with {tid} "
                                f"(owner={other_task.get('owner')}, "
                                f"scope={other_task.get('scope')}). use a "
                                "disjoint scope or task worktree.",
                                file=sys.stderr,
                            )
                            if request_fd is not None:
                                _submission_request_abandon(
                                    root, request_token, request_fd,
                                )
                                request_fd = None
                            return 1
                rc = _task_create_unlocked(root, create_args)
                if rc:
                    if request_fd is not None:
                        _submission_request_abandon(
                            root, request_token, request_fd,
                        )
                        request_fd = None
                    return rc
                print(f"agentctl: auto-created {task} for {agent}")
                if request_fd is not None:
                    # The task is durably on the board; whatever happens to
                    # session binding next, this request must never create a
                    # second task.
                    _submission_request_settle(
                        root, request_token, request_fd,
                        "confirmed", {"task": task},
                    )
                    request_fd = None
                start_args = argparse.Namespace(
                    task=task, agent=agent, scope=args.scope, force=args.force,
                    session_id=meta["session_id"], model=meta["model"],
                    reasoning_effort=meta["reasoning_effort"],
                    takeover=getattr(args, "takeover", False),
                    reason=getattr(args, "reason", ""),
                )
                start_fd = coordination_fd
                coordination_fd = None
                return cmd_start(
                    start_args, _coordination_fd=start_fd,
                )
            finally:
                if coordination_fd is not None:
                    _release_lock_file(
                        _session_coordination_lock_path(root), coordination_fd,
                    )
        print(f"agentctl: no ready/todo task assigned to {agent}.")
        print("agentctl: if this is a new user request, create and start a task in one command:")
        print("  python3 tools/agentctl.py work --agent " + agent + " --auto-create --title \"...\" --scope path/")
        return 1
    print(f"agentctl: auto-selected {task} for {agent}")
    selected = (_load_board(root).get("tasks") or {}).get(task) or {}
    isolation = _task_isolation(root, selected, args.isolation)
    if isolation in {"worktree", "exclusive"} and not _managed_worktree_lease(root):
        if isolation == "exclusive" and any(
                row.get("observed_status") in {"active", "stale"}
                for row in _session_rows_unlocked(root)):
            print(
                "agentctl: exclusive maintenance cannot start while another "
                "session is active or stale",
                file=sys.stderr,
            )
            return 1
        return _worktree_create(
            root,
            argparse.Namespace(
                task=task, agent=agent, branch=None, path=None, base="HEAD",
            ),
        )
    start_args = argparse.Namespace(task=task, agent=agent, scope=args.scope, force=args.force,
                                    session_id=meta["session_id"], model=meta["model"],
                                    reasoning_effort=meta["reasoning_effort"],
                                    takeover=getattr(args, "takeover", False),
                                    reason=getattr(args, "reason", ""))
    return cmd_start(start_args)


def _foreign_claim_holder(root: Path, task: str, entry: dict) -> str:
    """Owner of a task the board shows in_progress that no session here ever held.

    Sessions never leave the machine, so a board entry that is in_progress
    while this checkout (including its linked worktrees) has no record of
    the task -- active, stale, or released -- is a claim made somewhere
    else. Returns the recorded owner, or "" when the claim is local.
    """
    if str(entry.get("status") or "") != "in_progress":
        return ""
    for row in _session_rows_unlocked(root):
        if str(row.get("task") or "") == task:
            return ""
    return str(entry.get("owner") or "another agent")


def _foreign_claim_error(task: str, entry: dict, holder: str, args: argparse.Namespace) -> str:
    reason = str(getattr(args, "reason", "") or "").strip()
    if getattr(args, "takeover", False):
        if not reason:
            return f"--takeover of {task} requires --reason <why the previous holder is not coming back>"
        return ""
    updated = entry.get("updated_at") or entry.get("created_at") or "-"
    return (
        f"task {task} is in_progress for {holder} according to the board (updated {updated}), "
        f"and this checkout has no session for it, so it is probably being worked on from another "
        f"checkout or machine; after verifying that work is abandoned, rerun with "
        f"--takeover --reason <why>"
    )


def _record_takeover(root: Path, task: str, agent: str, holder: str, reason: str) -> None:
    ts = _now()
    line = f"taken over from {holder} by {agent}: {reason}"
    log = root / WORKFLOW_DIR / LOG_DIR / PROGRESS_LOG
    _write(log, _read(log) + f"- {ts} [{task}] {line}\n")
    task_doc = root / WORKFLOW_DIR / TASKS_DIR / f"{task}.md"
    if task_doc.is_file():
        body = _read(task_doc).replace("- No updates yet.\n", "", 1)
        i = body.find("## Stage Log")
        if i >= 0:
            j = body.find("\n", i) + 1
            body = body[:j] + f"- {ts} {line}\n" + body[j:]
            _write(task_doc, body)
    print(f"agentctl: {task} {line}", file=sys.stderr)


def cmd_start(
    args: argparse.Namespace, *, _coordination_fd: int | None = None,
) -> int:
    root = _repo_root()
    identity_error = _workflow_session_identity_error()
    if identity_error:
        print(f"agentctl: {identity_error}", file=sys.stderr)
        return 2
    if not (root / WORKFLOW_DIR / PLAN_FILE).is_file():
        print(f"agentctl: missing {WORKFLOW_DIR}/{PLAN_FILE}. run 'agentctl init' first.", file=sys.stderr)
        return 2
    task = args.task
    agent = args.agent or os.environ.get("AGENT_NAME", "unknown")
    meta = _resolve_worker_metadata(root, agent, args)
    coordination_fd = _coordination_fd
    if coordination_fd is None:
        try:
            coordination_fd = _acquire_lock_file(
                _session_coordination_lock_path(root),
            )
        except TimeoutError as exc:
            print(f"agentctl: {exc}", file=sys.stderr)
            return 2
    try:
        board = _load_board(root)
        tasks = board.setdefault("tasks", {})
        if task not in tasks:
            print(
                f"agentctl: unknown task {task}; create it first with "
                "'agentctl task create' or 'agentctl work --auto-create'",
                file=sys.stderr,
            )
            return 2
        entry = tasks.get(task, {})
        scope = entry.get("scope") or []
        if args.scope:
            scope = [s.strip() for s in args.scope.split(",") if s.strip()]
        scope_problems = _scope_errors(scope)
        if scope_problems:
            for problem in scope_problems:
                print(f"agentctl: {problem}", file=sys.stderr)
            return 2
        try:
            managed_lease = _managed_worktree_lease(root)
        except RuntimeError as exc:
            print(f"agentctl: {exc}", file=sys.stderr)
            return 2
        required_isolation = _task_isolation(root, entry, "auto")
        if managed_lease:
            observed = managed_lease.get("observed_status")
            if observed != "active":
                print(
                    f"agentctl: this managed worktree lease is {observed}; "
                    "inspect or release it from the primary checkout before starting work",
                    file=sys.stderr,
                )
                return 1
            lease_scope = managed_lease.get("scope") or []
            if task != managed_lease.get("task") or agent != managed_lease.get("agent"):
                print(
                    f"agentctl: this managed worktree is leased to task={managed_lease.get('task')} "
                    f"agent={managed_lease.get('agent')}",
                    file=sys.stderr,
                )
                return 1
            if args.scope and sorted(set(scope)) != sorted(set(lease_scope)):
                print(
                    "agentctl: --scope cannot change a managed worktree lease; "
                    "release it and allocate a new task scope",
                    file=sys.stderr,
                )
                return 1
            scope = lease_scope
        elif required_isolation in {"worktree", "exclusive"}:
            print(
                f"agentctl: task {task} type={entry.get('type') or 'generic'} "
                f"requires {required_isolation} isolation; from the planning "
                f"checkout run 'agentctl work --agent {agent}' WITHOUT --task "
                "so the controller can auto-select it and allocate the task "
                "worktree (commit the task document at HEAD first so the "
                "worktree checkout can see it)",
                file=sys.stderr,
            )
            return 1
        session_key = _workflow_session_key()
        presence_conflicts = _session_start_conflicts(
            root, session_key, task, scope, required_isolation,
        )
        if presence_conflicts:
            for conflict in presence_conflicts:
                print(f"agentctl: session conflict: {conflict}", file=sys.stderr)
            print(
                "agentctl: inspect '.agent/state/SESSIONS.md' or run 'agentctl sessions list'; "
                "release an inspected stale session or allocate a worktree. --force does not "
                "override live session ownership.",
                file=sys.stderr,
            )
            return 1
        foreign_holder = _foreign_claim_holder(root, task, entry)
        if foreign_holder:
            foreign_error = _foreign_claim_error(task, entry, foreign_holder, args)
            if foreign_error:
                print(f"agentctl: {foreign_error}", file=sys.stderr)
                return 1
        if scope:
            for tid, other_task in tasks.items():
                if tid == task or other_task.get("status") not in ACTIVE_STATUSES:
                    continue
                if _scopes_overlap(scope, other_task.get("scope") or []) and not args.force:
                    print(
                        f"agentctl: write-scope conflict with {tid} "
                        f"(owner={other_task.get('owner')}, scope={other_task.get('scope')}). "
                        "use a disjoint scope or task worktree.",
                        file=sys.stderr,
                    )
                    return 1
        lp = _lock_path(root, task)
        existing = _load_json(lp, {})
        if existing and existing.get("workflow_session_key") not in (None, "", session_key):
            if not args.force:
                print(
                    f"agentctl: {task} locked by session={existing.get('workflow_session_key')} "
                    f"agent={existing.get('agent')} since {existing.get('acquired_at')}",
                    file=sys.stderr,
                )
                return 1
        if existing and existing.get("agent") not in (None, "", agent):
            if _pid_alive(existing.get("pid")) and not args.force:
                print(
                    f"agentctl: {task} locked by agent={existing.get('agent')} "
                    f"pid={existing.get('pid')} since {existing.get('acquired_at')}",
                    file=sys.stderr,
                )
                return 1
        _save_json(lp, {
            "task": task, "agent": agent, "pid": os.getpid(), "scope": scope,
            "workflow_session_key": session_key,
            "session_id": meta["session_id"], "model": meta["model"],
            "reasoning_effort": meta["reasoning_effort"], "acquired_at": _now(),
        })
        now = _now()
        e = tasks.setdefault(task, {
            "title": task, "status": "todo", "owner": agent, "scope": scope,
            "deps": [], "created_at": now, "updated_at": now,
        })
        e["status"] = "in_progress"
        e["owner"] = agent
        if scope:
            e["scope"] = scope
        e["updated_at"] = now
        if foreign_holder:
            e["taken_over_from"] = foreign_holder
            e["taken_over_at"] = now
            e["takeover_reason"] = str(getattr(args, "reason", "") or "").strip()
        _save_board(root, board)
        _update_tasks_index(
            root, task, status="in_progress", owner=agent,
            scope=e.get("scope"), title=e.get("title"),
        )
        _set_task_doc_status(root, task, "in_progress")
        if foreign_holder:
            _record_takeover(root, task, agent, foreign_holder, str(getattr(args, "reason", "") or "").strip())
        session = {
            "task": task, "agent": agent, "started_at": now, "scope": scope,
            "task_type": entry.get("type") or "generic",
            "isolation": required_isolation,
            "protocol_epoch": _installed_protocol_epoch(root),
            "workflow_session_key": session_key,
            "session_id": meta["session_id"], "model": meta["model"],
            "reasoning_effort": meta["reasoning_effort"],
            "notes": [], "claimed_files": [], "doc_hashes": {},
        }
        _record_runtime_identity(session)
        _save_session(root, session)
        session["doc_hashes"] = _hash_docs(root, task)
        _save_session(root, session)
    finally:
        _release_lock_file(_session_coordination_lock_path(root), coordination_fd)
    label = f"agent={agent}"
    if meta["model"]:
        label += f" model={meta['model']}"
    if meta["reasoning_effort"]:
        label += f" reasoning={meta['reasoning_effort']}"
    if meta["session_id"]:
        label += f" session={meta['session_id']}"
    print(f"agentctl: started {task} ({label}) -> in_progress")
    _print_focus(
        root, task, agent, session_id=meta["session_id"], model=meta["model"],
        reasoning_effort=meta["reasoning_effort"],
    )
    awareness = _session_awareness(root, session.get("workflow_session_key"))
    if awareness:
        print("\n" + awareness)
    _run_loop_checkpoint(root, "work-start", once=True, trigger="work-start", strict=False)
    return 0


def cmd_focus(args: argparse.Namespace) -> int:
    root = _repo_root()
    task = args.task or _load_session(root).get("task")
    if not task:
        print("agentctl: no active task. pass --task or run 'agentctl work --agent <name>'.", file=sys.stderr)
        return 2
    st = _load_session(root)
    agent = args.agent or st.get("agent")
    meta = _resolve_worker_metadata(root, agent, args)
    _print_focus(root, task, agent,
                 session_id=getattr(args, "session_id", None) or st.get("session_id") or meta["session_id"],
                 model=getattr(args, "model", None) or st.get("model") or meta["model"],
                 reasoning_effort=(
                     getattr(args, "reasoning_effort", None)
                     or st.get("reasoning_effort")
                     or meta["reasoning_effort"]
                 ))
    return 0


def cmd_progress(args: argparse.Namespace) -> int:
    root = _repo_root()
    note = args.note or ""
    if not note:
        print("agentctl: --note is required", file=sys.stderr)
        return 2
    lock = _session_coordination_lock_path(root)
    fd = _acquire_lock_file(lock)
    try:
        st = _require_session(root)
        _record_runtime_identity(st)
        _save_session(root, st)
        changed = _check_receipt(root)
        if changed:
            print("agentctl: progress blocked because required workflow documents changed:", file=sys.stderr)
            for problem in changed:
                print(f"  - {problem}", file=sys.stderr)
            return 1
        ts = _now()
        log = root / WORKFLOW_DIR / LOG_DIR / PROGRESS_LOG
        _write(log, _read(log) + f"- {ts} [{st['task']}] {note}\n")
        task_doc = root / WORKFLOW_DIR / TASKS_DIR / f"{st['task']}.md"
        if task_doc.is_file():
            body = _read(task_doc).replace("- No updates yet.\n", "", 1)
            i = body.find("## Stage Log")
            if i >= 0:
                j = body.find("\n", i) + 1
                body = body[:j] + f"- {ts} {note}\n" + body[j:]
            _write(task_doc, body)
        board = _load_board(root)
        t = board.get("tasks", {}).get(st["task"])
        if t:
            t["updated_at"] = ts
            _save_board(root, board)
        st.setdefault("notes", []).append({"at": ts, "note": note})
        st["doc_hashes"] = _hash_docs(root, st["task"])
        _save_session(root, st)
    finally:
        _release_lock_file(lock, fd)
    print(f"agentctl: progress recorded for {st['task']}")
    return 0


def cmd_note(args: argparse.Namespace) -> int:
    note = args.note
    if isinstance(note, list):
        note = " ".join(note)
    return cmd_progress(argparse.Namespace(note=note))


def _completion_record_value(value: str) -> str:
    """Keep worker-provided completion text inside one Markdown field."""
    return " ".join(str(value or "").split())


def _task_output_requirement_error(root: Path, task: str) -> str | None:
    entry = (_load_board(root).get("tasks") or {}).get(task) or {}
    task_type = str(entry.get("type") or "generic")
    policy = _runtime_policy(root)
    type_policy = (policy.get("task_types") or {}).get(task_type) or {}
    if not type_policy.get("requires_outputs"):
        return None
    for lease in _load_runtime_leases(root).get("leases") or []:
        if not isinstance(lease, dict) or lease.get("kind") != "run":
            continue
        if str(lease.get("task") or "") != task:
            continue
        if _runtime_observed_status(lease) != "succeeded":
            continue
        checkout = Path(str(lease.get("checkout") or root))
        declared = [str(item) for item in lease.get("outputs") or [] if str(item)]
        if declared and any((checkout / item).exists() for item in declared):
            return None
    return (
        f"task {task} type={task_type} requires at least one successful run "
        "with existing declared output before finish"
    )


def _decided_review_evidence(root: Path, task: str) -> list[str]:
    """Gate documents that record a decision issued from this review task."""
    gates_dir = root / WORKFLOW_DIR / GATES_DIR
    if not gates_dir.is_dir():
        return []
    marker = f"- Reviewer task: {task}"
    evidence = []
    for path in sorted(gates_dir.glob("*.md")):
        try:
            body = _read(path)
        except OSError:
            continue
        if re.search(rf"^{re.escape(marker)}$", body, flags=re.M):
            evidence.append(path.name)
    return evidence


def _review_scope_only(entry: dict) -> bool:
    """True when the task could only write workflow records under .agent/."""
    scope = [str(item).strip() for item in entry.get("scope") or [] if str(item).strip()]
    if not scope:
        return False
    return all(item == WORKFLOW_DIR or item.startswith(f"{WORKFLOW_DIR}/") for item in scope)


def _decided_review_closure(root: Path, task: str, entry: dict, *,
                            require_review_type: bool) -> list[str]:
    """Evidence that lets a finished review task close without its own gate.

    A review task's deliverable is the gate decision it issued on another
    task, recorded with independence proof in `.agent/gates/`. Requiring a
    second gate on the review task itself only recurses. Closure stays
    fail-closed: the task must be restricted to workflow records under
    `.agent/`, and at least one recorded decision must name it as the
    reviewer task.
    """
    if require_review_type and str(entry.get("type") or "") != "review":
        return []
    if not _review_scope_only(entry):
        return []
    return _decided_review_evidence(root, task)


def cmd_complete(args: argparse.Namespace) -> int:
    root = _repo_root()
    st = _require_session(root)
    task = st["task"]
    # A runtime that participates in finalization is part of the worker set,
    # even when another runtime originally started the task. Persist this
    # before any early return so role-switching cannot recover independence.
    _record_runtime_identity(st)
    _save_session(root, st)
    changed = _check_receipt(root)
    if changed:
        print("agentctl: finish blocked because required workflow documents changed:", file=sys.stderr)
        for problem in changed:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    summary = _completion_record_value(args.summary)
    if not summary:
        print("agentctl: --summary is required", file=sys.stderr)
        return 2
    output_error = _task_output_requirement_error(root, task)
    if output_error:
        print(f"agentctl: {output_error}", file=sys.stderr)
        return 1
    pending_guidance = _open_guidance_packets(
        root,
        to_agent=st.get("agent"),
        task=task,
        task_specific_only=True,
        session_id=st.get("session_id") or "",
        model=st.get("model") or "",
        reasoning_effort=st.get("reasoning_effort") or "",
    )
    if pending_guidance:
        for _path, pkt in pending_guidance:
            print(
                f"agentctl: pending supervisor guidance {pkt.get('id')} for {task} "
                f"from {pkt.get('from_agent') or pkt.get('by')}; "
                f"ack it with 'agentctl guidance ack {pkt.get('id')}'.",
                file=sys.stderr,
            )
        print("agentctl: finish blocked; incorporate the supervisor guidance and acknowledge it before finishing.", file=sys.stderr)
        return 1
    tests = _completion_record_value(args.tests)
    ack = bool(getattr(args, "ack_escalations", False))
    escalated = _escalated_follow_ups(root, task)
    if escalated and not ack:
        for _path, pkt in escalated:
            print(f"agentctl: escalated follow-up {pkt.get('id')} targets {task} "
                  f"(checkpoint {pkt.get('checkpoint')}, {pkt.get('occurrences', 1)} failures).", file=sys.stderr)
        print("agentctl: finish blocked; resolve the checkpoint failures (a success auto-closes the packet) "
              "or re-run with --ack-escalations to record a human override.", file=sys.stderr)
        return 1
    if escalated and ack:
        for path, pkt in escalated:
            pkt["updated_at"] = _now()
            pkt["acknowledged_by"] = st.get("agent") or "unknown"
            pkt["notes"] = (pkt.get("notes") or "") + (
                f"\n{pkt['updated_at']}: escalation acknowledged via finish --ack-escalations "
                f"by {pkt['acknowledged_by']}."
            )
            _save_json(path, pkt)
            print(f"agentctl: escalation acknowledged: {pkt.get('id')}")
    rc = _run_loop_checkpoint(root, "pre-finish", once=True, trigger="pre-finish", strict=True)
    if rc:
        return rc
    lock = _session_coordination_lock_path(root)
    fd = _acquire_lock_file(lock)
    try:
        current = _require_session(root)
        if current.get("task") != task:
            print(
                f"agentctl: active session changed from {task} to {current.get('task')}; finish aborted",
                file=sys.stderr,
            )
            return 1
        changed = _check_receipt(root)
        if changed:
            print(
                "agentctl: finish blocked because required workflow documents changed "
                "while finalization was waiting:",
                file=sys.stderr,
            )
            for problem in changed:
                print(f"  - {problem}", file=sys.stderr)
            return 1
        st = current
        _record_runtime_identity(st)
        ts = _now()
        board = _load_board(root)
        t = board.get("tasks", {}).get(task)
        decisions = _decided_review_closure(
            root, task, t or {}, require_review_type=True,
        )
        final_status = "done" if decisions else "review"
        task_doc = root / WORKFLOW_DIR / TASKS_DIR / f"{task}.md"
        if task_doc.is_file():
            body = _read(task_doc)
            record = f"- Summary: {summary}\n"
            if tests:
                record += f"- Tests: {tests}\n"
            worker_runtimes = [str(item) for item in st.get("runtime_identities") or [] if str(item)]
            if worker_runtimes:
                record += f"- Worker-runtimes: {', '.join(worker_runtimes)}\n"
            record += f"- Completed-at: {ts}\n"
            record += f"- Completed-at-ns: {time.time_ns()}\n"
            i = body.find("## Completion Record")
            if i >= 0:
                body = body[:i] + "## Completion Record\n" + record
            else:
                body += "\n## Completion Record\n" + record
            body = re.sub(r"^Status: .*$", f"Status: {final_status}", body, count=1, flags=re.M)
            _write(task_doc, body)
        if t:
            t["status"] = final_status
            t["updated_at"] = ts
            _save_board(root, board)
            _update_tasks_index(
                root, task, status=final_status, owner=t.get("owner"),
                scope=t.get("scope"), title=t.get("title"),
            )
        if final_status == "done":
            _check_plan_box(root, task)
        lp = _lock_path(root, task)
        if lp.is_file():
            lp.unlink()
        st["status"] = final_status
        st["completed_at"] = ts
        st["doc_hashes"] = _hash_docs(root, task)
        _save_session(root, st)
    finally:
        _release_lock_file(lock, fd)
    if final_status == "done":
        print(
            f"agentctl: {task} -> done. review task closed on its recorded "
            f"gate decisions: {', '.join(decisions)}"
        )
    else:
        print(
            f"agentctl: {task} -> review. independent reviewer gate: from a different "
            f"conversation, register the reviewer once ('agentctl agents add --id <reviewer> "
            f"--role review'), start a separate review task, then run "
            f"agentctl gate approve --task {task} --by <reviewer>"
        )
    _run_loop_checkpoint(root, "post-finish", once=True, trigger="post-finish", strict=False)
    return 0


def cmd_finish(args: argparse.Namespace) -> int:
    root = _repo_root()
    st = _require_session(root)
    task = st["task"]
    summary = args.summary
    if not summary:
        title = (_load_board(root).get("tasks", {}).get(task) or {}).get("title") or task
        summary = f"Completed {task}: {title}"
    tests = args.tests or "not recorded"
    return cmd_complete(argparse.Namespace(summary=summary, tests=tests,
                                           ack_escalations=bool(getattr(args, "ack_escalations", False))))


def _github_merge_evidence(root: Path, args: argparse.Namespace) -> tuple[dict, list[str]]:
    problems: list[str] = []
    if not getattr(args, "pr", None):
        return {}, ["GitHub reconciliation requires --pr <number-or-url>"]
    gh = shutil.which("gh")
    if not gh:
        return {}, ["GitHub reconciliation requires the authenticated GitHub CLI (gh)"]
    remote = _git_process(root, "remote", "get-url", "origin")
    trusted_identity, trusted_host = _github_repository_identity(remote.stdout.strip())
    if remote.returncode != 0 or trusted_identity is None:
        return {}, ["unable to identify the authoritative GitHub repository from remote origin"]
    command = [
        gh, "pr", "view", str(args.pr), "--json",
        "state,mergedAt,mergeCommit,mergedBy,url,baseRefName",
    ]
    if getattr(args, "repo", None):
        repo_arg = str(args.repo).strip()
        if "://" not in repo_arg and repo_arg.count("/") == 1:
            repo_arg = f"{trusted_host}/{repo_arg}"
        command.extend(["--repo", repo_arg])
    try:
        result = subprocess.run(
            command, cwd=str(root), text=True, encoding="utf-8", errors="replace",
            capture_output=True, timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {}, [f"unable to read GitHub PR evidence: {exc}"]
    if result.returncode:
        return {}, ["unable to read GitHub PR evidence: " + (result.stderr or result.stdout).strip()]
    try:
        evidence = json.loads(result.stdout)
    except json.JSONDecodeError:
        return {}, ["GitHub CLI returned invalid PR evidence"]
    if evidence.get("state") != "MERGED":
        problems.append(f"GitHub PR is {evidence.get('state') or 'unknown'}, expected MERGED")
    merge_oid = str((evidence.get("mergeCommit") or {}).get("oid") or "")
    if not re.fullmatch(r"[0-9a-fA-F]{40}", merge_oid):
        problems.append("GitHub PR has no valid merge commit")
    elif _git_process(root, "merge-base", "--is-ancestor", merge_oid, "HEAD").returncode != 0:
        problems.append("GitHub PR merge commit is not an ancestor of the current HEAD")
    merged_by = str((evidence.get("mergedBy") or {}).get("login") or "")
    if not merged_by:
        problems.append("GitHub PR has no mergedBy identity")
    elif merged_by.casefold() != str(args.by or "").casefold():
        problems.append(f"--by {args.by} does not match GitHub mergedBy {merged_by}")
    evidence_identity, _evidence_host = _github_repository_identity(str(evidence.get("url") or ""))
    if evidence_identity is None:
        problems.append("GitHub PR evidence has no parseable repository URL")
    elif evidence_identity != trusted_identity:
        problems.append("GitHub PR repository does not match the checkout origin")
    return evidence, problems


def _github_repository_identity(url: str) -> tuple[tuple[str, str, str] | None, str]:
    value = url.strip()
    host = ""
    path = ""
    if "://" in value:
        parsed = urlparse(value)
        host = parsed.netloc.rsplit("@", 1)[-1]
        path = parsed.path
    else:
        match = re.match(r"^(?:[^@]+@)?([^:]+):(.+)$", value)
        if match:
            host, path = match.groups()
    parts = [part for part in path.strip("/").split("/") if part]
    if len(parts) >= 3 and parts[2] == "pull":
        parts = parts[:2]
    if len(parts) != 2 or not host:
        return None, ""
    owner, repo = parts
    if repo.endswith(".git"):
        repo = repo[:-4]
    if not owner or not repo:
        return None, ""
    return (host.casefold(), owner.casefold(), repo.casefold()), host


def _github_pr_changed_paths(root: Path, evidence: dict) -> tuple[set[str], list[str]]:
    url = str(evidence.get("url") or "")
    identity, host = _github_repository_identity(url)
    parsed = urlparse(url)
    path_parts = [part for part in parsed.path.strip("/").split("/") if part]
    if identity is None or len(path_parts) < 4 or path_parts[2] != "pull" or not path_parts[3].isdigit():
        return set(), ["GitHub PR evidence has no parseable pull request URL"]
    _normalized_host, owner, repo = identity
    number = path_parts[3]
    gh = shutil.which("gh")
    if not gh:
        return set(), ["GitHub reconciliation requires the authenticated GitHub CLI (gh)"]
    try:
        query = (
            "query($owner:String!,$name:String!,$number:Int!,$endCursor:String){"
            "repository(owner:$owner,name:$name){pullRequest(number:$number){"
            "files(first:100,after:$endCursor){nodes{path}pageInfo{hasNextPage endCursor}}}}}"
        )
        result = subprocess.run(
            [
                gh, "api", "graphql", "--hostname", host, "--paginate",
                "-f", f"query={query}", "-F", f"owner={owner}", "-F", f"name={repo}",
                "-F", f"number={number}",
                "--jq", ".data.repository.pullRequest.files.nodes[].path",
            ],
            cwd=str(root), text=True, encoding="utf-8", errors="replace",
            capture_output=True, timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return set(), [f"unable to read complete GitHub PR file evidence: {exc}"]
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        return set(), ["unable to read complete GitHub PR file evidence: " + detail]
    return {line.strip() for line in result.stdout.splitlines() if line.strip()}, []


def _gate_reconcile_github(root: Path, args: argparse.Namespace, board: dict, task: str, t: dict) -> int:
    recorder_session = _load_session(root)
    recorder = str(recorder_session.get("agent") or "")
    recorder_role = str(_agent_profile(root, recorder).get("role") or "").lower()
    problems = []
    if not recorder or not any(label in recorder_role for label in ("supervisor", "planning", "review")):
        problems.append("GitHub reconciliation requires an active supervisor/planning/review agent")
    recorder_task = str(recorder_session.get("task") or "")
    if not recorder_task or _task_status(root, recorder_task) != "in_progress":
        problems.append("GitHub reconciliation requires a separate active in_progress review task")
    if recorder_task == task:
        problems.append("the reconciliation task cannot be the task being approved")
    if t.get("status") not in {"review", "approved", "done"}:
        problems.append(f"task {task} is '{t.get('status')}', expected review/approved/done")

    evidence, evidence_problems = _github_merge_evidence(root, args)
    problems.extend(evidence_problems)
    changed_paths, changed_path_problems = _github_pr_changed_paths(root, evidence)
    problems.extend(changed_path_problems)
    task_path = f"{WORKFLOW_DIR}/{TASKS_DIR}/{task}.md"
    if evidence and task_path not in changed_paths:
        problems.append(f"GitHub PR did not include {task_path}")
    merge_oid = str((evidence.get("mergeCommit") or {}).get("oid") or "")
    merged_task = _git_process(root, "show", f"{merge_oid}:{task_path}") if merge_oid else None
    if merged_task is None or merged_task.returncode != 0:
        problems.append(f"unable to read {task_path} from the verified merge commit")
    else:
        merged_body = merged_task.stdout
        merged_status_match = re.search(r"^Status:[ \t]*(\S+)", merged_body, flags=re.M)
        merged_status = merged_status_match.group(1) if merged_status_match else ""
        problems.extend(_completion_evidence_problems(
            merged_body, task=task, status=merged_status, not_before_ns=0,
            require_completed_ns=False,
        ))

    gate_doc = root / WORKFLOW_DIR / GATES_DIR / f"{task}.md"
    prior_gate = _read(gate_doc)
    same_reconciliation = (
        t.get("status") == "done"
        and "- Source: github-merge" in prior_gate
        and f"- Pull request: {evidence.get('url') or ''}" in prior_gate
        and f"- Merge commit: {merge_oid}" in prior_gate
    )
    if t.get("status") == "done" and not same_reconciliation:
        problems.append(f"task {task} is already done; existing gate evidence will not be overwritten")
    if problems:
        print("agentctl: GitHub merge reconciliation rejected:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1

    ts = _now()
    merged_by = str((evidence.get("mergedBy") or {}).get("login") or "")
    if same_reconciliation:
        print(f"agentctl: {task} already reconciled from this merged GitHub PR")
        return 0
    if t.get("status") != "done":
        t["status"] = "done"
        t["updated_at"] = ts
        _save_board(root, board)
        _check_plan_box(root, task)
        _set_task_doc_status(root, task, "done")
        _update_tasks_index(
            root, task, status="done", owner=t.get("owner"), scope=t.get("scope"), title=t.get("title"),
        )
    _write(
        gate_doc,
        f"# Gate {task}\n\n- Decision: approved\n- Source: github-merge\n"
        f"- By: {merged_by}\n- Recorded-by: {recorder}\n- Recorder task: {recorder_task}\n"
        f"- Pull request: {evidence.get('url') or ''}\n- Base branch: {evidence.get('baseRefName') or ''}\n"
        f"- Merge commit: {merge_oid}\n- Merged at: {evidence.get('mergedAt') or ''}\n"
        f"- Reconciled at: {ts}\n- Note: {args.note or 'none'}\n",
    )
    recorder_session["last_gate"] = {
        "task": task, "decision": "approved", "source": "github-merge", "at": ts,
    }
    recorder_session["doc_hashes"] = _hash_docs(root, recorder_task)
    _save_session(root, recorder_session)
    print(f"agentctl: {task} reconciled from merged GitHub PR -> done")
    return 0


def cmd_gate(args: argparse.Namespace) -> int:
    root = _repo_root()
    try:
        coordination_fd = _acquire_lock_file(_session_coordination_lock_path(root))
    except TimeoutError as exc:
        print(f"agentctl: {exc}", file=sys.stderr)
        return 2
    try:
        return _cmd_gate_unlocked(root, args)
    finally:
        _release_lock_file(_session_coordination_lock_path(root), coordination_fd)


def _cmd_gate_unlocked(root: Path, args: argparse.Namespace) -> int:
    changed = _check_receipt(root)
    if changed:
        print("agentctl: gate blocked because required workflow documents changed:", file=sys.stderr)
        for problem in changed:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    task = args.task
    board = _load_board(root)
    t = board.get("tasks", {}).get(task)
    if not t:
        print(f"agentctl: task {task} not found on board", file=sys.stderr)
        print(
            "  if the task finished in a task worktree, its ledger lives on "
            "that branch; sync it into this checkout first: "
            "'agentctl reconcile merge-back --from-ref <branch>' "
            "(see docs/worktree-merge-back.md)",
            file=sys.stderr,
        )
        return 2
    if args.action == "reconcile-github":
        return _gate_reconcile_github(root, args, board, task, t)
    reviewer = (args.by or "").strip()
    reviewer_session = _load_session(root)
    reviewer_profile = _agent_profile(root, reviewer)
    reviewer_role = str(reviewer_profile.get("role") or "").lower()
    reviewer_task = str(reviewer_session.get("task") or "")
    reviewer_runtime = _runtime_identity()
    recorded_runtimes = reviewer_session.get("runtime_identities")
    if not isinstance(recorded_runtimes, list):
        recorded_runtimes = []
    session_runtimes = {
        str(item) for item in recorded_runtimes if str(item).strip()
    }
    legacy_session_runtime = str(reviewer_session.get("runtime_identity") or "")
    if legacy_session_runtime:
        session_runtimes.add(legacy_session_runtime)
    completion = _extract_section(
        _read(root / WORKFLOW_DIR / TASKS_DIR / f"{task}.md"), "## Completion Record",
    )
    worker_runtime_match = re.search(r"^- Worker-runtimes:\s*(.+)$", completion, flags=re.M)
    worker_runtimes = {
        item.strip() for item in (worker_runtime_match.group(1).split(",") if worker_runtime_match else [])
        if item.strip()
    }
    review_problems = []
    if reviewer_session.get("agent") != reviewer:
        review_problems.append(
            f"active reviewer session is {reviewer_session.get('agent') or 'missing'}, expected {reviewer}"
        )
    if not reviewer_task or _task_status(root, reviewer_task) != "in_progress":
        review_problems.append("reviewer must have an active in_progress planning/review task")
    if reviewer_task == task:
        review_problems.append("reviewer session cannot own the task being decided")
    if reviewer == (t.get("owner") or ""):
        review_problems.append("task owner cannot approve or reject their own task")
    if not reviewer_runtime:
        review_problems.append("reviewer host runtime identity is unavailable")
    elif reviewer_runtime not in session_runtimes:
        review_problems.append("active reviewer session is not bound to the current host runtime")
    if not worker_runtimes:
        review_problems.append(
            "worker completion has no host runtime evidence; if the task "
            "finished in a worktree, sync its ledger into this checkout "
            "first ('agentctl reconcile merge-back --from-ref <branch>'), "
            "otherwise finish it again with the current kit"
        )
    elif reviewer_runtime in worker_runtimes:
        review_problems.append("reviewer host runtime participated in the worker task and is not independent")
    if not any(label in reviewer_role for label in ("supervisor", "planning", "review")):
        review_problems.append(
            f"reviewer {reviewer or '<missing>'} is not registered with a "
            f"supervisor/planning/review role; register it first with "
            f"'agentctl agents add --id {reviewer or '<reviewer>'} --role review', "
            f"then run 'agentctl refresh' in the reviewer session"
        )
    if review_problems:
        print("agentctl: independent gate decision rejected:", file=sys.stderr)
        for problem in review_problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    ts = _now()
    gate_doc = root / WORKFLOW_DIR / GATES_DIR / f"{task}.md"
    if args.action == "approve":
        if t.get("status") not in ("review", "approved"):
            print(f"agentctl: {task} is '{t.get('status')}', must be 'review' to approve", file=sys.stderr)
            return 1
        doc = root / WORKFLOW_DIR / TASKS_DIR / f"{task}.md"
        if "Completed-at:" not in _extract_section(_read(doc), "## Completion Record"):
            print(f"agentctl: {task} has no completion record; run 'agentctl finish' first.", file=sys.stderr)
            return 1
        t["status"] = "done"
        t["updated_at"] = ts
        _save_board(root, board)
        _check_plan_box(root, task)
        _set_task_doc_status(root, task, "done")
        _update_tasks_index(root, task, status="done", owner=t.get("owner"), scope=t.get("scope"), title=t.get("title"))
        _write(
            gate_doc,
            f"# Gate {task}\n\n- Decision: approved\n- By: {reviewer}\n"
            f"- Reviewer task: {reviewer_task}\n- Reviewer session started: "
            f"{reviewer_session.get('started_at') or ''}\n- Reviewer runtime: {reviewer_runtime}\n"
            f"- Worker runtimes: {', '.join(sorted(worker_runtimes))}\n"
            f"- At: {ts}\n- Note: {args.note or 'none'}\n",
        )
        st = _load_session(root)
        st["last_gate"] = {"task": task, "decision": "approved", "at": ts}
        st["doc_hashes"] = _hash_docs(root, st.get("task"))
        _save_session(root, st)
        print(f"agentctl: {task} approved -> done")
        return 0
    t["status"] = "blocked"
    t["updated_at"] = ts
    _save_board(root, board)
    _set_task_doc_status(root, task, "blocked")
    _update_tasks_index(root, task, status="blocked", owner=t.get("owner"), scope=t.get("scope"), title=t.get("title"))
    _write(
        gate_doc,
        f"# Gate {task}\n\n- Decision: rejected\n- By: {reviewer}\n"
        f"- Reviewer task: {reviewer_task}\n- Reviewer session started: "
        f"{reviewer_session.get('started_at') or ''}\n- Reviewer runtime: {reviewer_runtime}\n"
        f"- Worker runtimes: {', '.join(sorted(worker_runtimes))}\n"
        f"- At: {ts}\n- Note: {args.note or 'none'}\n",
    )
    st = _load_session(root)
    st["last_gate"] = {"task": task, "decision": "rejected", "at": ts}
    st["doc_hashes"] = _hash_docs(root, st.get("task"))
    _save_session(root, st)
    print(f"agentctl: {task} rejected -> blocked")
    return 0


def cmd_refresh(args: argparse.Namespace) -> int:
    root = _repo_root()
    st = _load_session(root)
    if not st.get("task"):
        print("agentctl: no active session to refresh", file=sys.stderr)
        return 2
    st["doc_hashes"] = _hash_docs(root, st["task"])
    st["refreshed_at"] = _now()
    _record_runtime_identity(st)
    _save_session(root, st)
    print(f"agentctl: refreshed read receipt for {st['task']}")
    return 0


def cmd_board(args: argparse.Namespace) -> int:
    root = _repo_root()
    board = _load_board(root)
    if args.json:
        print(json.dumps(board, indent=2, ensure_ascii=False))
        return 0
    tasks = board.get("tasks", {})
    if not tasks:
        print("agentctl: board is empty")
        return 0
    print(f"Task Board (updated {board.get('updated_at', '')}):")
    for tid in sorted(tasks):
        t = tasks[tid]
        owner = t.get("owner") or "-"
        scope = ",".join(t.get("scope") or []) or "-"
        print(f"  {tid:<10} {t.get('status', '?'):<12} owner={owner:<12} scope={scope:<24} {t.get('title', '')}")
    return 0


def _task_create(root: Path, args: argparse.Namespace) -> int:
    lock = _session_coordination_lock_path(root)
    fd = _acquire_lock_file(lock)
    try:
        return _task_create_unlocked(root, args)
    finally:
        _release_lock_file(lock, fd)


def _task_create_unlocked(root: Path, args: argparse.Namespace) -> int:
    task = args.id
    if not TASK_RECORD_ID_RE.fullmatch(task):
        print(
            "agentctl: task id must use uppercase letters/digits with one safe "
            "dash-separated suffix (for example T-101 or EXP-GPU-A)",
            file=sys.stderr,
        )
        return 2
    title = args.title or task
    owner = args.owner or ""
    scope = [s.strip() for s in (args.scope or "").split(",") if s.strip()]
    scope_problems = _scope_errors(scope)
    if scope_problems:
        for problem in scope_problems:
            print(f"agentctl: {problem}", file=sys.stderr)
        return 2
    deps = [d.strip() for d in (args.deps or "").split(",") if d.strip()]
    invalid_deps = [dep for dep in deps if not TASK_RECORD_ID_RE.fullmatch(dep)]
    if invalid_deps:
        print(
            "agentctl: invalid dependency task id(s): " + ", ".join(invalid_deps),
            file=sys.stderr,
        )
        return 2
    task_type = str(getattr(args, "task_type", None) or "generic")
    if task_type not in TASK_TYPES:
        print(f"agentctl: unsupported task type: {task_type}", file=sys.stderr)
        return 2
    now = _now()
    board = _load_board(root)
    doc = root / WORKFLOW_DIR / TASKS_DIR / f"{task}.md"
    if task in board.get("tasks", {}) and not args.force:
        print(f"agentctl: task {task} already exists (use --force)", file=sys.stderr)
        return 1
    if task not in board.get("tasks", {}) and not args.force:
        # A task document (live or archived) or an active claim proves the id
        # is taken even when this checkout's board has not caught up. Refuse
        # instead of silently re-registering someone else's task.
        if doc.is_file():
            print(
                f"agentctl: task document .agent/tasks/{task}.md already exists "
                f"but {task} is not on this checkout's board; the board is "
                "likely out of date. Sync the checkout or pick another id "
                "(--force re-registers the board entry and keeps the document)",
                file=sys.stderr,
            )
            return 1
        foreign = _foreign_task_claim(root, task)
        if foreign:
            print(
                f"agentctl: task id {task} is already claimed by {foreign}; "
                "pick another id (--force overrides)",
                file=sys.stderr,
            )
            return 1
    board.setdefault("tasks", {})[task] = {"title": title, "type": task_type, "status": "todo",
                                           "owner": owner or None, "scope": scope, "deps": deps,
                                           "created_at": now, "updated_at": now}
    _save_board(root, board)
    if not doc.is_file():
        tmpl = _read(root / WORKFLOW_DIR / TASKS_DIR / "_template.md")
        if tmpl:
            body = string.Template(tmpl).safe_substitute(
                task_id=task, title=title, owner=owner or "unassigned",
                agent=owner or "unassigned", created_at=now, updated_at=now,
                scope=", ".join(scope) or "TBD")
        else:
            body = f"# {task} - {title}\n\nStatus: todo\n"
        _write(doc, body)
    tasks_md = root / WORKFLOW_DIR / TASKS_FILE
    ttext = _read(tasks_md)
    row = f"| {task} | todo | {owner or '-'} | `{', '.join(scope) or '-'}` | [.agent/tasks/{task}.md](tasks/{task}.md) | {title} |"
    task_rows = _tasks_index_rows(ttext)
    if "| ID |" in ttext and task not in task_rows:
        _write(tasks_md, ttext.rstrip("\n") + "\n" + row + "\n")
    elif task in task_rows:
        _update_tasks_index(root, task, status="todo", owner=owner or "-", scope=scope, title=title)
    plan = root / WORKFLOW_DIR / PLAN_FILE
    ptext = _read(plan)
    bullet = f"- [ ] {task} - {title}" + (f" (owner: {owner})" if owner else "")
    has_plan_bullet = re.search(rf"^- \[[ x]\][^\n]*{_task_word_re(task)}", ptext, flags=re.M)
    if "## Task Board" in ptext and not has_plan_bullet:
        _write(plan, re.sub(r"(## Task Board\n)", rf"\1{bullet}\n", ptext, count=1))
    print(f"agentctl: created {task} ({title})")
    return 0


def _task_show(root: Path, args: argparse.Namespace) -> int:
    task = args.id
    t = _load_board(root).get("tasks", {}).get(task)
    doc = root / WORKFLOW_DIR / TASKS_DIR / f"{task}.md"
    if t:
        print(json.dumps({task: t}, indent=2, ensure_ascii=False))
    if doc.is_file():
        print("\n--- task doc ---")
        print(_read(doc))
    elif not t:
        print(f"agentctl: task {task} not found", file=sys.stderr)
        return 2
    return 0


def cmd_task(args: argparse.Namespace) -> int:
    root = _repo_root()
    if args.task_action == "create":
        return _task_create(root, args)
    if args.task_action == "show":
        return _task_show(root, args)
    print("agentctl: unknown task action", file=sys.stderr)
    return 2


def cmd_agents(args: argparse.Namespace) -> int:
    root = _repo_root()
    data = _load_agents(root)
    if args.agents_action == "list":
        if args.json:
            print(json.dumps(data, indent=2, ensure_ascii=False))
            return 0
        ags = data.get("agents", {})
        if not ags:
            print("agentctl: no agents defined")
            return 0
        for aid in sorted(ags):
            a = ags[aid]
            scope = ",".join(a.get("write_scope") or []) or "-"
            model = a.get("model") or "-"
            effort = a.get("reasoning_effort") or "-"
            session_id = a.get("session_id") or "-"
            print(
                f"  {aid:<14} role={a.get('role', '-')} backend={a.get('backend', '-')} "
                f"model={model} reasoning={effort} session={session_id} scope={scope}"
            )
        return 0
    if args.agents_action == "add":
        data.setdefault("agents", {})[args.id] = {
            "role": args.role or "",
            "backend": args.backend or "any",
            "write_scope": [s.strip() for s in (args.scope or "").split(",") if s.strip()],
            "tools": [t.strip() for t in (args.tools or "").split(",") if t.strip()],
            "model": args.model or "",
            "reasoning_effort": args.reasoning_effort or "",
            "session_id": args.session_id or "",
        }
        _save_agents(root, data)
        print(f"agentctl: registered agent {args.id}")
        return 0
    print("agentctl: unknown agents action", file=sys.stderr)
    return 2


def _safe_segment(value: str) -> str:
    value = value.strip() or "unknown"
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-") or "unknown"


def _guidance_packet_paths(root: Path, packet: dict) -> tuple[Path, Path]:
    pid = packet["id"]
    from_agent = _safe_segment(packet.get("from_agent") or packet.get("by") or "supervisor")
    to_agent = _safe_segment(packet.get("to_agent") or "agent")
    return (
        _bus_dir(root, BUS_OUTBOX) / from_agent / f"{pid}.json",
        _bus_dir(root, BUS_INBOX) / to_agent / f"{pid}.json",
    )


def _guidance_matches_worker(pkt: dict, to_agent: str | None = None,
                             session_id: str | None = None,
                             model: str | None = None,
                             reasoning_effort: str | None = None) -> bool:
    if to_agent and pkt.get("to_agent") != to_agent:
        return False
    target_session = pkt.get("to_session") or ""
    if target_session and target_session != (session_id or ""):
        return False
    target_model = pkt.get("to_model") or ""
    if target_model and model and target_model != model:
        return False
    target_effort = pkt.get("to_reasoning_effort") or ""
    if target_effort and target_effort != (reasoning_effort or ""):
        return False
    return True


def _append_guidance_doc(root: Path, packet: dict) -> None:
    from_agent = _safe_segment(packet.get("from_agent") or "supervisor")
    to_agent = _safe_segment(packet.get("to_agent") or "agent")
    path = root / WORKFLOW_DIR / "handoffs" / f"guidance-{from_agent}-to-{to_agent}.md"
    task = packet.get("task") or "-"
    plan = (packet.get("plan") or "").strip() or "-"
    block = (
        f"\n## {packet['created_at']} - {packet['id']}\n\n"
        f"- From Agent: {packet.get('from_agent') or '-'}\n"
        f"- To Agent: {packet.get('to_agent') or '-'}\n"
        f"- To Model: {packet.get('to_model') or '-'}\n"
        f"- To Reasoning Effort: {packet.get('to_reasoning_effort') or '-'}\n"
        f"- To Session: {packet.get('to_session') or '-'}\n"
        f"- Task: {task}\n"
        f"- Summary: {packet.get('summary') or '-'}\n"
        f"- Artifacts: {', '.join(packet.get('artifacts') or []) or '-'}\n"
        f"- Packet: `.agent/{BUS_DIR}/{BUS_INBOX}/{to_agent}/{packet['id']}.json`\n\n"
        "### Plan\n\n"
        f"{plan}\n"
    )
    if not path.exists():
        _write(path, f"# Supervisor Guidance {from_agent} -> {to_agent}\n" + block)
    else:
        _write(path, _read(path).rstrip() + "\n" + block)


def _open_guidance_packets(root: Path, to_agent: str | None = None, task: str | None = None,
                           task_specific_only: bool = False,
                           session_id: str | None = None,
                           model: str | None = None,
                           reasoning_effort: str | None = None) -> list[tuple[Path, dict]]:
    packets: list[tuple[Path, dict]] = []
    inbox = _bus_dir(root, BUS_INBOX)
    if not inbox.is_dir():
        return packets
    for path in sorted(inbox.rglob("*.json")):
        pkt = _load_json(path, {})
        if pkt.get("kind") != GUIDANCE_KIND or pkt.get("status") != "ready":
            continue
        if not _guidance_matches_worker(
            pkt, to_agent=to_agent, session_id=session_id, model=model,
            reasoning_effort=reasoning_effort,
        ):
            continue
        pkt_task = pkt.get("task") or ""
        pkt_to_task = pkt.get("to_task") or ""
        if task:
            if task_specific_only:
                if pkt_task != task and pkt_to_task != task:
                    continue
            elif pkt_task and pkt_task != task and pkt_to_task != task:
                continue
        packets.append((path, pkt))
    return packets


def _guidance_packets(root: Path, agent: str | None = None, task: str | None = None,
                      status: str | None = None,
                      session_id: str | None = None,
                      model: str | None = None) -> list[tuple[Path, dict]]:
    packets: list[tuple[Path, dict]] = []
    seen: set[str] = set()
    for kind in (BUS_INBOX, BUS_DONE, BUS_FAILED):
        base = _bus_dir(root, kind)
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.json")):
            pkt = _load_json(path, {})
            if pkt.get("kind") != GUIDANCE_KIND:
                continue
            if pkt.get("id") in seen:
                continue
            if status and pkt.get("status") != status:
                continue
            if agent and agent not in {pkt.get("from_agent"), pkt.get("to_agent")}:
                continue
            if session_id and session_id not in {pkt.get("from_session"), pkt.get("to_session")}:
                continue
            if model and model not in {pkt.get("from_model"), pkt.get("to_model")}:
                continue
            if task and task not in {pkt.get("task"), pkt.get("to_task")}:
                continue
            pkt["_path"] = str(path.relative_to(root))
            pkt["_box"] = kind
            packets.append((path, pkt))
            if pkt.get("id"):
                seen.add(pkt["id"])
    return packets


def _guidance_plan_from_args(root: Path, args: argparse.Namespace) -> str:
    plan = args.plan or ""
    if args.plan_file:
        if args.plan_file == "-":
            file_text = sys.stdin.read()
        else:
            plan_path = Path(args.plan_file)
            if not plan_path.is_absolute():
                plan_path = root / plan_path
            file_text = _read(plan_path)
        plan = (plan + "\n" + file_text).strip() if plan else file_text
    return plan.strip()


def _guidance_dispatch_state_dir(root: Path) -> Path:
    return _state_dir(root) / "dispatch"


def _guidance_acceptance_dir(root: Path) -> Path:
    common = _git_common_dir(root)
    if common is None:
        raise ValueError("guidance acceptance requires a Git repository")
    return common / WORKTREE_LEASES_DIR / GUIDANCE_ACCEPTANCE_DIR


def _create_binary_secret(path: Path, payload: bytes) -> bool:
    """Create one private secret without Windows text-mode byte translation."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    try:
        fd = os.open(path, flags, 0o600)
    except FileExistsError:
        return False
    try:
        remaining = memoryview(payload)
        while remaining:
            written = os.write(fd, remaining)
            if written <= 0:
                raise OSError("unable to write complete secret")
            remaining = remaining[written:]
        os.fsync(fd)
    finally:
        os.close(fd)
    return True


def _guidance_signing_key(root: Path, *, create: bool) -> bytes:
    common = _git_common_dir(root)
    if common is None:
        raise ValueError("guidance evidence signing requires a Git repository")
    path = common / WORKTREE_LEASES_DIR / GUIDANCE_SIGNING_KEY_FILE
    try:
        key = path.read_bytes()
    except FileNotFoundError:
        if not create:
            raise ValueError("guidance signing key is unavailable; rerun dispatch from this checkout")
        path.parent.mkdir(parents=True, exist_ok=True)
        generated = secrets.token_bytes(32)
        key = generated if _create_binary_secret(path, generated) else path.read_bytes()
    except OSError as exc:
        raise ValueError(f"unable to read guidance signing key: {exc}") from exc
    if len(key) < 32:
        raise ValueError("guidance signing key is invalid")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return key


def _guidance_record_signature(record: dict, key: bytes) -> str:
    unsigned = dict(record)
    unsigned.pop("integrity", None)
    payload = json.dumps(
        unsigned, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False,
    ).encode("utf-8")
    return hmac.new(key, payload, hashlib.sha256).hexdigest()


def _guidance_sign_record(record: dict, key: bytes) -> None:
    # Sign the same plain JSON value types that will be read back from disk.
    # This also prevents platform-specific scalar subclasses from changing the
    # canonical payload between signing and verification.
    persisted = json.loads(json.dumps(record, ensure_ascii=False, allow_nan=False))
    record.clear()
    record.update(persisted)
    record["integrity"] = {
        "algorithm": "hmac-sha256",
        "signature": _guidance_record_signature(record, key),
    }


def _guidance_verify_record(root: Path, record: dict, label: str) -> None:
    integrity = record.get("integrity")
    if not isinstance(integrity, dict) or integrity.get("algorithm") != "hmac-sha256":
        raise ValueError(f"{label} has no supported supervisor integrity signature")
    signature = integrity.get("signature")
    if not isinstance(signature, str) or not re.fullmatch(r"[0-9a-f]{64}", signature):
        raise ValueError(f"{label} has an invalid supervisor integrity signature")
    expected = _guidance_record_signature(record, _guidance_signing_key(root, create=False))
    if not hmac.compare_digest(signature, expected):
        raise ValueError(f"{label} failed supervisor integrity verification")


def _guidance_contract(packet: dict) -> dict:
    """Return the immutable supervisor-to-worker contract for one dispatch."""
    return {
        key: packet.get(key) or ([] if key == "artifacts" else "")
        for key in (
            "id", "kind", "created_at", "from_agent", "from_model", "from_session",
            "to_agent", "to_model", "to_reasoning_effort", "to_session", "task",
            "summary", "plan", "artifacts",
        )
    }


def _guidance_contract_digest(contract: dict) -> str:
    payload = json.dumps(
        contract, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _guidance_file_digest(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


def _guidance_dispatch_prompt(packet: dict) -> str:
    packet_id = packet.get("id") or "unknown"
    to_agent = packet.get("to_agent") or "codex"
    task = packet.get("task") or packet.get("to_task") or "unassigned"
    model = packet.get("to_model") or "configured session model"
    effort = packet.get("to_reasoning_effort") or "configured session effort"
    session_id = packet.get("to_session") or "configured session"
    plan = (packet.get("plan") or "").strip()
    if len(plan) > GUIDANCE_DISPATCH_PROMPT_MAX:
        plan = plan[:GUIDANCE_DISPATCH_PROMPT_MAX].rstrip() + "\n[plan truncated; read the packet for the remainder]"
    return (
        "按 .agent 规范开始工作。\n\n"
        f"You are the implementation worker `{to_agent}` (model `{model}`, reasoning `{effort}`, "
        f"session `{session_id}`).\n"
        f"A supervisor dispatched guidance packet `{packet_id}` for task `{task}`.\n"
        "Before editing, follow `.agent/WORKFLOW_ENTRY.md`. Confirm this worktree has the matching "
        "task session; if it does not, enter that task with `agentctl work` using your agent, model, "
        "and session identity. Do not steal another live task lock.\n"
        f"Read the local packet with `python3 tools/agentctl.py guidance show {packet_id}` when it is "
        "present. The full supervisor plan is also included below so direction still arrives when "
        "packet visibility across worktrees lags; a missing local packet is not an acknowledgement.\n\n"
        f"Supervisor summary: {packet.get('summary') or '-'}\n\n"
        "Supervisor plan:\n"
        f"{plan or '-'}\n\n"
        "Execute the bounded task, run its verification, and record meaningful progress with "
        "`agentctl note`. Acknowledge the guidance only after incorporating it, then use "
        "`agentctl finish` when the task is genuinely ready for review. Obey the repository Git "
        "standards; do not merge or bypass hooks automatically."
    )


def _update_guidance_dispatch(root: Path, packet_id: str, dispatch: dict) -> dict:
    updated: dict = {}
    for path in _matching_packet_paths(root, packet_id):
        packet = _load_json(path, {})
        if packet.get("kind") != GUIDANCE_KIND:
            continue
        packet["dispatch"] = dict(dispatch)
        _save_json(path, packet)
        updated = packet
    return updated


def _update_guidance_route(root: Path, packet_id: str, *, session_id: str,
                           model: str, reasoning_effort: str) -> dict:
    updated: dict = {}
    for path in _matching_packet_paths(root, packet_id):
        packet = _load_json(path, {})
        if packet.get("kind") != GUIDANCE_KIND:
            continue
        packet["to_session"] = session_id
        if model:
            packet["to_model"] = model
        if reasoning_effort:
            packet["to_reasoning_effort"] = reasoning_effort
        _save_json(path, packet)
        updated = packet
    return updated


def _dispatch_output_tail(text: str) -> str:
    text = (text or "").strip()
    if len(text) <= GUIDANCE_DISPATCH_OUTPUT_CAP:
        return text
    return "[truncated]\n" + text[-GUIDANCE_DISPATCH_OUTPUT_CAP:]


def _guidance_dispatch(root: Path, args: argparse.Namespace) -> int:
    path = _find_packet(root, args.packet)
    if not path:
        print(f"agentctl: guidance packet not found: {args.packet}", file=sys.stderr)
        return 2
    packet = _load_json(path, {})
    if packet.get("kind") != GUIDANCE_KIND:
        print(f"agentctl: packet is not supervisor guidance: {args.packet}", file=sys.stderr)
        return 2
    if packet.get("status") != "ready":
        print(
            f"agentctl: guidance {packet.get('id')} is '{packet.get('status')}', must be 'ready' to dispatch",
            file=sys.stderr,
        )
        return 1

    transport = args.transport or "codex-cli"
    if transport != "codex-cli":
        print(f"agentctl: unsupported guidance transport '{transport}'", file=sys.stderr)
        return 2
    session_id = args.session_id or packet.get("to_session") or ""
    model = args.model or packet.get("to_model") or ""
    reasoning_effort = (
        getattr(args, "reasoning_effort", "")
        or packet.get("to_reasoning_effort")
        or ""
    )
    if not session_id:
        print(
            "agentctl: codex-cli dispatch requires a target session; set --to-session when creating "
            "guidance or pass --session-id to guidance dispatch.",
            file=sys.stderr,
        )
        return 2
    if packet.get("from_session") and packet.get("from_session") == session_id:
        print("agentctl: refusing to dispatch guidance back into its source session", file=sys.stderr)
        return 1
    if reasoning_effort and not REASONING_EFFORT_RE.fullmatch(reasoning_effort):
        print(
            "agentctl: reasoning effort must be a lowercase identifier such as low, high, or xhigh",
            file=sys.stderr,
        )
        return 2
    try:
        timeout = int(args.timeout or GUIDANCE_DISPATCH_TIMEOUT_DEFAULT)
    except (TypeError, ValueError):
        print("agentctl: dispatch timeout must be an integer number of seconds", file=sys.stderr)
        return 2
    if timeout < 1 or timeout > GUIDANCE_DISPATCH_TIMEOUT_MAX:
        print(
            f"agentctl: dispatch timeout must be between 1 and {GUIDANCE_DISPATCH_TIMEOUT_MAX} seconds",
            file=sys.stderr,
        )
        return 2

    prior = packet.get("dispatch") if isinstance(packet.get("dispatch"), dict) else {}
    attempts = int(prior.get("attempts") or 0) + 1
    prompt_packet = dict(packet)
    prompt_packet["to_session"] = session_id
    if model:
        prompt_packet["to_model"] = model
    if reasoning_effort:
        prompt_packet["to_reasoning_effort"] = reasoning_effort
    prompt = _guidance_dispatch_prompt(prompt_packet)
    state_dir = _guidance_dispatch_state_dir(root)
    last_message = state_dir / (
        f"{_safe_segment(packet['id'])}-attempt-{attempts}-last-message.txt"
    )
    codex_bin = shutil.which("codex") or "codex"
    command = [codex_bin, "exec", "resume"]
    if model:
        command.extend(["--model", model])
    if reasoning_effort:
        command.extend(["--config", f'model_reasoning_effort="{reasoning_effort}"'])
    command.extend(["--output-last-message", str(last_message), session_id, "-"])

    print(f"agentctl: dispatch command: {shlex.join(command[:-1] + ['<guidance-prompt>'])}")
    if args.dry_run:
        print("agentctl: guidance dispatch dry-run; no Codex process started")
        return 0
    if args.session_id or args.model or getattr(args, "reasoning_effort", ""):
        packet = _update_guidance_route(
            root,
            packet["id"],
            session_id=session_id,
            model=model,
            reasoning_effort=reasoning_effort,
        ) or packet

    started_at = _now()
    started_at_ns = time.time_ns()
    running = {
        "transport": transport,
        "status": "running",
        "attempts": attempts,
        "started_at": started_at,
        "started_at_ns": started_at_ns,
        "finished_at": "",
        "exit_code": None,
        "session_id": session_id,
        "model": model,
        "reasoning_effort": reasoning_effort,
        "last_message": str(last_message.relative_to(root)),
    }
    _update_guidance_dispatch(root, packet["id"], running)
    state_dir.mkdir(parents=True, exist_ok=True)

    stdout = ""
    stderr = ""
    exit_code = 1
    failure = ""
    child_env = os.environ.copy()
    for name in (
        SESSION_ID_ENV,
        SESSION_OWNER_RUNTIME_ENV,
        SESSION_INSTANCE_ENV,
        PARENT_SESSION_KEY_ENV,
        SESSION_ISOLATION_ERROR_ENV,
        "AGENT_WORKFLOW_SESSION_KEY",
        "CODEX_THREAD_ID",
        "CLAUDE_CODE_SESSION_ID",
        "CURSOR_CONVERSATION_ID",
        "WHALENT_AGENT_ID",
        "WHALENT_CODEX_INSTANCE_ID",
        "WHALENT_COMPOSER_ID",
        "WHALENT_FORK_SOURCE_AGENT_ID",
        "AGENT_SESSION_ID",
        "TERM_SESSION_ID",
    ):
        child_env.pop(name, None)
    child_env[SESSION_ID_ENV] = session_id
    try:
        popen_args = {}
        if os.name == "posix":
            popen_args["start_new_session"] = True
        elif os.name == "nt":
            popen_args["creationflags"] = getattr(
                subprocess, "CREATE_NEW_PROCESS_GROUP", 0,
            )
        proc = subprocess.Popen(
            command, cwd=str(root), text=True, encoding="utf-8", errors="replace",
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=child_env,
            **popen_args,
        )
        try:
            _attach_windows_kill_job(proc)
        except OSError:
            _terminate_loop_process(proc)
            try:
                proc.communicate(timeout=5)
            except subprocess.SubprocessError:
                pass
            raise
        try:
            stdout, stderr = proc.communicate(input=prompt, timeout=timeout)
            exit_code = proc.returncode
        except subprocess.TimeoutExpired:
            _terminate_loop_process(proc)
            try:
                stdout, stderr = proc.communicate(timeout=5)
            except subprocess.TimeoutExpired as cleanup_exc:
                stdout = cleanup_exc.stdout if isinstance(cleanup_exc.stdout, str) else ""
                stderr = cleanup_exc.stderr if isinstance(cleanup_exc.stderr, str) else ""
                try:
                    proc.kill()
                except OSError:
                    pass
            failure = f"codex dispatch timed out after {timeout}s"
            exit_code = 124
        finally:
            _close_windows_job(proc)
        if exit_code:
            failure = failure or f"codex exited with status {exit_code}"
    except OSError as exc:
        failure = f"failed to start codex: {exc}"
        stderr = str(exc)
        exit_code = 1

    finished_at = _now()
    finished_at_ns = time.time_ns()
    receipt = {
        "version": 1,
        "packet": packet["id"],
        "contract": _guidance_contract(prompt_packet),
        "contract_sha256": _guidance_contract_digest(_guidance_contract(prompt_packet)),
        "transport": transport,
        "status": "succeeded" if exit_code == 0 else "failed",
        "attempts": attempts,
        "started_at": started_at,
        "started_at_ns": started_at_ns,
        "finished_at": finished_at,
        "finished_at_ns": finished_at_ns,
        "exit_code": exit_code,
        "session_id": session_id,
        "model": model,
        "reasoning_effort": reasoning_effort,
        "last_message": str(last_message.relative_to(root)),
        "failure": failure,
        "stdout_tail": _dispatch_output_tail(stdout),
        "stderr_tail": _dispatch_output_tail(stderr),
    }
    try:
        _guidance_sign_record(receipt, _guidance_signing_key(root, create=True))
    except ValueError as exc:
        print(f"agentctl: unable to sign guidance receipt: {exc}", file=sys.stderr)
        receipt["status"] = "failed"
        receipt["exit_code"] = 1
        receipt["failure"] = str(exc)
        exit_code = 1
    _save_json(state_dir / f"{_safe_segment(packet['id'])}.json", receipt)
    persistent = {key: receipt[key] for key in (
        "transport", "status", "attempts", "started_at", "started_at_ns",
        "finished_at", "finished_at_ns", "exit_code",
        "session_id", "model", "reasoning_effort", "last_message", "failure",
    )}
    _update_guidance_dispatch(root, packet["id"], persistent)

    if exit_code == 0:
        print(f"agentctl: guidance {packet['id']} dispatched successfully to Codex session {session_id}")
        if last_message.is_file():
            final_text = _dispatch_output_tail(_read(last_message))
            if final_text:
                print("\n[Codex final message]\n" + final_text)
        return 0
    print(f"agentctl: guidance dispatch failed: {failure}", file=sys.stderr)
    detail = _dispatch_output_tail(stderr or stdout)
    if detail:
        print(detail, file=sys.stderr)
    return 1


def _completion_evidence_problems(
    body: str, *, task: str, status: str, not_before_ns: int,
    require_completed_ns: bool = True,
) -> list[str]:
    problems: list[str] = []
    if status not in {"review", "approved", "done"}:
        problems.append(f"task {task} status is {status or 'missing'}, expected review/approved/done")
    doc_status_match = re.search(r"^Status:[ \t]*(\S+)", body, flags=re.M)
    doc_status = doc_status_match.group(1) if doc_status_match else ""
    if doc_status != status:
        problems.append(
            f"task {task} document status is {doc_status or 'missing'}, "
            f"board status is {status or 'missing'}"
        )
    section = _extract_section(body, "## Completion Record")
    summary = re.search(r"^- Summary:[ \t]*(.+)$", section, flags=re.M)
    tests = re.search(r"^- Tests:[ \t]*(.+)$", section, flags=re.M)
    completed = re.search(r"^- Completed-at:[ \t]*(.+)$", section, flags=re.M)
    completed_ns = re.search(r"^- Completed-at-ns:[ \t]*(\d+)$", section, flags=re.M)
    if not summary or not summary.group(1).strip():
        problems.append("task completion summary is missing")
    missing_test_markers = {"", "-", "n/a", "na", "none", "not recorded", "not run"}
    test_evidence = tests.group(1).strip().lower() if tests else ""
    missing_test_prefix = re.match(
        r"^(?:not run|not recorded|n/?a|none)(?:\b|\s*[:;,({\[-])",
        test_evidence,
    )
    if test_evidence in missing_test_markers or missing_test_prefix:
        problems.append("task verification evidence is missing")
    if not completed or not completed.group(1).strip():
        problems.append("task completion timestamp is missing")
    if not completed_ns and require_completed_ns:
        problems.append("task completion lacks dispatch-bound nanosecond evidence")
    elif completed_ns and int(completed_ns.group(1)) < not_before_ns:
        problems.append("task completion predates this dispatch attempt")
    return problems


def _guidance_completion_evidence(
    root: Path, task: str, *, not_before_ns: int,
) -> tuple[bool, list[str]]:
    status = _task_status(root, task)
    path = root / WORKFLOW_DIR / TASKS_DIR / f"{task}.md"
    body = _read(path)
    if not body:
        problems = [f"task document is missing: {path.relative_to(root)}"]
        return False, problems
    problems = _completion_evidence_problems(
        body, task=task, status=status, not_before_ns=not_before_ns,
    )
    return not problems, problems


def _guidance_verify(root: Path, args: argparse.Namespace) -> int:
    target_root = Path(args.target or root).expanduser().resolve()
    if not target_root.is_dir():
        print(f"agentctl: guidance verify target is not a directory: {target_root}", file=sys.stderr)
        return 2
    if _git_common_dir(target_root) != _git_common_dir(root):
        print("agentctl: guidance verify target must belong to the supervisor repository", file=sys.stderr)
        return 2
    path = _find_packet(target_root, args.packet)
    if not path:
        print(f"agentctl: guidance packet not found: {args.packet}", file=sys.stderr)
        return 2
    packet = _load_json(path, {})
    supervisor_path = _find_packet(root, args.packet)
    supervisor_packet = _load_json(supervisor_path, {}) if supervisor_path else {}
    if packet.get("kind") != GUIDANCE_KIND:
        print(f"agentctl: packet is not supervisor guidance: {args.packet}", file=sys.stderr)
        return 2
    reviewer = (args.by or "").strip()
    if not reviewer:
        print("agentctl: guidance verify requires --by <supervisor>", file=sys.stderr)
        return 2
    problems: list[str] = []
    reviewer_session = _load_session(root)
    reviewer_profile = _agent_profile(root, reviewer)
    reviewer_role = (reviewer_profile.get("role") or "").lower()
    if reviewer_session.get("agent") != reviewer:
        problems.append(
            f"active reviewer session is {reviewer_session.get('agent') or 'missing'}, expected {reviewer}"
        )
    if not any(label in reviewer_role for label in ("supervisor", "planning", "review")):
        problems.append(
            f"reviewer {reviewer} is not registered with a supervisor/planning/review role; "
            f"register it first with 'agentctl agents add --id {reviewer} --role review'"
        )
    try:
        if _managed_worktree_lease(root):
            problems.append("guidance verification must run from the supervisor checkout, not a worker lease")
    except RuntimeError as exc:
        problems.append(f"unable to verify supervisor checkout ownership: {exc}")
    if reviewer == packet.get("to_agent"):
        problems.append("worker cannot verify its own dispatched turn")
    task = packet.get("task") or packet.get("to_task") or ""
    dispatch = packet.get("dispatch") if isinstance(packet.get("dispatch"), dict) else {}
    receipt_path = _guidance_dispatch_state_dir(target_root) / f"{_safe_segment(packet.get('id') or '')}.json"
    receipt: dict = {}
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        if not isinstance(receipt, dict):
            raise ValueError("receipt is not an object")
        _guidance_verify_record(root, receipt, f"guidance receipt {packet.get('id')}")
    except FileNotFoundError:
        problems.append("signed dispatch receipt is missing")
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        problems.append(f"dispatch receipt is invalid: {exc}")
        receipt = {}
    if receipt:
        contract = receipt.get("contract")
        contract_digest = receipt.get("contract_sha256")
        if not isinstance(contract, dict) or not isinstance(contract_digest, str):
            problems.append("signed receipt has no valid guidance contract")
            contract = {}
        elif _guidance_contract_digest(contract) != contract_digest:
            problems.append("signed receipt guidance contract digest is invalid")
            contract = {}
        if contract:
            task = contract.get("task") or ""
            if _guidance_contract_digest(_guidance_contract(packet)) != contract_digest:
                problems.append("worker guidance packet differs from the signed dispatch contract")
            if not supervisor_packet:
                problems.append("supervisor guidance packet is missing")
            elif _guidance_contract_digest(_guidance_contract(supervisor_packet)) != contract_digest:
                problems.append("supervisor guidance packet differs from the signed dispatch contract")
        expected = {
            "packet": contract.get("id") if contract else packet.get("id"),
            "transport": "codex-cli",
            "status": "succeeded",
            "exit_code": 0,
            "session_id": (contract.get("to_session") if contract else packet.get("to_session")) or "",
            "model": (contract.get("to_model") if contract else packet.get("to_model")) or "",
            "reasoning_effort": (
                contract.get("to_reasoning_effort") if contract
                else packet.get("to_reasoning_effort")
            ) or "",
        }
        for key, value in expected.items():
            if receipt.get(key) != value:
                problems.append(
                    f"receipt {key} is {receipt.get(key)!r}, expected {value!r}"
                )
        for key in (
            "transport", "status", "attempts", "started_at", "started_at_ns",
            "finished_at", "finished_at_ns", "exit_code",
            "session_id", "model", "reasoning_effort", "last_message", "failure",
        ):
            if dispatch.get(key) != receipt.get(key):
                problems.append(f"packet dispatch metadata differs from receipt field {key}")
        last_message = receipt.get("last_message") or ""
        final_path = target_root / last_message if last_message else None
        if not final_path or not final_path.is_file() or not _read(final_path).strip():
            problems.append("Codex final-message evidence is missing or empty")
    contract = receipt.get("contract") if isinstance(receipt.get("contract"), dict) else {}
    target_agent = contract.get("to_agent") or packet.get("to_agent") or ""
    if not task:
        problems.append("guidance packet has no task")
    if reviewer_session.get("task") == task:
        problems.append("reviewer session cannot own the worker task being verified")
    if packet.get("status") != "done":
        problems.append("worker has not acknowledged the guidance")
    if packet.get("acknowledged_by") != target_agent:
        problems.append(
            f"guidance was acknowledged by {packet.get('acknowledged_by') or 'nobody'}, "
            f"expected {target_agent or 'target worker'}"
        )
    if task and packet.get("acknowledged_task") != task:
        problems.append(
            f"guidance acknowledgement targets {packet.get('acknowledged_task') or 'no task'}, "
            f"expected {task}"
        )
    started_at_ns = receipt.get("started_at_ns") if isinstance(receipt, dict) else None
    if not isinstance(started_at_ns, int) or isinstance(started_at_ns, bool) or started_at_ns <= 0:
        problems.append("dispatch receipt has no valid nanosecond start marker")
        started_at_ns = time.time_ns()
    acknowledged_at_ns = packet.get("acknowledged_at_ns")
    if not isinstance(acknowledged_at_ns, int) or isinstance(acknowledged_at_ns, bool):
        problems.append("guidance acknowledgement lacks dispatch-bound nanosecond evidence")
    elif acknowledged_at_ns < started_at_ns:
        problems.append("guidance acknowledgement predates this dispatch attempt")
    if task:
        owner = ((_load_board(target_root).get("tasks") or {}).get(task) or {}).get("owner") or ""
        if owner and owner != target_agent:
            problems.append(
                f"task {task} owner is {owner}, "
                f"expected target worker {target_agent or 'missing'}"
            )
        _ok, task_problems = _guidance_completion_evidence(
            target_root, task, not_before_ns=started_at_ns,
        )
        problems.extend(task_problems)

    target_status = _git_process(target_root, "status", "--porcelain", "--untracked-files=all")
    target_head = _git(target_root, "rev-parse", "HEAD")
    target_tree = _git(target_root, "rev-parse", "HEAD^{tree}")
    if target_status.returncode:
        problems.append(f"unable to inspect worker checkout status: {target_status.stderr.strip()}")
    elif target_status.stdout.strip():
        problems.append("worker checkout has uncommitted evidence; commit the bounded turn before verification")
    if not target_head or not target_tree:
        problems.append("worker checkout has no committed HEAD/tree evidence")
    task_doc = target_root / WORKFLOW_DIR / TASKS_DIR / f"{task}.md"
    final_message = receipt.get("last_message") if receipt else ""
    final_path = target_root / final_message if final_message else Path()
    accepted = not problems
    receipt_integrity = receipt.get("integrity") if isinstance(receipt, dict) else {}
    if not isinstance(receipt_integrity, dict):
        receipt_integrity = {}
    record = {
        "version": 1,
        "id": f"guidance-acceptance-{secrets.token_hex(6)}",
        "packet": packet.get("id"),
        "task": task,
        "reviewed_at": _now(),
        "reviewed_by": reviewer,
        "reviewer_task": reviewer_session.get("task") or "",
        "target_root": str(target_root),
        "target_head": target_head,
        "target_tree": target_tree,
        "accepted": accepted,
        "checks": {
            "signed_receipt": bool(receipt) and not any("receipt" in p for p in problems),
            "route_matches": bool(receipt) and not any(
                p.startswith("receipt ") or "dispatch metadata" in p for p in problems
            ),
            "worker_acknowledged": packet.get("status") == "done"
            and packet.get("acknowledged_by") == packet.get("to_agent"),
            "task_evidence_complete": not any(p.startswith("task ") or "task completion" in p
                                                or "task verification" in p for p in problems),
        },
        "problems": problems,
        "evidence": {
            "contract": contract,
            "contract_sha256": receipt.get("contract_sha256") if receipt else "",
            "receipt_sha256": _guidance_file_digest(receipt_path),
            "receipt_signature": receipt_integrity.get("signature"),
            "task_document_sha256": _guidance_file_digest(task_doc),
            "board_sha256": _guidance_file_digest(target_root / WORKFLOW_DIR / BOARD_FILE),
            "final_message_sha256": _guidance_file_digest(final_path) if final_message else "",
        },
    }
    try:
        _guidance_sign_record(record, _guidance_signing_key(root, create=True))
    except ValueError as exc:
        print(f"agentctl: unable to sign guidance acceptance: {exc}", file=sys.stderr)
        return 2
    try:
        acceptance_dir = _guidance_acceptance_dir(root)
    except ValueError as exc:
        print(f"agentctl: {exc}", file=sys.stderr)
        return 2
    record_path = acceptance_dir / f"{record['id']}.json"
    _save_json(record_path, record)
    if args.json:
        print(json.dumps(record, indent=2, ensure_ascii=False))
    elif accepted:
        print(
            f"agentctl: guidance {packet.get('id')} accepted by {reviewer} "
            f"({record_path})"
        )
    else:
        print(f"agentctl: guidance {packet.get('id')} rejected by {reviewer}:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        print(f"agentctl: rejection recorded at {record_path}", file=sys.stderr)
    return 0 if accepted else 1


def _print_guidance_focus(root: Path, agent: str, task: str,
                          session_id: str = "", model: str = "",
                          reasoning_effort: str = "") -> None:
    packets = _open_guidance_packets(root, to_agent=agent, task=task,
                                     session_id=session_id, model=model,
                                     reasoning_effort=reasoning_effort)
    if not packets:
        return
    print("[Supervisor Guidance]\n")
    for path, pkt in packets[:5]:
        task_label = pkt.get("task") or "general"
        print(
            f"- {pkt.get('id')} from {pkt.get('from_agent') or pkt.get('by')} "
            f"to {pkt.get('to_agent')} task={task_label}"
        )
        if pkt.get("to_model") or pkt.get("to_reasoning_effort") or pkt.get("to_session"):
            print(
                f"  Target: model={pkt.get('to_model') or '-'} "
                f"reasoning={pkt.get('to_reasoning_effort') or '-'} "
                f"session={pkt.get('to_session') or '-'}"
            )
        print(f"  Summary: {pkt.get('summary') or '-'}")
        plan_lines = [ln for ln in (pkt.get("plan") or "").strip().splitlines() if ln.strip()]
        if plan_lines:
            excerpt = " / ".join(plan_lines[:3])
            if len(excerpt) > 240:
                excerpt = excerpt[:237] + "..."
            print(f"  Plan excerpt: {excerpt}")
        print(f"  Packet: {path.relative_to(root)}")
        if pkt.get("task") == task or pkt.get("to_task") == task:
            print(f"  Required before finish: python3 tools/agentctl.py guidance ack {pkt.get('id')} --by {agent}")
    if len(packets) > 5:
        print(f"  ... {len(packets) - 5} more guidance packet(s); run agentctl guidance list --agent {agent}")
    print("")


def _guidance_create(root: Path, args: argparse.Namespace) -> int:
    from_agent = args.from_agent or _load_session(root).get("agent") or "supervisor"
    from_profile = _agent_profile(root, from_agent)
    to_agent = args.to_agent
    to_profile = _agent_profile(root, to_agent)
    if not to_agent:
        print("agentctl: guidance create requires --to-agent", file=sys.stderr)
        return 2
    if not args.summary:
        print("agentctl: guidance create requires --summary", file=sys.stderr)
        return 2
    plan = _guidance_plan_from_args(root, args)
    if not plan:
        print("agentctl: guidance create requires --plan or --plan-file", file=sys.stderr)
        return 2
    artifacts = [a.strip() for a in (args.artifact or "").split(",") if a.strip()]
    packet = {
        "version": 1,
        "kind": GUIDANCE_KIND,
        "id": _packet_id(f"guidance-{from_agent}", f"agent-{to_agent}"),
        "created_at": _now(),
        "status": "ready",
        "from_agent": from_agent,
        "from_model": args.from_model or from_profile.get("model", "") or "",
        "from_reasoning_effort": (
            args.from_reasoning_effort or from_profile.get("reasoning_effort", "") or ""
        ),
        "from_session": args.from_session or from_profile.get("session_id", "") or "",
        "to_agent": to_agent,
        "to_model": args.to_model or to_profile.get("model", "") or "",
        "to_reasoning_effort": (
            args.to_reasoning_effort or to_profile.get("reasoning_effort", "") or ""
        ),
        "to_session": args.to_session or to_profile.get("session_id", "") or "",
        "from_task": f"guidance-{from_agent}",
        "to_task": args.task or f"agent-{to_agent}",
        "task": args.task or "",
        "by": from_agent,
        "summary": args.summary,
        "plan": plan,
        "artifacts": artifacts,
        "notes": args.note or "",
    }
    if args.dispatch and not packet["to_session"]:
        print(
            "agentctl: guidance create --dispatch requires --to-session or a target agent profile "
            "with session_id",
            file=sys.stderr,
        )
        return 2
    outbox, inbox = _guidance_packet_paths(root, packet)
    _save_json(outbox, packet)
    _save_json(inbox, packet)
    _append_guidance_doc(root, packet)
    print(f"agentctl: guidance packet created: {packet['id']}")
    print(f"  inbox: {inbox.relative_to(root)}")
    print(f"  outbox: {outbox.relative_to(root)}")
    if packet["to_model"] or packet["to_session"]:
        print(
            f"  target: agent={to_agent} model={packet['to_model'] or '-'} "
            f"reasoning={packet['to_reasoning_effort'] or '-'} "
            f"session={packet['to_session'] or '-'}"
        )
    if args.task:
        print(f"  finish gate: task {args.task} must ack this guidance before completion")
    if args.dispatch:
        return _guidance_dispatch(
            root,
            argparse.Namespace(
                packet=packet["id"],
                transport=args.transport,
                session_id="",
                model="",
                reasoning_effort="",
                timeout=args.timeout,
                dry_run=args.dry_run,
            ),
        )
    return 0


def _guidance_list(root: Path, args: argparse.Namespace) -> int:
    rows = [
        pkt for _path, pkt in _guidance_packets(
            root,
            agent=args.agent,
            task=args.task,
            status=args.status,
            session_id=args.session_id,
            model=args.model,
        )
    ]
    if args.json:
        print(json.dumps({"guidance": rows}, indent=2, ensure_ascii=False))
        return 0
    if not rows:
        print("agentctl: no guidance packets")
        return 0
    for pkt in rows:
        task = pkt.get("task") or "-"
        dispatch = pkt.get("dispatch") if isinstance(pkt.get("dispatch"), dict) else {}
        print(
            f"  {pkt.get('id'):<46} {pkt.get('status', '-'):<8} "
            f"{pkt.get('from_agent', '-')} -> {pkt.get('to_agent', '-')} "
            f"task={task} model={pkt.get('to_model') or '-'} "
            f"reasoning={pkt.get('to_reasoning_effort') or '-'} "
            f"session={pkt.get('to_session') or '-'} "
            f"dispatch={dispatch.get('status') or '-'} box={pkt.get('_box')}"
        )
    return 0


def _guidance_show(root: Path, args: argparse.Namespace) -> int:
    path = _find_packet(root, args.packet)
    if not path:
        print(f"agentctl: guidance packet not found: {args.packet}", file=sys.stderr)
        return 2
    pkt = _load_json(path, {})
    if pkt.get("kind") != GUIDANCE_KIND:
        print(f"agentctl: packet is not supervisor guidance: {args.packet}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(pkt, indent=2, ensure_ascii=False))
    else:
        print(f"Guidance: {pkt.get('id')}")
        print(f"Status: {pkt.get('status')}")
        print(f"Route: {pkt.get('from_agent')} -> {pkt.get('to_agent')}")
        print(f"Target Model: {pkt.get('to_model') or '-'}")
        print(f"Target Reasoning Effort: {pkt.get('to_reasoning_effort') or '-'}")
        print(f"Target Session: {pkt.get('to_session') or '-'}")
        print(f"Task: {pkt.get('task') or '-'}")
        print(f"Created: {pkt.get('created_at')}")
        print(f"Summary: {pkt.get('summary')}")
        print(f"Artifacts: {', '.join(pkt.get('artifacts') or []) or '-'}")
        dispatch = pkt.get("dispatch") if isinstance(pkt.get("dispatch"), dict) else {}
        if dispatch:
            print(
                f"Dispatch: {dispatch.get('status') or '-'} via {dispatch.get('transport') or '-'} "
                f"(attempts={dispatch.get('attempts') or 0}, exit={dispatch.get('exit_code')})"
            )
        print(f"Path: {path.relative_to(root)}")
        print("\nPlan:\n" + ((pkt.get("plan") or "").strip() or "-"))
    return 0


def _guidance_ack(root: Path, args: argparse.Namespace) -> int:
    path = _find_packet(root, args.packet)
    if not path:
        print(f"agentctl: guidance packet not found: {args.packet}", file=sys.stderr)
        return 2
    pkt = _load_json(path, {})
    if pkt.get("kind") != GUIDANCE_KIND:
        print(f"agentctl: packet is not supervisor guidance: {args.packet}", file=sys.stderr)
        return 2
    st = _load_session(root)
    by = args.by or st.get("agent") or pkt.get("to_agent") or "agent"
    bound_route = bool(
        pkt.get("to_session") or pkt.get("to_model")
        or pkt.get("to_reasoning_effort")
    )
    if bound_route:
        mismatches = []
        if not st.get("task"):
            mismatches.append("no active worker task session")
        if st.get("agent") != pkt.get("to_agent") or by != st.get("agent"):
            mismatches.append(
                f"agent is {st.get('agent') or 'missing'}, "
                f"expected {pkt.get('to_agent') or 'target worker'}"
            )
        if pkt.get("to_session") and st.get("session_id") != pkt.get("to_session"):
            mismatches.append(
                f"session is {st.get('session_id') or 'missing'}, expected {pkt.get('to_session')}"
            )
        if pkt.get("to_model") and st.get("model") != pkt.get("to_model"):
            mismatches.append(
                f"model is {st.get('model') or 'missing'}, expected {pkt.get('to_model')}"
            )
        if (pkt.get("to_reasoning_effort")
                and st.get("reasoning_effort") != pkt.get("to_reasoning_effort")):
            mismatches.append(
                "reasoning effort is "
                f"{st.get('reasoning_effort') or 'missing'}, "
                f"expected {pkt.get('to_reasoning_effort')}"
            )
        expected_task = pkt.get("task") or pkt.get("to_task") or ""
        if expected_task and st.get("task") != expected_task:
            mismatches.append(
                f"task is {st.get('task') or 'missing'}, expected {expected_task}"
            )
        if mismatches:
            print(
                "agentctl: session-bound guidance acknowledgement rejected; "
                "enter the exact worker task/session with agentctl work first:",
                file=sys.stderr,
            )
            for mismatch in mismatches:
                print(f"  - {mismatch}", file=sys.stderr)
            return 1
    pkt["status"] = "done"
    pkt["updated_at"] = _now()
    pkt["acknowledged_at_ns"] = time.time_ns()
    pkt["acknowledged_by"] = by
    pkt["acknowledged_task"] = args.task or st.get("task") or pkt.get("task") or ""
    if args.note:
        pkt["notes"] = (pkt.get("notes") or "") + f"\n{pkt['updated_at']}: {args.note}"
    dest = _bus_dir(root, BUS_DONE) / f"{pkt.get('id')}.json"
    _save_json(dest, pkt)
    for stale in _matching_packet_paths(root, pkt.get("id") or args.packet):
        if stale != dest and (BUS_INBOX in stale.parts or BUS_OUTBOX in stale.parts):
            try:
                stale.unlink()
            except OSError:
                pass
    if st.get("task") and (not pkt.get("task") or pkt.get("task") == st.get("task") or pkt.get("to_task") == st.get("task")):
        st["doc_hashes"] = _hash_docs(root, st["task"])
        st["guidance_acknowledged_at"] = pkt["updated_at"]
        _save_session(root, st)
    print(f"agentctl: guidance {pkt.get('id')} acknowledged by {by} ({dest.relative_to(root)})")
    return 0


def cmd_guidance(args: argparse.Namespace) -> int:
    root = _repo_root()
    if args.guidance_action == "create":
        return _guidance_create(root, args)
    if args.guidance_action == "list":
        return _guidance_list(root, args)
    if args.guidance_action == "show":
        return _guidance_show(root, args)
    if args.guidance_action == "ack":
        return _guidance_ack(root, args)
    if args.guidance_action == "dispatch":
        return _guidance_dispatch(root, args)
    if args.guidance_action == "verify":
        return _guidance_verify(root, args)
    print("agentctl: unknown guidance action", file=sys.stderr)
    return 2


def _packet_id(from_task: str, to_task: str) -> str:
    ts = _dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    return f"{ts}-{from_task}-to-{to_task}"


def _packet_paths(root: Path, packet: dict) -> tuple[Path, Path]:
    pid = packet["id"]
    by = packet.get("by") or packet.get("from_task") or "unknown"
    to_task = packet.get("to_task") or "unassigned"
    return (
        _bus_dir(root, BUS_OUTBOX) / by / f"{pid}.json",
        _bus_dir(root, BUS_INBOX) / to_task / f"{pid}.json",
    )


def _find_packet(root: Path, packet_id: str) -> Path | None:
    candidate = Path(packet_id)
    if candidate.is_file():
        return candidate
    for base in (_bus_dir(root, BUS_INBOX), _bus_dir(root, BUS_OUTBOX), _bus_dir(root, BUS_DONE), _bus_dir(root, BUS_FAILED)):
        matches = sorted(base.rglob(f"{packet_id}.json"))
        if matches:
            return matches[0]
        matches = sorted(base.rglob(f"*{packet_id}*.json"))
        if matches:
            return matches[0]
    return None


def _matching_packet_paths(root: Path, packet_id: str) -> list[Path]:
    paths: list[Path] = []
    for base in (_bus_dir(root, BUS_INBOX), _bus_dir(root, BUS_OUTBOX), _bus_dir(root, BUS_DONE), _bus_dir(root, BUS_FAILED)):
        for path in sorted(base.rglob("*.json")):
            if path.name == f"{packet_id}.json" or packet_id in path.stem:
                paths.append(path)
    return paths


def _append_handoff_doc(root: Path, packet: dict) -> None:
    path = root / WORKFLOW_DIR / "handoffs" / f"{packet['from_task']}-to-{packet['to_task']}.md"
    block = (
        f"\n## {packet['created_at']} - {packet['id']}\n\n"
        f"- From: {packet['from_task']}\n"
        f"- To: {packet['to_task']}\n"
        f"- By: {packet.get('by') or '-'}\n"
        f"- Summary: {packet.get('summary') or '-'}\n"
        f"- Artifacts: {', '.join(packet.get('artifacts') or []) or '-'}\n"
        f"- Packet: `.agent/{BUS_DIR}/{BUS_INBOX}/{packet['to_task']}/{packet['id']}.json`\n"
    )
    if not path.exists():
        _write(path, f"# Handoff {packet['from_task']} -> {packet['to_task']}\n" + block)
    else:
        _write(path, _read(path).rstrip() + "\n" + block)


def _handoff_create(root: Path, args: argparse.Namespace) -> int:
    from_task = args.from_task
    to_task = args.to_task
    by = args.by or _load_session(root).get("agent") or from_task
    artifacts = [a.strip() for a in (args.artifact or "").split(",") if a.strip()]
    packet = {
        "version": 1,
        "id": _packet_id(from_task, to_task),
        "created_at": _now(),
        "status": "ready",
        "from_task": from_task,
        "to_task": to_task,
        "by": by,
        "summary": args.summary or "",
        "artifacts": artifacts,
        "notes": args.note or "",
    }
    outbox, inbox = _packet_paths(root, packet)
    _save_json(outbox, packet)
    _save_json(inbox, packet)
    _append_handoff_doc(root, packet)
    print(f"agentctl: handoff packet created: {packet['id']}")
    print(f"  inbox: {inbox.relative_to(root)}")
    print(f"  outbox: {outbox.relative_to(root)}")
    return 0


def _handoff_list(root: Path, args: argparse.Namespace) -> int:
    packets = []
    for kind in (BUS_INBOX, BUS_OUTBOX, BUS_DONE, BUS_FAILED):
        for path in sorted(_bus_dir(root, kind).rglob("*.json")):
            pkt = _load_json(path, {})
            if not pkt:
                continue
            if args.task and args.task not in {pkt.get("from_task"), pkt.get("to_task")}:
                continue
            if args.status and pkt.get("status") != args.status:
                continue
            pkt["_path"] = str(path.relative_to(root))
            pkt["_box"] = kind
            packets.append(pkt)
    if args.json:
        print(json.dumps({"packets": packets}, indent=2, ensure_ascii=False))
        return 0
    if not packets:
        print("agentctl: no handoff packets")
        return 0
    for pkt in packets:
        print(
            f"  {pkt['id']:<34} {pkt.get('status', '-'):<8} "
            f"{pkt.get('from_task', '-')} -> {pkt.get('to_task', '-')} "
            f"box={pkt.get('_box')} artifacts={len(pkt.get('artifacts') or [])}"
        )
    return 0


def _handoff_show(root: Path, args: argparse.Namespace) -> int:
    path = _find_packet(root, args.packet)
    if not path:
        print(f"agentctl: packet not found: {args.packet}", file=sys.stderr)
        return 2
    pkt = _load_json(path, {})
    if args.json:
        print(json.dumps(pkt, indent=2, ensure_ascii=False))
    else:
        print(f"Packet: {pkt.get('id')}")
        print(f"Status: {pkt.get('status')}")
        print(f"Route: {pkt.get('from_task')} -> {pkt.get('to_task')}")
        print(f"By: {pkt.get('by')}")
        print(f"Created: {pkt.get('created_at')}")
        print(f"Summary: {pkt.get('summary')}")
        print(f"Artifacts: {', '.join(pkt.get('artifacts') or []) or '-'}")
        print(f"Path: {path.relative_to(root)}")
    return 0


def _handoff_mark(root: Path, args: argparse.Namespace) -> int:
    path = _find_packet(root, args.packet)
    if not path:
        print(f"agentctl: packet not found: {args.packet}", file=sys.stderr)
        return 2
    pkt = _load_json(path, {})
    status = args.status
    pkt["status"] = status
    pkt["updated_at"] = _now()
    if args.note:
        pkt["notes"] = (pkt.get("notes") or "") + f"\n{pkt['updated_at']}: {args.note}"
    dest_base = _bus_dir(root, BUS_DONE if status == "done" else BUS_FAILED)
    dest = dest_base / f"{pkt.get('id')}.json"
    _save_json(dest, pkt)
    for stale in _matching_packet_paths(root, pkt.get("id") or args.packet):
        if stale != dest and (BUS_INBOX in stale.parts or BUS_OUTBOX in stale.parts):
            try:
                stale.unlink()
            except OSError:
                pass
    print(f"agentctl: packet {pkt.get('id')} -> {status} ({dest.relative_to(root)})")
    return 0


def cmd_handoff(args: argparse.Namespace) -> int:
    root = _repo_root()
    if args.handoff_action == "create":
        return _handoff_create(root, args)
    if args.handoff_action == "list":
        return _handoff_list(root, args)
    if args.handoff_action == "show":
        return _handoff_show(root, args)
    if args.handoff_action == "mark":
        return _handoff_mark(root, args)
    print("agentctl: unknown handoff action", file=sys.stderr)
    return 2


def _worktree_git_timeout() -> int:
    raw = os.environ.get(WORKTREE_GIT_TIMEOUT_ENV, "")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return WORKTREE_GIT_TIMEOUT_DEFAULT
    return value if value > 0 else WORKTREE_GIT_TIMEOUT_DEFAULT


def _cleanup_interrupted_worktree(root: Path, branch: str, path: Path) -> None:
    """Best-effort removal of a checkout that never finished materializing.

    The lease is already marked failed, so leftovers only cost disk; every
    step here may fail without making anything worse.
    """
    for command in (
        ("worktree", "unlock", str(path)),
        ("worktree", "remove", "--force", str(path)),
        ("branch", "-D", branch),
    ):
        try:
            _git_process(root, *command, timeout=60)
        except (subprocess.TimeoutExpired, OSError):
            pass


# ---------- durable submission requests ----------
#
# A creation command can lose its response (client timeout, killed shell,
# machine load) after durable state was already written, and a plain retry
# then creates the work twice. Callers that need an exactly-once boundary
# pass --request-id; the head-side record under the Git common directory is
# the authority for whether that request already created something.
# Modeled on dt's durable submission intent: absent -> preparing ->
# confirmed | rejected, with "preparing"/interrupted records converging
# instead of relaunching.

def _submission_request_paths(
    root: Path, token: str,
) -> tuple[Path, Path] | tuple[None, None]:
    common = _git_common_dir(root)
    if common is None:
        return None, None
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]
    directory = common / WORKTREE_LEASES_DIR / SUBMISSION_REQUESTS_DIR
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"req-{digest}.json", directory / f"req-{digest}.lock"


def _submission_intent_digest(payload: dict) -> str:
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _submission_request_error(token: str) -> str | None:
    if not SUBMISSION_REQUEST_TOKEN_RE.fullmatch(token or ""):
        return (
            "request id must be 1-128 characters of letters, digits, "
            "'.', '_', ':' or '-'"
        )
    return None


def _load_submission_request(record_path: Path) -> dict | None:
    if not record_path.is_file():
        return None
    record = _load_json(record_path, None)
    return record if isinstance(record, dict) else None


def _write_submission_request(record_path: Path, record: dict) -> None:
    record["updated_at"] = _now()
    _save_json(record_path, record)


def _submission_request_begin(
    root: Path,
    token: str,
    intent_digest: str,
    kind: str,
    resolve_existing,
):
    """Open the durable request boundary for one creation attempt.

    Returns (action, record, lock_fd). Actions:
      proceed        caller may create; it must settle the record and release
                     lock_fd afterwards
      replay         same intent already confirmed; return the recorded result
      replay-reject  same intent already rejected; repeat the rejection
      conflict       token reused with a different intent
      blocked        an interrupted attempt cannot be proven settled

    resolve_existing(record) inspects durable state for an interrupted
    attempt and returns the confirmed result payload (dict) if the earlier
    attempt actually completed, else None.
    """
    record_path, lock_path = _submission_request_paths(root, token)
    if record_path is None or lock_path is None:
        return "proceed", None, None
    fd = _acquire_lock_file(lock_path)
    lock_handed_over = False
    try:
        record = _load_submission_request(record_path)
        if record is None:
            record = {
                "version": 1,
                "token": token,
                "kind": kind,
                "intent_digest": intent_digest,
                "state": "preparing",
                "created_at": _now(),
                "result": {},
                "error": "",
            }
            _write_submission_request(record_path, record)
            lock_handed_over = True
            return "proceed", record, fd
        if record.get("intent_digest") != intent_digest or record.get("kind") != kind:
            return "conflict", record, None
        state = record.get("state")
        if state == "confirmed":
            return "replay", record, None
        if state == "rejected":
            return "replay-reject", record, None
        # preparing: the earlier attempt was interrupted mid-flight. Converge
        # from durable state instead of guessing.
        resolved = resolve_existing(record) if resolve_existing else None
        if resolved:
            record["state"] = "confirmed"
            record["result"] = resolved
            _write_submission_request(record_path, record)
            return "replay", record, None
        return "blocked", record, None
    finally:
        if not lock_handed_over:
            _release_lock_file(lock_path, fd)


def _submission_request_note(root: Path, token: str, result: dict) -> None:
    """Record the allocated identity while the caller still holds the lock."""
    record_path, _lock_path = _submission_request_paths(root, token)
    if record_path is None:
        return
    record = _load_submission_request(record_path) or {}
    record["result"] = result
    _write_submission_request(record_path, record)


def _submission_request_settle(
    root: Path,
    token: str,
    lock_fd,
    state: str,
    result: dict | None = None,
    error: str = "",
) -> None:
    record_path, lock_path = _submission_request_paths(root, token)
    if record_path is None or lock_fd is None:
        return
    try:
        record = _load_submission_request(record_path) or {}
        record["state"] = state
        if result is not None:
            record["result"] = result
        if error:
            record["error"] = error[-2000:]
        _write_submission_request(record_path, record)
    finally:
        _release_lock_file(lock_path, lock_fd)


def _submission_request_abandon(root: Path, token: str, lock_fd) -> None:
    """Erase a request that was refused before any durable state existed.

    Admission-style rejections (scope conflicts, busy sessions) are often
    transient, so the same token must be retryable once the caller fixes the
    input; only attempts that crossed the creation boundary keep a record.
    """
    record_path, lock_path = _submission_request_paths(root, token)
    if record_path is None or lock_fd is None:
        return
    try:
        try:
            record_path.unlink()
        except FileNotFoundError:
            pass
    finally:
        _release_lock_file(lock_path, lock_fd)


# ---------- worktree leases ----------

def _worktree_lease_paths(root: Path) -> tuple[Path, Path] | tuple[None, None]:
    common = _git_common_dir(root)
    if common is None:
        return None, None
    directory = common / WORKTREE_LEASES_DIR
    return directory / WORKTREE_LEASES_FILE, directory / WORKTREE_LEASES_LOCK


def _load_worktree_leases(root: Path) -> dict:
    registry, _lock = _worktree_lease_paths(root)
    if registry is None:
        return {"version": 1, "leases": []}
    data = _load_json(registry, {"version": 1, "leases": []})
    if not isinstance(data, dict):
        data = {"version": 1, "leases": []}
    data.setdefault("version", 1)
    if not isinstance(data.get("leases"), list):
        data["leases"] = []
    return data


def _save_worktree_leases(root: Path, data: dict) -> None:
    registry, _lock = _worktree_lease_paths(root)
    if registry is None:
        raise RuntimeError("not inside a Git repository")
    _save_json(registry, data)


def _git_worktrees(root: Path) -> dict[str, dict]:
    proc = _git_process(root, "worktree", "list", "--porcelain")
    if proc.returncode:
        detail = proc.stderr.strip() or proc.stdout.strip() or f"exit {proc.returncode}"
        raise RuntimeError(f"unable to inspect Git worktrees: {detail}")
    rows: dict[str, dict] = {}
    current: dict = {}
    for raw in [*proc.stdout.splitlines(), ""]:
        line = raw.strip()
        if not line:
            path = current.get("path")
            if path:
                rows[str(Path(path).resolve())] = current
            current = {}
            continue
        key, _, value = line.partition(" ")
        if key == "worktree":
            current["path"] = value
        elif key == "HEAD":
            current["head"] = value
        elif key == "branch":
            prefix = "refs/heads/"
            current["branch"] = value[len(prefix):] if value.startswith(prefix) else value
        elif key in {"bare", "detached", "locked", "prunable"}:
            current[key] = value or True
    for resolved_path, row in rows.items():
        if row.get("prunable"):
            continue
        git_dir = _git_process(Path(resolved_path), "rev-parse", "--git-dir")
        if git_dir.returncode or not git_dir.stdout.strip():
            continue
        admin_path = Path(git_dir.stdout.strip())
        if not admin_path.is_absolute():
            admin_path = Path(resolved_path) / admin_path
        row["git_dir"] = str(admin_path.resolve())
    return rows


def _worktree_observed_status(lease: dict, worktrees: dict[str, dict]) -> str:
    status = lease.get("status") or "unknown"
    if status == "released":
        return "released"
    path = lease.get("path")
    if not path:
        return "missing"
    actual = worktrees.get(str(Path(path).resolve()))
    if not actual:
        expected_git_dir = lease.get("git_dir") or ""
        if expected_git_dir and any(
            row.get("git_dir") == expected_git_dir for row in worktrees.values()
        ):
            return "moved"
        expected_branch = lease.get("branch") or ""
        if expected_branch and any(
            row.get("branch") == expected_branch for row in worktrees.values()
        ):
            return "moved"
        return "missing"
    if actual.get("prunable"):
        return "prunable"
    expected_branch = lease.get("branch") or ""
    if expected_branch and actual.get("branch") != expected_branch:
        return "conflict"
    return "active"


def _worktree_rows(root: Path) -> list[dict]:
    registry, lock = _worktree_lease_paths(root)
    if registry is None or lock is None:
        return []
    fd = _acquire_lock_file(lock)
    try:
        data = _load_worktree_leases(root)
        worktrees = _git_worktrees(root)
        if _reconcile_worktree_leases(root, data, worktrees):
            _save_worktree_leases(root, data)
    finally:
        _release_lock_file(lock, fd)
    rows = []
    for lease in data.get("leases") or []:
        if not isinstance(lease, dict):
            continue
        row = dict(lease)
        row["observed_status"] = _worktree_observed_status(lease, worktrees)
        rows.append(row)
    return rows


def _path_contains(parent: Path, child: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def _committed_task(root: Path, base_sha: str, task: str) -> tuple[dict, dict] | None:
    board_text = _git(root, "show", f"{base_sha}:{WORKFLOW_DIR}/{BOARD_FILE}")
    task_text = _git(root, "show", f"{base_sha}:{WORKFLOW_DIR}/{TASKS_DIR}/{task}.md")
    if not board_text or not task_text:
        return None
    try:
        board = json.loads(board_text)
    except json.JSONDecodeError:
        return None
    entry = (board.get("tasks") or {}).get(task)
    return (entry, board) if isinstance(entry, dict) else None


def _reconcile_worktree_leases(root: Path, data: dict,
                               worktrees: dict[str, dict]) -> bool:
    changed = False
    for lease in data.get("leases") or []:
        if not isinstance(lease, dict):
            continue
        observed = _worktree_observed_status(lease, worktrees)
        actual = worktrees.get(str(Path(lease.get("path") or "").resolve())) or {}
        if observed == "active" and not lease.get("git_dir") and actual.get("git_dir"):
            lease["git_dir"] = actual["git_dir"]
            changed = True
        if not lease.get("scope") and lease.get("task") and lease.get("base_sha"):
            prior_task = _committed_task(root, lease["base_sha"], lease["task"])
            if prior_task:
                lease["scope"] = prior_task[0].get("scope") or []
                changed = True
        if lease.get("status") == "creating":
            lease["status"] = "active" if observed == "active" else "failed"
            lease["updated_at"] = _now()
            if observed != "active":
                lease["last_error"] = "creation was interrupted before Git registered the worktree"
            changed = True
    return changed


def _managed_worktree_lease(root: Path) -> dict | None:
    current = str(root.resolve())
    current_git_dir = _git_process(root, "rev-parse", "--git-dir")
    git_dir = ""
    if current_git_dir.returncode == 0 and current_git_dir.stdout.strip():
        git_dir_path = Path(current_git_dir.stdout.strip())
        if not git_dir_path.is_absolute():
            git_dir_path = root / git_dir_path
        git_dir = str(git_dir_path.resolve())
    for lease in _worktree_rows(root):
        if lease.get("status") == "released":
            continue
        leased_path = str(Path(lease.get("path") or "").resolve())
        if leased_path == current or (git_dir and lease.get("git_dir") == git_dir):
            return lease
    return None


def _default_worktree_branch(task: str, agent: str) -> str:
    agent_segment = _safe_segment(agent).lower()[:32]
    return f"feature/{task}-{agent_segment}"


def _default_worktree_path(root: Path, task: str, agent: str) -> Path:
    repository_root = root.resolve()
    pool = repository_root.parent / f"{repository_root.name}-worktrees"
    return pool / f"{task.lower()}-{_safe_segment(agent).lower()}"


def _worktree_bootstrap_task(root: Path, create_args: argparse.Namespace,
                             agent: str, isolation: str) -> int:
    registry, lock = _worktree_lease_paths(root)
    if registry is None or lock is None:
        print("agentctl: automatic task worktrees require a Git repository", file=sys.stderr)
        return 2
    task = str(create_args.id)
    scope = [item.strip() for item in str(create_args.scope or "").split(",") if item.strip()]
    if _scope_errors(scope):
        for problem in _scope_errors(scope):
            print(f"agentctl: {problem}", file=sys.stderr)
        return 2
    base_sha = _git(root, "rev-parse", "HEAD^{commit}")
    if not base_sha:
        print(
            "agentctl: automatic worktree creation requires an initial commit; "
            "commit the installed workflow baseline first",
            file=sys.stderr,
        )
        return 1
    dirty = _git_process(root, "status", "--porcelain", "--untracked-files=all")
    if dirty.returncode:
        print(f"agentctl: unable to inspect planning checkout: {dirty.stderr.strip()}", file=sys.stderr)
        return 2
    if dirty.stdout.strip():
        print(
            "agentctl: automatic worktree creation requires a clean planning checkout; "
            "preserve or commit its current changes first",
            file=sys.stderr,
        )
        return 1
    if isolation == "exclusive":
        peers = [
            row for row in _session_rows_unlocked(root)
            if row.get("observed_status") in {"active", "stale"}
        ]
        if peers:
            print(
                "agentctl: exclusive maintenance cannot start while another session is active or stale",
                file=sys.stderr,
            )
            return 1
    branch = _default_worktree_branch(task, agent)
    path = _default_worktree_path(root, task, agent).resolve()
    lease_id = "wt-" + hashlib.sha256(
        f"bootstrap:{task}:{agent}:{path}:{time.time_ns()}".encode("utf-8")
    ).hexdigest()[:16]
    fd = _acquire_lock_file(lock)
    try:
        data = _load_worktree_leases(root)
        try:
            worktrees = _git_worktrees(root)
        except RuntimeError as exc:
            print(f"agentctl: {exc}", file=sys.stderr)
            return 2
        if _reconcile_worktree_leases(root, data, worktrees):
            _save_worktree_leases(root, data)
        for existing in data.get("leases") or []:
            if not isinstance(existing, dict) or existing.get("status") == "released":
                continue
            observed = _worktree_observed_status(existing, worktrees)
            if existing.get("status") == "failed" and observed == "missing":
                continue
            if existing.get("task") == task:
                print(
                    f"agentctl: task {task} already has worktree lease {existing.get('id')}",
                    file=sys.stderr,
                )
                return 1
            if _scopes_overlap(scope, existing.get("scope") or []):
                print(
                    f"agentctl: task {task} scope overlaps active worktree task "
                    f"{existing.get('task')}; split or serialize the tasks",
                    file=sys.stderr,
                )
                return 1
            if existing.get("branch") == branch or str(
                    Path(existing.get("path") or "").resolve()) == str(path):
                print(
                    f"agentctl: worktree branch or path is already leased by {existing.get('id')}",
                    file=sys.stderr,
                )
                return 1
        if _git(root, "show-ref", "--verify", f"refs/heads/{branch}"):
            print(f"agentctl: worktree branch already exists: {branch}", file=sys.stderr)
            return 1
        if path.exists() and any(path.iterdir() if path.is_dir() else [path]):
            print(f"agentctl: worktree path is not empty: {path}", file=sys.stderr)
            return 1
        lease = {
            "id": lease_id,
            "status": "creating",
            "task": task,
            "agent": agent,
            "scope": scope,
            "task_type": create_args.task_type,
            "isolation": isolation,
            "branch": branch,
            "path": str(path),
            "base": "HEAD",
            "base_sha": base_sha,
            "created_from": str(root.resolve()),
            "created_at": _now(),
            "updated_at": _now(),
            "released_at": None,
            "last_error": None,
        }
        data.setdefault("leases", []).append(lease)
        _save_worktree_leases(root, data)
        git_budget = _worktree_git_timeout()
        try:
            added = _git_process(
                root, "worktree", "add", "-b", branch, str(path), base_sha,
                timeout=git_budget,
            )
        except subprocess.TimeoutExpired:
            # The checkout can take minutes on a loaded machine. Leave an
            # honest failed lease instead of an "active" ghost, and sweep the
            # half-materialized checkout so admission is not blocked.
            lease["status"] = "failed"
            lease["last_error"] = (
                f"git worktree add timed out after {git_budget}s; "
                f"raise {WORKTREE_GIT_TIMEOUT_ENV} on slow machines"
            )
            lease["updated_at"] = _now()
            _save_worktree_leases(root, data)
            _cleanup_interrupted_worktree(root, branch, path)
            print(f"agentctl: {lease['last_error']}", file=sys.stderr)
            return 2
        if added.returncode:
            lease["status"] = "failed"
            lease["last_error"] = (added.stderr or added.stdout).strip()[-2000:]
            lease["updated_at"] = _now()
            _save_worktree_leases(root, data)
            print(f"agentctl: git worktree add failed: {lease['last_error']}", file=sys.stderr)
            return 2
        create_command = [
            sys.executable, str(path / "tools" / "agentctl.py"),
            "task", "create",
            "--id", task,
            "--title", str(create_args.title or task),
            "--owner", str(create_args.owner or agent),
            "--scope", str(create_args.scope),
            "--type", str(create_args.task_type),
        ]
        if create_args.deps:
            create_command.extend(["--deps", str(create_args.deps)])
        try:
            created = subprocess.run(
                create_command, cwd=str(path), text=True, capture_output=True,
                timeout=git_budget,
            )
        except subprocess.TimeoutExpired:
            lease["status"] = "failed"
            lease["last_error"] = (
                f"task bootstrap timed out after {git_budget}s inside {path}"
            )
            lease["updated_at"] = _now()
            _save_worktree_leases(root, data)
            print(f"agentctl: {lease['last_error']}", file=sys.stderr)
            return 2
        if created.returncode:
            lease["status"] = "failed"
            lease["last_error"] = (created.stderr or created.stdout).strip()[-2000:]
            lease["updated_at"] = _now()
            _save_worktree_leases(root, data)
            print(
                "agentctl: worktree exists but task bootstrap failed; inspect "
                f"{path}: {lease['last_error']}",
                file=sys.stderr,
            )
            return 2
        created_worktree = _git_worktrees(root).get(str(path)) or {}
        lease["git_dir"] = created_worktree.get("git_dir")
        lease["status"] = "active"
        lease["updated_at"] = _now()
        _save_worktree_leases(root, data)
    finally:
        _release_lock_file(lock, fd)
    print(f"agentctl: created isolated {create_args.task_type} task {task}")
    print(f"  worktree_lease={lease_id}")
    print(f"  branch={branch}")
    print(f"  path={path}")
    print(
        f"  continue: cd {path} && python3 tools/agentctl.py work "
        f"--agent {agent} --task {task}"
    )
    return 0


def _worktree_create(root: Path, args: argparse.Namespace) -> int:
    registry, lock = _worktree_lease_paths(root)
    if registry is None or lock is None:
        print("agentctl: worktree create requires a Git repository", file=sys.stderr)
        return 2
    task = args.task.strip()
    agent = args.agent.strip()
    if not task or not agent:
        print("agentctl: worktree create requires non-empty --task and --agent values", file=sys.stderr)
        return 2
    base = args.base or "HEAD"
    base_sha = _git(root, "rev-parse", f"{base}^{{commit}}")
    if not base_sha:
        print(f"agentctl: worktree base is not a commit: {base}", file=sys.stderr)
        return 2
    committed_task = _committed_task(root, base_sha, task)
    if not committed_task:
        print(
            f"agentctl: task {task} is not committed at {base}; commit the plan and task document first",
            file=sys.stderr,
        )
        return 1
    task_entry, committed_board = committed_task
    if task_entry.get("status") not in {"todo", "ready"}:
        print(
            f"agentctl: task {task} is {task_entry.get('status')} at {base}; "
            "worktree allocation requires a todo or ready task that the worker can claim",
            file=sys.stderr,
        )
        return 1
    if not _agent_can_take(agent, task_entry):
        print(
            f"agentctl: task {task} is assigned to {task_entry.get('owner')}; "
            f"agent {agent} cannot claim it",
            file=sys.stderr,
        )
        return 1
    if not _deps_satisfied(committed_board, task_entry):
        print(f"agentctl: task {task} has unresolved dependencies at {base}", file=sys.stderr)
        return 1
    task_scope = task_entry.get("scope") or []
    if not task_scope:
        print(f"agentctl: task {task} has no bounded write scope", file=sys.stderr)
        return 1
    scope_problems = _scope_errors(task_scope)
    if scope_problems:
        for problem in scope_problems:
            print(f"agentctl: task {task}: {problem}", file=sys.stderr)
        return 1
    for other_task, other_entry in (committed_board.get("tasks") or {}).items():
        if other_task == task or other_entry.get("status") not in ACTIVE_STATUSES:
            continue
        if _scopes_overlap(task_scope, other_entry.get("scope") or []):
            print(
                f"agentctl: task {task} has a write-scope conflict with {other_task} "
                f"(owner={other_entry.get('owner')})",
                file=sys.stderr,
            )
            return 1
    dirty = _git_process(root, "status", "--porcelain", "--untracked-files=all")
    if dirty.returncode:
        print(f"agentctl: unable to inspect current worktree: {dirty.stderr.strip()}", file=sys.stderr)
        return 2
    if dirty.stdout.strip():
        print("agentctl: worktree create requires a clean committed baseline", file=sys.stderr)
        return 1
    branch = args.branch or _default_worktree_branch(task, agent)
    valid_branch = _git_process(root, "check-ref-format", "--branch", branch)
    if valid_branch.returncode:
        print(f"agentctl: invalid worktree branch: {branch}", file=sys.stderr)
        return 2
    path = Path(args.path).expanduser() if args.path else _default_worktree_path(root, task, agent)
    if not path.is_absolute():
        path = root / path
    path = path.resolve()
    lease_id = "wt-" + hashlib.sha256(
        f"{task}:{agent}:{path}:{time.time_ns()}".encode("utf-8")
    ).hexdigest()[:16]
    fd = _acquire_lock_file(lock)
    try:
        data = _load_worktree_leases(root)
        try:
            worktrees = _git_worktrees(root)
        except RuntimeError as exc:
            print(f"agentctl: {exc}", file=sys.stderr)
            return 2
        if _reconcile_worktree_leases(root, data, worktrees):
            _save_worktree_leases(root, data)
        for existing in data.get("leases") or []:
            if not isinstance(existing, dict):
                continue
            observed = _worktree_observed_status(existing, worktrees)
            if existing.get("status") == "released":
                continue
            if existing.get("status") == "failed" and observed == "missing":
                continue
            if existing.get("task") == task and existing.get("agent") == agent:
                print(
                    f"agentctl: active worktree lease already exists for {task}/{agent}: "
                    f"{existing.get('id')} ({observed})",
                    file=sys.stderr,
                )
                return 1
            if _scopes_overlap(task_scope, existing.get("scope") or []):
                print(
                    f"agentctl: task {task} has a write-scope conflict with active lease "
                    f"{existing.get('id')} (task={existing.get('task')}, "
                    f"agent={existing.get('agent')})",
                    file=sys.stderr,
                )
                return 1
            if existing.get("branch") == branch or str(Path(existing.get("path") or "").resolve()) == str(path):
                print(
                    f"agentctl: worktree branch or path is already leased by {existing.get('id')}",
                    file=sys.stderr,
                )
                return 1
        if _git(root, "show-ref", "--verify", f"refs/heads/{branch}"):
            print(f"agentctl: worktree branch already exists: {branch}", file=sys.stderr)
            return 1
        checkout_paths = {str(root.resolve()), *worktrees.keys()}
        for registered_path in checkout_paths:
            existing_path = Path(registered_path)
            if _path_contains(existing_path, path) or _path_contains(path, existing_path):
                print(
                    f"agentctl: worktree path overlaps registered checkout {existing_path}: {path}",
                    file=sys.stderr,
                )
                return 1
        if path.exists() and any(path.iterdir() if path.is_dir() else [path]):
            print(f"agentctl: worktree path is not empty: {path}", file=sys.stderr)
            return 1
        lease = {
            "id": lease_id,
            "status": "creating",
            "task": task,
            "agent": agent,
            "scope": task_scope,
            "branch": branch,
            "path": str(path),
            "base": base,
            "base_sha": base_sha,
            "created_from": str(root.resolve()),
            "created_at": _now(),
            "updated_at": _now(),
            "released_at": None,
            "last_error": None,
        }
        data.setdefault("leases", []).append(lease)
        _save_worktree_leases(root, data)
        proc = _git_process(root, "worktree", "add", "-b", branch, str(path), base_sha)
        lease["updated_at"] = _now()
        if proc.returncode:
            lease["status"] = "failed"
            lease["last_error"] = (proc.stderr or proc.stdout).strip()[-2000:]
            _save_worktree_leases(root, data)
            print(f"agentctl: git worktree add failed: {lease['last_error']}", file=sys.stderr)
            return 2
        try:
            created = _git_worktrees(root).get(str(path)) or {}
        except RuntimeError as exc:
            lease["last_error"] = str(exc)
            lease["updated_at"] = _now()
            _save_worktree_leases(root, data)
            print(
                f"agentctl: worktree was added but its lease could not be verified: {exc}",
                file=sys.stderr,
            )
            return 2
        lease["git_dir"] = created.get("git_dir")
        lease["status"] = "active"
        _save_worktree_leases(root, data)
    finally:
        _release_lock_file(lock, fd)
    print(f"agentctl: worktree lease {lease_id} active")
    print(f"  task={task} agent={agent}")
    print(f"  branch={branch}")
    print(f"  path={path}")
    print(f"  next: cd {path} && python3 tools/agentctl.py work "
          f"--agent {agent} --task {task}")
    print("  then run scripts/tests there; that checkout is exclusively yours.")
    return 0


def _worktree_list(root: Path, args: argparse.Namespace) -> int:
    try:
        rows = _worktree_rows(root)
    except RuntimeError as exc:
        print(f"agentctl: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps({"worktrees": rows}, indent=2, ensure_ascii=False))
        return 0
    if not rows:
        print("agentctl: no managed worktree leases")
        return 0
    for row in rows:
        print(
            f"  {row.get('id', '-'):<20} {row.get('observed_status', '-'):<10} "
            f"task={row.get('task') or '-'} agent={row.get('agent') or '-'} "
            f"branch={row.get('branch') or '-'} path={row.get('path') or '-'}"
        )
    return 0


def _worktree_release(root: Path, args: argparse.Namespace) -> int:
    registry, lock = _worktree_lease_paths(root)
    if registry is None or lock is None:
        print("agentctl: worktree release requires a Git repository", file=sys.stderr)
        return 2
    fd = _acquire_lock_file(lock)
    try:
        data = _load_worktree_leases(root)
        try:
            worktrees = _git_worktrees(root)
        except RuntimeError as exc:
            print(f"agentctl: {exc}", file=sys.stderr)
            return 2
        if _reconcile_worktree_leases(root, data, worktrees):
            _save_worktree_leases(root, data)
        lease = next(
            (item for item in data.get("leases") or []
             if isinstance(item, dict) and item.get("id") == args.lease),
            None,
        )
        if not lease:
            print(f"agentctl: worktree lease not found: {args.lease}", file=sys.stderr)
            return 2
        if lease.get("status") == "released":
            print(f"agentctl: worktree lease {args.lease} already released")
            return 0
        path = Path(lease.get("path") or "").resolve()
        observed = _worktree_observed_status(lease, worktrees)
        if observed in {"conflict", "moved"}:
            print(
                f"agentctl: worktree lease {args.lease} is {observed}; "
                "inspect it manually",
                file=sys.stderr,
            )
            return 1
        if observed == "active":
            if _path_contains(path, Path.cwd()):
                print(
                    "agentctl: cannot release the worktree containing the current directory; "
                    "run release from another worktree",
                    file=sys.stderr,
                )
                return 1
            status = _git_process(path, "status", "--porcelain", "--untracked-files=all")
            if status.returncode:
                print(f"agentctl: unable to inspect leased worktree: {status.stderr.strip()}", file=sys.stderr)
                return 2
            if status.stdout.strip():
                print(
                    f"agentctl: worktree {path} is dirty; commit or remove its changes before release",
                    file=sys.stderr,
                )
                return 1
            removed = _git_process(root, "worktree", "remove", str(path))
            if removed.returncode:
                print(f"agentctl: git worktree remove failed: {removed.stderr.strip()}", file=sys.stderr)
                return 2
        elif observed in {"prunable", "missing"}:
            if not args.ack_missing:
                print(
                    f"agentctl: worktree lease {args.lease} is {observed} and its contents cannot be "
                    "verified; inspect the path and rerun with --ack-missing",
                    file=sys.stderr,
                )
                return 1
            if observed == "prunable":
                removed = _git_process(root, "worktree", "remove", str(path))
                if removed.returncode:
                    print(f"agentctl: unable to remove prunable worktree metadata: {removed.stderr.strip()}", file=sys.stderr)
                    return 2
        lease["status"] = "released"
        lease["released_at"] = _now()
        lease["updated_at"] = lease["released_at"]
        lease["last_error"] = None
        _save_worktree_leases(root, data)
    finally:
        _release_lock_file(lock, fd)
    print(f"agentctl: worktree lease {args.lease} released; branch {lease.get('branch')} preserved")
    return 0


def cmd_worktree(args: argparse.Namespace) -> int:
    root = _repo_root()
    if args.worktree_action == "create":
        return _worktree_create(root, args)
    if args.worktree_action == "list":
        return _worktree_list(root, args)
    if args.worktree_action == "release":
        return _worktree_release(root, args)
    print("agentctl: unknown worktree action", file=sys.stderr)
    return 2


# ---------- execution, run, and resource leases ----------

def _runtime_lease_paths(root: Path) -> tuple[Path, Path] | tuple[None, None]:
    common = _git_common_dir(root)
    if common is None:
        return None, None
    directory = common / WORKTREE_LEASES_DIR
    return directory / RUNTIME_LEASES_FILE, directory / RUNTIME_LEASES_LOCK


def _runtime_runs_dir(root: Path) -> Path:
    common = _git_common_dir(root)
    base = common / WORKTREE_LEASES_DIR if common is not None else _state_dir(root)
    path = base / RUNTIME_RUNS_DIR
    path.mkdir(parents=True, exist_ok=True)
    return path


def _load_runtime_leases(root: Path) -> dict:
    registry, _lock = _runtime_lease_paths(root)
    if registry is None:
        return {"version": 1, "leases": []}
    data = _load_json(registry, {"version": 1, "leases": []})
    if not isinstance(data, dict):
        data = {"version": 1, "leases": []}
    data.setdefault("version", 1)
    if not isinstance(data.get("leases"), list):
        data["leases"] = []
    return data


def _save_runtime_leases(root: Path, data: dict) -> None:
    registry, _lock = _runtime_lease_paths(root)
    if registry is None:
        raise RuntimeError("execution leases require a Git repository")
    _save_json(registry, data)


def _update_runtime_leases(root: Path, updater) -> dict:
    registry, lock = _runtime_lease_paths(root)
    if registry is None or lock is None:
        raise RuntimeError("execution leases require a Git repository")
    fd = _acquire_lock_file(lock)
    try:
        data = _load_runtime_leases(root)
        updater(data)
        _save_runtime_leases(root, data)
        return data
    finally:
        _release_lock_file(lock, fd)


def _runtime_lease(root: Path, lease_id: str) -> dict | None:
    for lease in _load_runtime_leases(root).get("leases") or []:
        if isinstance(lease, dict) and lease.get("id") == lease_id:
            return dict(lease)
    return None


def _runtime_process_alive(process: dict | None) -> bool:
    if not isinstance(process, dict):
        return False
    return _same_process(process.get("pid"), process.get("birth_marker"))


def _runtime_observed_status(lease: dict) -> str:
    status = str(lease.get("status") or "unknown")
    if status in {"succeeded", "failed", "cancelled", "released", "release_failed"}:
        return status
    if lease.get("kind") != "run":
        return status
    supervisor = lease.get("supervisor_process")
    processes = lease.get("processes") or []
    child = processes[-1] if processes else None
    if lease.get("mode") == "adopted":
        return "running" if _runtime_process_alive(child) else "exited_unknown"
    if _runtime_process_alive(supervisor):
        return status
    if _runtime_process_alive(child):
        return "interrupted"
    return "exited_unknown"


def _execution_lease_rows(root: Path) -> list[dict]:
    rows = []
    for session in _session_rows_unlocked(root):
        session_key = str(session.get("workflow_session_key") or "default")
        rows.append({
            "id": f"conversation:{session_key}",
            "kind": "conversation",
            "task": session.get("task"),
            "holder": {"type": "conversation", "id": session_key},
            "mode": session.get("isolation") or "interactive",
            "checkout": session.get("checkout"),
            "scope": session.get("scope") or [],
            "resources": [],
            "processes": [],
            "heartbeat_at": session.get("heartbeat_at"),
            "status": session.get("observed_status"),
            "lineage": {
                "parent_session_key": session.get("parent_session_key"),
                "session_instance_key": session.get("session_instance_key"),
                "authority_inherited": False,
            },
        })
    try:
        worktrees = _worktree_rows(root)
    except RuntimeError:
        worktrees = []
    for lease in worktrees:
        rows.append({
            "id": f"worktree:{lease.get('id')}",
            "kind": "worktree",
            "task": lease.get("task"),
            "holder": {"type": "worktree", "id": lease.get("id")},
            "mode": "isolated-checkout",
            "checkout": lease.get("path"),
            "scope": lease.get("scope") or [],
            "resources": [],
            "processes": [],
            "heartbeat_at": lease.get("updated_at"),
            "status": lease.get("observed_status"),
        })
    for lease in _load_runtime_leases(root).get("leases") or []:
        if not isinstance(lease, dict):
            continue
        row = dict(lease)
        row["status"] = _runtime_observed_status(row)
        rows.append(row)
    execution = _loop_execution_lease(root, normalize=False)
    if execution:
        rows.append({
            "id": f"loop:{execution.get('token')}",
            "kind": "run",
            "task": execution.get("task"),
            "holder": {
                "type": "conversation",
                "id": execution.get("workflow_session_key") or "unknown",
            },
            "mode": "bounded-loop",
            "checkout": execution.get("checkout") or str(root.resolve()),
            "scope": execution.get("scope") or [],
            "resources": [],
            "processes": [{
                "pid": execution.get("owner_pid"),
                "birth_marker": execution.get("owner_birth_marker"),
            }],
            "heartbeat_at": execution.get("started_at"),
            "status": execution.get("status"),
        })
    return rows


def cmd_lease(args: argparse.Namespace) -> int:
    root = _repo_root()
    rows = _execution_lease_rows(root)
    if args.json:
        print(json.dumps({"version": 1, "leases": rows}, indent=2, ensure_ascii=False))
        return 0
    if not rows:
        print("agentctl: no execution leases")
        return 0
    for row in rows:
        holder = row.get("holder") or {}
        print(
            f"{row.get('id')} kind={row.get('kind')} status={row.get('status')} "
            f"task={row.get('task') or '-'} "
            f"holder={holder.get('type') or '-'}:{holder.get('id') or '-'}"
        )
    return 0


def _runtime_policy(root: Path) -> dict:
    default = {
        "version": 1,
        "artifact_roots": [".agent-artifacts"],
        "runtime_roots": [".cache"],
        "task_types": {},
        "performance": {},
    }
    data = _load_json(root / WORKFLOW_DIR / RUNTIME_POLICY_FILE, default)
    return data if isinstance(data, dict) else default


def _path_within(parent: Path, child: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except (OSError, ValueError):
        return False


def _validate_run_outputs(root: Path, task: str, scope: list[str],
                          values: list[str]) -> tuple[list[str], list[str]]:
    outputs = []
    problems = []
    policy = _runtime_policy(root)
    artifact_roots = []
    for value in policy.get("artifact_roots") or []:
        path = Path(str(value)).expanduser()
        artifact_roots.append(path.resolve() if path.is_absolute() else (root / path).resolve())
    for raw in values:
        candidate = Path(raw).expanduser()
        candidate = candidate.resolve() if candidate.is_absolute() else (root / candidate).resolve()
        allowed = False
        try:
            rel = candidate.relative_to(root.resolve()).as_posix()
        except ValueError:
            rel = ""
        if rel and _path_in_scope(rel, scope):
            allowed = True
        if not allowed:
            for artifact_root in artifact_roots:
                task_root = artifact_root / task
                if _path_within(task_root, candidate):
                    allowed = True
                    break
        if not allowed:
            problems.append(
                f"declared output {raw} is outside task scope and the task-specific "
                f"artifact directory ({WORKFLOW_DIR}/{RUNTIME_POLICY_FILE})"
            )
            continue
        outputs.append(str(candidate))
    if not outputs:
        problems.append("background runs require at least one declared --output")
    return sorted(set(outputs)), problems


def _resource_lock_base_dir() -> Path:
    """Machine-wide directory of local resource locks (one subdir per resource).

    A GPU is a GPU whichever checkout claims it, so these locks are shared
    by every project on the host; the per-checkout lease registry only
    mirrors the ones this checkout holds.
    """
    return Path(
        os.environ.get(RESOURCE_LOCK_ENV)
        or (Path.home() / ".agent-workflow" / "resource-locks")
    ).expanduser()


def _resource_lock_location(resource: str) -> dict:
    digest = hashlib.sha256(resource.encode("utf-8")).hexdigest()
    if resource.startswith(RESOURCE_REMOTE_PREFIX):
        parsed = urlparse(resource)
        if not parsed.hostname or not parsed.path.strip("/"):
            raise ValueError("remote resources must use ssh://<host>/<resource>")
        return {
            "provider": "ssh-mkdir",
            "host": parsed.netloc,
            "path": f"/tmp/agent-workflow-resource-{digest}",
        }
    return {
        "provider": "local-mkdir",
        "path": str((_resource_lock_base_dir() / digest).resolve()),
    }


class _ResourceTelemetryProbe(Protocol):
    def __call__(self, target: dict, timeout_seconds: float) -> dict:
        ...


def _gpu_resource_target(resource: str) -> dict | None:
    local = re.fullmatch(r"gpu:(\d+)", resource)
    if local:
        return {
            "kind": "gpu-local",
            "resource": resource,
            "index": int(local.group(1)),
            "host": platform.node(),
        }
    if not resource.startswith(RESOURCE_REMOTE_PREFIX):
        return None
    parsed = urlparse(resource)
    remote = re.fullmatch(r"gpu:(\d+)", parsed.path.strip("/"))
    remote_host = re.fullmatch(
        r"(?:[A-Za-z0-9_][A-Za-z0-9_.-]*@)?[A-Za-z0-9][A-Za-z0-9_.-]*",
        parsed.netloc,
    )
    if not remote_host or not remote or parsed.params or parsed.query or parsed.fragment:
        return None
    return {
        "kind": "gpu-ssh",
        "resource": resource,
        "index": int(remote.group(1)),
        "host": parsed.netloc,
    }


def _nvidia_smi_sample(command: list[str], target: dict,
                       timeout_seconds: float) -> dict:
    sampled_at_ns = time.time_ns()
    try:
        proc = subprocess.run(
            command, text=True, capture_output=True,
            timeout=max(0.1, float(timeout_seconds)),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {
            "ok": False,
            "target": target,
            "sampled_at_ns": sampled_at_ns,
            "error": str(exc)[:500],
        }
    if proc.returncode:
        detail = proc.stderr.strip() or proc.stdout.strip() or "nvidia-smi failed"
        return {
            "ok": False,
            "target": target,
            "sampled_at_ns": sampled_at_ns,
            "error": detail[-500:],
        }
    lines = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    try:
        utilization, memory = [float(part.strip()) for part in lines[0].split(",")[:2]]
    except (IndexError, TypeError, ValueError):
        return {
            "ok": False,
            "target": target,
            "sampled_at_ns": sampled_at_ns,
            "error": "nvidia-smi returned an invalid utilization,memory sample",
        }
    if not math.isfinite(utilization) or not math.isfinite(memory):
        return {
            "ok": False,
            "target": target,
            "sampled_at_ns": sampled_at_ns,
            "error": "nvidia-smi returned a non-finite sample",
        }
    return {
        "ok": True,
        "target": target,
        "sampled_at_ns": sampled_at_ns,
        "utilization_percent": max(0.0, min(100.0, utilization)),
        "memory_mib": max(0.0, memory),
    }


def _probe_local_gpu(target: dict, timeout_seconds: float) -> dict:
    executable = shutil.which("nvidia-smi") or "nvidia-smi"
    return _nvidia_smi_sample([
        executable,
        "-i", str(target["index"]),
        "--query-gpu=utilization.gpu,memory.used",
        "--format=csv,noheader,nounits",
    ], target, timeout_seconds)


def _probe_ssh_gpu(target: dict, timeout_seconds: float) -> dict:
    return _nvidia_smi_sample([
        "ssh", "-o", "BatchMode=yes", str(target["host"]),
        "nvidia-smi", "-i", str(target["index"]),
        "--query-gpu=utilization.gpu,memory.used",
        "--format=csv,noheader,nounits",
    ], target, timeout_seconds)


_RESOURCE_TELEMETRY_PROBES: dict[str, _ResourceTelemetryProbe] = {
    "gpu-local": _probe_local_gpu,
    "gpu-ssh": _probe_ssh_gpu,
}


def _gpu_watchdog_policy(root: Path, args: argparse.Namespace) -> tuple[dict | None, str | None]:
    configured = _runtime_policy(root).get("gpu_watchdog") or {}
    if not isinstance(configured, dict):
        configured = {}
    explicitly_enabled = bool(getattr(args, "gpu_watchdog", False))
    configured_enabled = configured.get("enabled") is True
    if not explicitly_enabled and not configured_enabled:
        return None, None

    targets = []
    for resource in list(getattr(args, "resource", None) or []):
        target = _gpu_resource_target(str(resource))
        if target:
            targets.append(target)
    if not targets and configured_enabled and not explicitly_enabled:
        return None, None
    if not targets:
        return None, (
            "GPU watchdog requires a canonical --resource gpu:<index> or "
            "ssh://<host>/gpu:<index>"
        )

    def value(name: str, default: float) -> float:
        cli_value = getattr(args, f"gpu_{name}", None)
        raw = cli_value if cli_value is not None else configured.get(name, default)
        return float(raw)

    try:
        policy = {
            "idle_seconds": value("idle_seconds", 600.0),
            "grace_seconds": value("grace_seconds", 300.0),
            "sample_seconds": value("sample_seconds", 15.0),
            "kill_seconds": value("kill_seconds", 30.0),
            "utilization_max": value("utilization_max", 5.0),
            "memory_min_mib": value("memory_min_mib", 1024.0),
            "probe_timeout_seconds": value("probe_timeout_seconds", 10.0),
            "action": (
                getattr(args, "gpu_idle_action", None)
                or configured.get("action")
                or "report"
            ),
        }
    except (TypeError, ValueError):
        return None, "GPU watchdog numeric policy values must be finite numbers"
    bounds = {
        "idle_seconds": (0.0, 30 * 24 * 3600.0),
        "grace_seconds": (0.0, 24 * 3600.0),
        "sample_seconds": (0.01, 3600.0),
        "kill_seconds": (0.1, 3600.0),
        "utilization_max": (0.0, 100.0),
        "memory_min_mib": (0.0, 10_000_000.0),
        "probe_timeout_seconds": (0.1, 300.0),
    }
    for name, (minimum, maximum) in bounds.items():
        current = policy[name]
        if not math.isfinite(current) or not minimum <= current <= maximum:
            return None, f"GPU watchdog {name.replace('_', '-')} is outside {minimum}..{maximum}"
    if policy["action"] not in {"report", "terminate"}:
        return None, "GPU watchdog action must be report or terminate"

    if policy["action"] == "terminate" and any(
        target["kind"] != "gpu-local" for target in targets
    ):
        return None, (
            "automatic GPU termination is host-local; run agentctl on the remote "
            "host or use --gpu-idle-action report"
        )
    policy["targets"] = targets
    return policy, None


def _gpu_watchdog_transition(
    state: dict, policy: dict, sample: dict, *, now_ns: int,
    progress_marker: str, progress_updated_ns: int, exempt_until_ns: int,
) -> tuple[dict, str | None]:
    current = dict(state or {})
    previous_marker = str(current.get("last_progress_marker") or "")
    previous_progress_ns = int(current.get("last_progress_updated_ns") or 0)
    current["last_progress_marker"] = progress_marker
    current["last_progress_updated_ns"] = max(previous_progress_ns, progress_updated_ns)
    current["last_sample"] = sample
    current["updated_at_ns"] = now_ns

    progress_changed = bool(previous_marker and progress_marker != previous_marker)
    progress_changed = progress_changed or progress_updated_ns > previous_progress_ns
    exempt = exempt_until_ns > now_ns
    if progress_changed or exempt:
        current.update({
            "state": "exempt" if exempt else "active",
            "low_samples": 0,
            "low_since_ns": None,
            "grace_since_ns": None,
        })
        return current, None
    if not sample.get("ok"):
        current.update({
            "state": "probe_error",
            "low_samples": 0,
            "low_since_ns": None,
            "grace_since_ns": None,
        })
        return current, None

    idle = (
        float(sample.get("utilization_percent") or 0.0) <= float(policy["utilization_max"])
        and float(sample.get("memory_mib") or 0.0) >= float(policy["memory_min_mib"])
    )
    if not idle:
        current.update({
            "state": "active",
            "low_samples": 0,
            "low_since_ns": None,
            "grace_since_ns": None,
        })
        return current, None

    low_since_ns = int(current.get("low_since_ns") or now_ns)
    low_samples = int(current.get("low_samples") or 0) + 1
    current["low_since_ns"] = low_since_ns
    current["low_samples"] = low_samples
    idle_elapsed = max(0.0, (now_ns - low_since_ns) / 1_000_000_000)
    if low_samples < 2 or idle_elapsed < float(policy["idle_seconds"]):
        current["state"] = "suspected_idle"
        return current, None

    grace_since_ns = current.get("grace_since_ns")
    if grace_since_ns is None:
        current["grace_since_ns"] = now_ns
        current["state"] = "grace"
        return current, None
    grace_elapsed = max(0.0, (now_ns - int(grace_since_ns)) / 1_000_000_000)
    if grace_elapsed < float(policy["grace_seconds"]):
        current["state"] = "grace"
        return current, None
    current["state"] = "reclaimable"
    current["reclaimable_at_ns"] = now_ns
    return current, str(policy["action"])


def _external_resource_acquire(resource: str, owner: dict) -> tuple[dict | None, str | None]:
    try:
        location = _resource_lock_location(resource)
    except ValueError as exc:
        return None, str(exc)
    if location["provider"] == "local-mkdir":
        path = Path(location["path"])
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            path.mkdir()
        except FileExistsError:
            existing, _raw = _read_lock_owner(path)
            detail = existing.get("lease_id") or "unknown owner"
            return None, f"resource {resource} is already locked by {detail}"
        try:
            _save_json(path / "owner.json", owner)
        except Exception:
            try:
                path.rmdir()
            except OSError:
                pass
            raise
        return location, None
    code = "import os,sys; os.mkdir(sys.argv[1])"
    proc = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", location["host"],
         "python3", "-c", code, location["path"]],
        text=True, capture_output=True, timeout=20,
    )
    if proc.returncode:
        detail = proc.stderr.strip() or "remote lock directory already exists"
        return None, f"resource {resource} could not be acquired: {detail}"
    return location, None


def _external_resource_release(provider: dict) -> str | None:
    if provider.get("provider") == "local-mkdir":
        path = Path(provider.get("path") or "")
        try:
            owner = path / "owner.json"
            if owner.exists():
                owner.unlink()
            path.rmdir()
            return None
        except FileNotFoundError:
            return None
        except OSError as exc:
            return str(exc)
    if provider.get("provider") == "ssh-mkdir":
        code = "import os,sys; os.rmdir(sys.argv[1])"
        proc = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", str(provider.get("host") or ""),
             "python3", "-c", code, str(provider.get("path") or "")],
            text=True, capture_output=True, timeout=20,
        )
        if proc.returncode:
            return proc.stderr.strip() or "remote lock release failed"
    return None


# Foreign-lock holder states that the holder's own registry proves dead, so
# the next acquisition may release the lock itself (mirrors the in-checkout
# orphan sweep: released sessions, finished runs, holders missing past the
# registration grace). Stale sessions and vanished checkouts stay with the
# operator because a conversation or a run may still be working there.
FOREIGN_LOCK_AUTO_RELEASE_STATES = frozenset({"released", "terminal", "missing"})
FOREIGN_LOCK_FORCE_RELEASE_STATES = FOREIGN_LOCK_AUTO_RELEASE_STATES | frozenset(
    {"stale", "checkout_gone", "unknown"}
)


def _read_lock_owner(lock_dir: Path) -> tuple[dict, bytes]:
    """Owner record of a local lock directory plus the exact bytes it came from.

    Never raises: a missing, unreadable, non-UTF-8, or non-JSON record is
    an empty dict (with whatever bytes were read), so `doctor` and
    `resource acquire` report a damaged lock instead of crashing on it.
    """
    try:
        raw = (lock_dir / "owner.json").read_bytes()
    except OSError:
        return {}, b""
    try:
        data = json.loads(raw.decode("utf-8"))
    except ValueError:  # UnicodeDecodeError and JSONDecodeError
        return {}, raw
    return (data if isinstance(data, dict) else {}), raw


def _reclaim_lock_dir(lock_dir: Path, expected: bytes) -> str | None:
    """Remove a local lock whose owner record still reads exactly `expected`.

    Two checkouts can classify the same dead lock at the same moment, and
    an acquirer can slip in between classification and removal. Renaming
    the owner record is atomic, so only one releaser gets it; the record
    is then compared byte-for-byte with what was classified, and a fresh
    record written by someone else is put back untouched. Returns an
    error string when nothing was removed.
    """
    owner = lock_dir / "owner.json"
    claimed = lock_dir / f".reclaim-{os.getpid()}-{time.time_ns()}.json"
    try:
        os.rename(owner, claimed)
    except FileNotFoundError:
        return "lock owner record was already removed by another release"
    except OSError as exc:
        return str(exc)
    try:
        current = claimed.read_bytes()
    except OSError as exc:
        return str(exc)
    if current != expected:
        try:
            os.rename(claimed, owner)
        except OSError:
            pass
        return "lock owner record changed while releasing; left in place"
    try:
        claimed.unlink()
        for straggler in lock_dir.glob(".reclaim-*.json"):
            straggler.unlink()
    except OSError as exc:
        return str(exc)
    try:
        lock_dir.rmdir()
    except FileNotFoundError:
        pass
    except OSError as exc:
        return str(exc)
    return None


def _foreign_lock_owner(root: Path, resource: str) -> dict | None:
    """owner.json of a local machine-wide lock that no lease in `root` holds.

    Returns None when the resource is remote, unlocked, or held by a
    live lease of this checkout (the in-checkout paths handle those).
    """
    try:
        location = _resource_lock_location(resource)
    except ValueError:
        return None
    if location.get("provider") != "local-mkdir":
        return None
    owner, _raw = _read_lock_owner(Path(location["path"]))
    if not owner:
        return None
    for item in _load_runtime_leases(root).get("leases") or []:
        if (
            isinstance(item, dict)
            and item.get("kind") == "resource"
            and item.get("status") not in {"released", "release_failed"}
            and resource in (item.get("resources") or [])
        ):
            return None
    return owner


def _foreign_lock_liveness(owner: dict) -> tuple[str, str]:
    """Classify the holder of a machine-wide lock recorded by another checkout.

    The lock's owner.json names the holder's checkout; that checkout's
    lease registry and session records are the evidence. States:
    "live", "stale", "released", "terminal", "missing", "registering"
    (lease not registered yet, inside the grace window), "checkout_gone",
    "remote" (recorded on another host), "unknown" (no checkout recorded,
    or its registry is unreadable). Anything short of hard evidence of
    death is reported, never acted on automatically.
    """
    host = str(owner.get("host") or "")
    if host and host != platform.node():
        return "remote", f"lock was recorded on host {host}; this host cannot verify its holder"
    checkout = str(owner.get("checkout") or "")
    lease_id = str(owner.get("lease_id") or "")
    if not checkout:
        return "unknown", (
            "lock predates holder-checkout recording, so its holder cannot be "
            "verified from another checkout"
        )
    path = Path(checkout)
    try:
        if not path.exists() or not (path / ".git").exists():
            return "checkout_gone", f"holder checkout {checkout} no longer exists"
        # "Cannot read" must never look like "nothing there": on a shared
        # host another user's checkout is typically unreadable, and an
        # empty-looking registry would age into an auto-release that
        # steals a card still in use. _git swallows errors and glob
        # skips unreadable directories, so probe readability explicitly.
        registry, _registry_lock = _runtime_lease_paths(path)
        if registry is None:
            return "unknown", (
                f"holder checkout {checkout} exists but its Git metadata cannot be "
                f"read from here"
            )
        sessions_dir = _session_runtime_dir(path)
        for probe in (registry.parent, registry, sessions_dir):
            if probe.exists() and not os.access(probe, os.R_OK):
                return "unknown", (
                    f"holder checkout {checkout} exists but its runtime state is not "
                    f"readable from here"
                )
        leases = [
            item for item in _load_runtime_leases(path).get("leases") or []
            if isinstance(item, dict)
        ]
        sessions = _session_liveness_index(path)
    except Exception as exc:  # anything unreadable: report, never guess
        return "unknown", f"holder checkout {checkout} runtime state could not be read: {exc}"
    lease = next(
        (item for item in leases
         if item.get("id") == lease_id and item.get("kind") == "resource"),
        None,
    )
    where = f"checkout {checkout}, task {owner.get('task') or '-'}"
    if lease is None:
        created = _parse_workflow_timestamp(owner.get("created_at"))
        holder_type = str(owner.get("holder_type") or "")
        window = _dt.timedelta(minutes=10) if holder_type == "run" else _dt.timedelta(hours=1)
        if created is None or created >= _dt.datetime.now() - window:
            return "registering", (
                f"lease {lease_id} is not registered in {where} yet (inside the "
                f"registration grace window)"
            )
        return "missing", f"lease {lease_id} is not in the registry of {where}"
    if lease.get("status") in {"released", "release_failed"}:
        return "released", f"lease {lease_id} is {lease.get('status')} in {where}"
    runs = {
        str(item.get("id")): item
        for item in leases
        if item.get("kind") == "run" and item.get("id")
    }
    state, detail = _resource_holder_liveness(lease, runs, sessions)
    detail = f"{detail} ({where})"
    if state == "missing":
        return ("missing" if _missing_holder_grace_expired(lease) else "registering"), detail
    if state == "unknown":
        return "live", detail
    return state, detail


def _foreign_lock_recovery_hint(resource: str, state: str) -> str:
    if state == "live":
        return "the holder is live; wait for it or coordinate with that conversation"
    if state == "registering":
        return "retry after the registration grace window if the holder never registers"
    if state == "remote":
        return "release it on the host that recorded it"
    if state in FOREIGN_LOCK_AUTO_RELEASE_STATES:
        return (
            f"the next 'agentctl resource acquire {resource}' releases it, or run "
            f"'agentctl resource release --lock {resource} --force-stale --reason <why>'"
        )
    if state == "unknown":
        return (
            f"after confirming nothing on this host still uses it, run "
            f"'agentctl resource release --lock {resource} --force-stale --reason <why>'"
        )
    return (
        f"after inspecting that checkout, run 'agentctl resource release --lock "
        f"{resource} --force-stale --reason <why>' to break the interlock"
    )


def _release_orphaned_foreign_lock(root: Path, resource: str) -> bool:
    """Drop a machine-wide lock whose foreign holder is proven dead.

    Same evidence rule as `_release_orphaned_resources`, applied to the
    lock of a resource that another checkout on this host recorded and
    never freed (for example a project whose conversation was released
    but whose lease registry the sweep never ran on again).
    """
    if _foreign_lock_owner(root, resource) is None:
        return False
    lock_dir = Path(_resource_lock_location(resource)["path"])
    # Classify the exact bytes that will be compared at removal time, so a
    # record rewritten by a faster releaser or a new acquirer is never
    # mistaken for the dead one that was classified.
    owner, raw = _read_lock_owner(lock_dir)
    if not owner:
        return False
    state, detail = _foreign_lock_liveness(owner)
    if state not in FOREIGN_LOCK_AUTO_RELEASE_STATES:
        return False
    if _reclaim_lock_dir(lock_dir, raw):
        return False
    print(
        f"agentctl: released orphaned machine-wide lock for {resource} "
        f"({owner.get('lease_id') or 'unknown lease'}): {detail}",
        file=sys.stderr,
    )
    return True


def _acquire_rejection_detail(root: Path, resource: str, base_error: str) -> str:
    """Explain who holds a contested resource and how to recover.

    A bare lease id in the rejection forced operators to dig through the
    registry by hand before they could tell a live holder from a dead
    one. The detail names the holder, its liveness, and the exact
    recovery command when the holder is demonstrably gone.
    """
    leases = [
        item for item in _load_runtime_leases(root).get("leases") or []
        if isinstance(item, dict)
    ]
    runs = {
        str(item.get("id")): item
        for item in leases
        if item.get("kind") == "run" and item.get("id")
    }
    holding = next(
        (item for item in leases
         if item.get("kind") == "resource"
         and item.get("status") not in {"released"}
         and resource in (item.get("resources") or [])),
        None,
    )
    if holding is None:
        # The external lock exists but no local lease claims it: another
        # checkout or host owns it. Its owner.json names that checkout,
        # whose registry is the evidence for whether the holder is alive.
        owner = _foreign_lock_owner(root, resource)
        if owner:
            state, detail = _foreign_lock_liveness(owner)
            return (
                f"{base_error}; the lock belongs to "
                f"{owner.get('holder_type') or 'unknown'}:"
                f"{owner.get('holder_id') or 'unknown'} "
                f"(task {owner.get('task') or '-'}, host {owner.get('host') or '-'}, "
                f"since {owner.get('created_at') or '-'}) in another checkout; "
                f"holder is {state}: {detail}; "
                f"{_foreign_lock_recovery_hint(resource, state)}"
            )
        return base_error
    holder = holding.get("holder") or {}
    sessions = _session_liveness_index(root)
    state, detail = _resource_holder_liveness(holding, runs, sessions)
    message = (
        f"{base_error}; lease {holding.get('id')} is held by "
        f"{holder.get('type') or 'unknown'}:{holder.get('id') or 'unknown'} "
        f"(task {holding.get('task') or '-'}, since "
        f"{holding.get('created_at') or '-'}); {detail}"
    )
    provably_dead = state in {"terminal", "released", "stale"} or (
        state == "missing" and _missing_holder_grace_expired(holding)
    )
    if provably_dead:
        message += (
            f"; the holder is not live, so any session may run "
            f"'agentctl resource release {holding.get('id')} --force-stale "
            f"--reason <why>' to break the interlock"
        )
    return message


def _resource_acquire_one(root: Path, task: str, resource: str,
                          holder_type: str, holder_id: str) -> tuple[dict | None, str | None]:
    resource = resource.strip()
    if not resource or not re.fullmatch(r"(?:ssh://)?[A-Za-z0-9_.:/@-]+", resource):
        return None, f"invalid resource identifier: {resource or '<empty>'}"
    lease_id = "resource-" + hashlib.sha256(
        f"{resource}:{holder_type}:{holder_id}:{time.time_ns()}".encode("utf-8")
    ).hexdigest()[:16]
    owner = {
        "lease_id": lease_id,
        "resource": resource,
        "task": task,
        "holder_type": holder_type,
        "holder_id": holder_id,
        "host": platform.node(),
        "pid": os.getpid(),
        # The checkout is what lets another project on this host verify
        # whether the holder is still alive (its lease registry and
        # session records live under that checkout's Git common dir).
        "checkout": str(root.resolve()),
        "created_at": _now(),
    }
    provider, error = _external_resource_acquire(resource, owner)
    if error:
        # The contested lock may be held by a dead run or a released
        # session; releasing demonstrably orphaned leases and retrying
        # once turns the historical "nobody uses the GPU but nobody can
        # claim it" interlock into a self-healing conflict.
        _release_orphaned_resources(root)
        provider, error = _external_resource_acquire(resource, owner)
    if error and _release_orphaned_foreign_lock(root, resource):
        provider, error = _external_resource_acquire(resource, owner)
    if error:
        return None, _acquire_rejection_detail(root, resource, error)
    lease = {
        "id": lease_id,
        "kind": "resource",
        "task": task,
        "holder": {"type": holder_type, "id": holder_id},
        "mode": provider.get("provider"),
        "checkout": str(root.resolve()),
        "scope": [],
        "resources": [resource],
        "processes": [],
        "provider": provider,
        "heartbeat_at": _now(),
        "created_at": _now(),
        "status": "active",
    }
    try:
        _update_runtime_leases(root, lambda data: data.setdefault("leases", []).append(lease))
    except Exception:
        _external_resource_release(provider)
        raise
    return lease, None


def _resource_release_by_id(
    root: Path, lease_id: str, reason: str, *,
    holder_type: str, holder_id: str,
) -> tuple[bool, str]:
    lease = _runtime_lease(root, lease_id)
    if not lease or lease.get("kind") != "resource":
        return False, f"resource lease not found: {lease_id}"
    if lease.get("status") == "released":
        return True, "already released"
    lease_holder = lease.get("holder") or {}
    lease_holder_type = str(lease_holder.get("type") or "")
    lease_holder_id = str(lease_holder.get("id") or "")
    if (holder_type, holder_id) != (lease_holder_type, lease_holder_id):
        return (
            False,
            f"resource lease {lease_id} belongs to "
            f"{lease_holder_type or 'unknown'}:{lease_holder_id or 'unknown'}",
        )
    release_error = _external_resource_release(lease.get("provider") or {})

    def update(data: dict) -> None:
        for item in data.get("leases") or []:
            if isinstance(item, dict) and item.get("id") == lease_id:
                item["status"] = "release_failed" if release_error else "released"
                item["released_at"] = _now()
                item["release_reason"] = reason
                item["release_error"] = release_error
                item["heartbeat_at"] = _now()
                break

    _update_runtime_leases(root, update)
    return release_error is None, release_error or "released"


def _release_session_resources(root: Path, session_key: str,
                               reason: str) -> tuple[list[str], list[str]]:
    """Release every active resource lease held by a session.

    Called when the session itself is released: holder binding is
    fail-closed, so without this sweep the resources of a released
    session could only be freed by the orphan grace path an hour later,
    or by hand. Returns (released ids, failure descriptions).
    """
    held = [
        dict(item) for item in _load_runtime_leases(root).get("leases") or []
        if isinstance(item, dict)
        and item.get("kind") == "resource"
        and item.get("status") not in {"released", "release_failed"}
        and (item.get("holder") or {}).get("type") == "conversation"
        and str((item.get("holder") or {}).get("id") or "") == session_key
    ]
    released: list[str] = []
    failures: list[str] = []
    for item in held:
        lease_id = str(item.get("id") or "")
        error = _external_resource_release(item.get("provider") or {})

        def update(data: dict, lease_id=lease_id, error=error) -> None:
            for row in data.get("leases") or []:
                if not isinstance(row, dict) or row.get("id") != lease_id:
                    continue
                if row.get("status") in {"released", "release_failed"}:
                    continue
                row["status"] = "release_failed" if error else "released"
                row["released_at"] = _now()
                row["release_reason"] = reason
                row["release_error"] = error
                row["heartbeat_at"] = _now()
                break

        try:
            _update_runtime_leases(root, update)
        except TimeoutError:
            failures.append(f"{lease_id}: lease registry lock timed out")
            continue
        if error:
            failures.append(f"{lease_id}: {error}")
        else:
            released.append(lease_id)
    return released, failures


def _resource_release_stale(root: Path, lease_id: str, reason: str,
                            forced_by: str) -> tuple[bool, str]:
    """Release a resource lease whose holder is demonstrably not live.

    The escape hatch for interlocks the automatic sweeps will not touch
    (stale sessions keep their resources because the conversation may
    still be working). Live holders are always refused, so this cannot
    steal a resource that is genuinely in use.
    """
    lease = _runtime_lease(root, lease_id)
    if not lease or lease.get("kind") != "resource":
        return False, f"resource lease not found: {lease_id}"
    if lease.get("status") == "released":
        return True, "already released"
    leases = [
        item for item in _load_runtime_leases(root).get("leases") or []
        if isinstance(item, dict)
    ]
    runs = {
        str(item.get("id")): item
        for item in leases
        if item.get("kind") == "run" and item.get("id")
    }
    state, detail = _resource_holder_liveness(
        lease, runs, _session_liveness_index(root),
    )
    if state in {"live", "unknown"}:
        return False, (
            f"resource lease {lease_id} holder is still live ({detail}); "
            f"forced release only applies to stale, released, terminal, or "
            f"missing holders"
        )
    if state == "missing" and not _missing_holder_grace_expired(lease):
        # run start registers the run lease after acquiring resources, so
        # a young missing holder may be a registration in flight rather
        # than a dead one.
        return False, (
            f"resource lease {lease_id} holder is missing but the lease is "
            f"still inside the registration grace window ({detail}); retry "
            f"after the window passes if the holder never registers"
        )
    release_error = _external_resource_release(lease.get("provider") or {})

    def update(data: dict) -> None:
        for item in data.get("leases") or []:
            if isinstance(item, dict) and item.get("id") == lease_id:
                item["status"] = "release_failed" if release_error else "released"
                item["released_at"] = _now()
                item["release_reason"] = reason
                item["release_error"] = release_error
                item["release_mode"] = "force-stale"
                item["released_by"] = forced_by
                item["heartbeat_at"] = _now()
                break

    _update_runtime_leases(root, update)
    if release_error:
        return False, release_error
    return True, f"released ({detail})"


def _resource_release_foreign_lock(root: Path, resource: str, reason: str,
                                   forced_by: str) -> tuple[bool, str]:
    """Break a machine-wide lock that another checkout recorded and never freed.

    Addressed by resource name because the lease id lives in the holder's
    registry, not this one. Live holders are refused; holders the
    holder's own registry proves dead, stale sessions, vanished
    checkouts, and unverifiable legacy locks may be released with a
    recorded reason. The release is written into this checkout's
    registry as an audit row so `resource status` shows who broke what.
    """
    try:
        location = _resource_lock_location(resource)
    except ValueError as exc:
        return False, str(exc)
    if location.get("provider") != "local-mkdir":
        return False, f"--lock only addresses local machine-wide locks; release {resource} on its own host"
    for item in _load_runtime_leases(root).get("leases") or []:
        if (
            isinstance(item, dict)
            and item.get("kind") == "resource"
            and item.get("status") not in {"released", "release_failed"}
            and resource in (item.get("resources") or [])
        ):
            return False, (
                f"resource {resource} is held by lease {item.get('id')} of this checkout; "
                f"release that lease instead ('agentctl resource release {item.get('id')} "
                f"--reason <why>', with --force-stale if its holder is not live)"
            )
    lock_dir = Path(location["path"])
    owner, raw = _read_lock_owner(lock_dir)
    if not owner:
        if not lock_dir.exists():
            return False, f"no machine-wide lock exists for {resource}"
        state, detail = "unknown", "lock directory has no readable owner record"
    else:
        state, detail = _foreign_lock_liveness(owner)
    if state not in FOREIGN_LOCK_FORCE_RELEASE_STATES:
        return False, (
            f"machine-wide lock for {resource} was not released: holder is {state} "
            f"({detail}); {_foreign_lock_recovery_hint(resource, state)}"
        )
    if owner:
        error = _reclaim_lock_dir(lock_dir, raw)
    else:
        # No parsable owner record: nothing to compare against, remove the
        # damaged directory outright (this is an explicit forced action).
        try:
            shutil.rmtree(lock_dir)
            error = None
        except OSError as exc:
            error = str(exc)
    if error:
        return False, f"machine-wide lock for {resource} could not be removed: {error}"
    audit = {
        "id": "resource-" + hashlib.sha256(
            f"{resource}:foreign-release:{forced_by}:{time.time_ns()}".encode("utf-8")
        ).hexdigest()[:16],
        "kind": "resource",
        "task": str(owner.get("task") or "-"),
        "holder": {
            "type": str(owner.get("holder_type") or "unknown"),
            "id": str(owner.get("holder_id") or "unknown"),
        },
        "mode": "local-mkdir",
        "checkout": str(owner.get("checkout") or ""),
        "scope": [],
        "resources": [resource],
        "processes": [],
        "provider": location,
        "created_at": str(owner.get("created_at") or _now()),
        "heartbeat_at": _now(),
        "status": "released",
        "released_at": _now(),
        "release_reason": reason,
        "release_mode": "force-stale-foreign",
        "released_by": forced_by,
        "foreign_holder_state": state,
    }
    try:
        _update_runtime_leases(root, lambda data: data.setdefault("leases", []).append(audit))
    except TimeoutError:
        pass
    caveat = (
        "; liveness could not be verified, released on operator judgment"
        if state == "unknown" else ""
    )
    return True, f"released machine-wide lock for {resource} ({detail}){caveat}"


def _run_process_cwd(pid: int) -> Path | None:
    proc_link = Path(f"/proc/{pid}/cwd")
    try:
        if proc_link.exists():
            return proc_link.resolve()
    except OSError:
        pass
    if sys.platform == "darwin":
        try:
            proc = subprocess.run(
                ["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"],
                text=True, capture_output=True, timeout=10,
            )
            for line in proc.stdout.splitlines():
                if line.startswith("n/"):
                    return Path(line[1:]).resolve()
        except (OSError, subprocess.TimeoutExpired):
            pass
    return None


def _run_payload_path(root: Path, lease_id: str) -> Path:
    return _runtime_runs_dir(root) / f"{lease_id}.command.json"


def _run_log_paths(root: Path, lease_id: str) -> tuple[Path, Path]:
    base = _runtime_runs_dir(root)
    return base / f"{lease_id}.stdout.log", base / f"{lease_id}.stderr.log"


def _run_progress_marker(lease: dict) -> str:
    rows = []
    for raw in [lease.get("stdout"), lease.get("stderr"), *(lease.get("outputs") or [])]:
        if not raw:
            continue
        path = Path(str(raw))
        try:
            stat = path.stat()
            rows.append((str(path), stat.st_size, stat.st_mtime_ns, path.is_dir()))
        except OSError:
            rows.append((str(path), None, None, None))
    return hashlib.sha256(
        json.dumps(rows, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _sample_gpu_watchdog(watchdog: dict) -> dict:
    policy = watchdog.get("policy") or {}
    samples = []
    for target in policy.get("targets") or []:
        probe = _RESOURCE_TELEMETRY_PROBES.get(str(target.get("kind") or ""))
        if probe is None:
            samples.append({
                "ok": False,
                "target": target,
                "sampled_at_ns": time.time_ns(),
                "error": f"no telemetry probe for {target.get('kind')}",
            })
            continue
        try:
            samples.append(probe(target, float(policy["probe_timeout_seconds"])))
        except Exception as exc:
            samples.append({
                "ok": False,
                "target": target,
                "sampled_at_ns": time.time_ns(),
                "error": str(exc)[:500],
            })
    if not samples or any(not sample.get("ok") for sample in samples):
        return {
            "ok": False,
            "samples": samples,
            "sampled_at_ns": time.time_ns(),
            "error": "one or more GPU telemetry probes failed",
        }
    return {
        "ok": True,
        "samples": samples,
        "sampled_at_ns": max(int(sample["sampled_at_ns"]) for sample in samples),
        "utilization_percent": max(float(sample["utilization_percent"]) for sample in samples),
        "memory_mib": sum(float(sample["memory_mib"]) for sample in samples),
    }


def _persist_run_watchdog(root: Path, lease_id: str, watchdog: dict, *,
                          status: str | None = None,
                          stop_reason: str | None = None) -> dict | None:
    found = {"lease": None}

    def update(data: dict) -> None:
        resource_ids = []
        for lease in data.get("leases") or []:
            if not isinstance(lease, dict) or lease.get("id") != lease_id:
                continue
            lease["watchdog"] = dict(watchdog)
            lease["heartbeat_at"] = _now()
            if status:
                lease["status"] = status
            if stop_reason:
                lease["stop_reason"] = stop_reason
            resource_ids = [str(item) for item in lease.get("resource_lease_ids") or []]
            found["lease"] = dict(lease)
            break
        for lease in data.get("leases") or []:
            if not isinstance(lease, dict) or str(lease.get("id") or "") not in resource_ids:
                continue
            lease["supervision"] = {
                "run_id": lease_id,
                "state": watchdog.get("state"),
                "policy": watchdog.get("policy"),
                "last_sample": watchdog.get("last_sample"),
                "updated_at_ns": watchdog.get("updated_at_ns"),
            }
            lease["heartbeat_at"] = _now()

    _update_runtime_leases(root, update)
    return found["lease"]


def _signal_run_process(process: dict, signum: int) -> None:
    pid = int(process["pid"])
    process_group = process.get("process_group")
    if os.name == "posix" and process_group:
        os.killpg(int(process_group), signum)
        return
    if os.name == "nt" and signum in {signal.SIGTERM, PORTABLE_SIGKILL}:
        command = ["taskkill", "/PID", str(pid), "/T"]
        if signum == PORTABLE_SIGKILL:
            command.append("/F")
        try:
            completed = subprocess.run(
                command,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
                check=False,
            )
            if completed.returncode == 0:
                return
        except (OSError, subprocess.SubprocessError):
            pass
    os.kill(pid, signum)


def _settle_stopped_run_tree(root: Path, lease_id: str, process: dict) -> bool:
    current = _runtime_lease(root, lease_id) or {}
    if current.get("status") != "stopping":
        return True
    process_group = process.get("process_group")
    if os.name != "posix" or not process_group:
        return True
    watchdog = current.get("watchdog") or {}
    deadline_ns = int(
        watchdog.get("kill_deadline_ns")
        or current.get("termination_deadline_ns")
        or time.time_ns()
    )
    while _posix_process_group_exists(process_group) and time.time_ns() < deadline_ns:
        _run_update(root, lease_id, lambda lease: lease.update({
            "heartbeat_at": _now(),
        }))
        remaining = max(0.01, (deadline_ns - time.time_ns()) / 1_000_000_000)
        time.sleep(min(RUN_HEARTBEAT_SECONDS, remaining))
    if _posix_process_group_exists(process_group):
        sent_at_ns = time.time_ns()
        try:
            _signal_run_process(process, PORTABLE_SIGKILL)
        except OSError:
            pass
        _run_update(root, lease_id, lambda lease: lease.update({
            "process_group_kill_sent_at_ns": sent_at_ns,
            "heartbeat_at": _now(),
        }))
        settle_deadline = time.monotonic() + RUN_HEARTBEAT_SECONDS
        while (
            _posix_process_group_exists(process_group)
            and time.monotonic() < settle_deadline
        ):
            time.sleep(0.02)
    if _posix_process_group_exists(process_group):
        _run_update(root, lease_id, lambda lease: lease.update({
            "cleanup_error": "owned process group remains alive after SIGKILL",
            "heartbeat_at": _now(),
        }))
        return False
    return True


def _run_update(root: Path, lease_id: str, updater) -> dict | None:
    found = {"lease": None}

    def update(data: dict) -> None:
        for lease in data.get("leases") or []:
            if isinstance(lease, dict) and lease.get("id") == lease_id:
                updater(lease)
                found["lease"] = dict(lease)
                return

    _update_runtime_leases(root, update)
    return found["lease"]


def _run_release_resources(root: Path, lease: dict, reason: str) -> None:
    for resource_id in lease.get("resource_lease_ids") or []:
        _resource_release_by_id(
            root, str(resource_id), reason,
            holder_type="run", holder_id=str(lease.get("id") or ""),
        )


def _parse_workflow_timestamp(value) -> _dt.datetime | None:
    try:
        return _dt.datetime.strptime(str(value), "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return None


def _resource_holder_liveness(lease: dict, runs: dict[str, dict],
                              sessions: dict[str, str]) -> tuple[str, str]:
    """Classify how alive the holder of a resource lease still is.

    Returns (state, detail) where state is one of "live", "terminal"
    (run finished), "released" (session released), "stale" (session lost
    its heartbeat), "missing" (no holder record at all), or "unknown"
    (unrecognized holder type; always treated as live). Anything but
    hard evidence of death stays "live" so automation never touches a
    resource a working holder might still need.
    """
    holder = lease.get("holder") or {}
    holder_type = str(holder.get("type") or "")
    holder_id = str(holder.get("id") or "")
    if holder_type == "run":
        run = runs.get(holder_id)
        if run is None:
            return "missing", f"run {holder_id or '<unknown>'} is not in the lease registry"
        status = str(run.get("status") or "")
        if status in {"succeeded", "failed", "cancelled"}:
            return "terminal", f"run {holder_id} finished with status {status}"
        return "live", f"run {holder_id} status {status or '<unset>'}"
    if holder_type == "conversation":
        observed = sessions.get(holder_id)
        if observed is None:
            return "missing", f"session {holder_id or '<unknown>'} has no record"
        if observed in {"active", "review", "approved", "done"}:
            return "live", f"session {holder_id} is {observed}"
        if observed == "released":
            return "released", f"session {holder_id} was released"
        return "stale", f"session {holder_id} is {observed}"
    return "unknown", f"unrecognized holder type {holder_type or '<unset>'}"


def _missing_holder_grace_expired(lease: dict) -> bool:
    """Whether a missing holder is old enough to prove the holder is gone.

    Run leases are registered after their resources are acquired and
    session records outlive their sessions, so a freshly created
    resource lease with a missing holder may simply not be registered
    yet. Mirrors the orphan-sweep grace windows: 10 minutes for run
    holders, 1 hour for conversation holders. Missing or unparseable
    created_at stays conservative and reports the grace as not expired.
    """
    created = _parse_workflow_timestamp(lease.get("created_at"))
    if created is None:
        return False
    holder_type = str((lease.get("holder") or {}).get("type") or "")
    window = _dt.timedelta(minutes=10) if holder_type == "run" else _dt.timedelta(hours=1)
    return created < _dt.datetime.now() - window


def _session_liveness_index(root: Path) -> dict[str, str]:
    """Map each session key to its most-alive observed status.

    A session key can appear once per checkout; resource holder binding
    only records the key, so any live record keeps the holder live.
    """
    rank = {"active": 5, "review": 4, "approved": 4, "done": 4,
            "released": 2, "stale": 1, "orphaned": 1}
    index: dict[str, str] = {}
    for row in _session_rows_unlocked(root):
        key = str(row.get("workflow_session_key") or "")
        observed = str(row.get("observed_status") or "stale")
        if not key:
            continue
        if rank.get(observed, 0) > rank.get(index.get(key, ""), 0):
            index[key] = observed
    return index


def _release_orphaned_resources(root: Path) -> None:
    """Release resource leases whose holder demonstrably no longer needs them.

    Holder binding is fail-closed, so a resource stranded by a dead
    holder can never be released through the normal path and blocks
    every later acquisition of the same resource - the interlock where
    nobody uses a GPU but nobody can claim it either. Release evidence
    stays conservative:

    - run holders: the run lease is terminal, or missing after the
      registration grace window (run start acquires resources before it
      registers the run lease);
    - conversation holders: the session record says released, or the
      record is gone entirely and the resource lease is over an hour old
      (records outlive sessions, so a missing record means it was
      cleaned up long after the session ended).

    Stale sessions (lost heartbeat) are never auto-released because the
    conversation may still be working; those go through the operator
    path (`resource release --force-stale`).
    """
    leases = [
        item for item in _load_runtime_leases(root).get("leases") or []
        if isinstance(item, dict)
    ]
    runs = {
        str(item.get("id")): item
        for item in leases
        if item.get("kind") == "run" and item.get("id")
    }
    sessions: dict[str, str] | None = None
    orphaned: dict[str, str] = {}
    now = _dt.datetime.now()
    for item in leases:
        if item.get("kind") != "resource":
            continue
        if item.get("status") in {"released", "release_failed"}:
            continue
        holder_type = str((item.get("holder") or {}).get("type") or "")
        if holder_type == "conversation" and sessions is None:
            sessions = _session_liveness_index(root)
        state, _detail = _resource_holder_liveness(item, runs, sessions or {})
        created = _parse_workflow_timestamp(item.get("created_at"))
        if holder_type == "run":
            if state == "terminal":
                reason = "holding run finished without releasing"
            elif state == "missing":
                # A missing holder only proves a pruned (terminal) run once
                # the resource lease has aged past the grace window;
                # unparseable timestamps stay conservative and keep the lease.
                if created is None or created >= now - _dt.timedelta(minutes=10):
                    continue
                reason = "holding run finished without releasing"
            else:
                continue
        elif holder_type == "conversation":
            if state == "released":
                reason = "holding session released without freeing the resource"
            elif state == "missing":
                if created is None or created >= now - _dt.timedelta(hours=1):
                    continue
                reason = "holding session record no longer exists"
            else:
                continue
        else:
            continue
        orphaned[str(item.get("id") or "")] = reason
    if not orphaned:
        return
    release_errors = {
        str(item.get("id") or ""): _external_resource_release(item.get("provider") or {})
        for item in leases
        if str(item.get("id") or "") in orphaned
    }

    def update(data: dict) -> None:
        for item in data.get("leases") or []:
            if not isinstance(item, dict) or item.get("kind") != "resource":
                continue
            lease_id = str(item.get("id") or "")
            if lease_id not in release_errors:
                continue
            if item.get("status") in {"released", "release_failed"}:
                continue
            error = release_errors[lease_id]
            item["status"] = "release_failed" if error else "released"
            item["released_at"] = _now()
            item["release_reason"] = orphaned[lease_id]
            item["release_error"] = error
            item["heartbeat_at"] = _now()

    try:
        _update_runtime_leases(root, update)
    except TimeoutError:
        return


def _prune_terminal_run_state(root: Path) -> None:
    """Age out terminal run/resource leases and their run artifacts.

    Terminal leases and per-run logs otherwise accumulate forever: every
    locked registry operation re-reads the growing file, and the runs
    directory keeps stdout/stderr/supervisor logs for runs that finished
    long ago. Live, non-terminal, and recent state is never touched, and
    release_failed resource leases are kept because they flag manual
    attention. Retention is `run_artifact_retention_days` in the runtime
    policy (default 14; 0 disables pruning).     Releasing resources orphaned
    by finished runs is correctness rather than hygiene, so it always runs
    regardless of the retention setting.
    """
    _release_orphaned_resources(root)
    configured = _runtime_policy(root).get("run_artifact_retention_days", 14)
    try:
        retention_days = float(configured)
    except (TypeError, ValueError):
        retention_days = 14.0
    if not math.isfinite(retention_days) or retention_days <= 0:
        return
    cutoff = _dt.datetime.now() - _dt.timedelta(days=retention_days)
    pruned_runs: list[str] = []

    def prune(data: dict) -> None:
        kept = []
        for lease in data.get("leases") or []:
            if not isinstance(lease, dict):
                kept.append(lease)
                continue
            kind = lease.get("kind")
            status = str(lease.get("status") or "")
            terminal = (
                (kind == "run" and status in {"succeeded", "failed", "cancelled"})
                or (kind == "resource" and status == "released")
            )
            if not terminal:
                kept.append(lease)
                continue
            stamp = (
                _parse_workflow_timestamp(lease.get("finished_at"))
                or _parse_workflow_timestamp(lease.get("released_at"))
                or _parse_workflow_timestamp(lease.get("heartbeat_at"))
            )
            if stamp is None or stamp >= cutoff:
                kept.append(lease)
                continue
            if kind == "run" and lease.get("id"):
                pruned_runs.append(str(lease["id"]))
        data["leases"] = kept

    try:
        _update_runtime_leases(root, prune)
    except TimeoutError:
        return
    runs_dir = _runtime_runs_dir(root)
    artifact_suffixes = (
        ".stdout.log", ".stderr.log", ".supervisor.log", ".command.json",
    )
    for lease_id in pruned_runs:
        for suffix in artifact_suffixes:
            try:
                (runs_dir / f"{lease_id}{suffix}").unlink()
            except OSError:
                pass
    live_ids = {
        str(lease.get("id"))
        for lease in _load_runtime_leases(root).get("leases") or []
        if isinstance(lease, dict) and lease.get("id")
    }
    cutoff_ts = cutoff.timestamp()
    try:
        candidates = list(runs_dir.iterdir())
    except OSError:
        return
    for path in candidates:
        match = re.match(
            r"^(run-[0-9a-f]{16})\."
            r"(?:stdout\.log|stderr\.log|supervisor\.log|command\.json)$",
            path.name,
        )
        if not match or match.group(1) in live_ids:
            continue
        try:
            if path.stat().st_mtime < cutoff_ts:
                path.unlink()
        except OSError:
            pass


def _supervisor_argv(root: Path, lease_id: str, token: str) -> list[str]:
    """Command line for the detached run supervisor.

    Values are attached with `=` so argparse never mistakes one that starts
    with "-" for an option name; a bare `--token -abc` was parsed as a
    missing value and the supervisor exited before claiming its lease.
    """
    return [
        sys.executable, str(Path(__file__).resolve()), "_run-supervise",
        f"--root={root}", f"--lease={lease_id}", f"--token={token}",
    ]


def _await_supervisor_claim(root: Path, lease_id: str, *,
                            process: "subprocess.Popen[bytes] | None" = None,
                            timeout_seconds: float = 30.0,
                            poll_seconds: float = 0.05) -> str:
    """Watch a freshly spawned supervisor until it claims its lease.

    Returns "claimed" once the claim landed (or the lease already moved
    past starting), "pending" when the supervisor is still alive but has
    not claimed within the timeout, and "died" when the recorded
    supervisor process exited before claiming. A pre-claim death would
    otherwise leave the lease unresolved forever, observed only as
    exited_unknown.

    `process` is the Popen handle when the caller is the supervisor's
    parent. An exited child stays a zombie until its parent reaps it, and
    a zombie still answers kill(pid, 0), so pid-based liveness would keep
    reporting a crashed supervisor as alive for the whole timeout.
    """
    deadline = time.monotonic() + max(0.0, timeout_seconds)
    while True:
        lease = _runtime_lease(root, lease_id) or {}
        if lease.get("supervisor_claimed_at") or (
            str(lease.get("status") or "starting") != "starting"
        ):
            return "claimed"
        if process is not None and process.poll() is not None:
            return "died"
        supervisor = lease.get("supervisor_process")
        if supervisor and not _runtime_process_alive(supervisor):
            return "died"
        if time.monotonic() >= deadline:
            return "pending"
        time.sleep(poll_seconds)


def _run_update_with_retry(root: Path, lease_id: str, updater, *,
                           attempts: int = 6,
                           first_delay_seconds: float = 1.0) -> dict | None:
    """Persist a run lease update through transient registry lock stalls.

    A supervisor that crashes on a single lock timeout leaves its lease
    permanently unresolved (observed as exited_unknown) even though the
    command finished, so state that decides the run outcome must outlast
    short contention windows.
    """
    delay = first_delay_seconds
    for attempt in range(attempts):
        try:
            return _run_update(root, lease_id, updater)
        except TimeoutError:
            if attempt == attempts - 1:
                raise
            time.sleep(delay)
            delay = min(delay * 2.0, 16.0)
    return None


def _run_owner_error(root: Path, lease: dict) -> str | None:
    session = _load_session(root)
    current = _workflow_session_key()
    holder = str((lease.get("holder") or {}).get("id") or "")
    if holder != current:
        return f"run {lease.get('id')} belongs to conversation {holder or 'unknown'}"
    if str(session.get("task") or "") != str(lease.get("task") or ""):
        return (
            f"run {lease.get('id')} belongs to task {lease.get('task')}; "
            f"this conversation owns {session.get('task') or 'no active task'}"
        )
    return None


def _run_supervisor_claim(root: Path, lease_id: str, token: str) -> tuple[dict | None, str | None]:
    found = {"lease": None, "error": None}
    supplied_hash = hashlib.sha256(str(token or "").encode("utf-8")).hexdigest()

    def claim(data: dict) -> None:
        for lease in data.get("leases") or []:
            if not isinstance(lease, dict) or lease.get("id") != lease_id:
                continue
            expected_hash = str(lease.get("supervisor_token_sha256") or "")
            if lease.get("kind") != "run" or lease.get("mode") != "supervised":
                found["error"] = "lease is not a supervised run"
            elif lease.get("status") != "starting" or lease.get("supervisor_claimed_at"):
                found["error"] = "supervisor was already claimed"
            elif not expected_hash or not hmac.compare_digest(expected_hash, supplied_hash):
                found["error"] = "supervisor token does not match"
            else:
                lease["supervisor_claimed_at"] = _now()
                lease["supervisor_process"] = {
                    "role": "supervisor",
                    "pid": os.getpid(),
                    "birth_marker": _process_birth_marker(os.getpid()),
                }
                lease.pop("supervisor_token_sha256", None)
                lease["heartbeat_at"] = _now()
                found["lease"] = dict(lease)
            return
        found["error"] = found["error"] or "run lease was not found"

    _update_runtime_leases(root, claim)
    return found["lease"], found["error"]


def _run_supervise(root: Path, lease_id: str, token: str) -> int:
    claimed = None
    claim_error: str | None = None
    claim_delay = 1.0
    for claim_attempt in range(4):
        try:
            claimed, claim_error = _run_supervisor_claim(root, lease_id, token)
            break
        except TimeoutError as exc:
            claim_error = str(exc)
            if claim_attempt < 3:
                time.sleep(claim_delay)
                claim_delay = min(claim_delay * 2.0, 8.0)
    if not claimed:
        print(
            f"agentctl: supervisor claim rejected for {lease_id}: {claim_error}",
            file=sys.stderr,
        )
        return 1
    payload_path = _run_payload_path(root, lease_id)
    payload = _load_json(payload_path, {})
    command = payload.get("command") if isinstance(payload, dict) else None
    cwd = Path(str(payload.get("cwd") or root)) if isinstance(payload, dict) else root
    try:
        payload_path.unlink()
    except FileNotFoundError:
        pass
    if not isinstance(command, list) or not command:
        try:
            failed = _run_update_with_retry(root, lease_id, lambda lease: lease.update({
                "status": "failed",
                "finished_at": _now(),
                "error": "private command payload is missing or invalid",
            }))
        except TimeoutError:
            print(
                f"agentctl: unable to persist failure for {lease_id}: "
                "lease registry lock timed out",
                file=sys.stderr,
            )
            return 2
        if failed:
            _run_release_resources(root, failed, "run payload invalid")
        return 2
    stdout_path, stderr_path = _run_log_paths(root, lease_id)
    try:
        with stdout_path.open("ab", buffering=0) as stdout, stderr_path.open("ab", buffering=0) as stderr:
            child_env = os.environ.copy()
            child_env[RUN_ID_ENV] = lease_id
            popen_options = {
                "cwd": str(cwd),
                "stdout": stdout,
                "stderr": stderr,
                "env": child_env,
            }
            if os.name == "posix":
                popen_options["start_new_session"] = True
            elif os.name == "nt":
                popen_options["creationflags"] = getattr(
                    subprocess, "CREATE_NEW_PROCESS_GROUP", 0
                )
            child = subprocess.Popen([str(item) for item in command], **popen_options)
            child_record = {
                "role": "command",
                "pid": child.pid,
                "birth_marker": _process_birth_marker(child.pid),
                "process_group": child.pid if os.name == "posix" else None,
            }
            _run_update_with_retry(root, lease_id, lambda lease: lease.update({
                "status": "running",
                "processes": [child_record],
                "heartbeat_at": _now(),
            }))

            def tolerate_lock_timeout(operation) -> bool:
                # Heartbeats and telemetry snapshots are periodic; skipping
                # one beat under registry contention beats marking a healthy
                # run failed or crashing the supervisor.
                try:
                    operation()
                    return True
                except TimeoutError:
                    return False

            while child.poll() is None:
                current = _runtime_lease(root, lease_id) or {}
                watchdog = dict(current.get("watchdog") or {})
                policy = watchdog.get("policy") or {}
                if watchdog.get("enabled"):
                    now_ns = time.time_ns()
                    sample_interval_ns = int(
                        float(policy["sample_seconds"]) * 1_000_000_000
                    )
                    kill_deadline_ns = int(watchdog.get("kill_deadline_ns") or 0)
                    if kill_deadline_ns and now_ns >= kill_deadline_ns:
                        try:
                            _signal_run_process(child_record, PORTABLE_SIGKILL)
                            watchdog["kill_sent_at_ns"] = now_ns
                        except OSError:
                            pass
                        watchdog["kill_deadline_ns"] = now_ns + int(
                            RUN_HEARTBEAT_SECONDS * 1_000_000_000
                        )
                        watchdog["updated_at_ns"] = now_ns
                        tolerate_lock_timeout(
                            lambda: _persist_run_watchdog(root, lease_id, watchdog)
                        )
                    elif current.get("status") == "stopping" and watchdog.get(
                        "auto_termination_at_ns"
                    ):
                        watchdog["state"] = "reclaiming"
                        watchdog["updated_at_ns"] = now_ns
                        tolerate_lock_timeout(
                            lambda: _persist_run_watchdog(root, lease_id, watchdog)
                        )
                    else:
                        next_sample_at_ns = int(watchdog.get("next_sample_at_ns") or 0)
                        if now_ns >= next_sample_at_ns:
                            sample = _sample_gpu_watchdog(watchdog)
                            progress = current.get("progress") or {}
                            watchdog, action = _gpu_watchdog_transition(
                                watchdog,
                                policy,
                                sample,
                                now_ns=now_ns,
                                progress_marker=_run_progress_marker(current),
                                progress_updated_ns=int(progress.get("updated_at_ns") or 0),
                                exempt_until_ns=int(progress.get("idle_exempt_until_ns") or 0),
                            )
                            watchdog["next_sample_at_ns"] = now_ns + sample_interval_ns
                            if action == "terminate":
                                reason = (
                                    "GPU watchdog confirmed consecutive low utilization, "
                                    "allocated memory, absent progress, and expired grace"
                                )
                                try:
                                    _signal_run_process(child_record, signal.SIGTERM)
                                except OSError:
                                    pass
                                watchdog.update({
                                    "state": "reclaiming",
                                    "auto_termination_at_ns": now_ns,
                                    "kill_deadline_ns": now_ns + int(
                                        float(policy["kill_seconds"]) * 1_000_000_000
                                    ),
                                    "updated_at_ns": now_ns,
                                })
                                tolerate_lock_timeout(
                                    lambda: _persist_run_watchdog(
                                        root, lease_id, watchdog,
                                        status="stopping", stop_reason=reason,
                                    )
                                )
                            else:
                                tolerate_lock_timeout(
                                    lambda: _persist_run_watchdog(
                                        root, lease_id, watchdog,
                                    )
                                )
                        else:
                            tolerate_lock_timeout(
                                lambda: _run_update(
                                    root, lease_id,
                                    lambda lease: lease.update({
                                        "heartbeat_at": _now(),
                                    }),
                                )
                            )
                    kill_deadline_ns = int(watchdog.get("kill_deadline_ns") or 0)
                    next_event_ns = int(watchdog.get("next_sample_at_ns") or 0)
                    if kill_deadline_ns:
                        next_event_ns = min(next_event_ns or kill_deadline_ns, kill_deadline_ns)
                    until_event = max(0.01, (next_event_ns - now_ns) / 1_000_000_000)
                    sleep_seconds = min(RUN_HEARTBEAT_SECONDS, until_event)
                else:
                    tolerate_lock_timeout(
                        lambda: _run_update(
                            root, lease_id,
                            lambda lease: lease.update({
                                "heartbeat_at": _now(),
                            }),
                        )
                    )
                    sleep_seconds = RUN_HEARTBEAT_SECONDS
                time.sleep(sleep_seconds)
            returncode = int(child.returncode or 0)
            if not _settle_stopped_run_tree(root, lease_id, child_record):
                return 2
    except Exception as exc:
        try:
            current = _run_update_with_retry(root, lease_id, lambda lease: lease.update({
                "status": "failed",
                "finished_at": _now(),
                "error": str(exc),
                "heartbeat_at": _now(),
            }))
        except TimeoutError:
            print(
                f"agentctl: unable to persist failure for {lease_id}: "
                "lease registry lock timed out",
                file=sys.stderr,
            )
            return 2
        if current:
            try:
                _run_release_resources(root, current, "run launch failed")
            except TimeoutError:
                pass
        return 2
    current = _runtime_lease(root, lease_id) or {}
    requested_stop = current.get("status") == "stopping"
    final_status = "cancelled" if requested_stop else ("succeeded" if returncode == 0 else "failed")
    try:
        completed = _run_update_with_retry(root, lease_id, lambda lease: lease.update({
            "status": final_status,
            "returncode": returncode,
            "finished_at": _now(),
            "heartbeat_at": _now(),
        }))
    except TimeoutError:
        print(
            f"agentctl: unable to persist terminal state for {lease_id}: "
            "lease registry lock timed out",
            file=sys.stderr,
        )
        return 2
    if completed:
        release_delay = 1.0
        for release_attempt in range(3):
            try:
                _run_release_resources(root, completed, f"run {final_status}")
                break
            except TimeoutError:
                if release_attempt == 2:
                    print(
                        f"agentctl: run {lease_id} recorded {final_status} but "
                        "resource release hit lease registry lock contention; "
                        "the orphaned leases release automatically on the next "
                        "run start",
                        file=sys.stderr,
                    )
                else:
                    time.sleep(release_delay)
                    release_delay = min(release_delay * 2.0, 4.0)
    return 0 if final_status == "succeeded" else 1


def _run_start(root: Path, args: argparse.Namespace) -> int:
    session = _require_session(root)
    try:
        _prune_terminal_run_state(root)
    except Exception:
        # Opportunistic hygiene must never block starting new work.
        pass
    task = args.task or session.get("task")
    if task != session.get("task"):
        print("agentctl: a background run must belong to this conversation's active task", file=sys.stderr)
        return 1
    command = list(args.command or [])
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        print("agentctl: run start requires a command after '--'", file=sys.stderr)
        return 2
    cwd = Path(args.cwd).expanduser() if args.cwd else root
    cwd = cwd.resolve() if cwd.is_absolute() else (root / cwd).resolve()
    if not _path_within(root, cwd):
        print("agentctl: run cwd must be inside the task checkout", file=sys.stderr)
        return 1
    outputs, problems = _validate_run_outputs(
        root, str(task), session.get("scope") or [], list(args.output or []),
    )
    if problems:
        for problem in problems:
            print(f"agentctl: {problem}", file=sys.stderr)
        return 1
    watchdog_policy, watchdog_error = _gpu_watchdog_policy(root, args)
    if watchdog_error:
        print(f"agentctl: {watchdog_error}", file=sys.stderr)
        return 1
    request_token = (getattr(args, "request_id", "") or "").strip()
    request_fd = None
    if request_token:
        token_error = _submission_request_error(request_token)
        if token_error:
            print(f"agentctl: {token_error}", file=sys.stderr)
            return 2
        intent_digest = _submission_intent_digest({
            "kind": "run",
            "task": str(task),
            "command": [str(item) for item in command],
            "cwd": str(cwd),
            "outputs": sorted(outputs),
            "resources": sorted(str(item) for item in args.resource or []),
        })

        def _resolve_started_run(record: dict):
            allocated = str((record.get("result") or {}).get("run") or "")
            if allocated and _runtime_lease(root, allocated):
                return {"run": allocated}
            return None

        action, record, request_fd = _submission_request_begin(
            root, request_token, intent_digest, "run", _resolve_started_run,
        )
        if action == "replay":
            confirmed = str((record.get("result") or {}).get("run") or "")
            print(
                f"agentctl: request {request_token} already started run "
                f"{confirmed}; not launching it again"
            )
            print(f"  inspect: python3 tools/agentctl.py run show {confirmed}")
            return 0
        if action == "replay-reject":
            print(
                f"agentctl: request {request_token} was already rejected: "
                f"{record.get('error') or 'no recorded reason'}",
                file=sys.stderr,
            )
            return 2
        if action == "conflict":
            print(
                f"agentctl: request {request_token} was already used with a "
                "different run intent; pick a new request id",
                file=sys.stderr,
            )
            return 2
        if action == "blocked":
            print(
                f"agentctl: request {request_token} has an interrupted attempt "
                "with no registered run; inspect 'agentctl run list' for "
                "leftovers, then retry with a new request id",
                file=sys.stderr,
            )
            return 1
    lease_id = "run-" + hashlib.sha256(
        f"{task}:{_workflow_session_key()}:{time.time_ns()}".encode("utf-8")
    ).hexdigest()[:16]
    if request_fd is not None:
        _submission_request_note(root, request_token, {"run": lease_id})
    resource_leases = []
    for resource in args.resource or []:
        lease, error = _resource_acquire_one(
            root, str(task), resource, "run", lease_id,
        )
        if error:
            for acquired in resource_leases:
                _resource_release_by_id(
                    root, acquired["id"], "run start rolled back",
                    holder_type="run", holder_id=lease_id,
                )
            print(f"agentctl: {error}", file=sys.stderr)
            if request_fd is not None:
                _submission_request_abandon(root, request_token, request_fd)
            return 1
        resource_leases.append(lease)
    stdout_path, stderr_path = _run_log_paths(root, lease_id)
    payload_path = _run_payload_path(root, lease_id)
    _save_json(payload_path, {"command": command, "cwd": str(cwd)})
    try:
        os.chmod(payload_path, 0o600)
    except OSError:
        pass
    # hex, not urlsafe: a urlsafe token starts with "-" one time in 64 and
    # argparse then rejected it as a missing option value, so the
    # supervisor died at start-up and the run never launched.
    supervisor_token = secrets.token_hex(32)
    lease = {
        "id": lease_id,
        "kind": "run",
        "task": task,
        "holder": {"type": "conversation", "id": _workflow_session_key()},
        "mode": "supervised",
        "checkout": str(root.resolve()),
        "scope": session.get("scope") or [],
        "outputs": outputs,
        "resources": [item for item in args.resource or []],
        "resource_lease_ids": [item["id"] for item in resource_leases],
        "processes": [],
        "command_sha256": hashlib.sha256(
            json.dumps(command, ensure_ascii=False).encode("utf-8")
        ).hexdigest(),
        "executable": Path(str(command[0])).name,
        "cwd": str(cwd),
        "stdout": str(stdout_path),
        "stderr": str(stderr_path),
        "progress": {
            "phase": "starting",
            "token": "",
            "updated_at": _now(),
            "updated_at_ns": time.time_ns(),
            "idle_exempt_until_ns": 0,
        },
        "supervisor_token_sha256": hashlib.sha256(
            supervisor_token.encode("utf-8")
        ).hexdigest(),
        "created_at": _now(),
        "heartbeat_at": _now(),
        "status": "starting",
    }
    if watchdog_policy:
        lease["watchdog"] = {
            "enabled": True,
            "policy": watchdog_policy,
            "state": "active",
            "low_samples": 0,
            "created_at_ns": time.time_ns(),
        }
    _update_runtime_leases(root, lambda data: data.setdefault("leases", []).append(lease))
    if request_fd is not None:
        # The run lease is durable from here on; even if the supervisor fails
        # to launch, this request maps to exactly this run and must never
        # start a second one.
        _submission_request_settle(
            root, request_token, request_fd, "confirmed", {"run": lease_id},
        )
        request_fd = None
    supervisor_log_path = _runtime_runs_dir(root) / f"{lease_id}.supervisor.log"
    outcome = "pending"
    for spawn_attempt in (1, 2):
        try:
            # Keep the supervisor's own stderr on disk: a supervisor that
            # dies before persisting state is otherwise undiagnosable,
            # because the lease just reports exited_unknown.
            with supervisor_log_path.open("ab") as supervisor_log:
                supervisor = subprocess.Popen(
                    _supervisor_argv(root, lease_id, supervisor_token),
                    cwd=str(root),
                    stdout=subprocess.DEVNULL,
                    stderr=supervisor_log,
                    start_new_session=True,
                )
        except Exception as exc:
            failed = _run_update(root, lease_id, lambda item: item.update({
                "status": "failed",
                "finished_at": _now(),
                "error": f"unable to launch supervisor: {exc}",
                "heartbeat_at": _now(),
            }))
            if failed:
                _run_release_resources(root, failed, "supervisor launch failed")
            try:
                payload_path.unlink()
            except FileNotFoundError:
                pass
            print(f"agentctl: unable to launch run supervisor: {exc}", file=sys.stderr)
            return 1
        supervisor_record = {
            "role": "supervisor",
            "pid": supervisor.pid,
            "birth_marker": _process_birth_marker(supervisor.pid),
        }
        _run_update(root, lease_id, lambda item: item.update({
            "supervisor_process": supervisor_record,
            "heartbeat_at": _now(),
        }))
        outcome = _await_supervisor_claim(root, lease_id, process=supervisor)
        if outcome != "died":
            break
        if spawn_attempt == 1:
            print(
                f"agentctl: run supervisor pid={supervisor.pid} exited before "
                f"claiming {lease_id}; spawning a replacement",
                file=sys.stderr,
            )
            continue
        detail = ""
        try:
            tail = supervisor_log_path.read_text(
                encoding="utf-8", errors="replace",
            ).strip()
            if tail:
                detail = " Last supervisor output: " + tail[-400:]
        except OSError:
            pass
        try:
            failed = _run_update_with_retry(root, lease_id, lambda item: item.update({
                "status": "failed",
                "finished_at": _now(),
                "error": (
                    "supervisor exited before claiming the run twice; "
                    f"see {supervisor_log_path}.{detail}"
                ),
                "heartbeat_at": _now(),
            }))
        except TimeoutError:
            failed = None
        if failed:
            try:
                _run_release_resources(root, failed, "supervisor died before claim")
            except TimeoutError:
                pass
        try:
            payload_path.unlink()
        except FileNotFoundError:
            pass
        print(
            f"agentctl: run supervisor for {lease_id} exited before claiming "
            f"twice; the run is failed. Inspect {supervisor_log_path}",
            file=sys.stderr,
        )
        return 1
    if outcome == "pending":
        print(
            f"agentctl: run supervisor for {lease_id} is alive but has not "
            "claimed within its startup window; track it with 'agentctl run show'",
            file=sys.stderr,
        )
    print(f"agentctl: run lease {lease_id} started")
    print(f"  task={task} pid={supervisor.pid}")
    print(f"  outputs={', '.join(outputs)}")
    print(f"  stdout={stdout_path}")
    print(f"  stderr={stderr_path}")
    return 0


def _run_adopt(root: Path, args: argparse.Namespace) -> int:
    session = _require_session(root)
    try:
        pid = int(args.pid)
    except (TypeError, ValueError):
        print("agentctl: run adopt requires a numeric --pid", file=sys.stderr)
        return 2
    birth = _process_birth_marker(pid)
    if not _same_process(pid, birth) or not birth:
        print("agentctl: adopted PID is not alive or has no verifiable birth marker", file=sys.stderr)
        return 1
    declared_cwd = Path(args.cwd).expanduser().resolve()
    observed_cwd = _run_process_cwd(pid)
    if observed_cwd is None or observed_cwd != declared_cwd:
        print(
            f"agentctl: adopted PID cwd cannot be verified as {declared_cwd} "
            f"(observed {observed_cwd or 'unknown'})",
            file=sys.stderr,
        )
        return 1
    if not _path_within(root, declared_cwd):
        print("agentctl: adopted process cwd must be inside the task checkout", file=sys.stderr)
        return 1
    task = str(session.get("task"))
    outputs, problems = _validate_run_outputs(
        root, task, session.get("scope") or [], list(args.output or []),
    )
    if problems:
        for problem in problems:
            print(f"agentctl: {problem}", file=sys.stderr)
        return 1
    lease_id = "run-" + hashlib.sha256(
        f"adopt:{task}:{pid}:{birth}:{time.time_ns()}".encode("utf-8")
    ).hexdigest()[:16]
    resource_leases = []
    for resource in args.resource or []:
        lease, error = _resource_acquire_one(root, task, resource, "run", lease_id)
        if error:
            for acquired in resource_leases:
                _resource_release_by_id(
                    root, acquired["id"], "adopt rolled back",
                    holder_type="run", holder_id=lease_id,
                )
            print(f"agentctl: {error}", file=sys.stderr)
            return 1
        resource_leases.append(lease)
    lease = {
        "id": lease_id,
        "kind": "run",
        "task": task,
        "holder": {"type": "conversation", "id": _workflow_session_key()},
        "mode": "adopted",
        "checkout": str(root.resolve()),
        "scope": session.get("scope") or [],
        "outputs": outputs,
        "resources": list(args.resource or []),
        "resource_lease_ids": [item["id"] for item in resource_leases],
        "processes": [{"role": "adopted", "pid": pid, "birth_marker": birth}],
        "cwd": str(declared_cwd),
        "command_sha256": args.command_sha256 or "",
        "created_at": _now(),
        "heartbeat_at": _now(),
        "status": "running",
    }
    _update_runtime_leases(root, lambda data: data.setdefault("leases", []).append(lease))
    print(f"agentctl: adopted process {pid} as run lease {lease_id}")
    return 0


def _run_list(root: Path, args: argparse.Namespace) -> int:
    rows = [
        dict(lease, status=_runtime_observed_status(lease))
        for lease in _load_runtime_leases(root).get("leases") or []
        if isinstance(lease, dict) and lease.get("kind") == "run"
    ]
    if args.run_action == "show":
        rows = [row for row in rows if row.get("id") == args.lease]
        if not rows:
            print(f"agentctl: run lease not found: {args.lease}", file=sys.stderr)
            return 2
    if args.json:
        print(json.dumps({"runs": rows}, indent=2, ensure_ascii=False))
    else:
        for row in rows:
            watchdog_state = str((row.get("watchdog") or {}).get("state") or "-")
            print(
                f"{row.get('id')} status={row.get('status')} task={row.get('task')} "
                f"mode={row.get('mode')} resources={','.join(row.get('resources') or []) or '-'} "
                f"watchdog={watchdog_state}"
            )
        if not rows:
            print("agentctl: no run leases")
    return 0


def _run_wait(root: Path, args: argparse.Namespace) -> int:
    deadline = time.monotonic() + max(0.0, float(args.timeout))
    while True:
        lease = _runtime_lease(root, args.lease)
        if not lease or lease.get("kind") != "run":
            print(f"agentctl: run lease not found: {args.lease}", file=sys.stderr)
            return 2
        status = _runtime_observed_status(lease)
        settling_supervisor = (
            status == "exited_unknown"
            and lease.get("mode") == "supervised"
            and time.monotonic() < deadline
        )
        if status not in {"starting", "running", "stopping"} and not settling_supervisor:
            print(f"agentctl: run {args.lease} -> {status}")
            if status == "exited_unknown" and lease.get("mode") == "supervised":
                print(
                    "agentctl: the supervisor exited without recording a result; "
                    f"inspect {_runtime_runs_dir(root) / (str(args.lease) + '.supervisor.log')} "
                    "and reconcile with 'agentctl run finish'",
                    file=sys.stderr,
                )
            return 0 if status == "succeeded" else 1
        if time.monotonic() >= deadline:
            print(f"agentctl: run {args.lease} still {status} after timeout", file=sys.stderr)
            return 1
        time.sleep(min(0.2, max(0.0, deadline - time.monotonic())))


def _run_progress(root: Path, args: argparse.Namespace) -> int:
    lease_id = str(args.lease or os.environ.get(RUN_ID_ENV) or "")
    if not lease_id:
        print(
            f"agentctl: run progress requires a lease or inherited {RUN_ID_ENV}",
            file=sys.stderr,
        )
        return 2
    lease = _runtime_lease(root, lease_id)
    if not lease or lease.get("kind") != "run":
        print(f"agentctl: run lease not found: {lease_id}", file=sys.stderr)
        return 2
    owner_error = _run_owner_error(root, lease)
    if owner_error:
        print(f"agentctl: {owner_error}", file=sys.stderr)
        return 1
    if lease.get("status") not in {"starting", "running"}:
        print(
            f"agentctl: run {lease_id} cannot accept progress while {lease.get('status')}",
            file=sys.stderr,
        )
        return 1
    phase = str(args.phase or "").strip()
    token = str(args.token or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_.:/-]{1,64}", phase):
        print("agentctl: progress phase must be a 1..64 character stable identifier", file=sys.stderr)
        return 2
    if len(token) > 256:
        print("agentctl: progress token must be at most 256 characters", file=sys.stderr)
        return 2
    exempt_seconds = float(args.idle_exempt_seconds or 0.0)
    if not math.isfinite(exempt_seconds) or not 0.0 <= exempt_seconds <= 24 * 3600.0:
        print("agentctl: idle exemption must be between 0 and 86400 seconds", file=sys.stderr)
        return 2
    now_ns = time.time_ns()
    progress = {
        "phase": phase,
        "token": token,
        "updated_at": _now(),
        "updated_at_ns": now_ns,
        "idle_exempt_until_ns": now_ns + int(exempt_seconds * 1_000_000_000),
    }
    _run_update(root, lease_id, lambda item: item.update({
        "progress": progress,
        "heartbeat_at": _now(),
    }))
    print(f"agentctl: run {lease_id} progress phase={phase}")
    return 0


def _run_finish(root: Path, args: argparse.Namespace) -> int:
    lease = _runtime_lease(root, args.lease)
    if not lease or lease.get("kind") != "run":
        print(f"agentctl: run lease not found: {args.lease}", file=sys.stderr)
        return 2
    owner_error = _run_owner_error(root, lease)
    if owner_error:
        print(f"agentctl: {owner_error}", file=sys.stderr)
        return 1
    if any(_runtime_process_alive(item) for item in lease.get("processes") or []):
        print("agentctl: cannot finish a run while its process is still alive", file=sys.stderr)
        return 1
    if lease.get("mode") != "adopted" and _runtime_observed_status(lease) not in {
        "interrupted", "exited_unknown",
    }:
        print("agentctl: only adopted or interrupted runs require explicit finish", file=sys.stderr)
        return 1
    updated = _run_update(root, args.lease, lambda item: item.update({
        "status": args.status,
        "finished_at": _now(),
        "finish_reason": args.reason,
        "heartbeat_at": _now(),
    }))
    if updated:
        _run_release_resources(root, updated, f"run explicitly finished: {args.reason}")
    print(f"agentctl: run {args.lease} reconciled -> {args.status}")
    return 0


def _run_stop(root: Path, args: argparse.Namespace) -> int:
    lease = _runtime_lease(root, args.lease)
    if not lease or lease.get("kind") != "run":
        print(f"agentctl: run lease not found: {args.lease}", file=sys.stderr)
        return 2
    owner_error = _run_owner_error(root, lease)
    if owner_error:
        print(f"agentctl: {owner_error}", file=sys.stderr)
        return 1
    processes = lease.get("processes") or []
    child = processes[-1] if processes else None
    if not _runtime_process_alive(child):
        print("agentctl: run process is not alive; inspect and finish/reconcile it", file=sys.stderr)
        return 1
    watchdog_policy = (lease.get("watchdog") or {}).get("policy") or {}
    kill_seconds = float(watchdog_policy.get("kill_seconds") or 30.0)
    _run_update(root, args.lease, lambda item: item.update({
        "status": "stopping",
        "stop_reason": args.reason,
        "termination_deadline_ns": time.time_ns() + int(kill_seconds * 1_000_000_000),
        "heartbeat_at": _now(),
    }))
    try:
        _signal_run_process(child, signal.SIGTERM)
    except OSError as exc:
        def rollback(item: dict) -> None:
            if item.get("status") == "stopping" and item.get("stop_reason") == args.reason:
                item["status"] = "running"
                item.pop("stop_reason", None)
                item.pop("termination_deadline_ns", None)
                item["heartbeat_at"] = _now()

        _run_update(root, args.lease, rollback)
        print(f"agentctl: unable to stop run process: {exc}", file=sys.stderr)
        return 1
    print(f"agentctl: stop requested for run {args.lease}")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve() if args.run_action == "_supervise" else _repo_root()
    if args.run_action == "_supervise":
        return _run_supervise(root, args.lease, args.token)
    if args.run_action == "start":
        return _run_start(root, args)
    if args.run_action == "adopt":
        return _run_adopt(root, args)
    if args.run_action in {"list", "show"}:
        return _run_list(root, args)
    if args.run_action == "wait":
        return _run_wait(root, args)
    if args.run_action == "progress":
        return _run_progress(root, args)
    if args.run_action == "finish":
        return _run_finish(root, args)
    if args.run_action == "stop":
        return _run_stop(root, args)
    print("agentctl: unknown run action", file=sys.stderr)
    return 2


def cmd_resource(args: argparse.Namespace) -> int:
    root = _repo_root()
    if args.resource_action == "status":
        rows = [
            lease for lease in _load_runtime_leases(root).get("leases") or []
            if isinstance(lease, dict) and lease.get("kind") == "resource"
        ]
        if args.json:
            print(json.dumps({"resources": rows}, indent=2, ensure_ascii=False))
        else:
            for row in rows:
                supervision_state = str(
                    (row.get("supervision") or {}).get("state") or "-"
                )
                print(
                    f"{row.get('id')} status={row.get('status')} "
                    f"resource={','.join(row.get('resources') or [])} "
                    f"task={row.get('task')} supervision={supervision_state}"
                )
            if not rows:
                print("agentctl: no resource leases")
        return 0
    session = _require_session(root)
    if args.resource_action == "acquire":
        lease, error = _resource_acquire_one(
            root, str(session.get("task")), args.resource,
            "conversation", _workflow_session_key(),
        )
        if error:
            print(f"agentctl: {error}", file=sys.stderr)
            return 1
        print(f"agentctl: resource lease {lease['id']} acquired for {args.resource}")
        return 0
    if args.resource_action == "release":
        lock_resource = getattr(args, "lock_resource", None)
        if lock_resource and args.lease:
            print("agentctl: pass either a lease id or --lock <resource>, not both", file=sys.stderr)
            return 2
        if not lock_resource and not args.lease:
            print("agentctl: resource release needs a lease id or --lock <resource>", file=sys.stderr)
            return 2
        if lock_resource:
            if not getattr(args, "force_stale", False):
                print(
                    "agentctl: releasing a machine-wide lock by resource name breaks "
                    "another checkout's claim; it requires --force-stale and a --reason",
                    file=sys.stderr,
                )
                return 1
            ok, detail = _resource_release_foreign_lock(
                root, lock_resource, args.reason, _workflow_session_key(),
            )
            if not ok:
                print(f"agentctl: {detail}", file=sys.stderr)
                return 1
            print(f"agentctl: {detail}")
            return 0
        if getattr(args, "force_stale", False):
            ok, detail = _resource_release_stale(
                root, args.lease, args.reason, _workflow_session_key(),
            )
        else:
            ok, detail = _resource_release_by_id(
                root, args.lease, args.reason,
                holder_type="conversation", holder_id=_workflow_session_key(),
            )
        if not ok:
            print(f"agentctl: {detail}", file=sys.stderr)
            return 1
        print(f"agentctl: resource lease {args.lease} released")
        return 0
    print("agentctl: unknown resource action", file=sys.stderr)
    return 2


# ---------- harness evaluation ----------

def _evals_dir(root: Path) -> Path:
    return root / WORKFLOW_DIR / EVALS_DIR


def _eval_runtime_dir(root: Path) -> Path:
    return root / WORKFLOW_DIR / STATE_DIR / EVALS_DIR


def _eval_suite_path(root: Path, value: str | None = None) -> Path:
    if not value:
        return _evals_dir(root) / EVAL_SUITES_FILE
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (root / path).resolve()


def _eval_validate_catalog(data: object) -> dict:
    if not isinstance(data, dict) or data.get("version") != 1:
        raise ValueError("eval catalog must be an object with version=1")
    suites = data.get("suites")
    if not isinstance(suites, dict) or not suites:
        raise ValueError("eval catalog requires a non-empty suites object")
    for suite_id, suite in suites.items():
        if not isinstance(suite_id, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", suite_id):
            raise ValueError(f"invalid eval suite id: {suite_id!r}")
        if not isinstance(suite, dict):
            raise ValueError(f"eval suite {suite_id} must be an object")
        cases = suite.get("cases")
        if not isinstance(cases, list) or not cases or len(cases) > 100:
            raise ValueError(f"eval suite {suite_id} requires 1-100 cases")
        seen = set()
        splits = set()
        for case in cases:
            if not isinstance(case, dict):
                raise ValueError(f"eval suite {suite_id} has a non-object case")
            case_id = case.get("id")
            if (not isinstance(case_id, str)
                    or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", case_id)
                    or case_id in seen):
                raise ValueError(f"eval suite {suite_id} has an invalid or duplicate case id: {case_id!r}")
            seen.add(case_id)
            split = case.get("split")
            if split not in {"held_in", "held_out"}:
                raise ValueError(f"eval case {case_id} must use split held_in or held_out")
            splits.add(split)
            argv = case.get("argv")
            if (not isinstance(argv, list) or not argv or len(argv) > 64
                    or any(not isinstance(item, str) or not item or len(item) > 4096 for item in argv)):
                raise ValueError(f"eval case {case_id} requires a bounded non-empty argv list")
            timeout = case.get("timeout_seconds", EVAL_TIMEOUT_DEFAULT)
            if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                    or not math.isfinite(float(timeout)) or timeout < 1 or timeout > EVAL_TIMEOUT_MAX):
                raise ValueError(f"eval case {case_id} has an invalid timeout_seconds")
            expected = case.get("expected_exit_codes", [0])
            if (not isinstance(expected, list) or not expected
                    or any(isinstance(code, bool) or not isinstance(code, int) for code in expected)):
                raise ValueError(f"eval case {case_id} has invalid expected_exit_codes")
            if not isinstance(case.get("required", True), bool):
                raise ValueError(f"eval case {case_id} required must be boolean")
            artifacts = case.get("artifacts", [])
            if not isinstance(artifacts, list):
                raise ValueError(f"eval case {case_id} artifacts must be a list")
            for artifact in artifacts:
                if not isinstance(artifact, str) or not artifact:
                    raise ValueError(f"eval case {case_id} has an invalid artifact path")
                artifact_path = Path(artifact)
                if artifact_path.is_absolute() or ".." in artifact_path.parts:
                    raise ValueError(f"eval case {case_id} artifact paths must stay inside the target")
        if splits != {"held_in", "held_out"}:
            raise ValueError(f"eval suite {suite_id} must include held_in and held_out cases")
    return data


def _eval_catalog(root: Path, suite_file: str | None = None) -> tuple[Path, dict]:
    path = _eval_suite_path(root, suite_file)
    if not path.is_file():
        raise ValueError(f"eval suite file not found: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"unable to read eval suite file {path}: {exc}") from exc
    return path, _eval_validate_catalog(data)


def _eval_suite_hash(suite_id: str, suite: dict) -> str:
    payload = json.dumps(
        {"id": suite_id, "suite": suite}, sort_keys=True, separators=(",", ":"),
        ensure_ascii=False, allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _eval_git_snapshot(path: Path) -> tuple[str, bool]:
    head = _git_process(path, "rev-parse", "HEAD")
    status = _git_process(path, "status", "--porcelain", "--untracked-files=all")
    if head.returncode or status.returncode:
        detail = (head.stderr or status.stderr or "not a readable Git checkout").strip()
        raise ValueError(f"unable to inspect eval checkout {path}: {detail}")
    generated_prefixes = (
        f"{WORKFLOW_DIR}/{STATE_DIR}/{EVALS_DIR}/{EVAL_RUNS_DIR}/",
        f"{WORKFLOW_DIR}/{STATE_DIR}/{EVALS_DIR}/{EVAL_DECISIONS_DIR}/",
    )
    relevant = []
    for line in status.stdout.splitlines():
        changed_path = line[3:] if len(line) > 3 else line
        if " -> " in changed_path:
            changed_path = changed_path.split(" -> ", 1)[1]
        changed_path = changed_path.strip('"')
        if not changed_path.startswith(generated_prefixes):
            relevant.append(line)
    return head.stdout.strip(), bool(relevant)


def _eval_environment() -> dict[str, str]:
    allowed = {
        "PATH", "HOME", "USER", "LOGNAME", "SHELL", "TMPDIR", "TMP", "TEMP",
        "LANG", "LC_ALL", "LC_CTYPE", "SYSTEMROOT", "WINDIR", "PATHEXT",
        "COMSPEC", "CI", "GITHUB_ACTIONS",
    }
    env = {key: value for key, value in os.environ.items() if key in allowed}
    env["AGENT_EVAL"] = "1"
    return env


def _eval_run_case(target: Path, case: dict) -> dict:
    started = time.monotonic()
    timeout = float(case.get("timeout_seconds", EVAL_TIMEOUT_DEFAULT))
    popen_args = {}
    if os.name == "posix":
        popen_args["start_new_session"] = True
    elif os.name == "nt":
        popen_args["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    stdout = ""
    stderr = ""
    exit_code = 1
    timed_out = False
    start_error = ""
    try:
        proc = subprocess.Popen(
            case["argv"], cwd=str(target), env=_eval_environment(), text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, **popen_args,
        )
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
            exit_code = proc.returncode
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            stdout = exc.stdout if isinstance(exc.stdout, str) else ""
            stderr = exc.stderr if isinstance(exc.stderr, str) else ""
            _terminate_loop_process(proc)
            tail_out, tail_err = proc.communicate()
            stdout += tail_out or ""
            stderr += tail_err or ""
            exit_code = 124
    except OSError as exc:
        start_error = str(exc)
        stderr = str(exc)
        exit_code = 127
    artifacts = []
    artifacts_ok = True
    for artifact in case.get("artifacts", []):
        exists = (target / artifact).exists()
        artifacts.append({"path": artifact, "exists": exists})
        artifacts_ok = artifacts_ok and exists
    expected = case.get("expected_exit_codes", [0])
    passed = exit_code in expected and artifacts_ok and not timed_out and not start_error
    return {
        "id": case["id"],
        "split": case["split"],
        "required": case.get("required", True),
        "argv": case["argv"],
        "timeout_seconds": timeout,
        "expected_exit_codes": expected,
        "exit_code": exit_code,
        "passed": passed,
        "timed_out": timed_out,
        "start_error": start_error,
        "duration_seconds": round(time.monotonic() - started, 6),
        "artifacts": artifacts,
        "stdout": _cap_output(stdout, EVAL_OUTPUT_CAP),
        "stderr": _cap_output(stderr, EVAL_OUTPUT_CAP),
    }


def _eval_metrics(cases: list[dict]) -> dict:
    metrics = {}
    for split in ("held_in", "held_out", "overall"):
        rows = cases if split == "overall" else [case for case in cases if case["split"] == split]
        passed = sum(1 for case in rows if case["passed"])
        required_failures = [case["id"] for case in rows if case["required"] and not case["passed"]]
        metrics[split] = {
            "total": len(rows),
            "passed": passed,
            "score": round(passed / len(rows), 6) if rows else 0.0,
            "required_failures": required_failures,
        }
    return metrics


def _eval_report_path(root: Path, run_id: str) -> Path:
    return _eval_runtime_dir(root) / EVAL_RUNS_DIR / f"{run_id}.json"


def _eval_signing_key(root: Path, *, create: bool) -> bytes:
    common = _git_common_dir(root)
    if common is None:
        raise ValueError("eval report signing requires a Git repository")
    path = common / WORKTREE_LEASES_DIR / EVAL_SIGNING_KEY_FILE
    try:
        key = path.read_bytes()
    except FileNotFoundError:
        if not create:
            raise ValueError("eval signing key is unavailable; rerun the suite from this policy checkout")
        path.parent.mkdir(parents=True, exist_ok=True)
        generated = secrets.token_bytes(32)
        key = generated if _create_binary_secret(path, generated) else path.read_bytes()
    except OSError as exc:
        raise ValueError(f"unable to read eval signing key: {exc}") from exc
    if len(key) < 32:
        raise ValueError("eval signing key is invalid")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return key


def _eval_record_signature(record: dict, key: bytes) -> str:
    unsigned = dict(record)
    unsigned.pop("integrity", None)
    payload = json.dumps(
        unsigned, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False,
    ).encode("utf-8")
    return hmac.new(key, payload, hashlib.sha256).hexdigest()


def _eval_sign_record(record: dict, key: bytes) -> None:
    record["integrity"] = {
        "algorithm": "hmac-sha256",
        "signature": _eval_record_signature(record, key),
    }


def _eval_verify_record(root: Path, record: dict, label: str) -> None:
    integrity = record.get("integrity")
    if not isinstance(integrity, dict) or integrity.get("algorithm") != "hmac-sha256":
        raise ValueError(f"{label} has no supported supervisor integrity signature")
    signature = integrity.get("signature")
    if not isinstance(signature, str):
        raise ValueError(f"{label} has an invalid supervisor integrity signature")
    expected = _eval_record_signature(record, _eval_signing_key(root, create=False))
    if not hmac.compare_digest(signature, expected):
        raise ValueError(f"{label} failed supervisor integrity verification")


def _eval_load_report(root: Path, report_id: str) -> tuple[Path, dict]:
    name = report_id[:-5] if report_id.endswith(".json") else report_id
    if not re.fullmatch(r"eval-[a-z0-9_-]+", name):
        raise ValueError(f"invalid eval report id: {report_id}")
    path = _eval_report_path(root, name)
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"eval report not found: {name}") from exc
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"unable to read eval report {name}: {exc}") from exc
    if not isinstance(report, dict) or report.get("version") != 1 or report.get("id") != name:
        raise ValueError(f"invalid eval report: {name}")
    _eval_verify_record(root, report, f"eval report {name}")
    return path, report


def _eval_list(root: Path, args: argparse.Namespace) -> int:
    try:
        path, catalog = _eval_catalog(root, args.suite_file)
    except ValueError as exc:
        print(f"agentctl: {exc}", file=sys.stderr)
        return 2
    rows = []
    for suite_id, suite in sorted(catalog["suites"].items()):
        rows.append({
            "id": suite_id,
            "description": suite.get("description", ""),
            "cases": len(suite["cases"]),
            "suite_hash": _eval_suite_hash(suite_id, suite),
        })
    if args.json:
        print(json.dumps({"suite_file": str(path), "suites": rows}, indent=2, ensure_ascii=False))
    else:
        for row in rows:
            print(f"  {row['id']:<24} cases={row['cases']:<3} hash={row['suite_hash'][:12]} {row['description']}")
    return 0


def _eval_run(root: Path, args: argparse.Namespace) -> int:
    try:
        suite_path, catalog = _eval_catalog(root, args.suite_file)
        suite = catalog["suites"].get(args.suite)
        if not suite:
            raise ValueError(f"eval suite not found: {args.suite}")
        target = Path(args.target or root).expanduser().resolve()
        if not target.is_dir():
            raise ValueError(f"eval target is not a directory: {target}")
        # Eval cases run arbitrary argv in cwd=target and can write anywhere in
        # it. If another conversation is live in the target checkout, that is a
        # cross-session clobber channel, so refuse and require an isolated
        # baseline/candidate clone or worktree.
        current_key = _workflow_session_key()
        peers = [
            row for row in _session_rows_unlocked(target)
            if _same_checkout(target, row)
            and row.get("workflow_session_key") != current_key
            and row.get("observed_status") in {"active", "stale"}
        ]
        if peers:
            owners = ", ".join(
                f"{row.get('workflow_session_key')}:{row.get('task')}" for row in peers
            )
            raise ValueError(
                "eval target has a live session and eval cases run arbitrary "
                f"commands there; refusing to clobber peers: {owners}. Run eval "
                "against an isolated baseline/candidate clone or worktree, not a "
                "shared live checkout."
            )
        policy_commit, policy_dirty = _eval_git_snapshot(root)
        target_commit, target_dirty = _eval_git_snapshot(target)
    except ValueError as exc:
        print(f"agentctl: {exc}", file=sys.stderr)
        return 2
    started_at = _now()
    started = time.monotonic()
    results = [_eval_run_case(target, case) for case in suite["cases"]]
    try:
        target_commit_after, target_dirty_after = _eval_git_snapshot(target)
    except ValueError as exc:
        target_commit_after, target_dirty_after = "", True
        results.append({
            "id": "target-post-check",
            "split": "held_out",
            "required": True,
            "argv": [],
            "timeout_seconds": 0,
            "expected_exit_codes": [0],
            "exit_code": 1,
            "passed": False,
            "timed_out": False,
            "start_error": str(exc),
            "duration_seconds": 0.0,
            "artifacts": [],
            "stdout": "",
            "stderr": str(exc),
        })
    metrics = _eval_metrics(results)
    run_id = "eval-" + _safe_segment(args.suite).lower() + "-" + hashlib.sha256(
        f"{time.time_ns()}:{target}:{target_commit}".encode("utf-8")
    ).hexdigest()[:12]
    report = {
        "version": 1,
        "id": run_id,
        "suite": args.suite,
        "suite_hash": _eval_suite_hash(args.suite, suite),
        "suite_source": str(suite_path),
        "policy_root": str(root.resolve()),
        "policy_commit": policy_commit,
        "policy_dirty": policy_dirty,
        "target_root": str(target),
        "target_commit": target_commit,
        "target_dirty": target_dirty,
        "target_commit_after": target_commit_after,
        "target_dirty_after": target_dirty_after,
        "started_at": started_at,
        "finished_at": _now(),
        "duration_seconds": round(time.monotonic() - started, 6),
        "status": "passed" if not metrics["overall"]["required_failures"] else "failed",
        "metrics": metrics,
        "cases": results,
    }
    try:
        _eval_sign_record(report, _eval_signing_key(root, create=True))
    except ValueError as exc:
        print(f"agentctl: {exc}", file=sys.stderr)
        return 2
    path = _eval_report_path(root, run_id)
    _save_json(path, report)
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print(f"agentctl: eval {run_id} -> {report['status']} ({path.relative_to(root)})")
        for split in ("held_in", "held_out", "overall"):
            row = metrics[split]
            print(f"  {split}: {row['passed']}/{row['total']} score={row['score']:.6f}")
    return 0 if report["status"] == "passed" else 1


def _eval_show(root: Path, args: argparse.Namespace) -> int:
    try:
        _path, report = _eval_load_report(root, args.report)
    except ValueError as exc:
        print(f"agentctl: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


def _eval_decision(root: Path, args: argparse.Namespace) -> tuple[dict | None, int]:
    try:
        _base_path, baseline = _eval_load_report(root, args.baseline)
        _candidate_path, candidate = _eval_load_report(root, args.candidate)
        _suite_path, catalog = _eval_catalog(root, args.suite_file)
        suite_id = baseline.get("suite")
        suite = catalog["suites"].get(suite_id)
        if not suite:
            raise ValueError(f"current eval catalog does not contain suite {suite_id}")
        current_hash = _eval_suite_hash(suite_id, suite)
        if candidate.get("suite") != suite_id:
            raise ValueError("baseline and candidate use different eval suites")
        if baseline.get("suite_hash") != current_hash or candidate.get("suite_hash") != current_hash:
            raise ValueError("eval suite hash changed; rerun both baseline and candidate with one policy")
        if baseline.get("policy_commit") != candidate.get("policy_commit"):
            raise ValueError("baseline and candidate were evaluated by different policy commits")
        expected_cases = [(case["id"], case["split"], case.get("required", True)) for case in suite["cases"]]
        for label, report in (("baseline", baseline), ("candidate", candidate)):
            actual_cases = [(case.get("id"), case.get("split"), case.get("required"))
                            for case in report.get("cases") or []]
            if actual_cases != expected_cases:
                raise ValueError(f"{label} report case evidence does not match the current suite")
            recomputed = _eval_metrics(report["cases"])
            if report.get("metrics") != recomputed:
                raise ValueError(f"{label} report metrics do not match its case evidence")
            expected_status = "passed" if not recomputed["overall"]["required_failures"] else "failed"
            if report.get("status") != expected_status:
                raise ValueError(f"{label} report status does not match its case evidence")
            if report.get("policy_dirty"):
                raise ValueError(f"{label} report used a dirty evaluator policy checkout")
    except ValueError as exc:
        print(f"agentctl: {exc}", file=sys.stderr)
        return None, 2
    reasons = []
    if baseline.get("target_dirty"):
        reasons.append("baseline target checkout was dirty")
    if baseline.get("target_commit_after") != baseline.get("target_commit"):
        reasons.append("baseline changed its Git commit during evaluation")
    if baseline.get("target_dirty_after"):
        reasons.append("baseline target checkout became dirty during evaluation")
    if candidate.get("target_dirty"):
        reasons.append("candidate target checkout was dirty")
    if candidate.get("target_commit_after") != candidate.get("target_commit"):
        reasons.append("candidate changed its Git commit during evaluation")
    if candidate.get("target_dirty_after"):
        reasons.append("candidate target checkout became dirty during evaluation")
    for split in ("held_in", "held_out"):
        base_score = float(baseline["metrics"][split]["score"])
        candidate_score = float(candidate["metrics"][split]["score"])
        if candidate_score < base_score:
            reasons.append(f"{split} regressed from {base_score:.6f} to {candidate_score:.6f}")
    required_failures = candidate["metrics"]["overall"].get("required_failures") or []
    if required_failures:
        reasons.append("candidate required cases failed: " + ", ".join(required_failures))
    decision = {
        "version": 1,
        "suite": baseline["suite"],
        "suite_hash": baseline["suite_hash"],
        "policy_commit": baseline["policy_commit"],
        "baseline": baseline["id"],
        "baseline_target_commit": baseline["target_commit"],
        "candidate": candidate["id"],
        "candidate_target_commit": candidate["target_commit"],
        "accepted": not reasons,
        "reasons": reasons,
        "metrics": {
            split: {
                "baseline": baseline["metrics"][split]["score"],
                "candidate": candidate["metrics"][split]["score"],
            } for split in ("held_in", "held_out", "overall")
        },
        "decided_at": _now(),
    }
    return decision, 0 if decision["accepted"] else 1


def _eval_compare(root: Path, args: argparse.Namespace) -> int:
    decision, rc = _eval_decision(root, args)
    if decision is None:
        return rc
    if args.json:
        print(json.dumps(decision, indent=2, ensure_ascii=False))
    else:
        print(f"agentctl: eval comparison -> {'accept' if decision['accepted'] else 'reject'}")
        for split, scores in decision["metrics"].items():
            print(f"  {split}: baseline={scores['baseline']:.6f} candidate={scores['candidate']:.6f}")
        for reason in decision["reasons"]:
            print(f"  reason: {reason}")
    return rc


def _eval_gate(root: Path, args: argparse.Namespace) -> int:
    decision, rc = _eval_decision(root, args)
    if decision is None:
        return rc
    decision_id = "eval-decision-" + hashlib.sha256(
        f"{time.time_ns()}:{decision['baseline']}:{decision['candidate']}".encode("utf-8")
    ).hexdigest()[:12]
    decision["id"] = decision_id
    decision["by"] = args.by
    decision["note"] = args.note or ""
    try:
        _eval_sign_record(decision, _eval_signing_key(root, create=False))
    except ValueError as exc:
        print(f"agentctl: {exc}", file=sys.stderr)
        return 2
    path = _eval_runtime_dir(root) / EVAL_DECISIONS_DIR / f"{decision_id}.json"
    _save_json(path, decision)
    if args.json:
        print(json.dumps(decision, indent=2, ensure_ascii=False))
    else:
        print(
            f"agentctl: eval gate {decision_id} -> "
            f"{'accepted' if decision['accepted'] else 'rejected'} ({path.relative_to(root)})"
        )
        for reason in decision["reasons"]:
            print(f"  reason: {reason}")
    return rc


def cmd_eval(args: argparse.Namespace) -> int:
    root = _repo_root()
    if args.eval_action == "list":
        return _eval_list(root, args)
    if args.eval_action == "run":
        return _eval_run(root, args)
    if args.eval_action == "show":
        return _eval_show(root, args)
    if args.eval_action == "compare":
        return _eval_compare(root, args)
    if args.eval_action == "gate":
        return _eval_gate(root, args)
    print("agentctl: unknown eval action", file=sys.stderr)
    return 2


# ---------- loops ----------

def _loop_follow_up_packets(root: Path, checkpoint: str | None = None) -> list[tuple[Path, dict]]:
    """Open (status=ready) loop follow-up packets sitting in the inbox."""
    packets = []
    inbox = _bus_dir(root, BUS_INBOX)
    if not inbox.is_dir():
        return packets
    for path in sorted(inbox.rglob("*.json")):
        pkt = _load_json(path, {})
        if pkt.get("kind") != LOOP_FOLLOW_UP_KIND:
            continue
        if checkpoint and pkt.get("checkpoint") != checkpoint:
            continue
        if pkt.get("status") != "ready":
            continue
        packets.append((path, pkt))
    return packets


def _create_loop_follow_up(root: Path, checkpoint: str, aggregate: str,
                           reports: list[str], strict: bool,
                           escalate_after: int = LOOP_ESCALATE_AFTER_DEFAULT) -> tuple[str, bool, bool]:
    """Create (or refresh) the follow-up packet for a failed checkpoint.

    Returns (packet_id, created, escalated_now). Deduplicates per checkpoint:
    re-running a still-failing checkpoint updates the existing open packet
    instead of flooding the inbox. When occurrences reaches escalate_after the
    packet is flagged escalated exactly once and requires a human decision.
    """
    session = _load_session(root)
    summary = f"checkpoint {checkpoint} reported {aggregate}" + (" under strict mode" if strict else "")
    existing = _loop_follow_up_packets(root, checkpoint)
    if existing:
        path, pkt = existing[0]
        pkt["updated_at"] = _now()
        pkt["summary"] = summary
        pkt["artifacts"] = reports
        pkt["occurrences"] = int(pkt.get("occurrences") or 1) + 1
        escalated_now = False
        if not pkt.get("escalated") and escalate_after > 0 and pkt["occurrences"] >= escalate_after:
            pkt["escalated"] = True
            pkt["escalated_at"] = _now()
            pkt["notes"] = (pkt.get("notes") or "") + (
                f"\n{pkt['escalated_at']}: ESCALATED after {pkt['occurrences']} failures; "
                "needs a human decision (or 'agentctl finish --ack-escalations' to override)."
            )
            escalated_now = True
        _save_json(path, pkt)
        outbox = _bus_dir(root, BUS_OUTBOX) / (pkt.get("by") or "loop-engine") / f"{pkt['id']}.json"
        if outbox.is_file():
            _save_json(outbox, pkt)
        return pkt["id"], False, escalated_now
    from_task = f"loop-{checkpoint}"
    to_task = session.get("task") or "supervisor"
    packet = {
        "version": 1,
        "id": _packet_id(from_task, to_task),
        "created_at": _now(),
        "status": "ready",
        "kind": LOOP_FOLLOW_UP_KIND,
        "checkpoint": checkpoint,
        "from_task": from_task,
        "to_task": to_task,
        "by": session.get("agent") or "loop-engine",
        "summary": summary,
        "artifacts": reports,
        "notes": "auto-created by loop checkpoint; fix the reported checks, then re-run the checkpoint to auto-close this packet.",
        "occurrences": 1,
    }
    escalated_now = False
    if escalate_after == 1:
        packet["escalated"] = True
        packet["escalated_at"] = _now()
        packet["notes"] += (
            f"\n{packet['escalated_at']}: ESCALATED after {packet['occurrences']} failure(s); "
            "needs a human decision (or 'agentctl finish --ack-escalations' to override)."
        )
        escalated_now = True
    outbox, inbox = _packet_paths(root, packet)
    _save_json(outbox, packet)
    _save_json(inbox, packet)
    _append_handoff_doc(root, packet)
    return packet["id"], True, escalated_now


def _escalated_follow_ups(root: Path, task: str | None = None) -> list[tuple[Path, dict]]:
    """Open follow-up packets that have been escalated, optionally per target task."""
    out = []
    for path, pkt in _loop_follow_up_packets(root):
        if not pkt.get("escalated"):
            continue
        if task and pkt.get("to_task") != task:
            continue
        out.append((path, pkt))
    return out


def _close_loop_follow_ups(root: Path, checkpoint: str, note: str) -> list[str]:
    """Mark open follow-up packets for a now-successful checkpoint as done."""
    closed = []
    for _path, pkt in _loop_follow_up_packets(root, checkpoint):
        pkt["status"] = "done"
        pkt["updated_at"] = _now()
        pkt["notes"] = (pkt.get("notes") or "") + f"\n{pkt['updated_at']}: {note}"
        dest = _bus_dir(root, BUS_DONE) / f"{pkt.get('id')}.json"
        _save_json(dest, pkt)
        for stale in _matching_packet_paths(root, pkt.get("id") or ""):
            if stale != dest and (BUS_INBOX in stale.parts or BUS_OUTBOX in stale.parts):
                try:
                    stale.unlink()
                except OSError:
                    pass
        closed.append(pkt.get("id") or "")
    return [c for c in closed if c]


def _loop_files(root: Path) -> list[Path]:
    base = _loops_dir(root)
    if not base.is_dir():
        return []
    return [
        p for p in sorted(base.glob("*.md"))
        if p.name != "_template.md" and not p.name.startswith(".")
    ]


def _loop_path(root: Path, loop_id: str) -> Path:
    safe = loop_id.strip().replace("/", "-")
    return _loops_dir(root) / f"{safe}.md"


def _loop_contract(root: Path, loop_id: str) -> tuple[Path, str, list[str]]:
    path = _loop_path(root, loop_id)
    text = _read(path)
    if not text:
        return path, text, [f"missing loop file: {path.relative_to(root)}"]
    missing = []
    for section in LOOP_REQUIRED_SECTIONS:
        if not _extract_section(text, f"## {section}"):
            missing.append(section)
    return path, text, missing


def _loop_command_spec(text: str) -> tuple[dict | None, list[str]]:
    """Parse the optional ```loop-check``` command block of a loop contract.

    Returns (spec, errors). spec is None when no block is declared. Inside the
    block: `$ <command>` lines are executed in order, `timeout:`/`max-output:`
    lines set integer options, `#` lines are comments.
    """
    lines = text.splitlines()
    block: list[str] | None = None
    blocks: list[list[str]] = []
    for line in lines:
        stripped = line.strip()
        if block is None:
            if stripped == f"```{LOOP_COMMAND_FENCE}":
                block = []
            continue
        if stripped == "```":
            blocks.append(block)
            block = None
            continue
        block.append(line)
    errors: list[str] = []
    if block is not None:
        errors.append(f"unterminated ```{LOOP_COMMAND_FENCE}``` block")
    if not blocks and not errors:
        return None, []
    if len(blocks) > 1:
        errors.append(f"more than one ```{LOOP_COMMAND_FENCE}``` block; declare exactly one")
    spec = {
        "timeout": LOOP_COMMAND_TIMEOUT_DEFAULT,
        "max_output": LOOP_COMMAND_OUTPUT_CAP_DEFAULT,
        "commands": [],
    }
    option_keys = {"timeout": "timeout", "max-output": "max_output"}
    for raw in (blocks[0] if blocks else []):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("$"):
            cmd = line[1:].strip()
            if not cmd:
                errors.append("empty command after '$'")
            else:
                spec["commands"].append(cmd)
            continue
        key, sep, value = line.partition(":")
        key = key.strip().lower()
        if sep and key in option_keys:
            try:
                num = int(value.strip())
            except ValueError:
                errors.append(f"option '{key}' must be an integer, got '{value.strip()}'")
                continue
            if num <= 0:
                errors.append(f"option '{key}' must be positive, got {num}")
                continue
            if key == "timeout" and num > LOOP_COMMAND_TIMEOUT_MAX:
                errors.append(f"option 'timeout' capped at {LOOP_COMMAND_TIMEOUT_MAX}s, got {num}")
                continue
            spec[option_keys[key]] = num
            continue
        errors.append(f"unrecognized line in {LOOP_COMMAND_FENCE} block: '{line}'")
    if blocks and not spec["commands"]:
        errors.append(f"{LOOP_COMMAND_FENCE} block declares no '$ <command>' lines")
    if errors:
        return None, errors
    return spec, []


def _loop_summary_line(root: Path, path: Path) -> dict:
    text = _read(path)
    missing = [
        section for section in LOOP_REQUIRED_SECTIONS
        if not _extract_section(text, f"## {section}")
    ]
    trigger = "-"
    trig = _extract_section(text, "## Trigger").splitlines()
    for line in trig:
        clean = line.strip(" -")
        if clean:
            trigger = clean
            break
    return {
        "id": path.stem,
        "path": str(path.relative_to(root)),
        "trigger": trigger,
        "ok": not missing,
        "missing": missing,
    }


def _load_loop_checkpoints(root: Path) -> dict:
    policy = _load_json(_loop_checkpoints_path(root), {})
    if not isinstance(policy, dict):
        policy = {}
    checkpoints = policy.get("checkpoints")
    if not isinstance(checkpoints, dict):
        checkpoints = {}
    merged = json.loads(json.dumps(DEFAULT_LOOP_CHECKPOINTS))
    merged["checkpoints"].update(checkpoints)
    return merged


def _parse_time(value: str | None) -> _dt.datetime | None:
    if not value:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y%m%d-%H%M%S"):
        try:
            return _dt.datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


def _checkpoint_input_fingerprint(root: Path) -> str:
    """Fingerprint the coordination docs a checkpoint reconciles against.

    Debounce exists to skip redundant reruns, but the loop contracts promise
    'do not rerun ... unless the plan changed'. Hash the plan, task index, and
    board so a genuine change bypasses the time window instead of being
    silently suppressed.
    """
    h = hashlib.sha256()
    for rel in (
        f"{WORKFLOW_DIR}/{PLAN_FILE}",
        f"{WORKFLOW_DIR}/{TASKS_FILE}",
        f"{WORKFLOW_DIR}/{BOARD_FILE}",
    ):
        p = root / rel
        if p.is_file():
            h.update(rel.encode("utf-8"))
            h.update(p.read_bytes())
    return h.hexdigest()[:16]


def _checkpoint_recent(state: dict, checkpoint: str, debounce_minutes: int,
                       input_hash: str | None = None) -> bool:
    if debounce_minutes <= 0:
        return False
    entry = (state.get("checkpoints") or {}).get(checkpoint) or {}
    last = _parse_time(entry.get("last_run_at"))
    if not last:
        return False
    if (_dt.datetime.now() - last).total_seconds() >= debounce_minutes * 60:
        return False
    # Within the window, but a coordination-doc change bypasses debounce so a
    # changed plan is never silently skipped (per the loop contract).
    if input_hash is not None and entry.get("last_input_hash") != input_hash:
        return False
    return True


def _acquire_lock_file(path: Path, timeout_seconds: float = 10.0, stale_seconds: float = 300.0) -> int:
    """Acquire a process-scoped advisory lock; the OS releases it after crashes."""
    del stale_seconds
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout_seconds
    payload = (
        f"pid={os.getpid()} host={_cycle_host_id()} acquired_at={_now()}\n"
    ).encode("utf-8")
    fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        if os.name == "posix":
            import fcntl

            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError(f"timed out waiting for lock {path}")
                    time.sleep(0.05)
        else:
            import msvcrt

            if path.stat().st_size == 0:
                os.write(fd, b"\0")
            while True:
                try:
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError(f"timed out waiting for lock {path}")
                    time.sleep(0.05)
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, payload)
        os.fsync(fd)
        return fd
    except BaseException:
        os.close(fd)
        raise


def _release_lock_file(path: Path, fd: int) -> None:
    del path
    try:
        if os.name == "posix":
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_UN)
        else:
            import msvcrt

            os.lseek(fd, 0, os.SEEK_SET)
            try:
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
    finally:
        os.close(fd)


def _update_loop_state(root: Path, updater) -> dict:
    lock = _loop_state_lock_path(root)
    fd = _acquire_lock_file(lock)
    try:
        state = _load_json(_loop_state_path(root), {"version": 1, "loops": {}, "checkpoints": {}})
        if not isinstance(state, dict):
            state = {"version": 1, "loops": {}, "checkpoints": {}}
        state.setdefault("version", 1)
        state.setdefault("loops", {})
        state.setdefault("checkpoints", {})
        updater(state)
        _save_json(_loop_state_path(root), state)
        return state
    finally:
        _release_lock_file(lock, fd)


def _format_bullets(items) -> str:
    if not items:
        return "- none"
    return "\n".join(f"- {item}" for item in items)


def _short_file_status(root: Path, rel: str) -> str:
    path = root / rel
    return "present" if path.exists() else "missing"


def _tasks_index_rows(text: str) -> dict:
    rows = {}
    for line in text.splitlines():
        if not line.startswith("| ") or line.startswith("| ID ") or line.startswith("|---"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) >= 6 and TASK_RECORD_ID_RE.fullmatch(cells[0]):
            rows[cells[0]] = {"status": cells[1], "owner": cells[2], "scope": cells[3], "title": cells[5]}
    return rows


def _scope_from_index_cell(value: str) -> list[str]:
    cell = (value or "").strip()
    if len(cell) >= 2 and cell.startswith("`") and cell.endswith("`"):
        cell = cell[1:-1].strip()
    if cell in {"", "-"}:
        return []
    return [item.strip() for item in cell.split(",") if item.strip()]


def _plan_task_rows(text: str) -> dict:
    rows = {}
    # The id group must accept every TASK_RECORD_ID_RE-shaped id, including
    # ids with more than one hyphen (e.g. TR024-REVIEW-001); a narrower
    # pattern here makes CI report a rendered row as missing.
    pattern = re.compile(
        r"^- \[([ xX])\]\s+([A-Za-z][A-Za-z0-9]*-[\w.-]+)\s+-\s+"
        r"(.*?)(?:\s+\(owner:\s*([^)]+)\))?\s*$"
    )
    for line in text.splitlines():
        match = pattern.match(line)
        if not match or not TASK_RECORD_ID_RE.fullmatch(match.group(2)):
            continue
        rows[match.group(2)] = {
            "checked": match.group(1).lower() == "x",
            "title": match.group(3).strip(),
            "owner": (match.group(4) or "").strip(),
        }
    return rows


def _task_doc_status(path: Path) -> str:
    match = re.search(r"^Status:\s*(\S+)\s*$", _read(path), flags=re.M)
    return match.group(1) if match else ""


def _task_evidence_ids(root: Path) -> dict[str, set[str]]:
    index_ids = set(_tasks_index_rows(_read(root / WORKFLOW_DIR / TASKS_FILE)))
    plan_ids = set(_plan_task_rows(_read(root / WORKFLOW_DIR / PLAN_FILE)))
    docs_dir = root / WORKFLOW_DIR / TASKS_DIR
    doc_ids = {
        path.stem
        for path in docs_dir.glob("*.md")
        if path.name != "_template.md" and TASK_RECORD_ID_RE.fullmatch(path.stem)
    } if docs_dir.is_dir() else set()
    return {"TASKS.md": index_ids, "PROJECT_PLAN.md": plan_ids, "task docs": doc_ids}


def _task_reconcile_problems(root: Path) -> list[str]:
    problems = []
    board = _load_board(root)
    tasks = board.get("tasks")
    if not isinstance(tasks, dict):
        return ["canonical board tasks must be an object"]
    task_rows = _tasks_index_rows(_read(root / WORKFLOW_DIR / TASKS_FILE))
    plan_rows = _plan_task_rows(_read(root / WORKFLOW_DIR / PLAN_FILE))
    evidence = _task_evidence_ids(root)
    canonical_ids = set(tasks)
    for source, ids in evidence.items():
        for task in sorted(ids - canonical_ids):
            problems.append(f"{task}: present in {source} but missing from canonical board")
    for task in sorted(canonical_ids):
        entry = tasks.get(task)
        if not TASK_RECORD_ID_RE.fullmatch(task):
            problems.append(f"{task}: canonical task id is unsafe or unsupported")
            continue
        if not isinstance(entry, dict):
            problems.append(f"{task}: canonical task record must be an object")
            continue
        status = str(entry.get("status") or "")
        task_type = str(entry.get("type") or "generic")
        owner = str(entry.get("owner") or "")
        scope = [str(item) for item in entry.get("scope") or []]
        title = str(entry.get("title") or task)
        for scope_problem in _scope_errors(scope):
            problems.append(f"{task}: {scope_problem}")
        if task_type not in TASK_TYPES:
            problems.append(f"{task}: unsupported task type '{task_type}'")
        if status not in STATUSES:
            problems.append(f"task {task} has invalid status '{status}'")
        if status in ACTIVE_STATUSES and not owner:
            problems.append(f"task {task} is in_progress but has no owner")
        for dependency in entry.get("deps") or []:
            if dependency not in tasks:
                problems.append(f"{task}: dependency {dependency} is missing from canonical board")
        row = task_rows.get(task)
        if not row:
            problems.append(f"{task}: missing generated row in TASKS.md")
        else:
            row_owner = "" if row["owner"] in {"", "-"} else row["owner"]
            if row["status"] != status:
                problems.append(
                    f"{task}: TASKS.md status {row['status']} differs from canonical status {status}"
                )
            if row_owner != owner:
                problems.append(
                    f"{task}: TASKS.md owner {row['owner']} differs from canonical owner {owner or '-'}"
                )
            if _scope_from_index_cell(row["scope"]) != scope:
                problems.append(f"{task}: TASKS.md scope differs from canonical scope")
            if row["title"] != title:
                problems.append(f"{task}: TASKS.md title differs from canonical title")
        plan_row = plan_rows.get(task)
        if not plan_row:
            problems.append(f"{task}: missing generated row in PROJECT_PLAN.md")
        else:
            if plan_row["title"] != title:
                problems.append(f"{task}: PROJECT_PLAN.md title differs from canonical title")
            if plan_row["owner"] != owner:
                problems.append(f"{task}: PROJECT_PLAN.md owner differs from canonical owner")
            expected_checked = status == "done"
            if plan_row["checked"] != expected_checked:
                problems.append(
                    f"{task}: PROJECT_PLAN.md checkbox differs from canonical status {status}"
                )
        doc = root / WORKFLOW_DIR / TASKS_DIR / f"{task}.md"
        if not doc.is_file():
            problems.append(f"{task}: missing task document")
        else:
            doc_status = _task_doc_status(doc)
            if doc_status != status:
                problems.append(
                    f"{task}: task document status {doc_status or '<missing>'} "
                    f"differs from canonical status {status}"
                )
    return problems


def _markdown_table_cell(value: object) -> str:
    return str(value or "").replace("|", "\\|").replace("\n", " ").strip()


def _render_task_views(root: Path, board: dict) -> None:
    tasks = board.get("tasks") or {}
    index_lines = [
        "# Task Index",
        "",
        "| ID | Status | Owner | Scope | Task Doc | Title |",
        "|---|---|---|---|---|---|",
    ]
    for task, entry in tasks.items():
        owner = entry.get("owner") or "-"
        scope = _format_scope(entry.get("scope"))
        title = _markdown_table_cell(entry.get("title") or task)
        index_lines.append(
            f"| {task} | {entry.get('status') or ''} | {_markdown_table_cell(owner)} | "
            f"`{_markdown_table_cell(scope)}` | "
            f"[.agent/tasks/{task}.md](tasks/{task}.md) | {title} |"
        )
    _write_atomic_text(root / WORKFLOW_DIR / TASKS_FILE, "\n".join(index_lines) + "\n")

    plan_path = root / WORKFLOW_DIR / PLAN_FILE
    plan = _read(plan_path)
    heading = "## Task Board"
    start = plan.find(heading)
    if start < 0:
        raise ValueError("PROJECT_PLAN.md missing Task Board section")
    content_start = start + len(heading)
    next_heading = re.search(r"^##\s+", plan[content_start:], flags=re.M)
    content_end = (
        content_start + next_heading.start()
        if next_heading else len(plan)
    )
    plan_lines = [heading]
    for task, entry in reversed(list(tasks.items())):
        checked = "x" if entry.get("status") == "done" else " "
        owner = entry.get("owner") or ""
        suffix = f" (owner: {owner})" if owner else ""
        plan_lines.append(
            f"- [{checked}] {task} - {entry.get('title') or task}{suffix}"
        )
    replacement = "\n".join(plan_lines) + "\n\n"
    rendered_plan = plan[:start] + replacement + plan[content_end:].lstrip("\n")
    _write_atomic_text(plan_path, rendered_plan)

    for task, entry in tasks.items():
        _set_task_doc_status(root, task, str(entry.get("status") or ""))


def _legacy_task_record(root: Path, task: str, row: dict, plan_row: dict) -> dict:
    doc = root / WORKFLOW_DIR / TASKS_DIR / f"{task}.md"
    if not doc.is_file():
        raise ValueError(f"{task}: legacy migration requires a task document")
    doc_status = _task_doc_status(doc)
    if not doc_status or doc_status != row.get("status"):
        raise ValueError(
            f"{task}: task document status {doc_status or '<missing>'} "
            f"does not match TASKS.md status {row.get('status') or '<missing>'}"
        )
    if plan_row.get("title") != row.get("title"):
        raise ValueError(f"{task}: PROJECT_PLAN.md and TASKS.md titles differ")
    owner = "" if row.get("owner") in {"", "-"} else str(row.get("owner") or "")
    if plan_row.get("owner") != owner:
        raise ValueError(f"{task}: PROJECT_PLAN.md and TASKS.md owners differ")
    text = _read(doc)
    created = re.search(r"^Created:\s*(.+)$", text, flags=re.M)
    updated = re.search(r"^Updated:\s*(.+)$", text, flags=re.M)
    return {
        "title": row.get("title") or task,
        "type": "generic",
        "status": row.get("status"),
        "owner": owner or None,
        "scope": _scope_from_index_cell(row.get("scope") or ""),
        "deps": [],
        "created_at": created.group(1).strip() if created else "legacy",
        "updated_at": updated.group(1).strip() if updated else _now(),
        "migration": {"source": "legacy-task-views", "migrated_at": _now()},
    }


def _reconcile_merge_back(root: Path, args: argparse.Namespace) -> int:
    """Import per-task ledger records from another git ref into this checkout.

    This is the tooled version of the manual worktree merge-back documented
    in docs/worktree-merge-back.md: after a task finishes on its worktree
    branch, its board entry, task document, and gate record need to reach the
    planning checkout before the review gate (or a later `git merge`) can see
    them. Only task-scoped records move; sessions, leases, and loop state
    stay untouched.
    """
    st = _load_session(root)
    if not str(st.get("agent") or ""):
        print(
            "agentctl: merge-back requires an active session so the import "
            "is attributable (run 'agentctl work --agent <name>' first)",
            file=sys.stderr,
        )
        return 1
    try:
        if _managed_worktree_lease(root):
            print(
                "agentctl: merge-back must run from the planning checkout, "
                "not from inside a managed task worktree",
                file=sys.stderr,
            )
            return 1
    except RuntimeError as exc:
        print(f"agentctl: {exc}", file=sys.stderr)
        return 2
    ref = str(getattr(args, "from_ref", "") or "").strip()
    if not ref:
        print("agentctl: --from-ref is required", file=sys.stderr)
        return 2
    resolved = _git(root, "rev-parse", "--verify", f"{ref}^{{commit}}")
    if not resolved:
        print(f"agentctl: cannot resolve git ref '{ref}'", file=sys.stderr)
        return 2
    shown = _git_process(root, "show", f"{resolved}:{WORKFLOW_DIR}/board.json")
    if shown.returncode:
        print(
            f"agentctl: {ref} has no {WORKFLOW_DIR}/board.json",
            file=sys.stderr,
        )
        return 2
    try:
        source_board = json.loads(shown.stdout)
    except json.JSONDecodeError as exc:
        print(
            f"agentctl: board.json on {ref} is not valid JSON: {exc}",
            file=sys.stderr,
        )
        return 2
    source_tasks = (
        source_board.get("tasks") if isinstance(source_board, dict) else {}
    ) or {}
    requested = [
        t.strip() for t in (getattr(args, "task", None) or []) if t.strip()
    ]
    unknown = [t for t in requested if t not in source_tasks]
    if unknown:
        print(
            f"agentctl: not on the {ref} board: {', '.join(sorted(unknown))}",
            file=sys.stderr,
        )
        return 2
    order = {"todo": 0, "in_progress": 1, "review": 2, "approved": 3, "done": 4}
    auto_eligible = {"review", "approved", "done"}
    lock = _session_coordination_lock_path(root)
    fd = _acquire_lock_file(lock)
    try:
        board = _load_board(root)
        local_tasks = board.setdefault("tasks", {})
        plan: list[tuple[str, str]] = []
        skipped: list[tuple[str, str]] = []
        for tid in (requested or sorted(source_tasks)):
            # The id becomes a filename below, so an id of an unexpected
            # shape (path separators, dot-dot) from a foreign board must
            # never reach the filesystem.
            if not TASK_RECORD_ID_RE.fullmatch(str(tid)):
                skipped.append((tid, "id is not a valid task record id"))
                continue
            entry = source_tasks.get(tid)
            if not isinstance(entry, dict):
                skipped.append((tid, "malformed source entry"))
                continue
            src_status = str(entry.get("status") or "")
            local = local_tasks.get(tid)
            if not requested and src_status not in auto_eligible:
                # Auto-discovery only imports work that already cleared
                # finish/review; anything earlier is still someone's live
                # claim and moves only when named explicitly.
                continue
            if src_status not in order:
                skipped.append(
                    (tid, f"source status '{src_status or '<missing>'}' "
                          "must be resolved manually")
                )
                continue
            if not isinstance(local, dict):
                plan.append((tid, f"import as {src_status}"))
                continue
            loc_status = str(local.get("status") or "")
            if local == entry:
                if requested:
                    skipped.append((tid, "already in sync"))
                continue
            if loc_status not in order:
                skipped.append(
                    (tid, f"local status '{loc_status or '<missing>'}' "
                          "must be resolved manually")
                )
                continue
            if order[loc_status] > order[src_status]:
                skipped.append(
                    (tid, f"local status '{loc_status}' is ahead of source "
                          f"'{src_status}'")
                )
                continue
            if loc_status == src_status and not requested:
                # Same rank but different content: contents may legitimately
                # differ (timestamps, notes); overwrite only when named.
                continue
            plan.append((tid, f"{loc_status} -> {src_status}"))
        if getattr(args, "dry_run", False):
            for tid, action in plan:
                print(f"agentctl: would merge back {tid} ({action})")
            for tid, why in skipped:
                print(f"agentctl: would skip {tid}: {why}")
            if not plan and not skipped:
                print("agentctl: nothing to merge back")
            return 0
        if not plan:
            print("agentctl: nothing to merge back")
            for tid, why in skipped:
                print(f"  - skipped {tid}: {why}")
            return 1 if (requested and skipped) else 0
        missing_docs: list[str] = []
        for tid, action in plan:
            local_tasks[tid] = source_tasks[tid]
            doc_found = False
            for subdir in (TASKS_DIR, GATES_DIR):
                rel = f"{WORKFLOW_DIR}/{subdir}/{tid}.md"
                blob = _git_process(root, "show", f"{resolved}:{rel}")
                if blob.returncode == 0:
                    _write(root / rel, blob.stdout)
                    if subdir == TASKS_DIR:
                        doc_found = True
            if not doc_found:
                missing_docs.append(tid)
        _save_board(root, board)
        _render_task_views(root, board)
    finally:
        _release_lock_file(lock, fd)
    for tid, action in plan:
        print(f"agentctl: merged back {tid} ({action})")
    for tid, why in skipped:
        print(f"agentctl: skipped {tid}: {why}")
    for tid in missing_docs:
        print(
            f"agentctl: warning: {ref} has no task document for {tid}; "
            "CI will flag it if the task is done",
            file=sys.stderr,
        )
    print(
        "agentctl: review the diff, then commit the ledger "
        "(git add .agent && git commit)"
    )
    return 0


def cmd_reconcile(args: argparse.Namespace) -> int:
    root = _repo_root()
    if args.reconcile_action == "check":
        problems = _task_reconcile_problems(root)
        if problems:
            print("agentctl reconcile: FAIL")
            for problem in problems:
                print(f"  - {problem}")
            return 1
        print("agentctl reconcile: OK")
        return 0
    if args.reconcile_action == "render":
        board = _load_board(root)
        canonical_ids = set((board.get("tasks") or {}))
        extras = {
            task
            for ids in _task_evidence_ids(root).values()
            for task in ids - canonical_ids
        }
        if extras:
            print(
                "agentctl: render would drop task evidence absent from the canonical board: "
                + ", ".join(sorted(extras))
                + "; run 'agentctl reconcile migrate' after inspecting the evidence",
                file=sys.stderr,
            )
            return 1
        _render_task_views(root, board)
        print("agentctl: rendered task views from canonical board")
        return 0
    if args.reconcile_action == "migrate":
        lock = _session_coordination_lock_path(root)
        fd = _acquire_lock_file(lock)
        try:
            board = _load_board(root)
            tasks = board.setdefault("tasks", {})
            task_rows = _tasks_index_rows(_read(root / WORKFLOW_DIR / TASKS_FILE))
            plan_rows = _plan_task_rows(_read(root / WORKFLOW_DIR / PLAN_FILE))
            evidence = _task_evidence_ids(root)
            extras = sorted(set().union(*evidence.values()) - set(tasks))
            for task in extras:
                if task not in task_rows or task not in plan_rows:
                    print(
                        f"agentctl: {task} cannot be migrated because its legacy views are incomplete",
                        file=sys.stderr,
                    )
                    return 1
                try:
                    tasks[task] = _legacy_task_record(
                        root, task, task_rows[task], plan_rows[task],
                    )
                except ValueError as exc:
                    print(f"agentctl: {exc}", file=sys.stderr)
                    return 1
            if extras:
                _save_board(root, board)
            _render_task_views(root, board)
        finally:
            _release_lock_file(lock, fd)
        print(
            "agentctl: migrated legacy task evidence into canonical board"
            + (": " + ", ".join(extras) if extras else " (nothing to migrate)")
        )
        return 0
    if args.reconcile_action == "archive":
        st = _load_session(root)
        recorder = str(st.get("agent") or "")
        role = str(_agent_profile(root, recorder).get("role") or "").lower()
        if not recorder or not any(
            label in role for label in ("supervisor", "planning", "review")
        ):
            print(
                "agentctl: archiving requires an active supervisor/planning/review session",
                file=sys.stderr,
            )
            return 1
        try:
            days = float(args.days)
        except (TypeError, ValueError):
            print("agentctl: --days must be a number", file=sys.stderr)
            return 2
        if not math.isfinite(days) or days < 0:
            print("agentctl: --days must be a nonnegative finite number", file=sys.stderr)
            return 2
        cutoff = _dt.datetime.now() - _dt.timedelta(days=days)
        archive_root = root / WORKFLOW_DIR / "archive"
        lock = _session_coordination_lock_path(root)
        fd = _acquire_lock_file(lock)
        moved: list[tuple[Path, Path]] = []
        try:
            board = _load_board(root)
            tasks = board.get("tasks") or {}
            aged = []
            for tid, entry in tasks.items():
                if not isinstance(entry, dict) or entry.get("status") != "done":
                    continue
                stamp = _parse_workflow_timestamp(entry.get("updated_at"))
                if stamp is None or stamp >= cutoff:
                    # Unparseable timestamps stay live rather than vanish
                    # into the archive unnoticed.
                    continue
                aged.append(tid)
            if not aged:
                print("agentctl: no done tasks older than the archive window")
                return 0
            try:
                for tid in aged:
                    for source_dir, target_dir in (
                        (root / WORKFLOW_DIR / TASKS_DIR, archive_root / "tasks"),
                        (root / WORKFLOW_DIR / GATES_DIR, archive_root / "gates"),
                    ):
                        source = source_dir / f"{tid}.md"
                        if not source.is_file():
                            continue
                        target_dir.mkdir(parents=True, exist_ok=True)
                        target = target_dir / f"{tid}.md"
                        os.replace(source, target)
                        moved.append((source, target))
            except OSError as exc:
                for source, target in reversed(moved):
                    try:
                        os.replace(target, source)
                    except OSError:
                        pass
                print(f"agentctl: archive aborted, no state changed: {exc}", file=sys.stderr)
                return 2
            archived_board_path = archive_root / "board.json"
            archived = _load_json(
                archived_board_path, {"version": 1, "tasks": {}},
            )
            if not isinstance(archived, dict):
                archived = {"version": 1, "tasks": {}}
            archived.setdefault("tasks", {})
            for tid in aged:
                archived["tasks"][tid] = tasks.pop(tid)
            archived["updated_at"] = _now()
            archive_root.mkdir(parents=True, exist_ok=True)
            _save_json(archived_board_path, archived)
            _save_board(root, board)
            _render_task_views(root, board)
        finally:
            _release_lock_file(lock, fd)
        for tid in aged:
            print(f"agentctl: archived {tid}")
        print(
            f"agentctl: archived {len(aged)} done task(s) older than {days:g} days "
            f"into {archive_root}"
        )
        return 0
    if args.reconcile_action == "merge-back":
        return _reconcile_merge_back(root, args)
    if args.reconcile_action == "close-decided-reviews":
        st = _load_session(root)
        recorder = str(st.get("agent") or "")
        role = str(_agent_profile(root, recorder).get("role") or "").lower()
        if not recorder or not any(
            label in role for label in ("supervisor", "planning", "review")
        ):
            print(
                "agentctl: closing decided reviews requires an active "
                "supervisor/planning/review session",
                file=sys.stderr,
            )
            return 1
        lock = _session_coordination_lock_path(root)
        fd = _acquire_lock_file(lock)
        try:
            board = _load_board(root)
            closed = []
            for tid, entry in sorted((board.get("tasks") or {}).items()):
                if not isinstance(entry, dict) or entry.get("status") != "review":
                    continue
                # Legacy review tasks predate task types, so the backlog pass
                # keys on the recorded decisions plus the .agent-only scope.
                decisions = _decided_review_closure(
                    root, tid, entry, require_review_type=False,
                )
                if not decisions:
                    continue
                entry["status"] = "done"
                entry["updated_at"] = _now()
                _check_plan_box(root, tid)
                _set_task_doc_status(root, tid, "done")
                _update_tasks_index(
                    root, tid, status="done", owner=entry.get("owner"),
                    scope=entry.get("scope"), title=entry.get("title"),
                )
                closed.append((tid, decisions))
            if closed:
                _save_board(root, board)
        finally:
            _release_lock_file(lock, fd)
        for tid, decisions in closed:
            print(f"agentctl: {tid} -> done (decisions: {', '.join(decisions)})")
        print(f"agentctl: closed {len(closed)} decided review task(s)")
        return 0
    print("agentctl: unknown reconcile action", file=sys.stderr)
    return 2


def _loop_daily_plan_triage(root: Path) -> dict:
    board = _load_board(root)
    plan = _read(root / WORKFLOW_DIR / PLAN_FILE)
    tasks_md = _read(root / WORKFLOW_DIR / TASKS_FILE)
    task_rows = _tasks_index_rows(tasks_md)
    checks = []
    feedback = []
    next_steps = []
    tasks = board.get("tasks", {})

    if not tasks:
        checks.append("board has no tasks")
    checks.extend(_task_reconcile_problems(root))
    for tid, entry in sorted(tasks.items()):
        row = task_rows.get(tid)
        doc = root / WORKFLOW_DIR / TASKS_DIR / f"{tid}.md"
        status = entry.get("status") or ""
        if not row:
            checks.append(f"{tid}: missing row in .agent/TASKS.md")
        elif row["status"] != status:
            checks.append(f"{tid}: board status {status} differs from TASKS.md status {row['status']}")
        if not doc.is_file():
            checks.append(f"{tid}: missing task doc")
        if not _plan_has_task_row(root, tid):
            checks.append(f"{tid}: missing task board row in PROJECT_PLAN.md")
        if status == "review":
            feedback.append(f"{tid}: awaiting review gate")
        if status == "in_progress":
            feedback.append(f"{tid}: currently in progress; keep notes current")
        if status == "done" and not _plan_checked(root, tid):
            checks.append(f"{tid}: done but not checked in PROJECT_PLAN.md")
    if "## Task Board" not in plan:
        checks.append("PROJECT_PLAN.md missing Task Board section")
    open_follow_ups = _loop_follow_up_packets(root)
    for _path, pkt in open_follow_ups:
        if pkt.get("escalated"):
            checks.append(
                f"ESCALATED loop follow-up {pkt.get('id')} (checkpoint={pkt.get('checkpoint')}, "
                f"occurrences={pkt.get('occurrences', 1)}): repeated failures need a human decision"
            )
            next_steps.append(
                f"Escalated follow-up {pkt.get('id')}: a human should decide how to unblock checkpoint "
                f"{pkt.get('checkpoint')}; 'finish --ack-escalations' overrides only with recorded intent."
            )
            continue
        feedback.append(
            f"open loop follow-up {pkt.get('id')} (checkpoint={pkt.get('checkpoint')}, "
            f"occurrences={pkt.get('occurrences', 1)}): {pkt.get('summary')}"
        )
        next_steps.append(
            f"Resolve follow-up {pkt.get('id')}: fix the reported checks, then re-run "
            f"'agentctl loop auto --checkpoint {pkt.get('checkpoint')} --once --force' to auto-close it."
        )
    runtime = _cycle_runtime(root)
    if runtime and runtime.get("status") == "interrupted":
        feedback.append(
            f"loop runtime {runtime.get('id')} was interrupted at "
            f"{runtime.get('completed_cycles', 0)}/{runtime.get('requested_cycles', 0)} cycles"
        )
        if runtime.get("resume_safe") is False:
            next_steps.append(
                "Inspect the in-flight command and its side effects; after it exits, reconcile with "
                "'agentctl loop stop --ack-inflight --reason <what-was-verified>'."
            )
        else:
            next_steps.append(
                "Inspect 'agentctl loop status', then use 'agentctl loop resume' or "
                "'agentctl loop stop --reason <reason>' before starting a replacement cycle."
            )
    elif runtime and runtime.get("status") in {"running", "stop_requested"}:
        feedback.append(
            f"loop runtime {runtime.get('id')} is {runtime.get('status')} at "
            f"{runtime.get('completed_cycles', 0)}/{runtime.get('requested_cycles', 0)} cycles"
        )
    elif runtime and runtime.get("status") in {"failed", "blocked"}:
        feedback.append(
            f"loop runtime {runtime.get('id')} ended {runtime.get('status')}: "
            f"{runtime.get('stop_reason') or 'inspect its reports'}"
        )
    if checks:
        next_steps.append("Resolve plan/task/board inconsistencies before relying on automation.")
    elif not open_follow_ups:
        next_steps.append("Plan, task index, and board are consistent enough for the next loop.")
    return {
        "status": "partial" if checks else "success",
        "read": [".agent/PROJECT_PLAN.md", ".agent/TASKS.md", ".agent/board.json", ".agent/tasks/*.md",
                 f".agent/{BUS_DIR}/{BUS_INBOX}/ (loop follow-ups)"],
        "actions": ["Compared board state, task index rows, task docs, and plan checkboxes.",
                    "Scanned the bus inbox for open loop follow-up packets."],
        "checks": checks or ["No plan/task/board inconsistencies found."],
        "feedback": feedback or ["No review or in-progress follow-up requiring triage."],
        "memory": ["Wrote this loop run report.", "Updated .agent/loops/state.json."],
        "next": next_steps,
    }


def _loop_doc_hygiene(root: Path) -> dict:
    checks = []
    feedback = []
    required = (
        "## Task Contract", "## Context To Read Before Starting", "## Work Scope",
        "## Stage Plan", "## Stage Log", "## Verification", "## Completion Record",
    )
    for doc in sorted((root / WORKFLOW_DIR / TASKS_DIR).glob("*.md")):
        if doc.name == "_template.md":
            continue
        rel = str(doc.relative_to(root))
        text = _read(doc)
        for header in required:
            if header not in text:
                checks.append(f"{rel}: missing {header}")
        stage_log = _extract_section(text, "## Stage Log")
        lines = [ln.strip() for ln in stage_log.splitlines() if ln.strip().startswith("- ")]
        duplicates = [item for item, count in Counter(lines).items() if count > 1]
        if duplicates:
            checks.append(f"{rel}: duplicate Stage Log lines: {len(duplicates)}")
        if "Status: review" in text and "- Summary:" in text and "- Summary:\n" in text:
            checks.append(f"{rel}: review task has empty completion summary")
        if "- No updates yet." in stage_log and len(lines) > 1:
            checks.append(f"{rel}: Stage Log still contains placeholder plus real updates")
    for rel in (".agent/PROJECT_PLAN.md", ".agent/TASKS.md", ".agent/WORKFLOW_ENTRY.md"):
        feedback.append(f"{rel}: {_short_file_status(root, rel)}")
    return {
        "status": "partial" if checks else "success",
        "read": [".agent/tasks/*.md", ".agent/PROJECT_PLAN.md", ".agent/TASKS.md", ".agent/WORKFLOW_ENTRY.md"],
        "actions": ["Checked task document schema, duplicate stage log entries, and required workflow docs."],
        "checks": checks or ["No document hygiene issues found in task docs."],
        "feedback": feedback,
        "memory": ["Wrote this loop run report.", "Updated .agent/loops/state.json."],
        "next": ["Fix reported document hygiene issues." if checks else "No doc cleanup is required before the next loop."],
    }


def _bounded_walk_markers(root: Path, bases: list[str], limit: int = 5000) -> tuple[Counter, list[str], bool]:
    counts = Counter()
    samples = []
    capped = False
    seen = 0
    for rel in bases:
        base = root / rel
        if not base.exists():
            continue
        for dirpath, _dirs, files in os.walk(base):
            for fn in files:
                seen += 1
                if seen > limit:
                    capped = True
                    return counts, samples, capped
                if fn in {"DONE", "ERROR"}:
                    counts[fn] += 1
                    if len(samples) < 10:
                        samples.append(str((Path(dirpath) / fn).relative_to(root)))
    return counts, samples, capped


def _loop_experiment_monitor(root: Path) -> dict:
    bases = ["results", "experiments/analysis_outputs", "experiments/logs"]
    counts, samples, capped = _bounded_walk_markers(root, bases)
    checks = []
    if not any((root / rel).exists() for rel in bases):
        checks.append("no standard experiment result directories found")
    if counts.get("ERROR", 0):
        checks.append(f"found {counts['ERROR']} ERROR markers")
    if capped:
        checks.append("experiment scan hit file limit; narrow the loop scope before relying on counts")
    feedback = [
        f"DONE markers: {counts.get('DONE', 0)}",
        f"ERROR markers: {counts.get('ERROR', 0)}",
    ]
    if samples:
        feedback.append("sample markers: " + ", ".join(samples))
    return {
        "status": "partial" if checks else ("success" if counts else "no-op"),
        "read": bases + ["EXPERIMENT_STATE.json", "RESEARCH_LOG.md"],
        "actions": ["Scanned bounded experiment directories for DONE/ERROR markers.", "Did not launch experiments."],
        "checks": checks or ["No experiment errors found in bounded scan."],
        "feedback": feedback,
        "memory": ["Wrote this loop run report.", "Updated .agent/loops/state.json."],
        "next": [
            "Create a task-specific relaunch list before starting new runs." if checks else
            "Use task-specific analysis scripts for deeper experiment decisions."
        ],
    }


def _loop_contract_only(root: Path, loop_id: str, missing: list[str]) -> dict:
    return {
        "status": "failed" if missing else "no-op",
        "read": [str(_loop_path(root, loop_id).relative_to(root))],
        "actions": ["Validated loop contract only; no built-in analyzer is registered for this loop."],
        "checks": [f"missing required sections: {', '.join(missing)}"] if missing else ["Loop contract contains all required sections."],
        "feedback": ["Register a built-in analyzer or execute this loop through an agent-specific adapter."],
        "memory": ["Wrote this loop run report.", "Updated .agent/loops/state.json."],
        "next": ["Stop; no automatic next cycle is available for custom loops."],
    }


def _cap_output(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit] + f"... (output capped at {limit} chars)"


_LOOP_COMMAND_GATE_WRAPPER = r"""
import pathlib
import subprocess
import sys
import time

gate = pathlib.Path(sys.argv[1])
token = sys.argv[2]
command = sys.argv[3]
deadline = time.monotonic() + float(sys.argv[4])
while time.monotonic() < deadline:
    try:
        released = gate.read_text(encoding="utf-8") == token
    except (FileNotFoundError, OSError, UnicodeError):
        released = False
    if released:
        try:
            gate.unlink()
        except FileNotFoundError:
            pass
        completed = subprocess.run(
            command,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        sys.stdout.buffer.write(completed.stdout or b"")
        sys.stderr.buffer.write(completed.stderr or b"")
        raise SystemExit(completed.returncode)
    time.sleep(0.02)
raise SystemExit(125)
"""


def _terminate_loop_process(proc: subprocess.Popen) -> None:
    if os.name == "posix":
        try:
            # The session leader may have exited while descendants still hold pipes.
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            if proc.poll() is None:
                try:
                    proc.kill()
                except OSError:
                    pass
        return
    if _close_windows_job(proc):
        return
    try:
        # taskkill can retain tree information briefly after the leader exits.
        subprocess.run(
            ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )
        if proc.poll() is None:
            proc.kill()
    except (OSError, subprocess.SubprocessError):
        try:
            if proc.poll() is None:
                proc.kill()
        except OSError:
            pass


def _attach_windows_kill_job(proc: subprocess.Popen) -> None:
    """Attach a Windows process to a job whose close terminates descendants."""
    if os.name != "nt":
        return
    import ctypes
    from ctypes import wintypes

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", BASIC_LIMIT_INFORMATION),
            ("IoInfo", IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
    ]
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        raise OSError(ctypes.get_last_error(), "CreateJobObjectW failed")
    info = EXTENDED_LIMIT_INFORMATION()
    info.BasicLimitInformation.LimitFlags = 0x00002000
    configured = kernel32.SetInformationJobObject(
        job, 9, ctypes.byref(info), ctypes.sizeof(info),
    )
    assigned = configured and kernel32.AssignProcessToJobObject(job, int(proc._handle))
    if not assigned:
        error = ctypes.get_last_error()
        kernel32.CloseHandle(job)
        raise OSError(error, "unable to assign dispatch process to Windows job")
    proc._agentctl_windows_job = job


def _close_windows_job(proc: subprocess.Popen) -> bool:
    job = getattr(proc, "_agentctl_windows_job", None)
    if not job:
        return False
    proc._agentctl_windows_job = None
    try:
        import ctypes
        from ctypes import wintypes
        close_handle = ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle
        close_handle.argtypes = [wintypes.HANDLE]
        close_handle.restype = wintypes.BOOL
        close_handle(job)
    except (AttributeError, OSError):
        return False
    return True


def _loop_command_scope_violation(root: Path, cmd: str) -> str | None:
    """Return a reason string if a loop command would write outside scope.

    Loop Check commands are ordinary shell writes and must obey the same
    session scope as any other mutation. Reuse the hook's shell classifier:
    read-only commands are fine; path-writing commands must land inside the
    active task's effective scope; commands whose paths cannot be enumerated
    (opaque) are refused when the checkout is shared with a live peer.
    """
    try:
        import importlib.util
        hook_path = root / "tools" / "agent_workflow_hook.py"
        spec = importlib.util.spec_from_file_location("_awk_hook_for_loop", hook_path)
        hook = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(hook)
    except Exception:  # noqa: BLE001 - if the bridge is unavailable, fail closed
        return "unable to load the command classifier to validate loop scope"
    verdict = hook.classify_shell_command(cmd)
    if verdict == "read_only":
        return None
    st = _load_session(root)
    # Consistent with opaque-command and contamination gating: a session alone
    # in its checkout may write anywhere (worktree-per-loop layout stays free);
    # scope is enforced only when a live peer shares the checkout.
    blockers = _blocking_session_rows(root, st.get("workflow_session_key"))
    if not blockers:
        return None
    scope = _session_effective_scope(st)
    if verdict == "opaque":
        return (f"loop command '{cmd}' cannot prove its write paths and a "
                "peer session shares this checkout; run it in a task worktree")
    paths = hook.shell_write_paths(cmd, root)
    for raw in paths:
        rel, error = _normalize_claim_path(root, raw)
        if error:
            return f"loop command '{cmd}' targets {raw}: {error}"
        if rel is None:
            continue
        if _controller_owned_claim_error(rel):
            return f"loop command '{cmd}' writes controller-owned {rel}"
        if not _path_in_scope(rel, scope):
            return (f"loop command '{cmd}' writes {rel} outside the active task "
                    f"scope {st.get('scope') or []}")
    return None


def _loop_run_commands(root: Path, loop_id: str, spec: dict) -> dict:
    """Execute the commands a custom loop contract declares; exit codes decide status."""
    checks = []
    feedback = []
    failed = 0
    stopped_early = False
    for cmd in spec["commands"]:
        scope_violation = _loop_command_scope_violation(root, cmd)
        if scope_violation:
            return {
                "status": "failed",
                "read": [str(_loop_path(root, loop_id).relative_to(root))],
                "actions": ["Refused a loop command that would write outside the task scope."],
                "checks": [scope_violation],
                "feedback": ["Bound the loop's Check command to the task's write scope, "
                             "or run it from a task worktree."],
                "memory": ["No command was launched."],
                "next": ["Fix the loop command scope, then re-run."],
            }
        runtime = _cycle_runtime(root, normalize=False)
        execution_lease = _loop_execution_lease(root, normalize=False)
        execution_token = (
            str(execution_lease.get("token"))
            if execution_lease
            and execution_lease.get("status") == "running"
            and execution_lease.get("owner_pid") == os.getpid()
            and _same_process(
                execution_lease.get("owner_pid"), execution_lease.get("owner_birth_marker"))
            else ""
        )
        if not execution_token:
            return {
                "status": "failed",
                "read": [str(_loop_path(root, loop_id).relative_to(root))],
                "actions": ["Refused to start a declared command without an execution lease."],
                "checks": ["missing current-process loop execution lease"],
                "feedback": ["Re-enter through agentctl loop run, auto, or cycle."],
                "memory": ["No command was launched."],
                "next": ["Stop; repair loop runtime ownership before retrying."],
            }
        tracking = bool(
            runtime
            and runtime.get("status") in {"running", "stop_requested"}
            and runtime.get("owner_pid") == os.getpid()
            and _same_process(runtime.get("owner_pid"), runtime.get("owner_birth_marker"))
            and runtime.get("inflight_cycle")
        )
        runtime_id = str(runtime.get("id")) if tracking else ""
        token = hashlib.sha256(
            f"{os.getpid()}:{time.time_ns()}:{loop_id}:{cmd}".encode("utf-8")
        ).hexdigest()[:20]
        launch_deadline = time.time() + LOOP_COMMAND_LAUNCH_TIMEOUT
        if tracking and runtime.get("status") == "stop_requested":
            checks.append(f"$ {cmd} -> skipped because a cooperative stop was requested")
            stopped_early = True
            break
        command_record = {
            "token": token,
            "loop": loop_id,
            "command_sha256": hashlib.sha256(cmd.encode("utf-8")).hexdigest()[:16],
            "pid": None,
            "birth_marker": None,
            "process_group": None,
            "host": _cycle_host_id(),
            "started_at": _now(),
            "launch_state": "pending",
            "launch_deadline_epoch": launch_deadline,
        }

        def mark_execution_pending(lease: dict) -> None:
            lease["active_command"] = dict(command_record)

        if not _loop_execution_command_update(root, execution_token, mark_execution_pending):
            raise RuntimeError("loop execution lease changed before command launch")

        if tracking:
            def mark_pending(current: dict) -> None:
                current["active_command"] = dict(command_record)

            claimed = _cycle_runtime_update(
                root,
                runtime_id,
                mark_pending,
                expected_statuses={"running"},
                expected_owner_pid=os.getpid(),
            )
            if not claimed:
                checks.append(f"$ {cmd} -> skipped because runtime ownership changed")
                stopped_early = True
                break

        proc = None
        gate_path = root / WORKFLOW_DIR / "tmp" / "loop-launch" / f"{token}.gate"
        try:
            popen_args = {
                "cwd": root,
                "stdout": subprocess.PIPE,
                "stderr": subprocess.PIPE,
                "text": True,
            }
            if os.name == "posix":
                popen_args["start_new_session"] = True
            elif os.name == "nt":
                popen_args["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            proc = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    _LOOP_COMMAND_GATE_WRAPPER,
                    str(gate_path),
                    token,
                    cmd,
                    str(LOOP_COMMAND_LAUNCH_TIMEOUT),
                ],
                **popen_args,
            )
            child_birth_marker = _process_birth_marker(proc.pid)
            if tracking:
                def mark_pid(current: dict) -> None:
                    active = current.get("active_command") or {}
                    if active.get("token") == token:
                        active["pid"] = proc.pid
                        active["birth_marker"] = child_birth_marker
                        active["process_group"] = proc.pid if os.name == "posix" else None
                        active["launch_state"] = "armed"

                recorded = _cycle_runtime_update(
                    root,
                    runtime_id,
                    mark_pid,
                    expected_statuses={"running", "stop_requested"},
                    expected_owner_pid=os.getpid(),
                )
                if not recorded:
                    raise RuntimeError("runtime ownership changed before command launch was recorded")

            def mark_execution_pid(lease: dict) -> None:
                active = lease.get("active_command") or {}
                if active.get("token") == token:
                    active["pid"] = proc.pid
                    active["birth_marker"] = child_birth_marker
                    active["process_group"] = proc.pid if os.name == "posix" else None
                    active["launch_state"] = "armed"

            if not _loop_execution_command_update(root, execution_token, mark_execution_pid):
                raise RuntimeError("loop execution lease changed before child identity was recorded")
            _write(gate_path, token)
            stdout, stderr = proc.communicate(timeout=spec["timeout"])
            code = proc.returncode
            output = (stdout or "") + (stderr or "")
        except subprocess.TimeoutExpired as exc:
            _terminate_loop_process(proc)
            stdout, stderr = proc.communicate()
            code = None
            output = ((exc.stdout or "") if isinstance(exc.stdout, str) else "") + \
                     ((exc.stderr or "") if isinstance(exc.stderr, str) else "") + \
                     (stdout or "") + (stderr or "")
        except BaseException:
            if proc is not None:
                _terminate_loop_process(proc)
                try:
                    proc.communicate(timeout=5)
                except Exception:
                    pass
            raise
        finally:
            try:
                gate_path.unlink()
            except FileNotFoundError:
                pass

        def clear_execution_active(lease: dict) -> None:
            active = lease.get("active_command") or {}
            if active.get("token") == token:
                lease["active_command"] = None

        if not _loop_execution_command_update(root, execution_token, clear_execution_active):
            raise RuntimeError("loop execution lease changed before command result was recorded")
        if tracking:
            def clear_active(current: dict) -> None:
                active = current.get("active_command") or {}
                if active.get("token") == token:
                    current["active_command"] = None

            _cycle_runtime_update(
                root,
                runtime_id,
                clear_active,
                expected_statuses={"running", "stop_requested"},
                expected_owner_pid=os.getpid(),
            )
        if code == 0:
            checks.append(f"$ {cmd} -> exit 0")
            continue
        failed += 1
        if code is None:
            checks.append(f"$ {cmd} -> timeout after {spec['timeout']}s")
        else:
            checks.append(f"$ {cmd} -> exit {code}")
        capped = _cap_output(output, spec["max_output"])
        if capped:
            feedback.append(f"output of failing '$ {cmd}': {capped}")
    status = "failed" if failed else ("no-op" if stopped_early else "success")
    loop_rel = str(_loop_path(root, loop_id).relative_to(root))
    return {
        "status": status,
        "read": [loop_rel],
        "actions": [f"Executed {len(spec['commands'])} declared check command(s) from the loop contract "
                    f"(timeout {spec['timeout']}s, output cap {spec['max_output']} chars)."],
        "checks": checks,
        "feedback": feedback or (["Execution stopped before the next command."] if stopped_early
                                  else ["All declared commands passed."]),
        "memory": ["Wrote this loop run report.", "Updated .agent/loops/state.json."],
        "next": ["No action needed before the next cycle." if not failed else
                 f"Fix the {failed} failing command(s), then re-run 'agentctl loop run {loop_id} --once'."],
    }


def _attach_previous_run(root: Path, loop_id: str, result: dict) -> dict:
    """Feed the prior run's outcome into this run (the Feedback link)."""
    prev = (_load_json(_loop_state_path(root), {}).get("loops") or {}).get(loop_id) or {}
    if not prev.get("last_run_at"):
        return result
    result["previous"] = {
        "run_at": prev.get("last_run_at"),
        "status": prev.get("last_status"),
        "report": prev.get("last_report"),
    }
    prev_status = prev.get("last_status")
    cur_status = result.get("status")
    feedback = result.setdefault("feedback", [])
    if prev_status in {"partial", "failed", "blocked"} and cur_status == "success":
        feedback.append(f"previous run ({prev.get('last_run_at')}) was {prev_status}; its issues are resolved in this run.")
    elif prev_status in {"partial", "failed", "blocked"} and cur_status in {"partial", "failed", "blocked"}:
        feedback.append(f"issues persist since previous run ({prev.get('last_run_at')}, {prev_status}); see {prev.get('last_report')}.")
    elif prev_status == "success" and cur_status in {"partial", "failed", "blocked"}:
        feedback.append(f"regression since previous successful run ({prev.get('last_run_at')}).")
    return result


def _run_loop_once(root: Path, loop_id: str, missing: list[str]) -> dict:
    builtins = {
        "daily-plan-triage": _loop_daily_plan_triage,
        "doc-hygiene": _loop_doc_hygiene,
        "experiment-monitor": _loop_experiment_monitor,
    }
    fn = builtins.get(loop_id)
    if missing:
        result = _loop_contract_only(root, loop_id, missing)
    elif fn:
        result = fn(root)
    else:
        spec, errors = _loop_command_spec(_read(_loop_path(root, loop_id)))
        if errors:
            result = _loop_contract_only(root, loop_id, [])
            result["status"] = "failed"
            result["checks"] = [f"invalid {LOOP_COMMAND_FENCE} block: {e}" for e in errors]
            result["next"] = [f"Fix the {LOOP_COMMAND_FENCE} block in the loop contract, then re-run."]
        elif spec:
            result = _loop_run_commands(root, loop_id, spec)
        else:
            result = _loop_contract_only(root, loop_id, missing)
    return _attach_previous_run(root, loop_id, result)


def _loop_report_nonce(root: Path) -> str:
    """Short tag that differs between checkouts, including same-path checkouts on other hosts.

    Run reports are committed to Git and named by the second; two machines
    running the same loop in the same second used to produce two different
    files with one name, an add/add conflict no merge driver can resolve.
    """
    digest = hashlib.sha256(f"{platform.node()}\n{root.resolve()}".encode("utf-8")).hexdigest()
    return digest[:6]


def _write_loop_report(root: Path, loop_id: str, trigger: str, result: dict) -> Path:
    ts = _dt.datetime.now()
    stamp = ts.strftime("%Y%m%d-%H%M%S")
    base = _loop_runs_dir(root) / f"{stamp}-{loop_id}-{_loop_report_nonce(root)}"
    path = base.with_suffix(".md")
    suffix = 2
    while path.exists():
        path = Path(f"{base}-{suffix}.md")
        suffix += 1
    session = _load_session(root)
    prev = result.get("previous") or {}
    previous_line = (
        f"- Previous: {prev.get('status')} at {prev.get('run_at')} ({prev.get('report')})"
        if prev else "- Previous: none recorded"
    )
    lines = [
        "# Loop Run",
        "",
        f"- Loop: {loop_id}",
        f"- Trigger: {trigger}",
        f"- Agent: {session.get('agent') or '-'}",
        f"- Task: {session.get('task') or '-'}",
        f"- Started: {ts.strftime('%Y-%m-%d %H:%M:%S')}",
        f"- Finished: {ts.strftime('%Y-%m-%d %H:%M:%S')}",
        f"- Status: {result.get('status', 'unknown')}",
        previous_line,
        "",
        "## Read",
        _format_bullets(result.get("read") or []),
        "",
        "## Actions",
        _format_bullets(result.get("actions") or []),
        "",
        "## Checks",
        _format_bullets(result.get("checks") or []),
        "",
        "## Feedback",
        _format_bullets(result.get("feedback") or []),
        "",
        "## Memory Updates",
        _format_bullets(result.get("memory") or []),
        "",
        "## Next",
        _format_bullets(result.get("next") or []),
        "",
    ]
    _write(path, "\n".join(lines))
    def update(state: dict) -> None:
        state.setdefault("loops", {})[loop_id] = {
            "last_run_at": ts.strftime("%Y-%m-%d %H:%M:%S"),
            "last_status": result.get("status", "unknown"),
            "last_report": str(path.relative_to(root)),
        }
    _update_loop_state(root, update)
    return path


def _loop_list(root: Path, args: argparse.Namespace) -> int:
    rows = [_loop_summary_line(root, p) for p in _loop_files(root)]
    if args.json:
        print(json.dumps(rows, indent=2, ensure_ascii=False))
        return 0
    if not rows:
        print("agentctl: no loops found in .agent/loops")
        return 0
    for row in rows:
        state = "ok" if row["ok"] else f"missing:{','.join(row['missing'])}"
        print(f"{row['id']}\t{state}\t{row['trigger']}")
    return 0


def _loop_show(root: Path, args: argparse.Namespace) -> int:
    path, text, missing = _loop_contract(root, args.id)
    if not text:
        print(f"agentctl: loop not found: {args.id}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps({
            "id": args.id,
            "path": str(path.relative_to(root)),
            "ok": not missing,
            "missing": missing,
            "sections": {section: _extract_section(text, f"## {section}") for section in LOOP_REQUIRED_SECTIONS},
        }, indent=2, ensure_ascii=False))
    else:
        print(text)
    return 0


def _loop_run_unlocked(root: Path, args: argparse.Namespace) -> int:
    if not args.once:
        print("agentctl: loop run currently requires --once; scheduled loops are intentionally not enabled yet.", file=sys.stderr)
        return 2
    path, text, missing = _loop_contract(root, args.id)
    if not text:
        print(f"agentctl: loop not found: {args.id}", file=sys.stderr)
        return 2
    result = _run_loop_once(root, args.id, missing)
    report = _write_loop_report(root, args.id, args.trigger or "manual", result)
    print(f"agentctl: loop {args.id} -> {result.get('status')} ({report.relative_to(root)})")
    if missing:
        print(f"agentctl: loop contract missing sections: {', '.join(missing)}")
        return 1
    return 0 if result.get("status") not in {"failed", "blocked"} else 1


def _loop_run(root: Path, args: argparse.Namespace) -> int:
    token, error = _loop_execution_claim(root, f"loop run {args.id}")
    if error:
        print(f"agentctl: {error}", file=sys.stderr)
        return 2
    result = _loop_run_unlocked(root, args)
    _loop_execution_release(root, token)
    return result


def _checkpoint_status(statuses: list[str]) -> str:
    if not statuses:
        return "no-op"
    if any(s in {"failed", "blocked"} for s in statuses):
        return "failed"
    if any(s == "partial" for s in statuses):
        return "partial"
    if any(s == "no-op" for s in statuses):
        return "no-op"
    return "success"


def _run_loop_checkpoint_unlocked(root: Path, checkpoint: str, *, once: bool, trigger: str,
                                  strict: bool | None = None, force: bool = False,
                                  quiet: bool = False) -> int:
    if not once:
        print("agentctl: loop auto currently requires --once; scheduled loops are not enabled.", file=sys.stderr)
        return 2
    policy = _load_loop_checkpoints(root)
    spec = (policy.get("checkpoints") or {}).get(checkpoint)
    if not isinstance(spec, dict):
        print(f"agentctl: unknown loop checkpoint: {checkpoint}", file=sys.stderr)
        return 2
    loop_ids = spec.get("loops") or []
    if isinstance(loop_ids, str):
        loop_ids = [loop_ids]
    loop_ids = [str(x).strip() for x in loop_ids if str(x).strip()]
    strict_effective = bool(spec.get("strict")) if strict is None else bool(strict)
    debounce = int(spec.get("debounce_minutes") or 0)
    input_hash = _checkpoint_input_fingerprint(root)
    state = _load_json(_loop_state_path(root), {"version": 1, "loops": {}, "checkpoints": {}})
    if _checkpoint_recent(state, checkpoint, debounce, input_hash) and not force:
        if not quiet:
            print(f"agentctl: loop checkpoint {checkpoint} skipped (debounced {debounce}m)")
        return 0
    reports = []
    statuses = []
    for loop_id in loop_ids:
        path, text, missing = _loop_contract(root, loop_id)
        if not text:
            print(f"agentctl: loop not found for checkpoint {checkpoint}: {loop_id}", file=sys.stderr)
            statuses.append("failed")
            continue
        result = _run_loop_once(root, loop_id, missing)
        report = _write_loop_report(root, loop_id, f"checkpoint:{checkpoint}:{trigger}", result)
        statuses.append(str(result.get("status", "unknown")))
        reports.append(str(report.relative_to(root)))
        if not quiet:
            print(f"agentctl: loop {loop_id} -> {result.get('status')} ({report.relative_to(root)})")
            if missing:
                print(f"agentctl: loop contract missing sections: {', '.join(missing)}")
    aggregate = _checkpoint_status(statuses)
    failing = aggregate in {"failed", "blocked"} or (strict_effective and aggregate == "partial")
    follow_up_id = None
    follow_up_created = False
    follow_up_escalated = False
    closed_follow_ups: list[str] = []
    if failing:
        try:
            escalate_after = int(spec.get("escalate_after") or LOOP_ESCALATE_AFTER_DEFAULT)
        except (TypeError, ValueError):
            escalate_after = LOOP_ESCALATE_AFTER_DEFAULT
        follow_up_id, follow_up_created, follow_up_escalated = _create_loop_follow_up(
            root, checkpoint, aggregate, reports, strict_effective, escalate_after)
    elif aggregate == "success":
        closed_follow_ups = _close_loop_follow_ups(
            root, checkpoint, f"checkpoint {checkpoint} succeeded (trigger={trigger})")
    def update(state: dict) -> None:
        state.setdefault("checkpoints", {})[checkpoint] = {
            "last_run_at": _now(),
            "last_status": aggregate,
            "last_reports": reports,
            "last_input_hash": input_hash,
            "strict": strict_effective,
            "trigger": trigger,
            "open_follow_up": follow_up_id if failing else None,
        }
    _update_loop_state(root, update)
    if not quiet:
        print(f"agentctl: checkpoint {checkpoint} -> {aggregate}")
        if follow_up_id:
            verb = "created" if follow_up_created else "updated"
            print(f"agentctl: follow-up packet {verb}: {follow_up_id} (see agentctl handoff show {follow_up_id})")
        if follow_up_escalated:
            print(f"agentctl: follow-up packet ESCALATED: {follow_up_id} — repeated failures need a human decision "
                  f"(finish is blocked for the target task until acknowledged).", file=sys.stderr)
        for pid in closed_follow_ups:
            print(f"agentctl: follow-up packet auto-closed: {pid}")
    if aggregate in {"failed", "blocked"}:
        return 1
    if strict_effective and aggregate == "partial":
        print(f"agentctl: checkpoint {checkpoint} is strict and reported partial results.", file=sys.stderr)
        return 1
    return 0


def _run_loop_checkpoint(root: Path, checkpoint: str, *, once: bool, trigger: str,
                         strict: bool | None = None, force: bool = False,
                         quiet: bool = False,
                         cycle_runtime_id: str | None = None) -> int:
    token, error = _loop_execution_claim(
        root, f"checkpoint {checkpoint}", cycle_runtime_id=cycle_runtime_id)
    if error:
        if not quiet:
            print(f"agentctl: {error}", file=sys.stderr)
        return 2
    result = _run_loop_checkpoint_unlocked(
        root,
        checkpoint,
        once=once,
        trigger=trigger,
        strict=strict,
        force=force,
        quiet=quiet,
    )
    _loop_execution_release(root, token)
    return result


def _loop_auto(root: Path, args: argparse.Namespace) -> int:
    return _run_loop_checkpoint(
        root,
        args.checkpoint,
        once=args.once,
        trigger=args.trigger or "manual",
        strict=True if args.strict else None,
        force=args.force,
        quiet=False,
    )


def _cycle_event(runtime: dict, event: str, **details) -> None:
    item = {"at": _now(), "event": event}
    item.update({key: value for key, value in details.items() if value is not None})
    events = runtime.setdefault("events", [])
    events.append(item)
    if len(events) > LOOP_CYCLE_EVENT_LIMIT:
        del events[:-LOOP_CYCLE_EVENT_LIMIT]


def _cycle_runtime_update(root: Path, runtime_id: str, updater, *,
                          expected_statuses: set[str] | None = None,
                          expected_owner_pid=_CYCLE_ANY_OWNER) -> dict | None:
    """Compare and update one runtime under the state lock."""
    found = {"runtime": None}

    def update(state: dict) -> None:
        runtime = state.get("cycle_runtime")
        if not isinstance(runtime, dict) or runtime.get("id") != runtime_id:
            return
        if expected_statuses is not None and runtime.get("status") not in expected_statuses:
            return
        if expected_owner_pid is not _CYCLE_ANY_OWNER and runtime.get("owner_pid") != expected_owner_pid:
            return
        updater(runtime)
        runtime["updated_at"] = _now()
        found["runtime"] = dict(runtime)

    _update_loop_state(root, update)
    return found["runtime"]


def _cycle_host_id() -> str:
    node = platform.node()
    return hashlib.sha256(node.encode("utf-8")).hexdigest()[:16] if node else ""


def _cycle_owner_alive(runtime: dict) -> bool:
    owner_host = runtime.get("owner_host") or ""
    if owner_host and owner_host != _cycle_host_id():
        return True
    return _same_process(runtime.get("owner_pid"), runtime.get("owner_birth_marker"))


def _posix_process_group_exists(process_group) -> bool:
    if os.name != "posix":
        return False
    try:
        process_group = int(process_group)
        if process_group <= 0:
            return False
        os.killpg(process_group, 0)
        return True
    except PermissionError:
        return True
    except ProcessLookupError:
        return False
    except OSError as exc:
        return exc.errno == errno.EPERM
    except (OverflowError, ValueError, TypeError):
        return False


def _active_command_state(active) -> str:
    """Return dead, live, or unverifiable for a persisted command identity."""
    if not isinstance(active, dict):
        return "dead"
    command_host = active.get("host") or ""
    if command_host and command_host != _cycle_host_id():
        return "live"
    if active.get("launch_state") == "pending" and not active.get("pid"):
        try:
            deadline = float(active.get("launch_deadline_epoch"))
        except (TypeError, ValueError):
            return "live"
        return "live" if not math.isfinite(deadline) or time.time() <= deadline + 1.0 else "dead"
    pid = active.get("pid")
    pid_alive = _pid_alive(pid)
    expected_birth = active.get("birth_marker")
    if pid_alive and expected_birth:
        current_birth = _process_birth_marker(pid)
        if current_birth is None:
            return "live"
        if current_birth != expected_birth:
            return ("unverifiable" if _posix_process_group_exists(
                active.get("process_group")) else "dead")
        return "live"
    if pid_alive:
        return "live"
    process_group = active.get("process_group")
    if _posix_process_group_exists(process_group):
        # Once the recorded leader is gone, a bare PGID cannot distinguish its
        # descendants from an unrelated group that reused the same numeric ID.
        return "unverifiable" if expected_birth else "live"
    return "dead"


def _active_command_alive(active) -> bool:
    """Conservatively block automatic replay unless a command is known dead."""
    return _active_command_state(active) != "dead"


def _cycle_active_command_alive(runtime: dict) -> bool:
    return _active_command_alive(runtime.get("active_command"))


def _cycle_terminal_status(runtime: dict) -> tuple[str | None, str | None]:
    failures = int(runtime.get("failures") or 0)
    max_failures = int(runtime.get("max_failures") or 1)
    completed = int(runtime.get("completed_cycles") or 0)
    requested = int(runtime.get("requested_cycles") or 0)
    if failures and failures >= max_failures:
        return "failed", f"failure budget exhausted ({failures}/{max_failures})"
    if requested > 0 and completed >= requested:
        return ("completed_with_failures" if failures else "completed"), None
    return None, None


def _cycle_set_terminal(runtime: dict, status: str, reason: str | None = None) -> None:
    runtime["status"] = status
    runtime["stop_reason"] = reason
    runtime["owner_pid"] = None
    runtime["owner_birth_marker"] = None
    runtime["finished_at"] = _now()
    _cycle_event(runtime, status, reason=reason)


def _cycle_recover_orphan(runtime: dict) -> None:
    """Recover a dead owner's persisted terminal predicates before declaring interruption."""
    if runtime.get("status") not in {"running", "stop_requested"} or _cycle_owner_alive(runtime):
        return
    inflight = runtime.get("inflight_cycle")
    if inflight or runtime.get("active_command"):
        cycle = inflight.get("cycle") if isinstance(inflight, dict) else None
        _cycle_set_terminal(
            runtime,
            "interrupted",
            f"owner process exited while cycle {cycle or 'unknown'} was in flight; "
            "command completion is unknown",
        )
        runtime["resume_safe"] = False
        return
    terminal, reason = _cycle_terminal_status(runtime)
    if terminal == "failed":
        _cycle_set_terminal(runtime, terminal, reason)
    elif runtime.get("status") == "stop_requested":
        _cycle_set_terminal(
            runtime, "stopped", runtime.get("stop_reason") or "stop requested; owner process exited")
    elif terminal:
        _cycle_set_terminal(runtime, terminal, reason)
    else:
        _cycle_set_terminal(runtime, "interrupted", "owner process is no longer running")
        runtime["resume_safe"] = True


def _cycle_runtime(root: Path, *, normalize: bool = True) -> dict | None:
    state = _load_json(_loop_state_path(root), {})
    runtime = state.get("cycle_runtime") if isinstance(state, dict) else None
    if not isinstance(runtime, dict):
        return None
    if not normalize or runtime.get("status") not in {"running", "stop_requested"}:
        return runtime
    if _cycle_owner_alive(runtime):
        return runtime

    def mark_orphaned(current: dict) -> None:
        _cycle_recover_orphan(current)

    updated = _cycle_runtime_update(
        root,
        str(runtime.get("id")),
        mark_orphaned,
        expected_statuses={str(runtime.get("status"))},
        expected_owner_pid=runtime.get("owner_pid"),
    )
    return updated or _cycle_runtime(root, normalize=False)


def _execution_owner_alive(record: dict) -> bool:
    owner_host = record.get("owner_host") or ""
    if owner_host and owner_host != _cycle_host_id():
        return True
    return _same_process(record.get("owner_pid"), record.get("owner_birth_marker"))


def _recover_execution_lease(lease: dict) -> None:
    lease.setdefault("status", "running")
    if lease.get("status") == "running" and not _execution_owner_alive(lease):
        lease["status"] = "interrupted"
        lease["owner_pid"] = None
        lease["owner_birth_marker"] = None
        lease["finished_at"] = _now()
        lease["stop_reason"] = "one-shot loop owner exited before its result was committed"


def _loop_execution_lease(root: Path, *, normalize: bool = True) -> dict | None:
    state = _load_json(_loop_state_path(root), {})
    lease = state.get("execution_lease") if isinstance(state, dict) else None
    if not isinstance(lease, dict):
        return None
    if not normalize or lease.get("status", "running") != "running" or _execution_owner_alive(lease):
        return lease
    token = lease.get("token")
    found = {"lease": None}

    def recover(state: dict) -> None:
        current = state.get("execution_lease")
        if not isinstance(current, dict) or current.get("token") != token:
            return
        _recover_execution_lease(current)
        found["lease"] = dict(current)

    _update_loop_state(root, recover)
    return found["lease"] or _loop_execution_lease(root, normalize=False)


def _loop_execution_claim(root: Path, operation: str, *,
                          cycle_runtime_id: str | None = None) -> tuple[str | None, str | None]:
    """Serialize one-shot loop execution with durable cycle ownership."""
    session = _load_session(root)
    token = hashlib.sha256(
        f"{os.getpid()}:{time.time_ns()}:{operation}".encode("utf-8")
    ).hexdigest()[:20]
    blocked = {"message": None}

    def claim(state: dict) -> None:
        runtime = state.get("cycle_runtime")
        if isinstance(runtime, dict) and runtime.get("status") in {"running", "stop_requested"}:
            _cycle_recover_orphan(runtime)
        if isinstance(runtime, dict) and runtime.get("status") in {
            "running", "stop_requested", "interrupted"
        }:
            same_owner = (
                cycle_runtime_id
                and runtime.get("id") == cycle_runtime_id
                and runtime.get("owner_pid") == os.getpid()
                and _same_process(runtime.get("owner_pid"), runtime.get("owner_birth_marker"))
                and (not runtime.get("owner_host") or runtime.get("owner_host") == _cycle_host_id())
            )
            if not same_owner:
                blocked["message"] = (
                    f"loop runtime {runtime.get('id')} is {runtime.get('status')}; "
                    "non-owner loop execution is blocked until it is completed, resumed, or stopped"
                )
                return

        lease = state.get("execution_lease")
        if isinstance(lease, dict):
            _recover_execution_lease(lease)
        if isinstance(lease, dict) and lease.get("status") in {"running", "interrupted"}:
            blocked["message"] = (
                f"loop execution lease for {lease.get('operation') or 'another command'} is "
                f"{lease.get('status')} (pid={lease.get('owner_pid')}); inspect 'loop status' "
                "and reconcile unknown results with 'loop stop --ack-inflight --reason <reason>'"
            )
            return
        state["execution_lease"] = {
            "token": token,
            "operation": operation,
            "status": "running",
            "owner_pid": os.getpid(),
            "owner_birth_marker": _process_birth_marker(os.getpid()),
            "owner_host": _cycle_host_id(),
            "workflow_session_key": (
                session.get("workflow_session_key") or _workflow_session_key()
            ),
            "task": session.get("task"),
            "scope": list(session.get("scope") or []),
            "checkout": str(root.resolve()),
            "cycle_runtime_id": cycle_runtime_id,
            "active_command": None,
            "started_at": _now(),
            "finished_at": None,
            "stop_reason": None,
        }

    _update_loop_state(root, claim)
    if blocked["message"]:
        return None, blocked["message"]
    return token, None


def _loop_execution_release(root: Path, token: str | None) -> None:
    if not token:
        return

    def release(state: dict) -> None:
        lease = state.get("execution_lease")
        if isinstance(lease, dict) and lease.get("token") == token:
            state.pop("execution_lease", None)

    _update_loop_state(root, release)


def _loop_execution_command_update(root: Path, token: str, updater) -> dict | None:
    found = {"lease": None}

    def update(state: dict) -> None:
        lease = state.get("execution_lease")
        if not isinstance(lease, dict) or lease.get("token") != token:
            return
        if (lease.get("status") != "running"
                or lease.get("owner_pid") != os.getpid()
                or not _same_process(lease.get("owner_pid"), lease.get("owner_birth_marker"))):
            return
        updater(lease)
        found["lease"] = dict(lease)

    _update_loop_state(root, update)
    return found["lease"]


def _loop_execution_reconcile(root: Path, token: str, reason: str) -> bool:
    cleared = {"value": False}

    def reconcile(state: dict) -> None:
        lease = state.get("execution_lease")
        if not isinstance(lease, dict) or lease.get("token") != token:
            return
        history = state.setdefault("execution_history", [])
        history.append({
            "token": token,
            "operation": lease.get("operation"),
            "status": "reconciled",
            "started_at": lease.get("started_at"),
            "finished_at": _now(),
            "reason": reason,
        })
        if len(history) > LOOP_CYCLE_HISTORY_LIMIT:
            del history[:-LOOP_CYCLE_HISTORY_LIMIT]
        state.pop("execution_lease", None)
        cleared["value"] = True

    _update_loop_state(root, reconcile)
    return cleared["value"]


def _cycle_begin(root: Path, args: argparse.Namespace) -> tuple[dict | None, str | None]:
    cycles = int(args.cycles)
    interval = float(args.interval or 0)
    max_failures = int(args.max_failures or 0)
    if cycles < 1:
        return None, "loop cycle requires --cycles >= 1"
    if cycles > LOOP_CYCLE_MAX:
        return None, f"loop cycle refuses more than {LOOP_CYCLE_MAX} cycles in one command"
    if not math.isfinite(interval):
        return None, "--interval must be finite"
    if interval < 0:
        return None, "--interval must be a non-negative number of seconds"
    if max_failures < 0:
        return None, "--max-failures must be zero or a positive integer"
    if max_failures > cycles:
        return None, "--max-failures cannot exceed --cycles"
    if max_failures > 1 and not args.continue_on_failure:
        return None, "--max-failures greater than 1 requires --continue-on-failure"
    policy = _load_loop_checkpoints(root)
    if args.checkpoint not in (policy.get("checkpoints") or {}):
        return None, f"unknown loop checkpoint: {args.checkpoint}"

    runtime_id = f"cycle-{_dt.datetime.now().strftime('%Y%m%d-%H%M%S')}-{os.getpid()}"
    effective_max_failures = max_failures or (cycles if args.continue_on_failure else 1)
    runtime = {
        "version": 1,
        "id": runtime_id,
        "checkpoint": args.checkpoint,
        "status": "running",
        "requested_cycles": cycles,
        "completed_cycles": 0,
        "failures": 0,
        "first_failure_code": 0,
        "max_failures": effective_max_failures,
        "interval_seconds": interval,
        "trigger": args.trigger or "cycle",
        "strict": bool(args.strict),
        "force": bool(args.force),
        "continue_on_failure": bool(args.continue_on_failure),
        "owner_pid": os.getpid(),
        "owner_birth_marker": _process_birth_marker(os.getpid()),
        "owner_host": _cycle_host_id(),
        "started_at": _now(),
        "updated_at": _now(),
        "finished_at": None,
        "last_cycle_at": None,
        "last_return_code": None,
        "last_reports": [],
        "stop_reason": None,
        "inflight_cycle": None,
        "active_command": None,
        "resume_safe": True,
        "events": [],
    }
    _cycle_event(runtime, "started", checkpoint=args.checkpoint, cycles=cycles)

    blocked = {"message": None}

    def save(state: dict) -> None:
        lease = state.get("execution_lease")
        if isinstance(lease, dict):
            _recover_execution_lease(lease)
        if isinstance(lease, dict) and lease.get("status") in {"running", "interrupted"}:
            blocked["message"] = (
                f"loop execution lease for {lease.get('operation') or 'another command'} is "
                f"{lease.get('status')}; reconcile it before starting a cycle"
            )
            return
        previous = state.get("cycle_runtime")
        if isinstance(previous, dict):
            previous_status = previous.get("status")
            if previous_status in {"running", "stop_requested"}:
                _cycle_recover_orphan(previous)
                previous_status = previous["status"]
            if previous_status in {"running", "stop_requested", "interrupted"}:
                blocked["message"] = (
                    f"unfinished loop runtime {previous.get('id')} is {previous_status}; "
                    "use 'agentctl loop status', 'loop resume', or 'loop stop' before starting another"
                )
                return
            history = state.setdefault("cycle_history", [])
            history.append({
                key: previous.get(key) for key in (
                    "id", "checkpoint", "status", "requested_cycles", "completed_cycles",
                    "failures", "started_at", "finished_at", "stop_reason")
            })
            if len(history) > LOOP_CYCLE_HISTORY_LIMIT:
                del history[:-LOOP_CYCLE_HISTORY_LIMIT]
        state["cycle_runtime"] = runtime

    state = _update_loop_state(root, save)
    if blocked["message"]:
        return None, blocked["message"]
    return state.get("cycle_runtime"), None


def _cycle_finish(root: Path, runtime_id: str, status: str, reason: str | None = None, *,
                  expected_statuses: set[str] | None = None,
                  expected_owner_pid=_CYCLE_ANY_OWNER) -> dict | None:
    def finish(runtime: dict) -> None:
        _cycle_set_terminal(runtime, status, reason)

    return _cycle_runtime_update(
        root,
        runtime_id,
        finish,
        expected_statuses=expected_statuses,
        expected_owner_pid=expected_owner_pid,
    )


def _cycle_escalated(root: Path, checkpoint: str) -> list[str]:
    return [
        str(pkt.get("id")) for _path, pkt in _loop_follow_up_packets(root, checkpoint)
        if pkt.get("escalated")
    ]


def _cycle_sleep(root: Path, runtime_id: str, seconds: float) -> bool:
    """Sleep cooperatively; return True when a stop was requested."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        runtime = _cycle_runtime(root, normalize=False)
        if not runtime or runtime.get("id") != runtime_id:
            return True
        if runtime.get("status") != "running":
            return True
        time.sleep(min(0.5, max(0.0, deadline - time.monotonic())))
    return False


def _cycle_terminal_exit(runtime: dict) -> int:
    status = runtime.get("status")
    cycles = int(runtime.get("requested_cycles") or 0)
    completed = int(runtime.get("completed_cycles") or 0)
    failures = int(runtime.get("failures") or 0)
    failure_code = int(runtime.get("first_failure_code") or 1)
    if status == "completed":
        print(f"agentctl: loop cycle finished successfully ({completed}/{cycles})")
        return 0
    if status == "completed_with_failures":
        print(f"agentctl: loop cycle finished with {failures}/{cycles} failing cycle(s)")
        return failure_code
    if status == "failed":
        if runtime.get("continue_on_failure"):
            print(f"agentctl: loop cycle stopped; {runtime.get('stop_reason')}")
        else:
            print(
                "agentctl: loop cycle stopped after failure; "
                "use --continue-on-failure to keep cycling and accumulate feedback."
            )
        return failure_code
    if status == "blocked":
        print(f"agentctl: loop cycle blocked; {runtime.get('stop_reason')}", file=sys.stderr)
        return failure_code
    if status == "stopped":
        print(f"agentctl: loop runtime {runtime.get('id')} stopped at {completed}/{cycles}")
        return failure_code if failures else 0
    print(f"agentctl: loop runtime {runtime.get('id')} is no longer runnable ({status})", file=sys.stderr)
    return 2


def _cycle_execute(root: Path, runtime_id: str) -> int:
    try:
        while True:
            runtime = _cycle_runtime(root, normalize=False)
            if not runtime or runtime.get("id") != runtime_id:
                print(f"agentctl: loop runtime disappeared or changed: {runtime_id}", file=sys.stderr)
                return 2
            if runtime.get("status") == "stop_requested":
                stopped = _cycle_finish(
                    root,
                    runtime_id,
                    "stopped",
                    runtime.get("stop_reason") or "cooperative stop requested",
                    expected_statuses={"stop_requested"},
                    expected_owner_pid=os.getpid(),
                )
                if not stopped:
                    continue
                return _cycle_terminal_exit(stopped)
            if runtime.get("status") != "running":
                return _cycle_terminal_exit(runtime)

            cycles = int(runtime.get("requested_cycles") or 0)
            completed = int(runtime.get("completed_cycles") or 0)
            if completed >= cycles:
                status, reason = _cycle_terminal_status(runtime)
                finished = _cycle_finish(
                    root,
                    runtime_id,
                    status or "completed",
                    reason,
                    expected_statuses={"running"},
                    expected_owner_pid=os.getpid(),
                )
                if not finished:
                    continue
                return _cycle_terminal_exit(finished)

            index = completed + 1

            def mark_cycle_start(current: dict) -> None:
                current["owner_host"] = _cycle_host_id()
                current["inflight_cycle"] = {"cycle": index, "started_at": _now()}
                current["active_command"] = None
                current["resume_safe"] = False
                _cycle_event(current, "cycle_started", cycle=index)

            claimed = _cycle_runtime_update(
                root,
                runtime_id,
                mark_cycle_start,
                expected_statuses={"running"},
                expected_owner_pid=os.getpid(),
            )
            if not claimed:
                continue
            runtime = claimed
            trigger = f"{runtime.get('trigger') or 'cycle'}:{index}/{cycles}"
            print(f"agentctl: loop cycle {index}/{cycles} checkpoint={runtime.get('checkpoint')}")
            rc = _run_loop_checkpoint(
                root,
                str(runtime.get("checkpoint")),
                once=True,
                trigger=trigger,
                strict=True if runtime.get("strict") else None,
                force=bool(runtime.get("force")),
                quiet=False,
                cycle_runtime_id=runtime_id,
            )
            checkpoint_state = (
                (_load_json(_loop_state_path(root), {}).get("checkpoints") or {})
                .get(runtime.get("checkpoint"), {})
            )
            escalated = _cycle_escalated(root, str(runtime.get("checkpoint")))

            def record_result(current: dict) -> None:
                stop_requested = current.get("status") == "stop_requested"
                current["inflight_cycle"] = None
                current["active_command"] = None
                current["resume_safe"] = True
                current["completed_cycles"] = int(current.get("completed_cycles") or 0) + 1
                current["last_cycle_at"] = _now()
                current["last_return_code"] = rc
                current["last_reports"] = checkpoint_state.get("last_reports") or []
                if rc:
                    current["failures"] = int(current.get("failures") or 0) + 1
                    if not current.get("first_failure_code"):
                        current["first_failure_code"] = rc
                _cycle_event(current, "cycle_finished", cycle=index, return_code=rc)
                if escalated:
                    _cycle_set_terminal(
                        current,
                        "blocked",
                        "escalated follow-up requires a decision: " + ", ".join(escalated),
                    )
                    return
                terminal, reason = _cycle_terminal_status(current)
                if terminal == "failed":
                    _cycle_set_terminal(current, terminal, reason)
                elif stop_requested:
                    _cycle_set_terminal(
                        current, "stopped", current.get("stop_reason") or "cooperative stop requested")
                elif terminal:
                    _cycle_set_terminal(current, terminal, reason)

            runtime = _cycle_runtime_update(
                root,
                runtime_id,
                record_result,
                expected_statuses={"running", "stop_requested"},
                expected_owner_pid=os.getpid(),
            )
            if not runtime:
                current = _cycle_runtime(root, normalize=False)
                if current and current.get("id") == runtime_id:
                    return _cycle_terminal_exit(current)
                print(f"agentctl: loop runtime changed before result persistence: {runtime_id}", file=sys.stderr)
                return 2
            if runtime.get("status") != "running":
                return _cycle_terminal_exit(runtime)
            interval = float(runtime.get("interval_seconds") or 0)
            if interval and _cycle_sleep(root, runtime_id, interval):
                continue
    except KeyboardInterrupt:
        interrupted = _cycle_finish(
            root,
            runtime_id,
            "interrupted",
            "runner interrupted by keyboard signal",
            expected_statuses={"running", "stop_requested"},
            expected_owner_pid=os.getpid(),
        )
        hint = (
            "inspect and reconcile its in-flight result"
            if interrupted and interrupted.get("resume_safe") is False
            else "use 'agentctl loop resume'"
        )
        print(f"agentctl: loop runtime {runtime_id} interrupted; {hint}", file=sys.stderr)
        return 130
    except Exception as exc:
        interrupted = _cycle_finish(
            root,
            runtime_id,
            "interrupted",
            f"runner exception: {type(exc).__name__}: {exc}",
            expected_statuses={"running", "stop_requested"},
            expected_owner_pid=os.getpid(),
        )
        hint = (
            "inspect and reconcile its in-flight result"
            if interrupted and interrupted.get("resume_safe") is False
            else "it can be resumed after inspection"
        )
        print(f"agentctl: loop runtime {runtime_id} interrupted: {exc}; {hint}", file=sys.stderr)
        return 2


def _loop_cycle(root: Path, args: argparse.Namespace) -> int:
    runtime, error = _cycle_begin(root, args)
    if error:
        print(f"agentctl: {error}", file=sys.stderr)
        return 2
    print(f"agentctl: loop runtime started: {runtime.get('id')}")
    return _cycle_execute(root, str(runtime.get("id")))


def _loop_status(root: Path, args: argparse.Namespace) -> int:
    runtime = _cycle_runtime(root)
    execution_lease = _loop_execution_lease(root)
    runtime_status = runtime.get("status") if runtime else None
    runtime_active = runtime_status in {"running", "stop_requested", "interrupted"}
    status = (
        runtime_status
        if runtime_active or not execution_lease
        else f"execution_{execution_lease.get('status')}"
    ) or "idle"
    payload = {
        "status": status,
        "runtime": runtime,
        "execution_lease": execution_lease,
    }
    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0
    if not runtime and not execution_lease:
        print("agentctl: no loop cycle runtime recorded")
        return 0
    if not runtime:
        print(
            f"agentctl: one-shot loop execution {execution_lease.get('operation')} "
            f"status={execution_lease.get('status')} pid={execution_lease.get('owner_pid')}"
        )
        if execution_lease.get("stop_reason"):
            print(f"agentctl: stop reason: {execution_lease.get('stop_reason')}")
        return 0
    print(
        f"agentctl: loop runtime {runtime.get('id')} status={runtime.get('status')} "
        f"checkpoint={runtime.get('checkpoint')} progress="
        f"{runtime.get('completed_cycles', 0)}/{runtime.get('requested_cycles', 0)} "
        f"failures={runtime.get('failures', 0)}"
    )
    if runtime.get("stop_reason"):
        print(f"agentctl: stop reason: {runtime.get('stop_reason')}")
    if runtime.get("status") == "interrupted" and runtime.get("resume_safe") is False:
        active = runtime.get("active_command") or {}
        print(
            "agentctl: resume blocked because an in-flight cycle has an unknown result; "
            f"active command pid={active.get('pid') or 'unknown'}"
        )
    if execution_lease:
        print(
            f"agentctl: one-shot execution {execution_lease.get('operation')} "
            f"status={execution_lease.get('status')} pid={execution_lease.get('owner_pid')}"
        )
    return 0


def _loop_resume(root: Path, _args: argparse.Namespace) -> int:
    runtime = _cycle_runtime(root)
    if not runtime:
        print("agentctl: no loop runtime to resume", file=sys.stderr)
        return 2
    status = runtime.get("status")
    if status in {"running", "stop_requested"}:
        print(f"agentctl: loop runtime {runtime.get('id')} is still {status}", file=sys.stderr)
        return 2
    if status != "interrupted":
        print(f"agentctl: loop runtime {runtime.get('id')} is terminal ({status}); start a new cycle", file=sys.stderr)
        return 2
    if runtime.get("resume_safe") is False:
        print(
            f"agentctl: loop runtime {runtime.get('id')} cannot be resumed because its in-flight "
            "cycle has an unknown result; inspect the active command, wait for it to exit, then "
            "run 'agentctl loop stop --ack-inflight --reason <reconciliation>'",
            file=sys.stderr,
        )
        return 2
    execution_lease = _loop_execution_lease(root)
    if execution_lease:
        print(
            f"agentctl: loop runtime cannot resume while execution lease "
            f"{execution_lease.get('operation')} is {execution_lease.get('status')}; "
            "reconcile it first",
            file=sys.stderr,
        )
        return 2
    if int(runtime.get("completed_cycles") or 0) >= int(runtime.get("requested_cycles") or 0):
        print(f"agentctl: loop runtime {runtime.get('id')} has no remaining cycles", file=sys.stderr)
        return 2

    def resume(current: dict) -> None:
        current["status"] = "running"
        current["owner_pid"] = os.getpid()
        current["owner_birth_marker"] = _process_birth_marker(os.getpid())
        current["owner_host"] = _cycle_host_id()
        current["finished_at"] = None
        current["stop_reason"] = None
        current["inflight_cycle"] = None
        current["active_command"] = None
        current["resume_safe"] = True
        _cycle_event(current, "resumed", next_cycle=int(current.get("completed_cycles") or 0) + 1)

    runtime_id = str(runtime.get("id"))
    runtime = _cycle_runtime_update(
        root,
        runtime_id,
        resume,
        expected_statuses={"interrupted"},
        expected_owner_pid=None,
    )
    if not runtime:
        current = _cycle_runtime(root)
        print(
            f"agentctl: loop runtime {runtime_id} could not be claimed; "
            f"current status is {(current or {}).get('status', 'missing')}",
            file=sys.stderr,
        )
        return 2
    print(f"agentctl: loop runtime resumed: {runtime.get('id')} at cycle "
          f"{int(runtime.get('completed_cycles') or 0) + 1}/{runtime.get('requested_cycles')}")
    return _cycle_execute(root, runtime_id)


def _reconcile_execution_lease(root: Path, lease: dict, args: argparse.Namespace) -> int:
    if lease.get("status") == "running":
        print(
            f"agentctl: one-shot loop execution {lease.get('operation')} is still running; "
            "wait for its owner or command to exit",
            file=sys.stderr,
        )
        return 2
    if lease.get("status") != "interrupted":
        return 0
    command_state = _active_command_state(lease.get("active_command"))
    if command_state == "live":
        print(
            f"agentctl: one-shot loop execution {lease.get('operation')} still has a live "
            "command; wait for it to exit before reconciling",
            file=sys.stderr,
        )
        return 2
    if not args.ack_inflight or not args.reason:
        print(
            "agentctl: one-shot loop result is unknown; after inspecting its side effects, run "
            "'agentctl loop stop --ack-inflight --reason <reconciliation>'",
            file=sys.stderr,
        )
        return 2
    if not _loop_execution_reconcile(root, str(lease.get("token")), args.reason):
        print("agentctl: one-shot execution lease changed before reconciliation", file=sys.stderr)
        return 2
    print(f"agentctl: reconciled interrupted one-shot execution {lease.get('operation')}")
    return 0


def _loop_stop(root: Path, args: argparse.Namespace) -> int:
    runtime = _cycle_runtime(root)
    execution_lease = _loop_execution_lease(root)
    if not runtime:
        if execution_lease:
            return _reconcile_execution_lease(root, execution_lease, args)
        print("agentctl: no loop runtime to stop")
        return 0
    status = runtime.get("status")
    if status not in {"running", "stop_requested", "interrupted"}:
        if execution_lease:
            return _reconcile_execution_lease(root, execution_lease, args)
        print(f"agentctl: loop runtime {runtime.get('id')} already terminal ({status})")
        return 0
    unsafe_inflight = status == "interrupted" and runtime.get("resume_safe") is False
    if unsafe_inflight:
        command_states = [_active_command_state(runtime.get("active_command"))]
        if execution_lease:
            command_states.append(_active_command_state(execution_lease.get("active_command")))
        if "live" in command_states:
            print(
                f"agentctl: loop runtime {runtime.get('id')} still has a live "
                "in-flight command; wait for it to exit on its recorded host before reconciling",
                file=sys.stderr,
            )
            return 2
        if not args.ack_inflight or not args.reason:
            print(
                "agentctl: interrupted cycle result is unknown; after inspecting its side effects, "
                "run 'agentctl loop stop --ack-inflight --reason <reconciliation>'",
                file=sys.stderr,
            )
            return 2
    reason = args.reason or "cooperative stop requested"
    if status in {"running", "stop_requested"} and _cycle_owner_alive(runtime):
        def request_stop(current: dict) -> None:
            current["status"] = "stop_requested"
            current["stop_reason"] = reason
            _cycle_event(current, "stop_requested", reason=reason)

        updated = _cycle_runtime_update(
            root,
            str(runtime.get("id")),
            request_stop,
            expected_statuses={"running", "stop_requested"},
            expected_owner_pid=runtime.get("owner_pid"),
        )
        if not updated:
            current = _cycle_runtime(root)
            print(
                f"agentctl: loop runtime changed before stop could be recorded; "
                f"current status is {(current or {}).get('status', 'missing')}",
                file=sys.stderr,
            )
            return 2
        print(f"agentctl: stop requested for loop runtime {runtime.get('id')}; "
              "the runner will stop after the current check or sleep poll")
        return 0
    stopped = _cycle_finish(
        root,
        str(runtime.get("id")),
        "stopped",
        reason,
        expected_statuses={"interrupted"},
        expected_owner_pid=None,
    )
    if not stopped:
        print(f"agentctl: loop runtime changed before it could be marked stopped", file=sys.stderr)
        return 2
    if execution_lease and execution_lease.get("status") == "interrupted":
        if not _loop_execution_reconcile(
                root, str(execution_lease.get("token")), args.reason or reason):
            print(
                "agentctl: cycle stopped, but its one-shot execution lease still needs reconciliation",
                file=sys.stderr,
            )
            return 2
    print(f"agentctl: loop runtime {runtime.get('id')} marked stopped")
    return 0


def cmd_loop(args: argparse.Namespace) -> int:
    root = _repo_root()
    if args.loop_action == "list":
        return _loop_list(root, args)
    if args.loop_action == "show":
        return _loop_show(root, args)
    if args.loop_action == "run":
        return _loop_run(root, args)
    if args.loop_action == "auto":
        return _loop_auto(root, args)
    if args.loop_action == "cycle":
        return _loop_cycle(root, args)
    if args.loop_action == "status":
        return _loop_status(root, args)
    if args.loop_action == "resume":
        return _loop_resume(root, args)
    if args.loop_action == "stop":
        return _loop_stop(root, args)
    print("agentctl: unknown loop action", file=sys.stderr)
    return 2


def _check_base(root: Path) -> list:
    p = []
    if not (root / "AGENTS.md").is_file():
        p.append("missing AGENTS.md")
    if not (root / WORKFLOW_DIR / PLAN_FILE).is_file():
        p.append(f"missing {WORKFLOW_DIR}/{PLAN_FILE}")
    for path in _loop_files(root):
        row = _loop_summary_line(root, path)
        if not row["ok"]:
            p.append(f"loop {row['id']} missing required sections: {', '.join(row['missing'])}")
        spec, errors = _loop_command_spec(_read(path))
        for err in errors:
            p.append(f"loop {row['id']}: {LOOP_COMMAND_FENCE} block invalid: {err}")
        if spec and row["id"] in LOOP_BUILTIN_IDS:
            p.append(f"loop {row['id']} is built-in; its {LOOP_COMMAND_FENCE} block would be ignored — remove it")
    return p


def _check_escalations(root: Path) -> list:
    return [
        f"escalated loop follow-up {pkt.get('id')} (checkpoint={pkt.get('checkpoint')}, "
        f"occurrences={pkt.get('occurrences', 1)}) needs a human decision"
        for _path, pkt in _escalated_follow_ups(root)
    ]


def _check_pending_guidance(root: Path) -> list:
    st = _load_session(root)
    task = st.get("task")
    if not task:
        return []
    return [
        f"pending supervisor guidance {pkt.get('id')} for active task {task}; "
        f"run 'agentctl guidance ack {pkt.get('id')}' after incorporating it"
        for _path, pkt in _open_guidance_packets(
            root,
            to_agent=st.get("agent"),
            task=task,
            task_specific_only=True,
            session_id=st.get("session_id") or "",
            model=st.get("model") or "",
            reasoning_effort=st.get("reasoning_effort") or "",
        )
    ]


def _check_receipt(root: Path) -> list:
    st = _load_session(root)
    if not st.get("task"):
        return []
    cur = _hash_docs(root, st["task"])
    old = st.get("doc_hashes", {})
    changed = sorted(k for k in set(cur) | set(old) if cur.get(k) != old.get(k))
    if changed:
        return [f"plan/rules/task docs changed since start ({', '.join(changed)}); "
                f"re-read them then run 'agentctl refresh'."]
    return []


def _scan_secrets_staged(root: Path, files: list) -> list:
    leaks = []
    for f in files:
        content = _git(root, "show", f":{f}")
        if not content:
            continue
        for rgx in SECRET_RES:
            if rgx.search(content):
                leaks.append(f"possible secret in staged file {f}")
                break
    return leaks


def _check_git_exclusive(root: Path) -> list[str]:
    current = _workflow_session_key()
    blockers = _blocking_session_rows(root, current)
    if not blockers:
        return []
    owners = ", ".join(
        f"{row.get('workflow_session_key')}:{row.get('task')}"
        for row in blockers
    )
    return [
        "Git commit/push requires exclusive use of this checkout; "
        f"other active or stale sessions: {owners}. Use task worktrees or finish/release them."
    ]


def _check_precommit(root: Path) -> list:
    p = _check_git_exclusive(root)
    st = _load_session(root)
    staged = [f for f in _git(root, "diff", "--cached", "--name-only").splitlines() if f.strip()]
    if staged and not st.get("task"):
        p.append("staged changes but no active task (run agentctl work --agent <name>)")
    if staged:
        agent_docs = [f for f in staged if f.startswith(".agent/") or f == "AGENTS.md"]
        nondoc = [f for f in staged if not (f.startswith(".agent/") or f == "AGENTS.md")]
        if nondoc and not agent_docs:
            p.append("code/data staged but no .agent task/plan/log update staged; run agentctl note.")
        if st.get("task") and nondoc:
            scope = st.get("scope") or []
            outside = sorted(path for path in nondoc if not _path_in_scope(path, scope))
            if not scope:
                p.append(
                    f"active task {st.get('task')} has no bounded write scope for staged code/data"
                )
            elif outside:
                p.append(
                    f"staged paths outside active task {st.get('task')} scope {scope}: "
                    + ", ".join(outside)
                )
    p += _scan_secrets_staged(root, staged)
    return p


def _check_commit_msg(root: Path, msg_file: str | None) -> list:
    if not msg_file:
        return ["commit-msg mode requires --message-file"]
    text = _read(Path(msg_file))
    lines = [l for l in text.splitlines() if not l.startswith("#")]
    subject = next((l for l in lines if l.strip()), "")
    p = []
    if not CONVENTIONAL_RE.match(subject.strip()):
        p.append(f"subject not Conventional Commits: '{subject.strip()}' (want 'type(scope): summary')")
    if not TASK_ID_RE.search("\n".join(lines)):
        p.append("commit message missing task ID (e.g. add 'Refs: T-001')")
    return p


# Ledger *data*: records the controller writes and nothing ever executes.
# Not on the list, deliberately: loop contracts (`loops/*.md`, whose check
# lines run through a shell) and `loops/checkpoints.json` that wires them to
# work-start, rules, evals, the runtime policy, the workflow entry, and the
# install manifest. Those change behavior and travel only with reviewed work.
LEDGER_DATA_PATHS = (
    f"{WORKFLOW_DIR}/board.json",
    f"{WORKFLOW_DIR}/TASKS.md",
    f"{WORKFLOW_DIR}/PROJECT_PLAN.md",
    f"{WORKFLOW_DIR}/agents.json",
    f"{WORKFLOW_DIR}/tasks/",
    f"{WORKFLOW_DIR}/logs/",
    f"{WORKFLOW_DIR}/gates/",
    f"{WORKFLOW_DIR}/loops/state.json",
    f"{WORKFLOW_DIR}/loops/runs/",
    f"{WORKFLOW_DIR}/handoffs/",
    f"{WORKFLOW_DIR}/decisions/",
    f"{WORKFLOW_DIR}/bus/",
    f"{WORKFLOW_DIR}/archive/",
)


def _is_ledger_data_path(path: str) -> bool:
    return any(
        path == entry or (entry.endswith("/") and path.startswith(entry))
        for entry in LEDGER_DATA_PATHS
    )


def _commit_is_ledger_only(root: Path, sha: str) -> bool:
    """True when every path the commit touches is ledger data (see LEDGER_DATA_PATHS)."""
    if not sha:
        return False
    listing = _git(root, "diff-tree", "--no-commit-id", "--name-only", "-r", "--root", sha)
    paths = [line.strip() for line in listing.splitlines() if line.strip()]
    return bool(paths) and all(_is_ledger_data_path(path) for path in paths)


def _refs_trailer_task_ids(body: str) -> list[str]:
    """Task ids named on `Refs:` lines of a commit body, in order, deduplicated."""
    ids: list[str] = []
    for line in body.splitlines():
        match = REFS_LINE_RE.match(line)
        if not match:
            continue
        for tid in TASK_ID_RE.findall(match.group(1)):
            if tid not in ids:
                ids.append(tid)
    return ids


def _check_prepush(
    root: Path,
    commit_range: str | None,
    published_remote: str | None = None,
) -> list:
    if not commit_range:
        return ["pre-push mode requires --commit-range"]
    p = _check_git_exclusive(root)
    rev_args = [commit_range]
    baseline = _load_json(_adoption_path(root), {}).get("ignore_commits_through")
    if baseline and _git(root, "rev-parse", "--verify", f"{baseline}^{{commit}}").strip():
        rev_args.append(f"^{baseline}")
    configured_remotes = set(_git(root, "remote").splitlines())
    if published_remote and published_remote in configured_remotes:
        rev_args.extend(["--not", f"--remotes={published_remote}"])
    log = _git(root, "log", "--format=%H%x1f%s%x1f%b%x1e", *rev_args)
    if not log:
        return p
    tasks = _load_board(root).get("tasks", {})
    seen = set()
    # Ids referenced by commits that change anything outside .agent/. Those
    # must have reached review: unreviewed code never leaves the machine.
    # A commit that only touches the ledger carries no code, and pushing it
    # is exactly how another checkout learns that a task has been claimed.
    code_refs = set()
    for rec in log.split("\x1e"):
        rec = rec.strip()
        if not rec:
            continue
        parts = rec.split("\x1f")
        sha = parts[0].strip()
        subject = parts[1] if len(parts) > 1 else ""
        body = parts[2] if len(parts) > 2 else ""
        if not CONVENTIONAL_RE.match(subject.strip()):
            p.append(f"commit not Conventional: '{subject.strip()}'")
        ids = TASK_ID_RE.findall(subject + " " + body)
        if not ids:
            p.append(f"commit missing task ID: '{subject.strip()}'")
        # With an explicit Refs: trailer only those ids are resolved against
        # the board, so prose such as "SHA-256" in the body is not mistaken
        # for a task reference. Without a trailer every id-shaped token
        # must resolve, as before.
        refs = set(_refs_trailer_task_ids(body) or ids)
        seen.update(refs)
        if not _commit_is_ledger_only(root, sha):
            code_refs.update(refs)
    archived_tasks: dict | None = None
    for tid in sorted(seen):
        t = tasks.get(tid)
        if not t:
            # reconcile archive moves done tasks off the live board, and the
            # commit that does so necessarily references their ids; resolve
            # against the archive before refusing.
            if archived_tasks is None:
                archived = _load_json(
                    root / WORKFLOW_DIR / "archive" / "board.json", {},
                )
                archived_tasks = (
                    archived.get("tasks") if isinstance(archived, dict) else {}
                ) or {}
            archived_entry = archived_tasks.get(tid)
            if isinstance(archived_entry, dict) and archived_entry.get(
                "status"
            ) in PUSHABLE_STATUSES:
                continue
            p.append(
                f"task {tid} referenced in commits but not on the live board "
                "or in .agent/archive/board.json"
            )
            continue
        if tid in code_refs and t.get("status") not in PUSHABLE_STATUSES:
            p.append(
                f"task {tid} is '{t.get('status')}', must be review/approved/done before "
                f"pushing commits that change anything but ledger data under .agent/"
            )
        if t.get("status") == "done":
            rec = _extract_section(_read(root / WORKFLOW_DIR / TASKS_DIR / f"{tid}.md"), "## Completion Record")
            if "Completed-at:" not in rec:
                p.append(f"task {tid} is done but task doc has no completion record")
            if not _plan_checked(root, tid):
                p.append(f"task {tid} is done but not checked off in PROJECT_PLAN.md")
    return p


def _check_board_consistency(root: Path) -> list:
    return _task_reconcile_problems(root)


def _doctor_required_paths() -> list[str]:
    return [
        "AGENTS.md",
        "tools/agentctl.py",
        "tools/agent_workflow_hook.py",
        ".agent/WORKFLOW_ENTRY.md",
        ".agent/PROJECT_PLAN.md",
        ".agent/TASKS.md",
        ".agent/board.json",
        ".agent/loops/checkpoints.json",
        ".agent/evals/suites.json",
        ".agent/rules/github-standards.md",
        ".githooks/pre-commit",
        ".githooks/commit-msg",
        ".githooks/pre-push",
        ".codex/hooks.json",
        ".claude/settings.json",
        ".cursor/hooks.json",
        ".github/workflows/agent-workflow-check.yml",
    ]


def _doctor_managed_install(root: Path) -> tuple[list[str], list[str], list[dict]]:
    problems: list[str] = []
    warnings: list[str] = []
    checks: list[dict] = []
    manifest_path = _install_manifest_path(root)
    manifest = {}
    if not manifest_path.is_file():
        if (root / "templates" / "project").is_dir() and _kit_root() == root:
            checks.append({
                "name": "managed installation", "status": "ok",
                "detail": "kit source checkout (manifest applies to installed targets)",
            })
        else:
            warnings.append(
                "installation manifest is missing (legacy install); reinstall from a current kit checkout"
            )
            checks.append({"name": "managed installation", "status": "warn", "detail": "no install manifest"})
    else:
        manifest = _load_json(manifest_path, {})
        hashes = manifest.get("managed_files") if isinstance(manifest, dict) else None
        if not isinstance(hashes, dict):
            problems.append("installation manifest has invalid managed_files")
            hashes = {}
        changed = []
        for rel, expected in hashes.items():
            path = root / rel
            # Installation manifests hash managed text after Python's universal
            # newline normalization. Doctor must use the same representation so
            # a normal CRLF checkout is not reported as drifted on Windows.
            observed = _sha256_text(_read(path)) if path.is_file() else "missing"
            if observed != expected:
                changed.append(rel)
        if changed:
            problems.append("managed installation files changed outside init: " + ", ".join(changed))
        checks.append({
            "name": "managed installation",
            "status": "fail" if changed else "ok",
            "detail": f"{len(hashes)} managed file(s), {len(changed)} drifted",
        })

    managed_hooks = manifest.get("managed_hooks") if isinstance(manifest, dict) else None
    if not isinstance(managed_hooks, dict):
        template_root = root / "templates" / "project"
        managed_hooks = {}
        for rel in (".codex/hooks.json", ".claude/settings.json", ".cursor/hooks.json"):
            path = template_root / rel
            if path.is_file():
                try:
                    managed_hooks[rel] = json.loads(_read(path))
                except json.JSONDecodeError:
                    pass
    hook_failures = []
    for rel in (".codex/hooks.json", ".claude/settings.json", ".cursor/hooks.json"):
        try:
            data = json.loads(_read(root / rel))
            hooks = data.get("hooks")
            if not isinstance(hooks, dict):
                raise ValueError("hooks is not an object")
            expected = managed_hooks.get(rel)
            expected_hooks = expected.get("hooks") if isinstance(expected, dict) else None
            if not isinstance(expected_hooks, dict):
                raise ValueError("managed hook contract is missing from the installation manifest")
            for event, wanted_rows in expected_hooks.items():
                rows = hooks.get(event)
                if not isinstance(rows, list) or not isinstance(wanted_rows, list):
                    raise ValueError(f"missing managed {event} hook")
                if not all(any(_hook_row_contains(row, wanted) for row in rows) for wanted in wanted_rows):
                    raise ValueError(f"managed {event} hook differs from the shipped contract")
        except (AttributeError, json.JSONDecodeError, ValueError) as exc:
            hook_failures.append(f"{rel}: {exc}")
    if hook_failures:
        problems.append("native hook configuration invalid: " + "; ".join(hook_failures))
    checks.append({
        "name": "native hook configuration",
        "status": "fail" if hook_failures else "ok",
        "detail": "Codex, Claude, and Cursor managed hook entries are present",
    })
    warnings.append(
        "native hooks still depend on client support, repository trust, and user policy; Git hooks and CI are the fallback"
    )
    return problems, warnings, checks


def _migration_installation_report(root: Path) -> dict:
    """Report whether this checkout has a complete, current managed install."""
    problems: list[str] = []
    warnings: list[str] = []
    checks: list[dict] = []

    missing = [rel for rel in _doctor_required_paths() if not (root / rel).exists()]
    if missing:
        problems.append("missing required files: " + ", ".join(missing))
    checks.append({
        "name": "required files",
        "status": "ok" if not missing else "fail",
        "detail": (
            "all required workflow files are present"
            if not missing else ", ".join(missing)
        ),
    })

    if (root / ".git").exists():
        hooks_path = _git(root, "config", "--get", "core.hooksPath")
        if hooks_path != ".githooks":
            problems.append(
                f"git core.hooksPath is '{hooks_path or '<unset>'}', expected '.githooks'"
            )
        checks.append({
            "name": "git hooks",
            "status": "ok" if hooks_path == ".githooks" else "fail",
            "detail": f"core.hooksPath={hooks_path or '<unset>'}",
        })
    else:
        warnings.append("not a Git repository; local Git hooks are not active")
        checks.append({
            "name": "git hooks", "status": "warn", "detail": "not a Git repository",
        })

    install_problems, install_warnings, install_checks = _doctor_managed_install(root)
    problems.extend(install_problems)
    warnings.extend(install_warnings)
    checks.extend(install_checks)

    manifest_path = _install_manifest_path(root)
    source_checkout = (
        (root / "templates" / "project").is_dir() and _kit_root() == root
    )
    manifest_data = _load_json(manifest_path, {}) if manifest_path.is_file() else {}
    if source_checkout and not manifest_path.is_file():
        manifest_state = "source_checkout"
    elif not manifest_path.is_file():
        manifest_state = "legacy_missing"
        problems.append(
            "installation manifest is missing (legacy install); reinstall from a current kit checkout"
        )
    elif not isinstance(manifest_data, dict) or not isinstance(
            manifest_data.get("managed_files"), dict):
        manifest_state = "invalid"
    else:
        manifest_state = "managed"

    return {
        "ok": not problems,
        "manifest": manifest_state,
        "manifest_version": (
            manifest_data.get("version") if isinstance(manifest_data, dict) else None
        ),
        "problems": problems,
        "warnings": warnings,
        "checks": checks,
    }


def _migration_current_record(root: Path) -> tuple[str, dict, str, Path | None, str]:
    """Locate current and legacy session state without triggering a migration write."""
    key = _workflow_session_key()
    identity_error = _workflow_session_identity_error()
    if identity_error:
        source = "untrusted_fork" if _workflow_session_isolation_error() else "untrusted_identity"
        return key, {}, source, None, identity_error

    exact_path = _session_path(root, key)
    exact = _load_json(exact_path, {})
    if isinstance(exact, dict) and exact.get("task"):
        source = "default" if key == "default" else "conversation"
        return key, exact, source, exact_path, ""
    if key == "default":
        return key, {}, "none", None, ""

    shared_path = _legacy_shared_session_path(root, key)
    shared = _load_json(shared_path, {})
    if isinstance(shared, dict) and shared.get("task"):
        legacy_checkout = shared.get("checkout")
        matches = (
            (legacy_checkout and _same_checkout(root, shared))
            or (not legacy_checkout and _session_matches_current_runtime(shared, key))
        )
        if matches:
            return key, shared, "shared_legacy", shared_path, ""

    singleton_path = _session_path(root, "default")
    singleton = _load_json(singleton_path, {})
    if (isinstance(singleton, dict) and singleton.get("task")
            and _session_matches_current_runtime(singleton, key)):
        return key, singleton, "singleton_legacy", singleton_path, ""
    return key, {}, "none", None, ""


def _migration_changed_documents(root: Path, session: dict, source: str) -> list[str]:
    if source != "conversation" or not session.get("task"):
        return []
    current = _hash_docs(root, session.get("task"))
    prior = session.get("doc_hashes") or {}
    return sorted(
        rel for rel in set(current) | set(prior)
        if current.get(rel) != prior.get(rel)
    )


def _migration_session_row(row: dict) -> dict:
    return {
        key: row.get(key)
        for key in (
            "workflow_session_key",
            "parent_session_key",
            "identity_source",
            "observed_status",
            "task",
            "agent",
            "scope",
            "claimed_files",
            "branch",
            "checkout",
            "heartbeat_at",
        )
        if row.get(key) not in (None, "", [])
    }


def _migration_report(root: Path) -> dict:
    installation = _migration_installation_report(root)
    key, session, source, record_path, identity_error = _migration_current_record(root)
    changed_documents = _migration_changed_documents(root, session, source)
    current_status = _session_observed_status(session) if session else "none"

    rows = [row for row in _session_rows_unlocked(root) if _same_checkout(root, row)]
    public_rows = [_migration_session_row(row) for row in rows]
    current_record = str(record_path) if record_path else ""
    peer_stale = [
        row for row in rows
        if row.get("observed_status") == "stale"
        and row.get("identity_source")
        and str(row.get("_record_path") or "") != current_record
    ]
    unknown_peers = [
        row for row in rows
        if row.get("observed_status") in {"active", "stale"}
        and not row.get("identity_source")
        and str(row.get("_record_path") or "") != current_record
    ]

    reasons: list[str] = []
    warnings: list[str] = []
    next_steps: list[str] = []
    if not installation["ok"]:
        action = "repair_install"
        reasons.extend(installation["problems"])
        next_steps.extend([
            "From the latest kit checkout, run agentctl init for this project; "
            "inspect conflicts before using --force-managed.",
            "Run agentctl migrate again after installation succeeds.",
        ])
    elif identity_error:
        action = "restart"
        reasons.append(identity_error)
        next_steps.extend([
            "Close and reopen this agent conversation in the project so the "
            "SessionStart hook establishes an isolated identity.",
            "Run agentctl migrate again before editing.",
        ])
    elif unknown_peers:
        action = "inspect_sessions"
        owners = ", ".join(
            f"{row.get('workflow_session_key')}:{row.get('task')}"
            for row in unknown_peers
        )
        reasons.append(
            "pre-identity session claims require explicit upgrade inspection: " + owners
        )
        next_steps.extend([
            "Inspect each listed task document, working tree, and whether its old "
            "conversation is still running.",
            "After the old conversation is closed, release only its verified claim "
            "with agentctl sessions release <session-key> --reason <verified-reason>.",
            "Run agentctl migrate again before selecting work.",
        ])
    elif source in {"shared_legacy", "singleton_legacy"}:
        action = "refresh"
        reasons.append(f"the current task is stored in a {source.replace('_', ' ')} record")
        next_steps.extend([
            "Run agentctl status --json once to move the matching legacy record "
            "to the per-conversation store.",
            "Re-read the workflow entry, plan, task index, rules, and current task "
            "document; then run agentctl refresh.",
            "Run agentctl migrate again before editing.",
        ])
    elif session.get("task") and not session.get("identity_source"):
        action = "refresh"
        reasons.append("the current pre-upgrade session record lacks identity metadata")
        next_steps.extend([
            "Re-read the workflow entry, plan, task index, rules, and current task "
            "document; then run agentctl refresh to bind the trusted identity source.",
            "Run agentctl migrate again before editing.",
        ])
    elif current_status in {"released", "review", "approved", "done"}:
        action = "continue"
        reasons.append(
            f"the prior conversation record is {current_status}; normal work "
            "selection must run before editing"
        )
        if changed_documents:
            next_steps.append(
                "Re-read every path in current_session.changed_documents before selecting work."
            )
        next_steps.append(
            "Run agentctl work --agent <agent-name> to reclaim an eligible task "
            "or select the next one; do not edit first."
        )
    elif current_status == "stale":
        action = "refresh"
        reasons.append("the current conversation record has a stale heartbeat")
        next_steps.extend([
            "Re-read the workflow entry, plan, task index, rules, and current task "
            "document; then run agentctl refresh.",
            "Run agentctl migrate again before editing.",
        ])
    elif changed_documents:
        action = "refresh"
        reasons.append("project workflow documents changed since this conversation last read them")
        next_steps.extend([
            "Re-read every path in current_session.changed_documents, then run agentctl refresh.",
            "Run agentctl migrate again before editing.",
        ])
    else:
        action = "continue"
        reasons.append("the managed installation and current conversation state are compatible")
        if session.get("task"):
            next_steps.append(
                f"Continue task {session.get('task')}; normal scope, hook, test, "
                "and gate checks remain authoritative."
            )
        else:
            next_steps.append(
                "Read the project workflow documents and run agentctl work --agent "
                "<agent-name> to claim or create a task."
            )

    if peer_stale:
        owners = ", ".join(
            f"{row.get('workflow_session_key')}:{row.get('task')}" for row in peer_stale
        )
        warnings.append(
            "stale session claims remain authoritative for same-task, overlapping-scope, "
            f"and exclusive admission, but do not block migration compatibility: {owners}"
        )
        if action == "continue":
            next_steps.extend([
                "Run agentctl sessions list and inspect each stale task document and working tree; "
                "unrelated work may continue through normal admission.",
                "Release a stale session only after verification, using agentctl sessions "
                "release <session-key> --reason <verified-reason>.",
            ])

    categorized = {
        status: [row for row in public_rows if row.get("observed_status") == status]
        for status in ("active", "stale", "released")
    }
    categorized["other"] = [
        row for row in public_rows
        if row.get("observed_status") not in {"active", "stale", "released"}
    ]
    return {
        "schema_version": 1,
        "root": str(root),
        "ok": action == "continue",
        "action": action,
        "installation": installation,
        "current_session": {
            "key": key,
            "source": source,
            "task": session.get("task") or None,
            "agent": session.get("agent") or None,
            "identity_source": session.get("identity_source") or None,
            "status": current_status,
            "changed_documents": changed_documents,
        },
        "sessions": categorized,
        "reasons": reasons,
        "warnings": warnings,
        "next_steps": next_steps,
    }


def cmd_migrate(args: argparse.Namespace) -> int:
    report = _migration_report(_repo_root())
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print(f"agentctl migrate: {report['action'].upper()}")
        for reason in report["reasons"]:
            print(f"  - {reason}")
        for warning in report["warnings"]:
            print(f"  ! {warning}")
        print("Next:")
        for index, step in enumerate(report["next_steps"], start=1):
            print(f"  {index}. {step}")
    return 0 if report["ok"] else 1


def _machine_wide_lock_findings(root: Path, local_leases: list[dict]) -> list[str]:
    """Report machine-wide locks other checkouts recorded and never freed.

    The per-checkout scan above cannot see them: a project whose
    conversation died with `gpu:0` still blocks every other project on
    the host, while each project's own registry looks clean. Locks held
    by a live lease of this checkout are covered by the local scan.
    """
    findings: list[str] = []
    base = _resource_lock_base_dir()
    if not base.is_dir():
        return findings
    local_resources = {
        str(resource)
        for item in local_leases
        if item.get("kind") == "resource"
        and item.get("status") not in {"released", "release_failed"}
        for resource in (item.get("resources") or [])
    }
    try:
        lock_dirs = sorted(p for p in base.iterdir() if p.is_dir())
    except OSError as exc:
        return [f"machine-wide lock directory {base} could not be listed: {exc}"]
    for lock_dir in lock_dirs:
        owner, _raw = _read_lock_owner(lock_dir)
        if not owner:
            findings.append(
                f"machine-wide lock directory {lock_dir} has no readable owner record; "
                f"once nothing on this host uses that resource, remove it with "
                f"'agentctl resource release --lock <resource> --force-stale --reason <why>' "
                f"or by hand"
            )
            continue
        resource = str(owner.get("resource") or "")
        if not resource:
            findings.append(
                f"machine-wide lock directory {lock_dir} has an owner record without a "
                f"resource name (lease {owner.get('lease_id') or 'unknown'}); remove it by "
                f"hand once nothing on this host uses it"
            )
            continue
        if resource in local_resources:
            continue
        state, detail = _foreign_lock_liveness(owner)
        if state in {"live", "registering", "remote"}:
            continue
        findings.append(
            f"machine-wide lock for {resource} ({owner.get('lease_id') or 'unknown lease'}) "
            f"has no live holder: {detail}; {_foreign_lock_recovery_hint(resource, state)}"
        )
    return findings


def _resource_interlock_findings(root: Path) -> list[str]:
    """Report resource and worktree leases stuck without a live holder.

    These are exactly the interlocks where progress and hardware stay
    blocked although nobody is working: a dead holder keeps the lease,
    and fail-closed binding stops everyone else from releasing it. Each
    finding names the evidence and the command that resolves it.
    """
    findings: list[str] = []
    leases = [
        item for item in _load_runtime_leases(root).get("leases") or []
        if isinstance(item, dict)
    ]
    runs = {
        str(item.get("id")): item
        for item in leases
        if item.get("kind") == "run" and item.get("id")
    }
    sessions: dict[str, str] | None = None
    for item in leases:
        if item.get("kind") != "resource":
            continue
        if item.get("status") in {"released"}:
            continue
        if item.get("status") == "release_failed":
            findings.append(
                f"resource lease {item.get('id')} failed to release its external "
                f"lock ({item.get('release_error') or 'unknown error'}); remove the "
                f"lock by hand and re-run 'agentctl resource release "
                f"{item.get('id')} --force-stale --reason cleanup'"
            )
            continue
        if sessions is None:
            sessions = _session_liveness_index(root)
        state, detail = _resource_holder_liveness(item, runs, sessions)
        if state in {"live", "unknown"}:
            continue
        resources = ",".join(item.get("resources") or []) or "-"
        findings.append(
            f"resource lease {item.get('id')} ({resources}) has no live holder: "
            f"{detail}; automatic sweeps release terminal-run and "
            f"released-session holders, or run 'agentctl resource release "
            f"{item.get('id')} --force-stale --reason <why>'"
        )
    findings.extend(_machine_wide_lock_findings(root, leases))
    board_tasks = _load_board(root).get("tasks") or {}
    try:
        worktree_rows = _worktree_rows(root)
    except RuntimeError as exc:
        # Doctor's dedicated worktree check reports the enumeration
        # failure as a problem; here it only means the worktree half of
        # the interlock scan could not run.
        findings.append(f"worktree interlock check skipped: {exc}")
        return findings
    for row in worktree_rows:
        status = str(row.get("status") or "")
        if status == "released":
            continue
        task = str(row.get("task") or "")
        entry = board_tasks.get(task) if isinstance(board_tasks, dict) else None
        task_status = str((entry or {}).get("status") or "")
        if status == "active" and task_status == "done":
            findings.append(
                f"worktree lease {row.get('id')} is active but its task {task} "
                f"is done; run 'agentctl worktree release {row.get('id')}' to "
                f"free the checkout"
            )
        elif status == "failed":
            findings.append(
                f"worktree lease {row.get('id')} failed "
                f"({row.get('last_error') or 'creation interrupted'}); run "
                f"'agentctl worktree release {row.get('id')}' to clear it"
            )
    return findings


def _doctor_report(root: Path) -> dict:
    problems: list[str] = []
    warnings: list[str] = []
    checks: list[dict] = []

    missing = [rel for rel in _doctor_required_paths() if not (root / rel).exists()]
    if missing:
        problems.append("missing required files: " + ", ".join(missing))
    checks.append({
        "name": "required files",
        "status": "ok" if not missing else "fail",
        "detail": "all required workflow files are present" if not missing else ", ".join(missing),
    })

    if (root / ".git").exists():
        hooks_path = _git(root, "config", "--get", "core.hooksPath")
        if hooks_path != ".githooks":
            problems.append(f"git core.hooksPath is '{hooks_path or '<unset>'}', expected '.githooks'")
        checks.append({
            "name": "git hooks",
            "status": "ok" if hooks_path == ".githooks" else "fail",
            "detail": f"core.hooksPath={hooks_path or '<unset>'}",
        })
        driver_ok = _ledger_merge_driver_configured(root)
        attributes_ok = all(
            entry in {" ".join(line.split()) for line in _read(root / ".gitattributes").splitlines()}
            for entry in GITATTRIBUTES_MANAGED_ENTRIES
        )
        if not driver_ok or not attributes_ok:
            warnings.append(
                "ledger merge driver is not fully set up "
                f"({'config missing' if not driver_ok else 'config ok'}, "
                f"{'.gitattributes incomplete' if not attributes_ok else '.gitattributes ok'}); "
                "concurrent ledger changes from other checkouts will conflict instead of "
                "merging; run 'agentctl init .' and commit .gitattributes"
            )
        checks.append({
            "name": "ledger merge driver",
            "status": "ok" if driver_ok and attributes_ok else "warn",
            "detail": (
                f"merge.{LEDGER_MERGE_DRIVER}.driver "
                f"{'configured' if driver_ok else 'missing'}, .gitattributes "
                f"{'routes ledger files' if attributes_ok else 'incomplete'}"
            ),
        })
    else:
        warnings.append("not a Git repository; local Git hooks are not active")
        checks.append({"name": "git hooks", "status": "warn", "detail": "not a Git repository"})

    install_problems, install_warnings, install_checks = _doctor_managed_install(root)
    problems.extend(install_problems)
    warnings.extend(install_warnings)
    checks.extend(install_checks)

    loop_rows = [_loop_summary_line(root, p) for p in _loop_files(root)]
    bad_loops = [row for row in loop_rows if not row.get("ok")]
    if bad_loops:
        problems.extend(
            f"loop {row['id']} missing required sections: {', '.join(row['missing'])}"
            for row in bad_loops
        )
    checks.append({
        "name": "loop contracts",
        "status": "ok" if not bad_loops else "fail",
        "detail": f"{len(loop_rows)} loop(s), {len(bad_loops)} invalid",
    })

    try:
        _eval_path, eval_catalog = _eval_catalog(root)
        eval_count = len(eval_catalog["suites"])
    except ValueError as exc:
        problems.append(str(exc))
        checks.append({"name": "eval contracts", "status": "fail", "detail": str(exc)})
    else:
        checks.append({
            "name": "eval contracts",
            "status": "ok",
            "detail": f"{eval_count} deterministic suite(s)",
        })

    manual_problems = _check_base(root) + _check_receipt(root) + _check_escalations(root)
    if manual_problems:
        problems.extend(manual_problems)
    checks.append({
        "name": "manual check",
        "status": "ok" if not manual_problems else "fail",
        "detail": "agentctl check --mode manual equivalent",
    })

    interlocks = _resource_interlock_findings(root)
    warnings.extend(interlocks)
    checks.append({
        "name": "resource interlocks",
        "status": "ok" if not interlocks else "warn",
        "detail": (
            "no leases stuck without a live holder" if not interlocks
            else f"{len(interlocks)} lease(s) stuck without a live holder"
        ),
    })

    open_follow_ups = _loop_follow_up_packets(root)
    escalated = [pkt for _path, pkt in open_follow_ups if pkt.get("escalated")]
    if open_follow_ups:
        warnings.append(f"{len(open_follow_ups)} open loop follow-up packet(s)")
    if escalated:
        problems.append(f"{len(escalated)} escalated loop follow-up packet(s) need a decision")
    checks.append({
        "name": "loop follow-ups",
        "status": "fail" if escalated else ("warn" if open_follow_ups else "ok"),
        "detail": f"open={len(open_follow_ups)}, escalated={len(escalated)}",
    })

    tasks = _load_board(root).get("tasks", {})
    status_counts = Counter(t.get("status", "<missing>") for t in tasks.values())
    checks.append({
        "name": "task board",
        "status": "ok",
        "detail": ", ".join(f"{k}={status_counts[k]}" for k in sorted(status_counts)) or "empty",
    })

    state = _load_json(_loop_state_path(root), {"checkpoints": {}})
    checkpoints = state.get("checkpoints") if isinstance(state, dict) else {}
    if not isinstance(checkpoints, dict):
        checkpoints = {}
    checks.append({
        "name": "checkpoint memory",
        "status": "ok",
        "detail": f"{len(checkpoints)} checkpoint state entr{'y' if len(checkpoints) == 1 else 'ies'}",
    })

    runtime = _cycle_runtime(root)
    runtime_status = runtime.get("status") if runtime else "idle"
    if runtime_status == "interrupted":
        if runtime.get("resume_safe") is False:
            warnings.append(
                f"loop runtime {runtime.get('id')} has an unknown in-flight result; "
                "inspect and reconcile it before starting another cycle"
            )
        else:
            warnings.append(
                f"loop runtime {runtime.get('id')} is interrupted; resume or stop it before starting another cycle"
            )
    checks.append({
        "name": "cycle runtime",
        "status": "warn" if runtime_status in {"running", "stop_requested", "interrupted"} else "ok",
        "detail": (
            f"{runtime_status}, progress={runtime.get('completed_cycles', 0)}/"
            f"{runtime.get('requested_cycles', 0)}" if runtime else "idle"
        ),
    })

    execution_lease = _loop_execution_lease(root)
    lease_status = execution_lease.get("status") if execution_lease else "idle"
    if execution_lease:
        warnings.append(
            f"loop execution {execution_lease.get('operation')} is {lease_status}; "
            "inspect loop status before starting another execution"
        )
    checks.append({
        "name": "loop execution lease",
        "status": "warn" if execution_lease else "ok",
        "detail": lease_status,
    })

    try:
        worktree_rows = _worktree_rows(root)
    except RuntimeError as exc:
        problems.append(str(exc))
        checks.append({
            "name": "worktree leases",
            "status": "fail",
            "detail": str(exc),
        })
    else:
        active_worktrees = [row for row in worktree_rows if row.get("observed_status") == "active"]
        stale_worktrees = [
            row for row in worktree_rows
            if row.get("status") != "released" and row.get("observed_status") != "active"
        ]
        if stale_worktrees:
            warnings.append(
                "stale or conflicting worktree lease(s): "
                + ", ".join(str(row.get("id")) for row in stale_worktrees)
            )
        checks.append({
            "name": "worktree leases",
            "status": "warn" if stale_worktrees else "ok",
            "detail": f"active={len(active_worktrees)}, stale={len(stale_worktrees)}",
        })

    session_rows = [row for row in _session_rows_unlocked(root) if _same_checkout(root, row)]
    active_sessions = [row for row in session_rows if row.get("observed_status") == "active"]
    stale_sessions = [row for row in session_rows if row.get("observed_status") == "stale"]
    overlaps = []
    blocking = active_sessions + stale_sessions
    for index, left in enumerate(blocking):
        for right in blocking[index + 1:]:
            if (left.get("task") == right.get("task")
                    or not (left.get("scope") or [])
                    or not (right.get("scope") or [])
                    or _scopes_overlap(left.get("scope") or [], right.get("scope") or [])):
                overlaps.append(
                    f"{left.get('workflow_session_key')}:{left.get('task')} <-> "
                    f"{right.get('workflow_session_key')}:{right.get('task')}"
                )
    if stale_sessions:
        warnings.append(
            "stale agent session claim(s): "
            + ", ".join(str(row.get("workflow_session_key")) for row in stale_sessions)
        )
    if overlaps:
        problems.append("overlapping agent session claims: " + ", ".join(overlaps))
    checks.append({
        "name": "agent sessions",
        "status": "fail" if overlaps else ("warn" if stale_sessions else "ok"),
        "detail": (
            f"active={len(active_sessions)}, stale={len(stale_sessions)}, "
            f"overlaps={len(overlaps)}"
        ),
    })

    return {
        "root": str(root),
        "ok": not problems,
        "problems": problems,
        "warnings": warnings,
        "checks": checks,
        "tasks": dict(status_counts),
    }


def cmd_doctor(args: argparse.Namespace) -> int:
    root = _repo_root()
    report = _doctor_report(root)
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print(f"agentctl doctor: {'OK' if report['ok'] else 'FAIL'}")
        for row in report["checks"]:
            print(f"[{row['status']}] {row['name']}: {row['detail']}")
        if report["warnings"]:
            print("Warnings:")
            for item in report["warnings"]:
                print(f"  - {item}")
        if report["problems"]:
            print("Problems:")
            for item in report["problems"]:
                print(f"  - {item}")
    return 0 if report["ok"] else 1


def cmd_check(args: argparse.Namespace) -> int:
    root = _repo_root()
    mode = args.mode or "manual"
    if mode == "manual":
        problems = (
            _check_base(root)
            + _check_board_consistency(root)
            + _check_receipt(root)
            + _check_escalations(root)
            + _check_pending_guidance(root)
        )
    elif mode == "pre-commit":
        problems = _check_base(root) + _check_precommit(root)
    elif mode == "commit-msg":
        problems = _check_commit_msg(root, args.message_file)
    elif mode == "pre-push":
        problems = _check_prepush(root, args.commit_range, args.published_remote)
    elif mode == "ci":
        problems = _check_base(root) + _check_board_consistency(root) + _check_escalations(root)
    else:
        print(f"agentctl: unknown check mode '{mode}'", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps({"mode": mode, "ok": not problems, "problems": problems},
                         indent=2, ensure_ascii=False))
    elif problems:
        print(f"agentctl check ({mode}): FAIL")
        for p in problems:
            print(f"  - {p}")
    else:
        print(f"agentctl check ({mode}): OK")
    return 1 if problems else 0


def _sessions_list(root: Path, args: argparse.Namespace) -> int:
    if _workflow_session_identity_error():
        rows = _session_rows_unlocked(root)
    else:
        lock = _session_coordination_lock_path(root)
        fd = _acquire_lock_file(lock)
        try:
            rows = _session_rows_unlocked(root)
            _render_sessions_view(root, rows)
        finally:
            _release_lock_file(lock, fd)
    public = [_public_session_row(row) for row in rows]
    if args.json:
        print(json.dumps({"current": _workflow_session_key(), "sessions": public},
                         indent=2, ensure_ascii=False))
        return 0
    if not rows:
        print("agentctl: no recorded sessions")
        return 0
    current = _workflow_session_key()
    for row in rows:
        marker = "*" if (
            row.get("workflow_session_key") == current and _same_checkout(root, row)
        ) else " "
        print(
            f"{marker} {row.get('workflow_session_key'):<32} "
            f"{row.get('observed_status'):<9} task={row.get('task') or '-'} "
            f"agent={row.get('agent') or '-'} scope={','.join(row.get('scope') or []) or '-'}"
        )
    print(f"Live view: {WORKFLOW_DIR}/{STATE_DIR}/{SESSION_VIEW_FILE}")
    return 0


def _sessions_heartbeat(root: Path) -> int:
    lock = _session_coordination_lock_path(root)
    fd = _acquire_lock_file(lock)
    try:
        st = _load_session(root)
        if not st.get("task"):
            print("agentctl: no active session to heartbeat", file=sys.stderr)
            return 3
        if st.get("presence_status") == "released":
            print(
                "agentctl: this session was released for handoff; run 'agentctl work' "
                "to reclaim it only if no other conversation owns the task",
                file=sys.stderr,
            )
            return 1
        session_key = st.get("workflow_session_key") or _workflow_session_key()
        conflicts = _session_start_conflicts(
            root, session_key, str(st.get("task")), st.get("scope") or [],
            str(st.get("isolation") or "shared"),
        )
        if conflicts:
            for conflict in conflicts:
                print(f"agentctl: session heartbeat blocked: {conflict}", file=sys.stderr)
            return 1
        _record_runtime_identity(st)
        # A heartbeat only refreshes the canonical per-conversation record.
        # Re-reading the branch and rendering the shared Markdown view here
        # makes one otherwise read-only hook per debounce window pay for Git
        # and every recorded session. Mutating commands already use
        # _save_session(), while `sessions list` refreshes the generated view.
        st["identity_source"] = _workflow_session_identity_source()
        st["heartbeat_at"] = _now()
        st["heartbeat_ns"] = time.time_ns()
        st["revision"] = int(st.get("revision") or 0) + 1
        if st.get("status") in {"review", "approved", "done", "released"}:
            st["presence_status"] = st.get("status")
        else:
            st["presence_status"] = "working"
        _save_json(_session_path(root, session_key), st)
    finally:
        _release_lock_file(lock, fd)
    return 0


def _sessions_guard(root: Path, args: argparse.Namespace) -> int:
    lock = _session_coordination_lock_path(root)
    fd = _acquire_lock_file(lock)
    try:
        st = _load_session(root)
        if not st.get("task"):
            print("agentctl: no active task session for this conversation", file=sys.stderr)
            return 3
        if st.get("presence_status") == "released":
            print(
                "agentctl: no active task session; this conversation released its claim for handoff",
                file=sys.stderr,
            )
            return 3
        current_key = st.get("workflow_session_key") or _workflow_session_key()
        blockers = _blocking_session_rows(root, current_key)
        prior_peer_snapshot = st.get("peer_snapshot") or ""
        peer_snapshot = _peer_session_snapshot(blockers)
        problems = []
        if st.get("isolation") == "read-only" and (
                args.path or args.git_write or getattr(args, "git_shared", False)
                or getattr(args, "opaque", False)):
            problems.append(
                f"task {st.get('task')} is read-only; create a writable child task "
                "before changing files or Git state"
            )
        for other in blockers:
            if other.get("task") == st.get("task"):
                problems.append(
                    f"task {st.get('task')} is owned by session "
                    f"{other.get('workflow_session_key')} ({other.get('observed_status')})"
                )
            elif not (st.get("scope") or []) or not (other.get("scope") or []):
                problems.append(
                    f"write scopes cannot be proven disjoint from session "
                    f"{other.get('workflow_session_key')} task={other.get('task')}"
                )
            elif _scopes_overlap(st.get("scope") or [], other.get("scope") or []):
                problems.append(
                    f"scope conflicts with session {other.get('workflow_session_key')} "
                    f"task={other.get('task')} ({other.get('observed_status')})"
                )
        if args.git_write and blockers:
            owners = ", ".join(
                f"{row.get('workflow_session_key')}:{row.get('task')}"
                for row in blockers
            )
            problems.append(
                "Git index/HEAD/remote mutation requires an exclusive checkout; "
                f"other sessions are present: {owners}. Use a task worktree or finish/release them."
            )
        if getattr(args, "git_shared", False):
            shared_blockers = [
                row for row in _session_rows_unlocked(root)
                if row.get("workflow_session_key") != current_key
                and row.get("observed_status") in {"active", "stale"}
            ]
            if shared_blockers:
                owners = ", ".join(
                    f"{row.get('workflow_session_key')}:{row.get('task')}"
                    f"@{row.get('checkout') or '?'}"
                    for row in shared_blockers
                )
                problems.append(
                    "this Git operation rewrites refs/objects shared by every "
                    f"worktree of this repository; live sessions elsewhere: {owners}. "
                    "Run it only when no other conversation is active in any "
                    "checkout or worktree, or have those sessions finish/release first."
                )
        if getattr(args, "opaque", False) and blockers:
            owners = ", ".join(
                f"{row.get('workflow_session_key')}:{row.get('task')}"
                for row in blockers
            )
            problems.append(
                "this command's written paths cannot be enumerated, so it "
                "requires exclusive use of this checkout; other live sessions: "
                f"{owners}. Wait until those sessions finish/release, or put the "
                "next execution phase in a task worktree before starting that "
                "phase: create the task (leave it todo) and commit its plan "
                "+ .agent/tasks doc, then `python3 tools/agentctl.py worktree "
                "create --task <task-id> --agent <agent-name>`, cd into the "
                "printed path, `work --agent <agent-name> --task <task-id>`, and "
                "run the command there. An already-active shared-checkout task is "
                "not relocated implicitly."
            )
        claims = []
        effective_scope = _session_effective_scope(st)
        for raw in args.path or []:
            path, error = _normalize_claim_path(root, raw)
            if error:
                problems.append(error)
                continue
            if not path:
                continue
            owned_error = _controller_owned_claim_error(path)
            if owned_error:
                problems.append(owned_error)
                continue
            if not _path_in_scope(path, effective_scope):
                problems.append(
                    f"path {path} is outside active task scope {st.get('scope') or []}"
                )
                continue
            for other in blockers:
                for claimed in other.get("claimed_files") or []:
                    if _scopes_overlap([path], [claimed]):
                        problems.append(
                            f"path {path} is claimed by session "
                            f"{other.get('workflow_session_key')} task={other.get('task')}"
                        )
            claims.append(path)
        # The contamination scan protects concurrent peers from an escaped
        # write that static command inspection could not attribute. With no
        # other live session in this checkout there is nothing to protect, so
        # skip it entirely - this also avoids a `git status` on every write in
        # the recommended worktree-per-session layout.
        if blockers:
            checkout_rows = [
                row for row in _session_rows_unlocked(root)
                if _same_checkout(root, row)
            ]
            for path in _workspace_contamination(root, checkout_rows):
                problems.append(
                    f"tracked file {path} was modified outside every live session scope; "
                    "a write escaped the session guards (interpreter, script, or manual "
                    "edit). Revert it, claim it through a task scope, or let a human "
                    "commit it separately before continuing"
                )
        if problems:
            for problem in sorted(set(problems)):
                print(f"agentctl: session guard blocked: {problem}", file=sys.stderr)
            return 1
        existing = {str(item) for item in st.get("claimed_files") or [] if str(item)}
        existing.update(claims)
        st["claimed_files"] = sorted(existing)
        st["last_action"] = "git-write" if args.git_write else "write"
        st["peer_snapshot"] = peer_snapshot
        _record_runtime_identity(st)
        _save_session(root, st)
        if prior_peer_snapshot and prior_peer_snapshot != peer_snapshot:
            if blockers:
                print(_session_awareness(root, current_key))
            else:
                print(
                    "Other active/stale conversations changed; none remain in this checkout.\n"
                    f"Live view: {WORKFLOW_DIR}/{STATE_DIR}/{SESSION_VIEW_FILE}"
                )
        elif not prior_peer_snapshot and blockers:
            print(_session_awareness(root, current_key))
    finally:
        _release_lock_file(lock, fd)
    return 0


def _sessions_release(root: Path, args: argparse.Namespace) -> int:
    lock = _session_coordination_lock_path(root)
    fd = _acquire_lock_file(lock)
    try:
        rows = _session_rows_unlocked(root)
        target = args.session or _workflow_session_key()
        row = next(
            (item for item in rows
             if item.get("workflow_session_key") == target and _same_checkout(root, item)),
            None,
        )
        if not row:
            print(f"agentctl: session not found: {target}", file=sys.stderr)
            return 2
        current = _workflow_session_key()
        if target != current and row.get("observed_status") == "active":
            print(
                f"agentctl: session {target} is active and cannot be released by another conversation",
                file=sys.stderr,
            )
            return 1
        row.pop("_record_path", None)
        row.pop("observed_status", None)
        row.pop("heartbeat_age_seconds", None)
        row["presence_status"] = "released"
        row["released_at"] = _now()
        row["release_reason"] = args.reason
        row["heartbeat_at"] = _now()
        row["heartbeat_ns"] = time.time_ns()
        row["revision"] = int(row.get("revision") or 0) + 1
        _save_json(_session_path(root, target), row)
        task_lock = _lock_path(root, str(row.get("task") or ""))
        lock_record = _load_json(task_lock, {})
        if lock_record.get("workflow_session_key") == target:
            try:
                task_lock.unlink()
            except FileNotFoundError:
                pass
        _render_sessions_view(root)
    finally:
        _release_lock_file(lock, fd)
    # Free the resources this session held; the holder binding is
    # fail-closed, so nothing else can release them once the session is
    # gone. Runs after the coordination lock is dropped because the lease
    # registry uses its own lock.
    released, failures = _release_session_resources(
        root, target, "holding session released",
    )
    if released:
        print(
            f"agentctl: released {len(released)} resource lease(s) held by "
            f"session {target}: {', '.join(released)}"
        )
    for failure in failures:
        print(f"agentctl: warning: resource release failed: {failure}", file=sys.stderr)
    print(f"agentctl: released session {target}; task state was preserved for handoff")
    return 0


def cmd_sessions(args: argparse.Namespace) -> int:
    root = _repo_root()
    if args.sessions_action == "list":
        return _sessions_list(root, args)
    identity_error = _workflow_session_identity_error()
    if identity_error:
        print(f"agentctl: {identity_error}", file=sys.stderr)
        return 2
    if args.sessions_action == "heartbeat":
        return _sessions_heartbeat(root)
    if args.sessions_action == "guard":
        return _sessions_guard(root, args)
    if args.sessions_action == "release":
        return _sessions_release(root, args)
    print("agentctl: unknown sessions action", file=sys.stderr)
    return 2


def cmd_upgrade(args: argparse.Namespace) -> int:
    root = _repo_root()
    action = args.upgrade_action
    installed = _installed_protocol_epoch(root)
    state = _load_upgrade_state(root)
    if action == "status":
        payload = dict(state)
        payload["installed_epoch"] = installed
        payload["kit_version"] = KIT_VERSION
        payload["blocking_sessions"] = [
            {
                "session": row.get("workflow_session_key"),
                "task": row.get("task"),
                "status": row.get("observed_status"),
                "protocol_epoch": _session_protocol_epoch(root, row),
                "checkout": row.get("checkout"),
            }
            for row in _upgrade_blocking_sessions(root)
        ]
        if args.json:
            print(json.dumps(payload, indent=2, ensure_ascii=False))
        else:
            print(
                f"agentctl: upgrade state={payload.get('state')} "
                f"installed_epoch={installed} target_epoch={payload.get('target_epoch')}"
            )
            for row in payload["blocking_sessions"]:
                print(
                    f"  {row['session']} task={row['task']} status={row['status']} "
                    f"epoch={row['protocol_epoch']} checkout={row['checkout']}"
                )
        return 0
    if action == "begin":
        target = int(args.target_epoch)
        if target <= installed:
            print(
                f"agentctl: target epoch {target} must be greater than installed "
                f"epoch {installed}",
                file=sys.stderr,
            )
            return 2
        state = {
            "state": "draining",
            "installed_epoch": installed,
            "target_epoch": target,
            "target_version": args.target_version or "",
            "started_at": _now(),
            "initiated_by": _workflow_session_key(),
        }
        _save_upgrade_state(root, state)
        blockers = _upgrade_blocking_sessions(root)
        print(
            f"agentctl: upgrade barrier active for epoch {installed} -> {target}; "
            f"{len(blockers)} active/stale session(s) must finish or release"
        )
        return 0
    if action == "validate":
        problems = []
        target = int(state.get("target_epoch") or installed)
        if state.get("state") not in {"draining", "validating", "steady"}:
            problems.append(f"unknown upgrade state {state.get('state')!r}")
        if state.get("state") == "steady" and target != installed:
            problems.append("steady state target epoch does not match installed epoch")
        if state.get("state") == "validating" and target != installed:
            problems.append(
                f"installed epoch {installed} does not match validating target {target}"
            )
        blockers = _upgrade_blocking_sessions(root)
        if state.get("state") in {"draining", "validating"} and blockers:
            problems.append(
                f"{len(blockers)} active/stale session(s) still hold write authority"
            )
        manifest = _load_json(_install_manifest_path(root), {})
        if _install_manifest_path(root).is_file():
            if manifest.get("version") != INSTALL_SCHEMA_VERSION:
                problems.append(
                    f"manifest schema is {manifest.get('version')}, expected "
                    f"{INSTALL_SCHEMA_VERSION}"
                )
            if int(manifest.get("protocol_epoch") or LEGACY_PROTOCOL_EPOCH) != installed:
                problems.append("manifest protocol epoch is invalid")
        if args.json:
            print(json.dumps({
                "ok": not problems,
                "state": state,
                "installed_epoch": installed,
                "problems": problems,
            }, indent=2, ensure_ascii=False))
        elif problems:
            print("agentctl: upgrade validation failed", file=sys.stderr)
            for problem in problems:
                print(f"  - {problem}", file=sys.stderr)
        else:
            print("agentctl: upgrade validation OK")
        return 1 if problems else 0
    if action == "complete":
        target = int(state.get("target_epoch") or installed)
        blockers = _upgrade_blocking_sessions(root)
        problems = []
        if state.get("state") not in {"draining", "validating"}:
            problems.append(
                f"upgrade state is {state.get('state')}, expected draining or validating"
            )
        if installed != target:
            problems.append(
                f"installed epoch {installed} does not match target epoch {target}"
            )
        if blockers:
            problems.append(
                f"{len(blockers)} active/stale session(s) still hold write authority"
            )
        if problems:
            print("agentctl: cannot complete upgrade:", file=sys.stderr)
            for problem in problems:
                print(f"  - {problem}", file=sys.stderr)
            return 1
        _save_upgrade_state(root, {
            "state": "steady",
            "installed_epoch": installed,
            "target_epoch": installed,
            "target_version": state.get("target_version") or KIT_VERSION,
            "started_at": state.get("started_at"),
            "completed_at": _now(),
            "initiated_by": state.get("initiated_by"),
        })
        print(f"agentctl: upgrade complete at protocol epoch {installed}")
        return 0
    if action == "rebind":
        if state.get("state") != "steady":
            print(
                f"agentctl: cannot rebind while upgrade is {state.get('state')}",
                file=sys.stderr,
            )
            return 1
        session = _load_session(root)
        if not session.get("task"):
            print("agentctl: no task session to rebind", file=sys.stderr)
            return 2
        session_key = session.get("workflow_session_key") or _workflow_session_key()
        conflicts = _session_start_conflicts(
            root, session_key, str(session.get("task")), session.get("scope") or [],
            str(session.get("isolation") or "shared"),
        )
        if conflicts:
            for conflict in conflicts:
                print(f"agentctl: rebind blocked: {conflict}", file=sys.stderr)
            return 1
        old_epoch = _session_protocol_epoch(root, session)
        session["protocol_epoch"] = installed
        session["doc_hashes"] = _hash_docs(root, session["task"])
        session["rebound_at"] = _now()
        session["rebound_from_epoch"] = old_epoch
        _record_runtime_identity(session)
        _save_session(root, session)
        print(
            f"agentctl: rebound {session['task']} from protocol epoch "
            f"{old_epoch} to {installed}"
        )
        return 0
    print("agentctl: unknown upgrade action", file=sys.stderr)
    return 2


def cmd_status(args: argparse.Namespace) -> int:
    root = _repo_root()
    st = _load_session(root)
    if args.json:
        print(json.dumps(st or {}, indent=2, ensure_ascii=False))
        return 0
    if not st:
        print("agentctl: no active session")
        return 0
    print(json.dumps(st, indent=2, ensure_ascii=False))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="agentctl", description="Agent Workflow Kit controller")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("init")
    sp.add_argument("path", nargs="?", default=".")
    sp.add_argument(
        "--force-managed", action="store_true",
        help="replace conflicting kit-managed files after explicit inspection",
    )
    sp.set_defaults(func=cmd_init)

    sp = sub.add_parser("work")
    sp.add_argument("--agent")
    sp.add_argument("--task")
    sp.add_argument("--scope")
    sp.add_argument("--auto-create", action="store_true")
    sp.add_argument("--title")
    sp.add_argument("--new-id")
    sp.add_argument("--prefix", default="T")
    sp.add_argument("--deps", default="")
    sp.add_argument("--type", dest="task_type", choices=sorted(TASK_TYPES), default="generic")
    sp.add_argument("--isolation", choices=sorted(ISOLATION_MODES), default="auto")
    sp.add_argument("--session-id")
    sp.add_argument("--model")
    sp.add_argument("--reasoning-effort")
    sp.add_argument("--force", action="store_true")
    sp.add_argument("--request-id", default="")
    sp.add_argument(
        "--takeover", action="store_true",
        help="Claim a task the board shows in_progress for another checkout or "
             "machine; requires --reason and is recorded in the task document",
    )
    sp.add_argument("--reason", default="")
    sp.set_defaults(func=cmd_work)

    sp = sub.add_parser("start")
    sp.add_argument("--task", required=True)
    sp.add_argument("--agent")
    sp.add_argument("--scope")
    sp.add_argument("--session-id")
    sp.add_argument("--model")
    sp.add_argument("--reasoning-effort")
    sp.add_argument("--force", action="store_true")
    sp.add_argument(
        "--takeover", action="store_true",
        help="Claim a task the board shows in_progress for another checkout or "
             "machine; requires --reason and is recorded in the task document",
    )
    sp.add_argument("--reason", default="")
    sp.set_defaults(func=cmd_start)

    sp = sub.add_parser("focus")
    sp.add_argument("--task")
    sp.add_argument("--agent")
    sp.add_argument("--session-id")
    sp.add_argument("--model")
    sp.add_argument("--reasoning-effort")
    sp.set_defaults(func=cmd_focus)

    sp = sub.add_parser("capsule")
    sp.add_argument("--task")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_capsule)

    sp = sub.add_parser("progress")
    sp.add_argument("--note")
    sp.set_defaults(func=cmd_progress)

    sp = sub.add_parser("note")
    sp.add_argument("note", nargs="+")
    sp.set_defaults(func=cmd_note)

    sp = sub.add_parser("complete")
    sp.add_argument("--summary")
    sp.add_argument("--tests")
    sp.add_argument("--ack-escalations", action="store_true", dest="ack_escalations")
    sp.set_defaults(func=cmd_complete)

    sp = sub.add_parser("finish")
    sp.add_argument("--summary")
    sp.add_argument("--tests")
    sp.add_argument("--ack-escalations", action="store_true", dest="ack_escalations")
    sp.set_defaults(func=cmd_finish)

    sp = sub.add_parser("gate")
    sp.add_argument("action", choices=["approve", "reject", "reconcile-github"])
    sp.add_argument("--task", required=True)
    sp.add_argument("--by", required=True)
    sp.add_argument("--note")
    sp.add_argument("--pr", help="merged GitHub PR number or URL for reconcile-github")
    sp.add_argument("--repo", help="GitHub OWNER/REPO for reconcile-github")
    sp.set_defaults(func=cmd_gate)

    sp = sub.add_parser("refresh")
    sp.set_defaults(func=cmd_refresh)

    sp = sub.add_parser("board")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_board)

    sp = sub.add_parser("task")
    tsub = sp.add_subparsers(dest="task_action", required=True)
    c = tsub.add_parser("create")
    c.add_argument("--id", required=True)
    c.add_argument("--title")
    c.add_argument("--owner")
    c.add_argument("--scope")
    c.add_argument("--deps")
    c.add_argument("--type", dest="task_type", choices=sorted(TASK_TYPES), default="generic")
    c.add_argument("--force", action="store_true")
    sh = tsub.add_parser("show")
    sh.add_argument("id")
    sp.set_defaults(func=cmd_task)

    sp = sub.add_parser("reconcile")
    rsub = sp.add_subparsers(dest="reconcile_action", required=True)
    rsub.add_parser("check")
    rsub.add_parser("render")
    rsub.add_parser("migrate")
    rsub.add_parser("close-decided-reviews")
    ra = rsub.add_parser("archive")
    ra.add_argument("--days", type=float, default=30.0)
    rm = rsub.add_parser("merge-back")
    rm.add_argument("--from-ref", dest="from_ref", required=True)
    rm.add_argument("--task", action="append", default=[])
    rm.add_argument("--dry-run", action="store_true")
    sp.set_defaults(func=cmd_reconcile)

    sp = sub.add_parser("agents")
    asub = sp.add_subparsers(dest="agents_action", required=True)
    aa = asub.add_parser("add")
    aa.add_argument("--id", required=True)
    aa.add_argument("--role")
    aa.add_argument("--backend")
    aa.add_argument("--scope")
    aa.add_argument("--tools")
    aa.add_argument("--model")
    aa.add_argument("--reasoning-effort")
    aa.add_argument("--session-id")
    al = asub.add_parser("list")
    al.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_agents)

    sp = sub.add_parser("handoff")
    hsub = sp.add_subparsers(dest="handoff_action", required=True)
    hc = hsub.add_parser("create")
    hc.add_argument("--from", dest="from_task", required=True)
    hc.add_argument("--to", dest="to_task", required=True)
    hc.add_argument("--by")
    hc.add_argument("--summary", required=True)
    hc.add_argument("--artifact", help="Comma-separated artifact paths")
    hc.add_argument("--note")
    hl = hsub.add_parser("list")
    hl.add_argument("--task")
    hl.add_argument("--status")
    hl.add_argument("--json", action="store_true")
    hs = hsub.add_parser("show")
    hs.add_argument("packet")
    hs.add_argument("--json", action="store_true")
    hm = hsub.add_parser("mark")
    hm.add_argument("packet")
    hm.add_argument("--status", choices=["done", "failed"], required=True)
    hm.add_argument("--note")
    sp.set_defaults(func=cmd_handoff)

    sp = sub.add_parser("worktree")
    wsub = sp.add_subparsers(dest="worktree_action", required=True)
    wc = wsub.add_parser("create")
    wc.add_argument("--task", required=True)
    wc.add_argument("--agent", required=True)
    wc.add_argument("--branch")
    wc.add_argument("--path")
    wc.add_argument("--base", default="HEAD")
    wl = wsub.add_parser("list")
    wl.add_argument("--json", action="store_true")
    wr = wsub.add_parser("release")
    wr.add_argument("lease")
    wr.add_argument(
        "--ack-missing", "--ack-prunable", dest="ack_missing", action="store_true",
        help="Acknowledge an inspected missing checkout before releasing its lease",
    )
    sp.set_defaults(func=cmd_worktree)

    sp = sub.add_parser("lease")
    lsub = sp.add_subparsers(dest="lease_action", required=True)
    ll = lsub.add_parser("list")
    ll.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_lease)

    sp = sub.add_parser("run")
    rsub = sp.add_subparsers(dest="run_action", required=True)
    rs = rsub.add_parser("start")
    rs.add_argument("--task")
    rs.add_argument("--cwd")
    rs.add_argument("--output", action="append", required=True)
    rs.add_argument("--resource", action="append", default=[])
    rs.add_argument("--gpu-watchdog", action="store_true")
    rs.add_argument("--gpu-idle-seconds", type=float)
    rs.add_argument("--gpu-grace-seconds", type=float)
    rs.add_argument("--gpu-sample-seconds", type=float)
    rs.add_argument("--gpu-kill-seconds", type=float)
    rs.add_argument("--gpu-utilization-max", type=float)
    rs.add_argument("--gpu-memory-min-mib", type=float)
    rs.add_argument("--gpu-probe-timeout-seconds", type=float)
    rs.add_argument("--gpu-idle-action", choices=["report", "terminate"])
    rs.add_argument("--request-id", default="")
    rs.add_argument("command", nargs=argparse.REMAINDER)
    ra = rsub.add_parser("adopt")
    ra.add_argument("--pid", required=True)
    ra.add_argument("--cwd", required=True)
    ra.add_argument("--output", action="append", required=True)
    ra.add_argument("--resource", action="append", default=[])
    ra.add_argument("--command-sha256")
    rl = rsub.add_parser("list")
    rl.add_argument("--json", action="store_true")
    rshow = rsub.add_parser("show")
    rshow.add_argument("lease")
    rshow.add_argument("--json", action="store_true")
    rw = rsub.add_parser("wait")
    rw.add_argument("lease")
    rw.add_argument("--timeout", type=float, default=3600)
    rp = rsub.add_parser("progress")
    rp.add_argument("lease", nargs="?")
    rp.add_argument("--phase", required=True)
    rp.add_argument("--token")
    rp.add_argument("--idle-exempt-seconds", type=float, default=0.0)
    rf = rsub.add_parser("finish")
    rf.add_argument("lease")
    rf.add_argument("--status", choices=["succeeded", "failed", "cancelled"], required=True)
    rf.add_argument("--reason", required=True)
    rstop = rsub.add_parser("stop")
    rstop.add_argument("lease")
    rstop.add_argument("--reason", required=True)
    sp.set_defaults(func=cmd_run)

    sp = sub.add_parser("resource")
    rsub = sp.add_subparsers(dest="resource_action", required=True)
    racquire = rsub.add_parser("acquire")
    racquire.add_argument("resource")
    rstatus = rsub.add_parser("status")
    rstatus.add_argument("--json", action="store_true")
    # allow_abbrev=False keeps a bare `--force` an error instead of a
    # silent abbreviation of --force-stale.
    rrelease = rsub.add_parser("release", allow_abbrev=False)
    rrelease.add_argument("lease", nargs="?")
    rrelease.add_argument(
        "--lock", dest="lock_resource", metavar="RESOURCE",
        help="Address a machine-wide lock by resource name (for example gpu:0) "
             "when another checkout on this host recorded it; requires --force-stale",
    )
    rrelease.add_argument("--reason", required=True)
    rrelease.add_argument(
        "--force-stale", dest="force_stale", action="store_true",
        help="Release a lease whose holder is demonstrably not live "
             "(stale, released, terminal, or missing); live holders are refused",
    )
    sp.set_defaults(func=cmd_resource)

    sp = sub.add_parser("_run-supervise", help=argparse.SUPPRESS)
    sp.add_argument("--root", required=True)
    sp.add_argument("--lease", required=True)
    sp.add_argument("--token", required=True)
    sp.set_defaults(func=cmd_run, run_action="_supervise")

    sp = sub.add_parser("eval")
    esub = sp.add_subparsers(dest="eval_action", required=True)
    el = esub.add_parser("list")
    el.add_argument("--suite-file")
    el.add_argument("--json", action="store_true")
    er = esub.add_parser("run")
    er.add_argument("suite")
    er.add_argument("--target", help="Candidate or baseline checkout; defaults to the policy checkout")
    er.add_argument("--suite-file", help="Supervisor-owned suite file; defaults to .agent/evals/suites.json")
    er.add_argument("--json", action="store_true")
    es = esub.add_parser("show")
    es.add_argument("report")
    ec = esub.add_parser("compare")
    ec.add_argument("--baseline", required=True)
    ec.add_argument("--candidate", required=True)
    ec.add_argument("--suite-file")
    ec.add_argument("--json", action="store_true")
    eg = esub.add_parser("gate")
    eg.add_argument("--baseline", required=True)
    eg.add_argument("--candidate", required=True)
    eg.add_argument("--suite-file")
    eg.add_argument("--by", required=True)
    eg.add_argument("--note")
    eg.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_eval)

    sp = sub.add_parser("guidance")
    gsub = sp.add_subparsers(dest="guidance_action", required=True)
    gc = gsub.add_parser("create")
    gc.add_argument("--from-agent", default="supervisor")
    gc.add_argument("--from-model")
    gc.add_argument("--from-reasoning-effort")
    gc.add_argument("--from-session")
    gc.add_argument("--to-agent", required=True)
    gc.add_argument("--to-model")
    gc.add_argument("--to-reasoning-effort")
    gc.add_argument("--to-session")
    gc.add_argument("--task")
    gc.add_argument("--summary", required=True)
    gc.add_argument("--plan")
    gc.add_argument("--plan-file")
    gc.add_argument("--artifact", help="Comma-separated artifact paths")
    gc.add_argument("--note")
    gc.add_argument("--dispatch", action="store_true",
                    help="Immediately dispatch the packet to the target Codex session")
    gc.add_argument("--transport", choices=["codex-cli"], default="codex-cli")
    gc.add_argument("--timeout", type=int, default=GUIDANCE_DISPATCH_TIMEOUT_DEFAULT)
    gc.add_argument("--dry-run", action="store_true",
                    help="Print the dispatch command without starting Codex")
    gl = gsub.add_parser("list")
    gl.add_argument("--agent")
    gl.add_argument("--model")
    gl.add_argument("--session-id")
    gl.add_argument("--task")
    gl.add_argument("--status")
    gl.add_argument("--json", action="store_true")
    gs = gsub.add_parser("show")
    gs.add_argument("packet")
    gs.add_argument("--json", action="store_true")
    ga = gsub.add_parser("ack")
    ga.add_argument("packet")
    ga.add_argument("--by")
    ga.add_argument("--task")
    ga.add_argument("--note")
    gd = gsub.add_parser("dispatch")
    gd.add_argument("packet")
    gd.add_argument("--transport", choices=["codex-cli"], default="codex-cli")
    gd.add_argument("--session-id", help="Override the packet target session")
    gd.add_argument("--model", help="Override the packet target model")
    gd.add_argument("--reasoning-effort", help="Override the target model reasoning effort")
    gd.add_argument("--timeout", type=int, default=GUIDANCE_DISPATCH_TIMEOUT_DEFAULT)
    gd.add_argument("--dry-run", action="store_true",
                    help="Print the dispatch command without starting Codex")
    gv = gsub.add_parser("verify")
    gv.add_argument("packet")
    gv.add_argument("--by", required=True, help="Independent supervisor or reviewer identity")
    gv.add_argument("--target", help="Worker checkout containing task and packet evidence")
    gv.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_guidance)

    sp = sub.add_parser("loop")
    lsub = sp.add_subparsers(dest="loop_action", required=True)
    ll = lsub.add_parser("list")
    ll.add_argument("--json", action="store_true")
    ls = lsub.add_parser("show")
    ls.add_argument("id")
    ls.add_argument("--json", action="store_true")
    lr = lsub.add_parser("run")
    lr.add_argument("id")
    lr.add_argument("--once", action="store_true")
    lr.add_argument("--trigger", default="manual")
    la = lsub.add_parser("auto")
    la.add_argument("--checkpoint", required=True)
    la.add_argument("--once", action="store_true")
    la.add_argument("--trigger", default="manual")
    la.add_argument("--strict", action="store_true")
    la.add_argument("--force", action="store_true")
    lc = lsub.add_parser("cycle")
    lc.add_argument("--checkpoint", required=True)
    lc.add_argument("--cycles", required=True, type=int)
    lc.add_argument("--interval", type=float, default=0.0,
                    help="Seconds to wait between cycles; default: 0")
    lc.add_argument("--trigger", default="cycle")
    lc.add_argument("--strict", action="store_true")
    lc.add_argument("--force", action="store_true",
                    help="Bypass checkpoint debounce on every cycle")
    lc.add_argument("--continue-on-failure", action="store_true",
                    help="Run remaining cycles after a failing checkpoint")
    lc.add_argument("--max-failures", type=int, default=0,
                    help="Stop after this many failures; default: 1, or all cycles with --continue-on-failure")
    lstatus = lsub.add_parser("status")
    lstatus.add_argument("--json", action="store_true")
    lsub.add_parser("resume")
    lstop = lsub.add_parser("stop")
    lstop.add_argument("--reason", help="Durable reason recorded with the cooperative stop")
    lstop.add_argument(
        "--ack-inflight",
        action="store_true",
        help="Acknowledge an inspected, no-longer-running command whose result was not recorded",
    )
    sp.set_defaults(func=cmd_loop)

    sp = sub.add_parser("check")
    sp.add_argument("--mode", default="manual")
    sp.add_argument("--message-file")
    sp.add_argument("--commit-range")
    sp.add_argument(
        "--published-remote",
        help="Exclude commits already reachable from this configured remote's tracking refs",
    )
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_check)

    sp = sub.add_parser("doctor")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_doctor)

    sp = sub.add_parser("migrate")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_migrate)

    sp = sub.add_parser("sessions")
    ssub = sp.add_subparsers(dest="sessions_action", required=True)
    sl = ssub.add_parser("list")
    sl.add_argument("--json", action="store_true")
    ssub.add_parser("heartbeat")
    sg = ssub.add_parser("guard")
    sg.add_argument("--path", action="append", default=[])
    sg.add_argument("--git-write", action="store_true")
    sg.add_argument("--git-shared", action="store_true")
    sg.add_argument("--opaque", action="store_true")
    sr = ssub.add_parser("release")
    sr.add_argument("session", nargs="?")
    sr.add_argument("--reason", required=True)
    sp.set_defaults(func=cmd_sessions)

    sp = sub.add_parser("upgrade")
    usub = sp.add_subparsers(dest="upgrade_action", required=True)
    ub = usub.add_parser("begin")
    ub.add_argument("--target-epoch", required=True, type=int)
    ub.add_argument("--target-version")
    ust = usub.add_parser("status")
    ust.add_argument("--json", action="store_true")
    uv = usub.add_parser("validate")
    uv.add_argument("--json", action="store_true")
    usub.add_parser("complete")
    usub.add_parser("rebind")
    sp.set_defaults(func=cmd_upgrade)

    sp = sub.add_parser("status")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser(
        "sync",
        help="Commit this checkout's ledger changes, pull with the ledger merge "
             "driver, and push, so other checkouts see the claim",
    )
    sp.add_argument("--remote", default="origin")
    sp.add_argument("--branch", default="", help="Remote branch; defaults to the current branch")
    sp.add_argument("--message", default="", help="Commit subject; defaults to a ledger summary")
    sp.add_argument("--no-push", dest="no_push", action="store_true")
    sp.set_defaults(func=cmd_sync)

    sp = sub.add_parser("merge-driver", help=argparse.SUPPRESS)
    sp.add_argument("--base", required=True)
    sp.add_argument("--ours", required=True)
    sp.add_argument("--theirs", required=True)
    sp.add_argument("--path", required=True)
    sp.set_defaults(func=cmd_merge_driver)

    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if _command_requires_trusted_identity(args):
        identity_error = _workflow_session_identity_error()
        if identity_error:
            print(f"agentctl: {identity_error}", file=sys.stderr)
            return 2
    if getattr(args, "cmd", "") != "init":
        upgrade_error = _upgrade_command_error(_repo_root(), args)
        if upgrade_error:
            print(f"agentctl: {upgrade_error}", file=sys.stderr)
            return 1
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
