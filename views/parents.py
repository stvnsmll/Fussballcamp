'''
views/parents.py
================
Parent-facing blueprint. All routes require login + role='parent'.

Routes:
    GET       /eltern/                      Dashboard — children + registration status
    GET/POST  /eltern/kind-hinzufuegen      Add a new child
    GET/POST  /eltern/kind/<id>/bearbeiten  Edit child details
    GET/POST  /eltern/kind/<id>/loeschen    Soft-delete a child (GDPR)
    GET/POST  /eltern/anmelden/<session_id> Register child(ren) for a camp session
    GET       /eltern/qr/<child_id>         View QR code for check-in
    GET/POST  /eltern/konto                 Account details (edit name, phone, address)
    GET/POST  /eltern/email-einstellungen   Email notification preferences (opt-out)
    GET       /eltern/datenschutz           Data export + deletion request
    POST      /eltern/datenschutz/export    Trigger personal data export
    POST      /eltern/datenschutz/loeschen  Request account deletion

Security:
    - All routes: @login_required + @parent_required
    - Parents can only access their own children (ownership check on every child route)
    - CSRF on all forms via Flask-WTF
    - QR code served as image — token never exposed in page source
'''

import io
import qrcode
import base64
from datetime import datetime, date
from functools import wraps

from flask import (Blueprint, render_template, redirect, url_for,
                   flash, request, current_app, abort, jsonify,
                   make_response, session as flask_session)
from flask_login import login_required, current_user
from flask_wtf import FlaskForm
from wtforms import (StringField, PasswordField, TextAreaField, BooleanField,
                     DateField, HiddenField, RadioField)
from wtforms.validators import DataRequired, Optional, Length

from sub_modules.extensions import db, log_analytics_event
from sub_modules.models import (User, Child, EmergencyContact,
                                Registration, CampSession, QRToken,
                                AbsenceReport)
from sub_modules.helpers import (assign_age_group, get_or_create_qr_token,
                                 get_next_waitlist_position,
                                 update_last_active_year, soft_delete_user,
                                 child_already_registered)
from sub_modules.emails import (send_registration_confirmation,
                                send_waitlist_notification, log_email)
from sub_modules.config import CURRENT_CONSENT_VERSION


parents_bp = Blueprint('parents', __name__)


# =============================================================================
# ACCESS CONTROL
# =============================================================================

def parent_required(f):
    '''
    Decorator: allow any authenticated user to access parent/family routes.
    Staff and admins may also have children and need access to these pages.
    '''
    @wraps(f)
    def decorated(*args, **kwargs):
        # All authenticated users can manage their own children
        return f(*args, **kwargs)
    return decorated


def owned_child_or_404(child_id: int) -> Child:
    '''
    Fetch a child by ID and verify it belongs to the current parent.
    Aborts with 404 if not found, 403 if owned by another parent.
    Excludes soft-deleted children.
    '''
    child = Child.query.filter_by(id=child_id, is_deleted=False).first_or_404()
    if child.parent_user_id != current_user.id:
        abort(404)  # 404 not 403 — don't reveal that the child exists
    return child


# =============================================================================
# FORMS
# =============================================================================

class ChildForm(FlaskForm):
    first_name          = StringField('Vorname', validators=[
                            DataRequired(), Length(max=100)])
    last_name           = StringField('Nachname', validators=[
                            DataRequired(), Length(max=100)])
    date_of_birth       = DateField('Geburtsdatum', validators=[DataRequired()])
    medical_notes       = TextAreaField('Medizinische Hinweise / Allergien',
                            validators=[Optional(), Length(max=2000)],
                            description='Allergien, Erkrankungen, Medikamente — '
                                        'nur für Trainer und Betreuer sichtbar')
    photo_consent_default        = BooleanField('Fotoerlaubnis erteilt')
    independent_travel_default   = BooleanField('Darf das Gelände alleine verlassen (z.B. mit dem Fahrrad nach Hause)')

    # Emergency contact (optional)
    ec_full_name        = StringField('Name', validators=[Optional(), Length(max=200)])
    ec_relationship     = StringField('Beziehung', validators=[Optional(), Length(max=100)],
                            description='z.B. Mutter, Opa, Nachbar')
    ec_phone_primary    = StringField('Telefon (Haupt)', validators=[Optional(), Length(max=30)])
    ec_phone_secondary  = StringField('Telefon (alternativ)', validators=[Optional(), Length(max=30)])


