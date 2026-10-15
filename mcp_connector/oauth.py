"""
mcp_connector/oauth.py

Who may use the connector: anyone signed in to Google with a company email.

HOW THE FLOW RUNS
-----------------
claude.ai speaks OAuth 2.1 to this server, and this server hands the actual
sign-in to Google. Two OAuth exchanges, chained:

    claude.ai -> /authorize (here) -> Google sign-in
              <- /oauth/google/callback (here) <- Google
    claude.ai <- redirect with OUR code
    claude.ai -> /token (here) -> our access + refresh tokens

Google proves who the person is. This server decides whether they get in
(`config.email_allowed`) and issues its own tokens, so access can be cut off
here, per person, without touching anyone's Google account.

THE CHECKS THAT MATTER
----------------------
  * The email must be verified by Google and in an allowed domain. Checked
    at sign-in AND on every request, so removing someone from
    MCP_ALLOWED_EMAILS or a domain from MCP_ALLOWED_DOMAINS takes effect on
    their next tool call rather than when their token expires.
  * Only known redirect hosts may register (claude.ai, claude.com, and
    localhost for Claude Code and Desktop). Without that, anyone could
    register a client pointing at their own server and send a colleague a
    sign-in link that hands them a token.
  * PKCE is enforced by the SDK's token handler. Codes are single use and
    five minutes long. Refresh tokens rotate on every use.

WHY THE ID TOKEN IS NOT SIGNATURE-CHECKED
-----------------------------------------
It arrives in the response to our own server-to-server POST to Google's
token endpoint over TLS, authenticated with our client secret. OpenID Connect
Core 3.1.3.7 allows TLS server validation in place of the signature in
exactly that case. We still check issuer, audience, expiry and
email_verified, and adding a JWT library to verify a signature on a token we
fetched ourselves would buy nothing.
"""

from __future__ import annotations

import base64
import html
import json
import os
import secrets
import time
from urllib.parse import urlencode, urlparse

import anyio
import httpx
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    RefreshToken,
    RegistrationError,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from mcp_connector import config, store

GOOGLE_AUTHORIZE = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN = "https://oauth2.googleapis.com/token"
GOOGLE_ISSUERS = ("https://accounts.google.com", "accounts.google.com")

DEFAULT_REDIRECT_HOSTS = "claude.ai,claude.com,localhost,127.0.0.1"


def allowed_redirect_hosts() -> tuple[str, ...]:
    raw = os.environ.get("MCP_ALLOWED_REDIRECT_HOSTS", DEFAULT_REDIRECT_HOSTS)
    return tuple(h.strip().lower() for h in raw.split(",") if h.strip())


def redirect_permitted(uri: str) -> bool:
    try:
        parsed = urlparse(str(uri))
    except ValueError:
        return False
    host = (parsed.hostname or "").lower()
    if host in ("localhost", "127.0.0.1"):
        return host in allowed_redirect_hosts() and parsed.scheme in (
            "http", "https",
        )
    if parsed.scheme != "https":
        return False
    return any(
        host == allowed or host.endswith(f".{allowed}")
        for allowed in allowed_redirect_hosts()
        if allowed not in ("localhost", "127.0.0.1")
    )


def decode_id_token(id_token: str) -> dict:
    """The claims of a JWT, without verifying it. See the module docstring."""
    try:
        payload = id_token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except (IndexError, ValueError):
        return {}


def check_google_claims(claims: dict, now: float | None = None) -> str:
    """The signed-in email if the claims are acceptable, else ''."""
    now = now if now is not None else time.time()
    if claims.get("iss") not in GOOGLE_ISSUERS:
        return ""
    if claims.get("aud") != config.google_client_id():
        return ""
    if float(claims.get("exp") or 0) < now:
        return ""
    if claims.get("email_verified") not in (True, "true"):
        return ""
    return str(claims.get("email") or "").strip().lower()


