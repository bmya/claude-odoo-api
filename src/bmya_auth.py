#!/usr/bin/env python3
"""
BMYA authorization layer for the Odoo MCP server.

The HTTP transport is shared by several tenants, so every request must carry a
BMYA-issued API key (header ``X-Bmya-Api-Key``) in addition to the user's own
Odoo API key. The BMYA key is looked up in a registry and resolves to a
:class:`Grant`, which pins:

* the Odoo URL and database the caller may reach (the caller no longer sends
  them, so it cannot point the server at an arbitrary host), and
* the permissions of the connection: read-only vs read-write, which business
  methods ``odoo_call_method`` may invoke, and optionally which models are
  reachable.

The registry is a JSON file holding only the *sha256 of each key*, never the key
itself, so a leaked registry cannot be used to authenticate. It is re-read when
its mtime changes, which makes revocation a file edit with no restart.

This module is deliberately standalone: it never imports ``odoo_mcp_server``, so
the dependency runs one way only and the whole authorization layer is testable
on its own.
"""

import hashlib
import hmac
import ipaddress
import json
import logging
import os
import re
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Optional
from urllib.parse import urlparse

logger = logging.getLogger("odoo-mcp-server.bmya")


def _env_flag(name: str, default: str = "") -> bool:
    """Read a boolean env var using the same convention as the rest of the server."""
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes")


def _env_list(name: str, default: str = "") -> tuple[str, ...]:
    return tuple(item.strip() for item in os.getenv(name, default).split(",") if item.strip())


# --- Configuration (read once at import, like the rest of the server, so tests
# --- can monkeypatch the module attributes directly).

# Master switch. Setting this to 0 restores the pre-BMYA behaviour (credentials
# fully supplied by the client via X-Odoo-* headers) and exists only as a
# rollback escape hatch during the migration.
BMYA_AUTH_ENABLED = _env_flag("BMYA_AUTH_ENABLED", "1")

# Where grants come from. Only "file" is implemented; this is the seam for the
# planned second phase, which will validate keys against www.bmya.cl instead.
BMYA_KEYS_BACKEND = os.getenv("BMYA_KEYS_BACKEND", "file").strip().lower()

BMYA_API_KEYS_FILE = os.getenv("BMYA_API_KEYS_FILE", "/app/config/bmya-api-keys.json")

# How long a loaded registry is trusted before we stat the source again.
BMYA_REGISTRY_TTL = float(os.getenv("BMYA_REGISTRY_TTL_SECONDS", "10"))

# When set, a grant's odoo_url must end with one of these hostname suffixes.
BMYA_ALLOWED_URL_SUFFIXES = _env_list("BMYA_ALLOWED_URL_SUFFIXES")

# Relaxes the https-only and public-IP requirements on odoo_url. Local dev only.
BMYA_ALLOW_INSECURE_URLS = _env_flag("BMYA_ALLOW_INSECURE_URLS")


# --- Protocol constants

HEADER_BMYA_KEY = "x-bmya-api-key"
HEADER_MODE = "x-odoo-mode"
HEADER_GATEWAY_TOKEN = "x-gateway-token"

MODE_RO = "readonly"
MODE_RW = "readwrite"
MODES = (MODE_RO, MODE_RW)

# Tools that mutate Odoo data. odoo_call_method is included because a business
# method's whole point is to have side effects.
WRITE_TOOLS = frozenset({"odoo_create", "odoo_write", "odoo_unlink", "odoo_call_method"})

# Every authentication failure returns this exact string, so a caller cannot
# tell an unknown key from a revoked or expired one.
PUBLIC_AUTH_ERROR = "Unauthorized: invalid or expired BMYA API key"

_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_KEY_RE = re.compile(r"^bmya_(ro|rw)_([0-9a-f]{6})_(.+)$")

_KNOWN_GRANT_FIELDS = frozenset(
    {
        "key_id",
        "key_sha256",
        "label",
        "odoo_url",
        "database",
        "mode",
        "allowed_methods",
        "allowed_models",
        "denied_models",
        "revoked",
        "expires_at",
        "created_at",
        "revoked_at",
        "notes",
    }
)


