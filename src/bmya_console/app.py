"""
Routes, middleware and security headers for the BMYA admin console.

The console is the second writer of the key registry (the first is
``tools/bmya-keys.py``), so every mutation goes through
``bmya_registry.mutate_registry`` and its lock. Nothing in here touches the
registry file directly.
"""

import datetime
import logging
import os

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

import bmya_auth
import bmya_registry
import bmya_snippets

from . import auth as console_auth
from . import forms, probe, usage
from .config import Settings
from .meta import JsonFileMetaStore, build_meta_store, history_entry, new_key_meta

logger = logging.getLogger("bmya-console")

HERE = os.path.dirname(os.path.abspath(__file__))


def _audit(actor: str, action: str, **fields) -> None:
    """One line per console mutation, shaped like bmya_auth.log_audit.

    Never logs a plaintext key or a hash -- only the key_id, which is public and
    is already the handle everything else uses.
    """
    extra = " ".join(f"{k}={v}" for k, v in fields.items() if v not in (None, ""))
    logger.info("console audit actor=%s action=%s%s", actor, action, f" {extra}" if extra else "")


def _grant_status(grant: dict, now=None) -> str:
    now = now or datetime.datetime.now(datetime.timezone.utc)
    if grant.get("revoked"):
        return "revocada"
    raw = grant.get("expires_at")
    if not raw:
        return "activa"
    try:
        expires = datetime.datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return "activa"
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=datetime.timezone.utc)
    if expires <= now:
        return "vencida"
    days = (expires - now).days
    if days <= 30:
        return f"vence en {days} día(s)"
    return "activa"