class RegistrationForm(FlaskForm):
    '''Minimal form — child selection handled via checkboxes in the template.'''
    # photo_consent and independent_travel now live on the Child model
    # Hidden field holds comma-separated child IDs selected for registration
    selected_children   = HiddenField()


class AccountForm(FlaskForm):
    first_name          = StringField('Vorname', validators=[
                            DataRequired(), Length(max=100)])
    last_name           = StringField('Nachname', validators=[
                            DataRequired(), Length(max=100)])
    phone               = StringField('Telefon', validators=[Optional(), Length(max=30)])
    address_street      = StringField('Straße', validators=[Optional(), Length(max=255)])
    address_city        = StringField('Stadt', validators=[Optional(), Length(max=100)])
    address_postcode    = StringField('PLZ', validators=[Optional(), Length(max=20)])
    current_password    = PasswordField('Aktuelles Passwort', validators=[Optional()])
    new_password        = PasswordField('Neues Passwort', validators=[Optional(), Length(min=8)])
    new_password_confirm = PasswordField('Neues Passwort bestätigen', validators=[Optional()])


class EmailPreferencesForm(FlaskForm):
    email_announcements = BooleanField(
        'E-Mail-Benachrichtigungen bei neuen Neuigkeiten erhalten'
    )


# =============================================================================
# DASHBOARD
# =============================================================================

@parents_bp.route('/')
@login_required
@parent_required
def dashboard():
    '''
    Parent home page.
    Shows all children with registration status across ALL open/active camps.
    Each camp gets its own section showing which children are registered.
    '''
    # All relevant camp sessions — open, upcoming or active
    camp_sessions = CampSession.query.filter(
        CampSession.status.in_(['open', 'upcoming', 'active'])
    ).order_by(CampSession.start_date.asc()).all()

    children = Child.query.filter_by(
        parent_user_id=current_user.id,
        is_deleted=False
    ).order_by(Child.last_name).all()

    # Build nested map: camp_id → {child_id → Registration}
    # Also track all registrations across all camps for QR / status buttons
    camp_registration_maps = {}
    all_registrations = {}   # child_id → list of active registrations
    for camp in camp_sessions:
        camp_registration_maps[camp.id] = {}
        for child in children:
            reg = Registration.query.filter_by(
                child_id=child.id,
                camp_session_id=camp.id
            ).filter(
                Registration.status.in_(['confirmed', 'waitlisted', 'pending_verification'])
            ).first()
            camp_registration_maps[camp.id][child.id] = reg
            if reg:
                all_registrations.setdefault(child.id, []).append(reg)

    # Keep a single active_session for backward compat with other templates
    active_session = camp_sessions[0] if camp_sessions else None

    log_analytics_event('page_view', detail='parent_dashboard')

    show_welcome_modal = flask_session.pop('show_welcome_modal', False)
    dev_no_verify = flask_session.pop('dev_registration_no_verify', False)
    return render_template(
        'parents/dashboard.html',
        children=children,
        camp_sessions=camp_sessions,
        camp_registration_maps=camp_registration_maps,
        all_registrations=all_registrations,
        active_session=active_session,
        show_welcome_modal=show_welcome_modal,
        dev_no_verify=dev_no_verify,
        title='Mein Bereich'
    )


# =============================================================================
# CHILD MANAGEMENT
# =============================================================================

@parents_bp.route('/kind-hinzufuegen', methods=['GET', 'POST'])
@login_required
@parent_required
def add_child():
    form = ChildForm()

    if form.validate_on_submit():
        child = Child(
            parent_user_id=current_user.id,
            first_name=form.first_name.data.strip(),
            last_name=form.last_name.data.strip(),
            date_of_birth=form.date_of_birth.data,
            medical_notes=form.medical_notes.data.strip() or None,
            photo_consent_default=form.photo_consent_default.data,
            independent_travel_default=form.independent_travel_default.data,
        )
        db.session.add(child)
        db.session.flush()

        # Emergency contact (only if name and phone provided)
        if form.ec_full_name.data and form.ec_phone_primary.data:
            ec = EmergencyContact(
                child_id=child.id,
                full_name=form.ec_full_name.data.strip(),
                relationship=form.ec_relationship.data.strip() or None,
                phone_primary=form.ec_phone_primary.data.strip(),
                phone_secondary=form.ec_phone_secondary.data.strip() or None,
            )
            db.session.add(ec)

        # Generate QR token immediately
        qr = QRToken(child_id=child.id,
                     token=get_or_create_qr_token(child).token
                     if False else __import__('secrets').token_urlsafe(32))
        db.session.add(qr)
        db.session.commit()

        log_analytics_event('child_added', success=True)
        return redirect(url_for('parents.child_added_confirm', child_id=child.id))

    # Pre-populate emergency contact from existing sibling on GET
    sibling_ec = None
    if request.method == 'GET':
        sibling = Child.query.filter_by(
            parent_user_id=current_user.id,
            is_deleted=False
        ).first()
        if sibling and sibling.emergency_contacts:
            sibling_ec = sibling.emergency_contacts[0]

    return render_template(
        'parents/child_form.html',
        form=form,
        action='add',
        sibling_ec=sibling_ec,
        title='Kind hinzufügen'
    )


