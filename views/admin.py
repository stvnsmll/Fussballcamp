'''
views/admin.py
==============
Admin-only blueprint. All routes require role='admin'.

Sections:
    1  - Access control + shared helpers
    2  - Dashboard
    3  - Camp session management
    4  - Age group management + group splitting
    5  - Group assignments (head coach)
    6  - User management (parents, staff, admin)
    7  - Child & registration management
    8  - Manual entry (paper form) with live parent search
    9  - Waitlist management
    10 - Announcements management
    11 - Analytics dashboard
    12 - Settings

Edit philosophy:
    - Admin can modify any data that does not break referential integrity
    - Soft delete only — nothing is hard deleted
    - Referential integrity guards enforced before any destructive action
    - All edits logged via updated_at timestamps and override audit fields
    - Admin cannot change parent-child ownership (delete + re-enter instead)
'''

import json
from datetime import datetime, date, timedelta
from functools import wraps

from flask import (Blueprint, render_template, redirect, url_for,
                   flash, request, current_app, abort, jsonify)
from flask_login import login_required, current_user
from flask_wtf import FlaskForm
from wtforms import (StringField, TextAreaField, BooleanField, SelectField, DateTimeLocalField,
                     IntegerField, DateField, HiddenField, EmailField)
from wtforms.validators import (DataRequired, Optional, Length, Email,
                                NumberRange)
from sqlalchemy import func

from sub_modules.extensions import db, log_analytics_event, _log_error
from sub_modules.models import (User, StaffProfile, Child, EmergencyContact,
                                Registration, CampSession, AgeGroup,
                                GroupAssignment, QRToken, Announcement,
                                AnalyticsEvent, ErrorLog, EmailLog,
                                ConsentVersion)
from sub_modules.helpers import (assign_age_group, save_group_split,
                                 recommend_group_split, preview_group_split,
                                 get_age_distribution, assign_head_coach,
                                 get_head_coach_conflicts,
                                 promote_from_waitlist,
                                 get_next_waitlist_position,
                                 create_manual_entry,
                                 search_existing_parents,
                                 get_parent_for_manual_entry,
                                 child_already_registered,
                                 regenerate_qr_token,
                                 hash_password, generate_token,
                                 soft_delete_user, paginate_query)
from sub_modules.emails import (send_staff_invite_email,
                                send_camp_reminder, log_email)
from sub_modules.config import (STAFF_CLASSES, DEFAULT_AGE_GROUPS,
                                CURRENT_CONSENT_VERSION,
                                INVITE_TOKEN_EXPIRY_HOURS)
from views.auth import create_staff_invite


admin_bp = Blueprint('admin', __name__)


# =============================================================================
# [1] ACCESS CONTROL + SHARED HELPERS
# =============================================================================

def admin_required(f):
    '''Decorator: admin only. Staff cannot access admin routes.'''
    @wraps(f)
    def decorated(*args, **kwargs):
        if current_user.role != 'admin':
            abort(403)
        return f(*args, **kwargs)
    return decorated


def get_active_session():
    '''Return the most recent active or open session, or None.'''
    return CampSession.query.filter(
        CampSession.status.in_(['active', 'open', 'upcoming'])
    ).order_by(CampSession.start_date.desc()).first()


# =============================================================================
# [2] DASHBOARD
# =============================================================================

@admin_bp.route('/')
@login_required
@admin_required
def dashboard():
    camp_session = get_active_session()

    stats = {}
    head_coach_warnings = []

    if camp_session:
        stats = {
            'confirmed':    Registration.query.filter_by(
                                camp_session_id=camp_session.id,
                                status='confirmed').count(),
            'waitlisted':   Registration.query.filter_by(
                                camp_session_id=camp_session.id,
                                status='waitlisted').count(),
            'pending':      Registration.query.filter_by(
                                camp_session_id=camp_session.id,
                                status='pending_verification').count(),
            'total_parents': User.query.filter_by(
                                role='parent', is_deleted=False).count(),
            'total_staff':   User.query.filter(
                                User.role.in_(['staff', 'admin']),
                                User.is_deleted == False).count(),
        }
        if camp_session.require_head_coaches:
            head_coach_warnings = get_head_coach_conflicts(camp_session)

    # Recent errors
    recent_errors = ErrorLog.query.order_by(
        ErrorLog.occurred_at.desc()
    ).limit(5).all()

    return render_template(
        'admin/dashboard.html',
        camp_session=camp_session,
        stats=stats,
        head_coach_warnings=head_coach_warnings,
        recent_errors=recent_errors,
        title='Admin-Dashboard'
    )


# =============================================================================
# [3] CAMP SESSION MANAGEMENT
# =============================================================================

class CampSessionForm(FlaskForm):
    name                    = StringField('Name', validators=[
                                DataRequired(), Length(max=255)])
    year                    = IntegerField('Jahr', validators=[
                                DataRequired(), NumberRange(min=2020, max=2100)])
    start_date              = DateField('Startdatum (Mittwoch)',
                                validators=[DataRequired()])
    end_date                = DateField('Enddatum (Samstag)',
                                validators=[DataRequired()])
    location                = StringField('Ort', validators=[Optional(), Length(max=255)])
    description             = TextAreaField('Beschreibung', validators=[Optional()])
    registration_open       = BooleanField('Anmeldung geöffnet')
    registration_opens_at   = DateTimeLocalField('Anmeldung öffnet am',
                                format='%Y-%m-%dT%H:%M',
                                validators=[Optional()])
    registration_closes_at  = DateTimeLocalField('Anmeldung schließt am',
                                format='%Y-%m-%dT%H:%M',
                                validators=[Optional()])
    status                  = SelectField('Status', choices=[
                                ('draft',     'Entwurf (nur Admin sichtbar)'),
                                ('upcoming',  'Geplant (noch nicht offen)'),
                                ('open',      'Offen (Anmeldung läuft)'),
                                ('active',    'Aktiv (Camp läuft)'),
                                ('completed', 'Abgeschlossen'),
                                ('cancelled', 'Abgesagt'),
                                ('archived',  'Archiviert (nur Admin sichtbar)'),
                              ], default='draft')
    max_registrants         = IntegerField(
                                'Max. Teilnehmerzahl (0 = unbegrenzt)',
                                validators=[Optional(), NumberRange(min=0, max=9999)],
                                default=0)
    min_age_limit           = IntegerField(
                                'Mindestalter (0 = kein Limit)',
                                validators=[Optional(), NumberRange(min=0, max=25)],
                                default=0)
    max_age_limit           = IntegerField(
                                'Höchstalter (0 = kein Limit)',
                                validators=[Optional(), NumberRange(min=0, max=25)],
                                default=0)
    require_head_coaches    = BooleanField(
                                'Warnung anzeigen wenn Haupttrainer fehlt',
                                default=True)


@admin_bp.route('/camps')
@login_required
@admin_required
def camp_list():
    camps = CampSession.query.order_by(CampSession.start_date.desc()).all()
    return render_template('admin/camp_list.html', camps=camps,
                           title='Camp-Sessions')


@admin_bp.route('/camps/neu', methods=['GET', 'POST'])
@login_required
@admin_required
def camp_create():
    form = CampSessionForm()

    if form.validate_on_submit():
        camp = CampSession(
            name=form.name.data.strip(),
            year=form.year.data,
            start_date=form.start_date.data,
            end_date=form.end_date.data,
            location=form.location.data.strip() if form.location.data else None,
            description=form.description.data.strip() if form.description.data else None,
            status=form.status.data,
            registration_open=form.registration_open.data,
            registration_opens_at=form.registration_opens_at.data or None,
            registration_closes_at=form.registration_closes_at.data or None,
            max_registrants=form.max_registrants.data or 0,
            min_age_limit=form.min_age_limit.data or 0,
            max_age_limit=form.max_age_limit.data or 0,
            require_head_coaches=form.require_head_coaches.data,
        )
        db.session.add(camp)
        db.session.flush()

        # Seed default age groups
        for i, ag in enumerate(DEFAULT_AGE_GROUPS):
            group = AgeGroup(
                camp_session_id=camp.id,
                name=ag['name'],
                min_age=ag['min_age'],
                max_age=ag['max_age'],
                capacity=ag['capacity'],
                display_order=ag['display_order']
            )
            db.session.add(group)

        db.session.commit()
        flash(f'Camp "{camp.name}" erstellt.', 'success')
        return redirect(url_for('admin.camp_list'))

    return render_template('admin/camp_form.html', form=form,
                           action='create', title='Neues Camp')


@admin_bp.route('/camps/<int:camp_id>/bearbeiten', methods=['GET', 'POST'])
@login_required
@admin_required
def camp_edit(camp_id):
    camp = CampSession.query.get_or_404(camp_id)
    form = CampSessionForm(obj=camp)

    if form.validate_on_submit():
        camp.name                   = form.name.data.strip()
        camp.year                   = form.year.data
        camp.start_date             = form.start_date.data
        camp.end_date               = form.end_date.data
        camp.location               = form.location.data.strip() if form.location.data else None
        camp.description            = form.description.data.strip() if form.description.data else None
        camp.status                 = form.status.data
        camp.registration_open      = form.registration_open.data
        camp.registration_opens_at  = form.registration_opens_at.data or None
        camp.registration_closes_at = form.registration_closes_at.data or None
        camp.max_registrants        = form.max_registrants.data or 0
        camp.min_age_limit          = form.min_age_limit.data or 0
        camp.max_age_limit          = form.max_age_limit.data or 0
        camp.require_head_coaches   = form.require_head_coaches.data
        camp.updated_at             = datetime.utcnow()
        db.session.commit()

        flash('Camp aktualisiert.', 'success')
        return redirect(url_for('admin.camp_list'))

    return render_template('admin/camp_form.html', form=form, camp=camp,
                           action='edit', title=f'{camp.name} bearbeiten')


@admin_bp.route('/camps/<int:camp_id>/status/<status>', methods=['POST'])
@login_required
@admin_required
def camp_set_status(camp_id, status):
    '''Set camp session status. Valid transitions enforced.'''
    camp = CampSession.query.get_or_404(camp_id)
    valid_statuses = ('draft', 'upcoming', 'open', 'active', 'completed', 'cancelled', 'archived')

    if status not in valid_statuses:
        abort(400)

    camp.status = status
    if status == 'open':
        camp.registration_open = True
    elif status in ('completed', 'cancelled', 'active'):
        camp.registration_open = False
    camp.updated_at = datetime.utcnow()
    db.session.commit()

    flash(f'Camp-Status auf "{status}" gesetzt.', 'success')
    return redirect(url_for('admin.camp_edit', camp_id=camp.id))


