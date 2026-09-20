"""Tests for app.events — admin-created events with sign-up.

Pinned here: the London-time conversion, validation, the two sign-up modes
(first come first served confirms straight away; approval files a request),
capacity being a hard stop on confirmed places only, who sees the attendance
numbers, and deleting an event taking its sign-ups with it. Endpoint
functions are called directly over an in-memory Firestore stand-in, so the
FastAPI dependencies (admin / reviewer checks) aren't what's under test here.
"""
import asyncio
import datetime as dt

import pytest
from fastapi import HTTPException

from app import events, outreach
from app import membership as mb
from app.models import User


# ── A small async Firestore stand-in that honours equality where() ──────────
class _Ref:
    def __init__(self, store, coll, doc_id):
        self._store, self._coll, self._id = store, coll, doc_id

    async def get(self):
        return _Doc(self._store, self._coll, self._id)

    async def set(self, data):
        self._store.setdefault(self._coll, {})[self._id] = dict(data)

    async def update(self, patch):
        self._store.setdefault(self._coll, {}).setdefault(self._id, {}).update(patch)

    async def delete(self):
        self._store.get(self._coll, {}).pop(self._id, None)


class _Doc:
    def __init__(self, store, coll, doc_id):
        self._data = store.get(coll, {}).get(doc_id)
        self.id = doc_id
        self.exists = self._data is not None
        self.reference = _Ref(store, coll, doc_id)

    def to_dict(self):
        return dict(self._data) if self._data is not None else None


class _Query:
    def __init__(self, docs):
        self._docs = docs

    async def get(self):
        return self._docs


class _Coll:
    def __init__(self, store, name):
        self._store, self._name = store, name

    def document(self, doc_id):
        return _Ref(self._store, self._name, doc_id)

    async def get(self):
        return [_Doc(self._store, self._name, k) for k in list(self._store.get(self._name, {}))]

    def where(self, field, _op, value):
        rows = self._store.get(self._name, {})
        return _Query([_Doc(self._store, self._name, k) for k, v in list(rows.items()) if v.get(field) == value])


class _DB:
    def __init__(self, store):
        self._store = store

    def collection(self, name):
        return _Coll(self._store, name)


@pytest.fixture
def store(monkeypatch):
    s = {"users": {
        "u1": {"username": "jo", "full_name": "Jo Bloggs", "email": "jo@ox.ac.uk"},
        "u2": {"username": "sam", "full_name": "Sam Lee", "email": "sam@ox.ac.uk"},
        "u3": {"username": "kit", "full_name": "Kit Doe", "email": "kit@ox.ac.uk"},
        "qa": {"username": "priya", "membership": mb.M_QUANT_ANALYST},
    }}
    monkeypatch.setattr(events.db_module, "db", _DB(s))
    return s


def run(coro):
    return asyncio.run(coro)


ADMIN = User(id="admin1", username="root", is_admin=True)
JO = User(id="u1", username="jo")
SAM = User(id="u2", username="sam")
KIT = User(id="u3", username="kit")
ANALYST = User(id="qa", username="priya")


def _payload(**over):
    base = dict(title="Kick-off", description="Come along", date="2099-10-05",
                start_time="18:00", end_time="19:30")
    base.update(over)
    return events.EventPayload(**base)


def _create(**over):
    return run(events.create_event(_payload(**over), ADMIN))["id"]


class TestCreateAndValidate:
    def test_london_times_are_stored_as_utc(self, store):
        # 5 Oct 2099 is British Summer Time (UTC+1), so 18:00 London is 17:00 UTC.
        eid = _create()
        doc = store["events"][eid]
        assert doc["starts_at"] == dt.datetime(2099, 10, 5, 17, 0, tzinfo=dt.timezone.utc)
        assert doc["ends_at"] == dt.datetime(2099, 10, 5, 18, 30, tzinfo=dt.timezone.utc)
        assert doc["created_by"] == "root"

    @pytest.mark.parametrize("over", [
        {"title": "   "},
        {"date": "05/10/2099"},
        {"start_time": "6pm"},
        {"end_time": "17:00"},          # before the 18:00 start
        {"end_time": "18:00"},          # not after it
        {"signup_mode": "whenever"},
        {"capacity": 0},
    ])
    def test_bad_input_is_refused(self, store, over):
        with pytest.raises(HTTPException) as exc:
            _create(**over)
        assert exc.value.status_code == 400
        assert not store.get("events")

    def test_blank_capacity_means_no_limit(self, store):
        eid = _create(capacity=None)
        assert store["events"][eid]["capacity"] is None

    def test_update_changes_the_event_but_not_who_made_it(self, store):
        eid = _create()
        run(events.update_event(eid, _payload(title="Renamed", capacity=10), ADMIN))
        assert store["events"][eid]["title"] == "Renamed"
        assert store["events"][eid]["capacity"] == 10
        assert store["events"][eid]["created_by"] == "root"

    def test_editing_a_missing_event_is_a_404(self, store):
        with pytest.raises(HTTPException) as exc:
            run(events.update_event("nope", _payload(), ADMIN))
        assert exc.value.status_code == 404


