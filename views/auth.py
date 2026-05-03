'''
views/auth.py
=============
Authentication blueprint.

Routes:
    GET/POST  /auth/registrieren          Parent self-registration
    GET/POST  /auth/anmelden              Login
    GET       /auth/abmelden              Logout
    GET       /auth/bestaetigen/<token>   Email verification
    GET/POST  /auth/passwort-vergessen    Password reset request
    GET/POST  /auth/passwort-neu/<token>  Password reset confirm
    GET       /auth/einladen/<token>      Staff invite redemption (GET = show form)
    POST      /auth/einladen/<token>      Staff invite redemption (POST = complete)
    GET       /auth/magic-link            Request magic login link
    POST      /auth/magic-link            Send magic login link
    GET       /auth/magic/<token>         Redeem magic login link
    GET/POST  /auth/sms-login             SMS login stub (Twilio-ready)

Security:
    - Rate limiting via Flask-Limiter on login and registration
    - Honeypot + form timing on registration to block bots
    - CSRF via Flask-WTF on all forms
    - Safe redirect validation on login next= parameter
    - Email verification required before registration is accepted
    - Invite tokens expire after INVITE_TOKEN_EXPIRY_HOURS
    - Verify tokens expire after VERIFY_TOKEN_EXPIRY_HOURS
'''

import time
from datetime import datetime, timedelta

from flask import (Blueprint, render_template, redirect, url_for,
                   flash, request, current_app, session as flask_session)
from flask_login import login_user, logout_user, login_required, current_user
from flask_wtf import FlaskForm
from wtforms import (StringField, PasswordField, BooleanField,
                     SelectField, HiddenField)
from wtforms.validators import (DataRequired, Email, EqualTo,
                                Length, Optional, ValidationError)

from sub_modules.extensions import db, limiter
from sub_modules.models import User, StaffProfile, ConsentVersion, Registration
from sub_modules.config import (CURRENT_CONSENT_VERSION,
                                INVITE_TOKEN_EXPIRY_HOURS,
                                VERIFY_TOKEN_EXPIRY_HOURS,
                                STAFF_CLASSES)
from sub_modules.helpers import (hash_password, verify_password,
                                 generate_token, is_safe_redirect_url,
                                 redirect_after_login, honeypot_triggered,
                                 submission_too_fast)
from sub_modules.emails import (send_verification_email,
                                send_password_reset_email,
                                log_email)


auth_bp = Blueprint('auth', __name__)


# =============================================================================
# FORMS
# =============================================================================

class RegistrationForm(FlaskForm):
    first_name      = StringField('Vorname', validators=[DataRequired(), Length(max=100)])
    last_name       = StringField('Nachname', validators=[DataRequired(), Length(max=100)])
    email           = StringField('E-Mail', validators=[DataRequired(), Email(check_deliverability=False), Length(max=255)])
    phone           = StringField('Telefon *', validators=[DataRequired(), Length(max=30)],
                        description='Wird für Rückfragen und ggf. SMS-Benachrichtigungen verwendet.')
    phone_confirm   = StringField('Telefon bestätigen *', validators=[DataRequired(), Length(max=30)])
    address_street  = StringField('Straße', validators=[Optional(), Length(max=255)])
    address_city    = StringField('Stadt', validators=[Optional(), Length(max=100)])
    address_postcode = StringField('PLZ', validators=[Optional(), Length(max=20)])
    password        = PasswordField('Passwort', validators=[
                        DataRequired(),
                        Length(min=8, message='Mindestens 8 Zeichen erforderlich.')
                      ])
    password_confirm = PasswordField('Passwort bestätigen', validators=[
                        DataRequired(),
                        EqualTo('password', message='Passwörter stimmen nicht überein.')
                      ])
    consent         = BooleanField(validators=[DataRequired(
                        message='Bitte stimmen Sie der Datenschutzerklärung zu.'
                      )])

    # Bot protection fields
    website         = StringField('Website')
    form_start      = HiddenField()

    def validate_phone_confirm(self, field):
        # Normalise both: strip spaces/dashes for comparison
        def norm(v):
            return ''.join(c for c in (v or '') if c.isdigit() or c == '+')
        if norm(self.phone.data) != norm(field.data):
            raise ValidationError('Telefonnummern stimmen nicht überein.')