@admin_bp.route('/camps/<int:camp_id>/loeschen', methods=['POST'])
@login_required
@admin_required
def camp_delete(camp_id):
    '''Delete a camp session. Only allowed when no confirmed/waitlisted registrations exist.'''
    camp = CampSession.query.get_or_404(camp_id)
    if camp.status in ('active', 'open', 'upcoming'):
        flash('Nur Entwürfe, abgeschlossene oder abgesagte Camps können gelöscht werden.', 'danger')
        return redirect(url_for('admin.camp_edit', camp_id=camp.id))
    # Block if any confirmed or waitlisted registrations exist
    active_regs = Registration.query.filter(
        Registration.camp_session_id == camp_id,
        Registration.status.in_(['confirmed', 'waitlisted', 'pending_verification'])
    ).count()
    if active_regs > 0:
        flash(f'Camp hat noch {active_regs} aktive Anmeldung(en). Zuerst stornieren.', 'danger')
        return redirect(url_for('admin.camp_edit', camp_id=camp.id))
    name = camp.name
    db.session.delete(camp)
    db.session.commit()
    flash(f'Camp-Session „{name}" wurde gelöscht.', 'success')
    return redirect(url_for('admin.camp_list'))


# =============================================================================
# [4] AGE GROUP MANAGEMENT + GROUP SPLITTING
# =============================================================================

class AgeGroupForm(FlaskForm):
    name            = StringField('Name (z.B. U10)', validators=[
                        DataRequired(), Length(max=20)])
    min_age         = IntegerField('Mindestalter', validators=[
                        DataRequired(), NumberRange(min=3, max=18)])
    max_age         = IntegerField('Höchstalter', validators=[
                        DataRequired(), NumberRange(min=3, max=18)])
    capacity        = IntegerField('Gruppenkapazität (0 = kein Limit)',
                        validators=[Optional(), NumberRange(min=0, max=500)],
                        default=0)
    display_order   = IntegerField('Anzeigereihenfolge',
                        validators=[Optional()])


@admin_bp.route('/camps/<int:camp_id>/gruppen')
@login_required
@admin_required
def age_group_list(camp_id):
    camp = CampSession.query.get_or_404(camp_id)
    age_groups = AgeGroup.query.filter_by(
        camp_session_id=camp_id
    ).order_by(AgeGroup.display_order).all()
    return render_template('admin/age_group_list.html', camp=camp,
                           age_groups=age_groups,
                           title=f'Altersgruppen — {camp.name}')


@admin_bp.route('/camps/<int:camp_id>/gruppen/neu', methods=['GET', 'POST'])
@login_required
@admin_required
def age_group_create(camp_id):
    camp = CampSession.query.get_or_404(camp_id)
    form = AgeGroupForm()

    if form.validate_on_submit():
        if form.min_age.data > form.max_age.data:
            flash('Mindestalter darf nicht größer als Höchstalter sein.', 'danger')
        else:
            next_order = db.session.query(
                db.func.coalesce(db.func.max(AgeGroup.display_order), 0)
            ).filter_by(camp_session_id=camp_id).scalar() + 1

            group = AgeGroup(
                camp_session_id=camp_id,
                name=form.name.data.strip(),
                min_age=form.min_age.data,
                max_age=form.max_age.data,
                capacity=form.capacity.data or 0,
                display_order=form.display_order.data or next_order,
            )
            db.session.add(group)
            db.session.commit()
            flash(f'Gruppe {group.name} erstellt.', 'success')
            return redirect(url_for('admin.age_group_list', camp_id=camp_id))

    return render_template('admin/age_group_form.html', form=form,
                           camp=camp, group=None,
                           title='Neue Gruppe')


@admin_bp.route('/camps/<int:camp_id>/gruppen/<int:group_id>/bearbeiten',
                methods=['GET', 'POST'])
@login_required
@admin_required
def age_group_edit(camp_id, group_id):
    camp  = CampSession.query.get_or_404(camp_id)
    group = AgeGroup.query.filter_by(
        id=group_id, camp_session_id=camp_id
    ).first_or_404()

    form = AgeGroupForm(obj=group)

    if form.validate_on_submit():
        if form.min_age.data > form.max_age.data:
            flash('Mindestalter darf nicht größer als Höchstalter sein.', 'danger')
        else:
            group.name          = form.name.data.strip()
            group.min_age       = form.min_age.data
            group.max_age       = form.max_age.data
            group.capacity      = form.capacity.data or 0
            group.display_order = form.display_order.data or group.display_order
            group.updated_at    = datetime.utcnow()
            db.session.commit()
            flash(f'Gruppe {group.name} aktualisiert.', 'success')
            return redirect(url_for('admin.age_group_list', camp_id=camp_id))

    return render_template('admin/age_group_form.html', form=form,
                           camp=camp, group=group,
                           title=f'{group.name} bearbeiten')


@admin_bp.route('/camps/<int:camp_id>/gruppen/<int:group_id>/loeschen',
                methods=['POST'])
@login_required
@admin_required
def age_group_delete(camp_id, group_id):
    '''
    Delete an age group. Blocked if confirmed registrations exist in it.
    Waitlisted and cancelled registrations are re-assigned to None.
    '''
    group = AgeGroup.query.filter_by(
        id=group_id, camp_session_id=camp_id
    ).first_or_404()

    confirmed_count = Registration.query.filter_by(
        age_group_id=group.id,
        status='confirmed'
    ).count()

    if confirmed_count > 0:
        flash(
            f'Gruppe {group.name} hat {confirmed_count} bestätigte Anmeldungen '
            f'und kann nicht gelöscht werden. Bitte zuerst die Anmeldungen '
            f'in eine andere Gruppe verschieben.',
            'danger'
        )
        return redirect(url_for('admin.age_group_list', camp_id=camp_id))

    # Nullify non-confirmed registrations
    Registration.query.filter_by(age_group_id=group.id).update(
        {'age_group_id': None}
    )
    db.session.delete(group)
    db.session.commit()

    flash(f'Gruppe {group.name} gelöscht.', 'info')
    return redirect(url_for('admin.age_group_list', camp_id=camp_id))


# --- Group Splitting (algorithm + slider UI) ---

@admin_bp.route('/camps/<int:camp_id>/gruppen-planer')
@login_required
@admin_required
def group_planner(camp_id):
    '''
    Interactive group splitting tool.
    Admin picks number of groups, app recommends splits,
    slider adjusts boundaries with live child count preview.
    Confirm saves and re-assigns all registrations.
    '''
    camp = CampSession.query.get_or_404(camp_id)
    age_distribution = get_age_distribution(camp)
    total_registered = sum(age_distribution.values())
    age_groups = camp.age_groups  # pass current groups so JS can set initial slider positions

    return render_template(
        'admin/group_planner.html',
        camp=camp,
        age_distribution=age_distribution,
        total_registered=total_registered,
        age_groups=age_groups,
        max_groups=min(6, len(age_distribution)),
        title=f'Gruppenplanung — {camp.name}'
    )


@admin_bp.route('/camps/<int:camp_id>/gruppen-planer/empfehlung')
@login_required
@admin_required
def group_recommendation(camp_id):
    '''
    AJAX: return recommended group split for given number of groups.
    Called when admin selects num_groups in the planner UI.
    '''
    camp        = CampSession.query.get_or_404(camp_id)
    num_groups  = request.args.get('n', request.args.get('num_groups', 2), type=int)
    num_groups  = max(1, min(num_groups, 10))

    distribution    = get_age_distribution(camp)
    recommendation  = recommend_group_split(distribution, num_groups)

    # Add capacity field expected by the save route
    for g in recommendation:
        g.setdefault('name', g.get('suggested_name', f'Gruppe'))  # capacity not set from planner

    return jsonify({'success': True, 'plan': recommendation})


@admin_bp.route('/camps/<int:camp_id>/gruppen-planer/vorschau')
@login_required
@admin_required
def group_preview():
    '''
    AJAX: live preview of child counts for given age boundaries.
    Called on every slider move — must be fast.

    Expects query params: boundaries=4-7,8-11,12-15
    (comma-separated min-max pairs with hyphen)
    '''
    camp_id     = request.view_args['camp_id']
    camp        = CampSession.query.get_or_404(camp_id)
    raw         = request.args.get('boundaries', '')

    try:
        boundaries = []
        for part in raw.split(','):
            lo, hi = part.strip().split('-')
            boundaries.append((int(lo), int(hi)))
    except (ValueError, AttributeError):
        return jsonify({'error': 'Ungültige Grenzen'}), 400

    distribution = get_age_distribution(camp)
    preview = preview_group_split(distribution, boundaries)
    return jsonify(preview)


@admin_bp.route('/camps/<int:camp_id>/gruppen-planer/speichern',
                methods=['POST'])
@login_required
@admin_required
def group_planner_save(camp_id):
    '''
    Save confirmed group split from the planner.
    Replaces existing age groups and re-assigns all registrations.
    '''
    camp = CampSession.query.get_or_404(camp_id)

    try:
        # Accept either JSON body or form-encoded plan_json field
        if request.is_json:
            groups_raw = request.get_json()
        else:
            plan_json = request.form.get('plan_json', '')
            groups_raw = json.loads(plan_json) if plan_json else None
        if not groups_raw:
            return jsonify({'success': False, 'message': 'Keine Daten.'}), 400

        # Validate each group definition
        group_defs = []
        for g in groups_raw:
            if not all(k in g for k in ('name', 'min_age', 'max_age')):
                return jsonify({
                    'success': False,
                    'message': 'Fehlende Felder in Gruppendefinition.'
                }), 400
            group_defs.append({
                'name':     g['name'].strip(),
                'min_age':  int(g['min_age']),
                'max_age':  int(g['max_age']),
                # Capacity is managed separately in age group settings,
                # not by the planner sliders. Use a large number so the
                # planner never creates an artificial cap.
                'capacity': int(g.get('capacity', 0))
            })

    except (ValueError, TypeError) as e:
        return jsonify({'success': False, 'message': str(e)}), 400

    success, message = save_group_split(camp, group_defs)
    log_analytics_event('group_split_saved', success=success,
                        detail=str(len(group_defs)))

    if request.is_json:
        return jsonify({'success': success, 'message': message})

    # Form POST — redirect with flash message
    if success:
        flash(f'Gruppen gespeichert und Anmeldungen neu zugewiesen.', 'success')
    else:
        flash(f'Fehler: {message}', 'danger')
    return redirect(url_for('admin.age_group_list', camp_id=camp_id))


# =============================================================================
# [5] GROUP ASSIGNMENTS (HEAD COACH)
# =============================================================================

@admin_bp.route('/camps/<int:camp_id>/trainer')
@login_required
@admin_required
def trainer_assignments(camp_id):
    '''
    Trainer assignment overview.
    Shows each age group, current head coach, and all assigned trainers.
    Orange warning shown for groups missing a head coach.
    '''
    camp = CampSession.query.get_or_404(camp_id)
    conflicts = get_head_coach_conflicts(camp) if camp.require_head_coaches else []

    trainers = User.query.join(StaffProfile).filter(
        StaffProfile.staff_class == 'trainer',
        User.is_deleted == False,
        User.is_active == True
    ).order_by(User.last_name).all()

    return render_template(
        'admin/trainer_assignments.html',
        camp=camp,
        trainers=trainers,
        conflicts=conflicts,
        title=f'Trainer-Zuweisung — {camp.name}'
    )


