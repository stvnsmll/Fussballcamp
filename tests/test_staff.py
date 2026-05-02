"""
tests/test_staff.py
===================
Integration tests for views/staff.py.

Covers:
  - GET /team/: dashboard (with and without active camp)
  - GET /team/roster: per-group filter
  - GET /team/checkin: check-in mode page
  - GET /team/checkin/suche: AJAX search by name
  - GET /team/checkin/qr/<token>: QR redirect
  - GET /team/checkin/<id>/bestaetigen: confirm check-in page
  - POST /team/checkin/<id>/ein: do_checkin
  - GET /team/checkout/<id>/bestaetigen: confirm check-out page
  - POST /team/checkout/<id>/aus: do_checkout
  - POST /team/checkin/stornieren/<log_id>: void event
  - GET /team/noch-da: still-present list
  - POST /team/selbst-einchecken: staff self-checkin
  - POST /team/kind/<id>/heimweg: approve independent travel
"""

import pytest
from datetime import date, datetime


def _session(client, user):
    with client.session_transaction() as sess:
        sess['_user_id'] = str(user.id)
        sess['_fresh'] = True


def _make_active_camp(db):
    from sub_modules.models import CampSession
    c = CampSession(name='ActiveCamp', year=2023,
                    start_date=date.today(), end_date=date.today(),
                    status='active')
    db.session.add(c)
    db.session.flush()
    return c


def _make_group(db, camp):
    from sub_modules.models import AgeGroup
    g = AgeGroup(camp_session_id=camp.id, name='U10',
                 min_age=8, max_age=11, capacity=20)
    db.session.add(g)
    db.session.flush()
    return g


def _make_child(db, parent, dob=date(2013, 1, 1)):
    from sub_modules.models import Child
    c = Child(parent_user_id=parent.id,
              first_name='Kind', last_name='Muster',
              date_of_birth=dob)
    db.session.add(c)
    db.session.flush()
    return c


def _make_registration(db, child, camp, group, status='confirmed'):
    from sub_modules.models import Registration
    r = Registration(child_id=child.id, camp_session_id=camp.id,
                     age_group_id=group.id, status=status)
    db.session.add(r)
    db.session.flush()
    return r


def _do_checkin(db, reg, staff_user):
    from sub_modules.models import CheckinLog
    log = CheckinLog(
        registration_id=reg.id,
        staff_user_id=staff_user.id,
        camp_session_id=reg.camp_session_id,
        event_type='checkin',
        camp_day=1,
        event_date=date.today(),
        event_time=datetime.utcnow(),
        method='manual',
    )
    db.session.add(log)
    db.session.flush()
    return log


# =============================================================================
# Dashboard
# =============================================================================

class TestStaffDashboard:

    def test_renders_without_camp(self, client, staff, db):
        _session(client, staff)
        resp = client.get('/team/')
        assert resp.status_code == 200

    def test_renders_with_active_camp(self, client, staff, db):
        _session(client, staff)
        camp = _make_active_camp(db)
        db.session.commit()
        resp = client.get('/team/')
        assert resp.status_code == 200

    def test_blocked_for_parent(self, client, parent):
        _session(client, parent)
        resp = client.get('/team/')
        assert resp.status_code == 403


# =============================================================================
# Roster
# =============================================================================

class TestRoster:

    def test_renders_all_groups(self, client, staff, db):
        _session(client, staff)
        camp = _make_active_camp(db)
        _make_group(db, camp)
        db.session.commit()
        resp = client.get('/team/roster')
        assert resp.status_code == 200

    def test_filter_by_group(self, client, staff, db):
        _session(client, staff)
        camp = _make_active_camp(db)
        group = _make_group(db, camp)
        db.session.commit()
        resp = client.get(f'/team/roster?gruppe={group.id}')
        assert resp.status_code == 200


# =============================================================================
# Check-in mode
# =============================================================================

class TestCheckinMode:

    def test_renders_checkin_page(self, client, staff, db):
        _session(client, staff)
        _make_active_camp(db)
        db.session.commit()
        resp = client.get('/team/checkin', follow_redirects=True)
        assert resp.status_code == 200

    def test_redirects_without_active_camp(self, client, staff, db):
        _session(client, staff)
        resp = client.get('/team/checkin')
        assert resp.status_code in (302, 404)


# =============================================================================
# Search
# =============================================================================

class TestCheckinSearch:

    def test_search_returns_results(self, client, staff, parent, db):
        _session(client, staff)
        camp = _make_active_camp(db)
        group = _make_group(db, camp)
        child = _make_child(db, parent)
        _make_registration(db, child, camp, group)
        db.session.commit()
        resp = client.get('/team/checkin/suche?q=Muster')
        assert resp.status_code == 200
        assert 'Muster' in resp.get_data(as_text=True)

    def test_empty_query_returns_empty(self, client, staff, db):
        _session(client, staff)
        _make_active_camp(db)
        db.session.commit()
        resp = client.get('/team/checkin/suche?q=')
        assert resp.status_code == 200


# =============================================================================
# QR check-in
# =============================================================================

