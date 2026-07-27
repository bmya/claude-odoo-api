#!/usr/bin/env python3
"""
Manage the BMYA API key registry (bmya-api-keys.json).

The registry stores only the sha256 of each key, so a plaintext key exists only
at the moment it is minted: it is printed once and cannot be recovered. If it is
lost, revoke it and mint a new one.

Usage:
    bmya-keys.py new --database clientex_prod --url https://clientex.bmya.cloud \
                     --mode readonly --label "ClienteX lectura" --write \
                     --server-url https://odoo-mcp.bmya.cloud/mcp/
    bmya-keys.py list
    bmya-keys.py revoke a3f19c --write
    bmya-keys.py verify --stdin
    bmya-keys.py snippet --stdin --server-url https://odoo-mcp.bmya.cloud/mcp/
    bmya-keys.py validate

Every subcommand reads the registry from --file, falling back to
$BMYA_API_KEYS_FILE and then to ./bmya-api-keys.json. Mutating subcommands print
the result and change nothing unless --write is given.

--server-url (or $BMYA_MCP_SERVER_URL) makes `new` and `snippet` also print a
ready-to-send block with both onboarding paths: a one-line `claude mcp add` for
Claude Code, and an mcp-remote-wrapped JSON block for Claude Desktop. Desktop's
claude_desktop_config.json only accepts stdio entries (command/args/env) --
verified against that app's own schema -- it has no native "http + headers"
support, so its snippet always goes through the mcp-remote bridge. The URL is
normalized to end in "/" before it reaches either snippet (see
normalize_server_url), so passing it either way is safe.

Exit codes: 0 ok, 1 validation/runtime error, 2 usage error.
"""

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

import bmya_auth  # noqa: E402

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2

DEFAULT_FILE = os.getenv("BMYA_API_KEYS_FILE") or "bmya-api-keys.json"
# Deliberately raw: every use funnels through render_client_snippets(), which
# normalizes the trailing slash, so there is exactly one place that does it.
DEFAULT_SERVER_URL = os.getenv("BMYA_MCP_SERVER_URL") or ""


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _resolve_path(args) -> str:
    return args.file or DEFAULT_FILE


def read_raw(path: str, *, allow_missing: bool = False) -> dict:
    """Read the registry as raw JSON, preserving fields this tool does not know."""
    if not os.path.exists(path):
        if allow_missing:
            return {"version": 1, "updated_at": _now_iso(), "grants": []}
        raise SystemExit(f"error: registry not found: {path}")
    with open(path, "r", encoding="utf-8") as handle:
        try:
            data = json.load(handle)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"error: {path} is not valid JSON: {exc}")
    if not isinstance(data, dict) or not isinstance(data.get("grants"), list):
        raise SystemExit(f"error: {path} must be an object with a 'grants' list")
    return data


def write_raw(path: str, data: dict) -> None:
    """Write the registry atomically, keeping a .bak of the previous content."""
    data["updated_at"] = _now_iso()
    directory = os.path.dirname(os.path.abspath(path)) or "."

    if os.path.exists(path):
        shutil.copy2(path, f"{path}.bak")

    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".bmya-keys-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    print(f"Wrote {path} (previous copy at {path}.bak)", file=sys.stderr)


def _split_csv(value):
    if value is None:
        return None
    return [item.strip() for item in value.split(",") if item.strip()]


def _parse_expiry(value):
    """Accept YYYY-MM-DD or a full ISO-8601 timestamp; store UTC."""
    if not value:
        return None
    text = value.strip()
    if len(text) == 10:
        text = f"{text}T23:59:59+00:00"
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise SystemExit(f"error: --expires is not a valid date: {value}")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.isoformat()


def _slugify(text, fallback: str) -> str:
    text = re.sub(r"[^a-z0-9]+", "-", (text or "").strip().lower()).strip("-")
    return text or fallback


