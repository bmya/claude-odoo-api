"""
Unit tests for Odoo MCP Server
"""

import os
import json
import pytest
from unittest.mock import Mock, patch, MagicMock
from configparser import ConfigParser
import sys

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from odoo_mcp_server import (
    OdooClient,
    load_company_configs,
    get_odoo_client,
    list_available_companies
)

# The full tool set the server exposes.
EXPECTED_TOOLS = {
    "odoo_list_companies",
    "odoo_search_read",
    "odoo_create",
    "odoo_write",
    "odoo_unlink",
    "odoo_search",
    "odoo_read",
    "odoo_search_count",
    "odoo_list_models",
    "odoo_fields_get",
    "odoo_name_search",
    "odoo_call_method",
}


class TestOdooClient:
    """Tests for OdooClient class"""

    def test_client_initialization(self):
        """Test OdooClient initialization"""
        client = OdooClient(
            url="http://localhost:8069",
            database="test_db",
            api_key="test_key"
        )

        assert client.url == "http://localhost:8069"
        assert client.database == "test_db"
        assert client.api_key == "test_key"
        assert "Bearer test_key" in client.session.headers["Authorization"]
        assert client.session.headers["X-Odoo-Database"] == "test_db"

    def test_url_strip_trailing_slash(self):
        """Test that trailing slash is removed from URL"""
        client = OdooClient(
            url="http://localhost:8069/",
            database="test_db",
            api_key="test_key"
        )
        assert client.url == "http://localhost:8069"

    @patch('odoo_mcp_server.requests.Session')
    def test_make_request_success(self, mock_session):
        """Test successful API request"""
        mock_response = Mock()
        mock_response.json.return_value = {"result": "success"}
        mock_response.raise_for_status = Mock()

        mock_session_instance = Mock()
        mock_session_instance.post.return_value = mock_response
        mock_session_instance.headers = {}
        mock_session.return_value = mock_session_instance

        client = OdooClient(
            url="http://localhost:8069",
            database="test_db",
            api_key="test_key"
        )
        client.session = mock_session_instance

        result = client._make_request("res.partner", "search_read", {"domain": []})

        assert result == {"result": "success"}
        mock_session_instance.post.assert_called_once()

    def test_search_read_basic(self):
        """Test search_read method"""
        client = OdooClient(
            url="http://localhost:8069",
            database="test_db",
            api_key="test_key"
        )

        with patch.object(client, '_make_request') as mock_request:
            mock_request.return_value = [{"id": 1, "name": "Test"}]

            result = client.search_read(
                model="res.partner",
                domain=[["name", "=", "Test"]],
                fields=["id", "name"],
                limit=10
            )

            assert result == [{"id": 1, "name": "Test"}]
            mock_request.assert_called_once_with(
                "res.partner",
                "search_read",
                {
                    "domain": [["name", "=", "Test"]],
                    "fields": ["id", "name"],
                    "limit": 10
                }
            )

    def test_create_record(self):
        """Test create method"""
        client = OdooClient(
            url="http://localhost:8069",
            database="test_db",
            api_key="test_key"
        )

        with patch.object(client, '_make_request') as mock_request:
            mock_request.return_value = 42

            result = client.create(
                model="res.partner",
                values={"name": "Test Partner", "email": "test@example.com"}
            )

            assert result == 42
            mock_request.assert_called_once_with(
                "res.partner",
                "create",
                {"vals_list": {"name": "Test Partner", "email": "test@example.com"}}
            )

    def test_create_records_mass(self):
        """Test create method with a list of dicts (mass creation)"""
        client = OdooClient(
            url="http://localhost:8069",
            database="test_db",
            api_key="test_key"
        )

        vals_list = [{"name": "Partner A"}, {"name": "Partner B"}]
        with patch.object(client, '_make_request') as mock_request:
            mock_request.return_value = [42, 43]

            result = client.create(model="res.partner", values=vals_list)

            assert result == [42, 43]
            mock_request.assert_called_once_with(
                "res.partner",
                "create",
                {"vals_list": vals_list}
            )

    def test_write_record(self):
        """Test write method"""
        client = OdooClient(
            url="http://localhost:8069",
            database="test_db",
            api_key="test_key"
        )

        with patch.object(client, '_make_request') as mock_request:
            mock_request.return_value = True

            result = client.write(
                model="res.partner",
                ids=[1, 2],
                values={"phone": "555-1234"}
            )

            assert result is True
            mock_request.assert_called_once_with(
                "res.partner",
                "write",
                {"ids": [1, 2], "vals": {"phone": "555-1234"}}
            )

    def test_unlink_record(self):
        """Test unlink method"""
        client = OdooClient(
            url="http://localhost:8069",
            database="test_db",
            api_key="test_key"
        )

        with patch.object(client, '_make_request') as mock_request:
            mock_request.return_value = True

            result = client.unlink(model="res.partner", ids=[1, 2])

            assert result is True

    def test_search_count(self):
        """Test search_count method"""
        client = OdooClient(
            url="http://localhost:8069",
            database="test_db",
            api_key="test_key"
        )

        with patch.object(client, '_make_request') as mock_request:
            mock_request.return_value = 42

            result = client.search_count(
                model="res.partner",
                domain=[["active", "=", True]]
            )

            assert result == 42

    def test_call_method_payload(self):
        """Test call_method builds the payload from ids + kwargs"""
        client = OdooClient(
            url="http://localhost:8069",
            database="test_db",
            api_key="test_key"
        )

        with patch.object(client, '_make_request') as mock_request:
            mock_request.return_value = True

            result = client.call_method(
                model="account.move",
                method="action_post",
                ids=[1, 2],
                kwargs={"soft": True}
            )

            assert result is True
            mock_request.assert_called_once_with(
                "account.move",
                "action_post",
                {"soft": True, "ids": [1, 2]}
            )

    def test_call_method_no_ids(self):
        """Test call_method omits ids when not provided"""
        client = OdooClient(
            url="http://localhost:8069",
            database="test_db",
            api_key="test_key"
        )

        with patch.object(client, '_make_request') as mock_request:
            mock_request.return_value = None

            client.call_method(model="calendar.event", method="action_sync_timesheets")

            mock_request.assert_called_once_with(
                "calendar.event",
                "action_sync_timesheets",
                {}
            )


