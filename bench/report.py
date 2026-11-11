"""Aggregate results rows into the numbers an experiment record and the dashboard show.

pass@1   mean over trials of "resolved", averaged over tasks
pass^k   chance that k independent trials all resolve: C(c, k) / C(n, k) per task, averaged
pass@k   chance that at least one of k trials resolves: 1 - C(n - c, k) / C(n, k)
deltas   paired by task (A - B), with a 95% bootstrap interval that resamples tasks, then trials

Rows are the latest per run key; infrastructure errors are left out of every
score and counted on their own. Manual labels (results/labels.jsonl) override
the automatic failure label.
"""

from __future__ import annotations

import random
from collections import Counter, defaultdict
from math import comb
from pathlib import Path
from statistics import mean, median
from typing import Dict, List, Optional, Sequence, Tuple

from common import percentile, read_json, read_jsonl

BOOTSTRAP = 2000


def load_rows(results: Path, labels: Optional[Path] = None, plan: Optional[Path] = None) -> List[dict]:
    rows = read_jsonl(results)
    overrides = {r["run_id"]: r for r in read_jsonl(labels)} if labels else {}
    for row in rows:
        o = overrides.get(row.get("run_id"))
        if o:
            row["auto_label"] = row.get("failure_label")
            row["failure_label"] = None if o["label"] == "none" else o["label"]
            row["label_note"] = o.get("note")
    return rows


def latest_per_key(rows: List[dict], keys: Optional[set] = None) -> Tuple[List[dict], List[dict]]:
    good, infra = {}, []
    for row in rows:
        if keys is not None and row.get("key") not in keys:
            continue
        if row.get("status") == "infra_error":
            infra.append(row)
            continue
        good[row.get("key") or row.get("run_id")] = row  # later rows win
    return list(good.values()), infra


def pass_hat_k(n: int, c: int, k: int) -> Optional[float]:
    return comb(c, k) / comb(n, k) if n >= k > 0 else None


def pass_at_k(n: int, c: int, k: int) -> Optional[float]:
    return 1 - comb(n - c, k) / comb(n, k) if n >= k > 0 else None


def _med(values: Sequence) -> Optional[float]:
    vals = [v for v in values if v is not None]
    return median(vals) if vals else None


def _cost(row) -> float:
    return float((row.get("totals") or {}).get("cost_usd") or 0.0)


def _wall(row) -> float:
    return float((row.get("totals") or {}).get("wall_ms") or 0.0) / 1000


def _tokens(t: dict) -> int:
    return sum(int(t.get(k) or 0) for k in ("input", "cache_read", "cache_write", "output"))


def cells(rows: List[dict]) -> Dict[Tuple[str, str], List[dict]]:
    out = defaultdict(list)
    for row in rows:
        out[(row["arm"]["name"], row["task"]["id"])].append(row)
    return out


def bootstrap(per_task: Dict[str, Tuple[List[float], List[float]]], stat, seed: int = 7) -> Tuple[float, float, float]:
    """Paired difference of ``stat`` (A - B) averaged over tasks, with a 95% interval."""
    tasks = sorted(per_task)
    if not tasks:
        return (float("nan"),) * 3

    def value(sample_tasks, rng=None):
        diffs = []
        for t in sample_tasks:
            a, b = per_task[t]
            if rng is not None:
                a = [rng.choice(a) for _ in a]
                b = [rng.choice(b) for _ in b]
            diffs.append(stat(a) - stat(b))
        return mean(diffs)

    point = value(tasks)
    rng = random.Random(seed)
    samples = sorted(value([rng.choice(tasks) for _ in tasks], rng) for _ in range(BOOTSTRAP))
    return point, percentile(samples, 0.025), percentile(samples, 0.975)


def default_pairs(arm_names: List[str]) -> List[Tuple[str, str]]:
    pairs = []
    for name in arm_names:
        if name.startswith("candidate-"):
            twin = "previous-" + name[len("candidate-"):]
            if twin in arm_names:
                pairs.append((name, twin))
            if "baseline" in arm_names:
                pairs.append((name, "baseline"))
    return pairs