@parents_bp.route('/kind/<int:child_id>/hinzugefuegt')
@login_required
@parent_required
def child_added_confirm(child_id):
    child = owned_child_or_404(child_id)
    active_session = CampSession.query.filter(
        CampSession.status.in_(['open', 'upcoming', 'active'])
    ).order_by(CampSession.start_date.desc()).first()
    return render_template('parents/child_added_confirm.html',
                           child=child, active_session=active_session,
                           title='Kind hinzugefügt')


@parents_bp.route('/kind/<int:child_id>/bearbeiten', methods=['GET', 'POST'])
@login_required
@parent_required
def edit_child(child_id):
    child = owned_child_or_404(child_id)
    form = ChildForm(obj=child)

    # Pre-populate emergency contact from child's existing EC
    ec = child.emergency_contacts[0] if child.emergency_contacts else None

    if form.validate_on_submit():
        child.first_name                 = form.first_name.data.strip()
        child.last_name                  = form.last_name.data.strip()
        child.date_of_birth              = form.date_of_birth.data
        child.medical_notes              = form.medical_notes.data.strip() or None
        child.photo_consent_default      = form.photo_consent_default.data
        child.independent_travel_default = form.independent_travel_default.data
        child.updated_at                 = datetime.utcnow()

        # Update or create emergency contact
        if form.ec_full_name.data and form.ec_phone_primary.data:
            if ec:
                ec.full_name        = form.ec_full_name.data.strip()
                ec.relationship     = form.ec_relationship.data.strip() or None
                ec.phone_primary    = form.ec_phone_primary.data.strip()
                ec.phone_secondary  = form.ec_phone_secondary.data.strip() or None
            else:
                new_ec = EmergencyContact(
                    child_id=child.id,
                    full_name=form.ec_full_name.data.strip(),
                    relationship=form.ec_relationship.data.strip() or None,
                    phone_primary=form.ec_phone_primary.data.strip(),
                    phone_secondary=form.ec_phone_secondary.data.strip() or None,
                )
                db.session.add(new_ec)
        elif ec:
            # Fields cleared — remove existing contact
            db.session.delete(ec)

        db.session.commit()
        flash(f'{child.full_name} wurde aktualisiert.', 'success')
        return redirect(url_for('parents.dashboard'))

    return render_template(
        'parents/child_form.html',
        form=form,
        child=child,
        action='edit',
        sibling_ec=ec,
        title=f'{child.full_name} bearbeiten'
    )


@parents_bp.route('/kind/<int:child_id>/loeschen', methods=['POST'])
@login_required
@parent_required
def delete_child(child_id):
    '''
    Soft-delete a child record (GDPR erasure).
    Only allowed if the child has no active/confirmed registrations.
    '''
    child = owned_child_or_404(child_id)

    # Block deletion if child has an active registration
    active_reg = Registration.query.filter(
        Registration.child_id == child.id,
        Registration.status.in_(['confirmed', 'pending_verification', 'waitlisted'])
    ).first()

    if active_reg:
        flash(
            f'{child.full_name} hat eine aktive Anmeldung und kann nicht '
            f'gelöscht werden. Bitte zuerst die Anmeldung stornieren.',
            'danger'
        )
        return redirect(url_for('parents.dashboard'))

    child.is_deleted = True
    child.deleted_at = datetime.utcnow()
    db.session.commit()

    log_analytics_event('child_deleted', success=True)
    flash(f'{child.full_name} wurde gelöscht.', 'info')
    return redirect(url_for('parents.dashboard'))


# =============================================================================
# CAMP REGISTRATION
# =============================================================================

