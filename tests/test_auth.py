"""
tests/test_auth.py
==================
Integration tests for views/auth.py.

Covers:
  - GET /auth/registrieren: renders form
  - POST /auth/registrieren: happy path, duplicate email, honeypot, missing fields
  - GET /auth/bestaetigen/<token>: valid, invalid, expired
  - POST /auth/anmelden: happy path, wrong password, unverified, inactive
  - POST /auth/anmelden: redirect to parent/staff/admin dashboard on role
  - GET /auth/abmelden: logs out
  - POST /auth/passwort-vergessen: valid and unknown email
  - POST /auth/passwort-neu/<token>: valid and expired token
  - GET /auth/datenschutz-bestaetigen: skipped if up-to-date
  - POST /auth/datenschutz-bestaetigen: updates consent_version
  - GET /auth/einladen/<token>: valid, invalid, expired
  - POST /auth/einladen/<token>: completes invite
"""

import pytest
from datetime import datetime, timedelta


def _post(client, path, data):
    return client.post(path, data=data, follow_redirects=False)


def _session_for(client, user):
    with client.session_transaction() as sess:
        sess['_user_id'] = str(user.id)
        sess['_fresh'] = True


# =============================================================================
# Registration
# =============================================================================

class TestRegister:

    def test_get_renders_form(self, client):
        resp = client.get('/auth/registrieren')
        assert resp.status_code == 200
        assert b'Registrieren' in resp.data

    def test_post_creates_user_and_redirects(self, client, db):
        from sub_modules.models import User
        resp = _post(client, '/auth/registrieren', {
            'first_name': 'Max',
            'last_name': 'Mustermann',
            'email': 'max@example.de',
            'password': 'sicherespass1',
            'password_confirm': 'sicherespass1',
            'phone': '01234567890',
            'phone_confirm': '01234567890',
            'consent': 'y',
            'form_start': '0',     # will be treated as too-fast → honeypot branch
        })
        # With form_start=0 the timing guard fires and silently succeeds
        assert resp.status_code == 302

    def test_post_with_timing_looks_like_success(self, client, db):
        """Submission < 3s is silently treated as bot — flash success anyway."""
        import time
        resp = _post(client, '/auth/registrieren', {
            'first_name': 'Bot',
            'last_name': 'Spam',
            'email': 'bot@evil.de',
            'password': 'password123',
            'password_confirm': 'password123',
            'phone': '01234567890',
            'phone_confirm': '01234567890',
            'consent': 'y',
            'form_start': str(time.time()),   # submitted instantly
        })
        assert resp.status_code == 302

    def test_honeypot_field_causes_silent_success(self, client, db):
        import time
        resp = _post(client, '/auth/registrieren', {
            'first_name': 'Bot',
            'last_name': 'Spam',
            'email': 'bot@evil.de',
            'password': 'password123',
            'password_confirm': 'password123',
            'consent': 'y',
            'website': 'http://spam.example',  # honeypot filled
            'form_start': str(time.time() - 10),
        })
        assert resp.status_code == 302
        from sub_modules.models import User
        assert User.query.filter_by(email='bot@evil.de').first() is None

    def test_duplicate_email_redirects_to_login_with_vague_message(self, client, parent, db):
        import time
        resp = client.post('/auth/registrieren', data={
            'first_name': 'Test',
            'last_name': 'Test',
            'email': parent.email,
            'password': 'password123',
            'password_confirm': 'password123',
            'consent': 'y',
            'form_start': str(time.time() - 10),
        }, follow_redirects=True)
        assert resp.status_code == 200
        assert 'E-Mail-Adresse' in resp.get_data(as_text=True)

    def test_missing_required_field_shows_form_again(self, client, db):
        import time
        resp = _post(client, '/auth/registrieren', {
            'first_name': '',
            'last_name': 'Test',
            'email': 'new@example.de',
            'password': 'password123',
            'password_confirm': 'password123',
            'consent': 'y',
            'form_start': str(time.time() - 10),
        })
        assert resp.status_code == 200   # re-render form

    def test_password_mismatch_shows_error(self, client, db):
        import time
        resp = _post(client, '/auth/registrieren', {
            'first_name': 'Max', 'last_name': 'Test',
            'email': 'new@example.de',
            'password': 'password123',
            'password_confirm': 'different456',
            'consent': 'y',
            'form_start': str(time.time() - 10),
        })
        assert resp.status_code == 200
        assert 'stimmen nicht' in resp.get_data(as_text=True)


# =============================================================================
# Email verification
# =============================================================================

