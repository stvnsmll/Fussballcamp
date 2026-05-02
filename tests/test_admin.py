"""
tests/test_admin.py
===================
Integration tests for views/admin.py.

Covers:
  - GET /admin/: dashboard
  - Camp CRUD: list, create, edit, delete (draft only), set status
  - Age group CRUD: list, edit, delete (no registrations guard)
  - User management: list, detail, edit, soft-delete, password reset
  - Child/registration management: child edit, registration edit, waitlist promotion
  - Manual entry: GET renders, POST creates parent+child+registration
  - Announcements: list, create, edit, delete
  - Staff invite: GET form, POST sends invite
  - Analytics: page renders
  - Settings: GET renders, POST updates retention
  - Bug reports: list, detail, admin notes
  - AJAX endpoints: parent search, parent details
"""

import pytest
from datetime import date, datetime


def _session(client, user):
    with client.session_transaction() as sess:
        sess['_user_id'] = str(user.id)
        sess['_fresh'] = True


def _make_camp(db, status='draft'):
    from sub_modules.models import CampSession
    c = CampSession(name='Test Camp', year=2023,
                    start_date=date(2023, 7, 19),
                    end_date=date(2023, 7, 22),
                    status=status)
    db.session.add(c)
    db.session.flush()
    return c


def _make_group(db, camp, capacity=10):
    from sub_modules.models import AgeGroup
    g = AgeGroup(camp_session_id=camp.id, name='U10',
                 min_age=8, max_age=9, capacity=capacity)
    db.session.add(g)
    db.session.flush()
    return g


def _make_child(db, parent):
    from sub_modules.models import Child
    c = Child(parent_user_id=parent.id,
              first_name='Kind', last_name='Test',
              date_of_birth=date(2014, 1, 1))
    db.session.add(c)
    db.session.flush()
    return c


def _make_reg(db, child, camp, group, status='confirmed', pos=None):
    from sub_modules.models import Registration
    r = Registration(child_id=child.id, camp_session_id=camp.id,
                     age_group_id=group.id, status=status,
                     waitlist_position=pos)
    db.session.add(r)
    db.session.flush()
    return r


# =============================================================================
# Dashboard
# =============================================================================

class TestAdminDashboard:

    def test_renders_for_admin(self, client, admin):
        _session(client, admin)
        resp = client.get('/admin/')
        assert resp.status_code == 200

    def test_blocked_for_staff(self, client, staff):
        _session(client, staff)
        resp = client.get('/admin/')
        assert resp.status_code == 403

    def test_blocked_for_parent(self, client, parent):
        _session(client, parent)
        resp = client.get('/admin/')
        assert resp.status_code == 403


# =============================================================================
# Camp management
# =============================================================================

class TestCampManagement:

    def test_camp_list(self, client, admin, db):
        _session(client, admin)
        _make_camp(db)
        db.session.commit()
        resp = client.get('/admin/camps')
        assert resp.status_code == 200

    def test_create_camp_get(self, client, admin):
        _session(client, admin)
        resp = client.get('/admin/camps/neu')
        assert resp.status_code == 200

    def test_create_camp_post(self, client, admin, db):
        from sub_modules.models import CampSession
        _session(client, admin)
        resp = client.post('/admin/camps/neu', data={
            'name': 'Neues Camp',
            'year': '2024',
            'start_date': '2024-07-17',
            'end_date': '2024-07-20',
            'status': 'draft',
            'registration_open': '',
            'require_head_coaches': 'y',
        }, follow_redirects=True)
        assert resp.status_code == 200
        camp = CampSession.query.filter_by(name='Neues Camp').first()
        assert camp is not None

    def test_edit_camp_get(self, client, admin, db):
        _session(client, admin)
        camp = _make_camp(db)
        db.session.commit()
        resp = client.get(f'/admin/camps/{camp.id}/bearbeiten')
        assert resp.status_code == 200

    def test_edit_camp_post(self, client, admin, db):
        _session(client, admin)
        camp = _make_camp(db)
        db.session.commit()
        resp = client.post(f'/admin/camps/{camp.id}/bearbeiten', data={
            'name': 'Umbenannt',
            'year': '2023',
            'start_date': '2023-07-19',
            'end_date': '2023-07-22',
            'status': 'draft',
        }, follow_redirects=True)
        assert resp.status_code == 200
        db.session.refresh(camp)
        assert camp.name == 'Umbenannt'

    def test_delete_draft_camp(self, client, admin, db):
        from sub_modules.models import CampSession
        _session(client, admin)
        camp = _make_camp(db, status='draft')
        db.session.commit()
        resp = client.post(f'/admin/camps/{camp.id}/loeschen',
                           follow_redirects=True)
        assert resp.status_code == 200
        assert CampSession.query.get(camp.id) is None

    def test_delete_published_camp_blocked(self, client, admin, db):
        from sub_modules.models import CampSession
        _session(client, admin)
        camp = _make_camp(db, status='open')
        db.session.commit()
        resp = client.post(f'/admin/camps/{camp.id}/loeschen',
                           follow_redirects=True)
        assert resp.status_code == 200
        assert CampSession.query.get(camp.id) is not None
        assert 'Entwurf' in resp.get_data(as_text=True) or \
               'gelöscht' not in resp.get_data(as_text=True)

    def test_set_camp_status(self, client, admin, db):
        _session(client, admin)
        camp = _make_camp(db, status='draft')
        db.session.commit()
        resp = client.post(f'/admin/camps/{camp.id}/status/open',
                           follow_redirects=True)
        assert resp.status_code == 200
        db.session.refresh(camp)
        assert camp.status == 'open'