class AuthError(Exception):
    """Authentication failed: no usable BMYA key on the request.

    ``reason`` distinguishes the cause for logging only. The message shown to
    the caller is always :data:`PUBLIC_AUTH_ERROR`.
    """

    def __init__(self, reason: str, key_id: Optional[str] = None):
        super().__init__(PUBLIC_AUTH_ERROR)
        self.reason = reason
        self.key_id = key_id


class RegistryError(Exception):
    """The registry source exists but could not be read or parsed."""


class RegistryUnavailable(Exception):
    """No usable registry has ever been loaded, so no request can be authorized."""


class ToolDenied(Exception):
    """Authenticated but not authorized. This message IS shown to the caller."""


@dataclass(frozen=True)
class Grant:
    """What a single BMYA API key is allowed to do."""

    key_id: str
    key_sha256: str
    odoo_url: str
    database: str
    mode: str
    label: str = ""
    # None means "inherit the server-level allowlist"; an empty frozenset means
    # "no methods at all". The distinction is intentional, see _parse_grant.
    allowed_methods: Optional[frozenset[str]] = None
    allowed_models: Optional[frozenset[str]] = None
    denied_models: frozenset[str] = frozenset()
    revoked: bool = False
    expires_at: Optional[datetime] = None
    notes: str = ""

    def is_expired(self, now: Optional[datetime] = None) -> bool:
        if self.expires_at is None:
            return False
        return self.expires_at <= (now or datetime.now(timezone.utc))

    def describe(
        self, *, mode: Optional[str] = None, allowed_methods: Optional[frozenset[str]] = None
    ) -> str:
        """Human-readable summary for odoo_list_companies. Contains no secrets."""
        lines = [
            "Current connection (BMYA grant):",
            f"  Key ID:    {self.key_id}",
            f"  Label:     {self.label or '(sin label)'}",
            f"  URL:       {self.odoo_url}",
            f"  Database:  {self.database}",
            f"  Mode:      {mode or self.mode}",
        ]
        methods = self.allowed_methods if allowed_methods is None else allowed_methods
        if methods is not None:
            lines.append(f"  Methods:   {', '.join(sorted(methods)) if methods else '(none)'}")
        if self.allowed_models is not None:
            lines.append(f"  Models:    {', '.join(sorted(self.allowed_models))}")
        if self.denied_models:
            lines.append(f"  Denied:    {', '.join(sorted(self.denied_models))}")
        if self.expires_at is not None:
            lines.append(f"  Expires:   {self.expires_at.isoformat()}")
        return "\n".join(lines)


@dataclass(frozen=True)
class Registry:
    """An immutable snapshot of the grant registry."""

    grants_by_hash: dict[str, Grant]
    source: str
    fingerprint: Any = None
    loaded_at: float = 0.0
    version: int = 1

    def by_key_id(self, key_id: str) -> Optional[Grant]:
        for grant in self.grants_by_hash.values():
            if grant.key_id == key_id:
                return grant
        return None

    @property
    def databases(self) -> list[str]:
        return sorted({g.database for g in self.grants_by_hash.values()})


# --- Key handling


def generate_key(mode: str) -> tuple[str, str]:
    """Mint a new key. Returns ``(plaintext, key_id)``.

    The secret is 256 bits of ``secrets`` entropy, which is what makes plain
    unsalted sha256 an appropriate digest here: there is no brute-force or
    rainbow-table surface to salt against, and indexing the registry by digest
    stays O(1).
    """
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}, got {mode!r}")
    key_id = secrets.token_hex(3)
    prefix = "ro" if mode == MODE_RO else "rw"
    return f"bmya_{prefix}_{key_id}_{secrets.token_urlsafe(32)}", key_id


def hash_key(plaintext: str) -> str:
    """sha256 of the whole key string, whitespace-stripped."""
    return hashlib.sha256(plaintext.strip().encode()).hexdigest()


