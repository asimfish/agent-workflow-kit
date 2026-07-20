"""Fresh-install regression coverage for concurrent conversations in one checkout."""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


KIT = Path(__file__).resolve().parents[1]
PROVIDER_ENV = (
    "CODEX_THREAD_ID",
    "CLAUDE_CODE_SESSION_ID",
    "CURSOR_CONVERSATION_ID",
    "WHALENT_AGENT_ID",
    "WHALENT_CODEX_INSTANCE_ID",
    "WHALENT_COMPOSER_ID",
    "WHALENT_FORK_SOURCE_AGENT_ID",
    "AGENT_SESSION_ID",
)
WORKFLOW_ENV = (
    "AGENT_WORKFLOW_SESSION_ID",
    "AGENT_WORKFLOW_SESSION_KEY",
    "AGENT_WORKFLOW_SESSION_OWNER_RUNTIME",
    "AGENT_WORKFLOW_SESSION_INSTANCE_ID",
    "AGENT_WORKFLOW_PARENT_SESSION_KEY",
    "AGENT_WORKFLOW_SESSION_ISOLATION_ERROR",
)


class MultiSessionWorkflowRegressionTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="awk-sessions-regress-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True, timeout=60)
        install = subprocess.run(
            [sys.executable, str(KIT / "tools" / "agentctl.py"), "init", str(self.root)],
            cwd=KIT, text=True, capture_output=True, timeout=120,
        )
        self.assertEqual(install.returncode, 0, install.stdout + install.stderr)

    def env(self, session):
        env = os.environ.copy()
        for name in (*PROVIDER_ENV, *WORKFLOW_ENV):
            env.pop(name, None)
        env["AGENT_WORKFLOW_SESSION_ID"] = session
        return env

    def bare_env(self):
        env = os.environ.copy()
        for name in (*PROVIDER_ENV, *WORKFLOW_ENV):
            env.pop(name, None)
        return env

    def agentctl_env(self, env, *args, expect=0):
        proc = subprocess.run(
            [sys.executable, "tools/agentctl.py", *args], cwd=self.root,
            env=env, text=True, capture_output=True, timeout=120,
        )
        self.assertEqual(proc.returncode, expect, proc.stdout + proc.stderr)
        return proc

    def agentctl(self, *args, session="one", expect=0):
        proc = subprocess.run(
            [sys.executable, "tools/agentctl.py", *args], cwd=self.root,
            env=self.env(session), text=True, capture_output=True, timeout=120,
        )
        self.assertEqual(proc.returncode, expect, proc.stdout + proc.stderr)
        return proc

    def hook(self, event, payload, session="one"):
        return subprocess.run(
            [sys.executable, "tools/agent_workflow_hook.py", event], cwd=self.root,
            env=self.env(session), input=json.dumps(payload), text=True,
            capture_output=True, timeout=120,
        )

    def start(self, session, task, scope, agent="codex"):
        return self.agentctl(
            "work", "--agent", agent, "--auto-create", "--new-id", task,
            "--title", f"work for {session}", "--scope", scope,
            session=session,
        )

    def sessions(self, session="one"):
        return json.loads(
            self.agentctl("sessions", "list", "--json", session=session).stdout
        )

    def test_disjoint_conversations_keep_independent_state_and_visible_status(self):
        self.start("one", "T-101", "src/one/")
        self.start("two", "T-102", "src/two/")

        one = json.loads(self.agentctl("status", "--json", session="one").stdout)
        two = json.loads(self.agentctl("status", "--json", session="two").stdout)
        self.assertEqual(one["task"], "T-101")
        self.assertEqual(two["task"], "T-102")
        self.assertNotEqual(one["workflow_session_key"], two["workflow_session_key"])

        rows = self.sessions("one")["sessions"]
        self.assertEqual({row["task"] for row in rows}, {"T-101", "T-102"})
        self.assertEqual({row["observed_status"] for row in rows}, {"active"})
        view = (self.root / ".agent" / "state" / "SESSIONS.md").read_text(encoding="utf-8")
        self.assertIn("T-101", view)
        self.assertIn("T-102", view)

        context = self.hook("session-start", {"source": "resume"}, session="one")
        self.assertEqual(context.returncode, 0, context.stderr)
        self.assertIn("T-102", json.loads(context.stdout)["additional_context"])
        self.agentctl("refresh", session="one")
        peer_notice = self.hook(
            "pre-tool-use",
            {"tool_name": "Write", "tool_input": {"file_path": "src/one/notice.py"}},
            session="one",
        )
        self.assertIn(
            "changed peer-session snapshot",
            json.loads(peer_notice.stdout)["additional_context"],
        )
        unchanged = self.hook(
            "pre-tool-use",
            {"tool_name": "Write", "tool_input": {"file_path": "src/one/notice.py"}},
            session="one",
        )
        self.assertEqual(unchanged.stdout, "")

    def test_hook_session_ids_persist_for_claude_and_return_cursor_environment(self):
        env_file = self.root / "claude-session.env"
        claude_env = self.bare_env()
        claude_env["CLAUDE_ENV_FILE"] = str(env_file)
        started = subprocess.run(
            [sys.executable, "tools/agent_workflow_hook.py", "session-start"],
            cwd=self.root, env=claude_env,
            input=json.dumps({"session_id": "claude-conversation-1", "source": "startup"}),
            text=True, capture_output=True, timeout=120,
        )
        self.assertEqual(started.returncode, 0, started.stderr)
        output = json.loads(started.stdout)
        self.assertEqual(output["env"]["AGENT_WORKFLOW_SESSION_ID"], "claude-conversation-1")
        self.assertIn("AGENT_WORKFLOW_SESSION_ID", env_file.read_text(encoding="utf-8"))

        work_env = self.bare_env()
        work_env.update(output["env"])
        work = subprocess.run(
            [
                sys.executable, "tools/agentctl.py", "work", "--agent", "claude",
                "--auto-create", "--new-id", "T-111", "--title", "claude hook identity",
                "--scope", "src/claude/",
            ],
            cwd=self.root, env=work_env, text=True, capture_output=True, timeout=120,
        )
        self.assertEqual(work.returncode, 0, work.stdout + work.stderr)

        pre_tool = subprocess.run(
            [sys.executable, "tools/agent_workflow_hook.py", "pre-tool-use"],
            cwd=self.root, env=self.bare_env(),
            input=json.dumps({
                "session_id": "claude-conversation-1",
                "tool_name": "Write",
                "tool_input": {"file_path": "src/claude/file.py"},
            }),
            text=True, capture_output=True, timeout=120,
        )
        self.assertEqual(pre_tool.returncode, 0, pre_tool.stderr)
        self.assertEqual(pre_tool.stdout, "")

        nested_env = self.bare_env()
        nested_env["CODEX_THREAD_ID"] = "outer-codex-conversation"
        nested_pre_tool = subprocess.run(
            [sys.executable, "tools/agent_workflow_hook.py", "pre-tool-use"],
            cwd=self.root, env=nested_env,
            input=json.dumps({
                "session_id": "claude-conversation-1",
                "tool_name": "Write",
                "tool_input": {"file_path": "src/claude/nested.py"},
            }),
            text=True, capture_output=True, timeout=120,
        )
        self.assertEqual(nested_pre_tool.returncode, 0, nested_pre_tool.stderr)
        self.assertEqual(nested_pre_tool.stdout, "")

        cursor = subprocess.run(
            [sys.executable, "tools/agent_workflow_hook.py", "session-start"],
            cwd=self.root, env=self.bare_env(),
            input=json.dumps({"conversation_id": "cursor-conversation-2"}),
            text=True, capture_output=True, timeout=120,
        )
        self.assertEqual(
            json.loads(cursor.stdout)["env"]["AGENT_WORKFLOW_SESSION_ID"],
            "cursor-conversation-2",
        )

    def test_same_terminal_session_starts_generate_distinct_workflow_identities(self):
        shared_terminal = self.bare_env()
        shared_terminal["TERM_SESSION_ID"] = "shared-terminal-window"
        exports = []
        for _index in range(2):
            started = subprocess.run(
                [sys.executable, "tools/agent_workflow_hook.py", "session-start"],
                cwd=self.root,
                env=shared_terminal,
                input=json.dumps({"source": "startup"}),
                text=True,
                capture_output=True,
                timeout=120,
            )
            self.assertEqual(started.returncode, 0, started.stderr)
            exports.append(json.loads(started.stdout)["env"])

        first_id = exports[0]["AGENT_WORKFLOW_SESSION_ID"]
        second_id = exports[1]["AGENT_WORKFLOW_SESSION_ID"]
        self.assertTrue(first_id)
        self.assertTrue(second_id)
        self.assertNotEqual(first_id, second_id)

        first_env = shared_terminal.copy()
        first_env.update(exports[0])
        second_env = shared_terminal.copy()
        second_env.update(exports[1])
        self.agentctl_env(
            first_env, "work", "--agent", "codex", "--auto-create",
            "--new-id", "T-190", "--title", "first terminal conversation",
            "--scope", "src/terminal-one/",
        )
        self.agentctl_env(
            second_env, "work", "--agent", "codex", "--auto-create",
            "--new-id", "T-191", "--title", "second terminal conversation",
            "--scope", "src/terminal-two/",
        )
        first = json.loads(
            self.agentctl_env(first_env, "status", "--json").stdout
        )
        second = json.loads(
            self.agentctl_env(second_env, "status", "--json").stdout
        )
        self.assertEqual(first["task"], "T-190")
        self.assertEqual(second["task"], "T-191")
        self.assertNotEqual(first["workflow_session_key"], second["workflow_session_key"])
        self.assertEqual(first["identity_source"], "session_start")
        self.assertEqual(second["identity_source"], "session_start")

    def test_terminal_only_identity_blocks_direct_and_hook_mutations(self):
        terminal_only = self.bare_env()
        terminal_only["TERM_SESSION_ID"] = "shared-terminal-window"

        direct = self.agentctl_env(
            terminal_only, "work", "--agent", "codex", "--auto-create",
            "--new-id", "T-192", "--title", "unsafe terminal fallback",
            "--scope", "src/unsafe/", expect=2,
        )
        self.assertIn("unique conversation identity", direct.stderr)

        hooked = subprocess.run(
            [sys.executable, "tools/agent_workflow_hook.py", "pre-tool-use"],
            cwd=self.root,
            env=terminal_only,
            input=json.dumps({
                "tool_name": "Bash",
                "tool_input": {
                    "command": "python3 tools/agentctl.py work --agent codex",
                },
            }),
            text=True,
            capture_output=True,
            timeout=120,
        )
        self.assertEqual(hooked.returncode, 0, hooked.stderr)
        decision = json.loads(hooked.stdout)
        self.assertEqual(decision["decision"], "block")
        self.assertIn("unique conversation identity", decision["reason"])

    def test_terminal_only_identity_blocks_non_session_controller_mutations(self):
        terminal_only = self.bare_env()
        terminal_only["TERM_SESSION_ID"] = "shared-terminal-window"

        direct = self.agentctl_env(
            terminal_only, "task", "create", "--id", "T-193",
            "--title", "unsafe global mutation", "--owner", "codex",
            "--scope", "src/unsafe-global/", expect=2,
        )
        self.assertIn("unique conversation identity", direct.stderr)
        self.assertFalse((self.root / ".agent" / "tasks" / "T-193.md").exists())

        hooked = subprocess.run(
            [sys.executable, "tools/agent_workflow_hook.py", "pre-tool-use"],
            cwd=self.root,
            env=terminal_only,
            input=json.dumps({
                "tool_name": "Bash",
                "tool_input": {
                    "command": (
                        "python3 tools/agentctl.py task create --id T-193 "
                        "--title unsafe --owner codex --scope src/unsafe-global/"
                    ),
                },
            }),
            text=True,
            capture_output=True,
            timeout=120,
        )
        self.assertEqual(hooked.returncode, 0, hooked.stderr)
        decision = json.loads(hooked.stdout)
        self.assertEqual(decision["decision"], "block")
        self.assertIn("unique workflow identity", decision["reason"])

        module_hook = subprocess.run(
            [sys.executable, "tools/agent_workflow_hook.py", "pre-tool-use"],
            cwd=self.root,
            env=terminal_only,
            input=json.dumps({
                "tool_name": "Bash",
                "tool_input": {
                    "command": (
                        "python3 -m tools.agentctl task create --id T-194 "
                        "--title unsafe --owner codex --scope src/unsafe-module/"
                    ),
                },
            }),
            text=True,
            capture_output=True,
            timeout=120,
        )
        self.assertEqual(module_hook.returncode, 0, module_hook.stderr)
        module_decision = json.loads(module_hook.stdout)
        self.assertEqual(module_decision["decision"], "block")
        self.assertIn("unique workflow identity", module_decision["reason"])

        module_direct = subprocess.run(
            [
                sys.executable, "-m", "tools.agentctl", "task", "create",
                "--id", "T-194", "--title", "unsafe module mutation",
                "--owner", "codex", "--scope", "src/unsafe-module/",
            ],
            cwd=self.root,
            env=terminal_only,
            text=True,
            capture_output=True,
            timeout=120,
        )
        self.assertEqual(module_direct.returncode, 2, module_direct.stdout + module_direct.stderr)
        self.assertIn("unique conversation identity", module_direct.stderr)
        self.assertFalse((self.root / ".agent" / "tasks" / "T-194.md").exists())

        shown = self.agentctl_env(terminal_only, "task", "show", "T-000")
        self.assertIn("T-000", shown.stdout)
        audited = self.agentctl_env(
            terminal_only, "migrate", "--json", expect=1,
        )
        self.assertEqual(json.loads(audited.stdout)["action"], "restart")

    def test_terminal_only_sessions_list_does_not_write_generated_state(self):
        terminal_only = self.bare_env()
        terminal_only["TERM_SESSION_ID"] = "shared-terminal-window"
        view = self.root / ".agent" / "state" / "SESSIONS.md"
        common_dir = subprocess.run(
            ["git", "rev-parse", "--git-common-dir"],
            cwd=self.root,
            text=True,
            capture_output=True,
            check=True,
            timeout=60,
        ).stdout.strip()
        common_path = Path(common_dir)
        if not common_path.is_absolute():
            common_path = self.root / common_path
        lock = common_path.resolve() / "agent-workflow" / "sessions.lock"
        view.unlink(missing_ok=True)
        lock.unlink(missing_ok=True)

        listed = self.agentctl_env(terminal_only, "sessions", "list", "--json")

        self.assertEqual(json.loads(listed.stdout)["sessions"], [])
        self.assertFalse(view.exists())
        self.assertFalse(lock.exists())

        trusted = self.env("trusted-list-conversation")
        self.agentctl_env(trusted, "sessions", "list", "--json")
        self.assertTrue(view.is_file())
        self.assertTrue(lock.is_file())

    def test_identity_policy_covers_every_controller_command_leaf(self):
        import argparse
        from tools import agent_workflow_hook as workflow_hook
        from tools import agentctl

        def leaf_paths(parser, prefix=()):
            action = next(
                (
                    item for item in parser._actions
                    if isinstance(item, argparse._SubParsersAction)
                ),
                None,
            )
            if action is None:
                return {prefix}
            paths = set()
            for name, child in action.choices.items():
                paths.update(leaf_paths(child, prefix + (name,)))
            return paths

        expected = {
            ("init",), ("work",), ("start",), ("focus",), ("progress",),
            ("note",), ("complete",), ("finish",), ("gate",), ("refresh",),
            ("board",), ("task", "create"), ("task", "show"),
            ("agents", "add"), ("agents", "list"),
            ("handoff", "create"), ("handoff", "list"),
            ("handoff", "show"), ("handoff", "mark"),
            ("worktree", "create"), ("worktree", "list"),
            ("worktree", "release"), ("eval", "list"), ("eval", "run"),
            ("eval", "show"), ("eval", "compare"), ("eval", "gate"),
            ("guidance", "create"), ("guidance", "list"),
            ("guidance", "show"), ("guidance", "ack"),
            ("guidance", "dispatch"), ("guidance", "verify"),
            ("loop", "list"), ("loop", "show"), ("loop", "run"),
            ("loop", "auto"), ("loop", "cycle"), ("loop", "status"),
            ("loop", "resume"), ("loop", "stop"), ("check",),
            ("doctor",), ("migrate",), ("sessions", "list"),
            ("sessions", "heartbeat"), ("sessions", "guard"),
            ("sessions", "release"), ("status",),
        }
        discovered = leaf_paths(agentctl.build_parser())
        self.assertEqual(discovered, expected)
        self.assertEqual(
            agentctl.IDENTITY_FREE_COMMAND_PATHS,
            workflow_hook.IDENTITY_FREE_COMMAND_PATHS,
        )
        self.assertLessEqual(agentctl.IDENTITY_FREE_COMMAND_PATHS, discovered)

        for path in sorted(discovered):
            namespace = argparse.Namespace(cmd=path[0])
            action_attr = agentctl.COMMAND_ACTION_ATTRS.get(path[0])
            if action_attr:
                setattr(namespace, action_attr, path[1])
            expected_required = path not in agentctl.IDENTITY_FREE_COMMAND_PATHS
            self.assertEqual(
                agentctl._command_requires_trusted_identity(namespace),
                expected_required,
                path,
            )
            command = "python3 tools/agentctl.py " + " ".join(path)
            self.assertEqual(
                workflow_hook.command_requires_agentctl_identity(command),
                expected_required,
                path,
            )
            module_command = "python3 -m tools.agentctl " + " ".join(path)
            self.assertEqual(
                workflow_hook.command_requires_agentctl_identity(module_command),
                expected_required,
                ("module", *path),
            )

        self.assertTrue(
            workflow_hook.command_requires_agentctl_identity(
                "python3 -m tools.agentctl"
            )
        )
        self.assertFalse(
            workflow_hook.command_requires_agentctl_identity(
                "python3 -m tools.agentctl --help"
            )
        )

        chained = (
            "python3 tools/agentctl.py task show T-000 && "
            "python3 tools/agentctl.py task create --id T-999"
        )
        self.assertTrue(workflow_hook.command_requires_agentctl_identity(chained))

    def test_fork_inheriting_parent_workflow_id_uses_child_runtime(self):
        parent_env = self.bare_env()
        parent_env.update({
            "CODEX_THREAD_ID": "shared-thread",
            "WHALENT_AGENT_ID": "parent-agent",
        })
        parent_hook = subprocess.run(
            [sys.executable, "tools/agent_workflow_hook.py", "session-start"],
            cwd=self.root, env=parent_env,
            input=json.dumps({"session_id": "parent-workflow-id", "source": "startup"}),
            text=True, capture_output=True, timeout=120,
        )
        self.assertEqual(parent_hook.returncode, 0, parent_hook.stderr)
        parent_env.update(json.loads(parent_hook.stdout)["env"])
        self.agentctl_env(
            parent_env, "work", "--agent", "codex", "--auto-create",
            "--new-id", "T-160", "--title", "parent fork source",
            "--scope", "src/parent/",
        )

        child_env = parent_env.copy()
        child_env.update({
            "CODEX_THREAD_ID": "child-thread",
            "WHALENT_AGENT_ID": "child-agent",
            "WHALENT_FORK_SOURCE_AGENT_ID": "parent-agent",
        })
        child_hook = subprocess.run(
            [sys.executable, "tools/agent_workflow_hook.py", "session-start"],
            cwd=self.root, env=child_env,
            input=json.dumps({
                "session_id": "parent-workflow-id",
                "source": "startup",
            }),
            text=True, capture_output=True, timeout=120,
        )
        self.assertEqual(child_hook.returncode, 0, child_hook.stderr)
        child_exports = json.loads(child_hook.stdout)["env"]
        self.assertEqual(child_exports["AGENT_WORKFLOW_SESSION_ID"], "")
        child_env.update(child_exports)
        self.agentctl_env(
            child_env, "work", "--agent", "codex", "--auto-create",
            "--new-id", "T-161", "--title", "fork child work",
            "--scope", "src/child/",
        )

        parent = json.loads(
            self.agentctl_env(parent_env, "status", "--json").stdout
        )
        child = json.loads(
            self.agentctl_env(child_env, "status", "--json").stdout
        )
        self.assertEqual(parent["task"], "T-160")
        self.assertEqual(child["task"], "T-161")
        self.assertNotEqual(
            parent["workflow_session_key"], child["workflow_session_key"],
        )
        self.assertRegex(child["parent_session_key"], r"^lineage-[0-9a-f]{24}$")

    def test_same_id_forks_get_instances_and_missing_instance_fails_closed(self):
        self.start("cloned-session", "T-170", "src/parent/")

        first_start = subprocess.run(
            [sys.executable, "tools/agent_workflow_hook.py", "session-start"],
            cwd=self.root, env=self.bare_env(),
            input=json.dumps({
                "session_id": "cloned-session",
                "parent_session_id": "cloned-session",
                "fork_id": "fork-one",
                "source": "startup",
            }),
            text=True, capture_output=True, timeout=120,
        )
        self.assertEqual(first_start.returncode, 0, first_start.stderr)
        first_env = self.bare_env()
        first_env.update(json.loads(first_start.stdout)["env"])
        self.agentctl_env(
            first_env, "work", "--agent", "codex", "--auto-create",
            "--new-id", "T-171", "--title", "first cloned branch",
            "--scope", "src/fork-one/",
        )

        generated_start = subprocess.run(
            [sys.executable, "tools/agent_workflow_hook.py", "session-start"],
            cwd=self.root, env=self.bare_env(),
            input=json.dumps({
                "session_id": "cloned-session",
                "parent_session_id": "cloned-session",
                "source": "startup",
            }),
            text=True, capture_output=True, timeout=120,
        )
        self.assertEqual(generated_start.returncode, 0, generated_start.stderr)
        generated_exports = json.loads(generated_start.stdout)["env"]
        self.assertRegex(
            generated_exports["AGENT_WORKFLOW_SESSION_INSTANCE_ID"],
            r"^instance-[0-9a-f]{24}$",
        )
        generated_env = self.bare_env()
        generated_env.update(generated_exports)
        self.agentctl_env(
            generated_env, "work", "--agent", "codex", "--auto-create",
            "--new-id", "T-172", "--title", "generated cloned branch",
            "--scope", "src/fork-two/",
        )

        rows = json.loads(
            self.agentctl_env(first_env, "sessions", "list", "--json").stdout
        )["sessions"]
        fork_rows = [row for row in rows if row["task"] in {"T-171", "T-172"}]
        self.assertEqual(len(fork_rows), 2)
        self.assertEqual(len({row["workflow_session_key"] for row in fork_rows}), 2)
        self.assertTrue(all(row.get("parent_session_key") for row in fork_rows))

        missing_instance = subprocess.run(
            [sys.executable, "tools/agent_workflow_hook.py", "pre-tool-use"],
            cwd=self.root, env=self.bare_env(),
            input=json.dumps({
                "session_id": "cloned-session",
                "parent_session_id": "cloned-session",
                "tool_name": "Write",
                "tool_input": {"file_path": "src/fork-two/unsafe.py"},
            }),
            text=True, capture_output=True, timeout=120,
        )
        decision = json.loads(missing_instance.stdout)
        self.assertEqual(decision["decision"], "block")
        self.assertIn("identity distinct from its parent", decision["reason"])

    def test_persisted_fork_lineage_without_instance_fails_closed(self):
        self.start("cloned-session", "T-175", "src/parent/")

        child_env = self.bare_env()
        child_env["CODEX_THREAD_ID"] = "child-runtime"
        started = subprocess.run(
            [sys.executable, "tools/agent_workflow_hook.py", "session-start"],
            cwd=self.root, env=child_env,
            input=json.dumps({
                "session_id": "cloned-session",
                "parent_session_id": "cloned-session",
                "fork_id": "child-fork",
                "source": "startup",
            }),
            text=True, capture_output=True, timeout=120,
        )
        self.assertEqual(started.returncode, 0, started.stderr)
        child_env.update(json.loads(started.stdout)["env"])
        child_env.pop("AGENT_WORKFLOW_SESSION_INSTANCE_ID", None)

        read_only = subprocess.run(
            [sys.executable, "tools/agent_workflow_hook.py", "pre-tool-use"],
            cwd=self.root, env=child_env,
            input=json.dumps({
                "session_id": "cloned-session",
                "tool_name": "Bash",
                "tool_input": {"command": "git status"},
            }),
            text=True, capture_output=True, timeout=120,
        )
        self.assertEqual(read_only.returncode, 0, read_only.stderr)
        self.assertEqual(read_only.stdout, "")

        status_hook = subprocess.run(
            [sys.executable, "tools/agent_workflow_hook.py", "pre-tool-use"],
            cwd=self.root, env=child_env,
            input=json.dumps({
                "session_id": "cloned-session",
                "tool_name": "Bash",
                "tool_input": {
                    "command": "python3 tools/agentctl.py status --json",
                },
            }),
            text=True, capture_output=True, timeout=120,
        )
        self.assertEqual(status_hook.returncode, 0, status_hook.stderr)
        self.assertEqual(status_hook.stdout, "")
        child_status = self.agentctl_env(child_env, "status", "--json")
        self.assertEqual(json.loads(child_status.stdout), {})

        work_hook = subprocess.run(
            [sys.executable, "tools/agent_workflow_hook.py", "pre-tool-use"],
            cwd=self.root, env=child_env,
            input=json.dumps({
                "session_id": "cloned-session",
                "tool_name": "Bash",
                "tool_input": {
                    "command": "python3 tools/agentctl.py work --agent codex",
                },
            }),
            text=True, capture_output=True, timeout=120,
        )
        self.assertEqual(work_hook.returncode, 0, work_hook.stderr)
        work_decision = json.loads(work_hook.stdout)
        self.assertEqual(work_decision["decision"], "block")
        direct_work = self.agentctl_env(
            child_env, "work", "--agent", "codex", expect=2,
        )
        self.assertIn("forked conversation", direct_work.stderr)

        start_hook = subprocess.run(
            [sys.executable, "tools/agent_workflow_hook.py", "pre-tool-use"],
            cwd=self.root, env=child_env,
            input=json.dumps({
                "session_id": "cloned-session",
                "tool_name": "Bash",
                "tool_input": {
                    "command": (
                        "python3 tools/agentctl.py start --task T-175 "
                        "--agent codex"
                    ),
                },
            }),
            text=True, capture_output=True, timeout=120,
        )
        self.assertEqual(start_hook.returncode, 0, start_hook.stderr)
        start_decision = json.loads(start_hook.stdout)
        self.assertEqual(start_decision["decision"], "block")
        direct_start = self.agentctl_env(
            child_env, "start", "--task", "T-175", "--agent", "codex",
            expect=2,
        )
        self.assertIn("forked conversation", direct_start.stderr)

        checked = subprocess.run(
            [sys.executable, "tools/agent_workflow_hook.py", "pre-tool-use"],
            cwd=self.root, env=child_env,
            input=json.dumps({
                "session_id": "cloned-session",
                "tool_name": "Write",
                "tool_input": {"file_path": "src/parent/unsafe.py"},
            }),
            text=True, capture_output=True, timeout=120,
        )
        self.assertEqual(checked.returncode, 0, checked.stderr)
        decision = json.loads(checked.stdout)
        self.assertEqual(decision["decision"], "block")
        self.assertIn("identity distinct from its parent", decision["reason"])
        parent = json.loads(
            self.agentctl("status", "--json", session="cloned-session").stdout
        )
        self.assertEqual(parent["task"], "T-175")

        forced_child_env = child_env.copy()
        forced_child_env.pop("AGENT_WORKFLOW_SESSION_ID", None)
        forced_child_env["AGENT_WORKFLOW_SESSION_KEY"] = parent[
            "workflow_session_key"
        ]
        forced_status = self.agentctl_env(
            forced_child_env, "status", "--json",
        )
        self.assertEqual(json.loads(forced_status.stdout), {})
        self.agentctl_env(
            forced_child_env, "work", "--agent", "codex", expect=2,
        )

    def test_legacy_ownerless_fork_does_not_resume_parent(self):
        parent_env = self.bare_env()
        parent_env.update({
            "AGENT_WORKFLOW_SESSION_ID": "legacy-workflow-id",
            "CODEX_THREAD_ID": "parent-thread",
            "WHALENT_AGENT_ID": "parent-agent",
        })
        self.agentctl_env(
            parent_env, "work", "--agent", "codex", "--auto-create",
            "--new-id", "T-176", "--title", "legacy parent task",
            "--scope", "src/parent/",
        )

        child_env = parent_env.copy()
        child_env.update({
            "CODEX_THREAD_ID": "child-thread",
            "WHALENT_AGENT_ID": "child-agent",
            "WHALENT_FORK_SOURCE_AGENT_ID": "parent-agent",
        })
        started = subprocess.run(
            [sys.executable, "tools/agent_workflow_hook.py", "session-start"],
            cwd=self.root, env=child_env,
            input=json.dumps({
                "session_id": "legacy-workflow-id",
                "source": "startup",
            }),
            text=True, capture_output=True, timeout=120,
        )
        self.assertEqual(started.returncode, 0, started.stderr)
        exports = json.loads(started.stdout)["env"]
        self.assertRegex(
            exports["AGENT_WORKFLOW_SESSION_INSTANCE_ID"],
            r"^instance-[0-9a-f]{24}$",
        )
        child_env.update(exports)

        child_status = json.loads(
            self.agentctl_env(child_env, "status", "--json").stdout
        )
        self.assertEqual(child_status, {})
        self.agentctl_env(
            child_env, "work", "--agent", "codex", "--auto-create",
            "--new-id", "T-177", "--title", "legacy fork child task",
            "--scope", "src/child/",
        )
        parent = json.loads(
            self.agentctl_env(parent_env, "status", "--json").stdout
        )
        child = json.loads(
            self.agentctl_env(child_env, "status", "--json").stdout
        )
        self.assertEqual(parent["task"], "T-176")
        self.assertEqual(child["task"], "T-177")
        self.assertNotEqual(
            parent["workflow_session_key"], child["workflow_session_key"],
        )

    def test_full_git_clone_keeps_same_session_id_runtime_records_local(self):
        self.start("same-session", "T-180", "src/original/")
        staged = subprocess.run(
            ["git", "add", "-A"], cwd=self.root,
            text=True, capture_output=True, timeout=60,
        )
        self.assertEqual(staged.returncode, 0, staged.stdout + staged.stderr)
        committed = subprocess.run(
            [
                "git", "-c", "user.name=Workflow Test",
                "-c", "user.email=workflow@example.invalid",
                "commit", "--no-verify", "-qm", "test: clone fixture",
            ],
            cwd=self.root, text=True, capture_output=True, timeout=60,
        )
        self.assertEqual(committed.returncode, 0, committed.stdout + committed.stderr)

        clone_parent = Path(tempfile.mkdtemp(prefix="awk-sessions-clone-"))
        self.addCleanup(shutil.rmtree, clone_parent, ignore_errors=True)
        clone = clone_parent / "repo"
        cloned = subprocess.run(
            ["git", "clone", "-q", str(self.root), str(clone)],
            text=True, capture_output=True, timeout=120,
        )
        self.assertEqual(cloned.returncode, 0, cloned.stdout + cloned.stderr)

        clone_env = self.env("same-session")
        empty = subprocess.run(
            [sys.executable, "tools/agentctl.py", "status", "--json"],
            cwd=clone, env=clone_env, text=True, capture_output=True, timeout=120,
        )
        self.assertEqual(empty.returncode, 0, empty.stdout + empty.stderr)
        self.assertEqual(json.loads(empty.stdout), {})
        child = subprocess.run(
            [
                sys.executable, "tools/agentctl.py", "work", "--agent", "codex",
                "--auto-create", "--new-id", "T-181", "--title", "clone-local work",
                "--scope", "src/clone/",
            ],
            cwd=clone, env=clone_env, text=True, capture_output=True, timeout=120,
        )
        self.assertEqual(child.returncode, 0, child.stdout + child.stderr)

        original_status = json.loads(
            self.agentctl("status", "--json", session="same-session").stdout
        )
        clone_status = json.loads(subprocess.run(
            [sys.executable, "tools/agentctl.py", "status", "--json"],
            cwd=clone, env=clone_env, text=True, capture_output=True,
            check=True, timeout=120,
        ).stdout)
        self.assertEqual(original_status["task"], "T-180")
        self.assertEqual(clone_status["task"], "T-181")
        self.assertEqual(
            original_status["workflow_session_key"],
            clone_status["workflow_session_key"],
        )
        original_common = subprocess.run(
            ["git", "rev-parse", "--git-common-dir"], cwd=self.root,
            text=True, capture_output=True, check=True, timeout=60,
        ).stdout.strip()
        clone_common = subprocess.run(
            ["git", "rev-parse", "--git-common-dir"], cwd=clone,
            text=True, capture_output=True, check=True, timeout=60,
        ).stdout.strip()
        self.assertNotEqual(
            (self.root / original_common).resolve(),
            (clone / clone_common).resolve(),
        )

    def test_interpreter_and_script_writes_require_an_active_session(self):
        # Inline interpreter code, project scripts, and stream copiers can
        # write anywhere, so without an active task session they must be
        # blocked instead of passing as "non-mutating".
        opaque_commands = (
            "python3 -c \"open('src/two/out.txt','w').write('x')\"",
            "python3 collect.py --out src/two/",
            "./collect.sh",
            "bash scripts/collect.sh",
            "rsync -a data/ src/two/",
            "tar -xf bundle.tar -C src/two/",
            "curl -o src/two/blob.bin https://example.invalid/blob",
        )
        for command in opaque_commands:
            blocked = self.hook(
                "pre-tool-use",
                {"tool_name": "Bash", "tool_input": {"command": command}},
                session="one",
            )
            self.assertEqual(blocked.returncode, 0, blocked.stderr)
            decision = json.loads(blocked.stdout or "{}")
            self.assertEqual(decision.get("decision"), "block", command)
        # The controller keeps its own identity/mutation gating: read-only
        # audits stay available to sessions that have no task yet. (Inline
        # perl left this list in T-079: a general-purpose language is opaque.)
        for command in (
            "python3 tools/agentctl.py migrate --json",
            "python3 tools/agentctl.py sessions list --json",
            "sed -n '1p' README.md",
        ):
            allowed = self.hook(
                "pre-tool-use",
                {"tool_name": "Bash", "tool_input": {"command": command}},
                session="one",
            )
            self.assertEqual(allowed.stdout, "", command)
        # With an active session the same interpreter write is admitted
        # through the normal session-level guard.
        self.start("one", "T-181", "src/one/")
        self.agentctl("refresh", session="one")
        admitted = self.hook(
            "pre-tool-use",
            {"tool_name": "Bash",
             "tool_input": {"command": "python3 -c \"open('x','w').write('x')\""}},
            session="one",
        )
        self.assertNotIn("\"decision\": \"block\"", admitted.stdout)

    def test_workspace_contamination_blocks_until_reconciled(self):
        subprocess.run(["git", "config", "user.email", "kit@test"], cwd=self.root, check=True)
        subprocess.run(["git", "config", "user.name", "kit"], cwd=self.root, check=True)
        victim = self.root / "src" / "two" / "data.txt"
        victim.parent.mkdir(parents=True)
        victim.write_text("original\n", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=self.root, check=True)
        subprocess.run(
            ["git", "commit", "--no-verify", "-qm", "seed tracked project files"],
            cwd=self.root, check=True,
        )
        self.start("one", "T-191", "src/one/")
        self.agentctl("refresh", session="one")
        self.agentctl("sessions", "guard", "--path", "src/one/ok.py", session="one")
        # Simulate an escaped write: a tracked file outside every live scope
        # changes on disk (e.g. an interpreter or script wrote it).
        victim.write_text("contaminated\n", encoding="utf-8")
        blocked = self.agentctl(
            "sessions", "guard", "--path", "src/one/ok.py",
            session="one", expect=1,
        )
        self.assertIn("outside every live session scope", blocked.stderr)
        self.assertIn("src/two/data.txt", blocked.stderr)
        # Once a session legitimately owns that path, the same state is fine.
        self.start("two", "T-192", "src/two/")
        self.agentctl("sessions", "guard", "--path", "src/one/ok.py", session="one")

    def test_explicit_auto_create_bypasses_queue_selection(self):
        self.agentctl(
            "task", "create", "--id", "T-EXIST", "--title", "queued for codex",
            "--owner", "codex", "--scope", "src/queue/", session="one",
        )
        claimed = self.agentctl(
            "work", "--agent", "codex", "--auto-create", "--new-id", "T-REQUEST",
            "--title", "explicit request", "--scope", "src/request/",
            session="one",
        )
        self.assertIn("T-REQUEST", claimed.stdout)
        board = json.loads(
            (self.root / ".agent" / "board.json").read_text(encoding="utf-8"))
        self.assertEqual(board["tasks"]["T-REQUEST"]["status"], "in_progress")
        self.assertEqual(board["tasks"]["T-EXIST"]["status"], "todo")
        # Plain work without an explicit request keeps selecting the queue.
        selected = self.agentctl("work", "--agent", "codex", session="two")
        self.assertIn("T-EXIST", selected.stdout)

    def test_opaque_commands_require_an_exclusive_checkout(self):
        """Audit repro: interpreters/scripts must not run beside live peers.

        A command whose written paths cannot be enumerated statically could
        write into any peer's scope without attribution, so with another live
        session in the checkout it is refused outright and pointed at task
        worktrees. Solo sessions keep running them, and read-only staples and
        path-checked writes stay available either way.
        """
        self.start("one", "T-101", "src/one/")
        self.start("two", "T-102", "src/two/")
        self.agentctl("refresh", session="one")

        opaque_commands = [
            # the literal audit reproduction
            'python3 -c "open(\'src/two/data.txt\',\'w\').write(\'x\')"',
            # nested shell must be unwrapped, not treated as read-only
            'bash -c "python3 -c \\"open(\'src/two/data.txt\',\'w\').write(\'x\')\\""',
            # test/build runners and arbitrary project binaries
            "pytest -q",
            "cargo test",
            "mycollector --output collect/batch1/",
        ]
        for command in opaque_commands:
            decision = self.hook(
                "pre-tool-use",
                {"tool_name": "Bash", "tool_input": {"command": command}},
                session="one",
            )
            self.assertTrue(decision.stdout.strip(), command)
            payload = json.loads(decision.stdout)
            self.assertEqual(payload.get("decision"), "block", command)
            self.assertIn("worktree", payload.get("reason", ""), command)

        # Read-only staples and path-checked writes keep working beside peers.
        # (perl left this list in T-079: as a general-purpose language it can
        # write files from inline code, so it is opaque like python.)
        for command in (
            "sed -n '1p' README.md",
            "grep -r Agent docs",
            "git status",
        ):
            passthrough = self.hook(
                "pre-tool-use",
                {"tool_name": "Bash", "tool_input": {"command": command}},
                session="one",
            )
            self.assertEqual(passthrough.stdout, "", command)
        pathed = self.hook(
            "pre-tool-use",
            {"tool_name": "Bash", "tool_input": {"command": "touch src/one/ok.py"}},
            session="one",
        )
        self.assertNotIn("block", pathed.stdout)

        # Once the peer finishes, the same opaque command is allowed again.
        self.agentctl("refresh", session="two")
        self.agentctl(
            "complete", "--summary", "done", "--tests", "fixture", session="two",
        )
        solo = self.hook(
            "pre-tool-use",
            {"tool_name": "Bash", "tool_input": {"command": "pytest -q"}},
            session="one",
        )
        self.assertNotIn('"decision": "block"', solo.stdout)

    def test_newline_separation_and_ampersand_redirects_are_segmented(self):
        """Self-audit: `\\n` must split commands and `&>`/`>|` must be writes."""
        self.start("one", "T-101", "src/one/")
        self.start("two", "T-102", "src/two/")
        self.agentctl("refresh", session="one")

        for command in (
            "echo hi\npython3 evil.py",
            "ls\ntouch src/two/x",
            "echo hi &> src/two/x",
            "echo hi &>> src/two/x",
            "echo hi >| src/two/x",
            "mv src/two/data.txt src/one/",
        ):
            decision = self.hook(
                "pre-tool-use",
                {"tool_name": "Bash", "tool_input": {"command": command}},
                session="one",
            )
            self.assertTrue(decision.stdout.strip(), repr(command))
            payload = json.loads(decision.stdout)
            self.assertEqual(payload.get("decision"), "block", repr(command))
        # Own-scope equivalents stay usable.
        for command in (
            "echo hi &> src/one/log.txt",
            "ls\ntouch src/one/x",
            "mv src/one/a.txt src/one/b.txt",
        ):
            passthrough = self.hook(
                "pre-tool-use",
                {"tool_name": "Bash", "tool_input": {"command": command}},
                session="one",
            )
            self.assertNotIn('"decision": "block"', passthrough.stdout, repr(command))
        # Wrapper commands must not distort the underlying classification.
        wrapped = self.hook(
            "pre-tool-use",
            {"tool_name": "Bash", "tool_input": {
                "command": "timeout 60 grep -r pattern src"}},
            session="one",
        )
        self.assertEqual(wrapped.stdout, "", "timeout-wrapped grep")

    def test_git_shared_ref_mutations_require_repo_wide_exclusivity(self):
        """Self-audit: branch -D etc. hit every worktree, not one checkout."""
        self.start("one", "T-101", "src/one/")
        self.agentctl("refresh", session="one")
        # Register a session that lives in a DIFFERENT checkout (a worktree).
        subprocess.run(["git", "add", "-A"], cwd=self.root, check=True, timeout=60)
        subprocess.run(
            ["git", "-c", "core.hooksPath=", "-c", "user.email=t@t.t",
             "-c", "user.name=t", "commit", "-qm", "seed for worktree"],
            cwd=self.root, check=True, timeout=60,
        )
        other = self.root / ".." / f"{self.root.name}-wt"
        subprocess.run(
            ["git", "worktree", "add", "-q", str(other), "-b", "wt-branch"],
            cwd=self.root, check=True, timeout=60,
        )
        self.addCleanup(
            lambda: subprocess.run(
                ["git", "worktree", "remove", "--force", str(other)],
                cwd=self.root, timeout=60,
            )
        )
        proc = subprocess.run(
            [sys.executable, "tools/agentctl.py", "work", "--agent", "codex",
             "--auto-create", "--new-id", "T-777", "--title", "wt",
             "--scope", "src/wt/"],
            cwd=other, env=self.env("wt-session"), text=True,
            capture_output=True, timeout=120,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

        # Shared-ref mutations are refused while ANY other checkout is live.
        for command in (
            "git branch -D wt-branch",
            "git branch -m wt-branch renamed",
            "git reflog expire --expire=now --all",
            "git fetch --prune origin",
            "git gc",
            "git tag -d v1",
            "git push --delete origin wt-branch",
            "git push --force origin main",
        ):
            decision = self.hook(
                "pre-tool-use",
                {"tool_name": "Bash", "tool_input": {"command": command}},
                session="one",
            )
            self.assertTrue(decision.stdout.strip(), command)
            payload = json.loads(decision.stdout)
            self.assertEqual(payload.get("decision"), "block", command)
            self.assertIn("worktree", payload.get("reason", ""), command)

    def test_allowlisted_text_tools_cannot_write_through_options_or_dsl(self):
        """Audit repro: sort -o, awk DSL redirects, yq -i, old-style tar."""
        self.start("one", "T-101", "src/one/")
        self.start("two", "T-102", "src/two/")
        self.agentctl("refresh", session="one")

        for command in (
            "sort -o src/two/data.txt src/one/source",
            'awk \'BEGIN { print "A" > "src/two/awk.txt" }\'',
            "awk '{print $1}' data.csv",
            "yq -i '.a=1' src/two/cfg.yaml",
            "yq '.a' cfg.yaml",
            "tar xvf data.tgz",
            "tar cf archive.tar src/two",
            "perl -ne 'print if /Agent/' README.md",
            "uniq src/one/in src/two/out",
            "sed 's/x/y/w src/two/leak.txt' input",
        ):
            decision = self.hook(
                "pre-tool-use",
                {"tool_name": "Bash", "tool_input": {"command": command}},
                session="one",
            )
            self.assertTrue(decision.stdout.strip(), command)
            payload = json.loads(decision.stdout)
            self.assertEqual(payload.get("decision"), "block", command)
        # sort -o into the session's own scope is a checked, allowed write.
        own = self.hook(
            "pre-tool-use",
            {"tool_name": "Bash", "tool_input": {
                "command": "sort -o src/one/sorted.txt src/one/raw.txt"}},
            session="one",
        )
        self.assertNotIn('"decision": "block"', own.stdout)
        # Verified read-only forms keep passing beside peers.
        for command in (
            "sort src/one/raw.txt",
            "jq '.a' cfg.json",
            "sed -n '1,5p' README.md",
            "tar -tzf data.tgz",
            "uniq src/one/in",
        ):
            passthrough = self.hook(
                "pre-tool-use",
                {"tool_name": "Bash", "tool_input": {"command": command}},
                session="one",
            )
            self.assertEqual(passthrough.stdout, "", command)

    def test_git_ref_and_config_mutations_are_not_read_only(self):
        """Audit repro: fetch, reflog expire, and branch config are writes."""
        self.start("one", "T-101", "src/one/")
        self.start("two", "T-102", "src/two/")
        self.agentctl("refresh", session="one")

        for command in (
            "git fetch origin",
            "git reflog expire --all",
            "git reflog delete HEAD@{1}",
            "git branch --set-upstream-to=origin/main",
            "git branch -D feature/x",
            "git branch new-branch",
        ):
            decision = self.hook(
                "pre-tool-use",
                {"tool_name": "Bash", "tool_input": {"command": command}},
                session="one",
            )
            self.assertTrue(decision.stdout.strip(), command)
            payload = json.loads(decision.stdout)
            self.assertEqual(payload.get("decision"), "block", command)
        for command in (
            "git reflog",
            "git reflog show HEAD",
            "git branch",
            "git branch -a --list",
            "git branch --show-current",
        ):
            passthrough = self.hook(
                "pre-tool-use",
                {"tool_name": "Bash", "tool_input": {"command": command}},
                session="one",
            )
            self.assertEqual(passthrough.stdout, "", command)

    def test_git_working_tree_writers_are_not_read_only(self):
        """Audit repro: `git restore` must not wipe a peer's uncommitted work."""
        self.start("one", "T-101", "src/one/")
        self.start("two", "T-102", "src/two/")
        self.agentctl("refresh", session="one")

        for command in (
            "git restore -- src/two/data.txt",
            "git stash",
            "git rm -r src/two",
            "git mv src/two/a src/two/b",
            "git apply patch.diff",
            "git cherry-pick abc123",
            "git revert HEAD",
            "git worktree remove ../gone",
        ):
            decision = self.hook(
                "pre-tool-use",
                {"tool_name": "Bash", "tool_input": {"command": command}},
                session="one",
            )
            self.assertTrue(decision.stdout.strip(), command)
            payload = json.loads(decision.stdout)
            self.assertEqual(payload.get("decision"), "block", command)
        # git reads keep passing beside peers, including flag-prefixed forms.
        for command in (
            "git status",
            "git log --oneline -5",
            "git -C . diff --stat",
            "git config --get core.hooksPath",
            "git stash list",
        ):
            passthrough = self.hook(
                "pre-tool-use",
                {"tool_name": "Bash", "tool_input": {"command": command}},
                session="one",
            )
            self.assertEqual(passthrough.stdout, "", command)

    def test_link_creation_and_pathless_writers_cannot_slip_through(self):
        """Audit repro: ln must be path-checked; pathed-without-paths escalates."""
        self.start("one", "T-101", "src/one/")
        self.start("two", "T-102", "src/two/")
        self.agentctl("refresh", session="one")

        into_peer = self.hook(
            "pre-tool-use",
            {"tool_name": "Bash", "tool_input": {
                "command": "ln -s ../../etc/hosts src/two/link"}},
            session="one",
        )
        payload = json.loads(into_peer.stdout)
        self.assertEqual(payload.get("decision"), "block")
        own_scope = self.hook(
            "pre-tool-use",
            {"tool_name": "Bash", "tool_input": {
                "command": "ln -s data.txt src/one/link"}},
            session="one",
        )
        self.assertNotIn('"decision": "block"', own_scope.stdout)
        # A write-classified command that yields no checkable path cannot pass
        # beside a live peer on the strength of an empty path list.
        pathless = self.hook(
            "pre-tool-use",
            {"tool_name": "Bash", "tool_input": {"command": "cp -r"}},
            session="one",
        )
        payload = json.loads(pathless.stdout)
        self.assertEqual(payload.get("decision"), "block")

    def test_command_substitution_and_process_substitution_are_opaque(self):
        """Audit repro: $(...), backticks, and <(...) can hide arbitrary writes."""
        self.start("one", "T-101", "src/one/")
        self.start("two", "T-102", "src/two/")
        self.agentctl("refresh", session="one")

        for command in (
            'echo $(python3 -c "open(\'src/two/x\',\'w\').write(\'x\')")',
            "echo `date` > /dev/null; echo `python3 evil.py`",
            "diff <(sort a.txt) <(sort b.txt)",
            'VAR=$(mycollector); echo "$VAR"',
        ):
            decision = self.hook(
                "pre-tool-use",
                {"tool_name": "Bash", "tool_input": {"command": command}},
                session="one",
            )
            self.assertTrue(decision.stdout.strip(), command)
            payload = json.loads(decision.stdout)
            self.assertEqual(payload.get("decision"), "block", command)

    def test_unknown_executables_default_to_writes_requiring_a_session(self):
        probe = self.hook(
            "pre-tool-use",
            {"tool_name": "Bash", "tool_input": {"command": "mycollector --run"}},
            session="ghost",
        )
        self.assertTrue(probe.stdout.strip(), "unknown executable passed as read-only")
        payload = json.loads(probe.stdout)
        self.assertEqual(payload.get("decision"), "block")
        self.assertIn("no active task session", payload.get("reason", ""))
        read_only = self.hook(
            "pre-tool-use",
            {"tool_name": "Bash", "tool_input": {"command": "grep -r pattern src"}},
            session="ghost",
        )
        self.assertEqual(read_only.stdout, "")

    def test_ancestor_and_glob_paths_that_exceed_scope_are_blocked(self):
        """Self-audit: `rm src/*` / `rm src` must not slip past a src/one scope.

        A parent directory and a glob whose expansion spans sibling scopes
        both reach another session's files even though the literal token is
        not itself inside the peer scope.
        """
        self.start("one", "T-101", "src/one/")
        self.start("two", "T-102", "src/two/")
        self.agentctl("refresh", session="one")

        for path in (
            "src",            # parent of the scope, reaches src/two
            "src/*",          # glob spanning src/one and src/two
            "src/two",        # sibling scope directly
            "src/two/*",      # glob inside the peer scope
            "*",              # checkout-root glob
            "src/one/../two/x",  # traversal into the peer scope
        ):
            blocked = self.agentctl(
                "sessions", "guard", "--path", path, session="one", expect=1,
            )
            self.assertIn("scope", blocked.stderr, path)
        # Descendants and same-directory globs of the OWN scope stay allowed.
        for path in (
            "src/one",
            "src/one/nested/file.py",
            "src/one/*",
            "src/one/**/*.py",
        ):
            self.agentctl("sessions", "guard", "--path", path, session="one")

    def test_start_rejects_unknown_task_ids_without_registering_a_claim(self):
        unknown = self.agentctl(
            "start", "--task", "T-999", "--agent", "codex",
            session="one", expect=2,
        )
        self.assertIn("unknown task", unknown.stderr)
        board = json.loads(
            (self.root / ".agent" / "board.json").read_text(encoding="utf-8"))
        self.assertNotIn("T-999", board.get("tasks", {}))
        self.assertFalse(
            (self.root / ".agent" / "tasks" / "T-999.md").exists())
        rows = self.sessions("one")["sessions"]
        self.assertNotIn("T-999", {row.get("task") for row in rows})
        # A follow-up conversation with a disjoint scope is not blocked by a
        # ghost claim left behind by the failed start.
        self.start("two", "T-102", "src/two/")

    def test_overlap_same_task_scope_and_parallel_git_are_blocked(self):
        self.start("one", "T-101", "src/shared/")
        self.agentctl(
            "task", "create", "--id", "T-102", "--title", "overlap",
            "--owner", "codex", "--scope", "src/shared/nested/", session="two",
        )
        overlap = self.agentctl(
            "start", "--task", "T-102", "--agent", "codex",
            session="two", expect=1,
        )
        self.assertIn("session conflict", overlap.stderr)
        same_task = self.agentctl(
            "start", "--task", "T-101", "--agent", "codex",
            session="two", expect=1,
        )
        self.assertIn("already claimed", same_task.stderr)
        self.agentctl(
            "task", "create", "--id", "T-104", "--title", "unbounded",
            "--owner", "codex", session="three",
        )
        unbounded = self.agentctl(
            "start", "--task", "T-104", "--agent", "codex",
            session="three", expect=1,
        )
        self.assertIn("cannot be proven disjoint", unbounded.stderr)

        self.agentctl(
            "task", "create", "--id", "T-103", "--title", "disjoint",
            "--owner", "codex", "--scope", "src/two/", session="two",
        )
        self.agentctl("start", "--task", "T-103", "--agent", "codex", session="two")
        first_key = next(
            row["workflow_session_key"]
            for row in self.sessions("two")["sessions"] if row["task"] == "T-101"
        )
        active_release = self.agentctl(
            "sessions", "release", first_key, "--reason", "unsafe takeover",
            session="two", expect=1,
        )
        self.assertIn("cannot be released", active_release.stderr)
        git_guard = self.agentctl(
            "sessions", "guard", "--git-write", session="one", expect=1,
        )
        self.assertIn("exclusive checkout", git_guard.stderr)
        precommit = self.agentctl(
            "check", "--mode", "pre-commit", session="one", expect=1,
        )
        self.assertIn("requires exclusive use", precommit.stdout)

        self.agentctl("refresh", session="one")
        hook_guard = self.hook(
            "pre-tool-use",
            {"tool_name": "Bash", "tool_input": {"command": "git add src/shared/a.py"}},
            session="one",
        )
        decision = json.loads(hook_guard.stdout)
        self.assertEqual(decision["decision"], "block")
        self.assertIn("exclusive checkout", decision["reason"])

    def test_file_scope_guard_and_finished_session_release_git_exclusivity(self):
        self.start("one", "T-101", "src/one/")
        self.start("two", "T-102", "src/two/")
        self.agentctl("refresh", session="one")

        self.agentctl(
            "sessions", "guard", "--path", "src/one/a.py", session="one",
        )
        outside = self.agentctl(
            "sessions", "guard", "--path", "src/two/a.py",
            session="one", expect=1,
        )
        self.assertIn("outside active task scope", outside.stderr)
        shell_outside = self.hook(
            "pre-tool-use",
            {"tool_name": "Bash", "tool_input": {"command": "touch src/two/from-shell.py"}},
            session="one",
        )
        self.assertIn(
            "outside active task scope",
            json.loads(shell_outside.stdout)["reason"],
        )
        redirected_outside = self.hook(
            "pre-tool-use",
            {
                "tool_name": "Bash",
                "tool_input": {"command": "printf 'content' > src/two/from-printf.py"},
            },
            session="one",
        )
        self.assertIn(
            "outside active task scope",
            json.loads(redirected_outside.stdout)["reason"],
        )
        read_only_sed = self.hook(
            "pre-tool-use",
            {
                "tool_name": "Bash",
                "tool_input": {"command": "sed -n '1p' README.md"},
            },
            session="one",
        )
        self.assertEqual(read_only_sed.stdout, "")
        # Since T-079, inline perl is opaque (a general-purpose language can
        # write arbitrary files); beside a live peer it is refused.
        inline_perl = self.hook(
            "pre-tool-use",
            {
                "tool_name": "Bash",
                "tool_input": {"command": "perl -ne 'print if /Agent/' README.md"},
            },
            session="one",
        )
        self.assertEqual(
            json.loads(inline_perl.stdout).get("decision"), "block",
        )
        in_place_sed = self.hook(
            "pre-tool-use",
            {
                "tool_name": "Bash",
                "tool_input": {"command": "sed -i.bak 's/x/y/' src/two/in-place.py"},
            },
            session="one",
        )
        self.assertIn(
            "outside active task scope",
            json.loads(in_place_sed.stdout)["reason"],
        )
        shell_inside = self.hook(
            "pre-tool-use",
            {"tool_name": "Bash", "tool_input": {"command": "touch src/one/from-shell.py"}},
            session="one",
        )
        self.assertEqual(shell_inside.stdout, "")
        cd_inside = self.hook(
            "pre-tool-use",
            {"tool_name": "Bash", "tool_input": {"command": "cd src/one && touch nested.py"}},
            session="one",
        )
        self.assertEqual(cd_inside.stdout, "")
        cd_escape = self.hook(
            "pre-tool-use",
            {
                "tool_name": "Bash",
                "tool_input": {"command": "cd src/one && touch ../../src/two/escaped.py"},
            },
            session="one",
        )
        self.assertIn(
            "outside active task scope",
            json.loads(cd_escape.stdout)["reason"],
        )
        row = next(row for row in self.sessions("one")["sessions"] if row["task"] == "T-101")
        self.assertEqual(
            row["claimed_files"],
            ["src/one/a.py", "src/one/from-shell.py", "src/one/nested.py"],
        )

        self.agentctl(
            "finish", "--summary", "finished first scope", "--tests", "unit fixture",
            session="one",
        )
        self.agentctl("refresh", session="two")
        self.agentctl("sessions", "guard", "--git-write", session="two")

    def test_stale_claim_remains_blocking_until_explicit_release(self):
        self.start("one", "T-101", "src/shared/")
        row = self.sessions("one")["sessions"][0]
        common = subprocess.run(
            ["git", "rev-parse", "--git-common-dir"], cwd=self.root,
            text=True, capture_output=True, check=True,
        ).stdout.strip()
        common_path = Path(common)
        if not common_path.is_absolute():
            common_path = self.root / common_path
        records = list(
            (common_path.resolve() / "agent-workflow" / "sessions").glob(
                f"{row['workflow_session_key']}-*.json"
            )
        )
        self.assertEqual(len(records), 1)
        record = records[0]
        payload = json.loads(record.read_text(encoding="utf-8"))
        payload["heartbeat_ns"] = 1
        payload["heartbeat_at"] = "1970-01-01 00:00:00"
        record.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

        blocked = self.agentctl(
            "start", "--task", "T-101", "--agent", "codex",
            session="two", expect=1,
        )
        self.assertIn("(stale)", blocked.stderr)
        self.agentctl(
            "sessions", "release", row["workflow_session_key"],
            "--reason", "inspected abandoned conversation", session="two",
        )
        self.agentctl("start", "--task", "T-101", "--agent", "codex", session="two")

        heartbeat = self.agentctl(
            "sessions", "heartbeat", session="one", expect=1,
        )
        self.assertIn("released", heartbeat.stderr)
        resumed = self.agentctl(
            "work", "--agent", "codex", session="one", expect=1,
        )
        self.assertIn("already claimed", resumed.stderr)
        old_hook = self.hook(
            "pre-tool-use",
            {"tool_name": "Write", "tool_input": {"file_path": "src/shared/old.py"}},
            session="one",
        )
        self.assertEqual(json.loads(old_hook.stdout)["decision"], "block")
        self.assertIn("no active task session", json.loads(old_hook.stdout)["reason"])

    def test_precommit_rejects_staged_files_outside_the_current_task_scope(self):
        self.start("one", "T-101", "src/one/")
        (self.root / "src" / "one").mkdir(parents=True)
        (self.root / "src" / "other").mkdir(parents=True)
        (self.root / "src" / "one" / "inside.py").write_text("inside = True\n", encoding="utf-8")
        (self.root / "src" / "other" / "outside.py").write_text("outside = True\n", encoding="utf-8")
        staged = subprocess.run(
            [
                "git", "add", "src/one/inside.py", "src/other/outside.py",
                ".agent/tasks/T-101.md",
            ],
            cwd=self.root, text=True, capture_output=True, timeout=60,
        )
        self.assertEqual(staged.returncode, 0, staged.stdout + staged.stderr)
        checked = self.agentctl(
            "check", "--mode", "pre-commit", session="one", expect=1,
        )
        self.assertIn("src/other/outside.py", checked.stdout)
        self.assertNotIn("src/one/inside.py", checked.stdout)

    def test_matching_legacy_singleton_migrates_to_conversation_record(self):
        provider_env = self.bare_env()
        provider_env["CODEX_THREAD_ID"] = "legacy-codex-conversation"
        runtime_digest = hashlib.sha256(
            b"CODEX_THREAD_ID=legacy-codex-conversation"
        ).hexdigest()
        runtime_identity = f"host-runtime:{runtime_digest[:32]}"
        expected_key = "session-" + hashlib.sha256(
            runtime_identity.encode("utf-8")
        ).hexdigest()[:24]
        state_dir = self.root / ".agent" / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        legacy = state_dir / "current_session.json"
        legacy.write_text(json.dumps({
            "task": "T-150",
            "agent": "codex",
            "started_at": "2026-01-01 00:00:00",
            "scope": ["src/legacy/"],
            "runtime_identity": runtime_identity,
            "runtime_identities": [runtime_identity],
            "notes": [],
            "doc_hashes": {},
        }, indent=2) + "\n", encoding="utf-8")

        status = subprocess.run(
            [sys.executable, "tools/agentctl.py", "status", "--json"],
            cwd=self.root, env=provider_env, text=True, capture_output=True, timeout=120,
        )
        self.assertEqual(status.returncode, 0, status.stdout + status.stderr)
        migrated = json.loads(status.stdout)
        self.assertEqual(migrated["task"], "T-150")
        self.assertEqual(migrated["workflow_session_key"], expected_key)
        self.assertFalse(legacy.exists())
        common = subprocess.run(
            ["git", "rev-parse", "--git-common-dir"], cwd=self.root,
            text=True, capture_output=True, check=True,
        ).stdout.strip()
        common_path = Path(common)
        if not common_path.is_absolute():
            common_path = self.root / common_path
        records = list(
            (common_path.resolve() / "agent-workflow" / "sessions").glob(
                f"{expected_key}-*.json"
            )
        )
        self.assertEqual(len(records), 1)

    def test_concurrent_start_and_progress_updates_are_serialized(self):
        self.agentctl(
            "task", "create", "--id", "T-201", "--title", "first writer",
            "--owner", "codex", "--scope", "shared/", session="one",
        )
        self.agentctl(
            "task", "create", "--id", "T-202", "--title", "second writer",
            "--owner", "codex", "--scope", "shared/", session="two",
        )
        commands = []
        for session, task in (("one", "T-201"), ("two", "T-202")):
            commands.append(subprocess.Popen(
                [sys.executable, "tools/agentctl.py", "start", "--task", task, "--agent", "codex"],
                cwd=self.root, env=self.env(session), text=True,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            ))
        results = []
        for proc in commands:
            stdout, stderr = proc.communicate(timeout=30)
            results.append((proc.returncode, stdout + stderr))
        self.assertEqual(sorted(code for code, _output in results), [0, 1], results)

        winner = next(row for row in self.sessions("one")["sessions"] if row["observed_status"] == "active")
        self.agentctl(
            "finish", "--summary", "release shared scope", "--tests", "fixture",
            session="one" if winner["task"] == "T-201" else "two",
        )

        self.start("three", "T-203", "src/three/")
        self.start("four", "T-204", "src/four/")
        self.agentctl("refresh", session="three")
        writers = []
        for session, message in (("three", "three phase"), ("four", "four phase")):
            writers.append(subprocess.Popen(
                [sys.executable, "tools/agentctl.py", "note", message], cwd=self.root,
                env=self.env(session), text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            ))
        outputs = [proc.communicate(timeout=30) for proc in writers]
        self.assertEqual([proc.returncode for proc in writers], [0, 0], outputs)
        progress = (self.root / ".agent" / "logs" / "progress.md").read_text(encoding="utf-8")
        self.assertIn("three phase", progress)
        self.assertIn("four phase", progress)


if __name__ == "__main__":
    unittest.main()
