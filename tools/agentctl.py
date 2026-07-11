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
  guidance   create/list/show/ack/dispatch supervisor guidance packets
  handoff    create/list/show/close cross-agent task packets
  loop       run/status/resume/stop bounded project loops and checkpoints
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
import errno
import hashlib
import json
import math
import os
import platform
import re
import signal
import shlex
import shutil
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
REASONING_EFFORT_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")

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
        return True
    except PermissionError:
        return True
    except ProcessLookupError:
        return False
    except OSError as exc:
        return exc.errno == errno.EPERM
    except (OverflowError, ValueError, TypeError):
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
                    )
                )
            else:
                targets.extend(path for path, _pkt in _open_guidance_packets(root, task=task))
        except NameError:
            pass
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


def _print_focus(root: Path, task: str, agent: str | None = None,
                 session_id: str = "", model: str = "") -> None:
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
    if agent:
        _print_guidance_focus(root, agent, task, session_id=session_id, model=model)
    print("Required reading: AGENTS.md, .agent/PROJECT_PLAN.md, and the task doc above.")
    print("=== end focus ===")


def cmd_work(args: argparse.Namespace) -> int:
    root = _repo_root()
    agent = args.agent or os.environ.get("AGENT_NAME", "unknown")
    meta = _resolve_worker_metadata(root, agent, args)
    if args.task:
        start_args = argparse.Namespace(task=args.task, agent=agent, scope=args.scope, force=args.force,
                                        session_id=meta["session_id"], model=meta["model"],
                                        reasoning_effort=meta["reasoning_effort"])
        return cmd_start(start_args)
    st = _load_session(root)
    active = st.get("task")
    if active and _task_status(root, active) == "in_progress" and not args.force:
        print(f"agentctl: resuming active task {active} (agent={st.get('agent') or agent})")
        _print_focus(root, active, st.get("agent") or agent,
                     session_id=st.get("session_id") or meta["session_id"],
                     model=st.get("model") or meta["model"])
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
            start_args = argparse.Namespace(task=task, agent=agent, scope=args.scope, force=args.force,
                                            session_id=meta["session_id"], model=meta["model"],
                                            reasoning_effort=meta["reasoning_effort"])
            return cmd_start(start_args)
        print(f"agentctl: no ready/todo task assigned to {agent}.")
        print("agentctl: if this is a new user request, create and start a task in one command:")
        print("  python3 tools/agentctl.py work --agent " + agent + " --auto-create --title \"...\" --scope path/")
        return 1
    print(f"agentctl: auto-selected {task} for {agent}")
    start_args = argparse.Namespace(task=task, agent=agent, scope=args.scope, force=args.force,
                                    session_id=meta["session_id"], model=meta["model"],
                                    reasoning_effort=meta["reasoning_effort"])
    return cmd_start(start_args)


def cmd_start(args: argparse.Namespace) -> int:
    root = _repo_root()
    if not (root / WORKFLOW_DIR / PLAN_FILE).is_file():
        print(f"agentctl: missing {WORKFLOW_DIR}/{PLAN_FILE}. run 'agentctl init' first.", file=sys.stderr)
        return 2
    task = args.task
    agent = args.agent or os.environ.get("AGENT_NAME", "unknown")
    meta = _resolve_worker_metadata(root, agent, args)
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
                    "scope": scope, "session_id": meta["session_id"], "model": meta["model"],
                    "reasoning_effort": meta["reasoning_effort"],
                    "acquired_at": _now()})
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
    session = {"task": task, "agent": agent, "started_at": now,
               "scope": scope, "session_id": meta["session_id"], "model": meta["model"],
               "reasoning_effort": meta["reasoning_effort"],
               "notes": [], "doc_hashes": {}}
    _save_session(root, session)
    session["doc_hashes"] = _hash_docs(root, task)
    _save_session(root, session)
    label = f"agent={agent}"
    if meta["model"]:
        label += f" model={meta['model']}"
    if meta["reasoning_effort"]:
        label += f" reasoning={meta['reasoning_effort']}"
    if meta["session_id"]:
        label += f" session={meta['session_id']}"
    print(f"agentctl: started {task} ({label}) -> in_progress")
    _print_focus(root, task, agent, session_id=meta["session_id"], model=meta["model"])
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
                 model=getattr(args, "model", None) or st.get("model") or meta["model"])
    return 0


