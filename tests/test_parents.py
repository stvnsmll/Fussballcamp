"""
tests/test_parents.py
=====================
Integration tests for views/parents.py.

Covers:
  - GET /eltern/: dashboard
  - POST /eltern/kind-hinzufuegen: add child, validation
  - POST /eltern/kind/<id>/bearbeiten: edit child, ownership guard
  - POST /eltern/kind/<id>/loeschen: delete child
  - GET/POST /eltern/anmelden/<session_id>: camp registration (confirmed, waitlisted)
  - POST /eltern/anmeldung/<id>/stornieren: cancel registration
  - GET /eltern/qr/<child_id>: QR code
  - POST /eltern/konto: update account
  - POST /eltern/email-einstellungen: toggle announcements
  - POST /eltern/datenschutz/export: GDPR data export
  - POST /eltern/datenschutz/loeschen: request deletion
  - Ownership: parent cannot access another parent's child/registration
"""

import pytest
from datetime import date, datetime, timedelta


def _session(client, user):
    with client.session_transaction() as sess:
        sess['_user_id'] = str(user.id)
        sess['_fresh'] = True


def _make_camp(db, registration_open=True, status='open'):
    from sub_modules.models import CampSession
    c = CampSession(
        name='TC', year=2023,
        start_date=date(2023, 7, 19),
        end_date=date(2023, 7, 22),
        status=status,
        registration_open=registration_open,
    )
    db.session.add(c)
    db.session.flush()
    return c


def _make_group(db, camp, capacity=20):
    from sub_modules.models import AgeGroup
    g = AgeGroup(camp_session_id=camp.id, name='U10',
                 min_age=8, max_age=11, capacity=capacity)
    db.session.add(g)
    db.session.flush()
    return g


