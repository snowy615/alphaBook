"""Copy email addresses from Firebase Auth into the Firestore user documents.

The recruiter directory shows `users.email`, but that field only started being
written once the directory existed, and older documents are backfilled lazily
on the owner's next sign-in. Anyone who has not signed in since then appears in
the directory with no way to contact them — which is the one thing the
directory is for.

Firebase Auth has the address all along, so this copies it across.

Dry run by default:

    python scripts/backfill_user_emails.py

Write the changes:

    python scripts/backfill_user_emails.py --write

Only fills blanks. An address already in Firestore is never overwritten, so a
user who changed it in their profile keeps their choice.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import firebase_admin                                            # noqa: E402
from firebase_admin import auth as fb_auth, credentials          # noqa: E402
from google.cloud import firestore                               # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CRED = REPO_ROOT / "service-account.json"


def init(cred_path: Path) -> firestore.Client:
    if not cred_path.exists():
        raise SystemExit(f"No credentials at {cred_path}")
    if not firebase_admin._apps:
        firebase_admin.initialize_app(credentials.Certificate(str(cred_path)))
    os.environ.setdefault("GOOGLE_APPLICATION_CREDENTIALS", str(cred_path))
    return firestore.Client.from_service_account_json(str(cred_path))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--write", action="store_true", help="apply (default is a dry run)")
    ap.add_argument("--credentials", default=str(DEFAULT_CRED))
    args = ap.parse_args()

    db = init(Path(args.credentials))

    filled, already, missing = [], 0, []
    for doc in db.collection("users").stream():
        data = doc.to_dict() or {}
        if data.get("email"):
            already += 1
            continue

        uid = data.get("firebase_uid") or doc.id
        try:
            email = fb_auth.get_user(str(uid)).email
        except Exception:
            email = None

        if not email:
            missing.append((doc.id, data.get("username") or "(no username)"))
            continue

        filled.append((doc.id, data.get("username") or "(no username)", email))
        if args.write:
            doc.reference.update({"email": email})

    print(f"Already had an email : {already}")
    print(f"Can be filled in     : {len(filled)}")
    print(f"No address anywhere  : {len(missing)}")

    if filled:
        print()
        for _uid, username, email in filled:
            print(f"  {username:20s} -> {email}")

    if missing:
        print("\nNo Firebase address (local-only accounts):")
        for _uid, username in missing:
            print(f"  {username}")

    if filled and not args.write:
        print("\nDry run — nothing written. Re-run with --write to apply.")
    elif filled:
        print(f"\nWrote {len(filled)} addresses. The directory can contact them now.")


if __name__ == "__main__":
    main()