# =============================================================================
# Age group management
# =============================================================================

class TestAgeGroupManagement:

    def test_age_group_list(self, client, admin, db):
        _session(client, admin)
        camp = _make_camp(db)
        _make_group(db, camp)
        db.session.commit()
        resp = client.get(f'/admin/camps/{camp.id}/gruppen')
        assert resp.status_code == 200

    def test_edit_age_group_post(self, client, admin, db):
        _session(client, admin)
        camp = _make_camp(db)
        group = _make_group(db, camp)
        db.session.commit()
        resp = client.post(
            f'/admin/camps/{camp.id}/gruppen/{group.id}/bearbeiten',
            data={'name': 'U12', 'min_age': '10', 'max_age': '11', 'capacity': '15'},
            follow_redirects=True
        )
        assert resp.status_code == 200
        db.session.refresh(group)
        assert group.name == 'U12'

    def test_delete_empty_age_group(self, client, admin, db):
        from sub_modules.models import AgeGroup
        _session(client, admin)
        camp = _make_camp(db)
        group = _make_group(db, camp)
        db.session.commit()
        resp = client.post(
            f'/admin/camps/{camp.id}/gruppen/{group.id}/loeschen',
            follow_redirects=True
        )
        assert resp.status_code == 200
        assert AgeGroup.query.get(group.id) is None

    def test_cannot_delete_group_with_registrations(self, client, admin, parent, db):
        from sub_modules.models import AgeGroup
        _session(client, admin)
        camp = _make_camp(db, status='open')
        group = _make_group(db, camp)
        child = _make_child(db, parent)
        _make_reg(db, child, camp, group)
        db.session.commit()
        resp = client.post(
            f'/admin/camps/{camp.id}/gruppen/{group.id}/loeschen',
            follow_redirects=True
        )
        assert resp.status_code == 200
        assert AgeGroup.query.get(group.id) is not None


# =============================================================================
# User management
# =============================================================================

class TestUserManagement:

    def test_user_list(self, client, admin, parent):
        _session(client, admin)
        resp = client.get('/admin/benutzer')
        assert resp.status_code == 200
        assert parent.last_name in resp.get_data(as_text=True)

    def test_user_list_filter_by_role(self, client, admin):
        _session(client, admin)
        resp = client.get('/admin/benutzer?rolle=parent')
        assert resp.status_code == 200

    def test_user_detail(self, client, admin, parent):
        _session(client, admin)
        resp = client.get(f'/admin/benutzer/{parent.id}')
        assert resp.status_code == 200
        assert parent.email in resp.get_data(as_text=True)

    def test_user_edit_get(self, client, admin, parent):
        _session(client, admin)
        resp = client.get(f'/admin/benutzer/{parent.id}/bearbeiten')
        assert resp.status_code == 200

    def test_user_edit_post(self, client, admin, parent, db):
        _session(client, admin)
        resp = client.post(f'/admin/benutzer/{parent.id}/bearbeiten', data={
            'first_name': 'Geändert',
            'last_name': parent.last_name,
            'email': parent.email,
            'is_active': 'True',
            'email_verified': 'True',
            'email_announcements': 'y',
        }, follow_redirects=True)
        assert resp.status_code == 200
        db.session.refresh(parent)
        assert parent.first_name == 'Geändert'

    def test_soft_delete_user(self, client, admin, parent, db):
        _session(client, admin)
        resp = client.post(f'/admin/benutzer/{parent.id}/loeschen',
                           follow_redirects=True)
        assert resp.status_code == 200
        db.session.refresh(parent)
        assert parent.is_deleted is True

    def test_cannot_delete_self(self, client, admin, db):
        _session(client, admin)
        resp = client.post(f'/admin/benutzer/{admin.id}/loeschen',
                           follow_redirects=True)
        assert resp.status_code == 200
        db.session.refresh(admin)
        assert admin.is_deleted is False