class TestConfigurationLoading:
    """Tests for configuration loading"""

    @pytest.fixture
    def temp_env_file(self, tmp_path):
        """Create a temporary .env file"""
        env_file = tmp_path / ".env"
        config = ConfigParser()

        config.add_section("company1")
        config.set("company1", "ODOO_URL", "http://localhost:8069")
        config.set("company1", "ODOO_DATABASE", "db1")
        config.set("company1", "ODOO_API_KEY", "key1")
        config.set("company1", "COMPANY_ID", "1")

        config.add_section("company2")
        config.set("company2", "ODOO_URL", "http://localhost:8069")
        config.set("company2", "ODOO_DATABASE", "db2")
        config.set("company2", "ODOO_API_KEY", "key2")
        config.set("company2", "COMPANY_ID", "2")

        with open(env_file, 'w') as f:
            config.write(f)

        return str(env_file)

    def test_load_company_configs(self, temp_env_file):
        """Test loading company configurations"""
        import odoo_mcp_server

        # Reset global configs
        odoo_mcp_server.company_configs = {}
        odoo_mcp_server.CONFIG_FILE = temp_env_file

        configs = load_company_configs()

        assert len(configs) == 2
        assert "company1" in configs
        assert "company2" in configs
        assert configs["company1"]["database"] == "db1"
        assert configs["company2"]["database"] == "db2"

    def test_list_available_companies(self, temp_env_file):
        """Test listing available companies"""
        import odoo_mcp_server

        odoo_mcp_server.company_configs = {}
        odoo_mcp_server.CONFIG_FILE = temp_env_file

        companies = list_available_companies()

        assert len(companies) == 2
        assert "company1" in companies
        assert "company2" in companies

    def test_get_odoo_client(self, temp_env_file):
        """Test getting Odoo client for specific company"""
        import odoo_mcp_server

        odoo_mcp_server.company_configs = {}
        odoo_mcp_server.odoo_clients = {}
        odoo_mcp_server.CONFIG_FILE = temp_env_file

        client = get_odoo_client("company1")

        assert client is not None
        assert client.database == "db1"
        assert client.api_key == "key1"

        # Test caching - should return same instance
        client2 = get_odoo_client("company1")
        assert client is client2

    def test_get_odoo_client_invalid_company(self, temp_env_file):
        """Test error handling for invalid company"""
        import odoo_mcp_server

        odoo_mcp_server.company_configs = {}
        odoo_mcp_server.odoo_clients = {}
        odoo_mcp_server.CONFIG_FILE = temp_env_file

        with pytest.raises(ValueError, match="Company 'invalid' not found"):
            get_odoo_client("invalid")

    def test_missing_config_file(self):
        """Test error when config file doesn't exist"""
        import odoo_mcp_server

        odoo_mcp_server.company_configs = {}
        odoo_mcp_server.CONFIG_FILE = "/nonexistent/file.env"

        with pytest.raises(ValueError, match="Configuration file not found"):
            load_company_configs()


