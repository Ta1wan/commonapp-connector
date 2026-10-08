"""OAuth sign-in for the HTTP connector.

The server is the OAuth authorization server Claude talks to (discovery,
dynamic client registration, PKCE, refresh, revocation — handled by the MCP
SDK). It doesn't keep passwords: ``authorize`` sends the user to your
institution's OpenID Connect provider (Google Workspace, Microsoft Entra ID,
Okta, ...), and only users whose verified email is on the allowlist get a token.

    Claude --/authorize--> this server --redirect--> your IdP login
    Claude <--code-------- /oauth/callback <--code-- your IdP
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlencode, urlparse

import anyio
import requests
from starlette.requests import Request
from starlette.responses import PlainTextResponse, RedirectResponse, Response

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    RefreshToken,
    RegistrationError,
    TokenError,
    construct_redirect_uri,
)
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

log = logging.getLogger(__name__)

SCOPE = "commonapp"
CALLBACK_PATH = "/oauth/callback"
LOGIN_TTL = 600            # seconds a user has to finish signing in at the IdP
CODE_TTL = 300
ACCESS_TTL = 3600
REFRESH_TTL = 30 * 24 * 3600
DEFAULT_REDIRECT_HOSTS = ("claude.ai", "claude.com", "localhost", "127.0.0.1")


@dataclass
class OAuthSettings:
    public_url: str                  # where Claude reaches this server, e.g. https://commonapp.example.edu
    oidc_issuer: str                 # e.g. https://accounts.google.com
    client_id: str
    client_secret: str
    allowed_emails: set[str] = field(default_factory=set)
    allowed_domains: set[str] = field(default_factory=set)
    # Hosts that dynamically registered clients may redirect to. Stops a stranger
    # registering a client that sends staff tokens to their own site.
    allowed_redirect_hosts: set[str] = field(default_factory=lambda: set(DEFAULT_REDIRECT_HOSTS))
    db_path: str = "./.state/auth.db"
    upstream_scopes: str = "openid email profile"

    def __post_init__(self):
        self.public_url = self.public_url.rstrip("/")
        self.oidc_issuer = self.oidc_issuer.rstrip("/")
        self.allowed_emails = {e.strip().lower() for e in self.allowed_emails if e.strip()}
        self.allowed_domains = {d.strip().lower().lstrip("@") for d in self.allowed_domains if d.strip()}
        if not (self.allowed_emails or self.allowed_domains):
            raise ValueError("Set COMMONAPP_ALLOWED_EMAILS and/or COMMONAPP_ALLOWED_DOMAINS: "
                             "without an allowlist anyone with an account at the IdP could sign in.")
        if not self.public_url.startswith("https://") and urlparse(self.public_url).hostname not in (
                "localhost", "127.0.0.1"):
            raise ValueError("COMMONAPP_PUBLIC_URL must be https:// (OAuth tokens travel over it)")

    @property
    def resource_url(self) -> str:
        return f"{self.public_url}/mcp"

    @classmethod
    def from_env(cls, env) -> "OAuthSettings | None":
        def get(name, default=""):
            return (env.get(f"COMMONAPP_{name}") or default).strip()

        def split(name):
            return {x for x in get(name).replace(";", ",").split(",") if x.strip()}

        if not get("OIDC_ISSUER"):
            return None
        missing = [n for n in ("PUBLIC_URL", "OIDC_CLIENT_ID", "OIDC_CLIENT_SECRET") if not get(n)]
        if missing:
            raise ValueError("OAuth needs " + ", ".join(f"COMMONAPP_{n}" for n in missing))
        kwargs = {}
        if get("ALLOWED_REDIRECT_HOSTS"):
            kwargs["allowed_redirect_hosts"] = {h.strip().lower() for h in split("ALLOWED_REDIRECT_HOSTS")}
        return cls(
            public_url=get("PUBLIC_URL"), oidc_issuer=get("OIDC_ISSUER"),
            client_id=get("OIDC_CLIENT_ID"), client_secret=get("OIDC_CLIENT_SECRET"),
            allowed_emails=split("ALLOWED_EMAILS"), allowed_domains=split("ALLOWED_DOMAINS"),
            db_path=get("AUTH_DB_PATH", "./.state/auth.db"),
            upstream_scopes=get("OIDC_SCOPES", "openid email profile"),
            **kwargs,
        )

    def auth_settings(self) -> AuthSettings:
        return AuthSettings(
            issuer_url=self.public_url,
            resource_server_url=self.resource_url,
            validate_token_resource=True,
            required_scopes=[SCOPE],
            client_registration_options=ClientRegistrationOptions(
                enabled=True, valid_scopes=[SCOPE], default_scopes=[SCOPE]),
            revocation_options=RevocationOptions(enabled=True),
        )

    def is_allowed(self, email: str) -> bool:
        email = email.lower()
        return email in self.allowed_emails or email.rpartition("@")[2] in self.allowed_domains


_SCHEMA = """
CREATE TABLE IF NOT EXISTS clients (client_id TEXT PRIMARY KEY, info TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS pending (state TEXT PRIMARY KEY, data TEXT NOT NULL, expires_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS codes (code TEXT PRIMARY KEY, data TEXT NOT NULL, expires_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS tokens (
    token_hash TEXT PRIMARY KEY, kind TEXT NOT NULL, family TEXT NOT NULL,
    data TEXT NOT NULL, expires_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tokens_family ON tokens(family);
"""


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class OIDCAuthProvider:
    """OAuthAuthorizationServerProvider that delegates login to an OIDC provider.

    Codes and tokens are stored hashed in SQLite, so restarts don't sign people out
    and a leaked database doesn't leak usable tokens.
    """

    def __init__(self, settings: OAuthSettings, http: requests.Session | None = None):
        self.settings = settings
        self.http = http or requests.Session()
        self._oidc: dict | None = None
        self._lock = threading.Lock()
        Path(settings.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(settings.db_path, check_same_thread=False)
        self._db.executescript(_SCHEMA)

    # -- storage helpers ---------------------------------------------------

    def _exec(self, sql: str, params=()) -> list[tuple]:
        with self._lock, self._db:
            return self._db.execute(sql, params).fetchall()

    def _take(self, table: str, key_col: str, key: str) -> dict | None:
        """Fetch and delete a one-time row (pending login or auth code)."""
        with self._lock, self._db:
            row = self._db.execute(
                f"SELECT data, expires_at FROM {table} WHERE {key_col} = ?", (key,)).fetchone()
            self._db.execute(f"DELETE FROM {table} WHERE {key_col} = ?", (key,))
            self._db.execute(f"DELETE FROM {table} WHERE expires_at < ?", (time.time(),))
        if not row or row[1] < time.time():
            return None
        return json.loads(row[0])

    # -- clients -----------------------------------------------------------

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        rows = self._exec("SELECT info FROM clients WHERE client_id = ?", (client_id,))
        return OAuthClientInformationFull.model_validate_json(rows[0][0]) if rows else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        for uri in client_info.redirect_uris or []:
            if (urlparse(str(uri)).hostname or "").lower() not in self.settings.allowed_redirect_hosts:
                raise RegistrationError("invalid_redirect_uri",
                                        f"Redirects to {urlparse(str(uri)).hostname} are not allowed")
        self._exec("INSERT OR REPLACE INTO clients VALUES (?, ?)",
                   (client_info.client_id, client_info.model_dump_json()))

    # -- authorization: hand off to the IdP ---------------------------------

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        if params.resource and params.resource.rstrip("/") != self.settings.resource_url:
            raise AuthorizeError("invalid_target", "Unknown resource")
        oidc = await self._discovery()
        state = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(48)
        pending = {
            "client_id": client.client_id,
            "params": params.model_dump(mode="json"),
            "verifier": verifier,
            "nonce": secrets.token_urlsafe(16),
        }
        self._exec("INSERT INTO pending VALUES (?, ?, ?)",
                   (state, json.dumps(pending), time.time() + LOGIN_TTL))
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        return oidc["authorization_endpoint"] + "?" + urlencode({
            "response_type": "code",
            "client_id": self.settings.client_id,
            "redirect_uri": self.settings.public_url + CALLBACK_PATH,
            "scope": self.settings.upstream_scopes,
            "state": state,
            "nonce": pending["nonce"],
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "prompt": "select_account",
        })

    async def handle_callback(self, request: Request) -> Response:
        """The IdP redirects here after the user signs in."""
        pending = self._take("pending", "state", request.query_params.get("state", ""))
        if pending is None:
            return PlainTextResponse("This sign-in link has expired. Start again from Claude.", 400)
        params = AuthorizationParams.model_validate(pending["params"])

        def back_to_client(**query) -> Response:
            return RedirectResponse(construct_redirect_uri(str(params.redirect_uri), state=params.state, **query),
                                    status_code=302)

        if "error" in request.query_params or "code" not in request.query_params:
            return back_to_client(error="access_denied",
                                  error_description=request.query_params.get("error_description", "Sign-in cancelled"))
        try:
            email = await self._identify(request.query_params["code"], pending["verifier"])
        except Exception:
            log.exception("OIDC sign-in failed")
            return back_to_client(error="server_error", error_description="Could not verify identity")

        if not email or not self.settings.is_allowed(email):
            log.warning("sign-in refused for %s", email or "<no verified email>")
            return back_to_client(error="access_denied",
                                  error_description=f"{email or 'This account'} is not authorized")

        code = secrets.token_urlsafe(32)
        auth_code = AuthorizationCode(
            code=code, scopes=params.scopes or [SCOPE], expires_at=time.time() + CODE_TTL,
            client_id=pending["client_id"], code_challenge=params.code_challenge,
            redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            resource=self.settings.resource_url, subject=email,
        )
        self._exec("INSERT INTO codes VALUES (?, ?, ?)",
                   (_hash(code), auth_code.model_dump_json(), auth_code.expires_at))
        log.info("signed in %s (client %s)", email, pending["client_id"])
        return back_to_client(code=code)

    async def _discovery(self) -> dict:
        if self._oidc is None:
            url = self.settings.oidc_issuer + "/.well-known/openid-configuration"
            resp = await anyio.to_thread.run_sync(lambda: self.http.get(url, timeout=10))
            resp.raise_for_status()
            self._oidc = resp.json()
        return self._oidc

    async def _identify(self, code: str, verifier: str) -> str | None:
        """Exchange the IdP code and return the user's verified email.

        Identity comes from the userinfo endpoint, called with the access token we
        just received directly from the IdP over TLS.
        """
        oidc = await self._discovery()

        def call() -> dict:
            token = self.http.post(oidc["token_endpoint"], timeout=15, data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self.settings.public_url + CALLBACK_PATH,
                "client_id": self.settings.client_id,
                "client_secret": self.settings.client_secret,
                "code_verifier": verifier,
            })
            token.raise_for_status()
            info = self.http.get(oidc["userinfo_endpoint"], timeout=15, headers={
                "Authorization": f"Bearer {token.json()['access_token']}"})
            info.raise_for_status()
            return info.json()

        info = await anyio.to_thread.run_sync(call)
        if info.get("email_verified") is False:
            return None
        return (info.get("email") or "").strip().lower() or None

    # -- codes and tokens --------------------------------------------------

    async def load_authorization_code(self, client, authorization_code: str) -> AuthorizationCode | None:
        rows = self._exec("SELECT data FROM codes WHERE code = ?", (_hash(authorization_code),))
        if not rows:
            return None
        code = AuthorizationCode.model_validate_json(rows[0][0])
        return code if code.client_id == client.client_id and code.expires_at > time.time() else None

    async def exchange_authorization_code(self, client, authorization_code: AuthorizationCode) -> OAuthToken:
        if self._take("codes", "code", _hash(authorization_code.code)) is None:
            raise TokenError("invalid_grant", "Authorization code already used or expired")
        return self._issue(client.client_id, authorization_code.scopes, authorization_code.subject,
                           family=secrets.token_hex(16))

    async def load_refresh_token(self, client, refresh_token: str) -> RefreshToken | None:
        token = self._load(refresh_token, "refresh")
        return token if token and token.client_id == client.client_id else None

    async def exchange_refresh_token(self, client, refresh_token: RefreshToken, scopes: list[str]) -> OAuthToken:
        rows = self._exec("SELECT family FROM tokens WHERE token_hash = ?", (_hash(refresh_token.token),))
        if not rows:
            raise TokenError("invalid_grant", "Refresh token revoked")
        family = rows[0][0]
        # Rotate: the old refresh token and its access tokens stop working.
        self._exec("DELETE FROM tokens WHERE family = ?", (family,))
        return self._issue(client.client_id, scopes or refresh_token.scopes, refresh_token.subject, family)

    async def load_access_token(self, token: str) -> AccessToken | None:
        return self._load(token, "access")

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        rows = self._exec("SELECT family FROM tokens WHERE token_hash = ?", (_hash(token.token),))
        if rows:
            self._exec("DELETE FROM tokens WHERE family = ?", (rows[0][0],))

    def _issue(self, client_id: str, scopes: list[str], subject: str | None, family: str) -> OAuthToken:
        now = int(time.time())
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        common = dict(client_id=client_id, scopes=scopes, resource=self.settings.resource_url, subject=subject)
        rows = [
            (_hash(access), "access", family,
             AccessToken(token="", expires_at=now + ACCESS_TTL, **common).model_dump_json(), now + ACCESS_TTL),
            (_hash(refresh), "refresh", family,
             RefreshToken(token="", expires_at=now + REFRESH_TTL, **common).model_dump_json(), now + REFRESH_TTL),
        ]
        with self._lock, self._db:
            self._db.execute("DELETE FROM tokens WHERE expires_at < ?", (now,))
            self._db.executemany("INSERT INTO tokens VALUES (?, ?, ?, ?, ?)", rows)
        return OAuthToken(access_token=access, token_type="Bearer", expires_in=ACCESS_TTL,
                          refresh_token=refresh, scope=" ".join(scopes))

    def _load(self, token: str, kind: str):
        rows = self._exec("SELECT data, expires_at FROM tokens WHERE token_hash = ? AND kind = ?",
                          (_hash(token), kind))
        if not rows or rows[0][1] < time.time():
            return None
        model = AccessToken if kind == "access" else RefreshToken
        return model.model_validate_json(rows[0][0]).model_copy(update={"token": token})
