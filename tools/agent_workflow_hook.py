#!/usr/bin/env python3
"""Lifecycle hook bridge for agent workflow enforcement.

This script is intentionally conservative and stdlib-only. It is invoked by
Codex/Claude/Cursor-native hook configs where supported.
"""

from __future__ import annotations

import argparse
import json
import re
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


def run(cmd: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=str(cwd), text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


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


def emit_context(event: str, message: str) -> None:
    print(
        json.dumps(
            {
                "additional_context": message,
                "hookSpecificOutput": {
                    "hookEventName": event,
                    "additionalContext": message,
                }
            },
            ensure_ascii=False,
        )
    )


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


def has_session(root: Path) -> bool:
    return (root / ".agent" / "state" / "current_session.json").exists()


def session_state(root: Path) -> dict:
    path = root / ".agent" / "state" / "current_session.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def session_completed(root: Path) -> bool:
    st = session_state(root)
    return bool(st.get("completed_at") or st.get("status") in {"review", "done"})


def check_manual(root: Path) -> tuple[bool, str]:
    agentctl = root / "tools" / "agentctl.py"
    if not agentctl.exists():
        return False, "tools/agentctl.py is missing."
    result = run(["python3", str(agentctl), "check", "--mode", "manual"], root)
    if result.returncode == 0:
        return True, ""
    return False, (result.stderr or result.stdout).strip()


def command_is_agentctl_start(command: str) -> bool:
    return "agentctl.py" in command and re.search(r"\b(?:start|work)\b", command) is not None


def payload_command(payload: dict) -> str:
    if payload.get("command"):
        return str(payload.get("command") or "")
    return str((payload.get("tool_input") or {}).get("command") or "")


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
        return MUTATING_BASH.search(command) is not None
    if tool == "Bash":
        if command_is_agentctl_start(command):
            return False
        return MUTATING_BASH.search(command) is not None
    return False


def current_focus(root: Path) -> str:
    """Return the active task focus (goal/scope/todo) so long tasks do not drift."""
    session = root / ".agent" / "state" / "current_session.json"
    agentctl = root / "tools" / "agentctl.py"
    if not session.exists() or not agentctl.exists():
        return ""
    result = run(["python3", str(agentctl), "focus"], root)
    return result.stdout.strip() if result.returncode == 0 else ""


def session_start() -> int:
    root = find_root()
    if not (root / ".agent").exists():
        return 0
    message = (
        "This repo uses Agent Workflow Kit. Before editing, enter the autonomous work loop:\n"
        "  python3 tools/agentctl.py work --agent <agent-name>\n"
        "It will resume the current task or auto-claim the next assigned task, then print the required focus.\n"
        "If no task is assigned for the current user request, create and start one yourself:\n"
        "  python3 tools/agentctl.py work --agent <agent-name> --auto-create --title \"<current request>\" --scope \"<paths>\""
    )
    focus = current_focus(root)
    if focus:
        message += (
            "\n\nA task session is already active. This may be a resume or a context "
            "compaction: re-read your current focus before continuing so you do not "
            "drift from the task or the plan:\n" + focus
        )
    emit_context("SessionStart", message)
    return 0


def pre_tool_use() -> int:
    root = find_root()
    if not (root / ".agent").exists():
        return 0
    payload = read_input()
    if not is_mutating_tool(payload):
        return 0
    if not has_session(root):
        return block(
            "PreToolUse",
            "Agent Workflow Kit blocked this write/mutating action because no active task session exists. "
            "Run: python3 tools/agentctl.py work --agent <agent-name>. "
            "If no task exists for the current request, run: python3 tools/agentctl.py work --agent <agent-name> "
            "--auto-create --title \"<current request>\" --scope \"<paths>\".",
        )
    ok, message = check_manual(root)
    if not ok:
        return block(
            "PreToolUse",
            "Agent Workflow Kit blocked this action because workflow checks failed. "
            "Re-read changed plan/rule files and run python3 tools/agentctl.py refresh if needed.\n"
            + message,
        )
    if session_completed(root) and not is_finalization_action(payload):
        return block(
            "PreToolUse",
            "Agent Workflow Kit blocked this mutating action because the active task has already been completed. "
            "Only finalization commands such as git add/commit/push, gh pr/repo, or agentctl status/board/gate/check are allowed. "
            "Start a new task before making more code or document edits.",
        )
    return 0


def stop() -> int:
    root = find_root()
    if not (root / ".agent").exists() or not has_session(root):
        return 0
    ok, message = check_manual(root)
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