class TestMCPToolIntegration:
    """Integration tests for MCP tools"""

    @pytest.fixture
    def temp_env_file(self, tmp_path):
        """Create a temporary .env file"""
        env_file = tmp_path / ".env"
        config = ConfigParser()

        config.add_section("testcompany")
        config.set("testcompany", "ODOO_URL", "http://localhost:8069")
        config.set("testcompany", "ODOO_DATABASE", "test_db")
        config.set("testcompany", "ODOO_API_KEY", "test_key")
        config.set("testcompany", "COMPANY_ID", "1")

        with open(env_file, 'w') as f:
            config.write(f)

        return str(env_file)

    @pytest.mark.asyncio
    async def test_list_tools(self):
        """Test that list_tools returns all tools"""
        from odoo_mcp_server import list_tools

        tools = await list_tools()

        # Compare the whole set rather than a bare count, so a rename fails
        # loudly instead of a counter silently drifting.
        assert {tool.name for tool in tools} == EXPECTED_TOOLS

    @pytest.mark.asyncio
    async def test_call_list_companies(self, temp_env_file):
        """Test odoo_list_companies tool"""
        import odoo_mcp_server
        from odoo_mcp_server import call_tool

        odoo_mcp_server.company_configs = {}
        odoo_mcp_server.CONFIG_FILE = temp_env_file

        result = await call_tool("odoo_list_companies", {})

        assert len(result) == 1
        assert "testcompany" in result[0].text
        assert "Total: 1" in result[0].text

    @pytest.mark.asyncio
    async def test_call_tool_without_credentials(self):
        """A tool call with neither credential headers nor a company is refused"""
        from odoo_mcp_server import call_tool

        result = await call_tool("odoo_search_read", {"model": "res.partner"})

        assert len(result) == 1
        assert "Error" in result[0].text
        assert "No Odoo credentials provided" in result[0].text
        assert "company" in result[0].text


class TestIntrospectionTools:
    """Tests for the introspection and name_search tools"""

    @pytest.fixture
    def mock_client(self):
        """Patch get_odoo_client to return a mock with a search_read spy"""
        import odoo_mcp_server
        client = Mock()
        client.search_read = Mock(return_value=[])
        with patch.object(odoo_mcp_server, "get_odoo_client", return_value=client):
            yield client

    @pytest.mark.asyncio
    async def test_list_models_with_filter(self, mock_client):
        from odoo_mcp_server import call_tool

        await call_tool("odoo_list_models", {"company": "c", "filter": "partner"})

        mock_client.search_read.assert_called_once()
        kwargs = mock_client.search_read.call_args.kwargs
        assert kwargs["model"] == "ir.model"
        assert kwargs["domain"] == [
            "|", ["model", "ilike", "partner"], ["name", "ilike", "partner"]
        ]

    @pytest.mark.asyncio
    async def test_list_models_without_filter(self, mock_client):
        from odoo_mcp_server import call_tool

        await call_tool("odoo_list_models", {"company": "c"})

        kwargs = mock_client.search_read.call_args.kwargs
        assert kwargs["model"] == "ir.model"
        assert kwargs["domain"] == []

    @pytest.mark.asyncio
    async def test_fields_get(self, mock_client):
        from odoo_mcp_server import call_tool

        await call_tool("odoo_fields_get", {"company": "c", "model": "res.partner"})

        kwargs = mock_client.search_read.call_args.kwargs
        assert kwargs["model"] == "ir.model.fields"
        assert kwargs["domain"] == [["model", "=", "res.partner"]]
        assert "ttype" in kwargs["fields"]

    @pytest.mark.asyncio
    async def test_name_search(self, mock_client):
        from odoo_mcp_server import call_tool

        await call_tool(
            "odoo_name_search",
            {"company": "c", "model": "res.partner", "name": "BMyA"}
        )

        kwargs = mock_client.search_read.call_args.kwargs
        assert kwargs["model"] == "res.partner"
        assert kwargs["domain"] == [["name", "ilike", "BMyA"]]
        assert kwargs["limit"] == 10


