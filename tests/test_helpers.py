"""
tests/test_helpers.py
=====================
Unit tests for sub_modules/helpers.py — pure logic, no HTTP.

Covers:
  - calculate_age_on: birthday on camp start, leap years, year boundary
  - assign_age_group: boundaries, too young/old, gap between groups
  - get_next_waitlist_position: empty list, existing entries
  - promote_from_waitlist: promotes correct record, compacts positions, rejects non-waitlisted
  - process_checkin: success, duplicate flagging, wrong camp day
  - process_checkout: success, no checkin first
  - process_staff_checkin: success, duplicate
  - run_staff_auto_checkout: only fires for staff, not children
  - honeypot_triggered / submission_too_fast: bot guards
"""

import time
import pytest
from datetime import date, datetime, timedelta


# =============================================================================
# Helpers — build fixtures
# =============================================================================

def make_camp(db, start=None, end=None, status='active'):
    from sub_modules.models import CampSession
    camp = CampSession(
        name='Testcamp',
        year=2023,
        start_date=start or date.today(),
        end_date=end or date.today(),
        status=status,
    )
    db.session.add(camp)
    db.session.flush()
    return camp


def make_group(db, camp, name='U10', min_age=8, max_age=9, capacity=20):
    from sub_modules.models import AgeGroup
    g = AgeGroup(camp_session_id=camp.id, name=name,
                 min_age=min_age, max_age=max_age, capacity=capacity)
    db.session.add(g)
    db.session.flush()
    return g


def make_child(db, parent, dob=None):
    from sub_modules.models import Child
    c = Child(
        parent_user_id=parent.id,
        first_name='Test',
        last_name='Kind',
        date_of_birth=dob or date(2014, 7, 1),
    )
    db.session.add(c)
    db.session.flush()
    return c


def make_registration(db, child, camp, group, status='confirmed', waitlist_position=None):
    from sub_modules.models import Registration
    r = Registration(
        child_id=child.id,
        camp_session_id=camp.id,
        age_group_id=group.id,
        status=status,
        waitlist_position=waitlist_position,
    )
    db.session.add(r)
    db.session.flush()
    return r


# =============================================================================
# calculate_age_on
# =============================================================================

class TestCalculateAgeOn:

    def test_standard_case(self):
        from sub_modules.helpers import calculate_age_on
        assert calculate_age_on(date(2015, 6, 1), date(2023, 7, 1)) == 8

    def test_birthday_is_camp_start_date(self):
        """Child turning 10 on first camp day counts as 10."""
        from sub_modules.helpers import calculate_age_on
        assert calculate_age_on(date(2013, 7, 15), date(2023, 7, 15)) == 10

    def test_birthday_day_after_camp_start(self):
        """Child whose birthday is tomorrow is still 9."""
        from sub_modules.helpers import calculate_age_on
        assert calculate_age_on(date(2013, 7, 16), date(2023, 7, 15)) == 9

    def test_leap_year_birthday_on_non_leap_year(self):
        """Born Feb 29 — age correct on non-leap reference date."""
        from sub_modules.helpers import calculate_age_on
        assert calculate_age_on(date(2008, 2, 29), date(2023, 7, 1)) == 15

    def test_year_boundary_december_birth_january_camp(self):
        """Born Dec 31, camp in January next year."""
        from sub_modules.helpers import calculate_age_on
        assert calculate_age_on(date(2014, 12, 31), date(2023, 1, 15)) == 8

    def test_infant_returns_zero_not_negative(self):
        from sub_modules.helpers import calculate_age_on
        assert calculate_age_on(date(2023, 6, 1), date(2023, 7, 1)) == 0


# =============================================================================
# assign_age_group
# =============================================================================

