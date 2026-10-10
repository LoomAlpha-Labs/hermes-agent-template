import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from starlette.testclient import TestClient

import server


class CapitalRouteTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.dashboard_dir = Path(self.tempdir.name)
        self.dir_patch = patch.object(server, "CAPITAL_DASHBOARD_DIR", self.dashboard_dir)
        self.dir_patch.start()
        self.client = TestClient(server.app)
        self.auth_headers = {
            "Cookie": f"{server.COOKIE_NAME}={server._make_auth_token()}"
        }

    def tearDown(self):
        self.client.close()
        self.dir_patch.stop()
        self.tempdir.cleanup()

    def _write_valid_dashboard(self, html=b"<!doctype html><title>Capital</title>"):
        digest = hashlib.sha256(html).hexdigest()
        receipt = {
            "stage": "static_html_rendered",
            "read_only": True,
            "network_requests": False,
            "counts": {
                "acceptance_checks_passed": 15,
                "deployable_projects": 0,
            },
            "html_sha256": digest,
        }
        (self.dashboard_dir / "dashboard.html").write_bytes(html)
        (self.dashboard_dir / "dashboard.html.receipt.json").write_text(
            json.dumps(receipt), encoding="utf-8"
        )
        return html, receipt, digest

    def test_get_and_head_serve_both_paths_with_security_headers(self):
        html, _, digest = self._write_valid_dashboard()
        for path in ("/capital", "/capital/"):
            with self.subTest(path=path):
                response = self.client.get(path, headers=self.auth_headers)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.content, html)
                self.assertEqual(response.headers["etag"], f'"{digest}"')
                self.assertEqual(response.headers["cache-control"], "no-store")
                self.assertEqual(response.headers["x-content-type-options"], "nosniff")
                self.assertEqual(response.headers["x-frame-options"], "SAMEORIGIN")
                self.assertIn("frame-ancestors 'self'", response.headers["content-security-policy"])
                self.assertIn("connect-src 'none'", response.headers["content-security-policy"])

                head = self.client.head(path, headers=self.auth_headers)
                self.assertEqual(head.status_code, 200)
                self.assertEqual(head.content, b"")
                self.assertEqual(head.headers["etag"], f'"{digest}"')
                self.assertEqual(int(head.headers["content-length"]), len(html))

    def test_unauthenticated_requests_use_existing_guard(self):
        self._write_valid_dashboard()
        html_response = self.client.get(
            "/capital", headers={"Accept": "text/html"}, follow_redirects=False
        )
        self.assertEqual(html_response.status_code, 302)
        self.assertEqual(html_response.headers["location"], "/login?returnTo=/capital")

        api_response = self.client.get("/capital", headers={"Accept": "application/json"})
        self.assertEqual(api_response.status_code, 401)
        self.assertEqual(api_response.json(), {"error": "Unauthorized"})

    def test_conditional_get_revalidates_then_returns_304(self):
        _, _, digest = self._write_valid_dashboard()
        response = self.client.get(
            "/capital",
            headers={**self.auth_headers, "If-None-Match": f'W/"{digest}"'},
        )
        self.assertEqual(response.status_code, 304)
        self.assertEqual(response.content, b"")
        self.assertEqual(response.headers["etag"], f'"{digest}"')
        self.assertEqual(response.headers["cache-control"], "no-store")

    def test_missing_or_invalid_artifacts_return_503_without_proxying(self):
        with patch.object(server, "_proxy_to_dashboard", new=AsyncMock()) as proxy:
            missing = self.client.get("/capital", headers=self.auth_headers)
            self.assertEqual(missing.status_code, 503)
            self.assertEqual(missing.text, "Capital dashboard unavailable\n")

            html, receipt, _ = self._write_valid_dashboard()
            invalid_receipts = (
                {**receipt, "stage": "draft"},
                {**receipt, "read_only": False},
                {**receipt, "network_requests": True},
                {**receipt, "counts": {**receipt["counts"], "acceptance_checks_passed": 14}},
                {**receipt, "counts": {**receipt["counts"], "deployable_projects": 1}},
                {**receipt, "html_sha256": "0" * 64},
            )
            for invalid_receipt in invalid_receipts:
                with self.subTest(receipt=invalid_receipt):
                    (self.dashboard_dir / "dashboard.html").write_bytes(html)
                    (self.dashboard_dir / "dashboard.html.receipt.json").write_text(
                        json.dumps(invalid_receipt), encoding="utf-8"
                    )
                    response = self.client.get("/capital", headers=self.auth_headers)
                    self.assertEqual(response.status_code, 503)
                    self.assertEqual(response.headers["cache-control"], "no-store")
            proxy.assert_not_awaited()

    def test_symlinked_artifacts_are_rejected(self):
        html, receipt, _ = self._write_valid_dashboard()
        real_html = self.dashboard_dir / "real.html"
        real_receipt = self.dashboard_dir / "real.receipt.json"
        real_html.write_bytes(html)
        real_receipt.write_text(json.dumps(receipt), encoding="utf-8")

        (self.dashboard_dir / "dashboard.html").unlink()
        (self.dashboard_dir / "dashboard.html").symlink_to(real_html)
        response = self.client.get("/capital", headers=self.auth_headers)
        self.assertEqual(response.status_code, 503)

        (self.dashboard_dir / "dashboard.html").unlink()
        (self.dashboard_dir / "dashboard.html").write_bytes(html)
        (self.dashboard_dir / "dashboard.html.receipt.json").unlink()
        (self.dashboard_dir / "dashboard.html.receipt.json").symlink_to(real_receipt)
        response = self.client.get("/capital", headers=self.auth_headers)
        self.assertEqual(response.status_code, 503)


if __name__ == "__main__":
    unittest.main()