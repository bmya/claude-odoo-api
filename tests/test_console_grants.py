"""
Minting, revoking and editing through the console.

The tests that matter most here are not the happy path: they are the ones that
pin (a) that a rejected form leaves the registry byte-identical, (b) that the
key the console shows actually resolves through the server's own code, and (c)
that the plaintext appears exactly once and never reaches a log.
"""

import json

import pytest
from starlette.testclient import TestClient

import bmya_auth
import bmya_registry
from bmya_console.app import build_console_app
from tests.conftest import GRANT_FORM, csrf_from

SHOW_ONCE_MARKER = '<textarea class="secret mono" readonly onclick="this.select()">'


def mint(client, **overrides):
    """Submit the grant form, returning the response."""
    data = dict(GRANT_FORM)
    data.update(overrides)
    data["csrf_token"] = csrf_from(client)
    return client.post("/grants", data=data)


def shown_key(response) -> str:
    return response.text.split(SHOW_ONCE_MARKER)[1].split("</textarea>")[0].strip()


def grants_of(settings):
    return bmya_registry.read_raw(settings.registry_file, allow_missing=True)["grants"]


class TestMint:
    def test_mints_and_shows_the_key(self, logged_in_client, console_settings):
        response = mint(logged_in_client)
        assert response.status_code == 200

        plaintext = shown_key(response)
        assert plaintext.startswith("bmya_ro_")
        # Exactly one dedicated "copy this" box. The key also appears inside the
        # onboarding snippets further down the page, which is the whole point of
        # them -- so counting occurrences page-wide would assert the wrong thing.
        assert response.text.count(SHOW_ONCE_MARKER) == 1

    def test_the_minted_key_actually_resolves(self, logged_in_client, console_settings):
        """The check that makes all the others meaningful: feed the plaintext
        the console displayed through bmya_auth, against the file it wrote."""
        plaintext = shown_key(mint(logged_in_client))

        bmya_auth.invalidate_registry_cache()
        grant = bmya_auth.resolve_grant({"x-bmya-api-key": plaintext})

        assert grant.database == "clientex_prod"
        assert grant.odoo_url == "https://clientex.bmya.cloud"
        assert grant.mode == bmya_auth.MODE_RO

    def test_registry_stores_only_the_digest(self, logged_in_client, console_settings):
        plaintext = shown_key(mint(logged_in_client))
        raw = open(console_settings.registry_file, encoding="utf-8").read()
        assert plaintext not in raw
        assert bmya_auth.hash_key(plaintext) in raw

    def test_the_key_is_not_retrievable_afterwards(self, logged_in_client, console_settings):
        plaintext = shown_key(mint(logged_in_client))
        key_id = grants_of(console_settings)[0]["key_id"]

        assert plaintext not in logged_in_client.get("/grants").text
        assert plaintext not in logged_in_client.get(f"/grants/{key_id}").text

    def test_the_plaintext_never_reaches_the_log(self, logged_in_client, caplog):
        """The audit line carries key_id only. A key in a rotating docker log is
        a key on disk for as long as the log survives."""
        import logging

        with caplog.at_level(logging.DEBUG):
            plaintext = shown_key(mint(logged_in_client))
        assert plaintext not in caplog.text

    def test_show_once_response_is_not_cacheable(self, logged_in_client):
        response = mint(logged_in_client)
        assert "no-store" in response.headers["cache-control"]
        assert response.headers["referrer-policy"] == "no-referrer"

    def test_no_route_can_show_the_key_again(self, logged_in_client, console_settings):
        """There must be no GET that returns a plaintext key -- a convenience
        route like /grants/{id}/key would put it in history and access logs."""
        mint(logged_in_client)
        key_id = grants_of(console_settings)[0]["key_id"]
        assert logged_in_client.get(f"/grants/{key_id}/key").status_code == 404

    def test_includes_the_client_snippets(self, logged_in_client):
        response = mint(logged_in_client)
        plaintext = shown_key(response)
        assert "claude mcp add --transport http clientex" in response.text
        # The snippet is built from the same normalizer the CLI uses.
        assert "https://mcp.bmya.cloud/mcp/" in response.text
        assert plaintext in response.text

    def test_records_who_minted_it(self, logged_in_client, console_settings):
        from tests.conftest import CONSOLE_OPERATOR

        mint(logged_in_client)
        key_id = grants_of(console_settings)[0]["key_id"]
        meta = json.load(open(console_settings.meta_file, encoding="utf-8"))
        assert meta["keys"][key_id]["created_by"] == CONSOLE_OPERATOR
        # Reserved and null from day one so enabling them is not a schema change.
        assert meta["keys"][key_id]["invitation_id"] is None
        assert meta["keys"][key_id]["credit_account_id"] is None
        assert meta["invitations"] == []