def cmd_progress(args: argparse.Namespace) -> int:
    root = _repo_root()
    st = _require_session(root)
    changed = _check_receipt(root)
    if changed:
        print("agentctl: progress blocked because required workflow documents changed:", file=sys.stderr)
        for problem in changed:
            print(f"  - {problem}", file=sys.stderr)
        return 1
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
    changed = _check_receipt(root)
    if changed:
        print("agentctl: finish blocked because required workflow documents changed:", file=sys.stderr)
        for problem in changed:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    summary = args.summary or ""
    if not summary:
        print("agentctl: --summary is required", file=sys.stderr)
        return 2
    pending_guidance = _open_guidance_packets(
        root,
        to_agent=st.get("agent"),
        task=task,
        task_specific_only=True,
        session_id=st.get("session_id") or "",
        model=st.get("model") or "",
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
    tests = args.tests or ""
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
    return cmd_complete(argparse.Namespace(summary=summary, tests=tests,
                                           ack_escalations=bool(getattr(args, "ack_escalations", False))))


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
                             model: str | None = None) -> bool:
    if to_agent and pkt.get("to_agent") != to_agent:
        return False
    target_session = pkt.get("to_session") or ""
    if target_session and target_session != (session_id or ""):
        return False
    target_model = pkt.get("to_model") or ""
    if target_model and model and target_model != model:
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
                           model: str | None = None) -> list[tuple[Path, dict]]:
    packets: list[tuple[Path, dict]] = []
    inbox = _bus_dir(root, BUS_INBOX)
    if not inbox.is_dir():
        return packets
    for path in sorted(inbox.rglob("*.json")):
        pkt = _load_json(path, {})
        if pkt.get("kind") != GUIDANCE_KIND or pkt.get("status") != "ready":
            continue
        if not _guidance_matches_worker(pkt, to_agent=to_agent, session_id=session_id, model=model):
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
    command.extend(["--output-last-message", str(last_message), session_id, prompt])

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
    running = {
        "transport": transport,
        "status": "running",
        "attempts": attempts,
        "started_at": started_at,
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
    try:
        proc = subprocess.run(
            command,
            cwd=str(root),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
        stdout = proc.stdout or ""
        stderr = proc.stderr or ""
        exit_code = proc.returncode
        if exit_code:
            failure = f"codex exited with status {exit_code}"
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout if isinstance(exc.stdout, str) else ""
        stderr = exc.stderr if isinstance(exc.stderr, str) else ""
        failure = f"codex dispatch timed out after {timeout}s"
        exit_code = 124
    except OSError as exc:
        failure = f"failed to start codex: {exc}"
        stderr = str(exc)
        exit_code = 1

    finished_at = _now()
    receipt = {
        "version": 1,
        "packet": packet["id"],
        "transport": transport,
        "status": "succeeded" if exit_code == 0 else "failed",
        "attempts": attempts,
        "started_at": started_at,
        "finished_at": finished_at,
        "exit_code": exit_code,
        "session_id": session_id,
        "model": model,
        "reasoning_effort": reasoning_effort,
        "last_message": str(last_message.relative_to(root)),
        "failure": failure,
        "stdout_tail": _dispatch_output_tail(stdout),
        "stderr_tail": _dispatch_output_tail(stderr),
    }
    _save_json(state_dir / f"{_safe_segment(packet['id'])}.json", receipt)
    persistent = {key: receipt[key] for key in (
        "transport", "status", "attempts", "started_at", "finished_at", "exit_code",
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


def _print_guidance_focus(root: Path, agent: str, task: str,
                          session_id: str = "", model: str = "") -> None:
    packets = _open_guidance_packets(root, to_agent=agent, task=task,
                                     session_id=session_id, model=model)
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
    pkt["status"] = "done"
    pkt["updated_at"] = _now()
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


def _checkpoint_recent(state: dict, checkpoint: str, debounce_minutes: int) -> bool:
    if debounce_minutes <= 0:
        return False
    entry = (state.get("checkpoints") or {}).get(checkpoint) or {}
    last = _parse_time(entry.get("last_run_at"))
    if not last:
        return False
    return (_dt.datetime.now() - last).total_seconds() < debounce_minutes * 60


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
    try:
        if proc.poll() is None:
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


def _loop_run_commands(root: Path, loop_id: str, spec: dict) -> dict:
    """Execute the commands a custom loop contract declares; exit codes decide status."""
    checks = []
    feedback = []
    failed = 0
    stopped_early = False
    for cmd in spec["commands"]:
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
        ".agent/rules/github-standards.md",
        ".githooks/pre-commit",
        ".githooks/commit-msg",
        ".githooks/pre-push",
        ".codex/hooks.json",
        ".claude/settings.json",
        ".cursor/hooks.json",
        ".github/workflows/agent-workflow-check.yml",
    ]


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
    else:
        warnings.append("not a Git repository; local Git hooks are not active")
        checks.append({"name": "git hooks", "status": "warn", "detail": "not a Git repository"})

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

    manual_problems = _check_base(root) + _check_receipt(root) + _check_escalations(root)
    if manual_problems:
        problems.extend(manual_problems)
    checks.append({
        "name": "manual check",
        "status": "ok" if not manual_problems else "fail",
        "detail": "agentctl check --mode manual equivalent",
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
        problems = _check_base(root) + _check_receipt(root) + _check_escalations(root) + _check_pending_guidance(root)
    elif mode == "pre-commit":
        problems = _check_base(root) + _check_precommit(root)
    elif mode == "commit-msg":
        problems = _check_commit_msg(root, args.message_file)
    elif mode == "pre-push":
        problems = _check_prepush(root, args.commit_range)
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
    sp.add_argument("--session-id")
    sp.add_argument("--model")
    sp.add_argument("--reasoning-effort")
    sp.add_argument("--force", action="store_true")
    sp.set_defaults(func=cmd_work)

    sp = sub.add_parser("start")
    sp.add_argument("--task", required=True)
    sp.add_argument("--agent")
    sp.add_argument("--scope")
    sp.add_argument("--session-id")
    sp.add_argument("--model")
    sp.add_argument("--reasoning-effort")
    sp.add_argument("--force", action="store_true")
    sp.set_defaults(func=cmd_start)

    sp = sub.add_parser("focus")
    sp.add_argument("--task")
    sp.add_argument("--agent")
    sp.add_argument("--session-id")
    sp.add_argument("--model")
    sp.add_argument("--reasoning-effort")
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
    sp.add_argument("--ack-escalations", action="store_true", dest="ack_escalations")
    sp.set_defaults(func=cmd_complete)

    sp = sub.add_parser("finish")
    sp.add_argument("--summary")
    sp.add_argument("--tests")
    sp.add_argument("--ack-escalations", action="store_true", dest="ack_escalations")
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
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_check)

    sp = sub.add_parser("doctor")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_doctor)

    sp = sub.add_parser("status")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_status)

    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
