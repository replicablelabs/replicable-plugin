"""Tests for the benchmark runner (bench/): parsing, attribution, labels, statistics, and a full run with a fake claude."""

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
import unittest.mock
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[1]
BENCH = PLUGIN / "bench"
FAKE_CLAUDE = Path(__file__).resolve().parent / "fixtures" / "bench" / "fake_claude.py"
sys.path.insert(0, str(BENCH))

import metrics  # noqa: E402
import report  # noqa: E402
import suite as suite_mod  # noqa: E402
import transcript  # noqa: E402
from common import project_slug  # noqa: E402

GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "-c", "commit.gpgsign=false"]


def write(root: Path, files: dict) -> None:
    for rel, body in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(body).lstrip() if isinstance(body, str) else json.dumps(body, indent=1))


def jsonl(path: Path, entries) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(e) + "\n" for e in entries))
    return path


def iso(s: float) -> str:
    return f"2026-01-01T00:00:{s:06.3f}Z"


def usage(inp, cr, cw, out):
    return {"input_tokens": inp, "cache_read_input_tokens": cr, "cache_creation_input_tokens": cw, "output_tokens": out}


SESSION = [
    {"type": "user", "timestamp": iso(0), "message": {"content": "<command-name>/replicable:epcc</command-name> do it"}},
    {"type": "api-request", "timestamp": iso(0.5)},
    {"type": "assistant", "timestamp": iso(1), "message": {"id": "m1", "model": "big", "usage": usage(10, 0, 1000, 100),
     "content": [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "FOO=1 graphify affected x.py"}},
                 {"type": "tool_use", "id": "t2", "name": "Edit", "input": {"file_path": "/r/a.py"}}]}},
    {"type": "attachment", "timestamp": iso(2), "attachment": {"type": "hook_success", "hookName": "PostToolUse:Edit",
     "hookEvent": "PostToolUse", "toolUseID": "t2", "durationMs": "500", "exitCode": "0"}},
    {"type": "attachment", "timestamp": iso(2), "attachment": {"type": "hook_additional_context", "hookName": "PostToolUse:Edit",
     "toolUseID": "t2", "content": ["c" * 100]}},
    {"type": "user", "timestamp": iso(1.5), "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": "a" * 300}]}},
    {"type": "user", "timestamp": iso(2), "message": {"content": [{"type": "tool_result", "tool_use_id": "t2",
                                                                    "content": [{"type": "text", "text": "b" * 100}]}]}},
    {"type": "api-request", "timestamp": iso(3)},
    # context grows from 1010 to 1610 with 100 output: 500 tokens of results + hook context arrived
    {"type": "assistant", "timestamp": iso(4), "message": {"id": "m2", "model": "big", "usage": usage(10, 1000, 600, 50),
     "content": [{"type": "tool_use", "id": "t3", "name": "Skill", "input": {"skill": "replicable:tdd"}}]}},
    {"type": "user", "timestamp": iso(4.2), "message": {"content": [{"type": "tool_result", "tool_use_id": "t3", "content": "s"}]}},
    {"type": "api-request", "timestamp": iso(5)},
    {"type": "assistant", "timestamp": iso(6), "message": {"id": "m3", "model": "big", "usage": usage(10, 1600, 10, 20),
     "content": [{"type": "text", "text": "done"}]}},
    {"type": "assistant", "timestamp": iso(6.5), "message": {"id": "m3", "model": "big", "usage": usage(10, 1600, 10, 30),
     "content": [{"type": "text", "text": "more of the same message"}]}},
    {"type": "system", "timestamp": iso(7), "subtype": "stop_hook_summary",
     "hookInfos": [{"command": "python3 /p/hooks/test_gate.py", "durationMs": 40}], "preventedContinuation": True},
    "not json at all",
]


class TranscriptTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        path = self.dir / "session.jsonl"
        jsonl(path, [e for e in SESSION if isinstance(e, dict)])
        with path.open("a") as fh:
            fh.write("not json at all\n")
        jsonl(self.dir / "subagents" / "agent-x1.jsonl", [
            {"type": "assistant", "timestamp": iso(1.2), "message": {"id": "s1", "model": "small", "usage": usage(5, 0, 200, 10),
             "content": [{"type": "tool_use", "id": "st1", "name": "Grep", "input": {}}]}},
            {"type": "user", "timestamp": iso(1.4), "message": {"content": [{"type": "tool_result", "tool_use_id": "st1", "content": "g"}]}},
        ])
        (self.dir / "subagents" / "agent-x1.meta.json").write_text(json.dumps({"agentType": "Explore", "toolUseId": "t9"}))
        self.parsed = transcript.parse(path)

    def test_requests_are_deduplicated_and_timed_from_the_api_request(self):
        reqs = {r["id"]: r for r in self.parsed["requests"]}
        self.assertEqual(set(reqs), {"m1", "m2", "m3", "s1"})
        self.assertEqual(reqs["m3"]["output"], 30)  # last usage of a split message wins
        self.assertAlmostEqual(reqs["m1"]["end_ts"] - reqs["m1"]["start_ts"], 0.5, places=3)
        self.assertEqual(reqs["s1"]["agent"], "x1")

    def test_tools_skills_hooks_and_subagents(self):
        calls = {c["id"]: c for c in self.parsed["tool_calls"]}
        self.assertEqual(calls["t1"]["detail"], "graphify affected")
        self.assertEqual(calls["t2"]["detail"], ".py")
        self.assertEqual(calls["t2"]["result_chars"], 100)
        self.assertEqual([s["skill"] for s in self.parsed["skills"]], ["replicable:epcc", "replicable:tdd"])
        self.assertEqual([s["via"] for s in self.parsed["skills"]], ["slash", "tool"])
        names = [h["name"] for h in self.parsed["hooks"]]
        self.assertEqual(names, ["PostToolUse:Edit", "Stop:test_gate.py"])
        self.assertEqual(self.parsed["stop_blocks_prevented"], 1)
        self.assertEqual(self.parsed["subagents"][0]["type"], "Explore")

    def test_missing_transcript_is_flagged_not_raised(self):
        self.assertTrue(transcript.parse(self.dir / "nope.jsonl")["missing"])

    def test_bash_labels_keep_the_program_not_the_arguments(self):
        cases = {"cd /x && uv run pytest -q tests/secret_test.py": "uv run", "python3 -m pytest -q": "python -m pytest",
                 "A=1 B=2 ./scripts/deploy.sh --prod": "deploy.sh", "git -C /r status": "git", "": "?",
                 "/usr/bin/graphify query 'who calls x'": "graphify query"}
        for command, label in cases.items():
            self.assertEqual(transcript.bash_detail(command), label, command)


class MetricsTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = jsonl(Path(tmp.name) / "session.jsonl", [e for e in SESSION if isinstance(e, dict)])
        self.parsed = transcript.parse(path)
        self.summary = {"total_cost_usd": 1.0, "models": {"big": {"costUSD": 1.0}}, "duration_ms": 7000, "num_turns": 3}
        self.m = metrics.compute(self.parsed, self.summary, [], 7000)

    def test_costs_add_up_to_what_claude_reported(self):
        self.assertAlmostEqual(sum(r["cost"] for r in self.parsed["requests"]), 1.0, places=6)

    def test_tool_attribution(self):
        tools = self.m["tools"]
        # time: Edit's interval (1.0s) minus its 500 ms PostToolUse hook
        self.assertEqual(tools["Edit"]["ms"]["total"], 500.0)
        self.assertEqual(tools["Edit"]["hook_ms"], 500)
        # output tokens of m1 (100) split over its two calls
        self.assertEqual(tools["Bash"]["out_tokens"], 50)
        # growth 1610-1010-100 = 500 tokens over 300 + 100 result chars + 100 hook chars
        self.assertEqual(tools["Bash"]["result_tokens"], 300)
        self.assertEqual(tools["Edit"]["result_tokens"], 100)
        # rent: re-read by every later request after the first one that saw it (m3 only)
        self.assertEqual(tools["Bash"]["rent_tokens"], 300)
        self.assertIn("Bash:graphify affected", tools)
        self.assertEqual(tools["(text)"]["out_tokens"], 30)

    def test_skill_spans(self):
        skills = self.m["skills"]
        self.assertEqual(set(skills), {"replicable:epcc", "replicable:tdd"})
        self.assertEqual(skills["replicable:epcc"]["requests"], 2)
        self.assertEqual(skills["replicable:tdd"]["requests"], 1)
        self.assertEqual(skills["replicable:epcc"]["wall_ms"], 4000)
        self.assertAlmostEqual(sum(s["cost_usd"] for s in skills.values()), 1.0, places=4)

    def test_hooks_and_totals(self):
        self.assertEqual(self.m["hooks"]["PostToolUse:Edit"]["context_chars"], 100)
        self.assertEqual(self.m["hooks"]["Stop:test_gate.py"]["ms"]["total"], 40)
        self.assertEqual(self.m["totals"]["context_peak"], 1620)
        self.assertEqual(self.m["totals"]["tokens"]["output"], 180)

    def test_trace_rows_give_every_hook_run_and_the_gate_summary(self):
        rows = [{"hook": "post_edit", "outcome": "skip", "reason": "docs", "duration_ms": 5},
                {"hook": "post_edit", "outcome": "context", "reason": "reported", "duration_ms": 700, "chars": 90},
                {"hook": "test_gate", "outcome": "block", "reason": "tests_fail", "duration_ms": 900},
                {"hook": "test_gate", "outcome": "pass", "reason": "tests_pass", "after_blocks": 1, "test_ms": 800}]
        m = metrics.compute(self.parsed, self.summary, rows, 7000)
        self.assertEqual(m["plugin_hooks"]["post_edit"]["outcomes"], {"skip:docs": 1, "context:reported": 1})
        self.assertEqual(m["plugin_hooks"]["post_edit"]["context_chars"], 90)
        self.assertEqual((m["gate"]["blocks"], m["gate"]["passes"], m["gate"]["fixed_after_block"]), (1, 1, True))


