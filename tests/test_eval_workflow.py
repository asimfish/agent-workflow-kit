"""Fresh-install regression tests for deterministic harness evaluation."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


KIT = Path(__file__).resolve().parents[1]


class HarnessEvaluationRegressionTest(unittest.TestCase):
    def setUp(self):
        self.temp = Path(tempfile.mkdtemp(prefix="awk-eval-regress-"))
        self.addCleanup(shutil.rmtree, self.temp, ignore_errors=True)
        self.root = self.temp / "policy"
        self.root.mkdir()
        self.git("init", "-q")
        self.git("config", "user.email", "agent@example.com")
        self.git("config", "user.name", "Agent Test")
        installed = subprocess.run(
            [sys.executable, str(KIT / "tools" / "agentctl.py"), "init", str(self.root)],
            cwd=str(KIT), text=True, capture_output=True, timeout=120,
        )
        self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
        self.write_fixture()
        self.commit("chore(agent): install eval fixture\n\nRefs: T-025")

    def git(self, *args, cwd=None, expect=0):
        proc = subprocess.run(
            ["git", *args], cwd=str(cwd or self.root), text=True,
            capture_output=True, timeout=120,
        )
        self.assertEqual(proc.returncode, expect, proc.stdout + proc.stderr)
        return proc.stdout.strip()

    def commit(self, message, cwd=None):
        self.git("add", "-A", cwd=cwd)
        self.git("commit", "--no-verify", "-q", "-m", message, cwd=cwd)

    def agentctl(self, *args, expect=None, env=None):
        proc = subprocess.run(
            [sys.executable, "tools/agentctl.py", *args], cwd=str(self.root),
            text=True, capture_output=True, timeout=120, env=env,
        )
        if expect is not None:
            self.assertEqual(
                proc.returncode, expect,
                f"agentctl {' '.join(args)} rc={proc.returncode}\n{proc.stdout}\n{proc.stderr}",
            )
        return proc

    def write_fixture(self):
        probe = self.root / "verify_case.py"
        probe.write_text(
            "import json, os, pathlib, sys\n"
            "state = json.loads(pathlib.Path('eval_state.json').read_text())\n"
            "case = sys.argv[1]\n"
            "if case == 'environment':\n"
            "    print('x' * 5000)\n"
            "    ok = 'OPENAI_API_KEY' not in os.environ and os.environ.get('AGENT_EVAL') == '1'\n"
            "else:\n"
            "    ok = bool(state.get(case))\n"
            "raise SystemExit(0 if ok else 9)\n",
            encoding="utf-8",
        )
        (self.root / "eval_state.json").write_text(
            json.dumps({"held_in": True, "held_out": True}) + "\n", encoding="utf-8",
        )
        catalog = {
            "version": 1,
            "suites": {
                "research-quality": {
                    "description": "Verifier-grounded split regression",
                    "cases": [
                        {
                            "id": "known-failure-regression",
                            "split": "held_in",
                            "required": True,
                            "argv": [sys.executable, "verify_case.py", "held_in"],
                            "timeout_seconds": 30,
                            "expected_exit_codes": [0],
                        },
                        {
                            "id": "unseen-behavior-regression",
                            "split": "held_out",
                            "required": True,
                            "argv": [sys.executable, "verify_case.py", "held_out"],
                            "timeout_seconds": 30,
                            "expected_exit_codes": [0],
                        },
                        {
                            "id": "scrubbed-eval-environment",
                            "split": "held_out",
                            "required": True,
                            "argv": [sys.executable, "verify_case.py", "environment"],
                            "timeout_seconds": 30,
                            "expected_exit_codes": [0],
                        },
                    ],
                }
            },
        }
        (self.root / ".agent" / "evals" / "suites.json").write_text(
            json.dumps(catalog, indent=2) + "\n", encoding="utf-8",
        )

    def clone_target(self, name):
        target = self.temp / name
        self.git("clone", "-q", str(self.root), str(target))
        self.git("config", "user.email", "agent@example.com", cwd=target)
        self.git("config", "user.name", "Agent Test", cwd=target)
        return target

    def set_target_state(self, target, *, held_in=True, held_out=True, marker=None):
        (target / "eval_state.json").write_text(
            json.dumps({"held_in": held_in, "held_out": held_out}) + "\n",
            encoding="utf-8",
        )
        if marker:
            (target / "candidate.txt").write_text(marker + "\n", encoding="utf-8")
        self.commit("test(eval): prepare candidate\n\nRefs: T-025", cwd=target)

    def run_eval(self, target, expect):
        env = os.environ.copy()
        env["OPENAI_API_KEY"] = "must-not-reach-verifier"
        proc = self.agentctl(
            "eval", "run", "research-quality", "--target", str(target), "--json",
            expect=expect, env=env,
        )
        return json.loads(proc.stdout)

    def test_split_non_regression_gate_accepts_and_rejects_candidates(self):
        listed = json.loads(self.agentctl("eval", "list", "--json", expect=0).stdout)
        self.assertEqual(listed["suites"][0]["id"], "research-quality")

        baseline_target = self.clone_target("baseline")
        bad_target = self.clone_target("bad-candidate")
        good_target = self.clone_target("good-candidate")
        self.set_target_state(bad_target, held_out=False)
        self.set_target_state(good_target, marker="bounded harness change")

        baseline = self.run_eval(baseline_target, expect=0)
        bad = self.run_eval(bad_target, expect=1)
        good = self.run_eval(good_target, expect=0)

        self.assertFalse(baseline["policy_dirty"])
        self.assertFalse(good["policy_dirty"])
        self.assertFalse(good["target_dirty"])
        self.assertEqual(good["target_commit"], good["target_commit_after"])
        environment_case = next(case for case in good["cases"] if case["id"] == "scrubbed-eval-environment")
        self.assertTrue(environment_case["passed"])
        self.assertLess(len(environment_case["stdout"]), 4100)

        rejected = self.agentctl(
            "eval", "compare", "--baseline", baseline["id"],
            "--candidate", bad["id"], "--json", expect=1,
        )
        rejection = json.loads(rejected.stdout)
        self.assertFalse(rejection["accepted"])
        self.assertTrue(any("held_out regressed" in reason for reason in rejection["reasons"]))

        accepted = self.agentctl(
            "eval", "compare", "--baseline", baseline["id"],
            "--candidate", good["id"], "--json", expect=0,
        )
        self.assertTrue(json.loads(accepted.stdout)["accepted"])

        bad_gate = json.loads(self.agentctl(
            "eval", "gate", "--baseline", baseline["id"], "--candidate", bad["id"],
            "--by", "independent-verifier", "--json", expect=1,
        ).stdout)
        good_gate = json.loads(self.agentctl(
            "eval", "gate", "--baseline", baseline["id"], "--candidate", good["id"],
            "--by", "independent-verifier", "--json", expect=0,
        ).stdout)
        self.assertFalse(bad_gate["accepted"])
        self.assertTrue(good_gate["accepted"])
        for decision in (bad_gate, good_gate):
            path = self.root / ".agent" / "state" / "evals" / "decisions" / f"{decision['id']}.json"
            self.assertTrue(path.is_file())

    def test_gate_rejects_dirty_targets_changed_suites_and_inconsistent_reports(self):
        baseline_target = self.clone_target("baseline")
        candidate_target = self.clone_target("candidate")
        mutating_target = self.clone_target("mutating-candidate")
        forged_target = self.clone_target("forged-candidate")
        baseline = self.run_eval(baseline_target, expect=0)

        (candidate_target / "uncommitted.txt").write_text("dirty\n", encoding="utf-8")
        dirty = self.run_eval(candidate_target, expect=0)
        comparison = json.loads(self.agentctl(
            "eval", "compare", "--baseline", baseline["id"],
            "--candidate", dirty["id"], "--json", expect=1,
        ).stdout)
        self.assertIn("candidate target checkout was dirty", comparison["reasons"])

        probe_path = mutating_target / "verify_case.py"
        probe_text = probe_path.read_text(encoding="utf-8")
        probe_path.write_text(
            probe_text.replace(
                "case = sys.argv[1]\n",
                "case = sys.argv[1]\n"
                "if case == 'environment': pathlib.Path('side-effect.txt').write_text('changed')\n",
            ),
            encoding="utf-8",
        )
        self.commit("test(eval): add verifier side effect\n\nRefs: T-025", cwd=mutating_target)
        mutating = self.run_eval(mutating_target, expect=0)
        self.assertTrue(mutating["target_dirty_after"])
        mutation_decision = json.loads(self.agentctl(
            "eval", "compare", "--baseline", baseline["id"],
            "--candidate", mutating["id"], "--json", expect=1,
        ).stdout)
        self.assertIn(
            "candidate target checkout became dirty during evaluation",
            mutation_decision["reasons"],
        )

        report_path = self.root / ".agent" / "state" / "evals" / "runs" / f"{dirty['id']}.json"
        report = json.loads(report_path.read_text(encoding="utf-8"))
        report["metrics"]["held_out"]["score"] = 0.0
        report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        inconsistent = self.agentctl(
            "eval", "compare", "--baseline", baseline["id"],
            "--candidate", dirty["id"], expect=2,
        )
        self.assertIn("integrity verification", inconsistent.stdout + inconsistent.stderr)

        report_path.write_text(json.dumps(dirty, indent=2) + "\n", encoding="utf-8")
        self.set_target_state(forged_target, held_out=False)
        forged = self.run_eval(forged_target, expect=1)
        forged_path = self.root / ".agent" / "state" / "evals" / "runs" / f"{forged['id']}.json"
        forged_report = json.loads(forged_path.read_text(encoding="utf-8"))
        for case in forged_report["cases"]:
            case["passed"] = True
            case["exit_code"] = 0
        forged_report["metrics"] = {
            split: {"total": total, "passed": total, "score": 1.0, "required_failures": []}
            for split, total in (("held_in", 1), ("held_out", 2), ("overall", 3))
        }
        forged_report["status"] = "passed"
        forged_path.write_text(json.dumps(forged_report, indent=2) + "\n", encoding="utf-8")
        forged_compare = self.agentctl(
            "eval", "compare", "--baseline", baseline["id"],
            "--candidate", forged["id"], expect=2,
        )
        self.assertIn("integrity verification", forged_compare.stdout + forged_compare.stderr)

        suite_path = self.root / ".agent" / "evals" / "suites.json"
        catalog = json.loads(suite_path.read_text(encoding="utf-8"))
        catalog["suites"]["research-quality"]["description"] = "changed after execution"
        suite_path.write_text(json.dumps(catalog, indent=2) + "\n", encoding="utf-8")
        mismatched = self.agentctl(
            "eval", "compare", "--baseline", baseline["id"],
            "--candidate", dirty["id"], expect=2,
        )
        self.assertIn("suite hash changed", mismatched.stdout + mismatched.stderr)


if __name__ == "__main__":
    unittest.main()
