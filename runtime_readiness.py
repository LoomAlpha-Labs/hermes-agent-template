"""Bounded runtime probes and atomic deployment-scoped readiness receipts."""

from __future__ import annotations

import json
import importlib
import os
import re
import sqlite3
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

PID_HEADROOM_MIN = 32
PID_HEADROOM_RATIO = 0.05
SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _process_alive(pid: int | None) -> bool:
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ValueError):
        return False


def _state_db_status(home: Path, *, required: bool) -> dict:
    path = home / "state.db"
    if not path.is_file():
        return {"status": "absent", "readable": not required, "required": required}
    try:
        uri = f"file:{path}?mode=ro"
        with sqlite3.connect(uri, uri=True, timeout=2) as conn:
            conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
        return {"status": "ready", "readable": True, "required": required}
    except (OSError, sqlite3.Error) as exc:
        return {
            "status": "error",
            "readable": False,
            "required": required,
            "error_type": type(exc).__name__,
        }


def _gateway_status(home: Path, expected_pid: int | None) -> dict:
    result = {
        "verified": False,
        "live_pid": None,
        "runtime_state": None,
        "session_store": None,
        "connected_platforms": [],
    }
    try:
        gateway_status = importlib.import_module("gateway.status")
        live_pid = gateway_status.live_gateway_pid_for_home(home)
        result["live_pid"] = live_pid
    except Exception as exc:
        result["error_type"] = type(exc).__name__
        return result

    try:
        runtime = json.loads((home / "gateway_state.json").read_text())
    except (OSError, json.JSONDecodeError) as exc:
        result["error_type"] = type(exc).__name__
        return result

    platforms = runtime.get("platforms") if isinstance(runtime.get("platforms"), dict) else {}
    connected = sorted(
        name
        for name, details in platforms.items()
        if isinstance(details, dict)
        and details.get("state") == "connected"
        and not details.get("needs_attention", False)
    )
    session_store = runtime.get("session_store") if isinstance(runtime.get("session_store"), dict) else {}
    result.update(
        {
            "runtime_state": runtime.get("gateway_state"),
            "session_store": session_store.get("status"),
            "connected_platforms": connected,
        }
    )
    result["verified"] = bool(
        live_pid
        and live_pid == expected_pid
        and runtime.get("pid") == live_pid
        and runtime.get("gateway_state") == "running"
        and session_store.get("status") == "ok"
        and connected
    )
    return result


def _dashboard_status(url: str, expected_pid: int | None) -> dict:
    result = {"verified": False, "pid": expected_pid, "http_status": None}
    if not _process_alive(expected_pid):
        return result
    try:
        request = urllib.request.Request(
            f"{url.rstrip('/')}/api/health",
            headers={"User-Agent": "Hermes-runtime-readiness/1.0"},
        )
        with urllib.request.urlopen(request, timeout=1) as response:
            result["http_status"] = response.status
            payload = json.loads(response.read(4096))
        result["verified"] = response.status == 200 and payload.get("ok") is True
    except (OSError, ValueError, json.JSONDecodeError, urllib.error.URLError) as exc:
        result["error_type"] = type(exc).__name__
    return result


def _process_pressure() -> dict:
    zombies = 0
    proc = Path("/proc")
    if proc.is_dir():
        for entry in proc.iterdir():
            if not entry.name.isdigit():
                continue
            try:
                for line in (entry / "status").read_text().splitlines():
                    if line.startswith("State:"):
                        fields = line.split()
                        zombies += int(len(fields) > 1 and fields[1] == "Z")
                        break
            except (OSError, UnicodeDecodeError):
                continue

    def read_cgroup(name: str) -> str | None:
        try:
            return (Path("/sys/fs/cgroup") / name).read_text().strip()
        except OSError:
            return None

    current_raw = read_cgroup("pids.current")
    max_raw = read_cgroup("pids.max")
    headroom = None
    headroom_required = PID_HEADROOM_MIN
    headroom_ok = False
    try:
        current = int(current_raw or "")
        if max_raw == "max":
            headroom_ok = True
        else:
            maximum = int(max_raw or "")
            headroom = maximum - current
            headroom_required = max(PID_HEADROOM_MIN, int(maximum * PID_HEADROOM_RATIO))
            headroom_ok = headroom >= headroom_required
    except ValueError:
        pass
    return {
        "zombies": zombies,
        "pids_current": current_raw,
        "pids_max": max_raw,
        "pid_headroom": headroom,
        "pid_headroom_required": headroom_required,
        "pid_headroom_ok": headroom_ok,
    }


def collect_runtime_readiness(
    *,
    hermes_home: str | Path,
    configured: bool,
    gateway_state: str,
    gateway_pid: int | None,
    dashboard_pid: int | None,
    dashboard_url: str = "http://127.0.0.1:9119",
    environ: dict[str, str] | None = None,
) -> dict:
    env = os.environ if environ is None else environ
    home = Path(hermes_home)
    deployment = {
        "id": env.get("RAILWAY_DEPLOYMENT_ID"),
        "commit_sha": env.get("RAILWAY_GIT_COMMIT_SHA"),
        "replica_id": env.get("RAILWAY_REPLICA_ID"),
    }
    deployment_identity_present = bool(
        deployment["id"]
        and deployment["replica_id"]
        and isinstance(deployment["commit_sha"], str)
        and SHA_RE.fullmatch(deployment["commit_sha"])
    )
    state_db = _state_db_status(home, required=configured)
    gateway = _gateway_status(home, gateway_pid)
    dashboard = _dashboard_status(dashboard_url, dashboard_pid)
    pressure = _process_pressure()
    checks = {
        "home_present": home.is_dir(),
        "cron_store_present": (home / "cron").is_dir(),
        "session_store_present": (home / "sessions").is_dir(),
        "state_db_readable": state_db["readable"],
        "gateway_ready": gateway["verified"] if configured else True,
        "dashboard_ready": dashboard["verified"],
        "zero_zombies": pressure["zombies"] == 0,
        "pid_headroom_ok": pressure["pid_headroom_ok"],
        "deployment_identity_present": deployment_identity_present,
    }
    ready = all(checks.values())
    return {
        "schema_version": 2,
        "checked_at": _now(),
        "status": "ready" if ready else "not_ready",
        "ready": ready,
        "deployment": deployment,
        "configured": configured,
        "gateway": {"manager_state": gateway_state, **gateway},
        "dashboard": dashboard,
        "state_db": state_db,
        "process_pressure": pressure,
        "checks": checks,
    }


def lifecycle_receipt(status: str, environ: dict[str, str] | None = None) -> dict:
    env = os.environ if environ is None else environ
    return {
        "schema_version": 2,
        "checked_at": _now(),
        "status": status,
        "ready": False,
        "deployment": {
            "id": env.get("RAILWAY_DEPLOYMENT_ID"),
            "commit_sha": env.get("RAILWAY_GIT_COMMIT_SHA"),
            "replica_id": env.get("RAILWAY_REPLICA_ID"),
        },
        "checks": {},
    }


def write_runtime_receipt(receipt: dict, path: str | Path) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    temporary.chmod(0o600)
    os.replace(temporary, target)
