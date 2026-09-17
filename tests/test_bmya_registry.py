"""
Tests for src/bmya_registry.py -- the registry write path.

Two groups:

* **Ownership**, moved here from tests/test_bmya_keys_cli.py. They patch
  ``bmya_registry`` now: after the extraction, ``write_raw`` resolves
  ``bmya_registry._owner_of``, so a test patching ``bmya_keys_cli._owner_of``
  would be a no-op and **pass vacuously** -- the worst possible outcome for a
  test that exists to pin a production incident.
* **Locking**, new. The lost-update race they cover is real and reachable today
  with two operators, and becomes routine once the admin console is a second
  writer alongside the CLI.
"""

import json
import multiprocessing
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import bmya_registry  # noqa: E402


class TestWriteRawOwnership:
    """The atomic rewrite creates a new inode owned by whoever ran the write. On
    the deploy host that is root while the container reads as uid 1000, and the
    loader's response to an unreadable registry is to keep its last good copy
    and go "stale" -- so the mint looks successful and the key 401s. Verified in
    production 2026-07-28. These tests pin the ownership handling."""

    def _registry(self, tmp_path):
        path = tmp_path / "reg.json"
        bmya_registry.write_raw(str(path), {"version": 1, "grants": []})
        return str(path)

    def _pretend_owned_by_container(self, monkeypatch, path):
        """Report the registry as owned by uid/gid 1000, the state a correctly
        set-up deployment is in. Patches the helper rather than os.stat, which
        pytest itself calls."""
        real = bmya_registry._owner_of
        monkeypatch.setattr(
            bmya_registry, "_owner_of", lambda p: (1000, 1000) if p == path else real(p)
        )

    def test_rewrite_inherits_the_previous_owner(self, tmp_path, monkeypatch):
        path = self._registry(tmp_path)
        calls = []
        self._pretend_owned_by_container(monkeypatch, path)
        monkeypatch.setattr(bmya_registry.os, "chown", lambda p, u, g: calls.append((u, g)))

        bmya_registry.write_raw(path, {"version": 1, "grants": []})
        assert calls == [(1000, 1000)]

    def test_no_chown_when_ownership_already_matches(self, tmp_path, monkeypatch):
        """The console's normal case: it runs as uid 1000 against a file already
        owned by 1000:1000, so chown is never reached and cap_drop: ALL (no
        CAP_CHOWN) stays intact."""
        path = self._registry(tmp_path)

        def explode(*_args):
            raise AssertionError("chown must not be called when the owner already matches")

        monkeypatch.setattr(bmya_registry.os, "chown", explode)
        bmya_registry.write_raw(path, {"version": 1, "grants": []})

    def test_failed_chown_warns_with_the_exact_remedy(self, tmp_path, monkeypatch, capsys):
        """Not being root is the common case; the write still has to succeed,
        but the operator must be told or the server silently goes stale."""
        path = self._registry(tmp_path)
        self._pretend_owned_by_container(monkeypatch, path)

        def denied(*_args):
            raise PermissionError("Operation not permitted")

        monkeypatch.setattr(bmya_registry.os, "chown", denied)

        bmya_registry.write_raw(path, {"version": 1, "grants": ["x"]})

        err = capsys.readouterr().err
        assert "warning" in err
        assert f"sudo chown 1000:1000 {path}" in err
        assert "stale" in err
        # The write itself must still have gone through.
        assert json.loads(open(path).read())["grants"] == ["x"]

    def test_new_registry_hints_about_the_container_uid(self, tmp_path, capsys):
        path = str(tmp_path / "fresh.json")
        bmya_registry.write_raw(path, {"version": 1, "grants": []})
        err = capsys.readouterr().err
        assert "is new and owned by uid" in err
        assert f"sudo chown 1000:1000 {path}" in err

    def test_every_write_reminds_to_check_readyz(self, tmp_path, capsys):
        bmya_registry.write_raw(str(tmp_path / "reg.json"), {"version": 1, "grants": []})
        assert '"stale": false' in capsys.readouterr().err