class FailureLabelTests(unittest.TestCase):
    def label(self, **kw):
        base = dict(resolved=False, infra_error=False, agent_status="ok", summary={}, parsed={"hooks": []},
                    trace_rows=[], grade={"f2p": {"ok": False}, "p2p": {"ok": True}, "f2p_pass_rate": 0.0},
                    diff={"files_changed": 2}, plugin_arm=True)
        base.update(kw)
        return metrics.failure_label(**base)

    def test_precedence(self):
        self.assertIsNone(self.label(resolved=True))
        self.assertEqual(self.label(infra_error=True, agent_status="timeout"), "infra_error")
        self.assertEqual(self.label(agent_status="timeout"), "timeout")
        self.assertEqual(self.label(summary={"subtype": "error_max_budget_usd"}), "budget_exhausted")
        self.assertEqual(self.label(parsed={"hooks": [{"kind": "hook_non_blocking_error"}]}), "hook_error")
        self.assertEqual(self.label(trace_rows=[{"hook": "session_start", "reason": "no_graphify"}]), "graphify_unavailable")
        self.assertEqual(self.label(trace_rows=[{"hook": "test_gate", "reason": "cap_hit"}]), "gate_cap_hit")
        self.assertEqual(self.label(diff={"files_changed": 0}), "agent_gave_up")
        self.assertEqual(self.label(grade={"f2p": {"ok": True}, "p2p": {"ok": False}, "f2p_pass_rate": 1.0}),
                         "broke_other_tests")
        self.assertEqual(self.label(grade={"f2p": {"ok": False}, "p2p": {"ok": True}, "f2p_pass_rate": 0.5}), "incomplete")
        self.assertEqual(self.label(), "wrong_fix")
        self.assertEqual(set(metrics.FAILURE_LABELS) >= {"wrong_fix", "infra_error"}, True)

    def test_false_done(self):
        self.assertTrue(metrics.false_done(False, "ok", {"subtype": "success"}))
        self.assertFalse(metrics.false_done(True, "ok", {"subtype": "success"}))
        self.assertFalse(metrics.false_done(False, "timeout", {}))
        self.assertFalse(metrics.false_done(False, "ok", {"subtype": "error_max_turns", "is_error": True}))


class StatsTests(unittest.TestCase):
    def test_pass_hat_k_and_pass_at_k(self):
        self.assertEqual(report.pass_hat_k(3, 3, 3), 1.0)
        self.assertAlmostEqual(report.pass_hat_k(4, 2, 2), 1 / 6)
        self.assertAlmostEqual(report.pass_at_k(4, 2, 2), 5 / 6)
        self.assertIsNone(report.pass_hat_k(1, 1, 3))

    def test_bootstrap_of_identical_arms_is_zero(self):
        data = {"t1": ([1.0, 0.0], [1.0, 0.0]), "t2": ([1.0, 1.0], [1.0, 1.0])}
        point, lo, hi = report.bootstrap(data, lambda xs: sum(xs) / len(xs))
        self.assertEqual(point, 0.0)
        self.assertLessEqual(lo, 0.0)
        self.assertGreaterEqual(hi, 0.0)
        better = {"t1": ([1.0, 1.0], [0.0, 0.0])}
        self.assertEqual(report.bootstrap(better, lambda xs: sum(xs) / len(xs)), (1.0, 1.0, 1.0))


def result_row(task, arm, resolved, trial, task_hash="v1", route=None, cost=0.1, status="ok"):
    plugin = None if arm == "baseline" else {"version": "0.6.0", "sha": "a" * 40, "content_hash": "p"}
    key = f"{task}|{arm}|{task_hash}|{trial}"
    return {"run_id": key, "key": key, "ts": f"2026-01-01T00:00:{trial:02d}Z", "status": status,
            "resolved": resolved, "task": {"id": task, "hash": task_hash}, "route": route,
            "arm": {"name": arm, "plugin": plugin, "prompt": "raw"}, "totals": {"cost_usd": cost, "wall_ms": 2000}}