def normalize_server_url(server_url: str) -> str:
    """Return the endpoint URL with its trailing slash guaranteed.

    Verified 2026-07-27: the server mounts the MCP transport with Starlette's
    Mount, which answers the bare path with a 307 to the trailing-slash form,
    and mcp-remote -- the stdio bridge Claude Desktop needs -- does not follow
    redirects. A snippet built from ".../mcp" leaves the app stuck on
    "Connecting to remote server..." and surfaces as "Could not attach to MCP
    server"; the same URL with the slash connects.

    build_http_app() now also serves the bare path directly, so the slash is no
    longer load-bearing against a current deployment. This stays because a
    snippet may be pasted against an older server, and because the operator
    passing --server-url has no reason to know any of the above.
    """
    server_url = (server_url or "").strip()
    if not server_url or server_url.endswith("/"):
        return server_url
    return server_url + "/"


def render_client_snippets(*, name: str, server_url: str, bmya_key: str) -> str:
    """A ready-to-send block covering both onboarding paths for one key.

    Claude Code supports HTTP + custom headers natively (`claude mcp add
    --transport http ... --header ...`, verified end to end: connects with no
    JSON editing and no restart). Claude Desktop's claude_desktop_config.json
    does not: its schema only accepts stdio entries (command/args/env), so its
    path goes through the mcp-remote stdio bridge instead. Both are shown
    because we cannot tell which client a given recipient uses.

    The single place server_url is normalized: both snippets are built from the
    normalized value, so neither can ship a URL the client cannot connect to.
    """
    server_url = normalize_server_url(server_url)
    code_cmd = (
        f"claude mcp add --transport http {name} {server_url} \\\n"
        f'  --header "X-Bmya-Api-Key: {bmya_key}" \\\n'
        '  --header "X-Odoo-Api-Key: TU_API_KEY_DE_ODOO"'
    )
    desktop_json = json.dumps(
        {
            "mcpServers": {
                name: {
                    "command": "npx",
                    "args": [
                        "-y",
                        "mcp-remote",
                        server_url,
                        "--transport",
                        "http-only",
                        "--header",
                        f"X-Bmya-Api-Key: {bmya_key}",
                        "--header",
                        "X-Odoo-Api-Key: TU_API_KEY_DE_ODOO",
                    ],
                }
            }
        },
        indent=2,
        ensure_ascii=False,
    )
    bar = "=" * 72
    return (
        f"{bar}\n"
        f"Acceso MCP a Odoo -- {name}\n"
        f"{bar}\n\n"
        "Antes de usar cualquiera de las dos opciones: genera tu propia API key\n"
        "en Odoo (Preferencias de usuario -> Seguridad de la cuenta -> Nueva API\n"
        "key) y reemplaza TU_API_KEY_DE_ODOO por esa key.\n\n"
        "Opcion A -- Claude Code (un solo comando, sin editar nada):\n\n"
        f"{code_cmd}\n\n"
        "Opcion B -- Claude Desktop (la app): pega este bloque dentro de\n"
        "claude_desktop_config.json (Configuracion -> Developer -> Edit Config)\n"
        "y reinicia la app por completo para que lo tome:\n\n"
        f"{desktop_json}\n\n"
        'Para verificar: pedile al asistente "listá las compañías de Odoo".\n'
        f"{bar}"
    )


# --- Subcommands


