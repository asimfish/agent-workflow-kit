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
import time
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
SESSION_KEY_ENV = "AGENT_WORKFLOW_SESSION_KEY"
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
PROVIDER_BINDING_SCHEMA = 1
PROVIDER_BINDING_PENDING_NS = 5 * 60 * 1_000_000_000
PROVIDER_BINDING_LOCK_STALE_NS = 30 * 1_000_000_000
PROVIDER_BINDING_LOCK_WAIT_SECONDS = 2.0
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


def _host_runtime_session_environment(env: dict) -> dict:
    fallback = env.copy()
    fallback.pop(SESSION_ID_ENV, None)
    fallback.pop(SESSION_KEY_ENV, None)
    fallback.pop(SESSION_OWNER_RUNTIME_ENV, None)
    fallback.pop(SESSION_ISOLATION_ERROR_ENV, None)
    return fallback


def _provider_shell_session_keys(root: Path, payload_id: str, env: dict) -> set[str]:
    """Return active sessions from bounded provider-ID shell environments."""
    base = _host_runtime_session_environment(env)
    session_keys: set[str] = set()
    for name in RUNTIME_ID_ENV_NAMES:
        candidate = base.copy()
        candidate[name] = payload_id
        state = session_state(root, candidate)
        session_key = str(state.get("workflow_session_key") or "").strip()
        if (
            state.get("task")
            and state.get("presence_status") != "released"
            and state.get("status") != "released"
            and re.fullmatch(r"session-[0-9a-f]{24}", session_key)
        ):
            session_keys.add(session_key)
    return session_keys


def _provider_binding_path(root: Path, runtime_identity: str) -> Path:
    result = run(["git", "rev-parse", "--git-common-dir"], root)
    if result.returncode == 0 and result.stdout.strip():
        common = Path(result.stdout.strip())
        if not common.is_absolute():
            common = root / common
        base = common.resolve() / "agent-workflow" / "provider-bindings"
    else:
        base = root / ".agent" / "state" / "provider-bindings"
    binding_identity = f"{runtime_identity}\ncheckout={root.resolve()}"
    digest = hashlib.sha256(binding_identity.encode("utf-8")).hexdigest()[:24]
    return base / f"host-{digest}.json"


def _read_provider_binding(path: Path) -> tuple[dict, bool]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}, True
    except (OSError, json.JSONDecodeError):
        return {}, False
    if not isinstance(value, dict) or value.get("schema_version") != PROVIDER_BINDING_SCHEMA:
        return {}, False
    host_runtime = value.get("host_runtime")
    provider_key = value.get("provider_key")
    bound_session_key = value.get("bound_session_key")
    timestamps = (value.get("created_at_ns"), value.get("updated_at_ns"))
    if (
        not isinstance(host_runtime, str)
        or re.fullmatch(r"host-runtime:[0-9a-f]{32}", host_runtime) is None
        or not isinstance(provider_key, str)
        or re.fullmatch(r"provider-[0-9a-f]{24}", provider_key) is None
        or not isinstance(bound_session_key, str)
        or (
            bound_session_key
            and re.fullmatch(r"session-[0-9a-f]{24}", bound_session_key) is None
        )
        or any(
            not isinstance(item, int) or isinstance(item, bool) or item < 0
            for item in timestamps
        )
    ):
        return {}, False
    return value, True


def _write_provider_binding(path: Path, value: dict) -> bool:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False
    temporary = path.parent / (
        f".{path.name}.{os.getpid()}.{secrets.token_hex(6)}.tmp"
    )
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        try:
            temporary.chmod(0o600)
        except OSError:
            pass
        os.replace(temporary, path)
        return True
    except OSError:
        return False
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _acquire_provider_binding_lock(path: Path) -> Path | None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    lock = path.with_suffix(path.suffix + ".lock")
    deadline = time.monotonic() + PROVIDER_BINDING_LOCK_WAIT_SECONDS
    while True:
        try:
            lock.mkdir()
            return lock
        except FileExistsError:
            try:
                stale = time.time_ns() - lock.stat().st_mtime_ns
                if stale > PROVIDER_BINDING_LOCK_STALE_NS:
                    lock.rmdir()
                    continue
            except FileNotFoundError:
                continue
            except OSError:
                return None
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.01)
        except OSError:
            return None


