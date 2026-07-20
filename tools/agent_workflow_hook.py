#!/usr/bin/env python3
"""Lifecycle hook bridge for agent workflow enforcement.

This script is intentionally conservative and stdlib-only. It is invoked by
Codex/Claude/Cursor-native hook configs where supported.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
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
RUNTIME_ID_ENV_NAMES = (
    "CODEX_THREAD_ID",
    "CLAUDE_CODE_SESSION_ID",
    "CURSOR_CONVERSATION_ID",
    "WHALENT_AGENT_ID",
    "WHALENT_CODEX_INSTANCE_ID",
)
SESSION_ID_ENV = "AGENT_WORKFLOW_SESSION_ID"
SESSION_OWNER_RUNTIME_ENV = "AGENT_WORKFLOW_SESSION_OWNER_RUNTIME"
SESSION_INSTANCE_ENV = "AGENT_WORKFLOW_SESSION_INSTANCE_ID"
PARENT_SESSION_KEY_ENV = "AGENT_WORKFLOW_PARENT_SESSION_KEY"
SESSION_ISOLATION_ERROR_ENV = "AGENT_WORKFLOW_SESSION_ISOLATION_ERROR"
SESSION_EXPORT_ENV_NAMES = (
    SESSION_ID_ENV,
    SESSION_OWNER_RUNTIME_ENV,
    SESSION_INSTANCE_ENV,
    PARENT_SESSION_KEY_ENV,
    SESSION_ISOLATION_ERROR_ENV,
)
AGENTCTL_ACTION_COMMANDS = frozenset({
    "task", "agents", "handoff", "worktree", "eval", "guidance", "loop", "sessions",
})
IDENTITY_FREE_COMMAND_PATHS = frozenset({
    ("init",),
    ("focus",),
    ("board",),
    ("task", "show"),
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
    ("migrate",),
    ("sessions", "list"),
    ("status",),
})


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
    for key in (
        "session_id", "sessionId", "conversation_id", "conversationId",
        "thread_id", "threadId", "composer_id", "composerId",
    ):
        value = payload.get(key)
        if value:
            return str(value)
    return ""


def payload_parent_session_id(payload: dict) -> str:
    for key in (
        "parent_session_id", "parentSessionId",
        "parent_conversation_id", "parentConversationId",
        "parent_thread_id", "parentThreadId",
        "fork_source_session_id", "forkSourceSessionId",
        "forked_from_session_id", "forkedFromSessionId",
    ):
        value = payload.get(key)
        if value:
            return str(value)
    return ""


def _private_identity_key(prefix: str, value: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        return ""
    if re.fullmatch(rf"{re.escape(prefix)}-[0-9a-f]{{24}}", normalized):
        return normalized
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:24]
    return f"{prefix}-{digest}"


def payload_session_instance_key(payload: dict) -> str:
    for key in (
        "session_instance_id", "sessionInstanceId",
        "fork_id", "forkId", "branch_id", "branchId",
        "conversation_branch_id", "conversationBranchId",
        "clone_id", "cloneId",
    ):
        value = payload.get(key)
        if value:
            return _private_identity_key("instance", f"{key}={value}")
    return ""


def _host_runtime_identity(env: dict) -> str:
    values = []
    for name in RUNTIME_ID_ENV_NAMES:
        value = str(env.get(name) or "").strip()
        if value:
            values.append(f"{name}={value}")
    if not values:
        return ""
    digest = hashlib.sha256("\n".join(values).encode("utf-8")).hexdigest()
    return f"host-runtime:{digest[:32]}"


def _session_identity_source(env: dict) -> str:
    forced = str(env.get("AGENT_WORKFLOW_SESSION_KEY") or "").strip()
    if forced == "default":
        return "default"
    if re.fullmatch(r"session-[0-9a-f]{24}", forced):
        return "forced_key"
    if str(env.get(SESSION_ID_ENV) or "").strip():
        return "session_start"
    if _host_runtime_identity(env):
        return "host_runtime"
    if str(env.get("WHALENT_COMPOSER_ID") or "").strip():
        return "whalent_composer"
    if str(env.get("AGENT_SESSION_ID") or "").strip():
        return "agent_session"
    if str(env.get(SESSION_INSTANCE_ENV) or "").strip():
        return "session_instance"
    if str(env.get("TERM_SESSION_ID") or "").strip():
        return "terminal"
    return "default"


def session_identity_error(env: dict) -> str:
    isolation_error = str(env.get(SESSION_ISOLATION_ERROR_ENV) or "").strip()
    if isolation_error:
        return isolation_error
    source = _session_identity_source(env)
    if source not in {"terminal", "default"}:
        return ""
    if source == "terminal":
        detail = "TERM_SESSION_ID is terminal-scoped and may be shared by multiple agents"
    else:
        detail = "the client supplied no conversation, runtime, or SessionStart identity"
    return (
        "unique conversation identity is unavailable: " + detail + "; restart "
        "this conversation so the project SessionStart hook can establish one"
    )


def _payload_is_fork(payload: dict, parent_id: str) -> bool:
    source = str(payload.get("source") or payload.get("reason") or "").strip().lower()
    return bool(parent_id or source in {"fork", "forked", "clone", "cloned", "branch"})


def hook_environment(payload: dict, *, create_fork_instance: bool = False) -> dict:
    env = os.environ.copy()
    inherited_id = str(env.get(SESSION_ID_ENV) or "").strip()
    inherited_owner = str(env.get(SESSION_OWNER_RUNTIME_ENV) or "").strip()
    current_runtime = _host_runtime_identity(env)
    session_id = payload_session_id(payload).strip()
    parent_id = (
        payload_parent_session_id(payload).strip()
        or str(env.get("WHALENT_FORK_SOURCE_AGENT_ID") or "").strip()
        or str(env.get(PARENT_SESSION_KEY_ENV) or "").strip()
    )
    parent_key = _private_identity_key("lineage", parent_id)
    forked = _payload_is_fork(payload, parent_id)
    owner_changed = bool(
        current_runtime and inherited_owner and current_runtime != inherited_owner
    )
    owner_unbound_inheritance = bool(
        forked and session_id and inherited_id and session_id == inherited_id
        and not inherited_owner
    )

    if (create_fork_instance and not forked and not session_id and not inherited_id
            and _session_identity_source(env) in {"terminal", "default"}):
        transcript_path = str(
            payload.get("transcript_path") or payload.get("transcriptPath") or ""
        ).strip()
        seed = (
            f"transcript_path={transcript_path}"
            if transcript_path else f"generated={secrets.token_hex(24)}"
        )
        session_id = _private_identity_key("generated", seed)

    instance_key = payload_session_instance_key(payload)
    inherited_instance = str(env.get(SESSION_INSTANCE_ENV) or "").strip()
    if not instance_key and not (
        forked and (owner_changed or owner_unbound_inheritance)
    ):
        instance_key = _private_identity_key("instance", inherited_instance)

    if forked and not instance_key:
        transcript_path = str(
            payload.get("transcript_path") or payload.get("transcriptPath") or ""
        ).strip()
        if transcript_path:
            instance_key = _private_identity_key(
                "instance", f"transcript_path={transcript_path}",
            )
    if forked and not instance_key and create_fork_instance:
        instance_key = _private_identity_key(
            "instance", f"generated={secrets.token_hex(24)}",
        )

    stale_payload_id = bool(
        session_id and inherited_id and session_id == inherited_id
        and (owner_changed or owner_unbound_inheritance)
    )
    if session_id:
        if stale_payload_id:
            env[SESSION_ID_ENV] = ""
            env[SESSION_OWNER_RUNTIME_ENV] = ""
        else:
            env[SESSION_ID_ENV] = session_id
            env[SESSION_OWNER_RUNTIME_ENV] = current_runtime
    if parent_key:
        env[PARENT_SESSION_KEY_ENV] = parent_key
    if instance_key:
        env[SESSION_INSTANCE_ENV] = instance_key
    elif forked:
        env[SESSION_INSTANCE_ENV] = ""

    if forked and not instance_key:
        env[SESSION_ID_ENV] = ""
        env[SESSION_OWNER_RUNTIME_ENV] = ""
        env[SESSION_ISOLATION_ERROR_ENV] = (
            "forked conversation must use an identity distinct from its parent; "
            "its instance is missing or untrusted; "
            "restart the session so SessionStart can establish an isolated instance"
        )
    elif SESSION_ISOLATION_ERROR_ENV in env:
        env[SESSION_ISOLATION_ERROR_ENV] = ""
    return env


def session_environment_exports(env: dict) -> dict:
    return {
        name: str(env.get(name) or "")
        for name in SESSION_EXPORT_ENV_NAMES
        if name in env
    }


def persist_session_environment(env_vars: dict) -> dict:
    if not env_vars:
        return {}
    env_file = (os.environ.get("CLAUDE_ENV_FILE") or "").strip()
    if env_file:
        path = Path(env_file).expanduser()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                for name, value in env_vars.items():
                    handle.write(f"export {name}=" + shlex.quote(value) + "\n")
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


def _agentctl_command_paths(command: str) -> list[tuple[str, ...]]:
    if "agentctl" not in command:
        return []
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|<>")
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        return [("__unparseable__",)]

    segments: list[list[str]] = []
    current: list[str] = []
    for token in tokens:
        if token in {";", "&&", "||", "|", "&"}:
            if current:
                segments.append(current)
            current = []
        else:
            current.append(token)
    if current:
        segments.append(current)

    paths: list[tuple[str, ...]] = []
    for segment in segments:
        for index, token in enumerate(segment):
            file_invocation = Path(token).name in {"agentctl", "agentctl.py"}
            module_invocation = (
                index > 0
                and segment[index - 1] == "-m"
                and token.rsplit(".", 1)[-1] == "agentctl"
            )
            if not (file_invocation or module_invocation):
                continue
            trailing = segment[index + 1:]
            if any(item in {"-h", "--help"} for item in trailing):
                continue
            words = [item for item in trailing if item and not item.startswith("-")]
            if not words:
                paths.append(("__unclassified__",))
                continue
            command_name = words[0]
            if command_name in AGENTCTL_ACTION_COMMANDS:
                action = words[1] if len(words) > 1 else ""
                paths.append((command_name, action))
            else:
                paths.append((command_name,))
    return paths


def command_is_agentctl_start(command: str) -> bool:
    return any(path in {("start",), ("work",)} for path in _agentctl_command_paths(command))


def command_requires_agentctl_identity(command: str) -> bool:
    return any(
        path not in IDENTITY_FREE_COMMAND_PATHS
        for path in _agentctl_command_paths(command)
    )


def payload_command(payload: dict) -> str:
    if payload.get("command"):
        return str(payload.get("command") or "")
    return str((payload.get("tool_input") or {}).get("command") or "")


def _shell_command_segments(command: str) -> list[tuple[list[str], str]]:
    if not command:
        return []
    try:
        # A newline separates commands exactly like `;`. shlex treats it as
        # plain whitespace, which would silently fuse the second command into
        # the first segment's arguments, so make the separator explicit.
        # (Quoted/heredoc newlines become separators too - a fail-closed
        # over-split, never a merge.)
        lexer = shlex.shlex(
            command.replace("\r\n", "\n").replace("\n", " ; "),
            posix=True, punctuation_chars=";&|<>",
        )
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        return []
    segments = []
    current = []
    for token in tokens:
        if token in {";", "&&", "||", "|", "&"}:
            if current:
                segments.append((current, token))
            current = []
        else:
            current.append(token)
    if current:
        segments.append((current, ""))
    return segments


_DURATION_TOKEN = re.compile(r"^\d+(\.\d+)?[smhd]?$")


def _strip_command_prefixes(tokens: list[str]) -> list[str]:
    command_tokens = list(tokens)
    changed = True
    while changed and command_tokens:
        changed = False
        head = command_tokens[0]
        if head in {"sudo", "command", "nohup", "time", "setsid", "caffeinate"}:
            command_tokens = command_tokens[1:]
            changed = True
            continue
        if head in {"nice", "ionice", "stdbuf"}:
            command_tokens = command_tokens[1:]
            while command_tokens and command_tokens[0].startswith("-"):
                flag = command_tokens[0]
                command_tokens = command_tokens[1:]
                # `nice -n 10` / `ionice -c 2` carry a separate value token.
                if flag in {"-n", "-c"} and command_tokens:
                    command_tokens = command_tokens[1:]
            changed = True
            continue
        if head == "timeout":
            command_tokens = command_tokens[1:]
            while command_tokens and command_tokens[0].startswith("-"):
                flag = command_tokens[0]
                command_tokens = command_tokens[1:]
                if flag in {"-k", "--kill-after", "-s", "--signal"} and command_tokens:
                    command_tokens = command_tokens[1:]
            if command_tokens and _DURATION_TOKEN.match(command_tokens[0]):
                command_tokens = command_tokens[1:]
            changed = True
            continue
        if head == "env":
            command_tokens = command_tokens[1:]
            while command_tokens and (
                    command_tokens[0].startswith("-")
                    or re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", command_tokens[0])):
                command_tokens = command_tokens[1:]
            changed = True
            continue
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", head):
            command_tokens = command_tokens[1:]
            changed = True
            continue
    return command_tokens


# Executables whose writes cannot be statically mapped to paths. They demand
# an active task session (and the session-level guard) instead of passing as
# read-only. agentctl invocations are excluded: the controller enforces its
# own identity/mutation policy for every subcommand.
OPAQUE_WRITE_EXECUTABLES = {
    "rsync", "dd", "install", "truncate", "shred", "unzip", "make",
    "cmake", "ninja", "pip", "pip3", "xargs", "eval", "source",
}
OPAQUE_INTERPRETERS = re.compile(r"^(python[0-9.]*|node|deno|bun|ruby)$")
SCRIPT_SUFFIX = re.compile(r"\.(sh|bash|zsh|py|js|ts|rb|pl)$")

# Commands that read the workspace without writing it. Unknown executables are
# NOT assumed read-only: any project binary, test runner, or build tool may
# write arbitrary paths, so everything outside this list is treated as an
# opaque write. sed/awk/perl one-liners without in-place flags are accepted as
# conventional text filters; their in-place forms are path-extracted instead.
# Commands verified to have no file-writing options or embedded languages.
# sort/uniq (output targets), awk/yq/perl (DSLs), base64 (-o), and tar are
# handled by dedicated argument-aware branches instead of this list.
READ_ONLY_EXECUTABLES = {
    "ls", "cat", "head", "tail", "less", "more", "grep", "egrep", "fgrep",
    "rg", "ag", "fd", "tree", "wc", "cut", "tr", "diff",
    "cmp", "comm", "file", "stat", "du", "df", "pwd", "whoami", "id",
    "uname", "hostname", "date", "sleep", "true", "false", "test", "[",
    "which", "whereis", "type", "printenv", "ps", "echo", "printf",
    "basename", "dirname", "realpath", "readlink", "md5", "md5sum",
    "shasum", "sha1sum", "sha256sum", "cksum", "jq", "xxd",
    "hexdump", "strings", "column", "nl", "od", "seq", "expr",
    "getconf", "sysctl", "sw_vers", "arch", "nproc", "tty", "uptime",
    "cd", "export", "unset", "set", "wait", "kill",
    "gh",
}
# sed scripts that provably only print: line addresses plus p/q commands,
# e.g. `1p`, `1,5p`, `$p`. Anything else (s///w, r/w commands, hold space
# tricks) is not verified and falls through to opaque.
_SED_PRINT_ONLY = re.compile(r"^[0-9,$;np ]*$")
_IN_PLACE_FLAG = re.compile(r"^-[a-zA-Z]*i|^--in-place")

# Shell constructs that splice arbitrary command output into another command.
# Static tokenization cannot see inside them, so their presence makes the
# whole command an opaque write (fail closed, including quoted literals).
_SHELL_SUBSTITUTION = re.compile(r"\$\(|`|<\(|>\(")

# Git subcommands that never modify the working tree, index, refs, or config.
# fetch and reflog are NOT blanket reads: fetch rewrites shared refs that
# every worktree sees, and reflog expire/delete mutate history metadata.
GIT_READ_SUBCOMMANDS = {
    "status", "log", "show", "diff", "rev-parse", "describe", "ls-files",
    "ls-tree", "ls-remote", "cat-file", "blame", "annotate", "shortlog",
    "grep", "help", "merge-base", "name-rev", "diff-tree", "diff-index",
    "count-objects", "cherry", "var", "show-ref",
    "whatchanged", "rev-list", "check-ignore", "check-attr",
}
_GIT_BRANCH_READ_FLAGS = re.compile(
    r"^(-a|-r|-v|-vv|--list|--show-current|--contains|--no-contains"
    r"|--merged|--no-merged|--sort=.*|--format=.*|--points-at=.*|--all)$"
)
_GIT_VALUE_FLAGS = {"-C", "-c", "--git-dir", "--work-tree", "--namespace",
                    "--exec-path"}


def _git_segment_details(tokens: list[str]) -> tuple[str, list[str]]:
    """Return (subcommand, remaining args) after git's global options."""
    rest = list(tokens[1:])
    while rest:
        item = rest[0]
        if item in _GIT_VALUE_FLAGS:
            rest = rest[2:]
            continue
        if item.startswith("-"):
            rest = rest[1:]
            continue
        break
    if not rest:
        return "", []
    return rest[0], rest[1:]


