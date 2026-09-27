"""
Console authentication: who gets in, and what the session cookie looks like.

Every failure mode has to be indistinguishable from the outside -- unknown
operator, wrong key, malformed input -- so these assert on the shared message
rather than on anything that would let a caller tell them apart.
"""

import pytest
from starlette.testclient import TestClient

import bmya_auth
from bmya_console.app import build_console_app
from bmya_console.auth import PUBLIC_LOGIN_ERROR, authenticate
from tests.conftest import CONSOLE_KEY, CONSOLE_OPERATOR, csrf_from


class TestAuthenticate:
    @pytest.fixture
    def operators(self):
        return {CONSOLE_OPERATOR: bmya_auth.hash_key(CONSOLE_KEY)}

    def test_correct_credentials_resolve(self, operators):
        identity = authenticate(CONSOLE_OPERATOR, CONSOLE_KEY, operators)
        assert identity is not None and identity.email == CONSOLE_OPERATOR

    def test_email_is_case_insensitive(self, operators):
        assert authenticate("Daniel@BMYA.cl", CONSOLE_KEY, operators) is not None

    def test_wrong_key_is_rejected(self, operators):
        assert authenticate(CONSOLE_OPERATOR, "bmyacon_nope", operators) is None

    def test_unknown_operator_is_rejected(self, operators):
        assert authenticate("otro@bmya.cl", CONSOLE_KEY, operators) is None

    def test_a_key_that_belongs_to_another_operator_is_rejected(self):
        """The digest must be checked against *that* operator's entry, not
        against any entry in the table."""
        operators = {
            "a@bmya.cl": bmya_auth.hash_key("bmyacon_aaa"),
            "b@bmya.cl": bmya_auth.hash_key("bmyacon_bbb"),
        }
        assert authenticate("a@bmya.cl", "bmyacon_bbb", operators) is None

    def test_empty_inputs_are_rejected(self, operators):
        assert authenticate("", "", operators) is None
        assert authenticate(CONSOLE_OPERATOR, "", operators) is None

    def test_no_operators_means_nobody(self):
        assert authenticate(CONSOLE_OPERATOR, CONSOLE_KEY, {}) is None


