"""Per-run metrics: totals, attribution to tools / skills / subagents / hooks, and the failure label.

Attribution (docs/benchmarking.md#attribution; estimates are marked as such):

tool time      tool_result time - tool_use time, minus the hooks that ran for that call
tool tokens    out:    the issuing request's output tokens, split evenly over its tool calls
               result: the growth of the next request's context, split over the results that
                       arrived in between by size (estimate; falls back to chars / 4)
               rent:   result tokens x the number of later requests in the same context, the
                       re-reading a large output costs for the rest of the run (estimate)
skill span     from a skill's start (slash command or Skill call) to the next skill's start or
               the end of the run; every request, tool call and subagent inside it counts
cost           each model's reported cost, split over its requests by price-weighted tokens
"""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import Dict, List, Optional

from common import percentile

METRICS_VERSION = "1"
NO_SKILL = "(no skill)"
TEXT_ONLY = "(text)"
# Relative price of each token kind (input = 1), the same across current Claude models.
WEIGHTS = {"input": 1.0, "cache_write_5m": 1.25, "cache_write_1h": 2.0, "cache_read": 0.1, "output": 5.0}

FAILURE_LABELS = ("infra_error", "timeout", "budget_exhausted", "context_exhausted", "hook_error", "hook_timeout",
                  "graphify_unavailable", "gate_cap_hit", "agent_gave_up", "broke_other_tests", "incomplete",
                  "wrong_fix")


def context_size(req: dict) -> int:
    return req["input"] + req["cache_read"] + req["cache_write"]


def weight(req: dict) -> float:
    cw_1h = min(req["cache_write_1h"], req["cache_write"])
    return (req["input"] * WEIGHTS["input"] + (req["cache_write"] - cw_1h) * WEIGHTS["cache_write_5m"]
            + cw_1h * WEIGHTS["cache_write_1h"] + req["cache_read"] * WEIGHTS["cache_read"]
            + req["output"] * WEIGHTS["output"])


def price_requests(requests: List[dict], models: Dict[str, dict], total_cost: Optional[float]) -> None:
    """Set req['cost'] so that each model's requests add up to the cost Claude Code reported for it."""
    by_model = defaultdict(list)
    for req in requests:
        by_model[req.get("model")].append(req)
    priced = 0.0
    unpriced = []
    for model, reqs in by_model.items():
        reported = (models.get(model) or {}).get("costUSD") if model else None
        total_w = sum(weight(r) for r in reqs)
        if reported is None or not total_w:
            unpriced += reqs
            continue
        for r in reqs:
            r["cost"] = reported * weight(r) / total_w
        priced += reported
    rest = max((total_cost or 0.0) - priced, 0.0)
    total_w = sum(weight(r) for r in unpriced)
    for r in unpriced:
        r["cost"] = rest * weight(r) / total_w if total_w else 0.0


def _stats(values: List[float]) -> dict:
    vals = [v for v in values if v is not None]
    return {"n": len(vals), "total": round(sum(vals), 3), "p50": _r(percentile(vals, 0.5)),
            "p95": _r(percentile(vals, 0.95)), "max": _r(max(vals) if vals else None)}


def _r(x):
    return None if x is None else round(x, 3)


def _bucket():
    return {"calls": 0, "errors": 0, "ms": [], "hook_ms": 0, "out_tokens": 0.0, "result_tokens": 0.0,
            "rent_tokens": 0.0, "result_chars": 0}


