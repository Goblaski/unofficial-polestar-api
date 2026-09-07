"""Async OIDC/PKCE authentication for the Polestar API."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import ssl
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol
from urllib.parse import parse_qs, urljoin, urlparse

import httpx

from .exceptions import AuthError, TokenExpiredError

# Pre-build SSL context at import time so httpx never triggers
# blocking load_verify_locations inside the HA event loop (Python 3.14).
_HTTPX_SSL_CONTEXT = ssl.create_default_context()

OIDC_PROVIDER = "https://polestarid.eu.polestar.com"
OIDC_DISCOVERY = f"{OIDC_PROVIDER}/.well-known/openid-configuration"
CLIENT_ID = "lp8dyrd_10"
REDIRECT_URI = "polestar-explore://explore.polestar.com"
SCOPES = "openid profile email customer:attributes customer:attributes:write"

_OIDC_HOST = "polestarid.eu.polestar.com"
_ALLOWED_CALLBACK_HOSTS = {"www.polestar.com"}
_ALLOWED_HTTPS_PORTS = {443}


@dataclass
class TokenData:
    access_token: str
    refresh_token: str | None = None
    token_type: str = "Bearer"
    expires_in: int = 0
    obtained_at: float = field(default_factory=time.time)

    @property
    def is_expired(self) -> bool:
        if not self.expires_in:
            return False
        return time.time() > self.obtained_at + self.expires_in - 60  # 60s buffer

    def to_dict(self) -> dict:
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "token_type": self.token_type,
            "expires_in": self.expires_in,
            "obtained_at": self.obtained_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> TokenData:
        return cls(
            access_token=data["access_token"],
            refresh_token=data.get("refresh_token"),
            token_type=data.get("token_type", "Bearer"),
            expires_in=data.get("expires_in", 0),
            obtained_at=data.get("obtained_at", 0),
        )


class TokenStore(Protocol):
    """Protocol for pluggable token persistence."""

    async def load(self) -> TokenData | None: ...
    async def save(self, tokens: TokenData) -> None: ...


class FileTokenStore:
    """Stores tokens in a JSON file."""

    def __init__(self, path: str | Path):
        self._path = Path(path).expanduser()

    async def load(self) -> TokenData | None:
        if not self._path.exists():
            return None
        data = json.loads(self._path.read_text())
        return TokenData.from_dict(data)

    async def save(self, tokens: TokenData) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(tokens.to_dict(), indent=2))
        self._path.chmod(0o600)


class MemoryTokenStore:
    """In-memory token store (no persistence)."""

    def __init__(self) -> None:
        self._tokens: TokenData | None = None

    async def load(self) -> TokenData | None:
        return self._tokens

    async def save(self, tokens: TokenData) -> None:
        self._tokens = tokens


def _b64urlencode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _generate_pkce() -> tuple[str, str]:
    verifier = _b64urlencode(os.urandom(32))
    challenge = _b64urlencode(hashlib.sha256(verifier.encode()).digest())
    return verifier, challenge


def _host_matches_suffix(host: str, suffix: str) -> bool:
    host = host.rstrip(".").lower()
    suffix = suffix.rstrip(".").lower()
    return host == suffix or host.endswith(f".{suffix}")


def _validate_https_url(
    url: str,
    *,
    label: str,
    exact_hosts: set[str] | None = None,
    allowed_suffixes: tuple[str, ...] = (),
) -> str:
    """Validate a security-sensitive HTTPS endpoint before sending secrets."""
    if not isinstance(url, str) or not url:
        raise AuthError(f"Invalid {label}: missing URL")

    try:
        parsed = urlparse(url)
        port = parsed.port
    except ValueError as err:
        raise AuthError(f"Invalid {label}: malformed URL") from err

    host = (parsed.hostname or "").rstrip(".").lower()
    if parsed.scheme.lower() != "https" or not host:
        raise AuthError(f"Invalid {label}: HTTPS is required")
    if parsed.username is not None or parsed.password is not None:
        raise AuthError(f"Invalid {label}: userinfo is not allowed")
    if port is not None and port not in _ALLOWED_HTTPS_PORTS:
        raise AuthError(f"Invalid {label}: unexpected port")

    allowed = False
    if exact_hosts and host in {item.lower() for item in exact_hosts}:
        allowed = True
    if allowed_suffixes and any(_host_matches_suffix(host, suffix) for suffix in allowed_suffixes):
        allowed = True
    if not allowed:
        raise AuthError(f"Invalid {label}: untrusted hostname")

    return url


def _validate_oidc_config(config: object) -> tuple[str, str]:
    """Validate OIDC discovery output against the fixed Polestar issuer."""
    if not isinstance(config, dict):
        raise AuthError("Invalid OIDC discovery response")

    issuer = config.get("issuer")
    if not isinstance(issuer, str) or issuer.rstrip("/") != OIDC_PROVIDER:
        raise AuthError("Invalid OIDC discovery response: unexpected issuer")

    auth_endpoint = config.get("authorization_endpoint")
    token_endpoint = config.get("token_endpoint")
    if not isinstance(auth_endpoint, str) or not isinstance(token_endpoint, str):
        raise AuthError("Invalid OIDC discovery response: missing endpoints")

    _validate_https_url(
        auth_endpoint,
        label="OIDC authorization endpoint",
        exact_hosts={_OIDC_HOST},
    )
    _validate_https_url(
        token_endpoint,
        label="OIDC token endpoint",
        exact_hosts={_OIDC_HOST},
    )
    return auth_endpoint, token_endpoint


def _validate_resume_url(location: str) -> str:
    """Resolve and validate the PingFederate resume URL."""
    resume_url = urljoin(f"{OIDC_PROVIDER}/", location)
    return _validate_https_url(
        resume_url,
        label="OIDC resume endpoint",
        exact_hosts={_OIDC_HOST},
    )


async def _get_oidc_login_page(
    client: httpx.AsyncClient,
    url: str,
    *,
    params: dict[str, str],
    max_redirects: int = 5,
) -> httpx.Response:
    """GET the OIDC login page while keeping every redirect on Polestar ID."""
    current_url = _validate_https_url(
        url,
        label="OIDC authorization endpoint",
        exact_hosts={_OIDC_HOST},
    )
    current_params: dict[str, str] | None = params

    for _ in range(max_redirects + 1):
        response = await client.get(
            current_url,
            params=current_params,
            follow_redirects=False,
        )
        if response.status_code not in {301, 302, 303, 307, 308}:
            return response

        location = response.headers.get("location")
        if not location:
            raise AuthError("OIDC redirect missing Location header")
        current_url = urljoin(str(response.url), location)
        current_url = _validate_https_url(
            current_url,
            label="OIDC authorization redirect",
            exact_hosts={_OIDC_HOST},
        )
        # The redirect target already contains whatever query state the provider
        # needs. Re-appending the initial OAuth parameters could duplicate them.
        current_params = None

    raise AuthError("Too many OIDC authorization redirects")


def _authorization_values(
    location: str,
    *,
    expected_state: str,
) -> tuple[str | None, str | None]:
    """Extract code/uid and strictly validate OAuth state when a code is returned."""
    parsed = urlparse(location)
    qs = parse_qs(parsed.query)
    code = qs.get("code", [None])[0]
    uid = qs.get("uid", [None])[0]

    if code is not None:
        returned_state = qs.get("state", [None])[0]
        if not isinstance(returned_state, str) or not hmac.compare_digest(
            returned_state, expected_state
        ):
            raise AuthError("OIDC state mismatch")

    return code, uid


def _should_follow_callback(location: str) -> bool:
    """Return whether an old HTTPS callback is safe to fetch.

    The current mobile-app flow uses a custom scheme and therefore returns False.
    Older web flows are followed only when they stay within Polestar's HTTPS DNS
    namespace. This prevents an upstream redirect from becoming an arbitrary GET.
    """
    try:
        parsed = urlparse(location)
        port = parsed.port
    except ValueError:
        return False

    host = (parsed.hostname or "").rstrip(".").lower()
    if parsed.scheme.lower() != "https" or not host:
        return False
    if parsed.username is not None or parsed.password is not None:
        return False
    if port is not None and port not in _ALLOWED_HTTPS_PORTS:
        return False
    return host in _ALLOWED_CALLBACK_HOSTS


class AuthManager:
    """Manages OIDC authentication and token lifecycle."""

    def __init__(self, token_store: TokenStore | None = None) -> None:
        self._token_store = token_store or MemoryTokenStore()
        self._tokens: TokenData | None = None
        self._auth_endpoint: str | None = None
        self._token_endpoint: str | None = None

    @property
    def access_token(self) -> str | None:
        return self._tokens.access_token if self._tokens else None

    async def authenticate(self, email: str, password: str | None = None) -> None:
        """Authenticate using a refresh token first, then credentials if supplied.

        Passwords are intentionally not retained by AuthManager. Once the initial
        OIDC exchange succeeds, token refresh is the only unattended credential
        path. If refresh later fails, callers must trigger an explicit reauth flow.
        """
        self._tokens = await self._token_store.load()
        await self._discover_endpoints()

        if self._tokens and self._tokens.refresh_token:
            try:
                await self._refresh()
                return
            except (AuthError, httpx.HTTPStatusError):
                # A transient refresh failure should not discard an access token
                # that is still usable. Reauth is required only once it expires.
                if not self._tokens.is_expired:
                    return

        if self._tokens and not self._tokens.is_expired:
            return

        if password is None:
            raise TokenExpiredError("No valid refresh token; Polestar re-authentication required")

        await self._full_auth(email, password)

    async def ensure_valid_token(self) -> str:
        """Return a valid access token, refreshing when needed."""
        if not self._tokens:
            raise AuthError("Not authenticated")

        if self._tokens.is_expired:
            if self._tokens.refresh_token:
                try:
                    await self._refresh()
                    return self._tokens.access_token
                except (AuthError, httpx.HTTPStatusError):
                    pass

            raise TokenExpiredError("Token expired; Polestar re-authentication required")

        return self._tokens.access_token

    async def _discover_endpoints(self) -> None:
        async with httpx.AsyncClient(verify=_HTTPX_SSL_CONTEXT, timeout=30) as client:
            r = await client.get(OIDC_DISCOVERY)
            r.raise_for_status()
            self._auth_endpoint, self._token_endpoint = _validate_oidc_config(r.json())

    async def _full_auth(self, email: str, password: str) -> None:
        if not self._auth_endpoint or not self._token_endpoint:
            await self._discover_endpoints()

        code_verifier, code_challenge = _generate_pkce()
        code = await self._authorize(code_challenge, email, password)
        await self._exchange_token(code, code_verifier)

    async def _authorize(self, code_challenge: str, email: str, password: str) -> str:
        state = _b64urlencode(os.urandom(32))
        params = {
            "response_type": "code",
            "client_id": CLIENT_ID,
            "redirect_uri": REDIRECT_URI,
            "scope": SCOPES,
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "response_mode": "query",
        }

        async with httpx.AsyncClient(verify=_HTTPX_SSL_CONTEXT, timeout=30) as client:
            # Step 1: GET auth endpoint — lands on login page. Redirects are
            # followed manually so they cannot escape the Polestar ID hostname.
            r = await _get_oidc_login_page(client, self._auth_endpoint, params=params)

            # Extract resume path from PingFederate HTML/JS.
            resume_match = re.search(r'(?:url|action):\s*"(.+)"', r.text)
            if not resume_match:
                raise AuthError(
                    f"Could not find resume path in auth response (status {r.status_code})"
                )
            resume_url = _validate_resume_url(resume_match.group(1))

            # Step 2: POST credentials. The validated resume URL is pinned to the
            # Polestar ID host, so username/password cannot be redirected elsewhere.
            r = await client.post(
                resume_url,
                params=params,
                data={"pf.username": email, "pf.pass": password},
                follow_redirects=False,
            )

            if r.status_code not in (302, 303):
                if "ERR001" in r.text:
                    raise AuthError("Invalid username or password")
                raise AuthError(f"Auth failed with status {r.status_code}")

            # Step 3: Extract code from redirect and validate OAuth state.
            location = r.headers.get("location", "")
            code, uid = _authorization_values(location, expected_state=state)

            # Handle Terms & Conditions acceptance.
            if code is None and uid is not None:
                r = await client.post(
                    resume_url,
                    params=params,
                    data={"pf.submit": "true", "subject": uid},
                    follow_redirects=False,
                )
                if r.status_code in (302, 303):
                    location = r.headers.get("location", "")
                    code, _ = _authorization_values(location, expected_state=state)

            if code is None:
                raise AuthError("No auth code in redirect")

            # Old web flows used an HTTPS callback we could fetch; the current
            # mobile-app flow redirects to a custom scheme that is not fetchable.
            if _should_follow_callback(location):
                await client.get(location)

        return code

    async def _exchange_token(self, code: str, code_verifier: str) -> None:
        if not self._token_endpoint:
            raise AuthError("OIDC token endpoint is not initialized")

        async with httpx.AsyncClient(verify=_HTTPX_SSL_CONTEXT, timeout=30) as client:
            r = await client.post(
                self._token_endpoint,
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": REDIRECT_URI,
                    "client_id": CLIENT_ID,
                    "code_verifier": code_verifier,
                },
            )
            r.raise_for_status()
            data = r.json()

        self._tokens = TokenData(
            access_token=data["access_token"],
            refresh_token=data.get("refresh_token"),
            token_type=data.get("token_type", "Bearer"),
            expires_in=data.get("expires_in", 0),
        )
        await self._token_store.save(self._tokens)

    async def _refresh(self) -> None:
        if not self._token_endpoint:
            await self._discover_endpoints()

        if not self._tokens or not self._tokens.refresh_token:
            raise AuthError("No refresh token available")
        if not self._token_endpoint:
            raise AuthError("OIDC token endpoint is not initialized")

        async with httpx.AsyncClient(verify=_HTTPX_SSL_CONTEXT, timeout=30) as client:
            r = await client.post(
                self._token_endpoint,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": self._tokens.refresh_token,
                    "client_id": CLIENT_ID,
                },
            )
            if r.status_code >= 400:
                raise AuthError(f"Token refresh failed: {r.status_code}")
            data = r.json()

        self._tokens = TokenData(
            access_token=data["access_token"],
            refresh_token=data.get("refresh_token", self._tokens.refresh_token),
            token_type=data.get("token_type", "Bearer"),
            expires_in=data.get("expires_in", 0),
        )
        await self._token_store.save(self._tokens)
