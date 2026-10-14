"""
Tests for the local (stdio) demo workflow: hot-reloaded config, databases usable
by name through [DEFAULT], per-section ODOO_MODE, method wildcards and the
database-management tools.
"""

import os
import sys
from unittest.mock import Mock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import bmya_auth  # noqa: E402
import odoo_mcp_server  # noqa: E402
from odoo_mcp_server import call_tool, list_tools  # noqa: E402

URL = "http://odoo.test:8069"
DATABASES = ["odoo20e_demo_a", "odoo20e_template_cl"]

BASE_CONFIG = f"""
[DEFAULT]
ODOO_URL={URL}
ODOO_API_KEY=template-key  # comments after a value are stripped
ODOO_LOGIN=admin
ODOO_MASTER_PASSWORD=master%pwd

[template_cl]
ODOO_DATABASE=odoo20e_template_cl
ODOO_MODE=readonly
"""


def _response(*, json_data=None, status=200, text="", redirect=False):
    response = Mock()
    response.status_code = status
    response.text = text
    response.is_redirect = redirect
    response.json.return_value = json_data
    response.raise_for_status = Mock()
    return response


@pytest.fixture
def local_env(tmp_path, monkeypatch):
    """A local config file, fresh module state and no version probing."""
    config_file = tmp_path / "odoo.env"
    config_file.write_text(BASE_CONFIG)

    monkeypatch.setattr(odoo_mcp_server, "CONFIG_FILE", str(config_file))
    monkeypatch.setattr(odoo_mcp_server, "company_configs", {})
    monkeypatch.setattr(odoo_mcp_server, "company_defaults", {})
    monkeypatch.setattr(odoo_mcp_server, "_config_stamp", None)
    monkeypatch.setattr(odoo_mcp_server, "odoo_clients", {})
    monkeypatch.setattr(odoo_mcp_server, "_db_list_cache", {})
    monkeypatch.setattr(odoo_mcp_server, "READ_ONLY", False)
    monkeypatch.setattr(odoo_mcp_server, "MCP_TRANSPORT", "stdio")
    monkeypatch.setattr(
        odoo_mcp_server, "_select_api", lambda url, pinned: bmya_auth.ODOO_API_JSON2
    )
    return config_file


@pytest.fixture
def db_list(monkeypatch):
    """Stub the instance's database listing; tests may mutate the list."""
    databases = list(DATABASES)
    monkeypatch.setattr(
        odoo_mcp_server, "list_instance_databases", lambda url, fresh=False: databases
    )
    return databases


def _rewrite(path, text):
    """Rewrite a config so its (mtime_ns, size) signature surely changes."""
    stat = os.stat(path)
    path.write_text(text)
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))


class TestConfigLoading:
    def test_defaults_and_sections(self, local_env):
        configs = odoo_mcp_server.load_company_configs()

        assert list(configs) == ["template_cl"]
        template = configs["template_cl"]
        assert template["url"] == URL
        assert template["api_key"] == "template-key"
        assert template["mode"] == bmya_auth.MODE_RO
        # No interpolation: '%' in a password is kept verbatim.
        assert template["master_password"] == "master%pwd"
        assert odoo_mcp_server.company_defaults["mode"] == bmya_auth.MODE_RW

    def test_defaults_only_file_is_valid(self, local_env):
        _rewrite(local_env, f"[DEFAULT]\nODOO_URL={URL}\nODOO_API_KEY=k\n")

        assert odoo_mcp_server.load_company_configs() == {}
        assert odoo_mcp_server.company_defaults["url"] == URL

    def test_invalid_mode_is_rejected(self, local_env):
        _rewrite(local_env, BASE_CONFIG.replace("readonly", "sometimes"))

        with pytest.raises(ValueError, match="ODOO_MODE"):
            odoo_mcp_server.load_company_configs()

    def test_edit_is_picked_up_without_restart(self, local_env, db_list):
        first = odoo_mcp_server.get_odoo_client("template_cl")
        assert odoo_mcp_server.get_odoo_client("template_cl") is first

        _rewrite(local_env, BASE_CONFIG + "\n[clientex]\nODOO_DATABASE=odoo20e_demo_a\n")

        assert "clientex" in odoo_mcp_server.load_company_configs()
        # The change closed the cached clients, so the next one is new.
        assert odoo_mcp_server.get_odoo_client("template_cl") is not first

    def test_broken_edit_keeps_last_good_snapshot(self, local_env):
        odoo_mcp_server.load_company_configs()
        _rewrite(local_env, "[broken]\nODOO_URL=\n")

        assert list(odoo_mcp_server.load_company_configs()) == ["template_cl"]