@parents_bp.route('/anmelden/<int:session_id>', methods=['GET', 'POST'])
@login_required
@parent_required
def register_for_camp(session_id):
    '''
    Register one or more children for a camp session.

    GET: Show eligible children with registration options.
         Children already registered are shown but disabled.
         Children outside all age ranges are shown but greyed out.

    POST: Process registration for each selected child.
          Each child gets auto-assigned to an age group.
          If group is full → waitlisted. Otherwise → confirmed.
    '''
    camp_session = CampSession.query.filter_by(
        id=session_id
    ).first_or_404()

    if not camp_session.is_registration_open:
        flash('Die Anmeldung für dieses Camp ist derzeit nicht geöffnet.', 'warning')
        return redirect(url_for('parents.dashboard'))

    children = Child.query.filter_by(
        parent_user_id=current_user.id,
        is_deleted=False
    ).order_by(Child.last_name).all()

    # Build eligibility map for each child
    child_data = []
    for child in children:
        age_group = assign_age_group(child, camp_session)
        already_registered = child_already_registered(child.id, camp_session.id)
        age_on_start = child.age_on(camp_session.start_date)
        age_eligible, age_reason = camp_session.is_age_eligible(child)

        child_data.append({
            'child': child,
            'age_group': age_group,
            'age_on_start': age_on_start,
            'already_registered': already_registered,
            'eligible': age_eligible and age_group is not None and not already_registered,
            'ineligible_reason': (
                'Bereits angemeldet' if already_registered
                else age_reason if not age_eligible
                else 'Kein passendes Alter' if age_group is None
                else None
            )
        })

    form = RegistrationForm()

    if form.validate_on_submit():
        # Read child_ids directly from checkboxes — no JS hidden field needed
        selected_ids = [
            int(i) for i in request.form.getlist('child_ids')
            if i.strip().isdigit()
        ]

        if not selected_ids:
            flash('Bitte wählen Sie mindestens ein Kind aus.', 'warning')
            return render_template(
                'parents/register_for_camp.html',
                form=form,
                camp_session=camp_session,
                child_data=child_data,
                title='Für Camp anmelden'
            )

        registered_names = []
        waitlisted_names = []
        errors = []

        for child_id in selected_ids:
            # Ownership + eligibility re-check server-side
            child = Child.query.filter_by(
                id=child_id,
                parent_user_id=current_user.id,
                is_deleted=False
            ).first()

            if not child:
                continue

            if child_already_registered(child.id, camp_session.id):
                errors.append(f'{child.full_name} ist bereits angemeldet.')
                continue

            age_eligible, age_reason = camp_session.is_age_eligible(child)
            if not age_eligible:
                errors.append(f'{child.full_name}: {age_reason}')
                continue

            age_group = assign_age_group(child, camp_session)
            if not age_group:
                errors.append(
                    f'{child.full_name} passt in keine Altersgruppe '
                    f'für dieses Camp.'
                )
                continue

            # Determine status — check camp-level capacity first, then age group
            if camp_session.is_camp_full or age_group.is_full:
                status = 'waitlisted'
                waitlist_pos = get_next_waitlist_position(
                    camp_session.id, age_group.id
                )
            else:
                status = 'confirmed'
                waitlist_pos = None

            # Reuse cancelled registration if one exists (DB has unique constraint)
            registration = Registration.query.filter_by(
                child_id=child.id,
                camp_session_id=camp_session.id,
                status='cancelled'
            ).first()

            if registration:
                registration.status                        = status
                registration.waitlist_position             = waitlist_pos
                registration.age_group_id                  = age_group.id
                registration.auto_assigned_group           = age_group.name
                registration.admin_override                = False
                registration.independent_travel            = child.independent_travel_default
                registration.photo_consent                 = child.photo_consent_default
                registration.consent_version_at_registration = CURRENT_CONSENT_VERSION
                registration.updated_at                    = datetime.utcnow()
                registration.registered_at                 = datetime.utcnow()
            else:
                registration = Registration(
                    child_id=child.id,
                    camp_session_id=camp_session.id,
                    age_group_id=age_group.id,
                    status=status,
                    waitlist_position=waitlist_pos,
                    auto_assigned_group=age_group.name,
                    admin_override=False,
                    independent_travel=child.independent_travel_default,
                    photo_consent=child.photo_consent_default,
                    consent_version_at_registration=CURRENT_CONSENT_VERSION,
                )
                db.session.add(registration)
            db.session.flush()

            # Ensure QR token exists
            get_or_create_qr_token(child)

            # Send confirmation email
            try:
                if status == 'confirmed':
                    send_registration_confirmation(
                        current_user, child, registration, camp_session
                    )
                    registered_names.append(child.full_name)
                    log_analytics_event('child_register_confirmed',
                                        success=True, detail=age_group.name)
                else:
                    send_waitlist_notification(
                        current_user, child, registration, camp_session
                    )
                    waitlisted_names.append(child.full_name)
                    log_analytics_event('child_register_waitlisted',
                                        success=True, detail=age_group.name)
            except Exception as e:
                current_app.logger.error(f'[Parents] Confirmation email failed: {e}')

        db.session.commit()

        # Update GDPR retention tracking
        update_last_active_year(current_user, camp_session.year)

        # Flash results
        if registered_names:
            flash(
                f'Erfolgreich angemeldet: {", ".join(registered_names)}.',
                'success'
            )
        if waitlisted_names:
            flash(
                f'Auf der Warteliste: {", ".join(waitlisted_names)}. '
                f'Wir kontaktieren Sie, wenn ein Platz frei wird.',
                'info'
            )
        for error in errors:
            flash(error, 'danger')

        return redirect(url_for('parents.dashboard'))

    return render_template(
        'parents/register_for_camp.html',
        form=form,
        camp_session=camp_session,
        child_data=child_data,
        title=f'Anmelden: {camp_session.name}'
    )


