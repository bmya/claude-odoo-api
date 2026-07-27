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