class TestResolution:
    def test_section_name(self, local_env, db_list):
        assert odoo_mcp_server.resolve_company_config("template_cl")["database"] == "odoo20e_template_cl"

    def test_database_name_of_a_section_keeps_its_mode(self, local_env, db_list):
        _rewrite(local_env, BASE_CONFIG + "\n[rw_alias]\nODOO_DATABASE=odoo20e_template_cl\n")

        config = odoo_mcp_server.resolve_company_config("odoo20e_template_cl")

        assert config["mode"] == bmya_auth.MODE_RO

    def test_any_listed_database_through_default(self, local_env, db_list):
        client = odoo_mcp_server.get_odoo_client("odoo20e_demo_a")

        assert client.database == "odoo20e_demo_a"
        assert client.api_key == "template-key"
        assert client.mode == bmya_auth.MODE_RW

    def test_unknown_database_lists_what_exists(self, local_env, db_list):
        with pytest.raises(ValueError, match="odoo20e_demo_a, odoo20e_template_cl"):
            odoo_mcp_server.resolve_company_config("odoo20e_nope")

    def test_unlistable_instance_is_tried_anyway(self, local_env, monkeypatch):
        monkeypatch.setattr(
            odoo_mcp_server, "list_instance_databases", lambda url, fresh=False: None
        )

        assert odoo_mcp_server.resolve_company_config("odoo20e_x")["database"] == "odoo20e_x"

    def test_without_default_instance_names_must_be_sections(self, local_env):
        _rewrite(local_env, f"[only]\nODOO_URL={URL}\nODOO_DATABASE=d\nODOO_API_KEY=k\n")

        with pytest.raises(ValueError, match="Company 'other' not found"):
            odoo_mcp_server.resolve_company_config("other")

    def test_database_listing_is_cached(self, local_env):
        answer = _response(json_data={"result": ["b", "a"]})
        with patch.object(odoo_mcp_server.requests, "post", return_value=answer) as post:
            assert odoo_mcp_server.list_instance_databases(URL) == ["a", "b"]
            assert odoo_mcp_server.list_instance_databases(URL) == ["a", "b"]
            assert post.call_count == 1
            odoo_mcp_server.list_instance_databases(URL, fresh=True)
            assert post.call_count == 2

    def test_listing_disabled_means_unknown(self, local_env):
        answer = _response(json_data={"error": {"message": "Access Denied"}})
        with patch.object(odoo_mcp_server.requests, "post", return_value=answer):
            assert odoo_mcp_server.list_instance_databases(URL) is None


class TestSectionMode:
    @pytest.mark.asyncio
    async def test_readonly_section_blocks_writes(self, local_env, db_list):
        with patch.object(odoo_mcp_server.OdooClient, "write") as write:
            result = await call_tool("odoo_write", {
                "company": "template_cl", "model": "res.partner", "ids": [1], "values": {"name": "x"},
            })

        assert "configured read-only" in result[0].text
        write.assert_not_called()

    @pytest.mark.asyncio
    async def test_readonly_section_still_reads(self, local_env, db_list):
        with patch.object(odoo_mcp_server.OdooClient, "search_count", return_value=5):
            result = await call_tool("odoo_search_count", {
                "company": "template_cl", "model": "res.partner",
            })

        assert result[0].text == "Count: 5"

    @pytest.mark.asyncio
    async def test_dynamic_database_writes(self, local_env, db_list):
        with patch.object(odoo_mcp_server.OdooClient, "create", return_value=42) as create:
            result = await call_tool("odoo_create", {
                "company": "odoo20e_demo_a", "model": "res.partner", "values": {"name": "Cliente X"},
            })

        assert result[0].text == "Created record with ID: 42"
        create.assert_called_once()


class TestMethodWildcards:
    @pytest.fixture
    def client(self, monkeypatch):
        client = Mock()
        client.mode = bmya_auth.MODE_RW
        client.call_method = Mock(return_value=True)
        monkeypatch.setattr(odoo_mcp_server, "get_odoo_client", lambda company: client)
        monkeypatch.setattr(odoo_mcp_server, "READ_ONLY", False)
        return client

    async def _call(self, model, method):
        return await call_tool("odoo_call_method", {"company": "c", "model": model, "method": method})

    @pytest.mark.asyncio
    async def test_star_allows_everything(self, client, monkeypatch):
        monkeypatch.setattr(odoo_mcp_server, "ODOO_ALLOWED_METHODS", {"*"})

        await self._call("stock.picking", "button_validate")

        client.call_method.assert_called_once()

    @pytest.mark.asyncio
    async def test_model_wildcard(self, client, monkeypatch):
        monkeypatch.setattr(odoo_mcp_server, "ODOO_ALLOWED_METHODS", {"sale.order.*"})

        await self._call("sale.order", "action_confirm")
        refused = await self._call("account.move", "action_post")

        assert client.call_method.call_count == 1
        assert "is not allowed" in refused[0].text


