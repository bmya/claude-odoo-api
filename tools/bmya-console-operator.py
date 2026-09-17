#!/usr/bin/env python3
"""
Mint a console operator credential.

The console authenticates with one key per person, configured as
``BMYA_CONSOLE_OPERATORS="email:sha256hex,email:sha256hex"``. Only the digest is
ever stored, so a leaked environment cannot be used to log in -- the same
discipline the API key registry uses.

Per *person*, not one shared key for the team: the credential is what fills
``created_by``, and "somebody at BMYA minted this key" is not an audit trail.

Usage:
    bmya-console-operator.py daniel@bmya.cl

Prints the key once (it is not recoverable) and the line to add to
BMYA_CONSOLE_OPERATORS. Standard library only, like tools/bmya-keys.py.
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

import bmya_auth  # noqa: E402
from bmya_console.auth import generate_operator_key  # noqa: E402


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[1])
    parser.add_argument("email", help="the operator's address, e.g. daniel@bmya.cl")
    parser.add_argument(
        "--existing",
        default="",
        help="the current BMYA_CONSOLE_OPERATORS value, to print the merged result",
    )
    args = parser.parse_args(argv)

    email = args.email.strip().lower()
    if "@" not in email:
        print(f"error: {args.email!r} no parece un correo", file=sys.stderr)
        return 1

    key = generate_operator_key()
    digest = bmya_auth.hash_key(key)
    entry = f"{email}:{digest}"

    bar = "=" * 72
    print(bar)
    print("Clave de consola (se muestra una sola vez, no se puede recuperar):")
    print()
    print(f"    {key}")
    print()
    print(f"  operador: {email}")
    print(bar)
    print()
    print("Agregá esta entrada a BMYA_CONSOLE_OPERATORS en deploy/.env:")
    print()
    if args.existing.strip():
        merged = [e.strip() for e in args.existing.split(",") if e.strip()]
        merged = [e for e in merged if not e.lower().startswith(f"{email}:")]
        merged.append(entry)
        print(f"    BMYA_CONSOLE_OPERATORS={','.join(merged)}")
    else:
        print(f"    BMYA_CONSOLE_OPERATORS={entry}")
    print()
    print(
        "warning: guardá la clave ahora; el servidor sólo conoce su sha256",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