class TestFirstComeFirstServed:
    def test_signing_up_confirms_straight_away(self, store):
        eid = _create()
        assert run(events.sign_up(eid, JO))["status"] == "confirmed"
        assert store["event_signups"][f"{eid}_u1"]["email"] == "jo@ox.ac.uk"

    def test_cannot_sign_up_twice(self, store):
        eid = _create()
        run(events.sign_up(eid, JO))
        with pytest.raises(HTTPException) as exc:
            run(events.sign_up(eid, JO))
        assert exc.value.status_code == 400

    def test_capacity_is_a_hard_stop(self, store):
        eid = _create(capacity=1)
        run(events.sign_up(eid, JO))
        with pytest.raises(HTTPException) as exc:
            run(events.sign_up(eid, SAM))
        assert "full" in exc.value.detail.lower()

    def test_cancelling_frees_the_place(self, store):
        eid = _create(capacity=1)
        run(events.sign_up(eid, JO))
        run(events.cancel_signup(eid, JO))
        assert run(events.sign_up(eid, SAM))["status"] == "confirmed"

    def test_cancelling_without_a_sign_up_is_a_404(self, store):
        eid = _create()
        with pytest.raises(HTTPException) as exc:
            run(events.cancel_signup(eid, JO))
        assert exc.value.status_code == 404

    def test_a_finished_event_cannot_be_signed_up_to(self, store):
        eid = _create()
        store["events"][eid]["ends_at"] = dt.datetime(2020, 1, 1, tzinfo=dt.timezone.utc)
        with pytest.raises(HTTPException):
            run(events.sign_up(eid, JO))


class TestApprovalMode:
    def test_signing_up_only_files_a_request(self, store):
        eid = _create(signup_mode="approval", capacity=1)
        assert run(events.sign_up(eid, JO))["status"] == "pending"
        # A pending request holds no place, so someone else can still ask.
        assert run(events.sign_up(eid, SAM))["status"] == "pending"

    def test_approving_confirms_and_records_who(self, store):
        eid = _create(signup_mode="approval")
        run(events.sign_up(eid, JO))
        run(events.decide_signup(eid, "u1", events.Decision(decision="approve"), ANALYST))
        row = store["event_signups"][f"{eid}_u1"]
        assert row["status"] == "confirmed"
        assert row["decided_by"] == "priya"

    def test_approval_stops_at_capacity(self, store):
        eid = _create(signup_mode="approval", capacity=1)
        run(events.sign_up(eid, JO))
        run(events.sign_up(eid, SAM))
        run(events.decide_signup(eid, "u1", events.Decision(decision="approve"), ADMIN))
        with pytest.raises(HTTPException) as exc:
            run(events.decide_signup(eid, "u2", events.Decision(decision="approve"), ADMIN))
        assert "full" in exc.value.detail.lower()

    def test_once_full_nobody_new_can_even_ask(self, store):
        eid = _create(signup_mode="approval", capacity=1)
        run(events.sign_up(eid, JO))
        run(events.decide_signup(eid, "u1", events.Decision(decision="approve"), ADMIN))
        with pytest.raises(HTTPException):
            run(events.sign_up(eid, SAM))

    def test_a_declined_request_cannot_be_resubmitted(self, store):
        eid = _create(signup_mode="approval")
        run(events.sign_up(eid, JO))
        run(events.decide_signup(eid, "u1", events.Decision(decision="decline"), ADMIN))
        with pytest.raises(HTTPException) as exc:
            run(events.sign_up(eid, JO))
        assert "approved" in exc.value.detail

    def test_declining_a_confirmed_person_frees_their_place(self, store):
        eid = _create(capacity=1)
        run(events.sign_up(eid, JO))
        run(events.decide_signup(eid, "u1", events.Decision(decision="decline"), ADMIN))
        assert run(events.sign_up(eid, SAM))["status"] == "confirmed"

    def test_an_unknown_decision_or_signup_is_refused(self, store):
        eid = _create(signup_mode="approval")
        with pytest.raises(HTTPException) as exc:
            run(events.decide_signup(eid, "u1", events.Decision(decision="maybe"), ADMIN))
        assert exc.value.status_code == 400
        with pytest.raises(HTTPException) as exc:
            run(events.decide_signup(eid, "u1", events.Decision(decision="approve"), ADMIN))
        assert exc.value.status_code == 404


