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
  handoff    create/list/show/close cross-agent task packets
  loop       list/show/run one-shot project loops; auto-run checkpoint loops
  refresh    re-record doc hashes after plan/rules/task docs changed
  board      print the task board (human or --json)
  task       create / show task documents and board entries
  agents     add / list agent profiles
  check      verify workflow state (--mode manual|pre-commit|commit-msg|pre-push|ci)
  status     print the current session (human or --json)

Exit codes: 0 = ok, 1 = violations found, 2 = usage/internal error, 3 = no session.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import os
import re
import string
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

WORKFLOW_DIR = ".agent"
STATE_DIR = "state"
SESSION_FILE = "current_session.json"
LOCKS_DIR = "locks"
BOARD_FILE = "board.json"
AGENTS_FILE = "agents.json"
ADOPTION_FILE = "adoption.json"
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
TASK_ID_RE = re.compile(r"\b[A-Z][A-Z0-9]*-\d+\b")
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
    _write(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def _git(root: Path, *args: str) -> str:
    try:
        out = subprocess.check_output(["git", "-C", str(root), *args], stderr=subprocess.DEVNULL)
        return out.decode("utf-8", "replace").strip()
    except Exception:
        return ""


def _ensure_gitignore(root: Path) -> None:
    path = root / ".gitignore"
    block = "\n# Agent Workflow Kit local state\n.agent/state/\n.agent/tmp/\n"
    text = _read(path)
    if ".agent/state/" in text:
        return
    _write(path, text.rstrip() + block if text else block.lstrip())


def _state_dir(root: Path) -> Path:
    return root / WORKFLOW_DIR / STATE_DIR


def _session_path(root: Path) -> Path:
    return _state_dir(root) / SESSION_FILE


def _load_session(root: Path) -> dict:
    return _load_json(_session_path(root), {})


def _save_session(root: Path, st: dict) -> None:
    _save_json(_session_path(root), st)


def _clear_session(root: Path) -> None:
    p = _session_path(root)
    if p.is_file():
        p.unlink()


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


def _load_agents(root: Path) -> dict:
    return _load_json(_agents_path(root), {"version": 1, "agents": {}})


def _save_agents(root: Path, data: dict) -> None:
    _save_json(_agents_path(root), data)


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
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError, TypeError):
        return False


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


def _doc_hash_targets(root: Path, task: str | None):
    targets = [
        root / "AGENTS.md",
        root / WORKFLOW_DIR / PLAN_FILE,
        root / WORKFLOW_DIR / TASKS_FILE,
        root / WORKFLOW_DIR / RULES_DIR / "agent-operating-rules.md",
    ]
    if task:
        targets.append(root / WORKFLOW_DIR / TASKS_DIR / f"{task}.md")
    return targets


def _hash_docs(root: Path, task: str | None) -> dict:
    hashes = {}
    for d in _doc_hash_targets(root, task):
        if d.is_file():
            hashes[str(d.relative_to(root))] = hashlib.sha256(d.read_bytes()).hexdigest()[:12]
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


def _next_task_id(root: Path, prefix: str = "T") -> str:
    board = _load_board(root)
    max_num = 0
    pattern = re.compile(rf"^{re.escape(prefix)}-(\d+)$")
    for tid in board.get("tasks", {}):
        m = pattern.match(tid)
        if m:
            max_num = max(max_num, int(m.group(1)))
    return f"{prefix}-{max_num + 1:03d}"


# ---------- commands ----------