def cmd_new(args) -> int:
    try:
        url = bmya_auth.validate_odoo_url(args.url)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        if not bmya_auth.BMYA_ALLOW_INSECURE_URLS:
            print(
                "hint: for a local/self-hosted URL set BMYA_ALLOW_INSECURE_URLS=1",
                file=sys.stderr,
            )
        return EXIT_ERROR

    plaintext, key_id = bmya_auth.generate_key(args.mode)

    entry = {
        "key_id": key_id,
        "key_sha256": bmya_auth.hash_key(plaintext),
        "label": args.label or "",
        "odoo_url": url,
        "database": args.database,
        "mode": args.mode,
        "allowed_methods": _split_csv(args.methods),
        "allowed_models": _split_csv(args.allow_models),
        "denied_models": _split_csv(args.deny_models) or [],
        "revoked": False,
        "expires_at": _parse_expiry(args.expires),
        "created_at": _now_iso(),
        "revoked_at": None,
        "notes": args.notes or "",
    }

    path = _resolve_path(args)
    if args.write:
        data = read_raw(path, allow_missing=True)
        if any(g.get("key_id") == key_id for g in data["grants"]):
            print(f"error: key_id {key_id} already present, retry", file=sys.stderr)
            return EXIT_ERROR
        data["grants"].append(entry)
        write_raw(path, data)

    if args.json:
        print(json.dumps({"key": plaintext, "entry": entry}, indent=2, ensure_ascii=False))
    else:
        print("=" * 72)
        print("BMYA API key (shown once, it is NOT stored and cannot be recovered):")
        print()
        print(f"    {plaintext}")
        print()
        print(f"  key_id:   {key_id}")
        print(f"  database: {args.database}")
        print(f"  url:      {url}")
        print(f"  mode:     {args.mode}")
        if entry["expires_at"]:
            print(f"  expires:  {entry['expires_at']}")
        print("=" * 72)
        if not args.write:
            print()
            print("Registry entry (add it to the registry, or re-run with --write):")
            print(json.dumps(entry, indent=2, ensure_ascii=False))

        server_url = args.server_url or DEFAULT_SERVER_URL
        if server_url:
            slug = _slugify(args.client_name or args.label, f"odoo-{args.database}-{args.mode}")
            print()
            print(render_client_snippets(name=slug, server_url=server_url, bmya_key=plaintext))
        else:
            print(
                "\n(pass --server-url, or set BMYA_MCP_SERVER_URL, to also print "
                "ready-to-send client snippets)",
                file=sys.stderr,
            )

    print(
        "warning: store the key now; only its sha256 is kept on the server",
        file=sys.stderr,
    )
    return EXIT_OK


def cmd_add(args) -> int:
    with open(args.entry_file, "r", encoding="utf-8") as handle:
        try:
            entry = json.load(handle)
        except json.JSONDecodeError as exc:
            print(f"error: {args.entry_file} is not valid JSON: {exc}", file=sys.stderr)
            return EXIT_ERROR

    # A file produced by `new --json` carries the plaintext too; take only the entry.
    if isinstance(entry, dict) and "entry" in entry:
        entry = entry["entry"]

    path = _resolve_path(args)
    data = read_raw(path, allow_missing=True)
    key_id = entry.get("key_id")
    if any(g.get("key_id") == key_id for g in data["grants"]):
        print(f"error: key_id {key_id} is already in {path}", file=sys.stderr)
        return EXIT_ERROR
    data["grants"].append(entry)

    if args.write:
        write_raw(path, data)
    else:
        print(json.dumps(data, indent=2, ensure_ascii=False))
    return EXIT_OK


def cmd_list(args) -> int:
    path = _resolve_path(args)
    data = read_raw(path)
    grants = [g for g in data["grants"] if args.show_revoked or not g.get("revoked")]

    if args.json:
        print(json.dumps(grants, indent=2, ensure_ascii=False))
        return EXIT_OK

    if not grants:
        print(f"No grants in {path}" + ("" if args.show_revoked else " (revoked hidden)"))
        return EXIT_OK

    header = f"{'KEY_ID':<10} {'MODE':<10} {'DATABASE':<24} {'EXPIRES':<26} {'REV':<4} LABEL"
    print(header)
    print("-" * len(header))
    for grant in grants:
        print(
            f"{grant.get('key_id', '?'):<10} "
            f"{grant.get('mode', '?'):<10} "
            f"{grant.get('database', '?'):<24} "
            f"{(grant.get('expires_at') or '-'):<26} "
            f"{('yes' if grant.get('revoked') else 'no'):<4} "
            f"{grant.get('label', '')}"
        )
    print()
    print(f"{len(grants)} grant(s) in {path}")
    return EXIT_OK


