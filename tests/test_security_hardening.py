"""Regression tests for the security-hardening patch."""

import httpx
import pytest

from .polestar_api.auth import (
    _authorization_values,
    _get_oidc_login_page,
    _should_follow_callback,
    _validate_oidc_config,
)
from .polestar_api.discovery import _validate_c3_endpoint
from .polestar_api.exceptions import ApiError, AuthError


def _oidc_config(**overrides):
    config = {
        "issuer": "https://polestarid.eu.polestar.com",
        "authorization_endpoint": "https://polestarid.eu.polestar.com/as/authorization.oauth2",
        "token_endpoint": "https://polestarid.eu.polestar.com/as/token.oauth2",
    }
    config.update(overrides)
    return config


def test_oidc_discovery_accepts_expected_polestar_host():
    auth, token = _validate_oidc_config(_oidc_config())
    assert auth.startswith("https://polestarid.eu.polestar.com/")
    assert token.startswith("https://polestarid.eu.polestar.com/")


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://polestarid.eu.polestar.com/as/token.oauth2",
        "https://polestarid.eu.polestar.com.evil.example/as/token.oauth2",
        "https://evil.example/as/token.oauth2",
        "https://user@polestarid.eu.polestar.com/as/token.oauth2",
        "https://polestarid.eu.polestar.com:8443/as/token.oauth2",
    ],
)
def test_oidc_discovery_rejects_untrusted_token_endpoint(endpoint):
    with pytest.raises(AuthError):
        _validate_oidc_config(_oidc_config(token_endpoint=endpoint))


def test_oidc_discovery_rejects_wrong_issuer():
    with pytest.raises(AuthError):
        _validate_oidc_config(_oidc_config(issuer="https://evil.example"))


def test_oauth_state_must_match():
    code, uid = _authorization_values(
        "polestar-explore://explore.polestar.com?code=abc&state=expected",
        expected_state="expected",
    )
    assert code == "abc"
    assert uid is None

    with pytest.raises(AuthError):
        _authorization_values(
            "polestar-explore://explore.polestar.com?code=abc&state=wrong",
            expected_state="expected",
        )

    with pytest.raises(AuthError):
        _authorization_values(
            "polestar-explore://explore.polestar.com?code=abc",
            expected_state="expected",
        )


def test_callback_fetch_is_restricted_to_polestar_https():
    assert _should_follow_callback("https://www.polestar.com/sign-in-callback")
    assert not _should_follow_callback("https://polestar.com.evil.example/callback")
    assert not _should_follow_callback("https://evil.example/callback")
    assert not _should_follow_callback("http://www.polestar.com/callback")
    assert not _should_follow_callback("https://user@www.polestar.com/callback")


@pytest.mark.asyncio
async def test_oidc_authorization_redirect_cannot_escape_polestar_host():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://evil.example/login"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(AuthError):
            await _get_oidc_login_page(
                client,
                "https://polestarid.eu.polestar.com/as/authorization.oauth2",
                params={"state": "abc"},
            )


@pytest.mark.parametrize(
    "host",
    [
        "cepmobtoken.eu.prod.c3.volvocars.com",
        "cepmobtoken.prod.c3.volvocars.com.cn",
    ],
)
def test_c3_discovery_accepts_known_volvo_namespaces(host):
    assert _validate_c3_endpoint(host, 443) == (host, 443)


@pytest.mark.parametrize(
    "host,port",
    [
        ("evil.example", 443),
        ("volvocars.com.evil.example", 443),
        ("127.0.0.1", 443),
        ("::1", 443),
        ("cepmobtoken.eu.prod.c3.volvocars.com", 8443),
    ],
)
def test_c3_discovery_rejects_untrusted_destinations(host, port):
    with pytest.raises(ApiError):
        _validate_c3_endpoint(host, port)
