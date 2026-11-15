"""Opt-in bounded child after existing research jobs; trading results unchanged."""

from pathlib import Path
import json
import os
import signal
import subprocess
import sys

from alphaos.scheduler.research_budget import admission


def maybe_run(orch, *, runner=None):
    config = (
        Path(orch.settings.db_path).resolve().parent / "swing-audit-activation.json"
    )
    if not config.exists():
        return None
    try:
        # Reuse the existing conservative five-minute scan reservation.
        if not admission(orch.journal, orch.settings, "swing_audit")["allowed"]:
            return {"status": "deferred_scan_reserve"}
        command = [
            sys.executable,
            "-m",
            "alphaos.research.swing_audit",
            "--db",
            str(Path(orch.settings.db_path).resolve()),
            "--activation",
            str(config),
        ]
        if runner is not None:
            return runner(command)
        child = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            env={"PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1"},
        )
        try:
            raw, _ = child.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            os.killpg(child.pid, signal.SIGKILL)
            child.communicate()
            return {"status": "timed_out"}
        except BaseException:
            if child.poll() is None:
                os.killpg(child.pid, signal.SIGKILL)
            child.communicate()
            raise
        if len(raw) > 65536:
            raise ValueError("audit_output_limit")
        value = json.loads(raw)
        allowed = {"status", "run_receipt", "new_captures", "cohort_id", "error_type"}
        return {k: v for k, v in value.items() if k in allowed}
    except Exception as exc:
        return {"status": "blocked", "error_type": type(exc).__name__}