class TestQrCheckin:

    def test_valid_qr_token_redirects_to_confirm(self, client, staff, parent, db):
        from sub_modules.models import QRToken
        from sub_modules.helpers import generate_token
        _session(client, staff)
        camp = _make_active_camp(db)
        group = _make_group(db, camp)
        child = _make_child(db, parent)
        reg = _make_registration(db, child, camp, group)
        token_str = generate_token()
        qr = QRToken(child_id=child.id, token=token_str)
        db.session.add(qr)
        db.session.commit()
        resp = client.get(f'/team/checkin/qr/{token_str}')
        assert resp.status_code == 302
        assert 'bestaetigen' in resp.headers['Location']

    def test_invalid_token_shows_error(self, client, staff, db):
        _session(client, staff)
        _make_active_camp(db)
        db.session.commit()
        resp = client.get('/team/checkin/qr/badtoken', follow_redirects=True)
        assert resp.status_code == 200


# =============================================================================
# do_checkin / do_checkout
# =============================================================================

class TestDoCheckinCheckout:

    def test_do_checkin_creates_log(self, client, staff, parent, db):
        from sub_modules.models import CheckinLog
        _session(client, staff)
        camp = _make_active_camp(db)
        group = _make_group(db, camp)
        child = _make_child(db, parent)
        reg = _make_registration(db, child, camp, group)
        db.session.commit()
        resp = client.post(f'/team/checkin/{reg.id}/ein',
                           follow_redirects=True)
        assert resp.status_code == 200
        log = CheckinLog.query.filter_by(
            registration_id=reg.id, event_type='checkin'
        ).first()
        assert log is not None

    def test_do_checkout_creates_log(self, client, staff, parent, db):
        from sub_modules.models import CheckinLog
        _session(client, staff)
        camp = _make_active_camp(db)
        group = _make_group(db, camp)
        child = _make_child(db, parent)
        reg = _make_registration(db, child, camp, group)
        _do_checkin(db, reg, staff)
        db.session.commit()
        resp = client.post(f'/team/checkout/{reg.id}/aus',
                           follow_redirects=True)
        assert resp.status_code == 200
        log = CheckinLog.query.filter_by(
            registration_id=reg.id, event_type='checkout'
        ).first()
        assert log is not None

    def test_confirm_checkin_page_renders(self, client, staff, parent, db):
        _session(client, staff)
        camp = _make_active_camp(db)
        group = _make_group(db, camp)
        child = _make_child(db, parent)
        reg = _make_registration(db, child, camp, group)
        db.session.commit()
        resp = client.get(f'/team/checkin/{reg.id}/bestaetigen')
        assert resp.status_code == 200

    def test_confirm_checkout_page_renders(self, client, staff, parent, db):
        _session(client, staff)
        camp = _make_active_camp(db)
        group = _make_group(db, camp)
        child = _make_child(db, parent)
        reg = _make_registration(db, child, camp, group)
        _do_checkin(db, reg, staff)
        db.session.commit()
        resp = client.get(f'/team/checkout/{reg.id}/bestaetigen')
        assert resp.status_code == 200


# =============================================================================
# Void event
# =============================================================================

class TestVoidEvent:

    def test_void_marks_log_as_voided(self, client, staff, parent, db):
        from sub_modules.models import CheckinLog
        _session(client, staff)
        camp = _make_active_camp(db)
        group = _make_group(db, camp)
        child = _make_child(db, parent)
        reg = _make_registration(db, child, camp, group)
        log = _do_checkin(db, reg, staff)
        db.session.commit()
        resp = client.post(f'/team/checkin/stornieren/{log.id}',
                           data={'log_id': log.id, 'reason': 'Fehler'},
                           follow_redirects=True)
        assert resp.status_code == 200
        db.session.refresh(log)
        assert log.voided is True


# =============================================================================
# Still present
# =============================================================================

class TestStillPresent:

    def test_renders_page(self, client, staff, db):
        _session(client, staff)
        _make_active_camp(db)
        db.session.commit()
        resp = client.get('/team/noch-da')
        assert resp.status_code == 200


# =============================================================================
# Self check-in
# =============================================================================

class TestSelfCheckin:

    def test_staff_can_self_checkin(self, client, staff, db):
        from sub_modules.models import CheckinLog
        _session(client, staff)
        _make_active_camp(db)
        db.session.commit()
        resp = client.post('/team/selbst-einchecken', follow_redirects=True)
        assert resp.status_code == 200
        log = CheckinLog.query.filter_by(
            staff_user_id=staff.id,
            registration_id=None,
            event_type='checkin'
        ).first()
        assert log is not None


# =============================================================================
# Approve independent travel
# =============================================================================

class TestIndependentTravel:

    def test_approve_sets_flag(self, client, staff, parent, db):
        from sub_modules.models import Registration
        _session(client, staff)
        camp = _make_active_camp(db)
        group = _make_group(db, camp)
        child = _make_child(db, parent)
        reg = _make_registration(db, child, camp, group)
        db.session.commit()
        resp = client.post(f'/team/kind/{reg.id}/heimweg',
                           follow_redirects=True)
        assert resp.status_code == 200
        db.session.refresh(reg)
        assert reg.independent_travel is True