class TestValidationLeavesTheRegistryAlone:
    """Every rejection must be byte-identical-file territory. A form error that
    half-writes a grant is worse than one that writes nothing."""

    def _unchanged(self, client, settings, **overrides):
        before = open(settings.registry_file, "rb").read()
        response = mint(client, **overrides)
        assert response.status_code == 400
        assert open(settings.registry_file, "rb").read() == before
        return response

    def test_url_outside_the_suffix_allowlist(self, logged_in_client, console_settings):
        """The console cannot mint what the server would refuse: both call the
        same validate_odoo_url."""
        response = self._unchanged(
            logged_in_client, console_settings, odoo_url="https://evil.example.com"
        )
        assert "BMYA_ALLOWED_URL_SUFFIXES" in response.text

    def test_private_ip_url(self, logged_in_client, console_settings):
        self._unchanged(logged_in_client, console_settings, odoo_url="https://10.0.0.14")

    def test_http_url(self, logged_in_client, console_settings):
        self._unchanged(logged_in_client, console_settings, odoo_url="http://clientex.bmya.cloud")

    def test_missing_database(self, logged_in_client, console_settings):
        self._unchanged(logged_in_client, console_settings, database="")

    def test_bad_mode(self, logged_in_client, console_settings):
        self._unchanged(logged_in_client, console_settings, mode="readonlyy")

    def test_past_expiry_is_accepted_but_the_key_is_born_expired(
        self, logged_in_client, console_settings
    ):
        """Not a validation error -- the schema allows it -- but the grant must
        actually be expired, not quietly active."""
        mint(logged_in_client, expires_at="2020-01-01")
        bmya_auth.invalidate_registry_cache()
        registry = bmya_auth.load_registry(console_settings.registry_file)
        assert all(g.is_expired() for g in registry.grants_by_hash.values())

    def test_bad_expiry_format(self, logged_in_client, console_settings):
        self._unchanged(logged_in_client, console_settings, expires_at="no-es-fecha")


class TestMethodsTriState:
    """allowed_methods is the only field where absent != empty, and a text box
    can express only two of the three states."""

    def _written(self, client, settings, **overrides):
        overrides.setdefault("mode", "readwrite")
        assert mint(client, **overrides).status_code == 200
        return grants_of(settings)[0]

    def test_inherit_writes_null(self, logged_in_client, console_settings):
        grant = self._written(logged_in_client, console_settings, methods_mode="inherit")
        assert grant["allowed_methods"] is None

    def test_none_writes_empty_list(self, logged_in_client, console_settings):
        grant = self._written(logged_in_client, console_settings, methods_mode="none")
        assert grant["allowed_methods"] == []

    def test_list_writes_the_list(self, logged_in_client, console_settings):
        grant = self._written(
            logged_in_client,
            console_settings,
            methods_mode="list",
            methods_list="account.move.action_post\nsale.order.action_confirm",
        )
        assert grant["allowed_methods"] == [
            "account.move.action_post",
            "sale.order.action_confirm",
        ]

    def test_list_with_an_empty_box_is_refused(self, logged_in_client, console_settings):
        """The ambiguity test. An empty box must never silently become [], which
        is the exact opposite of 'inherit'."""
        before = open(console_settings.registry_file, "rb").read()
        response = mint(logged_in_client, mode="readwrite", methods_mode="list", methods_list="   ")
        assert response.status_code == 400
        assert "ningún método" in response.text
        assert open(console_settings.registry_file, "rb").read() == before

    def test_models_list_with_an_empty_box_is_refused(self, logged_in_client, console_settings):
        """[] parses fine but yields a key where every model is denied: valid,
        minted and useless. Never offer it by accident."""
        response = mint(logged_in_client, models_mode="list", models_list="")
        assert response.status_code == 400
        assert not grants_of(console_settings)

    def test_unknown_methods_are_flagged_not_rejected(self, logged_in_client):
        """effective_allowed_methods intersects with the server list, so a
        method absent from it is dead on arrival -- worth a warning, not a 400."""
        response = mint(
            logged_in_client, mode="readwrite", methods_mode="list", methods_list="res.partner.inventado"
        )
        assert response.status_code == 200
        assert "res.partner.inventado" in response.text
        assert "intersección" in response.text

    def test_the_picker_posts_one_value_per_tag(self, logged_in_client, console_settings):
        grant = self._written(
            logged_in_client,
            console_settings,
            methods_mode="list",
            methods=["sale.order.action_confirm", "account.move.action_post", "sale.order.action_confirm"],
        )
        assert grant["allowed_methods"] == ["sale.order.action_confirm", "account.move.action_post"]

    def test_the_picker_offers_exactly_the_server_list(self, logged_in_client):
        page = logged_in_client.get("/grants/new").text
        for m in bmya_auth.server_allowed_methods():
            assert f'name="methods" value="{m}"' in page
        assert "<textarea name=\"methods_list\"" not in page

    def test_a_readonly_key_ignores_methods_and_inherits(self, logged_in_client, console_settings):
        """odoo_call_method is a write tool: for a readonly key the section is
        moot, and an empty 'sólo estos' must not block minting it."""
        grant = self._written(
            logged_in_client, console_settings, mode="readonly", methods_mode="list", methods_list=""
        )
        assert grant["allowed_methods"] is None

    def test_a_failed_mint_keeps_the_picked_tags(self, logged_in_client):
        response = mint(
            logged_in_client,
            mode="readwrite",
            database="",
            methods_mode="list",
            methods=["account.move.action_post", "sale.order.action_confirm"],
        )
        assert response.status_code == 400
        assert 'value="account.move.action_post" checked' in response.text
        assert 'value="sale.order.action_confirm" checked' in response.text


