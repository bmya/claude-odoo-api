#!/usr/bin/env python3
"""
Client onboarding snippets and the small parsers the grant forms share.

Extracted from ``tools/bmya-keys.py`` so the admin console renders the *same*
block the CLI prints, from the same function, rather than reimplementing it.
That matters more than it looks: the trailing-slash normalization below was a
production bug fix, and a second copy of this text would not have it.

Standard library only, like the CLI that imports it.
"""

import json
import re
from datetime import datetime, timezone


def split_csv(value):
    """Parse a comma-separated list, preserving the None/[] distinction.

    ``None`` in, ``None`` out -- which the registry reads as "inherit the server
    list" for ``allowed_methods`` and "no restriction" for ``allowed_models``.
    An empty string yields ``[]``, which for ``allowed_methods`` means *no
    methods at all*. Callers must not collapse the two; see the schema notes in
    ``docs/bmya-api-keys.md``.
    """
    if value is None:
        return None
    return [item.strip() for item in value.split(",") if item.strip()]


def parse_expiry(value):
    """Accept YYYY-MM-DD or a full ISO-8601 timestamp; store UTC.

    Raises ``ValueError`` on a malformed date. The CLI turns that into a
    ``SystemExit`` and the console into a field error -- neither policy belongs
    here.
    """
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
        raise ValueError(f"not a valid date: {value}")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.isoformat()


def slugify(text, fallback: str) -> str:
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
