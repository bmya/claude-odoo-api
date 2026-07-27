"""
Unit tests for the BMYA authorization layer (src/bmya_auth.py).
"""

import json
from datetime import datetime, timedelta, timezone

import pytest

import bmya_auth
from bmya_auth import (
    MODE_RO,
    MODE_RW,
    PUBLIC_AUTH_ERROR,
    AuthError,
    Grant,
    RegistryError,
    RegistryUnavailable,
    ToolDenied,
)
from tests.conftest import (
    KEY_EXPIRED,
    KEY_REVOKED,
    KEY_RO,
    KEY_RW,
    KEY_UNKNOWN,
    build_registry_data,
)


def write_registry(path, data):
    path.write_text(json.dumps(data), encoding="utf-8")


def single_grant(**overrides):
    """A minimal valid grant dict, with overrides applied."""
    grant = {
        "key_id": "aaa111",
        "key_sha256": bmya_auth.hash_key(KEY_RO),
        "odoo_url": "https://clientex.bmya.cloud",
        "database": "clientex_prod",
        "mode": "readonly",
    }
    grant.update(overrides)
    return {"version": 1, "grants": [grant]}


class TestKeyHandling:
    def test_generate_key_shape_and_roundtrip(self):
        plaintext, key_id = bmya_auth.generate_key(MODE_RO)
        assert plaintext.startswith(f"bmya_ro_{key_id}_")
        assert bmya_auth.parse_key_id(plaintext) == key_id
        assert bmya_auth.parse_key_mode_hint(plaintext) == MODE_RO
        assert len(bmya_auth.hash_key(plaintext)) == 64

    def test_generate_key_readwrite_prefix(self):
        plaintext, _ = bmya_auth.generate_key(MODE_RW)
        assert plaintext.startswith("bmya_rw_")
        assert bmya_auth.parse_key_mode_hint(plaintext) == MODE_RW

    def test_generate_key_rejects_bad_mode(self):
        with pytest.raises(ValueError):
            bmya_auth.generate_key("admin")

    def test_keys_are_unique(self):
        first, _ = bmya_auth.generate_key(MODE_RO)
        second, _ = bmya_auth.generate_key(MODE_RO)
        assert first != second

    def test_hash_ignores_surrounding_whitespace(self):
        assert bmya_auth.hash_key(f"  {KEY_RO}\n") == bmya_auth.hash_key(KEY_RO)

    def test_parse_key_id_returns_none_for_garbage(self):
        assert bmya_auth.parse_key_id("not-a-bmya-key") is None
        assert bmya_auth.parse_key_id("bmya_xx_aaa111_secret") is None
        # key_id must be hex
        assert bmya_auth.parse_key_id("bmya_ro_zzzzzz_secret") is None

    def test_gateway_token_comparison(self):
        assert bmya_auth.check_gateway_token("s3cret", "s3cret") is True
        assert bmya_auth.check_gateway_token("wrong", "s3cret") is False
        assert bmya_auth.check_gateway_token(None, "s3cret") is False
        # No token configured means the check is not in play.
        assert bmya_auth.check_gateway_token(None, None) is True
        assert bmya_auth.check_gateway_token("anything", "") is True