class LoginForm(FlaskForm):
    email           = StringField('E-Mail', validators=[DataRequired(), Email(check_deliverability=False)])
    password        = PasswordField('Passwort', validators=[DataRequired()])
    remember_me     = BooleanField('Angemeldet bleiben')


class ConsentReconfirmForm(FlaskForm):
    consent = BooleanField(validators=[DataRequired(
                message='Bitte stimmen Sie der aktualisierten Datenschutzerklärung zu.'
              )])


class PasswordResetRequestForm(FlaskForm):
    email           = StringField('E-Mail', validators=[DataRequired(), Email(check_deliverability=False)])


class PasswordResetForm(FlaskForm):
    password        = PasswordField('Neues Passwort', validators=[
                        DataRequired(),
                        Length(min=8, message='Mindestens 8 Zeichen erforderlich.')
                      ])
    password_confirm = PasswordField('Passwort bestätigen', validators=[
                        DataRequired(),
                        EqualTo('password', message='Passwörter stimmen nicht überein.')
                      ])


class InviteCompleteForm(FlaskForm):
    first_name      = StringField('Vorname', validators=[DataRequired(), Length(max=100)])
    last_name       = StringField('Nachname', validators=[DataRequired(), Length(max=100)])
    phone           = StringField('Telefon', validators=[Optional(), Length(max=30)])
    password        = PasswordField('Passwort', validators=[
                        DataRequired(),
                        Length(min=8, message='Mindestens 8 Zeichen erforderlich.')
                      ])
    password_confirm = PasswordField('Passwort bestätigen', validators=[
                        DataRequired(),
                        EqualTo('password', message='Passwörter stimmen nicht überein.')
                      ])
    consent         = BooleanField(validators=[DataRequired(
                        message='Bitte stimmen Sie der Datenschutzerklärung zu.'
                      )])


class MagicLinkRequestForm(FlaskForm):
    email = StringField('E-Mail', validators=[DataRequired(), Email(check_deliverability=False)])


class SmsLoginForm(FlaskForm):
    phone = StringField('Telefonnummer', validators=[DataRequired(), Length(max=30)])


# =============================================================================
# HELPERS
# =============================================================================

def _get_current_consent_version():
    return ConsentVersion.query.get(CURRENT_CONSENT_VERSION)


def _already_logged_in_redirect():
    if current_user.is_authenticated:
        return redirect_after_login(current_user)
    return None


def _needs_consent_reconfirmation(user) -> bool:
    if not user.email_verified:
        return False
    if user.consent_version == CURRENT_CONSENT_VERSION:
        return False
    return True


def _is_dev_open_registration():
    '''Return True if dev mode allows registration without invite.'''
    return current_app.config.get('DEV_OPEN_REGISTRATION', False)


# =============================================================================
# ROUTES
# =============================================================================

# -----------------------------------------------------------------------------
# REGISTRATION
# -----------------------------------------------------------------------------