def build_console_app(settings: Settings = None) -> FastAPI:
    settings = settings or Settings.from_env()

    if not settings.session_secret:
        # Same reasoning as docsonline: without a fixed secret every restart
        # invalidates all sessions and logs everyone out with no warning.
        raise RuntimeError(
            "BMYA_CONSOLE_SESSION_SECRET no está configurado. Generá uno con "
            '`python -c "import secrets; print(secrets.token_urlsafe(32))"`.'
        )
    if not settings.auth_ready:
        # Fail-closed, and loud. /readyz reports 503 so a deploy with an empty
        # operator list fails visibly instead of locking everyone out quietly.
        logger.error(
            "%s. La consola arranca igual para que /readyz lo reporte.",
            settings.operators_problem,
        )
    elif settings.operators_rejected:
        logger.warning(
            "BMYA_CONSOLE_OPERATORS: %d entrada(s) ignoradas por no ser email:sha256",
            settings.operators_rejected,
        )

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    app.state.meta = build_meta_store(settings)

    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.session_secret,
        session_cookie="bmya_console_session",
        max_age=settings.session_max_age,
        # Lax, never Strict: a Strict cookie is withheld from top-level
        # cross-site navigations, which is how an OAuth callback arrives -- and
        # that is the shape the Google login will take when it replaces the
        # operator keys. Lax still withholds it from cross-site POSTs.
        same_site="lax",
        # A Secure cookie is not sent over plain HTTP: on http://10.0.0.14:8081
        # the session would be set and never come back, an infinite login loop.
        # Flip this to 1 the day Traefik fronts the console -- env only, no code.
        https_only=settings.cookie_secure,
    )

    templates = Jinja2Templates(directory=os.path.join(HERE, "templates"))
    app.mount("/static", StaticFiles(directory=os.path.join(HERE, "static")), name="static")

    def render(request, name, **context):
        context.setdefault("settings", settings)
        context.setdefault("identity", console_auth.current_identity(request))
        context.setdefault("csrf", console_auth.csrf_token(request))
        return templates.TemplateResponse(request, name, context)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    # --- Health, never authenticated (same contract as the MCP server)

    @app.get("/health", include_in_schema=False)
    def health():
        return JSONResponse({"status": "ok"})

    @app.get("/readyz", include_in_schema=False)
    def readyz():
        """Can this console actually do its job.

        503 unless the registry parses, the config directory is writable, and
        somebody is able to log in. The writability check is what makes the
        :ro -> :rw mount flip verifiable: a botched mount fails the deploy here
        instead of 500-ing on the first mint.
        """
        problems = []
        try:
            bmya_auth.load_registry(settings.registry_file)
        except Exception as exc:  # noqa: BLE001
            problems.append(f"registry: {exc}")

        try:
            with bmya_registry.registry_lock(settings.registry_file, timeout=2):
                pass
        except Exception as exc:  # noqa: BLE001
            problems.append(f"config dir not writable: {exc}")

        if not settings.auth_ready:
            problems.append(settings.operators_problem)

        if problems:
            return JSONResponse({"status": "unready", "problems": problems}, status_code=503)
        return JSONResponse({"status": "ready", "read_only": settings.read_only})

    # --- Session

    @app.get("/login", response_class=HTMLResponse, include_in_schema=False)
    def login_form(request: Request):
        if console_auth.current_identity(request):
            return RedirectResponse("/grants", status_code=302)
        return render(request, "login.html", error=None)

    @app.post("/login", response_class=HTMLResponse, include_in_schema=False)
    def login_submit(request: Request, email: str = Form(""), key: str = Form("")):
        identity = console_auth.authenticate(email, key, settings.operators)
        if identity is None:
            logger.warning("Login rechazado para %r", (email or "").strip().lower())
            response = render(request, "login.html", error=console_auth.PUBLIC_LOGIN_ERROR)
            response.status_code = 401
            return response
        console_auth.login_session(request, identity)
        _audit(identity.email, "login")
        return RedirectResponse("/grants", status_code=303)

    @app.post("/logout", include_in_schema=False)
    def logout(request: Request, csrf_token: str = Form("")):
        if not console_auth.csrf_ok(request, csrf_token):
            return JSONResponse({"error": "csrf"}, status_code=403)
        console_auth.logout_session(request)
        return RedirectResponse("/login", status_code=303)

    # --- Guards shared by every authenticated route

    def _require_login(request):
        """Returns (identity, response). A response means: stop and return it."""
        identity = console_auth.current_identity(request)
        if identity is None:
            return None, RedirectResponse("/login", status_code=302)
        return identity, None

    def _guard_mutation(request, csrf_token):
        """Login + CSRF + the read-only kill switch, in that order.

        Returns (identity, response); a response means the request is refused
        and the registry has not been touched.
        """
        identity = console_auth.current_identity(request)
        if identity is None:
            return None, RedirectResponse("/login", status_code=302)
        if not console_auth.csrf_ok(request, csrf_token):
            logger.warning("CSRF rechazado para %s", identity.email)
            return None, JSONResponse({"error": "csrf"}, status_code=403)
        if settings.read_only:
            return None, JSONResponse({"error": "console is read-only"}, status_code=403)
        return identity, None

    def _load_grants():
        """Raw JSON, never load_registry().

        bmya_auth.Grant drops created_at and revoked_at -- it lists them in
        _KNOWN_GRANT_FIELDS but does not carry them as attributes -- so a list
        view built on it would simply have no dates to show.
        """
        return bmya_registry.read_raw(settings.registry_file, allow_missing=True)["grants"]

    # --- Grants

    @app.get("/grants", response_class=HTMLResponse, include_in_schema=False)
    def grants_list(request: Request, show_revoked: int = 0):
        identity, redirect = _require_login(request)
        if redirect:
            return redirect

        grants = _load_grants()
        meta = app.state.meta.all()
        rows = []
        for grant in grants:
            if not show_revoked and grant.get("revoked"):
                continue
            key_id = grant.get("key_id", "?")
            rows.append(
                {
                    "grant": grant,
                    "status": _grant_status(grant),
                    "created_by": (meta.get(key_id) or {}).get("created_by") or "—",
                }
            )
        rows.sort(key=lambda r: r["grant"].get("created_at") or "", reverse=True)

        return render(
            request,
            "grants_list.html",
            rows=rows,
            show_revoked=bool(show_revoked),
            total=len(grants),
            readyz=probe.readyz_status(settings.mcp_readyz_url),
        )

    @app.get("/grants/new", response_class=HTMLResponse, include_in_schema=False)
    def grants_new(request: Request):
        identity, redirect = _require_login(request)
        if redirect:
            return redirect
        return render(
            request,
            "grants_new.html",
            errors=[],
            form={},
            server_methods=sorted(bmya_auth.server_allowed_methods()),
        )

    @app.post("/grants", response_class=HTMLResponse, include_in_schema=False)
    async def grants_create(request: Request):
        """Mint a key and show it exactly once.

        Order matters and is load-bearing:
          validate -> mint -> assemble -> re-parse with the server's own parser
          -> write the registry under the lock -> write the meta -> render.

        If the registry write fails the key is never rendered. Showing a key the
        server cannot validate is worse than showing nothing at all.
        """
        form = await request.form()
        identity, refusal = _guard_mutation(request, form.get("csrf_token"))
        if refusal:
            return refusal

        entry, errors = forms.parse_grant_form(form)
        server_methods = sorted(bmya_auth.server_allowed_methods())

        def _back(errs):
            response = render(
                request,
                "grants_new.html",
                errors=errs,
                form=dict(form),
                server_methods=server_methods,
            )
            response.status_code = 400
            return response

        if errors:
            return _back(errors)

        plaintext, key_id = bmya_auth.generate_key(entry["mode"])
        entry = dict(entry)
        entry["key_id"] = key_id
        entry["key_sha256"] = bmya_auth.hash_key(plaintext)
        entry["created_at"] = bmya_registry.now_iso()

        errors = forms.validate_against_server_parser(entry)
        if errors:
            return _back(errors)

        class _Duplicate(Exception):
            pass

        def _append(data):
            if any(g.get("key_id") == key_id for g in data["grants"]):
                raise _Duplicate()
            data["grants"].append(entry)

        try:
            bmya_registry.mutate_registry(settings.registry_file, _append, allow_missing=True)
        except _Duplicate:
            return _back(["Colisión de key_id, volvé a intentar."])
        except bmya_registry.RegistryFileError as exc:
            logger.error("No se pudo escribir el registro: %s", exc)
            return _back([f"No se pudo escribir el registro: {exc}"])

        # Best effort by construction: the key exists and must be listable even
        # if this fails. A missing created_by is a degraded audit record; a key
        # in no list at all would be an orphan.
        try:
            doc = new_key_meta(
                created_by=identity.email,
                created_from_ip=request.client.host if request.client else "",
            )
            doc["history"].append(history_entry(identity.email, "create"))
            app.state.meta.put(key_id, doc)
        except Exception as exc:  # noqa: BLE001
            logger.error("Grant %s creado pero no se pudo escribir la metadata: %s", key_id, exc)

        _audit(identity.email, "create", key_id=key_id, database=entry["database"])

        slug = bmya_snippets.slugify(
            form.get("client_name") or entry["label"],
            f"odoo-{entry['database']}-{entry['mode']}",
        )
        snippets = (
            bmya_snippets.render_client_snippets(
                name=slug, server_url=settings.mcp_server_url, bmya_key=plaintext
            )
            if settings.mcp_server_url
            else ""
        )

        response = render(
            request,
            "grants_show_once.html",
            plaintext=plaintext,
            entry=entry,
            snippets=snippets,
            method_warnings=forms.method_warnings(entry.get("allowed_methods")),
        )
        # This is the one response in the app that carries a secret. Keep it out
        # of every cache between here and the operator's screen.
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, private"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        return response

    @app.get("/grants/{key_id}", response_class=HTMLResponse, include_in_schema=False)
    def grant_detail(request: Request, key_id: str):
        identity, redirect = _require_login(request)
        if redirect:
            return redirect

        grant = next((g for g in _load_grants() if g.get("key_id") == key_id), None)
        if grant is None:
            response = render(request, "error.html", message=f"No existe la key {key_id}.")
            response.status_code = 404
            return response

        return render(
            request,
            "grant_detail.html",
            grant=grant,
            status=_grant_status(grant),
            meta=app.state.meta.get(key_id),
            method_warnings=forms.method_warnings(grant.get("allowed_methods")),
        )

    @app.post("/grants/{key_id}/revoke", include_in_schema=False)
    async def grant_revoke(request: Request, key_id: str):
        """Revoke = revoked: true. The record stays; there is no delete, ever."""
        form = await request.form()
        identity, refusal = _guard_mutation(request, form.get("csrf_token"))
        if refusal:
            return refusal

        class _NotFound(Exception):
            pass

        def _revoke(data):
            matches = [g for g in data["grants"] if g.get("key_id") == key_id]
            if not matches:
                raise _NotFound()
            for grant in matches:
                grant["revoked"] = True
                grant["revoked_at"] = bmya_registry.now_iso()

        try:
            bmya_registry.mutate_registry(settings.registry_file, _revoke)
        except _NotFound:
            return JSONResponse({"error": "not found"}, status_code=404)
        except bmya_registry.RegistryFileError as exc:
            logger.error("No se pudo revocar %s: %s", key_id, exc)
            return JSONResponse({"error": str(exc)}, status_code=500)

        try:
            app.state.meta.append_history(key_id, identity.email, "revoke")
        except Exception as exc:  # noqa: BLE001
            logger.error("Revocado %s pero no se pudo escribir la metadata: %s", key_id, exc)

        _audit(identity.email, "revoke", key_id=key_id)
        return RedirectResponse(f"/grants/{key_id}", status_code=303)

    #: Changing any of these repoints a credential the holder already has, and a
    #: readonly -> readwrite flip would silently escalate a key already sitting
    #: in a client's config. To change one: revoke and mint.
    IMMUTABLE_FIELDS = ("key_id", "key_sha256", "odoo_url", "database", "mode")

    @app.post("/grants/{key_id}/edit", include_in_schema=False)
    async def grant_edit(request: Request, key_id: str):
        form = await request.form()
        identity, refusal = _guard_mutation(request, form.get("csrf_token"))
        if refusal:
            return refusal

        # Rejected, not ignored: silently dropping them would leave the operator
        # believing a change took effect.
        attempted = [f for f in IMMUTABLE_FIELDS if f in form]
        if attempted:
            return JSONResponse(
                {
                    "error": "immutable fields",
                    "fields": attempted,
                    "detail": "Revocá la key y emití una nueva.",
                },
                status_code=400,
            )

        try:
            expires_at = bmya_snippets.parse_expiry((form.get("expires_at") or "").strip())
        except ValueError as exc:
            return JSONResponse({"error": f"expires_at: {exc}"}, status_code=400)

        odoo_api = (form.get("odoo_api") or bmya_auth.ODOO_API_AUTO).strip()
        if odoo_api not in bmya_auth.ODOO_APIS:
            return JSONResponse({"error": f"odoo_api: {odoo_api!r}"}, status_code=400)

        class _NotFound(Exception):
            pass

        def _edit(data):
            matches = [g for g in data["grants"] if g.get("key_id") == key_id]
            if not matches:
                raise _NotFound()
            for grant in matches:
                grant["label"] = (form.get("label") or "").strip()
                grant["notes"] = (form.get("notes") or "").strip()
                grant["expires_at"] = expires_at
                # Editable, unlike the URL: the login only has to match the
                # owner of the API key the client already sends, so changing it
                # cannot widen what the key reaches.
                if "odoo_login" in form:
                    grant["odoo_login"] = (form.get("odoo_login") or "").strip()
                if "odoo_api" in form:
                    grant["odoo_api"] = odoo_api

        try:
            bmya_registry.mutate_registry(settings.registry_file, _edit)
        except _NotFound:
            return JSONResponse({"error": "not found"}, status_code=404)
        except bmya_registry.RegistryFileError as exc:
            return JSONResponse({"error": str(exc)}, status_code=500)

        try:
            app.state.meta.append_history(key_id, identity.email, "edit")
        except Exception as exc:  # noqa: BLE001
            logger.error("Editado %s pero no se pudo escribir la metadata: %s", key_id, exc)

        _audit(identity.email, "edit", key_id=key_id)
        return RedirectResponse(f"/grants/{key_id}", status_code=303)

    @app.post("/grants/probe", include_in_schema=False)
    async def grants_probe(request: Request):
        """Advisory reachability check. Never gates a mint."""
        form = await request.form()
        identity, redirect = _require_login(request)
        if redirect:
            return redirect
        if not console_auth.csrf_ok(request, form.get("csrf_token")):
            return JSONResponse({"error": "csrf"}, status_code=403)
        if not settings.probe_enabled:
            return JSONResponse({"verdict": "disabled"})

        # Only ever probe a URL validate_odoo_url has already accepted: this is
        # an outbound request to an operator-supplied address, and that function
        # is what rejects loopback/private literals and enforces the suffix
        # allowlist.
        try:
            url = bmya_auth.validate_odoo_url(form.get("odoo_url"))
        except ValueError as exc:
            return JSONResponse({"verdict": "invalid_url", "detail": str(exc)}, status_code=400)

        database = (form.get("database") or "").strip()
        if not database:
            return JSONResponse(
                {"verdict": "invalid_url", "detail": "falta la base"}, status_code=400
            )

        result = probe.probe_grant(url, database, timeout=settings.probe_timeout)
        return JSONResponse(
            {"verdict": result.verdict, "detail": result.detail, "version": result.version}
        )

    # --- Usage

    @app.get("/usage", response_class=HTMLResponse, include_in_schema=False)
    def usage_view(request: Request, days: int = 30):
        """What each key actually did.

        Read-only over the journal the MCP server appends to. It bills nothing:
        it exists so that a price per tool call can eventually be chosen from
        real traffic, which is the one input that cannot be reconstructed later.
        """
        identity, redirect = _require_login(request)
        if redirect:
            return redirect

        days = max(1, min(int(days or 30), 365))
        summary = usage.summarize(settings.usage_dir, days=days)
        labels = {g.get("key_id"): g.get("label") or "" for g in _load_grants()}
        return render(request, "usage.html", summary=summary, labels=labels, days=days)

    @app.get("/", include_in_schema=False)
    def index(request: Request):
        return RedirectResponse("/grants" if console_auth.current_identity(request) else "/login")

    return app
