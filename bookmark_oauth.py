"""Opt-in official bookmark reader. No cookies, unmetered retries or fallbacks."""
from datetime import datetime, timezone, timedelta
from contextlib import closing
from decimal import Decimal, InvalidOperation
import fcntl
import json
import os
from pathlib import Path
import re
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request

from x_oauth import OAuthConfig, TokenStore, XOAuthSession, XOAuthError

STATE = Path('/data/.hermes/state/x-bookmark-oauth')


class CollectionError(ValueError):
    pass


def pricing(env):
    try:
        cap = Decimal(env.get('X_BOOKMARK_MONTHLY_BUDGET_USD', '5'))
        rate = Decimal(env.get('X_BOOKMARK_POST_READ_USD', '0.005'))
    except InvalidOperation:
        raise CollectionError('Invalid bookmark pricing configuration') from None
    if not cap.is_finite() or not rate.is_finite() or not 0 < cap <= 5:
        raise CollectionError('Bookmark budget must be positive and at most $5')
    if rate not in (Decimal('0.001'), Decimal('0.005')):
        raise CollectionError('Unverified bookmark unit price')
    if rate == Decimal('0.001') and env.get('X_BOOKMARK_OWNED_READS_VERIFIED') != '1':
        raise CollectionError('Owned-read pricing requires explicit owner verification')
    return int(cap * 1_000_000), int(rate * 1_000_000)