class TestListing:
    def test_numbers_hidden_from_the_public_unless_the_admin_chose_to_show_them(self, store):
        hidden = _create(title="Hidden", capacity=5, show_attendance=False)
        shown = _create(title="Shown", capacity=5, show_attendance=True)
        run(events.sign_up(hidden, JO))
        run(events.sign_up(shown, JO))

        by_title = {e["title"]: e for e in run(events.list_events(None))["events"]}
        assert by_title["Hidden"]["capacity"] is None and by_title["Hidden"]["attending"] is None
        assert by_title["Shown"]["capacity"] == 5 and by_title["Shown"]["attending"] == 1

    def test_reviewers_always_see_the_numbers(self, store):
        eid = _create(capacity=5, show_attendance=False)
        run(events.sign_up(eid, JO))
        for viewer in (ADMIN, ANALYST):
            view = run(events.list_events(viewer))
            ev = view["events"][0]
            assert ev["attending"] == 1 and ev["capacity"] == 5
            assert view["can_manage"] is True

    def test_an_ordinary_member_cannot_manage(self, store):
        _create()
        view = run(events.list_events(JO))
        assert view["can_manage"] is False and view["is_admin"] is False

    def test_full_is_visible_even_when_the_count_is_not(self, store):
        eid = _create(capacity=1, show_attendance=False)
        run(events.sign_up(eid, JO))
        ev = run(events.list_events(None))["events"][0]
        assert ev["full"] is True and ev["attending"] is None

    def test_my_status_is_the_viewers_own(self, store):
        eid = _create(signup_mode="approval")
        run(events.sign_up(eid, JO))
        assert run(events.list_events(JO))["events"][0]["my_status"] == "pending"
        assert run(events.list_events(SAM))["events"][0]["my_status"] is None
        assert run(events.list_events(None))["events"][0]["my_status"] is None

    def test_upcoming_first_soonest_first_then_past_most_recent_first(self, store):
        later = _create(title="Later", date="2099-12-01")
        sooner = _create(title="Sooner", date="2099-11-01")
        old = _create(title="Old")
        older = _create(title="Older")
        store["events"][old]["starts_at"] = dt.datetime(2020, 6, 1, 10, tzinfo=dt.timezone.utc)
        store["events"][old]["ends_at"] = dt.datetime(2020, 6, 1, 11, tzinfo=dt.timezone.utc)
        store["events"][older]["starts_at"] = dt.datetime(2019, 6, 1, 10, tzinfo=dt.timezone.utc)
        store["events"][older]["ends_at"] = dt.datetime(2019, 6, 1, 11, tzinfo=dt.timezone.utc)
        assert [e["title"] for e in run(events.list_events(None))["events"]] == [
            "Sooner", "Later", "Old", "Older"]
        assert later and sooner

    def test_the_label_reads_in_london_time(self, store):
        _create()
        label = run(events.list_events(None))["events"][0]["when_label"]
        assert "05 Oct 2099" in label and "18:00–19:30" in label and "London" in label

    def test_pending_count_is_for_reviewers_only(self, store):
        eid = _create(signup_mode="approval")
        run(events.sign_up(eid, JO))
        assert run(events.list_events(None))["events"][0]["pending"] is None
        assert run(events.list_events(ADMIN))["events"][0]["pending"] == 1