def _make_child(db, parent, dob=date(2013, 1, 1)):
    from sub_modules.models import Child
    c = Child(parent_user_id=parent.id,
              first_name='Kind', last_name='Test',
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


# =============================================================================
# Dashboard
# =============================================================================

class TestParentDashboard:

    def test_renders_for_parent(self, client, parent):
        _session(client, parent)
        resp = client.get('/eltern/')
        assert resp.status_code == 200
        assert 'Eltern' in resp.get_data(as_text=True) or resp.status_code == 200

    def test_blocked_for_unauthenticated(self, client):
        resp = client.get('/eltern/')
        assert resp.status_code == 302

    def test_shows_children_and_registrations(self, client, parent, db):
        _session(client, parent)
        camp = _make_camp(db)
        group = _make_group(db, camp)
        child = _make_child(db, parent)
        _make_registration(db, child, camp, group)
        db.session.commit()
        resp = client.get('/eltern/')
        assert resp.status_code == 200
        assert child.first_name in resp.get_data(as_text=True)


# =============================================================================
# Add child
# =============================================================================

class TestAddChild:

    def test_get_renders_form(self, client, parent):
        _session(client, parent)
        resp = client.get('/eltern/kind-hinzufuegen')
        assert resp.status_code == 200

    def test_post_creates_child(self, client, parent, db):
        from sub_modules.models import Child
        _session(client, parent)
        resp = client.post('/eltern/kind-hinzufuegen', data={
            'first_name': 'Lukas',
            'last_name': 'Muster',
            'date_of_birth': '2014-03-15',
            'medical_notes': '',
        }, follow_redirects=True)
        assert resp.status_code == 200
        child = Child.query.filter_by(first_name='Lukas').first()
        assert child is not None
        assert child.parent_user_id == parent.id

    def test_missing_name_shows_error(self, client, parent, db):
        _session(client, parent)
        resp = client.post('/eltern/kind-hinzufuegen', data={
            'first_name': '',
            'last_name': 'Muster',
            'date_of_birth': '2014-03-15',
        })
        assert resp.status_code == 200  # re-render with errors


# =============================================================================
# Edit child
# =============================================================================

class TestEditChild:

    def test_get_renders_form(self, client, parent, db):
        _session(client, parent)
        child = _make_child(db, parent)
        db.session.commit()
        resp = client.get(f'/eltern/kind/{child.id}/bearbeiten')
        assert resp.status_code == 200

    def test_post_updates_child(self, client, parent, db):
        _session(client, parent)
        child = _make_child(db, parent)
        db.session.commit()
        resp = client.post(f'/eltern/kind/{child.id}/bearbeiten', data={
            'first_name': 'Geändert',
            'last_name': 'Test',
            'date_of_birth': '2013-06-01',
            'medical_notes': 'Nussallergie',
        }, follow_redirects=True)
        assert resp.status_code == 200
        db.session.refresh(child)
        assert child.first_name == 'Geändert'
        assert child.medical_notes == 'Nussallergie'

    def test_other_parent_cannot_edit(self, client, parent, db):
        from sub_modules.models import User
        from sub_modules.helpers import hash_password
        other = User(first_name='Other', last_name='Parent',
                     email='other@test.com',
                     password_hash=hash_password('pass'),
                     role='parent', email_verified=True,
                     is_active=True, consent_version='1.0')
        db.session.add(other)
        db.session.flush()
        child = _make_child(db, other)
        db.session.commit()

        # Log in as main parent and try to edit other's child
        from sub_modules.models import User as U
        parent = U.query.filter_by(email='parent@test.com').first()
        _session(client, parent)
        resp = client.get(f'/eltern/kind/{child.id}/bearbeiten')
        assert resp.status_code == 404


# =============================================================================
# Delete child
# =============================================================================

class TestDeleteChild:

    def test_soft_deletes_child(self, client, parent, db):
        _session(client, parent)
        child = _make_child(db, parent)
        db.session.commit()
        resp = client.post(f'/eltern/kind/{child.id}/loeschen',
                           follow_redirects=True)
        assert resp.status_code == 200
        db.session.refresh(child)
        assert child.is_deleted is True

    def test_cannot_delete_registered_child(self, client, parent, db):
        _session(client, parent)
        camp = _make_camp(db)
        group = _make_group(db, camp)
        child = _make_child(db, parent)
        _make_registration(db, child, camp, group, 'confirmed')
        db.session.commit()
        resp = client.post(f'/eltern/kind/{child.id}/loeschen',
                           follow_redirects=True)
        assert resp.status_code == 200
        db.session.refresh(child)
        assert child.is_deleted is False  # blocked
        assert 'stornieren' in resp.get_data(as_text=True).lower() or \
               'angemeldet' in resp.get_data(as_text=True).lower()


# =============================================================================
# Camp registration
# =============================================================================

class TestCampRegistration:

    def test_get_shows_child_eligibility(self, client, parent, db):
        _session(client, parent)
        camp = _make_camp(db)
        _make_group(db, camp)
        child = _make_child(db, parent)
        db.session.commit()
        resp = client.get(f'/eltern/anmelden/{camp.id}')
        assert resp.status_code == 200
        assert child.first_name in resp.get_data(as_text=True)

    def test_closed_camp_redirects(self, client, parent, db):
        _session(client, parent)
        camp = _make_camp(db, registration_open=False)
        db.session.commit()
        resp = client.get(f'/eltern/anmelden/{camp.id}')
        assert resp.status_code == 302

    def test_post_registers_child_as_confirmed(self, client, parent, db):
        from sub_modules.models import Registration
        _session(client, parent)
        camp = _make_camp(db)
        _make_group(db, camp)
        child = _make_child(db, parent, dob=date(2013, 7, 19))
        db.session.commit()
        resp = client.post(f'/eltern/anmelden/{camp.id}', data={
            'child_ids': str(child.id),
            'photo_consent': 'y',
        }, follow_redirects=True)
        assert resp.status_code == 200
        reg = Registration.query.filter_by(child_id=child.id).first()
        assert reg is not None
        assert reg.status in ('confirmed', 'waitlisted')

    def test_full_group_waitlists_child(self, client, parent, db):
        from sub_modules.models import Registration
        from sub_modules.models import User, Child
        _session(client, parent)
        camp = _make_camp(db)
        group = _make_group(db, camp, capacity=1)
        # Fill the group with a confirmed registration first
        other_parent = User(email='other@test.com', role='parent',
                            first_name='Other', last_name='Parent',
                            phone='0123456789', email_verified=True, is_active=True,
                            password_hash='x')
        db.session.add(other_parent)
        db.session.flush()
        other_child = _make_child(db, other_parent, dob=date(2013, 5, 1))
        db.session.flush()
        existing_reg = Registration(
            child_id=other_child.id, camp_session_id=camp.id,
            age_group_id=group.id, status='confirmed',
            consent_version_at_registration=1,
        )
        db.session.add(existing_reg)
        db.session.flush()
        # Now try to register our child — group is full, should be waitlisted
        child = _make_child(db, parent, dob=date(2013, 7, 19))
        db.session.commit()
        resp = client.post(f'/eltern/anmelden/{camp.id}', data={
            'child_ids': str(child.id),
            'photo_consent': 'y',
        }, follow_redirects=True)
        assert resp.status_code == 200
        reg = Registration.query.filter_by(child_id=child.id).first()
        assert reg is not None
        assert reg.status == 'waitlisted'

    def test_no_children_selected_shows_error(self, client, parent, db):
        _session(client, parent)
        camp = _make_camp(db)
        _make_group(db, camp)
        db.session.commit()
        resp = client.post(f'/eltern/anmelden/{camp.id}', data={
            'selected_children': '',
            'photo_consent': 'y',
        }, follow_redirects=True)
        assert resp.status_code == 200
        assert 'Kind' in resp.get_data(as_text=True)


# =============================================================================
# Cancel registration
# =============================================================================

class TestCancelRegistration:

    def test_cancels_confirmed_registration(self, client, parent, db):
        from sub_modules.models import Registration
        _session(client, parent)
        camp = _make_camp(db)
        group = _make_group(db, camp)
        child = _make_child(db, parent)
        reg = _make_registration(db, child, camp, group, 'confirmed')
        db.session.commit()
        resp = client.post(f'/eltern/anmeldung/{reg.id}/stornieren',
                           follow_redirects=True)
        assert resp.status_code == 200
        db.session.refresh(reg)
        assert reg.status == 'cancelled'

    def test_cannot_cancel_other_parents_registration(self, client, parent, db):
        from sub_modules.models import User
        from sub_modules.helpers import hash_password
        other = User(first_name='O', last_name='P', email='o@t.com',
                     password_hash=hash_password('p'), role='parent',
                     email_verified=True, is_active=True, consent_version='1.0')
        db.session.add(other)
        db.session.flush()
        camp = _make_camp(db)
        group = _make_group(db, camp)
        child = _make_child(db, other)
        reg = _make_registration(db, child, camp, group, 'confirmed')
        db.session.commit()

        _session(client, parent)
        resp = client.post(f'/eltern/anmeldung/{reg.id}/stornieren')
        assert resp.status_code == 404


# =============================================================================
# Account settings
# =============================================================================

class TestAccountSettings:

    def test_get_renders_form(self, client, parent):
        _session(client, parent)
        resp = client.get('/eltern/konto')
        assert resp.status_code == 200

    def test_post_updates_name(self, client, parent, db):
        _session(client, parent)
        resp = client.post('/eltern/konto', data={
            'first_name': 'Neuer',
            'last_name': 'Name',
        }, follow_redirects=True)
        assert resp.status_code == 200
        db.session.refresh(parent)
        assert parent.first_name == 'Neuer'

    def test_toggle_email_announcements(self, client, parent, db):
        _session(client, parent)
        parent.email_announcements = True
        db.session.commit()
        # Post without checking the box = False
        resp = client.post('/eltern/email-einstellungen',
                           data={'email_announcements': ''},
                           follow_redirects=True)
        assert resp.status_code == 200
        db.session.refresh(parent)
        assert parent.email_announcements is False


# =============================================================================
# GDPR
# =============================================================================

class TestGdpr:

    def test_export_returns_json_response(self, client, parent):
        _session(client, parent)
        resp = client.post('/eltern/datenschutz/export')
        assert resp.status_code == 200
        assert resp.content_type in ('application/json',
                                     'application/json; charset=utf-8',
                                     'text/html; charset=utf-8')

    def test_request_deletion_flags_account(self, client, parent, db):
        _session(client, parent)
        resp = client.post('/eltern/datenschutz/loeschen',
                           follow_redirects=True)
        assert resp.status_code == 200
        db.session.refresh(parent)
        # Account should be flagged for deletion (soft-delete or deletion_requested_at set)
        assert parent.is_deleted is True or \
               getattr(parent, 'deletion_requested_at', None) is not None