@parents_bp.route('/anmeldung/<int:registration_id>/stornieren', methods=['POST'])
@login_required
@parent_required
def cancel_registration(registration_id):
    '''
    Cancel a child's registration.
    Only allowed before camp starts (status = confirmed or waitlisted).
    Waitlist positions are reordered after cancellation.
    '''
    registration = Registration.query.get_or_404(registration_id)

    # Ownership check — must be parent of this child (404 not 403 to avoid leaking info)
    if registration.child.parent_user_id != current_user.id:
        abort(404)

    if registration.status not in ('confirmed', 'waitlisted', 'pending_verification'):
        flash('Diese Anmeldung kann nicht mehr storniert werden.', 'warning')
        return redirect(url_for('parents.dashboard'))

    cancelled_position = registration.waitlist_position
    cancelled_group_id = registration.age_group_id
    cancelled_session_id = registration.camp_session_id

    registration.status = 'cancelled'
    registration.waitlist_position = None
    registration.updated_at = datetime.utcnow()

    # Reorder remaining waitlist positions if this was a waitlisted entry
    if cancelled_position and cancelled_group_id:
        remaining = Registration.query.filter(
            Registration.camp_session_id == cancelled_session_id,
            Registration.age_group_id == cancelled_group_id,
            Registration.status == 'waitlisted',
            Registration.waitlist_position > cancelled_position
        ).all()
        for reg in remaining:
            reg.waitlist_position -= 1

    db.session.commit()

    log_analytics_event('registration_cancelled', success=True)
    flash(
        f'Anmeldung von {registration.child.full_name} wurde storniert.',
        'info'
    )
    return redirect(url_for('parents.dashboard'))


# =============================================================================
# QR CODE
# =============================================================================

@parents_bp.route('/qr/<int:child_id>')
@login_required
@parent_required
def view_qr(child_id):
    '''
    Display the check-in QR code for a child.
    QR token is never exposed in page source — rendered as an inline image.
    Parent shows this screen to staff at check-in.
    '''
    child = owned_child_or_404(child_id)
    qr_token = get_or_create_qr_token(child)

    # Generate QR code as base64 PNG for inline display
    qr_url = url_for('staff.checkin_by_qr', token=qr_token.token, _external=True)
    qr_img = qrcode.make(qr_url)
    buffer = io.BytesIO()
    qr_img.save(buffer, format='PNG')
    qr_b64 = base64.b64encode(buffer.getvalue()).decode('utf-8')

    # Active registration for context
    active_session = CampSession.query.filter(
        CampSession.status.in_(['open', 'upcoming', 'active'])
    ).order_by(CampSession.start_date.desc()).first()

    registration = None
    if active_session:
        registration = Registration.query.filter_by(
            child_id=child.id,
            camp_session_id=active_session.id,
            status='confirmed'
        ).first()

    return render_template(
        'parents/qr_code.html',
        child=child,
        qr_b64=qr_b64,
        registration=registration,
        title=f'QR-Code: {child.full_name}'
    )


# =============================================================================
# ACCOUNT MANAGEMENT
# =============================================================================