def efficiency(rows: List[dict]) -> dict:
    n = len(rows)
    skills, tools, subagents, phooks, chooks = (defaultdict(list) for _ in range(5))
    for row in rows:
        for name, s in (row.get("skills") or {}).items():
            skills[name].append(s)
        for name, t in (row.get("tools") or {}).items():
            tools[name].append(t)
        for name, s in (row.get("subagents") or {}).items():
            subagents[name].append(s)
        for name, h in (row.get("plugin_hooks") or {}).items():
            phooks[name].append(h)
        for name, h in (row.get("hooks") or {}).items():
            chooks[name].append(h)

    def per_run(items, f):
        return round(sum(f(i) for i in items) / n, 3) if n else None

    out = {"runs": n, "skills": {}, "tools": {}, "subagents": {}, "plugin_hooks": {}, "hooks": {}}
    for name, items in sorted(skills.items()):
        out["skills"][name] = {
            "used_in": round(sum(1 for i in items if i.get("invocations") or name == "(no skill)") / n, 3),
            "invocations_per_run": per_run(items, lambda i: i.get("invocations", 0)),
            "wall_s_per_run": per_run(items, lambda i: i.get("wall_ms", 0) / 1000),
            "cost_per_run": per_run(items, lambda i: i.get("cost_usd", 0)),
            "tokens_per_run": per_run(items, _tokens),
            "output_per_run": per_run(items, lambda i: i.get("output", 0)),
            "tool_calls_per_run": per_run(items, lambda i: i.get("tool_calls", 0)),
        }
    for name, items in sorted(tools.items()):
        out["tools"][name] = {
            "calls_per_run": per_run(items, lambda i: i.get("calls", 0)),
            "errors_per_run": per_run(items, lambda i: i.get("errors", 0)),
            "s_per_run": per_run(items, lambda i: ((i.get("ms") or {}).get("total") or 0) / 1000),
            "p95_ms": _med([(i.get("ms") or {}).get("p95") for i in items]),
            "hook_s_per_run": per_run(items, lambda i: (i.get("hook_ms") or 0) / 1000),
            "out_tokens_per_run": per_run(items, lambda i: i.get("out_tokens", 0)),
            "result_tokens_per_run": per_run(items, lambda i: i.get("result_tokens", 0)),
            "rent_tokens_per_run": per_run(items, lambda i: i.get("rent_tokens", 0)),
        }
    for name, items in sorted(subagents.items()):
        out["subagents"][name] = {
            "count_per_run": per_run(items, lambda i: i.get("count", 0)),
            "wall_s_per_run": per_run(items, lambda i: i.get("wall_ms", 0) / 1000),
            "cost_per_run": per_run(items, lambda i: i.get("cost_usd", 0)),
            "tokens_per_run": per_run(items, _tokens),
        }
    for name, items in sorted(phooks.items()):
        outcomes = Counter()
        for i in items:
            outcomes.update(i.get("outcomes") or {})
        out["plugin_hooks"][name] = {
            "runs_per_run": per_run(items, lambda i: i.get("runs", 0)),
            "p50_ms": _med([(i.get("ms") or {}).get("p50") for i in items]),
            "p95_ms": _med([(i.get("ms") or {}).get("p95") for i in items]),
            "s_per_run": per_run(items, lambda i: ((i.get("ms") or {}).get("total") or 0) / 1000),
            "context_chars_per_run": per_run(items, lambda i: i.get("context_chars", 0)),
            "outcomes": dict(outcomes.most_common()),
        }
    for name, items in sorted(chooks.items()):
        out["hooks"][name] = {
            "runs_per_run": per_run(items, lambda i: i.get("runs", 0)),
            "errors_per_run": per_run(items, lambda i: i.get("errors", 0)),
            "p95_ms": _med([(i.get("ms") or {}).get("p95") for i in items]),
            "s_per_run": per_run(items, lambda i: ((i.get("ms") or {}).get("total") or 0) / 1000),
            "context_chars_per_run": per_run(items, lambda i: i.get("context_chars", 0)),
        }
    gates = [row.get("gate") or {} for row in rows]
    out["gate"] = {
        "blocks_per_run": round(sum(g.get("blocks", 0) for g in gates) / n, 3) if n else None,
        "cap_hit_rate": round(sum(1 for g in gates if g.get("cap_hit")) / n, 3) if n else None,
        "fixed_after_block_rate": round(sum(1 for g in gates if g.get("fixed_after_block")) / n, 3) if n else None,
        "runs_with_gate": sum(1 for g in gates if g.get("runs")),
    }
    return out