def _git_segment_read_only(tokens: list[str]) -> bool:
    sub, tail = _git_segment_details(tokens)
    if not sub:
        return True
    positional = [item for item in tail if not item.startswith("-") and item != "--"]
    if sub in GIT_READ_SUBCOMMANDS:
        return True
    if sub == "branch":
        # Creating, deleting, renaming, or configuring branches is a write;
        # only pure listing forms are read-only, and unknown flags fail
        # closed. With an explicit --list, positionals are match patterns.
        flags_ok = all(
            _GIT_BRANCH_READ_FLAGS.match(item)
            for item in tail if item.startswith("-")
        )
        if not flags_ok:
            return False
        return not positional or "--list" in tail
    if sub == "reflog":
        return not tail or tail[0] in {"show"} or tail[0].startswith("-")
    if sub == "config":
        return any(item in {"--get", "--get-all", "--get-regexp", "--list", "-l"}
                   for item in tail)
    if sub == "tag":
        return not positional or any(item in {"-l", "--list"} for item in tail)
    if sub == "stash":
        return bool(tail) and tail[0] in {"list", "show"}
    if sub == "remote":
        return not tail or tail[0] in {"-v", "show", "get-url"}
    if sub == "worktree":
        return bool(tail) and tail[0] == "list"
    # Default-deny: restore/checkout/rm/mv/apply/cherry-pick/revert/clean/
    # stash-mutations and anything unrecognized can rewrite shared state.
    return False