class TestAssignAgeGroup:

    @pytest.fixture
    def camp_and_groups(self, db):
        camp = make_camp(db, start=date(2023, 7, 15), end=date(2023, 7, 18))
        u8  = make_group(db, camp, 'U8',  min_age=6, max_age=7)
        u10 = make_group(db, camp, 'U10', min_age=8, max_age=9)
        db.session.commit()
        return camp, u8, u10

    def _child_with_dob(self, db, parent, dob):
        from sub_modules.models import Child
        c = Child(parent_user_id=parent.id,
                  first_name='X', last_name='Y', date_of_birth=dob)
        db.session.add(c)
        db.session.flush()
        return c

    def test_assigns_correct_group(self, db, parent, camp_and_groups):
        from sub_modules.helpers import assign_age_group
        camp, u8, u10 = camp_and_groups
        # Age 8 on camp start → U10
        child = self._child_with_dob(db, parent, date(2015, 7, 15))
        assert assign_age_group(child, camp).name == 'U10'

    def test_child_at_lower_boundary_included(self, db, parent, camp_and_groups):
        from sub_modules.helpers import assign_age_group
        camp, u8, _ = camp_and_groups
        # Exactly 6 on camp start → U8
        child = self._child_with_dob(db, parent, date(2017, 7, 15))
        assert assign_age_group(child, camp).name == 'U8'

    def test_child_at_upper_boundary_included(self, db, parent, camp_and_groups):
        from sub_modules.helpers import assign_age_group
        camp, _, u10 = camp_and_groups
        # Exactly 9 on camp start → U10
        child = self._child_with_dob(db, parent, date(2014, 7, 15))
        assert assign_age_group(child, camp).name == 'U10'

    def test_child_too_young_returns_none(self, db, parent, camp_and_groups):
        from sub_modules.helpers import assign_age_group
        camp, _, _ = camp_and_groups
        child = self._child_with_dob(db, parent, date(2020, 7, 15))  # age 3
        assert assign_age_group(child, camp) is None

    def test_child_too_old_returns_none(self, db, parent, camp_and_groups):
        from sub_modules.helpers import assign_age_group
        camp, _, _ = camp_and_groups
        child = self._child_with_dob(db, parent, date(2005, 7, 15))  # age 18
        assert assign_age_group(child, camp) is None

    def test_child_in_gap_between_groups_returns_none(self, db, parent):
        """Groups U8 (6–7) and U12 (10–11): age 9 falls in the gap."""
        from sub_modules.helpers import assign_age_group
        from sub_modules.models import AgeGroup
        camp = make_camp(db, start=date(2023, 7, 15), end=date(2023, 7, 18))
        make_group(db, camp, 'U8',  min_age=6, max_age=7)
        make_group(db, camp, 'U12', min_age=10, max_age=11)
        db.session.commit()
        child = self._child_with_dob(db, parent, date(2014, 7, 15))  # age 9
        assert assign_age_group(child, camp) is None


# =============================================================================
# get_next_waitlist_position
# =============================================================================

class TestGetNextWaitlistPosition:

    def test_first_position_is_one(self, db, parent):
        from sub_modules.helpers import get_next_waitlist_position
        camp = make_camp(db); group = make_group(db, camp); db.session.commit()
        assert get_next_waitlist_position(camp.id, group.id) == 1

    def test_increments_after_existing(self, db, parent):
        from sub_modules.helpers import get_next_waitlist_position
        camp = make_camp(db); group = make_group(db, camp, capacity=0)
        child = make_child(db, parent)
        make_registration(db, child, camp, group, status='waitlisted', waitlist_position=3)
        db.session.commit()
        assert get_next_waitlist_position(camp.id, group.id) == 4

    def test_ignores_cancelled_positions(self, db, parent):
        """A cancelled registration at position 1 should not block next being 1."""
        from sub_modules.helpers import get_next_waitlist_position
        camp = make_camp(db); group = make_group(db, camp, capacity=0)
        child = make_child(db, parent)
        make_registration(db, child, camp, group, status='cancelled', waitlist_position=1)
        db.session.commit()
        # 'cancelled' has waitlist_position set but status != 'waitlisted'
        # get_next_waitlist_position filters on status='waitlisted'
        assert get_next_waitlist_position(camp.id, group.id) == 1