class TestRegistryLoading:
    def test_loads_valid_registry(self, auth_env):
        registry = bmya_auth.load_registry(str(auth_env))
        assert len(registry.grants_by_hash) == 4
        assert registry.databases == [
            "clientex_prod",
            "clientey_prod",
            "clientez_prod",
        ]
        grant = registry.grants_by_hash[bmya_auth.hash_key(KEY_RO)]
        assert grant.key_id == "aaa111"
        assert grant.mode == MODE_RO
        assert grant.database == "clientex_prod"

    def test_missing_file_raises_registry_error(self, tmp_path):
        with pytest.raises(RegistryError, match="not found"):
            bmya_auth.load_registry(str(tmp_path / "nope.json"))

    def test_malformed_json_raises_registry_error(self, tmp_path):
        path = tmp_path / "bad.json"
        path.write_text("{not json", encoding="utf-8")
        with pytest.raises(RegistryError, match="not valid JSON"):
            bmya_auth.load_registry(str(path))

    def test_top_level_must_be_object(self, tmp_path):
        path = tmp_path / "list.json"
        path.write_text("[]", encoding="utf-8")
        with pytest.raises(RegistryError, match="must be a JSON object"):
            bmya_auth.load_registry(str(path))

    def test_grants_must_be_a_list(self, tmp_path):
        path = tmp_path / "g.json"
        write_registry(path, {"version": 1, "grants": {}})
        with pytest.raises(RegistryError, match="'grants' must be a list"):
            bmya_auth.load_registry(str(path))

    def test_invalid_mode_skips_grant_without_killing_registry(self, tmp_path, caplog):
        path = tmp_path / "g.json"
        data = single_grant(mode="admin")
        data["grants"].append(
            {
                "key_id": "bbb222",
                "key_sha256": bmya_auth.hash_key(KEY_RW),
                "odoo_url": "https://ok.bmya.cloud",
                "database": "ok",
                "mode": "readwrite",
            }
        )
        write_registry(path, data)
        registry = bmya_auth.load_registry(str(path))
        # The bad grant is dropped; the good one survives.
        assert len(registry.grants_by_hash) == 1
        assert registry.by_key_id("bbb222") is not None
        assert "mode" in caplog.text

    def test_bad_hash_format_skips_grant(self, tmp_path):
        path = tmp_path / "g.json"
        write_registry(path, single_grant(key_sha256="TOOSHORT"))
        assert bmya_auth.load_registry(str(path)).grants_by_hash == {}

    def test_uppercase_hash_is_normalised(self, tmp_path):
        path = tmp_path / "g.json"
        digest = bmya_auth.hash_key(KEY_RO)
        write_registry(path, single_grant(key_sha256=digest.upper()))
        registry = bmya_auth.load_registry(str(path))
        assert digest in registry.grants_by_hash

    def test_missing_required_field_skips_grant(self, tmp_path):
        path = tmp_path / "g.json"
        data = single_grant()
        del data["grants"][0]["database"]
        write_registry(path, data)
        assert bmya_auth.load_registry(str(path)).grants_by_hash == {}

    def test_http_url_rejected_by_default(self, tmp_path):
        path = tmp_path / "g.json"
        write_registry(path, single_grant(odoo_url="http://clientex.bmya.cloud"))
        assert bmya_auth.load_registry(str(path)).grants_by_hash == {}

    def test_http_url_allowed_when_insecure_flag_set(self, tmp_path, monkeypatch):
        monkeypatch.setattr(bmya_auth, "BMYA_ALLOW_INSECURE_URLS", True)
        path = tmp_path / "g.json"
        write_registry(path, single_grant(odoo_url="http://localhost:8069"))
        assert len(bmya_auth.load_registry(str(path)).grants_by_hash) == 1

    @pytest.mark.parametrize(
        "url",
        [
            "https://127.0.0.1",
            "https://169.254.169.254",
            "https://10.0.0.14",
            "https://192.168.1.10",
        ],
    )
    def test_private_and_link_local_ip_literals_rejected(self, tmp_path, url):
        """A bad registry edit must not be able to aim the server at the metadata service."""
        path = tmp_path / "g.json"
        write_registry(path, single_grant(odoo_url=url))
        assert bmya_auth.load_registry(str(path)).grants_by_hash == {}

    def test_url_with_credentials_or_query_rejected(self, tmp_path):
        for url in [
            "https://user:pass@clientex.bmya.cloud",
            "https://clientex.bmya.cloud?db=x",
            "https://clientex.bmya.cloud#frag",
        ]:
            path = tmp_path / "g.json"
            write_registry(path, single_grant(odoo_url=url))
            assert bmya_auth.load_registry(str(path)).grants_by_hash == {}, url

    def test_trailing_slash_normalised(self, tmp_path):
        path = tmp_path / "g.json"
        write_registry(path, single_grant(odoo_url="https://clientex.bmya.cloud/"))
        grant = bmya_auth.load_registry(str(path)).by_key_id("aaa111")
        assert grant.odoo_url == "https://clientex.bmya.cloud"

    def test_url_suffix_allowlist(self, tmp_path, monkeypatch):
        monkeypatch.setattr(bmya_auth, "BMYA_ALLOWED_URL_SUFFIXES", (".bmya.cloud", ".bmya.cl"))
        path = tmp_path / "g.json"
        write_registry(path, single_grant(odoo_url="https://evil.example.com"))
        assert bmya_auth.load_registry(str(path)).grants_by_hash == {}

        write_registry(path, single_grant(odoo_url="https://clientex.bmya.cloud"))
        assert len(bmya_auth.load_registry(str(path)).grants_by_hash) == 1

    def test_duplicate_hash_keeps_first(self, tmp_path, caplog):
        path = tmp_path / "g.json"
        data = single_grant()
        duplicate = dict(data["grants"][0])
        duplicate["key_id"] = "ffffff"
        duplicate["database"] = "other_db"
        data["grants"].append(duplicate)
        write_registry(path, data)
        registry = bmya_auth.load_registry(str(path))
        assert len(registry.grants_by_hash) == 1
        assert registry.grants_by_hash[bmya_auth.hash_key(KEY_RO)].database == "clientex_prod"
        assert "duplicate" in caplog.text.lower()

    def test_duplicate_key_id_keeps_first(self, tmp_path, caplog):
        path = tmp_path / "g.json"
        data = single_grant()
        data["grants"].append(
            {
                "key_id": "aaa111",
                "key_sha256": bmya_auth.hash_key(KEY_RW),
                "odoo_url": "https://other.bmya.cloud",
                "database": "other_db",
                "mode": "readwrite",
            }
        )
        write_registry(path, data)
        registry = bmya_auth.load_registry(str(path))
        assert len(registry.grants_by_hash) == 1
        assert "duplicate" in caplog.text.lower()

    def test_unknown_field_tolerated(self, tmp_path, caplog):
        path = tmp_path / "g.json"
        write_registry(path, single_grant(future_option="whatever"))
        registry = bmya_auth.load_registry(str(path))
        assert len(registry.grants_by_hash) == 1
        assert "unknown field" in caplog.text.lower()

    def test_naive_expires_at_treated_as_utc(self, tmp_path):
        path = tmp_path / "g.json"
        write_registry(path, single_grant(expires_at="2099-01-31T23:59:59"))
        grant = bmya_auth.load_registry(str(path)).by_key_id("aaa111")
        assert grant.expires_at.tzinfo is not None
        assert grant.expires_at.utcoffset().total_seconds() == 0

    def test_z_suffix_expires_at(self, tmp_path):
        path = tmp_path / "g.json"
        write_registry(path, single_grant(expires_at="2099-01-31T23:59:59Z"))
        grant = bmya_auth.load_registry(str(path)).by_key_id("aaa111")
        assert grant.expires_at.year == 2099

    def test_invalid_expires_at_skips_grant(self, tmp_path):
        path = tmp_path / "g.json"
        write_registry(path, single_grant(expires_at="not-a-date"))
        assert bmya_auth.load_registry(str(path)).grants_by_hash == {}

    def test_allowed_methods_null_versus_empty(self, tmp_path):
        path = tmp_path / "g.json"
        write_registry(path, single_grant(allowed_methods=None))
        assert bmya_auth.load_registry(str(path)).by_key_id("aaa111").allowed_methods is None

        write_registry(path, single_grant(allowed_methods=[]))
        assert bmya_auth.load_registry(str(path)).by_key_id("aaa111").allowed_methods == frozenset()