class TestLoginRoutes:
    def test_unauthenticated_grants_redirects_to_login(self, console_client):
        response = console_client.get("/grants", follow_redirects=False)
        assert response.status_code == 302
        assert response.headers["location"] == "/login"

    def test_bad_login_returns_401_and_the_generic_message(self, console_client):
        response = console_client.post("/login", data={"email": CONSOLE_OPERATOR, "key": "wrong"})
        assert response.status_code == 401
        assert PUBLIC_LOGIN_ERROR in response.text

    def test_unknown_operator_gets_the_same_message(self, console_client):
        """A different message would turn the login form into an operator
        directory, one guess at a time."""
        response = console_client.post(
            "/login", data={"email": "intruso@example.com", "key": CONSOLE_KEY}
        )
        assert response.status_code == 401
        assert PUBLIC_LOGIN_ERROR in response.text

    def test_good_login_sets_a_session_and_redirects(self, console_client):
        response = console_client.post(
            "/login",
            data={"email": CONSOLE_OPERATOR, "key": CONSOLE_KEY},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/grants"

    def test_logged_in_operator_sees_the_list(self, logged_in_client):
        response = logged_in_client.get("/grants")
        assert response.status_code == 200
        assert CONSOLE_OPERATOR in response.text

    def test_logout_clears_the_session(self, logged_in_client):
        token = csrf_from(logged_in_client)
        logged_in_client.post("/logout", data={"csrf_token": token})
        response = logged_in_client.get("/grants", follow_redirects=False)
        assert response.status_code == 302

    def test_logout_requires_csrf(self, logged_in_client):
        assert logged_in_client.post("/logout", data={"csrf_token": "nope"}).status_code == 403
        # ...and the session survived the attempt.
        assert logged_in_client.get("/grants").status_code == 200


class TestSessionCookie:
    def _cookie(self, console_client):
        response = console_client.post(
            "/login",
            data={"email": CONSOLE_OPERATOR, "key": CONSOLE_KEY},
            follow_redirects=False,
        )
        return response.headers.get("set-cookie", "")

    def test_cookie_is_httponly_and_lax(self, console_client):
        cookie = self._cookie(console_client).lower()
        assert "httponly" in cookie
        # Lax, not Strict: Strict is withheld from top-level cross-site
        # navigations, which is the shape an OAuth callback arrives in.
        assert "samesite=lax" in cookie

    def test_cookie_is_not_secure_over_plain_http_by_default(self, console_client):
        """A Secure cookie is never sent over http://, so on the VLAN the
        session would be set and never come back: an infinite login loop."""
        assert "secure" not in self._cookie(console_client).lower()

    def test_cookie_is_secure_when_configured(self, console_settings):
        secure = build_console_app(
            type(console_settings)(**{**console_settings.__dict__, "cookie_secure": True})
        )
        client = TestClient(secure, base_url="https://testserver")
        response = client.post(
            "/login",
            data={"email": CONSOLE_OPERATOR, "key": CONSOLE_KEY},
            follow_redirects=False,
        )
        assert "secure" in response.headers.get("set-cookie", "").lower()


class TestFailClosed:
    """An empty operator list must lock the console, never open it.

    This is not hypothetical: docker-compose's "${VAR:-}" always injects the key
    even when empty, while os.getenv(name, default) only falls back when the key
    is *absent* -- the repo documents that trap in docker-compose.yml. If empty
    meant "no check", that single most likely misconfiguration would silently
    publish the console.
    """

    @pytest.fixture
    def openless(self, console_settings):
        return type(console_settings)(**{**console_settings.__dict__, "operators": {}})

    def test_nobody_can_log_in(self, openless):
        client = TestClient(build_console_app(openless))
        response = client.post("/login", data={"email": CONSOLE_OPERATOR, "key": CONSOLE_KEY})
        assert response.status_code == 401

    def test_readyz_reports_it(self, openless):
        client = TestClient(build_console_app(openless))
        response = client.get("/readyz")
        assert response.status_code == 503
        assert any("OPERATORS" in p for p in response.json()["problems"])

    def test_app_refuses_to_start_without_a_session_secret(self, console_settings):
        broken = type(console_settings)(**{**console_settings.__dict__, "session_secret": ""})
        with pytest.raises(RuntimeError, match="SESSION_SECRET"):
            build_console_app(broken)


class TestMalformedOperators:
    """A value that is present but not email:sha256 must not read as "empty".

    Happened in production: a random token_urlsafe pasted into
    BMYA_CONSOLE_OPERATORS, reported as "is empty", which sent the search to a
    missing variable instead of a wrong one.
    """

    RANDOM_SECRET = "TJNZivdxt8cwlEctps5ulvU5Yj3ycmJCTSvPYz24YjM"

    def _settings(self, console_settings, raw):
        from bmya_console.config import parse_operators, rejected_operator_entries

        return type(console_settings)(
            **{
                **console_settings.__dict__,
                "operators": parse_operators(raw),
                "operators_rejected": rejected_operator_entries(raw),
            }
        )

    def test_a_random_secret_is_reported_as_malformed(self, console_settings):
        settings = self._settings(console_settings, self.RANDOM_SECRET)
        body = TestClient(build_console_app(settings)).get("/readyz").json()

        (problem,) = [p for p in body["problems"] if "OPERATORS" in p]
        assert "none is email:sha256" in problem
        assert "bmya-console-operator.py" in problem
        assert "is empty" not in problem

    def test_empty_is_still_reported_as_empty(self, console_settings):
        settings = self._settings(console_settings, "")
        body = TestClient(build_console_app(settings)).get("/readyz").json()
        assert any("is empty" in p for p in body["problems"])

    def test_one_bad_entry_does_not_lock_out_the_good_one(self, console_settings):
        good = f"{CONSOLE_OPERATOR}:{bmya_auth.hash_key(CONSOLE_KEY)}"
        settings = self._settings(console_settings, f"{self.RANDOM_SECRET},{good}")

        assert settings.operators_rejected == 1
        assert settings.auth_ready
        assert TestClient(build_console_app(settings)).get("/readyz").status_code == 200


class TestSecurityHeaders:
    def test_every_response_carries_them(self, console_client):
        headers = console_client.get("/login").headers
        assert headers["referrer-policy"] == "no-referrer"
        assert headers["x-frame-options"] == "DENY"
        assert headers["x-content-type-options"] == "nosniff"


class TestReadyz:
    def test_ready_when_everything_is_in_place(self, console_client):
        response = console_client.get("/readyz")
        assert response.status_code == 200
        assert response.json()["status"] == "ready"

    def test_unready_when_the_config_dir_is_not_writable(self, console_settings, tmp_path):
        """The check that makes the :ro -> :rw mount flip verifiable: a botched
        mount fails the deploy here instead of 500-ing on the first mint."""
        import os

        client = TestClient(build_console_app(console_settings))
        directory = os.path.dirname(console_settings.registry_file)
        os.chmod(directory, 0o500)
        try:
            response = client.get("/readyz")
            assert response.status_code == 503
            assert any("writable" in p for p in response.json()["problems"])
        finally:
            os.chmod(directory, 0o700)

    def test_health_is_liveness_only(self, console_client):
        assert console_client.get("/health").json() == {"status": "ok"}
