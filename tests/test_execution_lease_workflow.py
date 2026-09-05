"""Unified conversation, run, and resource lease regressions."""

import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock


KIT = Path(__file__).resolve().parents[1]
IDENTITY_ENV = (
    "CODEX_THREAD_ID",
    "CLAUDE_CODE_SESSION_ID",
    "CURSOR_CONVERSATION_ID",
    "WHALENT_AGENT_ID",
    "WHALENT_CODEX_INSTANCE_ID",
    "WHALENT_COMPOSER_ID",
    "WHALENT_FORK_SOURCE_AGENT_ID",
    "AGENT_SESSION_ID",
    "TERM_SESSION_ID",
    "AGENT_WORKFLOW_SESSION_ID",
    "AGENT_WORKFLOW_SESSION_KEY",
    "AGENT_WORKFLOW_SESSION_OWNER_RUNTIME",
    "AGENT_WORKFLOW_SESSION_INSTANCE_ID",
    "AGENT_WORKFLOW_PARENT_SESSION_KEY",
    "AGENT_WORKFLOW_SESSION_ISOLATION_ERROR",
)


class ExecutionLeaseWorkflowRegressionTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="awk-execution-leases-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True, timeout=60)
        install = subprocess.run(
            [sys.executable, str(KIT / "tools" / "agentctl.py"), "init", str(self.root)],
            cwd=KIT, text=True, capture_output=True, timeout=600,
        )
        self.assertEqual(install.returncode, 0, install.stdout + install.stderr)

    def env(self, session, **extra):
        env = os.environ.copy()
        for name in IDENTITY_ENV:
            env.pop(name, None)
        env["AGENT_WORKFLOW_SESSION_ID"] = session
        env["AGENT_WORKFLOW_RESOURCE_LOCK_DIR"] = str(self.root / ".resource-locks")
        env.update(extra)
        return env

    def agentctl(self, *args, session="one", expect=0, env=None, timeout=600):
        proc = subprocess.run(
            [sys.executable, "tools/agentctl.py", *args],
            cwd=self.root, env=env or self.env(session), text=True,
            capture_output=True, timeout=timeout,
        )
        if proc.returncode != expect:
            # CI runners destroy the temp checkout after a failure, so any
            # supervisor stderr must be surfaced in the assertion itself or
            # a silent pre-claim supervisor death stays undiagnosable.
            self.fail(
                f"agentctl {' '.join(str(a) for a in args)} "
                f"rc={proc.returncode} expected={expect}\n"
                f"{proc.stdout}{proc.stderr}\n"
                f"supervisor logs:\n{self.supervisor_log_dump()}"
            )
        return proc

    def supervisor_log_dump(self):
        """Every per-lease supervisor stderr log, for failure diagnostics."""
        try:
            runs_dir = self.workflow_common_dir() / "runs"
            chunks = []
            for path in sorted(runs_dir.glob("*.supervisor.log")):
                body = path.read_text(encoding="utf-8", errors="replace").strip()
                if body:
                    chunks.append(f"--- {path.name} ---\n{body}")
            return "\n\n".join(chunks) or "<no supervisor log content>"
        except OSError as exc:
            return f"<supervisor logs unreadable: {exc}>"

    def start(self, session, task, scope):
        return self.agentctl(
            "work", "--agent", "codex", "--auto-create", "--new-id", task,
            "--title", f"work for {session}", "--scope", scope, session=session,
        )

    @staticmethod
    def lease_id(output, prefix):
        match = re.search(rf"\b({re.escape(prefix)}-[0-9a-f]{{16}})\b", output)
        if not match:
            raise AssertionError(f"missing {prefix} lease id in: {output}")
        return match.group(1)

    def test_unified_view_keeps_conversations_distinct_and_never_inherits_authority(self):
        self.start("parent", "T-301", "src/parent/")
        child_env = self.env(
            "child",
            AGENT_WORKFLOW_PARENT_SESSION_KEY="parent",
            AGENT_WORKFLOW_SESSION_INSTANCE_ID="child-instance",
        )
        self.agentctl(
            "work", "--agent", "codex", "--auto-create", "--new-id", "T-302",
            "--title", "derived work", "--scope", "src/child/", env=child_env,
        )

        rows = json.loads(
            self.agentctl("lease", "list", "--json", session="parent").stdout
        )["leases"]
        conversations = [row for row in rows if row["kind"] == "conversation"]
        self.assertEqual({row["task"] for row in conversations}, {"T-301", "T-302"})
        self.assertEqual(len({row["holder"]["id"] for row in conversations}), 2)
        child = next(row for row in conversations if row["task"] == "T-302")
        self.assertFalse(child["lineage"]["authority_inherited"])
        self.assertTrue(child["lineage"]["parent_session_key"])

    def test_supervised_run_holds_resource_then_releases_it_after_success(self):
        self.start("one", "T-311", "outputs/T-311/")
        self.start("two", "T-312", "outputs/T-312/")
        command = (
            "from pathlib import Path; import time; time.sleep(0.4); "
            "p=Path('outputs/T-311/result.txt'); p.parent.mkdir(parents=True, exist_ok=True); "
            "p.write_text('ok', encoding='utf-8')"
        )
        started = self.agentctl(
            "run", "start", "--output", "outputs/T-311/",
            "--resource", "host:test:gpu:1", "--",
            sys.executable, "-c", command, session="one",
        )
        run_id = self.lease_id(started.stdout, "run")

        blocked = self.agentctl(
            "resource", "acquire", "host:test:gpu:1",
            session="two", expect=1,
        )
        self.assertIn("already locked", blocked.stderr)
        self.wait_run_with_diagnostics(run_id)
        self.assertEqual(
            (self.root / "outputs" / "T-311" / "result.txt").read_text(encoding="utf-8"),
            "ok",
        )

        acquired = self.agentctl(
            "resource", "acquire", "host:test:gpu:1", session="two",
        )
        resource_id = self.lease_id(acquired.stdout, "resource")
        self.agentctl(
            "resource", "release", resource_id, "--reason", "test complete",
            session="two",
        )

    def test_peer_cannot_force_release_another_conversations_resource(self):
        self.start("owner", "T-313", "outputs/T-313/")
        self.start("peer", "T-314", "outputs/T-314/")
        acquired = self.agentctl(
            "resource", "acquire", "host:test:gpu:2", session="owner",
        )
        resource_id = self.lease_id(acquired.stdout, "resource")

        unsupported = self.agentctl(
            "resource", "release", resource_id, "--reason", "peer takeover",
            "--force", session="peer", expect=2,
        )
        self.assertIn("unrecognized arguments: --force", unsupported.stderr)
        refused = self.agentctl(
            "resource", "release", resource_id, "--reason", "peer takeover",
            session="peer", expect=1,
        )
        self.assertIn("belongs to conversation:", refused.stderr)
        rows = json.loads(
            self.agentctl("resource", "status", "--json", session="owner").stdout
        )["resources"]
        lease = next(row for row in rows if row["id"] == resource_id)
        self.assertEqual(lease["status"], "active")

        self.agentctl(
            "resource", "release", resource_id, "--reason", "owner complete",
            session="owner",
        )

    def test_run_rejects_undeclared_write_ownership_outside_task_scope(self):
        self.start("one", "T-321", "outputs/T-321/")
        refused = self.agentctl(
            "run", "start", "--output", "outputs/other/", "--",
            sys.executable, "-c", "print('never launched')",
            session="one", expect=1,
        )
        self.assertIn("outside task scope", refused.stderr)
        rows = json.loads(
            self.agentctl("run", "list", "--json", session="one").stdout
        )["runs"]
        self.assertEqual(rows, [])

    def test_adopted_process_requires_explicit_result_reconciliation(self):
        self.start("one", "T-331", "outputs/T-331/")
        self.start("two", "T-332", "outputs/T-332/")
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(3)"],
            cwd=self.root,
        )
        self.addCleanup(lambda: process.poll() is None and process.terminate())
        adopted = self.agentctl(
            "run", "adopt", "--pid", str(process.pid), "--cwd", str(self.root),
            "--output", "outputs/T-331/", session="one",
        )
        run_id = self.lease_id(adopted.stdout, "run")
        process.wait(timeout=10)

        unknown = self.agentctl(
            "run", "wait", run_id, "--timeout", "2", session="one", expect=1,
        )
        self.assertIn("exited_unknown", unknown.stdout)
        refused = self.agentctl(
            "run", "finish", run_id, "--status", "succeeded",
            "--reason", "not my run", session="two", expect=1,
        )
        self.assertIn("belongs to conversation", refused.stderr)
        self.agentctl(
            "run", "finish", run_id, "--status", "succeeded",
            "--reason", "external result inspected", session="one",
        )
        shown = json.loads(
            self.agentctl("run", "show", run_id, "--json", session="one").stdout
        )["runs"][0]
        self.assertEqual(shown["status"], "succeeded")

    def test_supervised_wait_rechecks_transient_unknown_before_timeout(self):
        from tools import agentctl as workflow

        transient = {
            "id": "run-transient",
            "kind": "run",
            "mode": "supervised",
            "status": "running",
            "processes": [],
            "supervisor_process": {},
        }
        succeeded = dict(transient, status="succeeded")
        args = type("Args", (), {"lease": "run-transient", "timeout": 1.0})()
        with (
            mock.patch.object(
                workflow, "_runtime_lease", side_effect=[transient, succeeded]
            ) as load_lease,
            mock.patch.object(workflow.time, "sleep"),
        ):
            self.assertEqual(workflow._run_wait(self.root, args), 0)
        self.assertEqual(load_lease.call_count, 2)

    def test_gpu_watchdog_requires_consecutive_idle_samples_and_no_progress(self):
        from tools import agentctl as workflow

        policy = {
            "idle_seconds": 10.0,
            "grace_seconds": 5.0,
            "utilization_max": 5.0,
            "memory_min_mib": 1024.0,
            "action": "terminate",
        }
        idle = {"ok": True, "utilization_percent": 0.0, "memory_mib": 2048.0}
        state, action = workflow._gpu_watchdog_transition(
            {}, policy, idle, now_ns=1_000_000_000, progress_marker="same",
            progress_updated_ns=0, exempt_until_ns=0,
        )
        self.assertEqual(state["state"], "suspected_idle")
        self.assertIsNone(action)

        state, action = workflow._gpu_watchdog_transition(
            state, policy, idle, now_ns=12_000_000_000, progress_marker="same",
            progress_updated_ns=0, exempt_until_ns=0,
        )
        self.assertEqual(state["state"], "grace")
        self.assertIsNone(action)

        state, action = workflow._gpu_watchdog_transition(
            state, policy, idle, now_ns=18_000_000_000, progress_marker="same",
            progress_updated_ns=0, exempt_until_ns=0,
        )
        self.assertEqual(state["state"], "reclaimable")
        self.assertEqual(action, "terminate")

        state, action = workflow._gpu_watchdog_transition(
            state, policy, idle, now_ns=19_000_000_000, progress_marker="advanced",
            progress_updated_ns=19_000_000_000, exempt_until_ns=0,
        )
        self.assertEqual(state["state"], "active")
        self.assertIsNone(action)

    def test_supervised_gpu_watchdog_reclaims_idle_but_preserves_progress(self):
        from tools import agentctl as workflow

        self.start("one", "T-336", "outputs/T-336/")
        fake_bin = self.root / "fake-bin"
        fake_bin.mkdir()
        nvidia_smi = fake_bin / "nvidia-smi"
        nvidia_smi.write_text("#!/bin/sh\nprintf '0, 2048\\n'\n", encoding="utf-8")
        nvidia_smi.chmod(0o755)
        env = self.env("one", PATH=f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}")

        idle_pid_file = self.root / "outputs" / "T-336" / "idle.txt"
        idle_command = (
            "import pathlib, subprocess, sys, time; "
            "child=subprocess.Popen([sys.executable, '-c', "
            "'import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            "time.sleep(10)']); "
            f"p=pathlib.Path({str(idle_pid_file)!r}); "
            "p.parent.mkdir(parents=True, exist_ok=True); "
            "p.write_text(str(child.pid), encoding='utf-8'); time.sleep(10)"
        )
        # The watchdog starts sampling as soon as the supervisor is up, so
        # the payload must publish its child pid before idle + grace
        # elapse. Interpreter start-up on a loaded CI runner takes well
        # over the 0.1s the original budget allowed; a 1s + 1s window keeps
        # the reclaim fast while leaving real headroom.
        idle = self.agentctl(
            "run", "start", "--output", "outputs/T-336/idle.txt",
            "--resource", "gpu:0", "--gpu-watchdog",
            "--gpu-idle-seconds", "1", "--gpu-grace-seconds", "1",
            "--gpu-sample-seconds", "0.05", "--gpu-kill-seconds", "1",
            "--gpu-idle-action", "terminate", "--",
            sys.executable, "-c", idle_command,
            env=env,
        )
        idle_id = self.lease_id(idle.stdout, "run")
        deadline = time.monotonic() + 30
        while not idle_pid_file.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(idle_pid_file.exists(), self.supervisor_log_dump())
        waited = self.agentctl(
            "run", "wait", idle_id, "--timeout", "30",
            env=env, expect=1,
        )
        self.assertIn("cancelled", waited.stdout)
        idle_run = json.loads(
            self.agentctl("run", "show", idle_id, "--json", env=env).stdout
        )["runs"][0]
        self.assertEqual(idle_run["watchdog"]["state"], "reclaiming")
        resource_rows = json.loads(
            self.agentctl("resource", "status", "--json", env=env).stdout
        )["resources"]
        idle_resource = next(row for row in resource_rows if row["holder"]["id"] == idle_id)
        self.assertEqual(idle_resource["status"], "released")
        self.assertEqual(idle_resource["supervision"]["state"], "reclaiming")
        self.assertIn(
            "supervision=reclaiming",
            self.agentctl("resource", "status", env=env).stdout,
        )
        if os.name == "posix":
            process_group = idle_run["processes"][0]["process_group"]
            deadline = time.monotonic() + 30
            while (
                workflow._posix_process_group_exists(process_group)
                and time.monotonic() < deadline
            ):
                time.sleep(0.02)
            self.assertFalse(workflow._posix_process_group_exists(process_group))

        # A payload that keeps producing output must never be reclaimed.
        # It runs for ~3s so progress spans many samples even when the
        # interpreter itself is slow to start.
        command = (
            "import time; "
            "[(print(i, flush=True), time.sleep(0.1)) for i in range(30)]"
        )
        progressing = self.agentctl(
            "run", "start", "--output", "outputs/T-336/progress.txt",
            "--resource", "gpu:0", "--gpu-watchdog",
            "--gpu-idle-seconds", "1", "--gpu-grace-seconds", "1",
            "--gpu-sample-seconds", "0.05", "--gpu-idle-action", "terminate", "--",
            sys.executable, "-c", command, env=env,
        )
        progressing_id = self.lease_id(progressing.stdout, "run")
        self.agentctl("run", "wait", progressing_id, "--timeout", "30", env=env)
        progressing_run = json.loads(
            self.agentctl("run", "show", progressing_id, "--json", env=env).stdout
        )["runs"][0]
        self.assertEqual(progressing_run["status"], "succeeded")
        self.assertNotEqual(progressing_run["watchdog"]["state"], "reclaiming")

    def test_gpu_watchdog_progress_is_owner_bound_and_phase_exempt(self):
        self.start("owner", "T-337", "outputs/T-337/")
        self.start("peer", "T-338", "outputs/T-338/")
        fake_bin = self.root / "fake-bin"
        fake_bin.mkdir()
        nvidia_smi = fake_bin / "nvidia-smi"
        nvidia_smi.write_text("#!/bin/sh\nprintf '0, 2048\\n'\n", encoding="utf-8")
        nvidia_smi.chmod(0o755)
        owner_env = self.env(
            "owner", PATH=f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}"
        )
        # Margins are sized for heavily loaded hosts where every controller
        # invocation can take seconds. The idle and grace windows must exceed
        # the worst-case delay before the first progress report lands, and
        # the payload must outlive the whole assertion window (run stop
        # terminates it long before 300s). Exemption handling itself is
        # asserted through the explicit "exempt" watchdog state, and the
        # reclaim path keeps its own dedicated timing regressions.
        started = self.agentctl(
            "run", "start", "--output", "outputs/T-337/exempt.txt",
            "--resource", "gpu:0", "--gpu-watchdog",
            "--gpu-idle-seconds", "60", "--gpu-grace-seconds", "60",
            "--gpu-sample-seconds", "0.05", "--gpu-idle-action", "terminate", "--",
            sys.executable, "-c", "import time; time.sleep(300)", env=owner_env,
        )
        run_id = self.lease_id(started.stdout, "run")
        deadline = time.monotonic() + 30
        while True:
            running = json.loads(
                self.agentctl("run", "show", run_id, "--json", env=owner_env).stdout
            )["runs"][0]
            if running["status"] == "running":
                break
            if time.monotonic() >= deadline:
                self.fail(
                    f"run did not become observable as running: {running}\n"
                    f"supervisor logs:\n{self.supervisor_log_dump()}"
                )
            time.sleep(0.05)
        self.agentctl(
            "run", "progress", run_id, "--phase", "compile", "--token", "kernel-1",
            "--idle-exempt-seconds", "300", env=owner_env,
        )
        refused = self.agentctl(
            "run", "progress", run_id, "--phase", "compile", "--token", "peer",
            "--idle-exempt-seconds", "1", session="peer", expect=1,
        )
        self.assertIn("belongs to conversation", refused.stderr)
        deadline = time.monotonic() + 60
        while True:
            shown = json.loads(
                self.agentctl("run", "show", run_id, "--json", env=owner_env).stdout
            )["runs"][0]
            if (shown.get("watchdog") or {}).get("state") == "exempt":
                break
            if time.monotonic() >= deadline:
                self.fail(
                    f"watchdog never observed the phase exemption: {shown}\n"
                    f"supervisor logs:\n{self.supervisor_log_dump()}"
                )
            time.sleep(0.05)
        self.assertEqual(shown["status"], "running")
        self.assertEqual(shown["progress"]["phase"], "compile")
        self.agentctl(
            "run", "stop", run_id, "--reason", "phase exemption verified", env=owner_env,
        )
        self.agentctl(
            "run", "wait", run_id, "--timeout", "30", env=owner_env, expect=1,
        )
        terminal_progress = self.agentctl(
            "run", "progress", run_id, "--phase", "late", env=owner_env, expect=1,
        )
        self.assertIn("cannot accept progress while cancelled", terminal_progress.stderr)

    def test_gpu_watchdog_fails_safe_for_remote_termination_and_probe_errors(self):
        self.start("one", "T-339", "outputs/T-339/")
        remote = self.agentctl(
            "run", "start", "--output", "outputs/T-339/remote.txt",
            "--resource", "ssh://example.invalid/gpu:0", "--gpu-watchdog",
            "--gpu-idle-action", "terminate", "--",
            sys.executable, "-c", "print('never launched')", expect=1,
        )
        self.assertIn("automatic GPU termination is host-local", remote.stderr)

        unsafe_host = self.agentctl(
            "run", "start", "--output", "outputs/T-339/unsafe-host.txt",
            "--resource", "ssh://-Fmalicious/gpu:0", "--gpu-watchdog",
            "--gpu-idle-action", "report", "--",
            sys.executable, "-c", "print('never launched')", expect=1,
        )
        self.assertIn("requires a canonical", unsafe_host.stderr)

        fake_bin = self.root / "fake-bin"
        fake_bin.mkdir()
        nvidia_smi = fake_bin / "nvidia-smi"
        nvidia_smi.write_text("#!/bin/sh\nexit 7\n", encoding="utf-8")
        nvidia_smi.chmod(0o755)
        env = self.env("one", PATH=f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}")
        # The payload must outlive the supervisor's start-up plus at least
        # one probe, or the watchdog never gets to record the probe error.
        started = self.agentctl(
            "run", "start", "--output", "outputs/T-339/probe-error.txt",
            "--resource", "gpu:0", "--gpu-watchdog",
            "--gpu-idle-seconds", "0", "--gpu-grace-seconds", "0",
            "--gpu-sample-seconds", "0.05", "--gpu-idle-action", "terminate", "--",
            sys.executable, "-c", "import time; time.sleep(4)", env=env,
        )
        run_id = self.lease_id(started.stdout, "run")
        self.agentctl("run", "wait", run_id, "--timeout", "30", env=env)
        shown = json.loads(
            self.agentctl("run", "show", run_id, "--json", env=env).stdout
        )["runs"][0]
        self.assertEqual(shown["status"], "succeeded")
        self.assertEqual(shown["watchdog"]["state"], "probe_error")

    def test_gpu_watchdog_keeps_heartbeats_between_samples_and_rejects_bad_policy(self):
        self.start("one", "T-340", "outputs/T-340/")
        fake_bin = self.root / "fake-bin"
        fake_bin.mkdir()
        nvidia_smi = fake_bin / "nvidia-smi"
        nvidia_smi.write_text("#!/bin/sh\nprintf '50, 2048\\n'\n", encoding="utf-8")
        nvidia_smi.chmod(0o755)
        env = self.env("one", PATH=f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}")

        # The payload only has to outlive the checks below; `run stop` ends
        # it. Every poll below is a full controller start, so the budgets
        # are sized for a loaded CI runner, not a fast laptop.
        started = self.agentctl(
            "run", "start", "--output", "outputs/T-340/heartbeat.txt",
            "--resource", "gpu:0", "--gpu-watchdog",
            "--gpu-sample-seconds", "30", "--gpu-idle-action", "report", "--",
            sys.executable, "-c", "import time; time.sleep(120)", env=env,
        )
        run_id = self.lease_id(started.stdout, "run")
        deadline = time.monotonic() + 30
        while True:
            initial = json.loads(
                self.agentctl("run", "show", run_id, "--json", env=env).stdout
            )["runs"][0]
            if initial["status"] == "running" and initial["watchdog"].get(
                "next_sample_at_ns"
            ):
                break
            if time.monotonic() >= deadline or initial["status"] not in {"starting", "running"}:
                self.fail(
                    f"watchdog did not publish its first sample: {initial}\n"
                    f"supervisor logs:\n{self.supervisor_log_dump()}"
                )
            time.sleep(0.05)
        deadline = time.monotonic() + 30
        while True:
            time.sleep(1.1)
            refreshed = json.loads(
                self.agentctl("run", "show", run_id, "--json", env=env).stdout
            )["runs"][0]
            if refreshed["heartbeat_at"] != initial["heartbeat_at"]:
                break
            if time.monotonic() >= deadline:
                self.fail(f"supervisor heartbeat never advanced: {refreshed}")
        self.assertEqual(refreshed["status"], "running")
        self.assertIn("watchdog=active", self.agentctl("run", "list", env=env).stdout)
        self.agentctl(
            "run", "stop", run_id, "--reason", "heartbeat verified", env=env,
        )
        self.agentctl(
            "run", "wait", run_id, "--timeout", "30", env=env, expect=1,
        )

        policy_path = self.root / ".agent" / "runtime-policy.json"
        policy = json.loads(policy_path.read_text(encoding="utf-8"))
        policy["gpu_watchdog"] = {"enabled": True, "idle_seconds": "not-a-number"}
        policy_path.write_text(json.dumps(policy, indent=2) + "\n", encoding="utf-8")
        invalid = self.agentctl(
            "run", "start", "--output", "outputs/T-340/invalid.txt",
            "--resource", "gpu:0", "--",
            sys.executable, "-c", "print('never launched')", env=env, expect=1,
        )
        self.assertIn("numeric policy values", invalid.stderr)
        self.assertNotIn("Traceback", invalid.stderr)

        cpu = self.agentctl(
            "run", "start", "--output", "outputs/T-340/cpu.txt", "--",
            sys.executable, "-c", "import time; time.sleep(0.2); print('cpu-only')", env=env,
        )
        cpu_id = self.lease_id(cpu.stdout, "run")
        self.agentctl("run", "wait", cpu_id, "--timeout", "30", env=env)

    def test_only_the_owner_conversation_can_stop_a_run(self):
        self.start("owner", "T-341", "outputs/T-341/")
        self.start("peer", "T-342", "outputs/T-342/")
        # The payload must outlive the refusal/stop round trips even on slow
        # Windows runners; run stop terminates it long before 60s.
        started = self.agentctl(
            "run", "start", "--output", "outputs/T-341/result.txt", "--",
            sys.executable, "-c", "import time; time.sleep(60)",
            session="owner",
        )
        run_id = self.lease_id(started.stdout, "run")
        time.sleep(0.2)

        refused = self.agentctl(
            "run", "stop", run_id, "--reason", "not my run",
            session="peer", expect=1,
        )
        self.assertIn("belongs to conversation", refused.stderr)
        self.agentctl(
            "run", "stop", run_id, "--reason", "owner requested stop",
            session="owner",
        )
        self.agentctl(
            "run", "wait", run_id, "--timeout", "10",
            session="owner", expect=1,
        )

    def test_stop_right_after_start_cancels_the_run(self):
        # No sleep between start and stop: on a loaded machine this lands in
        # the window where the supervisor has claimed the lease but not yet
        # registered the payload, which used to be refused as "not alive".
        self.start("one", "T-343", "outputs/T-343/")
        started = self.agentctl(
            "run", "start", "--output", "outputs/T-343/result.txt", "--",
            sys.executable, "-c", "import time; time.sleep(60)",
        )
        run_id = self.lease_id(started.stdout, "run")
        stopped = self.agentctl("run", "stop", run_id, "--reason", "wrong arguments")
        self.assertRegex(stopped.stdout, r"stop requested|stopped before its process launched|stop recorded")
        waited = self.agentctl("run", "wait", run_id, "--timeout", "30", expect=1)
        self.assertIn("cancelled", waited.stdout)
        shown = json.loads(self.agentctl("run", "show", run_id, "--json").stdout)["runs"][0]
        self.assertEqual(shown["status"], "cancelled")
        from tools import agentctl as workflow
        for process in shown.get("processes") or []:
            self.assertFalse(workflow._runtime_process_alive(process))

    def test_stop_in_the_launch_window_waits_for_the_payload(self):
        # Deterministic version of the window: a lease whose supervisor is a
        # live process and whose payload has not been registered. The stop
        # must record the request, wait, then signal the payload the moment
        # it appears.
        from tools import agentctl as workflow

        self.start("one", "T-344", "outputs/T-344/")
        session_key = json.loads(
            self.agentctl("sessions", "list", "--json").stdout
        )["current"]
        supervisor = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        self.addCleanup(supervisor.kill)
        run_id = "run-launchwindow001"
        workflow._update_runtime_leases(self.root, lambda data: data.setdefault("leases", []).append({
            "id": run_id, "kind": "run", "mode": "supervised", "status": "running",
            "task": "T-344", "holder": {"type": "conversation", "id": session_key},
            "checkout": str(self.root), "scope": ["outputs/T-344/"], "outputs": [], "resources": [],
            "processes": [],
            "supervisor_process": {
                "role": "supervisor", "pid": supervisor.pid,
                "birth_marker": workflow._process_birth_marker(supervisor.pid),
            },
            "created_at": workflow._now(), "heartbeat_at": workflow._now(),
        }))
        stopper = subprocess.Popen(
            [sys.executable, "tools/agentctl.py", "run", "stop", run_id,
             "--reason", "operator", "--wait-seconds", "20"],
            cwd=self.root, env=self.env("one"), text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        # The stop is recorded before any payload exists...
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            lease = workflow._runtime_lease(self.root, run_id) or {}
            if lease.get("status") == "stopping":
                break
            time.sleep(0.05)
        self.assertEqual((workflow._runtime_lease(self.root, run_id) or {}).get("status"), "stopping")
        # ...and the payload is signalled as soon as it is registered.
        payload = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        self.addCleanup(payload.kill)
        record = {
            "role": "command", "pid": payload.pid,
            "birth_marker": workflow._process_birth_marker(payload.pid),
            "process_group": None,
        }
        workflow._run_update(self.root, run_id, lambda item: item.update({"processes": [record]}))
        out, err = stopper.communicate(timeout=60)
        self.assertEqual(stopper.returncode, 0, out + err)
        self.assertIn("stop requested", out)
        try:
            payload.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self.fail("payload was not terminated after registration")
        supervisor.kill()

    def test_stop_before_the_supervisor_claim_still_lets_the_claim_land(self):
        # `run start` can return while the supervisor is alive but has not
        # claimed yet. A stop in that state marks the lease stopping; the
        # claim must still succeed so the supervisor can finalize the
        # cancellation instead of exiting and stranding the lease.
        from tools import agentctl as workflow

        self.start("one", "T-346", "outputs/T-346/")
        session_key = json.loads(self.agentctl("sessions", "list", "--json").stdout)["current"]
        supervisor = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        self.addCleanup(supervisor.kill)
        token = "a" * 64
        run_id = "run-preclaimstop001"
        workflow._update_runtime_leases(self.root, lambda data: data.setdefault("leases", []).append({
            "id": run_id, "kind": "run", "mode": "supervised", "status": "starting",
            "task": "T-346", "holder": {"type": "conversation", "id": session_key},
            "checkout": str(self.root), "scope": ["outputs/T-346/"], "outputs": [], "resources": [],
            "processes": [],
            "supervisor_token_sha256": hashlib.sha256(token.encode("utf-8")).hexdigest(),
            "supervisor_process": {
                "role": "supervisor", "pid": supervisor.pid,
                "birth_marker": workflow._process_birth_marker(supervisor.pid),
            },
            "created_at": workflow._now(), "heartbeat_at": workflow._now(),
        }))
        stopped = self.agentctl("run", "stop", run_id, "--reason", "changed my mind", "--wait-seconds", "1")
        self.assertIn("stop recorded", stopped.stdout)
        self.assertEqual((workflow._runtime_lease(self.root, run_id) or {}).get("status"), "stopping")

        claimed, error = workflow._run_supervisor_claim(self.root, run_id, token)
        self.assertIsNone(error, error)
        self.assertTrue(claimed)
        self.assertTrue(workflow._run_stop_requested_before_launch(self.root, run_id))
        # And a second claim is still refused: the token is single-use.
        again, error_again = workflow._run_supervisor_claim(self.root, run_id, token)
        self.assertIsNone(again)
        self.assertIn("already claimed", error_again)

    @unittest.skipIf(os.name == "nt", "SIGTERM handling is POSIX-specific")
    def test_stop_sends_sigterm_exactly_once(self):
        # `run stop` signals directly and the supervisor also honors the stop;
        # the payload must see one SIGTERM, then the kill after --kill-seconds.
        self.start("one", "T-347", "outputs/T-347/")
        marker = self.root / "outputs" / "T-347" / "terms.txt"
        command = (
            "import signal, time, pathlib; "
            f"p=pathlib.Path({str(marker)!r}); p.parent.mkdir(parents=True, exist_ok=True); "
            "signal.signal(signal.SIGTERM, lambda *_: p.open('a').write('TERM\\n')); "
            "p.write_text(''); time.sleep(120)"
        )
        started = self.agentctl(
            "run", "start", "--output", "outputs/T-347/terms.txt", "--",
            sys.executable, "-c", command,
        )
        run_id = self.lease_id(started.stdout, "run")
        deadline = time.monotonic() + 30
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertTrue(marker.exists())
        self.agentctl("run", "stop", run_id, "--reason", "once", "--kill-seconds", "3")
        waited = self.agentctl("run", "wait", run_id, "--timeout", "30", expect=1)
        self.assertIn("cancelled", waited.stdout)
        self.assertEqual(marker.read_text(encoding="utf-8").count("TERM"), 1)

    @unittest.skipIf(os.name == "nt", "SIGTERM handling is POSIX-specific")
    def test_stop_escalates_to_kill_for_a_payload_that_ignores_sigterm(self):
        # Without a watchdog there used to be no escalation at all: a payload
        # that ignored SIGTERM could not be stopped through the kit.
        self.start("one", "T-345", "outputs/T-345/")
        started = self.agentctl(
            "run", "start", "--output", "outputs/T-345/result.txt", "--",
            sys.executable, "-c",
            "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(120)",
        )
        run_id = self.lease_id(started.stdout, "run")
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            shown = json.loads(self.agentctl("run", "show", run_id, "--json").stdout)["runs"][0]
            if shown.get("processes"):
                break
            time.sleep(0.1)
        self.agentctl("run", "stop", run_id, "--reason", "hung", "--kill-seconds", "1")
        waited = self.agentctl("run", "wait", run_id, "--timeout", "30", expect=1)
        self.assertIn("cancelled", waited.stdout)
        shown = json.loads(self.agentctl("run", "show", run_id, "--json").stdout)["runs"][0]
        self.assertEqual(shown["status"], "cancelled")
        self.assertTrue(shown.get("supervisor_kill_sent_at_ns"), shown)

    def test_supervisor_entrypoint_is_single_use(self):
        self.start("one", "T-351", "outputs/T-351/")
        command = (
            "from pathlib import Path; import os, time; "
            "p=Path('outputs/T-351/pids.txt'); p.parent.mkdir(parents=True, exist_ok=True); "
            "p.write_text(str(os.getpid())+'\\n', encoding='utf-8'); time.sleep(0.5)"
        )
        started = self.agentctl(
            "run", "start", "--output", "outputs/T-351/pids.txt", "--",
            sys.executable, "-c", command, session="one",
        )
        run_id = self.lease_id(started.stdout, "run")
        replay = self.agentctl(
            "_run-supervise", "--root", str(self.root), "--lease", run_id,
            "--token", "invalid-replay-token", session="one", expect=1,
        )
        self.assertIn("claim rejected", replay.stderr)
        self.agentctl("run", "wait", run_id, "--timeout", "20", session="one")
        lines = (
            self.root / "outputs" / "T-351" / "pids.txt"
        ).read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 1)

    def test_request_id_makes_auto_create_idempotent(self):
        self.agentctl(
            "work", "--agent", "codex", "--auto-create", "--new-id", "T-701",
            "--title", "durable creation", "--scope", "src/a/",
            "--request-id", "req-alpha",
        )
        # Same token, same intent: the retry returns the recorded task
        # instead of creating a second one.
        replay = self.agentctl(
            "work", "--agent", "codex", "--auto-create", "--new-id", "T-701",
            "--title", "durable creation", "--scope", "src/a/",
            "--request-id", "req-alpha", session="two",
        )
        self.assertIn("already created task T-701", replay.stdout)
        board = json.loads(self.agentctl("board", "--json").stdout)
        self.assertIn("T-701", board["tasks"])

        # Same token, different intent: fail closed.
        conflict = self.agentctl(
            "work", "--agent", "codex", "--auto-create", "--new-id", "T-702",
            "--title", "a different creation", "--scope", "src/b/",
            "--request-id", "req-alpha", session="three", expect=2,
        )
        self.assertIn("different creation intent", conflict.stderr)

        # An interrupted attempt whose allocation never landed anywhere
        # blocks instead of silently relaunching.
        import hashlib as _hashlib
        stuck_payload = {
            "kind": "auto-create",
            "agent": "codex",
            "title": "interrupted creation",
            "scope": ["src/c/"],
            "type": "generic",
            "new_id": "T-703",
            "deps": "",
        }
        stuck_digest = _hashlib.sha256(json.dumps(
            stuck_payload, sort_keys=True, ensure_ascii=False,
        ).encode("utf-8")).hexdigest()
        requests_dir = self.workflow_common_dir() / "requests"
        requests_dir.mkdir(parents=True, exist_ok=True)
        token_hash = _hashlib.sha256(b"req-stuck").hexdigest()[:16]
        (requests_dir / f"req-{token_hash}.json").write_text(json.dumps({
            "version": 1,
            "token": "req-stuck",
            "kind": "auto-create",
            "intent_digest": stuck_digest,
            "state": "preparing",
            "result": {"task": "T-703"},
            "error": "",
        }), encoding="utf-8")
        blocked = self.agentctl(
            "work", "--agent", "codex", "--auto-create", "--new-id", "T-703",
            "--title", "interrupted creation", "--scope", "src/c/",
            "--request-id", "req-stuck", session="four", expect=1,
        )
        self.assertIn("interrupted attempt", blocked.stderr)

        # Once the allocated task is actually durable, the same interrupted
        # record converges to a confirmed replay instead of blocking.
        self.agentctl(
            "task", "create", "--id", "T-703", "--title", "interrupted creation",
            "--owner", "codex", "--scope", "src/c/", session="four",
        )
        converged = self.agentctl(
            "work", "--agent", "codex", "--auto-create", "--new-id", "T-703",
            "--title", "interrupted creation", "--scope", "src/c/",
            "--request-id", "req-stuck", session="four",
        )
        self.assertIn("already created task T-703", converged.stdout)

    def test_request_id_makes_run_start_idempotent(self):
        self.start("one", "T-711", "src/run/")
        started = self.agentctl(
            "run", "start", "--output", "src/run/", "--request-id", "run-alpha",
            "--", sys.executable, "-c", "print('ok')",
        )
        run_id = self.lease_id(started.stdout, "run")
        replay = self.agentctl(
            "run", "start", "--output", "src/run/", "--request-id", "run-alpha",
            "--", sys.executable, "-c", "print('ok')",
        )
        self.assertIn(f"already started run {run_id}", replay.stdout)
        runs = json.loads(self.agentctl("run", "list", "--json").stdout)["runs"]
        matching = [row for row in runs if row.get("id") == run_id]
        self.assertEqual(len(matching), 1)
        conflict = self.agentctl(
            "run", "start", "--output", "src/run/", "--request-id", "run-alpha",
            "--", sys.executable, "-c", "print('different')",
            expect=2,
        )
        self.assertIn("different run intent", conflict.stderr)

    def test_worktree_bootstrap_timeout_marks_lease_failed_and_sweeps(self):
        import argparse
        import importlib.util

        subprocess.run(
            ["git", "add", "-A"], cwd=self.root, check=True,
            capture_output=True, timeout=60,
        )
        subprocess.run(
            ["git", "-c", "user.email=t@example.com", "-c", "user.name=T",
             "commit", "--no-verify", "-q", "-m", "baseline"],
            cwd=self.root, check=True, capture_output=True, timeout=60,
        )
        spec = importlib.util.spec_from_file_location(
            "agentctl_worktree_timeout_test", KIT / "tools" / "agentctl.py",
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        real_git_process = module._git_process

        def timing_out_git(root, *cmd, timeout=120):
            if cmd[:2] == ("worktree", "add"):
                raise subprocess.TimeoutExpired(list(cmd), timeout)
            return real_git_process(root, *cmd, timeout=timeout)

        create_args = argparse.Namespace(
            id="T-721", title="timed out bootstrap", owner="codex",
            scope="src/w/", deps="", task_type="code", force=False,
        )
        with mock.patch.object(module, "_git_process", timing_out_git):
            rc = module._worktree_bootstrap_task(
                self.root, create_args, "codex", "worktree",
            )
        self.assertEqual(rc, 2)
        leases = json.loads(
            (self.workflow_common_dir() / "worktree-leases.json").read_text(
                encoding="utf-8",
            )
        )["leases"]
        matching = [row for row in leases if row.get("task") == "T-721"]
        self.assertEqual(len(matching), 1)
        self.assertEqual(matching[0]["status"], "failed")
        self.assertIn("timed out", matching[0]["last_error"] or "")
        # The half-materialized checkout and its branch are swept, so a fresh
        # attempt is not blocked by leftovers.
        heads = subprocess.run(
            ["git", "branch", "--list", "feature/T-721-codex"],
            cwd=self.root, text=True, capture_output=True, timeout=60,
        ).stdout.strip()
        self.assertEqual(heads, "")

    def workflow_common_dir(self):
        common = subprocess.run(
            ["git", "rev-parse", "--git-common-dir"], cwd=self.root,
            check=True, text=True, capture_output=True, timeout=60,
        ).stdout.strip()
        common_path = Path(common)
        if not common_path.is_absolute():
            common_path = self.root / common_path
        return common_path.resolve() / "agent-workflow"

    def hold_leases_lock(self, seconds):
        """Hold the shared lease-registry lock from the test process."""
        import fcntl

        lock_path = self.workflow_common_dir() / "execution-leases.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            time.sleep(seconds)
        finally:
            os.close(fd)

    def wait_run_with_diagnostics(self, run_id, session="one"):
        """run wait that surfaces the supervisor's stderr log on failure."""
        proc = subprocess.run(
            [sys.executable, "tools/agentctl.py", "run", "wait", run_id,
             "--timeout", "60"],
            cwd=self.root, env=self.env(session), text=True,
            capture_output=True, timeout=150,
        )
        if proc.returncode != 0:
            log_path = (
                self.workflow_common_dir() / "runs" / f"{run_id}.supervisor.log"
            )
            try:
                log = log_path.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                log = f"<unreadable: {exc}>"
            self.fail(
                f"run wait rc={proc.returncode}: {proc.stdout}{proc.stderr}\n"
                f"supervisor log ({log_path}):\n{log}"
            )
        return proc

    def test_await_supervisor_claim_flags_pre_claim_death_and_slow_start(self):
        from tools import agentctl as workflow

        dead = subprocess.Popen([sys.executable, "-c", "raise SystemExit(3)"])
        dead.wait()
        alive = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
        )
        self.addCleanup(alive.kill)
        leases = [
            {
                "id": "run-preclaimdead001",
                "kind": "run", "mode": "supervised", "status": "starting",
                "processes": [],
                "supervisor_process": {
                    "role": "supervisor", "pid": dead.pid,
                    "birth_marker": "test:never-matches",
                },
            },
            {
                "id": "run-slowclaim000001",
                "kind": "run", "mode": "supervised", "status": "starting",
                "processes": [],
                "supervisor_process": {
                    "role": "supervisor", "pid": alive.pid,
                    "birth_marker": workflow._process_birth_marker(alive.pid),
                },
            },
            {
                "id": "run-claimed00000001",
                "kind": "run", "mode": "supervised", "status": "starting",
                "processes": [],
                "supervisor_claimed_at": "2026-01-01 00:00:00",
                "supervisor_process": {},
            },
        ]
        workflow._update_runtime_leases(
            self.root, lambda data: data.setdefault("leases", []).extend(leases),
        )
        self.assertEqual(
            workflow._await_supervisor_claim(
                self.root, "run-preclaimdead001", timeout_seconds=5.0,
            ),
            "died",
        )
        self.assertEqual(
            workflow._await_supervisor_claim(
                self.root, "run-slowclaim000001", timeout_seconds=0.3,
            ),
            "pending",
        )
        self.assertEqual(
            workflow._await_supervisor_claim(
                self.root, "run-claimed00000001", timeout_seconds=0.3,
            ),
            "claimed",
        )

    def test_supervisor_argv_survives_tokens_that_start_with_a_dash(self):
        # secrets.token_urlsafe starts with "-" one time in 64. Passed as
        # `--token -abc...`, argparse reported a missing option value and
        # the supervisor died before claiming, so roughly 1.5% of run starts
        # never launched their payload and surfaced later as exited_unknown.
        from tools import agentctl as workflow

        for token in ("-" + "a" * 42, "--" + "b" * 41, "-_-" + "c" * 40, "d" * 64):
            argv = workflow._supervisor_argv(self.root, "run-0123456789abcdef", token)
            self.assertEqual(argv[0], sys.executable)
            self.assertTrue(argv[1].endswith("agentctl.py"), argv)
            args = workflow.build_parser().parse_args(argv[2:])
            self.assertEqual(args.cmd, "_run-supervise")
            self.assertEqual(args.root, str(self.root))
            self.assertEqual(args.lease, "run-0123456789abcdef")
            self.assertEqual(args.token, token)

    def test_await_supervisor_claim_sees_through_a_zombie_supervisor(self):
        # `run start` is the supervisor's parent. A supervisor that crashes
        # before claiming stays a zombie until reaped, and a zombie still
        # answers kill(pid, 0) with a matching birth marker -- so the
        # pid-based check alone reported it alive for the whole timeout and
        # the replacement spawn never happened (seen as exited_unknown on
        # a slow CI runner).
        from tools import agentctl as workflow

        zombie = subprocess.Popen(
            [sys.executable, "-c", "raise SystemExit(3)"], start_new_session=True,
        )
        self.addCleanup(zombie.wait)
        # Let the child exit without reaping it. Linux exposes the zombie
        # state in /proc; elsewhere a generous sleep covers interpreter exit.
        deadline = time.monotonic() + 10
        if sys.platform.startswith("linux"):
            while not workflow._posix_process_is_zombie(zombie.pid) and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue(workflow._posix_process_is_zombie(zombie.pid))
        else:
            time.sleep(1.0)
        workflow._update_runtime_leases(
            self.root, lambda data: data.setdefault("leases", []).append({
                "id": "run-zombiesuper0001",
                "kind": "run", "mode": "supervised", "status": "starting",
                "processes": [],
                "supervisor_process": {
                    "role": "supervisor", "pid": zombie.pid,
                    "birth_marker": workflow._process_birth_marker(zombie.pid),
                },
            }),
        )
        started = time.monotonic()
        self.assertEqual(
            workflow._await_supervisor_claim(
                self.root, "run-zombiesuper0001", process=zombie, timeout_seconds=10.0,
            ),
            "died",
        )
        self.assertLess(time.monotonic() - started, 5.0)
        if sys.platform.startswith("linux"):
            # Defense in depth: other processes (run show, doctor) read the
            # zombie state from /proc and stop counting it as live too.
            again = subprocess.Popen(
                [sys.executable, "-c", "raise SystemExit(3)"], start_new_session=True,
            )
            self.addCleanup(again.wait)
            deadline = time.monotonic() + 10
            while not workflow._posix_process_is_zombie(again.pid) and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue(workflow._posix_process_is_zombie(again.pid))
            self.assertFalse(workflow._pid_alive(again.pid))

    def test_resources_orphaned_by_finished_runs_are_released(self):
        from tools import agentctl as workflow

        lock_root = self.root / ".orphan-locks"

        def provider(name):
            path = lock_root / name
            path.mkdir(parents=True)
            (path / "owner.json").write_text("{}", encoding="utf-8")
            return {"provider": "local-mkdir", "path": str(path)}

        aged = (
            workflow._dt.datetime.now() - workflow._dt.timedelta(hours=1)
        ).strftime("%Y-%m-%d %H:%M:%S")
        leases = [
            {"id": "run-tttttttttttttttt", "kind": "run", "mode": "supervised",
             "status": "succeeded", "finished_at": workflow._now()},
            {"id": "run-llllllllllllllll", "kind": "run", "mode": "supervised",
             "status": "running", "heartbeat_at": workflow._now()},
            {"id": "resource-aaaaaaaaaaaaaaaa", "kind": "resource",
             "status": "active",
             "holder": {"type": "run", "id": "run-tttttttttttttttt"},
             "provider": provider("terminal-run")},
            {"id": "resource-bbbbbbbbbbbbbbbb", "kind": "resource",
             "status": "active", "created_at": aged,
             "holder": {"type": "run", "id": "run-missing000000000"},
             "provider": provider("missing-run")},
            {"id": "resource-cccccccccccccccc", "kind": "resource",
             "status": "active",
             "holder": {"type": "run", "id": "run-llllllllllllllll"},
             "provider": provider("live-run")},
            {"id": "resource-dddddddddddddddd", "kind": "resource",
             "status": "active",
             "holder": {"type": "conversation", "id": "session-x"},
             "provider": provider("conversation")},
            # run start acquires resources before registering the run lease:
            # a fresh missing-holder lease must survive the hygiene pass.
            {"id": "resource-ffffffffffffffff", "kind": "resource",
             "status": "active", "created_at": workflow._now(),
             "holder": {"type": "run", "id": "run-starting00000000"},
             "provider": provider("starting-run")},
        ]
        workflow._update_runtime_leases(
            self.root, lambda data: data.setdefault("leases", []).extend(leases),
        )

        workflow._release_orphaned_resources(self.root)

        rows = {
            lease["id"]: lease
            for lease in workflow._load_runtime_leases(self.root)["leases"]
            if isinstance(lease, dict)
        }
        self.assertEqual(rows["resource-aaaaaaaaaaaaaaaa"]["status"], "released")
        self.assertEqual(
            rows["resource-aaaaaaaaaaaaaaaa"]["release_reason"],
            "holding run finished without releasing",
        )
        self.assertEqual(rows["resource-bbbbbbbbbbbbbbbb"]["status"], "released")
        self.assertEqual(rows["resource-cccccccccccccccc"]["status"], "active")
        self.assertEqual(rows["resource-dddddddddddddddd"]["status"], "active")
        self.assertEqual(rows["resource-ffffffffffffffff"]["status"], "active")
        self.assertFalse((lock_root / "terminal-run").exists())
        self.assertFalse((lock_root / "missing-run").exists())
        self.assertTrue((lock_root / "live-run").exists())
        self.assertTrue((lock_root / "conversation").exists())
        self.assertTrue((lock_root / "starting-run").exists())

    def write_session_record(self, key, **fields):
        from tools import agentctl as workflow

        record = {
            "workflow_session_key": key,
            "task": fields.pop("task", "T-900"),
            "checkout": str(self.root.resolve()),
            "heartbeat_at": workflow._now(),
            "heartbeat_ns": time.time_ns(),
        }
        record.update(fields)
        path = workflow._session_path(self.root, key)
        path.parent.mkdir(parents=True, exist_ok=True)
        workflow._save_json(path, record)

    def test_resources_orphaned_by_released_sessions_are_released(self):
        from tools import agentctl as workflow

        lock_root = self.root / ".session-orphan-locks"

        def provider(name):
            path = lock_root / name
            path.mkdir(parents=True)
            (path / "owner.json").write_text("{}", encoding="utf-8")
            return {"provider": "local-mkdir", "path": str(path)}

        stale_ns = time.time_ns() - 10 * 3600 * 1_000_000_000
        self.write_session_record("session-gone01", presence_status="released")
        self.write_session_record("session-live01")
        self.write_session_record("session-stale1", heartbeat_ns=stale_ns)
        aged = (
            workflow._dt.datetime.now() - workflow._dt.timedelta(hours=2)
        ).strftime("%Y-%m-%d %H:%M:%S")
        leases = [
            {"id": "resource-gonegonegonegone", "kind": "resource",
             "status": "active", "created_at": aged,
             "holder": {"type": "conversation", "id": "session-gone01"},
             "provider": provider("released-session")},
            {"id": "resource-livelivelivelive", "kind": "resource",
             "status": "active", "created_at": aged,
             "holder": {"type": "conversation", "id": "session-live01"},
             "provider": provider("live-session")},
            {"id": "resource-stalestalestale1", "kind": "resource",
             "status": "active", "created_at": aged,
             "holder": {"type": "conversation", "id": "session-stale1"},
             "provider": provider("stale-session")},
            {"id": "resource-oldmissingoldmis", "kind": "resource",
             "status": "active", "created_at": aged,
             "holder": {"type": "conversation", "id": "session-none01"},
             "provider": provider("old-missing-session")},
            {"id": "resource-newmissingnewmis", "kind": "resource",
             "status": "active", "created_at": workflow._now(),
             "holder": {"type": "conversation", "id": "session-none01"},
             "provider": provider("new-missing-session")},
        ]
        workflow._update_runtime_leases(
            self.root, lambda data: data.setdefault("leases", []).extend(leases),
        )

        workflow._release_orphaned_resources(self.root)

        rows = {
            lease["id"]: lease
            for lease in workflow._load_runtime_leases(self.root)["leases"]
            if isinstance(lease, dict)
        }
        self.assertEqual(rows["resource-gonegonegonegone"]["status"], "released")
        self.assertEqual(
            rows["resource-gonegonegonegone"]["release_reason"],
            "holding session released without freeing the resource",
        )
        self.assertEqual(rows["resource-livelivelivelive"]["status"], "active")
        self.assertEqual(rows["resource-stalestalestale1"]["status"], "active")
        self.assertEqual(rows["resource-oldmissingoldmis"]["status"], "released")
        self.assertEqual(
            rows["resource-oldmissingoldmis"]["release_reason"],
            "holding session record no longer exists",
        )
        self.assertEqual(rows["resource-newmissingnewmis"]["status"], "active")
        self.assertFalse((lock_root / "released-session").exists())
        self.assertTrue((lock_root / "live-session").exists())
        self.assertTrue((lock_root / "stale-session").exists())
        self.assertFalse((lock_root / "old-missing-session").exists())
        self.assertTrue((lock_root / "new-missing-session").exists())

    def test_acquire_self_heals_dead_holders_and_explains_live_conflicts(self):
        from tools import agentctl as workflow

        with mock.patch.dict(os.environ, {
            "AGENT_WORKFLOW_RESOURCE_LOCK_DIR": str(self.root / ".resource-locks"),
        }):
            # A finished run left its resource behind: the next acquire
            # must release the orphan and succeed instead of interlocking.
            dead, error = workflow._resource_acquire_one(
                self.root, "T-931", "host:heal:gpu:0",
                "run", "run-deaddeaddeaddead",
            )
            self.assertIsNone(error)
            workflow._update_runtime_leases(
                self.root,
                lambda data: data.setdefault("leases", []).append({
                    "id": "run-deaddeaddeaddead", "kind": "run",
                    "mode": "supervised", "status": "succeeded",
                    "finished_at": workflow._now(),
                }),
            )
            healed, error = workflow._resource_acquire_one(
                self.root, "T-932", "host:heal:gpu:0",
                "conversation", "session-live01",
            )
            self.assertIsNone(error, error)
            rows = {
                lease["id"]: lease
                for lease in workflow._load_runtime_leases(self.root)["leases"]
                if isinstance(lease, dict)
            }
            self.assertEqual(rows[dead["id"]]["status"], "released")
            self.assertEqual(rows[healed["id"]]["status"], "active")

            # A live conversation holder is a genuine conflict: the
            # rejection must name the holder without inviting a forced
            # release.
            self.write_session_record("session-live01")
            refused, error = workflow._resource_acquire_one(
                self.root, "T-933", "host:heal:gpu:0",
                "conversation", "session-other1",
            )
            self.assertIsNone(refused)
            self.assertIn("conversation:session-live01", error)
            self.assertIn("is active", error)
            self.assertNotIn("--force-stale", error)

            # A stale holder keeps the lock but the rejection must point
            # at the recovery command.
            stale_ns = time.time_ns() - 10 * 3600 * 1_000_000_000
            self.write_session_record("session-stale1", heartbeat_ns=stale_ns)
            held, error = workflow._resource_acquire_one(
                self.root, "T-934", "host:heal:gpu:1",
                "conversation", "session-stale1",
            )
            self.assertIsNone(error)
            refused, error = workflow._resource_acquire_one(
                self.root, "T-935", "host:heal:gpu:1",
                "conversation", "session-other1",
            )
            self.assertIsNone(refused)
            self.assertIn(held["id"], error)
            self.assertIn("--force-stale", error)

            # A missing holder still inside the registration grace window
            # may be a run whose lease registration is in flight: neither
            # the self-heal sweep nor the rejection hint may treat it as
            # dead.
            pending, error = workflow._resource_acquire_one(
                self.root, "T-936", "host:heal:gpu:2",
                "run", "run-notyet0000000000",
            )
            self.assertIsNone(error)
            refused, error = workflow._resource_acquire_one(
                self.root, "T-937", "host:heal:gpu:2",
                "conversation", "session-other1",
            )
            self.assertIsNone(refused)
            self.assertIn(pending["id"], error)
            self.assertNotIn("--force-stale", error)

    def test_sessions_release_frees_resources_held_by_the_session(self):
        self.start("one", "T-941", "outputs/T-941/")
        acquired = self.agentctl(
            "resource", "acquire", "host:release:gpu:0", session="one",
        )
        resource_id = self.lease_id(acquired.stdout, "resource")

        released = self.agentctl(
            "sessions", "release", "--reason", "handoff", session="one",
        )
        self.assertIn("released 1 resource lease(s)", released.stdout)
        self.assertIn(resource_id, released.stdout)

        rows = json.loads(
            self.agentctl("resource", "status", "--json", session="two").stdout
        )["resources"]
        lease = next(row for row in rows if row["id"] == resource_id)
        self.assertEqual(lease["status"], "released")
        self.assertEqual(lease["release_reason"], "holding session released")

    def test_force_stale_release_frees_dead_holders_but_refuses_live_ones(self):
        from tools import agentctl as workflow

        self.start("owner", "T-951", "outputs/T-951/")
        self.start("peer", "T-952", "outputs/T-952/")
        acquired = self.agentctl(
            "resource", "acquire", "host:force:gpu:0", session="owner",
        )
        live_id = self.lease_id(acquired.stdout, "resource")

        refused = self.agentctl(
            "resource", "release", live_id, "--reason", "takeover",
            "--force-stale", session="peer", expect=1,
        )
        self.assertIn("still live", refused.stderr)

        # A missing holder inside the registration grace window may be a
        # run that acquired its resources but has not registered its
        # lease yet: force-stale must refuse it.
        lock_dir = self.root / ".resource-locks-dead"
        lock_dir.mkdir()
        (lock_dir / "owner.json").write_text("{}", encoding="utf-8")
        workflow._update_runtime_leases(
            self.root,
            lambda data: data.setdefault("leases", []).append({
                "id": "resource-deadholderdeadho", "kind": "resource",
                "status": "active", "created_at": workflow._now(),
                "resources": ["host:force:gpu:9"],
                "holder": {"type": "run", "id": "run-vanished00000000"},
                "provider": {"provider": "local-mkdir", "path": str(lock_dir)},
            }),
        )
        too_young = self.agentctl(
            "resource", "release", "resource-deadholderdeadho",
            "--reason", "dead run cleanup", "--force-stale", session="peer",
            expect=1,
        )
        self.assertIn("registration grace window", too_young.stderr)

        # Past the grace window the same lease is exactly the interlock
        # the flag exists for.
        aged = (
            workflow._dt.datetime.now() - workflow._dt.timedelta(hours=2)
        ).strftime("%Y-%m-%d %H:%M:%S")

        def age_lease(data):
            for row in data.get("leases") or []:
                if isinstance(row, dict) and row.get("id") == "resource-deadholderdeadho":
                    row["created_at"] = aged

        workflow._update_runtime_leases(self.root, age_lease)
        self.agentctl(
            "resource", "release", "resource-deadholderdeadho",
            "--reason", "dead run cleanup", "--force-stale", session="peer",
        )
        rows = json.loads(
            self.agentctl("resource", "status", "--json", session="peer").stdout
        )["resources"]
        forced = next(row for row in rows if row["id"] == "resource-deadholderdeadho")
        self.assertEqual(forced["status"], "released")
        self.assertEqual(forced["release_mode"], "force-stale")
        self.assertFalse(lock_dir.exists())

        self.agentctl(
            "resource", "release", live_id, "--reason", "owner done",
            session="owner",
        )

    def test_doctor_reports_leases_stuck_without_live_holders(self):
        from tools import agentctl as workflow

        stale_ns = time.time_ns() - 10 * 3600 * 1_000_000_000
        self.write_session_record("session-stale1", heartbeat_ns=stale_ns)
        workflow._update_runtime_leases(
            self.root,
            lambda data: data.setdefault("leases", []).extend([
                {"id": "resource-stuckstuckstucks", "kind": "resource",
                 "status": "active", "created_at": workflow._now(),
                 "resources": ["host:doc:gpu:0"],
                 "holder": {"type": "conversation", "id": "session-stale1"},
                 "provider": {}},
                {"id": "resource-failedfailedfail", "kind": "resource",
                 "status": "release_failed", "release_error": "rmdir denied",
                 "resources": ["host:doc:gpu:1"],
                 "holder": {"type": "conversation", "id": "session-stale1"},
                 "provider": {}},
            ]),
        )

        findings = workflow._resource_interlock_findings(self.root)

        text = "\n".join(findings)
        self.assertIn("resource-stuckstuckstucks", text)
        self.assertIn("session-stale1 is stale", text)
        self.assertIn("--force-stale", text)
        self.assertIn("resource-failedfailedfail", text)
        self.assertIn("rmdir denied", text)

    def test_terminal_run_state_prunes_after_retention_window(self):
        from tools import agentctl as workflow

        runs_dir = workflow._runtime_runs_dir(self.root)
        old = (
            workflow._dt.datetime.now() - workflow._dt.timedelta(days=30)
        ).strftime("%Y-%m-%d %H:%M:%S")
        fresh = workflow._now()
        leases = [
            {"id": "run-aaaaaaaaaaaaaaaa", "kind": "run", "mode": "supervised",
             "status": "succeeded", "finished_at": old},
            {"id": "run-bbbbbbbbbbbbbbbb", "kind": "run", "mode": "supervised",
             "status": "succeeded", "finished_at": fresh},
            {"id": "run-cccccccccccccccc", "kind": "run", "mode": "supervised",
             "status": "running", "heartbeat_at": old},
            {"id": "resource-dddddddddddddddd", "kind": "resource",
             "status": "released", "released_at": old},
            {"id": "resource-eeeeeeeeeeeeeeee", "kind": "resource",
             "status": "release_failed", "released_at": old},
        ]
        workflow._update_runtime_leases(
            self.root, lambda data: data.setdefault("leases", []).extend(leases),
        )
        for name in (
            "run-aaaaaaaaaaaaaaaa.stdout.log",
            "run-cccccccccccccccc.supervisor.log",
            "run-ffffffffffffffff.supervisor.log",
            "run-9999999999999999.command.json",
        ):
            (runs_dir / name).write_text("x", encoding="utf-8")
        stale = (
            workflow._dt.datetime.now() - workflow._dt.timedelta(days=30)
        ).timestamp()
        for name in (
            "run-aaaaaaaaaaaaaaaa.stdout.log",
            "run-cccccccccccccccc.supervisor.log",
            "run-ffffffffffffffff.supervisor.log",
        ):
            os.utime(runs_dir / name, (stale, stale))

        workflow._prune_terminal_run_state(self.root)

        remaining = {
            lease["id"]
            for lease in workflow._load_runtime_leases(self.root)["leases"]
            if isinstance(lease, dict)
        }
        self.assertIn("run-bbbbbbbbbbbbbbbb", remaining)
        self.assertIn("run-cccccccccccccccc", remaining)
        self.assertIn("resource-eeeeeeeeeeeeeeee", remaining)
        self.assertNotIn("run-aaaaaaaaaaaaaaaa", remaining)
        self.assertNotIn("resource-dddddddddddddddd", remaining)
        self.assertFalse((runs_dir / "run-aaaaaaaaaaaaaaaa.stdout.log").exists())
        self.assertTrue((runs_dir / "run-cccccccccccccccc.supervisor.log").exists())
        self.assertFalse((runs_dir / "run-ffffffffffffffff.supervisor.log").exists())
        self.assertTrue((runs_dir / "run-9999999999999999.command.json").exists())

        policy_path = self.root / ".agent" / "runtime-policy.json"
        if policy_path.is_file():
            policy = json.loads(policy_path.read_text(encoding="utf-8"))
        else:
            policy = {"version": 1}
        policy["run_artifact_retention_days"] = 0
        policy_path.write_text(
            json.dumps(policy, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        disabled_seed = {
            "id": "run-1111111111111111", "kind": "run", "mode": "supervised",
            "status": "failed", "finished_at": old,
        }
        workflow._update_runtime_leases(
            self.root,
            lambda data: data.setdefault("leases", []).append(disabled_seed),
        )
        workflow._prune_terminal_run_state(self.root)
        remaining = {
            lease["id"]
            for lease in workflow._load_runtime_leases(self.root)["leases"]
            if isinstance(lease, dict)
        }
        self.assertIn("run-1111111111111111", remaining)

    def test_signal_run_process_routes_windows_signals_through_taskkill(self):
        from tools import agentctl as workflow

        # signal.SIGKILL does not exist on Windows, so the nt branch must
        # not reference it; the portable constant keeps the same value.
        self.assertEqual(
            workflow.PORTABLE_SIGKILL, getattr(signal, "SIGKILL", 9),
        )
        calls = []

        def fake_run(command, **kwargs):
            calls.append(list(command))
            return subprocess.CompletedProcess(command, 0)

        process = {"pid": 4242, "process_group": None}
        with (
            mock.patch.object(workflow.os, "name", "nt"),
            mock.patch.object(workflow.subprocess, "run", side_effect=fake_run),
        ):
            workflow._signal_run_process(process, signal.SIGTERM)
            workflow._signal_run_process(process, workflow.PORTABLE_SIGKILL)
        self.assertEqual(
            calls,
            [
                ["taskkill", "/PID", "4242", "/T"],
                ["taskkill", "/PID", "4242", "/T", "/F"],
            ],
        )

    @unittest.skipIf(os.name != "posix", "flock-based contention fixture")
    def test_supervisor_persists_terminal_state_through_lock_contention(self):
        self.start("one", "T-354", "outputs/T-354/")
        command = (
            "from pathlib import Path; import time; "
            "p=Path('outputs/T-354/out.txt'); p.parent.mkdir(parents=True, exist_ok=True); "
            "p.write_text('done', encoding='utf-8'); time.sleep(3)"
        )
        started = self.agentctl(
            "run", "start", "--output", "outputs/T-354/out.txt", "--",
            sys.executable, "-c", command, session="one",
        )
        run_id = self.lease_id(started.stdout, "run")
        shown = json.loads(
            self.agentctl("run", "show", run_id, "--json", session="one").stdout
        )["runs"][0]
        # run start now confirms the supervisor claim before returning.
        self.assertTrue(shown.get("supervisor_claimed_at"), shown)
        deadline = time.monotonic() + 30
        while True:
            if shown["status"] == "running":
                break
            if time.monotonic() >= deadline:
                self.fail(
                    f"run never became observable as running: {shown}\n"
                    f"supervisor logs:\n{self.supervisor_log_dump()}"
                )
            time.sleep(0.05)
            shown = json.loads(
                self.agentctl("run", "show", run_id, "--json", session="one").stdout
            )["runs"][0]
        # Deny the registry lock across the child's exit so the terminal
        # write and the interleaved heartbeats must survive contention
        # instead of crashing the supervisor or marking the run failed.
        self.hold_leases_lock(13.0)
        waited = self.wait_run_with_diagnostics(run_id)
        self.assertIn("succeeded", waited.stdout)

    def test_loop_lease_keeps_creator_attribution_when_queried_by_peer(self):
        self.start("creator", "T-361", "outputs/T-361/")
        self.start("peer", "T-362", "outputs/T-362/")
        script = (
            "import importlib.util, pathlib; "
            "p=pathlib.Path('tools/agentctl.py').resolve(); "
            "s=importlib.util.spec_from_file_location('agentctl_under_test', p); "
            "m=importlib.util.module_from_spec(s); s.loader.exec_module(m); "
            "token,error=m._loop_execution_claim(pathlib.Path.cwd(), 'ownership-test'); "
            "assert token and not error, error"
        )
        claimed = subprocess.run(
            [sys.executable, "-c", script],
            cwd=self.root, env=self.env("creator"), text=True,
            capture_output=True, timeout=120,
        )
        self.assertEqual(claimed.returncode, 0, claimed.stdout + claimed.stderr)
        rows = json.loads(
            self.agentctl("lease", "list", "--json", session="peer").stdout
        )["leases"]
        loop = next(row for row in rows if str(row["id"]).startswith("loop:"))
        creator = next(
            row for row in rows
            if row["kind"] == "conversation" and row["task"] == "T-361"
        )
        self.assertEqual(loop["task"], "T-361")
        self.assertEqual(loop["scope"], ["outputs/T-361/"])
        self.assertEqual(loop["holder"]["id"], creator["holder"]["id"])


if __name__ == "__main__":
    unittest.main()
