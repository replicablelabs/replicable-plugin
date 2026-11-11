"""PostToolUse(Edit|Write): keep graphify's graph current and report the blast radius.

graphify's own rules ask the model to run `graphify update .` after edits; this
hook makes that automatic and adds what `graphify affected` says about the
edited file, with the tests among the affected files listed first.
"""

import re
import time
from pathlib import Path

from _common import ROOT, context, project, read_event, trace

DOCS = (".md", ".mdx", ".rst", ".txt", ".json", ".yml", ".yaml", ".toml", ".lock", ".png", ".jpg", ".svg")
TEST_FILE = re.compile(r"(^|/)(tests?|__tests__|spec)/|(^|/)test_[^/]+$|_test\.[a-z]+$|\.(test|spec)\.[a-z]+$")


def skip(reason: str, **fields) -> None:
    trace("skip", reason, **fields)
    raise SystemExit(0)


event = read_event()
tool_input = event.get("tool_input") or {}
file_path = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
if not file_path:
    skip("no_path")
if file_path.lower().endswith(DOCS):
    skip("docs")
if not project.graphify():
    skip("no_graphify")
try:
    rel = Path(file_path).resolve().relative_to(ROOT).as_posix()
except ValueError:
    skip("outside_root")

started = time.monotonic()
try:
    update = project.run_graphify(["update", "."], ROOT, timeout=60)
    update_ms = round((time.monotonic() - started) * 1000)
    if update.returncode != 0:
        text = f"graphify update failed after editing {rel}: {(update.stderr or update.stdout).strip()[:300]}"
        context("PostToolUse", text)
        trace("context", "update_failed", chars=len(text), update_ms=update_ms)
        raise SystemExit(0)
    affected = project.run_graphify(["affected", rel, "--depth", "3"], ROOT, timeout=30)
except SystemExit:
    raise
except Exception as exc:
    trace("error", f"graphify_error:{type(exc).__name__}")
    raise SystemExit(0)  # helper hook: fail open
affected_ms = round((time.monotonic() - started) * 1000) - update_ms
if affected.returncode != 0:
    skip("not_modeled", update_ms=update_ms, affected_ms=affected_ms)  # e.g. a file graphify does not model

hits = [line.strip()[2:] for line in affected.stdout.splitlines() if line.strip().startswith("- ")]
if not hits:
    skip("no_hits", update_ms=update_ms, affected_ms=affected_ms)
files = sorted({h.rsplit(" ", 1)[-1].split(":L")[0] for h in hits if ":L" in h})
tests = [f for f in files if TEST_FILE.search(f)]
lines = [f"graphify: editing {rel} can affect {len(hits)} symbol(s) in {len(files)} file(s)."]
lines.append("Tests to run: " + (", ".join(tests) if tests else "none found among affected files (consider adding one)"))
lines += [f"- {h}" for h in hits[:8]]
if len(hits) > 8:
    lines.append(f"... {len(hits) - 8} more: `graphify affected {rel}`")
text = "\n".join(lines)
context("PostToolUse", text)
trace("context", "reported", chars=len(text), symbols=len(hits), files=len(files), tests=len(tests),
      update_ms=update_ms, affected_ms=affected_ms)
