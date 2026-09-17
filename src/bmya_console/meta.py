"""
Console-side metadata about each grant: who minted it, and the reserved slots
for invitations and credits.

**Why a separate file and not extra fields in the registry.** Mechanically the
fields would fit -- ``bmya_auth._parse_grant`` warns about unknown fields and
ignores them, and ``read_raw``/``write_raw`` round-trip them intact. Three
reasons not to:

1. That warning fires **per grant, per reload**, and a reload happens on every
   mint and every revocation. Ten grants times three extra fields is a permanent
   wall of warnings in the only audit trail the server has, which trains whoever
   reads it to ignore the logger that also reports skipped grants and duplicate
   key ids. Destroying signal on a security log to store an email address is a
   bad trade.
2. The registry is parsed on the authentication path by a security-critical
   loader with a stable, documented, versioned schema. Metadata that will churn
   (invitations now, credits later) does not belong in the artifact you want to
   stay boring.
3. Blast radius. A corrupt meta file means the list shows an em dash where a
   name should be. A corrupt registry means every client gets 401 -- or worse,
   the loader keeps its last good copy, goes ``stale``, and revocations stop
   applying.

Adding the fields to ``_KNOWN_GRANT_FIELDS`` to silence the warning is worse
still: it grows the security schema to carry data the server ignores.

The file lives in the **same directory** as the registry, so it inherits one
bind mount, one chown, one lock and one backup story. It holds no secret -- no
plaintext, no key hash -- so the MCP container seeing it read-only is harmless.
"""

import json
import logging
import os
from typing import Protocol

import bmya_registry

logger = logging.getLogger("bmya-console.meta")

META_VERSION = 1


def empty_meta() -> dict:
    """A fresh meta document.

    ``invitations`` and ``credit_accounts`` exist, empty, from day one so that
    turning those features on is not a schema change and ``version`` does not
    have to move.
    """
    return {
        "version": META_VERSION,
        "updated_at": bmya_registry.now_iso(),
        "keys": {},
        "invitations": [],
        "credit_accounts": [],
    }


def new_key_meta(*, created_by: str, created_from_ip: str = "") -> dict:
    return {
        "created_by": created_by,
        "created_at": bmya_registry.now_iso(),
        "created_from_ip": created_from_ip,
        # Reserved. Always null until the matching feature is switched on.
        "invitation_id": None,
        "credit_account_id": None,
        "history": [],
    }


def history_entry(actor: str, action: str, detail: str = "") -> dict:
    entry = {"at": bmya_registry.now_iso(), "actor": actor, "action": action}
    if detail:
        entry["detail"] = detail
    return entry


# --- Invitations: the shape only. No routes, no token minting, nothing enabled.
#
# Recorded here now so that switching the feature on is not a schema change, and
# so the decisions behind it are not re-litigated from scratch later.
#
#   {"id": "...", "created_by": "...", "created_at": "...", "expires_at": "...",
#    "token_sha256": "...", "consumed_at": null, "consumed_key_id": null,
#    "odoo_login": "...", "provisioning_status": "pending|provisioned",
#    "template": {"odoo_url": ..., "database": ..., "mode": "readonly",
#                 "denied_models": [...], "sandbox": true}}
#
# Decisions worth keeping:
#
# * ``token_sha256`` only, same discipline as the registry: shown once, never
#   stored. The recipient is external and never authenticates any other way, so
#   the invitation token is the ONLY credential on that path -- which is why it
#   must be high-entropy, short-lived and single-use.
# * **Single-use, always.** A multi-use invitation saves the inviter one click
#   and costs the ability to say who redeemed what. For an outsider touching a
#   client's data that trade is not close.
# * Two short, independent clocks: 7 days to redeem, and the resulting grant
#   expires 14 days after redemption, never renewable in place.
# * ``odoo_url`` and ``database`` are NOT inputs to the redeem endpoint -- they
#   are read from ``template``. That is the real control that stops an invited
#   tester pointing a key at another client's Odoo. Do not "simplify" it into a
#   form field.
# * ``BMYA_ALLOWED_URL_SUFFIXES`` is NOT that control and must not be relied on
#   for it: production already carries ``.odoo.com``, which admits every Odoo
#   Online database in existence. It is an anti-SSRF backstop, not a tenant
#   boundary. Invitations need an EXACT (url, database) allowlist.
# * ``odoo_login`` / ``provisioning_status`` exist because an invitee needs an
#   Odoo user with an API key on the sandbox database or the minted key is inert
#   (resolve_odoo_client raises "Missing X-Odoo-Api-Key"). Without tracking it,
#   every invitation turns into a support conversation.
# * When the routes exist they must 404 while disabled, not 403: a 403 confirms
#   the feature is there.
#
# Residual risk, stated plainly: an invited tester with a readonly sandbox key
# and their own Odoo user reads whatever that Odoo user can read. The isolation
# is the sandbox database's, not the MCP server's. MCP narrows; it does not
# create a boundary Odoo does not already have.


