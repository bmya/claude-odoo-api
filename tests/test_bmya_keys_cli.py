"""
Unit tests for tools/bmya-keys.py, in particular the client onboarding
snippets (`new --server-url` / `snippet`).

The file is imported by path because its name is not a valid Python module
identifier (it has a hyphen, to read naturally as a CLI on the command line).
"""

import importlib.util
import io
import json
import os
import sys

import pytest

_TOOLS_DIR = os.path.join(os.path.dirname(__file__), "..", "tools")
_spec = importlib.util.spec_from_file_location(
    "bmya_keys_cli", os.path.join(_TOOLS_DIR, "bmya-keys.py")
)
bmya_keys_cli = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bmya_keys_cli)

import bmya_auth  # noqa: E402


def run(args_list, stdin_text=""):
    """Run one subcommand, capturing stdout/stderr and the exit code."""
    args = bmya_keys_cli.build_parser().parse_args(args_list)
    if stdin_text:
        sys.stdin = io.StringIO(stdin_text)
    try:
        return args.func(args)
    finally:
        sys.stdin = sys.__stdin__


class TestSlugify:
    def test_lowercases_and_dashes(self):
        assert (
            bmya_keys_cli._slugify("ClienteX Prod - Lectura", "fallback") == "clientex-prod-lectura"
        )

    def test_collapses_repeated_separators(self):
        assert bmya_keys_cli._slugify("a   b--c", "fallback") == "a-b-c"

    def test_blank_uses_fallback(self):
        assert bmya_keys_cli._slugify("", "fallback") == "fallback"
        assert bmya_keys_cli._slugify(None, "fallback") == "fallback"

    def test_punctuation_only_uses_fallback(self):
        assert bmya_keys_cli._slugify("!!!", "fallback") == "fallback"


class TestRenderClientSnippets:
    def _render(self):
        return bmya_keys_cli.render_client_snippets(
            name="odoo-clientex-lectura",
            server_url="https://odoo-mcp.bmya.cloud/mcp",
            bmya_key="bmya_ro_abc123_secretsecretsecretsecretsecretsecretsecret",
        )

    def test_contains_the_claude_code_one_liner(self):
        text = self._render()
        assert "claude mcp add --transport http odoo-clientex-lectura" in text
        assert "https://odoo-mcp.bmya.cloud/mcp" in text
        assert "bmya_ro_abc123_secretsecretsecretsecretsecretsecretsecret" in text

    def test_contains_a_placeholder_for_the_recipients_own_odoo_key(self):
        """BMYA never has the client's personal Odoo key; the block must not
        pretend otherwise. Once in the intro plus once per option."""
        text = self._render()
        assert text.count("TU_API_KEY_DE_ODOO") == 3

    def test_desktop_json_block_is_valid_and_stdio_shaped(self):
        """Regression guard: Claude Desktop's claude_desktop_config.json only
        accepts {command, args, env, extensionId} entries (verified against the
        app's own schema) -- it has no native "type"/"url"/"headers" support.
        If this ever emits that shape again, Desktop would silently reject it."""
        text = self._render()
        start = text.index('{\n  "mcpServers"')
        end = text.index("\n\nPara verificar")
        block = json.loads(text[start:end])

        entry = block["mcpServers"]["odoo-clientex-lectura"]
        assert set(entry.keys()) == {"command", "args"}
        assert entry["command"] == "npx"
        assert "mcp-remote" in entry["args"]
        assert "https://odoo-mcp.bmya.cloud/mcp" in entry["args"]

    def test_key_and_url_are_baked_into_the_desktop_args(self):
        text = self._render()
        assert "X-Bmya-Api-Key: bmya_ro_abc123_secretsecretsecretsecretsecretsecretsecret" in text


class TestCmdNewSnippets:
    def _args(self, tmp_path, **overrides):
        base = dict(
            file=str(tmp_path / "reg.json"),
            database="clientex_prod",
            url="https://clientex.bmya.cloud",
            mode="readonly",
            label="ClienteX lectura",
            expires=None,
            methods=None,
            allow_models=None,
            deny_models=None,
            notes=None,
            client_name=None,
            server_url=None,
            write=False,
            json=False,
        )
        base.update(overrides)
        return bmya_keys_cli.argparse.Namespace(**base)

    def test_prints_snippets_when_server_url_given(self, tmp_path, capsys, monkeypatch):
        monkeypatch.setattr(bmya_auth, "BMYA_ALLOWED_URL_SUFFIXES", ())
        args = self._args(tmp_path, server_url="https://odoo-mcp.bmya.cloud/mcp")
        assert bmya_keys_cli.cmd_new(args) == bmya_keys_cli.EXIT_OK

        out = capsys.readouterr().out
        assert "Claude Code" in out
        assert "Claude Desktop" in out
        # No --client-name given: the slug falls back to the slugified label.
        assert "claude mcp add --transport http clientex-lectura" in out

    def test_client_name_overrides_the_label_based_slug(self, tmp_path, capsys, monkeypatch):
        monkeypatch.setattr(bmya_auth, "BMYA_ALLOWED_URL_SUFFIXES", ())
        args = self._args(
            tmp_path,
            server_url="https://odoo-mcp.bmya.cloud/mcp",
            client_name="Acme Corp RO",
        )
        bmya_keys_cli.cmd_new(args)
        out = capsys.readouterr().out
        assert "acme-corp-ro" in out
        assert "clientex-lectura" not in out  # the label-based slug it overrides

    def test_hints_instead_of_printing_snippets_without_a_server_url(
        self, tmp_path, capsys, monkeypatch
    ):
        monkeypatch.setattr(bmya_auth, "BMYA_ALLOWED_URL_SUFFIXES", ())
        monkeypatch.setattr(bmya_keys_cli, "DEFAULT_SERVER_URL", "")
        args = self._args(tmp_path)
        bmya_keys_cli.cmd_new(args)

        captured = capsys.readouterr()
        assert "Claude Code" not in captured.out
        assert "--server-url" in captured.err

    def test_env_default_server_url_is_honoured(self, tmp_path, capsys, monkeypatch):
        monkeypatch.setattr(bmya_auth, "BMYA_ALLOWED_URL_SUFFIXES", ())
        monkeypatch.setattr(bmya_keys_cli, "DEFAULT_SERVER_URL", "https://odoo-mcp.bmya.cloud/mcp")
        args = self._args(tmp_path)
        bmya_keys_cli.cmd_new(args)
        assert "Claude Code" in capsys.readouterr().out


