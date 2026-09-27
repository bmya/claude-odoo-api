"""
Turn the grant form into a registry entry, or into field errors.

The whole reason this is a module and not inline in the routes is the
``allowed_methods`` tri-state. Everything else is ordinary parsing.
"""

import bmya_auth
import bmya_snippets

#: allowed_methods: absent/null inherits the server list, [] means no methods at
#: all, a list narrows. It is the one field in the schema where absent != empty,
#: and a single text box can only express two of the three.
METHODS_MODES = ("inherit", "none", "list")
#: allowed_models has no third state worth offering: [] parses fine but yields a
#: key for which authorize_tool denies every model -- valid, minted and useless.
MODELS_MODES = ("unrestricted", "list")


def _lines(raw):
    """Split a textarea into entries, accepting commas or newlines.

    Operators paste both. Nothing downstream cares which they used.
    """
    if raw is None:
        return []
    items = []
    for chunk in str(raw).replace(",", "\n").split("\n"):
        chunk = chunk.strip()
        if chunk:
            items.append(chunk)
    return items


def parse_methods(form) -> tuple:
    """``(allowed_methods, errors)`` from the methods tri-state.

    Shared by minting and editing so both enforce the same rule: "sólo estos"
    with an empty box is an error, never a silent ``[]``.
    """
    errors = []
    methods_mode = (form.get("methods_mode") or "inherit").strip()
    allowed_methods = None
    if methods_mode not in METHODS_MODES:
        errors.append("Opción de métodos inválida.")
    elif methods_mode == "none":
        allowed_methods = []
    elif methods_mode == "list":
        allowed_methods = _lines(form.get("methods_list"))
        if not allowed_methods:
            # Never silently fall through to [] -- that is the opposite of
            # "inherit", and the dangerous state has to be chosen by name.
            errors.append(
                'Elegiste "sólo estos métodos" pero no listaste ninguno. '
                'Si querías deshabilitarlos todos, elegí "ningún método".'
            )
    return allowed_methods, errors


def describe_methods(allowed_methods) -> str:
    """Human form of the tri-state, for the edit history."""
    if allowed_methods is None:
        return "hereda"
    if not allowed_methods:
        return "ninguno"
    return ", ".join(allowed_methods)


def parse_grant_form(form) -> tuple:
    """Build a registry entry from the submitted form.

    Returns ``(entry, errors)``. ``entry`` is None when ``errors`` is non-empty;
    it carries no ``key_id``/``key_sha256`` -- minting is the caller's job, and
    keeping it out of here means this function can be called to re-validate an
    edit without generating a key.
    """
    errors = []

    raw_url = (form.get("odoo_url") or "").strip()
    url = None
    try:
        url = bmya_auth.validate_odoo_url(raw_url)
    except ValueError as exc:
        # The same function the server calls, so the console structurally cannot
        # mint a URL the server would then refuse to load.
        message = str(exc)
        if not bmya_auth.BMYA_ALLOW_INSECURE_URLS:
            message += " (para una URL local o self-hosted hace falta BMYA_ALLOW_INSECURE_URLS=1)"
        errors.append(message)

    database = (form.get("database") or "").strip()
    if not database:
        errors.append("La base de datos es obligatoria.")

    mode = (form.get("mode") or "").strip()
    if mode not in bmya_auth.MODES:
        # Mirrors _parse_grant: a typo has to be loud, never a silent downgrade.
        errors.append(f"El modo debe ser uno de {', '.join(bmya_auth.MODES)}.")

    allowed_methods, method_errors = parse_methods(form)
    errors.extend(method_errors)

    models_mode = (form.get("models_mode") or "unrestricted").strip()
    allowed_models = None
    if models_mode not in MODELS_MODES:
        errors.append("Opción de modelos inválida.")
    elif models_mode == "list":
        allowed_models = _lines(form.get("models_list"))
        if not allowed_models:
            errors.append(
                'Elegiste "sólo estos modelos" pero no listaste ninguno. '
                'Si no querías restringir, elegí "sin restricción".'
            )

    denied_models = _lines(form.get("denied_models"))

    odoo_api = (form.get("odoo_api") or bmya_auth.ODOO_API_AUTO).strip()
    if odoo_api not in bmya_auth.ODOO_APIS:
        errors.append("Opción de API de Odoo inválida.")
    odoo_login = (form.get("odoo_login") or "").strip()
    if odoo_api == bmya_auth.ODOO_API_JSONRPC and not odoo_login:
        errors.append(
            "Con la API fijada en JSON-RPC (Odoo 17/18) el login de Odoo es obligatorio: "
            "sin él el servidor no puede resolver el uid."
        )

    expires_at = None
    try:
        expires_at = bmya_snippets.parse_expiry((form.get("expires_at") or "").strip())
    except ValueError as exc:
        errors.append(f"Fecha de expiración inválida: {exc}")

    if errors:
        return None, errors

    entry = {
        "label": (form.get("label") or "").strip(),
        "odoo_url": url,
        "database": database,
        "mode": mode,
        "odoo_login": odoo_login,
        "odoo_api": odoo_api,
        "allowed_methods": allowed_methods,
        "allowed_models": allowed_models,
        "denied_models": denied_models,
        "revoked": False,
        "expires_at": expires_at,
        "revoked_at": None,
        "notes": (form.get("notes") or "").strip(),
    }
    return entry, []


def method_warnings(allowed_methods) -> list:
    """Methods that the server allowlist will intersect away to nothing.

    Not an error: the grant is valid and the server may be reconfigured later.
    But a method listed here and absent from ODOO_MCP_ALLOWED_METHODS is dead on
    arrival -- effective_allowed_methods() intersects the two -- and finding that
    out from a client's bug report is expensive.
    """
    if not allowed_methods:
        return []
    server = bmya_auth.server_allowed_methods()
    return [m for m in allowed_methods if m not in server]


def validate_against_server_parser(entry: dict) -> list:
    """Last gate: run the assembled entry through the server's own parser.

    Belt and braces over validate_odoo_url above. It makes "the console can
    never mint a key the server would refuse" structurally true rather than
    aspirational, and it costs one call.
    """
    probe = dict(entry)
    probe.setdefault("key_id", "000000")
    probe.setdefault("key_sha256", "0" * 64)
    try:
        bmya_auth._parse_grant(probe, 0)
    except ValueError as exc:
        return [f"El servidor rechazaría este grant: {exc}"]
    return []