@admin_bp.route('/camps/<int:camp_id>/trainer/zuweisen', methods=['POST'])
@login_required
@admin_required
def assign_trainer(camp_id):
    '''Assign or update head coach for an age group.'''
    camp        = CampSession.query.get_or_404(camp_id)
    group_id    = request.form.get('age_group_id', type=int)
    staff_id    = request.form.get('staff_user_id', type=int)

    if not group_id or not staff_id:
        flash('Bitte Gruppe und Trainer auswählen.', 'danger')
        return redirect(url_for('admin.trainer_assignments', camp_id=camp_id))

    age_group   = AgeGroup.query.filter_by(
        id=group_id, camp_session_id=camp_id
    ).first_or_404()
    staff_user  = User.query.filter_by(
        id=staff_id, is_deleted=False
    ).first_or_404()

    success, message = assign_head_coach(age_group, staff_user, current_user)
    log_analytics_event('head_coach_assigned', success=success)

    flash(message, 'success' if success else 'danger')
    return redirect(url_for('admin.trainer_assignments', camp_id=camp_id))


@admin_bp.route('/camps/<int:camp_id>/trainer/<int:assignment_id>/entfernen',
                methods=['POST'])
@login_required
@admin_required
def remove_trainer(camp_id, assignment_id):
    '''Remove a trainer from a group assignment.'''
    assignment = GroupAssignment.query.filter_by(
        id=assignment_id, camp_session_id=camp_id
    ).first_or_404()

    was_head = assignment.is_head_coach
    group_name = assignment.age_group.name
    db.session.delete(assignment)
    db.session.commit()

    if was_head:
        flash(
            f'Haupttrainer aus {group_name} entfernt. '
            f'Bitte neuen Haupttrainer zuweisen.',
            'warning'
        )
    else:
        flash(f'Trainer aus {group_name} entfernt.', 'info')

    return redirect(url_for('admin.trainer_assignments', camp_id=camp_id))


# =============================================================================
# [6] USER MANAGEMENT
# =============================================================================

class UserEditForm(FlaskForm):
    first_name      = StringField('Vorname', validators=[DataRequired(), Length(max=100)])
    last_name       = StringField('Nachname', validators=[DataRequired(), Length(max=100)])
    email           = EmailField('E-Mail', validators=[DataRequired(), Email(check_deliverability=False), Length(max=255)])
    phone           = StringField('Telefon', validators=[Optional(), Length(max=30)])
    address_street  = StringField('Straße', validators=[Optional(), Length(max=255)])
    address_city    = StringField('Stadt', validators=[Optional(), Length(max=100)])
    address_postcode = StringField('PLZ', validators=[Optional(), Length(max=20)])
    role            = SelectField('Rolle', choices=[
                        ('parent', 'Elternteil'),
                        ('staff',  'Team-Mitglied'),
                        ('admin',  'Administrator'),
                      ])
    is_active       = SelectField('Konto aktiv', choices=[
                        ('True', 'Ja'), ('False', 'Nein')
                      ], coerce=lambda x: x == 'True' or x is True)
    email_verified  = SelectField('E-Mail bestätigt', choices=[
                        ('True', 'Ja'), ('False', 'Nein')
                      ], coerce=lambda x: x == 'True' or x is True)
    email_announcements = BooleanField('E-Mail-Benachrichtigungen')


class StaffProfileForm(FlaskForm):
    staff_class     = SelectField('Funktion', choices=[
                        ('trainer',  'Trainer'),
                        ('general',  'Allgemein'),
                        ('food',     'Verpflegung'),
                      ])
    is_first_aid    = BooleanField('Ersthelfer')
    notes           = TextAreaField('Interne Notizen',
                        validators=[Optional(), Length(max=1000)])


class StaffInviteForm(FlaskForm):
    email           = EmailField('E-Mail', validators=[DataRequired(), Email(check_deliverability=False)])
    role            = SelectField('Rolle', choices=[
                        ('staff', 'Team-Mitglied'),
                        ('admin', 'Administrator'),
                      ])
    staff_class     = SelectField('Funktion', choices=[
                        ('trainer',  'Trainer'),
                        ('general',  'Allgemein'),
                        ('food',     'Verpflegung'),
                      ])
    is_first_aid    = BooleanField('Ersthelfer')


class ResetPasswordForm(FlaskForm):
    new_password    = StringField('Neues Passwort', validators=[
                        DataRequired(), Length(min=8)])


@admin_bp.route('/benutzer')
@login_required
@admin_required
def user_list():
    role_filter = request.args.get('rolle', 'all')
    page        = request.args.get('seite', 1, type=int)
    query_str   = request.args.get('q', '').strip()

    query = User.query.filter_by(is_deleted=False)

    if role_filter != 'all':
        query = query.filter_by(role=role_filter)

    if query_str:
        from sqlalchemy import or_
        query = query.filter(
            or_(
                User.email.ilike(f'%{query_str}%'),
                User.first_name.ilike(f'%{query_str}%'),
                User.last_name.ilike(f'%{query_str}%'),
            )
        )

    users = paginate_query(
        query.order_by(User.last_name, User.first_name),
        page=page
    )

    return render_template(
        'admin/user_list.html',
        users=users,
        role_filter=role_filter,
        query_str=query_str,
        title='Benutzerverwaltung'
    )


@admin_bp.route('/benutzer/<int:user_id>')
@login_required
@admin_required
def user_detail(user_id):
    user = User.query.get_or_404(user_id)
    return render_template(
        'admin/user_detail.html',
        user=user,
        title=user.full_name
    )


@admin_bp.route('/benutzer/<int:user_id>/bearbeiten', methods=['GET', 'POST'])
@login_required
@admin_required
def user_edit(user_id):
    user = User.query.filter_by(id=user_id, is_deleted=False).first_or_404()
    form = UserEditForm(obj=user)

    if form.validate_on_submit():
        # Email uniqueness check (excluding this user)
        new_email = form.email.data.lower().strip()
        if new_email != user.email:
            conflict = User.query.filter_by(email=new_email).first()
            if conflict:
                flash('Diese E-Mail-Adresse wird bereits verwendet.', 'danger')
                return render_template('admin/user_edit.html', form=form,
                                       user=user, title=f'{user.full_name} bearbeiten')

        user.first_name         = (form.first_name.data or '').strip()
        user.last_name          = (form.last_name.data or '').strip()
        user.email              = new_email
        user.role               = form.role.data
        user.phone              = (form.phone.data or '').strip() or None
        user.address_street     = (form.address_street.data or '').strip() or None
        user.address_city       = (form.address_city.data or '').strip() or None
        user.address_postcode   = (form.address_postcode.data or '').strip() or None
        user.is_active          = form.is_active.data
        user.email_verified     = form.email_verified.data
        user.email_announcements = form.email_announcements.data
        user.updated_at         = datetime.utcnow()
        db.session.commit()

        flash(f'{user.full_name} aktualisiert.', 'success')
        return redirect(url_for('admin.user_detail', user_id=user.id))

    return render_template('admin/user_edit.html', form=form, user=user,
                           title=f'{user.full_name} bearbeiten')


@admin_bp.route('/benutzer/<int:user_id>/passwort-reset', methods=['POST'])
@login_required
@admin_required
def user_reset_password(user_id):
    '''Admin resets a user's password directly. Used when user is locked out.'''
    user = User.query.filter_by(id=user_id, is_deleted=False).first_or_404()
    form = ResetPasswordForm()

    if form.validate_on_submit():
        user.password_hash = hash_password(form.new_password.data)
        user.updated_at = datetime.utcnow()
        db.session.commit()
        flash(f'Passwort für {user.full_name} zurückgesetzt.', 'success')

    return redirect(url_for('admin.user_detail', user_id=user.id))


@admin_bp.route('/benutzer/<int:user_id>/staff-profil', methods=['POST'])
@login_required
@admin_required
def user_edit_staff_profile(user_id):
    '''Edit staff class, first aid flag, and notes for a staff/admin user.'''
    user    = User.query.filter_by(id=user_id, is_deleted=False).first_or_404()
    profile = user.staff_profile
    form    = StaffProfileForm()

    if not profile:
        profile = StaffProfile(user_id=user.id)
        db.session.add(profile)

    if form.validate_on_submit():
        profile.staff_class  = form.staff_class.data
        profile.is_first_aid = form.is_first_aid.data
        profile.notes        = form.notes.data.strip() or None
        profile.updated_at   = datetime.utcnow()
        db.session.commit()
        flash('Staff-Profil aktualisiert.', 'success')

    return redirect(url_for('admin.user_detail', user_id=user.id))


@admin_bp.route('/benutzer/<int:user_id>/loeschen', methods=['POST'])
@login_required
@admin_required
def user_delete(user_id):
    '''
    Soft-delete a user account (GDPR erasure).
    Blocked if user has children with active registrations.
    Admin cannot delete their own account this way.
    '''
    user = User.query.filter_by(id=user_id, is_deleted=False).first_or_404()

    if user.id == current_user.id:
        flash('Sie können Ihr eigenes Konto nicht löschen.', 'danger')
        return redirect(url_for('admin.user_detail', user_id=user.id))

    # Check for active registrations on children
    if user.role == 'parent':
        active_regs = Registration.query.join(Child).filter(
            Child.parent_user_id == user.id,
            Registration.status.in_(['confirmed', 'waitlisted', 'pending_verification'])
        ).count()

        if active_regs > 0:
            flash(
                f'{user.full_name} hat {active_regs} aktive Anmeldungen. '
                f'Bitte zuerst alle Anmeldungen stornieren.',
                'danger'
            )
            return redirect(url_for('admin.user_detail', user_id=user.id))

    success, message = soft_delete_user(user, reason='admin_action')
    flash(message, 'success' if success else 'danger')
    return redirect(url_for('admin.user_list'))


# --- Staff Invites ---

@admin_bp.route('/einladen', methods=['GET', 'POST'])
@login_required
@admin_required
def invite_staff():
    form = StaffInviteForm()

    if form.validate_on_submit():
        success, token, message = create_staff_invite(
            email=form.email.data,
            staff_class=form.staff_class.data,
            is_first_aid=False,
            role=form.role.data,
            invited_by_user=current_user
        )

        if success:
            # Get the newly created user to send invite email
            invited_user = User.query.filter_by(
                email=form.email.data.lower().strip()
            ).first()

            try:
                send_staff_invite_email(invited_user, token, current_user)
                log_email(invited_user.id, invited_user.email, 'invite',
                          f'Einladung zum {current_app.config["CAMP_NAME"]}')
                log_analytics_event('invite_sent', success=True,
                                    detail=form.role.data)
            except Exception as e:
                current_app.logger.error(f'[Admin] Invite email failed: {e}')
                flash(
                    f'Konto erstellt, aber Einladungs-E-Mail fehlgeschlagen. '
                    f'Einladungslink: {url_for("auth.redeem_invite", token=token, _external=True)}',
                    'warning'
                )
                return redirect(url_for('admin.user_list'))

            flash(f'Einladung an {form.email.data} gesendet.', 'success')
        else:
            flash(message, 'danger')

        return redirect(url_for('admin.user_list'))

    return render_template('admin/invite_staff.html', form=form,
                           title='Team-Mitglied einladen')


