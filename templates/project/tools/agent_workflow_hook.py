#!/usr/bin/env python3
"""Lifecycle hook bridge for agent workflow enforcement.

This script is intentionally conservative and stdlib-only. It is invoked by
Codex/Claude/Cursor-native hook configs where supported.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path


MUTATING_BASH = re.compile(
    r"(^|\s)("
    r"git\s+(add|commit|push|merge|rebase|reset|checkout|switch|clean)|"
    r"rm\s+-|mv\s+|cp\s+|mkdir\s+|touch\s+|"
    r"sed\s+-i|perl\s+-pi|"
    r"npm\s+install|pnpm\s+add|yarn\s+add|pip\s+install|"
    r"cat\s+>|tee\s+|echo\s+.*>|apply_patch"
    r")\b",
    re.IGNORECASE,
)
GIT_WRITE_BASH = re.compile(
    r"(^|\s)git\s+(add|commit|push|merge|rebase|reset|checkout|switch|clean)\b",
    re.IGNORECASE,
)


def run(cmd: list[str], cwd: Path, env: dict | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd, cwd=str(cwd), env=env, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )


def find_root() -> Path:
    result = run(["git", "rev-parse", "--show-toplevel"], Path.cwd())
    if result.returncode == 0 and result.stdout.strip():
        return Path(result.stdout.strip())
    current = Path.cwd().resolve()
    for path in [current, *current.parents]:
        if (path / ".agent").is_dir():
            return path
    return current


def read_input() -> dict:
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def emit_context(event: str, message: str, env_vars: dict | None = None) -> None:
    payload = {
        "additional_context": message,
        "hookSpecificOutput": {
            "hookEventName": event,
            "additionalContext": message,
        },
    }
    if env_vars:
        payload["env"] = env_vars
    print(json.dumps(payload, ensure_ascii=False))


def block(event: str, reason: str) -> int:
    print(
        json.dumps(
            {
                "decision": "block",
                "reason": reason,
                "permission": "deny",
                "user_message": reason,
                "agent_message": reason,
                "hookSpecificOutput": {
                    "hookEventName": event,
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason,
                },
            },
            ensure_ascii=False,
        )
    )
    return 0


def payload_session_id(payload: dict) -> str:
    for key in ("session_id", "sessionId", "conversation_id", "conversationId", "thread_id", "threadId"):
        value = payload.get(key)
        if value:
            return str(value)
    return ""


def hook_environment(payload: dict) -> dict:
    env = os.environ.copy()
    session_id = payload_session_id(payload)
    if session_id:
        env["AGENT_WORKFLOW_SESSION_ID"] = session_id
    return env


def persist_session_environment(session_id: str) -> dict:
    if not session_id:
        return {}
    env_vars = {"AGENT_WORKFLOW_SESSION_ID": session_id}
    env_file = (os.environ.get("CLAUDE_ENV_FILE") or "").strip()
    if env_file:
        path = Path(env_file).expanduser()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(
                    "export AGENT_WORKFLOW_SESSION_ID="
                    + shlex.quote(session_id) + "\n"
                )
        except OSError:
            pass
    return env_vars


def session_state(root: Path, env: dict) -> dict:
    agentctl = root / "tools" / "agentctl.py"
    if not agentctl.exists():
        return {}
    result = run([sys.executable, str(agentctl), "status", "--json"], root, env)
    if result.returncode != 0:
        return {}
    try:
        state = json.loads(result.stdout or "{}")
        return state if isinstance(state, dict) else {}
    except json.JSONDecodeError:
        return {}


def has_session(root: Path, env: dict) -> bool:
    state = session_state(root, env)
    return bool(
        state.get("task")
        and state.get("presence_status") != "released"
        and state.get("status") != "released"
    )


def session_completed(root: Path, env: dict) -> bool:
    st = session_state(root, env)
    return bool(st.get("completed_at") or st.get("status") in {"review", "done"})


def check_manual(root: Path, env: dict) -> tuple[bool, str]:
    agentctl = root / "tools" / "agentctl.py"
    if not agentctl.exists():
        return False, "tools/agentctl.py is missing."
    result = run([sys.executable, str(agentctl), "check", "--mode", "manual"], root, env)
    if result.returncode == 0:
        return True, ""
    return False, (result.stderr or result.stdout).strip()


def command_is_agentctl_start(command: str) -> bool:
    return "agentctl.py" in command and re.search(r"\b(?:start|work)\b", command) is not None


def payload_command(payload: dict) -> str:
    if payload.get("command"):
        return str(payload.get("command") or "")
    return str((payload.get("tool_input") or {}).get("command") or "")


def shell_write_paths(command: str, cwd: Path | None = None) -> list[str]:
    values = []
    working_dir = (cwd or Path.cwd()).expanduser()
    discard_targets = {"/dev/null", "/dev/stdout", "/dev/stderr", "nul"}

    def add_target(raw: str) -> None:
        value = str(raw or "").strip()
        if not value or value.lower() in discard_targets:
            return
        target = Path(value).expanduser()
        if not target.is_absolute():
            target = working_dir / target
        values.append(str(target))

    for value in re.findall(
            r"^\*\*\* (?:Add|Update|Delete) File: (.+)$", command, flags=re.M):
        add_target(value)
    if not command:
        return values
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|<>")
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        return values
    segments = []
    current = []
    for token in tokens:
        if token in {";", "&&", "||", "|"}:
            if current:
                segments.append((current, token))
            current = []
        else:
            current.append(token)
    if current:
        segments.append((current, ""))
    redirect_tokens = {">", ">>", "1>", "1>>", "2>", "2>>"}
    for segment, connector in segments:
        command_tokens = []
        index = 0
        while index < len(segment):
            token = segment[index]
            if (token.isdigit() and index + 2 < len(segment)
                    and segment[index + 1] in {">", ">>"}):
                add_target(segment[index + 2])
                index += 3
                continue
            if token in redirect_tokens and index + 1 < len(segment):
                add_target(segment[index + 1])
                index += 2
                continue
            command_tokens.append(token)
            index += 1
        while command_tokens and command_tokens[0] in {"sudo", "command"}:
            command_tokens = command_tokens[1:]
        if command_tokens and command_tokens[0] == "env":
            command_tokens = command_tokens[1:]
            while command_tokens and (
                    command_tokens[0].startswith("-")
                    or re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", command_tokens[0])):
                command_tokens = command_tokens[1:]
        while command_tokens and re.match(
                r"^[A-Za-z_][A-Za-z0-9_]*=", command_tokens[0]):
            command_tokens = command_tokens[1:]
        if not command_tokens:
            continue
        executable = Path(command_tokens[0]).name
        command_args = command_tokens[1:]
        args = [item for item in command_args if not item.startswith("-")]
        if executable == "cd" and args:
            target = Path(args[0]).expanduser()
            next_dir = target if target.is_absolute() else working_dir / target
            if connector in {"&&", ";", ""}:
                working_dir = next_dir
            continue
        if executable in {"touch", "mkdir", "rm", "tee"}:
            for value in args:
                add_target(value)
        elif executable in {"cp", "mv"} and args:
            add_target(args[-1])
        elif executable in {"sed", "perl"} and args and any(
                item == "--in-place"
                or item.startswith("--in-place=")
                or (item.startswith("-") and not item.startswith("--")
                    and "i" in item[1:])
                for item in command_args):
            add_target(args[-1])
    return list(dict.fromkeys(values))


def payload_paths(payload: dict) -> list[str]:
    tool = str(payload.get("tool_name") or payload.get("tool") or payload.get("toolType") or "")
    tool_input = payload.get("tool_input") or {}
    values = []
    if tool in {"Write", "Edit", "MultiEdit", "apply_patch"}:
        for key in ("file_path", "path"):
            value = tool_input.get(key) or payload.get(key)
            if value:
                values.append(str(value))
        for edit in tool_input.get("edits") or []:
            if isinstance(edit, dict) and (edit.get("file_path") or edit.get("path")):
                values.append(str(edit.get("file_path") or edit.get("path")))
        patch = str(tool_input.get("patch") or tool_input.get("input") or "")
        values.extend(re.findall(r"^\*\*\* (?:Add|Update|Delete) File: (.+)$", patch, flags=re.M))
    elif payload_command(payload):
        cwd_value = (
            tool_input.get("cwd") or tool_input.get("workdir")
            or payload.get("cwd") or payload.get("workdir")
        )
        values.extend(shell_write_paths(
            payload_command(payload), Path(str(cwd_value)) if cwd_value else None,
        ))
    return list(dict.fromkeys(value for value in values if value.strip()))


def guard_session(root: Path, payload: dict, env: dict) -> tuple[bool, str]:
    agentctl = root / "tools" / "agentctl.py"
    command = [sys.executable, str(agentctl), "sessions", "guard"]
    for path in payload_paths(payload):
        command.extend(["--path", path])
    if GIT_WRITE_BASH.search(payload_command(payload)):
        command.append("--git-write")
    result = run(command, root, env)
    if result.returncode == 0:
        return True, result.stdout.strip()
    return False, (result.stderr or result.stdout).strip()


def heartbeat(root: Path, env: dict) -> None:
    agentctl = root / "tools" / "agentctl.py"
    run([sys.executable, str(agentctl), "sessions", "heartbeat"], root, env)


def is_finalization_action(payload: dict) -> bool:
    tool = str(payload.get("tool_name") or payload.get("tool") or payload.get("toolType") or "")
    if tool in {"Write", "Edit", "MultiEdit", "apply_patch"}:
        return False
    command = payload_command(payload)
    if not command:
        return False
    if "agentctl.py" in command:
        return True
    return re.search(r"\b(?:git\s+(?:status|diff|add|commit|push|log|show|remote)|gh\s+(?:repo|pr|issue))\b", command) is not None


def is_mutating_tool(payload: dict) -> bool:
    tool = str(payload.get("tool_name") or payload.get("tool") or payload.get("toolType") or "")
    command = payload_command(payload)
    if tool in {"Write", "Edit", "MultiEdit", "apply_patch"}:
        return True
    if command:
        if command_is_agentctl_start(command):
            return False
        return bool(payload_paths(payload)) or MUTATING_BASH.search(command) is not None
    if tool == "Bash":
        if command_is_agentctl_start(command):
            return False
        return MUTATING_BASH.search(command) is not None
    return False


def current_focus(root: Path, env: dict) -> str:
    """Return the active task focus (goal/scope/todo) so long tasks do not drift."""
    agentctl = root / "tools" / "agentctl.py"
    if not agentctl.exists() or not has_session(root, env):
        return ""
    result = run([sys.executable, str(agentctl), "focus"], root, env)
    return result.stdout.strip() if result.returncode == 0 else ""


def workflow_entry(root: Path) -> str:
    path = root / ".agent" / "WORKFLOW_ENTRY.md"
    if path.is_file():
        try:
            return path.read_text(encoding="utf-8").strip()
        except OSError:
            return ""
    return ""


def session_start() -> int:
    root = find_root()
    if not (root / ".agent").exists():
        return 0
    payload = read_input()
    env = hook_environment(payload)
    session_env = persist_session_environment(payload_session_id(payload))
    entry = workflow_entry(root)
    if entry:
        message = (
            "This repo uses Agent Workflow Kit. Read and follow "
            "`.agent/WORKFLOW_ENTRY.md` before editing. The human may only say "
            "`按 .agent 规范开始工作。`; that short prompt means the full workflow below:\n\n"
            + entry
        )
    else:
        message = (
            "This repo uses Agent Workflow Kit. Before editing, enter the autonomous work loop:\n"
            "  python3 tools/agentctl.py work --agent <agent-name>\n"
            "It will resume the current task or auto-claim the next assigned task, then print the required focus.\n"
            "If no task is assigned for the current user request, create and start one yourself:\n"
            "  python3 tools/agentctl.py work --agent <agent-name> --auto-create --title \"<current request>\" --scope \"<paths>\""
        )
    focus = current_focus(root, env)
    if focus:
        message += (
            "\n\nA task session is already active. This may be a resume or a context "
            "compaction: re-read your current focus before continuing so you do not "
            "drift from the task or the plan:\n" + focus
        )
    sessions = run(
        [sys.executable, str(root / "tools" / "agentctl.py"), "sessions", "list"],
        root, env,
    )
    if sessions.returncode == 0 and "no recorded sessions" not in sessions.stdout:
        message += (
            "\n\nCurrent multi-session status (also generated at "
            "`.agent/state/SESSIONS.md`):\n" + sessions.stdout.strip()
        )
    emit_context("SessionStart", message, session_env)
    return 0


def pre_tool_use() -> int:
    root = find_root()
    if not (root / ".agent").exists():
        return 0
    payload = read_input()
    env = hook_environment(payload)
    active = has_session(root, env)
    if not is_mutating_tool(payload):
        if active:
            heartbeat(root, env)
        return 0
    if not active:
        return block(
            "PreToolUse",
            "Agent Workflow Kit blocked this write/mutating action because no active task session exists. "
            "Read `.agent/WORKFLOW_ENTRY.md`, then run: python3 tools/agentctl.py work --agent <agent-name>. "
            "If no task exists for the current request, run: python3 tools/agentctl.py work --agent <agent-name> "
            "--auto-create --title \"<current request>\" --scope \"<paths>\".",
        )
    guarded, guard_message = guard_session(root, payload, env)
    if not guarded:
        return block(
            "PreToolUse",
            "Agent Workflow Kit blocked this action because its session/scope/file/Git claim conflicts "
            "with the current checkout. Inspect `.agent/state/SESSIONS.md` or run "
            "`python3 tools/agentctl.py sessions list`.\n" + guard_message,
        )
    ok, message = check_manual(root, env)
    if not ok:
        return block(
            "PreToolUse",
            "Agent Workflow Kit blocked this action because workflow checks failed. "
            "Re-read changed plan/rule files and run python3 tools/agentctl.py refresh if needed.\n"
            + message,
        )
    if session_completed(root, env) and not is_finalization_action(payload):
        return block(
            "PreToolUse",
            "Agent Workflow Kit blocked this mutating action because the active task has already been completed. "
            "Only finalization commands such as git add/commit/push, gh pr/repo, or agentctl status/board/gate/check are allowed. "
            "Start a new task before making more code or document edits.",
        )
    if guard_message:
        emit_context(
            "PreToolUse",
            "Agent Workflow Kit detected a changed peer-session snapshot. Re-read the live "
            "coordination view before continuing:\n" + guard_message,
        )
    return 0


def stop() -> int:
    root = find_root()
    if not (root / ".agent").exists():
        return 0
    payload = read_input()
    env = hook_environment(payload)
    if not has_session(root, env):
        return 0
    heartbeat(root, env)
    ok, message = check_manual(root, env)
    reminder = (
        "Agent Workflow Kit: a task session is still active. Before you stop, either "
        "record progress (python3 tools/agentctl.py note ...) or finish the "
        "task (python3 tools/agentctl.py finish --summary ... --tests ...), which "
        "updates the plan, task doc, and board so the next agent can pick up cleanly."
    )
    if not ok:
        reminder += (
            "\nAlso resolve these workflow issues (run agentctl refresh if plan/rules "
            "changed):\n" + message
        )
    emit_context("Stop", reminder)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Agent Workflow Kit lifecycle hook bridge.")
    parser.add_argument("event", choices=["session-start", "pre-tool-use", "stop"])
    args = parser.parse_args()
    if args.event == "session-start":
        return session_start()
    if args.event == "pre-tool-use":
        return pre_tool_use()
    if args.event == "stop":
        return stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