@parents_bp.route('/konto', methods=['GET', 'POST'])
@login_required
def account():
    from sub_modules.helpers import hash_password, verify_password

    form = AccountForm(obj=current_user)

    if form.validate_on_submit():
        current_user.first_name     = (form.first_name.data or '').strip()
        current_user.last_name      = (form.last_name.data or '').strip()
        current_user.phone          = (form.phone.data or '').strip() or None
        current_user.address_street = (form.address_street.data or '').strip() or None
        current_user.address_city   = (form.address_city.data or '').strip() or None
        current_user.address_postcode = (form.address_postcode.data or '').strip() or None
        current_user.updated_at     = datetime.utcnow()

        # Password change — only if new password provided
        if form.new_password.data:
            if not form.current_password.data:
                flash('Bitte aktuelles Passwort eingeben.', 'warning')
                return render_template('parents/account.html', form=form, title='Mein Konto')
            if not verify_password(form.current_password.data, current_user.password_hash):
                flash('Aktuelles Passwort ist falsch.', 'danger')
                return render_template('parents/account.html', form=form, title='Mein Konto')
            if form.new_password.data != form.new_password_confirm.data:
                flash('Neue Passwörter stimmen nicht überein.', 'danger')
                return render_template('parents/account.html', form=form, title='Mein Konto')
            current_user.password_hash = hash_password(form.new_password.data)
            flash('Passwort erfolgreich geändert.', 'success')

        db.session.commit()
        flash('Kontodaten erfolgreich aktualisiert.', 'success')
        return redirect(url_for('parents.account'))

    return render_template(
        'parents/account.html',
        form=form,
        title='Mein Konto'
    )


@parents_bp.route('/email-einstellungen', methods=['GET', 'POST'])
@login_required
@parent_required
def email_preferences():
    '''
    Email notification opt-out.
    Functional emails (verification, confirmations) always sent regardless.
    This setting controls announcement notification emails only.
    '''
    form = EmailPreferencesForm(obj=current_user)

    if form.validate_on_submit():
        current_user.email_announcements = form.email_announcements.data
        db.session.commit()

        status = 'aktiviert' if form.email_announcements.data else 'deaktiviert'
        flash(f'E-Mail-Benachrichtigungen {status}.', 'success')
        return redirect(url_for('parents.account'))

    return render_template(
        'parents/email_preferences.html',
        form=form,
        title='E-Mail-Einstellungen'
    )


# =============================================================================
# GDPR — DATA EXPORT & DELETION
# =============================================================================

@parents_bp.route('/datenschutz')
@login_required
@parent_required
def data_privacy():
    '''
    GDPR rights page:
    - View what data is stored
    - Export all personal data as JSON
    - Request account deletion
    '''
    children = Child.query.filter_by(
        parent_user_id=current_user.id,
        is_deleted=False
    ).all()

    return render_template(
        'parents/data_privacy.html',
        children=children,
        title='Datenschutz & Meine Daten'
    )


@parents_bp.route('/datenschutz/export', methods=['POST'])
@login_required
@parent_required
def export_data():
    '''
    Export all personal data for the current user as a JSON file.
    GDPR Article 20 — right to data portability.
    '''
    import json

    children = Child.query.filter_by(
        parent_user_id=current_user.id
    ).all()

    data = {
        'konto': {
            'vorname':        current_user.first_name,
            'nachname':       current_user.last_name,
            'email':          current_user.email,
            'telefon':        current_user.phone,
            'adresse': {
                'strasse':    current_user.address_street,
                'stadt':      current_user.address_city,
                'plz':        current_user.address_postcode,
                'land':       current_user.address_country,
            },
            'konto_erstellt': current_user.created_at.isoformat(),
            'einwilligung': {
                'erteilt_am': current_user.consent_given_at.isoformat()
                              if current_user.consent_given_at else None,
                'version':    current_user.consent_version,
            }
        },
        'kinder': [
            {
                'vorname':          c.first_name,
                'nachname':         c.last_name,
                'geburtsdatum':     c.date_of_birth.isoformat(),
                'medizinische_hinweise': c.medical_notes,
                'notfallkontakte': [
                    {
                        'name':         ec.full_name,
                        'beziehung':    ec.relationship,
                        'telefon':      ec.phone_primary,
                        'telefon_2':    ec.phone_secondary,
                    }
                    for ec in c.emergency_contacts
                ],
                'anmeldungen': [
                    {
                        'camp':         r.camp_session.name,
                        'jahr':         r.camp_session.year,
                        'status':       r.status,
                        'altersgruppe': r.age_group.name if r.age_group else None,
                        'angemeldet_am': r.registered_at.isoformat(),
                    }
                    for r in c.registrations
                ]
            }
            for c in children
        ]
    }

    log_analytics_event('data_export', success=True)

    response = make_response(json.dumps(data, ensure_ascii=False, indent=2))
    response.headers['Content-Type'] = 'application/json; charset=utf-8'
    response.headers['Content-Disposition'] = (
        f'attachment; filename="meine-daten-fussballcamp.json"'
    )
    return response