def cmd_revoke(args) -> int:
    path = _resolve_path(args)
    data = read_raw(path)

    matches = [g for g in data["grants"] if g.get("key_id") == args.key_id]
    if not matches:
        print(f"error: no grant with key_id {args.key_id} in {path}", file=sys.stderr)
        return EXIT_ERROR

    for grant in matches:
        if grant.get("revoked"):
            print(f"note: {args.key_id} was already revoked", file=sys.stderr)
        grant["revoked"] = True
        grant["revoked_at"] = _now_iso()

    if args.write:
        write_raw(path, data)
        print(
            f"Revoked {args.key_id}; it stops working within BMYA_REGISTRY_TTL_SECONDS "
            "with no restart."
        )
    else:
        print(json.dumps(data, indent=2, ensure_ascii=False))
        print("(dry run: re-run with --write to apply)", file=sys.stderr)
    return EXIT_OK


def cmd_verify(args) -> int:
    if not args.stdin:
        print("error: pass --stdin and pipe the key in", file=sys.stderr)
        print(
            "hint: the key is never taken from argv so it stays out of shell history",
            file=sys.stderr,
        )
        return EXIT_USAGE

    plaintext = sys.stdin.read().strip()
    if not plaintext:
        print("error: no key on stdin", file=sys.stderr)
        return EXIT_USAGE

    path = _resolve_path(args)
    try:
        registry = bmya_auth.load_registry(path)
    except bmya_auth.RegistryError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR

    grant = registry.grants_by_hash.get(bmya_auth.hash_key(plaintext))
    if grant is None:
        print("NOT FOUND: this key is not in the registry")
        return EXIT_ERROR

    print(grant.describe())
    if grant.revoked:
        print("  STATUS:    REVOKED")
        return EXIT_ERROR
    if grant.is_expired():
        print("  STATUS:    EXPIRED")
        return EXIT_ERROR
    print("  STATUS:    valid")
    return EXIT_OK


def cmd_snippet(args) -> int:
    """Regenerate the client onboarding block for an already-minted key.

    Reads the plaintext key from stdin (never argv, same as `verify`) and looks
    it up so the command can't accidentally print snippets for a revoked or
    expired key by mistake.
    """
    if not args.stdin:
        print("error: pass --stdin and pipe the key in", file=sys.stderr)
        return EXIT_USAGE

    plaintext = sys.stdin.read().strip()
    if not plaintext:
        print("error: no key on stdin", file=sys.stderr)
        return EXIT_USAGE

    server_url = args.server_url or DEFAULT_SERVER_URL
    if not server_url:
        print("error: pass --server-url or set BMYA_MCP_SERVER_URL", file=sys.stderr)
        return EXIT_USAGE

    path = _resolve_path(args)
    try:
        registry = bmya_auth.load_registry(path)
    except bmya_auth.RegistryError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR

    grant = registry.grants_by_hash.get(bmya_auth.hash_key(plaintext))
    if grant is None:
        print("NOT FOUND: this key is not in the registry", file=sys.stderr)
        return EXIT_ERROR
    if grant.revoked:
        print(f"error: key_id {grant.key_id} is revoked; mint a new one instead", file=sys.stderr)
        return EXIT_ERROR
    if grant.is_expired():
        print(f"error: key_id {grant.key_id} is expired; mint a new one instead", file=sys.stderr)
        return EXIT_ERROR

    slug = _slugify(args.name or grant.label, f"odoo-{grant.database}-{grant.mode}")
    print(render_client_snippets(name=slug, server_url=server_url, bmya_key=plaintext))
    return EXIT_OK