@auth_bp.route('/registrieren', methods=['GET', 'POST'])
@limiter.limit('10 per hour')
def register():
    '''
    Parent self-registration.
    Staff and admin accounts are created via invite only — not here.
    '''
    early_redirect = _already_logged_in_redirect()
    if early_redirect:
        return early_redirect

    form = RegistrationForm()

    if request.method == 'POST':

        # Bot protection
        if honeypot_triggered(request.form, field_name='website'):
            current_app.logger.warning(
                f'[Auth] Honeypot triggered on registration from {request.remote_addr}'
            )
            flash('Registrierung eingereicht. Bitte bestätigen Sie Ihre E-Mail-Adresse.', 'success')
            return redirect(url_for('public.index'))

        try:
            form_start = float(request.form.get('form_start', 0))
        except (ValueError, TypeError):
            form_start = 0

        # Check duplicate email early — before timing check so we always show
        # the helpful "already have an account" banner regardless of speed
        early_email = form.email.data.lower().strip() if form.email.data else ''
        if early_email:
            existing_early = User.query.filter_by(email=early_email).first()
            if existing_early:
                return render_template(
                    'auth/register.html', form=form,
                    form_start_time=str(time.time()),
                    consent_version=_get_current_consent_version(),
                    title='Registrieren',
                    duplicate_email=early_email
                )

        min_seconds = 1 if current_app.debug else 3
        # Skip timing guard when dev open registration is enabled
        if not _is_dev_open_registration() and submission_too_fast(form_start, min_seconds=min_seconds):
            current_app.logger.warning(
                f'[Auth] Fast submission on registration from {request.remote_addr}'
            )
            flash('Registrierung eingereicht. Bitte bestätigen Sie Ihre E-Mail-Adresse.', 'success')
            return redirect(url_for('public.index'))

        if form.validate_on_submit():

            # Re-check duplicate email (in case validate added new data)
            existing_email = User.query.filter_by(
                email=form.email.data.lower().strip()
            ).first()
            if existing_email:
                return render_template(
                    'auth/register.html', form=form,
                    form_start_time=str(time.time()),
                    consent_version=_get_current_consent_version(),
                    title='Registrieren',
                    duplicate_email=form.email.data.lower().strip()
                )

            # Check duplicate phone
            phone_normalised = ''.join(
                c for c in (form.phone.data or '') if c.isdigit() or c == '+'
            )
            if phone_normalised:
                existing_phone = User.query.filter(
                    User.phone.isnot(None),
                    User.is_deleted == False
                ).all()
                for u in existing_phone:
                    existing_norm = ''.join(
                        c for c in (u.phone or '') if c.isdigit() or c == '+'
                    )
                    if existing_norm == phone_normalised:
                        form.phone.errors.append(
                            'Diese Telefonnummer ist bereits registriert.'
                        )
                        return render_template(
                            'auth/register.html', form=form,
                            form_start_time=str(time.time()),
                            consent_version=_get_current_consent_version(),
                            title='Registrieren'
                        )

            dev_open = _is_dev_open_registration()

            # In dev open-registration mode, skip email verification
            if dev_open:
                user = User(
                    email=form.email.data.lower().strip(),
                    password_hash=hash_password(form.password.data),
                    first_name=form.first_name.data.strip(),
                    last_name=form.last_name.data.strip(),
                    phone=form.phone.data.strip() if form.phone.data else None,
                    address_street=form.address_street.data.strip() if form.address_street.data else None,
                    address_city=form.address_city.data.strip() if form.address_city.data else None,
                    address_postcode=form.address_postcode.data.strip() if form.address_postcode.data else None,
                    role='parent',
                    email_verified=True,
                    consent_given_at=datetime.utcnow(),
                    consent_version=CURRENT_CONSENT_VERSION,
                )
                db.session.add(user)
                db.session.commit()
                login_user(user, remember=False)
                flask_session['dev_registration_no_verify'] = True
                return redirect(url_for('parents.dashboard'))

            # Normal flow: send verification email
            token = generate_token()
            expiry = datetime.utcnow() + timedelta(hours=VERIFY_TOKEN_EXPIRY_HOURS)

            user = User(
                email=form.email.data.lower().strip(),
                password_hash=hash_password(form.password.data),
                first_name=form.first_name.data.strip(),
                last_name=form.last_name.data.strip(),
                phone=form.phone.data.strip() if form.phone.data else None,
                address_street=form.address_street.data.strip() if form.address_street.data else None,
                address_city=form.address_city.data.strip() if form.address_city.data else None,
                address_postcode=form.address_postcode.data.strip() if form.address_postcode.data else None,
                role='parent',
                email_verified=False,
                verify_token=token,
                verify_token_expiry=expiry,
                consent_given_at=datetime.utcnow(),
                consent_version=CURRENT_CONSENT_VERSION,
            )
            db.session.add(user)
            db.session.commit()

            try:
                send_verification_email(user, token)
                log_email(user.id, user.email, 'verification',
                          'E-Mail-Adresse bestätigen')
            except Exception as e:
                current_app.logger.error(f'[Auth] Failed to send verification email: {e}')

            flash('Konto erstellt! Bitte bestätigen Sie Ihre E-Mail-Adresse, '
                  'um fortzufahren.', 'success')
            return redirect(url_for('auth.login'))

    form_start_time = str(time.time())
    consent_version = _get_current_consent_version()

    return render_template(
        'auth/register.html',
        form=form,
        form_start_time=form_start_time,
        consent_version=consent_version,
        title='Registrieren'
    )