class DifficultyTests(unittest.TestCase):
    """Task difficulty is measured from the baseline's results, never declared."""

    def rows(self):
        rows = []
        for i, ok in enumerate([True, True, True]):
            rows.append(result_row("easy-one", "baseline", ok, i, cost=0.2))
        for i, ok in enumerate([True, False, False]):
            rows.append(result_row("mid-one", "baseline", ok, i))
        for i in range(2):
            rows.append(result_row("hard-one", "baseline", False, i, cost=0.9))
        rows.append(result_row("hard-one", "baseline", True, 9, task_hash="v0"))       # an older task version
        rows.append(result_row("hard-one", "baseline", True, 8, status="infra_error"))  # never scored
        rows.append(result_row("new-one", "candidate-raw", True, 0))                    # no baseline: unmeasured
        for i, (task, route) in enumerate([("easy-one", "single-stage"), ("mid-one", "single-stage"),
                                           ("hard-one", "multi-stage")]):
            rows.append(result_row(task, "candidate-raw", task != "hard-one", i, route=route))
        return rows

    def test_buckets_come_from_the_baseline_on_the_current_task_version(self):
        diff = report.difficulty(self.rows(), hashes={"easy-one": "v1", "mid-one": "v1", "hard-one": "v1",
                                                      "new-one": "v1"})
        self.assertEqual({t: d["bucket"] for t, d in diff.items()},
                         {"easy-one": "easy", "mid-one": "medium", "hard-one": "hard"})
        self.assertEqual((diff["hard-one"]["baseline_solved"], diff["hard-one"]["baseline_runs"]), (0, 2))
        self.assertEqual(diff["hard-one"]["baseline_median_cost"], 0.9)
        self.assertEqual(report.bucket_of(diff, "new-one"), "unmeasured")
        old = report.difficulty(self.rows(), hashes={"hard-one": "v0"})
        self.assertEqual(old["hard-one"]["bucket"], "easy")

    def test_reports_group_by_difficulty_and_route(self):
        data = report.build(self.rows())
        cand = data["arms"]["candidate-raw"]
        self.assertEqual(list(cand["by_difficulty"]), ["easy", "medium", "hard", "unmeasured"])
        self.assertEqual(cand["by_difficulty"]["hard"]["pass1"], 0.0)
        self.assertEqual(cand["by_route"]["single-stage"]["tasks"], 2)
        self.assertEqual(cand["by_route"]["none"]["tasks"], 1)
        self.assertEqual(data["difficulty"]["new-one"], {"bucket": "unmeasured"})
        md = report.markdown(data)
        self.assertIn("## By measured difficulty", md)
        self.assertIn("## By route", md)
        self.assertIn("| hard-one | hard (0/2 · $0.90) |", md)
        self.assertNotIn("Tier", md)
        no_routes = [r for r in self.rows() if r["route"] is None]
        self.assertNotIn("## By route", report.markdown(report.build(no_routes)))


class SuiteTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        write(self.root, {
            "suite.json": {"dataset": {"repo": "x", "sha": "a" * 40}, "defaults": {"test_command": "true {tests}"}},
            "tasks/t-one/task.json": {"tier": "L1", "fail_to_pass": ["a"]},
            "tasks/t-one/prompt.md": "Do the thing.\n",
            "tasks/_draft/task.json": "not even json",
        })

    def test_loads_and_skips_drafts(self):
        s = suite_mod.load(self.root)
        self.assertEqual(list(s.tasks), ["t-one"])
        self.assertIn("candidate-skill", s.arms)
        self.assertTrue(s.tasks["t-one"].content_hash)

    def test_tier_is_an_optional_tag_and_selection_spans_measured_difficulty(self):
        write(self.root, {f"tasks/{t}/task.json": {"fail_to_pass": ["a"]} for t in ("t-a", "t-b", "t-c")})
        write(self.root, {"tasks/t-one/task.json": {"tier": "cross-module refactor", "fail_to_pass": ["a"]}})
        for t in ("t-a", "t-b", "t-c"):
            write(self.root, {f"tasks/{t}/prompt.md": "Do it.\n"})
        s = suite_mod.load(self.root)
        self.assertIsNone(s.tasks["t-a"].tier)
        self.assertEqual(s.tasks["t-one"].tier, "cross-module refactor")
        self.assertEqual([t.id for t in s.select(tier="smoke")], ["t-a", "t-b", "t-c"])  # unmeasured: by id
        measured = {"t-a": "easy", "t-b": "easy", "t-c": "hard", "t-one": "medium"}
        self.assertEqual([t.id for t in s.select(tier="smoke", difficulty=measured)], ["t-a", "t-c", "t-one"])

    def test_rejects_what_cannot_be_measured(self):
        cases = [
            ("suite.json", {"dataset": {"repo": "x", "sha": "main"}}, "40-character"),
            ("tasks/t-one/task.json", {"tier": ["L1"], "fail_to_pass": ["a"]}, "free-form"),
            ("tasks/t-one/task.json", {"tier": "L1", "fail_to_pass": []}, "fail_to_pass"),
            ("suite.json", {"dataset": {"repo": "x", "sha": "a" * 40}, "defaults": {"test_command": "t"},
                            "arms": {"bad": {"plugin": None, "prompt": "skill"}}}, "needs a plugin"),
            ("suite.json", {"dataset": {"repo": "x", "sha": "a" * 40}, "defaults": {"test_command": "t"},
                            "arms": {"bad": {"plugin": "candidate", "setup": ["deploy"]}}}, "unknown setup"),
        ]
        for rel, body, message in cases:
            original = (self.root / rel).read_text()
            write(self.root, {rel: body})
            with self.assertRaises(suite_mod.SuiteError) as ctx:
                suite_mod.load(self.root)
            self.assertIn(message, str(ctx.exception))
            (self.root / rel).write_text(original)


class PreviousTagTests(unittest.TestCase):
    def test_previous_is_the_newest_tag_that_is_not_the_candidate(self):
        import arms
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        repo = Path(tmp.name)
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        for version in ("0.9.0", "0.10.0"):  # 0.10.0 must sort above 0.9.0: versions, not strings
            (repo / "f").write_text(version)
            subprocess.run(["git", "add", "f"], cwd=repo, check=True)
            subprocess.run([*GIT, "commit", "-qm", version], cwd=repo, check=True)
            subprocess.run([*GIT, "tag", f"v{version}"], cwd=repo, check=True)
        self.assertEqual(arms.previous_tag(repo), "v0.9.0")  # HEAD is v0.10.0, so it is the candidate
        (repo / "f").write_text("work in progress")
        self.assertEqual(arms.previous_tag(repo), "v0.10.0")  # a dirty tree is newer than every tag