def attribute_tools(parsed: dict) -> dict:
    requests = parsed["requests"]
    calls = {c["id"]: c for c in parsed["tool_calls"]}
    hook_ms = defaultdict(int)
    for h in parsed["hooks"]:
        if h.get("tool_use_id") in calls and h.get("ms") is not None:
            hook_ms[h["tool_use_id"]] += int(h["ms"])
    context_chars = defaultdict(int)
    for c in parsed["hook_context"]:
        if c.get("tool_use_id") in calls:
            context_chars[c["tool_use_id"]] += c["chars"]

    # result tokens: per context (main chain or one subagent), from the growth between consecutive requests
    for call in calls.values():
        call["out_tokens"] = 0.0
        call["result_tokens"] = call["result_chars"] / 4.0
        call["rent_tokens"] = 0.0
    hook_tokens = defaultdict(float)
    chains = defaultdict(list)
    for req in requests:
        chains[req["agent"]].append(req)
    for chain in chains.values():
        for i, req in enumerate(chain):
            ids = [t for t in req["tool_use_ids"] if t in calls]
            for t in ids:
                calls[t]["out_tokens"] = req["output"] / len(ids)
            if i + 1 >= len(chain) or not ids:
                continue
            growth = context_size(chain[i + 1]) - context_size(req) - req["output"]
            sizes = {t: calls[t]["result_chars"] for t in ids}
            hook_sizes = {t: context_chars.get(t, 0) for t in ids}
            total_chars = sum(sizes.values()) + sum(hook_sizes.values())
            if growth > 0 and total_chars > 0:
                for t in ids:
                    calls[t]["result_tokens"] = growth * sizes[t] / total_chars
                    hook_tokens[t] += growth * hook_sizes[t] / total_chars
            later = len(chain) - (i + 1)
            for t in ids:
                calls[t]["rent_tokens"] = (calls[t]["result_tokens"] + hook_tokens[t]) * max(later - 1, 0)

    by_tool: Dict[str, dict] = defaultdict(_bucket)
    for call in calls.values():
        for key in {call["name"], f"{call['name']}:{call['detail']}" if call.get("detail") else None} - {None}:
            b = by_tool[key]
            b["calls"] += 1
            b["errors"] += int(call["is_error"])
            interval = None
            if call["start_ts"] is not None and call["end_ts"] is not None:
                interval = max((call["end_ts"] - call["start_ts"]) * 1000 - hook_ms.get(call["id"], 0), 0)
            b["ms"].append(interval)
            b["hook_ms"] += hook_ms.get(call["id"], 0)
            b["out_tokens"] += call["out_tokens"]
            b["result_tokens"] += call["result_tokens"]
            b["rent_tokens"] += call["rent_tokens"]
            b["result_chars"] += call["result_chars"]
    text_out = sum(r["output"] for r in requests if not r["tool_use_ids"])
    out = {}
    for key, b in sorted(by_tool.items()):
        out[key] = {"calls": b["calls"], "errors": b["errors"], "ms": _stats(b["ms"]), "hook_ms": b["hook_ms"],
                    "out_tokens": round(b["out_tokens"]), "result_tokens": round(b["result_tokens"]),
                    "rent_tokens": round(b["rent_tokens"]), "result_chars": b["result_chars"]}
    out[TEXT_ONLY] = {"calls": 0, "out_tokens": text_out}
    return out


def _tokens(reqs: List[dict]) -> dict:
    return {"input": sum(r["input"] for r in reqs), "cache_read": sum(r["cache_read"] for r in reqs),
            "cache_write": sum(r["cache_write"] for r in reqs), "output": sum(r["output"] for r in reqs)}


def attribute_skills(parsed: dict, known_skills: Optional[set] = None) -> dict:
    spans = [s for s in parsed["skills"] if s.get("start_ts") is not None
             and (s["via"] == "tool" or ":" in s["skill"] or (known_skills and s["skill"] in known_skills))]
    start, end = parsed["first_ts"], parsed["last_ts"]
    if start is None or end is None:
        return {}
    bounds = []
    for i, s in enumerate(spans):
        stop = spans[i + 1]["start_ts"] if i + 1 < len(spans) else end
        bounds.append((s["skill"], s["start_ts"], stop))
    if not spans or spans[0]["start_ts"] > start:
        bounds.insert(0, (NO_SKILL, start, spans[0]["start_ts"] if spans else end))

    def owner(t):
        if t is None:
            return NO_SKILL
        for name, lo, hi in bounds:
            if lo <= t < hi:
                return name
        return bounds[-1][0] if t >= bounds[-1][2] else NO_SKILL

    agents_start = {s["agent_id"]: s["start_ts"] for s in parsed["subagents"]}
    out: Dict[str, dict] = {}
    for name, lo, hi in bounds:
        b = out.setdefault(name, {"invocations": 0, "wall_ms": 0, "requests": 0, "cost_usd": 0.0, "tool_calls": 0,
                                  "subagents": 0, **{k: 0 for k in ("input", "cache_read", "cache_write", "output")}})
        if name != NO_SKILL:
            b["invocations"] += 1
        b["wall_ms"] += round(max(hi - lo, 0) * 1000)
    for req in parsed["requests"]:
        # a subagent's requests belong to the skill that was active when the subagent started
        t = agents_start.get(req["agent"], req["start_ts"]) if req["agent"] else req["start_ts"]
        b = out[owner(t)]
        b["requests"] += 1
        b["cost_usd"] += req.get("cost", 0.0)
        for k, v in _tokens([req]).items():
            b[k] += v
    for call in parsed["tool_calls"]:
        t = agents_start.get(call["agent"], call["start_ts"]) if call["agent"] else call["start_ts"]
        out[owner(t)]["tool_calls"] += 1
    for sub in parsed["subagents"]:
        out[owner(sub["start_ts"])]["subagents"] += 1
    for b in out.values():
        b["cost_usd"] = round(b["cost_usd"], 6)
    return out


