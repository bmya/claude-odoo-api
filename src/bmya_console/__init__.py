"""
BMYA admin console: a web front end for the MCP key registry.

Replaces `ssh barbol && python tools/bmya-keys.py new --write` as the way a key
is issued, so that issuing one does not require an SSH key, and so that there is
a record of who issued what.

It is a **separate service from the MCP server**, in its own container, sharing
only the registry directory. That separation is deliberate: the MCP server is
hardened to be exposed through Traefik (read-only root filesystem, config
mounted :ro, no state, no admin surface), and growing a write path and a login
form on it would spend that posture.

Entry point: :func:`bmya_console.app.build_console_app`.
"""

from .app import build_console_app

__all__ = ["build_console_app"]
