#!/usr/bin/env python3
"""
Read and write the BMYA API key registry (bmya-api-keys.json).

This module owns the *write* side of the registry. The read side that the MCP
server uses lives in ``bmya_auth`` (``load_registry`` / ``get_registry``) and is
deliberately separate: the server only ever reads, and it must not grow a
dependency on anything that can mutate its own input.

It was extracted from ``tools/bmya-keys.py`` for two reasons:

* the CLI cannot be imported. Its filename has a hyphen, so it is not a valid
  module identifier, and the tests reach it through
  ``importlib.util.spec_from_file_location``. That is acceptable in a test; it
  is not acceptable from production code, and the admin console needs the very
  same mint/revoke logic.
* ``write_raw`` alone is not enough. It is atomic (tempfile + fsync + rename),
  but every caller does *read* -> mutate -> *write*, and that cycle is not. With
  one human at a terminal the race was unreachable; with the console it is two
  writers against one file. See :func:`mutate_registry`.

Like ``tools/bmya-keys.py`` and ``bmya_auth``, this module uses **only the
standard library**, so the CLI keeps running on any ``python3`` >= 3.9 with no
virtualenv and nothing installed.
"""

import contextlib
import errno
import fcntl
import json
import os
import shutil
import sys
import tempfile
import time
from datetime import datetime, timezone

# The uid the container runs as (Dockerfile: `useradd -m -u 1000 odoo`). Only
# used to phrase the hint printed when the registry is created from scratch;
# every later rewrite inherits the ownership the file already had.
CONTAINER_UID = int(os.getenv("BMYA_KEYS_CONTAINER_UID", "1000"))

#: Appended to the registry path to get the lock file. A *separate* file on
#: purpose -- see :func:`registry_lock`.
LOCK_SUFFIX = ".lock"

#: How long to wait for the lock before giving up. A mint is milliseconds, so
#: anything near this means a stuck writer, not contention.
LOCK_TIMEOUT = float(os.getenv("BMYA_REGISTRY_LOCK_TIMEOUT", "10"))


class RegistryFileError(Exception):
    """The registry file is missing, unreadable, or not shaped like a registry.

    The CLI turns this into a ``SystemExit``; the console turns it into a 500 or
    a 503. Neither behaviour belongs in here.
    """


class RegistryLockTimeout(RegistryFileError):
    """Could not acquire the registry lock within :data:`LOCK_TIMEOUT`."""


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def read_raw(path: str, *, allow_missing: bool = False) -> dict:
    """Read the registry as raw JSON, preserving fields this tool does not know.

    Raw rather than parsed on purpose: ``bmya_auth.Grant`` drops ``created_at``
    and ``revoked_at`` (it lists them in ``_KNOWN_GRANT_FIELDS`` but does not
    carry them as attributes), so anything that has to round-trip or display the
    file -- a rewrite, the console's grant list -- must not go through it.
    """
    if not os.path.exists(path):
        if allow_missing:
            return {"version": 1, "updated_at": now_iso(), "grants": []}
        raise RegistryFileError(f"registry not found: {path}")
    with open(path, "r", encoding="utf-8") as handle:
        try:
            data = json.load(handle)
        except json.JSONDecodeError as exc:
            raise RegistryFileError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("grants"), list):
        raise RegistryFileError(f"{path} must be an object with a 'grants' list")
    return data


