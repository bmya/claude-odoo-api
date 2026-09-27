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

Odoo 17 and 18 have no /json/2: they redirect it to /web/login. So the probe
first asks the public /web/webclient/version_info, and for those versions checks
the database with JSON-RPC ``db.db_exist`` instead -- also credential-free, and
unlike a deliberately failed ``authenticate`` it does not count towards Odoo's
per-IP login cooldown, which the MCP server shares with this console.
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
    #: server_serie ("18.0"), or "" when the instance did not say.
    version: str = ""

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

    version = odoo_version(odoo_url, timeout=timeout)
    if version is not None and version[0] < 19:
        return _probe_jsonrpc(odoo_url, database, version[1], timeout=timeout)
    serie = version[1] if version else ""

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
        return ProbeResult(UNREACHABLE, f"{type(exc).__name__}: {exc}", serie)

    body = (response.text or "")[:200]

    if response.status_code in (401, 403):
        return ProbeResult(
            OK, f"HTTP {response.status_code}: la instancia respondió y aceptó la base.", serie
        )
    if response.status_code == 404 and "no database is selected" in body.lower():
        return ProbeResult(
            WRONG_DATABASE,
            f"La instancia responde pero no reconoce la base {database!r}. "
            "En Odoo.sh el nombre lleva un sufijo de build que cambia en cada reconstrucción.",
            serie,
        )
    return ProbeResult(UNEXPECTED, f"HTTP {response.status_code}: {body}", serie)


def odoo_version(odoo_url: str, *, timeout: float = 5.0):
    """``(major, server_serie)`` from the public version_info route, or None.

    Same rules as probe_grant: only a URL validate_odoo_url accepted, and no
    redirects. Never raises -- "could not tell" is None.
    """
    import requests

    try:
        response = requests.post(
            f"{odoo_url.rstrip('/')}/web/webclient/version_info",
            json={"jsonrpc": "2.0", "method": "call", "params": {}},
            timeout=timeout,
            allow_redirects=False,
        )
        info = response.json()["result"]
        major = int(info["server_version_info"][0])
        return major, str(info.get("server_serie") or major)
    except Exception as exc:  # noqa: BLE001
        logger.info("No version from %s: %s", odoo_url, exc)
        return None


def _probe_jsonrpc(odoo_url: str, database: str, serie: str, *, timeout: float) -> ProbeResult:
    import requests

    try:
        response = requests.post(
            f"{odoo_url.rstrip('/')}/jsonrpc",
            json={
                "jsonrpc": "2.0",
                "method": "call",
                "params": {"service": "db", "method": "db_exist", "args": [database]},
                "id": 1,
            },
            timeout=timeout,
            allow_redirects=False,
        )
        answer = response.json()
    except Exception as exc:  # noqa: BLE001
        return ProbeResult(UNREACHABLE, f"{type(exc).__name__}: {exc}", serie)

    note = (
        f"Odoo {serie}: sin API JSON-2, el servidor la usa por JSON-RPC y el grant "
        "necesita el login de Odoo del dueño de la API key."
    )
    result = answer.get("result") if isinstance(answer, dict) else None
    if result is True:
        return ProbeResult(OK, f"{note} La base existe.", serie)
    if result is False:
        return ProbeResult(
            WRONG_DATABASE,
            f"Odoo {serie} responde pero no reconoce la base {database!r}.",
            serie,
        )
    return ProbeResult(UNEXPECTED, f"{note} db_exist respondió: {str(answer)[:200]}", serie)


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