class TestReadOnlyKillSwitch:
    """Tests for the ODOO_MCP_READONLY kill-switch"""

    @pytest.fixture
    def readonly_client(self):
        """Enable read-only mode and patch the client; restore afterwards"""
        import odoo_mcp_server
        original = odoo_mcp_server.READ_ONLY
        odoo_mcp_server.READ_ONLY = True
        client = Mock()
        client.create = Mock(return_value=1)
        client.write = Mock(return_value=True)
        client.unlink = Mock(return_value=True)
        client.search_read = Mock(return_value=[])
        with patch.object(odoo_mcp_server, "get_odoo_client", return_value=client):
            yield client
        odoo_mcp_server.READ_ONLY = original

    @pytest.mark.asyncio
    @pytest.mark.parametrize("tool,args", [
        ("odoo_create", {"model": "res.partner", "values": {"name": "X"}}),
        ("odoo_write", {"model": "res.partner", "ids": [1], "values": {"name": "X"}}),
        ("odoo_unlink", {"model": "res.partner", "ids": [1]}),
    ])
    async def test_writes_blocked(self, readonly_client, tool, args):
        from odoo_mcp_server import call_tool

        result = await call_tool(tool, {"company": "c", **args})

        assert "read-only mode" in result[0].text
        readonly_client.create.assert_not_called()
        readonly_client.write.assert_not_called()
        readonly_client.unlink.assert_not_called()

    @pytest.mark.asyncio
    async def test_reads_allowed_in_readonly(self, readonly_client):
        from odoo_mcp_server import call_tool

        result = await call_tool(
            "odoo_search_read", {"company": "c", "model": "res.partner"}
        )

        assert "read-only mode" not in result[0].text
        readonly_client.search_read.assert_called_once()


class TestCallMethod:
    """Tests for the odoo_call_method tool (allowlist + readonly + result parsing)"""

    @pytest.fixture
    def call_method_client(self):
        """Patch get_odoo_client and set a deterministic allowlist; restore afterwards."""
        import odoo_mcp_server
        original_allowed = odoo_mcp_server.ODOO_ALLOWED_METHODS
        original_readonly = odoo_mcp_server.READ_ONLY
        odoo_mcp_server.ODOO_ALLOWED_METHODS = {"account.move.action_post"}
        odoo_mcp_server.READ_ONLY = False
        client = Mock()
        client.call_method = Mock(return_value=True)
        with patch.object(odoo_mcp_server, "get_odoo_client", return_value=client):
            yield client
        odoo_mcp_server.ODOO_ALLOWED_METHODS = original_allowed
        odoo_mcp_server.READ_ONLY = original_readonly

    @pytest.mark.asyncio
    async def test_allowed_method(self, call_method_client):
        from odoo_mcp_server import call_tool

        result = await call_tool("odoo_call_method", {
            "company": "c",
            "model": "account.move",
            "method": "action_post",
            "ids": [7],
            "kwargs": {"soft": True},
        })

        call_method_client.call_method.assert_called_once_with(
            model="account.move",
            method="action_post",
            ids=[7],
            kwargs={"soft": True},
        )
        assert "not allowed" not in result[0].text

    @pytest.mark.asyncio
    async def test_not_allowed_method(self, call_method_client):
        from odoo_mcp_server import call_tool

        result = await call_tool("odoo_call_method", {
            "company": "c",
            "model": "res.partner",
            "method": "unlink_everything",
        })

        assert "not allowed" in result[0].text
        assert "ODOO_MCP_ALLOWED_METHODS" in result[0].text
        call_method_client.call_method.assert_not_called()

    @pytest.mark.asyncio
    async def test_blocked_by_readonly(self, call_method_client):
        import odoo_mcp_server
        from odoo_mcp_server import call_tool

        odoo_mcp_server.READ_ONLY = True
        result = await call_tool("odoo_call_method", {
            "company": "c",
            "model": "account.move",
            "method": "action_post",
        })

        assert "read-only mode" in result[0].text
        call_method_client.call_method.assert_not_called()

    @pytest.mark.asyncio
    async def test_notification_message_parsing(self, call_method_client):
        from odoo_mcp_server import call_tool

        call_method_client.call_method.return_value = {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {"message": "Posted"},
        }

        result = await call_tool("odoo_call_method", {
            "company": "c",
            "model": "account.move",
            "method": "action_post",
        })

        assert result[0].text == "Posted"


