import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from starlette.testclient import TestClient

import runtime_readiness
import server


GOOD_PRESSURE = {
    "zombies": 0,
    "pids_current": "12",
    "pids_max": "1000",
    "pid_headroom": 988,
    "pid_headroom_required": 50,
    "pid_headroom_ok": True,
}
GOOD_GATEWAY = {
    "verified": True,
    "live_pid": 10,
    "runtime_state": "running",
    "session_store": "ok",
    "connected_platforms": ["telegram"],
}
GOOD_DASHBOARD = {"verified": True, "pid": 11, "http_status": 200}


class RuntimeReadinessTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.home = Path(self.tempdir.name)
        (self.home / "cron").mkdir()
        (self.home / "sessions").mkdir()
        with sqlite3.connect(self.home / "state.db") as conn:
            conn.execute("CREATE TABLE receipt_test (id INTEGER PRIMARY KEY)")

    def tearDown(self):
        self.tempdir.cleanup()

    def _collect(self, *, configured=True, environ=None):
        if environ is None:
            environ = {
                "RAILWAY_DEPLOYMENT_ID": "deployment-id",
                "RAILWAY_GIT_COMMIT_SHA": "a" * 40,
                "RAILWAY_REPLICA_ID": "replica-id",
            }
        return runtime_readiness.collect_runtime_readiness(
            hermes_home=self.home,
            configured=configured,
            gateway_state="running" if configured else "stopped",
            gateway_pid=10 if configured else None,
            dashboard_pid=11,
            environ=environ,
        )

    def test_ready_receipt_requires_functional_gateway_and_dashboard(self):
        with (
            patch.object(runtime_readiness, "_gateway_status", return_value=GOOD_GATEWAY),
            patch.object(runtime_readiness, "_dashboard_status", return_value=GOOD_DASHBOARD),
            patch.object(runtime_readiness, "_process_pressure", return_value=GOOD_PRESSURE),
        ):
            receipt = self._collect()
        self.assertTrue(receipt["ready"])
        self.assertEqual(receipt["state_db"]["status"], "ready")
        self.assertEqual(receipt["deployment"]["commit_sha"], "a" * 40)

    def test_spawned_but_unverified_children_fail_readiness(self):
        gateway = {**GOOD_GATEWAY, "verified": False}
        dashboard = {**GOOD_DASHBOARD, "verified": False}
        with (
            patch.object(runtime_readiness, "_gateway_status", return_value=gateway),
            patch.object(runtime_readiness, "_dashboard_status", return_value=dashboard),
            patch.object(runtime_readiness, "_process_pressure", return_value=GOOD_PRESSURE),
        ):
            receipt = self._collect()
        self.assertFalse(receipt["ready"])
        self.assertFalse(receipt["checks"]["gateway_ready"])
        self.assertFalse(receipt["checks"]["dashboard_ready"])

    def test_configured_runtime_requires_existing_state_database(self):
        (self.home / "state.db").unlink()
        with (
            patch.object(runtime_readiness, "_gateway_status", return_value=GOOD_GATEWAY),
            patch.object(runtime_readiness, "_dashboard_status", return_value=GOOD_DASHBOARD),
            patch.object(runtime_readiness, "_process_pressure", return_value=GOOD_PRESSURE),
        ):
            receipt = self._collect(configured=True)
        self.assertFalse(receipt["ready"])
        self.assertEqual(receipt["state_db"]["status"], "absent")
        self.assertFalse(receipt["checks"]["state_db_readable"])

    def test_missing_deployment_identity_fails_readiness(self):
        with (
            patch.object(runtime_readiness, "_gateway_status", return_value=GOOD_GATEWAY),
            patch.object(runtime_readiness, "_dashboard_status", return_value=GOOD_DASHBOARD),
            patch.object(runtime_readiness, "_process_pressure", return_value=GOOD_PRESSURE),
        ):
            receipt = self._collect(environ={})
        self.assertFalse(receipt["ready"])
        self.assertFalse(receipt["checks"]["deployment_identity_present"])

    def test_unconfigured_template_allows_absent_database_and_gateway(self):
        (self.home / "state.db").unlink()
        with (
            patch.object(runtime_readiness, "_gateway_status", return_value={**GOOD_GATEWAY, "verified": False}),
            patch.object(runtime_readiness, "_dashboard_status", return_value=GOOD_DASHBOARD),
            patch.object(runtime_readiness, "_process_pressure", return_value=GOOD_PRESSURE),
        ):
            receipt = self._collect(configured=False)
        self.assertTrue(receipt["ready"])

    def test_zombie_or_exhausted_pid_headroom_fails_readiness(self):
        bad_pressure = {
            **GOOD_PRESSURE,
            "zombies": 1,
            "pids_current": "1000",
            "pids_max": "1000",
            "pid_headroom": 0,
            "pid_headroom_ok": False,
        }
        with (
            patch.object(runtime_readiness, "_gateway_status", return_value=GOOD_GATEWAY),
            patch.object(runtime_readiness, "_dashboard_status", return_value=GOOD_DASHBOARD),
            patch.object(runtime_readiness, "_process_pressure", return_value=bad_pressure),
        ):
            receipt = self._collect()
        self.assertFalse(receipt["ready"])
        self.assertFalse(receipt["checks"]["zero_zombies"])
        self.assertFalse(receipt["checks"]["pid_headroom_ok"])

    def test_receipt_write_is_atomic_and_private(self):
        target = self.home / "state" / "runtime-readiness.json"
        runtime_readiness.write_runtime_receipt({"status": "ready"}, target)
        self.assertEqual(json.loads(target.read_text()), {"status": "ready"})
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        self.assertEqual(list(target.parent.glob("*.tmp")), [])


class RuntimeReadinessRouteTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(server.app)

    def tearDown(self):
        self.client.close()

    def _receipt(self, ready: bool) -> dict:
        return {
            "schema_version": 2,
            "status": "ready" if ready else "not_ready",
            "ready": ready,
            "checked_at": "2026-10-17T00:00:00+00:00",
            "deployment": {
                "commit_sha": "a" * 40,
                "id": "private-deployment-id",
                "replica_id": "private-replica-id",
            },
        }

    def test_public_readiness_is_minimal_cached_and_no_store(self):
        receipt = self._receipt(True)
        with (
            patch.object(server, "_runtime_snapshot", receipt),
            patch.object(server, "_runtime_snapshot_at", time.monotonic()),
            patch.object(server, "_collect_runtime_readiness", side_effect=AssertionError("must not probe")),
        ):
            response = self.client.get("/health/ready")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                "status": "ready",
                "checked_at": "2026-10-17T00:00:00+00:00",
                "commit_sha_prefix": "a" * 12,
                "fresh": True,
            },
        )
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")
        self.assertNotIn("private-deployment-id", response.text)

    def test_stale_snapshot_returns_503(self):
        with (
            patch.object(server, "_runtime_snapshot", self._receipt(True)),
            patch.object(
                server,
                "_runtime_snapshot_at",
                time.monotonic() - server.RUNTIME_READINESS_MAX_AGE_S - 1,
            ),
        ):
            response = self.client.get("/health/ready")
        self.assertEqual(response.status_code, 503)
        self.assertFalse(response.json()["fresh"])

    def test_missing_commit_identity_returns_503(self):
        receipt = self._receipt(True)
        receipt["deployment"]["commit_sha"] = None
        with (
            patch.object(server, "_runtime_snapshot", receipt),
            patch.object(server, "_runtime_snapshot_at", time.monotonic()),
        ):
            response = self.client.get("/health/ready")
        self.assertEqual(response.status_code, 503)
        self.assertIsNone(response.json()["commit_sha_prefix"])

    def test_not_ready_returns_503(self):
        with (
            patch.object(server, "_runtime_snapshot", self._receipt(False)),
            patch.object(server, "_runtime_snapshot_at", time.monotonic()),
        ):
            response = self.client.get("/health/ready")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["status"], "not_ready")


if __name__ == "__main__":
    unittest.main()