def _page(title: str, message: str, status: int) -> HTMLResponse:
    body = (
        "<!doctype html><meta charset=utf-8>"
        "<meta name=viewport content='width=device-width,initial-scale=1'>"
        f"<title>{html.escape(title)}</title>"
        "<body style='font-family:system-ui;max-width:32rem;margin:4rem auto;"
        "padding:0 1rem;line-height:1.5'>"
        f"<h1 style='font-size:1.25rem'>{html.escape(title)}</h1>"
        f"<p>{html.escape(message)}</p></body>"
    )
    return HTMLResponse(body, status_code=status)


class GoogleOAuthProvider:
    """The OAuth authorization server, delegating identity to Google."""

    # -- clients ---------------------------------------------------------

    async def get_client(
        self, client_id: str
    ) -> OAuthClientInformationFull | None:
        info = await anyio.to_thread.run_sync(store.load_client, client_id)
        if not info:
            return None
        return OAuthClientInformationFull.model_validate(info)

    async def register_client(
        self, client_info: OAuthClientInformationFull
    ) -> None:
        uris = [str(u) for u in (client_info.redirect_uris or [])]
        refused = [u for u in uris if not redirect_permitted(u)]
        if not uris or refused:
            raise RegistrationError(
                error="invalid_redirect_uri",
                error_description=(
                    "redirect URI not permitted for this server: "
                    + ", ".join(refused or ["(none given)"])
                ),
            )
        await anyio.to_thread.run_sync(
            store.save_client,
            client_info.client_id,
            client_info.model_dump(mode="json", exclude_none=True),
        )

    # -- the hop to Google ------------------------------------------------

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        txn = secrets.token_urlsafe(32)
        await anyio.to_thread.run_sync(store.put_pending, txn, {
            "client_id": client.client_id,
            "redirect_uri": str(params.redirect_uri),
            "redirect_uri_provided_explicitly": (
                params.redirect_uri_provided_explicitly
            ),
            "code_challenge": params.code_challenge,
            "state": params.state,
            "scopes": params.scopes or [config.SCOPE],
        })
        query = {
            "client_id": config.google_client_id(),
            "redirect_uri": config.google_redirect_uri(),
            "response_type": "code",
            "scope": "openid email",
            "state": txn,
            "prompt": "select_account",
        }
        domains = config.allowed_domains()
        if len(domains) == 1 and not config.allowed_emails():
            # Narrows Google's account chooser to the company account.
            query["hd"] = domains[0]
        return f"{GOOGLE_AUTHORIZE}?{urlencode(query)}"

    async def google_callback(self, request: Request) -> Response:
        """Google sends the person back here. Finish, then return to Claude."""
        txn = request.query_params.get("state", "")
        flow = await anyio.to_thread.run_sync(store.take_pending, txn)
        if not flow:
            return _page(
                "Sign-in expired",
                "This sign-in link has expired or was already used. Go back "
                "to Claude and connect Vome OS again.",
                400,
            )

        def back_to_client(**params: str | None) -> RedirectResponse:
            return RedirectResponse(
                construct_redirect_uri(
                    flow["redirect_uri"], state=flow.get("state"), **params
                ),
                status_code=302,
            )

        if request.query_params.get("error"):
            return back_to_client(
                error="access_denied",
                error_description="Google sign-in was cancelled",
            )

        email = await self._google_email(request.query_params.get("code", ""))
        if not email:
            return back_to_client(
                error="access_denied",
                error_description="Could not verify the Google account",
            )
        if not config.email_allowed(email):
            print(f"[MCP AUTH] refused sign-in for {email}")
            return back_to_client(
                error="access_denied",
                error_description=(
                    f"{email} is not allowed to use Vome OS. Sign in with "
                    "your Vome work account."
                ),
            )

        code = secrets.token_urlsafe(32)
        await anyio.to_thread.run_sync(store.put_code, code, {
            "client_id": flow["client_id"],
            "redirect_uri": flow["redirect_uri"],
            "redirect_uri_provided_explicitly": (
                flow["redirect_uri_provided_explicitly"]
            ),
            "code_challenge": flow["code_challenge"],
            "scopes": flow["scopes"],
            "subject": email,
        })
        print(f"[MCP AUTH] signed in {email}")
        return back_to_client(code=code)

    async def _google_email(self, google_code: str) -> str:
        if not google_code:
            return ""
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                response = await client.post(GOOGLE_TOKEN, data={
                    "code": google_code,
                    "client_id": config.google_client_id(),
                    "client_secret": config.google_client_secret(),
                    "redirect_uri": config.google_redirect_uri(),
                    "grant_type": "authorization_code",
                })
            response.raise_for_status()
            id_token = response.json().get("id_token", "")
        except Exception as exc:
            print(f"[MCP AUTH] Google token exchange failed: {exc}")
            return ""
        return check_google_claims(decode_id_token(id_token))

    # -- our codes and tokens --------------------------------------------

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        data = await anyio.to_thread.run_sync(
            store.peek_code, authorization_code
        )
        if not data or data["client_id"] != client.client_id:
            return None
        return AuthorizationCode(
            code=authorization_code,
            scopes=data["scopes"],
            expires_at=time.time() + 60,
            client_id=data["client_id"],
            code_challenge=data["code_challenge"],
            redirect_uri=data["redirect_uri"],
            redirect_uri_provided_explicitly=(
                data["redirect_uri_provided_explicitly"]
            ),
            resource=config.resource_url(),
            subject=data["subject"],
        )

    async def exchange_authorization_code(
        self,
        client: OAuthClientInformationFull,
        authorization_code: AuthorizationCode,
    ) -> OAuthToken:
        data = await anyio.to_thread.run_sync(
            store.take_code, authorization_code.code
        )
        if not data or data["client_id"] != client.client_id:
            raise TokenError(
                error="invalid_grant",
                error_description="authorization code already used or expired",
            )
        return await self._issue(
            client.client_id, data["subject"], data["scopes"],
            family=secrets.token_hex(16),
        )

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        data = await anyio.to_thread.run_sync(
            store.load_token, refresh_token, "refresh"
        )
        if not data or data["client_id"] != client.client_id:
            return None
        if not config.email_allowed(data["subject"]):
            return None
        return RefreshToken(
            token=refresh_token,
            client_id=data["client_id"],
            scopes=data["scopes"],
            expires_at=int(data["expires_at"]) if data["expires_at"] else None,
            resource=data["resource"],
            subject=data["subject"],
        )

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        data = await anyio.to_thread.run_sync(
            store.load_token, refresh_token.token, "refresh"
        )
        if not data:
            raise TokenError(error="invalid_grant")
        # Rotation: the presented refresh token is spent.
        await anyio.to_thread.run_sync(
            store.revoke_single, refresh_token.token
        )
        held = data["scopes"]
        granted = [s for s in (scopes or held) if s in held]
        return await self._issue(
            client.client_id, data["subject"], granted or data["scopes"],
            family=data["family"],
        )

    async def load_access_token(self, token: str) -> AccessToken | None:
        data = await anyio.to_thread.run_sync(
            store.load_token, token, "access"
        )
        if not data or not config.email_allowed(data["subject"]):
            return None
        return AccessToken(
            token=token,
            client_id=data["client_id"],
            scopes=data["scopes"],
            expires_at=int(data["expires_at"]) if data["expires_at"] else None,
            resource=data["resource"],
            subject=data["subject"],
        )

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        await anyio.to_thread.run_sync(store.revoke_token, token.token)

    async def _issue(
        self, client_id: str, subject: str, scopes: list[str], family: str
    ) -> OAuthToken:
        if not config.email_allowed(subject):
            raise TokenError(
                error="invalid_grant",
                error_description="this account is no longer allowed",
            )
        access = secrets.token_urlsafe(40)
        refresh = secrets.token_urlsafe(40)
        now = time.time()
        common = {
            "client_id": client_id,
            "subject": subject,
            "scopes": scopes,
            "resource": config.resource_url(),
            "family": family,
        }

        def save() -> None:
            store.save_token(
                access, kind="access",
                expires_at=now + config.access_token_ttl(), **common,
            )
            store.save_token(
                refresh, kind="refresh",
                expires_at=now + config.refresh_token_ttl(), **common,
            )

        await anyio.to_thread.run_sync(save)
        return OAuthToken(
            access_token=access,
            expires_in=config.access_token_ttl(),
            scope=" ".join(scopes),
            refresh_token=refresh,
        )