class TestCallToolWithGrant:
    """Tool-level authorization under a BMYA grant (HTTP transport)."""

    @pytest.fixture
    def env_with_company(self, tmp_path):
        """A server .env whose section name must never leak to an HTTP caller."""
        import odoo_mcp_server

        env_file = tmp_path / "server.env"
        config = ConfigParser()
        config.add_section("secret_internal_company")
        config.set("secret_internal_company", "ODOO_URL", "http://internal:8069")
        config.set("secret_internal_company", "ODOO_DATABASE", "internal_db")
        config.set("secret_internal_company", "ODOO_API_KEY", "internal_key")
        with open(env_file, "w") as handle:
            config.write(handle)

        original_configs = odoo_mcp_server.company_configs
        original_file = odoo_mcp_server.CONFIG_FILE
        odoo_mcp_server.company_configs = {}
        odoo_mcp_server.CONFIG_FILE = str(env_file)
        yield env_file
        odoo_mcp_server.company_configs = original_configs
        odoo_mcp_server.CONFIG_FILE = original_file

    @pytest.fixture
    def grant_env(self, auth_env, monkeypatch):
        """Patch client creation so nothing touches the network, and record args."""
        import odoo_mcp_server

        client = Mock()
        client.create = Mock(return_value=1)
        client.write = Mock(return_value=True)
        client.unlink = Mock(return_value=True)
        client.search_read = Mock(return_value=[])
        client.call_method = Mock(return_value=True)

        calls = []

        def fake_get_or_create(url, database, api_key):
            calls.append((url, database, api_key))
            return client

        monkeypatch.setattr(odoo_mcp_server, "_get_or_create_client", fake_get_or_create)
        monkeypatch.setattr(odoo_mcp_server, "READ_ONLY", False)
        monkeypatch.setattr(
            odoo_mcp_server, "ODOO_ALLOWED_METHODS", {"account.move.action_post"}
        )
        client.creation_calls = calls
        return client

    @staticmethod
    def set_headers(monkeypatch, bmya_key, odoo_key="user-key", **extra):
        import odoo_mcp_server
        from starlette.datastructures import Headers

        raw = {"X-Bmya-Api-Key": bmya_key}
        if odoo_key is not None:
            raw["X-Odoo-Api-Key"] = odoo_key
        raw.update(extra)
        headers = Headers(raw)
        monkeypatch.setattr(odoo_mcp_server, "_request_headers", lambda: headers)
        return headers

    # --- The instance is fixed by the key (SSRF surface removed)

    @pytest.mark.asyncio
    async def test_instance_comes_from_the_grant(self, grant_env, monkeypatch):
        from odoo_mcp_server import call_tool
        from tests.conftest import KEY_RO

        self.set_headers(monkeypatch, KEY_RO)
        await call_tool("odoo_search_read", {"model": "res.partner"})

        assert grant_env.creation_calls == [
            ("https://clientex.bmya.cloud", "clientex_prod", "user-key")
        ]

    @pytest.mark.asyncio
    async def test_spoofed_url_header_cannot_redirect_the_server(
        self, grant_env, monkeypatch
    ):
        """The SSRF regression: a caller-supplied host must never be used."""
        from odoo_mcp_server import call_tool
        from tests.conftest import KEY_RO

        self.set_headers(
            monkeypatch,
            KEY_RO,
            **{"X-Odoo-Url": "https://evil.example", "X-Odoo-Database": "evil_db"},
        )
        result = await call_tool("odoo_search_read", {"model": "res.partner"})

        assert "Error" in result[0].text
        assert "evil.example" not in str(grant_env.creation_calls)
        assert grant_env.creation_calls == []
        grant_env.search_read.assert_not_called()

    @pytest.mark.asyncio
    async def test_matching_legacy_headers_are_ignored(self, grant_env, monkeypatch):
        from odoo_mcp_server import call_tool
        from tests.conftest import KEY_RO

        self.set_headers(
            monkeypatch,
            KEY_RO,
            **{
                "X-Odoo-Url": "https://clientex.bmya.cloud/",
                "X-Odoo-Database": "clientex_prod",
            },
        )
        await call_tool("odoo_search_read", {"model": "res.partner"})

        assert grant_env.creation_calls == [
            ("https://clientex.bmya.cloud", "clientex_prod", "user-key")
        ]

    @pytest.mark.asyncio
    async def test_company_argument_cannot_escape_the_grant(self, grant_env, monkeypatch):
        """A grant-bound caller passing 'company' still gets its own instance."""
        from odoo_mcp_server import call_tool
        from tests.conftest import KEY_RO

        self.set_headers(monkeypatch, KEY_RO)
        await call_tool(
            "odoo_search_read", {"model": "res.partner", "company": "secret_internal_company"}
        )

        assert grant_env.creation_calls == [
            ("https://clientex.bmya.cloud", "clientex_prod", "user-key")
        ]

    # --- Mode enforcement

    @pytest.mark.asyncio
    @pytest.mark.parametrize("tool,args", [
        ("odoo_create", {"model": "res.partner", "values": {"name": "X"}}),
        ("odoo_write", {"model": "res.partner", "ids": [1], "values": {"name": "X"}}),
        ("odoo_unlink", {"model": "res.partner", "ids": [1]}),
        ("odoo_call_method", {"model": "account.move", "method": "action_post"}),
    ])
    async def test_readonly_grant_blocks_writes(self, grant_env, monkeypatch, tool, args):
        from odoo_mcp_server import call_tool
        from tests.conftest import KEY_RO

        self.set_headers(monkeypatch, KEY_RO)
        result = await call_tool(tool, args)

        assert "Read-only connection" in result[0].text
        grant_env.create.assert_not_called()
        grant_env.write.assert_not_called()
        grant_env.unlink.assert_not_called()
        grant_env.call_method.assert_not_called()

    @pytest.mark.asyncio
    async def test_readwrite_grant_allows_writes(self, grant_env, monkeypatch):
        from odoo_mcp_server import call_tool
        from tests.conftest import KEY_RW

        self.set_headers(monkeypatch, KEY_RW)
        result = await call_tool(
            "odoo_create", {"model": "res.partner", "values": {"name": "X"}}
        )

        assert "Created record with ID: 1" in result[0].text
        grant_env.create.assert_called_once()

    @pytest.mark.asyncio
    async def test_client_can_narrow_itself_to_readonly(self, grant_env, monkeypatch):
        from odoo_mcp_server import call_tool
        from tests.conftest import KEY_RW

        self.set_headers(monkeypatch, KEY_RW, **{"X-Odoo-Mode": "readonly"})
        result = await call_tool(
            "odoo_create", {"model": "res.partner", "values": {"name": "X"}}
        )

        assert "Read-only connection" in result[0].text
        grant_env.create.assert_not_called()

    @pytest.mark.asyncio
    async def test_client_cannot_widen_a_readonly_grant(self, grant_env, monkeypatch):
        """Asking for readwrite against a readonly key changes nothing."""
        from odoo_mcp_server import call_tool
        from tests.conftest import KEY_RO

        self.set_headers(monkeypatch, KEY_RO, **{"X-Odoo-Mode": "readwrite"})
        result = await call_tool(
            "odoo_create", {"model": "res.partner", "values": {"name": "X"}}
        )

        assert "Read-only connection" in result[0].text
        grant_env.create.assert_not_called()

    @pytest.mark.asyncio
    async def test_server_readonly_overrides_a_readwrite_grant(self, grant_env, monkeypatch):
        import odoo_mcp_server
        from odoo_mcp_server import call_tool
        from tests.conftest import KEY_RW

        monkeypatch.setattr(odoo_mcp_server, "READ_ONLY", True)
        self.set_headers(monkeypatch, KEY_RW)
        result = await call_tool(
            "odoo_create", {"model": "res.partner", "values": {"name": "X"}}
        )

        assert "Read-only connection" in result[0].text
        grant_env.create.assert_not_called()

    @pytest.mark.asyncio
    async def test_invalid_mode_header_denied(self, grant_env, monkeypatch):
        from odoo_mcp_server import call_tool
        from tests.conftest import KEY_RW

        self.set_headers(monkeypatch, KEY_RW, **{"X-Odoo-Mode": "superuser"})
        result = await call_tool("odoo_search_read", {"model": "res.partner"})

        assert "Unsupported" in result[0].text

    # --- Method and model policy

    @pytest.mark.asyncio
    async def test_call_method_inside_grant_allowlist(self, grant_env, monkeypatch):
        from odoo_mcp_server import call_tool
        from tests.conftest import KEY_RW

        self.set_headers(monkeypatch, KEY_RW)
        result = await call_tool(
            "odoo_call_method", {"model": "account.move", "method": "action_post", "ids": [1]}
        )

        assert "not allowed" not in result[0].text
        grant_env.call_method.assert_called_once()

    @pytest.mark.asyncio
    async def test_call_method_outside_grant_allowlist(self, grant_env, monkeypatch):
        """The grant narrows the server list; sale.order is server-allowed but not granted."""
        import odoo_mcp_server
        from odoo_mcp_server import call_tool
        from tests.conftest import KEY_RW

        monkeypatch.setattr(
            odoo_mcp_server,
            "ODOO_ALLOWED_METHODS",
            {"account.move.action_post", "sale.order.action_confirm"},
        )
        self.set_headers(monkeypatch, KEY_RW)
        result = await call_tool(
            "odoo_call_method", {"model": "sale.order", "method": "action_confirm"}
        )

        assert "not allowed" in result[0].text
        grant_env.call_method.assert_not_called()

    @pytest.mark.asyncio
    async def test_denied_model_blocked(self, grant_env, monkeypatch):
        from odoo_mcp_server import call_tool
        from tests.conftest import KEY_RW

        self.set_headers(monkeypatch, KEY_RW)
        result = await call_tool("odoo_search_read", {"model": "res.users"})

        assert "not available" in result[0].text
        grant_env.search_read.assert_not_called()

    # --- Authentication failures at the tool layer

    @pytest.mark.asyncio
    async def test_revoked_key_gives_the_generic_message(self, grant_env, monkeypatch):
        import bmya_auth
        from odoo_mcp_server import call_tool
        from tests.conftest import KEY_REVOKED

        self.set_headers(monkeypatch, KEY_REVOKED)
        result = await call_tool("odoo_search_read", {"model": "res.partner"})

        assert result[0].text == f"Error: {bmya_auth.PUBLIC_AUTH_ERROR}"
        assert grant_env.creation_calls == []

    @pytest.mark.asyncio
    async def test_unknown_key_gives_the_same_message_as_revoked(
        self, grant_env, monkeypatch
    ):
        import bmya_auth
        from odoo_mcp_server import call_tool
        from tests.conftest import KEY_UNKNOWN

        self.set_headers(monkeypatch, KEY_UNKNOWN)
        result = await call_tool("odoo_search_read", {"model": "res.partner"})

        assert result[0].text == f"Error: {bmya_auth.PUBLIC_AUTH_ERROR}"

    @pytest.mark.asyncio
    async def test_missing_odoo_api_key_is_actionable(self, grant_env, monkeypatch):
        from odoo_mcp_server import call_tool
        from tests.conftest import KEY_RO

        self.set_headers(monkeypatch, KEY_RO, odoo_key=None)
        result = await call_tool("odoo_search_read", {"model": "res.partner"})

        assert "X-Odoo-Api-Key" in result[0].text
        assert grant_env.creation_calls == []

    @pytest.mark.asyncio
    async def test_no_fallback_to_env_when_grant_missing(self, grant_env, monkeypatch):
        """With auth on, an unresolvable grant must never fall back to the .env."""
        import bmya_auth
        import odoo_mcp_server
        from odoo_mcp_server import call_tool

        self.set_headers(monkeypatch, "garbage")
        with patch.object(odoo_mcp_server, "get_odoo_client") as get_client:
            result = await call_tool(
                "odoo_search_read", {"model": "res.partner", "company": "whatever"}
            )

        assert result[0].text == f"Error: {bmya_auth.PUBLIC_AUTH_ERROR}"
        get_client.assert_not_called()

    # --- The .env leak

    @pytest.mark.asyncio
    async def test_list_companies_under_grant_does_not_leak_env_sections(
        self, grant_env, env_with_company, monkeypatch
    ):
        from odoo_mcp_server import call_tool
        from tests.conftest import KEY_RO

        self.set_headers(monkeypatch, KEY_RO)
        result = await call_tool("odoo_list_companies", {})

        assert "clientex_prod" in result[0].text
        assert "https://clientex.bmya.cloud" in result[0].text
        assert "secret_internal_company" not in result[0].text
        assert "internal_db" not in result[0].text

    @pytest.mark.asyncio
    async def test_list_companies_requires_authentication(
        self, grant_env, env_with_company, monkeypatch
    ):
        """It used to be answered before any auth check, leaking .env sections."""
        import bmya_auth
        from odoo_mcp_server import call_tool

        self.set_headers(monkeypatch, "garbage")
        result = await call_tool("odoo_list_companies", {})

        assert result[0].text == f"Error: {bmya_auth.PUBLIC_AUTH_ERROR}"
        assert "secret_internal_company" not in result[0].text

    @pytest.mark.asyncio
    async def test_list_companies_describe_hides_secrets(self, grant_env, monkeypatch):
        from odoo_mcp_server import call_tool
        from tests.conftest import KEY_RW

        self.set_headers(monkeypatch, KEY_RW)
        result = await call_tool("odoo_list_companies", {})

        assert KEY_RW not in result[0].text
        assert "user-key" not in result[0].text

    # --- Tool listing

    @pytest.mark.asyncio
    async def test_write_tools_hidden_from_readonly_connections(
        self, grant_env, monkeypatch
    ):
        import bmya_auth
        from odoo_mcp_server import list_tools
        from tests.conftest import KEY_RO

        self.set_headers(monkeypatch, KEY_RO)
        names = {tool.name for tool in await list_tools()}

        assert names == EXPECTED_TOOLS - bmya_auth.WRITE_TOOLS
        assert len(names) == 8

    @pytest.mark.asyncio
    async def test_all_tools_listed_for_readwrite_connections(
        self, grant_env, monkeypatch
    ):
        from odoo_mcp_server import list_tools
        from tests.conftest import KEY_RW

        self.set_headers(monkeypatch, KEY_RW)
        names = {tool.name for tool in await list_tools()}

        assert names == EXPECTED_TOOLS

    @pytest.mark.asyncio
    async def test_unresolvable_grant_lists_the_narrow_set(self, grant_env, monkeypatch):
        import bmya_auth
        from odoo_mcp_server import list_tools

        self.set_headers(monkeypatch, "garbage")
        names = {tool.name for tool in await list_tools()}

        assert names == EXPECTED_TOOLS - bmya_auth.WRITE_TOOLS


