#!/usr/bin/env python3
"""Benchmark the replicable plugin against a suite: a dataset at a pinned commit and a set of tasks.

    python3 bench/run.py init-suite <dir> --repo <git url> --sha <commit>
    python3 bench/run.py validate --suite <dir> [--grade] [--tasks 'l1-*']
    python3 bench/run.py run --suite <dir> [--tier smoke|standard|release] [--arms a b] [--tasks glob]
                         [--trials N] [--experiment ID] [--delivery] [--max-cost-usd X] [--dry-run]
    python3 bench/run.py report --suite <dir> [--experiment ID] [--md out.md] [--html out.html]
    python3 bench/run.py new-experiment --suite <dir> <slug> [--hypothesis TEXT]
    python3 bench/run.py label --suite <dir> <run-id> <label> [--note TEXT]
    python3 bench/run.py snapshot-issues --suite <dir> [<issue number> ...] [--refresh]

Everything is described in docs/benchmarking.md.
"""

from __future__ import annotations

import argparse
import platform
import random
import shutil
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import agent  # noqa: E402
import arms as arms_mod  # noqa: E402
import grade as grade_mod  # noqa: E402
import metrics  # noqa: E402
import suite as suite_mod  # noqa: E402
import transcript  # noqa: E402
import workspace  # noqa: E402
from common import (BENCH_DIR, PLUGIN_ROOT, RUNNER_VERSION, append_jsonl, claude_bin, clean_env, digest,  # noqa: E402
                    git, graphify_bin, home, now_iso, read_json, read_jsonl, tool_version, write_json)

_lock = threading.Lock()


# -- environment ---------------------------------------------------------------
def environment_info(model, effort) -> dict:
    graphify = graphify_bin()
    return {
        "model": model,
        "effort": effort,
        "claude_code_version": tool_version([claude_bin(), "--version"]),
        "graphify_version": tool_version([graphify, "--version"]) if graphify else None,
        "os": f"{platform.system()} {platform.release()} {platform.machine()}",
        "python": platform.python_version(),
        "managed_settings": agent.managed_settings_hash(),
        "runner_version": RUNNER_VERSION,
        "parser_version": transcript.PARSER_VERSION,
        "metrics_version": metrics.METRICS_VERSION,
    }


def arm_record(arm: dict) -> dict:
    plugin = arm.get("plugin_version")
    return {
        "name": arm["name"], "prompt": arm.get("prompt", "raw"), "setup": list(arm.get("setup", [])),
        "env": dict(arm.get("env") or {}),
        "plugin": None if plugin is None else {k: plugin[k] for k in ("ref", "sha", "version", "dirty", "content_hash")},
    }


def run_key(suite, task, arm: dict, trial: int, env_info: dict) -> str:
    return digest({
        "task": [task.id, task.version, task.content_hash],
        "dataset": [suite.dataset["sha"], suite.dataset.get("setup", [])],
        "env": [env_info["model"], env_info["effort"], env_info["claude_code_version"], env_info["graphify_version"]],
        "arm": arm_record(arm),
        "settings": [suite.setting("skill", task), suite.setting("budget_usd", task), suite.setting("timeout_s", task),
                     suite.defaults["permission_mode"], suite.defaults["allowed_tools"],
                     suite.defaults.get("disallowed_tools") or [],
                     suite.setting("harness_test_command")],
        "trial": trial,
    })


