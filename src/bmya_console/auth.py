"""
Who is using the console, and the CSRF token that protects its forms.

**Why a per-operator key and not Google.** The console is published on the VLAN
IP only, and Google's OAuth client configuration refuses a redirect URI that is
either a raw IP address or plain ``http://`` on anything but ``localhost`` -- so
``http://10.0.0.14:8081/auth/callback`` cannot be registered at all. Rather than
expose the console publicly just to satisfy the identity provider, it
authenticates with a key per person.

Per *person*, not one shared key for the team: the credential is what fills
``created_by`` in the meta store, and "somebody at BMYA minted this" is not an
audit trail. The cost is identical.

**The seam for Google.** Everything outside this module deals in
:class:`Identity` and calls :func:`authenticate`. When the console moves behind
Traefik with a real hostname, an ``oidc.py`` implementing the same signature
replaces the body of ``authenticate`` and nothing in ``app.py`` or the templates
changes. Do not let the operator-key details leak past this module.

Credentials are stored as sha256 only, reusing ``bmya_auth.hash_key`` -- the same
discipline, the same primitive and the same constant-time comparison as the API
key registry, with no new dependency.
"""

import hmac
import logging
import secrets
from dataclasses import dataclass
from typing import Optional

import bmya_auth

logger = logging.getLogger("bmya-console.auth")

SESSION_EMAIL = "operator_email"
SESSION_CSRF = "csrf"

#: One message for every failure -- unknown operator, wrong key, malformed
#: input. The login form must not tell an attacker which half they got right.
PUBLIC_LOGIN_ERROR = "Correo o clave incorrectos."


@dataclass(frozen=True)
class Identity:
    """An authenticated console operator. The only thing routes should see."""

    email: str


def generate_operator_key() -> str:
    """Mint a console credential. Returned once, stored only as sha256.

    Reuses ``secrets.token_urlsafe(32)`` -- 256 bits, the same entropy budget
    that makes unsalted sha256 the right digest for the API keys.
    """
    return f"bmyacon_{secrets.token_urlsafe(32)}"


def authenticate(email: str, key: str, operators: dict) -> Optional[Identity]:
    """Resolve an operator from a login form, or None.

    ``operators`` maps a lowercased email to the sha256 of its key.

    The comparison is constant-time and, when the email is unknown, runs against
    a dummy digest so that a wrong email and a wrong key take the same work and
    the same path. Returning early on an unknown email would leak the operator
    list one guess at a time.
    """
    email = (email or "").strip().lower()
    key = (key or "").strip()
    if not email or not key:
        return None

    expected = operators.get(email)
    candidate = bmya_auth.hash_key(key)
    # 64 hex chars that no key hashes to: the shape has to match for
    # compare_digest, the value must never match.
    reference = expected if expected is not None else "0" * 64

    if not hmac.compare_digest(candidate, reference) or expected is None:
        return None
    return Identity(email=email)


def current_identity(request) -> Optional[Identity]:
    """The logged-in operator for this request, or None.

    The identity comes from the signed session cookie and nowhere else -- never
    from a header, never from a query string, never from module state shared
    between requests.
    """
    email = request.session.get(SESSION_EMAIL)
    if not email:
        return None
    return Identity(email=email)


def login_session(request, identity: Identity) -> None:
    """Start a session, replacing whatever was there.

    ``clear()`` first so a fresh login always mints a new CSRF token: reusing
    the previous one would let a token captured before login stay valid after.
    """
    request.session.clear()
    request.session[SESSION_EMAIL] = identity.email
    request.session[SESSION_CSRF] = secrets.token_urlsafe(32)


def logout_session(request) -> None:
    request.session.clear()


def csrf_token(request) -> str:
    """The session's CSRF token, minting one if the session has none."""
    token = request.session.get(SESSION_CSRF)
    if not token:
        token = secrets.token_urlsafe(32)
        request.session[SESSION_CSRF] = token
    return token


def csrf_ok(request, submitted: Optional[str]) -> bool:
    """Constant-time check of a submitted CSRF token against the session's.

    ``SameSite=Lax`` already withholds the cookie from cross-site POSTs and is
    the main defense; this is the second lock, because revoking a client's
    access is a single destructive click.
    """
    expected = request.session.get(SESSION_CSRF)
    if not expected or not submitted:
        return False
    return hmac.compare_digest(str(submitted), str(expected))
