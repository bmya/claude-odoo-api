"""
Odoo 17/18 support: version detection, the JSON-RPC transport, and the
JSON-2 client's handling of an instance that is not Odoo 19.
"""

import json
import os
import sys
from unittest.mock import create_autospec

import pytest
import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import bmya_auth  # noqa: E402
import odoo_mcp_server  # noqa: E402
from odoo_mcp_server import OdooClient, OdooLegacyClient  # noqa: E402

URL = "https://apv.bmya.cloud"


def make_response(status=200, body=None, content_type="application/json", headers=None):
    """A real requests.Response, so the code under test runs its real methods."""
    response = requests.Response()
    response.status_code = status
    response.url = URL
    response.headers["Content-Type"] = content_type
    response.headers.update(headers or {})
    if body is None:
        response._content = b""
    elif isinstance(body, (bytes, str)):
        response._content = body.encode() if isinstance(body, str) else body
    else:
        response._content = json.dumps(body).encode()
    return response


def stub_post(client, *responses):
    """Replace the client's session.post, keeping requests' real signature."""
    post = create_autospec(client.session.post, side_effect=list(responses))
    client.session.post = post
    return post


def rpc_result(result, request_id=1):
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def rpc_error(name, message):
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "error": {
            "code": 200,
            "message": "Odoo Server Error",
            "data": {"name": name, "message": message},
        },
    }


def version_info(serie, major):
    return rpc_result(
        {"server_version": f"{serie}+e", "server_version_info": [major, 0, 0, "final", 0, "e"],
         "server_serie": serie, "protocol_version": 1}
    )


class TestJson2AgainstAnOlderInstance:
    """What APV hit: /json/2 on Odoo 18 redirects to the login page."""

    def test_a_redirect_is_reported_instead_of_followed(self):
        client = OdooClient(URL, "odoo18e_apv", "key")
        post = stub_post(
            client,
            make_response(303, "", "text/html", {"Location": "/web/login?redirect=%2Fjson%2F2"}),
        )

        with pytest.raises(ValueError) as excinfo:
            client.search_count("sale.order", [])

        assert "HTTP 303" in str(excinfo.value)
        assert "/web/login" in str(excinfo.value)
        assert "Odoo 19" in str(excinfo.value)
        assert post.call_args.kwargs["allow_redirects"] is False

    def test_an_html_body_is_not_leaked_as_a_raw_decode_error(self):
        client = OdooClient(URL, "odoo18e_apv", "key")
        stub_post(client, make_response(200, "<!DOCTYPE html><html>", "text/html"))

        with pytest.raises(ValueError) as excinfo:
            client.search_count("sale.order", [])

        message = str(excinfo.value)
        assert message.startswith("Invalid JSON response from Odoo API")
        assert "text/html" in message