class IssueTests(unittest.TestCase):
    """Only the chosen issues are frozen; a task can be an issue; a frozen issue never changes by itself."""

    def setUp(self):
        import issues
        self.issues = issues
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        write(self.root, {
            "suite.json": {"dataset": {"repo": "https://github.com/org/data.git", "sha": "a" * 40},
                           "defaults": {"test_command": "true {tests}"}},
            "tasks/fix-826/task.json": {"tier": "L1", "issue": 826, "fail_to_pass": ["a"]},
        })
        self.remote = {
            826: {"number": 826, "title": "Can't submit a second issue", "body": "Submitting twice hangs.",
                  "state": "open", "labels": [{"name": "bug"}], "user": {"login": "dev"}, "comments": 1},
            70: {"number": 70, "title": "Unique study names", "body": "", "state": "open", "labels": [], "comments": 0},
            5: {"number": 5, "title": "A PR", "pull_request": {}, "comments": 0},
        }
        self.calls = []

    def fetch(self, path, paginate=False):
        self.calls.append(path)
        n = int(path.split("/issues/")[1].split("/")[0])
        if path.endswith("comments?per_page=100"):
            return [{"user": {"login": "lead"}, "created_at": "2026-11-09T10:00:00Z", "body": "Repro: click twice."}]
        return dict(self.remote[n])

    def test_freezes_only_what_is_asked_and_keeps_it_frozen(self):
        self.assertEqual(self.issues.repo_slug("https://github.com/org/data.git"), "org/data")
        self.assertEqual(self.issues.repo_slug("git@github.com:org/data.git"), "org/data")
        self.assertEqual(self.issues.referenced(self.root), [826])
        out = self.issues.snapshot("org/data", self.root, [826], fetch=self.fetch)
        self.assertEqual((out["added"], out["meta"]["issues"]), ([826], 1))
        frozen = self.issues.load(self.root)
        self.assertEqual(list(frozen), [826])
        self.assertEqual(frozen[826]["comments"][0]["body"], "Repro: click twice.")
        self.assertEqual(frozen[826]["labels"], ["bug"])

        self.remote[826]["body"] = "Edited later on GitHub."
        out = self.issues.snapshot("org/data", self.root, [826, 70], fetch=self.fetch)
        self.assertEqual((out["added"], out["kept"]), ([70], [826]))
        self.assertEqual(self.issues.load(self.root)[826]["body"], "Submitting twice hangs.")
        with self.assertRaises(RuntimeError):
            self.issues.snapshot("org/data", self.root, [5], fetch=self.fetch)

    def test_personal_data_from_in_app_reports_is_not_frozen(self):
        self.remote[826]["body"] = ("Submitting twice hangs, ask dev@example.com.\n\n![img](https://x)\n\n---\n\n"
                                    "### Report diagnostics\n\n<details><pre>{&quot;email&quot;: &quot;me@corp.com&quot;, "
                                    "&quot;sub&quot;: &quot;google-oauth2|123&quot;}</pre></details>\n")
        self.issues.snapshot("org/data", self.root, [826], fetch=self.fetch)
        body = self.issues.load(self.root)[826]["body"]
        self.assertTrue(body.startswith("Submitting twice hangs, ask [email removed]."))
        self.assertIn("(Report diagnostics removed from the benchmark snapshot.)", body)
        for secret in ("me@corp.com", "google-oauth2", "dev@example.com", "<details>"):
            self.assertNotIn(secret, (self.root / "dataset" / "issues.jsonl").read_text())

    def test_a_task_can_be_an_issue_and_its_text_is_part_of_the_task(self):
        with self.assertRaises(suite_mod.SuiteError) as ctx:
            suite_mod.load(self.root)
        self.assertIn("#826 is not frozen", str(ctx.exception))
        self.issues.snapshot("org/data", self.root, [826], fetch=self.fetch)
        task = suite_mod.load(self.root).tasks["fix-826"]
        self.assertTrue(task.prompt.startswith("Resolve GitHub issue #826: Can't submit a second issue"))
        self.assertIn("Submitting twice hangs.", task.prompt)
        self.assertIn("- lead (2026-11-09): Repro: click twice.", task.prompt)
        self.assertEqual((task.issue, task.issue_state), (826, "open"))
        before = task.content_hash
        self.remote[826]["body"] = "Edited later on GitHub."
        self.issues.snapshot("org/data", self.root, [826], refresh=True, fetch=self.fetch)
        self.assertNotEqual(suite_mod.load(self.root).tasks["fix-826"].content_hash, before)
        write(self.root, {"tasks/fix-826/prompt.md": "Our own words.\n"})
        self.assertEqual(suite_mod.load(self.root).tasks["fix-826"].prompt, "Our own words.")


