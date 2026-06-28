#!/usr/bin/env python3
"""agentctl: task / session / board controller for the Agent Workflow Kit.

Dependency-free (stdlib only), Python 3.8+.

Commands:
  init       scaffold workflow files; distribute agentctl.py + git hooks
  start      begin a task session: read receipt + lock + board -> in_progress
  focus      print the active task focus (goal/scope/todo) -- re-read anytime
  progress   append a progress note to the active task
  complete   move the active task to review (write completion record, free lock)
  gate       approve/reject a task in review (-> done / blocked)
  handoff    create/list/show/close cross-agent task packets
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
from pathlib import Path

WORKFLOW_DIR = ".agent"
STATE_DIR = "state"
SESSION_FILE = "current_session.json"
LOCKS_DIR = "locks"
BOARD_FILE = "board.json"
AGENTS_FILE = "agents.json"
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
        print("agentctl: no active task. run 'agentctl start --task <id>' first.", file=sys.stderr)
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


def _load_agents(root: Path) -> dict:
    return _load_json(_agents_path(root), {"version": 1, "agents": {}})


def _save_agents(root: Path, data: dict) -> None:
    _save_json(_agents_path(root), data)


def _bus_dir(root: Path, kind: str) -> Path:
    return root / WORKFLOW_DIR / BUS_DIR / kind


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


def _plan_checked(root: Path, task: str) -> bool:
    text = _read(root / WORKFLOW_DIR / PLAN_FILE)
    return re.search(rf"- \[x\][^\n]*{re.escape(task)}", text) is not None


def _check_plan_box(root: Path, task: str) -> None:
    plan = root / WORKFLOW_DIR / PLAN_FILE
    text = _read(plan)
    new = re.sub(rf"- \[ \]([^\n]*{re.escape(task)}[^\n]*)", r"- [x]\1", text, count=1)
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
    # read receipt
    _save_session(root, {"task": task, "agent": agent, "started_at": _now(),
                         "scope": scope, "notes": [], "doc_hashes": _hash_docs(root, task)})
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
    print(f"agentctl: started {task} (agent={agent}) -> in_progress")
    _print_focus(root, task)
    return 0


def cmd_focus(args: argparse.Namespace) -> int:
    root = _repo_root()
    task = args.task or _load_session(root).get("task")
    if not task:
        print("agentctl: no active task. pass --task or run 'agentctl start'.", file=sys.stderr)
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


def cmd_complete(args: argparse.Namespace) -> int:
    root = _repo_root()
    st = _require_session(root)
    task = st["task"]
    summary = args.summary or ""
    if not summary:
        print("agentctl: --summary is required", file=sys.stderr)
        return 2
    tests = args.tests or ""
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
    print(f"agentctl: {task} -> review. run 'agentctl gate approve --task {task} --by <reviewer>' to finish.")
    return 0


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
            print(f"agentctl: {task} has no completion record; run 'agentctl complete' first.", file=sys.stderr)
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
    if "| ID |" in ttext and task not in ttext:
        _write(tasks_md, ttext.rstrip("\n") + "\n" + row + "\n")
    elif task in ttext:
        _update_tasks_index(root, task, status="todo", owner=owner or "-", scope=scope, title=title)
    plan = root / WORKFLOW_DIR / PLAN_FILE
    ptext = _read(plan)
    bullet = f"- [ ] {task} - {title}" + (f" (owner: {owner})" if owner else "")
    if "## Task Board" in ptext and task not in ptext:
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


def _check_base(root: Path) -> list:
    p = []
    if not (root / "AGENTS.md").is_file():
        p.append("missing AGENTS.md")
    if not (root / WORKFLOW_DIR / PLAN_FILE).is_file():
        p.append(f"missing {WORKFLOW_DIR}/{PLAN_FILE}")
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
        p.append("staged changes but no active task (run agentctl start)")
    if staged:
        agent_docs = [f for f in staged if f.startswith(".agent/") or f == "AGENTS.md"]
        nondoc = [f for f in staged if not (f.startswith(".agent/") or f == "AGENTS.md")]
        if nondoc and not agent_docs:
            p.append("code/data staged but no .agent task/plan/log update staged; run agentctl progress.")
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
    log = _git(root, "log", "--format=%H%x1f%s%x1f%b%x1e", commit_range)
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

    sp = sub.add_parser("complete")
    sp.add_argument("--summary")
    sp.add_argument("--tests")
    sp.set_defaults(func=cmd_complete)

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