class TestRegistryCache:
    def test_no_reread_within_ttl(self, auth_env, monkeypatch):
        monkeypatch.setattr(bmya_auth, "BMYA_REGISTRY_TTL", 3600.0)
        bmya_auth.invalidate_registry_cache()
        first = bmya_auth.get_registry()

        # Corrupt the file: within the TTL it must not even be looked at.
        auth_env.write_text("{ garbage", encoding="utf-8")
        assert bmya_auth.get_registry() is first

    def test_reload_on_change_makes_revocation_visible(self, auth_env, monkeypatch):
        monkeypatch.setattr(bmya_auth, "BMYA_REGISTRY_TTL", 0.0)
        bmya_auth.invalidate_registry_cache()
        headers = {bmya_auth.HEADER_BMYA_KEY: KEY_RO}
        assert bmya_auth.resolve_grant(headers).key_id == "aaa111"

        data = build_registry_data()
        data["grants"][0]["revoked"] = True
        write_registry(auth_env, data)

        # No restart, no cache invalidation: the mtime change is enough.
        with pytest.raises(AuthError) as excinfo:
            bmya_auth.resolve_grant(headers)
        assert excinfo.value.reason == "revoked"

    def test_last_good_snapshot_kept_when_file_breaks(self, auth_env, monkeypatch, caplog):
        monkeypatch.setattr(bmya_auth, "BMYA_REGISTRY_TTL", 0.0)
        bmya_auth.invalidate_registry_cache()
        good = bmya_auth.get_registry()
        assert len(good.grants_by_hash) == 4

        auth_env.write_text("{ broken", encoding="utf-8")
        still = bmya_auth.get_registry()
        assert len(still.grants_by_hash) == 4
        assert bmya_auth.registry_status()["stale"] is True
        assert "keeping last good copy" in caplog.text

    def test_registry_unavailable_when_never_loaded(self, tmp_path, monkeypatch):
        monkeypatch.setattr(bmya_auth, "BMYA_API_KEYS_FILE", str(tmp_path / "missing.json"))
        monkeypatch.setattr(bmya_auth, "BMYA_REGISTRY_TTL", 0.0)
        bmya_auth.invalidate_registry_cache()
        with pytest.raises(RegistryUnavailable):
            bmya_auth.get_registry()

    def test_unsupported_backend_raises(self, auth_env, monkeypatch):
        monkeypatch.setattr(bmya_auth, "BMYA_KEYS_BACKEND", "http")
        bmya_auth.invalidate_registry_cache()
        with pytest.raises(RegistryUnavailable):
            bmya_auth.get_registry()

    def test_registry_status_reports_disabled(self, monkeypatch):
        monkeypatch.setattr(bmya_auth, "BMYA_AUTH_ENABLED", False)
        assert bmya_auth.registry_status() == {
            "enabled": False,
            "loaded": True,
            "grants": 0,
            "stale": False,
        }

    def test_registry_status_reports_unloaded(self, tmp_path, monkeypatch):
        monkeypatch.setattr(bmya_auth, "BMYA_AUTH_ENABLED", True)
        monkeypatch.setattr(bmya_auth, "BMYA_API_KEYS_FILE", str(tmp_path / "missing.json"))
        bmya_auth.invalidate_registry_cache()
        status = bmya_auth.registry_status()
        assert status["loaded"] is False