def build(rows: List[dict], experiment: Optional[str] = None, pairs: Optional[List[str]] = None,
          k: Optional[int] = None, plan_keys: Optional[set] = None) -> dict:
    if experiment and plan_keys is None:
        plan_keys = {r.get("key") for r in rows if r.get("experiment") == experiment}
    good, infra = latest_per_key(rows, plan_keys)
    grid = cells(good)
    arm_names = sorted({a for a, _ in grid}, key=lambda a: (a != "baseline", a))
    task_ids = sorted({t for _, t in grid})
    trials = [len(v) for v in grid.values()]
    k = k or (min(trials) if trials else 1)
    tier_of = {r["task"]["id"]: r["task"].get("tier") for r in good}

    arms = {}
    for arm in arm_names:
        arm_rows = [r for r in good if r["arm"]["name"] == arm]
        per_task = {}
        for t in task_ids:
            rs = grid.get((arm, t), [])
            if not rs:
                continue
            c = sum(1 for r in rs if r.get("resolved"))
            per_task[t] = {"n": len(rs), "c": c, "pass1": c / len(rs), "pass_hat_k": pass_hat_k(len(rs), c, k),
                           "pass_at_k": pass_at_k(len(rs), c, k), "median_cost": _med([_cost(r) for r in rs]),
                           "median_wall_s": _med([_wall(r) for r in rs]),
                           "labels": dict(Counter(r.get("failure_label") for r in rs if r.get("failure_label")))}
        first = next((r for r in arm_rows if r["arm"].get("plugin")), None)
        plugin = first["arm"]["plugin"] if first else None
        hats = [v["pass_hat_k"] for v in per_task.values() if v["pass_hat_k"] is not None]
        ats = [v["pass_at_k"] for v in per_task.values() if v["pass_at_k"] is not None]
        arms[arm] = {
            "plugin": plugin, "prompt": arm_rows[0]["arm"].get("prompt") if arm_rows else None,
            "runs": len(arm_rows), "tasks": len(per_task),
            "pass1": mean(v["pass1"] for v in per_task.values()) if per_task else None,
            "pass_hat_k": mean(hats) if hats else None, "pass_at_k": mean(ats) if ats else None,
            "false_done_rate": (sum(1 for r in arm_rows if r.get("false_done")) / len(arm_rows)) if arm_rows else None,
            "median_cost": _med([_cost(r) for r in arm_rows]), "total_cost": round(sum(_cost(r) for r in arm_rows), 4),
            "median_wall_s": _med([_wall(r) for r in arm_rows]),
            "median_tokens": _med([_tokens((r.get("totals") or {}).get("tokens") or {}) for r in arm_rows]),
            "median_turns": _med([(r.get("totals") or {}).get("turns") for r in arm_rows]),
            "median_context_peak": _med([(r.get("totals") or {}).get("context_peak") for r in arm_rows]),
            "p2p_broken_per_run": (sum((r.get("grade") or {}).get("p2p_broken") or 0 for r in arm_rows) / len(arm_rows))
            if arm_rows else None,
            "labels": dict(Counter(r.get("failure_label") for r in arm_rows if r.get("failure_label")).most_common()),
            "infra_errors": sum(1 for r in infra if r["arm"]["name"] == arm),
            "per_task": per_task,
            "by_tier": _by_tier(per_task, tier_of),
            "efficiency": efficiency(arm_rows),
            "delivery": _delivery(arm_rows),
        }

    comparisons = []
    wanted = [tuple(p.split(":", 1)) for p in pairs] if pairs else default_pairs(arm_names)
    for a, b in wanted:
        common = [t for t in task_ids if grid.get((a, t)) and grid.get((b, t))]
        if not common:
            continue
        res = {t: ([1.0 if r.get("resolved") else 0.0 for r in grid[(a, t)]],
                   [1.0 if r.get("resolved") else 0.0 for r in grid[(b, t)]]) for t in common}
        cost = {t: ([_cost(r) for r in grid[(a, t)]], [_cost(r) for r in grid[(b, t)]]) for t in common}
        wall = {t: ([_wall(r) for r in grid[(a, t)]], [_wall(r) for r in grid[(b, t)]]) for t in common}
        per_task = []
        for t in common:
            pa, pb = mean(res[t][0]), mean(res[t][1])
            per_task.append({"task": t, "tier": tier_of.get(t), "a_pass1": pa, "b_pass1": pb, "delta": pa - pb,
                             "a_cost": _med(cost[t][0]), "b_cost": _med(cost[t][1]),
                             "a_wall_s": _med(wall[t][0]), "b_wall_s": _med(wall[t][1]),
                             "verdict": "better" if pa > pb else "worse" if pa < pb else "same"})
        comparisons.append({
            "a": a, "b": b, "tasks": len(common),
            "pass1": bootstrap(res, mean), "cost": bootstrap(cost, median), "wall_s": bootstrap(wall, median),
            "regressions": [p["task"] for p in per_task if p["verdict"] == "worse"],
            "improvements": [p["task"] for p in per_task if p["verdict"] == "better"],
            "per_task": per_task,
        })

    return {"experiment": experiment, "k": k, "arms": arms, "arm_names": arm_names, "tasks": task_ids,
            "tiers": tier_of, "comparisons": comparisons, "infra_errors": len(infra), "rows": len(good),
            "trend": trend(rows), "env": _env(good), "runs": _run_list(good + infra)}