class GradeTests(unittest.TestCase):
    def test_a_selection_that_passes_nothing_never_counts_as_passing(self):
        import grade
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        out = Path(tmp.name)
        cases = {  # junit written by the "test runner", and whether the group may pass with exit code 0
            '<testsuites><testsuite tests="3" failures="0" errors="0" skipped="0"/></testsuites>': True,
            '<testsuites><testsuite tests="2" failures="0" errors="0" skipped="2"/></testsuites>': False,
            '<testsuite tests="0" failures="0" errors="0" skipped="0"/>': False,
        }
        for xml, expected in cases.items():
            (out / "x.xml").write_text(xml)
            cmd = f"cp {out / 'x.xml'} {{junit}} # {{tests}}"
            row = grade.run_group("g", ["t"], cmd, out, out, dict(os.environ), 30)
            self.assertEqual(row["ok"], expected, xml)
        no_junit = grade.run_group("g", ["t"], "true {tests} {junit}", out, out, dict(os.environ), 30)
        self.assertTrue(no_junit["ok"])  # without JUnit only the exit code is known


def toy_mirror(test: unittest.TestCase):
    """A one-file origin repository and the bench's mirror of it, under a temporary bench home."""
    import workspace
    tmp = tempfile.TemporaryDirectory()
    test.addCleanup(tmp.cleanup)
    base = Path(tmp.name).resolve()
    origin = base / "origin"
    origin.mkdir()
    (origin / "f").write_text("x")
    subprocess.run(["git", "init", "-q"], cwd=origin, check=True)
    subprocess.run(["git", "add", "f"], cwd=origin, check=True)
    subprocess.run([*GIT, "commit", "-qm", "init"], cwd=origin, check=True)
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=origin, capture_output=True, text=True).stdout.strip()
    patcher = unittest.mock.patch.dict(os.environ, {"REPLICABLE_BENCH_HOME": str(base / "home")})
    patcher.start()
    test.addCleanup(patcher.stop)
    return base, origin, sha, workspace.mirror(str(origin), sha, "toy")


class WorkspaceTests(unittest.TestCase):
    def test_bytecode_and_graphify_output_are_not_the_agents_change(self):
        import workspace
        base, _, sha, mirror = toy_mirror(self)
        repo = workspace.checkout(mirror, sha, base / "run" / "repo")
        env = dict(os.environ, **workspace.BENCH_IDENTITY)
        write(repo, {"__pycache__/setup.cpython-312.pyc": "setup"})
        start = workspace.commit_all(repo, "bench: setup", env)
        self.assertEqual(start, sha)  # setup's bytecode is not committed either
        write(repo, {"f": "changed\n", "__pycache__/f.cpython-312.pyc": "b", "pkg/__pycache__/m.cpython-39.pyc": "b",
                     "legacy.pyc": "b", "graphify-out/graph.json": "{}"})
        patch = base / "agent.patch"
        stats = workspace.diff_stats(repo, start, env, patch_out=patch)
        self.assertEqual((stats["files_changed"], stats["lines_added"]), (1, 1))
        self.assertNotIn("pyc", patch.read_text())

    def test_an_existing_mirror_gains_new_ignore_rules(self):
        import workspace
        _, origin, sha, mirror = toy_mirror(self)
        exclude = mirror / "info" / "exclude"
        exclude.write_text("# mine\ngraphify-out/\n")
        workspace.mirror(str(origin), sha, "toy")
        lines = exclude.read_text().splitlines()
        self.assertEqual(lines[0], "# mine")
        self.assertEqual(sorted(lines[1:]), sorted(workspace.IGNORED_BY_BENCH))


class NoWayOutTests(unittest.TestCase):
    """A run must never reach the dataset's real repository: no push, no gh, no tokens."""

    def test_pushes_from_a_run_fail_and_the_agent_is_denied_push_and_gh(self):
        import agent
        import workspace
        base, origin, sha, mirror = toy_mirror(self)
        repo = workspace.checkout(mirror, sha, base / "run" / "repo")
        (repo / "f").write_text("bench change")
        subprocess.run([*GIT, "commit", "-qam", "agent"], cwd=repo, check=True)
        for args in (["push", "origin", "HEAD:refs/heads/bench"], ["push", "--mirror"], ["push"]):
            res = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True,
                                 env=dict(os.environ, GIT_TERMINAL_PROMPT="0"))
            self.assertNotEqual(res.returncode, 0, args)
        branches = subprocess.run(["git", "branch", "--list"], cwd=origin, capture_output=True, text=True).stdout
        self.assertNotIn("bench", branches)
        cmd = agent.command("p", "s", model="m", effort=None, budget_usd=1, permission_mode="acceptEdits",
                            allowed_tools=["Bash"], plugin_dir=None, disallowed_tools=["WebFetch"])
        denied = cmd[cmd.index("--disallowedTools") + 1:]
        self.assertEqual(denied, ["Bash(git push:*)", "Bash(gh:*)", "WebFetch"])
        with unittest.mock.patch.dict(os.environ, {"GH_TOKEN": "secret", "GITHUB_TOKEN": "secret"}):
            env = agent.environment(base / "run", {})
        self.assertNotIn("GH_TOKEN", env)
        self.assertNotIn("GITHUB_TOKEN", env)
        self.assertEqual(env["GH_CONFIG_DIR"], str(base / "run" / "gh-config"))