def _provider_runtime_binding(
        root: Path, payload: dict, env: dict, *, claim: bool) -> tuple[str, str]:
    payload_id = payload_session_id(payload).strip()
    runtime_identity = _host_runtime_identity(env)
    parent_id = (
        payload_parent_session_id(payload).strip()
        or str(env.get("WHALENT_FORK_SOURCE_AGENT_ID") or "").strip()
        or str(env.get(PARENT_SESSION_KEY_ENV) or "").strip()
    )
    if (
        not payload_id
        or not runtime_identity
        or str(os.environ.get(SESSION_ID_ENV) or "").strip()
        or str(env.get(SESSION_INSTANCE_ENV) or "").strip()
        or _payload_is_fork(payload, parent_id)
    ):
        return "none", ""

    provider_key = _private_identity_key("provider", payload_id)
    runtime_ids = {
        str(env.get(name) or "").strip()
        for name in RUNTIME_ID_ENV_NAMES
        if str(env.get(name) or "").strip()
    }
    runtime_proven = payload_id in runtime_ids
    fallback = _host_runtime_session_environment(env)
    host_state = session_state(root, fallback)
    host_active = bool(
        host_state.get("task")
        and host_state.get("presence_status") != "released"
        and host_state.get("status") != "released"
    )
    host_session_key = str(host_state.get("workflow_session_key") or "").strip()
    path = _provider_binding_path(root, runtime_identity)
    lock = _acquire_provider_binding_lock(path)
    if lock is None:
        return "conflict", (
            "provider/runtime binding state is busy; retry after the current "
            "session transition completes"
        )
    try:
        binding, binding_is_valid = _read_provider_binding(path)
        if not binding_is_valid:
            return "conflict", (
                "provider/runtime binding state is invalid; restart this "
                "conversation before mutating the project"
            )
        valid_binding = bool(
            binding
            and binding.get("host_runtime") == runtime_identity
            and re.fullmatch(
                r"provider-[0-9a-f]{24}",
                str(binding.get("provider_key") or ""),
            )
        )
        if binding and not valid_binding:
            return "conflict", (
                "provider/runtime binding state is invalid; restart this "
                "conversation before mutating the project"
            )
        if valid_binding and binding.get("provider_key") == provider_key:
            bound_session_key = str(binding.get("bound_session_key") or "")
            if bound_session_key:
                return "bound", bound_session_key

            candidate_session_keys = _provider_shell_session_keys(
                root, payload_id, env,
            )
            if host_active and re.fullmatch(
                r"session-[0-9a-f]{24}", host_session_key,
            ):
                candidate_session_keys.add(host_session_key)
            if len(candidate_session_keys) > 1:
                return "conflict", (
                    "multiple active provider-derived shell sessions match this "
                    "conversation; release the duplicate sessions or restart "
                    "with an isolated runtime"
                )
            if candidate_session_keys:
                bound_session_key = next(iter(candidate_session_keys))
                binding["bound_session_key"] = bound_session_key
                binding["updated_at_ns"] = time.time_ns()
                if not _write_provider_binding(path, binding):
                    return "conflict", "provider/runtime binding could not be persisted"
                return "bound", bound_session_key
            return "match", ""

        if valid_binding:
            created_at_ns = int(binding.get("created_at_ns") or 0)
            pending_is_fresh = (
                not binding.get("bound_session_key")
                and time.time_ns() - created_at_ns <= PROVIDER_BINDING_PENDING_NS
            )
            if host_active or pending_is_fresh or not claim:
                return "conflict", (
                    "this host runtime is already bound to another provider "
                    "conversation; use a distinct runtime or reopen the session "
                    "so SessionStart establishes an isolated identity"
                )

        if host_active:
            if runtime_proven:
                return "none", ""
            return "conflict", (
                "this host runtime already owns an unbound active task; refusing "
                "to attach a different provider conversation"
            )
        if not claim:
            return "none", ""

        now_ns = time.time_ns()
        record = {
            "schema_version": PROVIDER_BINDING_SCHEMA,
            "host_runtime": runtime_identity,
            "provider_key": provider_key,
            "bound_session_key": "",
            "created_at_ns": now_ns,
            "updated_at_ns": now_ns,
        }
        if not _write_provider_binding(path, record):
            return "conflict", "provider/runtime binding could not be persisted"
        return "match", ""
    finally:
        try:
            lock.rmdir()
        except OSError:
            pass


def resolve_existing_session_environment(
        root: Path, payload: dict, env: dict, *, claim_runtime_binding: bool = False,
        enforce_runtime_binding_conflict: bool = True) -> dict:
    """Resolve direct, proven-runtime, or locally bound provider identity."""
    if has_session(root, env):
        return env
    payload_id = payload_session_id(payload)
    runtime_ids = {
        str(env.get(name) or "").strip()
        for name in RUNTIME_ID_ENV_NAMES
        if str(env.get(name) or "").strip()
    }
    if (
        not payload_id
        or not _host_runtime_identity(env)
        or str(os.environ.get(SESSION_ID_ENV) or "").strip()
        or str(env.get(SESSION_INSTANCE_ENV) or "").strip()
    ):
        return env
    binding_status, binding_value = _provider_runtime_binding(
        root, payload, env, claim=claim_runtime_binding,
    )
    if binding_status == "conflict":
        if not enforce_runtime_binding_conflict:
            return env
        isolated = env.copy()
        isolated[SESSION_ISOLATION_ERROR_ENV] = binding_value
        return isolated
    if binding_status == "bound":
        bound = _host_runtime_session_environment(env)
        bound[SESSION_KEY_ENV] = binding_value
        return bound
    fallback = _host_runtime_session_environment(env)
    if binding_status == "match" or payload_id in runtime_ids:
        return fallback if has_session(root, fallback) else env
    return env


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
        if token in {";", "&&", "||", "|", "&", "|&"}:
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
        if head in {"command", "time", "setsid", "caffeinate"}:
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
            env_tokens = command_tokens[1:]
            # env options can change cwd (-C/--chdir) or re-tokenize a
            # command (-S/--split-string). Keep env visible so those forms
            # remain opaque instead of attributing paths to the old cwd.
            if env_tokens and env_tokens[0].startswith("-"):
                return command_tokens
            command_tokens = env_tokens
            while command_tokens and _safe_environment_assignment(
                    command_tokens[0]):
                command_tokens = command_tokens[1:]
            if command_tokens and _environment_assignment(command_tokens[0]):
                return tokens
            changed = True
            continue
        if _environment_assignment(head):
            if not _safe_environment_assignment(head):
                return tokens
            command_tokens = command_tokens[1:]
            changed = True
            continue
    return command_tokens