class TestOdooLegacyClient:
    @pytest.fixture
    def client(self):
        return OdooLegacyClient(URL, "odoo18e_apv", "user-key", "ana@apv.cl")

    def test_the_key_never_travels_as_a_bearer_token(self, client):
        assert "Authorization" not in client.session.headers
        assert "X-Odoo-Database" not in client.session.headers

    def test_a_login_is_required(self):
        with pytest.raises(ValueError):
            OdooLegacyClient(URL, "db", "key", "")

    def test_search_read_authenticates_then_calls_execute_kw(self, client):
        post = stub_post(
            client,
            make_response(body=rpc_result(8)),
            make_response(body=rpc_result([{"id": 1, "name": "S00001"}], 2)),
        )

        result = client.search_read("sale.order", [["state", "=", "sale"]], fields=["name"], limit=5)

        assert result == [{"id": 1, "name": "S00001"}]
        auth, call = (c.kwargs["json"]["params"] for c in post.call_args_list)
        assert post.call_args_list[0].args[0] == f"{URL}/jsonrpc"
        assert auth == {
            "service": "common",
            "method": "authenticate",
            "args": ["odoo18e_apv", "ana@apv.cl", "user-key", {}],
        }
        assert call == {
            "service": "object",
            "method": "execute_kw",
            "args": [
                "odoo18e_apv", 8, "user-key", "sale.order", "search_read", [],
                {"domain": [["state", "=", "sale"]], "fields": ["name"], "limit": 5},
            ],
        }

    @pytest.mark.parametrize(
        "call, expected_method, expected_args, expected_kwargs",
        [
            (lambda c: c.write("res.partner", [1, 2], {"name": "X"}),
             "write", [[1, 2]], {"vals": {"name": "X"}}),
            (lambda c: c.read("res.partner", [3], ["name"]),
             "read", [[3]], {"fields": ["name"]}),
            (lambda c: c.unlink("res.partner", [4]), "unlink", [[4]], {}),
            (lambda c: c.create("res.partner", {"name": "Y"}),
             "create", [{"name": "Y"}], {}),
            (lambda c: c.create("res.partner", [{"name": "Y"}, {"name": "Z"}]),
             "create", [[{"name": "Y"}, {"name": "Z"}]], {}),
            (lambda c: c.search_count("res.partner", []),
             "search_count", [], {"domain": []}),
            (lambda c: c.search("res.partner", [], limit=3),
             "search", [], {"domain": [], "limit": 3}),
            (lambda c: c.call_method("account.move", "action_post", ids=[7]),
             "action_post", [[7]], {}),
            (lambda c: c.call_method("res.partner", "name_create", kwargs={"name": "Z"}),
             "name_create", [], {"name": "Z"}),
        ],
    )
    def test_json2_payload_maps_onto_execute_kw(
        self, client, call, expected_method, expected_args, expected_kwargs
    ):
        post = stub_post(
            client, make_response(body=rpc_result(8)), make_response(body=rpc_result(True, 2))
        )

        call(client)

        args = post.call_args_list[1].kwargs["json"]["params"]["args"]
        assert args[4] == expected_method
        assert args[5] == expected_args
        assert args[6] == expected_kwargs

    def test_the_uid_is_resolved_once(self, client):
        post = stub_post(
            client,
            make_response(body=rpc_result(8)),
            make_response(body=rpc_result(1, 2)),
            make_response(body=rpc_result(2, 3)),
        )

        client.search_count("res.partner", [])
        client.search_count("res.partner", [])

        methods = [c.kwargs["json"]["params"]["method"] for c in post.call_args_list]
        assert methods == ["authenticate", "execute_kw", "execute_kw"]

    def test_a_key_that_does_not_belong_to_the_login_is_a_clear_error(self, client):
        stub_post(client, make_response(body=rpc_result(False)))

        with pytest.raises(ValueError) as excinfo:
            client.search_count("res.partner", [])

        assert "ana@apv.cl" in str(excinfo.value)
        assert "must belong to that user" in str(excinfo.value)

    def test_a_rejected_login_is_not_retried_until_the_ttl(self, client, monkeypatch):
        """Odoo's per-IP login cooldown is shared by every tenant of an instance."""
        clock = [1000.0]
        monkeypatch.setattr(odoo_mcp_server.time, "time", lambda: clock[0])
        monkeypatch.setattr(odoo_mcp_server, "ODOO_AUTH_FAILURE_TTL", 60)
        post = stub_post(
            client,
            make_response(body=rpc_result(False)),
            make_response(body=rpc_result(8)),
            make_response(body=rpc_result(3, 3)),
        )

        for _ in range(3):
            with pytest.raises(ValueError):
                client.search_count("res.partner", [])
        assert post.call_count == 1

        clock[0] += 61
        assert client.search_count("res.partner", []) == 3
        assert post.call_count == 3

    def test_a_json_rpc_error_surfaces_odoo_s_message(self, client):
        stub_post(
            client,
            make_response(body=rpc_result(8)),
            make_response(body=rpc_error("odoo.exceptions.AccessError", "No access to sale.order")),
        )

        with pytest.raises(ValueError) as excinfo:
            client.search_count("sale.order", [])

        assert str(excinfo.value) == "Odoo API error (AccessError): No access to sale.order"


@pytest.mark.real_version_probe
class TestDetectOdooApi:
    @pytest.fixture
    def probe(self, monkeypatch):
        def install(*responses):
            post = create_autospec(requests.post, side_effect=list(responses))
            monkeypatch.setattr(odoo_mcp_server.requests, "post", post)
            return post

        return install

    def test_odoo_18_uses_json_rpc(self, probe):
        post = probe(make_response(body=version_info("18.0", 18)))

        assert odoo_mcp_server.detect_odoo_api(URL + "/") == ("jsonrpc", "18.0")
        assert post.call_args.args[0] == f"{URL}/web/webclient/version_info"
        assert post.call_args.kwargs["allow_redirects"] is False

    def test_odoo_17_uses_json_rpc(self, probe):
        probe(make_response(body=version_info("17.0", 17)))
        assert odoo_mcp_server.detect_odoo_api(URL)[0] == "jsonrpc"

    def test_saas_18_still_uses_json_rpc(self, probe):
        probe(make_response(body=version_info("saas~18.3", 18)))
        assert odoo_mcp_server.detect_odoo_api(URL)[0] == "jsonrpc"

    def test_odoo_19_uses_json2(self, probe):
        probe(make_response(body=version_info("19.0", 19)))
        assert odoo_mcp_server.detect_odoo_api(URL) == ("json2", "19.0")

    def test_a_failed_probe_falls_back_to_json2(self, probe):
        probe(requests.exceptions.ConnectionError("down"))
        assert odoo_mcp_server.detect_odoo_api(URL) == ("json2", None)

    def test_the_answer_is_cached_until_the_ttl(self, probe, monkeypatch):
        post = probe(
            make_response(body=version_info("18.0", 18)),
            make_response(body=version_info("19.0", 19)),
        )
        clock = [1000.0]
        monkeypatch.setattr(odoo_mcp_server.time, "time", lambda: clock[0])
        monkeypatch.setattr(odoo_mcp_server, "ODOO_VERSION_CACHE_TTL", 60)

        assert odoo_mcp_server.detect_odoo_api(URL)[0] == "jsonrpc"
        clock[0] += 59
        assert odoo_mcp_server.detect_odoo_api(URL)[0] == "jsonrpc"
        assert post.call_count == 1

        # The instance was upgraded meanwhile: past the TTL it switches alone.
        clock[0] += 2
        assert odoo_mcp_server.detect_odoo_api(URL)[0] == "json2"
        assert post.call_count == 2