def cmd_init(args: argparse.Namespace) -> int:
    root = Path(args.path).resolve()
    kit = _kit_root()
    src = kit / "templates" / "project"
    if not src.is_dir():
        print(f"agentctl: template dir not found: {src}", file=sys.stderr)
        return 2
    copied = 0
    for dirpath, _dirs, files in os.walk(src):
        rel = Path(dirpath).relative_to(src)
        for fn in files:
            d = root / rel / fn
            if d.exists():
                continue
            _write(d, _read(Path(dirpath) / fn))
            copied += 1
    # distribute agentctl.py itself so project hooks can call tools/agentctl.py
    self_src = kit / "tools" / "agentctl.py"
    self_dst = root / "tools" / "agentctl.py"
    if self_src.resolve() != self_dst.resolve():
        _write(self_dst, _read(self_src))
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
                _write(dst, _read(h))
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
                           "tools": [], "model": ""}}})
    _record_adoption_baseline(root)
    _ensure_gitignore(root)
    for kind in (BUS_INBOX, BUS_OUTBOX, BUS_DONE, BUS_FAILED):
        _bus_dir(root, kind).mkdir(parents=True, exist_ok=True)
    wired = False
    if (root / ".git").exists() and installed:
        _git(root, "config", "core.hooksPath", ".githooks")
        wired = True
    print(f"agentctl: initialized workflow ({copied} template files) at {root}")
    print(f"agentctl: distributed agentctl.py + {len(installed)} git hooks into .githooks/")
    if wired:
        print("agentctl: git core.hooksPath -> .githooks")
    elif installed:
        print("agentctl: NOTE not a git repo; after 'git init' run: git config core.hooksPath .githooks")
    return 0


def _print_focus(root: Path, task: str) -> None:
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
    print("Required reading: AGENTS.md, .agent/PROJECT_PLAN.md, and the task doc above.")
    print("=== end focus ===")


def cmd_work(args: argparse.Namespace) -> int:
    root = _repo_root()
    agent = args.agent or os.environ.get("AGENT_NAME", "unknown")
    if args.task:
        start_args = argparse.Namespace(task=args.task, agent=agent, scope=args.scope, force=args.force)
        return cmd_start(start_args)
    st = _load_session(root)
    active = st.get("task")
    if active and _task_status(root, active) == "in_progress" and not args.force:
        print(f"agentctl: resuming active task {active} (agent={st.get('agent') or agent})")
        _print_focus(root, active)
        _run_loop_checkpoint(root, "work-start", once=True, trigger="work-resume", strict=False)
        return 0
    task = _select_next_task(root, agent)
    if not task:
        if args.auto_create:
            if not args.title:
                print("agentctl: --title is required with --auto-create", file=sys.stderr)
                return 2
            if not args.scope:
                print("agentctl: --scope is required with --auto-create so the task has a safe write boundary", file=sys.stderr)
                return 2
            task = args.new_id or _next_task_id(root, args.prefix or "T")
            create_args = argparse.Namespace(
                id=task,
                title=args.title,
                owner=agent,
                scope=args.scope,
                deps=args.deps or "",
                force=args.force,
            )
            rc = _task_create(root, create_args)
            if rc:
                return rc
            print(f"agentctl: auto-created {task} for {agent}")
            start_args = argparse.Namespace(task=task, agent=agent, scope=args.scope, force=args.force)
            return cmd_start(start_args)
        print(f"agentctl: no ready/todo task assigned to {agent}.")
        print("agentctl: if this is a new user request, create and start a task in one command:")
        print("  python3 tools/agentctl.py work --agent " + agent + " --auto-create --title \"...\" --scope path/")
        return 1
    print(f"agentctl: auto-selected {task} for {agent}")
    start_args = argparse.Namespace(task=task, agent=agent, scope=args.scope, force=args.force)
    return cmd_start(start_args)