class TestListDatabases:
    @pytest.mark.asyncio
    async def test_lists_sections_and_instance(self, local_env, db_list):
        result = await call_tool("odoo_list_databases", {})
        text = result[0].text

        assert "template_cl: odoo20e_template_cl" in text
        assert "(readonly)" in text
        assert f"Any other database on {URL}" in text
        assert "  odoo20e_demo_a" in text
        # One listing per instance, not one per section.
        assert text.count(f"Databases on {URL}") == 1


class TestDuplicateDatabase:
    @pytest.fixture
    def http(self, local_env):
        """Route requests.post: the database list, then the duplication."""
        databases = list(DATABASES)
        duplicate = _response(status=303, redirect=True)

        def post(url, **kwargs):
            if url.endswith("/web/database/list"):
                return _response(json_data={"result": list(databases)})
            if url.endswith("/web/database/duplicate"):
                databases.append(kwargs["data"]["new_name"])
                return duplicate
            raise AssertionError(url)

        with patch.object(odoo_mcp_server.requests, "post", side_effect=post) as mocked:
            yield mocked, duplicate

    def _duplicate_calls(self, mocked):
        return [c for c in mocked.call_args_list if c.args[0].endswith("/duplicate")]

    @pytest.mark.asyncio
    async def test_duplicates_once_without_retries(self, http):
        mocked, _ = http

        result = await call_tool("odoo_duplicate_database", {
            "source": "template_cl", "target": "odoo20e_demo_clientex",
        })

        assert "created from 'odoo20e_template_cl'" in result[0].text
        assert "company='odoo20e_demo_clientex'" in result[0].text
        (call,) = self._duplicate_calls(mocked)
        assert call.args[0] == f"{URL}/web/database/duplicate"
        assert call.kwargs["data"] == {
            "master_pwd": "master%pwd",
            "name": "odoo20e_template_cl",
            "new_name": "odoo20e_demo_clientex",
            "neutralize_database": "false",
        }
        assert call.kwargs["timeout"] == odoo_mcp_server.ODOO_DB_OPERATION_TIMEOUT
        assert call.kwargs["allow_redirects"] is False

    @pytest.mark.asyncio
    async def test_odoo_error_is_surfaced(self, http):
        mocked, duplicate = http
        duplicate.is_redirect = False
        duplicate.text = '<div class="alert alert-danger" role="alert">Access <b>Denied</b></div>'

        result = await call_tool("odoo_duplicate_database", {
            "source": "odoo20e_template_cl", "target": "odoo20e_demo_x",
        })

        assert result[0].text.startswith("Error:")
        assert "Access Denied" in result[0].text

    @pytest.mark.asyncio
    async def test_existing_target_is_refused(self, http):
        mocked, _ = http

        result = await call_tool("odoo_duplicate_database", {
            "source": "template_cl", "target": "odoo20e_demo_a",
        })

        assert "already exists" in result[0].text
        assert not self._duplicate_calls(mocked)

    @pytest.mark.asyncio
    async def test_invalid_name_is_refused(self, http):
        mocked, _ = http

        result = await call_tool("odoo_duplicate_database", {
            "source": "template_cl", "target": "bad name!",
        })

        assert "not a valid database name" in result[0].text
        assert not self._duplicate_calls(mocked)

    @pytest.mark.asyncio
    async def test_needs_master_password(self, http, local_env):
        _rewrite(local_env, BASE_CONFIG.replace("ODOO_MASTER_PASSWORD=master%pwd\n", ""))

        result = await call_tool("odoo_duplicate_database", {
            "source": "template_cl", "target": "odoo20e_demo_x",
        })

        assert "ODOO_MASTER_PASSWORD" in result[0].text

    @pytest.mark.asyncio
    async def test_blocked_by_server_readonly(self, http, monkeypatch):
        mocked, _ = http
        monkeypatch.setattr(odoo_mcp_server, "READ_ONLY", True)

        result = await call_tool("odoo_duplicate_database", {
            "source": "template_cl", "target": "odoo20e_demo_x",
        })

        assert "read-only" in result[0].text
        assert not self._duplicate_calls(mocked)


class TestLocalToolsOverHttp:
    @pytest.mark.asyncio
    async def test_hidden_and_refused_over_http(self, local_env, monkeypatch):
        monkeypatch.setattr(odoo_mcp_server, "MCP_TRANSPORT", "http")

        names = {tool.name for tool in await list_tools()}
        result = await call_tool("odoo_list_databases", {})

        assert not names & odoo_mcp_server.LOCAL_TOOLS
        assert "only available on a local" in result[0].text
