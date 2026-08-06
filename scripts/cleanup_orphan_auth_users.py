"""Find (and optionally delete) Firebase Auth accounts with no Firestore user.

Deleting a user used to remove only the Firestore document, leaving the
Firebase Auth record behind. Those orphans still own their email address, so
signing up again with it fails with "email already exists". This script finds
them and can clear them.

Dry run by default — it prints what it would delete and changes nothing:

    python scripts/cleanup_orphan_auth_users.py

Delete them for real (irreversible):

    python scripts/cleanup_orphan_auth_users.py --delete

Safety rails, because this deletes real accounts:
  * an account is only an orphan when Firestore has no document for its uid
    *and* no user document records it as their firebase_uid;
  * anything created in the last --min-age-hours (default 24) is skipped, so a
    sign-up mid-flight is never caught;
  * --keep protects specific emails or uids.
"""
from __future__ import annotations

import argparse
import datetime as dt
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


def firestore_identity(db: firestore.Client) -> tuple[set[str], set[str]]:
    """(document ids, firebase_uid values) for every user document."""
    ids: set[str] = set()
    uids: set[str] = set()
    for doc in db.collection("users").stream():
        ids.add(doc.id)
        data = doc.to_dict() or {}
        if data.get("firebase_uid"):
            uids.add(str(data["firebase_uid"]))
    return ids, uids


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--delete", action="store_true",
                    help="actually delete (default is a dry run)")
    ap.add_argument("--credentials", default=str(DEFAULT_CRED))
    ap.add_argument("--min-age-hours", type=float, default=24.0,
                    help="skip accounts newer than this (default 24)")
    ap.add_argument("--keep", nargs="*", default=[],
                    help="emails or uids to protect")
    args = ap.parse_args()

    db = init(Path(args.credentials))
    doc_ids, linked_uids = firestore_identity(db)
    known = doc_ids | linked_uids
    keep = {k.lower() for k in args.keep}
    now = dt.datetime.now(dt.timezone.utc)

    orphans, skipped_young, total = [], [], 0
    page = fb_auth.list_users()
    while page:
        for u in page.users:
            total += 1
            if u.uid in known:
                continue
            if u.uid.lower() in keep or (u.email or "").lower() in keep:
                continue

            created_ms = getattr(u.user_metadata, "creation_timestamp", None)
            age_h = None
            if created_ms:
                created = dt.datetime.fromtimestamp(created_ms / 1000, dt.timezone.utc)
                age_h = (now - created).total_seconds() / 3600
                if age_h < args.min_age_hours:
                    skipped_young.append((u.email or "(no email)", round(age_h, 1)))
                    continue
            orphans.append((u.uid, u.email or "(no email)", age_h))
        page = page.get_next_page()

    print(f"Firebase Auth accounts : {total}")
    print(f"Firestore user docs    : {len(doc_ids)}")
    print(f"Orphans (no Firestore) : {len(orphans)}")
    if skipped_young:
        print(f"Skipped as too new     : {len(skipped_young)} "
              f"(under {args.min_age_hours}h)")
        for email, age in skipped_young:
            print(f"    {email}  {age}h old")

    if not orphans:
        print("\nNothing to clean up.")
        return

    print()
    for uid, email, age in orphans:
        age_txt = f"{age / 24:.0f}d old" if age else "age unknown"
        print(f"  {email:44s} {uid}  ({age_txt})")

    if not args.delete:
        print("\nDry run — nothing deleted. Re-run with --delete to remove these.")
        return

    print(f"\nDeleting {len(orphans)} accounts…")
    ok = 0
    for uid, email, _ in orphans:
        try:
            fb_auth.delete_user(uid)
            ok += 1
            print(f"  deleted {email}")
        except Exception as exc:
            print(f"  FAILED  {email}: {exc}")
    print(f"\n{ok} of {len(orphans)} deleted. Those emails can sign up again.")


if __name__ == "__main__":
    main()
