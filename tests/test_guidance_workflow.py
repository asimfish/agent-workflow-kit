"""Regression tests for supervisor guidance packets.

The tests install the kit into a fresh Git repository and verify the intended
Fable -> Codex flow: a stronger planning agent writes a durable plan packet,
Codex sees it at work start, and task completion is blocked until Codex
acknowledges that the guidance was incorporated.
"""

import importlib.util
import hashlib
import hmac
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

KIT = Path(__file__).resolve().parents[1]


class GuidanceWorkflowRegressionTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="awk-guidance-regress-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.env = os.environ.copy()
        git = subprocess.run(["git", "init", "-q"], cwd=str(self.root),
                             text=True, capture_output=True, timeout=60)
        self.assertEqual(git.returncode, 0, git.stdout + git.stderr)
        subprocess.run(
            ["git", "config", "user.email", "agent@example.com"],
            cwd=self.root, check=True, capture_output=True, text=True)
        subprocess.run(
            ["git", "config", "user.name", "Agent Test"],
            cwd=self.root, check=True, capture_output=True, text=True)
        install = subprocess.run(
            [sys.executable, str(KIT / "tools" / "agentctl.py"), "init", str(self.root)],
            cwd=str(KIT), text=True, capture_output=True, timeout=120)
        self.assertEqual(install.returncode, 0, install.stdout + install.stderr)

    def agentctl(self, *args, expect=None, cwd=None, env=None):
        proc = subprocess.run(
            [sys.executable, "tools/agentctl.py", *args],
            cwd=str(cwd or self.root), text=True, capture_output=True, timeout=120,
            env=env or self.env)
        if expect is not None:
            self.assertEqual(
                proc.returncode, expect,
                f"agentctl {' '.join(args)} rc={proc.returncode}\n{proc.stdout}\n{proc.stderr}")
        return proc

    def commit(self, message, cwd=None):
        subprocess.run(
            ["git", "add", "-A"], cwd=cwd or self.root,
            check=True, capture_output=True, text=True)
        subprocess.run(
            ["git", "commit", "--no-verify", "-q", "-m", message],
            cwd=cwd or self.root, check=True, capture_output=True, text=True)

    def assert_guidance_receipt_integrity(self, packet_id, cwd=None):
        checkout = Path(cwd or self.root)
        receipt_path = (
            checkout / ".agent" / "state" / "dispatch" / f"{packet_id}.json"
        )
        receipt_bytes = receipt_path.read_bytes()
        common_dir = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            cwd=checkout, check=True, capture_output=True, text=True,
        ).stdout.strip()
        key_bytes = (
            Path(common_dir) / "agent-workflow" / "guidance-hmac.key"
        ).read_bytes()
        receipt_payload = json.loads(receipt_bytes)
        receipt_signature = receipt_payload.pop("integrity")["signature"]
        canonical_receipt = json.dumps(
            receipt_payload, sort_keys=True, separators=(",", ":"),
            ensure_ascii=False, allow_nan=False,
        ).encode("utf-8")
        expected_signature = hmac.new(
            key_bytes, canonical_receipt, hashlib.sha256,
        ).hexdigest()
        self.assertEqual(
            receipt_signature, expected_signature,
            (common_dir, receipt_payload),
        )
        return receipt_bytes, key_bytes

    def install_fake_codex(self, exit_code=0):
        fake_bin = self.root / "fake-bin"
        fake_bin.mkdir(parents=True, exist_ok=True)
        script = fake_bin / ("codex.py" if os.name == "nt" else "codex")
        script.write_text(
            """#!/usr/bin/env python3
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

args = sys.argv[1:]
Path(os.environ["FAKE_CODEX_RECORD"]).write_text(json.dumps(args), encoding="utf-8")
prompt = sys.stdin.read() if args[-1] == "-" else args[-1]
Path(os.environ["FAKE_CODEX_STDIN_RECORD"]).write_text(prompt, encoding="utf-8")
if os.environ.get("FAKE_CODEX_CHILD_PID"):
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    Path(os.environ["FAKE_CODEX_CHILD_PID"]).write_text(str(child.pid), encoding="utf-8")
if os.environ.get("FAKE_CODEX_SLEEP"):
    time.sleep(float(os.environ["FAKE_CODEX_SLEEP"]))
packets = []
for path in Path(".agent/bus/inbox").rglob("*.json"):
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("kind") == "supervisor-guidance" and payload.get("status") == "ready":
        packets.append(payload)
packet_payload = max(
    packets,
    key=lambda payload: (payload.get("dispatch") or {}).get("started_at_ns") or 0,
)
packet = packet_payload["id"]
task = packet_payload.get("task") or packet_payload.get("to_task")
if os.environ.get("FAKE_CODEX_FINISH") == "1":
    model = args[args.index("--model") + 1] if "--model" in args else ""
    effort = ""
    if "--config" in args:
        effort = args[args.index("--config") + 1].split('"')[1]
    work_command = [
        sys.executable, "tools/agentctl.py", "work", "--agent", "codex",
        "--task", task, "--session-id", args[args.index("--output-last-message") + 2],
    ]
    if model:
        work_command.extend(["--model", model])
    if effort:
        work_command.extend(["--reasoning-effort", effort])
    started = subprocess.run(
        work_command,
        text=True,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
    )
    if started.returncode:
        print(started.stdout + started.stderr, file=sys.stderr)
        raise SystemExit(started.returncode)
if os.environ.get("FAKE_CODEX_ACK") == "1":
    acknowledged = subprocess.run(
        [sys.executable, "tools/agentctl.py", "guidance", "ack", packet, "--by", "codex"],
        text=True,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
    )
    if acknowledged.returncode:
        print(acknowledged.stdout + acknowledged.stderr, file=sys.stderr)
        raise SystemExit(acknowledged.returncode)
if os.environ.get("FAKE_CODEX_FINISH") == "1":
    finished = subprocess.run(
        [sys.executable, "tools/agentctl.py", "finish",
         "--summary", "fake Codex completed the bounded worker phase",
         "--tests", "fake worker acceptance verification"],
        text=True,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
    )
    if finished.returncode:
        print(finished.stdout + finished.stderr, file=sys.stderr)
        raise SystemExit(finished.returncode)
if "--output-last-message" in args:
    output = Path(args[args.index("--output-last-message") + 1])
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("fake Codex completed the dispatched turn", encoding="utf-8")
print("fake Codex stdout")
print("fake Codex stderr", file=sys.stderr)
raise SystemExit(int(os.environ.get("FAKE_CODEX_EXIT", "0")))
""",
            encoding="utf-8",
        )
        script.chmod(0o755)
        if os.name == "nt":
            executable = fake_bin / "codex.cmd"
            executable.write_text(
                f'@"{sys.executable}" -X utf8 "%~dp0codex.py" %*\n', encoding="utf-8")
        else:
            executable = script
        record = self.root / ".agent" / "state" / "fake-codex-args.json"
        prompt_record = self.root / ".agent" / "state" / "fake-codex-stdin.txt"
        self.env["PATH"] = str(fake_bin) + os.pathsep + self.env.get("PATH", "")
        self.env["FAKE_CODEX_RECORD"] = str(record)
        self.env["FAKE_CODEX_STDIN_RECORD"] = str(prompt_record)
        self.env["FAKE_CODEX_EXIT"] = str(exit_code)
        return record

    def start_fable_supervisor(self, task_id="SUP-001"):
        self.agentctl(
            "task", "create",
            "--id", task_id,
            "--title", "supervise worker acceptance",
            "--owner", "fable",
            "--scope", "docs/",
            expect=0)
        self.agentctl(
            "work", "--agent", "fable", "--task", task_id,
            expect=0)

    def test_fable_guidance_surfaces_to_codex_and_blocks_until_ack(self):
        self.agentctl(
            "task", "create",
            "--id", "T-101",
            "--title", "guided implementation",
            "--owner", "codex",
            "--scope", "src/",
            expect=0)

        created = self.agentctl(
            "guidance", "create",
            "--from-agent", "fable",
            "--to-agent", "codex",
            "--task", "T-101",
            "--summary", "Use a three-phase implementation plan",
            "--plan", "1. Inspect the target code.\n2. Implement narrowly.\n3. Run verification.",
            expect=0)
        match = re.search(r"guidance packet created: (\S+)", created.stdout)
        self.assertIsNotNone(match, created.stdout)
        packet_id = match.group(1)

        listed = self.agentctl("guidance", "list", "--agent", "codex", "--json", expect=0)
        guidance = json.loads(listed.stdout)["guidance"]
        self.assertEqual(len(guidance), 1, guidance)
        self.assertEqual(guidance[0]["from_agent"], "fable")
        self.assertEqual(guidance[0]["to_agent"], "codex")
        self.assertEqual(guidance[0]["task"], "T-101")

        work = self.agentctl("work", "--agent", "codex", expect=0)
        combined = work.stdout + work.stderr
        self.assertIn("Supervisor Guidance", combined)
        self.assertIn("Use a three-phase implementation plan", combined)
        self.assertIn("Required before finish", combined)

        blocked = self.agentctl(
            "finish", "--summary", "should be blocked",
            "--tests", "not run",
            expect=1)
        self.assertIn("pending supervisor guidance", blocked.stdout + blocked.stderr)

        acked = self.agentctl(
            "guidance", "ack", packet_id,
            "--by", "codex",
            "--note", "incorporated into task plan",
            expect=0)
        self.assertIn("acknowledged by codex", acked.stdout)
        self.assertEqual(
            sorted((self.root / ".agent" / "bus" / "inbox").rglob("*.json")),
            [])

        finished = self.agentctl(
            "finish", "--summary", "guided work complete",
            "--tests", "guidance regression",
            expect=0)
        self.assertIn("T-101 -> review", finished.stdout + finished.stderr)

        done_packets = sorted((self.root / ".agent" / "bus" / "done").glob("*.json"))
        self.assertTrue(done_packets)
        packet = json.loads(done_packets[0].read_text(encoding="utf-8"))
        self.assertEqual(packet["status"], "done")
        self.assertEqual(packet["acknowledged_by"], "codex")

    def test_session_scoped_guidance_only_blocks_matching_worker_session(self):
        self.agentctl(
            "task", "create",
            "--id", "T-102",
            "--title", "session routed implementation",
            "--owner", "codex",
            "--scope", ".agent/",
            expect=0)

        self.agentctl(
            "guidance", "create",
            "--from-agent", "fable",
            "--to-agent", "codex",
            "--to-model", "gpt-5.5",
            "--to-reasoning-effort", "xhigh",
            "--to-session", "xxx",
            "--task", "T-102",
            "--summary", "Plan for the target high-effort Codex session",
            "--plan", "Only session xxx should receive and ack this plan.",
            expect=0)
        self.agentctl(
            "guidance", "create",
            "--from-agent", "fable",
            "--to-agent", "codex",
            "--to-model", "gpt-5.5",
            "--to-reasoning-effort", "xhigh",
            "--to-session", "yyy",
            "--task", "T-102",
            "--summary", "Plan for a different Codex session",
            "--plan", "Session xxx must not see or be blocked by this plan.",
            expect=0)

        work = self.agentctl(
            "work", "--agent", "codex",
            "--model", "gpt-5.5",
            "--reasoning-effort", "xhigh",
            "--session-id", "xxx",
            expect=0)
        combined = work.stdout + work.stderr
        self.assertIn("Plan for the target high-effort Codex session", combined)
        self.assertIn("session=xxx", combined)
        self.assertNotIn("Plan for a different Codex session", combined)

        visible = self.agentctl(
            "guidance", "list",
            "--agent", "codex",
            "--session-id", "xxx",
            "--json",
            expect=0)
        visible_packets = json.loads(visible.stdout)["guidance"]
        self.assertEqual(len(visible_packets), 1, visible_packets)
        self.assertEqual(visible_packets[0]["to_session"], "xxx")
        packet_id = visible_packets[0]["id"]

        blocked = self.agentctl("check", "--mode", "manual", expect=1)
        self.assertIn("pending supervisor guidance", blocked.stdout + blocked.stderr)

        self.agentctl(
            "guidance", "ack", packet_id,
            "--by", "codex",
            "--note", "session xxx incorporated the supervisor plan",
            expect=0)
        self.agentctl("check", "--mode", "manual", expect=0)

        finished = self.agentctl(
            "finish",
            "--summary", "session scoped guidance complete",
            "--tests", "session routing regression",
            expect=0)
        self.assertIn("T-102 -> review", finished.stdout + finished.stderr)

        remaining = self.agentctl(
            "guidance", "list",
            "--agent", "codex",
            "--session-id", "yyy",
            "--status", "ready",
            "--json",
            expect=0)
        remaining_packets = json.loads(remaining.stdout)["guidance"]
        self.assertEqual(len(remaining_packets), 1, remaining_packets)
        self.assertEqual(remaining_packets[0]["to_session"], "yyy")

    def test_reasoning_effort_routes_guidance_without_session_id(self):
        self.agentctl(
            "task", "create", "--id", "T-104", "--title", "effort routed work",
            "--owner", "codex", "--scope", ".agent/", expect=0)
        packet_ids = {}
        for effort in ("high", "xhigh"):
            created = self.agentctl(
                "guidance", "create",
                "--from-agent", "fable", "--to-agent", "codex",
                "--to-model", "gpt-5.5", "--to-reasoning-effort", effort,
                "--task", "T-104", "--summary", f"Plan for {effort} worker",
                "--plan", f"Only the {effort} worker incorporates this packet.",
                expect=0)
            packet_ids[effort] = re.search(
                r"guidance packet created: (\S+)", created.stdout
            ).group(1)

        work = self.agentctl(
            "work", "--agent", "codex", "--model", "gpt-5.5",
            "--reasoning-effort", "high", expect=0)
        combined = work.stdout + work.stderr
        self.assertIn("Plan for high worker", combined)
        self.assertNotIn("Plan for xhigh worker", combined)
        wrong_ack = self.agentctl(
            "guidance", "ack", packet_ids["xhigh"], "--by", "codex", expect=1)
        self.assertIn("reasoning effort is high, expected xhigh", wrong_ack.stderr)
        self.agentctl(
            "guidance", "ack", packet_ids["high"], "--by", "codex", expect=0)
        self.agentctl("check", "--mode", "manual", expect=0)

    def test_effort_only_guidance_rejects_mismatched_ack(self):
        self.agentctl(
            "task", "create", "--id", "T-105", "--title", "effort-only route",
            "--owner", "codex", "--scope", ".agent/", expect=0)
        created = self.agentctl(
            "guidance", "create", "--from-agent", "fable", "--to-agent", "codex",
            "--to-reasoning-effort", "xhigh", "--task", "T-105",
            "--summary", "Effort-only supervisor route",
            "--plan", "Only an xhigh worker may acknowledge this packet.", expect=0)
        packet_id = re.search(r"guidance packet created: (\S+)", created.stdout).group(1)
        self.agentctl(
            "work", "--agent", "codex", "--reasoning-effort", "high", expect=0)
        rejected = self.agentctl(
            "guidance", "ack", packet_id, "--by", "codex", expect=1)
        self.assertIn("reasoning effort is high, expected xhigh", rejected.stderr)

    def test_resume_persists_reasoning_effort_for_all_gates(self):
        self.agentctl(
            "task", "create", "--id", "T-106", "--title", "resume route",
            "--owner", "codex", "--scope", ".agent/", expect=0)
        self.agentctl(
            "work", "--agent", "codex", "--model", "gpt-5.5", expect=0)
        created = self.agentctl(
            "guidance", "create", "--from-agent", "fable", "--to-agent", "codex",
            "--to-model", "gpt-5.5", "--to-reasoning-effort", "high",
            "--task", "T-106", "--summary", "Resume with the intended effort",
            "--plan", "Persist the route before applying any completion gate.", expect=0)
        packet_id = re.search(r"guidance packet created: (\S+)", created.stdout).group(1)
        resumed = self.agentctl(
            "work", "--agent", "codex", "--model", "gpt-5.5",
            "--reasoning-effort", "high", expect=0)
        self.assertIn("Resume with the intended effort", resumed.stdout)
        state = json.loads(self.agentctl("status", "--json", expect=0).stdout)
        self.assertEqual(state["reasoning_effort"], "high")
        self.agentctl("check", "--mode", "manual", expect=1)
        self.agentctl(
            "finish", "--summary", "must remain blocked", "--tests", "none", expect=1)
        self.agentctl("guidance", "ack", packet_id, "--by", "codex", expect=0)
        self.agentctl("check", "--mode", "manual", expect=0)

    def test_agent_profile_session_metadata_routes_guidance_by_default(self):
        self.agentctl(
            "agents", "add",
            "--id", "codex-gpt55xhigh",
            "--role", "implementation worker",
            "--backend", "codex",
            "--model", "gpt-5.5",
            "--reasoning-effort", "xhigh",
            "--session-id", "xxx",
            expect=0)
        self.agentctl(
            "task", "create",
            "--id", "T-103",
            "--title", "profile routed implementation",
            "--owner", "codex-gpt55xhigh",
            "--scope", ".agent/",
            expect=0)
        created = self.agentctl(
            "guidance", "create",
            "--from-agent", "fable",
            "--to-agent", "codex-gpt55xhigh",
            "--task", "T-103",
            "--summary", "Profile metadata should target session xxx",
            "--plan", "Use the registered worker model and session metadata.",
            expect=0)
        self.assertIn("model=gpt-5.5 reasoning=xhigh session=xxx", created.stdout)

        work = self.agentctl("work", "--agent", "codex-gpt55xhigh", expect=0)
        combined = work.stdout + work.stderr
        self.assertIn("model=gpt-5.5 reasoning=xhigh session=xxx", combined)
        self.assertIn("Profile metadata should target session xxx", combined)

    def test_dispatch_dry_run_builds_resume_command_without_starting_codex(self):
        created = self.agentctl(
            "guidance", "create",
            "--from-agent", "fable",
            "--to-agent", "codex",
            "--to-model", "gpt-5.5",
            "--to-reasoning-effort", "xhigh",
            "--to-session", "session-dry-run",
            "--task", "T-201",
            "--summary", "Dry-run the worker dispatch",
            "--plan", "Inspect, implement, verify.",
            expect=0)
        packet_id = re.search(r"guidance packet created: (\S+)", created.stdout).group(1)

        dispatched = self.agentctl(
            "guidance", "dispatch", packet_id,
            "--dry-run",
            expect=0)
        combined = dispatched.stdout + dispatched.stderr
        self.assertIn("exec resume", combined)
        self.assertIn("--model gpt-5.5", combined)
        self.assertIn("model_reasoning_effort", combined)
        self.assertIn("session-dry-run", combined)
        self.assertIn("<guidance-prompt>", combined)
        self.assertIn("no Codex process started", combined)

        packet = json.loads(
            self.agentctl("guidance", "show", packet_id, "--json", expect=0).stdout)
        self.assertNotIn("dispatch", packet)
        self.assertEqual(packet["to_reasoning_effort"], "xhigh")

    def test_create_dispatch_invokes_target_session_and_records_receipt(self):
        record = self.install_fake_codex(exit_code=0)
        created = self.agentctl(
            "guidance", "create",
            "--from-agent", "fable",
            "--to-agent", "codex",
            "--to-model", "gpt-5.5",
            "--to-reasoning-effort", "xhigh",
            "--to-session", "session-success",
            "--task", "T-202",
            "--summary", "Implement the bounded worker phase",
            "--plan", "1. Read the task.\n2. Implement.\n3. Test and finish.",
            "--dispatch",
            expect=0)
        packet_id = re.search(r"guidance packet created: (\S+)", created.stdout).group(1)
        self.assertIn("dispatched successfully", created.stdout)
        self.assertIn("fake Codex completed the dispatched turn", created.stdout)

        args = json.loads(record.read_text(encoding="utf-8"))
        self.assertEqual(args[:2], ["exec", "resume"])
        self.assertEqual(args[args.index("--model") + 1], "gpt-5.5")
        self.assertEqual(
            args[args.index("--config") + 1],
            'model_reasoning_effort="xhigh"')
        self.assertEqual(args[-2], "session-success")
        self.assertEqual(args[-1], "-")
        prompt = record.with_name("fake-codex-stdin.txt").read_text(encoding="utf-8")
        self.assertIn(packet_id, prompt)
        self.assertIn("T-202", prompt)
        self.assertIn("Implement the bounded worker phase", prompt)
        self.assertIn("Do not steal another live task lock", prompt)

        packet = json.loads(
            self.agentctl("guidance", "show", packet_id, "--json", expect=0).stdout)
        self.assertEqual(packet["status"], "ready")
        self.assertEqual(packet["dispatch"]["status"], "succeeded")
        self.assertEqual(packet["dispatch"]["attempts"], 1)
        self.assertEqual(packet["dispatch"]["exit_code"], 0)
        self.assertEqual(packet["dispatch"]["reasoning_effort"], "xhigh")
        receipt = self.root / ".agent" / "state" / "dispatch" / f"{packet_id}.json"
        self.assertTrue(receipt.is_file())
        self.assertEqual(json.loads(receipt.read_text(encoding="utf-8"))["status"], "succeeded")
        self.assertTrue(sorted((self.root / ".agent" / "bus" / "inbox").rglob("*.json")))

    def test_session_bound_ack_rejects_claimed_identity_without_matching_work_session(self):
        created = self.agentctl(
            "guidance", "create",
            "--from-agent", "fable",
            "--to-agent", "codex",
            "--to-model", "gpt-5.5",
            "--to-session", "session-identity",
            "--task", "T-208",
            "--summary", "Require a real matching local worker session",
            "--plan", "Do not trust an unbound --by string.",
            expect=0)
        packet_id = re.search(r"guidance packet created: (\S+)", created.stdout).group(1)
        rejected = self.agentctl(
            "guidance", "ack", packet_id,
            "--by", "codex",
            expect=1)
        self.assertIn("session-bound guidance acknowledgement rejected", rejected.stderr)

    def test_failed_dispatch_keeps_guidance_ready_and_can_retry(self):
        self.install_fake_codex(exit_code=9)
        failed = self.agentctl(
            "guidance", "create",
            "--from-agent", "fable",
            "--to-agent", "codex",
            "--to-session", "session-retry",
            "--task", "T-203",
            "--summary", "Retry a failed dispatch",
            "--plan", "Run the worker phase once transport is healthy.",
            "--dispatch",
            expect=1)
        packet_id = re.search(r"guidance packet created: (\S+)", failed.stdout).group(1)
        packet = json.loads(
            self.agentctl("guidance", "show", packet_id, "--json", expect=0).stdout)
        self.assertEqual(packet["status"], "ready")
        self.assertEqual(packet["dispatch"]["status"], "failed")
        self.assertEqual(packet["dispatch"]["exit_code"], 9)
        self.assertEqual(packet["dispatch"]["attempts"], 1)

        self.env["FAKE_CODEX_EXIT"] = "0"
        retried = self.agentctl("guidance", "dispatch", packet_id, expect=0)
        self.assertIn("dispatched successfully", retried.stdout)
        packet = json.loads(
            self.agentctl("guidance", "show", packet_id, "--json", expect=0).stdout)
        self.assertEqual(packet["status"], "ready")
        self.assertEqual(packet["dispatch"]["status"], "succeeded")
        self.assertEqual(packet["dispatch"]["attempts"], 2)

    def test_dispatch_timeout_records_bounded_failure(self):
        self.install_fake_codex(exit_code=0)
        self.env["FAKE_CODEX_SLEEP"] = "2"
        failed = self.agentctl(
            "guidance", "create",
            "--from-agent", "fable",
            "--to-agent", "codex",
            "--to-session", "session-timeout",
            "--task", "T-204",
            "--summary", "Bound a slow worker turn",
            "--plan", "Stop and preserve a retryable packet if transport times out.",
            "--dispatch",
            "--timeout", "1",
            expect=1)
        packet_id = re.search(r"guidance packet created: (\S+)", failed.stdout).group(1)
        packet = json.loads(
            self.agentctl("guidance", "show", packet_id, "--json", expect=0).stdout)
        self.assertEqual(packet["status"], "ready")
        self.assertEqual(packet["dispatch"]["status"], "failed")
        self.assertEqual(packet["dispatch"]["exit_code"], 124)
        self.assertIn("timed out after 1s", packet["dispatch"]["failure"])

    def test_dispatch_timeout_terminates_descendant_process_tree(self):
        self.install_fake_codex(exit_code=0)
        child_pid_path = self.root / ".agent" / "state" / "fake-child.pid"
        self.env["FAKE_CODEX_CHILD_PID"] = str(child_pid_path)
        self.env["FAKE_CODEX_SLEEP"] = "10"
        failed = self.agentctl(
            "guidance", "create",
            "--from-agent", "fable", "--to-agent", "codex",
            "--to-session", "session-timeout-tree", "--task", "T-212",
            "--summary", "Bound the complete dispatch process tree",
            "--plan", "Terminate descendants before recording timeout failure.",
            "--dispatch", "--timeout", "1", expect=1)
        self.assertIn("timed out after 1s", failed.stdout + failed.stderr)
        child_pid = int(child_pid_path.read_text(encoding="utf-8"))
        if os.name == "posix":
            state = subprocess.run(
                ["ps", "-o", "stat=", "-p", str(child_pid)],
                text=True, capture_output=True, timeout=10,
            ).stdout.strip()
            self.assertTrue(not state or state.startswith("Z"), state)
        else:
            for _ in range(50):
                listing = subprocess.run(
                    ["tasklist", "/FI", f"PID eq {child_pid}", "/FO", "CSV", "/NH"],
                    text=True, capture_output=True, timeout=10,
                ).stdout
                if str(child_pid) not in listing:
                    break
                time.sleep(0.1)
            self.assertNotIn(str(child_pid), listing)

    def test_windows_timeout_cleanup_attempts_tree_kill_after_leader_exit(self):
        spec = importlib.util.spec_from_file_location(
            "agentctl_windows_guidance_cleanup", self.root / "tools" / "agentctl.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        proc = mock.Mock(pid=4242)
        proc.poll.return_value = 0
        with mock.patch.object(module.os, "name", "nt"), \
                mock.patch.object(module, "_close_windows_job", return_value=False), \
                mock.patch.object(module.subprocess, "run") as taskkill:
            module._terminate_loop_process(proc)
        taskkill.assert_called_once()
        self.assertIn("/T", taskkill.call_args.args[0])

    def test_signing_keys_use_binary_mode_and_preserve_exact_bytes(self):
        spec = importlib.util.spec_from_file_location(
            "agentctl_binary_signing_keys", self.root / "tools" / "agentctl.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        real_open = os.open
        synthetic_binary_flag = 1 << 29
        opened_flags = []
        secrets_to_write = [b"\n" * 32, (b"\r\n\x1a\x00" * 8)[:32]]
        common_dir = self.root / ".git"

        def binary_open(path, flags, mode):
            opened_flags.append(flags)
            return real_open(path, flags & ~synthetic_binary_flag, mode)

        with mock.patch.object(
                module.os, "O_BINARY", synthetic_binary_flag, create=True), \
                mock.patch.object(module.os, "open", side_effect=binary_open), \
                mock.patch.object(module, "_git_common_dir", return_value=common_dir), \
                mock.patch.object(
                    module.secrets, "token_bytes", side_effect=secrets_to_write):
            guidance_key = module._guidance_signing_key(self.root, create=True)
            eval_key = module._eval_signing_key(self.root, create=True)

        self.assertEqual(guidance_key, secrets_to_write[0])
        self.assertEqual(eval_key, secrets_to_write[1])
        self.assertEqual(len(opened_flags), 2)
        self.assertTrue(all(flags & synthetic_binary_flag for flags in opened_flags))
        key_dir = common_dir / "agent-workflow"
        self.assertEqual(
            (key_dir / "guidance-hmac.key").read_bytes(), secrets_to_write[0])
        self.assertEqual(
            (key_dir / "eval-hmac.key").read_bytes(), secrets_to_write[1])

    def test_dispatch_refuses_source_session_recursion(self):
        created = self.agentctl(
            "guidance", "create",
            "--from-agent", "fable",
            "--from-session", "same-session",
            "--to-agent", "codex",
            "--to-session", "same-session",
            "--task", "T-205",
            "--summary", "Do not recurse into the supervisor session",
            "--plan", "This packet must remain file-only.",
            expect=0)
        packet_id = re.search(r"guidance packet created: (\S+)", created.stdout).group(1)
        blocked = self.agentctl(
            "guidance", "dispatch", packet_id,
            "--dry-run",
            expect=1)
        self.assertIn("source session", blocked.stdout + blocked.stderr)

    def test_supervisor_verify_requires_ack_and_completed_task_evidence(self):
        self.install_fake_codex(exit_code=0)
        self.agentctl(
            "task", "create",
            "--id", "T-206",
            "--title", "verify incomplete dispatch",
            "--owner", "codex",
            "--scope", ".agent/",
            expect=0)
        created = self.agentctl(
            "guidance", "create",
            "--from-agent", "fable",
            "--to-agent", "codex",
            "--to-model", "gpt-5.5",
            "--to-reasoning-effort", "xhigh",
            "--to-session", "session-incomplete",
            "--task", "T-206",
            "--summary", "Do not accept transport success as task completion",
            "--plan", "Read, implement, verify, acknowledge, and finish.",
            "--dispatch",
            expect=0)
        packet_id = re.search(r"guidance packet created: (\S+)", created.stdout).group(1)

        self.start_fable_supervisor()
        rejected = self.agentctl(
            "guidance", "verify", packet_id,
            "--by", "fable",
            "--json",
            expect=1)
        record = json.loads(rejected.stdout)
        self.assertFalse(record["accepted"])
        self.assertIn("worker has not acknowledged the guidance", record["problems"])
        self.assertTrue(any("expected review/approved/done" in p for p in record["problems"]))
        self.assertEqual(record["integrity"]["algorithm"], "hmac-sha256")

    def test_supervisor_verify_accepts_complete_turn_and_rejects_tampered_receipt(self):
        self.install_fake_codex(exit_code=0)
        self.env["FAKE_CODEX_ACK"] = "1"
        self.env["FAKE_CODEX_FINISH"] = "1"
        self.agentctl(
            "task", "create",
            "--id", "T-207",
            "--title", "verify complete dispatch",
            "--owner", "codex",
            "--scope", ".agent/",
            expect=0)
        created = self.agentctl(
            "guidance", "create",
            "--from-agent", "fable",
            "--to-agent", "codex",
            "--to-model", "gpt-5.5",
            "--to-reasoning-effort", "xhigh",
            "--to-session", "session-accepted",
            "--task", "T-207",
            "--summary", "Complete one evidence-checked bounded worker turn",
            "--plan", "Read, implement, verify, acknowledge, and finish.",
            "--dispatch",
            expect=0)
        packet_id = re.search(r"guidance packet created: (\S+)", created.stdout).group(1)
        initial_receipt, initial_key = self.assert_guidance_receipt_integrity(packet_id)

        self_review = self.agentctl(
            "guidance", "verify", packet_id,
            "--by", "fable",
            "--json",
            expect=1)
        self_review_record = json.loads(self_review.stdout)
        self.assertTrue(any("active reviewer session" in p for p in self_review_record["problems"]))
        self.assertEqual(
            self.assert_guidance_receipt_integrity(packet_id),
            (initial_receipt, initial_key),
        )

        self.start_fable_supervisor()
        self.assertEqual(
            self.assert_guidance_receipt_integrity(packet_id),
            (initial_receipt, initial_key),
        )
        self.commit("chore(agent): record completed guided turn\n\nRefs: T-207")
        self.assertEqual(
            self.assert_guidance_receipt_integrity(packet_id),
            (initial_receipt, initial_key),
        )
        accepted = self.agentctl(
            "guidance", "verify", packet_id,
            "--by", "fable",
            "--json",
            expect=0)
        record = json.loads(accepted.stdout)
        self.assertTrue(record["accepted"])
        self.assertTrue(all(record["checks"].values()), record)
        self.assertEqual(record["problems"], [])
        common_acceptance = list(
            (self.root / ".git" / "agent-workflow" / "acceptance").glob("*.json")
        )
        self.assertTrue(common_acceptance)
        self.assertFalse(
            (self.root / ".agent" / "state" / "dispatch" / "acceptance").exists()
        )

        task_path = self.root / ".agent" / "tasks" / "T-207.md"
        original_task = task_path.read_text(encoding="utf-8")
        no_tests = re.sub(
            r"^- Tests:.*$",
            "- Tests: not run: test environment unavailable",
            original_task,
            flags=re.M,
        )
        task_path.write_text(no_tests, encoding="utf-8")
        self.commit("test(guidance): record missing verification case\n\nRefs: T-207")
        missing_tests = self.agentctl(
            "guidance", "verify", packet_id, "--by", "fable", "--json", expect=1)
        self.assertIn(
            "task verification evidence is missing",
            json.loads(missing_tests.stdout)["problems"],
        )
        task_path.write_text(original_task, encoding="utf-8")
        self.commit("test(guidance): restore verification evidence\n\nRefs: T-207")

        packet_path = next(
            (self.root / ".agent" / "bus" / "done").rglob(f"{packet_id}.json")
        )
        original_packet = packet_path.read_text(encoding="utf-8")
        changed_packet = json.loads(original_packet)
        changed_packet["task"] = "T-208"
        changed_packet["acknowledged_task"] = "T-208"
        packet_path.write_text(json.dumps(changed_packet), encoding="utf-8")
        wrong_task = self.agentctl(
            "guidance", "verify", packet_id,
            "--by", "fable",
            "--json",
            expect=1)
        wrong_task_record = json.loads(wrong_task.stdout)
        self.assertIn(
            "worker guidance packet differs from the signed dispatch contract",
            wrong_task_record["problems"],
        )
        packet_path.write_text(original_packet, encoding="utf-8")

        receipt_path = self.root / ".agent" / "state" / "dispatch" / f"{packet_id}.json"
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        receipt["status"] = "failed"
        receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
        tampered = self.agentctl(
            "guidance", "verify", packet_id,
            "--by", "fable",
            "--json",
            expect=1)
        tampered_record = json.loads(tampered.stdout)
        self.assertFalse(tampered_record["accepted"])
        self.assertTrue(any("integrity verification" in p for p in tampered_record["problems"]))

    def test_supervisor_verify_records_rejection_for_malformed_receipt_integrity(self):
        self.install_fake_codex(exit_code=0)
        self.agentctl(
            "task", "create", "--id", "T-211", "--title", "reject malformed receipt",
            "--owner", "codex", "--scope", ".agent/", expect=0)
        created = self.agentctl(
            "guidance", "create",
            "--from-agent", "fable", "--to-agent", "codex",
            "--to-model", "gpt-5.5", "--to-session", "session-malformed",
            "--task", "T-211", "--summary", "Reject malformed signed evidence",
            "--plan", "Fail closed and preserve the rejection record.",
            "--dispatch", expect=0)
        packet_id = re.search(r"guidance packet created: (\S+)", created.stdout).group(1)
        receipt_path = self.root / ".agent" / "state" / "dispatch" / f"{packet_id}.json"
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        original_receipt = json.dumps(receipt)
        receipt["integrity"] = "malformed"
        receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
        self.start_fable_supervisor()

        rejected = self.agentctl(
            "guidance", "verify", packet_id, "--by", "fable", "--json", expect=1)
        record = json.loads(rejected.stdout)
        self.assertFalse(record["accepted"])
        self.assertTrue(any("receipt is invalid" in p for p in record["problems"]))
        self.assertTrue(list(
            (self.root / ".git" / "agent-workflow" / "acceptance").glob("*.json")
        ))

        receipt = json.loads(original_receipt)
        receipt["integrity"]["signature"] = "é"
        receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
        non_ascii = self.agentctl(
            "guidance", "verify", packet_id, "--by", "fable", "--json", expect=1)
        non_ascii_record = json.loads(non_ascii.stdout)
        self.assertFalse(non_ascii_record["accepted"])
        self.assertTrue(any("receipt is invalid" in p for p in non_ascii_record["problems"]))

    def test_supervisor_verifies_worker_worktree_and_acceptance_survives_release(self):
        self.install_fake_codex(exit_code=0)
        self.env["FAKE_CODEX_ACK"] = "1"
        self.env["FAKE_CODEX_FINISH"] = "1"
        self.start_fable_supervisor()
        self.agentctl(
            "task", "create",
            "--id", "T-210",
            "--title", "verify managed worker target",
            "--owner", "codex",
            "--scope", "src/",
            expect=0)
        created = self.agentctl(
            "guidance", "create",
            "--from-agent", "fable",
            "--to-agent", "codex",
            "--to-model", "gpt-5.5",
            "--to-reasoning-effort", "xhigh",
            "--to-session", "session-worktree",
            "--task", "T-210",
            "--summary", "Complete one turn in an isolated worker worktree",
            "--plan", "Acknowledge, finish, and preserve supervisor acceptance.",
            expect=0)
        packet_id = re.search(r"guidance packet created: (\S+)", created.stdout).group(1)
        self.commit("chore(agent): prepare managed guidance test\n\nRefs: T-210")

        worker = self.root.parent / "worker"
        self.addCleanup(shutil.rmtree, worker, ignore_errors=True)
        leased = self.agentctl(
            "worktree", "create",
            "--task", "T-210",
            "--agent", "codex",
            "--path", str(worker),
            expect=0)
        lease_id = re.search(r"worktree lease (\S+) active", leased.stdout).group(1)
        self.agentctl(
            "guidance", "dispatch", packet_id,
            expect=0, cwd=worker)

        receipt_path = (
            worker / ".agent" / "state" / "dispatch" / f"{packet_id}.json"
        )
        receipt_before_commit = receipt_path.read_bytes()
        common_dir = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            cwd=self.root, check=True, capture_output=True, text=True,
        ).stdout.strip()
        signing_key = Path(common_dir) / "agent-workflow" / "guidance-hmac.key"
        key_before_commit = signing_key.read_bytes()
        worker_common_dir = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            cwd=worker, check=True, capture_output=True, text=True,
        ).stdout.strip()
        worker_signing_key = (
            Path(worker_common_dir) / "agent-workflow" / "guidance-hmac.key"
        )
        self.assertEqual(
            worker_signing_key.read_bytes(), key_before_commit,
            (common_dir, worker_common_dir),
        )
        receipt_payload = json.loads(receipt_before_commit)
        receipt_signature = receipt_payload.pop("integrity")["signature"]
        canonical_receipt = json.dumps(
            receipt_payload, sort_keys=True, separators=(",", ":"),
            ensure_ascii=False, allow_nan=False,
        ).encode("utf-8")
        expected_signature = hmac.new(
            key_before_commit, canonical_receipt, hashlib.sha256,
        ).hexdigest()
        self.assertEqual(
            receipt_signature, expected_signature,
            (common_dir, worker_common_dir, receipt_payload),
        )

        pre_commit = self.agentctl(
            "guidance", "verify", packet_id,
            "--by", "fable",
            "--target", str(worker),
            "--json",
            expect=1)
        pre_commit_record = json.loads(pre_commit.stdout)
        self.assertTrue(
            pre_commit_record["checks"]["signed_receipt"], pre_commit_record)

        self.commit(
            "feat(worker): complete managed guidance test\n\nRefs: T-210",
            cwd=worker,
        )
        self.assertEqual(receipt_path.read_bytes(), receipt_before_commit)
        self.assertEqual(signing_key.read_bytes(), key_before_commit)

        accepted = self.agentctl(
            "guidance", "verify", packet_id,
            "--by", "fable",
            "--target", str(worker),
            "--json",
            expect=0)
        record = json.loads(accepted.stdout)
        self.assertTrue(record["accepted"])
        self.assertTrue(all(record["checks"].values()), record)
        worker_head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=worker,
            check=True, capture_output=True, text=True).stdout.strip()
        self.assertEqual(record["target_head"], worker_head)
        self.assertTrue(record["evidence"]["contract_sha256"])
        self.assertTrue(record["evidence"]["task_document_sha256"])
        common_acceptance = list(
            (self.root / ".git" / "agent-workflow" / "acceptance").glob("*.json")
        )
        self.assertTrue(common_acceptance)

        self.agentctl("worktree", "release", lease_id, expect=0)
        self.assertFalse(worker.exists())
        self.assertTrue(all(path.is_file() for path in common_acceptance))

    def test_supervisor_verify_rejects_completion_from_before_dispatch(self):
        self.install_fake_codex(exit_code=0)
        self.agentctl(
            "task", "create",
            "--id", "T-209",
            "--title", "reject stale completion evidence",
            "--owner", "codex",
            "--scope", ".agent/",
            expect=0)
        self.agentctl(
            "work", "--agent", "codex", "--task", "T-209",
            "--session-id", "session-stale",
            "--model", "gpt-5.5",
            "--reasoning-effort", "xhigh",
            expect=0)
        self.agentctl(
            "finish",
            "--summary", "completion created before guidance",
            "--tests", "old verification",
            expect=0)
        self.env["FAKE_CODEX_ACK"] = "1"
        created = self.agentctl(
            "guidance", "create",
            "--from-agent", "fable",
            "--to-agent", "codex",
            "--to-model", "gpt-5.5",
            "--to-reasoning-effort", "xhigh",
            "--to-session", "session-stale",
            "--task", "T-209",
            "--summary", "Require new work after the old completion",
            "--plan", "Acknowledge only; stale completion must not satisfy this turn.",
            "--dispatch",
            expect=0)
        packet_id = re.search(r"guidance packet created: (\S+)", created.stdout).group(1)

        self.start_fable_supervisor()
        rejected = self.agentctl(
            "guidance", "verify", packet_id,
            "--by", "fable",
            "--json",
            expect=1)
        record = json.loads(rejected.stdout)
        self.assertFalse(record["accepted"])
        self.assertIn("task completion predates this dispatch attempt", record["problems"])


if __name__ == "__main__":
    unittest.main()