class TestSignupList:
    def test_reviewers_see_who_signed_up_with_their_status(self, store):
        eid = _create(signup_mode="approval")
        run(events.sign_up(eid, JO))
        rows = run(events.list_signups(eid, ANALYST))["signups"]
        assert rows == [{
            "user_id": "u1", "name": "Jo Bloggs", "username": "jo",
            "email": "jo@ox.ac.uk", "status": "pending", "ticket": None, "decided_by": "",
        }]


class TestDelete:
    def test_deleting_an_event_takes_its_signups_but_not_other_events(self, store):
        gone, kept = _create(title="Gone"), _create(title="Kept")
        run(events.sign_up(gone, JO))
        run(events.sign_up(kept, JO))
        run(events.delete_event(gone, ADMIN))
        assert gone not in store["events"]
        assert list(store["event_signups"]) == [f"{kept}_u1"]


# ── Quant Outreach: the event whose sign-up is also the application's first step ─
OUT = outreach.OUTREACH_EVENT_ID


def _seed_outreach(store):
    run(outreach.ensure_event())
    return OUT


def _holders(store, n, ticket="fast_track"):
    for i in range(n):
        store.setdefault("event_signups", {})[f"{OUT}_x{i}"] = {
            "event_id": OUT, "user_id": f"x{i}", "status": "confirmed", "ticket": ticket}


class TestOutreachEvent:
    def test_it_is_created_for_12_october_and_only_once(self, store):
        _seed_outreach(store)
        ev = store["events"][OUT]
        assert ev["kind"] == "outreach" and ev["date"] == "2026-10-12"
        assert ev["title"] == "Quant Outreach"
        store["events"][OUT]["title"] = "Edited by an admin"
        run(outreach.ensure_event())
        assert store["events"][OUT]["title"] == "Edited by an admin"

    def test_the_list_offers_two_tickets_with_fast_track_places_left(self, store):
        _seed_outreach(store)
        _holders(store, 3)
        ev = run(events.list_events(JO))["events"][0]
        keys = [t["key"] for t in ev["tickets"]]
        assert keys == ["fast_track", "general"]
        assert ev["tickets"][0]["remaining"] == outreach.FAST_TRACK_CAPACITY - 3
        assert ev["my_ticket"] is None

    def test_choosing_a_ticket_signs_you_up_with_it(self, store):
        _seed_outreach(store)
        run(events.choose_ticket(OUT, events.TicketChoice(ticket="fast_track"), JO))
        assert store["event_signups"][f"{OUT}_u1"]["ticket"] == "fast_track"
        assert run(events.list_events(JO))["events"][0]["my_ticket"] == "fast_track"

    def test_you_can_change_ticket_or_stop_attending(self, store):
        _seed_outreach(store)
        run(events.choose_ticket(OUT, events.TicketChoice(ticket="fast_track"), JO))
        run(events.choose_ticket(OUT, events.TicketChoice(ticket="general"), JO))
        assert store["event_signups"][f"{OUT}_u1"]["ticket"] == "general"
        run(events.choose_ticket(OUT, events.TicketChoice(ticket="none"), JO))
        assert f"{OUT}_u1" not in store["event_signups"]

    def test_cancel_on_the_events_page_is_the_same_as_not_attending(self, store):
        _seed_outreach(store)
        run(events.choose_ticket(OUT, events.TicketChoice(ticket="general"), JO))
        run(events.cancel_signup(OUT, JO))
        assert f"{OUT}_u1" not in store["event_signups"]

    def test_fast_track_stops_at_fifty_but_general_does_not(self, store):
        _seed_outreach(store)
        _holders(store, outreach.FAST_TRACK_CAPACITY)
        with pytest.raises(HTTPException) as exc:
            run(events.choose_ticket(OUT, events.TicketChoice(ticket="fast_track"), JO))
        assert "full" in exc.value.detail.lower()
        run(events.choose_ticket(OUT, events.TicketChoice(ticket="general"), JO))

    def test_your_own_fast_track_place_is_never_blocked_by_the_limit(self, store):
        _seed_outreach(store)
        _holders(store, outreach.FAST_TRACK_CAPACITY - 1)
        run(events.choose_ticket(OUT, events.TicketChoice(ticket="fast_track"), JO))   # the 50th
        run(events.choose_ticket(OUT, events.TicketChoice(ticket="fast_track"), JO))   # re-confirming is fine

    def test_a_place_held_only_by_an_application_still_counts(self, store):
        _seed_outreach(store)
        _holders(store, outreach.FAST_TRACK_CAPACITY - 2)   # plus the application below = 49
        store["applications"] = {"legacy": {"event_ticket": "fast_track", "status": "submitted"}}
        run(events.choose_ticket(OUT, events.TicketChoice(ticket="fast_track"), JO))   # the 50th
        with pytest.raises(HTTPException):
            run(events.choose_ticket(OUT, events.TicketChoice(ticket="fast_track"), SAM))

    def test_the_plain_endpoints_refuse_it(self, store):
        _seed_outreach(store)
        with pytest.raises(HTTPException) as exc:
            run(events.sign_up(OUT, JO))
        assert "ticket" in exc.value.detail.lower()
        with pytest.raises(HTTPException):
            run(events.delete_event(OUT, ADMIN))
        with pytest.raises(HTTPException):
            run(events.decide_signup(OUT, "u1", events.Decision(decision="approve"), ADMIN))

    def test_a_plain_event_has_no_ticket_endpoint(self, store):
        eid = _create()
        with pytest.raises(HTTPException):
            run(events.choose_ticket(eid, events.TicketChoice(ticket="general"), JO))

    def test_editing_it_leaves_places_and_signup_to_the_ticket_system(self, store):
        _seed_outreach(store)
        run(events.update_event(OUT, _payload(title="Quant Outreach 2", capacity=3, signup_mode="approval"), ADMIN))
        ev = store["events"][OUT]
        assert ev["title"] == "Quant Outreach 2" and ev["capacity"] is None and ev["signup_mode"] == "first_come"

    def test_reviewers_see_which_ticket_each_person_has(self, store):
        _seed_outreach(store)
        run(events.choose_ticket(OUT, events.TicketChoice(ticket="fast_track"), JO))
        rows = run(events.list_signups(OUT, ANALYST))["signups"]
        assert rows[0]["ticket"] == "CV clinic + Fast-Track"