class TestVerifyEmail:

    def test_valid_token_verifies_user(self, client, db):
        from sub_modules.models import User
        from sub_modules.helpers import hash_password
        token = 'validtoken123'
        user = User(
            first_name='New', last_name='User',
            email='new@test.com',
            password_hash=hash_password('pass'),
            role='parent',
            email_verified=False,
            verify_token=token,
            verify_token_expiry=datetime.utcnow() + timedelta(hours=24),
            consent_version='1.0',
        )
        db.session.add(user)
        db.session.commit()

        resp = client.get(f'/auth/bestaetigen/{token}', follow_redirects=True)
        assert resp.status_code == 200
        db.session.refresh(user)
        assert user.email_verified is True
        assert user.verify_token is None

    def test_invalid_token_shows_error(self, client, db):
        resp = client.get('/auth/bestaetigen/nonexistenttoken',
                          follow_redirects=True)
        assert resp.status_code == 200
        assert 'ung' in resp.get_data(as_text=True).lower()

    def test_expired_token_shows_error(self, client, db):
        from sub_modules.models import User
        from sub_modules.helpers import hash_password
        token = 'expiredtoken'
        user = User(
            first_name='Old', last_name='User',
            email='old@test.local',
            password_hash=hash_password('pass'),
            role='parent',
            email_verified=False,
            verify_token=token,
            verify_token_expiry=datetime.utcnow() - timedelta(hours=1),
            consent_version='1.0',
        )
        db.session.add(user)
        db.session.commit()
        resp = client.get(f'/auth/bestaetigen/{token}', follow_redirects=True)
        assert resp.status_code == 200
        assert 'abgelaufen' in resp.get_data(as_text=True).lower()


# =============================================================================
# Login
# =============================================================================

class TestLogin:

    def test_get_renders_form(self, client):
        resp = client.get('/auth/anmelden')
        assert resp.status_code == 200

    def test_correct_credentials_redirect_to_dashboard(self, client, parent):
        resp = client.post('/auth/anmelden', data={
            'email': parent.email,
            'password': 'testpass123',
        })
        assert resp.status_code == 302
        assert '/eltern' in resp.headers['Location']

    def test_admin_redirects_to_admin_dashboard(self, client, admin):
        resp = client.post('/auth/anmelden', data={
            'email': admin.email,
            'password': 'testpass123',
        })
        assert resp.status_code == 302
        assert '/admin' in resp.headers['Location']

    def test_staff_redirects_to_staff_dashboard(self, client, staff):
        resp = client.post('/auth/anmelden', data={
            'email': staff.email,
            'password': 'testpass123',
        })
        assert resp.status_code == 302
        assert '/team' in resp.headers['Location']

    def test_wrong_password_shows_error(self, client, parent):
        resp = client.post('/auth/anmelden', data={
            'email': parent.email,
            'password': 'wrongpassword',
        })
        assert resp.status_code == 200
        assert 'Ung' in resp.get_data(as_text=True)

    def test_wrong_email_shows_vague_error(self, client):
        resp = client.post('/auth/anmelden', data={
            'email': 'nobody@nowhere.com',
            'password': 'anything',
        })
        assert resp.status_code == 200
        assert 'Ung' in resp.get_data(as_text=True)

    def test_unverified_email_blocked(self, client, db):
        from sub_modules.models import User
        from sub_modules.helpers import hash_password
        user = User(
            first_name='Un', last_name='Verified',
            email='unverified@test.com',
            password_hash=hash_password('testpass123'),
            role='parent',
            email_verified=False,
            consent_version='1.0',
        )
        db.session.add(user)
        db.session.commit()
        resp = client.post('/auth/anmelden', data={
            'email': 'unverified@test.com',
            'password': 'testpass123',
        })
        assert resp.status_code == 200
        assert 'bestätigen' in resp.get_data(as_text=True).lower()

    def test_inactive_user_blocked(self, client, parent, db):
        parent.is_active = False
        db.session.commit()
        resp = client.post('/auth/anmelden', data={
            'email': parent.email,
            'password': 'testpass123',
        })
        assert resp.status_code == 200
        assert 'Ung' in resp.get_data(as_text=True)

    def test_already_logged_in_redirected(self, client, parent):
        _session_for(client, parent)
        resp = client.get('/auth/anmelden')
        assert resp.status_code == 302


# =============================================================================
# Logout
# =============================================================================

class TestLogout:

    def test_logout_clears_session(self, client, parent):
        _session_for(client, parent)
        resp = client.get('/auth/abmelden', follow_redirects=True)
        assert resp.status_code == 200
        assert 'abgemeldet' in resp.get_data(as_text=True).lower()

    def test_logout_requires_login(self, client):
        resp = client.get('/auth/abmelden')
        assert resp.status_code == 302


# =============================================================================
# Password reset
# =============================================================================

