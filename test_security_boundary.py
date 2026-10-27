import os
import unittest
from unittest.mock import AsyncMock, patch
from starlette.testclient import TestClient
import server

class BrowserBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(server.app)
        self.cookie = {"Cookie": f"{server.COOKIE_NAME}={server._make_auth_token()}"}
    def tearDown(self):
        self.client.close()
    def test_return_to_rejects_browser_normalized_external_paths(self):
        for value in ['//evil.example', '/\\evil.example', '/\r\nLocation:evil', 'https://evil.example']:
            self.assertEqual(server._safe_return_to(value), '/')
        self.assertEqual(server._safe_return_to('/capital?q=ok'), '/capital?q=ok')
    def test_authenticated_foreign_origin_rejected_before_handlers(self):
        with patch.object(server.gw, 'restart', new_callable=AsyncMock) as restart:
            response = self.client.post('/setup/api/gateway/restart', headers={**self.cookie, 'Origin': 'http://sibling.testserver'})
            self.assertEqual(response.status_code, 403)
            restart.assert_not_awaited()
    def test_matching_origin_and_cli_remain_allowed(self):
        for origin in [None, 'http://testserver']:
            headers = {**self.cookie, **({'Origin': origin} if origin else {})}
            self.assertEqual(self.client.get('/setup/api/status', headers=headers).status_code, 200)
    def test_login_rejects_foreign_origin_and_secure_on_https(self):
        response = self.client.post('/login', data={'username':'x','password':'x'}, headers={'Origin':'https://evil.example'})
        self.assertEqual(response.status_code, 403)
        with patch.object(server, 'ADMIN_PASSWORD', 'test-only'), patch.object(server, 'ADMIN_USERNAME', 'admin'):
            response = self.client.post('https://testserver/login', data={'username':'admin','password':'test-only'}, follow_redirects=False)
        self.assertEqual(response.status_code,302)
        self.assertIn('Secure',response.headers['set-cookie'])
        self.assertIn('HttpOnly',response.headers['set-cookie'])
    def test_pairing_paths_reject_traversal(self):
        for endpoint in ['approve','deny','revoke']:
            response = self.client.post('/setup/api/pairing/'+endpoint, json={'platform':'../../escape','code':'entry','user_id':'user'}, headers=self.cookie)
            self.assertEqual(response.status_code,400)
    def test_websocket_foreign_origin_rejected_before_upstream_connect(self):
        with patch.object(server.websockets, 'connect') as connect:
            with self.assertRaises(Exception):
                with self.client.websocket_connect('/api/pty',headers={**self.cookie,'Origin':'https://evil.example'}):
                    pass
            connect.assert_not_called()

    def test_pairing_rejects_malformed_json_shapes(self):
        for endpoint in ['approve', 'deny', 'revoke']:
            for body in [[], {'platform':'telegram', 'code':123, 'user_id':[]}]:
                response = self.client.post('/setup/api/pairing/'+endpoint, json=body, headers=self.cookie)
                self.assertEqual(response.status_code,400)