class TestRevoke:
    def _mint_one(self, client, settings):
        plaintext = shown_key(mint(client))
        return plaintext, grants_of(settings)[0]["key_id"]

    def test_revoke_marks_and_keeps_the_record(self, logged_in_client, console_settings):
        _, key_id = self._mint_one(logged_in_client, console_settings)
        response = logged_in_client.post(
            f"/grants/{key_id}/revoke",
            data={"csrf_token": csrf_from(logged_in_client)},
            follow_redirects=False,
        )
        assert response.status_code == 303

        grants = grants_of(console_settings)
        assert len(grants) == 1, "revoking must never delete: the record is the audit trail"
        assert grants[0]["revoked"] is True
        assert grants[0]["revoked_at"]

    def test_a_revoked_key_stops_working(self, logged_in_client, console_settings):
        plaintext, key_id = self._mint_one(logged_in_client, console_settings)
        bmya_auth.invalidate_registry_cache()
        assert bmya_auth.resolve_grant({"x-bmya-api-key": plaintext})  # works first

        logged_in_client.post(
            f"/grants/{key_id}/revoke", data={"csrf_token": csrf_from(logged_in_client)}
        )

        bmya_auth.invalidate_registry_cache()
        with pytest.raises(bmya_auth.AuthError) as excinfo:
            bmya_auth.resolve_grant({"x-bmya-api-key": plaintext})
        assert excinfo.value.reason == "revoked"

    def test_revoking_an_unknown_key_is_a_404(self, logged_in_client):
        response = logged_in_client.post(
            "/grants/zzzzzz/revoke", data={"csrf_token": csrf_from(logged_in_client)}
        )
        assert response.status_code == 404

    def test_revoked_keys_are_hidden_by_default(self, logged_in_client, console_settings):
        _, key_id = self._mint_one(logged_in_client, console_settings)
        logged_in_client.post(
            f"/grants/{key_id}/revoke", data={"csrf_token": csrf_from(logged_in_client)}
        )
        assert key_id not in logged_in_client.get("/grants").text
        assert key_id in logged_in_client.get("/grants?show_revoked=1").text