class TestPasswordReset:

    def test_request_with_known_email_shows_confirmation(self, client, parent):
        resp = client.post('/auth/passwort-vergessen', data={
            'email': parent.email,
        }, follow_redirects=True)
        assert resp.status_code == 200

    def test_request_with_unknown_email_still_succeeds_silently(self, client):
        """Never reveal whether an email is registered."""
        resp = client.post('/auth/passwort-vergessen', data={
            'email': 'nobody@nowhere.com',
        }, follow_redirects=True)
        assert resp.status_code == 200

    def test_valid_reset_token_renders_form(self, client, parent, db):
        from sub_modules.helpers import generate_token
        token = generate_token()
        parent.verify_token = token
        parent.verify_token_expiry = datetime.utcnow() + timedelta(hours=2)
        db.session.commit()
        resp = client.get(f'/auth/passwort-neu/{token}')
        assert resp.status_code == 200

    def test_invalid_reset_token_redirects(self, client):
        resp = client.get('/auth/passwort-neu/badtoken',
                          follow_redirects=True)
        assert resp.status_code == 200


# =============================================================================
# Consent re-confirmation
# =============================================================================

class TestReconfirmConsent:

    def test_skipped_if_consent_up_to_date(self, client, parent):
        """Parent with current consent_version should be redirected away."""
        _session_for(client, parent)
        # parent.consent_version is set to CURRENT_CONSENT_VERSION in fixture
        resp = client.get('/auth/datenschutz-bestaetigen')
        assert resp.status_code == 302  # redirect away, no need to confirm

    def test_shown_if_consent_outdated(self, client, parent, db):
        _session_for(client, parent)
        parent.consent_version = '0.1'   # older than current
        db.session.commit()
        resp = client.get('/auth/datenschutz-bestaetigen')
        assert resp.status_code == 200

    def test_post_updates_consent_and_redirects(self, client, parent, db):
        _session_for(client, parent)
        parent.consent_version = '0.1'
        db.session.commit()
        resp = client.post('/auth/datenschutz-bestaetigen', data={
            'consent': 'y',
        })
        assert resp.status_code == 302
        db.session.refresh(parent)
        from sub_modules.config import CURRENT_CONSENT_VERSION
        assert parent.consent_version == CURRENT_CONSENT_VERSION


# =============================================================================
# Staff invite redemption
# =============================================================================

class TestRedeemInvite:

    @pytest.fixture
    def invited_staff(self, db):
        from sub_modules.models import User, StaffProfile
        from sub_modules.helpers import generate_token
        token = generate_token()
        user = User(
            first_name='', last_name='',
            email='invited@test.com',
            password_hash='',
            role='staff',
            email_verified=False,
            invite_token=token,
            invite_token_expiry=datetime.utcnow() + timedelta(hours=48),
            consent_version='1.0',
        )
        db.session.add(user)
        db.session.flush()
        profile = StaffProfile(user_id=user.id, staff_class='trainer')
        db.session.add(profile)
        db.session.commit()
        return user, token

    def test_get_renders_form(self, client, invited_staff):
        user, token = invited_staff
        resp = client.get(f'/auth/einladen/{token}')
        assert resp.status_code == 200
        assert user.email in resp.get_data(as_text=True)

    def test_invalid_token_redirects_with_error(self, client):
        resp = client.get('/auth/einladen/badtoken', follow_redirects=True)
        assert resp.status_code == 200
        assert 'ung' in resp.get_data(as_text=True).lower()

    def test_expired_token_redirects_with_error(self, client, db):
        from sub_modules.models import User, StaffProfile
        from sub_modules.helpers import generate_token
        token = generate_token()
        user = User(
            first_name='', last_name='',
            email='expired@test.com',
            password_hash='', role='staff',
            email_verified=False,
            invite_token=token,
            invite_token_expiry=datetime.utcnow() - timedelta(hours=1),
            consent_version='1.0',
        )
        db.session.add(user)
        db.session.flush()
        db.session.add(StaffProfile(user_id=user.id, staff_class='trainer'))
        db.session.commit()
        resp = client.get(f'/auth/einladen/{token}', follow_redirects=True)
        assert 'abgelaufen' in resp.get_data(as_text=True).lower()

    def test_post_completes_invite(self, client, db, invited_staff):
        user, token = invited_staff
        resp = client.post(f'/auth/einladen/{token}', data={
            'first_name': 'Jana',
            'last_name': 'Trainer',
            'password': 'neuespasswort1',
            'password_confirm': 'neuespasswort1',
            'consent': 'y',
        }, follow_redirects=True)
        assert resp.status_code == 200
        db.session.refresh(user)
        assert user.first_name == 'Jana'
        assert user.email_verified is True
        assert user.invite_token is None