class TestResolveGrant:
    def test_happy_path(self, auth_env):
        grant = bmya_auth.resolve_grant({bmya_auth.HEADER_BMYA_KEY: KEY_RW})
        assert grant.key_id == "bbb222"
        assert grant.mode == MODE_RW
        assert grant.odoo_url == "https://clientex.bmya.cloud"

    def test_whitespace_padded_key_accepted(self, auth_env):
        grant = bmya_auth.resolve_grant({bmya_auth.HEADER_BMYA_KEY: f"  {KEY_RO}  "})
        assert grant.key_id == "aaa111"

    def test_case_insensitive_header_name(self, auth_env, headers_factory):
        grant = bmya_auth.resolve_grant(headers_factory(bmya_key=KEY_RO))
        assert grant.key_id == "aaa111"

    @pytest.mark.parametrize(
        "key,expected_reason",
        [
            (None, "missing_key"),
            ("", "missing_key"),
            ("garbage", "malformed_key"),
            (KEY_UNKNOWN, "unknown_key"),
            (KEY_REVOKED, "revoked"),
            (KEY_EXPIRED, "expired"),
        ],
    )
    def test_failures_share_one_public_message(self, auth_env, key, expected_reason):
        headers = {} if key is None else {bmya_auth.HEADER_BMYA_KEY: key}
        with pytest.raises(AuthError) as excinfo:
            bmya_auth.resolve_grant(headers)
        # The reason differs for the log...
        assert excinfo.value.reason == expected_reason
        # ...but the caller learns nothing about which failure it was.
        assert str(excinfo.value) == PUBLIC_AUTH_ERROR

    def test_mode_hint_mismatch_logs_but_registry_wins(self, auth_env, tmp_path, caplog):
        """A key minted as rw but granted readonly resolves readonly."""
        data = single_grant(key_sha256=bmya_auth.hash_key(KEY_RW), mode="readonly")
        write_registry(auth_env, data)
        bmya_auth.invalidate_registry_cache()
        grant = bmya_auth.resolve_grant({bmya_auth.HEADER_BMYA_KEY: KEY_RW})
        assert grant.mode == MODE_RO
        assert "registry wins" in caplog.text


