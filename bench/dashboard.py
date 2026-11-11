"""A self-contained HTML dashboard for a benchmark report (no network, no external scripts).

Views, all driven by one "compare A with B" filter row:
  headline   pass@1, cost, time and false-done for A, with the change against B
  per task   a dumbbell per task: B's pass@1 to A's, regressions first
  trend      pass@1 and median cost per plugin version, raw and skill prompts, baseline as reference
  efficiency per skill, tool, subagent and hook: time and tokens per run, A beside B
  failures   failure labels per arm (a table: there are more labels than colours)
  runs       every run, including infrastructure errors, with its local run directory

The page embeds report.build()'s data as JSON; every label is inserted with textContent.
"""

from __future__ import annotations

import html as html_mod
import json
import time

PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
:root {
  color-scheme: light;
  --page: #f9f9f7; --surface-1: #fcfcfb; --text-primary: #0b0b0b; --text-secondary: #52514e; --muted: #898781;
  --grid: #e1e0d9; --axis: #c3c2b7; --border: rgba(11,11,11,0.10);
  --series-1: #2a78d6; --series-2: #eb6834; --reference: #898781;
  --good: #006300; --bad: #d03b3b; --wash: rgba(42,120,214,0.08);
}
@media (prefers-color-scheme: dark) {
  :root:where(:not([data-theme="light"])) {
    color-scheme: dark;
    --page: #0d0d0d; --surface-1: #1a1a19; --text-primary: #f0efec; --text-secondary: #c3c2b7; --muted: #898781;
    --grid: #2c2c2a; --axis: #383835; --border: rgba(255,255,255,0.10);
    --series-1: #3987e5; --series-2: #d95926; --reference: #898781;
    --good: #0ca30c; --bad: #e66767; --wash: rgba(57,135,229,0.12);
  }
}
:root[data-theme="dark"] {
  color-scheme: dark;
  --page: #0d0d0d; --surface-1: #1a1a19; --text-primary: #f0efec; --text-secondary: #c3c2b7; --muted: #898781;
  --grid: #2c2c2a; --axis: #383835; --border: rgba(255,255,255,0.10);
  --series-1: #3987e5; --series-2: #d95926; --reference: #898781;
  --good: #0ca30c; --bad: #e66767; --wash: rgba(57,135,229,0.12);
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--page); color: var(--text-primary);
       font: 14px/1.45 system-ui, -apple-system, "Segoe UI", sans-serif; }
main { max-width: 1180px; margin: 0 auto; padding: 24px 16px 64px; }
h1 { font-size: 22px; margin: 0 0 4px; font-weight: 600; }
h2 { font-size: 16px; margin: 32px 0 4px; font-weight: 600; }
.sub { color: var(--text-secondary); margin: 0 0 12px; }
.meta { color: var(--muted); font-size: 12px; }
.filters { display: flex; flex-wrap: wrap; gap: 12px; align-items: center; margin: 16px 0;
           padding: 12px; background: var(--surface-1); border: 1px solid var(--border); border-radius: 8px; }
.filters label { color: var(--text-secondary); display: flex; gap: 6px; align-items: center; }
select { font: inherit; color: var(--text-primary); background: var(--page); border: 1px solid var(--border);
         border-radius: 6px; padding: 4px 8px; max-width: 220px; }
.tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(190px, 1fr)); gap: 12px; }
.tile, .card { background: var(--surface-1); border: 1px solid var(--border); border-radius: 8px; padding: 14px 16px; }
.tile .label { color: var(--text-secondary); font-size: 13px; }
.tile .value { font-size: 28px; font-weight: 600; margin-top: 2px; }
.tile .delta { font-size: 13px; color: var(--text-secondary); }
.delta.good { color: var(--good); } .delta.bad { color: var(--bad); }
.grid2 { display: grid; grid-template-columns: repeat(auto-fit, minmax(340px, 1fr)); gap: 12px; }
.card h3 { font-size: 14px; margin: 0 0 8px; font-weight: 600; }
.legend { display: flex; flex-wrap: wrap; gap: 14px; color: var(--text-secondary); font-size: 12px; margin-bottom: 6px; }
.key { display: inline-block; width: 14px; height: 2px; vertical-align: middle; margin-right: 6px; border-radius: 1px; }
.dotkey { display: inline-block; width: 8px; height: 8px; border-radius: 50%; vertical-align: middle; margin-right: 6px; }
svg { display: block; width: 100%; overflow: visible; }
svg text { fill: var(--muted); font-size: 11px; font-variant-numeric: tabular-nums; }
.scroll { overflow-x: auto; }
table { border-collapse: collapse; width: 100%; font-size: 13px; }
th, td { text-align: left; padding: 6px 8px; border-bottom: 1px solid var(--grid); white-space: nowrap; }
th { color: var(--text-secondary); font-weight: 600; }
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
tr.infra td { color: var(--muted); }
.pill { font-size: 12px; padding: 1px 6px; border-radius: 9px; border: 1px solid var(--border); color: var(--text-secondary); }
.tip { position: fixed; pointer-events: none; background: var(--surface-1); color: var(--text-primary);
       border: 1px solid var(--border); border-radius: 6px; padding: 8px 10px; font-size: 12px;
       box-shadow: 0 4px 16px rgba(0,0,0,.12); display: none; z-index: 10; max-width: 280px; }
