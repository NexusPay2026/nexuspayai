#!/usr/bin/env python3
"""
Generate the bcrypt hash for the CMS console admin password.

WHY THIS EXISTS
---------------
The console's password must never exist in plaintext anywhere: not in code, not
in a config file, not in a commit, not in a log, and not in a network request.
The ONLY thing that ever leaves this machine is the hash, which you paste into
the Render environment variable CMS_ADMIN_PASSWORD_HASH.

USAGE
-----
    python scripts/make_admin_hash.py

You will be prompted for the password twice. The input is NOT echoed to the
terminal (getpass), so it does not appear on screen or in your shell history.
The script makes no network calls and writes no files.

Then, in the Render dashboard for nexuspay-api-ochi:
    CMS_ADMIN_USERNAME       = <the username you chose>
    CMS_ADMIN_PASSWORD_HASH  = <the $2b$... string this prints>

SECURITY NOTES
--------------
* Do NOT pass the password as a command-line argument — arguments are visible
  to other processes and land in shell history. This script deliberately has no
  --password flag.
* Do NOT paste the plaintext into chat, a ticket, or a commit.
* Rotating the password = re-run this script, update the Render env var, save.
  Render restarts the service and the new hash takes effect. Existing console
  sessions keep working until their JWT expires; to kill them immediately,
  also rotate CMS_JWT_SECRET.
"""

import getpass
import sys

# 12 rounds ~= 250ms per verification on typical hardware: slow enough to make
# offline cracking expensive, fast enough for an interactive login.
BCRYPT_ROUNDS = 12

MIN_LENGTH = 12


def check_strength(password: str) -> list:
    """Return a list of unmet requirements (empty means the password is fine)."""
    problems = []
    if len(password) < MIN_LENGTH:
        problems.append(f"at least {MIN_LENGTH} characters")
    if not any(c.islower() for c in password):
        problems.append("a lower-case letter")
    if not any(c.isupper() for c in password):
        problems.append("an upper-case letter")
    if not any(c.isdigit() for c in password):
        problems.append("a number")
    if all(c.isalnum() for c in password):
        problems.append("a symbol")
    return problems


def main() -> int:
    try:
        import bcrypt
    except ImportError:
        print(
            "bcrypt is not installed.\n"
            "  pip install bcrypt\n"
            "(or: pip install -r requirements.txt)",
            file=sys.stderr,
        )
        return 1

    print("CMS console — admin password hash generator")
    print("-" * 44)
    print("The password is not echoed, not logged, and never leaves this machine.")
    print()

    try:
        password = getpass.getpass("New password: ")
        confirm = getpass.getpass("Confirm password: ")
    except (KeyboardInterrupt, EOFError):
        print("\nCancelled.", file=sys.stderr)
        return 1

    if not password:
        print("Empty password — nothing to do.", file=sys.stderr)
        return 1

    if password != confirm:
        print("The two entries do not match. Nothing was generated.", file=sys.stderr)
        return 1

    problems = check_strength(password)
    if problems:
        print(f"\nWeak password — it needs {', '.join(problems)}.", file=sys.stderr)
        print("Nothing was generated. Re-run with a stronger password.", file=sys.stderr)
        return 1

    hashed = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=BCRYPT_ROUNDS))

    # Drop the plaintext from memory as soon as it is no longer needed. (Python
    # strings are immutable so this is best-effort, not a guarantee.)
    del password, confirm

    print()
    print("Set this in Render (nexuspay-api-ochi -> Environment):")
    print()
    print("  CMS_ADMIN_PASSWORD_HASH=" + hashed.decode("ascii"))
    print()
    print("Do not commit this value and do not paste the plaintext anywhere.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