class TestLinkedToTheApplication:
    def _application(self, store, **over):
        store["applications"] = {"u1": {"user_id": "u1", "status": "cv", **over}}
        return store["applications"]["u1"]

    def test_signing_up_first_is_visible_to_the_application(self, store):
        _seed_outreach(store)
        run(events.choose_ticket(OUT, events.TicketChoice(ticket="general"), JO))
        assert run(outreach.ticket_of("u1")) == "general"
        assert run(outreach.ticket_of("u2")) is None

    def test_an_application_that_has_not_chosen_yet_is_left_to_show_its_choice_screen(self, store):
        _seed_outreach(store)
        app = self._application(store)
        run(events.choose_ticket(OUT, events.TicketChoice(ticket="fast_track"), JO))
        assert "event_ticket" not in store["applications"]["u1"] and app

    def test_a_change_on_the_events_page_carries_into_a_chosen_application(self, store):
        _seed_outreach(store)
        self._application(store, event_ticket="fast_track")
        run(events.choose_ticket(OUT, events.TicketChoice(ticket="general"), JO))
        assert store["applications"]["u1"]["event_ticket"] == "general"
        run(events.cancel_signup(OUT, JO))
        assert store["applications"]["u1"]["event_ticket"] == "none"

    def test_a_fast_tracked_submission_cannot_be_downgraded_from_the_events_page(self, store):
        _seed_outreach(store)
        self._application(store, status="submitted", event_ticket="fast_track")
        run(outreach.set_ticket("u1", "jo", "fast_track"))
        with pytest.raises(HTTPException) as exc:
            run(events.choose_ticket(OUT, events.TicketChoice(ticket="general"), JO))
        assert "fast-tracked" in exc.value.detail
        with pytest.raises(HTTPException):
            run(events.cancel_signup(OUT, JO))
        assert store["applications"]["u1"]["event_ticket"] == "fast_track"

    def test_deleting_the_user_frees_the_fast_track_place(self, store):
        _seed_outreach(store)
        run(events.choose_ticket(OUT, events.TicketChoice(ticket="fast_track"), JO))
        assert "u1" in run(outreach.fast_track_holders())
        store["event_signups"].pop(f"{OUT}_u1")
        assert "u1" not in run(outreach.fast_track_holders())