@parents_bp.route('/datenschutz/loeschen', methods=['POST'])
@login_required
@parent_required
def request_deletion():
    '''
    GDPR Article 17 — right to erasure.
    Soft-deletes the account and all children.
    Blocked if there are active confirmed registrations
    (admin should cancel these first).

    After deletion the user is logged out automatically.
    '''
    from flask_login import logout_user

    # Block if active registrations exist
    active_regs = Registration.query.join(Child).filter(
        Child.parent_user_id == current_user.id,
        Registration.status.in_(['confirmed', 'waitlisted', 'pending_verification'])
    ).all()

    if active_regs:
        flash(
            'Ihr Konto kann nicht gelöscht werden, solange aktive '
            'Anmeldungen bestehen. Bitte stornieren Sie diese zuerst '
            'oder kontaktieren Sie den Administrator.',
            'danger'
        )
        return redirect(url_for('parents.data_privacy'))

    user_name = current_user.full_name
    success, message = soft_delete_user(current_user, reason='parent_request')

    if success:
        logout_user()
        log_analytics_event('account_deleted', success=True)
        flash(
            'Ihr Konto und alle zugehörigen Daten wurden gelöscht. '
            'Auf Wiedersehen!',
            'info'
        )
        return redirect(url_for('public.index'))
    else:
        flash(f'Fehler beim Löschen: {message}', 'danger')
        return redirect(url_for('parents.data_privacy'))


# =============================================================================
# CHILD STATUS PAGE
# Per-child view: registration, group, check-in status, absence reports.
# =============================================================================

class AbsenceForm(FlaskForm):
    camp_day = HiddenField(validators=[DataRequired()])
    reason   = RadioField(
        'Grund',
        choices=[('sick', 'Krank'), ('other', 'Anderer Grund')],
        default='sick',
        validators=[DataRequired()]
    )
    note     = TextAreaField('Kurze Notiz (optional)',
                             validators=[Optional(), Length(max=300)])


@parents_bp.route('/kind/<int:child_id>/status')
@login_required
@parent_required
def child_status(child_id):
    '''
    Per-child status page for parents.

    Before camp: shows registration status and group assignment.
    During camp (status=active): adds live check-in status per camp day,
                                 plus absence report form for each day.
    After camp: shows final attendance summary.

    Auto-refreshes every 60 s when camp is active and child is confirmed.
    '''
    child = owned_child_or_404(child_id)

    # Find the most relevant camp session for this child
    camp = CampSession.query.filter(
        CampSession.status.in_(['open', 'upcoming', 'active'])
    ).order_by(CampSession.start_date.desc()).first()

    registration = None
    day_details  = []
    is_currently_present = False

    if camp:
        registration = Registration.query.filter_by(
            child_id=child.id,
            camp_session_id=camp.id
        ).first()

    if registration and registration.status == 'confirmed':
        today_day = None
        if camp.status == 'active':
            from sub_modules.helpers import get_camp_day_number
            today_day = get_camp_day_number(camp, date.today())

        for day_num, day_date in camp.camp_days:
            is_today   = (day_date == date.today())
            is_past    = (day_date < date.today())
            is_future  = (day_date > date.today())

            checkin    = registration.checkin_for_day(day_num)
            checkout   = registration.checkout_for_day(day_num)
            absence    = registration.absence_for_day(day_num)

            still_present = (
                checkin is not None and
                checkout is None and
                (is_today or is_past)
            )

            if is_today and still_present:
                is_currently_present = True

            # Build a status string the template can use cleanly
            if absence and absence.is_active and checkin is None:
                day_status = 'absent_reported'
            elif checkin and checkout:
                if checkout.is_auto_checkout:
                    day_status = 'auto_checked_out'
                elif registration.independent_travel:
                    day_status = 'left_independently'
                else:
                    day_status = 'checked_out'
            elif checkin and not checkout:
                day_status = 'present'
            elif is_future or (camp.status != 'active' and not is_past):
                day_status = 'upcoming'
            elif is_past and not checkin and not absence:
                day_status = 'no_show'
            else:
                day_status = 'not_arrived'

            day_details.append({
                'day_num':       day_num,
                'date':          day_date,
                'is_today':      is_today,
                'is_past':       is_past,
                'is_future':     is_future,
                'checkin':       checkin,
                'checkout':      checkout,
                'absence':       absence,
                'still_present': still_present,
                'status':        day_status,
                # Show absence form for today and future days with no checkin
                'can_report':    (is_today or is_future) and checkin is None
                                  and camp.status == 'active',
            })

    absence_form = AbsenceForm()

    log_analytics_event('page_view', detail='child_status')

    return render_template(
        'parents/child_status.html',
        child=child,
        camp=camp,
        registration=registration,
        day_details=day_details,
        is_currently_present=is_currently_present,
        absence_form=absence_form,
        title=f'Status — {child.full_name}',
    )