class TestCmdSnippet:
    def _mint(self, tmp_path, monkeypatch, **overrides):
        monkeypatch.setattr(bmya_auth, "BMYA_ALLOWED_URL_SUFFIXES", ())
        registry = tmp_path / "reg.json"
        args = bmya_keys_cli.argparse.Namespace(
            file=str(registry),
            database=overrides.get("database", "clientex_prod"),
            url="https://clientex.bmya.cloud",
            mode=overrides.get("mode", "readonly"),
            label=overrides.get("label", "ClienteX lectura"),
            expires=None,
            methods=None,
            allow_models=None,
            deny_models=None,
            notes=None,
            client_name=None,
            server_url=None,
            write=True,
            json=True,
        )
        run_bmya_keys_new = capture_stdout(lambda: bmya_keys_cli.cmd_new(args))
        plaintext = json.loads(run_bmya_keys_new)["key"]
        return str(registry), plaintext

    def test_regenerates_the_same_snippet(self, tmp_path, monkeypatch):
        registry, plaintext = self._mint(tmp_path, monkeypatch)
        result = run(
            ["snippet", "--stdin", "--file", registry, "--server-url", "https://x.bmya.cloud/mcp"],
            stdin_text=plaintext,
        )
        assert result == bmya_keys_cli.EXIT_OK

    def test_output_contains_the_key_and_both_options(self, tmp_path, monkeypatch, capsys):
        registry, plaintext = self._mint(tmp_path, monkeypatch)
        run(
            ["snippet", "--stdin", "--file", registry, "--server-url", "https://x.bmya.cloud/mcp"],
            stdin_text=plaintext,
        )
        out = capsys.readouterr().out
        assert plaintext in out
        assert "claude mcp add" in out
        assert "mcp-remote" in out

    def test_requires_server_url(self, tmp_path, monkeypatch):
        registry, plaintext = self._mint(tmp_path, monkeypatch)
        monkeypatch.setattr(bmya_keys_cli, "DEFAULT_SERVER_URL", "")
        result = run(["snippet", "--stdin", "--file", registry], stdin_text=plaintext)
        assert result == bmya_keys_cli.EXIT_USAGE

    def test_unknown_key_reports_not_found(self, tmp_path, monkeypatch):
        registry, _ = self._mint(tmp_path, monkeypatch)
        result = run(
            ["snippet", "--stdin", "--file", registry, "--server-url", "https://x.bmya.cloud/mcp"],
            stdin_text="bmya_ro_ffffff_" + "z" * 43,
        )
        assert result == bmya_keys_cli.EXIT_ERROR

    def test_revoked_key_is_refused(self, tmp_path, monkeypatch, capsys):
        registry, plaintext = self._mint(tmp_path, monkeypatch)
        key_id = json.loads(capture_stdout(lambda: run(["list", "--file", registry, "--json"])))[0][
            "key_id"
        ]
        run(["revoke", key_id, "--file", registry, "--write"])

        result = run(
            ["snippet", "--stdin", "--file", registry, "--server-url", "https://x.bmya.cloud/mcp"],
            stdin_text=plaintext,
        )
        assert result == bmya_keys_cli.EXIT_ERROR
        assert "revoked" in capsys.readouterr().err.lower()


class TestCmdValidate:
    """No example registry is checked into the repo (a real one never belongs in
    git, and a duplicate next to deploy/config/bmya-api-keys.json -- the one
    actually mounted -- was confusing about which path is which). These tests
    are what stand in for the CI smoke test that used to run against it."""

    def test_valid_registry_reports_ok(self, tmp_path):
        registry = tmp_path / "reg.json"
        registry.write_text(
            json.dumps(
                {
                    "version": 1,
                    "grants": [
                        {
                            "key_id": "aaaaaa",
                            "key_sha256": "0" * 64,
                            "odoo_url": "https://x.bmya.cloud",
                            "database": "db",
                            "mode": "readonly",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        assert run(["validate", "--file", str(registry)]) == bmya_keys_cli.EXIT_OK

    def test_invalid_grant_reports_error(self, tmp_path):
        registry = tmp_path / "reg.json"
        registry.write_text(
            json.dumps(
                {
                    "version": 1,
                    "grants": [
                        {
                            "key_id": "aaaaaa",
                            "key_sha256": "0" * 64,
                            "odoo_url": "https://x.bmya.cloud",
                            "database": "db",
                            "mode": "superuser",  # invalid: not readonly/readwrite
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        assert run(["validate", "--file", str(registry)]) == bmya_keys_cli.EXIT_ERROR

    def test_missing_file_reports_error(self, tmp_path):
        result = run(["validate", "--file", str(tmp_path / "missing.json")])
        assert result == bmya_keys_cli.EXIT_ERROR


def capture_stdout(fn):
    import contextlib

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        fn()
    return buf.getvalue()