class DeliveryTests(unittest.TestCase):
    """The delivery proxy builds, measures and tears down; it never pushes. Docker is faked."""

    def test_builds_measures_and_tears_down(self):
        import delivery
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        bin_dir, log = Path(tmp.name) / "bin", Path(tmp.name) / "docker.log"
        bin_dir.mkdir()
        fake = bin_dir / "docker"
        fake.write_text(textwrap.dedent(f"""\
            #!/bin/sh
            echo "$@" >> {log}
            case "$*" in
              "image inspect"*) echo 52428800 ;;
              *"up --build"*) [ -n "$FAIL_UP" ] && exit 3 ;;
            esac
            exit 0
            """))
        fake.chmod(0o755)
        env = dict(os.environ, PATH=f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
        config = {"build": [{"name": "api", "cmd": "docker build -t {image} ."}],
                  "suites": {"finance": {"compose": "compose.finance-tests.yml", "service": "finance-tests"}}}
        out = delivery.run(config, ["finance", "missing"], Path(tmp.name), env, "20260101-Task-X")
        self.assertEqual(out["builds"][0]["image_bytes"], 52428800)
        self.assertTrue(out["builds"][0]["ok"])
        self.assertTrue(out["suites"][0]["ok"])
        self.assertEqual(out["suites"][1], {"name": "missing", "ok": False, "error": "not defined"})
        self.assertFalse(out["ok"])
        calls = log.read_text()
        self.assertIn("build -t bench-20260101-task-x-api .", calls)
        self.assertIn("image rm -f bench-20260101-task-x-api", calls)
        self.assertIn("-f compose.finance-tests.yml down -v", calls)
        self.assertNotIn("push", calls)
        env["FAIL_UP"] = "1"
        failed = delivery.run(config, ["finance"], Path(tmp.name), env, "r2")
        self.assertEqual(failed["suites"][0]["error"], "exit 3")


class EndToEndTests(unittest.TestCase):
    """A whole benchmark against a toy dataset, with a fake claude: no API calls, no graphify needed."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name).resolve()
        self.dataset, self.suite, self.home, self.config = (base / d for d in ("dataset", "suite", "home", "claude"))
        write(self.dataset, {
            "calc.py": "def add(a, b):\n    return a + b\n",
            "tests/__init__.py": "",
            "tests/test_calc.py": "import unittest\nfrom calc import add\n\nclass T(unittest.TestCase):\n"
                                  "    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n",
        })
        subprocess.run(["git", "init", "-q"], cwd=self.dataset, check=True)
        subprocess.run(["git", "add", "-A"], cwd=self.dataset, check=True)
        subprocess.run([*GIT, "commit", "-qm", "init"], cwd=self.dataset, check=True)
        sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.dataset, capture_output=True, text=True).stdout.strip()
        subprocess.run([sys.executable, str(BENCH / "run.py"), "init-suite", str(self.suite), "--repo", str(self.dataset),
                        "--sha", sha, "--name", "toy"], check=True, capture_output=True, env=self.env())
        config = json.loads((self.suite / "suite.json").read_text())
        config["defaults"].update(model="fake-model", trials=1, test_command=f"{sys.executable} -m unittest {{tests}}",
                                  harness_test_command=f"{sys.executable} -m unittest discover -s tests")
        config["arms"] = {"candidate-raw": {"plugin": "candidate", "prompt": "raw", "setup": ["test_gate"]},
                          "candidate-skill": {"plugin": "candidate", "prompt": "skill", "setup": ["test_gate"]}}
        write(self.suite, {
            "suite.json": config,
            "tasks/add-double/task.json": {"fail_to_pass": ["tests/test_double.py"],
                                           "pass_to_pass": ["tests/test_calc.py"]},
            "tasks/add-double/prompt.md": "Add double(x) to calc.py.\n",
            "tasks/add-double/hidden/tests/test_double.py":
                "import unittest\nfrom calc import double\n\nclass T(unittest.TestCase):\n"
                "    def test_double(self):\n        self.assertEqual(double(4), 8)\n",
            "tasks/add-double/solution.patch": textwrap.dedent("""\
                --- a/calc.py
                +++ b/calc.py
                @@ -1,2 +1,6 @@
                 def add(a, b):
                     return a + b
                +
                +
                +def double(x):
                +    return 2 * x
                """),
        })

    def env(self, **extra):
        env = {k: v for k, v in os.environ.items() if not k.startswith(("REPLICABLE_", "LAB_", "HARNESS_"))}
        env.update(CLAUDE_BIN=str(FAKE_CLAUDE), CLAUDE_CONFIG_DIR=str(self.config), REPLICABLE_BENCH_HOME=str(self.home))
        env.update(extra)
        return env

    def bench(self, *args, **env):
        res = subprocess.run([sys.executable, str(BENCH / "run.py"), *args], capture_output=True, text=True,
                             env=self.env(**env), timeout=300)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        return res.stdout

    def rows(self):
        return [json.loads(line) for line in (self.suite / "results" / "runs.jsonl").read_text().splitlines() if line]

    def test_validate_proves_the_task(self):
        out = self.bench("validate", "--suite", str(self.suite), "--grade")
        self.assertIn("ok  add-double", out)
        write(self.suite, {"tasks/add-double/task.json": {"fail_to_pass": ["tests/test_calc.py"]}})
        res = subprocess.run([sys.executable, str(BENCH / "run.py"), "validate", "--suite", str(self.suite), "--grade"],
                             capture_output=True, text=True, env=self.env(), timeout=300)
        self.assertEqual(res.returncode, 1)
        self.assertIn("FAIL_TO_PASS already passes", res.stdout)

    def test_run_records_every_path_reuses_identical_runs_and_reports(self):
        arms = ["--arms", "baseline", "candidate-raw", "candidate-skill"]
        out = self.bench("run", "--suite", str(self.suite), *arms, "--seed", "1", "--experiment", "0000-test")
        self.assertIn("3 to run", out)
        rows = {r["arm"]["name"]: r for r in self.rows()}
        self.assertEqual(set(rows), {"baseline", "candidate-raw", "candidate-skill"})
        for name, row in rows.items():
            self.assertEqual(row["status"], "ok", row.get("error"))
            self.assertTrue(row["resolved"], name)
            self.assertIsNone(row["failure_label"])
            self.assertEqual(row["env"]["claude_code_version"], "9.9.9 (Claude Code)")
            self.assertEqual(row["totals"]["cost_usd"], 0.05)
            self.assertEqual(row["subagents"]["replicable:graph-explorer"]["count"], 1)
            self.assertEqual(row["diff"]["files_changed"], 1)
            self.assertNotIn("never-store-me", json.dumps(row))
            self.assertFalse((self.config / "projects" / project_slug(Path(row["run_dir"]) / "repo")).exists())
            self.assertTrue((Path(row["run_dir"]) / "transcript" / "session.jsonl").exists())
            self.assertFalse((Path(row["run_dir"]) / "repo").exists())  # worktree removed
        self.assertIsNone(rows["baseline"]["arm"]["plugin"])
        self.assertEqual(rows["baseline"]["plugin_hooks"], {})
        self.assertEqual(rows["candidate-raw"]["arm"]["plugin"]["sha"],
                         subprocess.run(["git", "rev-parse", "HEAD"], cwd=PLUGIN, capture_output=True, text=True).stdout.strip())
        hooks = rows["candidate-raw"]["plugin_hooks"]
        self.assertEqual(set(hooks), {"session_start", "post_edit", "test_gate"})
        self.assertEqual(hooks["test_gate"]["outcomes"], {"pass:tests_pass": 1})
        self.assertIn("replicable:epcc", rows["candidate-skill"]["skills"])
        self.assertEqual(set(rows["candidate-raw"]["skills"]), {"(no skill)"})

        out = self.bench("run", "--suite", str(self.suite), *arms, "--seed", "1", "--experiment", "0000-test")
        self.assertIn("3 reused from earlier runs, 0 to run", out)

        self.bench("run", "--suite", str(self.suite), "--arms", "candidate-raw", "--rerun", "--experiment", "0001-noop",
                   FAKE_CLAUDE_MODE="noop")
        noop = self.rows()[-1]
        self.assertFalse(noop["resolved"])
        self.assertEqual(noop["failure_label"], "agent_gave_up")
        self.assertTrue(noop["false_done"])

        self.bench("run", "--suite", str(self.suite), "--arms", "candidate-raw", "--rerun", "--experiment", "0002-break",
                   FAKE_CLAUDE_MODE="break")
        broken = self.rows()[-1]
        self.assertEqual(broken["plugin_hooks"]["test_gate"]["outcomes"], {"block:tests_fail": 1})
        self.assertEqual(broken["failure_label"], "wrong_fix")
        self.assertFalse(broken["grade"]["p2p"]["ok"])

        self.bench("label", "--suite", str(self.suite), broken["run_id"], "broke_other_tests", "--note", "read it")
        md = self.bench("report", "--suite", str(self.suite), "--experiment", "0000-test")
        self.assertIn("| candidate-raw |", md)
        self.assertIn("candidate-raw | baseline", md)
        self.assertIn("| add-double | easy (1/1 · $0.05) |", md)  # measured from the baseline run
        self.assertNotIn("0002", md)
        html_path = self.suite / "results" / "reports" / "all.html"
        html_path.parent.mkdir(parents=True, exist_ok=True)
        self.bench("report", "--suite", str(self.suite), "--html", str(html_path))
        page = html_path.read_text()
        self.assertIn("<title>", page)
        self.assertIn("broke_other_tests", page)  # the manual label wins over the automatic one

    def test_dry_run_prints_the_plan_and_spends_nothing(self):
        out = self.bench("run", "--suite", str(self.suite), "--arms", "baseline", "candidate-raw", "--dry-run")
        self.assertIn("2 to run", out)
        self.assertIn("at most $10.00", out)
        self.assertEqual((self.suite / "results" / "runs.jsonl").read_text(), "")
