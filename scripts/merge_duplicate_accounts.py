"""Merge one person's duplicate AlphaBook accounts into a single account.

Some people made two accounts (a Gmail and an Oxford sign-up, say), and
reached their emails or their application through the wrong one. Merging
points the extra account at the main one: its user document gets
``merged_into`` = the main account's id, and from then on every way of
signing in (password, Google, an emailed link, an existing session) lands in
the main account. Both logins keep working; no account is deleted or created.

Input is a JSON file listing each person's accounts by email, and optionally
which one is the main account:

    [{"emails": ["sherrytsai1218@gmail.com", "x.mfe26@said.oxford.edu"]},
     {"emails": ["a@gmail.com", "a@college.ox.ac.uk"], "main": "a@gmail.com"}]

Without "main", the account holding the furthest-along application is the
main one; a person with two real applications must be given a "main".

Dry run by default: it prints exactly what it would do and changes nothing.

    python scripts/merge_duplicate_accounts.py groups.json
    python scripts/merge_duplicate_accounts.py groups.json --apply

What a merge does, per extra account:
  * the main account keeps its own application. An extra account's
    application moves to the main account if the main one has none;
    otherwise it is archived in ``merged_applications`` (not deleted), so
    there is only ever one live application per person;
  * an outreach event sign-up moves to the main account if it has none;
  * the main account gains the extra account's email under ``other_emails``,
    plus its CV / name / graduation year where the main one has none;
  * the extra account's user document is marked ``merged_into``.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import firebase_admin                                            # noqa: E402
from firebase_admin import auth as fb_auth, credentials          # noqa: E402
from google.cloud import firestore                               # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CRED = REPO_ROOT / "service-account.json"
OUTREACH_EVENT_ID = "quant-outreach"
# How far along an application is, for choosing the main account.
PROGRESS = {"cv": 1, "oa_ready": 2, "oa_active": 3, "submitted": 4, "shortlisted": 5, "rejected": 5, "accepted": 6}
PROFILE_FIELDS = ("cv_blob_path", "cv_filename", "cv_uploaded_at", "full_name", "graduation_year")


def init(cred_path: Path) -> firestore.Client:
    if not cred_path.exists():
        raise SystemExit(f"No credentials at {cred_path}")
    if not firebase_admin._apps:
        firebase_admin.initialize_app(credentials.Certificate(str(cred_path)))
    return firestore.Client.from_service_account_json(str(cred_path))


def load_group(db: firestore.Client, group: dict) -> list:
    accounts = []
    for email in group["emails"]:
        try:
            uid = fb_auth.get_user_by_email(email).uid
        except fb_auth.UserNotFoundError:
            raise SystemExit(f"No account for {email}")
        user = db.collection("users").document(uid).get()
        app = db.collection("applications").document(uid).get()
        accounts.append({"email": email, "uid": uid, "user": user.to_dict() if user.exists else None,
                         "app": app.to_dict() if app.exists else None})
    return accounts


def choose_main(accounts: list, main_email: str | None) -> dict:
    if main_email:
        return next(a for a in accounts if a["email"].lower() == main_email.lower())
    with_apps = [a for a in accounts if a["app"]]
    if len(with_apps) > 1:
        a, b = sorted(with_apps, key=lambda a: PROGRESS.get(a["app"].get("status"), 0), reverse=True)[:2]
        if PROGRESS.get(a["app"].get("status"), 0) == PROGRESS.get(b["app"].get("status"), 0) \
                or b["app"].get("status") not in ("cv",):
            raise SystemExit(f"{[x['email'] for x in accounts]} have two real applications; say which is \"main\"")
        return a
    if with_apps:
        return with_apps[0]
    oxford = [a for a in accounts if a["email"].lower().endswith("ox.ac.uk")]
    return (oxford or accounts)[0]


def plan(db: firestore.Client, group: dict) -> list:
    accounts = load_group(db, group)
    main = choose_main(accounts, group.get("main"))
    if (main["user"] or {}).get("merged_into"):
        raise SystemExit(f"{main['email']} is itself merged into another account")
    steps = []
    main_has_app = bool(main["app"])
    main_signup = db.collection("event_signups").document(f"{OUTREACH_EVENT_ID}_{main['uid']}").get().exists
    for extra in accounts:
        if extra is main:
            continue
        if (extra["user"] or {}).get("merged_into") == main["uid"]:
            steps.append((f"{extra['email']} already merged into {main['email']}", None))
            continue
        if extra["app"]:
            if main_has_app:
                steps.append((f"archive {extra['email']}'s {extra['app'].get('programme')} application "
                               f"({extra['app'].get('status')})", ("archive_app", extra, main)))
            else:
                steps.append((f"move {extra['email']}'s {extra['app'].get('programme')} application "
                              f"({extra['app'].get('status')}) to {main['email']}", ("move_app", extra, main)))
                main_has_app = True
        signup = db.collection("event_signups").document(f"{OUTREACH_EVENT_ID}_{extra['uid']}").get()
        if signup.exists and not main_signup:
            steps.append((f"move {extra['email']}'s outreach sign-up to {main['email']}", ("move_signup", extra, main)))
            main_signup = True
        steps.append((f"merge {extra['email']} into {main['email']}", ("merge", extra, main)))
    return [main, steps]


def run(db: firestore.Client, step: tuple) -> None:
    kind, extra, main = step
    now = dt.datetime.now(dt.timezone.utc)
    apps = db.collection("applications")
    if kind == "archive_app":
        db.collection("merged_applications").document(extra["uid"]).set(
            {**extra["app"], "merged_into": main["uid"], "archived_at": now})
        apps.document(extra["uid"]).delete()
    elif kind == "move_app":
        apps.document(main["uid"]).set({**extra["app"], "user_id": main["uid"], "moved_from": extra["uid"],
                                        "moved_at": now})
        apps.document(extra["uid"]).delete()
    elif kind == "move_signup":
        ref = db.collection("event_signups").document(f"{OUTREACH_EVENT_ID}_{extra['uid']}")
        data = ref.get().to_dict() or {}
        db.collection("event_signups").document(f"{OUTREACH_EVENT_ID}_{main['uid']}").set(
            {**data, "user_id": main["uid"], "moved_from": extra["uid"]})
        ref.delete()
    elif kind == "merge":
        main_ref = db.collection("users").document(main["uid"])
        main_user = main_ref.get().to_dict() or {}
        patch = {"other_emails": firestore.ArrayUnion([extra["email"].lower()])}
        for field in PROFILE_FIELDS:
            if not main_user.get(field) and (extra["user"] or {}).get(field):
                patch[field] = extra["user"][field]
        main_ref.set(patch, merge=True)
        db.collection("users").document(extra["uid"]).set(
            {"merged_into": main["uid"], "merged_at": now, "email": extra["email"]}, merge=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("groups", type=Path)
    parser.add_argument("--apply", action="store_true", help="make the changes (default: dry run)")
    parser.add_argument("--cred", type=Path, default=DEFAULT_CRED)
    args = parser.parse_args()
    db = init(args.cred)
    groups = json.loads(args.groups.read_text())
    plans = [plan(db, g) for g in groups]          # every group checked before anything changes
    for main_acct, steps in plans:
        print(f"\nMain account: {main_acct['email']}"
              f"{' (' + main_acct['app'].get('status') + ', ' + main_acct['app'].get('programme', '') + ')' if main_acct['app'] else ''}")
        for text, step in steps:
            print(f"  {'DONE ' if args.apply and step else ''}{text}")
            if args.apply and step:
                run(db, step)
    print(f"\n{len(plans)} people. {'Applied.' if args.apply else 'Dry run: nothing changed. Re-run with --apply.'}")


if __name__ == "__main__":
    main()