# =============================================================================
# [7] CHILD & REGISTRATION MANAGEMENT
# =============================================================================

class ChildEditForm(FlaskForm):
    first_name          = StringField('Vorname', validators=[
                            DataRequired(), Length(max=100)])
    last_name           = StringField('Nachname', validators=[
                            DataRequired(), Length(max=100)])
    date_of_birth       = DateField('Geburtsdatum', validators=[DataRequired()])
    medical_notes       = TextAreaField('Medizinische Hinweise',
                            validators=[Optional(), Length(max=2000)])
    photo_consent_default = BooleanField('Allgemeine Fotoerlaubnis')


class EmergencyContactForm(FlaskForm):
    full_name           = StringField('Name', validators=[DataRequired(), Length(max=200)])
    relationship        = StringField('Beziehung', validators=[Optional(), Length(max=100)])
    phone_primary       = StringField('Telefon (Haupt)', validators=[DataRequired(), Length(max=30)])
    phone_secondary     = StringField('Telefon (alternativ)', validators=[Optional(), Length(max=30)])


class RegistrationEditForm(FlaskForm):
    status              = SelectField('Status', choices=[
                            ('confirmed',           'Bestätigt'),
                            ('waitlisted',          'Warteliste'),
                            ('pending_verification','Ausstehend'),
                            ('cancelled',           'Storniert'),
                            ('attended',            'Teilgenommen'),
                          ])
    age_group_id        = SelectField('Altersgruppe', coerce=int)
    waitlist_position   = IntegerField('Wartelistenposition',
                            validators=[Optional()])
    independent_travel  = BooleanField('Heimweg alleine genehmigt')
    photo_consent       = BooleanField('Fotoerlaubnis bestätigt')
    admin_override_reason = TextAreaField('Grund für manuelle Zuweisung',
                            validators=[Optional(), Length(max=500)])


@admin_bp.route('/kinder')
@login_required
@admin_required
def child_list():
    page      = request.args.get('seite', 1, type=int)
    query_str = request.args.get('q', '').strip()

    query = Child.query.filter_by(is_deleted=False)

    if query_str:
        from sqlalchemy import or_
        query = query.filter(
            or_(
                Child.first_name.ilike(f'%{query_str}%'),
                Child.last_name.ilike(f'%{query_str}%'),
            )
        )

    children = paginate_query(
        query.order_by(Child.last_name, Child.first_name),
        page=page
    )

    return render_template('admin/child_list.html', children=children,
                           query_str=query_str, title='Kinder')


@admin_bp.route('/kinder/<int:child_id>/bearbeiten', methods=['GET', 'POST'])
@login_required
@admin_required
def child_edit(child_id):
    '''Admin can edit any child's details. Cannot change parent ownership.'''
    child = Child.query.filter_by(id=child_id, is_deleted=False).first_or_404()
    form  = ChildEditForm(obj=child)

    if form.validate_on_submit():
        child.first_name            = form.first_name.data.strip()
        child.last_name             = form.last_name.data.strip()
        child.date_of_birth         = form.date_of_birth.data
        child.medical_notes         = form.medical_notes.data.strip() or None
        child.photo_consent_default = form.photo_consent_default.data
        child.updated_at            = datetime.utcnow()
        db.session.commit()

        flash(f'{child.full_name} aktualisiert.', 'success')
        return redirect(url_for('admin.user_detail',
                                user_id=child.parent_user_id))

    # Emergency contact
    ec = child.emergency_contacts[0] if child.emergency_contacts else None

    return render_template('admin/child_edit.html', form=form, child=child,
                           ec=ec, title=f'{child.full_name} bearbeiten')


@admin_bp.route('/kinder/<int:child_id>/notfallkontakt', methods=['POST'])
@login_required
@admin_required
def child_edit_emergency_contact(child_id):
    '''Add or update emergency contact for a child.'''
    child = Child.query.filter_by(id=child_id, is_deleted=False).first_or_404()
    form  = EmergencyContactForm()

    if form.validate_on_submit():
        ec = child.emergency_contacts[0] if child.emergency_contacts else None
        if ec:
            ec.full_name        = form.full_name.data.strip()
            ec.relationship     = form.relationship.data.strip() or None
            ec.phone_primary    = form.phone_primary.data.strip()
            ec.phone_secondary  = form.phone_secondary.data.strip() or None
        else:
            ec = EmergencyContact(
                child_id=child.id,
                full_name=form.full_name.data.strip(),
                relationship=form.relationship.data.strip() or None,
                phone_primary=form.phone_primary.data.strip(),
                phone_secondary=form.phone_secondary.data.strip() or None,
            )
            db.session.add(ec)
        db.session.commit()
        flash('Notfallkontakt aktualisiert.', 'success')

    return redirect(url_for('admin.child_edit', child_id=child_id))


@admin_bp.route('/anmeldungen/<int:registration_id>/bearbeiten',
                methods=['GET', 'POST'])
@login_required
@admin_required
def registration_edit(registration_id):
    '''
    Edit a registration record.
    Admin can override age group, status, waitlist position,
    independent travel consent, and photo consent.
    Age group changes are recorded as admin_override with reason.
    '''
    reg  = Registration.query.get_or_404(registration_id)
    camp = reg.camp_session
    form = RegistrationEditForm(obj=reg)

    # Populate age group choices for this session
    form.age_group_id.choices = [
        (g.id, f'{g.name} (U{g.min_age}–{g.max_age})')
        for g in camp.age_groups
    ]
    form.age_group_id.choices.insert(0, (0, '— Keine Gruppe —'))

    if form.validate_on_submit():
        old_group_id    = reg.age_group_id
        new_group_id    = form.age_group_id.data or None

        reg.status              = form.status.data
        reg.waitlist_position   = form.waitlist_position.data
        reg.independent_travel  = form.independent_travel.data
        reg.photo_consent       = form.photo_consent.data
        reg.updated_at          = datetime.utcnow()

        # Record age group override if group changed
        if new_group_id != old_group_id:
            reg.age_group_id        = new_group_id
            reg.admin_override      = True
            reg.admin_override_by   = current_user.id
            reg.admin_override_at   = datetime.utcnow()
            reg.admin_override_reason = form.admin_override_reason.data.strip() or None

        db.session.commit()
        flash('Anmeldung aktualisiert.', 'success')
        return redirect(url_for('admin.user_detail',
                                user_id=reg.child.parent_user_id))

    return render_template('admin/registration_edit.html', form=form,
                           reg=reg, registration=reg, camp=camp,
                           title=f'Anmeldung bearbeiten — {reg.child.full_name}')


@admin_bp.route('/kinder/<int:child_id>/qr-neu', methods=['POST'])
@login_required
@admin_required
def regenerate_qr(child_id):
    '''Regenerate QR token for a child (e.g. if token was compromised).'''
    child = Child.query.filter_by(id=child_id, is_deleted=False).first_or_404()
    regenerate_qr_token(child)
    flash(f'QR-Code für {child.full_name} neu generiert.', 'success')
    return redirect(url_for('admin.user_detail',
                            user_id=child.parent_user_id))


# =============================================================================
# [8] MANUAL ENTRY (paper signup form)
# =============================================================================

@admin_bp.route('/manuelle-eingabe', methods=['GET', 'POST'])
@login_required
@admin_required
def manual_entry():
    '''
    Admin enters a child from a paper signup form.

    Two-phase UI:
    Phase 1 — Parent search: live search by name/email/phone.
              Existing parent selected → auto-fill + child toggle cards.
              No match → create new parent account.

    Phase 2 — Child selection: existing children shown as toggle cards.
              Eligible = toggle on (default on).
              Ineligible age = greyed out, disabled, tooltip showing why.
              Already registered = greyed out, "Bereits angemeldet" label.
              New child option always available.

    Paper consent checkboxes recorded by admin.
    Verification email sent to parent to complete digital consent.
    '''
    camp_session = get_active_session()
    if not camp_session:
        flash('Kein aktives Camp gefunden. Bitte zuerst eine Camp-Session erstellen.',
              'warning')
        return redirect(url_for('admin.dashboard'))

    if request.method == 'POST':
        form_data = {
            'parent_email':             request.form.get('parent_email', '').strip(),
            'parent_first_name':        request.form.get('parent_first_name', '').strip(),
            'parent_last_name':         request.form.get('parent_last_name', '').strip(),
            'parent_phone':             request.form.get('parent_phone', '').strip(),
            'parent_address_street':    request.form.get('parent_address_street', '').strip(),
            'parent_address_city':      request.form.get('parent_address_city', '').strip(),
            'parent_address_postcode':  request.form.get('parent_address_postcode', '').strip(),
            'child_first_name':         request.form.get('child_first_name', '').strip(),
            'child_last_name':          request.form.get('child_last_name', '').strip(),
            'child_dob':                _parse_date(request.form.get('child_dob')),
            'child_medical_notes':      request.form.get('child_medical_notes', '').strip(),
            'emergency_contact_name':   request.form.get('ec_name', '').strip(),
            'emergency_contact_relationship': request.form.get('ec_relationship', '').strip(),
            'emergency_contact_phone':  request.form.get('ec_phone', '').strip(),
            'camp_session_id':          camp_session.id,
            'paper_consent_data':       bool(request.form.get('paper_consent_data')),
            'paper_consent_photo':      bool(request.form.get('paper_consent_photo')),
            'paper_consent_travel':     bool(request.form.get('paper_consent_travel')),
        }

        # If existing child selected, use that child_id instead
        existing_child_id = request.form.get('existing_child_id', type=int)
        force_group_id    = request.form.get('force_age_group_id', type=int)

        if existing_child_id:
            # Register existing child for the camp
            child = Child.query.filter_by(
                id=existing_child_id, is_deleted=False
            ).first_or_404()

            if child_already_registered(child.id, camp_session.id):
                flash(f'{child.full_name} ist bereits angemeldet.', 'warning')
                return redirect(url_for('admin.manual_entry'))

            # Use existing child data, just create the registration
            age_group = assign_age_group(child, camp_session)
            if not age_group and not force_group_id:
                flash(
                    f'{child.full_name} passt in keine Altersgruppe. '
                    f'Bitte manuell zuweisen.',
                    'warning'
                )
                return redirect(url_for('admin.manual_entry'))

            from sub_modules.helpers import get_next_waitlist_position
            if age_group and age_group.is_full:
                status = 'waitlisted'
                waitlist_pos = get_next_waitlist_position(
                    camp_session.id,
                    force_group_id or age_group.id
                )
            else:
                status = 'pending_verification' if not child.parent.email_verified \
                         else 'confirmed'
                waitlist_pos = None

            reg = Registration(
                child_id=child.id,
                camp_session_id=camp_session.id,
                age_group_id=force_group_id or (age_group.id if age_group else None),
                status=status,
                waitlist_position=waitlist_pos,
                auto_assigned_group=age_group.name if age_group else None,
                admin_override=bool(force_group_id),
                admin_override_by=current_user.id if force_group_id else None,
                admin_override_at=datetime.utcnow() if force_group_id else None,
                independent_travel=form_data['paper_consent_travel'],
                photo_consent=form_data['paper_consent_photo'],
                paper_consent_data_processing=form_data['paper_consent_data'],
                paper_consent_photo=form_data['paper_consent_photo'],
                paper_consent_independent_travel=form_data['paper_consent_travel'],
                entered_by_user_id=current_user.id,
                entered_at=datetime.utcnow(),
            )
            db.session.add(reg)
            db.session.commit()

            log_analytics_event('manual_entry', success=True, detail='existing_child')
            flash(f'{child.full_name} erfolgreich angemeldet.', 'success')
            return redirect(url_for('admin.user_detail',
                                    user_id=child.parent_user_id))

        else:
            # Create new parent + child via helper
            if form_data['child_dob'] is None:
                flash('Ungültiges Geburtsdatum.', 'danger')
                return redirect(url_for('admin.manual_entry'))

            form_data['force_age_group_id'] = force_group_id

            success, message, parent = create_manual_entry(current_user, form_data)
            log_analytics_event('manual_entry', success=success, detail='new_parent')

            flash(message, 'success' if success else 'danger')
            if success and parent:
                return redirect(url_for('admin.user_detail', user_id=parent.id))
            return redirect(url_for('admin.manual_entry'))

    # GET: build context for the form
    age_groups = camp_session.age_groups

    return render_template(
        'admin/manual_entry.html',
        camp_session=camp_session,
        age_groups=age_groups,
        title='Manuelle Anmeldung (Papierformular)'
    )


