import concurrent.futures
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import bookmark_oauth as B
from x_oauth import OAuthConfig, TokenStore, XOAuthSession, XOAuthError

TOKEN = {'access_token': 'test-access', 'refresh_token': 'test-refresh',
         'scope': 'bookmark.read tweet.read users.read offline.access',
         'expires_at': 9999999999, 'user_id': '123', 'client_id': 'test-client'}
PAYLOAD = {'data': [{'id': '1', 'author_id': '2', 'text': 'short', 'note_tweet': {'text': 'long'}}],
           'includes': {'users': [{'id': '2', 'username': 'author', 'name': 'Author'}]}}

class OAuthCollectorTests(unittest.TestCase):
    def test_concurrent_reservations_do_not_overspend(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / 'budget.sqlite3'
            def attempt(_):
                try:
                    B.reserve(path, 5_000_000, 1_100_000, '2026-10'); return True
                except B.CollectionError: return False
            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                self.assertEqual(sum(pool.map(attempt, range(12))), 4)
            with closing(sqlite3.connect(path)) as db:
                self.assertEqual(db.execute('SELECT SUM(amount) FROM reservations').fetchone()[0], 4_400_000)
            B.reserve(path, 5_000_000, 1_100_000, '2026-11')

    def test_pricing_fail_closed(self):
        for env in ({'X_BOOKMARK_MONTHLY_BUDGET_USD': '6'}, {'X_BOOKMARK_MONTHLY_BUDGET_USD': 'NaN'},
                    {'X_BOOKMARK_POST_READ_USD': '0.001'}, {'X_BOOKMARK_POST_READ_USD': '0'}):
            with self.assertRaises(B.CollectionError): B.pricing(env)
        self.assertEqual(B.pricing({'X_BOOKMARK_POST_READ_USD': '0.001', 'X_BOOKMARK_OWNED_READS_VERIFIED': '1'}), (5_000_000,1000))

    def test_normalization_and_malformed_payloads(self):
        post = B.normalize(PAYLOAD)[0]
        self.assertEqual(post['text'], 'long'); self.assertEqual(post['author']['username'], 'author')
        for payload in (None, {}, {'data': []}, {'data': [None]},
                        dict(PAYLOAD, errors=[{'detail': 'partial'}]),
                        dict(PAYLOAD, includes={}), dict(PAYLOAD, data=PAYLOAD['data'] * 2)):
            with self.assertRaises(B.CollectionError): B.normalize(payload)

    def test_refresh_rotation_persists_and_malformed_does_not_overwrite(self):
        with tempfile.TemporaryDirectory() as td:
            store = TokenStore(Path(td) / 'tokens.json'); store.save(dict(TOKEN, expires_at=0))
            fresh = dict(TOKEN, access_token='new-access', refresh_token='new-refresh', expires_in=7200)
            session = XOAuthSession(OAuthConfig('test-client'), store, transport=lambda *a: fresh)
            self.assertEqual(session.access_token(), 'new-access')
            self.assertEqual(store.load()['refresh_token'], 'new-refresh')
            self.assertEqual(store.path.stat().st_mode & 0o777, 0o600)
            for payload in ([], {}, dict(fresh, expires_in='invalid'), dict(fresh, access_token=[])):
                bad = XOAuthSession(OAuthConfig('test-client'), store, transport=lambda *a: payload)
                with self.assertRaises(XOAuthError): bad.refresh()
                self.assertEqual(store.load()['refresh_token'], 'new-refresh')

    def test_failure_reservation_retained_and_no_bird_fallback(self):
        with tempfile.TemporaryDirectory() as td:
            state=Path(td)
            env={'X_BOOKMARK_PROVIDER':'oauth', 'X_BOOKMARK_USER_ID':'123',
                 'X_OAUTH_CLIENT_ID':'test-client', 'X_BOOKMARK_OAUTH_BOOTSTRAP':json.dumps(TOKEN),
                 'X_BOOKMARK_POST_READ_USD':'0.001', 'X_BOOKMARK_OWNED_READS_VERIFIED':'1'}
            calls=[]
            def transport(url, headers):
                calls.append(url)
                self.assertIn('max_results=100',url); self.assertNotIn('expansions=',url)
                raise B.CollectionError('network failure')
            for _ in range(50):
                with self.assertRaises(B.CollectionError): B.collect(env,state,transport)
            with self.assertRaisesRegex(B.CollectionError,'cap reached'): B.collect(env,state,transport)
            self.assertEqual(len(calls),50)
            with self.assertRaisesRegex(B.CollectionError,'not enabled'): B.collect({},state,transport)

    def test_author_cache_charges_only_unknown_ids(self):
        with tempfile.TemporaryDirectory() as td:
            state=Path(td)
            env={'X_BOOKMARK_PROVIDER':'oauth','X_BOOKMARK_USER_ID':'123','X_OAUTH_CLIENT_ID':'test-client',
                 'X_BOOKMARK_OAUTH_BOOTSTRAP':json.dumps(TOKEN), 'X_BOOKMARK_POST_READ_USD':'0.001',
                 'X_BOOKMARK_OWNED_READS_VERIFIED':'1'}
            calls=[]
            def transport(url, headers):
                calls.append(url)
                return {'data':PAYLOAD['data']} if '/bookmarks?' in url else {'data':PAYLOAD['includes']['users']}
            for _ in range(2): self.assertEqual(B.collect(env,state,transport)[0]['author']['username'],'author')
            self.assertEqual(sum('/bookmarks?' not in url for url in calls),1)
            with closing(sqlite3.connect(state/'budget.sqlite3')) as db:
                self.assertEqual(db.execute('SELECT SUM(amount) FROM reservations').fetchone()[0],210_000)

    def test_author_cap_failure_never_returns_partial_snapshot(self):
        with tempfile.TemporaryDirectory() as td:
            state=Path(td)
            env={'X_BOOKMARK_PROVIDER':'oauth','X_BOOKMARK_USER_ID':'123','X_OAUTH_CLIENT_ID':'test-client',
                 'X_BOOKMARK_OAUTH_BOOTSTRAP':json.dumps(TOKEN), 'X_BOOKMARK_MONTHLY_BUDGET_USD':'0.1',
                 'X_BOOKMARK_POST_READ_USD':'0.001','X_BOOKMARK_OWNED_READS_VERIFIED':'1'}
            calls=[]
            def transport(url, headers): calls.append(url); return {'data':PAYLOAD['data']}
            with self.assertRaisesRegex(B.CollectionError,'cap reached'): B.collect(env,state,transport)
            self.assertEqual(len(calls),1)

    def test_malformed_author_lookup_never_caches_or_releases_reservation(self):
        for response in ({'data': []}, {'data': [{'id':'99','username':'other'}]},
                         {'data':PAYLOAD['includes']['users'], 'errors':[{}]}):
            with tempfile.TemporaryDirectory() as td:
                state=Path(td)
                with self.assertRaises(B.CollectionError):
                    B.authors({'data':PAYLOAD['data']},state,5_000_000,lambda *args:response,{})
                self.assertFalse((state/'authors.json').exists())
                with closing(sqlite3.connect(state/'budget.sqlite3')) as db:
                    self.assertEqual(db.execute('SELECT SUM(amount) FROM reservations').fetchone()[0],10_000)

    def test_existing_token_extra_scopes_and_wrong_client_fail_before_paid_read(self):
        for changes in ({'scope':TOKEN['scope'] + ' tweet.write'}, {'client_id':'other'}, {'user_id':'999'}):
            with tempfile.TemporaryDirectory() as td:
                state=Path(td); TokenStore(state/'tokens.json').save(dict(TOKEN,**changes))
                env={'X_BOOKMARK_PROVIDER':'oauth','X_BOOKMARK_USER_ID':'123','X_OAUTH_CLIENT_ID':'test-client'}
                with patch.object(B,'reserve') as reserve, patch.object(B,'fetch') as fetch:
                    with self.assertRaises(B.CollectionError): B.collect(env,state,fetch)
                    reserve.assert_not_called(); fetch.assert_not_called()

    def test_authorization_redirects_refused(self):
        import x_oauth
        for handler in (B.NoRedirect(),x_oauth.NoRedirect()):
            self.assertIsNone(handler.redirect_request(None,None,302,'',{},'https://other.test'))

    def test_month_boundary_bookmark_and_author_each_reserve_both_months(self):
        from datetime import datetime, timezone
        end = datetime(2026,12,31,23,59,30,tzinfo=timezone.utc).timestamp()
        with tempfile.TemporaryDirectory() as td:
            state=Path(td)
            env={'X_BOOKMARK_PROVIDER':'oauth','X_BOOKMARK_USER_ID':'123','X_OAUTH_CLIENT_ID':'test-client',
                 'X_BOOKMARK_OAUTH_BOOTSTRAP':json.dumps(TOKEN),'X_BOOKMARK_POST_READ_USD':'0.001',
                 'X_BOOKMARK_OWNED_READS_VERIFIED':'1'}
            B.collect(env,state,lambda url,h: {'data':PAYLOAD['data']} if '/bookmarks?' in url else {'data':PAYLOAD['includes']['users']},clock=lambda:end)
            with closing(sqlite3.connect(state/'budget.sqlite3')) as db:
                self.assertEqual(db.execute('SELECT month,SUM(amount) FROM reservations GROUP BY month ORDER BY month').fetchall(),
                                 [('2026-12',110_000),('2027-01',110_000)])

    def test_month_boundary_second_month_cap_rolls_back_both_reservations(self):
        from datetime import datetime, timezone
        end = datetime(2026,10,31,23,59,59,tzinfo=timezone.utc).timestamp()
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'budget.sqlite3'
            B.reserve(path,5_000_000,5_000_000,month='2026-11')
            with self.assertRaises(B.CollectionError): B.reserve(path,5_000_000,100_000,clock=lambda:end)
            with closing(sqlite3.connect(path)) as db:
                self.assertEqual(db.execute('SELECT month,SUM(amount) FROM reservations GROUP BY month').fetchall(), [('2026-11',5_000_000)])

    def test_author_lookup_after_midnight_charges_new_month(self):
        from datetime import datetime, timezone
        before=datetime(2026,10,31,23,59,59,tzinfo=timezone.utc).timestamp()
        after=before+2
        with tempfile.TemporaryDirectory() as td:
            state=Path(td)
            env={'X_BOOKMARK_PROVIDER':'oauth','X_BOOKMARK_USER_ID':'123','X_OAUTH_CLIENT_ID':'test-client',
                 'X_BOOKMARK_OAUTH_BOOTSTRAP':json.dumps(TOKEN),'X_BOOKMARK_POST_READ_USD':'0.001',
                 'X_BOOKMARK_OWNED_READS_VERIFIED':'1'}
            ticks=iter([before,after])
            B.collect(env,state,lambda url,h: {'data':PAYLOAD['data']} if '/bookmarks?' in url else {'data':PAYLOAD['includes']['users']},clock=lambda:next(ticks))
            with closing(sqlite3.connect(state/'budget.sqlite3')) as db:
                self.assertEqual(db.execute('SELECT month,SUM(amount) FROM reservations GROUP BY month ORDER BY month').fetchall(),
                                 [('2026-10',100_000),('2026-11',110_000)])

    def test_existing_rotated_token_ignores_bootstrap(self):
        with tempfile.TemporaryDirectory() as td:
            state=Path(td); TokenStore(state/'tokens.json').save(TOKEN)
            env={'X_BOOKMARK_PROVIDER':'oauth','X_BOOKMARK_USER_ID':'123','X_OAUTH_CLIENT_ID':'test-client',
                 'X_BOOKMARK_OAUTH_BOOTSTRAP':'malformed old bootstrap'}
            self.assertEqual(len(B.collect(env,state,lambda url, headers: PAYLOAD if '/bookmarks?' in url else {'data':PAYLOAD['includes']['users']})),1)

if __name__ == '__main__': unittest.main()