class TestEffectiveMode:
    @pytest.mark.parametrize("server_readonly", [False, True])
    @pytest.mark.parametrize("grant_mode", [MODE_RO, MODE_RW])
    @pytest.mark.parametrize("requested", [None, MODE_RO, MODE_RW])
    def test_most_restrictive_wins(self, server_readonly, grant_mode, requested):
        grant = Grant(
            key_id="k",
            key_sha256="0" * 64,
            odoo_url="https://x.bmya.cloud",
            database="db",
            mode=grant_mode,
        )
        result = bmya_auth.effective_mode(
            grant, server_readonly=server_readonly, requested=requested
        )
        expected = (
            MODE_RO
            if (server_readonly or grant_mode == MODE_RO or requested == MODE_RO)
            else MODE_RW
        )
        assert result == expected

    def test_client_cannot_widen_beyond_its_grant(self):
        grant = Grant(
            key_id="k",
            key_sha256="0" * 64,
            odoo_url="https://x.bmya.cloud",
            database="db",
            mode=MODE_RO,
        )
        assert bmya_auth.effective_mode(grant, server_readonly=False, requested=MODE_RW) == MODE_RO

    def test_server_ceiling_overrides_readwrite_grant(self):
        grant = Grant(
            key_id="k",
            key_sha256="0" * 64,
            odoo_url="https://x.bmya.cloud",
            database="db",
            mode=MODE_RW,
        )
        assert bmya_auth.effective_mode(grant, server_readonly=True, requested=MODE_RW) == MODE_RO

    def test_unknown_requested_mode_is_denied(self):
        grant = Grant(
            key_id="k",
            key_sha256="0" * 64,
            odoo_url="https://x.bmya.cloud",
            database="db",
            mode=MODE_RW,
        )
        with pytest.raises(ToolDenied, match="Unsupported"):
            bmya_auth.effective_mode(grant, server_readonly=False, requested="admin")

    def test_blank_requested_mode_ignored(self):
        grant = Grant(
            key_id="k",
            key_sha256="0" * 64,
            odoo_url="https://x.bmya.cloud",
            database="db",
            mode=MODE_RW,
        )
        assert bmya_auth.effective_mode(grant, server_readonly=False, requested="  ") == MODE_RW


def _grant(**kwargs):
    base = dict(
        key_id="k",
        key_sha256="0" * 64,
        odoo_url="https://x.bmya.cloud",
        database="db",
        mode=MODE_RW,
    )
    base.update(kwargs)
    return Grant(**base)


class TestEffectiveAllowedMethods:
    SERVER = {"account.move.action_post", "sale.order.action_confirm"}

    def test_null_inherits_server_list(self):
        assert bmya_auth.effective_allowed_methods(
            _grant(allowed_methods=None), self.SERVER
        ) == frozenset(self.SERVER)

    def test_empty_means_none_allowed(self):
        assert (
            bmya_auth.effective_allowed_methods(_grant(allowed_methods=frozenset()), self.SERVER)
            == frozenset()
        )

    def test_subset_is_intersected(self):
        assert bmya_auth.effective_allowed_methods(
            _grant(allowed_methods=frozenset({"account.move.action_post"})), self.SERVER
        ) == frozenset({"account.move.action_post"})

    def test_grant_cannot_add_methods_the_server_forbids(self):
        """The grant narrows the server list; it can never extend it."""
        assert (
            bmya_auth.effective_allowed_methods(
                _grant(allowed_methods=frozenset({"res.users.unlink"})), self.SERVER
            )
            == frozenset()
        )