class TestHttpClientCache:
    """The per-credential client cache must stay bounded on a shared server."""

    @pytest.fixture(autouse=True)
    def clean_cache(self):
        import odoo_mcp_server

        odoo_mcp_server._http_clients.clear()
        yield
        odoo_mcp_server._http_clients.clear()

    def test_same_credentials_reuse_one_client(self):
        import odoo_mcp_server

        first = odoo_mcp_server._get_or_create_client("https://a.bmya.cloud", "db", "k")
        second = odoo_mcp_server._get_or_create_client("https://a.bmya.cloud", "db", "k")
        assert first is second
        assert len(odoo_mcp_server._http_clients) == 1

    def test_different_tenants_never_share_a_client(self):
        import odoo_mcp_server

        a = odoo_mcp_server._get_or_create_client("https://a.bmya.cloud", "db", "k1")
        b = odoo_mcp_server._get_or_create_client("https://a.bmya.cloud", "db", "k2")
        assert a is not b

    def test_cache_is_bounded_and_closes_evicted_sessions(self, monkeypatch):
        import odoo_mcp_server

        monkeypatch.setattr(odoo_mcp_server, "ODOO_CLIENT_CACHE_MAX", 2)
        first = odoo_mcp_server._get_or_create_client("https://a.bmya.cloud", "db", "k1")
        first.session.close = Mock()
        odoo_mcp_server._get_or_create_client("https://a.bmya.cloud", "db", "k2")
        odoo_mcp_server._get_or_create_client("https://a.bmya.cloud", "db", "k3")

        assert len(odoo_mcp_server._http_clients) == 2
        first.session.close.assert_called_once()

    def test_recently_used_client_is_not_evicted(self, monkeypatch):
        import odoo_mcp_server

        monkeypatch.setattr(odoo_mcp_server, "ODOO_CLIENT_CACHE_MAX", 2)
        first = odoo_mcp_server._get_or_create_client("https://a.bmya.cloud", "db", "k1")
        odoo_mcp_server._get_or_create_client("https://a.bmya.cloud", "db", "k2")
        # Touching k1 makes k2 the eviction candidate.
        assert odoo_mcp_server._get_or_create_client("https://a.bmya.cloud", "db", "k1") is first
        odoo_mcp_server._get_or_create_client("https://a.bmya.cloud", "db", "k3")

        assert (
            odoo_mcp_server._get_or_create_client("https://a.bmya.cloud", "db", "k1")
            is first
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