# -----------------------------------------------------------------------------
# EMAIL VERIFICATION
# -----------------------------------------------------------------------------

@auth_bp.route('/bestaetigen/<token>')
def verify_email(token):
    user = User.query.filter_by(
        verify_token=token,
        email_verified=False
    ).first()

    if not user:
        flash('Dieser Bestätigungslink ist ungültig oder wurde bereits verwendet.', 'danger')
        return redirect(url_for('auth.login'))

    if user.verify_token_expiry and datetime.utcnow() > user.verify_token_expiry:
        flash('Dieser Bestätigungslink ist abgelaufen. '
              'Bitte registrieren Sie sich erneut.', 'danger')
        return redirect(url_for('auth.register'))

    user.email_verified = True
    user.verify_token = None
    user.verify_token_expiry = None
    user.consent_given_at = datetime.utcnow()
    from sub_modules.config import CURRENT_CONSENT_VERSION
    user.consent_version = CURRENT_CONSENT_VERSION
    db.session.commit()

    if user.manually_entered:
        from sub_modules.helpers import finalise_manual_registration
        success, promoted = finalise_manual_registration(user)
        if success and promoted:
            from sub_modules.emails import (send_registration_confirmation,
                                            send_waitlist_notification)
            for reg in promoted:
                try:
                    if reg.status == 'confirmed':
                        send_registration_confirmation(
                            user, reg.child, reg, reg.camp_session
                        )
                    else:
                        send_waitlist_notification(
                            user, reg.child, reg, reg.camp_session
                        )
                except Exception as e:
                    current_app.logger.error(
                        f'[Auth] Post-verify email failed: {e}'
                    )

    login_user(user, remember=False)
    flask_session['show_welcome_modal'] = True
    flash('E-Mail-Adresse bestätigt! Willkommen bei Fußballcamp.', 'success')
    return redirect(url_for('parents.dashboard'))


# -----------------------------------------------------------------------------
# LOGIN
# -----------------------------------------------------------------------------

@auth_bp.route('/anmelden', methods=['GET', 'POST'])
@limiter.limit('10 per minute')
def login():
    early_redirect = _already_logged_in_redirect()
    if early_redirect:
        return early_redirect

    form = LoginForm()

    if form.validate_on_submit():
        user = User.query.filter_by(
            email=form.email.data.lower().strip()
        ).first()

        auth_error = 'Ungültige E-Mail-Adresse oder falsches Passwort.'

        if not user or not verify_password(form.password.data, user.password_hash):
            current_app.logger.info(
                f'[Auth] Failed login attempt for {form.email.data} '
                f'from {request.remote_addr}'
            )
            flash(auth_error, 'danger')
            return render_template('auth/login.html', form=form, title='Anmelden')

        if user.is_deleted or not user.is_active:
            flash(auth_error, 'danger')
            return render_template('auth/login.html', form=form, title='Anmelden')

        if not user.email_verified:
            flash('Bitte bestätigen Sie zuerst Ihre E-Mail-Adresse. '
                  'Überprüfen Sie Ihren Posteingang.', 'warning')
            return render_template('auth/login.html', form=form, title='Anmelden')

        login_user(user, remember=form.remember_me.data)

        current_app.logger.info(
            f'[Auth] Successful login: user={user.id} role={user.role} '
            f'from {request.remote_addr}'
        )

        if _needs_consent_reconfirmation(user):
            next_page = request.args.get('next')
            return redirect(url_for(
                'auth.reconfirm_consent',
                next=next_page or ''
            ))

        next_page = request.args.get('next')
        return redirect_after_login(user, next_page)

    return render_template('auth/login.html', form=form, title='Anmelden')


# -----------------------------------------------------------------------------
# MAGIC LINK LOGIN
# -----------------------------------------------------------------------------

