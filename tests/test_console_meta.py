"""
The console-side metadata store.

Its defining property is that it is **best-effort**: the registry is the source
of truth, so meta being missing or corrupt must degrade the UI, never take the
grant list down with it. These tests pin that, and pin that no secret ever
lands in it.
"""

import json

import pytest

from bmya_console.meta import JsonFileMetaStore, build_meta_store, new_key_meta
from tests.conftest import CONSOLE_OPERATOR, GRANT_FORM, csrf_from


@pytest.fixture
def store(tmp_path):
    return JsonFileMetaStore(str(tmp_path / "meta.json"))


class TestJsonFileMetaStore:
    def test_missing_file_reads_as_empty(self, store):
        assert store.all() == {}
        assert store.get("aaa111") == {}

    def test_put_and_get_round_trip(self, store):
        store.put("aaa111", new_key_meta(created_by=CONSOLE_OPERATOR))
        assert store.get("aaa111")["created_by"] == CONSOLE_OPERATOR

    def test_reserved_slots_exist_from_day_one(self, store):
        """invitations and credit_accounts are present and empty so switching
        those features on is not a schema change."""
        store.put("aaa111", new_key_meta(created_by=CONSOLE_OPERATOR))
        data = json.load(open(store.path, encoding="utf-8"))
        assert data["invitations"] == []
        assert data["credit_accounts"] == []
        assert data["keys"]["aaa111"]["invitation_id"] is None
        assert data["keys"]["aaa111"]["credit_account_id"] is None

    def test_history_accumulates(self, store):
        store.put("aaa111", new_key_meta(created_by=CONSOLE_OPERATOR))
        store.append_history("aaa111", CONSOLE_OPERATOR, "revoke")
        store.append_history("aaa111", "otro@bmya.cl", "edit")
        actions = [h["action"] for h in store.get("aaa111")["history"]]
        assert actions == ["revoke", "edit"]

    def test_history_on_an_unknown_key_does_not_explode(self, store):
        """A grant minted by the CLI has no meta entry. Recording an action on
        it must create one rather than raise."""
        store.append_history("cli999", CONSOLE_OPERATOR, "revoke")
        assert store.get("cli999")["history"][0]["action"] == "revoke"

    def test_corrupt_file_degrades_instead_of_raising(self, store, caplog):
        open(store.path, "w").write("{not json")
        assert store.all() == {}
        assert "Could not read console meta" in caplog.text

    def test_wrong_shape_degrades(self, store):
        open(store.path, "w").write(json.dumps(["not", "a", "dict"]))
        assert store.all() == {}

    def test_file_is_written_with_restrictive_permissions(self, store):
        import os
        import stat

        store.put("aaa111", new_key_meta(created_by=CONSOLE_OPERATOR))
        mode = stat.S_IMODE(os.stat(store.path).st_mode)
        assert mode == 0o600

    def test_unknown_backend_is_a_loud_error(self, console_settings):
        broken = type(console_settings)(**{**console_settings.__dict__, "meta_backend": "mongo"})
        with pytest.raises(RuntimeError, match="only 'file' is implemented"):
            build_meta_store(broken)


class TestMetaNeverHoldsSecrets:
    def test_no_key_material_after_a_mint(self, logged_in_client, console_settings):
        data = dict(GRANT_FORM)
        data["csrf_token"] = csrf_from(logged_in_client)
        response = logged_in_client.post("/grants", data=data)
        plaintext = (
            response.text.split('<textarea class="secret mono" readonly onclick="this.select()">')[
                1
            ]
            .split("</textarea>")[0]
            .strip()
        )

        raw = open(console_settings.meta_file, encoding="utf-8").read()
        assert plaintext not in raw
        assert "key_sha256" not in raw


class TestListDegradesWithoutMeta:
    def test_grant_list_renders_when_meta_is_corrupt(self, logged_in_client, console_settings):
        """The registry is the source of truth. A corrupt meta file is worth an
        em dash in one column, never a 500."""
        data = dict(GRANT_FORM)
        data["csrf_token"] = csrf_from(logged_in_client)
        logged_in_client.post("/grants", data=data)

        open(console_settings.meta_file, "w").write("{corrupto")

        response = logged_in_client.get("/grants")
        assert response.status_code == 200
        assert "clientex_prod" in response.text
        assert "—" in response.text