# =============================================================================
# promote_from_waitlist
# =============================================================================

class TestPromoteFromWaitlist:

    @pytest.fixture
    def setup(self, db, parent):
        from sub_modules.models import Child, Registration
        camp  = make_camp(db, start=date(2023, 7, 15), end=date(2023, 7, 18))
        group = make_group(db, camp, capacity=1)
        c1    = make_child(db, parent)
        c2    = make_child(db, parent)
        c3    = make_child(db, parent)
        r_confirmed = make_registration(db, c1, camp, group, 'confirmed')
        r_w1 = make_registration(db, c2, camp, group, 'waitlisted', waitlist_position=1)
        r_w2 = make_registration(db, c3, camp, group, 'waitlisted', waitlist_position=2)
        db.session.commit()
        return camp, group, r_confirmed, r_w1, r_w2

    def test_rejects_non_waitlisted(self, db, parent, setup):
        from sub_modules.helpers import promote_from_waitlist
        _, _, r_confirmed, _, _ = setup
        success, msg = promote_from_waitlist(r_confirmed)
        assert success is False
        assert 'warteliste' in msg.lower()

    def test_rejects_when_group_still_full(self, db, parent, setup):
        from sub_modules.helpers import promote_from_waitlist
        _, _, _, r_w1, _ = setup
        # Group still full (capacity=1, 1 confirmed)
        success, msg = promote_from_waitlist(r_w1)
        assert success is False
        assert 'voll' in msg.lower()

    def test_promotes_when_spot_freed(self, db, parent, setup):
        from sub_modules.helpers import promote_from_waitlist
        _, _, r_confirmed, r_w1, r_w2 = setup
        r_confirmed.status = 'cancelled'
        db.session.commit()

        success, msg = promote_from_waitlist(r_w1)
        db.session.refresh(r_w1)
        db.session.refresh(r_w2)

        assert success is True
        assert r_w1.status == 'confirmed'
        assert r_w1.waitlist_position is None

    def test_compacts_remaining_positions(self, db, parent, setup):
        from sub_modules.helpers import promote_from_waitlist
        _, _, r_confirmed, r_w1, r_w2 = setup
        r_confirmed.status = 'cancelled'
        db.session.commit()

        promote_from_waitlist(r_w1)
        db.session.refresh(r_w2)
        assert r_w2.waitlist_position == 1


# =============================================================================
# process_checkin / process_checkout
# =============================================================================

class TestCheckin:

    @pytest.fixture
    def today_camp_reg(self, db, parent, staff):
        camp  = make_camp(db, start=date.today(), end=date.today(), status='active')
        group = make_group(db, camp)
        child = make_child(db, parent)
        reg   = make_registration(db, child, camp, group)
        db.session.commit()
        return camp, reg

    def test_first_checkin_succeeds(self, db, today_camp_reg, staff):
        from sub_modules.helpers import process_checkin
        _, reg = today_camp_reg
        success, msg, log = process_checkin(reg, staff)
        assert success is True
        assert log is not None
        assert log.is_duplicate is False

    def test_second_checkin_flagged_as_duplicate(self, db, today_camp_reg, staff):
        from sub_modules.helpers import process_checkin
        _, reg = today_camp_reg
        process_checkin(reg, staff)
        success, msg, log = process_checkin(reg, staff)
        assert success is True   # recorded, but flagged
        assert log.is_duplicate is True

    def test_checkin_outside_camp_days_fails(self, db, parent, staff):
        from sub_modules.helpers import process_checkin
        # Camp was yesterday — today is not a camp day
        yesterday = date.today() - timedelta(days=1)
        camp  = make_camp(db, start=yesterday, end=yesterday, status='active')
        group = make_group(db, camp)
        child = make_child(db, parent)
        reg   = make_registration(db, child, camp, group)
        db.session.commit()
        success, msg, log = process_checkin(reg, staff)
        assert success is False
        assert log is None

    def test_checkout_after_checkin_succeeds(self, db, today_camp_reg, staff):
        from sub_modules.helpers import process_checkin, process_checkout
        _, reg = today_camp_reg
        process_checkin(reg, staff)
        success, msg, log, _ = process_checkout(reg, staff)
        assert success is True
        assert log.event_type == 'checkout'


