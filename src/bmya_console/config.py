"""
Console settings, read from the environment once at app-build time.

Deliberately a dataclass built by :meth:`Settings.from_env` rather than the
module-level constants ``bmya_auth`` uses: those exist because that module is
imported by a stdlib-only CLI and must not grow machinery, and the tests
monkeypatch them one by one. The console has a build step (``build_console_app``)
where settings can be passed in, which makes every test able to construct the
exact configuration it needs without patching globals.

Naming follows the repo convention: ``BMYA_CONSOLE_*`` for anything the console
owns, and the existing ``BMYA_*`` names are reused untouched where the console
and the MCP server must agree (``BMYA_API_KEYS_FILE``, ``BMYA_ALLOWED_URL_SUFFIXES``,
``BMYA_REGISTRY_TTL_SECONDS``).
"""

import os
from dataclasses import dataclass, field


def _flag(name: str, default: str = "") -> bool:
    """Same truthiness convention as the rest of the server."""
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes")


def parse_operators(raw: str) -> dict:
    """Parse ``BMYA_CONSOLE_OPERATORS`` into ``{email: sha256hex}``.

    Format: ``email:sha256hex`` entries separated by commas. Only the digest is
    ever configured -- the same discipline the key registry uses, so a leaked
    environment cannot be used to log in.

    Malformed entries are dropped rather than raising: one bad entry must not
    lock the whole team out. An empty result is handled by the caller, and it
    means *nobody* can log in (see :meth:`Settings.auth_ready`).
    """
    operators = {}
    for item in (raw or "").split(","):
        item = item.strip()
        if not item:
            continue
        email, _, digest = item.partition(":")
        email = email.strip().lower()
        digest = digest.strip().lower()
        if not email or len(digest) != 64:
            continue
        try:
            int(digest, 16)
        except ValueError:
            continue
        operators[email] = digest
    return operators


@dataclass(frozen=True)
class Settings:
    registry_file: str = "/app/config/bmya-api-keys.json"
    meta_file: str = "/app/config/bmya-console-meta.json"
    meta_backend: str = "file"
    usage_dir: str = "/app/var/usage"

    http_host: str = "0.0.0.0"
    http_port: int = 8081

    session_secret: str = ""
    session_max_age: int = 28800
    cookie_secure: bool = False

    operators: dict = field(default_factory=dict)
    read_only: bool = False

    mcp_server_url: str = ""
    mcp_readyz_url: str = ""

    probe_enabled: bool = True
    probe_timeout: float = 5.0

    invitations_enabled: bool = False
    credits_enabled: bool = False

    registry_ttl_seconds: int = 10

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            registry_file=os.getenv("BMYA_API_KEYS_FILE", "/app/config/bmya-api-keys.json"),
            meta_file=os.getenv("BMYA_CONSOLE_META_FILE", "/app/config/bmya-console-meta.json"),
            meta_backend=os.getenv("BMYA_CONSOLE_META_BACKEND", "file").strip().lower(),
            usage_dir=os.getenv("BMYA_CONSOLE_USAGE_DIR", "/app/var/usage"),
            http_host=os.getenv("BMYA_CONSOLE_HTTP_HOST", "0.0.0.0"),
            http_port=int(os.getenv("BMYA_CONSOLE_HTTP_PORT", "8081")),
            session_secret=os.getenv("BMYA_CONSOLE_SESSION_SECRET", ""),
            session_max_age=int(os.getenv("BMYA_CONSOLE_SESSION_MAX_AGE", "28800")),
            cookie_secure=_flag("BMYA_CONSOLE_COOKIE_SECURE"),
            operators=parse_operators(os.getenv("BMYA_CONSOLE_OPERATORS", "")),
            read_only=_flag("BMYA_CONSOLE_READ_ONLY"),
            mcp_server_url=os.getenv("BMYA_MCP_SERVER_URL", ""),
            mcp_readyz_url=os.getenv("BMYA_CONSOLE_MCP_READYZ_URL", ""),
            probe_enabled=_flag("BMYA_CONSOLE_PROBE_ENABLED", "1"),
            probe_timeout=float(os.getenv("BMYA_CONSOLE_PROBE_TIMEOUT", "5")),
            invitations_enabled=_flag("BMYA_CONSOLE_INVITATIONS_ENABLED"),
            credits_enabled=_flag("BMYA_CREDITS_ENABLED"),
            registry_ttl_seconds=int(os.getenv("BMYA_REGISTRY_TTL_SECONDS", "10")),
        )

    @property
    def auth_ready(self) -> bool:
        """Whether anyone can log in at all.

        Fail-closed on purpose: an empty operator list means **nobody** gets in,
        never "everybody". docker-compose's ``"${VAR:-}"`` always injects the
        key even when empty (the repo documents this at
        deploy/docker-compose.yml:27-33), so an unset operator list is the most
        likely misconfiguration there is. If empty meant "no check", that
        mistake would silently open the console; this way it locks it and
        /readyz says so.
        """
        return bool(self.operators)

    @property
    def config_dir(self) -> str:
        return os.path.dirname(os.path.abspath(self.registry_file)) or "."