def attribute_subagents(parsed: dict) -> dict:
    out: Dict[str, dict] = {}
    for sub in parsed["subagents"]:
        reqs = [r for r in parsed["requests"] if r["agent"] == sub["agent_id"]]
        b = out.setdefault(sub["type"], {"count": 0, "wall_ms": 0, "requests": 0, "cost_usd": 0.0, "tool_calls": 0,
                                         **{k: 0 for k in ("input", "cache_read", "cache_write", "output")}})
        b["count"] += 1
        if sub["start_ts"] is not None and sub["end_ts"] is not None:
            b["wall_ms"] += round((sub["end_ts"] - sub["start_ts"]) * 1000)
        b["requests"] += len(reqs)
        b["cost_usd"] = round(b["cost_usd"] + sum(r.get("cost", 0.0) for r in reqs), 6)
        b["tool_calls"] += sum(1 for c in parsed["tool_calls"] if c["agent"] == sub["agent_id"])
        for k, v in _tokens(reqs).items():
            b[k] += v
    return out


def hook_name(name: str) -> str:
    """'SessionStart:startup' and 'SessionStart' are the same hooks; tool-scoped names keep their matcher."""
    event = name.split(":", 1)[0]
    return event if event in ("SessionStart", "SessionEnd", "UserPromptSubmit", "PreCompact") else name


def hooks_from_transcript(parsed: dict) -> dict:
    """Every hook Claude Code reports (the plugin's and graphify's), keyed by hookName. Silent runs may be missing."""
    groups: Dict[str, dict] = defaultdict(lambda: {"runs": 0, "errors": 0, "ms": [], "context_chars": 0})
    for h in parsed["hooks"]:
        g = groups[hook_name(h["name"])]
        g["runs"] += 1
        g["errors"] += int("error" in h["kind"] or "cancel" in h["kind"] or str(h.get("exit_code") or "0") not in ("0", "None"))
        g["ms"].append(h.get("ms"))
    for c in parsed["hook_context"]:
        groups[hook_name(c["name"])]["context_chars"] += c["chars"]
    return {k: {"runs": g["runs"], "errors": g["errors"], "ms": _stats(g["ms"]), "context_chars": g["context_chars"]}
            for k, g in sorted(groups.items())}


def hooks_from_trace(rows: List[dict]) -> dict:
    """The plugin's own hooks, from REPLICABLE_TRACE: every run, including silent ones, with why."""
    groups: Dict[str, dict] = defaultdict(lambda: {"runs": 0, "ms": [], "outcomes": Counter(), "chars": 0})
    for row in rows:
        g = groups[str(row.get("hook"))]
        g["runs"] += 1
        g["ms"].append(row.get("duration_ms"))
        g["outcomes"][f"{row.get('outcome')}:{row.get('reason')}"] += 1
        g["chars"] += int(row.get("chars") or 0)
    return {k: {"runs": g["runs"], "ms": _stats(g["ms"]), "outcomes": dict(g["outcomes"]), "context_chars": g["chars"]}
            for k, g in sorted(groups.items())}


def gate_summary(rows: List[dict]) -> dict:
    gate = [r for r in rows if r.get("hook") == "test_gate"]
    passes = [r for r in gate if r.get("outcome") == "pass"]
    return {
        "runs": len(gate),
        "blocks": sum(1 for r in gate if r.get("outcome") == "block"),
        "passes": len(passes),
        "cap_hit": any(r.get("reason") == "cap_hit" for r in gate),
        "fixed_after_block": any((r.get("after_blocks") or 0) > 0 for r in passes),
        "skips": dict(Counter(r.get("reason") for r in gate if r.get("outcome") == "skip")),
        "test_ms": sum(int(r.get("test_ms") or 0) for r in gate),
    }