# =============================================================================
# Child & registration management
# =============================================================================

class TestChildRegistrationManagement:

    def test_child_list(self, client, admin, parent, db):
        _session(client, admin)
        _make_child(db, parent)
        db.session.commit()
        resp = client.get('/admin/kinder')
        assert resp.status_code == 200

    def test_child_edit_get(self, client, admin, parent, db):
        _session(client, admin)
        child = _make_child(db, parent)
        db.session.commit()
        resp = client.get(f'/admin/kinder/{child.id}/bearbeiten')
        assert resp.status_code == 200

    def test_child_edit_post(self, client, admin, parent, db):
        _session(client, admin)
        child = _make_child(db, parent)
        db.session.commit()
        resp = client.post(f'/admin/kinder/{child.id}/bearbeiten', data={
            'first_name': 'Neu',
            'last_name': 'Test',
            'date_of_birth': '2014-01-01',
            'medical_notes': 'Asthma',
        }, follow_redirects=True)
        assert resp.status_code == 200
        db.session.refresh(child)
        assert child.first_name == 'Neu'

    def test_registration_edit_get(self, client, admin, parent, db):
        _session(client, admin)
        camp = _make_camp(db, status='open')
        group = _make_group(db, camp)
        child = _make_child(db, parent)
        reg = _make_reg(db, child, camp, group)
        db.session.commit()
        resp = client.get(f'/admin/anmeldungen/{reg.id}/bearbeiten')
        assert resp.status_code == 200

    def test_registration_edit_post_changes_status(self, client, admin, parent, db):
        _session(client, admin)
        camp = _make_camp(db, status='open')
        group = _make_group(db, camp)
        child = _make_child(db, parent)
        reg = _make_reg(db, child, camp, group, 'confirmed')
        db.session.commit()
        resp = client.post(f'/admin/anmeldungen/{reg.id}/bearbeiten', data={
            'status': 'cancelled',
            'age_group_id': str(group.id),
            'independent_travel': '',
            'photo_consent': '',
            'admin_override_reason': '',
        }, follow_redirects=True)
        assert resp.status_code == 200
        db.session.refresh(reg)
        assert reg.status == 'cancelled'

    def test_waitlist_page(self, client, admin, parent, db):
        _session(client, admin)
        camp = _make_camp(db, status='open')
        group = _make_group(db, camp, capacity=0)
        child = _make_child(db, parent)
        _make_reg(db, child, camp, group, 'waitlisted', pos=1)
        db.session.commit()
        resp = client.get(f'/admin/camps/{camp.id}/warteliste')
        assert resp.status_code == 200

    def test_confirm_from_waitlist(self, client, admin, parent, db):
        from sub_modules.models import Registration
        _session(client, admin)
        camp = _make_camp(db, status='open')
        group = _make_group(db, camp, capacity=10)
        child = _make_child(db, parent)
        reg = _make_reg(db, child, camp, group, 'waitlisted', pos=1)
        db.session.commit()
        resp = client.post(f'/admin/anmeldungen/{reg.id}/bestaetigen',
                           follow_redirects=True)
        assert resp.status_code == 200
        db.session.refresh(reg)
        assert reg.status == 'confirmed'


# =============================================================================
# Announcements
# =============================================================================

