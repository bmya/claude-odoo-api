"""
Check a grant's URL and database *before* minting a key for it.

The console never sees the end user's Odoo API key, so it cannot make an
authenticated call. What it can do is separate the two failure modes that look
identical to a client and cost the most support time:

``docs/bmya-api-keys.md`` records that a stale Odoo.sh database name -- the
``bmya-bmya-sh-sta-35226662`` build suffix, which changes on every rebuild --
makes every call fail with ``404 "No database is selected"``, and that it "no es
un problema de credenciales ni del grant". That is detectable with no
credentials at all: send a deliberately invalid Bearer and read which way Odoo
says no.

A 401 is the *good* answer here. It means the host answered, accepted the
database, and rejected the key -- which is exactly what a real client with a
real key would get past.
"""

import logging
from dataclasses import dataclass

logger = logging.getLogger("bmya-console.probe")

OK = "ok"
WRONG_DATABASE = "wrong_database"
UNREACHABLE = "unreachable"
UNEXPECTED = "unexpected"


@dataclass(frozen=True)
class ProbeResult:
    verdict: str
    detail: str = ""

    @property
    def is_ok(self) -> bool:
        return self.verdict == OK


def probe_grant(odoo_url: str, database: str, *, timeout: float = 5.0) -> ProbeResult:
    """Ask Odoo whether this (url, database) pair exists.

    **Only ever call this with a URL that has already passed
    ``bmya_auth.validate_odoo_url``.** This is an outbound HTTP request to an
    address an operator typed, i.e. an SSRF vector out of the console;
    ``validate_odoo_url`` is what rejects loopback, link-local and private IP
    literals and enforces the suffix allowlist. There is no second check here,
    on purpose -- one authority for that rule, not two that can drift.

    Redirects are not followed: a 302 to 169.254.169.254 would otherwise walk
    straight into the cloud metadata service. No retry adapter and a hard
    timeout, because this is a UI affordance, not a resilient client.
    """
    import requests

    endpoint = f"{odoo_url.rstrip('/')}/json/2/res.company/search_read"
    try:
        response = requests.post(
            endpoint,
            json={"domain": [], "fields": ["id"], "limit": 1},
            headers={
                "Authorization": "Bearer bmya-console-probe-invalid",
                "X-Odoo-Database": database,
                "Content-Type": "application/json",
            },
            timeout=timeout,
            allow_redirects=False,
        )
    except Exception as exc:  # noqa: BLE001 - any transport failure is "unreachable"
        return ProbeResult(UNREACHABLE, f"{type(exc).__name__}: {exc}")

    body = (response.text or "")[:200]

    if response.status_code in (401, 403):
        return ProbeResult(
            OK, f"HTTP {response.status_code}: la instancia respondió y aceptó la base."
        )
    if response.status_code == 404 and "no database is selected" in body.lower():
        return ProbeResult(
            WRONG_DATABASE,
            f"La instancia responde pero no reconoce la base {database!r}. "
            "En Odoo.sh el nombre lleva un sufijo de build que cambia en cada reconstrucción.",
        )
    return ProbeResult(UNEXPECTED, f"HTTP {response.status_code}: {body}")


def readyz_status(url: str, *, timeout: float = 3.0) -> dict:
    """Poll the MCP server's /readyz so the console can show whether it is stale.

    ``stale: true`` is the signature of the ownership/permission trap: the write
    succeeded, the loader could not re-read the file, it kept its last good copy
    and the freshly minted key answers 401. Surfacing it is the single most
    useful thing this console can do after a mint.

    /readyz is unauthenticated by design (BmyaAuthMiddleware.EXEMPT_PATHS).
    """
    if not url:
        return {"configured": False}
    import requests

    try:
        response = requests.get(url, timeout=timeout)
        data = response.json()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not poll %s: %s", url, exc)
        return {"configured": True, "reachable": False, "error": type(exc).__name__}
    return {
        "configured": True,
        "reachable": True,
        "status": data.get("status"),
        "stale": bool(data.get("stale")),
    }