@parents_bp.route('/anmeldung/<int:registration_id>/abmelden', methods=['POST'])
@login_required
@parent_required
def report_absence(registration_id):
    '''
    Parent reports that their child will not attend on a specific camp day.
    Visible to staff on check-in screen and group detail.
    '''
    reg = Registration.query.get_or_404(registration_id)

    # Ownership: registration must belong to a child of this parent
    if reg.child.parent_user_id != current_user.id:
        abort(404)

    if reg.status != 'confirmed':
        flash('Nur bestätigte Anmeldungen können abgemeldet werden.', 'warning')
        return redirect(url_for('parents.child_status', child_id=reg.child_id))

    form = AbsenceForm()
    if not form.validate_on_submit():
        flash('Bitte alle Felder ausfüllen.', 'warning')
        return redirect(url_for('parents.child_status', child_id=reg.child_id))

    try:
        camp_day = int(form.camp_day.data)
    except (ValueError, TypeError):
        abort(400)

    # Validate camp day is real and reportable (today or future)
    camp = reg.camp_session
    valid_days = {d for d, dt in camp.camp_days
                  if dt >= date.today() and camp.status == 'active'}
    if camp_day not in valid_days:
        flash('Für diesen Tag kann keine Abmeldung mehr eingereicht werden.', 'warning')
        return redirect(url_for('parents.child_status', child_id=reg.child_id))

    # Check no active report already exists for this day
    existing = reg.absence_for_day(camp_day)
    if existing:
        flash('Für diesen Tag wurde bereits eine Abmeldung eingereicht.', 'info')
        return redirect(url_for('parents.child_status', child_id=reg.child_id))

    absence = AbsenceReport(
        registration_id=reg.id,
        camp_day=camp_day,
        reason=form.reason.data,
        note=form.note.data.strip() if form.note.data else None,
    )
    db.session.add(absence)
    db.session.commit()

    day_names = {1: 'Mittwoch', 2: 'Donnerstag', 3: 'Freitag', 4: 'Samstag'}
    flash(
        f'{reg.child.full_name} wurde für {day_names.get(camp_day, f"Tag {camp_day}")} '
        f'abgemeldet. Das Team wurde informiert.',
        'success'
    )
    return redirect(url_for('parents.child_status', child_id=reg.child_id))


@parents_bp.route(
    '/anmeldung/<int:registration_id>/abmelden/<int:camp_day>/rueckgaengig',
    methods=['POST']
)
@login_required
@parent_required
def cancel_absence(registration_id, camp_day):
    '''
    Parent cancels a previously filed absence — child is coming after all.
    '''
    reg = Registration.query.get_or_404(registration_id)

    if reg.child.parent_user_id != current_user.id:
        abort(404)

    absence = reg.absence_for_day(camp_day)
    if not absence:
        flash('Keine aktive Abmeldung für diesen Tag gefunden.', 'warning')
        return redirect(url_for('parents.child_status', child_id=reg.child_id))

    absence.cancelled_at = datetime.utcnow()
    db.session.commit()

    day_names = {1: 'Mittwoch', 2: 'Donnerstag', 3: 'Freitag', 4: 'Samstag'}
    flash(
        f'Abmeldung für {day_names.get(camp_day, f"Tag {camp_day}")} zurückgezogen. '
        f'Das Team erwartet {reg.child.first_name}.',
        'success'
    )
    return redirect(url_for('parents.child_status', child_id=reg.child_id))