def parse_key_id(plaintext: str) -> Optional[str]:
    """Extract the key id from a plaintext key, for logging only.

    Never trusted for any decision: it is caller-supplied and unauthenticated.
    """
    match = _KEY_RE.match(plaintext.strip())
    return match.group(2) if match else None


def parse_key_mode_hint(plaintext: str) -> Optional[str]:
    """The ro/rw hint embedded in a key. Advisory; the registry's mode wins."""
    match = _KEY_RE.match(plaintext.strip())
    if not match:
        return None
    return MODE_RO if match.group(1) == "ro" else MODE_RW


def check_gateway_token(provided: Optional[str], expected: Optional[str]) -> bool:
    """Constant-time gateway-token comparison. False when either side is missing."""
    if not expected:
        return True  # no token configured: this check is not in play
    if not provided:
        return False
    return hmac.compare_digest(provided.strip(), expected)


# --- Registry parsing


def _parse_datetime(value: Any, field_name: str) -> Optional[datetime]:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be an ISO-8601 string, got {type(value).__name__}")
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"{field_name} is not a valid ISO-8601 datetime: {value!r}") from exc
    # A date-only or naive value is interpreted as UTC rather than local time, so
    # expiry does not depend on the server's timezone.
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def validate_odoo_url(raw: Any) -> str:
    """Validate and normalise a grant's Odoo URL. Raises ValueError.

    Also used by tools/bmya-keys.py so a key can never be minted against a URL
    the server would refuse to load.
    """
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("odoo_url is required and must be a non-empty string")
    url = raw.strip().rstrip("/")
    parsed = urlparse(url)

    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"odoo_url must be http(s), got scheme {parsed.scheme!r}")
    if parsed.scheme == "http" and not BMYA_ALLOW_INSECURE_URLS:
        raise ValueError(
            "odoo_url must use https (set BMYA_ALLOW_INSECURE_URLS=1 for local development)"
        )
    if parsed.username or parsed.password:
        raise ValueError("odoo_url must not contain credentials")
    if parsed.query or parsed.fragment:
        raise ValueError("odoo_url must not contain a query string or fragment")
    if not parsed.hostname:
        raise ValueError("odoo_url must contain a hostname")

    host = parsed.hostname
    # Reject IP literals pointing at the server's own network. Hostnames are not
    # resolved here on purpose: a DNS check would be both slow and defeatable by
    # rebinding, and the suffix allowlist below is the real control.
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None and not BMYA_ALLOW_INSECURE_URLS:
        if ip.is_loopback or ip.is_link_local or ip.is_private or ip.is_reserved:
            raise ValueError(
                f"odoo_url must not point at a loopback, link-local or private address ({host})"
            )

    if BMYA_ALLOWED_URL_SUFFIXES:
        lowered = host.lower()
        if not any(lowered.endswith(suffix.lower()) for suffix in BMYA_ALLOWED_URL_SUFFIXES):
            raise ValueError(
                f"odoo_url host {host!r} does not match any of "
                f"BMYA_ALLOWED_URL_SUFFIXES ({', '.join(BMYA_ALLOWED_URL_SUFFIXES)})"
            )

    return url


def _as_str_set(value: Any, field_name: str) -> Optional[frozenset[str]]:
    """Parse an optional list-of-strings field, preserving the None/[] distinction."""
    if value is None:
        return None
    if not isinstance(value, list):
        raise ValueError(f"{field_name} must be a list of strings or null")
    items = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"{field_name} must contain non-empty strings")
        items.append(item.strip())
    return frozenset(items)