@admin_bp.route('/eltern-suche')
@login_required
@admin_required
def parent_search_ajax():
    '''
    AJAX: live parent search for manual entry form.
    Returns JSON list of matching parents with their children.
    Called as user types in the search box.
    '''
    query = request.args.get('q', '').strip()
    results = search_existing_parents(query, limit=8)
    return jsonify(results)


@admin_bp.route('/eltern/<int:user_id>/details')
@login_required
@admin_required
def parent_details_ajax(user_id):
    '''
    AJAX: return full parent + children details for form auto-fill.
    Also returns child eligibility for the active camp session.
    Called when admin selects a parent from search results.
    '''
    parent_data = get_parent_for_manual_entry(user_id)
    if not parent_data:
        return jsonify({'error': 'Nicht gefunden'}), 404

    # Enrich children with eligibility data for active session
    camp = get_active_session()
    if camp:
        for child_data in parent_data['children']:
            child = Child.query.get(child_data['id'])
            if child:
                age_group = assign_age_group(child, camp)
                already_reg = child_already_registered(child.id, camp.id)
                child_data['eligible']          = age_group is not None and not already_reg
                child_data['already_registered'] = already_reg
                child_data['age_group_name']    = age_group.name if age_group else None
                child_data['age_on_start']      = child.age_on(camp.start_date)
                child_data['ineligible_reason'] = (
                    'Bereits angemeldet' if already_reg
                    else 'Kein passendes Alter' if not age_group
                    else None
                )

    return jsonify(parent_data)


def _parse_date(date_str):
    '''Parse a date string from form input. Returns date object or None.'''
    if not date_str:
        return None
    for fmt in ('%Y-%m-%d', '%d.%m.%Y', '%d/%m/%Y'):
        try:
            return datetime.strptime(date_str, fmt).date()
        except ValueError:
            continue
    return None


# =============================================================================
# [9] PRINTABLE ROSTER
# =============================================================================

@admin_bp.route('/camps/<int:camp_id>/teilnehmerliste')
@login_required
@admin_required
def roster(camp_id):
    '''
    Full printable roster for a camp — all confirmed children across all age
    groups. Intended to be printed; opens as a clean @media print page.

    Sorted by age group display order, then child last name.
    One page-break section per group so each group starts on a fresh page.

    Columns: name, age group, emergency contact name + phone,
             independent travel approved.
    '''
    camp = CampSession.query.get_or_404(camp_id)

    groups = AgeGroup.query.filter_by(
        camp_session_id=camp.id
    ).order_by(AgeGroup.min_age).all()

    group_sections = []
    total_confirmed = 0

    for group in groups:
        registrations = (
            Registration.query
            .filter_by(age_group_id=group.id, status='confirmed')
            .join(Child)
            .order_by(Child.last_name, Child.first_name)
            .all()
        )
        total_confirmed += len(registrations)
        group_sections.append({
            'group':         group,
            'registrations': registrations,
        })

    return render_template(
        'admin/roster.html',
        camp=camp,
        group_sections=group_sections,
        total_confirmed=total_confirmed,
        title=f'Teilnehmerliste — {camp.name}',
    )


# =============================================================================
# NAME TAGS — printable, 10 per A4 page
# =============================================================================

@admin_bp.route('/camps/<int:camp_id>/namensschilder')
@login_required
@admin_required
def name_tags(camp_id):
    '''
    Printable name tags — 10 per A4 page (2 columns x 5 rows), business card size.
    Each card: child name, age group, QR code, medical alert icon.
    Staff cards have a different colour scheme.
    A separate "back" page prints the camp logo/name/year for double-sided printing.
    '''
    import qrcode, io, base64
    from flask import request as req
    from sub_modules.helpers import get_or_create_qr_token

    camp = CampSession.query.get_or_404(camp_id)
    side = req.args.get('side', 'front')   # 'front' or 'back'
    kind = req.args.get('kind', 'kids')    # 'kids' or 'staff'

    cards = []

    if kind == 'kids':
        groups = AgeGroup.query.filter_by(camp_session_id=camp.id).order_by(AgeGroup.min_age).all()
        for group in groups:
            regs = (Registration.query
                    .filter_by(age_group_id=group.id, status='confirmed')
                    .join(Child).order_by(Child.last_name, Child.first_name).all())
            for reg in regs:
                child = reg.child
                qr_token = get_or_create_qr_token(child)
                qr_url   = url_for('staff.checkin_by_qr', token=qr_token.token, _external=True)
                qr_img   = qrcode.make(qr_url, box_size=6, border=2)
                buf      = io.BytesIO()
                qr_img.save(buf, format='PNG')
                qr_b64   = base64.b64encode(buf.getvalue()).decode()
                cards.append({
                    'name':      child.full_name,
                    'sub':       group.name,
                    'medical':   child.has_medical_notes,
                    'qr_b64':    qr_b64,
                    'colour':    '#1b3f2a',
                    'text_colour': '#b8f442',
                    'kind':      'child',
                })
    else:
        staff_users = (User.query
                       .filter(User.role.in_(['staff', 'admin']),
                               User.is_deleted == False,
                               User.is_active == True,
                               User.email_verified == True)
                       .order_by(User.last_name, User.first_name).all())
        for user in staff_users:
            profile = user.staff_profile
            func = ''
            if profile:
                func = {'trainer': 'Trainer', 'food': 'Verpflegung'}.get(profile.staff_class, 'Team')
            cards.append({
                'name':       user.full_name,
                'sub':        func,
                'medical':    False,
                'qr_b64':     None,
                'colour':     '#6366f1',
                'text_colour': '#ffffff',
                'kind':       'staff',
            })

    return render_template(
        'admin/name_tags.html',
        camp=camp,
        cards=cards,
        side=side,
        kind=kind,
        title=f'Namensschilder — {camp.name}',
    )


# =============================================================================
# ROSTER EXPORTS — CSV
# =============================================================================

@admin_bp.route('/camps/<int:camp_id>/teilnehmerliste/csv')
@login_required
@admin_required
def roster_csv(camp_id):
    '''Download kids roster as CSV.'''
    import csv, io
    camp = CampSession.query.get_or_404(camp_id)
    groups = AgeGroup.query.filter_by(camp_session_id=camp.id).order_by(AgeGroup.min_age).all()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        'Vorname', 'Nachname', 'Geburtsdatum', 'Altersgruppe',
        'Notfallkontakt', 'Notfall-Telefon', 'Heimweg alleine', 'Fotoerlaubnis'
    ])
    for group in groups:
        regs = (Registration.query
                .filter_by(age_group_id=group.id, status='confirmed')
                .join(Child).order_by(Child.last_name, Child.first_name).all())
        for reg in regs:
            child = reg.child
            ec = child.emergency_contacts[0] if child.emergency_contacts else None
            writer.writerow([
                child.first_name, child.last_name,
                child.date_of_birth.strftime('%d.%m.%Y'),
                group.name,
                ec.full_name if ec else '',
                ec.phone_primary if ec else '',
                'Ja' if reg.independent_travel else 'Nein',
                'Ja' if reg.photo_consent else 'Nein',
            ])

    response = make_response(output.getvalue().encode('utf-8-sig'))
    response.headers['Content-Type'] = 'text/csv; charset=utf-8'
    response.headers['Content-Disposition'] = f'attachment; filename="teilnehmerliste-{camp.id}.csv"'
    return response


@admin_bp.route('/camps/<int:camp_id>/team-liste')
@login_required
@admin_required
def staff_roster(camp_id):
    '''Printable staff roster for a camp.'''
    camp = CampSession.query.get_or_404(camp_id)
    staff_users = (User.query
                   .filter(User.role.in_(['staff', 'admin']),
                           User.is_deleted == False,
                           User.is_active == True,
                           User.email_verified == True)
                   .order_by(User.last_name, User.first_name).all())

    # Annotate with group assignments
    staff_data = []
    for user in staff_users:
        assignments = []
        for a in (user.group_assignments or []):
            if a.age_group and a.age_group.camp_session_id == camp_id:
                assignments.append(a)
        staff_data.append({
            'user': user,
            'assignments': assignments,
            'profile': user.staff_profile,
        })

    return render_template(
        'admin/staff_roster.html',
        camp=camp,
        staff_data=staff_data,
        title=f'Team-Liste — {camp.name}',
    )