@auth_bp.route('/magic-link', methods=['GET', 'POST'])
@limiter.limit('5 per hour')
def magic_link_request():
    '''Send a one-time login link to the user's email address.'''
    early_redirect = _already_logged_in_redirect()
    if early_redirect:
        return early_redirect

    form = MagicLinkRequestForm()

    if form.validate_on_submit():
        user = User.query.filter_by(
            email=form.email.data.lower().strip(),
            is_deleted=False,
            email_verified=True
        ).first()

        if user and user.is_active:
            token = generate_token()
            user.verify_token = token
            user.verify_token_expiry = datetime.utcnow() + timedelta(minutes=15)
            db.session.commit()
            try:
                from sub_modules.emails import send_magic_link_email
                send_magic_link_email(user, token)
            except Exception as e:
                current_app.logger.error(f'[Auth] Magic link email failed: {e}')

        # Always same message — don't reveal if email exists
        flash('Falls ein Konto mit dieser E-Mail existiert, erhalten Sie in Kürze einen Anmeldelink. '
              'Der Link ist 15 Minuten gültig.', 'info')
        return redirect(url_for('auth.login'))

    return render_template('auth/magic_link.html', form=form, title='Per E-Mail anmelden')


@auth_bp.route('/magic/<token>')
def magic_link_redeem(token):
    '''Redeem a magic login link.'''
    user = User.query.filter_by(verify_token=token, email_verified=True).first()

    if not user or user.is_deleted or not user.is_active:
        flash('Dieser Link ist ungültig oder abgelaufen.', 'danger')
        return redirect(url_for('auth.login'))

    if user.verify_token_expiry and datetime.utcnow() > user.verify_token_expiry:
        flash('Dieser Link ist abgelaufen. Bitte fordern Sie einen neuen an.', 'danger')
        return redirect(url_for('auth.magic_link_request'))

    # Consume the token
    user.verify_token = None
    user.verify_token_expiry = None
    db.session.commit()

    login_user(user, remember=False)
    current_app.logger.info(f'[Auth] Magic link login: user={user.id}')
    flash('Erfolgreich angemeldet.', 'success')
    return redirect_after_login(user)


# -----------------------------------------------------------------------------
# SMS LOGIN STUB (Twilio-ready)
# -----------------------------------------------------------------------------

@auth_bp.route('/sms-login', methods=['GET', 'POST'])
@limiter.limit('5 per hour')
def sms_login():
    '''
    SMS login stub. Shows the UI and explains it's not yet active.
    When Twilio is configured, this will send a 6-digit code to the user's phone.
    '''
    early_redirect = _already_logged_in_redirect()
    if early_redirect:
        return early_redirect

    form = SmsLoginForm()
    sms_enabled = bool(current_app.config.get('TWILIO_ACCOUNT_SID'))

    if form.validate_on_submit():
        if not sms_enabled:
            flash('SMS-Anmeldung ist noch nicht aktiviert.', 'warning')
            return render_template('auth/sms_login.html', form=form,
                                   sms_enabled=sms_enabled, title='Per SMS anmelden')
        # TODO: Implement when Twilio is configured
        # 1. Look up user by phone
        # 2. Generate 6-digit code, store in session with expiry
        # 3. Send via twilio_client.messages.create(...)
        # 4. Redirect to code entry page
        flash('SMS-Funktion noch nicht verfügbar.', 'warning')

    return render_template('auth/sms_login.html', form=form,
                           sms_enabled=sms_enabled, title='Per SMS anmelden')


# -----------------------------------------------------------------------------
# LOGOUT
# -----------------------------------------------------------------------------

@auth_bp.route('/abmelden', methods=['GET', 'POST'])
@login_required
def logout():
    current_app.logger.info(f'[Auth] Logout: user={current_user.id}')
    logout_user()
    flash('Sie wurden erfolgreich abgemeldet.', 'info')
    return redirect(url_for('public.index'))


# -----------------------------------------------------------------------------
# PASSWORD RESET — REQUEST
# -----------------------------------------------------------------------------

