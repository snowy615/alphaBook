"""Tests for app.membership — roles, memberships, clubs and opt-ins.

The rules that matter here are access rules, so they're pinned: a role is
never self-granted, the recruiter directory needs both the role and the
student's opt-in, and accounts created before any of this existed keep
working and keep their place in the CV book.
"""

import pytest

from app import membership as mb


class TestMembershipResolution:
    def test_explicit_membership_wins(self):
        assert mb.membership_of({"membership": mb.M_QUANT_ANALYST}) == mb.M_QUANT_ANALYST

    @pytest.mark.parametrize("track,expected", [
        ("Fundamental", mb.M_FUND_ANALYST),
        ("Quant", mb.M_QUANT_ANALYST),
        ("Fundamental Bootcamp", mb.M_FUND_BOOTCAMP),
        ("Quant Bootcamp", mb.M_QUANT_BOOTCAMP),
    ])
    def test_legacy_tracks_map_across(self, track, expected):
        # Nothing rewrites old documents, so the read path has to do it.
        assert mb.membership_of({"track": track}) == expected

    def test_unknown_and_empty_fall_back_to_public(self):
        assert mb.membership_of({}) == mb.M_PUBLIC
        assert mb.membership_of({"track": ""}) == mb.M_PUBLIC
        assert mb.membership_of({"membership": "Wizard"}) == mb.M_PUBLIC

    def test_club_defaults_to_alpha_fund(self):
        assert mb.club_of({}) == mb.CLUB_ALPHA_FUND
        assert mb.club_of({"club": "Nonexistent Society"}) == mb.CLUB_ALPHA_FUND


class TestRoles:
    def test_default_role_is_general(self):
        assert mb.role_of({}) == mb.ROLE_GENERAL

    def test_admins_can_host_without_a_grant(self):
        assert mb.role_of({"is_admin": True}) == mb.ROLE_HOST
        assert mb.can_host({"is_admin": True})
        assert mb.is_recruiter({"is_admin": True})

    def test_granted_roles_are_honoured(self):
        assert mb.can_host({"role": mb.ROLE_HOST})
        assert mb.is_recruiter({"role": mb.ROLE_RECRUITER})

    def test_roles_do_not_leak_into_each_other(self):
        # A recruiter must not be able to run competitions, or vice versa.
        assert not mb.can_host({"role": mb.ROLE_RECRUITER})
        assert not mb.is_recruiter({"role": mb.ROLE_HOST})
        assert not mb.can_host({"role": mb.ROLE_GENERAL})
        assert not mb.is_recruiter({})

    def test_only_recruiter_and_host_can_be_requested(self):
        assert mb.REQUESTABLE_ROLES == {mb.ROLE_RECRUITER, mb.ROLE_HOST}
        assert mb.ROLE_GENERAL not in mb.REQUESTABLE_ROLES


class TestOptIns:
    def test_contact_is_off_by_default(self):
        assert not mb.contactable({})
        assert not mb.contactable({"membership": mb.M_QUANT_ANALYST})
        assert mb.contactable({"opt_in_contact": True})

    def test_only_analysts_are_cv_book_eligible(self):
        for m in (mb.M_PUBLIC, mb.M_SPONSOR, mb.M_MEMBER,
                  mb.M_FUND_BOOTCAMP, mb.M_QUANT_BOOTCAMP):
            assert not mb.cv_book_included({"membership": m, "opt_in_cv_book": True}), m

    def test_analysts_from_before_the_opt_in_stay_included(self):
        # The book used to include every analyst; an unset flag must not
        # quietly drop someone out of a book they expect to be in.
        assert mb.cv_book_included({"track": "Quant"})
        assert mb.cv_book_included({"membership": mb.M_FUND_ANALYST})

    def test_an_analyst_can_opt_out(self):
        assert not mb.cv_book_included(
            {"membership": mb.M_QUANT_ANALYST, "opt_in_cv_book": False})

    def test_opting_in_without_eligibility_does_nothing(self):
        assert not mb.cv_book_included(
            {"membership": mb.M_SPONSOR, "opt_in_cv_book": True})


class TestMembershipChanges:
    def test_gated_memberships_need_their_password(self):
        for m in mb.ANALYST_MEMBERSHIPS:
            assert mb.validate_membership_change(m, None, "analyst", "boot")
            assert mb.validate_membership_change(m, "wrong", "analyst", "boot")
            assert mb.validate_membership_change(m, "analyst", "analyst", "boot") is None
        for m in mb.BOOTCAMP_MEMBERSHIPS:
            assert mb.validate_membership_change(m, "analyst", "analyst", "boot")
            assert mb.validate_membership_change(m, "boot", "analyst", "boot") is None

    def test_open_memberships_need_no_password(self):
        for m in (mb.M_PUBLIC, mb.M_SPONSOR, mb.M_MEMBER):
            assert mb.validate_membership_change(m, None, "analyst", "boot") is None

    def test_unknown_membership_is_rejected(self):
        assert mb.validate_membership_change("Wizard", None, "a", "b")


class TestPublicProfile:
    def test_it_never_carries_contact_details(self):
        row = mb.public_profile("u1", {
            "username": "jane", "email": "jane@example.com",
            "membership": mb.M_QUANT_ANALYST, "cv_blob_path": "cvs/x.pdf",
        })
        assert "email" not in row
        assert "cv_blob_path" not in row
        assert row["membership"] == mb.M_QUANT_ANALYST

    def test_vocabulary_covers_the_form(self):
        v = mb.vocabulary()
        assert set(v["memberships"]) == set(mb.MEMBERSHIPS)
        assert v["clubs"] == mb.CLUBS
        assert {r["key"] for r in v["roles"]} == mb.ROLE_KEYS