def _classify_segment(tokens: list[str]) -> str:
    """Classify one pipeline segment as read_only / pathed / opaque."""
    if not tokens:
        return "read_only"
    joined = " ".join(tokens)
    if "agentctl.py" in joined or "agent_workflow_hook.py" in joined:
        return "read_only"
    head = tokens[0]
    executable = Path(head).name
    args = tokens[1:]
    if head.startswith("./"):
        return "opaque"
    if executable in {"bash", "sh", "zsh", "dash", "ksh"}:
        for index, item in enumerate(args):
            if item == "-c" and index + 1 < len(args):
                # Unwrap the nested command and classify its contents.
                return classify_shell_command(args[index + 1])
        if any(SCRIPT_SUFFIX.search(a) for a in args if not a.startswith("-")):
            return "opaque"
        # `bash` alone or `sh -s` executes stdin we cannot see.
        return "opaque"
    if executable in OPAQUE_WRITE_EXECUTABLES:
        if executable == "unzip" and any(a in {"-l", "-t"} for a in args):
            return "read_only"
        return "opaque"
    if executable == "tar":
        # Handle both `-xzf` and old-style bundled `xvf` option words; extract,
        # create, update, and append all write files or archives.
        first_word = args[0] if args else ""
        option_words = [a.lstrip("-") for a in args if a.startswith("-")]
        if not first_word.startswith("-") and first_word:
            option_words.append(first_word)
        if any(set(word) & set("xcuUrA") for word in option_words):
            return "opaque"
        return "read_only"
    if executable in {"curl", "wget"}:
        if any(a in {"-o", "-O", "--output", "--remote-name"}
               or a.startswith("--output=") for a in args):
            return "opaque"
        return "read_only"
    if OPAQUE_INTERPRETERS.match(executable):
        return "opaque"
    if executable == "git":
        return "read_only" if _git_segment_read_only(tokens) else "git_write"
    if executable == "find":
        if any(a in {"-delete", "-exec", "-execdir", "-ok", "-okdir"} for a in args):
            return "opaque"
        return "read_only"
    if executable == "sed":
        if any(_IN_PLACE_FLAG.match(a) for a in args if a.startswith("-")):
            return "pathed"
        scripts = [a for a in args if not a.startswith("-")]
        if scripts and _SED_PRINT_ONLY.match(scripts[0]):
            return "read_only"
        # sed scripts can write via the w command (`s///w file`, `1w file`).
        return "opaque"
    if executable in {"perl", "awk", "gawk", "mawk", "nawk", "yq"}:
        # General-purpose or file-writing DSLs: inline code can open, write,
        # or in-place edit arbitrary paths (awk print > file, perl open,
        # yq -i). In-place perl keeps its extracted-path form.
        if executable == "perl" and any(
                _IN_PLACE_FLAG.match(a) for a in args if a.startswith("-")):
            return "pathed"
        return "opaque"
    if executable == "sort":
        if any(a == "-o" or a.startswith("--output") for a in args):
            return "pathed"
        return "read_only"
    if executable == "uniq":
        positional = [a for a in args if not a.startswith("-")]
        if len(positional) >= 2:
            return "pathed"
        return "read_only"
    if executable == "base64":
        if any(a == "-o" or a.startswith("--output") for a in args):
            return "pathed"
        return "read_only"
    if executable in {"touch", "mkdir", "rm", "tee", "cp", "mv", "ln"}:
        return "pathed"
    if executable in READ_ONLY_EXECUTABLES:
        return "read_only"
    # Default-deny: an unknown executable may write anywhere.
    return "opaque"