@auth_bp.route('/passwort-vergessen', methods=['GET', 'POST'])
@limiter.limit('5 per hour')
def password_reset_request():
    early_redirect = _already_logged_in_redirect()
    if early_redirect:
        return early_redirect

    form = PasswordResetRequestForm()

    if form.validate_on_submit():
        user = User.query.filter_by(
            email=form.email.data.lower().strip(),
            is_deleted=False
        ).first()

        if user and user.email_verified:
            token = generate_token()
            user.verify_token = token
            user.verify_token_expiry = datetime.utcnow() + timedelta(hours=2)
            db.session.commit()

            try:
                send_password_reset_email(user, token)
                log_email(user.id, user.email, 'password_reset',
                          'Passwort zurücksetzen')
            except Exception as e:
                current_app.logger.error(f'[Auth] Failed to send reset email: {e}')

        flash('Falls ein Konto mit dieser E-Mail-Adresse existiert, '
              'erhalten Sie in Kürze eine E-Mail.', 'info')
        return redirect(url_for('auth.login'))

    return render_template(
        'auth/password_reset_request.html',
        form=form,
        title='Passwort vergessen'
    )


# -----------------------------------------------------------------------------
# PASSWORD RESET — CONFIRM
# -----------------------------------------------------------------------------

@auth_bp.route('/passwort-neu/<token>', methods=['GET', 'POST'])
def password_reset_confirm(token):
    user = User.query.filter_by(verify_token=token).first()

    if not user:
        flash('Dieser Link ist ungültig oder abgelaufen.', 'danger')
        return redirect(url_for('auth.password_reset_request'))

    if user.verify_token_expiry and datetime.utcnow() > user.verify_token_expiry:
        flash('Dieser Link ist abgelaufen. Bitte fordern Sie einen neuen an.', 'danger')
        return redirect(url_for('auth.password_reset_request'))

    form = PasswordResetForm()

    if form.validate_on_submit():
        user.password_hash = hash_password(form.password.data)
        user.verify_token = None
        user.verify_token_expiry = None
        db.session.commit()

        flash('Passwort erfolgreich geändert. Sie können sich jetzt anmelden.', 'success')
        return redirect(url_for('auth.login'))

    return render_template(
        'auth/password_reset_confirm.html',
        form=form,
        token=token,
        title='Neues Passwort setzen'
    )


# -----------------------------------------------------------------------------
# CONSENT RE-CONFIRMATION
# -----------------------------------------------------------------------------

@auth_bp.route('/datenschutz-bestaetigen', methods=['GET', 'POST'])
@login_required
def reconfirm_consent():
    if not _needs_consent_reconfirmation(current_user):
        next_page = request.args.get('next')
        return redirect_after_login(current_user, next_page)

    form = ConsentReconfirmForm()
    consent_version = _get_current_consent_version()
    previous_version = current_user.consent_version

    if form.validate_on_submit():
        current_user.consent_given_at = datetime.utcnow()
        current_user.consent_version = CURRENT_CONSENT_VERSION
        db.session.commit()

        flash('Datenschutzerklärung erfolgreich bestätigt.', 'success')

        next_page = request.args.get('next')
        return redirect_after_login(current_user, next_page)

    return render_template(
        'auth/reconfirm_consent.html',
        form=form,
        consent_version=consent_version,
        previous_version=previous_version,
        title='Datenschutzerklärung bestätigen'
    )


# -----------------------------------------------------------------------------
# STAFF INVITE — REDEEM
# -----------------------------------------------------------------------------

@auth_bp.route('/einladen/<token>', methods=['GET', 'POST'])
def redeem_invite(token):
    early_redirect = _already_logged_in_redirect()
    if early_redirect:
        return early_redirect

    user = User.query.filter_by(
        invite_token=token,
        email_verified=False
    ).first()

    if not user:
        flash('Dieser Einladungslink ist ungültig oder wurde bereits verwendet.', 'danger')
        return redirect(url_for('auth.login'))

    if user.invite_token_expiry and datetime.utcnow() > user.invite_token_expiry:
        flash('Dieser Einladungslink ist abgelaufen. '
              'Bitte wenden Sie sich an den Administrator.', 'danger')
        return redirect(url_for('auth.login'))

    form = InviteCompleteForm()

    if form.validate_on_submit():
        user.first_name     = form.first_name.data.strip()
        user.last_name      = form.last_name.data.strip()
        user.phone          = form.phone.data.strip() if form.phone.data else None
        user.password_hash  = hash_password(form.password.data)
        user.email_verified = True
        user.invite_token   = None
        user.invite_token_expiry = None
        user.is_active      = True
        db.session.commit()

        flash(f'Willkommen beim {current_app.config["CAMP_NAME"]}! '
              'Sie können sich jetzt anmelden.', 'success')
        return redirect(url_for('auth.login'))

    staff_class_label = STAFF_CLASSES.get(
        user.staff_profile.staff_class if user.staff_profile else 'general',
        'Allgemein'
    )

    return render_template(
        'auth/staff_invite.html',
        form=form,
        invite_email=user.email,
        staff_class_label=staff_class_label,
        camp_name=current_app.config['CAMP_NAME'],
        title='Einladung annehmen'
    )