@admin_bp.route('/camps/<int:camp_id>/team-liste/csv')
@login_required
@admin_required
def staff_roster_csv(camp_id):
    '''Download staff roster as CSV.'''
    import csv, io
    camp = CampSession.query.get_or_404(camp_id)
    staff_users = (User.query
                   .filter(User.role.in_(['staff', 'admin']),
                           User.is_deleted == False,
                           User.is_active == True,
                           User.email_verified == True)
                   .order_by(User.last_name, User.first_name).all())

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        'Vorname', 'Nachname', 'E-Mail', 'Telefon',
        'Funktion', 'Ersthelfer', 'Gruppen', 'Rolle'
    ])
    for user in staff_users:
        profile = user.staff_profile
        assignments = [a for a in (user.group_assignments or [])
                       if a.age_group and a.age_group.camp_session_id == camp_id]
        groups_str = ', '.join(
            f"{a.age_group.name}{' (HT)' if a.is_head_coach else ''}"
            for a in assignments
        )
        writer.writerow([
            user.first_name, user.last_name, user.email,
            user.phone or '',
            profile.staff_class if profile else '',
            'Ja' if (profile and profile.is_first_aid) else 'Nein',
            groups_str,
            user.role,
        ])

    response = make_response(output.getvalue().encode('utf-8-sig'))
    response.headers['Content-Type'] = 'text/csv; charset=utf-8'
    response.headers['Content-Disposition'] = f'attachment; filename="team-liste-{camp_id}.csv"'
    return response


# =============================================================================
# [9] WAITLIST MANAGEMENT
# =============================================================================

@admin_bp.route('/camps/<int:camp_id>/warteliste')
@login_required
@admin_required
def waitlist(camp_id):
    '''
    Waitlist overview per age group.
    Admin promotes manually after external communication with families.
    '''
    camp = CampSession.query.get_or_404(camp_id)

    waitlisted = Registration.query.filter_by(
        camp_session_id=camp.id,
        status='waitlisted'
    ).order_by(
        Registration.age_group_id,
        Registration.waitlist_position
    ).all()

    # Group by age group
    by_group = {}
    for reg in waitlisted:
        group_name = reg.age_group.name if reg.age_group else 'Keine Gruppe'
        by_group.setdefault(group_name, []).append(reg)

    return render_template(
        'admin/waitlist.html',
        camp=camp,
        waitlisted_by_group=by_group,
        title=f'Warteliste — {camp.name}'
    )


@admin_bp.route('/anmeldungen/<int:registration_id>/bestaetigen',
                methods=['POST'])
@login_required
@admin_required
def confirm_from_waitlist(registration_id):
    '''Promote a waitlisted registration to confirmed.'''
    reg = Registration.query.get_or_404(registration_id)
    success, message = promote_from_waitlist(reg)

    if success:
        log_analytics_event('waitlist_promoted', success=True)
        flash(message, 'success')
    else:
        flash(message, 'danger')

    return redirect(url_for('admin.waitlist',
                            camp_id=reg.camp_session_id))


# =============================================================================
# [10] ANNOUNCEMENTS MANAGEMENT
# =============================================================================

class AnnouncementForm(FlaskForm):
    title               = StringField('Titel', validators=[
                            DataRequired(), Length(max=255)])
    body                = TextAreaField('Inhalt', validators=[DataRequired()])
    visibility          = SelectField('Sichtbarkeit', choices=[
                            ('public', 'Alle angemeldeten Nutzer (Standard)'),
                            ('open',   'Öffentlich — auch ohne Login sichtbar'),
                            ('staff',  'Nur Team (intern)'),
                          ])
    target_age_groups   = StringField(
                            'Zielgruppe (leer = alle)',
                            validators=[Optional(), Length(max=100)],
                            description='z.B. U8,U10 — leer lassen für alle Gruppen')
    staff_tags          = StringField(
                            'Interne Tags',
                            validators=[Optional(), Length(max=100)],
                            description='z.B. trainer,first_aid')
    is_pinned           = BooleanField('Oben anheften')
    send_email          = BooleanField('E-Mail-Benachrichtigung senden')


@admin_bp.route('/neuigkeiten')
@login_required
@admin_required
def announcement_list():
    page = request.args.get('seite', 1, type=int)
    announcements = paginate_query(
        Announcement.query.order_by(
            Announcement.is_pinned.desc(),
            Announcement.created_at.desc()
        ),
        page=page
    )
    return render_template('admin/announcement_list.html',
                           announcements=announcements,
                           title='Neuigkeiten verwalten')


@admin_bp.route('/neuigkeiten/neu', methods=['GET', 'POST'])
@login_required
@admin_required
def announcement_create():
    form = AnnouncementForm()
    camp = get_active_session()

    if form.validate_on_submit():
        announcement = Announcement(
            author_user_id=current_user.id,
            camp_session_id=camp.id if camp else None,
            title=form.title.data.strip(),
            body=form.body.data.strip(),
            visibility=form.visibility.data,
            target_age_groups=(form.target_age_groups.data or '').strip() or None,
            staff_tags=(form.staff_tags.data or '').strip() or None,
            is_pinned=form.is_pinned.data,
        )
        db.session.add(announcement)
        db.session.commit()

        # Send email notifications if requested
        if form.send_email.data:
            _send_announcement_emails(announcement)

        log_analytics_event('announcement_create', success=True,
                            detail=form.visibility.data)
        flash('Neuigkeit veröffentlicht.', 'success')
        return redirect(url_for('admin.announcement_list'))

    return render_template('admin/announcement_form.html', form=form,
                           action='create', title='Neuigkeit erstellen')


@admin_bp.route('/neuigkeiten/<int:announcement_id>/bearbeiten',
                methods=['GET', 'POST'])
@login_required
@admin_required
def announcement_edit(announcement_id):
    announcement = Announcement.query.get_or_404(announcement_id)
    form = AnnouncementForm(obj=announcement)

    if form.validate_on_submit():
        announcement.title              = form.title.data.strip()
        announcement.body               = form.body.data.strip()
        announcement.visibility         = form.visibility.data
        announcement.target_age_groups  = (form.target_age_groups.data or '').strip() or None
        announcement.staff_tags         = (form.staff_tags.data or '').strip() or None
        announcement.is_pinned          = form.is_pinned.data
        announcement.updated_at         = datetime.utcnow()
        db.session.commit()

        if form.send_email.data:
            _send_announcement_emails(announcement)

        flash('Neuigkeit aktualisiert.', 'success')
        return redirect(url_for('admin.announcement_list'))

    return render_template('admin/announcement_form.html', form=form,
                           announcement=announcement,
                           action='edit', title='Neuigkeit bearbeiten')


@admin_bp.route('/neuigkeiten/<int:announcement_id>/loeschen', methods=['POST'])
@login_required
@admin_required
def announcement_delete(announcement_id):
    announcement = Announcement.query.get_or_404(announcement_id)
    db.session.delete(announcement)
    db.session.commit()
    flash('Neuigkeit gelöscht.', 'info')
    return redirect(url_for('admin.announcement_list'))


@admin_bp.route('/camps/<int:camp_id>/erinnerung-senden', methods=['POST'])
@login_required
@admin_required
def send_reminder(camp_id):
    '''
    Send pre-camp reminder emails to all parents with confirmed registrations.
    Admin triggers this manually — not automatic.
    '''
    camp = CampSession.query.get_or_404(camp_id)

    # Get all parents with confirmed children in this session
    confirmed_regs = Registration.query.filter_by(
        camp_session_id=camp.id,
        status='confirmed'
    ).all()

    # Group by parent
    parent_children = {}
    for reg in confirmed_regs:
        parent = reg.child.parent
        if parent.id not in parent_children:
            parent_children[parent.id] = {'parent': parent, 'children': []}
        parent_children[parent.id]['children'].append(reg.child)

    sent = 0
    failed = 0
    for entry in parent_children.values():
        try:
            success = send_camp_reminder(
                entry['parent'], camp, entry['children']
            )
            if success:
                sent += 1
            else:
                failed += 1
        except Exception as e:
            current_app.logger.error(f'[Admin] Reminder email failed: {e}')
            failed += 1

    flash(
        f'Erinnerungsmail gesendet: {sent} erfolgreich, {failed} fehlgeschlagen.',
        'success' if failed == 0 else 'warning'
    )
    return redirect(url_for('admin.camp_edit', camp_id=camp_id))


@admin_bp.route('/neuigkeiten/empfaenger-anzahl')
@login_required
@admin_required
def announcement_recipient_count():
    '''AJAX: return how many users would receive an email for given visibility.'''
    visibility = request.args.get('visibility', 'all')
    if visibility == 'open':
        count = User.query.filter_by(is_deleted=False, is_active=True).count()
        return jsonify({'count': count, 'label': 'alle Nutzer (öffentlich)'})
    if visibility == 'staff':
        count = User.query.filter(
            User.role.in_(['staff', 'admin']),
            User.is_deleted == False,
            User.email_verified == True,
            User.is_active == True,
        ).count()
    else:
        parent_count = User.query.filter_by(
            role='parent', is_deleted=False,
            email_verified=True, email_announcements=True,
        ).count()
        staff_count = User.query.filter(
            User.role.in_(['staff', 'admin']),
            User.is_deleted == False,
            User.email_verified == True,
            User.is_active == True,
        ).count()
        count = parent_count + staff_count
    return jsonify({'count': count})


def _send_announcement_emails(announcement):
    '''Send email notifications to relevant opted-in users (parents and/or staff).'''
    from sub_modules.emails import send_announcement_email

    # Build recipient list based on visibility
    if announcement.visibility == 'open':
        recipients = User.query.filter_by(
            is_deleted=False, is_active=True, email_verified=True
        ).all()
    elif announcement.visibility == 'staff':
        # Staff-only: send to all active staff and admins
        recipients = User.query.filter(
            User.role.in_(['staff', 'admin']),
            User.is_deleted == False,
            User.email_verified == True,
            User.is_active == True,
        ).all()
    else:
        # Public ('all'): send to opted-in parents + all staff
        parents = User.query.filter_by(
            role='parent',
            is_deleted=False,
            email_verified=True,
            email_announcements=True,
        ).all()
        staff = User.query.filter(
            User.role.in_(['staff', 'admin']),
            User.is_deleted == False,
            User.email_verified == True,
            User.is_active == True,
        ).all()
        recipients = parents + staff

    sent = 0
    for user in recipients:
        if announcement.is_visible_to(user):
            try:
                send_announcement_email(user, announcement)
                sent += 1
            except Exception as e:
                current_app.logger.error(
                    f'[Admin] Announcement email failed for {user.email}: {e}'
                )

    announcement.email_sent_at = datetime.utcnow()
    db.session.commit()
    return sent


# =============================================================================
# [11] ANALYTICS DASHBOARD
# =============================================================================