class TestClientSelection:
    @pytest.fixture(autouse=True)
    def clean_cache(self):
        odoo_mcp_server._http_clients.clear()
        yield
        odoo_mcp_server._http_clients.clear()

    @pytest.fixture
    def odoo_18(self, monkeypatch):
        monkeypatch.setattr(odoo_mcp_server, "detect_odoo_api", lambda url: ("jsonrpc", "18.0"))

    def test_a_pinned_api_skips_detection(self, monkeypatch):
        def fail(url):
            raise AssertionError("detection must not run for a pinned grant")

        monkeypatch.setattr(odoo_mcp_server, "detect_odoo_api", fail)
        assert odoo_mcp_server._select_api(URL, "jsonrpc") == "jsonrpc"

    def test_json_rpc_without_a_login_names_the_version_and_the_fix(self, odoo_18):
        with pytest.raises(ValueError) as excinfo:
            odoo_mcp_server._get_or_create_client(URL, "db", "key", api="jsonrpc")

        assert "Odoo 18.0" in str(excinfo.value)
        assert "odoo_login" in str(excinfo.value)

    def test_the_same_key_on_another_api_is_another_client(self):
        legacy = odoo_mcp_server._get_or_create_client(
            URL, "db", "key", api="jsonrpc", login="ana@apv.cl"
        )
        modern = odoo_mcp_server._get_or_create_client(URL, "db", "key", api="json2")

        assert isinstance(legacy, OdooLegacyClient)
        assert type(modern) is OdooClient

    def test_a_grant_on_odoo_18_gets_the_legacy_client(self, odoo_18, auth_env, monkeypatch):
        from starlette.datastructures import Headers

        monkeypatch.setattr(
            odoo_mcp_server, "_request_headers", lambda: Headers({"X-Odoo-Api-Key": "user-key"})
        )
        grant = bmya_auth._parse_grant(
            {
                "key_id": "ded582",
                "key_sha256": "0" * 64,
                "odoo_url": URL,
                "database": "odoo18e_apv",
                "mode": "readonly",
                "odoo_login": "ana@apv.cl",
            },
            0,
        )

        client = odoo_mcp_server.resolve_odoo_client({}, grant=grant)

        assert isinstance(client, OdooLegacyClient)
        assert client.login == "ana@apv.cl"
        assert client.database == "odoo18e_apv"

    def test_list_companies_reports_the_detected_api(self, odoo_18):
        grant = bmya_auth._parse_grant(
            {"key_id": "ded582", "key_sha256": "0" * 64, "odoo_url": URL,
             "database": "odoo18e_apv", "mode": "readonly", "odoo_login": "ana@apv.cl"},
            0,
        )

        text = odoo_mcp_server._describe_connection(None, grant, "readonly")

        assert "Login:     ana@apv.cl" in text
        assert "jsonrpc (detected: Odoo 18.0)" in text


class TestGrantSchema:
    BASE = {"key_id": "k1", "key_sha256": "0" * 64, "odoo_url": URL,
            "database": "db", "mode": "readonly"}

    def test_old_grants_default_to_auto_and_no_login(self):
        grant = bmya_auth._parse_grant(dict(self.BASE), 0)
        assert grant.odoo_api == "auto"
        assert grant.odoo_login == ""

    def test_an_unknown_api_is_refused(self):
        with pytest.raises(ValueError):
            bmya_auth._parse_grant({**self.BASE, "odoo_api": "xmlrpc"}, 0)

    def test_login_is_stripped(self):
        grant = bmya_auth._parse_grant({**self.BASE, "odoo_login": "  ana@apv.cl "}, 0)
        assert grant.odoo_login == "ana@apv.cl"