class TestReadRaw:
    def test_missing_file_raises_rather_than_exiting(self, tmp_path):
        """The library raises; only the CLI turns this into a SystemExit."""
        with pytest.raises(bmya_registry.RegistryFileError):
            bmya_registry.read_raw(str(tmp_path / "nope.json"))

    def test_missing_file_allowed_returns_empty_registry(self, tmp_path):
        data = bmya_registry.read_raw(str(tmp_path / "nope.json"), allow_missing=True)
        assert data["grants"] == []
        assert data["version"] == 1

    def test_invalid_json_raises(self, tmp_path):
        path = tmp_path / "reg.json"
        path.write_text("{not json")
        with pytest.raises(bmya_registry.RegistryFileError):
            bmya_registry.read_raw(str(path))

    def test_wrong_shape_raises(self, tmp_path):
        path = tmp_path / "reg.json"
        path.write_text(json.dumps({"version": 1}))
        with pytest.raises(bmya_registry.RegistryFileError):
            bmya_registry.read_raw(str(path))

    def test_unknown_fields_survive_a_round_trip(self, tmp_path):
        """read_raw is raw on purpose: bmya_auth.Grant drops created_at and
        revoked_at, so a rewrite that went through it would silently erase
        them from every grant in the file."""
        path = str(tmp_path / "reg.json")
        grant = {"key_id": "aaa111", "created_at": "2026-01-01T00:00:00+00:00", "custom": 42}
        bmya_registry.write_raw(path, {"version": 1, "grants": [grant]})
        assert bmya_registry.read_raw(path)["grants"][0] == grant


def _append_grant(path, key_id, delay):
    """Read, wait, then append -- widening the read-modify-write window on
    purpose so the race is reproducible rather than occasional."""

    def mutate(data):
        time.sleep(delay)
        data["grants"].append({"key_id": key_id})

    bmya_registry.mutate_registry(path, mutate)


class TestLocking:
    def test_concurrent_writers_do_not_lose_a_grant(self, tmp_path):
        """The bug this module exists to fix.

        Without the lock both processes read the same file, each appends its own
        grant to its own copy, and the second write wins: the file ends up with
        exactly one of the two, no error on either side, and the .bak clobbered
        in the same cycle. The operator sees a successful mint and a key that
        401s.
        """
        path = str(tmp_path / "reg.json")
        bmya_registry.write_raw(path, {"version": 1, "grants": []})

        ctx = multiprocessing.get_context("spawn")
        procs = [
            ctx.Process(target=_append_grant, args=(path, "aaa111", 0.3)),
            ctx.Process(target=_append_grant, args=(path, "bbb222", 0.3)),
        ]
        for p in procs:
            p.start()
        for p in procs:
            p.join(timeout=30)

        assert all(p.exitcode == 0 for p in procs), [p.exitcode for p in procs]
        key_ids = {g["key_id"] for g in bmya_registry.read_raw(path)["grants"]}
        assert key_ids == {"aaa111", "bbb222"}

    def test_lock_file_inode_is_stable_across_writes(self, tmp_path):
        """The lock must never be taken on the registry itself: write_raw
        replaces that inode on every write, and a flock held on a replaced inode
        guards nothing -- the next writer would lock a different file."""
        path = str(tmp_path / "reg.json")
        bmya_registry.write_raw(path, {"version": 1, "grants": []})
        lock = bmya_registry.lock_path_for(path)

        inodes = set()
        registry_inodes = set()
        for i in range(3):
            bmya_registry.mutate_registry(path, lambda d, i=i: d["grants"].append({"key_id": i}))
            inodes.add(os.stat(lock).st_ino)
            registry_inodes.add(os.stat(path).st_ino)

        assert len(inodes) == 1, "the lock file must keep one inode"
        assert len(registry_inodes) == 3, "the registry is expected to be replaced every write"

    def test_a_raising_mutation_leaves_the_registry_untouched(self, tmp_path):
        path = str(tmp_path / "reg.json")
        bmya_registry.write_raw(path, {"version": 1, "grants": [{"key_id": "aaa111"}]})
        before = open(path, "rb").read()

        def boom(_data):
            raise RuntimeError("nope")

        with pytest.raises(RuntimeError):
            bmya_registry.mutate_registry(path, boom)

        assert open(path, "rb").read() == before
        # And the lock is free again, so the next writer is not wedged.
        bmya_registry.mutate_registry(path, lambda d: d["grants"].append({"key_id": "bbb222"}))
        assert len(bmya_registry.read_raw(path)["grants"]) == 2

    def test_bak_holds_the_previous_content(self, tmp_path):
        path = str(tmp_path / "reg.json")
        bmya_registry.write_raw(path, {"version": 1, "grants": [{"key_id": "aaa111"}]})
        bmya_registry.mutate_registry(path, lambda d: d["grants"].append({"key_id": "bbb222"}))

        backup = json.loads(open(f"{path}.bak").read())
        assert [g["key_id"] for g in backup["grants"]] == ["aaa111"]

    def test_lock_timeout_raises_rather_than_hanging(self, tmp_path):
        path = str(tmp_path / "reg.json")
        bmya_registry.write_raw(path, {"version": 1, "grants": []})

        with bmya_registry.registry_lock(path):
            with pytest.raises(bmya_registry.RegistryLockTimeout):
                with bmya_registry.registry_lock(path, timeout=0.2):
                    pass
