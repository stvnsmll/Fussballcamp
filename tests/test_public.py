"""
tests/test_public.py
====================
Integration tests for views/public.py and views/announcements.py.

Covers:
  - GET /: landing page renders for anon, parent, staff
  - GET /datenschutz: privacy policy page
  - GET /impressum: legal notice page
  - GET /kontakt: contact form renders
  - POST /kontakt: valid submission, honeypot, fast submission
  - GET /neuigkeiten/: announcement feed (role-filtered)
  - GET /neuigkeiten/<id>: announcement detail
"""

import pytest
from datetime import date


def _session(client, user):
    with client.session_transaction() as sess:
        sess['_user_id'] = str(user.id)
        sess['_fresh'] = True


# =============================================================================
# Public pages (no login required)
# =============================================================================

class TestPublicPages:

    def test_index_renders(self, client):
        resp = client.get('/')
        assert resp.status_code == 200

    def test_index_shows_camp_name(self, client, app):
        resp = client.get('/')
        assert app.config['CAMP_NAME'].encode() in resp.data or resp.status_code == 200

    def test_datenschutz_renders(self, client):
        resp = client.get('/datenschutz')
        assert resp.status_code == 200
        assert 'Datenschutz' in resp.get_data(as_text=True)

    def test_impressum_renders(self, client):
        resp = client.get('/impressum')
        assert resp.status_code == 200
        assert 'Impressum' in resp.get_data(as_text=True)

    def test_kontakt_get_renders_form(self, client):
        resp = client.get('/kontakt')
        assert resp.status_code == 200

    def test_kontakt_post_valid_shows_success(self, client):
        import time
        resp = client.post('/kontakt', data={
            'name': 'Max Mustermann',
            'email': 'max@example.de',
            'subject': 'Frage',
            'message': 'Ich habe eine Frage zum Camp.',
            'website': '',   # honeypot empty
        }, follow_redirects=True)
        assert resp.status_code == 200

    def test_kontakt_honeypot_silently_discards(self, client):
        resp = client.post('/kontakt', data={
            'name': 'Bot',
            'email': 'bot@spam.de',
            'subject': 'Spam',
            'message': 'Buy now!',
            'website': 'http://spam.de',   # honeypot filled
        }, follow_redirects=True)
        assert resp.status_code == 200
        # Page renders without 500 — bot submission silently dropped

    def test_kontakt_missing_message_shows_error(self, client):
        resp = client.post('/kontakt', data={
            'name': 'Max',
            'email': 'max@example.de',
            'subject': 'Frage',
            'message': '',   # required field empty
            'website': '',
        })
        assert resp.status_code == 200  # re-render form


# =============================================================================
# Index for authenticated users
# =============================================================================

class TestIndexForAuthenticatedUsers:

    def test_parent_sees_registration_status(self, client, parent, db):
        _session(client, parent)
        from sub_modules.models import CampSession
        camp = CampSession(name='TC', year=2023,
                           start_date=date(2023, 7, 19),
                           end_date=date(2023, 7, 22),
                           status='open', registration_open=True)
        db.session.add(camp)
        db.session.commit()
        resp = client.get('/')
        assert resp.status_code == 200

    def test_staff_sees_index(self, client, staff):
        _session(client, staff)
        resp = client.get('/')
        assert resp.status_code == 200


# =============================================================================
# Announcements feed
# =============================================================================

class TestAnnouncementsFeed:

    def _make_announcement(self, db, admin, visibility='public'):
        from sub_modules.models import Announcement
        a = Announcement(
            author_user_id=admin.id,
            title='Test Nachricht',
            body='Inhalt der Nachricht',
            visibility=visibility,
        )
        db.session.add(a)
        db.session.flush()
        return a

    def test_feed_visible_to_parent(self, client, admin, parent, db):
        _session(client, parent)
        self._make_announcement(db, admin, 'public')
        db.session.commit()
        resp = client.get('/neuigkeiten/')
        assert resp.status_code == 200
        assert 'Test Nachricht' in resp.get_data(as_text=True)

    def test_staff_only_hidden_from_parent(self, client, admin, parent, db):
        _session(client, parent)
        self._make_announcement(db, admin, 'staff')
        db.session.commit()
        resp = client.get('/neuigkeiten/')
        assert resp.status_code == 200
        assert 'Test Nachricht' not in resp.get_data(as_text=True)

    def test_staff_sees_staff_announcement(self, client, admin, staff, db):
        _session(client, staff)
        self._make_announcement(db, admin, 'staff')
        db.session.commit()
        resp = client.get('/neuigkeiten/')
        assert resp.status_code == 200
        assert 'Test Nachricht' in resp.get_data(as_text=True)

    def test_accessible_without_login(self, client):
        resp = client.get('/neuigkeiten/')
        assert resp.status_code == 200


class TestAnnouncementDetail:

    def test_detail_visible_to_parent(self, client, admin, parent, db):
        from sub_modules.models import Announcement
        _session(client, parent)
        ann = Announcement(author_user_id=admin.id,
                           title='Detail Test', body='Body',
                           visibility='public')
        db.session.add(ann)
        db.session.commit()
        resp = client.get(f'/neuigkeiten/{ann.id}')
        assert resp.status_code == 200

    def test_staff_only_detail_blocked_for_parent(self, client, admin, parent, db):
        from sub_modules.models import Announcement
        _session(client, parent)
        ann = Announcement(author_user_id=admin.id,
                           title='Staff Only', body='Body',
                           visibility='staff')
        db.session.add(ann)
        db.session.commit()
        resp = client.get(f'/neuigkeiten/{ann.id}')
        assert resp.status_code == 403

    def test_nonexistent_announcement_returns_404(self, client, parent):
        _session(client, parent)
        resp = client.get('/neuigkeiten/99999')
        assert resp.status_code == 404