def _owner_of(path: str):
    """``(uid, gid)`` of an existing path, or ``None`` if it cannot be stat'd."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_uid, st.st_gid)


def _preserve_ownership(tmp: str, uid: int, gid: int, path: str) -> None:
    """Give the replacement file the ownership the registry already had.

    Verified in production 2026-07-28, and the reason this function exists: the
    atomic rewrite below creates a **new inode**, owned by whoever ran the CLI.
    On the deployment host that is root, while the container reads the registry
    as uid 1000 -- so a plain rename leaves the server unable to read its own
    key registry.

    That failure is silent by design elsewhere: the loader keeps its last good
    snapshot, logs an ERROR and flips ``/readyz`` to ``"stale": true`` rather
    than locking every client out (see ``get_registry`` in ``bmya_auth``). The
    operator sees a *successful* mint whose key then 401s, with nothing in
    between to connect the two. Preserving ownership here removes the trap
    instead of documenting it.

    The early return matters for the console: it runs as uid 1000 against a file
    already owned by 1000:1000, so ``os.chown`` is never called and the
    container keeps ``cap_drop: ALL`` (no ``CAP_CHOWN``).
    """
    if _owner_of(tmp) == (uid, gid):
        return
    try:
        os.chown(tmp, uid, gid)
    except (OSError, AttributeError) as exc:
        # Not fatal: the write still happens. But the server may go stale, so
        # this has to be loud and carry the exact remedy.
        print(
            f"warning: could not keep {path} owned by {uid}:{gid} ({exc}).\n"
            f"         The MCP server may no longer be able to read it "
            f'(check /readyz for "stale": true). Fix with:\n'
            f"           sudo chown {uid}:{gid} {path}",
            file=sys.stderr,
        )


def write_raw(path: str, data: dict) -> None:
    """Write the registry atomically, keeping a .bak of the previous content.

    Atomic in isolation, but **not** a substitute for :func:`mutate_registry`:
    the ``.bak`` copy happens here, before the write, so two racing read-modify
    -write cycles clobber the backup as well as losing a grant. Call this
    directly only when you already hold the lock, or when there is provably no
    second writer (tests, first creation).
    """
    data["updated_at"] = now_iso()
    directory = os.path.dirname(os.path.abspath(path)) or "."

    previous_owner = None
    if os.path.exists(path):
        previous_owner = _owner_of(path)
        shutil.copy2(path, f"{path}.bak")

    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".bmya-keys-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o600)
        if previous_owner is not None:
            _preserve_ownership(tmp, previous_owner[0], previous_owner[1], path)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    print(f"Wrote {path} (previous copy at {path}.bak)", file=sys.stderr)

    if previous_owner is None:
        # First creation: there is no previous owner to inherit, so the only
        # thing we can do is say who owns it now.
        current = _owner_of(path)
        owner = current[0] if current else -1
        print(
            f"note: {path} is new and owned by uid {owner}. If it is bind-mounted "
            f"into the container (which runs as uid {CONTAINER_UID}), chown it or "
            f"the server cannot read it:\n"
            f"        sudo chown {CONTAINER_UID}:{CONTAINER_UID} {path}",
            file=sys.stderr,
        )
    print(
        "note: the server reloads within BMYA_REGISTRY_TTL_SECONDS (default 10); "
        'confirm with GET /readyz showing "stale": false',
        file=sys.stderr,
    )


def lock_path_for(path: str) -> str:
    return f"{path}{LOCK_SUFFIX}"


@contextlib.contextmanager
def registry_lock(path: str, *, timeout: float = None):
    """Hold an exclusive lock covering a whole read-modify-write of the registry.

    The lock is taken on ``<registry>.lock``, a **separate file**, never on the
    registry itself. That is load-bearing: ``write_raw`` replaces the registry's
    inode on every write (tempfile + ``os.replace``), and a ``flock`` held on an
    inode that has just been unlinked protects nothing -- the next writer opens
    the *new* inode and locks something else entirely. The lock file is created
    once and never replaced, so every writer contends on the same inode.

    ``flock`` is per-inode in the host kernel, so this serializes across
    processes *and* across containers sharing the bind mount, which is exactly
    the console-vs-CLI case. It does **not** work over NFS; everything here is
    local to BARBOL, but that is why.

    Readers take nothing and need no change: ``os.replace`` is atomic, so a
    reader sees either the whole old file or the whole new one. This lock exists
    only to serialize writers.
    """
    if timeout is None:
        timeout = LOCK_TIMEOUT
    lock_file = lock_path_for(path)
    directory = os.path.dirname(os.path.abspath(lock_file)) or "."
    if not os.path.isdir(directory):
        raise RegistryFileError(f"registry directory does not exist: {directory}")

    try:
        fd = os.open(lock_file, os.O_CREAT | os.O_RDWR, 0o600)
    except OSError as exc:
        raise RegistryFileError(f"cannot open registry lock {lock_file}: {exc}") from exc

    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN):
                    raise RegistryFileError(f"cannot lock {lock_file}: {exc}") from exc
                if time.monotonic() >= deadline:
                    raise RegistryLockTimeout(
                        f"another writer has held {lock_file} for more than {timeout}s; "
                        "check for a stuck process before removing it"
                    ) from exc
                time.sleep(0.05)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def mutate_registry(path: str, fn, *, allow_missing: bool = False) -> dict:
    """Read, mutate and write the registry inside a single lock hold.

    ``fn(data)`` receives the parsed registry and mutates it in place. Returning
    a value is not how you pass the result back; the mutated ``data`` is written
    and returned.

    This is the whole point of the module. Without it, ``read_raw`` ->
    ``append`` -> ``write_raw`` from two writers produces a file containing
    exactly one of the two new grants, **with no error on either side**: the
    operator sees a successful mint and a key that answers 401 -- the same
    signature as the ownership bug, and just as hard to trace. The ``.bak`` is
    overwritten in the same cycle, so there is nothing to recover from either.

    If ``fn`` raises, the registry is left byte-identical and the lock is
    released.
    """
    with registry_lock(path):
        data = read_raw(path, allow_missing=allow_missing)
        fn(data)
        write_raw(path, data)
        return data