class MetaStore(Protocol):
    """The seam. One Protocol, one implementation, one factory.

    Deliberately the same shape -- and the same error message -- as
    ``bmya_auth._load_from_backend``: the repo already has a house style for
    "this is where a second backend plugs in", so this reuses it rather than
    inventing a second one. A future Mongo implementation is a collection whose
    ``_id`` is the ``key_id`` and whose documents are exactly these.
    """

    def all(self) -> dict: ...

    def get(self, key_id: str) -> dict: ...

    def put(self, key_id: str, doc: dict) -> None: ...

    def append_history(self, key_id: str, actor: str, action: str, detail: str = "") -> None: ...


class JsonFileMetaStore:
    """The only implementation today."""

    def __init__(self, path: str):
        self.path = path

    def _read(self) -> dict:
        """Read the meta document, degrading to empty rather than raising.

        A missing file is normal (nothing has been minted through the console
        yet). A corrupt one is loud in the log but must not take the grant list
        down with it: the registry is the source of truth, and a list showing
        "created by --" beats a 500.
        """
        if not os.path.exists(self.path):
            return empty_meta()
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            logger.error(
                "Could not read console meta %s, continuing without it: %s", self.path, exc
            )
            return empty_meta()
        if not isinstance(data, dict) or not isinstance(data.get("keys"), dict):
            logger.error("Console meta %s is not shaped like a meta document; ignoring", self.path)
            return empty_meta()
        data.setdefault("invitations", [])
        data.setdefault("credit_accounts", [])
        return data

    def _write(self, data: dict) -> None:
        data["updated_at"] = bmya_registry.now_iso()
        data.setdefault("version", META_VERSION)
        # Reuses the registry's atomic write: tempfile + fsync + rename, mode
        # 600, ownership preserved. Same directory, so the same uid rules apply
        # and the container keeps cap_drop: ALL.
        bmya_registry.write_raw_json(self.path, data)

    def all(self) -> dict:
        return self._read().get("keys", {})

    def get(self, key_id: str) -> dict:
        return self._read().get("keys", {}).get(key_id, {})

    def put(self, key_id: str, doc: dict) -> None:
        data = self._read()
        data.setdefault("keys", {})[key_id] = doc
        self._write(data)

    def append_history(self, key_id: str, actor: str, action: str, detail: str = "") -> None:
        data = self._read()
        keys = data.setdefault("keys", {})
        doc = keys.setdefault(key_id, new_key_meta(created_by=""))
        doc.setdefault("history", []).append(history_entry(actor, action, detail))
        self._write(data)


def build_meta_store(settings) -> MetaStore:
    if settings.meta_backend == "file":
        return JsonFileMetaStore(settings.meta_file)
    raise RuntimeError(
        f"Unsupported BMYA_CONSOLE_META_BACKEND {settings.meta_backend!r} "
        "(only 'file' is implemented)"
    )
