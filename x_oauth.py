#!/usr/bin/env python3
"""Read-only X OAuth 2.0 PKCE session with refresh-token persistence.

The token file is deliberately outside the repository and is written atomically
with mode 0600.  This module requests no write scopes and never prints tokens.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import stat
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Callable

AUTHORIZE_URL = "https://x.com/i/oauth2/authorize"
TOKEN_URL = "https://api.x.com/2/oauth2/token"
DEFAULT_REDIRECT_URI = "http://127.0.0.1:8765/callback"
REQUIRED_SCOPES = ("bookmark.read", "tweet.read", "users.read", "offline.access")
REFRESH_SKEW_SECONDS = 60


class XOAuthError(RuntimeError):
    """Safe OAuth failure that never intentionally includes credentials."""


TokenTransport = Callable[[str, dict[str, str], dict[str, str]], dict[str, Any]]


def default_token_path() -> Path:
    configured = os.environ.get("X_OAUTH_TOKEN_FILE", "").strip()
    if configured:
        return Path(configured).expanduser()
    if Path("/data/.hermes").is_dir():
        return Path("/data/.hermes/state/x-user-token.json")
    return Path.home() / ".config" / "loomalpha" / "x-user-token.json"


def _safe_oauth_detail(payload: object) -> str:
    if not isinstance(payload, dict):
        return "request rejected"
    value = payload.get("error_description") or payload.get("error") or "request rejected"
    text = " ".join(str(value).split())[:240]
    lowered = text.lower()
    if any(marker in lowered for marker in ("token=", "code=", "secret=")):
        return "request rejected"
    return text


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _default_token_transport(
    url: str,
    data: dict[str, str],
    headers: dict[str, str],
) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=urllib.parse.urlencode(data).encode(),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=30) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as exc:
        try:
            payload = json.loads(exc.read().decode("utf-8", "replace"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            payload = None
        raise XOAuthError(f"X OAuth HTTP {exc.code}: request rejected") from None
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise XOAuthError(f"X OAuth request failed: {type(exc).__name__}") from None
    if not isinstance(payload, dict):
        raise XOAuthError("X OAuth returned a non-object response")
    return payload


def _base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def generate_pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    challenge = _base64url(hashlib.sha256(verifier.encode()).digest())
    return verifier, challenge


@dataclass(frozen=True)
class OAuthConfig:
    client_id: str
    client_secret: str = ""
    redirect_uri: str = DEFAULT_REDIRECT_URI

    @classmethod
    def from_env(
        cls,
        *,
        client_id: str | None = None,
        client_secret: str | None = None,
        redirect_uri: str | None = None,
    ) -> "OAuthConfig":
        resolved_id = (client_id or os.environ.get("X_OAUTH_CLIENT_ID", "")).strip()
        if not resolved_id:
            raise XOAuthError("X_OAUTH_CLIENT_ID is not configured")
        return cls(
            client_id=resolved_id,
            client_secret=(
                client_secret
                if client_secret is not None
                else os.environ.get("X_OAUTH_CLIENT_SECRET", "")
            ).strip(),
            redirect_uri=(
                redirect_uri
                or os.environ.get("X_OAUTH_REDIRECT_URI", "")
                or DEFAULT_REDIRECT_URI
            ).strip(),
        )


@dataclass(frozen=True)
class AuthorizationRequest:
    url: str
    state: str
    verifier: str


def prepare_authorization(config: OAuthConfig) -> AuthorizationRequest:
    verifier, challenge = generate_pkce()
    state = secrets.token_urlsafe(32)
    params = {
        "response_type": "code",
        "client_id": config.client_id,
        "redirect_uri": config.redirect_uri,
        "scope": " ".join(REQUIRED_SCOPES),
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    return AuthorizationRequest(
        url=f"{AUTHORIZE_URL}?{urllib.parse.urlencode(params)}",
        state=state,
        verifier=verifier,
    )


class LocalOAuthReceiver:
    """One-shot loopback callback receiver for the PKCE authorization code."""

    def __init__(self, redirect_uri: str, expected_state: str):
        parsed = urllib.parse.urlparse(redirect_uri)
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"}:
            raise XOAuthError("redirect URI must be an http://127.0.0.1 or localhost callback")
        if not parsed.port:
            raise XOAuthError("redirect URI must include an explicit loopback port")
        self.path = parsed.path or "/"
        self.expected_state = expected_state
        self.code: str | None = None
        self.error: str | None = None
        receiver = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - stdlib callback name
                request = urllib.parse.urlparse(self.path)
                if request.path != receiver.path:
                    self.send_error(404)
                    return
                params = urllib.parse.parse_qs(request.query)
                state = (params.get("state") or [""])[0]
                code = (params.get("code") or [""])[0]
                error = (params.get("error_description") or params.get("error") or [""])[0]
                if error:
                    receiver.error = "X authorization was denied or failed"
                elif not secrets.compare_digest(state, receiver.expected_state):
                    receiver.error = "OAuth state mismatch; authorization rejected"
                elif not code:
                    receiver.error = "X callback did not contain an authorization code"
                else:
                    receiver.code = code
                ok = receiver.code is not None
                body = (
                    "X bookmark authorization complete. You can close this tab."
                    if ok
                    else "X bookmark authorization failed. Return to the terminal for details."
                ).encode()
                self.send_response(200 if ok else 400)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _format: str, *_args: object) -> None:
                return

        self.server = HTTPServer((parsed.hostname, parsed.port), Handler)

    def wait(self, timeout_seconds: int) -> str:
        deadline = time.monotonic() + timeout_seconds
        self.server.timeout = 1
        try:
            while time.monotonic() < deadline and not self.code and not self.error:
                self.server.handle_request()
        finally:
            self.server.server_close()
        if self.error:
            raise XOAuthError(self.error)
        if not self.code:
            raise XOAuthError("timed out waiting for the X authorization callback")
        return self.code


class TokenStore:
    def __init__(self, path: Path):
        self.path = path

    def load(self) -> dict[str, Any]:
        try:
            mode = stat.S_IMODE(self.path.stat().st_mode)
            if mode & 0o077:
                raise XOAuthError("X OAuth token store permissions must be 0600")
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise XOAuthError(f"X user authorization is missing; run auth first ({self.path})") from None
        except (OSError, json.JSONDecodeError):
            raise XOAuthError("X OAuth token store is unreadable") from None
        if not isinstance(payload, dict):
            raise XOAuthError("X OAuth token store has an invalid format")
        return payload

    def save(self, payload: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix=".x-user-token-",
                delete=False,
            ) as handle:
                temporary = Path(handle.name)
                json.dump(payload, handle, separators=(",", ":"), sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.path)
            os.chmod(self.path, 0o600)
        finally:
            if temporary and temporary.exists():
                temporary.unlink()


class XOAuthSession:
    def __init__(
        self,
        config: OAuthConfig,
        store: TokenStore,
        *,
        transport: TokenTransport | None = None,
        now: Callable[[], float] | None = None,
    ):
        self.config = config
        self.store = store
        self.transport = transport or _default_token_transport
        self.now = now or time.time

    def _request_token(self, data: dict[str, str]) -> dict[str, Any]:
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "loomalpha-x-bookmarks/1.0",
        }
        body = dict(data)
        if self.config.client_secret:
            credential = f"{self.config.client_id}:{self.config.client_secret}".encode()
            headers["Authorization"] = "Basic " + base64.b64encode(credential).decode("ascii")
        else:
            body["client_id"] = self.config.client_id
        return self.transport(TOKEN_URL, body, headers)

    def _validated_token(
        self,
        payload: dict[str, Any],
        *,
        previous: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise XOAuthError("Malformed OAuth response")
        access_token = payload.get("access_token")
        if not isinstance(access_token, str):
            raise XOAuthError("Malformed OAuth access token")
        access_token = access_token.strip()
        if not access_token:
            raise XOAuthError("X OAuth response did not include an access token")
        scope_text = payload.get("scope") or (previous or {}).get("scope") or ""
        if not isinstance(scope_text, str):
            raise XOAuthError("Invalid OAuth scopes")
        scopes = set(scope_text.split())
        if scopes - set(REQUIRED_SCOPES):
            raise XOAuthError("Unexpected OAuth privileges; reauthorize read-only")
        missing = set(REQUIRED_SCOPES) - scopes
        if missing:
            raise XOAuthError("X authorization is missing required read/offline scopes: " + ", ".join(sorted(missing)))
        expires_in = payload.get("expires_in")
        if isinstance(expires_in, bool) or not isinstance(expires_in, int) or expires_in <= 0:
            raise XOAuthError("Invalid OAuth expiry")
        refresh_token = payload.get("refresh_token") or (previous or {}).get("refresh_token") or ""
        if not isinstance(refresh_token, str):
            raise XOAuthError("Invalid OAuth refresh token")
        refresh_token = refresh_token.strip()
        if not refresh_token:
            raise XOAuthError("X did not issue a refresh token; offline.access is required")
        result = dict(previous or {})
        result.update(
            {
                "access_token": access_token,
                "refresh_token": refresh_token,
                "token_type": str(payload.get("token_type") or "bearer"),
                "scope": " ".join(sorted(scopes)),
                "expires_at": int(self.now()) + max(1, expires_in),
                "client_id": self.config.client_id,
                "updated_at": int(self.now()),
            }
        )
        return result

    def exchange_code(self, code: str, verifier: str) -> dict[str, Any]:
        payload = self._request_token(
            {
                "code": code,
                "grant_type": "authorization_code",
                "redirect_uri": self.config.redirect_uri,
                "code_verifier": verifier,
            }
        )
        token = self._validated_token(payload)
        self.store.save(token)
        return token

    def refresh(self, previous: dict[str, Any] | None = None) -> dict[str, Any]:
        prior = previous or self.store.load()
        refresh_token = str(prior.get("refresh_token") or "").strip()
        if not refresh_token:
            raise XOAuthError("stored X authorization has no refresh token; run auth again")
        payload = self._request_token(
            {"refresh_token": refresh_token, "grant_type": "refresh_token"}
        )
        token = self._validated_token(payload, previous=prior)
        self.store.save(token)
        return token

    def token(self) -> dict[str, Any]:
        payload = self.store.load()
        if str(payload.get("client_id") or self.config.client_id) != self.config.client_id:
            raise XOAuthError("stored X authorization belongs to a different OAuth client")
        scope = payload.get("scope")
        if not isinstance(scope, str) or set(scope.split()) != set(REQUIRED_SCOPES):
            raise XOAuthError("Stored OAuth scopes must match read-only bookmark authorization")
        if not isinstance(payload.get("refresh_token"), str) or not payload['refresh_token']:
            raise XOAuthError("Stored OAuth refresh token is missing")
        if not isinstance(payload.get("access_token"), str) or not payload['access_token']:
            raise XOAuthError("Stored OAuth access token is missing")
        expires_at = int(payload.get("expires_at") or 0)
        if expires_at <= int(self.now()) + REFRESH_SKEW_SECONDS:
            payload = self.refresh(payload)
        return payload

    def access_token(self) -> str:
        token = str(self.token().get("access_token") or "").strip()
        if not token:
            raise XOAuthError("stored X authorization has no access token")
        return token