class TestAuthorizeTool:
    SERVER = {"account.move.action_post", "sale.order.action_confirm"}

    @pytest.mark.parametrize(
        "tool", ["odoo_create", "odoo_write", "odoo_unlink", "odoo_call_method"]
    )
    def test_readonly_blocks_every_write_tool(self, tool):
        with pytest.raises(ToolDenied, match="Read-only"):
            bmya_auth.authorize_tool(
                tool,
                {"model": "res.partner", "method": "action_post"},
                _grant(mode=MODE_RO),
                mode=MODE_RO,
                server_allowed_methods=self.SERVER,
            )

    @pytest.mark.parametrize(
        "tool", ["odoo_search_read", "odoo_search", "odoo_read", "odoo_search_count"]
    )
    def test_readonly_allows_read_tools(self, tool):
        bmya_auth.authorize_tool(
            tool,
            {"model": "res.partner"},
            _grant(mode=MODE_RO),
            mode=MODE_RO,
            server_allowed_methods=self.SERVER,
        )

    def test_denied_model_blocked(self):
        with pytest.raises(ToolDenied, match="not available"):
            bmya_auth.authorize_tool(
                "odoo_search_read",
                {"model": "res.users"},
                _grant(denied_models=frozenset({"res.users"})),
                mode=MODE_RW,
                server_allowed_methods=self.SERVER,
            )

    def test_model_outside_allowlist_blocked(self):
        with pytest.raises(ToolDenied, match="not in the allowed model list"):
            bmya_auth.authorize_tool(
                "odoo_search_read",
                {"model": "account.move"},
                _grant(allowed_models=frozenset({"res.partner"})),
                mode=MODE_RW,
                server_allowed_methods=self.SERVER,
            )

    def test_denied_models_wins_over_allowed_models(self):
        grant = _grant(
            allowed_models=frozenset({"res.users"}),
            denied_models=frozenset({"res.users"}),
        )
        with pytest.raises(ToolDenied, match="not available"):
            bmya_auth.authorize_tool(
                "odoo_search_read",
                {"model": "res.users"},
                grant,
                mode=MODE_RW,
                server_allowed_methods=self.SERVER,
            )

    def test_no_model_restriction_by_default(self):
        bmya_auth.authorize_tool(
            "odoo_search_read",
            {"model": "anything.at.all"},
            _grant(),
            mode=MODE_RW,
            server_allowed_methods=self.SERVER,
        )

    def test_call_method_inside_intersection_allowed(self):
        bmya_auth.authorize_tool(
            "odoo_call_method",
            {"model": "account.move", "method": "action_post"},
            _grant(allowed_methods=frozenset({"account.move.action_post"})),
            mode=MODE_RW,
            server_allowed_methods=self.SERVER,
        )

    def test_call_method_outside_intersection_denied(self):
        with pytest.raises(ToolDenied, match="not allowed"):
            bmya_auth.authorize_tool(
                "odoo_call_method",
                {"model": "sale.order", "method": "action_confirm"},
                _grant(allowed_methods=frozenset({"account.move.action_post"})),
                mode=MODE_RW,
                server_allowed_methods=self.SERVER,
            )

    def test_call_method_denied_when_server_forbids(self):
        with pytest.raises(ToolDenied, match="not allowed"):
            bmya_auth.authorize_tool(
                "odoo_call_method",
                {"model": "res.users", "method": "unlink"},
                _grant(allowed_methods=None),
                mode=MODE_RW,
                server_allowed_methods=self.SERVER,
            )

    def test_tools_without_model_argument_pass(self):
        bmya_auth.authorize_tool(
            "odoo_list_models",
            {"filter": "res"},
            _grant(allowed_models=frozenset({"res.partner"})),
            mode=MODE_RW,
            server_allowed_methods=self.SERVER,
        )


class TestGrantDescribe:
    def test_describe_has_no_secrets(self, auth_env):
        grant = bmya_auth.resolve_grant({bmya_auth.HEADER_BMYA_KEY: KEY_RW})
        text = grant.describe(mode=MODE_RO)
        assert "clientex_prod" in text
        assert "https://clientex.bmya.cloud" in text
        assert MODE_RO in text
        assert grant.key_sha256 not in text
        assert KEY_RW not in text

    def test_describe_shows_effective_values(self):
        grant = _grant(allowed_methods=frozenset({"a.b"}))
        text = grant.describe(mode=MODE_RO, allowed_methods=frozenset({"c.d"}))
        assert "c.d" in text
        assert "a.b" not in text


class TestGrantExpiry:
    def test_no_expiry_never_expires(self):
        assert _grant().is_expired() is False

    def test_past_expiry(self):
        past = datetime.now(timezone.utc) - timedelta(seconds=1)
        assert _grant(expires_at=past).is_expired() is True

    def test_future_expiry(self):
        future = datetime.now(timezone.utc) + timedelta(days=1)
        assert _grant(expires_at=future).is_expired() is False

    def test_explicit_now_argument(self):
        moment = datetime(2026, 1, 1, tzinfo=timezone.utc)
        grant = _grant(expires_at=datetime(2025, 1, 1, tzinfo=timezone.utc))
        assert grant.is_expired(now=moment) is True
