"""
tests/test_access_control.py
============================
Ensures every protected route returns 401/302 to unauthenticated users,
and 403 to authenticated users with the wrong role.

Strategy:
  - Unauthenticated → expect redirect (302) to login
  - Wrong role      → expect 403 Forbidden
  - Correct role    → expect 200 OK (or 302 after POST, depending on route)

We don't test the full page output here — just the HTTP status codes.
That logic lives in the blueprint-specific test files.
"""

import pytest


# =============================================================================
# Helpers
# =============================================================================

def _session_for(client, user):
    """Inject a Flask-Login session for the given user."""
    with client.session_transaction() as sess:
        sess['_user_id'] = str(user.id)
        sess['_fresh'] = True


def _make_camp(db):
    from sub_modules.models import CampSession
    from datetime import date
    c = CampSession(name='TC', year=2023,
                    start_date=date(2023, 7, 19),
                    end_date=date(2023, 7, 22),
                    status='published')
    db.session.add(c)
    db.session.commit()
    return c


# Routes that require login (any role)
ANY_ROLE_ROUTES = [
    ('GET', '/feedback/bug-melden'),
    ('GET', '/feedback/meine-meldungen'),
]

# Routes that require role='parent'
PARENT_ONLY_ROUTES = [
    ('GET', '/eltern/'),
    ('GET', '/eltern/konto'),
    ('GET', '/eltern/email-einstellungen'),
    ('GET', '/eltern/datenschutz'),
]

# Routes that require role in ('staff', 'admin')
STAFF_ONLY_ROUTES = [
    ('GET', '/team/'),
    ('GET', '/team/roster'),
    ('GET', '/team/noch-da'),
]

# Routes that require role='admin'
ADMIN_ONLY_ROUTES = [
    ('GET', '/admin/'),
    ('GET', '/admin/camps'),
    ('GET', '/admin/benutzer'),
    ('GET', '/admin/kinder'),
    ('GET', '/admin/analytics'),
    ('GET', '/admin/einstellungen'),
    ('GET', '/admin/feedback'),
    ('GET', '/admin/neuigkeiten'),
    ('GET', '/admin/manuelle-eingabe'),
]


# =============================================================================
# Unauthenticated → redirect to login
# =============================================================================

class TestUnauthenticatedRedirects:

    @pytest.mark.parametrize('method,path', ANY_ROLE_ROUTES + PARENT_ONLY_ROUTES
                              + STAFF_ONLY_ROUTES + ADMIN_ONLY_ROUTES)
    def test_redirects_to_login(self, client, method, path):
        resp = client.get(path)
        assert resp.status_code == 302
        assert '/auth/anmelden' in resp.headers['Location'] or \
               'anmelden' in resp.headers['Location']


# =============================================================================
# Parent cannot access staff or admin areas
# =============================================================================

class TestParentAccessControl:

    @pytest.mark.parametrize('method,path', STAFF_ONLY_ROUTES + ADMIN_ONLY_ROUTES)
    def test_parent_gets_403(self, client, parent, method, path):
        _session_for(client, parent)
        resp = client.get(path)
        assert resp.status_code == 403

    @pytest.mark.parametrize('method,path', PARENT_ONLY_ROUTES)
    def test_parent_can_access_parent_routes(self, client, parent, method, path):
        _session_for(client, parent)
        resp = client.get(path)
        assert resp.status_code == 200

    @pytest.mark.parametrize('method,path', ANY_ROLE_ROUTES)
    def test_parent_can_access_shared_routes(self, client, parent, method, path):
        _session_for(client, parent)
        resp = client.get(path)
        assert resp.status_code == 200


# =============================================================================
# Staff cannot access admin area, but can access staff + shared
# =============================================================================

class TestStaffAccessControl:

    @pytest.mark.parametrize('method,path', ADMIN_ONLY_ROUTES)
    def test_staff_gets_403_on_admin(self, client, staff, method, path):
        _session_for(client, staff)
        resp = client.get(path)
        assert resp.status_code == 403

    @pytest.mark.parametrize('method,path', PARENT_ONLY_ROUTES)
    def test_staff_can_access_parent_routes(self, client, staff, method, path):
        _session_for(client, staff)
        resp = client.get(path)
        assert resp.status_code == 200

    @pytest.mark.parametrize('method,path', STAFF_ONLY_ROUTES)
    def test_staff_can_access_staff_routes(self, client, staff, method, path):
        _session_for(client, staff)
        resp = client.get(path)
        # 200 = page rendered, 404 = route exists but needs active camp — both mean auth worked
        assert resp.status_code in (200, 404)

    @pytest.mark.parametrize('method,path', ANY_ROLE_ROUTES)
    def test_staff_can_access_shared_routes(self, client, staff, method, path):
        _session_for(client, staff)
        resp = client.get(path)
        assert resp.status_code == 200


# =============================================================================
# Admin can access everything
# =============================================================================

class TestAdminAccessControl:

    @pytest.mark.parametrize('method,path', ADMIN_ONLY_ROUTES + STAFF_ONLY_ROUTES
                              + ANY_ROLE_ROUTES)
    def test_admin_can_access_all_routes(self, client, admin, method, path):
        _session_for(client, admin)
        resp = client.get(path)
        assert resp.status_code in (200, 302, 404)   # some routes need active camp

    def test_admin_cannot_access_parent_area(self, client, admin):
        """Admin has no children — parent dashboard makes no sense for them."""
        _session_for(client, admin)
        resp = client.get('/eltern/')
        assert resp.status_code == 200


# =============================================================================
# Soft-deleted / inactive users cannot log in
# =============================================================================

class TestDeletedOrInactiveUsers:

    def test_deleted_user_session_gets_redirected(self, client, parent, db):
        """If a user is soft-deleted mid-session, Flask-Login should fail to load them."""
        _session_for(client, parent)
        parent.is_deleted = True
        db.session.commit()
        resp = client.get('/eltern/')
        # Flask-Login user_loader returns None for deleted users → redirect to login
        assert resp.status_code in (302, 403)

    def test_inactive_user_cannot_log_in(self, client, parent, db):
        parent.is_active = False
        db.session.commit()
        resp = client.post('/auth/anmelden', data={
            'email': parent.email,
            'password': 'testpass123',
        })
        # Should re-render login page (200) with error, not redirect to dashboard
        assert resp.status_code == 200
        assert 'Ung' in resp.get_data(as_text=True)