.tip strong { font-size: 13px; }
.tip .row { display: flex; gap: 8px; align-items: center; }
.empty { color: var(--muted); padding: 12px 0; }
details summary { cursor: pointer; color: var(--text-secondary); margin: 8px 0; }
.mark:hover, .mark:focus { opacity: .8; outline: none; }
</style>
</head>
<body>
<main>
  <h1 id="title"></h1>
  <p class="sub" id="subtitle"></p>
  <p class="meta" id="env"></p>
  <div class="filters" role="group" aria-label="Comparison">
    <label>Compare <select id="armA" aria-label="Arm A"></select></label>
    <label>with <select id="armB" aria-label="Arm B"></select></label>
    <span class="meta" id="pairnote"></span>
  </div>
  <div class="tiles" id="tiles"></div>

  <h2>Per task</h2>
  <p class="sub">pass@1 of each task, B to A. Tasks that got worse come first.</p>
  <div class="card"><div class="legend" id="dumbLegend"></div><div id="dumbbell"></div>
    <details><summary>Table</summary><div class="scroll" id="taskTable"></div></details></div>

  <h2>By difficulty and route</h2>
  <p class="sub">Difficulty is measured from the baseline arm: easy when it resolved every trial of the task, hard when it resolved none, medium in between. Routes appear for runs that had one.</p>
  <div class="card scroll" id="groupTable"></div>

  <h2>Trend across plugin versions</h2>
  <p class="sub">Every run ever recorded, grouped by plugin version in the order versions were first run. The baseline is the no-plugin reference.</p>
  <div class="grid2">
    <div class="card"><h3>pass@1</h3><div class="legend trendLegend"></div><div id="trendPass"></div></div>
    <div class="card"><h3>Median cost per run</h3><div class="legend trendLegend"></div><div id="trendCost"></div></div>
  </div>
  <details><summary>Table</summary><div class="scroll card" id="trendTable"></div></details>

  <h2>Where time and tokens go</h2>
  <p class="sub" id="effSub"></p>
  <div class="grid2">
    <div class="card"><h3 id="toolTimeTitle">Time per run by tool</h3><div id="toolTime"></div></div>
    <div class="card"><h3 id="toolTokTitle">Tokens per run by tool (output + result + re-reads)</h3><div id="toolTok"></div></div>
  </div>
  <div class="card" style="margin-top:12px"><h3>Skills</h3><div class="scroll" id="skillTable"></div></div>
  <div class="card" style="margin-top:12px"><h3>Tools</h3><div class="scroll" id="toolTable"></div></div>
  <div class="card" style="margin-top:12px"><h3>Subagents</h3><div class="scroll" id="subTable"></div></div>
  <div class="card" style="margin-top:12px"><h3>Plugin hooks (from the trace: every run, with why)</h3><div class="scroll" id="hookTable"></div></div>
  <div class="card" style="margin-top:12px"><h3>All hooks Claude Code reported</h3><div class="scroll" id="ccHookTable"></div></div>

  <h2>Failures</h2>
  <p class="sub">One label per unresolved run. Manual labels override automatic ones.</p>
  <div class="card scroll" id="labelTable"></div>

  <h2>Runs</h2>
  <p class="sub">Every run, including infrastructure errors (left out of every score). Run directories are local to the machine that ran them.</p>
  <div class="card scroll" id="runTable"></div>