# -- one run ---------------------------------------------------------------------
def run_one(suite, task, arm: dict, trial: int, ctx: dict) -> dict:
    run_id = f"{time.strftime('%Y%m%d-%H%M%S')}-{task.id}-{arm['name']}-t{trial}-{uuid.uuid4().hex[:6]}"
    run_dir = home() / "runs" / suite.name / run_id
    repo = run_dir / "repo"
    base_env = clean_env({**workspace.BENCH_IDENTITY, **suite.defaults.get("env", {})})
    plugin = arm.get("plugin_version")
    row = {
        "run_id": run_id, "experiment": ctx["experiment"], "ts": now_iso(), "suite": suite.name,
        "key": ctx["key"], "seed": ctx["seed"], "trial": trial,
        "task": {"id": task.id, "version": task.version, "tier": task.tier, "tags": task.tags,
                 "hash": task.content_hash[:16], "issue": task.issue},
        "arm": arm_record(arm),
        "env": dict(ctx["env_info"], dataset_repo=suite.dataset["repo"], dataset_sha=suite.dataset["sha"],
                    issues_snapshot=(suite.issues_meta or {}).get("sha256", "")[:16] or None),
        "status": "ok", "error": None, "setup": [], "resolved": False,
    }
    try:
        workspace.checkout(ctx["mirror"], suite.dataset["sha"], repo)
        steps = workspace.run_setup(list(suite.dataset.get("setup", [])) + list(task.setup), repo, base_env)
        harness_cmd = suite.setting("harness_test_command")
        if all(s["ok"] for s in steps):
            steps += arms_mod.setup(arm, repo, base_env, harness_cmd)
        row["setup"] = steps
        row["setup_ms"] = sum(s["ms"] for s in steps)
        failed = [s for s in steps if not s["ok"]]
        if failed:
            raise RuntimeError(f"setup step failed: {failed[0]['step']} ({failed[0].get('error')})")
        base = workspace.commit_all(repo, "bench: setup", base_env)

        prompt = arms_mod.prompt_for(arm, task.prompt, task.skill or suite.defaults["skill"])
        session_id = agent.new_session_id()
        cmd = agent.command(prompt, session_id, model=ctx["env_info"]["model"], effort=ctx["env_info"]["effort"],
                            budget_usd=float(suite.setting("budget_usd", task)),
                            permission_mode=suite.defaults["permission_mode"],
                            allowed_tools=list(suite.defaults["allowed_tools"]),
                            disallowed_tools=list(suite.defaults.get("disallowed_tools") or []),
                            plugin_dir=plugin.path if plugin else None)
        extra = dict(workspace.BENCH_IDENTITY, **suite.defaults.get("env", {}), **(arm.get("env") or {}))
        if plugin:
            extra.update(agent.plugin_env("test_gate" in arm.get("setup", []), harness_cmd))
        result = agent.run(cmd, repo, agent.environment(run_dir, extra), run_dir, float(suite.setting("timeout_s", task)))
        row["session_id"] = session_id
        row["agent_status"] = result["status"]
        session = agent.collect_transcript(session_id, repo, run_dir)
        summary = agent.summary(agent.stream_events(run_dir))
        row["claude"] = {k: v for k, v in summary.items() if k != "init"}
        row["init"] = summary.get("init")

        row["diff"] = workspace.diff_stats(repo, base, base_env, patch_out=run_dir / "agent.patch")
        command = task.test_command or suite.defaults["test_command"]
        graded = grade_mod.grade(task, repo, command, run_dir / "grade", base_env, int(suite.defaults["grade_timeout_s"]))
        row["grade"] = graded
        row["resolved"] = graded["resolved"]
        if ctx.get("delivery") and task.delivery:
            import delivery
            row["delivery"] = delivery.run(suite.delivery, task.delivery, repo, base_env, run_id)

        parsed = transcript.parse(session)
        trace_rows = read_jsonl(run_dir / "trace.jsonl")
        known = {f"replicable:{s.name}" for s in (plugin.path / "skills").iterdir()} if plugin else set()
        row.update(metrics.compute(parsed, summary, trace_rows, result["wall_ms"], known))
        row["failure_label"] = metrics.failure_label(
            resolved=row["resolved"], infra_error=result["status"] == "infra_error", agent_status=result["status"],
            summary=summary, parsed=parsed, trace_rows=trace_rows, grade=graded, diff=row["diff"],
            plugin_arm=plugin is not None)
        row["false_done"] = metrics.false_done(row["resolved"], result["status"], summary)
        if result["status"] == "infra_error":
            row["status"], row["error"] = "infra_error", result.get("error")
    except Exception as exc:  # any failure of the runner itself is an infra error, recorded like any other run
        row.update(status="infra_error", error=f"{type(exc).__name__}: {str(exc)[:300]}", resolved=False,
                   failure_label="infra_error", false_done=False)
    finally:
        if not ctx.get("keep"):
            workspace.remove(ctx["mirror"], repo)
    row["run_dir"] = str(run_dir)
    return row