def cmd_start(args: argparse.Namespace) -> int:
    root = _repo_root()
    if not (root / WORKFLOW_DIR / PLAN_FILE).is_file():
        print(f"agentctl: missing {WORKFLOW_DIR}/{PLAN_FILE}. run 'agentctl init' first.", file=sys.stderr)
        return 2
    task = args.task
    agent = args.agent or os.environ.get("AGENT_NAME", "unknown")
    board = _load_board(root)
    tasks = board.setdefault("tasks", {})
    entry = tasks.get(task, {})
    scope = entry.get("scope") or []
    if args.scope:
        scope = [s.strip() for s in args.scope.split(",") if s.strip()]
    # write-scope conflict vs other in_progress tasks owned by others
    if scope:
        for tid, t in tasks.items():
            if tid == task or t.get("status") not in ACTIVE_STATUSES:
                continue
            if t.get("owner") in (None, "", agent):
                continue
            if _scopes_overlap(scope, t.get("scope") or []) and not args.force:
                print(f"agentctl: write-scope conflict with {tid} (owner={t.get('owner')}, "
                      f"scope={t.get('scope')}). use --force to override.", file=sys.stderr)
                return 1
    # acquire lock
    lp = _lock_path(root, task)
    existing = _load_json(lp, {})
    if existing and existing.get("agent") not in (None, "", agent):
        if _pid_alive(existing.get("pid")) and not args.force:
            print(f"agentctl: {task} locked by agent={existing.get('agent')} pid={existing.get('pid')} "
                  f"since {existing.get('acquired_at')}. use --force to steal.", file=sys.stderr)
            return 1
    _save_json(lp, {"task": task, "agent": agent, "pid": os.getpid(),
                    "scope": scope, "acquired_at": _now()})
    # board -> in_progress
    now = _now()
    e = tasks.setdefault(task, {"title": task, "status": "todo", "owner": agent,
                                "scope": scope, "deps": [], "created_at": now, "updated_at": now})
    e["status"] = "in_progress"
    e["owner"] = agent
    if scope:
        e["scope"] = scope
    e["updated_at"] = now
    _save_board(root, board)
    _update_tasks_index(root, task, status="in_progress", owner=agent, scope=e.get("scope"), title=e.get("title"))
    _set_task_doc_status(root, task, "in_progress")
    # Save the read receipt after all start-side document/status writes.
    _save_session(root, {"task": task, "agent": agent, "started_at": now,
                         "scope": scope, "notes": [], "doc_hashes": _hash_docs(root, task)})
    print(f"agentctl: started {task} (agent={agent}) -> in_progress")
    _print_focus(root, task)
    _run_loop_checkpoint(root, "work-start", once=True, trigger="work-start", strict=False)
    return 0


def cmd_focus(args: argparse.Namespace) -> int:
    root = _repo_root()
    task = args.task or _load_session(root).get("task")
    if not task:
        print("agentctl: no active task. pass --task or run 'agentctl work --agent <name>'.", file=sys.stderr)
        return 2
    _print_focus(root, task)
    return 0


def cmd_progress(args: argparse.Namespace) -> int:
    root = _repo_root()
    st = _require_session(root)
    note = args.note or ""
    if not note:
        print("agentctl: --note is required", file=sys.stderr)
        return 2
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
    print(f"agentctl: progress recorded for {st['task']}")
    return 0


def cmd_note(args: argparse.Namespace) -> int:
    note = args.note
    if isinstance(note, list):
        note = " ".join(note)
    return cmd_progress(argparse.Namespace(note=note))


def cmd_complete(args: argparse.Namespace) -> int:
    root = _repo_root()
    st = _require_session(root)
    task = st["task"]
    summary = args.summary or ""
    if not summary:
        print("agentctl: --summary is required", file=sys.stderr)
        return 2
    tests = args.tests or ""
    rc = _run_loop_checkpoint(root, "pre-finish", once=True, trigger="pre-finish", strict=True)
    if rc:
        return rc
    ts = _now()
    task_doc = root / WORKFLOW_DIR / TASKS_DIR / f"{task}.md"
    if task_doc.is_file():
        body = _read(task_doc)
        record = f"- Summary: {summary}\n"
        if tests:
            record += f"- Tests: {tests}\n"
        record += f"- Completed-at: {ts}\n"
        i = body.find("## Completion Record")
        if i >= 0:
            body = body[:i] + "## Completion Record\n" + record
        else:
            body += "\n## Completion Record\n" + record
        body = re.sub(r"^Status: .*$", "Status: review", body, count=1, flags=re.M)
        _write(task_doc, body)
    board = _load_board(root)
    t = board.get("tasks", {}).get(task)
    if t:
        t["status"] = "review"
        t["updated_at"] = ts
        _save_board(root, board)
        _update_tasks_index(root, task, status="review", owner=t.get("owner"), scope=t.get("scope"), title=t.get("title"))
    lp = _lock_path(root, task)
    if lp.is_file():
        lp.unlink()
    st["status"] = "review"
    st["completed_at"] = ts
    st["doc_hashes"] = _hash_docs(root, task)
    _save_session(root, st)
    print(f"agentctl: {task} -> review. optional review gate: agentctl gate approve --task {task} --by <reviewer>")
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
    return cmd_complete(argparse.Namespace(summary=summary, tests=tests))