# =============================================================================
# process_staff_checkin + run_staff_auto_checkout
# =============================================================================

class TestStaffCheckin:

    def test_staff_checkin_succeeds(self, db, staff):
        from sub_modules.helpers import process_staff_checkin
        camp = make_camp(db, status='active')
        db.session.commit()
        success, msg = process_staff_checkin(staff, camp)
        assert success is True

    def test_staff_checkin_outside_camp_days_fails(self, db, staff):
        from sub_modules.helpers import process_staff_checkin
        yesterday = date.today() - timedelta(days=1)
        camp = make_camp(db, start=yesterday, end=yesterday, status='active')
        db.session.commit()
        success, msg = process_staff_checkin(staff, camp)
        assert success is False


class TestStaffAutoCheckout:

    def test_auto_checkout_creates_event_for_staff(self, db, staff):
        from sub_modules.helpers import process_staff_checkin, run_staff_auto_checkout
        from sub_modules.models import CheckinLog
        camp = make_camp(db, status='active')
        db.session.commit()
        process_staff_checkin(staff, camp)
        run_staff_auto_checkout()
        auto = CheckinLog.query.filter_by(
            staff_user_id=staff.id,
            event_type='checkout',
            is_auto_checkout=True
        ).count()
        assert auto == 1

    def test_auto_checkout_does_not_affect_children(self, db, parent, staff):
        from sub_modules.helpers import (process_checkin, process_staff_checkin,
                                          run_staff_auto_checkout)
        from sub_modules.models import CheckinLog
        camp  = make_camp(db, status='active')
        group = make_group(db, camp)
        child = make_child(db, parent)
        reg   = make_registration(db, child, camp, group)
        db.session.commit()

        process_checkin(reg, staff)
        process_staff_checkin(staff, camp)
        run_staff_auto_checkout()

        child_checkouts = CheckinLog.query.filter_by(
            registration_id=reg.id, event_type='checkout'
        ).count()
        assert child_checkouts == 0

    def test_auto_checkout_skips_staff_already_checked_out(self, db, staff):
        from sub_modules.helpers import (process_staff_checkin, process_checkout,
                                          run_staff_auto_checkout)
        from sub_modules.models import CheckinLog
        camp = make_camp(db, status='active')
        db.session.commit()
        process_staff_checkin(staff, camp)
        # Manual checkout first
        from sub_modules.models import CheckinLog
        manual_log = CheckinLog(
            staff_user_id=staff.id,
            camp_session_id=camp.id,
            event_type='checkout',
            camp_day=1,
            event_date=date.today(),
            event_time=datetime.utcnow(),
            registration_id=None,
        )
        db.session.add(manual_log)
        db.session.commit()

        run_staff_auto_checkout()
        auto = CheckinLog.query.filter_by(
            staff_user_id=staff.id, is_auto_checkout=True
        ).count()
        assert auto == 0


# =============================================================================
# Bot guards
# =============================================================================

class TestBotGuards:

    def test_honeypot_triggered_when_field_filled(self):
        from sub_modules.helpers import honeypot_triggered
        assert honeypot_triggered({'website': 'http://spam.com'}) is True

    def test_honeypot_not_triggered_when_empty(self):
        from sub_modules.helpers import honeypot_triggered
        assert honeypot_triggered({'website': ''}) is False
        assert honeypot_triggered({}) is False

    def test_submission_too_fast_under_threshold(self):
        from sub_modules.helpers import submission_too_fast
        form_start = time.time() - 1   # 1 second ago
        assert submission_too_fast(form_start, min_seconds=3) is True

    def test_submission_ok_above_threshold(self):
        from sub_modules.helpers import submission_too_fast
        form_start = time.time() - 10  # 10 seconds ago
        assert submission_too_fast(form_start, min_seconds=3) is False