# -- commands --------------------------------------------------------------------
def cmd_run(args) -> int:
    suite = suite_mod.load(args.suite)
    tier = suite.tiers.get(args.tier, {}) if args.tier else {}
    if args.tier and args.tier not in suite.tiers:
        raise SystemExit(f"unknown tier {args.tier!r}; defined: {', '.join(suite.tiers)}")
    arm_names = args.arms or tier.get("arms") or list(suite.arms)
    import report
    measured = report.difficulty(report.load_rows(suite.results),
                                 hashes={t.id: t.content_hash[:16] for t in suite.tasks.values()})
    tasks = suite.select(args.tasks, args.tier, difficulty={t: d["bucket"] for t, d in measured.items()})
    if not tasks:
        raise SystemExit("no tasks selected")
    trials = args.trials or tier.get("trials") or int(suite.defaults["trials"])
    model = args.model or suite.defaults["model"]
    effort = args.effort or suite.defaults.get("effort")
    if not model:
        raise SystemExit("set defaults.model in suite.json (a full model id) or pass --model")
    resolved = arms_mod.resolve(suite.arms, arm_names, args.candidate, args.previous)
    env_info = environment_info(model, effort)
    seed = args.seed if args.seed is not None else random.randrange(1 << 30)
    plan = [(task, resolved[name], trial) for task in tasks for name in arm_names for trial in range(1, trials + 1)]
    random.Random(seed).shuffle(plan)  # interleave arms so drift in API latency hits them alike

    history = [r for r in read_jsonl(suite.results) if r.get("status") != "infra_error"]
    done = {r.get("key") for r in history}
    todo, reused = [], []
    for task, arm, trial in plan:
        key = run_key(suite, task, arm, trial, env_info)
        (reused if key in done and not args.rerun else todo).append((task, arm, trial, key))

    print(f"suite {suite.name}: {len(tasks)} task(s) x {len(arm_names)} arm(s) x {trials} trial(s) = {len(plan)} runs; "
          f"{len(reused)} reused from earlier runs, {len(todo)} to run. seed={seed}")
    for name in arm_names:
        plugin = resolved[name].get("plugin_version")
        print(f"  arm {name:16} plugin={'none' if not plugin else plugin['version'] + ' @ ' + plugin['sha'][:10] + (' +dirty' if plugin['dirty'] else '')}"
              f"  prompt={resolved[name].get('prompt', 'raw')}  setup={','.join(resolved[name].get('setup', [])) or '-'}")
    print(f"  model={model} effort={effort or '-'} claude={env_info['claude_code_version']} graphify={env_info['graphify_version']}")
    cap = sum(float(suite.setting("budget_usd", t)) for t, _, _, _ in todo)
    costs = {}
    for r in history:
        costs.setdefault((r["task"]["id"], r["arm"]["name"]), []).append((r.get("claude") or {}).get("total_cost_usd") or 0)
    est = sum(sorted(costs[(t.id, a["name"])])[len(costs[(t.id, a["name"])]) // 2] if (t.id, a["name"]) in costs
              else float(suite.setting("budget_usd", t)) for t, a, _, _ in todo)
    print(f"  cost: at most ${cap:.2f} (per-run budget caps); estimate ${est:.2f} (median of earlier runs, else the cap)")
    if args.dry_run:
        return 0
    # The experiment's plan: every run key it covers, including reused ones, so its report can find them.
    plan_file = suite.root / "results" / "plans" / f"{args.experiment}.json"
    keys = set((read_json(plan_file) or {}).get("keys", [])) | {k for *_, k in todo} | {k for *_, k in reused}
    write_json(plan_file, {"experiment": args.experiment, "keys": sorted(keys), "updated": now_iso()})
    if not todo:
        return 0

    mirror = workspace.mirror(suite.dataset["repo"], suite.dataset["sha"], suite.name)
    spent = 0.0
    counts = {"resolved": 0, "unresolved": 0, "infra_error": 0}

    def go(item):
        task, arm, trial, key = item
        ctx = {"experiment": args.experiment, "key": key, "seed": seed, "env_info": env_info, "mirror": mirror,
               "delivery": args.delivery, "keep": args.keep_worktrees}
        return run_one(suite, task, arm, trial, ctx)

    def record(row):
        nonlocal spent
        with _lock:
            append_jsonl(suite.results, row)
            spent += float((row.get("claude") or {}).get("total_cost_usd") or 0)
            state = "infra_error" if row["status"] == "infra_error" else "resolved" if row["resolved"] else "unresolved"
            counts[state] += 1
            totals = row.get("totals") or {}
            print(f"  {state:11} {row['task']['id']:24} {row['arm']['name']:16} t{row['trial']}  "
                  f"{(totals.get('wall_ms') or 0) / 1000:6.0f}s  ${float(totals.get('cost_usd') or 0):.2f}  "
                  f"{row.get('failure_label') or ''}{'  ' + row['error'] if row.get('error') else ''}", flush=True)

    if args.concurrency <= 1:
        for item in todo:
            if args.max_cost_usd is not None and spent >= args.max_cost_usd:
                print(f"stopping: spent ${spent:.2f} >= --max-cost-usd {args.max_cost_usd}")
                break
            record(go(item))
    else:
        with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
            for row in pool.map(go, todo):
                record(row)
    print(f"done: {counts['resolved']} resolved, {counts['unresolved']} unresolved, {counts['infra_error']} infra errors; "
          f"${spent:.2f} spent. Rows appended to {suite.results}")
    return 0


def cmd_validate(args) -> int:
    suite = suite_mod.load(args.suite)
    tasks = suite.select(args.tasks)
    print(f"suite {suite.name}: {len(tasks)} task(s), dataset {suite.dataset['repo']} @ {suite.dataset['sha'][:12]}")
    meta = suite.issues_meta
    print(f"  issues: {meta['issues']} frozen ({meta['open']} open when frozen), last update {meta['updated_at']}"
          if meta else "  issues: none frozen (bench/run.py snapshot-issues <numbers>)")
    if not args.grade:
        for t in tasks:
            issue = f", issue #{t.issue} ({t.issue_state} in the snapshot)" if t.issue else ""
            print(f"  ok  {t.id} (v{t.version}{', ' + t.tier if t.tier else ''}): {len(t.fail_to_pass)} FAIL_TO_PASS, "
                  f"{len(t.pass_to_pass)} PASS_TO_PASS, {len(t.hidden_files())} hidden file(s)"
                  f"{'' if t.solution.exists() else ', NO solution.patch'}{issue}")
        print("schema ok; add --grade to prove each task fails at the pinned commit and passes with its solution")
        return 0
    mirror = workspace.mirror(suite.dataset["repo"], suite.dataset["sha"], suite.name)
    env = clean_env({**workspace.BENCH_IDENTITY, **suite.defaults.get("env", {})})
    bad = 0
    for t in tasks:
        problems = validate_task(suite, t, mirror, env, args.keep_worktrees)
        bad += bool(problems)
        print(f"  {'ok ' if not problems else 'BAD'} {t.id}" + ("" if not problems else ": " + "; ".join(problems)))
    return 1 if bad else 0


def validate_task(suite, task, mirror: Path, env: dict, keep: bool = False) -> list:
    problems = []
    run_dir = home() / "validate" / suite.name / f"{task.id}-{uuid.uuid4().hex[:6]}"
    repo = run_dir / "repo"
    command = task.test_command or suite.defaults["test_command"]
    timeout = int(suite.defaults["grade_timeout_s"])
    try:
        workspace.checkout(mirror, suite.dataset["sha"], repo)
        steps = workspace.run_setup(list(suite.dataset.get("setup", [])) + list(task.setup), repo, env)
        if not all(s["ok"] for s in steps):
            return [f"setup failed: {steps[-1]['step']}"]
        before = grade_mod.grade(task, repo, command, run_dir / "base", env, timeout)
        if before["f2p"]["ok"]:
            problems.append("FAIL_TO_PASS already passes at the pinned commit")
        if not before["p2p"]["ok"]:
            problems.append("PASS_TO_PASS fails at the pinned commit")
        git(["checkout", "--", "."], repo, env=env)
        git(["clean", "-fdq"], repo, env=env)
        if not task.solution.exists():
            problems.append("no solution.patch: cannot prove the task is solvable")
        elif not grade_mod.apply_patch(task.solution, repo, env):
            problems.append("solution.patch does not apply at the pinned commit")
        else:
            after = grade_mod.grade(task, repo, command, run_dir / "solution", env, timeout)
            if not after["resolved"]:
                problems.append("the solution does not resolve the task "
                                f"(FAIL_TO_PASS ok={after['f2p']['ok']}, PASS_TO_PASS ok={after['p2p']['ok']})")
    except Exception as exc:
        problems.append(f"{type(exc).__name__}: {str(exc)[:200]}")
    finally:
        if not keep:
            workspace.remove(mirror, repo)
    return problems


def cmd_report(args) -> int:
    import report
    suite = suite_mod.load(args.suite)
    keys = report.plan_keys(suite.root / "results" / "plans" / f"{args.experiment}.json") if args.experiment else None
    data = report.build(report.load_rows(suite.results, suite.labels), experiment=args.experiment,
                        pairs=args.compare, k=args.k, plan_keys=keys)
    md = report.markdown(data)
    if args.md:
        Path(args.md).write_text(md)
        print(f"wrote {args.md}")
    if args.html:
        Path(args.html).write_text(report.html(data, title=f"{suite.name} benchmark"))
        print(f"wrote {args.html}")
    if not args.md and not args.html:
        print(md)
    return 0


def cmd_new_experiment(args) -> int:
    root = Path(args.suite).resolve()
    exp_dir = root / "experiments"
    exp_dir.mkdir(parents=True, exist_ok=True)
    numbers = [int(p.name[:4]) for p in exp_dir.glob("[0-9][0-9][0-9][0-9]-*.md")]
    number = f"{(max(numbers) + 1) if numbers else 0:04d}"
    exp_id = f"{number}-{args.slug}"
    head = git(["rev-parse", "HEAD"], PLUGIN_ROOT)
    version = arms_mod._manifest_version(PLUGIN_ROOT)
    template = (BENCH_DIR / "templates" / "experiment.md").read_text()
    text = (template.replace("{id}", exp_id).replace("{date}", time.strftime("%Y-%m-%d"))
            .replace("{hypothesis}", args.hypothesis or "<what should change, and why>")
            .replace("{plugin_sha}", head[:12]).replace("{plugin_version}", version))
    path = exp_dir / f"{exp_id}.md"
    if path.exists():
        raise SystemExit(f"{path} already exists")
    path.write_text(text)
    index = exp_dir / "README.md"
    if not index.exists():
        index.write_text("# Experiments\n\nOne line per experiment, newest last. Verdicts: keep, revert, inconclusive.\n\n")
    with index.open("a") as fh:
        fh.write(f"- [{exp_id}]({exp_id}.md) — {time.strftime('%Y-%m-%d')} — open — {args.hypothesis or ''}\n")
    print(f"created {path}\nrun it with: python3 {BENCH_DIR / 'run.py'} run --suite {root} --experiment {exp_id}")
    return 0


def cmd_label(args) -> int:
    suite = suite_mod.load(args.suite)
    allowed = set(metrics.FAILURE_LABELS) | {"none"}
    if args.label not in allowed:
        raise SystemExit(f"label must be one of: {', '.join(sorted(allowed))}")
    if not any(r.get("run_id") == args.run_id for r in read_jsonl(suite.results)):
        raise SystemExit(f"no run {args.run_id} in {suite.results}")
    append_jsonl(suite.labels, {"run_id": args.run_id, "label": args.label, "note": args.note or "", "ts": now_iso()})
    print(f"labelled {args.run_id} as {args.label}")
    return 0


def cmd_snapshot_issues(args) -> int:
    import issues as issues_mod
    root = Path(args.suite).resolve()
    data = read_json(root / "suite.json") or {}
    repo = args.repo or issues_mod.repo_slug((data.get("dataset") or {}).get("repo", ""))
    if not repo:
        raise SystemExit("cannot tell the GitHub repository from dataset.repo; pass --repo owner/name")
    numbers = args.numbers or issues_mod.referenced(root)
    if not numbers:
        raise SystemExit("name the issues to freeze (e.g. 826 70), or reference them from tasks first")
    out = issues_mod.snapshot(repo, root, numbers, refresh=args.refresh)
    fmt = lambda ns: ", ".join(f"#{n}" for n in ns) or "none"
    print(f"{repo}: added {fmt(out['added'])}; refreshed {fmt(out['refreshed'])}; kept as frozen {fmt(out['kept'])}. "
          f"{out['meta']['issues']} issue(s) in {root / 'dataset' / 'issues.jsonl'}")
    return 0


def cmd_init_suite(args) -> int:
    dest = Path(args.dir).resolve()
    if dest.exists() and any(dest.iterdir()):
        raise SystemExit(f"{dest} is not empty")
    shutil.copytree(BENCH_DIR / "templates" / "suite", dest, dirs_exist_ok=True)
    suite_json = dest / "suite.json"
    text = suite_json.read_text().replace("{repo}", args.repo).replace("{sha}", args.sha).replace("{name}", args.name or dest.name)
    suite_json.write_text(text)
    (dest / "results").mkdir(exist_ok=True)
    (dest / "results" / "runs.jsonl").touch()
    print(f"created {dest}. Next: write tasks under tasks/, then `run.py validate --suite {dest} --grade`.")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("run", help="run tasks under arms and append one row per run")
    p.add_argument("--suite", required=True)
    p.add_argument("--tier")
    p.add_argument("--arms", nargs="+")
    p.add_argument("--tasks", nargs="+", help="task id globs")
    p.add_argument("--trials", type=int)
    p.add_argument("--model")
    p.add_argument("--effort")
    p.add_argument("--experiment", default="adhoc")
    p.add_argument("--candidate", default="candidate", help="plugin ref for 'candidate' arms (default: working tree)")
    p.add_argument("--previous", default="previous", help="plugin ref for 'previous' arms (default: newest earlier v* tag)")
    p.add_argument("--delivery", action="store_true", help="also run the local delivery proxy (docker build, compose tests)")
    p.add_argument("--max-cost-usd", type=float)
    p.add_argument("--concurrency", type=int, default=1)
    p.add_argument("--seed", type=int)
    p.add_argument("--rerun", action="store_true", help="run even when an identical earlier run exists")
    p.add_argument("--keep-worktrees", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(fn=cmd_run)

    p = sub.add_parser("validate", help="check the suite; with --grade, prove every task is sound")
    p.add_argument("--suite", required=True)
    p.add_argument("--tasks", nargs="+")
    p.add_argument("--grade", action="store_true")
    p.add_argument("--keep-worktrees", action="store_true")
    p.set_defaults(fn=cmd_validate)

    p = sub.add_parser("report", help="aggregate results: Markdown to stdout, or --md / --html files")
    p.add_argument("--suite", required=True)
    p.add_argument("--experiment")
    p.add_argument("--compare", nargs="+", help="arm pairs as A:B (default: candidate-* vs previous-* and baseline)")
    p.add_argument("--k", type=int, help="k for pass^k (default: the smallest trial count)")
    p.add_argument("--md")
    p.add_argument("--html")
    p.set_defaults(fn=cmd_report)

    p = sub.add_parser("new-experiment", help="start an experiment record")
    p.add_argument("--suite", required=True)
    p.add_argument("slug")
    p.add_argument("--hypothesis")
    p.set_defaults(fn=cmd_new_experiment)

    p = sub.add_parser("label", help="override a run's failure label after reading its transcript")
    p.add_argument("--suite", required=True)
    p.add_argument("run_id")
    p.add_argument("label")
    p.add_argument("--note")
    p.set_defaults(fn=cmd_label)

    p = sub.add_parser("snapshot-issues", help="freeze chosen GitHub issues of the dataset into the suite (read-only)")
    p.add_argument("--suite", required=True)
    p.add_argument("numbers", nargs="*", type=int, help="issue numbers (default: those the tasks reference)")
    p.add_argument("--repo", help="owner/name (default: from dataset.repo)")
    p.add_argument("--refresh", action="store_true", help="re-fetch issues that are already frozen")
    p.set_defaults(fn=cmd_snapshot_issues)

    p = sub.add_parser("init-suite", help="create a suite skeleton")
    p.add_argument("dir")
    p.add_argument("--repo", required=True, help="the dataset's git URL or path")
    p.add_argument("--sha", required=True, help="the full commit to pin")
    p.add_argument("--name")
    p.set_defaults(fn=cmd_init_suite)

    args = parser.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