@admin_bp.route('/analytics')
@login_required
@admin_required
def analytics():
    '''
    Internal analytics dashboard. Admin only — not linked in main nav.
    Shows usage patterns, error rates, device splits, check-in methods.
    All data is anonymous — no personal information displayed here.
    '''
    # Date range (default: last 30 days)
    days = request.args.get('tage', 30, type=int)
    days = min(max(days, 7), 365)
    since = datetime.utcnow() - timedelta(days=days)

    # Page view counts by route
    top_routes = db.session.query(
        AnalyticsEvent.route,
        func.count(AnalyticsEvent.id).label('count')
    ).filter(
        AnalyticsEvent.event_type == 'page_view',
        AnalyticsEvent.occurred_at >= since
    ).group_by(AnalyticsEvent.route
    ).order_by(func.count(AnalyticsEvent.id).desc()
    ).limit(20).all()

    # Hourly distribution (hour of day, all page views)
    hourly = db.session.query(
        func.extract('hour', AnalyticsEvent.occurred_at).label('hour'),
        func.count(AnalyticsEvent.id).label('count')
    ).filter(
        AnalyticsEvent.event_type == 'page_view',
        AnalyticsEvent.occurred_at >= since
    ).group_by(
        func.extract('hour', AnalyticsEvent.occurred_at)
    ).order_by('hour').all()

    # Device type split
    device_split = db.session.query(
        AnalyticsEvent.device_type,
        func.count(AnalyticsEvent.id).label('count')
    ).filter(
        AnalyticsEvent.occurred_at >= since
    ).group_by(AnalyticsEvent.device_type).all()

    # Role split
    role_split = db.session.query(
        AnalyticsEvent.user_role,
        func.count(AnalyticsEvent.id).label('count')
    ).filter(
        AnalyticsEvent.event_type == 'page_view',
        AnalyticsEvent.occurred_at >= since
    ).group_by(AnalyticsEvent.user_role).all()

    # Check-in method split (qr vs search)
    checkin_methods = db.session.query(
        AnalyticsEvent.detail,
        func.count(AnalyticsEvent.id).label('count')
    ).filter(
        AnalyticsEvent.event_type == 'checkin',
        AnalyticsEvent.occurred_at >= since
    ).group_by(AnalyticsEvent.detail).all()

    # Daily page views trend
    daily_views = db.session.query(
        func.date(AnalyticsEvent.occurred_at).label('day'),
        func.count(AnalyticsEvent.id).label('count')
    ).filter(
        AnalyticsEvent.event_type == 'page_view',
        AnalyticsEvent.occurred_at >= since
    ).group_by(
        func.date(AnalyticsEvent.occurred_at)
    ).order_by('day').all()

    # Average response times by route (top 10 slowest)
    slow_routes = db.session.query(
        AnalyticsEvent.route,
        func.avg(AnalyticsEvent.response_ms).label('avg_ms'),
        func.count(AnalyticsEvent.id).label('count')
    ).filter(
        AnalyticsEvent.event_type == 'page_view',
        AnalyticsEvent.occurred_at >= since,
        AnalyticsEvent.response_ms.isnot(None)
    ).group_by(AnalyticsEvent.route
    ).order_by(func.avg(AnalyticsEvent.response_ms).desc()
    ).limit(10).all()

    # Recent errors
    recent_errors = ErrorLog.query.order_by(
        ErrorLog.occurred_at.desc()
    ).limit(50).all()

    # Error counts by type
    error_counts = db.session.query(
        ErrorLog.error_type,
        func.count(ErrorLog.id).label('count')
    ).filter(
        ErrorLog.occurred_at >= since
    ).group_by(ErrorLog.error_type
    ).order_by(func.count(ErrorLog.id).desc()).all()

    # Login failure rate
    login_success = db.session.query(func.count(AnalyticsEvent.id)).filter(
        AnalyticsEvent.event_type == 'login_success',
        AnalyticsEvent.occurred_at >= since
    ).scalar() or 0

    login_fail = db.session.query(func.count(AnalyticsEvent.id)).filter(
        AnalyticsEvent.event_type == 'login_fail',
        AnalyticsEvent.occurred_at >= since
    ).scalar() or 0

    return render_template(
        'admin/analytics.html',
        days=days,
        top_routes=top_routes,
        hourly=hourly,
        device_split=device_split,
        role_split=role_split,
        checkin_methods=checkin_methods,
        daily_views=daily_views,
        slow_routes=slow_routes,
        recent_errors=recent_errors,
        error_counts=error_counts,
        login_success=login_success,
        login_fail=login_fail,
        title='Analytics'
    )


# =============================================================================
# [12] SETTINGS
# =============================================================================

def _dev_state_path():
    '''Path to the dev toggles state file in the instance folder.'''
    import os
    instance = current_app.instance_path
    os.makedirs(instance, exist_ok=True)
    return os.path.join(instance, 'dev_toggles.json')


def _load_dev_state():
    '''Load dev toggle state from file, return dict with defaults.'''
    import json, os
    defaults = {
        'DEV_OPEN_REGISTRATION': False,
        'MAIL_SUPPRESS_SEND': False,
        'SHOW_LANGUAGE_SWITCHER': False,
        'DEV_CAMP_TODAY': False,
        'DEV_CAMP_ORIG_START': None,
        'DEV_CAMP_ORIG_END': None,
        'DEV_CAMP_ORIG_STATUS': None,
    }
    try:
        with open(_dev_state_path()) as f:
            saved = json.load(f)
        defaults.update(saved)
    except (FileNotFoundError, Exception):
        pass
    return defaults


def _save_dev_state(state):
    '''Save dev toggle state to file.'''
    import json
    # Convert dates to ISO strings for JSON serialisation
    out = {}
    for k, v in state.items():
        if hasattr(v, 'isoformat'):
            out[k] = v.isoformat()
        else:
            out[k] = v
    with open(_dev_state_path(), 'w') as f:
        json.dump(out, f)


def _apply_dev_state(state):
    '''Apply saved dev state to current_app.config.'''
    current_app.config['DEV_OPEN_REGISTRATION'] = state.get('DEV_OPEN_REGISTRATION', False)
    current_app.config['MAIL_SUPPRESS_SEND']    = state.get('MAIL_SUPPRESS_SEND', False)
    current_app.config['SHOW_LANGUAGE_SWITCHER'] = state.get('SHOW_LANGUAGE_SWITCHER', False)
    current_app.config['DEV_CAMP_TODAY']         = state.get('DEV_CAMP_TODAY', False)
    current_app.config['TOAST_DURATION']         = state.get('TOAST_DURATION', 5)
    current_app.config['SHOW_TEMPLATE_NAME']     = state.get('SHOW_TEMPLATE_NAME', False)
    current_app.config['DISABLE_RATE_LIMIT']     = state.get('DISABLE_RATE_LIMIT', False)


@admin_bp.route('/dev-tools', methods=['GET', 'POST'])
@login_required
@admin_required
def dev_tools():
    '''
    Developer tools panel. Only meaningful in development mode.
    Toggles are persisted to instance/dev_toggles.json so they survive
    Flask reloads during development.
    '''
    from sub_modules.models import CampSession
    from datetime import date, timedelta
    import json

    is_dev = current_app.debug
    state  = _load_dev_state()
    _apply_dev_state(state)

    if request.method == 'POST':
        action = request.form.get('action')

        if action == 'toggle_open_registration':
            state['DEV_OPEN_REGISTRATION'] = not state.get('DEV_OPEN_REGISTRATION', False)
            flash(f'Offene Registrierung: {"an" if state["DEV_OPEN_REGISTRATION"] else "aus"}.', 'success')

        elif action == 'toggle_suppress_email':
            state['MAIL_SUPPRESS_SEND'] = not state.get('MAIL_SUPPRESS_SEND', False)
            flash(f'E-Mail-Versand: {"unterdrückt" if state["MAIL_SUPPRESS_SEND"] else "aktiv"}.', 'success')

        elif action == 'toggle_language_switcher':
            state['SHOW_LANGUAGE_SWITCHER'] = not state.get('SHOW_LANGUAGE_SWITCHER', False)
            flash(f'Sprachumschalter: {"sichtbar" if state["SHOW_LANGUAGE_SWITCHER"] else "versteckt"}.', 'success')

        elif action == 'toggle_camp_today':
            camp = CampSession.query.filter(
                CampSession.status.in_(['open', 'upcoming', 'active'])
            ).order_by(CampSession.start_date.desc()).first()
            if camp:
                if not state.get('DEV_CAMP_TODAY', False):
                    state['DEV_CAMP_ORIG_START']  = camp.start_date.isoformat()
                    state['DEV_CAMP_ORIG_END']    = camp.end_date.isoformat()
                    state['DEV_CAMP_ORIG_STATUS'] = camp.status
                    today = date.today()
                    duration = (camp.end_date - camp.start_date).days
                    camp.start_date = today
                    camp.end_date   = today + timedelta(days=duration)
                    camp.status     = 'active'
                    state['DEV_CAMP_TODAY'] = True
                    db.session.commit()
                    flash('Camp auf heute gesetzt. Check-In aktiv.', 'success')
                else:
                    from datetime import date as dt_date
                    orig_start  = state.get('DEV_CAMP_ORIG_START')
                    orig_end    = state.get('DEV_CAMP_ORIG_END')
                    orig_status = state.get('DEV_CAMP_ORIG_STATUS', 'open')
                    if orig_start:
                        camp.start_date = dt_date.fromisoformat(orig_start)
                        camp.end_date   = dt_date.fromisoformat(orig_end)
                        camp.status     = orig_status
                        db.session.commit()
                    state['DEV_CAMP_TODAY'] = False
                    flash('Camp-Datum wiederhergestellt.', 'success')
            else:
                flash('Kein aktives Camp gefunden.', 'warning')

        elif action == 'set_toast_duration':
            try:
                dur = int(request.form.get('toast_duration', 5))
                state['TOAST_DURATION'] = max(2, min(30, dur))
                current_app.config['TOAST_DURATION'] = state['TOAST_DURATION']
                flash(f'Toast-Anzeigedauer: {state["TOAST_DURATION"]}s.', 'success')
            except (ValueError, TypeError):
                pass
        elif action == 'toggle_show_template':
            state['SHOW_TEMPLATE_NAME'] = not state.get('SHOW_TEMPLATE_NAME', False)
            current_app.config['SHOW_TEMPLATE_NAME'] = state['SHOW_TEMPLATE_NAME']
            flash(f'Template-Name im HTML: {"sichtbar" if state["SHOW_TEMPLATE_NAME"] else "versteckt"}.', 'success')
        elif action == 'toggle_disable_rate_limit':
            state['DISABLE_RATE_LIMIT'] = not state.get('DISABLE_RATE_LIMIT', False)
            current_app.config['DISABLE_RATE_LIMIT'] = state['DISABLE_RATE_LIMIT']
            flash(f'Rate-Limit Feedback: {"deaktiviert" if state["DISABLE_RATE_LIMIT"] else "aktiv"}.', 'success')

        _save_dev_state(state)
        _apply_dev_state(state)
        return redirect(url_for('admin.dev_tools'))

    camp = CampSession.query.filter(
        CampSession.status.in_(['open', 'upcoming', 'active'])
    ).order_by(CampSession.start_date.desc()).first()

    return render_template(
        'admin/dev_tools.html',
        is_dev=is_dev,
        dev_open_registration=state.get('DEV_OPEN_REGISTRATION', False),
        dev_suppress_email=state.get('MAIL_SUPPRESS_SEND', False),
        show_language_switcher=state.get('SHOW_LANGUAGE_SWITCHER', False),
        dev_camp_today=state.get('DEV_CAMP_TODAY', False),
        toast_duration=state.get('TOAST_DURATION', 5),
        show_template_name=state.get('SHOW_TEMPLATE_NAME', False),
        disable_rate_limit=state.get('DISABLE_RATE_LIMIT', False),
        camp=camp,
        title='Dev Tools'
    )