</main>
<div class="tip" id="tip" role="tooltip"></div>
<script id="data" type="application/json">__DATA__</script>
<script>
(function () {
  "use strict";
  const D = JSON.parse(document.getElementById("data").textContent);
  const $ = (id) => document.getElementById(id);
  const NS = "http://www.w3.org/2000/svg";
  const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  const el = (tag, attrs, text) => { const e = document.createElement(tag); for (const k in attrs || {}) e.setAttribute(k, attrs[k]); if (text != null) e.textContent = text; return e; };
  const sv = (tag, attrs) => { const e = document.createElementNS(NS, tag); for (const k in attrs || {}) e.setAttribute(k, attrs[k]); return e; };
  const pct = (x) => x == null || isNaN(x) ? "–" : Math.round(x * 100) + "%";
  const usd = (x) => x == null || isNaN(x) ? "–" : "$" + (x < 1 ? x.toFixed(3) : x.toFixed(2));
  const secs = (x) => x == null || isNaN(x) ? "–" : (x >= 120 ? (x / 60).toFixed(1) + " min" : Math.round(x) + " s");
  const num = (x, d) => x == null || isNaN(x) ? "–" : Number(x).toLocaleString(undefined, { maximumFractionDigits: d == null ? 0 : d });
  const compact = (x) => x == null ? "–" : Math.abs(x) >= 1e6 ? (x / 1e6).toFixed(1) + "M" : Math.abs(x) >= 1e4 ? Math.round(x / 1e3) + "K" : num(x);

  // tooltip ---------------------------------------------------------------------
  const tip = $("tip");
  function showTip(evt, rows, title) {
    tip.replaceChildren();
    if (title) { const t = el("div", { class: "meta" }, title); tip.appendChild(t); }
    for (const r of rows) {
      const row = el("div", { class: "row" });
      if (r.color) { const k = el("span", { class: "key" }); k.style.background = r.color; row.appendChild(k); }
      row.appendChild(el("strong", {}, r.value));
      row.appendChild(el("span", { class: "meta" }, r.label));
      tip.appendChild(row);
    }
    tip.style.display = "block";
    const box = evt.target.getBoundingClientRect ? evt.target.getBoundingClientRect() : { left: 0, top: 0, width: 0 };
    const x = evt.clientX != null && evt.clientX !== 0 ? evt.clientX : box.left + box.width / 2;
    const y = evt.clientY != null && evt.clientY !== 0 ? evt.clientY : box.top;
    tip.style.left = Math.min(x + 14, innerWidth - tip.offsetWidth - 8) + "px";
    tip.style.top = Math.max(y - tip.offsetHeight - 10, 8) + "px";
  }
  const hideTip = () => { tip.style.display = "none"; };

  // tables ----------------------------------------------------------------------
  function table(target, columns, rows, rowClass) {
    const t = el("table");
    const head = el("tr");
    for (const c of columns) head.appendChild(el("th", c.num ? { class: "num" } : {}, c.label));
    t.appendChild(el("thead")).appendChild(head);
    const body = t.appendChild(el("tbody"));
    for (const r of rows) {
      const tr = el("tr", rowClass ? { class: rowClass(r) || "" } : {});
      for (const c of columns) {
        const v = c.get(r);
        const td = el("td", c.num ? { class: "num" } : {});
        if (v instanceof Node) td.appendChild(v); else td.textContent = v == null ? "–" : v;
        tr.appendChild(td);
      }
      body.appendChild(tr);
    }
    target.replaceChildren(rows.length ? t : el("div", { class: "empty" }, "Nothing recorded."));
  }

  // ranked horizontal bars (one hue) ------------------------------------------------
  function bars(target, items, fmt) {
    target.replaceChildren();
    if (!items.length) { target.appendChild(el("div", { class: "empty" }, "Nothing recorded.")); return; }
    const W = Math.max(target.clientWidth || 520, 280), labelW = Math.min(170, W * 0.36), right = 64, rowH = 26, barH = 14;
    const max = Math.max(...items.map((i) => i.value), 1e-9);
    const svg = sv("svg", { viewBox: `0 0 ${W} ${items.length * rowH + 4}`, role: "img",
                            "aria-label": "Ranked bars; the same values are in the table below" });
    items.forEach((it, i) => {
      const y = i * rowH + 4, w = Math.max((W - labelW - right) * it.value / max, 1);
      const label = sv("text", { x: labelW - 8, y: y + barH - 2, "text-anchor": "end" }); label.textContent = it.label;
      label.style.fill = css("--text-secondary");
      const r = Math.min(4, w / 2);
      const path = sv("path", { d: `M${labelW},${y} h${w - r} a${r},${r} 0 0 1 ${r},${r} v${barH - 2 * r} a${r},${r} 0 0 1 -${r},${r} h-${w - r} z`,
                                fill: css("--series-1"), class: "mark", tabindex: "0" });
      const value = sv("text", { x: labelW + w + 6, y: y + barH - 2 }); value.textContent = fmt(it.value);
      value.style.fill = css("--text-primary");
      const hit = sv("rect", { x: 0, y: y - 4, width: W, height: rowH, fill: "transparent" });
      const show = (e) => showTip(e, [{ value: fmt(it.value), label: it.label }].concat(it.extra || []));
      for (const n of [hit, path]) { n.addEventListener("pointermove", show); n.addEventListener("pointerleave", hideTip); }
      path.addEventListener("focus", show); path.addEventListener("blur", hideTip);
      svg.append(label, hit, path, value);
    });
    target.appendChild(svg);
  }

  // dumbbell: B -> A per task --------------------------------------------------------
  function dumbbell(target, rows) {
    target.replaceChildren();
    if (!rows.length) { target.appendChild(el("div", { class: "empty" }, "No task ran under both arms.")); return; }
    const W = Math.max(target.clientWidth || 600, 300), labelW = Math.min(200, W * 0.34), right = 54, rowH = 26, top = 18;
    const x = (v) => labelW + (W - labelW - right) * v;
    const svg = sv("svg", { viewBox: `0 0 ${W} ${rows.length * rowH + top + 4}`, role: "img",
                            "aria-label": "pass@1 per task for both arms; the same values are in the table" });
    for (const t of [0, 0.5, 1]) {
      svg.appendChild(sv("line", { x1: x(t), x2: x(t), y1: top - 4, y2: rows.length * rowH + top, stroke: css("--grid"), "stroke-width": 1 }));
      const lab = sv("text", { x: x(t), y: 10, "text-anchor": "middle" }); lab.textContent = pct(t); svg.appendChild(lab);
    }
    rows.forEach((r, i) => {
      const y = top + i * rowH + rowH / 2;
      const label = sv("text", { x: labelW - 10, y: y + 4, "text-anchor": "end" }); label.textContent = r.task;
      label.style.fill = css("--text-secondary");
      const line = sv("line", { x1: x(r.b), x2: x(r.a), y1: y, y2: y, stroke: css("--axis"), "stroke-width": 2, "stroke-linecap": "round" });
      const b = sv("circle", { cx: x(r.b), cy: y, r: 5, fill: css("--reference"), stroke: css("--surface-1"), "stroke-width": 2 });
      const a = sv("circle", { cx: x(r.a), cy: y, r: 5, fill: css("--series-1"), stroke: css("--surface-1"), "stroke-width": 2 });
      const d = r.a - r.b;
      const delta = sv("text", { x: W - right + 10, y: y + 4 }); delta.textContent = d === 0 ? "same" : (d > 0 ? "+" : "−") + Math.round(Math.abs(d) * 100) + " pts";
      delta.style.fill = d < 0 ? css("--bad") : d > 0 ? css("--good") : css("--muted");
      const hit = sv("rect", { x: 0, y: y - rowH / 2, width: W, height: rowH, fill: "transparent", tabindex: "0", class: "mark" });
      const show = (e) => showTip(e, [{ value: pct(r.a), label: r.aName, color: css("--series-1") },
                                      { value: pct(r.b), label: r.bName, color: css("--reference") },
                                      { value: usd(r.aCost) + " vs " + usd(r.bCost), label: "median cost" }], r.task);
      hit.addEventListener("pointermove", show); hit.addEventListener("pointerleave", hideTip);
      hit.addEventListener("focus", show); hit.addEventListener("blur", hideTip);
      svg.append(label, line, b, a, delta, hit);
    });
    target.appendChild(svg);
  }

  // line chart over ordinal versions, crosshair + one tooltip for every series --------------
  function lines(target, xs, series, fmt, yMax) {
    target.replaceChildren();
    if (!xs.length) { target.appendChild(el("div", { class: "empty" }, "No versions recorded yet.")); return; }
    const W = Math.max(target.clientWidth || 520, 280), H = 210, L = 44, R = 12, T = 10, B = 34;
    const max = yMax != null ? yMax : Math.max(1e-9, ...series.flatMap((s) => s.values.filter((v) => v != null)));
    const xAt = (i) => xs.length === 1 ? (L + W - R) / 2 : L + (W - L - R) * i / (xs.length - 1);
    const yAt = (v) => T + (H - T - B) * (1 - v / max);
    const svg = sv("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": "Trend by plugin version; the same values are in the table" });
    for (const t of [0, 0.5, 1]) {
      const v = max * t;
      svg.appendChild(sv("line", { x1: L, x2: W - R, y1: yAt(v), y2: yAt(v), stroke: t === 0 ? css("--axis") : css("--grid"), "stroke-width": 1 }));
      const lab = sv("text", { x: L - 6, y: yAt(v) + 4, "text-anchor": "end" }); lab.textContent = fmt(v); svg.appendChild(lab);
    }
    const every = Math.ceil(xs.length / Math.max(1, Math.floor((W - L - R) / 90)));
    xs.forEach((x, i) => {
      if (i % every) return;
      const lab = sv("text", { x: xAt(i), y: H - 12, "text-anchor": "middle" }); lab.textContent = x.length > 14 ? x.slice(0, 13) + "…" : x;
      svg.appendChild(lab);
    });
    for (const s of series) {
      let d = "";
      s.values.forEach((v, i) => { if (v == null) return; d += (d ? "L" : "M") + xAt(i) + "," + yAt(v); });
      if (d) svg.appendChild(sv("path", { d, fill: "none", stroke: s.color, "stroke-width": 2, "stroke-linejoin": "round", "stroke-linecap": "round" }));
      s.values.forEach((v, i) => { if (v != null) svg.appendChild(sv("circle", { cx: xAt(i), cy: yAt(v), r: 4, fill: s.color, stroke: css("--surface-1"), "stroke-width": 2 })); });
    }
    const cross = sv("line", { y1: T, y2: H - B, stroke: css("--axis"), "stroke-width": 1, visibility: "hidden" });
    svg.appendChild(cross);
    const hit = sv("rect", { x: L, y: T, width: W - L - R, height: H - T - B, fill: "transparent" });
    hit.addEventListener("pointermove", (e) => {
      const box = svg.getBoundingClientRect(), px = (e.clientX - box.left) * W / box.width;
      let i = 0, best = Infinity;
      xs.forEach((_, j) => { const dd = Math.abs(xAt(j) - px); if (dd < best) { best = dd; i = j; } });
      cross.setAttribute("x1", xAt(i)); cross.setAttribute("x2", xAt(i)); cross.setAttribute("visibility", "visible");
      showTip(e, series.map((s) => ({ value: s.values[i] == null ? "–" : fmt(s.values[i]), label: s.name, color: s.color })), xs[i]);
    });
    hit.addEventListener("pointerleave", () => { cross.setAttribute("visibility", "hidden"); hideTip(); });
    svg.appendChild(hit);
    target.appendChild(svg);
  }

  function legend(target, items) {
    target.replaceChildren();
    for (const it of items) {
      const s = el("span");
      const k = el("span", { class: it.dot ? "dotkey" : "key" }); k.style.background = it.color;
      s.append(k, document.createTextNode(it.label));
      target.appendChild(s);
    }
  }

  // page ----------------------------------------------------------------------------
  $("title").textContent = D.title;
  $("subtitle").textContent = (D.experiment ? "Experiment " + D.experiment + " · " : "All recorded runs · ") +
    D.rows + " scored runs over " + D.tasks.length + " task(s) · " + D.infra_errors + " infrastructure error(s) left out · pass^" + D.k;
  const env = D.env || {};
  $("env").textContent = "Model " + (env.model || []).join(", ") + " · Claude Code " + (env.claude_code_version || []).join(", ") +
    " · graphify " + (env.graphify_version || []).join(", ") + " · dataset " + (env.dataset_sha || []).map((s) => s.slice(0, 12)).join(", ") +
    " · generated " + D.generated;

  const arms = D.arm_names;
  const selA = $("armA"), selB = $("armB");
  for (const s of [selA, selB]) for (const a of arms) s.appendChild(el("option", { value: a }, a));
  const firstPair = (D.comparisons || [])[0];
  selA.value = firstPair ? firstPair.a : arms[arms.length - 1] || "";
  selB.value = firstPair ? firstPair.b : arms[0] || "";
  try { const saved = JSON.parse(localStorage.getItem("bench-pair") || "null"); if (saved && arms.includes(saved[0]) && arms.includes(saved[1])) { selA.value = saved[0]; selB.value = saved[1]; } } catch (e) {}

  function tile(label, value, deltaText, cls) {
    const t = el("div", { class: "tile" });
    t.append(el("div", { class: "label" }, label), el("div", { class: "value" }, value), el("div", { class: "delta " + (cls || "") }, deltaText || ""));
    return t;
  }
  const signed = (x, f) => x == null || isNaN(x) ? "" : (x > 0 ? "+" : x < 0 ? "−" : "±") + f(Math.abs(x));
  const ciText = (c, f) => c ? signed(c[0], f) + " (95% CI " + signed(c[1], f) + " to " + signed(c[2], f) + ")" : "";

  function render() {
    try { localStorage.setItem("bench-pair", JSON.stringify([selA.value, selB.value])); } catch (e) {}
    const A = D.arms[selA.value], B = D.arms[selB.value];
    const cmp = (D.comparisons || []).find((c) => c.a === selA.value && c.b === selB.value);
    $("pairnote").textContent = cmp ? cmp.tasks + " shared task(s); intervals from a paired bootstrap" : "no precomputed comparison for this pair: differences only";
    const tiles = $("tiles"); tiles.replaceChildren();
    if (A && B) {
      const dp = cmp ? cmp.pass1 : [A.pass1 - B.pass1];
      tiles.append(
        tile("pass@1 · " + selA.value, pct(A.pass1), ciText(cmp ? cmp.pass1 : null, (v) => Math.round(v * 100) + " pts") || signed(dp[0], (v) => Math.round(v * 100) + " pts") + " vs " + selB.value, dp[0] > 0 ? "good" : dp[0] < 0 ? "bad" : ""),
        tile("pass^" + D.k, pct(A.pass_hat_k), signed(A.pass_hat_k - B.pass_hat_k, (v) => Math.round(v * 100) + " pts") + " vs " + selB.value, (A.pass_hat_k - B.pass_hat_k) > 0 ? "good" : (A.pass_hat_k - B.pass_hat_k) < 0 ? "bad" : ""),
        tile("Median cost per run", usd(A.median_cost), (cmp ? ciText(cmp.cost, (v) => "$" + v.toFixed(2)) : signed(A.median_cost - B.median_cost, (v) => "$" + v.toFixed(2))) + " vs " + selB.value, (A.median_cost - B.median_cost) < 0 ? "good" : (A.median_cost - B.median_cost) > 0 ? "bad" : ""),
        tile("Median time per run", secs(A.median_wall_s), (cmp ? ciText(cmp.wall_s, (v) => Math.round(v) + " s") : signed(A.median_wall_s - B.median_wall_s, (v) => Math.round(v) + " s")) + " vs " + selB.value, (A.median_wall_s - B.median_wall_s) < 0 ? "good" : (A.median_wall_s - B.median_wall_s) > 0 ? "bad" : ""),
        tile("False done", pct(A.false_done_rate), signed(A.false_done_rate - B.false_done_rate, (v) => Math.round(v * 100) + " pts") + " vs " + selB.value, (A.false_done_rate - B.false_done_rate) < 0 ? "good" : (A.false_done_rate - B.false_done_rate) > 0 ? "bad" : ""));
    }
    // per task
    const rows = [];
    for (const t of D.tasks) {
      const a = A && A.per_task[t], b = B && B.per_task[t];
      if (a && b) rows.push({ task: t, a: a.pass1, b: b.pass1, aCost: a.median_cost, bCost: b.median_cost, aName: selA.value, bName: selB.value, aN: a.n, bN: b.n, difficulty: D.difficulty[t] });
    }
    rows.sort((x, y) => (x.a - x.b) - (y.a - y.b) || x.task.localeCompare(y.task));
    legend($("dumbLegend"), [{ label: selA.value, color: css("--series-1"), dot: true }, { label: selB.value, color: css("--reference"), dot: true }]);
    dumbbell($("dumbbell"), rows);
    table($("taskTable"), [
      { label: "Task", get: (r) => r.task },
      { label: "Difficulty", get: (r) => !r.difficulty ? "" : r.difficulty.bucket + (r.difficulty.baseline_runs ? " (" + r.difficulty.baseline_solved + "/" + r.difficulty.baseline_runs + ")" : "") },
      { label: selA.value + " pass@1", num: 1, get: (r) => pct(r.a) + " (n=" + r.aN + ")" },
      { label: selB.value + " pass@1", num: 1, get: (r) => pct(r.b) + " (n=" + r.bN + ")" },
      { label: "Change", num: 1, get: (r) => signed(r.a - r.b, (v) => Math.round(v * 100) + " pts") || "same" },
      { label: selA.value + " cost", num: 1, get: (r) => usd(r.aCost) }, { label: selB.value + " cost", num: 1, get: (r) => usd(r.bCost) },
    ], rows);
    const groups = [];
    for (const [field, kind] of [["by_difficulty", "difficulty"], ["by_route", "route"]]) {
      const names = Array.from(new Set(Object.keys((A && A[field]) || {}).concat(Object.keys((B && B[field]) || {}))));
      if (kind === "route" && names.length === 1 && names[0] === "none") continue;
      for (const n of names) groups.push({ kind, name: n, a: (A && A[field][n]) || {}, b: (B && B[field][n]) || {} });
    }
    table($("groupTable"), [
      { label: "Group", get: (r) => r.kind + ": " + r.name },
      { label: selA.value + " pass@1", num: 1, get: (r) => pct(r.a.pass1) + (r.a.tasks ? " (" + r.a.tasks + " tasks)" : "") },
      { label: selB.value + " pass@1", num: 1, get: (r) => pct(r.b.pass1) + (r.b.tasks ? " (" + r.b.tasks + " tasks)" : "") },
      { label: selA.value + " cost", num: 1, get: (r) => usd(r.a.median_cost) }, { label: selB.value + " cost", num: 1, get: (r) => usd(r.b.median_cost) },
    ], groups);
    efficiency(A, B);
  }

  function efficiency(A, B) {
    $("effSub").textContent = "Per run, averaged over each arm's runs: " + selA.value + " (bars and first columns) beside " + selB.value + ".";
    if (!A) return;
    const EA = A.efficiency, EB = (B && B.efficiency) || { skills: {}, tools: {}, subagents: {}, plugin_hooks: {}, hooks: {} };
    const toolNames = Object.keys(EA.tools).filter((n) => !n.includes(":") && n !== "(text)");
    const timeItems = toolNames.map((n) => ({ label: n, value: EA.tools[n].s_per_run || 0,
      extra: [{ value: num(EA.tools[n].calls_per_run, 1), label: "calls per run" }] })).filter((i) => i.value > 0).sort((a, b) => b.value - a.value).slice(0, 12);
    $("toolTimeTitle").textContent = "Time per run by tool · " + selA.value;
    bars($("toolTime"), timeItems, (v) => v.toFixed(1) + " s");
    const tok = (t) => (t.out_tokens_per_run || 0) + (t.result_tokens_per_run || 0) + (t.rent_tokens_per_run || 0);
    const tokItems = toolNames.map((n) => ({ label: n, value: tok(EA.tools[n]),
      extra: [{ value: compact(EA.tools[n].out_tokens_per_run), label: "output" }, { value: compact(EA.tools[n].result_tokens_per_run), label: "result" },
              { value: compact(EA.tools[n].rent_tokens_per_run), label: "re-read later" }] })).filter((i) => i.value > 0).sort((a, b) => b.value - a.value).slice(0, 12);
    $("toolTokTitle").textContent = "Tokens per run by tool · " + selA.value;
    bars($("toolTok"), tokItems, compact);
    const pair = (objA, objB) => Array.from(new Set(Object.keys(objA).concat(Object.keys(objB)))).sort().map((n) => ({ n, a: objA[n] || {}, b: objB[n] || {} }));
    table($("skillTable"), [
      { label: "Skill", get: (r) => r.n },
      { label: "Used in (A)", num: 1, get: (r) => pct(r.a.used_in) }, { label: "Time (A)", num: 1, get: (r) => secs(r.a.wall_s_per_run) },
      { label: "Time (B)", num: 1, get: (r) => secs(r.b.wall_s_per_run) }, { label: "Cost (A)", num: 1, get: (r) => usd(r.a.cost_per_run) },
      { label: "Cost (B)", num: 1, get: (r) => usd(r.b.cost_per_run) }, { label: "Tokens (A)", num: 1, get: (r) => compact(r.a.tokens_per_run) },
      { label: "Tokens (B)", num: 1, get: (r) => compact(r.b.tokens_per_run) }, { label: "Tool calls (A)", num: 1, get: (r) => num(r.a.tool_calls_per_run, 1) },
    ], pair(EA.skills, EB.skills));
    table($("toolTable"), [
      { label: "Tool", get: (r) => r.n }, { label: "Calls (A)", num: 1, get: (r) => num(r.a.calls_per_run, 1) },
      { label: "Calls (B)", num: 1, get: (r) => num(r.b.calls_per_run, 1) }, { label: "Errors (A)", num: 1, get: (r) => num(r.a.errors_per_run, 1) },
      { label: "Time (A)", num: 1, get: (r) => r.a.s_per_run == null ? "–" : r.a.s_per_run.toFixed(1) + " s" },
      { label: "Time (B)", num: 1, get: (r) => r.b.s_per_run == null ? "–" : r.b.s_per_run.toFixed(1) + " s" },
      { label: "p95 (A)", num: 1, get: (r) => r.a.p95_ms == null ? "–" : num(r.a.p95_ms) + " ms" },
      { label: "Hook time (A)", num: 1, get: (r) => r.a.hook_s_per_run == null ? "–" : r.a.hook_s_per_run.toFixed(1) + " s" },
      { label: "Output (A)", num: 1, get: (r) => compact(r.a.out_tokens_per_run) }, { label: "Result (A)", num: 1, get: (r) => compact(r.a.result_tokens_per_run) },
      { label: "Re-read (A)", num: 1, get: (r) => compact(r.a.rent_tokens_per_run) }, { label: "Re-read (B)", num: 1, get: (r) => compact(r.b.rent_tokens_per_run) },
    ], pair(EA.tools, EB.tools));
    table($("subTable"), [
      { label: "Subagent", get: (r) => r.n }, { label: "Count (A)", num: 1, get: (r) => num(r.a.count_per_run, 1) },
      { label: "Count (B)", num: 1, get: (r) => num(r.b.count_per_run, 1) }, { label: "Time (A)", num: 1, get: (r) => secs(r.a.wall_s_per_run) },
      { label: "Time (B)", num: 1, get: (r) => secs(r.b.wall_s_per_run) }, { label: "Cost (A)", num: 1, get: (r) => usd(r.a.cost_per_run) },
      { label: "Cost (B)", num: 1, get: (r) => usd(r.b.cost_per_run) }, { label: "Tokens (A)", num: 1, get: (r) => compact(r.a.tokens_per_run) },
    ], pair(EA.subagents, EB.subagents));
    table($("hookTable"), [
      { label: "Hook", get: (r) => r.n }, { label: "Runs (A)", num: 1, get: (r) => num(r.a.runs_per_run, 1) },
      { label: "p50 (A)", num: 1, get: (r) => r.a.p50_ms == null ? "–" : num(r.a.p50_ms) + " ms" },
      { label: "p95 (A)", num: 1, get: (r) => r.a.p95_ms == null ? "–" : num(r.a.p95_ms) + " ms" },
      { label: "p95 (B)", num: 1, get: (r) => r.b.p95_ms == null ? "–" : num(r.b.p95_ms) + " ms" },
      { label: "Context chars (A)", num: 1, get: (r) => num(r.a.context_chars_per_run) },
      { label: "Outcomes (A)", get: (r) => Object.entries(r.a.outcomes || {}).map(([k, v]) => k + " ×" + v).join(", ") },
    ], pair(EA.plugin_hooks, EB.plugin_hooks));
    table($("ccHookTable"), [
      { label: "Hook", get: (r) => r.n }, { label: "Runs (A)", num: 1, get: (r) => num(r.a.runs_per_run, 1) },
      { label: "Runs (B)", num: 1, get: (r) => num(r.b.runs_per_run, 1) }, { label: "Errors (A)", num: 1, get: (r) => num(r.a.errors_per_run, 1) },
      { label: "p95 (A)", num: 1, get: (r) => r.a.p95_ms == null ? "–" : num(r.a.p95_ms) + " ms" },
      { label: "Time (A)", num: 1, get: (r) => r.a.s_per_run == null ? "–" : r.a.s_per_run.toFixed(1) + " s" },
      { label: "Context chars (A)", num: 1, get: (r) => num(r.a.context_chars_per_run) },
    ], pair(EA.hooks, EB.hooks));
  }

  function staticViews() {
    // trend
    const versions = [], index = {};
    for (const t of D.trend) if (!(t.version in index)) { index[t.version] = versions.length; versions.push(t.version); }
    const seriesDef = [{ key: "raw", name: "plugin, raw prompt", color: css("--series-1") },
                       { key: "skill", name: "plugin, skill prompt", color: css("--series-2") },
                       { key: "baseline", name: "baseline (no plugin)", color: css("--reference") }];
    const present = seriesDef.filter((s) => D.trend.some((t) => t.series === s.key));
    const valuesOf = (key, field) => {
      const v = versions.map(() => null);
      for (const t of D.trend) if (t.series === key) v[index[t.version]] = t[field];
      if (key === "baseline") { let last = null; for (let i = 0; i < v.length; i++) { if (v[i] != null) last = v[i]; else v[i] = last; } }
      return v;
    };
    for (const lg of document.querySelectorAll(".trendLegend")) legend(lg, present.map((s) => ({ label: s.name, color: s.color })));
    lines($("trendPass"), versions, present.map((s) => ({ name: s.name, color: s.color, values: valuesOf(s.key, "pass1") })), pct, 1);
    lines($("trendCost"), versions, present.map((s) => ({ name: s.name, color: s.color, values: valuesOf(s.key, "median_cost") })), usd);
    table($("trendTable"), [
      { label: "Series", get: (r) => r.series }, { label: "Version", get: (r) => r.version }, { label: "First run", get: (r) => (r.first_ts || "").slice(0, 10) },
      { label: "Runs", num: 1, get: (r) => r.runs }, { label: "Tasks", num: 1, get: (r) => r.tasks }, { label: "pass@1", num: 1, get: (r) => pct(r.pass1) },
      { label: "Median cost", num: 1, get: (r) => usd(r.median_cost) }, { label: "Median time", num: 1, get: (r) => secs(r.median_wall_s) },
    ], D.trend);
    // labels
    const labels = Array.from(new Set(arms.flatMap((a) => Object.keys(D.arms[a].labels)))).sort();
    table($("labelTable"), [{ label: "Label", get: (r) => r }].concat(arms.map((a) => ({ label: a, num: 1, get: (r) => D.arms[a].labels[r] || "" }))), labels);
    // runs
    table($("runTable"), [
      { label: "When", get: (r) => (r.ts || "").replace("T", " ").replace("Z", "") }, { label: "Task", get: (r) => r.task },
      { label: "Arm", get: (r) => r.arm }, { label: "Trial", num: 1, get: (r) => r.trial },
      { label: "Result", get: (r) => r.status === "infra_error" ? "infra error" : r.resolved ? "resolved" : "unresolved" },
      { label: "Label", get: (r) => r.label || "" }, { label: "False done", get: (r) => r.false_done ? "yes" : "" },
      { label: "Cost", num: 1, get: (r) => usd(r.cost) }, { label: "Time", num: 1, get: (r) => secs(r.wall_s) },
      { label: "Turns", num: 1, get: (r) => r.turns }, { label: "Run directory", get: (r) => r.run_dir || "" },
    ], D.runs.slice().reverse(), (r) => r.status === "infra_error" ? "infra" : "");
  }

  selA.addEventListener("change", render);
  selB.addEventListener("change", render);
  let resizeTimer;
  addEventListener("resize", () => { clearTimeout(resizeTimer); resizeTimer = setTimeout(() => { render(); staticViews(); }, 150); });
  matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => { render(); staticViews(); });
  render();
  staticViews();
})();
</script>
</body>
</html>
"""


def _clean(value):
    """JSON.parse rejects NaN and Infinity: send them as null."""
    if isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))):
        return None
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    return value


def render(data: dict, title: str) -> str:
    payload = _clean(dict(data, title=title, generated=time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())))
    blob = json.dumps(payload, default=str, allow_nan=False).replace("</", "<\\/")
    return PAGE.replace("__TITLE__", html_mod.escape(title)).replace("__DATA__", blob)
