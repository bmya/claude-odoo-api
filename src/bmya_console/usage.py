"""
Read the MCP server's usage journal and roll it up per key.

The server appends one JSON line per tool call to ``<usage_dir>/YYYY-MM-DD.jsonl``
(``bmya_auth.record_usage``). The console only ever reads it -- the directory is
mounted read-only here -- and summarises it, so that when the time comes to put
a price on a tool call the number is chosen from real traffic instead of a guess.

Nothing bills on this yet. That is the point: the data has to already exist on
the day the decision is made, because it cannot be reconstructed afterwards.
"""

import json
import logging
import os
from datetime import datetime, timedelta, timezone

logger = logging.getLogger("bmya-console.usage")


def _day_files(usage_dir: str, days: int):
    """The journal files covering the last ``days`` days, oldest first.

    Built from the date range rather than by listing the directory, so a stray
    file dropped in there is never parsed.
    """
    today = datetime.now(timezone.utc).date()
    for offset in range(days - 1, -1, -1):
        day = today - timedelta(days=offset)
        path = os.path.join(usage_dir, f"{day.isoformat()}.jsonl")
        if os.path.exists(path):
            yield day.isoformat(), path


def read_records(usage_dir: str, days: int = 30):
    """Yield the parsed records from the last ``days`` days.

    A malformed line is skipped, not fatal: this is an append-only journal
    written by a different process, so a truncated last line during a read is
    normal rather than exceptional.
    """
    if not usage_dir or not os.path.isdir(usage_dir):
        return
    for day, path in _day_files(usage_dir, days):
        try:
            with open(path, "r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    record.setdefault("day", day)
                    yield record
        except OSError as exc:
            logger.warning("Could not read usage journal %s: %s", path, exc)


def summarize(usage_dir: str, days: int = 30) -> dict:
    """Roll the journal up per key and per day.

    ``calls`` counts every attempt; ``refused`` counts the ones that came back
    as an error to the client. Both matter: a key that is mostly refusals is a
    misconfigured client, and that is worth seeing before a customer reports it.
    """
    by_key = {}
    by_day = {}
    by_tool = {}
    total = 0
    refused = 0

    for record in read_records(usage_dir, days):
        total += 1
        key_id = record.get("key_id") or "—"
        day = record.get("day")
        tool = record.get("tool") or "—"
        is_refused = not record.get("ok")
        if is_refused:
            refused += 1

        entry = by_key.setdefault(
            key_id,
            {
                "key_id": key_id,
                "database": record.get("database"),
                "calls": 0,
                "refused": 0,
                "rows": 0,
                "ms": 0.0,
                "last_seen": None,
            },
        )
        entry["calls"] += 1
        entry["refused"] += 1 if is_refused else 0
        entry["rows"] += record.get("rows") or 0
        entry["ms"] += record.get("ms") or 0.0
        if record.get("ts") and (entry["last_seen"] or "") < record["ts"]:
            entry["last_seen"] = record["ts"]

        by_day[day] = by_day.get(day, 0) + 1
        by_tool[tool] = by_tool.get(tool, 0) + 1

    for entry in by_key.values():
        entry["avg_ms"] = round(entry["ms"] / entry["calls"], 1) if entry["calls"] else 0.0

    return {
        "total": total,
        "refused": refused,
        "days": days,
        "by_key": sorted(by_key.values(), key=lambda e: e["calls"], reverse=True),
        "by_day": sorted(by_day.items()),
        "by_tool": sorted(by_tool.items(), key=lambda kv: kv[1], reverse=True),
    }


def prune(usage_dir: str, keep_days: int) -> list:
    """Delete journal files older than ``keep_days``. Returns what it removed.

    The console owns retention because it is the only one of the two services
    with a writable view of anything -- and because the MCP server must not
    spend time on housekeeping in the request path.
    """
    if not usage_dir or not os.path.isdir(usage_dir):
        return []
    cutoff = datetime.now(timezone.utc).date() - timedelta(days=keep_days)
    removed = []
    for name in os.listdir(usage_dir):
        if not name.endswith(".jsonl"):
            continue
        try:
            day = datetime.fromisoformat(name[: -len(".jsonl")]).date()
        except ValueError:
            continue
        if day < cutoff:
            try:
                os.unlink(os.path.join(usage_dir, name))
                removed.append(name)
            except OSError as exc:
                logger.warning("Could not prune %s: %s", name, exc)
    return removed