# =============================================================================
# ADMIN HELPER — CREATE STAFF INVITE
# =============================================================================

def create_staff_invite(email: str, staff_class: str, is_first_aid: bool,
                        role: str, invited_by_user) -> tuple:
    existing = User.query.filter_by(email=email.lower().strip()).first()
    if existing:
        return False, None, f'Ein Konto mit der E-Mail {email} existiert bereits.'

    token = generate_token()
    expiry = datetime.utcnow() + timedelta(hours=INVITE_TOKEN_EXPIRY_HOURS)

    user = User(
        email=email.lower().strip(),
        password_hash='',
        first_name='',
        last_name='',
        role=role,
        email_verified=False,
        invite_token=token,
        invite_token_expiry=expiry,
        invited_by_user_id=invited_by_user.id,
        is_active=False,
    )
    db.session.add(user)
    db.session.flush()

    profile = StaffProfile(
        user_id=user.id,
        staff_class=staff_class,
        is_first_aid=is_first_aid
    )
    db.session.add(profile)
    db.session.commit()

    return True, token, f'Einladung für {email} erstellt.'


# =============================================================================
# BOOTSTRAP: First-admin setup — only works when no admin account exists yet.
# Reads ADMIN_EMAIL + ADMIN_PASSWORD from environment variables.
# Once an admin exists this route returns 403 permanently.
# =============================================================================

@auth_bp.route('/setup', methods=['GET', 'POST'])
def setup():
    '''
    One-time admin bootstrap. Accessible without login.
    Disabled permanently once any admin account exists.
    '''
    import os
    from flask import current_app
    from sub_modules.models import ConsentVersion
    from sub_modules.config import CURRENT_CONSENT_VERSION
    from datetime import date

    # Block if any admin already exists
    if User.query.filter_by(role='admin').first():
        return render_template('auth/setup_done.html'), 403

    credentials = None
    error = None

    if request.method == 'POST':
        email    = os.environ.get('ADMIN_EMAIL', '').strip().lower()
        password = os.environ.get('ADMIN_PASSWORD', '').strip()

        if not email or not password:
            error = ('ADMIN_EMAIL und ADMIN_PASSWORD müssen als '
                     'Umgebungsvariablen gesetzt sein.')
        elif User.query.filter_by(email=email).first():
            error = f'Ein Konto mit {email} existiert bereits (aber kein Admin?). Bitte DB prüfen.'
        else:
            # Ensure a consent version row exists (required FK)
            cv = ConsentVersion.query.filter_by(version=CURRENT_CONSENT_VERSION).first()
            if not cv:
                cv = ConsentVersion(
                    version=CURRENT_CONSENT_VERSION,
                    summary='Erstveröffentlichung.',
                    full_text='[Vollständiger Datenschutztext hier einfügen]',
                    effective_date=date(2025, 1, 1),
                )
                db.session.add(cv)
                db.session.flush()

            admin = User(
                email=email,
                password_hash=hash_password(password),
                first_name='Admin',
                last_name='',
                role='admin',
                email_verified=True,
                is_active=True,
                consent_given_at=datetime.utcnow(),
                consent_version=CURRENT_CONSENT_VERSION,
            )
            db.session.add(admin)

            # Admin needs a StaffProfile row (FK constraint)
            db.session.flush()
            profile = StaffProfile(user_id=admin.id, staff_class='trainer')
            db.session.add(profile)
            db.session.commit()

            credentials = {'email': email, 'password': password}

    return render_template(
        'auth/setup.html',
        credentials=credentials,
        error=error,
    )
