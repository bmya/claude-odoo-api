"""
The pre-mint reachability probe.

It exists for one specific, documented, expensive failure: a stale Odoo.sh
database name (the build suffix that changes on every rebuild) makes every call
fail with 404 "No database is selected", which looks like a credentials problem
and is not. The probe separates that from "the host is down" without holding any
Odoo credential.
"""

import pytest

from bmya_console import probe
from tests.conftest import csrf_from


class FakeResponse:
    def __init__(self, status_code, text=""):
        self.status_code = status_code
        self.text = text


class TestProbeVerdicts:
    def _probe(self, monkeypatch, response=None, raises=None):
        import requests

        def fake_post(*_args, **kwargs):
            self.kwargs = kwargs
            if raises:
                raise raises
            return response

        monkeypatch.setattr(requests, "post", fake_post)
        return probe.probe_grant("https://clientex.bmya.cloud", "clientex_prod")

    def test_401_is_success(self, monkeypatch):
        """The good answer: the host replied and accepted the database, and only
        rejected our deliberately invalid key."""
        assert self._probe(monkeypatch, FakeResponse(401)).verdict == probe.OK

    def test_403_is_success(self, monkeypatch):
        assert self._probe(monkeypatch, FakeResponse(403)).verdict == probe.OK

    def test_404_no_database_is_the_wrong_database_verdict(self, monkeypatch):
        result = self._probe(monkeypatch, FakeResponse(404, '{"error": "No database is selected"}'))
        assert result.verdict == probe.WRONG_DATABASE
        assert "Odoo.sh" in result.detail

    def test_connection_failure_is_unreachable(self, monkeypatch):
        result = self._probe(monkeypatch, raises=OSError("connection refused"))
        assert result.verdict == probe.UNREACHABLE

    def test_anything_else_is_unexpected(self, monkeypatch):
        result = self._probe(monkeypatch, FakeResponse(500, "boom"))
        assert result.verdict == probe.UNEXPECTED
        assert "500" in result.detail

    def test_redirects_are_not_followed(self, monkeypatch):
        """A 302 to 169.254.169.254 would otherwise walk into the cloud
        metadata service."""
        self._probe(monkeypatch, FakeResponse(401))
        assert self.kwargs["allow_redirects"] is False

    def test_sends_no_real_credential(self, monkeypatch):
        self._probe(monkeypatch, FakeResponse(401))
        assert self.kwargs["headers"]["Authorization"] == "Bearer bmya-console-probe-invalid"


class TestProbeRoute:
    def test_rejects_a_url_the_server_would_refuse(self, logged_in_client, monkeypatch):
        """The probe is an outbound request to an operator-supplied address --
        an SSRF vector out of the console. It must never be reached with a URL
        validate_odoo_url has not already accepted."""
        import requests

        def explode(*_a, **_k):
            raise AssertionError("probe_grant must not be reached with an unvalidated URL")

        monkeypatch.setattr(requests, "post", explode)

        response = logged_in_client.post(
            "/grants/probe",
            data={
                "csrf_token": csrf_from(logged_in_client),
                "odoo_url": "http://169.254.169.254",
                "database": "x",
            },
        )
        assert response.status_code == 400
        assert response.json()["verdict"] == "invalid_url"

    def test_requires_a_session(self, console_client):
        response = console_client.post(
            "/grants/probe",
            data={"odoo_url": "https://clientex.bmya.cloud", "database": "x"},
            follow_redirects=False,
        )
        assert response.status_code == 302

    def test_requires_csrf(self, logged_in_client):
        response = logged_in_client.post(
            "/grants/probe",
            data={
                "csrf_token": "forjado",
                "odoo_url": "https://clientex.bmya.cloud",
                "database": "x",
            },
        )
        assert response.status_code == 403

    def test_reports_the_verdict(self, logged_in_client, monkeypatch):
        monkeypatch.setattr(probe, "probe_grant", lambda *a, **k: probe.ProbeResult(probe.OK, "ok"))
        response = logged_in_client.post(
            "/grants/probe",
            data={
                "csrf_token": csrf_from(logged_in_client),
                "odoo_url": "https://clientex.bmya.cloud",
                "database": "clientex_prod",
            },
        )
        assert response.json()["verdict"] == probe.OK

    def test_a_failing_probe_never_blocks_a_mint(self, logged_in_client, console_settings):
        """An instance can legitimately be down at mint time. The probe is an
        affordance, not a gate."""
        from tests.conftest import GRANT_FORM

        monkeypatched = dict(GRANT_FORM)
        monkeypatched["csrf_token"] = csrf_from(logged_in_client)
        assert logged_in_client.post("/grants", data=monkeypatched).status_code == 200
