"""
Shared pytest fixtures for the Odoo MCP server test suite.
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

# Add src to path so both odoo_mcp_server and bmya_auth are importable.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import bmya_auth  # noqa: E402

# Keys used across the auth tests. Real keys are minted by tools/bmya-keys.py;
# these are fixed so the expected hashes are stable.
KEY_RO = "bmya_ro_aaa111_" + "r" * 43
KEY_RW = "bmya_rw_bbb222_" + "w" * 43
KEY_REVOKED = "bmya_ro_ccc333_" + "x" * 43
KEY_EXPIRED = "bmya_ro_ddd444_" + "y" * 43
KEY_UNKNOWN = "bmya_ro_eee555_" + "z" * 43

_PAST = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
_FUTURE = (datetime.now(timezone.utc) + timedelta(days=365)).isoformat()


def build_registry_data():
    """A registry covering the grant states the tests need."""
    return {
        "version": 1,
        "grants": [
            {
                "key_id": "aaa111",
                "key_sha256": bmya_auth.hash_key(KEY_RO),
                "label": "ClienteX prod - lectura",
                "odoo_url": "https://clientex.bmya.cloud",
                "database": "clientex_prod",
                "mode": "readonly",
                "expires_at": _FUTURE,
            },
            {
                "key_id": "bbb222",
                "key_sha256": bmya_auth.hash_key(KEY_RW),
                "label": "ClienteX prod - escritura",
                "odoo_url": "https://clientex.bmya.cloud",
                "database": "clientex_prod",
                "mode": "readwrite",
                "allowed_methods": ["account.move.action_post"],
                "denied_models": ["res.users"],
            },
            {
                "key_id": "ccc333",
                "key_sha256": bmya_auth.hash_key(KEY_REVOKED),
                "label": "revocada",
                "odoo_url": "https://clientey.bmya.cloud",
                "database": "clientey_prod",
                "mode": "readonly",
                "revoked": True,
            },
            {
                "key_id": "ddd444",
                "key_sha256": bmya_auth.hash_key(KEY_EXPIRED),
                "label": "vencida",
                "odoo_url": "https://clientez.bmya.cloud",
                "database": "clientez_prod",
                "mode": "readonly",
                "expires_at": _PAST,
            },
        ],
    }


@pytest.fixture
def registry_path(tmp_path):
    """Write a registry file and return its path."""
    path = tmp_path / "bmya-api-keys.json"
    path.write_text(json.dumps(build_registry_data(), indent=2), encoding="utf-8")
    return path


@pytest.fixture
def auth_env(registry_path, monkeypatch):
    """Point bmya_auth at a temp registry with a clean cache on both sides.

    The module-level registry cache is the one piece of cross-test state that
    causes order-dependent flakes, so it is invalidated on entry and on exit.
    """
    bmya_auth.invalidate_registry_cache()
    monkeypatch.setattr(bmya_auth, "BMYA_API_KEYS_FILE", str(registry_path))
    monkeypatch.setattr(bmya_auth, "BMYA_AUTH_ENABLED", True)
    monkeypatch.setattr(bmya_auth, "BMYA_KEYS_BACKEND", "file")
    monkeypatch.setattr(bmya_auth, "BMYA_REGISTRY_TTL", 0.0)
    monkeypatch.setattr(bmya_auth, "BMYA_ALLOWED_URL_SUFFIXES", ())
    monkeypatch.setattr(bmya_auth, "BMYA_ALLOW_INSECURE_URLS", False)
    yield registry_path
    bmya_auth.invalidate_registry_cache()


@pytest.fixture
def headers_factory():
    """Build a case-insensitive header mapping like Starlette's."""
    from starlette.datastructures import Headers

    def _make(bmya_key=None, odoo_key="user-odoo-key", mode=None, **extra):
        raw = dict(extra)
        if bmya_key is not None:
            raw["X-Bmya-Api-Key"] = bmya_key
        if odoo_key is not None:
            raw["X-Odoo-Api-Key"] = odoo_key
        if mode is not None:
            raw["X-Odoo-Mode"] = mode
        return Headers(raw)

    return _make


# --- Admin console fixtures
#
# The console is a second service in the same repo; these live here rather than
# in a console-only conftest so a test can mix both (mint through the console,
# then resolve the key through bmya_auth, which is the check that matters).

CONSOLE_OPERATOR = "daniel@bmya.cl"
CONSOLE_KEY = "bmyacon_" + "k" * 43
CONSOLE_SESSION_SECRET = "test-session-secret"


@pytest.fixture
def console_settings(tmp_path, monkeypatch):
    """A Settings pointing at an empty temp registry and meta file.

    Also invalidates bmya_auth's module-level registry cache on both sides and
    points it at the same file, so a test can mint through the console and then
    resolve the plaintext through the server's own code path. That cache is the
    documented source of order-dependent flakes.
    """
    from bmya_console.config import Settings

    bmya_auth.invalidate_registry_cache()
    registry = tmp_path / "bmya-api-keys.json"
    registry.write_text(json.dumps({"version": 1, "grants": []}), encoding="utf-8")

    monkeypatch.setattr(bmya_auth, "BMYA_API_KEYS_FILE", str(registry))
    monkeypatch.setattr(bmya_auth, "BMYA_AUTH_ENABLED", True)
    monkeypatch.setattr(bmya_auth, "BMYA_KEYS_BACKEND", "file")
    monkeypatch.setattr(bmya_auth, "BMYA_REGISTRY_TTL", 0.0)
    monkeypatch.setattr(bmya_auth, "BMYA_ALLOWED_URL_SUFFIXES", (".bmya.cloud",))
    monkeypatch.setattr(bmya_auth, "BMYA_ALLOW_INSECURE_URLS", False)

    settings = Settings(
        registry_file=str(registry),
        meta_file=str(tmp_path / "bmya-console-meta.json"),
        session_secret=CONSOLE_SESSION_SECRET,
        mcp_server_url="https://mcp.bmya.cloud/mcp/",
        mcp_readyz_url="",  # not polled in tests unless a test sets it
        operators={CONSOLE_OPERATOR: bmya_auth.hash_key(CONSOLE_KEY)},
        probe_enabled=True,
    )
    yield settings
    bmya_auth.invalidate_registry_cache()


@pytest.fixture
def console_client(console_settings):
    """An unauthenticated TestClient over the console app."""
    from starlette.testclient import TestClient

    from bmya_console.app import build_console_app

    return TestClient(build_console_app(console_settings))


@pytest.fixture
def logged_in_client(console_client):
    """A client that has completed a real login.

    Deliberately logs in through the actual form rather than forging a cookie or
    adding a test-only bypass env var: a bypass in production code is exactly
    the kind of thing that ships.
    """
    response = console_client.post("/login", data={"email": CONSOLE_OPERATOR, "key": CONSOLE_KEY})
    assert response.status_code == 200, "login fixture failed"
    return console_client


def csrf_from(client, path="/grants/new") -> str:
    """Pull the session's CSRF token out of a rendered form."""
    text = client.get(path).text
    return text.split('name="csrf_token" value="')[1].split('"')[0]


GRANT_FORM = {
    "odoo_url": "https://clientex.bmya.cloud",
    "database": "clientex_prod",
    "mode": "readonly",
    "label": "ClienteX lectura",
    "client_name": "clientex",
    "methods_mode": "inherit",
    "models_mode": "unrestricted",
    "denied_models": "",
    "expires_at": "",
    "notes": "",
}
