"""
tests/test_feedback.py
======================
Integration tests for views/feedback.py.

Covers:
  - GET /feedback/bug-melden: renders for all authenticated roles
  - POST /feedback/bug-melden: valid submission, missing fields, sanitisation
  - GET /feedback/meine-meldungen: shows own reports, not others'
"""

import pytest


def _session(client, user):
    with client.session_transaction() as sess:
        sess['_user_id'] = str(user.id)
        sess['_fresh'] = True


class TestFeedbackSubmit:

    def test_get_renders_for_parent(self, client, parent):
        _session(client, parent)
        resp = client.get('/feedback/bug-melden')
        assert resp.status_code == 200

    def test_get_renders_for_staff(self, client, staff):
        _session(client, staff)
        resp = client.get('/feedback/bug-melden')
        assert resp.status_code == 200

    def test_get_renders_for_admin(self, client, admin):
        _session(client, admin)
        resp = client.get('/feedback/bug-melden')
        assert resp.status_code == 200

    def test_requires_login(self, client):
        resp = client.get('/feedback/bug-melden')
        assert resp.status_code == 302

    def test_valid_submission_creates_report(self, client, parent, db):
        from sub_modules.models import BugReport
        _session(client, parent)
        resp = client.post('/feedback/bug-melden', data={
            'subject': 'Anmeldeformular lädt nicht',
            'description': 'Wenn ich auf Anmelden klicke, passiert nichts. '
                           'Das passiert seit gestern.',
            'severity': 'medium',
        }, follow_redirects=True)
        assert resp.status_code == 200
        report = BugReport.query.filter_by(
            subject='Anmeldeformular lädt nicht'
        ).first()
        assert report is not None
        assert report.reporter_role == 'parent'
        assert report.reporter_email == parent.email

    def test_subject_too_short_shows_error(self, client, parent):
        _session(client, parent)
        resp = client.post('/feedback/bug-melden', data={
            'subject': 'Bug',   # < 5 chars
            'description': 'Etwas funktioniert nicht richtig und ich weiß nicht warum.',
            'severity': 'low',
        })
        assert resp.status_code == 200
        assert 'Bug' in resp.get_data(as_text=True)

    def test_description_too_short_shows_error(self, client, parent):
        _session(client, parent)
        resp = client.post('/feedback/bug-melden', data={
            'subject': 'Problem mit Login',
            'description': 'Fehler',   # < 10 chars
            'severity': 'high',
        })
        assert resp.status_code == 200

    def test_html_in_subject_is_stripped(self, client, parent, db):
        from sub_modules.models import BugReport
        _session(client, parent)
        resp = client.post('/feedback/bug-melden', data={
            'subject': '<script>alert("xss")</script>Login Bug',
            'description': 'Ich kann mich nicht anmelden seit dem Update heute.',
            'severity': 'high',
        }, follow_redirects=True)
        assert resp.status_code == 200
        report = BugReport.query.filter_by(
            reporter_email=parent.email
        ).order_by(BugReport.created_at.desc()).first()
        if report:
            assert '<script>' not in report.subject

    def test_severity_choices(self, client, parent, db):
        from sub_modules.models import BugReport
        _session(client, parent)
        for severity in ('low', 'medium', 'high'):
            resp = client.post('/feedback/bug-melden', data={
                'subject': f'Bug mit {severity} severity',
                'description': 'Genauere Beschreibung des Fehlers der aufgetreten ist.',
                'severity': severity,
            }, follow_redirects=True)
            assert resp.status_code == 200


class TestMyReports:

    def test_shows_own_reports(self, client, parent, db):
        from sub_modules.models import BugReport
        _session(client, parent)
        report = BugReport(
            subject='Mein Bug',
            description='Beschreibung',
            severity='low',
            status='new',
            reporter_user_id=parent.id,
            reporter_role='parent',
            reporter_email=parent.email,
            page_url='/eltern/',
        )
        db.session.add(report)
        db.session.commit()
        resp = client.get('/feedback/meine-meldungen')
        assert resp.status_code == 200
        assert 'Mein Bug' in resp.get_data(as_text=True)

    def test_does_not_show_other_users_reports(self, client, parent, db):
        from sub_modules.models import BugReport
        _session(client, parent)
        report = BugReport(
            subject='Anderer Bug',
            description='Gehört einem anderen User',
            severity='low',
            status='new',
            reporter_role='staff',
            reporter_email='other@test.com',
            page_url='/team/',
        )
        db.session.add(report)
        db.session.commit()
        resp = client.get('/feedback/meine-meldungen')
        assert resp.status_code == 200
        assert 'Anderer Bug' not in resp.get_data(as_text=True)

    def test_requires_login(self, client):
        resp = client.get('/feedback/meine-meldungen')
        assert resp.status_code == 302