_VERDICT_RANK = {"read_only": 0, "pathed": 1, "git_write": 2, "opaque": 3}
# Tokens that redirect stream output into a file. `>&` is excluded here and
# handled positionally: followed by a digit it duplicates a descriptor,
# otherwise it writes the named file.
REDIRECT_WRITE_TOKENS = {">", ">>", "1>", "1>>", "2>", "2>>", "&>", "&>>", ">|"}


def _segment_redirects_to_file(segment: list[str]) -> bool:
    for index, token in enumerate(segment):
        if token in REDIRECT_WRITE_TOKENS:
            return True
        if token == ">&":
            nxt = segment[index + 1] if index + 1 < len(segment) else ""
            if not nxt.isdigit():
                return True
    return False


def classify_shell_command(command: str) -> str:
    """Strongest write capability across all pipeline segments."""
    if command and _SHELL_SUBSTITUTION.search(command):
        return "opaque"
    strongest = "read_only"
    for segment, _connector in _shell_command_segments(command):
        if _segment_redirects_to_file(segment):
            if _VERDICT_RANK[strongest] < _VERDICT_RANK["pathed"]:
                strongest = "pathed"
        tokens = _strip_command_prefixes(segment)
        verdict = _classify_segment(tokens)
        if verdict == "opaque":
            return "opaque"
        if _VERDICT_RANK[verdict] > _VERDICT_RANK[strongest]:
            strongest = verdict
    return strongest


