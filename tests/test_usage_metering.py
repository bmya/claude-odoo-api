"""
The usage journal: the server writing it, and the console reading it.

Its value is entirely about what it makes possible later -- choosing a price per
tool call from real traffic rather than a guess -- so the tests concentrate on
the properties that would make the data untrustworthy or dangerous: that a
broken sink never breaks a tool call, and that no Odoo data leaks into it.
"""

import json
import os
from datetime import datetime, timedelta, timezone

import pytest

import bmya_auth
from bmya_console import usage
from tests.conftest import KEY_RO


@pytest.fixture
def usage_dir(tmp_path, monkeypatch):
    path = tmp_path / "usage"
    path.mkdir()
    monkeypatch.setattr(bmya_auth, "BMYA_USAGE_DIR", str(path))
    return path


def _today_file(usage_dir):
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return usage_dir / f"{day}.jsonl"


def _records(usage_dir):
    return [json.loads(line) for line in _today_file(usage_dir).read_text().splitlines() if line]


class TestRecordUsage:
    def test_writes_one_line_per_call(self, usage_dir, auth_env):
        grant = bmya_auth.resolve_grant({"x-bmya-api-key": KEY_RO})
        bmya_auth.record_usage(
            grant=grant,
            tool="odoo_search_read",
            mode="readonly",
            decision="allowed",
            ok=True,
            rows=12,
            ms=34.56,
        )
        bmya_auth.record_usage(
            grant=grant,
            tool="odoo_read",
            mode="readonly",
            decision="allowed",
            ok=True,
            rows=1,
        )

        records = _records(usage_dir)
        assert len(records) == 2
        assert records[0]["key_id"] == grant.key_id
        assert records[0]["database"] == grant.database
        assert records[0]["tool"] == "odoo_search_read"
        assert records[0]["rows"] == 12
        assert records[0]["ms"] == 34.6

    def test_is_a_no_op_when_unconfigured(self, monkeypatch, tmp_path):
        """Off by default: the MCP container's filesystem is read-only, so the
        journal only exists where a volume was mounted for it."""
        monkeypatch.setattr(bmya_auth, "BMYA_USAGE_DIR", "")
        bmya_auth.record_usage(grant=None, tool="odoo_read", mode=None, decision="allowed")
        assert not list(tmp_path.iterdir())

    def test_an_unwritable_sink_never_raises(self, tmp_path, monkeypatch, caplog):
        """A metering sink that can fail a tool call is worse than no metering."""
        blocked = tmp_path / "blocked"
        blocked.mkdir()
        os.chmod(blocked, 0o500)
        monkeypatch.setattr(bmya_auth, "BMYA_USAGE_DIR", str(blocked))
        monkeypatch.setattr(bmya_auth, "_usage_warned_at", 0.0)
        try:
            bmya_auth.record_usage(grant=None, tool="odoo_read", mode=None, decision="allowed")
        finally:
            os.chmod(blocked, 0o700)

    def test_records_an_unauthenticated_call(self, usage_dir):
        bmya_auth.record_usage(
            grant=None, tool="odoo_read", mode=None, decision="unauthenticated", ok=False
        )
        record = _records(usage_dir)[0]
        assert record["key_id"] is None
        assert record["ok"] is False

    def test_stores_the_exception_class_never_its_message(self, usage_dir):
        """Exception messages carry Odoo data -- field names, record values,
        database contents. The class name is enough to spot a pattern."""
        bmya_auth.record_usage(
            grant=None,
            tool="odoo_write",
            mode="readwrite",
            decision="allowed",
            ok=False,
            err_class="ValidationError",
        )
        raw = _today_file(usage_dir).read_text()
        assert '"err_class": "ValidationError"' in raw

    def test_never_writes_a_key_or_a_hash(self, usage_dir, auth_env):
        grant = bmya_auth.resolve_grant({"x-bmya-api-key": KEY_RO})
        bmya_auth.record_usage(
            grant=grant, tool="odoo_read", mode="readonly", decision="allowed", ok=True
        )
        raw = _today_file(usage_dir).read_text()
        assert KEY_RO not in raw
        assert grant.key_sha256 not in raw


class TestCountRows:
    """rows is recorded, never billed -- but it has to be right or it is worse
    than absent."""

    def _content(self, text):
        from mcp.types import TextContent

        return [TextContent(type="text", text=text)]

    def test_counts_a_json_list(self):
        from odoo_mcp_server import _count_rows

        assert _count_rows(self._content(json.dumps([{"id": 1}, {"id": 2}]))) == 2

    def test_a_dict_result_has_no_row_count(self):
        from odoo_mcp_server import _count_rows

        assert _count_rows(self._content(json.dumps({"name": {"type": "char"}}))) is None

    def test_a_plain_message_has_no_row_count(self):
        from odoo_mcp_server import _count_rows

        assert _count_rows(self._content("Created record with ID: 42")) is None

    def test_no_content_is_none(self):
        from odoo_mcp_server import _count_rows

        assert _count_rows(None) is None
        assert _count_rows([]) is None


