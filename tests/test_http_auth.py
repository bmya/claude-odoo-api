"""
Tests for the HTTP transport's authentication layer (BmyaAuthMiddleware).

These are the first tests that exercise the real ASGI stack: they drive
build_http_app() through a Starlette TestClient, so the 401/503 behaviour is
verified at the transport level rather than inferred from unit tests.
"""

import pytest
from starlette.testclient import TestClient

import bmya_auth
import odoo_mcp_server
from tests.conftest import KEY_EXPIRED, KEY_REVOKED, KEY_RO, KEY_UNKNOWN

MCP_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}

INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "1"},
    },
}


@pytest.fixture
def no_gateway_token(monkeypatch):
    monkeypatch.setattr(odoo_mcp_server, "MCP_GATEWAY_TOKEN", None)


@pytest.fixture
def client(auth_env, no_gateway_token):
    """A TestClient over the real app. The context manager runs the lifespan,
    which the streamable-HTTP session manager needs."""
    with TestClient(odoo_mcp_server.build_http_app()) as test_client:
        yield test_client


def post_mcp(client, **headers):
    return client.post("/mcp", json=INITIALIZE, headers={**MCP_HEADERS, **headers})


class TestHealthEndpoints:
    def test_health_needs_no_headers(self, client):
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}

    def test_health_exempt_from_gateway_token(self, client, monkeypatch):
        monkeypatch.setattr(odoo_mcp_server, "MCP_GATEWAY_TOKEN", "s3cret")
        assert client.get("/health").status_code == 200

    def test_readyz_ready_with_a_good_registry(self, client):
        response = client.get("/readyz")
        assert response.status_code == 200
        assert response.json()["status"] == "ready"

    def test_readyz_unready_without_a_registry(self, client, tmp_path, monkeypatch):
        monkeypatch.setattr(bmya_auth, "BMYA_API_KEYS_FILE", str(tmp_path / "gone.json"))
        bmya_auth.invalidate_registry_cache()
        response = client.get("/readyz")
        assert response.status_code == 503
        assert response.json()["status"] == "unready"

    def test_readyz_ready_when_auth_disabled(self, client, monkeypatch):
        monkeypatch.setattr(bmya_auth, "BMYA_AUTH_ENABLED", False)
        assert client.get("/readyz").status_code == 200


class TestBmyaKeyAuthentication:
    def test_missing_key_is_rejected(self, client):
        response = post_mcp(client)
        assert response.status_code == 401
        assert response.json() == {"error": "unauthorized"}

    def test_valid_key_is_not_rejected(self, client):
        response = post_mcp(client, **{"X-Bmya-Api-Key": KEY_RO})
        assert response.status_code != 401
        assert response.status_code < 500

    @pytest.mark.parametrize("key", ["garbage", KEY_UNKNOWN, KEY_REVOKED, KEY_EXPIRED])
    def test_every_bad_key_is_rejected(self, client, key):
        response = post_mcp(client, **{"X-Bmya-Api-Key": key})
        assert response.status_code == 401

    def test_rejection_bodies_are_byte_identical(self, client):
        """A caller must not be able to tell unknown from revoked from expired."""
        bodies = {
            post_mcp(client, **{"X-Bmya-Api-Key": key}).content
            for key in ["garbage", KEY_UNKNOWN, KEY_REVOKED, KEY_EXPIRED]
        }
        bodies.add(post_mcp(client).content)
        assert len(bodies) == 1

    def test_auth_failure_is_a_transport_error_not_a_tool_result(self, client):
        """A 401 must never arrive as a 200 carrying an error in the body."""
        response = post_mcp(client)
        assert response.status_code == 401
        assert "jsonrpc" not in response.text

    def test_registry_unavailable_returns_503(self, client, tmp_path, monkeypatch):
        monkeypatch.setattr(bmya_auth, "BMYA_API_KEYS_FILE", str(tmp_path / "gone.json"))
        bmya_auth.invalidate_registry_cache()
        response = post_mcp(client, **{"X-Bmya-Api-Key": KEY_RO})
        assert response.status_code == 503
        assert response.json() == {"error": "service_unavailable"}

    def test_revocation_takes_effect_without_a_restart(self, client, auth_env):
        """The whole point of the file registry: edit, no redeploy."""
        import json

        from tests.conftest import build_registry_data

        assert post_mcp(client, **{"X-Bmya-Api-Key": KEY_RO}).status_code != 401

        data = build_registry_data()
        data["grants"][0]["revoked"] = True
        auth_env.write_text(json.dumps(data), encoding="utf-8")

        assert post_mcp(client, **{"X-Bmya-Api-Key": KEY_RO}).status_code == 401


class TestGatewayToken:
    def test_wrong_token_rejected(self, client, monkeypatch):
        monkeypatch.setattr(odoo_mcp_server, "MCP_GATEWAY_TOKEN", "s3cret")
        response = post_mcp(client, **{"X-Gateway-Token": "nope", "X-Bmya-Api-Key": KEY_RO})
        assert response.status_code == 401

    def test_right_token_without_bmya_key_rejected(self, client, monkeypatch):
        monkeypatch.setattr(odoo_mcp_server, "MCP_GATEWAY_TOKEN", "s3cret")
        response = post_mcp(client, **{"X-Gateway-Token": "s3cret"})
        assert response.status_code == 401

    def test_both_credentials_accepted(self, client, monkeypatch):
        monkeypatch.setattr(odoo_mcp_server, "MCP_GATEWAY_TOKEN", "s3cret")
        response = post_mcp(client, **{"X-Gateway-Token": "s3cret", "X-Bmya-Api-Key": KEY_RO})
        assert response.status_code != 401

    def test_missing_token_rejected_when_configured(self, client, monkeypatch):
        monkeypatch.setattr(odoo_mcp_server, "MCP_GATEWAY_TOKEN", "s3cret")
        assert post_mcp(client, **{"X-Bmya-Api-Key": KEY_RO}).status_code == 401


class TestRollbackSwitch:
    def test_auth_disabled_restores_legacy_behaviour(self, client, monkeypatch):
        """BMYA_AUTH_ENABLED=0 is the documented rollback path, so it is tested."""
        monkeypatch.setattr(bmya_auth, "BMYA_AUTH_ENABLED", False)
        response = post_mcp(client)
        assert response.status_code != 401

    def test_gateway_token_still_applies_when_auth_disabled(self, client, monkeypatch):
        monkeypatch.setattr(bmya_auth, "BMYA_AUTH_ENABLED", False)
        monkeypatch.setattr(odoo_mcp_server, "MCP_GATEWAY_TOKEN", "s3cret")
        assert post_mcp(client).status_code == 401
        assert post_mcp(client, **{"X-Gateway-Token": "s3cret"}).status_code != 401


class TestScopeHeaders:
    def test_lowercases_and_decodes(self):
        scope = {"headers": [(b"X-Bmya-Api-Key", b"abc"), (b"accept", b"*/*")]}
        assert odoo_mcp_server._scope_headers(scope) == {
            "x-bmya-api-key": "abc",
            "accept": "*/*",
        }

    def test_missing_headers_key(self):
        assert odoo_mcp_server._scope_headers({}) == {}


class TestNonHttpScopes:
    @pytest.mark.asyncio
    async def test_lifespan_scope_passes_through_untouched(self):
        """A non-http scope must not be authenticated (or the app never starts)."""
        seen = []

        async def inner(scope, _receive, _send):
            seen.append(scope["type"])

        middleware = odoo_mcp_server.BmyaAuthMiddleware(inner)
        await middleware({"type": "lifespan"}, None, None)
        assert seen == ["lifespan"]
