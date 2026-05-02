"""
tests/test_models.py
====================
Unit tests for model @property and @hybrid_property methods.
No HTTP — pure SQLAlchemy.

Covers:
  - AgeGroup.confirmed_count / waitlist_count / spots_remaining / is_full / head_coach
  - Child.age_on / full_name / has_medical_notes
  - Registration.is_confirmed
  - CampSession.is_registration_open / camp_days / is_active_today
  - Announcement.is_visible_to (role + age-group targeting)
  - User.full_name
"""

import pytest
from datetime import date, datetime, timedelta


# =============================================================================
# Helpers
# =============================================================================

def make_camp(db, start=None, end=None, status='published',
              registration_open=True, opens_at=None, closes_at=None):
    from sub_modules.models import CampSession
    c = CampSession(
        name='TC', year=2023,
        start_date=start or date(2023, 7, 19),
        end_date=end or date(2023, 7, 22),
        status=status,
        registration_open=registration_open,
        registration_opens_at=opens_at,
        registration_closes_at=closes_at,
    )
    db.session.add(c)
    db.session.flush()
    return c


def make_group(db, camp, name='U10', min_age=8, max_age=9, capacity=2):
    from sub_modules.models import AgeGroup
    g = AgeGroup(camp_session_id=camp.id, name=name,
                 min_age=min_age, max_age=max_age, capacity=capacity)
    db.session.add(g)
    db.session.flush()
    return g


def make_child(db, parent, dob=date(2014, 1, 1), medical_notes=None):
    from sub_modules.models import Child
    c = Child(parent_user_id=parent.id,
              first_name='Anna', last_name='Muster',
              date_of_birth=dob,
              medical_notes=medical_notes)
    db.session.add(c)
    db.session.flush()
    return c


def make_reg(db, child, camp, group, status='confirmed', waitlist_pos=None):
    from sub_modules.models import Registration
    r = Registration(child_id=child.id, camp_session_id=camp.id,
                     age_group_id=group.id, status=status,
                     waitlist_position=waitlist_pos)
    db.session.add(r)
    db.session.flush()
    return r


# =============================================================================
# AgeGroup properties
# =============================================================================

class TestAgeGroupProperties:

    def test_confirmed_count(self, db, parent):
        camp = make_camp(db); group = make_group(db, camp)
        c1 = make_child(db, parent); c2 = make_child(db, parent)
        make_reg(db, c1, camp, group, 'confirmed')
        make_reg(db, c2, camp, group, 'waitlisted', waitlist_pos=1)
        db.session.commit()
        assert group.confirmed_count == 1

    def test_waitlist_count(self, db, parent):
        camp = make_camp(db); group = make_group(db, camp)
        c1 = make_child(db, parent); c2 = make_child(db, parent)
        make_reg(db, c1, camp, group, 'waitlisted', waitlist_pos=1)
        make_reg(db, c2, camp, group, 'waitlisted', waitlist_pos=2)
        db.session.commit()
        assert group.waitlist_count == 2

    def test_spots_remaining(self, db, parent):
        camp = make_camp(db); group = make_group(db, camp, capacity=3)
        child = make_child(db, parent)
        make_reg(db, child, camp, group, 'confirmed')
        db.session.commit()
        assert group.spots_remaining == 2

    def test_is_full_true(self, db, parent):
        camp = make_camp(db); group = make_group(db, camp, capacity=1)
        child = make_child(db, parent)
        make_reg(db, child, camp, group, 'confirmed')
        db.session.commit()
        assert group.is_full is True

    def test_is_full_false(self, db, parent):
        camp = make_camp(db); group = make_group(db, camp, capacity=5)
        child = make_child(db, parent)
        make_reg(db, child, camp, group, 'confirmed')
        db.session.commit()
        assert group.is_full is False

    def test_head_coach_returns_none_when_none(self, db):
        camp = make_camp(db); group = make_group(db, camp)
        db.session.commit()
        assert group.head_coach is None

    def test_head_coach_returns_correct_assignment(self, db, staff):
        from sub_modules.models import GroupAssignment
        camp = make_camp(db); group = make_group(db, camp)
        ga = GroupAssignment(
            camp_session_id=camp.id,
            age_group_id=group.id,
            staff_user_id=staff.id,
            is_head_coach=True,
        )
        db.session.add(ga)
        db.session.commit()
        assert group.head_coach is not None
        assert group.head_coach.id == staff.id


# =============================================================================
# Child properties
# =============================================================================