def _by_tier(per_task: dict, tier_of: dict) -> dict:
    groups = defaultdict(list)
    for t, v in per_task.items():
        groups[tier_of.get(t) or "?"].append(v["pass1"])
    return {tier: round(mean(v), 4) for tier, v in sorted(groups.items())}


def _delivery(rows: List[dict]) -> Optional[dict]:
    d = [r["delivery"] for r in rows if r.get("delivery")]
    if not d:
        return None
    return {"runs": len(d), "ok_rate": sum(1 for x in d if x.get("ok")) / len(d),
            "median_build_s": _med([x.get("build_ms", 0) / 1000 for x in d]),
            "median_integration_s": _med([x.get("integration_ms", 0) / 1000 for x in d]),
            "median_image_mb": _med([sum(b.get("image_bytes") or 0 for b in x.get("builds", [])) / 1e6 for x in d])}


def _env(rows: List[dict]) -> dict:
    out = {}
    for key in ("model", "claude_code_version", "graphify_version", "dataset_sha", "effort"):
        out[key] = sorted({str((r.get("env") or {}).get(key)) for r in rows})
    return out


def _run_list(rows: List[dict]) -> List[dict]:
    out = []
    for r in sorted(rows, key=lambda r: r.get("ts") or ""):
        t = r.get("totals") or {}
        out.append({"run_id": r.get("run_id"), "ts": r.get("ts"), "task": r["task"]["id"], "arm": r["arm"]["name"],
                    "trial": r.get("trial"), "status": r.get("status"), "resolved": r.get("resolved"),
                    "label": r.get("failure_label"), "false_done": r.get("false_done"),
                    "cost": t.get("cost_usd"), "wall_s": (t.get("wall_ms") or 0) / 1000, "turns": t.get("turns"),
                    "run_dir": r.get("run_dir"), "error": r.get("error")})
    return out


def trend(rows: List[dict]) -> List[dict]:
    """pass@1, cost and time per plugin version (and the baseline), in the order versions were first run."""
    groups = defaultdict(list)
    for r in rows:
        if r.get("status") == "infra_error":
            continue
        plugin = r["arm"].get("plugin")
        label = "baseline" if not plugin else f"{plugin['version']}@{plugin['sha'][:7]}{'+dirty' if plugin.get('dirty') else ''}"
        series = "baseline" if not plugin else r["arm"].get("prompt", "raw")
        groups[(series, label, (plugin or {}).get("content_hash"))].append(r)
    out = []
    for (series, label, _), rs in groups.items():
        by_task = defaultdict(list)
        for r in rs:
            by_task[r["task"]["id"]].append(1.0 if r.get("resolved") else 0.0)
        out.append({"series": series, "version": label, "first_ts": min(r.get("ts") or "" for r in rs),
                    "runs": len(rs), "tasks": len(by_task),
                    "pass1": mean(mean(v) for v in by_task.values()),
                    "median_cost": _med([_cost(r) for r in rs]), "median_wall_s": _med([_wall(r) for r in rs])})
    return sorted(out, key=lambda x: (x["first_ts"], x["series"]))


