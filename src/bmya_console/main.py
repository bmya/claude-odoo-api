#!/usr/bin/env python3
"""
uvicorn entrypoint for the admin console. Mirrors run_http() in the MCP server.
"""

import logging
import os
import sys

# Same trick as tools/bmya-keys.py: src/ on the path so bmya_auth,
# bmya_registry and bmya_snippets import as flat modules, exactly as they do in
# the MCP server.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bmya_console.app import build_console_app  # noqa: E402
from bmya_console.config import Settings  # noqa: E402

logging.basicConfig(
    level=os.getenv("BMYA_CONSOLE_LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("bmya-console")


def main() -> int:
    import uvicorn

    settings = Settings.from_env()
    app = build_console_app(settings)

    logger.info(
        "Console on %s:%s | registry=%s | operators=%d | read_only=%s | cookie_secure=%s",
        settings.http_host,
        settings.http_port,
        settings.registry_file,
        len(settings.operators),
        settings.read_only,
        settings.cookie_secure,
    )
    if not settings.cookie_secure:
        logger.warning(
            "BMYA_CONSOLE_COOKIE_SECURE=0: la cookie de sesión viaja sin TLS. "
            "Correcto mientras la consola sea sólo VLAN; ponelo en 1 detrás de Traefik."
        )

    uvicorn.run(
        app,
        host=settings.http_host,
        port=settings.http_port,
        log_level=os.getenv("BMYA_CONSOLE_LOG_LEVEL", "info").lower(),
        proxy_headers=True,
        # Same rule as the MCP server: trusting "*" lets anyone who reaches the
        # port forge X-Forwarded-For, and created_from_ip is written from it.
        forwarded_allow_ips=os.getenv("MCP_FORWARDED_ALLOW_IPS", "127.0.0.1"),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