class TestEdit:
    def _mint_one(self, client, settings):
        mint(client)
        return grants_of(settings)[0]["key_id"]

    def test_can_change_label_notes_and_expiry(self, logged_in_client, console_settings):
        key_id = self._mint_one(logged_in_client, console_settings)
        logged_in_client.post(
            f"/grants/{key_id}/edit",
            data={
                "csrf_token": csrf_from(logged_in_client),
                "label": "nuevo label",
                "notes": "contacto: juan@clientex.cl",
                "expires_at": "2028-06-30",
            },
        )
        grant = grants_of(console_settings)[0]
        assert grant["label"] == "nuevo label"
        assert grant["notes"] == "contacto: juan@clientex.cl"
        assert grant["expires_at"].startswith("2028-06-30")

    @pytest.mark.parametrize(
        "field,value",
        [
            ("mode", "readwrite"),
            ("database", "otra_base"),
            ("odoo_url", "https://otro.bmya.cloud"),
            ("key_sha256", "0" * 64),
        ],
    )
    def test_immutable_fields_are_rejected_not_ignored(
        self, logged_in_client, console_settings, field, value
    ):
        """Silently dropping them would leave the operator believing a change
        took effect. A readonly -> readwrite flip would escalate a key that is
        already sitting in a client's config."""
        key_id = self._mint_one(logged_in_client, console_settings)
        before = open(console_settings.registry_file, "rb").read()

        response = logged_in_client.post(
            f"/grants/{key_id}/edit",
            data={"csrf_token": csrf_from(logged_in_client), field: value},
        )
        assert response.status_code == 400
        assert field in response.json()["fields"]
        assert open(console_settings.registry_file, "rb").read() == before


class TestEditMethods:
    """Methods can change on a live key; the mode still cannot."""

    def _mint_rw(self, client, settings, **overrides):
        mint(client, mode="readwrite", **overrides)
        return grants_of(settings)[0]["key_id"]

    def _edit(self, client, key_id, **fields):
        data = {"csrf_token": csrf_from(client), "label": "x"}
        data.update(fields)
        return client.post(f"/grants/{key_id}/edit", data=data, follow_redirects=False)

    def test_a_list_can_be_added_to_an_inheriting_key(self, logged_in_client, console_settings):
        key_id = self._mint_rw(logged_in_client, console_settings)
        assert grants_of(console_settings)[0]["allowed_methods"] is None

        response = self._edit(
            logged_in_client, key_id,
            methods_mode="list", methods_list="account.move.action_post\nsale.order.action_confirm",
        )

        assert response.status_code == 303
        assert grants_of(console_settings)[0]["allowed_methods"] == [
            "account.move.action_post", "sale.order.action_confirm"
        ]

    def test_the_three_states_stay_distinct(self, logged_in_client, console_settings):
        key_id = self._mint_rw(logged_in_client, console_settings)

        self._edit(logged_in_client, key_id, methods_mode="none")
        assert grants_of(console_settings)[0]["allowed_methods"] == []

        self._edit(logged_in_client, key_id, methods_mode="inherit")
        assert grants_of(console_settings)[0]["allowed_methods"] is None

    def test_list_with_an_empty_box_is_refused_not_silently_none(
        self, logged_in_client, console_settings
    ):
        key_id = self._mint_rw(logged_in_client, console_settings)
        before = open(console_settings.registry_file, "rb").read()

        response = self._edit(logged_in_client, key_id, methods_mode="list", methods_list="  ")

        assert response.status_code == 400
        assert open(console_settings.registry_file, "rb").read() == before

    def test_the_change_is_in_the_history_with_before_and_after(
        self, logged_in_client, console_settings
    ):
        key_id = self._mint_rw(logged_in_client, console_settings)

        self._edit(logged_in_client, key_id, methods_mode="list",
                   methods_list="account.move.action_post")

        page = logged_in_client.get(f"/grants/{key_id}").text
        assert "métodos: hereda -&gt; account.move.action_post" in page

    def test_the_new_list_is_what_the_server_enforces(self, logged_in_client, console_settings):
        plaintext = shown_key(mint(logged_in_client, mode="readwrite"))
        key_id = grants_of(console_settings)[0]["key_id"]

        self._edit(logged_in_client, key_id, methods_mode="none")

        grant = bmya_auth.resolve_grant({"x-bmya-api-key": plaintext})
        assert grant.allowed_methods == frozenset()

    def test_methods_the_server_dropped_stay_visible_as_stale_tags(
        self, logged_in_client, console_settings
    ):
        """Saving an unrelated field must not silently drop them."""
        key_id = self._mint_rw(
            logged_in_client, console_settings,
            methods_mode="list", methods_list="account.move.action_post\npurchase.order.button_confirm",
        )
        page = logged_in_client.get(f"/grants/{key_id}").text
        assert 'value="purchase.order.button_confirm" checked' in page
        assert "el servidor no lo permite" in page

    def test_a_readonly_key_has_no_methods_to_edit(self, logged_in_client, console_settings):
        mint(logged_in_client)
        key_id = grants_of(console_settings)[0]["key_id"]
        assert 'name="methods_mode"' not in logged_in_client.get(f"/grants/{key_id}").text

        before = open(console_settings.registry_file, "rb").read()
        response = self._edit(logged_in_client, key_id, methods_mode="none")
        assert response.status_code == 400
        assert open(console_settings.registry_file, "rb").read() == before

    def test_saving_other_fields_leaves_methods_alone(self, logged_in_client, console_settings):
        """Old clients of the edit route send no methods_mode at all."""
        key_id = self._mint_rw(logged_in_client, console_settings, methods_mode="none")

        self._edit(logged_in_client, key_id, notes="solo notas")

        assert grants_of(console_settings)[0]["allowed_methods"] == []