def cmd_gate(args: argparse.Namespace) -> int:
    root = _repo_root()
    task = args.task
    board = _load_board(root)
    t = board.get("tasks", {}).get(task)
    if not t:
        print(f"agentctl: task {task} not found on board", file=sys.stderr)
        return 2
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
        _write(gate_doc, f"# Gate {task}\n\n- Decision: approved\n- By: {args.by}\n- At: {ts}\n- Note: {args.note or ''}\n")
        st = _load_session(root)
        if st.get("task") == task:
            st["status"] = "done"
            st["gated_at"] = ts
            st["doc_hashes"] = _hash_docs(root, task)
            _save_session(root, st)
        print(f"agentctl: {task} approved -> done")
        return 0
    t["status"] = "blocked"
    t["updated_at"] = ts
    _save_board(root, board)
    _set_task_doc_status(root, task, "blocked")
    _update_tasks_index(root, task, status="blocked", owner=t.get("owner"), scope=t.get("scope"), title=t.get("title"))
    _write(gate_doc, f"# Gate {task}\n\n- Decision: rejected\n- By: {args.by}\n- At: {ts}\n- Note: {args.note or ''}\n")
    st = _load_session(root)
    if st.get("task") == task:
        st["status"] = "blocked"
        st["gated_at"] = ts
        st["doc_hashes"] = _hash_docs(root, task)
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
    task = args.id
    title = args.title or task
    owner = args.owner or ""
    scope = [s.strip() for s in (args.scope or "").split(",") if s.strip()]
    deps = [d.strip() for d in (args.deps or "").split(",") if d.strip()]
    now = _now()
    board = _load_board(root)
    if task in board.get("tasks", {}) and not args.force:
        print(f"agentctl: task {task} already exists (use --force)", file=sys.stderr)
        return 1
    board.setdefault("tasks", {})[task] = {"title": title, "status": "todo",
                                           "owner": owner or None, "scope": scope, "deps": deps,
                                           "created_at": now, "updated_at": now}
    _save_board(root, board)
    doc = root / WORKFLOW_DIR / TASKS_DIR / f"{task}.md"
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
            print(f"  {aid:<14} role={a.get('role', '-')} backend={a.get('backend', '-')} scope={scope}")
        return 0
    if args.agents_action == "add":
        data.setdefault("agents", {})[args.id] = {
            "role": args.role or "",
            "backend": args.backend or "any",
            "write_scope": [s.strip() for s in (args.scope or "").split(",") if s.strip()],
            "tools": [t.strip() for t in (args.tools or "").split(",") if t.strip()],
            "model": args.model or "",
        }
        _save_agents(root, data)
        print(f"agentctl: registered agent {args.id}")
        return 0
    print("agentctl: unknown agents action", file=sys.stderr)
    return 2