class TestChildProperties:

    def test_full_name(self, db, parent):
        child = make_child(db, parent)
        db.session.commit()
        assert child.full_name == 'Anna Muster'

    def test_age_on_birthday(self, db, parent):
        child = make_child(db, parent, dob=date(2013, 7, 15))
        db.session.commit()
        assert child.age_on(date(2023, 7, 15)) == 10

    def test_age_on_day_before_birthday(self, db, parent):
        child = make_child(db, parent, dob=date(2013, 7, 15))
        db.session.commit()
        assert child.age_on(date(2023, 7, 14)) == 9

    def test_has_medical_notes_true(self, db, parent):
        child = make_child(db, parent, medical_notes='Nussallergie')
        db.session.commit()
        assert child.has_medical_notes is True

    def test_has_medical_notes_false(self, db, parent):
        child = make_child(db, parent, medical_notes=None)
        db.session.commit()
        assert child.has_medical_notes is False

    def test_has_medical_notes_false_for_empty_string(self, db, parent):
        child = make_child(db, parent, medical_notes='')
        db.session.commit()
        assert child.has_medical_notes is False


# =============================================================================
# Registration properties
# =============================================================================

class TestRegistrationProperties:

    def test_is_confirmed_true(self, db, parent):
        camp = make_camp(db); group = make_group(db, camp)
        child = make_child(db, parent)
        reg = make_reg(db, child, camp, group, 'confirmed')
        db.session.commit()
        assert reg.is_confirmed is True

    def test_is_confirmed_false_for_waitlisted(self, db, parent):
        camp = make_camp(db); group = make_group(db, camp)
        child = make_child(db, parent)
        reg = make_reg(db, child, camp, group, 'waitlisted', waitlist_pos=1)
        db.session.commit()
        assert reg.is_confirmed is False


# =============================================================================
# CampSession properties
# =============================================================================

class TestCampSessionProperties:

    def test_camp_days_returns_correct_list(self, db):
        start = date(2023, 7, 19)
        end   = date(2023, 7, 22)
        camp = make_camp(db, start=start, end=end)
        db.session.commit()
        days = camp.camp_days
        assert len(days) == 4
        assert days[0][1] == start    # each entry is (day_num, date)
        assert days[-1][1] == end

    def test_is_registration_open_when_flag_set(self, db):
        camp = make_camp(db, registration_open=True)
        db.session.commit()
        assert camp.is_registration_open is True

    def test_is_registration_open_false_when_flag_clear(self, db):
        camp = make_camp(db, registration_open=False)
        db.session.commit()
        assert camp.is_registration_open is False

    def test_registration_closed_after_closes_at(self, db):
        past = datetime.utcnow() - timedelta(days=1)
        camp = make_camp(db, registration_open=True,
                         closes_at=past.date())
        db.session.commit()
        assert camp.is_registration_open is False

    def test_registration_not_open_before_opens_at(self, db):
        future = date.today() + timedelta(days=5)
        camp = make_camp(db, registration_open=True, opens_at=future)
        db.session.commit()
        assert camp.is_registration_open is False


# =============================================================================
# Announcement.is_visible_to
# =============================================================================

class TestAnnouncementVisibility:

    def _make_announcement(self, db, admin, visibility='public',
                            target_age_groups=None, camp=None):
        from sub_modules.models import Announcement
        a = Announcement(
            author_user_id=admin.id,
            camp_session_id=camp.id if camp else None,
            title='Test', body='Body',
            visibility=visibility,
            target_age_groups=target_age_groups,
        )
        db.session.add(a)
        db.session.flush()
        return a

    def test_public_visible_to_admin(self, db, admin):
        ann = self._make_announcement(db, admin, 'public')
        db.session.commit()
        assert ann.is_visible_to(admin) is True

    def test_public_visible_to_staff(self, db, admin, staff):
        ann = self._make_announcement(db, admin, 'public')
        db.session.commit()
        assert ann.is_visible_to(staff) is True

    def test_staff_only_hidden_from_parent(self, db, admin, parent):
        ann = self._make_announcement(db, admin, 'staff')
        db.session.commit()
        assert ann.is_visible_to(parent) is False

    def test_staff_only_visible_to_staff(self, db, admin, staff):
        ann = self._make_announcement(db, admin, 'staff')
        db.session.commit()
        assert ann.is_visible_to(staff) is True

    def test_public_visible_to_parent(self, db, admin, parent):
        ann = self._make_announcement(db, admin, 'public')
        db.session.commit()
        assert ann.is_visible_to(parent) is True


# =============================================================================
# User properties
# =============================================================================

class TestUserProperties:

    def test_full_name(self, db, parent):
        assert parent.full_name == 'Test Elternteil'

    def test_is_active_account_true(self, db, parent):
        assert parent.is_active_account is True

    def test_is_active_account_false_when_deleted(self, db, parent):
        parent.is_deleted = True
        db.session.commit()
        assert parent.is_active_account is False

    def test_is_active_account_false_when_inactive(self, db, parent):
        parent.is_active = False
        db.session.commit()
        assert parent.is_active_account is False