def _parse_grant(raw: Any, index: int) -> Grant:
    where = f"grants[{index}]"
    if not isinstance(raw, dict):
        raise ValueError(f"{where} must be an object")

    unknown = set(raw) - _KNOWN_GRANT_FIELDS
    if unknown:
        logger.warning(
            "%s has unknown field(s) %s; ignoring them", where, ", ".join(sorted(unknown))
        )

    key_id = raw.get("key_id")
    if not isinstance(key_id, str) or not key_id.strip():
        raise ValueError(f"{where}.key_id is required")
    key_id = key_id.strip()

    key_sha256 = raw.get("key_sha256")
    if not isinstance(key_sha256, str) or not _HASH_RE.match(key_sha256.strip().lower()):
        raise ValueError(f"{where}.key_sha256 must be 64 lowercase hex characters")
    key_sha256 = key_sha256.strip().lower()

    database = raw.get("database")
    if not isinstance(database, str) or not database.strip():
        raise ValueError(f"{where}.database is required")

    mode = raw.get("mode")
    # A typo must be loud rather than silently downgrading a grant.
    if mode not in MODES:
        raise ValueError(f"{where}.mode must be one of {MODES}, got {mode!r}")

    grant = Grant(
        key_id=key_id,
        key_sha256=key_sha256,
        odoo_url=validate_odoo_url(raw.get("odoo_url")),
        database=database.strip(),
        mode=mode,
        label=(raw.get("label") or "").strip(),
        allowed_methods=_as_str_set(raw.get("allowed_methods"), f"{where}.allowed_methods"),
        allowed_models=_as_str_set(raw.get("allowed_models"), f"{where}.allowed_models"),
        denied_models=_as_str_set(raw.get("denied_models"), f"{where}.denied_models")
        or frozenset(),
        revoked=bool(raw.get("revoked", False)),
        expires_at=_parse_datetime(raw.get("expires_at"), f"{where}.expires_at"),
        notes=(raw.get("notes") or "").strip(),
    )
    return grant


def _parse_registry(data: Any, *, source: str, fingerprint: Any) -> Registry:
    if not isinstance(data, dict):
        raise RegistryError(f"{source}: top level must be a JSON object")

    version = data.get("version", 1)
    if not isinstance(version, int) or version < 1:
        raise RegistryError(f"{source}: 'version' must be a positive integer")
    if version > 1:
        logger.warning(
            "%s declares version %s, newer than this server understands (1); "
            "unknown fields will be ignored",
            source,
            version,
        )

    raw_grants = data.get("grants")
    if not isinstance(raw_grants, list):
        raise RegistryError(f"{source}: 'grants' must be a list")

    grants_by_hash: dict[str, Grant] = {}
    seen_ids: dict[str, str] = {}
    for index, raw in enumerate(raw_grants):
        try:
            grant = _parse_grant(raw, index)
        except ValueError as exc:
            # One bad grant must not take the whole registry down: the other
            # tenants keep working and the error is loud in the logs.
            logger.error("%s: skipping invalid grant: %s", source, exc)
            continue

        if grant.key_sha256 in grants_by_hash:
            logger.error(
                "%s: duplicate key_sha256 for key_id %s (already used by %s); keeping the first",
                source,
                grant.key_id,
                grants_by_hash[grant.key_sha256].key_id,
            )
            continue
        if grant.key_id in seen_ids:
            logger.error("%s: duplicate key_id %s; keeping the first", source, grant.key_id)
            continue

        seen_ids[grant.key_id] = grant.key_sha256
        grants_by_hash[grant.key_sha256] = grant

    return Registry(
        grants_by_hash=grants_by_hash,
        source=source,
        fingerprint=fingerprint,
        loaded_at=time.monotonic(),
        version=version,
    )


_warned_permissions: set[tuple[str, int]] = set()


def _warn_if_world_readable(path: str) -> None:
    try:
        mode = os.stat(path).st_mode & 0o777
    except OSError:
        return
    if mode & 0o077:
        marker = (path, mode)
        if marker not in _warned_permissions:
            _warned_permissions.add(marker)
            logger.warning(
                "%s is readable beyond its owner (mode %o); consider chmod 600", path, mode
            )


def _file_fingerprint(path: str) -> Optional[tuple[int, int]]:
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