def _environment_assignment(token: str) -> bool:
    return bool(re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", token))


def _safe_environment_assignment(token: str) -> bool:
    if not _environment_assignment(token):
        return False
    name = token.split("=", 1)[0]
    return bool(
        name.startswith("LC_")
        or name in {
            "LANG", "LANGUAGE", "TZ", "TERM", "NO_COLOR", "CLICOLOR",
            "CLICOLOR_FORCE",
        }
    )


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
# opaque write. Only provably print-only sed forms are accepted as conventional
# text filters; embedded languages and programmable in-place forms are opaque
# even when their direct input files are extractable.
# Commands verified to have no file-writing options or embedded languages.
# sort/uniq (output targets), awk/yq/perl (DSLs), base64 (-o), and tar are
# handled by dedicated argument-aware branches instead of this list.
READ_ONLY_EXECUTABLES = {
    "ls", "cat", "head", "tail", "less", "more", "grep", "egrep", "fgrep",
    "rg", "ag", "fd", "tree", "wc", "cut", "tr", "diff",
    "cmp", "comm", "file", "stat", "du", "df", "pwd", "whoami", "id",
    "uname", "hostname", "date", "sleep", "true", "false", "test", "[",
    "which", "whereis", "type", "printenv", "ps", "echo",
    "basename", "dirname", "realpath", "readlink", "md5", "md5sum",
    "shasum", "sha1sum", "sha256sum", "cksum", "jq", "xxd",
    "hexdump", "strings", "column", "nl", "od", "seq", "expr",
    "getconf", "sysctl", "sw_vers", "arch", "nproc", "tty", "uptime",
    "wait",
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
_SHELL_DYNAMIC_EXPANSION = re.compile(
    r"(?<!\\)\$(?:[A-Za-z_][A-Za-z0-9_]*|[0-9@*?$!#-]|\{[^}\n]+\})"
    r"|(?<!\\)\{[^{}\n]*,[^{}\n]*\}"
)

# Git subcommands that never modify the working tree, index, refs, or config.
# fetch and reflog are NOT blanket reads: fetch rewrites shared refs that
# every worktree sees, and reflog expire/delete mutate history metadata.
GIT_READ_SUBCOMMANDS = {
    "status", "log", "show", "diff", "rev-parse", "describe", "ls-files",
    "ls-tree", "ls-remote", "cat-file", "blame", "annotate", "shortlog",
    "grep", "merge-base", "name-rev", "diff-tree", "diff-index",
    "count-objects", "cherry", "var", "show-ref",
    "whatchanged", "rev-list", "check-ignore", "check-attr",
}
_GIT_BRANCH_READ_FLAGS = re.compile(
    r"^(-a|-r|-v|-vv|--list|--show-current|--contains|--no-contains"
    r"|--merged|--no-merged|--sort=.*|--format=.*|--points-at=.*|--all)$"
)
_GIT_VALUE_FLAGS = {"-C", "-c", "--git-dir", "--work-tree", "--namespace",
                    "--exec-path"}
_GIT_EMBEDDED_EXEC_FLAGS = {"--ext-diff", "--textconv", "--filters"}
_GIT_CONFIG_READ_ACTIONS = {"get", "list", "get-color", "get-colorbool"}
_GIT_CONFIG_WRITE_ACTIONS = {
    "set", "unset", "rename-section", "remove-section", "edit",
}
_GIT_CONFIG_READ_FLAGS = {
    "--get", "--get-all", "--get-regexp", "--get-urlmatch", "--list", "-l",
    "--get-color", "--get-colorbool",
}
_GIT_CONFIG_WRITE_FLAGS = {
    "--add", "--replace-all", "--unset", "--unset-all", "--rename-section",
    "--remove-section", "--edit", "-e", "--set",
}
_GIT_CONFIG_VALUE_FLAGS = {"--file", "-f", "--blob", "--type", "--default"}
_GIT_CONFIG_NEUTRAL_FLAGS = {
    "--local", "--global", "--system", "--worktree", "--includes",
    "--no-includes", "--null", "-z", "--name-only", "--show-origin",
    "--show-scope", "--bool", "--int", "--bool-or-int", "--bool-or-str",
    "--path", "--expiry-date", "--color",
}
_SUDO_VALUE_LONG_FLAGS = {
    "--chdir", "--chroot", "--close-from", "--command-timeout", "--group",
    "--host", "--other-user", "--prompt", "--role", "--type", "--user",
}
_SUDO_VALUE_SHORT_FLAGS = set("CDghpRTUuacrt")
_ENV_VALUE_FLAGS = {"-u", "--unset", "-C", "--chdir", "-P"}
_ENV_SPLIT_FLAGS = {"-S", "--split-string"}
_ENV_BOOLEAN_FLAGS = {
    "-i", "--ignore-environment", "-0", "--null", "-v", "--debug",
    "--block-signal", "--default-signal", "--ignore-signal",
    "--list-signal-handling",
}
_SHELL_COMMAND_EXECUTABLES = {"bash", "sh", "zsh", "dash", "ksh"}
_GH_GLOBAL_VALUE_FLAGS = {"-R", "--repo", "--hostname"}
_GH_READ_ACTIONS = {
    ("auth", "status"),
    ("cache", "list"),
    ("issue", "list"), ("issue", "status"), ("issue", "view"),
    ("pr", "checks"), ("pr", "diff"), ("pr", "list"),
    ("pr", "status"), ("pr", "view"),
    ("release", "list"), ("release", "view"),
    ("repo", "list"), ("repo", "view"),
    ("run", "list"), ("run", "view"), ("run", "watch"),
    ("workflow", "list"), ("workflow", "view"),
}
_XXD_VALUE_FLAGS = {
    "-c", "-cols", "-g", "-groupsize", "-l", "-len", "-n", "-name",
    "-o", "-offset", "-s", "-seek",
}
_FIND_OUTPUT_ACTIONS = {"-fls", "-fprint", "-fprint0", "-fprintf"}
_CURL_PATH_FLAGS = {
    "-c", "--cookie-jar", "-D", "--dump-header", "--trace",
    "--trace-ascii", "--etag-save", "--hsts", "--alt-svc", "-o", "--output",
    "--libcurl", "--stderr", "--ssl-sessions",
}
_CURL_WRITE_OUT_FLAGS = {"-w", "--write-out"}
_MV_VALUE_FLAGS = {"-S", "--suffix", "-t", "--target-directory"}
_SED_SCRIPT_VALUE_FLAGS = {
    "-e", "--expression", "-f", "--file", "-l", "--line-length",
}
_WRITE_REDIRECTS = {">", ">>", ">|", "&>", "&>>", "<>"}
_GLOB_CHARS = re.compile(r"[*?\[]")


def _option_values(args: list[str], flags: set[str],
                   attached_short: bool = False) -> list[str]:
    """Return explicit values for selected short/long command options."""
    values = []
    index = 0
    while index < len(args):
        item = args[index]
        if item in flags:
            if index + 1 < len(args):
                values.append(args[index + 1])
                index += 2
                continue
        for flag in flags:
            if flag.startswith("--") and item.startswith(flag + "="):
                values.append(item.split("=", 1)[1])
                break
            if (attached_short and len(flag) == 2 and item.startswith(flag)
                    and len(item) > 2):
                values.append(item[2:])
                break
        index += 1
    return values


def _positionals(args: list[str], value_flags: set[str]) -> list[str]:
    """Return positionals while skipping values consumed by known options."""
    values = []
    index = 0
    positional_only = False
    while index < len(args):
        item = args[index]
        if positional_only:
            values.append(item)
        elif item == "--":
            positional_only = True
        elif item == "-":
            values.append(item)
        elif item in value_flags:
            index += 1
        elif not item.startswith("-"):
            values.append(item)
        index += 1
    return values


def _clustered_short_option_values(args: list[str], option: str) -> list[str]:
    """Read a required value from `-abcVALUE` style short-option clusters."""
    values = []
    for index, item in enumerate(args):
        if not item.startswith("-") or item.startswith("--") or len(item) < 2:
            continue
        cluster = item[1:]
        position = cluster.find(option)
        if position < 0:
            continue
        attached = cluster[position + 1:]
        if attached:
            values.append(attached)
        elif index + 1 < len(args):
            values.append(args[index + 1])
    return values


def _target_directory_values(args: list[str]) -> list[str]:
    values = _option_values(args, {"--target-directory"})
    values.extend(_clustered_short_option_values(args, "t"))
    return list(dict.fromkeys(values))


def _tree_output_values(args: list[str]) -> list[str]:
    values = _option_values(args, {"--output"})
    values.extend(_clustered_short_option_values(args, "o"))
    return list(dict.fromkeys(values))


def _curl_write_out_details(args: list[str]) -> tuple[list[str], bool]:
    """Return literal write-out files and whether the format is uninspectable."""
    paths = []
    opaque = False
    for value in _option_values(args, _CURL_WRITE_OUT_FLAGS, attached_short=True):
        if value.startswith("@"):
            opaque = True
            continue
        for raw in re.findall(r"%output\{([^{}]+)\}", value):
            target = raw[2:] if raw.startswith(">>") else raw
            if target:
                paths.append(target)
            else:
                opaque = True
    return list(dict.fromkeys(paths)), opaque


def _write_redirection_details(
        tokens: list[str]) -> tuple[list[str], list[str], bool]:
    """Return output targets, non-redirection tokens, and malformed state."""
    paths = []
    command_tokens = []
    index = 0
    while index < len(tokens):
        fd_prefix = tokens[index].isdigit()
        op_index = index + 1 if fd_prefix else index
        op = tokens[op_index] if op_index < len(tokens) else ""
        if op not in _WRITE_REDIRECTS | {">&"}:
            command_tokens.append(tokens[index])
            index += 1
            continue
        target_index = op_index + 1
        if target_index >= len(tokens):
            return paths, command_tokens, True
        target = tokens[target_index]
        if op != ">&" or not (target.isdigit() or target == "-"):
            paths.append(target)
        index = target_index + 1
    return list(dict.fromkeys(paths)), command_tokens, False


def _git_config_may_execute(value: str) -> bool:
    """Only color.* overrides are inert enough for shared-checkout reads."""
    if "=" not in value:
        return True
    key = value.split("=", 1)[0].strip().lower()
    return not bool(re.fullmatch(r"color\.[a-z0-9.-]+", key))


def _git_global_config_may_execute(tokens: list[str]) -> bool:
    rest = list(tokens[1:])
    index = 0
    while index < len(rest):
        item = rest[index]
        if item == "-c":
            if index + 1 >= len(rest):
                return True
            if _git_config_may_execute(rest[index + 1]):
                return True
            index += 2
            continue
        if item.startswith("-c") and len(item) > 2:
            if _git_config_may_execute(item[2:]):
                return True
            index += 1
            continue
        if item == "--config-env" or item.startswith("--config-env="):
            return True
        if item in _GIT_VALUE_FLAGS:
            index += 2
            continue
        if item.startswith("-"):
            index += 1
            continue
        break
    return False


def _remove_option_values(positionals: list[str], values: list[str]) -> list[str]:
    remaining = list(positionals)
    for value in values:
        try:
            remaining.remove(value)
        except ValueError:
            pass
    return remaining


def _mv_write_values(args: list[str]) -> list[str]:
    target_dirs = _target_directory_values(args)
    positionals = _positionals(args, _MV_VALUE_FLAGS)
    positionals = _remove_option_values(positionals, target_dirs)
    if target_dirs:
        positionals.append(target_dirs[-1])
    return list(dict.fromkeys(positionals))


def _ln_output_values(args: list[str]) -> list[str]:
    target_dirs = _target_directory_values(args)
    positionals = _positionals(args, {"-S", "--suffix", "-t", "--target-directory"})
    positionals = _remove_option_values(positionals, target_dirs)
    if target_dirs:
        return [target_dirs[-1]]
    if len(positionals) >= 2:
        return [positionals[-1]]
    if len(positionals) == 1:
        return [Path(positionals[0]).name]
    return []


def _sed_in_place_targets(args: list[str]) -> list[str] | None:
    positionals = []
    explicit_script = False
    index = 0
    positional_only = False
    while index < len(args):
        item = args[index]
        if positional_only:
            positionals.append(item)
        elif item == "--":
            positional_only = True
        elif item in _SED_SCRIPT_VALUE_FLAGS:
            if index + 1 >= len(args):
                return None
            if item in {"-e", "--expression", "-f", "--file"}:
                explicit_script = True
            index += 1
        elif item.startswith("--expression=") or item.startswith("--file="):
            explicit_script = True
        elif ((item.startswith("-e") or item.startswith("-f"))
              and len(item) > 2):
            explicit_script = True
        elif item.startswith("-"):
            pass
        else:
            positionals.append(item)
        index += 1
    return positionals if explicit_script else positionals[1:]


def _perl_in_place_targets(args: list[str]) -> list[str] | None:
    positionals = []
    code_supplied = False
    index = 0
    positional_only = False
    while index < len(args):
        item = args[index]
        if positional_only:
            positionals.append(item)
        elif item == "--":
            positional_only = True
        elif item in {"-e", "-E"}:
            if index + 1 >= len(args):
                return None
            code_supplied = True
            index += 1
        elif (item.startswith("-e") or item.startswith("-E")) and len(item) > 2:
            code_supplied = True
        elif item.startswith("-"):
            pass
        else:
            positionals.append(item)
        index += 1
    return positionals if code_supplied else None


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


def _git_output_paths(tokens: list[str]) -> list[str]:
    sub, tail = _git_segment_details(tokens)
    if sub not in GIT_READ_SUBCOMMANDS:
        return []
    return _option_values(tail, {"--output"})


def _git_effective_cwd(tokens: list[str], cwd: Path) -> Path:
    """Apply Git's leading `-C` options to resolve command output paths."""
    current = cwd
    rest = list(tokens[1:])
    index = 0
    while index < len(rest):
        item = rest[index]
        if item == "-C" and index + 1 < len(rest):
            target = Path(rest[index + 1]).expanduser()
            current = target if target.is_absolute() else current / target
            index += 2
            continue
        if item in _GIT_VALUE_FLAGS:
            index += 2
            continue
        if item.startswith("-"):
            index += 1
            continue
        break
    return current


def _git_has_embedded_execution(tokens: list[str]) -> bool:
    sub, tail = _git_segment_details(tokens)
    if _git_global_config_may_execute(tokens):
        return True
    if any(item == "--exec-path" or item.startswith("--exec-path=")
           for item in tokens[1:]):
        return True
    if any(item in _GIT_EMBEDDED_EXEC_FLAGS for item in tail):
        return True
    return sub == "grep" and any(
        item == "--open-files-in-pager"
        or item.startswith("--open-files-in-pager=")
        for item in tail
    )


def _gh_segment_read_only(tokens: list[str]) -> bool:
    rest = list(tokens[1:])
    if any(item == "--web" or item.startswith("--web=") for item in rest):
        return False
    words = []
    index = 0
    while index < len(rest):
        item = rest[index]
        if item in _GH_GLOBAL_VALUE_FLAGS:
            index += 2
            continue
        if any(item.startswith(flag + "=") for flag in _GH_GLOBAL_VALUE_FLAGS
               if flag.startswith("--")):
            index += 1
            continue
        if not item.startswith("-"):
            words.append(item)
            if len(words) == 2:
                break
        index += 1
    if not words:
        return any(item in {"--help", "--version"} for item in rest)
    if words[0] in {"help", "status"}:
        return True
    return len(words) >= 2 and (words[0], words[1]) in _GH_READ_ACTIONS


def _git_config_read_only(tail: list[str]) -> bool:
    """Recognize explicit reads and the legacy `config <name>` query form."""
    if not tail:
        return False
    if tail[0] in _GIT_CONFIG_WRITE_ACTIONS:
        return False
    if tail[0] in _GIT_CONFIG_READ_ACTIONS:
        return True
    if any(item in _GIT_CONFIG_WRITE_FLAGS for item in tail):
        return False
    if any(item in _GIT_CONFIG_READ_FLAGS for item in tail):
        return True

    operands = []
    index = 0
    positional_only = False
    while index < len(tail):
        item = tail[index]
        if positional_only:
            operands.append(item)
        elif item == "--":
            positional_only = True
        elif item in _GIT_CONFIG_VALUE_FLAGS:
            if index + 1 >= len(tail):
                return False
            index += 1
        elif any(item.startswith(flag + "=") for flag in _GIT_CONFIG_VALUE_FLAGS
                 if flag.startswith("--")):
            pass
        elif item in _GIT_CONFIG_NEUTRAL_FLAGS:
            pass
        elif item.startswith("-"):
            return False
        else:
            operands.append(item)
        index += 1
    return len(operands) == 1


def _git_notes_read_only(tail: list[str]) -> bool:
    rest = list(tail)
    while rest and (
            rest[0] in {"--ref", "--no-ref"}
            or rest[0].startswith("--ref=")):
        if rest[0] == "--ref":
            if len(rest) < 2:
                return False
            rest = rest[2:]
        else:
            rest = rest[1:]
    if not rest:
        return True
    if rest[0] in {"-h", "--help"}:
        return True
    return rest[0] in {"list", "show", "get-ref"}


def _git_replace_read_only(tail: list[str]) -> bool:
    mutating = {
        "-d", "--delete", "-e", "--edit", "-f", "--force", "-g", "--graft",
        "--convert-graft-file", "--raw",
    }
    if any(item in mutating for item in tail):
        return False
    positional = []
    list_mode = False
    index = 0
    while index < len(tail):
        item = tail[index]
        if item == "--":
            positional.extend(tail[index + 1:])
            break
        if item in {"-l", "--list"}:
            list_mode = True
        elif item == "--format":
            if index + 1 >= len(tail):
                return False
            index += 1
        elif item.startswith("--format=") or item in {"-h", "--help"}:
            pass
        elif item.startswith("-"):
            return False
        else:
            positional.append(item)
        index += 1
    return not positional or (list_mode and len(positional) == 1)


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
        return _git_config_read_only(tail)
    if sub == "tag":
        return not positional or any(item in {"-l", "--list"} for item in tail)
    if sub == "stash":
        return bool(tail) and tail[0] in {"list", "show", "-h", "--help"}
    if sub == "notes":
        return _git_notes_read_only(tail)
    if sub == "replace":
        return _git_replace_read_only(tail)
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
        if any(
            item in {
                "-I", "--use-compress-program", "--checkpoint-action",
                "--to-command", "--rsh-command", "--info-script",
                "--new-volume-script", "--index-file", "--volno-file",
            }
            or any(item.startswith(flag + "=") for flag in {
                "--use-compress-program", "--checkpoint-action", "--to-command",
                "--rsh-command", "--info-script", "--new-volume-script",
                "--index-file", "--volno-file",
            })
            or (item.startswith("-I") and len(item) > 2)
            for item in args
        ):
            return "opaque"
        # Handle both `-xzf` and old-style bundled `xvf` option words; extract,
        # create, update, and append all write files or archives.
        first_word = args[0] if args else ""
        option_words = [a.lstrip("-") for a in args if a.startswith("-")]
        if not first_word.startswith("-") and first_word:
            option_words.append(first_word)
        if any(set(word) & set("xcuUrA") for word in option_words):
            return "opaque"
        return "read_only"
    if executable == "wget":
        # wget writes downloads (and commonly its HSTS store) by default.
        return "opaque"
    if executable == "curl":
        if any(a in {"-O", "--remote-name", "--remote-name-all", "-K", "--config"}
               or a.startswith("--config=") for a in args):
            return "opaque"
        if any(a.startswith("-") and not a.startswith("--") and len(a) > 2
               and any(flag in a[1:] for flag in "oOcDK") for a in args):
            return "opaque"
        if any(a == "--output-dir" or a.startswith("--output-dir=") for a in args):
            return "opaque"
        write_out_paths, write_out_opaque = _curl_write_out_details(args)
        if write_out_opaque:
            return "opaque"
        if write_out_paths:
            return "pathed"
        if any(value != "-" for value in _option_values(
                args, _CURL_PATH_FLAGS, attached_short=True)):
            return "pathed"
        return "read_only"
    if OPAQUE_INTERPRETERS.match(executable):
        return "opaque"
    if executable == "git":
        if _git_has_embedded_execution(tokens):
            return "opaque"
        if _git_output_paths(tokens):
            return "pathed"
        return "read_only" if _git_segment_read_only(tokens) else "git_write"
    if executable == "gh":
        return "read_only" if _gh_segment_read_only(tokens) else "opaque"
    if executable == "rg":
        if any(a in {"--pre", "--hostname-bin"}
               or a.startswith("--pre=")
               or a.startswith("--hostname-bin=") for a in args):
            return "opaque"
        return "read_only"
    if executable == "fd":
        if any(a in {"-x", "-X", "--exec", "--exec-batch"}
               or (a.startswith("-x") and len(a) > 2)
               or (a.startswith("-X") and len(a) > 2)
               or a.startswith("--exec=")
               or a.startswith("--exec-batch=") for a in args):
            return "opaque"
        return "read_only"
    if executable == "ag":
        if any(a == "--pager" or a.startswith("--pager=") for a in args):
            return "opaque"
        return "read_only"
    if executable == "find":
        if any(a in {"-delete", "-exec", "-execdir", "-ok", "-okdir"} for a in args):
            return "opaque"
        if any(a in _FIND_OUTPUT_ACTIONS for a in args):
            return "pathed"
        return "read_only"
    if executable == "tree":
        if _tree_output_values(args):
            return "pathed"
        return "read_only"
    if executable == "xxd":
        return "pathed" if len(_positionals(args, _XXD_VALUE_FLAGS)) >= 2 else "read_only"
    if executable == "sysctl":
        if any(a == "-w" or a == "--write" or a.startswith("--write=")
               or (a.startswith("-") and not a.startswith("--") and "w" in a[1:])
               for a in args):
            return "opaque"
        return "read_only"
    if executable == "kill":
        return "opaque"
    if executable == "sed":
        if any(_IN_PLACE_FLAG.match(a) for a in args if a.startswith("-")):
            return "opaque"
        scripts = [a for a in args if not a.startswith("-")]
        if scripts and _SED_PRINT_ONLY.match(scripts[0]):
            return "read_only"
        # sed scripts can write via the w command (`s///w file`, `1w file`).
        return "opaque"
    if executable in {"perl", "awk", "gawk", "mawk", "nawk", "yq"}:
        # General-purpose or file-writing DSLs: inline code can open, write,
        # or in-place edit arbitrary paths (awk print > file, perl open,
        # yq -i).
        if executable == "perl" and any(
                _IN_PLACE_FLAG.match(a) for a in args if a.startswith("-")):
            return "opaque"
        return "opaque"
    if executable == "sort":
        if any(
            a in {"-T", "--temporary-directory", "--compress-program"}
            or a.startswith("-T")
            or a.startswith("--temporary-directory=")
            or a.startswith("--compress-program=")
            for a in args
        ):
            return "opaque"
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
    if executable in {"cp", "mv", "ln"} and any(
            a == "-b" or a == "--backup" or a.startswith("--backup=")
            or (a.startswith("-") and not a.startswith("--") and "b" in a[1:])
            for a in args):
        return "opaque"
    if executable in {"touch", "mkdir", "rm", "tee", "cp", "mv", "ln"}:
        return "pathed"
    if executable == "cd":
        return "read_only" if (
            len(args) == 1 and not args[0].startswith("-")
            and not _GLOB_CHARS.search(args[0])
        ) else "opaque"
    if executable == "printf":
        return "opaque" if any(
            a == "-v" or a.startswith("--assign=") for a in args
        ) else "read_only"
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
    if command and (
            _SHELL_SUBSTITUTION.search(command)
            or _SHELL_DYNAMIC_EXPANSION.search(command)):
        return "opaque"
    strongest = "read_only"
    segments = _shell_command_segments(command)
    if command.strip() and not segments:
        return "opaque"
    for segment, _connector in segments:
        redirection_paths, command_tokens, malformed = _write_redirection_details(segment)
        if malformed:
            return "opaque"
        if redirection_paths:
            if _VERDICT_RANK[strongest] < _VERDICT_RANK["pathed"]:
                strongest = "pathed"
        tokens = _strip_command_prefixes(command_tokens)
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

    Branch deletion/rename, reflog expiry, pruning fetches, config writes, gc,
    tag deletion/force-update, forced or deleting pushes, and worktree removal
    hit state that all checkouts of the repository see, so per-checkout
    exclusivity is not enough for them.
    """
    sub, tail = _git_segment_details(tokens)
    flags = [item for item in tail if item.startswith("-")]
    short_flags = {
        char
        for item in flags
        if item.startswith("-") and not item.startswith("--")
        for char in item[1:]
    }
    if sub == "branch":
        return bool(short_flags & {"D", "d", "m", "M", "f", "c", "C"}) or any(
            item in {"--force", "--delete", "--move", "--copy"}
            for item in flags
        )
    if sub == "reflog":
        return bool(tail) and tail[0] in {"expire", "delete", "drop"}
    if sub == "fetch":
        return "p" in short_flags or any(
            item in {"--prune", "--prune-tags"} for item in flags
        )
    if sub == "config":
        return not _git_segment_read_only(tokens)
    if sub == "tag":
        return bool(short_flags & {"d", "f"}) or any(
            item in {"--delete", "--force"} for item in flags
        )
    if sub == "stash":
        if _git_segment_read_only(tokens):
            return False
        action = tail[0] if tail else "push"
        return action not in {"apply", "create"}
    if sub in {"notes", "replace"}:
        return not _git_segment_read_only(tokens)
    if sub == "push":
        destructive_refspec = any(item.startswith(":") for item in tail)
        force_refspec = any(item.startswith("+") and len(item) > 1 for item in tail)
        force_or_delete_flag = bool(short_flags & {"f", "d"}) or any(
            item in {"--force", "--force-with-lease", "--delete",
                     "--prune", "--mirror"}
            or item.startswith("--force-with-lease=")
            for item in flags
        )
        return destructive_refspec or force_refspec or force_or_delete_flag
    if sub == "worktree":
        return bool(tail) and tail[0] in {"remove", "prune", "move"}
    return sub in {"gc", "prune", "update-ref", "pack-refs", "filter-branch"}


def _sudo_wrapped_command(tokens: list[str]) -> list[str]:
    rest = list(tokens[1:])
    index = 0
    while index < len(rest):
        item = rest[index]
        if item == "--":
            return rest[index + 1:]
        if _environment_assignment(item):
            index += 1
            continue
        if not item.startswith("-") or item == "-":
            return rest[index:]
        if item in _SUDO_VALUE_LONG_FLAGS:
            if index + 1 >= len(rest):
                return []
            index += 2
            continue
        if any(item.startswith(flag + "=") for flag in _SUDO_VALUE_LONG_FLAGS):
            index += 1
            continue
        if item.startswith("--"):
            index += 1
            continue
        short = item[1:]
        value_at = next(
            (offset for offset, flag in enumerate(short)
             if flag in _SUDO_VALUE_SHORT_FLAGS),
            None,
        )
        if value_at is not None and value_at == len(short) - 1:
            if index + 1 >= len(rest):
                return []
            index += 2
        else:
            index += 1
    return []


def _env_wrapped_command(tokens: list[str]) -> list[str]:
    rest = list(tokens[1:])
    index = 0
    while index < len(rest):
        item = rest[index]
        if item == "--":
            return rest[index + 1:]
        if _environment_assignment(item):
            index += 1
            continue
        if not item.startswith("-") or item == "-":
            return rest[index:]
        if item in _ENV_VALUE_FLAGS:
            if index + 1 >= len(rest):
                return []
            index += 2
            continue
        if any(item.startswith(flag + "=") for flag in _ENV_VALUE_FLAGS
               if flag.startswith("--")):
            index += 1
            continue
        if item in _ENV_SPLIT_FLAGS:
            if index + 1 >= len(rest):
                return []
            try:
                return shlex.split(rest[index + 1]) + rest[index + 2:]
            except ValueError:
                return []
        if item.startswith("--split-string="):
            try:
                return shlex.split(item.split("=", 1)[1]) + rest[index + 1:]
            except ValueError:
                return []
        if item in _ENV_BOOLEAN_FLAGS or any(
                item.startswith(flag + "=") for flag in _ENV_BOOLEAN_FLAGS
                if flag.startswith("--")):
            index += 1
            continue
        if len(item) > 2 and item[:2] in {"-u", "-C", "-P"}:
            index += 1
            continue
        if len(item) > 2 and item[:2] == "-S":
            try:
                return shlex.split(item[2:]) + rest[index + 1:]
            except ValueError:
                return []
        return []
    return []


def _tokens_git_shared_mutation(tokens: list[str], depth: int = 0) -> bool:
    """Recover Git behind execution wrappers without trusting generic writes."""
    if depth >= 8:
        return False
    command_tokens = _strip_command_prefixes(tokens)
    if not command_tokens:
        return False
    executable = Path(command_tokens[0]).name
    if executable == "git":
        return _git_segment_shared_mutation(command_tokens)
    if executable == "sudo":
        nested = _sudo_wrapped_command(command_tokens)
    elif executable == "env":
        nested = _env_wrapped_command(command_tokens)
    elif executable == "nohup":
        rest = command_tokens[1:]
        nested = rest[1:] if rest and rest[0] == "--" else rest
        if nested and nested[0] in {"--help", "--version"}:
            return False
    elif executable in _SHELL_COMMAND_EXECUTABLES:
        args = command_tokens[1:]
        for index, item in enumerate(args):
            shell_code = item == "-c" or (
                item.startswith("-") and not item.startswith("--")
                and "c" in item[1:]
            )
            if shell_code and index + 1 < len(args):
                return any(
                    _tokens_git_shared_mutation(segment, depth + 1)
                    for segment, _connector in _shell_command_segments(args[index + 1])
                )
        return False
    else:
        return False
    return _tokens_git_shared_mutation(nested, depth + 1)


def command_git_shared_mutation(command: str) -> bool:
    for segment, _connector in _shell_command_segments(command):
        if _tokens_git_shared_mutation(segment):
            return True
    return False


def shell_write_paths(command: str, cwd: Path | None = None) -> list[str]:
    values = []
    working_dir = (cwd or Path.cwd()).expanduser()
    discard_targets = {"/dev/null", "/dev/stdout", "/dev/stderr", "nul"}

    def add_target(raw: str, base: Path | None = None) -> None:
        value = str(raw or "").strip()
        if not value or value.lower() in discard_targets:
            return
        target = Path(value).expanduser()
        if not target.is_absolute():
            target = (base or working_dir) / target
        values.append(str(target))

    for value in re.findall(
            r"^\*\*\* (?:(?:Add|Update|Delete) File|Move to): (.+)$",
            command, flags=re.M):
        add_target(value)
    if not command:
        return values
    segments = _shell_command_segments(command)
    for segment, connector in segments:
        redirect_paths, command_tokens, _malformed = _write_redirection_details(segment)
        for value in redirect_paths:
            add_target(value)
        command_tokens = _strip_command_prefixes(command_tokens)
        if not command_tokens:
            continue
        executable = Path(command_tokens[0]).name
        command_args = command_tokens[1:]
        args = [item for item in command_args if not item.startswith("-")]
        if (executable == "cd" and len(args) == 1
                and not args[0].startswith("-")
                and not _GLOB_CHARS.search(args[0])):
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
        elif executable == "curl":
            for value in _option_values(
                    command_args, _CURL_PATH_FLAGS, attached_short=True):
                if value != "-":
                    add_target(value)
            write_out_paths, _write_out_opaque = _curl_write_out_details(command_args)
            for value in write_out_paths:
                add_target(value)
        elif executable == "tree":
            for value in _tree_output_values(command_args):
                add_target(value)
        elif executable == "find":
            for index, item in enumerate(command_args):
                if item in _FIND_OUTPUT_ACTIONS and index + 1 < len(command_args):
                    add_target(command_args[index + 1])
        elif executable == "xxd":
            positional = _positionals(command_args, _XXD_VALUE_FLAGS)
            if len(positional) >= 2:
                add_target(positional[-1])
        elif executable == "ln" and args:
            for value in _ln_output_values(command_args):
                add_target(value)
        elif executable == "git":
            sub, tail = _git_segment_details(command_tokens)
            git_cwd = _git_effective_cwd(command_tokens, working_dir)
            for value in _git_output_paths(command_tokens):
                add_target(value, git_cwd)
            if sub in {"restore", "rm", "mv", "checkout", "clean"}:
                for value in tail:
                    if value.startswith("-") or value == "--":
                        continue
                    if sub == "checkout" and "--" not in tail:
                        continue  # branch switch, not a path restore
                    add_target(value)
        elif executable == "cp" and args:
            target_dirs = _target_directory_values(command_args)
            add_target(target_dirs[-1] if target_dirs else args[-1])
        elif executable == "mv" and args:
            for value in _mv_write_values(command_args):
                add_target(value)
        elif executable in {"sed", "perl"} and args and any(
                item == "--in-place"
                or item.startswith("--in-place=")
                or (item.startswith("-") and not item.startswith("--")
                    and "i" in item[1:])
                for item in command_args):
            targets = (
                _sed_in_place_targets(command_args)
                if executable == "sed"
                else _perl_in_place_targets(command_args)
            )
            for value in targets or []:
                add_target(value)
    return list(dict.fromkeys(values))


def _tool_name(payload: dict) -> str:
    return str(
        payload.get("tool_name") or payload.get("tool")
        or payload.get("toolType") or ""
    )


def _tool_leaf(payload: dict) -> str:
    raw = re.split(r"__|[./:]", _tool_name(payload))[-1]
    return re.sub(r"[^a-z0-9]", "", raw.lower())


_PATH_BOUNDED_MUTATION_TOOLS = {
    "write", "edit", "multiedit", "applypatch", "notebookedit",
    "strreplace", "save", "writefile", "writemultiplefiles", "editfile",
    "appendfile", "patchfile", "createfile", "touchfile", "deletefile",
    "movefile", "copyfile", "renamefile", "createdirectory",
    "makedirectory", "deletedirectory", "move", "copy", "rename",
}
_READ_ONLY_TOOLS = {
    "read", "glob", "grep", "websearch", "webfetch", "skill",
    "todowrite", "askuserquestion", "updateplan", "requestuserinput",
    "viewimage", "getgoal", "updategoal", "creategoal", "toolsearch",
    "listmcpresources", "listmcpresourcetemplates", "readmcpresource",
    "enterplanmode", "exitplanmode", "readfile", "readdirectory",
    "listdirectory", "directorytree", "getfileinfo", "searchfiles",
}
_STRUCTURED_PATH_KEYS = {
    "file_path", "filepath", "notebook_path", "notebookpath", "path",
    "source", "source_path", "sourcepath", "src", "destination",
    "destination_path", "destinationpath", "dest", "dst", "target_path",
    "targetpath", "old_path", "oldpath", "new_path", "newpath",
    "directory", "directory_path", "directorypath", "target",
}


def _tool_is_read_only(payload: dict) -> bool:
    return _tool_leaf(payload) in _READ_ONLY_TOOLS


def _tool_is_path_bounded_mutation(payload: dict) -> bool:
    return _tool_leaf(payload) in _PATH_BOUNDED_MUTATION_TOOLS


def _structured_paths(value) -> list[str]:
    paths = []
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = str(key).lower().replace("-", "_")
            if normalized in _STRUCTURED_PATH_KEYS and isinstance(
                    item, (str, os.PathLike)):
                paths.append(str(item))
            elif isinstance(item, (dict, list, tuple)):
                paths.extend(_structured_paths(item))
    elif isinstance(value, (list, tuple)):
        for item in value:
            paths.extend(_structured_paths(item))
    return paths


def payload_paths(payload: dict) -> list[str]:
    tool_input = payload.get("tool_input") or {}
    values = []
    if _tool_is_path_bounded_mutation(payload):
        values.extend(_structured_paths(tool_input))
        values.extend(_structured_paths(payload))
        patch = str(tool_input.get("patch") or tool_input.get("input") or "")
        values.extend(re.findall(
            r"^\*\*\* (?:(?:Add|Update|Delete) File|Move to): (.+)$",
            patch, flags=re.M,
        ))
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
        elif any(_GLOB_CHARS.search(path) for path in paths):
            command.append("--opaque")
    elif is_mutating_tool(payload) and (
            not _tool_is_path_bounded_mutation(payload) or not paths):
        command.append("--opaque")
    result = run(command, root, env)
    if result.returncode == 0:
        return True, result.stdout.strip()
    return False, (result.stderr or result.stdout).strip()


def heartbeat(root: Path, env: dict) -> None:
    agentctl = root / "tools" / "agentctl.py"
    run([sys.executable, str(agentctl), "sessions", "heartbeat"], root, env)


def is_finalization_action(payload: dict) -> bool:
    if _tool_is_path_bounded_mutation(payload):
        return False
    command = payload_command(payload)
    if not command:
        return False
    if "agentctl.py" in command:
        return True
    return re.search(r"\b(?:git\s+(?:status|diff|add|commit|push|log|show|remote)|gh\s+(?:repo|pr|issue))\b", command) is not None


def is_mutating_tool(payload: dict) -> bool:
    command = payload_command(payload)
    if _tool_is_path_bounded_mutation(payload):
        return True
    if command:
        if command_is_agentctl_start(command):
            return False
        return (
            bool(payload_paths(payload))
            or MUTATING_BASH.search(command) is not None
            or classify_shell_command(command) != "read_only"
        )
    if _tool_leaf(payload) in {"bash", "shell"}:
        return True
    return not _tool_is_read_only(payload)


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
    env = resolve_existing_session_environment(
        root, payload, hook_environment(payload, create_fork_instance=True),
        enforce_runtime_binding_conflict=False,
    )
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
    env = resolve_existing_session_environment(
        root, payload, hook_environment(payload),
        claim_runtime_binding=command_is_agentctl_start(payload_command(payload)),
    )
    identity_error = session_identity_error(env)
    mutating = is_mutating_tool(payload)
    controller_mutation = command_requires_agentctl_identity(payload_command(payload))
    if identity_error and (mutating or controller_mutation):
        return block(
            "PreToolUse",
            "Agent Workflow Kit blocked this action because this conversation does "
            "not have a unique workflow identity. " + identity_error + ".",
        )
    active = has_session(root, env)
    if not mutating:
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
    env = resolve_existing_session_environment(root, payload, hook_environment(payload))
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