# -- Markdown ----------------------------------------------------------------------
def _f(x, digits=2, pct=False):
    if x is None or x != x:
        return "–"
    return f"{x * 100:.0f}%" if pct else f"{x:.{digits}f}"


def _ci(t, pct=False, digits=2):
    point, lo, hi = t
    if pct:
        return f"{point * 100:+.0f} pts [{lo * 100:+.0f}, {hi * 100:+.0f}]"
    return f"{point:+.{digits}f} [{lo:+.{digits}f}, {hi:+.{digits}f}]"


def markdown(data: dict) -> str:
    k = data["k"]
    lines = [f"# Benchmark report{' — ' + data['experiment'] if data['experiment'] else ''}", ""]
    env = data["env"]
    lines += [f"{data['rows']} scored runs over {len(data['tasks'])} task(s); {data['infra_errors']} infrastructure "
              f"error(s) left out. Model {', '.join(env.get('model', []))}; Claude Code "
              f"{', '.join(env.get('claude_code_version', []))}; graphify {', '.join(env.get('graphify_version', []))}; "
              f"dataset {', '.join(s[:12] for s in env.get('dataset_sha', []))}.", ""]
    lines += ["## Arms", "", f"| Arm | Plugin | Prompt | Runs | pass@1 | pass^{k} | False done | Median cost | "
              "Median time | Median tokens | Infra errors |", "|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for name in data["arm_names"]:
        a = data["arms"][name]
        plugin = "none" if not a["plugin"] else f"{a['plugin']['version']} @ {a['plugin']['sha'][:7]}"
        lines.append(f"| {name} | {plugin} | {a['prompt']} | {a['runs']} | {_f(a['pass1'], pct=True)} | "
                     f"{_f(a['pass_hat_k'], pct=True)} | {_f(a['false_done_rate'], pct=True)} | ${_f(a['median_cost'])} | "
                     f"{_f(a['median_wall_s'], 0)} s | {_f(a['median_tokens'], 0)} | {a['infra_errors']} |")
    if data["comparisons"]:
        lines += ["", "## Comparisons (A − B, paired by task, 95% bootstrap interval)", "",
                  "| A | B | Tasks | pass@1 | Median cost ($) | Median time (s) | Better | Worse |",
                  "|---|---|---:|---|---|---|---|---|"]
        for c in data["comparisons"]:
            lines.append(f"| {c['a']} | {c['b']} | {c['tasks']} | {_ci(c['pass1'], pct=True)} | {_ci(c['cost'])} | "
                         f"{_ci(c['wall_s'], digits=0)} | {', '.join(c['improvements']) or '–'} | "
                         f"{', '.join(c['regressions']) or '–'} |")
    lines += ["", "## Per task (pass@1, median cost)", "", "| Task | Tier | " + " | ".join(data["arm_names"]) + " |",
              "|---|---|" + "---:|" * len(data["arm_names"])]
    for t in data["tasks"]:
        cellv = []
        for name in data["arm_names"]:
            v = data["arms"][name]["per_task"].get(t)
            cellv.append("–" if not v else f"{v['c']}/{v['n']} · ${_f(v['median_cost'])}")
        lines.append(f"| {t} | {data['tiers'].get(t) or ''} | " + " | ".join(cellv) + " |")
    lines += ["", "## Failure labels", "", "| Arm | " + " | ".join(["label", "runs"]) + " |", "|---|---|---:|"]
    for name in data["arm_names"]:
        for label, n in data["arms"][name]["labels"].items():
            lines.append(f"| {name} | {label} | {n} |")
    for name in data["arm_names"]:
        e = data["arms"][name]["efficiency"]
        lines += ["", f"## Efficiency: {name}", "", "Per run, averaged over the arm's runs.", "",
                  "| Skill | Used in | Time (s) | Cost ($) | Tokens | Output tokens | Tool calls |",
                  "|---|---:|---:|---:|---:|---:|---:|"]
        for s, v in e["skills"].items():
            lines.append(f"| {s} | {_f(v['used_in'], pct=True)} | {_f(v['wall_s_per_run'], 1)} | {_f(v['cost_per_run'], 3)} | "
                         f"{_f(v['tokens_per_run'], 0)} | {_f(v['output_per_run'], 0)} | {_f(v['tool_calls_per_run'], 1)} |")
        lines += ["", "| Tool | Calls | Errors | Time (s) | p95 (ms) | Hook time (s) | Output tok | Result tok | Rent tok |",
                  "|---|---:|---:|---:|---:|---:|---:|---:|---:|"]
        for tname, v in sorted(e["tools"].items(), key=lambda kv: -(kv[1].get("s_per_run") or 0)):
            if ":" in tname or tname == "(text)":
                continue
            lines.append(f"| {tname} | {_f(v['calls_per_run'], 1)} | {_f(v['errors_per_run'], 1)} | {_f(v['s_per_run'], 1)} | "
                         f"{_f(v['p95_ms'], 0)} | {_f(v['hook_s_per_run'], 1)} | {_f(v['out_tokens_per_run'], 0)} | "
                         f"{_f(v['result_tokens_per_run'], 0)} | {_f(v['rent_tokens_per_run'], 0)} |")
        if e["subagents"]:
            lines += ["", "| Subagent | Count | Time (s) | Cost ($) | Tokens |", "|---|---:|---:|---:|---:|"]
            for s, v in e["subagents"].items():
                lines.append(f"| {s} | {_f(v['count_per_run'], 1)} | {_f(v['wall_s_per_run'], 1)} | "
                             f"{_f(v['cost_per_run'], 3)} | {_f(v['tokens_per_run'], 0)} |")
        if e["plugin_hooks"]:
            lines += ["", "| Plugin hook | Runs | p50 (ms) | p95 (ms) | Time (s) | Context chars | Outcomes |",
                      "|---|---:|---:|---:|---:|---:|---|"]
            for h, v in e["plugin_hooks"].items():
                outcomes = ", ".join(f"{o} ×{n}" for o, n in list(v["outcomes"].items())[:6])
                lines.append(f"| {h} | {_f(v['runs_per_run'], 1)} | {_f(v['p50_ms'], 0)} | {_f(v['p95_ms'], 0)} | "
                             f"{_f(v['s_per_run'], 1)} | {_f(v['context_chars_per_run'], 0)} | {outcomes} |")
        g = e["gate"]
        if g.get("runs_with_gate"):
            lines += ["", f"Test gate: {_f(g['blocks_per_run'], 1)} blocks per run, cap hit in "
                      f"{_f(g['cap_hit_rate'], pct=True)} of runs, fixed after a block in {_f(g['fixed_after_block_rate'], pct=True)}."]
        d = data["arms"][name]["delivery"]
        if d:
            lines += ["", f"Delivery proxy: {_f(d['ok_rate'], pct=True)} ok over {d['runs']} runs; median build "
                      f"{_f(d['median_build_s'], 0)} s, integration {_f(d['median_integration_s'], 0)} s, image "
                      f"{_f(d['median_image_mb'], 0)} MB."]
    if data["trend"]:
        lines += ["", "## Trend across plugin versions (every row ever recorded)", "",
                  "| Series | Version | First run | Runs | Tasks | pass@1 | Median cost | Median time |",
                  "|---|---|---|---:|---:|---:|---:|---:|"]
        for t in data["trend"]:
            lines.append(f"| {t['series']} | {t['version']} | {t['first_ts'][:10]} | {t['runs']} | {t['tasks']} | "
                         f"{_f(t['pass1'], pct=True)} | ${_f(t['median_cost'])} | {_f(t['median_wall_s'], 0)} s |")
    return "\n".join(lines) + "\n"


def html(data: dict, title: str) -> str:
    import dashboard
    return dashboard.render(data, title)


def plan_keys(path: Path) -> Optional[set]:
    data = read_json(path)
    return set(data.get("keys", [])) if isinstance(data, dict) else None