def command_opaque_write(command: str) -> bool:
    """True when a shell command can write files we cannot enumerate."""
    return classify_shell_command(command) == "opaque"


def command_git_write(command: str) -> bool:
    """True when any segment mutates git state (index/refs/working tree)."""
    if GIT_WRITE_BASH.search(command):
        return True
    for segment, _connector in _shell_command_segments(command):
        tokens = _strip_command_prefixes(segment)
        if tokens and Path(tokens[0]).name == "git" and not _git_segment_read_only(tokens):
            return True
    return False


def _git_segment_shared_mutation(tokens: list[str]) -> bool:
    """True when a git segment rewrites state shared by every worktree.

    Branch deletion/rename, reflog expiry, pruning fetches, gc, tag deletion,
    forced or deleting pushes, and worktree removal hit refs and objects that
    all checkouts of the repository see, so per-checkout exclusivity is not
    enough for them.
    """
    sub, tail = _git_segment_details(tokens)
    flags = [item for item in tail if item.startswith("-")]
    if sub == "branch":
        return any(item in {"-D", "-d", "-m", "-M", "-f", "--force", "--delete",
                            "--move", "-c", "-C", "--copy"} for item in flags)
    if sub == "reflog":
        return bool(tail) and tail[0] in {"expire", "delete", "drop"}
    if sub == "fetch":
        return any(item in {"--prune", "-p", "--prune-tags"} for item in flags)
    if sub == "tag":
        return any(item in {"-d", "--delete"} for item in flags)
    if sub == "push":
        return any(item in {"--force", "-f", "--force-with-lease", "--delete",
                            "-d", "--prune", "--mirror"} for item in flags)
    if sub == "worktree":
        return bool(tail) and tail[0] in {"remove", "prune", "move"}
    return sub in {"gc", "prune", "update-ref", "pack-refs", "filter-branch"}