def _packet_id(from_task: str, to_task: str) -> str:
    ts = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
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
                           reports: list[str], strict: bool) -> tuple[str, bool]:
    """Create (or refresh) the follow-up packet for a failed checkpoint.

    Returns (packet_id, created). Deduplicates per checkpoint: re-running a
    still-failing checkpoint updates the existing open packet instead of
    flooding the inbox.
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
        _save_json(path, pkt)
        outbox = _bus_dir(root, BUS_OUTBOX) / (pkt.get("by") or "loop-engine") / f"{pkt['id']}.json"
        if outbox.is_file():
            _save_json(outbox, pkt)
        return pkt["id"], False
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
    outbox, inbox = _packet_paths(root, packet)
    _save_json(outbox, packet)
    _save_json(inbox, packet)
    _append_handoff_doc(root, packet)
    return packet["id"], True


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


def _checkpoint_recent(state: dict, checkpoint: str, debounce_minutes: int) -> bool:
    if debounce_minutes <= 0:
        return False
    entry = (state.get("checkpoints") or {}).get(checkpoint) or {}
    last = _parse_time(entry.get("last_run_at"))
    if not last:
        return False
    return (_dt.datetime.now() - last).total_seconds() < debounce_minutes * 60


def _acquire_lock_file(path: Path, timeout_seconds: float = 10.0, stale_seconds: float = 300.0) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout_seconds
    payload = f"pid={os.getpid()} acquired_at={_now()}\n".encode("utf-8")
    while True:
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, payload)
            return fd
        except FileExistsError:
            try:
                if time.time() - path.stat().st_mtime > stale_seconds:
                    path.unlink()
                    continue
            except FileNotFoundError:
                continue
            if time.monotonic() >= deadline:
                raise TimeoutError(f"timed out waiting for lock {path}")
            time.sleep(0.05)


def _release_lock_file(path: Path, fd: int) -> None:
    try:
        os.close(fd)
    finally:
        try:
            path.unlink()
        except FileNotFoundError:
            pass


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
        if len(cells) >= 6 and TASK_ID_RE.fullmatch(cells[0]):
            rows[cells[0]] = {"status": cells[1], "owner": cells[2], "scope": cells[3], "title": cells[5]}
    return rows


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
        feedback.append(
            f"open loop follow-up {pkt.get('id')} (checkpoint={pkt.get('checkpoint')}, "
            f"occurrences={pkt.get('occurrences', 1)}): {pkt.get('summary')}"
        )
        next_steps.append(
            f"Resolve follow-up {pkt.get('id')}: fix the reported checks, then re-run "
            f"'agentctl loop auto --checkpoint {pkt.get('checkpoint')} --once --force' to auto-close it."
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
        result = _loop_contract_only(root, loop_id, missing)
    return _attach_previous_run(root, loop_id, result)


def _write_loop_report(root: Path, loop_id: str, trigger: str, result: dict) -> Path:
    ts = _dt.datetime.now()
    stamp = ts.strftime("%Y%m%d-%H%M%S")
    base = _loop_runs_dir(root) / f"{stamp}-{loop_id}"
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


def _loop_run(root: Path, args: argparse.Namespace) -> int:
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


def _run_loop_checkpoint(root: Path, checkpoint: str, *, once: bool, trigger: str,
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
    state = _load_json(_loop_state_path(root), {"version": 1, "loops": {}, "checkpoints": {}})
    if _checkpoint_recent(state, checkpoint, debounce) and not force:
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
    closed_follow_ups: list[str] = []
    if failing:
        follow_up_id, follow_up_created = _create_loop_follow_up(
            root, checkpoint, aggregate, reports, strict_effective)
    elif aggregate == "success":
        closed_follow_ups = _close_loop_follow_ups(
            root, checkpoint, f"checkpoint {checkpoint} succeeded (trigger={trigger})")
    def update(state: dict) -> None:
        state.setdefault("checkpoints", {})[checkpoint] = {
            "last_run_at": _now(),
            "last_status": aggregate,
            "last_reports": reports,
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
        for pid in closed_follow_ups:
            print(f"agentctl: follow-up packet auto-closed: {pid}")
    if aggregate in {"failed", "blocked"}:
        return 1
    if strict_effective and aggregate == "partial":
        print(f"agentctl: checkpoint {checkpoint} is strict and reported partial results.", file=sys.stderr)
        return 1
    return 0


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
    return p


def _check_receipt(root: Path) -> list:
    st = _load_session(root)
    if not st.get("task"):
        return []
    cur = _hash_docs(root, st["task"])
    old = st.get("doc_hashes", {})
    changed = [k for k, v in cur.items() if old.get(k) != v]
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


def _check_precommit(root: Path) -> list:
    p = []
    st = _load_session(root)
    staged = [f for f in _git(root, "diff", "--cached", "--name-only").splitlines() if f.strip()]
    if staged and not st.get("task"):
        p.append("staged changes but no active task (run agentctl work --agent <name>)")
    if staged:
        agent_docs = [f for f in staged if f.startswith(".agent/") or f == "AGENTS.md"]
        nondoc = [f for f in staged if not (f.startswith(".agent/") or f == "AGENTS.md")]
        if nondoc and not agent_docs:
            p.append("code/data staged but no .agent task/plan/log update staged; run agentctl note.")
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


def _check_prepush(root: Path, commit_range: str | None) -> list:
    if not commit_range:
        return ["pre-push mode requires --commit-range"]
    rev_args = [commit_range]
    baseline = _load_json(_adoption_path(root), {}).get("ignore_commits_through")
    if baseline and _git(root, "rev-parse", "--verify", f"{baseline}^{{commit}}").strip():
        rev_args.append(f"^{baseline}")
    log = _git(root, "log", "--format=%H%x1f%s%x1f%b%x1e", *rev_args)
    if not log:
        return []
    p = []
    tasks = _load_board(root).get("tasks", {})
    seen = set()
    for rec in log.split("\x1e"):
        rec = rec.strip()
        if not rec:
            continue
        parts = rec.split("\x1f")
        subject = parts[1] if len(parts) > 1 else ""
        body = parts[2] if len(parts) > 2 else ""
        if not CONVENTIONAL_RE.match(subject.strip()):
            p.append(f"commit not Conventional: '{subject.strip()}'")
        ids = TASK_ID_RE.findall(subject + " " + body)
        if not ids:
            p.append(f"commit missing task ID: '{subject.strip()}'")
        seen.update(ids)
    for tid in sorted(seen):
        t = tasks.get(tid)
        if not t:
            p.append(f"task {tid} referenced in commits but not on board")
            continue
        if t.get("status") not in PUSHABLE_STATUSES:
            p.append(f"task {tid} is '{t.get('status')}', must be review/approved/done before push")
        if t.get("status") == "done":
            rec = _extract_section(_read(root / WORKFLOW_DIR / TASKS_DIR / f"{tid}.md"), "## Completion Record")
            if "Completed-at:" not in rec:
                p.append(f"task {tid} is done but task doc has no completion record")
            if not _plan_checked(root, tid):
                p.append(f"task {tid} is done but not checked off in PROJECT_PLAN.md")
    return p


def _check_board_consistency(root: Path) -> list:
    p = []
    for tid, t in _load_board(root).get("tasks", {}).items():
        if t.get("status") not in STATUSES:
            p.append(f"task {tid} has invalid status '{t.get('status')}'")
        if t.get("status") in ACTIVE_STATUSES and not t.get("owner"):
            p.append(f"task {tid} is in_progress but has no owner")
    return p


def cmd_check(args: argparse.Namespace) -> int:
    root = _repo_root()
    mode = args.mode or "manual"
    if mode == "manual":
        problems = _check_base(root) + _check_receipt(root)
    elif mode == "pre-commit":
        problems = _check_base(root) + _check_precommit(root)
    elif mode == "commit-msg":
        problems = _check_commit_msg(root, args.message_file)
    elif mode == "pre-push":
        problems = _check_prepush(root, args.commit_range)
    elif mode == "ci":
        problems = _check_base(root) + _check_board_consistency(root)
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
    sp.add_argument("--force", action="store_true")
    sp.set_defaults(func=cmd_work)

    sp = sub.add_parser("start")
    sp.add_argument("--task", required=True)
    sp.add_argument("--agent")
    sp.add_argument("--scope")
    sp.add_argument("--force", action="store_true")
    sp.set_defaults(func=cmd_start)

    sp = sub.add_parser("focus")
    sp.add_argument("--task")
    sp.set_defaults(func=cmd_focus)

    sp = sub.add_parser("progress")
    sp.add_argument("--note")
    sp.set_defaults(func=cmd_progress)

    sp = sub.add_parser("note")
    sp.add_argument("note", nargs="+")
    sp.set_defaults(func=cmd_note)

    sp = sub.add_parser("complete")
    sp.add_argument("--summary")
    sp.add_argument("--tests")
    sp.set_defaults(func=cmd_complete)

    sp = sub.add_parser("finish")
    sp.add_argument("--summary")
    sp.add_argument("--tests")
    sp.set_defaults(func=cmd_finish)

    sp = sub.add_parser("gate")
    sp.add_argument("action", choices=["approve", "reject"])
    sp.add_argument("--task", required=True)
    sp.add_argument("--by", required=True)
    sp.add_argument("--note")
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
    c.add_argument("--force", action="store_true")
    sh = tsub.add_parser("show")
    sh.add_argument("id")
    sp.set_defaults(func=cmd_task)

    sp = sub.add_parser("agents")
    asub = sp.add_subparsers(dest="agents_action", required=True)
    aa = asub.add_parser("add")
    aa.add_argument("--id", required=True)
    aa.add_argument("--role")
    aa.add_argument("--backend")
    aa.add_argument("--scope")
    aa.add_argument("--tools")
    aa.add_argument("--model")
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
    sp.set_defaults(func=cmd_loop)

    sp = sub.add_parser("check")
    sp.add_argument("--mode", default="manual")
    sp.add_argument("--message-file")
    sp.add_argument("--commit-range")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_check)

    sp = sub.add_parser("status")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_status)

    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