def totals(parsed: dict, summary: dict, wall_ms: int) -> dict:
    main = [r for r in parsed["requests"] if r["agent"] is None]
    every = _tokens(parsed["requests"])  # the result event's usage covers the main thread only
    thinking = (summary.get("usage") or {}).get("thinking_tokens")
    tool_ms = 0
    for c in parsed["tool_calls"]:
        if c["agent"] is None and c["start_ts"] is not None and c["end_ts"] is not None:
            tool_ms += round((c["end_ts"] - c["start_ts"]) * 1000)
    hook_ms = sum(int(h["ms"]) for h in parsed["hooks"] if h.get("ms") is not None)
    return {
        "wall_ms": wall_ms,
        "claude_ms": summary.get("duration_ms"),
        "api_ms": summary.get("duration_api_ms"),
        "tool_ms": tool_ms,
        "hook_ms": hook_ms,
        "turns": summary.get("num_turns"),
        "requests": len(parsed["requests"]),
        "tool_calls": len(parsed["tool_calls"]),
        "cost_usd": summary.get("total_cost_usd"),
        "tokens": dict(every, thinking_main=thinking),
        "tokens_main": _tokens(main),
        "context_peak": max((context_size(r) for r in main), default=0),
        "compactions": parsed["compactions"],
        "subagents": len(parsed["subagents"]),
        "permission_denials": summary.get("permission_denials", 0),
    }


def failure_label(*, resolved: bool, infra_error: bool, agent_status: str, summary: dict, parsed: dict,
                  trace_rows: List[dict], grade: Optional[dict], diff: Optional[dict], plugin_arm: bool) -> Optional[str]:
    """One label per unresolved run, the first rule that matches (FAILURE_LABELS order)."""
    if resolved:
        return None
    if infra_error or (grade or {}).get("grader_error") or parsed.get("missing"):
        return "infra_error"
    if agent_status == "timeout":
        return "timeout"
    reason = " ".join(str(summary.get(k) or "") for k in ("subtype", "terminal_reason", "stop_reason")).lower()
    if "budget" in reason:
        return "budget_exhausted"
    if "context" in reason or "prompt_too_long" in reason or "max_tokens" in reason:
        return "context_exhausted"
    kinds = [h["kind"] for h in parsed["hooks"]]
    if any("cancel" in k or "timeout" in k for k in kinds):
        return "hook_timeout"
    if any("error" in k for k in kinds) or parsed.get("stop_hook_errors") or any(
            r.get("outcome") == "error" for r in trace_rows):
        return "hook_error"
    if plugin_arm and any(r.get("reason") == "no_graphify" for r in trace_rows):
        return "graphify_unavailable"
    if any(r.get("hook") == "test_gate" and r.get("reason") == "cap_hit" for r in trace_rows):
        return "gate_cap_hit"
    if diff is not None and diff.get("files_changed", 0) == 0:
        return "agent_gave_up"
    if grade and grade["f2p"].get("ok") and not grade["p2p"].get("ok"):
        return "broke_other_tests"
    if grade and (grade.get("f2p_pass_rate") or 0) > 0:
        return "incomplete"
    return "wrong_fix"


def false_done(resolved: bool, agent_status: str, summary: dict) -> bool:
    """The agent finished normally and believed it was done, but the task is not resolved."""
    finished = agent_status == "ok" and not summary.get("is_error") and summary.get("subtype") in (None, "success")
    return bool(finished and not resolved)


def compute(parsed: dict, summary: dict, trace_rows: List[dict], wall_ms: int,
            known_skills: Optional[set] = None) -> dict:
    price_requests(parsed["requests"], summary.get("models") or {}, summary.get("total_cost_usd"))
    return {
        "metrics_version": METRICS_VERSION,
        "parser_version": parsed.get("parser_version"),
        "totals": totals(parsed, summary, wall_ms),
        "tools": attribute_tools(parsed),
        "skills": attribute_skills(parsed, known_skills),
        "subagents": attribute_subagents(parsed),
        "hooks": hooks_from_transcript(parsed),
        "plugin_hooks": hooks_from_trace(trace_rows),
        "gate": gate_summary(trace_rows),
    }