class TestOdooLegacyFields:
    """Odoo 17/18: the grant carries the login JSON-RPC needs for the uid."""

    def test_login_and_api_are_stored_and_the_server_accepts_them(
        self, logged_in_client, console_settings
    ):
        plaintext = shown_key(
            mint(logged_in_client, odoo_login=" ana@clientex.cl ", odoo_api="jsonrpc")
        )

        grant = grants_of(console_settings)[0]
        assert grant["odoo_login"] == "ana@clientex.cl"
        assert grant["odoo_api"] == "jsonrpc"
        resolved = bmya_auth.resolve_grant({"x-bmya-api-key": plaintext})
        assert resolved.odoo_login == "ana@clientex.cl"
        assert resolved.odoo_api == "jsonrpc"

    def test_defaults_are_auto_and_no_login(self, logged_in_client, console_settings):
        mint(logged_in_client)
        grant = grants_of(console_settings)[0]
        assert grant["odoo_api"] == "auto"
        assert grant["odoo_login"] == ""

    def test_pinned_json_rpc_without_login_mints_nothing(self, logged_in_client, console_settings):
        response = mint(logged_in_client, odoo_api="jsonrpc", odoo_login="")
        assert response.status_code == 400
        assert "login de Odoo es obligatorio" in response.text
        assert not grants_of(console_settings)

    def test_an_unknown_api_mints_nothing(self, logged_in_client, console_settings):
        assert mint(logged_in_client, odoo_api="xmlrpc").status_code == 400
        assert not grants_of(console_settings)

    def test_the_login_can_be_added_to_an_existing_grant(self, logged_in_client, console_settings):
        """What APV needs: its grant predates the field."""
        mint(logged_in_client)
        key_id = grants_of(console_settings)[0]["key_id"]

        logged_in_client.post(
            f"/grants/{key_id}/edit",
            data={
                "csrf_token": csrf_from(logged_in_client),
                "label": "APV",
                "odoo_login": "ana@apv.cl",
                "odoo_api": "auto",
            },
        )

        grant = grants_of(console_settings)[0]
        assert grant["odoo_login"] == "ana@apv.cl"
        assert grant["odoo_api"] == "auto"


class TestGuards:
    def test_mutations_require_csrf(self, logged_in_client, console_settings):
        before = open(console_settings.registry_file, "rb").read()
        data = dict(GRANT_FORM)
        data["csrf_token"] = "forjado"
        response = logged_in_client.post("/grants", data=data)
        assert response.status_code == 403
        assert open(console_settings.registry_file, "rb").read() == before

    def test_mutations_require_a_session(self, console_client, console_settings):
        before = open(console_settings.registry_file, "rb").read()
        response = console_client.post("/grants", data=GRANT_FORM, follow_redirects=False)
        assert response.status_code == 302
        assert open(console_settings.registry_file, "rb").read() == before

    def test_read_only_mode_refuses_to_mint(self, console_settings):
        from tests.conftest import CONSOLE_KEY, CONSOLE_OPERATOR

        frozen = type(console_settings)(**{**console_settings.__dict__, "read_only": True})
        client = TestClient(build_console_app(frozen))
        client.post("/login", data={"email": CONSOLE_OPERATOR, "key": CONSOLE_KEY})

        response = mint(client)
        assert response.status_code == 403
        assert not grants_of(console_settings)