class TestSummarize:
    def _write(self, usage_dir, day, records):
        path = usage_dir / f"{day}.jsonl"
        path.write_text("\n".join(json.dumps(r) for r in records) + "\n")

    def test_rolls_up_per_key(self, tmp_path):
        directory = tmp_path / "usage"
        directory.mkdir()
        today = datetime.now(timezone.utc).date().isoformat()
        self._write(
            directory,
            today,
            [
                {
                    "ts": "2026-09-17T10:00:00",
                    "key_id": "aaa111",
                    "database": "db1",
                    "tool": "odoo_search_read",
                    "ok": True,
                    "rows": 10,
                    "ms": 100,
                },
                {
                    "ts": "2026-09-17T10:01:00",
                    "key_id": "aaa111",
                    "database": "db1",
                    "tool": "odoo_read",
                    "ok": True,
                    "rows": 2,
                    "ms": 50,
                },
                {
                    "ts": "2026-09-17T10:02:00",
                    "key_id": "bbb222",
                    "database": "db2",
                    "tool": "odoo_write",
                    "ok": False,
                    "rows": None,
                    "ms": 20,
                },
            ],
        )
        summary = usage.summarize(str(directory))

        assert summary["total"] == 3
        assert summary["refused"] == 1
        top = summary["by_key"][0]
        assert top["key_id"] == "aaa111"
        assert top["calls"] == 2 and top["rows"] == 12 and top["avg_ms"] == 75.0
        assert dict(summary["by_tool"])["odoo_search_read"] == 1

    def test_a_malformed_line_is_skipped(self, tmp_path):
        """The journal is appended to by another process: a truncated last line
        during a read is normal, not exceptional."""
        directory = tmp_path / "usage"
        directory.mkdir()
        today = datetime.now(timezone.utc).date().isoformat()
        (directory / f"{today}.jsonl").write_text(
            json.dumps({"key_id": "aaa111", "tool": "t", "ok": True}) + "\n{trunca"
        )
        assert usage.summarize(str(directory))["total"] == 1

    def test_missing_directory_is_empty_not_an_error(self, tmp_path):
        assert usage.summarize(str(tmp_path / "nope"))["total"] == 0

    def test_only_the_requested_window_is_read(self, tmp_path):
        directory = tmp_path / "usage"
        directory.mkdir()
        old = (datetime.now(timezone.utc).date() - timedelta(days=40)).isoformat()
        today = datetime.now(timezone.utc).date().isoformat()
        self._write(directory, old, [{"key_id": "old", "tool": "t", "ok": True}])
        self._write(directory, today, [{"key_id": "new", "tool": "t", "ok": True}])

        assert usage.summarize(str(directory), days=7)["total"] == 1
        assert usage.summarize(str(directory), days=60)["total"] == 2

    def test_prune_removes_only_old_files(self, tmp_path):
        directory = tmp_path / "usage"
        directory.mkdir()
        old = (datetime.now(timezone.utc).date() - timedelta(days=40)).isoformat()
        today = datetime.now(timezone.utc).date().isoformat()
        self._write(directory, old, [{"key_id": "old"}])
        self._write(directory, today, [{"key_id": "new"}])
        (directory / "no-es-una-fecha.jsonl").write_text("{}")

        removed = usage.prune(str(directory), keep_days=30)
        assert removed == [f"{old}.jsonl"]
        assert (directory / f"{today}.jsonl").exists()
        assert (directory / "no-es-una-fecha.jsonl").exists()


class TestUsageView:
    def test_requires_a_session(self, console_client):
        assert console_client.get("/usage", follow_redirects=False).status_code == 302

    def test_renders_without_a_journal(self, logged_in_client):
        response = logged_in_client.get("/usage")
        assert response.status_code == 200
        assert "No hay registros todavía" in response.text

    def test_shows_a_key_summary(self, logged_in_client, console_settings, tmp_path):
        directory = tmp_path / "usage"
        directory.mkdir()
        today = datetime.now(timezone.utc).date().isoformat()
        (directory / f"{today}.jsonl").write_text(
            json.dumps(
                {
                    "ts": "2026-09-17T10:00:00",
                    "key_id": "aaa111",
                    "database": "clientex_prod",
                    "tool": "odoo_search_read",
                    "ok": True,
                    "rows": 5,
                    "ms": 42,
                }
            )
            + "\n"
        )
        from starlette.testclient import TestClient

        from bmya_console.app import build_console_app
        from tests.conftest import CONSOLE_KEY, CONSOLE_OPERATOR

        settings = type(console_settings)(
            **{**console_settings.__dict__, "usage_dir": str(directory)}
        )
        client = TestClient(build_console_app(settings))
        client.post("/login", data={"email": CONSOLE_OPERATOR, "key": CONSOLE_KEY})

        response = client.get("/usage")
        assert "aaa111" in response.text
        assert "clientex_prod" in response.text