def command_git_shared_mutation(command: str) -> bool:
    for segment, _connector in _shell_command_segments(command):
        tokens = _strip_command_prefixes(segment)
        if tokens and Path(tokens[0]).name == "git" and _git_segment_shared_mutation(tokens):
            return True
    return False


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
    segments = _shell_command_segments(command)
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
            if token in REDIRECT_WRITE_TOKENS and index + 1 < len(segment):
                add_target(segment[index + 1])
                index += 2
                continue
            if token == ">&" and index + 1 < len(segment):
                nxt = segment[index + 1]
                if not nxt.isdigit():
                    add_target(nxt)
                index += 2
                continue
            command_tokens.append(token)
            index += 1
        command_tokens = _strip_command_prefixes(command_tokens)
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
        elif executable == "sort":
            for index, item in enumerate(command_args):
                if item == "-o" and index + 1 < len(command_args):
                    add_target(command_args[index + 1])
                elif item.startswith("--output="):
                    add_target(item.split("=", 1)[1])
        elif executable == "base64":
            for index, item in enumerate(command_args):
                if item == "-o" and index + 1 < len(command_args):
                    add_target(command_args[index + 1])
                elif item.startswith("--output="):
                    add_target(item.split("=", 1)[1])
        elif executable == "uniq" and len(args) >= 2:
            add_target(args[-1])
        elif executable == "ln" and args:
            # `ln [-s] target linkname` writes the link name; a bare
            # `ln target` links into the working directory.
            link_name = args[-1] if len(args) >= 2 else args[0]
            add_target(link_name)
            if len(args) >= 2:
                # Claim the target too: a link whose target is outside scope
                # would alias a peer's tree. A SYMLINK target is resolved
                # relative to the link's own directory; a HARD link target is
                # resolved relative to cwd (add_target's default).
                target = args[0]
                is_symlink = any(
                    item.startswith("-") and "s" in item.lstrip("-")
                    for item in command_args)
                target_path = Path(target).expanduser()
                if is_symlink and not target_path.is_absolute():
                    link_path = Path(link_name).expanduser()
                    if not link_path.is_absolute():
                        link_path = working_dir / link_path
                    target_path = link_path.parent / target
                add_target(str(target_path))
        elif executable == "git":
            sub, tail = _git_segment_details(command_tokens)
            if sub in {"restore", "rm", "mv", "checkout", "clean"}:
                for value in tail:
                    if value.startswith("-") or value == "--":
                        continue
                    if sub == "checkout" and "--" not in tail:
                        continue  # branch switch, not a path restore
                    add_target(value)
        elif executable == "mv" and args:
            # A rename writes the destination AND deletes every source.
            for value in args:
                add_target(value)
        elif executable == "cp" and args:
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
    paths = payload_paths(payload)
    for path in paths:
        command.extend(["--path", path])
    shell_command = payload_command(payload)
    if shell_command:
        verdict = classify_shell_command(shell_command)
        if command_git_write(shell_command):
            command.append("--git-write")
        if command_git_shared_mutation(shell_command):
            command.append("--git-shared")
        if verdict == "opaque" or (verdict == "pathed" and not paths):
            # A write-capable command that yielded no checkable path must not
            # pass on the strength of an empty path list.
            command.append("--opaque")
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
        return (
            bool(payload_paths(payload))
            or MUTATING_BASH.search(command) is not None
            or classify_shell_command(command) != "read_only"
        )
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
    env = hook_environment(payload, create_fork_instance=True)
    session_env = persist_session_environment(session_environment_exports(env))
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
    identity_error = session_identity_error(env)
    mutating = is_mutating_tool(payload)
    controller_mutation = command_requires_agentctl_identity(payload_command(payload))
    if identity_error and (mutating or controller_mutation):
        return block(
            "PreToolUse",
            "Agent Workflow Kit blocked this action because this conversation does "
            "not have a unique workflow identity. " + identity_error + ".",
        )
    if not mutating:
        # Read-only path: refresh the heartbeat in a single spawn. The
        # heartbeat command is a no-op (nonzero, ignored) when there is no
        # active/owned session, so we skip the extra `status --json` spawn
        # that `has_session` would cost on every read.
        heartbeat(root, env)
        return 0
    active = has_session(root, env)
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
