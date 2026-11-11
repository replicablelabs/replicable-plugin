"""The local delivery proxy: what a change costs to build and to integration-test. Never pushes or deploys.

suite.json::

    "delivery": {
      "build": [{"name": "agent", "cmd": "docker build -q -f src/x/Dockerfile -t {image} ."}],
      "suites": {
        "finance": {"compose": "compose.finance-tests.yml", "service": "finance-tests", "timeout_s": 1800}
      }
    }

A task lists the suites that exercise it (``"delivery": ["finance"]``). Each
build is timed and its image size recorded, then the images are removed. Each
compose suite runs under its own project name and is torn down with its volumes.
Delivery metrics are reported apart from the harness score.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import List

from common import sh


def _project(run_id: str) -> str:
    return re.sub(r"[^a-z0-9_-]", "-", f"bench-{run_id}".lower())[:60]


def run(config: dict, suites: List[str], repo: Path, env: dict, run_id: str) -> dict:
    project = _project(run_id)
    out = {"builds": [], "suites": [], "ok": True}
    for build in config.get("build", []):
        image = f"{project}-{build.get('name', 'image')}"
        res = sh(build["cmd"].replace("{image}", image), cwd=repo, env=env, timeout=int(build.get("timeout_s", 3600)))
        size = None
        if res.ok:
            inspected = sh(["docker", "image", "inspect", "-f", "{{.Size}}", image], env=env, timeout=60)
            size = int(inspected.proc.stdout.strip()) if inspected.ok and inspected.proc.stdout.strip().isdigit() else None
            sh(["docker", "image", "rm", "-f", image], env=env, timeout=120)
        out["builds"].append({"name": build.get("name"), "ok": res.ok, "ms": res.ms, "image_bytes": size,
                              **({} if res.ok else {"error": "timeout" if res.timed_out else f"exit {res.proc.returncode}"})})
        out["ok"] &= res.ok
    for name in suites:
        spec = config.get("suites", {}).get(name)
        if not spec:
            out["suites"].append({"name": name, "ok": False, "error": "not defined"})
            out["ok"] = False
            continue
        files = spec["compose"] if isinstance(spec["compose"], list) else [spec["compose"]]
        base = ["docker", "compose", "-p", f"{project}-{name}"[:63]]
        for f in files:
            base += ["-f", f]
        up = base + ["up", "--build", "--abort-on-container-exit", "--exit-code-from", spec["service"]]
        res = sh(up, cwd=repo, env=env, timeout=int(spec.get("timeout_s", 1800)))
        sh(base + ["down", "-v", "--remove-orphans", "--rmi", "local"], cwd=repo, env=env, timeout=600)
        out["suites"].append({"name": name, "ok": res.ok, "ms": res.ms,
                              **({} if res.ok else {"error": "timeout" if res.timed_out else f"exit {res.proc.returncode}"})})
        out["ok"] &= res.ok
    out["build_ms"] = sum(b["ms"] for b in out["builds"])
    out["integration_ms"] = sum(s.get("ms", 0) for s in out["suites"])
    return out