# =============================================================================
# HISTORIC CSV IMPORT
# =============================================================================

@admin_bp.route('/import', methods=['GET', 'POST'])
@login_required
@admin_required
def csv_import():
    '''
    Import historic participant data from CSV.
    Creates historical parent/child records linked to an archived camp.
    Historical accounts use placeholder emails, are inactive, and have
    photo_consent=False. Data retention rules apply as normal.
    '''
    import csv, io, secrets
    from datetime import datetime as _dt
    from sub_modules.models import CampSession, User, Child, EmergencyContact, QRToken, Registration
    from sub_modules.helpers import get_or_create_qr_token
    from sub_modules.config import CURRENT_CONSENT_VERSION

    # Only archived/completed camps available as targets
    target_camps = CampSession.query.filter(
        CampSession.status.in_(['archived', 'completed', 'cancelled'])
    ).order_by(CampSession.start_date.desc()).all()

    preview_rows   = []
    errors         = []
    committed      = False
    row_count      = 0
    success_count  = 0

    if request.method == 'POST':
        action = request.form.get('action', 'preview')
        camp_id = request.form.get('camp_id', type=int)
        camp = CampSession.query.get(camp_id) if camp_id else None

        csv_text = request.form.get('csv_text', '').strip()

        if not csv_text:
            f = request.files.get('csv_file')
            if f and f.filename:
                csv_text = f.read().decode('utf-8-sig').strip()

        if not csv_text:
            flash('Bitte CSV-Datei hochladen oder Text einfügen.', 'warning')
        elif not camp:
            flash('Bitte ein Ziel-Camp auswählen.', 'warning')
        else:
            reader = csv.DictReader(io.StringIO(csv_text))
            rows = list(reader)
            row_count = len(rows)

            # Group by parent email so one parent → many children
            parents_map = {}  # email → {parent_data, children: []}
            for i, row in enumerate(rows, 1):
                def g(k): return (row.get(k) or '').strip()
                p_email = g('parent_email').lower()
                p_key   = p_email or f'row_{i}'
                if p_key not in parents_map:
                    parents_map[p_key] = {
                        'first_name': g('parent_first_name'),
                        'last_name':  g('parent_last_name'),
                        'email':      p_email,
                        'phone':      g('parent_phone'),
                        'children':   [],
                        'row':        i,
                    }
                child_dob_str = g('child_dob')
                child_dob = None
                for fmt in ('%d.%m.%Y', '%Y-%m-%d', '%d/%m/%Y'):
                    try:
                        child_dob = _dt.strptime(child_dob_str, fmt).date()
                        break
                    except ValueError:
                        pass

                parents_map[p_key]['children'].append({
                    'first_name':  g('child_first_name'),
                    'last_name':   g('child_last_name') or g('parent_last_name'),
                    'dob':         child_dob,
                    'dob_str':     child_dob_str,
                    'medical':     g('medical_notes'),
                    'ec_name':     g('emergency_contact_name'),
                    'ec_phone':    g('emergency_contact_phone'),
                    'row':         i,
                })

            # Validate
            for pkey, pdata in parents_map.items():
                if not pdata['first_name'] or not pdata['last_name']:
                    errors.append(f'Zeile {pdata["row"]}: Elternname fehlt.')
                for ch in pdata['children']:
                    if not ch['first_name']:
                        errors.append(f'Zeile {ch["row"]}: Kindname fehlt.')
                    if not ch['dob']:
                        errors.append(f'Zeile {ch["row"]}: Ungültiges Geburtsdatum "{ch["dob_str"]}".')

            preview_rows = list(parents_map.values())

            if action == 'commit' and not errors:
                for pdata in parents_map.values():
                    # Create or reuse historical user
                    placeholder = f'hist_{secrets.token_hex(8)}@import.local'
                    user = User(
                        first_name=pdata['first_name'],
                        last_name=pdata['last_name'],
                        email=placeholder,
                        phone=pdata['phone'] or None,
                        password_hash='!',  # unusable password
                        role='parent',
                        is_active=False,
                        email_verified=False,
                        gdpr_note=f'Historischer Import. Ursprüngliche E-Mail: {pdata["email"] or "unbekannt"}',
                    )
                    db.session.add(user)
                    db.session.flush()

                    for ch in pdata['children']:
                        if not ch['dob']:
                            continue
                        child = Child(
                            parent_user_id=user.id,
                            first_name=ch['first_name'],
                            last_name=ch['last_name'],
                            date_of_birth=ch['dob'],
                            medical_notes=ch['medical'] or None,
                            photo_consent_default=False,  # never assumed
                            independent_travel_default=False,
                        )
                        db.session.add(child)
                        db.session.flush()

                        # QR token
                        qr = QRToken(
                            child_id=child.id,
                            token=secrets.token_urlsafe(32)
                        )
                        db.session.add(qr)

                        # Emergency contact
                        if ch['ec_name'] or ch['ec_phone']:
                            ec = EmergencyContact(
                                child_id=child.id,
                                full_name=ch['ec_name'] or 'Unbekannt',
                                relationship='Unbekannt',
                                phone_primary=ch['ec_phone'] or '',
                            )
                            db.session.add(ec)

                        # Link to camp as historic registration
                        age_group = assign_age_group(child, camp)
                        reg = Registration(
                            child_id=child.id,
                            camp_session_id=camp.id,
                            age_group_id=age_group.id if age_group else None,
                            status='confirmed',
                            photo_consent=False,
                            independent_travel=False,
                            consent_version_at_registration=0,  # sentinel = imported
                            auto_assigned_group=age_group.name if age_group else None,
                        )
                        db.session.add(reg)
                        success_count += 1

                db.session.commit()
                committed = True
                flash(f'{success_count} Kind(er) aus {len(parents_map)} Familie(n) importiert.', 'success')
                log_analytics_event('csv_import', success=True, detail=f'camp={camp_id} rows={success_count}')

    return render_template(
        'admin/csv_import.html',
        target_camps=target_camps,
        preview_rows=preview_rows,
        errors=errors,
        committed=committed,
        row_count=row_count,
        success_count=success_count,
        title='Historischer CSV-Import',
    )


@admin_bp.route('/einstellungen', methods=['GET', 'POST'])
@login_required
@admin_required
def settings():
    '''
    Admin settings panel.
    Currently: analytics retention period.
    Future: GDPR retention period, email settings, etc.
    '''
    from sub_modules.models import AnalyticsEvent

    current_retention = current_app.config.get('ANALYTICS_RETENTION_DAYS', 365)

    if request.method == 'POST':
        try:
            new_retention = int(request.form.get('analytics_retention_days', 365))
            new_retention = max(0, min(new_retention, 3650))
            current_app.config['ANALYTICS_RETENTION_DAYS'] = new_retention
            flash(f'Analytics-Aufbewahrungsdauer auf {new_retention} Tage gesetzt.',
                  'success')
        except ValueError:
            flash('Ungültiger Wert.', 'danger')

        return redirect(url_for('admin.settings'))

    # Analytics storage size estimate
    event_count = AnalyticsEvent.query.count()
    error_count = ErrorLog.query.count()

    return render_template(
        'admin/settings.html',
        current_retention=current_retention,
        event_count=event_count,
        error_count=error_count,
        title='Einstellungen'
    )


# =============================================================================
# ADMIN SELF-EDIT (#32)
# =============================================================================

@admin_bp.route('/mein-konto')
@login_required
def self_edit():
    '''Redirects to the unified account page.'''
    return redirect(url_for('parents.account'))


# =============================================================================
# [13] BUG REPORT MANAGEMENT
# =============================================================================

class BugReportAdminForm(FlaskForm):
    status      = SelectField('Status', choices=[
                    ('new',          'Neu'),
                    ('acknowledged', 'Gesehen'),
                    ('in_progress',  'In Bearbeitung'),
                    ('resolved',     'Gelöst'),
                    ('wontfix',      'Kein Fix geplant'),
                  ])
    admin_notes = TextAreaField('Interne Notizen',
                    validators=[Optional(), Length(max=2000)])


@admin_bp.route('/feedback')
@login_required
@admin_required
def bug_report_list():
    '''
    All submitted bug reports. Filterable by status and severity.
    New/open reports shown first.
    '''
    from sub_modules.models import BugReport

    status_filter   = request.args.get('status', 'open')
    severity_filter = request.args.get('schweregrad', 'all')
    page            = request.args.get('seite', 1, type=int)

    query = BugReport.query

    if status_filter == 'open':
        query = query.filter(BugReport.status.in_(
            ['new', 'acknowledged', 'in_progress']
        ))
    elif status_filter != 'all':
        query = query.filter_by(status=status_filter)

    if severity_filter != 'all':
        query = query.filter_by(severity=severity_filter)

    reports = paginate_query(
        query.order_by(
            # High severity and new reports float to top
            BugReport.severity.desc(),
            BugReport.created_at.desc()
        ),
        page=page
    )

    # Counts for filter badges
    from sqlalchemy import case
    counts = db.session.query(
        func.count(BugReport.id).label('total'),
        func.sum(case((BugReport.status == 'new', 1), else_=0)).label('new'),
        func.sum(case((BugReport.severity == 'high', 1), else_=0)).label('high'),
    ).first()

    return render_template(
        'admin/bug_report_list.html',
        reports=reports,
        status_filter=status_filter,
        severity_filter=severity_filter,
        counts=counts,
        title='Fehlermeldungen'
    )


@admin_bp.route('/feedback/<int:report_id>', methods=['GET', 'POST'])
@login_required
@admin_required
def bug_report_detail(report_id):
    '''
    View and update a single bug report.
    Admin can change status and add internal notes.
    Notes are sanitized before storage.
    '''
    from sub_modules.models import BugReport
    from sub_modules.helpers import sanitize_text

    report = BugReport.query.get_or_404(report_id)
    form   = BugReportAdminForm(obj=report)

    if form.validate_on_submit():
        old_status  = report.status
        new_status  = form.status.data

        report.status       = new_status
        report.admin_notes  = sanitize_text(
            form.admin_notes.data, max_length=2000
        ) if form.admin_notes.data else None
        report.updated_at   = datetime.utcnow()

        if new_status == 'resolved' and old_status != 'resolved':
            report.resolved_by_user_id  = current_user.id
            report.resolved_at          = datetime.utcnow()
        elif new_status != 'resolved':
            report.resolved_at = None
            report.resolved_by_user_id = None

        db.session.commit()

        flash(f'Meldung #{report.id} aktualisiert.', 'success')
        return redirect(url_for('admin.bug_report_detail', report_id=report.id))

    return render_template(
        'admin/bug_report_detail.html',
        report=report,
        form=form,
        title=f'Meldung #{report.id}'
    )