def cmd_validate(args) -> int:
    path = _resolve_path(args)
    try:
        registry = bmya_auth.load_registry(path)
    except bmya_auth.RegistryError as exc:
        print(f"INVALID: {exc}", file=sys.stderr)
        return EXIT_ERROR

    raw_count = len(read_raw(path)["grants"])
    loaded = len(registry.grants_by_hash)
    if loaded != raw_count:
        # Skipped grants are logged as errors by the loader; failing here makes a
        # broken entry a deploy gate rather than a silent partial load.
        print(
            f"INVALID: {path}: {raw_count - loaded} of {raw_count} entry/entries "
            "were rejected (see the errors above)",
            file=sys.stderr,
        )
        return EXIT_ERROR

    print(f"OK: {path}")
    print(f"  grants loaded: {loaded}")
    print(f"  databases:     {', '.join(registry.databases) or '(none)'}")
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bmya-keys.py",
        description="Manage the BMYA API key registry.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"Default registry file: {DEFAULT_FILE}",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common(p):
        p.add_argument("--file", help=f"registry path (default: {DEFAULT_FILE})")

    p_new = sub.add_parser("new", help="mint a key and print it once")
    add_common(p_new)
    p_new.add_argument("--database", required=True, help="Odoo database the key is bound to")
    p_new.add_argument("--url", required=True, help="Odoo base URL the key is bound to")
    p_new.add_argument("--mode", required=True, choices=list(bmya_auth.MODES))
    p_new.add_argument("--label", help="human label, e.g. 'ClienteX lectura'")
    p_new.add_argument("--expires", help="YYYY-MM-DD or ISO-8601 timestamp")
    p_new.add_argument(
        "--methods",
        help="comma-separated model.method list for odoo_call_method "
        "(omit to inherit the server allowlist)",
    )
    p_new.add_argument("--allow-models", help="comma-separated model allowlist")
    p_new.add_argument("--deny-models", help="comma-separated model denylist")
    p_new.add_argument("--notes", help="free-form note, e.g. a contact address")
    p_new.add_argument(
        "--client-name",
        help="short name for the MCP server entry in client snippets "
        "(default: slugified --label, else 'odoo-<database>-<mode>')",
    )
    p_new.add_argument(
        "--server-url",
        help="public MCP endpoint (e.g. https://odoo-mcp.bmya.cloud/mcp/); if set "
        "(or $BMYA_MCP_SERVER_URL is), also prints ready-to-send client snippets "
        "(a missing trailing slash is added)",
    )
    p_new.add_argument("--write", action="store_true", help="append it to the registry")
    p_new.add_argument("--json", action="store_true", help="machine-readable output")
    p_new.set_defaults(func=cmd_new)

    p_add = sub.add_parser("add", help="add a previously generated entry")
    add_common(p_add)
    p_add.add_argument("--entry-file", required=True, help="JSON file holding the entry")
    p_add.add_argument("--write", action="store_true")
    p_add.set_defaults(func=cmd_add)

    p_list = sub.add_parser("list", help="list grants (never secrets or hashes)")
    add_common(p_list)
    p_list.add_argument("--show-revoked", action="store_true")
    p_list.add_argument("--json", action="store_true")
    p_list.set_defaults(func=cmd_list)

    p_revoke = sub.add_parser("revoke", help="revoke a grant by key_id")
    add_common(p_revoke)
    p_revoke.add_argument("key_id")
    p_revoke.add_argument("--write", action="store_true")
    p_revoke.set_defaults(func=cmd_revoke)

    p_verify = sub.add_parser("verify", help="resolve a plaintext key read from stdin")
    add_common(p_verify)
    p_verify.add_argument("--stdin", action="store_true", help="read the key from stdin")
    p_verify.set_defaults(func=cmd_verify)

    p_snippet = sub.add_parser(
        "snippet", help="regenerate the client onboarding block for an already-minted key"
    )
    add_common(p_snippet)
    p_snippet.add_argument("--stdin", action="store_true", help="read the key from stdin")
    p_snippet.add_argument(
        "--server-url",
        help="public MCP endpoint (a missing trailing slash is added); "
        "falls back to $BMYA_MCP_SERVER_URL",
    )
    p_snippet.add_argument("--name", help="override the MCP server name used in the snippets")
    p_snippet.set_defaults(func=cmd_snippet)

    p_validate = sub.add_parser("validate", help="parse the registry, non-zero on error")
    add_common(p_validate)
    p_validate.set_defaults(func=cmd_validate)

    return parser


def main(argv=None) -> int:
    import logging

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except SystemExit as exc:
        if isinstance(exc.code, str):
            print(exc.code, file=sys.stderr)
            return EXIT_ERROR
        raise


if __name__ == "__main__":
    sys.exit(main())