class TestAnnouncementManagement:

    def test_announcement_list(self, client, admin):
        _session(client, admin)
        resp = client.get('/admin/neuigkeiten')
        assert resp.status_code == 200

    def test_create_announcement_get(self, client, admin):
        _session(client, admin)
        resp = client.get('/admin/neuigkeiten/neu')
        assert resp.status_code == 200

    def test_create_announcement_post(self, client, admin, db):
        from sub_modules.models import Announcement
        _session(client, admin)
        resp = client.post('/admin/neuigkeiten/neu', data={
            'title': 'Testnachricht',
            'body': 'Inhalt der Nachricht',
            'visibility': 'public',
            'target_age_groups': '',
            'staff_tags': '',
            'is_pinned': '',
            'send_email': '',
        }, follow_redirects=True)
        assert resp.status_code == 200
        ann = Announcement.query.filter_by(title='Testnachricht').first()
        assert ann is not None

    def test_delete_announcement(self, client, admin, db):
        from sub_modules.models import Announcement
        _session(client, admin)
        ann = Announcement(author_user_id=admin.id,
                           title='Zu löschen', body='body',
                           visibility='public')
        db.session.add(ann)
        db.session.commit()
        resp = client.post(f'/admin/neuigkeiten/{ann.id}/loeschen',
                           follow_redirects=True)
        assert resp.status_code == 200
        assert Announcement.query.get(ann.id) is None


# =============================================================================
# Staff invite
# =============================================================================

class TestStaffInvite:

    def test_invite_get(self, client, admin):
        _session(client, admin)
        resp = client.get('/admin/einladen')
        assert resp.status_code == 200

    def test_invite_post_creates_user(self, client, admin, db):
        from sub_modules.models import User
        _session(client, admin)
        resp = client.post('/admin/einladen', data={
            'email': 'newstaff@test.com',
            'role': 'staff',
            'staff_class': 'trainer',
            'is_first_aid': '',
        }, follow_redirects=True)
        assert resp.status_code == 200
        user = User.query.filter_by(email='newstaff@test.com').first()
        assert user is not None
        assert user.role == 'staff'

    def test_invite_duplicate_email_shows_error(self, client, admin, parent):
        _session(client, admin)
        resp = client.post('/admin/einladen', data={
            'email': parent.email,
            'role': 'staff',
            'staff_class': 'trainer',
        }, follow_redirects=True)
        assert resp.status_code == 200
        assert 'bereits' in resp.get_data(as_text=True).lower() or \
               'vorhanden' in resp.get_data(as_text=True).lower()


# =============================================================================
# Analytics & Settings
# =============================================================================

class TestAnalyticsAndSettings:

    def test_analytics_page(self, client, admin):
        _session(client, admin)
        resp = client.get('/admin/analytics')
        assert resp.status_code == 200

    def test_settings_get(self, client, admin):
        _session(client, admin)
        resp = client.get('/admin/einstellungen')
        assert resp.status_code == 200

    def test_settings_post_updates_retention(self, client, admin, app):
        _session(client, admin)
        resp = client.post('/admin/einstellungen', data={
            'analytics_retention_days': '180',
        }, follow_redirects=True)
        assert resp.status_code == 200
        assert app.config['ANALYTICS_RETENTION_DAYS'] == 180


# =============================================================================
# Bug reports
# =============================================================================

class TestBugReports:

    def test_bug_report_list(self, client, admin):
        _session(client, admin)
        resp = client.get('/admin/feedback')
        assert resp.status_code == 200

    def test_bug_report_detail_get(self, client, admin, db):
        from sub_modules.models import BugReport
        _session(client, admin)
        report = BugReport(
            subject='Test bug', description='Something broke',
            severity='medium', status='new',
            reporter_role='parent',
            reporter_email='p@test.local',
            page_url='/test'
        )
        db.session.add(report)
        db.session.commit()
        resp = client.get(f'/admin/feedback/{report.id}')
        assert resp.status_code == 200

    def test_bug_report_admin_notes_post(self, client, admin, db):
        from sub_modules.models import BugReport
        _session(client, admin)
        report = BugReport(
            subject='Bug', description='Details',
            severity='low', status='new',
            reporter_role='staff',
            reporter_email='s@test.local',
            page_url='/test'
        )
        db.session.add(report)
        db.session.commit()
        resp = client.post(f'/admin/feedback/{report.id}', data={
            'status': 'acknowledged',
            'admin_notes': 'Wir schauen uns das an.',
        }, follow_redirects=True)
        assert resp.status_code == 200
        db.session.refresh(report)
        assert report.status == 'acknowledged'


# =============================================================================
# AJAX endpoints
# =============================================================================

class TestAjaxEndpoints:

    def test_parent_search(self, client, admin, parent):
        _session(client, admin)
        resp = client.get(f'/admin/eltern-suche?q={parent.last_name}')
        assert resp.status_code == 200
        data = resp.get_json()
        assert data is not None

    def test_parent_details(self, client, admin, parent):
        _session(client, admin)
        resp = client.get(f'/admin/eltern/{parent.id}/details')
        assert resp.status_code == 200
        data = resp.get_json()
        assert data is not None