def reserve(path, cap, cost, month=None, clock=time.time):
    """Durable pessimistic charge; never refund errors or assume provider dedup."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with closing(sqlite3.connect(path, timeout=30)) as db:
        os.chmod(path, 0o600)
        db.execute('PRAGMA synchronous=FULL')
        db.execute('CREATE TABLE IF NOT EXISTS reservations (month TEXT, amount INTEGER NOT NULL CHECK(amount > 0))')
        db.execute('BEGIN IMMEDIATE')
        # Determine the billing window AFTER obtaining the write lock. Requests
        # starting in the final120s may bill in either month: reserve both, in
        # one transaction, and never refund either conservative reservation.
        now = datetime.fromtimestamp(clock(), timezone.utc)
        next_month = (now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
                      + timedelta(days=32)).replace(day=1)
        months = [month or now.strftime('%Y-%m')]
        if month is None and (next_month - now).total_seconds() <= 120:
            months.append(next_month.strftime('%Y-%m'))
        if not isinstance(cost, int) or cost <= 0 or not 0 < cap <= 5_000_000:
            raise CollectionError('Monthly bookmark reservation cap reached; collection paused')
        for billing_month in months:
            spent = db.execute('SELECT COALESCE(SUM(amount),0) FROM reservations WHERE month=?', (billing_month,)).fetchone()[0]
            if spent + cost > cap:
                raise CollectionError('Monthly bookmark reservation cap reached; collection paused')
        db.executemany('INSERT INTO reservations VALUES (?,?)', [(m, cost) for m in months])
        db.commit()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def fetch(url, headers):
    try:
        with urllib.request.build_opener(NoRedirect()).open(urllib.request.Request(url, headers=headers), timeout=30) as response:
            return json.load(response)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        raise CollectionError('Official bookmark request failed; reservation retained') from None


def validate_user(user):
    if not isinstance(user, dict) or not isinstance(user.get('id'), str) or not user['id'].isdigit() or not isinstance(user.get('username'), str) or not re.fullmatch(r'[A-Za-z0-9_]{1,15}', user['username']):
        raise CollectionError('Malformed bookmark author')


def authors(payload, state_dir, cap, transport, headers, clock=time.time):
    """Durably cache actual author identities; no invented handles or free retries."""
    if not isinstance(payload, dict) or payload.get('errors'):
        raise CollectionError('Incomplete official bookmark response')
    rows = payload.get('data')
    if not isinstance(rows, list) or not 1 <= len(rows) <= 100:
        raise CollectionError('Expected 1–100 bookmarks; checkpoint unchanged')
    wanted = set()
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get('author_id'), str) or not re.fullmatch(r'[0-9]{1,19}', row['author_id']):
            raise CollectionError('Missing bookmark author identity')
        wanted.add(row['author_id'])
    cache_path = state_dir / 'authors.json'
    try:
        cache = json.loads(cache_path.read_text()) if cache_path.exists() else {}
        if not isinstance(cache, dict):
            raise ValueError()
        for key, user in cache.items():
            validate_user(user)
            if key != user['id']: raise ValueError()
    except (ValueError, OSError):
        raise CollectionError('Invalid author cache; collection paused') from None
    missing = sorted(wanted - set(cache))
    if missing:
        reserve(state_dir / 'budget.sqlite3', cap, len(missing) * 10_000, clock=clock)
        query = urllib.parse.urlencode({'ids': ','.join(missing), 'user.fields': 'id,name,username'})
        response = transport('https://api.x.com/2/users?' + query, headers)
        if not isinstance(response, dict) or response.get('errors') or not isinstance(response.get('data'), list):
            raise CollectionError('Incomplete bookmark author lookup')
        found = {}
        for user in response['data']:
            validate_user(user)
            if user['id'] not in missing or user['id'] in found:
                raise CollectionError('Unexpected bookmark author lookup result')
            found[user['id']] = user
        if set(found) != set(missing):
            raise CollectionError('Missing bookmark author lookup result')
        cache.update(found)
        TokenStore(cache_path).save(cache)
    return cache


def normalize(payload, cached_users=None):
    if not isinstance(payload, dict) or payload.get('errors'):
        raise CollectionError('Incomplete official bookmark response')
    rows = payload.get('data')
    if not isinstance(rows, list) or not 1 <= len(rows) <= 100:
        raise CollectionError('Expected 1–100 bookmarks; checkpoint unchanged')
    includes = payload.get('includes', {})
    users = dict(cached_users or {})
    if not isinstance(includes, dict):
        raise CollectionError('Malformed expanded bookmark authors')
    expanded = includes.get('users', [])
    if not isinstance(expanded, list):
        raise CollectionError('Malformed expanded bookmark authors')
    for user in expanded:
        validate_user(user)
        users[user['id']] = user
    posts = []
    ids = set()
    for row in rows:
        if not isinstance(row, dict):
            raise CollectionError('Malformed bookmark')
        ident, author_id = row.get('id'), row.get('author_id')
        if not isinstance(ident, str) or not re.fullmatch(r'[0-9]+', ident) or ident in ids:
            raise CollectionError('Invalid or duplicate bookmark identity')
        if not isinstance(author_id, str) or not author_id.isdigit():
            raise CollectionError('Missing bookmark author identity')
        note = row.get('note_tweet', {})
        if not isinstance(note, dict):
            raise CollectionError('Malformed long-form bookmark')
        text = note.get('text', row.get('text'))
        if not isinstance(text, str) or not text.strip():
            raise CollectionError('Missing bookmark text')
        author = users.get(author_id)
        if author is None:
            raise CollectionError('Missing bookmark author expansion')
        posts.append({'id': ident, 'text': text, 'createdAt': row.get('created_at', ''),
                      'author': {'id': author_id, 'username': author['username'],
                                 'name': author.get('name', author['username'])},
                      'url': f"https://x.com/{author['username']}/status/{ident}"})
        ids.add(ident)
    return posts


def collect(env=None, state_dir=STATE, transport=fetch, session_factory=XOAuthSession, clock=time.time):
    env = os.environ if env is None else env
    if env.get('X_BOOKMARK_PROVIDER') != 'oauth':
        raise CollectionError('Official bookmark reader is not enabled')
    user_id = env.get('X_BOOKMARK_USER_ID', '')
    if not re.fullmatch(r'[0-9]{1,19}', user_id):
        raise CollectionError('Verified bookmark user ID is required')
    cap, rate = pricing(env)
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Serialize refresh + persistence across all collector processes.
    with (state_dir / 'oauth.lock').open('a') as lock:
        os.chmod(state_dir / 'oauth.lock', 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        config = OAuthConfig(client_id=env.get('X_OAUTH_CLIENT_ID', ''),
                             client_secret=env.get('X_OAUTH_CLIENT_SECRET', ''))
        if not config.client_id:
            raise CollectionError('OAuth client ID is required')
        store = TokenStore(state_dir / 'tokens.json')
        if not store.path.exists():
            try:
                bootstrap = json.loads(env.get('X_BOOKMARK_OAUTH_BOOTSTRAP', ''))
            except json.JSONDecodeError:
                raise CollectionError('Private OAuth token bootstrap is required') from None
            if not isinstance(bootstrap, dict):
                raise CollectionError('Invalid OAuth token bootstrap')
            store.save(bootstrap)
        try:
            prior = store.load()
            if str(prior.get('user_id', '')) != user_id:
                raise CollectionError('OAuth authorization user does not match configured owner')
            token = session_factory(config, store).access_token()
        except (XOAuthError, ValueError, TypeError, OverflowError):
            raise CollectionError('OAuth authorization unavailable; reauthorization may be required') from None
        # Never assume provider duplicate-read discounts.
        reserve(state_dir / 'budget.sqlite3', cap, 100 * rate, clock=clock)
        query = urllib.parse.urlencode({'max_results': 100,
                                      'tweet.fields': 'id,text,created_at,author_id,note_tweet'})
        headers = {'Authorization': 'Bearer ' + token, 'Accept': 'application/json'}
        payload = transport(f'https://api.x.com/2/users/{user_id}/bookmarks?{query}', headers)
        cache = authors(payload, state_dir, cap, transport, headers, clock=clock)
        return normalize(payload, cache)
