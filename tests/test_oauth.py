import base64
import hashlib
import secrets
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlparse

from starlette.testclient import TestClient

from mcp.server.transport_security import TransportSecuritySettings

from commonapp_connector.auth import OAuthSettings, OIDCAuthProvider
from commonapp_connector.config import Settings
from commonapp_connector.server import create_server

FIXTURE = Path(__file__).parent / "fixtures" / "sample_export.txt"
PUBLIC = "https://commonapp.example.edu"
CLIENT_REDIRECT = "https://claude.ai/api/mcp/auth_callback"
DISCOVERY = {
    "authorization_endpoint": "https://idp.example.com/authorize",
    "token_endpoint": "https://idp.example.com/token",
    "userinfo_endpoint": "https://idp.example.com/userinfo",
}
MCP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


def fake_idp(email, verified=True):
    """A requests.Session stand-in for the upstream OpenID Connect provider."""
    def get(url, **kw):
        body = DISCOVERY if url.endswith("openid-configuration") else {"email": email, "email_verified": verified}
        return mock.Mock(status_code=200, json=lambda: body, raise_for_status=lambda: None)

    def post(url, data=None, **kw):
        assert data["client_secret"] == "idp-secret" and data["code_verifier"]
        return mock.Mock(status_code=200, json=lambda: {"access_token": "upstream"}, raise_for_status=lambda: None)

    return mock.Mock(get=mock.Mock(side_effect=get), post=mock.Mock(side_effect=post))


class OAuthFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        (root / "inbox").mkdir()
        shutil.copy(FIXTURE, root / "inbox" / "export_1.txt")
        self.settings = Settings(local_dir=str(root / "inbox"), state_path=str(root / "state.json"),
                                 db_path=str(root / "apps.db"))
        self.oauth = OAuthSettings(public_url=PUBLIC, oidc_issuer="https://idp.example.com",
                                   client_id="idp-client", client_secret="idp-secret",
                                   allowed_domains={"example.edu"}, allowed_emails={"guest@gmail.com"},
                                   db_path=str(root / "auth.db"))

    def tearDown(self):
        self.tmp.cleanup()

    def client(self, email="admissions@example.edu", verified=True):
        provider = OIDCAuthProvider(self.oauth, http=fake_idp(email, verified))
        app = create_server(self.settings, self.oauth, provider).streamable_http_app(
            stateless_http=True, json_response=True,
            transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False))
        return TestClient(app, base_url=PUBLIC, follow_redirects=False)

    def sign_in(self, http, redirect=CLIENT_REDIRECT):
        """Run Claude's side of the flow: register, authorize, IdP callback. Returns the callback query."""
        reg = http.post("/register", json={"redirect_uris": [redirect], "client_name": "Claude",
                                            "token_endpoint_auth_method": "none"})
        self.assertEqual(reg.status_code, 201, reg.text)
        client_id = reg.json()["client_id"]
        verifier = secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        auth = http.get("/authorize", params={
            "response_type": "code", "client_id": client_id, "redirect_uri": redirect,
            "code_challenge": challenge, "code_challenge_method": "S256", "state": "claude-state",
            "scope": "commonapp", "resource": f"{PUBLIC}/mcp"})
        self.assertEqual(auth.status_code, 302, auth.text)
        idp = urlparse(auth.headers["location"])
        idp_q = parse_qs(idp.query)
        self.assertEqual(f"{idp.scheme}://{idp.netloc}{idp.path}", DISCOVERY["authorization_endpoint"])
        self.assertEqual(idp_q["redirect_uri"], [f"{PUBLIC}/oauth/callback"])
        cb = http.get("/oauth/callback", params={"code": "idp-code", "state": idp_q["state"][0]})
        self.assertEqual(cb.status_code, 302)
        back = urlparse(cb.headers["location"])
        self.assertEqual(f"{back.scheme}://{back.netloc}{back.path}", redirect)
        q = {k: v[0] for k, v in parse_qs(back.query).items()}
        self.assertEqual(q["state"], "claude-state")
        return client_id, verifier, q

    def token(self, http, client_id, verifier, code, redirect=CLIENT_REDIRECT):
        return http.post("/token", data={"grant_type": "authorization_code", "code": code,
                                         "client_id": client_id, "code_verifier": verifier,
                                         "redirect_uri": redirect, "resource": f"{PUBLIC}/mcp"})

    def mcp(self, http, method, params=None, token=None):
        headers = dict(MCP_HEADERS)
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return http.post("/mcp", headers=headers,
                         json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}})

    def test_discovery_metadata(self):
        with self.client() as http:
            rs = http.get("/.well-known/oauth-protected-resource/mcp").json()
            self.assertEqual([u.rstrip("/") for u in rs["authorization_servers"]], [PUBLIC])
            self.assertEqual(rs["resource"], f"{PUBLIC}/mcp")
            asm = http.get("/.well-known/oauth-authorization-server").json()
            self.assertIn("registration_endpoint", asm)
            self.assertIn("S256", asm["code_challenge_methods_supported"])

    def test_mcp_requires_token(self):
        with self.client() as http:
            resp = self.mcp(http, "tools/list")
            self.assertEqual(resp.status_code, 401)
            self.assertIn("resource_metadata", resp.headers["www-authenticate"])
            self.assertEqual(self.mcp(http, "tools/list", token="forged").status_code, 401)

    def test_full_sign_in_then_tool_call(self):
        with self.client() as http, self.assertLogs("commonapp_connector.audit", "INFO") as audit:
            client_id, verifier, q = self.sign_in(http)
            tok = self.token(http, client_id, verifier, q["code"])
            self.assertEqual(tok.status_code, 200, tok.text)
            access = tok.json()["access_token"]
            self.assertEqual(self.token(http, client_id, verifier, q["code"]).status_code, 400)  # code is one-time

            resp = self.mcp(http, "tools/call", {"name": "sync_exports", "arguments": {}}, token=access)
            self.assertEqual(resp.status_code, 200, resp.text)
            self.assertFalse(resp.json()["result"].get("isError"))
        self.assertIn("admissions@example.edu called sync_exports", audit.output[0])

    def test_refresh_rotates_and_revokes_old_tokens(self):
        with self.client() as http:
            client_id, verifier, q = self.sign_in(http)
            first = self.token(http, client_id, verifier, q["code"]).json()
            refreshed = http.post("/token", data={"grant_type": "refresh_token", "client_id": client_id,
                                                  "refresh_token": first["refresh_token"]})
            self.assertEqual(refreshed.status_code, 200, refreshed.text)
            self.assertEqual(self.mcp(http, "tools/list", token=first["access_token"]).status_code, 401)
            self.assertEqual(self.mcp(http, "tools/list",
                                      token=refreshed.json()["access_token"]).status_code, 200)
            reuse = http.post("/token", data={"grant_type": "refresh_token", "client_id": client_id,
                                              "refresh_token": first["refresh_token"]})
            self.assertEqual(reuse.status_code, 400)

    def test_allowlisted_individual_email(self):
        with self.client(email="Guest@Gmail.com") as http:
            _, _, q = self.sign_in(http)
            self.assertIn("code", q)

    def test_rejects_users_outside_allowlist(self):
        for email, verified in [("someone@gmail.com", True), ("admissions@example.edu", False),
                                ("x@notexample.edu", True)]:
            with self.subTest(email=email), self.client(email=email, verified=verified) as http:
                _, _, q = self.sign_in(http)
                self.assertEqual(q["error"], "access_denied")
                self.assertNotIn("code", q)

    def test_rejects_unknown_redirect_hosts(self):
        with self.client() as http:
            reg = http.post("/register", json={"redirect_uris": ["https://evil.example/cb"],
                                                "token_endpoint_auth_method": "none"})
            self.assertEqual(reg.status_code, 400)
            self.assertEqual(reg.json()["error"], "invalid_redirect_uri")

    def test_stale_callback_state(self):
        with self.client() as http:
            self.assertEqual(http.get("/oauth/callback", params={"code": "x", "state": "nope"}).status_code, 400)

    def test_settings_require_allowlist_and_https(self):
        base = dict(oidc_issuer="https://idp", client_id="c", client_secret="s")
        with self.assertRaises(ValueError):
            OAuthSettings(public_url=PUBLIC, **base)
        with self.assertRaises(ValueError):
            OAuthSettings(public_url="http://commonapp.example.edu", allowed_domains={"x.edu"}, **base)
        self.assertIsNone(OAuthSettings.from_env({}))
        with self.assertRaises(ValueError):
            OAuthSettings.from_env({"COMMONAPP_OIDC_ISSUER": "https://idp"})


if __name__ == "__main__":
    unittest.main()