def load_registry(path: Optional[str] = None) -> Registry:
    """Read and parse the registry file unconditionally. Raises RegistryError."""
    source = path or BMYA_API_KEYS_FILE
    fingerprint = _file_fingerprint(source)
    try:
        with open(source, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError as exc:
        raise RegistryError(f"BMYA key registry not found: {source}") from exc
    except OSError as exc:
        raise RegistryError(f"Could not read BMYA key registry {source}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise RegistryError(f"BMYA key registry {source} is not valid JSON: {exc}") from exc

    _warn_if_world_readable(source)
    return _parse_registry(data, source=source, fingerprint=fingerprint)


# --- Cached access. This is the ONLY place that knows where grants come from,
# --- which is what makes the planned www.bmya.cl backend a local change.

_cache: Optional[Registry] = None
_cache_stale: bool = False
_last_checked_at: float = 0.0


def _source_fingerprint() -> Any:
    """Cheap change-detection token for the active backend, or None if unsupported."""
    if BMYA_KEYS_BACKEND == "file":
        return _file_fingerprint(BMYA_API_KEYS_FILE)
    return None


def _load_from_backend() -> Registry:
    if BMYA_KEYS_BACKEND == "file":
        return load_registry()
    raise RegistryError(
        f"Unsupported BMYA_KEYS_BACKEND {BMYA_KEYS_BACKEND!r} (only 'file' is implemented)"
    )


def invalidate_registry_cache() -> None:
    """Force the next get_registry() to reload. Used by tests and by SIGHUP."""
    global _cache, _cache_stale, _last_checked_at
    _cache = None
    _cache_stale = False
    _last_checked_at = 0.0


def get_registry() -> Registry:
    """Return the current registry, reloading it when the source has changed.

    Within :data:`BMYA_REGISTRY_TTL` seconds the cached snapshot is returned
    untouched. After that the source is fingerprinted and only re-parsed when it
    actually changed, so revocation takes effect within the TTL without a
    restart. If a reload fails the last good snapshot is kept — availability
    beats freshness here, and the failure is logged and surfaced by
    :func:`registry_status`.
    """
    global _cache, _cache_stale, _last_checked_at

    now = time.monotonic()
    if _cache is not None and (now - _last_checked_at) < BMYA_REGISTRY_TTL:
        return _cache

    _last_checked_at = now
    fingerprint = _source_fingerprint()
    if _cache is not None and fingerprint is not None and fingerprint == _cache.fingerprint:
        _cache_stale = False
        return _cache

    try:
        registry = _load_from_backend()
    except RegistryError as exc:
        if _cache is not None:
            _cache_stale = True
            logger.error("Could not reload BMYA key registry, keeping last good copy: %s", exc)
            return _cache
        logger.error("BMYA key registry unavailable: %s", exc)
        raise RegistryUnavailable(str(exc)) from exc

    _cache = registry
    _cache_stale = False
    logger.info(
        "Loaded BMYA key registry from %s: %d grant(s) over %d database(s)",
        registry.source,
        len(registry.grants_by_hash),
        len(registry.databases),
    )
    return registry


def registry_status() -> dict:
    """Readiness summary for /readyz. Contains no secrets and no labels."""
    if not BMYA_AUTH_ENABLED:
        return {"enabled": False, "loaded": True, "grants": 0, "stale": False}
    try:
        registry = get_registry()
    except RegistryUnavailable:
        return {"enabled": True, "loaded": False, "grants": 0, "stale": True}
    return {
        "enabled": True,
        "loaded": True,
        "grants": len(registry.grants_by_hash),
        "stale": _cache_stale,
    }


# --- Request-time resolution


def resolve_grant(headers: Mapping[str, str]) -> Grant:
    """Resolve the Grant for a request. Raises AuthError on any failure.

    Every failure mode raises with the same public message; only ``reason``
    differs, and that is for the server log.
    """
    provided = (headers.get(HEADER_BMYA_KEY) or "").strip()
    if not provided:
        raise AuthError("missing_key")

    key_id_hint = parse_key_id(provided)
    if key_id_hint is None:
        # Unrecognised shape. Still hash and look it up so the work done is the
        # same either way, then fail.
        raise AuthError("malformed_key")

    digest = hash_key(provided)
    registry = get_registry()
    grant = registry.grants_by_hash.get(digest)
    # The dict lookup is keyed by a digest, so it cannot be inverted; the
    # compare_digest below is what makes the accept/reject decision itself
    # timing-safe.
    if grant is None or not hmac.compare_digest(grant.key_sha256, digest):
        raise AuthError("unknown_key", key_id=key_id_hint)
    if grant.revoked:
        raise AuthError("revoked", key_id=grant.key_id)
    if grant.is_expired():
        raise AuthError("expired", key_id=grant.key_id)

    hint = parse_key_mode_hint(provided)
    if hint is not None and hint != grant.mode:
        logger.warning(
            "Key %s has a '%s' prefix but the registry grants '%s'; the registry wins",
            grant.key_id,
            hint,
            grant.mode,
        )
    return grant


def effective_mode(grant: Grant, *, server_readonly: bool, requested: Optional[str] = None) -> str:
    """The most restrictive of the server ceiling, the grant, and the request.

    A client can only ever narrow its own access: asking for ``readwrite``
    against a ``readonly`` grant yields ``readonly``. Widening is structurally
    impossible, not merely rejected.
    """
    if requested is not None:
        requested = requested.strip().lower() or None
    if requested is not None and requested not in MODES:
        raise ToolDenied(
            f"Unsupported {HEADER_MODE} value {requested!r}; expected one of {', '.join(MODES)}"
        )

    if server_readonly or grant.mode == MODE_RO or requested == MODE_RO:
        return MODE_RO
    return MODE_RW


def effective_allowed_methods(grant: Grant, server_allowed: set[str]) -> frozenset[str]:
    """Intersection of the server allowlist with the grant's.

    ``allowed_methods`` absent or null means "inherit the server list"; an empty
    list means "no methods at all". That is the one place in the schema where
    absent and empty differ, and it is what lets a grant opt out of methods
    entirely without listing them.
    """
    if grant.allowed_methods is None:
        return frozenset(server_allowed)
    return frozenset(grant.allowed_methods) & frozenset(server_allowed)


def authorize_tool(
    name: str,
    arguments: Mapping[str, Any],
    grant: Grant,
    *,
    mode: str,
    server_allowed_methods: set[str],
) -> None:
    """Authorize one tool call against a grant. Raises ToolDenied.

    The model policy applies to the ``model`` argument of whichever tool carries
    one, which includes the introspection tools: denying ``res.users`` also hides
    its field metadata.
    """
    if mode == MODE_RO and name in WRITE_TOOLS:
        raise ToolDenied(
            "Read-only connection: write operations are disabled for this BMYA API key"
        )

    model = arguments.get("model")
    if isinstance(model, str) and model:
        if model in grant.denied_models:
            raise ToolDenied(f"Model '{model}' is not available on this connection")
        if grant.allowed_models is not None and model not in grant.allowed_models:
            raise ToolDenied(
                f"Model '{model}' is not in the allowed model list for this BMYA API key"
            )

    if name == "odoo_call_method":
        method = arguments.get("method")
        key = f"{model}.{method}"
        allowed = effective_allowed_methods(grant, server_allowed_methods)
        if key not in allowed:
            raise ToolDenied(
                f"Method '{key}' is not allowed on this connection. "
                f"Currently allowed: {', '.join(sorted(allowed)) or '(none)'}"
            )


def log_audit(
    *,
    grant: Optional[Grant],
    tool: str,
    mode: Optional[str],
    decision: str,
    detail: str = "",
) -> None:
    """One audit line per tool call. Never logs a key, a hash, or an Odoo key."""
    logger.info(
        "audit key_id=%s database=%s tool=%s mode=%s decision=%s%s",
        grant.key_id if grant else "-",
        grant.database if grant else "-",
        tool,
        mode or "-",
        decision,
        f" detail={detail}" if detail else "",
    )
