'''
views/staff.py
==============
Staff-facing blueprint. All routes require login + role in ('staff', 'admin').

Routes:
    GET       /team/                            Staff dashboard
    GET       /team/roster                      Full camp roster by age group
    GET       /team/gruppe/<age_group_id>        Single group roster + head coach info
    GET/POST  /team/checkin                      Check-in mode (search + QR)
    GET       /team/checkin/qr/<token>           QR scan endpoint (redirects into check-in flow)
    POST      /team/checkin/<reg_id>/ein        Process child check-in
    POST      /team/checkin/<reg_id>/aus        Process child check-out
    POST      /team/checkin/<log_id>/stornieren Void an accidental check-in/out
    GET       /team/noch-da                      End-of-day: children still checked in
    POST      /team/selbst-einchecken            Staff self check-in
    GET       /team/kind/<child_id>              Child detail view (medical, emergency, travel)

Security:
    - All routes: @login_required + @staff_required
    - Check-in/out actions: validate registration belongs to active session
    - Void actions: staff can void own-session events; admin can void any
    - Child detail view: read-only for staff (no edit access — that is parents only)
    - QR token lookup never exposes the token in responses
'''

from datetime import date, datetime
from functools import wraps

from flask import (Blueprint, render_template, redirect, url_for,
                   flash, request, current_app, abort, jsonify)
from flask_login import login_required, current_user
from flask_wtf import FlaskForm
from wtforms import TextAreaField, HiddenField
from wtforms.validators import Optional, Length

from sub_modules.extensions import db, log_analytics_event
from sub_modules.models import (CampSession, AgeGroup, Registration,
                                Child, CheckinLog, User, AbsenceReport)
from sub_modules.helpers import (process_checkin, process_checkout,
                                 void_checkin_event, process_staff_checkin,
                                 get_still_present_registrations,
                                 resolve_checkin_by_qr_token,
                                 get_camp_day_number,
                                 get_head_coach_conflicts,
                                 head_coach_warnings_enabled)


staff_bp = Blueprint('staff', __name__)


# =============================================================================
# ACCESS CONTROL
# =============================================================================

def staff_required(f):
    '''Decorator: staff and admin only. Parents cannot access these routes.'''
    @wraps(f)
    def decorated(*args, **kwargs):
        if current_user.role not in ('staff', 'admin'):
            abort(403)
        return f(*args, **kwargs)
    return decorated


def get_active_session_or_404():
    '''
    Return the currently active or open camp session.
    Aborts 404 if none found — most staff routes require an active session.
    '''
    session = CampSession.query.filter(
        CampSession.status.in_(['active', 'open'])
    ).order_by(CampSession.start_date.desc()).first()
    if not session:
        abort(404)
    return session


def get_head_coach_warning(camp_session):
    '''
    Return list of groups missing a head coach if warnings are enabled.
    Passed to every staff template for the persistent orange banner.
    '''
    if not head_coach_warnings_enabled(camp_session):
        return []
    return get_head_coach_conflicts(camp_session)


# =============================================================================
# FORMS
# =============================================================================

class VoidForm(FlaskForm):
    reason = TextAreaField('Grund (optional)',
                           validators=[Optional(), Length(max=500)])
    log_id = HiddenField()


class CheckinNoteForm(FlaskForm):
    notes = TextAreaField('Notiz (optional)',
                          validators=[Optional(), Length(max=500)])


# =============================================================================
# CONTEXT HELPER
# =============================================================================

def _staff_context(camp_session):
    '''
    Build shared template context passed to every staff page.
    Includes head coach warning and today's camp day number.
    '''
    today = date.today()
    camp_day = get_camp_day_number(camp_session, today)
    head_coach_warnings = get_head_coach_warning(camp_session)
    return {
        'camp_session': camp_session,
        'camp_day': camp_day,
        'today': today,
        'head_coach_warnings': head_coach_warnings,
    }


# =============================================================================
# DASHBOARD
# =============================================================================

@staff_bp.route('/')
@login_required
@staff_required
def dashboard():
    '''
    Staff home. Shows all active/open camps with per-camp stats and CHECK-IN button.
    Each camp card shows its own stats and a CHECK-IN button that is only
    active when today falls within the camp date range.
    '''
    today = date.today()

    camp_sessions = CampSession.query.filter(
        CampSession.status.in_(['active', 'open', 'upcoming'])
    ).order_by(CampSession.start_date.asc()).all()

    if not camp_sessions:
        return render_template(
            'staff/dashboard.html',
            camp_session=None,
            camp_cards=[],
            title='Team-Dashboard'
        )

    # Build per-camp stats and context
    camp_cards = []
    for camp in camp_sessions:
        camp_day = get_camp_day_number(camp, today)
        is_today = camp_day is not None

        stats = {}
        own_checked_in = False
        if is_today:
            total_confirmed = Registration.query.filter_by(
                camp_session_id=camp.id,
                status='confirmed'
            ).count()

            checked_in_today = CheckinLog.query.filter_by(
                camp_session_id=camp.id,
                event_type='checkin',
                camp_day=camp_day,
                is_duplicate=False,
                voided=False
            ).filter(CheckinLog.registration_id.isnot(None)).count()

            still_present = get_still_present_registrations(camp, camp_day)

            stats = {
                'total_confirmed':  total_confirmed,
                'checked_in_today': checked_in_today,
                'still_present':    len(still_present),
                'checked_out':      checked_in_today - len(still_present),
            }

            own_checkin = CheckinLog.query.filter_by(
                staff_user_id=current_user.id,
                camp_session_id=camp.id,
                event_type='checkin',
                camp_day=camp_day,
                registration_id=None,
                voided=False
            ).first()
            own_checked_in = own_checkin is not None

        head_coach_warnings = get_head_coach_warning(camp)

        camp_cards.append({
            'camp':                camp,
            'camp_day':            camp_day,
            'is_today':            is_today,
            'stats':               stats,
            'own_checked_in':      own_checked_in,
            'head_coach_warnings': head_coach_warnings,
        })

    # Keep camp_session for backward compat with base template context
    camp_session = camp_sessions[0] if camp_sessions else None

    return render_template(
        'staff/dashboard.html',
        camp_session=camp_session,
        camp_cards=camp_cards,
        today=today,
        title='Team-Dashboard'
    )


# =============================================================================
# ROSTER
# =============================================================================

@staff_bp.route('/roster')
@login_required
@staff_required
def roster():
    '''
    Full camp roster grouped by age group.
    Shows registration status, check-in status for today,
    medical note indicator, and independent travel flag.
    '''
    camp_session = get_active_session_or_404()
    ctx = _staff_context(camp_session)
    camp_day = ctx['camp_day']

    # Build roster grouped by age group
    groups = []
    for age_group in camp_session.age_groups:
        registrations = Registration.query.filter_by(
            age_group_id=age_group.id,
            status='confirmed'
        ).join(Child).order_by(Child.last_name, Child.first_name).all()

        # Annotate each registration with today's check-in status
        annotated = []
        for reg in registrations:
            checkin = reg.checkin_for_day(camp_day) if camp_day else None
            checkout = reg.checkout_for_day(camp_day) if camp_day else None
            annotated.append({
                'registration':     reg,
                'child':            reg.child,
                'checked_in':       checkin is not None,
                'checked_out':      checkout is not None,
                'still_present':    checkin is not None and checkout is None,
                'checkin_method':   checkin.method if checkin else None,
            })

        groups.append({
            'age_group':    age_group,
            'head_coach':   age_group.head_coach,
            'registrations': annotated,
            'confirmed_count': len(registrations),
        })

    # Build the structures the template expects:
    #   age_groups  — list of AgeGroup objects for the filter tabs
    #   roster_by_group — OrderedDict {group.name: [annotated_regs]}
    from collections import OrderedDict
    age_groups = [g['age_group'] for g in groups]
    roster_by_group = OrderedDict(
        (g['age_group'].name, g['registrations']) for g in groups
    )

    # Apply group filter if ?gruppe=<id> is in the query string
    group_filter = request.args.get('gruppe')
    if group_filter:
        roster_by_group = OrderedDict(
            (name, items) for name, items in roster_by_group.items()
            if any(str(item['registration'].age_group_id) == group_filter
                   for item in items)
        )

    return render_template(
        'staff/roster.html',
        **ctx,
        age_groups=age_groups,
        roster_by_group=roster_by_group,
        group_filter=group_filter,
        title='Anwesenheitsliste'
    )


@staff_bp.route('/gruppe/<int:age_group_id>')
@login_required
@staff_required
def group_detail(age_group_id):
    '''
    Single age group view. Shows full child details visible to staff:
    - Name, DOB, age group
    - Medical notes (highlighted if present)
    - Emergency contacts
    - Independent travel flag
    - Today's check-in status
    '''
    age_group = AgeGroup.query.get_or_404(age_group_id)
    camp_session = age_group.camp_session
    ctx = _staff_context(camp_session)
    camp_day = ctx['camp_day']

    registrations = Registration.query.filter_by(
        age_group_id=age_group.id,
        status='confirmed'
    ).join(Child).order_by(Child.last_name).all()

    annotated = []
    for reg in registrations:
        checkin  = reg.checkin_for_day(camp_day) if camp_day else None
        checkout = reg.checkout_for_day(camp_day) if camp_day else None
        absence  = reg.absence_for_day(camp_day) if camp_day else None
        annotated.append({
            'registration':      reg,
            'child':             reg.child,
            'emergency_contacts': reg.child.emergency_contacts,
            'checked_in':        checkin is not None,
            'checked_out':       checkout is not None,
            'still_present':     checkin is not None and checkout is None,
            'absence_reported':  absence is not None and absence.is_active,
            'absence':           absence,
        })

    return render_template(
        'staff/group_detail.html',
        **ctx,
        age_group=age_group,
        registrations=annotated,
        head_coach=age_group.head_coach,
        title=f'Gruppe {age_group.name}'
    )


# =============================================================================
# CHECK-IN MODE
# Mobile-first. Two entry methods: name search and QR scan.
# =============================================================================

@staff_bp.route('/camp/<int:camp_id>/checkin')
@login_required
@staff_required
def checkin_mode_camp(camp_id):
    '''Camp-scoped check-in mode — all context tied to specific camp.'''
    camp_session = CampSession.query.get_or_404(camp_id)
    return _checkin_mode_render(camp_session)


@staff_bp.route('/checkin')
@login_required
@staff_required
def checkin_mode():
    '''Legacy route — redirects to most active camp's check-in page.'''
    camp_session = CampSession.query.filter(
        CampSession.status.in_(['active', 'open'])
    ).order_by(CampSession.start_date.desc()).first()
    if camp_session:
        return redirect(url_for('staff.checkin_mode_camp', camp_id=camp_session.id))
    return _checkin_mode_render(None)


def _checkin_mode_render(camp_session):
    '''
    Check-in mode landing page — scoped to a specific camp session.
    '''
    if not camp_session:
        flash('Kein aktives Camp gefunden.', 'warning')
        return redirect(url_for('staff.dashboard'))

    ctx = _staff_context(camp_session)
    camp_day = ctx['camp_day']

    if not camp_day:
        flash('Heute ist kein offizieller Camptag für dieses Camp.', 'warning')
        return redirect(url_for('staff.dashboard'))

    # All confirmed registrations with today's status
    registrations = Registration.query.filter_by(
        camp_session_id=camp_session.id,
        status='confirmed'
    ).join(Child).order_by(Child.last_name, Child.first_name).all()

    roster = []
    for reg in registrations:
        checkin  = reg.checkin_for_day(camp_day)
        checkout = reg.checkout_for_day(camp_day)
        roster.append({
            'registration':    reg,
            'child':           reg.child,
            'checked_in':      checkin is not None,
            'checked_out':     checkout is not None,
            'still_present':   checkin is not None and checkout is None,
            'checkin_log_id':  checkin.id if checkin else None,
            'checkout_log_id': checkout.id if checkout else None,
            'checkin_method':  checkin.method if checkin else None,
        })

    # Staff attendance for today
    all_staff = User.query.filter(
        User.role.in_(['staff', 'admin']),
        User.is_deleted == False,
        User.is_active == True,
    ).order_by(User.last_name, User.first_name).all()

    staff_roster = []
    for user in all_staff:
        checkin = CheckinLog.query.filter_by(
            staff_user_id=user.id,
            camp_session_id=camp_session.id,
            event_type='checkin',
            camp_day=camp_day,
            registration_id=None,
            voided=False
        ).first()
        checkout = CheckinLog.query.filter_by(
            staff_user_id=user.id,
            camp_session_id=camp_session.id,
            event_type='checkout',
            camp_day=camp_day,
            registration_id=None,
            voided=False
        ).first()
        staff_roster.append({
            'user':          user,
            'checked_in':    checkin is not None,
            'checked_out':   checkout is not None,
            'still_present': checkin is not None and checkout is None,
            'checkin_log_id':  checkin.id if checkin else None,
            'checkout_log_id': checkout.id if checkout else None,
            'is_me':         user.id == current_user.id,
        })

    log_analytics_event('page_view', detail='checkin_mode')

    return render_template(
        'staff/checkin_mode.html',
        **ctx,
        roster=roster,
        staff_roster=staff_roster,
        title='Check-In'
    )


@staff_bp.route('/checkin/suche')
@login_required
@staff_required
def checkin_search():
    '''
    AJAX endpoint: search children by name for the check-in search box.
    Returns JSON — called live as staff types.
    Never returns QR tokens.
    '''
    camp_session = CampSession.query.filter(
        CampSession.status.in_(['active', 'open'])
    ).first()

    if not camp_session:
        return jsonify([])

    query = request.args.get('q', '').strip()
    if len(query) < 2:
        return jsonify([])

    camp_day = get_camp_day_number(camp_session, date.today())

    from sqlalchemy import or_, func
    registrations = Registration.query.filter_by(
        camp_session_id=camp_session.id,
        status='confirmed'
    ).join(Child).filter(
        or_(
            func.lower(Child.first_name).contains(query.lower()),
            func.lower(Child.last_name).contains(query.lower()),
            func.lower(
                Child.first_name + ' ' + Child.last_name
            ).contains(query.lower())
        )
    ).limit(20).all()

    results = []
    for reg in registrations:
        checkin  = reg.checkin_for_day(camp_day) if camp_day else None
        checkout = reg.checkout_for_day(camp_day) if camp_day else None
        results.append({
            'registration_id':  reg.id,
            'child_name':       reg.child.full_name,
            'age_group':        reg.age_group.name if reg.age_group else '',
            'checked_in':       checkin is not None,
            'checked_out':      checkout is not None,
            'still_present':    checkin is not None and checkout is None,
            'has_medical_notes': reg.child.has_medical_notes,
            'independent_travel': reg.independent_travel,
            'checkin_log_id':   checkin.id if checkin else None,
            'absence_reported': reg.absence_for_day(camp_day).is_active
                                if camp_day and reg.absence_for_day(camp_day) else False,
        })

    return jsonify(results)


@staff_bp.route('/checkin/qr/<token>')
@login_required
@staff_required
def checkin_by_qr(token):
    '''
    QR scan endpoint.
    Finds the child's registration across all active camps —
    so the correct camp is always used even with multiple simultaneous camps.
    Redirects to checkout if already checked in today, otherwise check-in.
    '''
    from sub_modules.models import QRToken

    # Look up the QR token to find the child
    qr = QRToken.query.filter_by(token=token).first()
    if not qr:
        flash('Ungültiger QR-Code.', 'danger')
        log_analytics_event('checkin', success=False, detail='qr_invalid')
        return redirect(url_for('staff.checkin_mode'))

    # Find the child's confirmed (or waitlisted) registration in ANY active camp
    registration = Registration.query.filter(
        Registration.child_id == qr.child_id,
        Registration.status.in_(['confirmed', 'waitlisted']),
    ).join(CampSession).filter(
        CampSession.status.in_(['active', 'open'])
    ).first()

    if not registration:
        flash('Kind ist für kein aktives Camp angemeldet.', 'warning')
        log_analytics_event('checkin', success=False, detail='qr_no_registration')
        return redirect(url_for('staff.checkin_mode'))

    camp_session = registration.camp_session
    camp_day = get_camp_day_number(camp_session, date.today())
    checkin  = registration.checkin_for_day(camp_day) if camp_day else None

    # Already checked in today → go to checkout page
    if checkin and not registration.checkout_for_day(camp_day):
        return redirect(url_for(
            'staff.confirm_checkout',
            registration_id=registration.id,
            method='qr'
        ))
    # Not yet checked in (or already checked out) → go to check-in page
    return redirect(url_for(
        'staff.confirm_checkin',
        registration_id=registration.id,
        method='qr'
    ))


@staff_bp.route('/kind-info-scanner')
@login_required
@staff_required
def child_info_scan():
    '''Dedicated page for scanning a child info QR code — no check-in.'''
    return render_template('staff/child_info_scan.html', title='Kind-Info scannen')


@staff_bp.route('/kind-info/<token>')
@login_required
@staff_required
def child_info_by_qr(token):
    '''
    QR scan → child info view (read-only).
    Shows parent contact, medical notes, pickup/travel info.
    Does NOT trigger a check-in.
    '''
    from sub_modules.models import QRToken
    qr = QRToken.query.filter_by(token=token).first()
    if not qr:
        flash('Ungültiger QR-Code.', 'danger')
        return redirect(url_for('staff.checkin_mode'))

    child = qr.child
    if not child or child.is_deleted:
        flash('Kind nicht gefunden.', 'danger')
        return redirect(url_for('staff.checkin_mode'))

    camp_session = CampSession.query.filter(
        CampSession.status.in_(['active', 'open'])
    ).first()

    registration = None
    if camp_session:
        registration = Registration.query.filter_by(
            child_id=child.id,
            camp_session_id=camp_session.id
        ).first()

    return render_template(
        'staff/child_info.html',
        child=child,
        registration=registration,
        title=f'Info: {child.full_name}'
    )


@staff_bp.route('/checkin/<int:registration_id>/bestaetigen')
@login_required
@staff_required
def confirm_checkin(registration_id):
    '''
    Confirmation screen before recording a check-in.
    Shows child name, age group, medical note indicator.
    Staff taps confirm to record the check-in.
    '''
    registration = Registration.query.get_or_404(registration_id)
    method = request.args.get('method', 'search')

    camp_session = registration.camp_session
    camp_day = get_camp_day_number(camp_session, date.today())

    if not camp_day:
        flash('Heute ist kein Camptag.', 'warning')
        return redirect(url_for('staff.checkin_mode'))

    existing = registration.checkin_for_day(camp_day)
    if existing:
        flash(f'{registration.child.full_name} ist bereits eingecheckt.', 'info')
        return redirect(url_for('staff.checkin_mode'))

    return render_template(
        'staff/confirm_checkin.html',
        registration=registration,
        method=method,
        title=f'Einchecken: {registration.child.full_name}'
    )


@staff_bp.route('/checkin/<int:registration_id>/ein', methods=['POST'])
@login_required
@staff_required
def do_checkin(registration_id):
    '''Process child check-in.'''
    registration = Registration.query.get_or_404(registration_id)
    method = request.form.get('method', 'search')

    success, message, log = process_checkin(registration, current_user, method)

    log_analytics_event(
        'checkin',
        success=success,
        detail=method
    )

    if success:
        flash(message, 'success' if not (log and log.is_duplicate) else 'warning')
    else:
        flash(message, 'danger')

    return redirect(url_for('staff.checkin_mode'))


@staff_bp.route('/checkout/<int:registration_id>/bestaetigen')
@login_required
@staff_required
def confirm_checkout(registration_id):
    '''
    Confirmation screen before recording a check-out.

    IMPORTANT: Independent travel status is displayed prominently here
    so staff knows before confirming whether the child can leave alone.

    Green banner  = child may go home independently
    Red banner    = child must be collected by a parent/guardian
    '''
    registration = Registration.query.get_or_404(registration_id)
    method = request.args.get('method', 'search')

    camp_session = registration.camp_session
    camp_day = get_camp_day_number(camp_session, date.today())

    if not camp_day:
        flash('Heute ist kein Camptag.', 'warning')
        return redirect(url_for('staff.checkin_mode'))

    # Must be checked in first
    checkin = registration.checkin_for_day(camp_day)
    if not checkin:
        flash(f'{registration.child.full_name} ist nicht eingecheckt.', 'warning')
        return redirect(url_for('staff.checkin_mode'))

    # Already checked out
    if registration.checkout_for_day(camp_day):
        flash(f'{registration.child.full_name} ist bereits ausgecheckt.', 'info')
        return redirect(url_for('staff.checkin_mode'))

    return render_template(
        'staff/confirm_checkout.html',
        registration=registration,
        method=method,
        # Independent travel prominently surfaced — drives the visual in template
        independent_travel=registration.independent_travel,
        title=f'Auschecken: {registration.child.full_name}'
    )


@staff_bp.route('/checkout/<int:registration_id>/aus', methods=['POST'])
@login_required
@staff_required
def do_checkout(registration_id):
    '''Process child check-out.'''
    registration = Registration.query.get_or_404(registration_id)
    method = request.form.get('method', 'search')

    success, message, log, independent_travel = process_checkout(
        registration, current_user, method
    )

    log_analytics_event('checkout', success=success, detail=method)

    if success:
        flash(message, 'success')
    else:
        flash(message, 'danger')

    return redirect(url_for('staff.checkin_mode'))


@staff_bp.route('/checkin/stornieren/<int:log_id>', methods=['POST'])
@login_required
@staff_required
def void_event(log_id):
    '''
    Void an accidental check-in or check-out event.
    Kept in the database for audit — marked voided=True.
    Staff can void events from the current session.
    Admin can void any event.
    '''
    log = CheckinLog.query.get_or_404(log_id)
    reason = request.form.get('reason', '').strip()

    # Staff can only void events from today's session
    if current_user.role != 'admin':
        if log.event_date != date.today():
            flash('Nur Events vom heutigen Tag können storniert werden.', 'danger')
            return redirect(url_for('staff.checkin_mode'))

    success, message = void_checkin_event(log, current_user, reason)
    log_analytics_event('checkin_void', success=success)

    flash(message, 'success' if success else 'danger')
    return redirect(url_for('staff.checkin_mode'))


# =============================================================================
# END-OF-DAY: STILL CHECKED IN
# =============================================================================

@staff_bp.route('/noch-da')
@login_required
@staff_required
def still_present():
    '''
    End-of-day view: children who are checked in but not yet checked out.
    Sorted by age group then last name.
    Shows emergency contacts and independent travel status per child.
    Refreshes automatically every 60 seconds.
    '''
    camp_session = get_active_session_or_404()
    ctx = _staff_context(camp_session)
    camp_day = ctx['camp_day']

    if not camp_day:
        flash('Heute ist kein Camptag.', 'warning')
        return redirect(url_for('staff.dashboard'))

    still_in = get_still_present_registrations(camp_session, camp_day)

    # Annotate with emergency contacts for quick reference
    annotated = [
        {
            'registration':      reg,
            'child':             reg.child,
            'emergency_contacts': reg.child.emergency_contacts,
            'independent_travel': reg.independent_travel,
            'age_group':         reg.age_group,
        }
        for reg in still_in
    ]

    return render_template(
        'staff/still_present.html',
        **ctx,
        still_present=annotated,
        auto_refresh_seconds=60,
        title='Noch anwesend'
    )


# =============================================================================
# STAFF SELF CHECK-IN
# =============================================================================

@staff_bp.route('/selbst-einchecken', methods=['POST'])
@login_required
@staff_required
def self_checkin():
    '''
    Staff voluntary self check-in.
    Called from a button on the staff dashboard.
    Auto-checkout will fire at STAFF_AUTO_CHECKOUT_TIME.
    '''
    camp_session = CampSession.query.filter(
        CampSession.status.in_(['active', 'open'])
    ).first()

    if not camp_session:
        flash('Kein aktives Camp.', 'warning')
        return redirect(url_for('staff.dashboard'))

    method = request.form.get('method', 'search')
    success, message = process_staff_checkin(current_user, camp_session, method)

    log_analytics_event('staff_checkin', success=success)
    flash(message, 'success' if success else 'warning')
    return redirect(url_for('staff.dashboard'))


@staff_bp.route('/<int:user_id>/einchecken', methods=['POST'])
@login_required
@staff_required
def staff_checkin_other(user_id):
    '''Check in another staff member.'''
    user = User.query.get_or_404(user_id)
    camp_session = CampSession.query.filter(
        CampSession.status.in_(['active', 'open'])
    ).first()
    if not camp_session:
        flash('Kein aktives Camp.', 'warning')
        return redirect(request.referrer or url_for('staff.checkin_mode'))
    success, message = process_staff_checkin(user, camp_session, method='manual')
    flash(f'{user.full_name}: {message}', 'success' if success else 'warning')
    return redirect(request.referrer or url_for('staff.checkin_mode'))


@staff_bp.route('/<int:user_id>/auschecken', methods=['POST'])
@login_required
@staff_required
def staff_checkout_other(user_id):
    '''Check out another staff member.'''
    user = User.query.get_or_404(user_id)
    camp_session = CampSession.query.filter(
        CampSession.status.in_(['active', 'open'])
    ).first()
    if not camp_session:
        flash('Kein aktives Camp.', 'warning')
        return redirect(request.referrer or url_for('staff.checkin_mode'))

    today = date.today()
    camp_day = get_camp_day_number(camp_session, today)
    if not camp_day:
        flash('Kein Camptag heute.', 'warning')
        return redirect(request.referrer or url_for('staff.checkin_mode'))

    existing_checkin = CheckinLog.query.filter_by(
        staff_user_id=user.id,
        camp_session_id=camp_session.id,
        event_type='checkin',
        camp_day=camp_day,
        voided=False,
        registration_id=None,
    ).first()

    if not existing_checkin:
        flash(f'{user.full_name} ist noch nicht eingecheckt.', 'warning')
        return redirect(request.referrer or url_for('staff.checkin_mode'))

    log = CheckinLog(
        registration_id=None,
        staff_user_id=user.id,
        camp_session_id=camp_session.id,
        event_type='checkout',
        camp_day=camp_day,
        event_date=today,
        event_time=datetime.utcnow(),
        method='manual',
        is_duplicate=False,
    )
    db.session.add(log)
    db.session.commit()
    flash(f'{user.full_name} ausgecheckt.', 'success')
    return redirect(request.referrer or url_for('staff.checkin_mode'))


# =============================================================================
# INDEPENDENT TRAVEL APPROVAL
# =============================================================================

@staff_bp.route('/kind/<int:registration_id>/heimweg', methods=['POST'])
@login_required
@staff_required
def approve_independent_travel(registration_id):
    '''
    Approve independent travel for a child mid-camp.
    Called when a parent gives verbal permission and staff needs to update.
    Records which staff member approved and when for accountability.
    '''
    registration = Registration.query.get_or_404(registration_id)

    if registration.independent_travel:
        flash('Heimweg alleine ist bereits genehmigt.', 'info')
        return redirect(url_for(
            'staff.group_detail',
            age_group_id=registration.age_group_id
        ))

    registration.independent_travel = True
    registration.independent_travel_approved_by = current_user.id
    registration.independent_travel_approved_at = datetime.utcnow()
    registration.updated_at = datetime.utcnow()
    db.session.commit()

    log_analytics_event('travel_approval', success=True)
    flash(
        f'{registration.child.full_name} darf jetzt selbstständig '
        f'nach Hause gehen.',
        'success'
    )
    return redirect(url_for(
        'staff.group_detail',
        age_group_id=registration.age_group_id
    ))


# =============================================================================
# PRINTABLE GROUP ROSTER
# =============================================================================

@staff_bp.route('/gruppe/<int:age_group_id>/drucken')
@login_required
@staff_required
def print_group_roster(age_group_id):
    '''
    Printable single-group roster.
    Accessible to all staff (not just the assigned head coach).

    Columns: name, emergency contact name + phone, independent travel.
    Sorted by child last name.
    '''
    age_group = AgeGroup.query.get_or_404(age_group_id)
    camp_session = age_group.camp_session

    registrations = (
        Registration.query
        .filter_by(age_group_id=age_group.id, status='confirmed')
        .join(Child)
        .order_by(Child.last_name, Child.first_name)
        .all()
    )

    return render_template(
        'staff/print_roster.html',
        age_group=age_group,
        camp_session=camp_session,
        registrations=registrations,
        title=f'Teilnehmerliste {age_group.name}',
    )


@staff_bp.route('/kind/<int:child_id>')
@login_required
@staff_required
def child_detail(child_id):
    '''
    Full child detail view for staff.
    Read-only — staff cannot edit child data, only view it.
    Shows: name, DOB, age group, medical notes, emergency contacts,
           independent travel status, check-in history for this camp.
    '''
    child = Child.query.filter_by(
        id=child_id,
        is_deleted=False
    ).first_or_404()

    camp_session = CampSession.query.filter(
        CampSession.status.in_(['active', 'open'])
    ).first()

    registration = None
    checkin_history = []

    if camp_session:
        registration = Registration.query.filter_by(
            child_id=child.id,
            camp_session_id=camp_session.id
        ).first()

        if registration:
            checkin_history = CheckinLog.query.filter_by(
                registration_id=registration.id,
                voided=False
            ).order_by(CheckinLog.event_time).all()

    ctx = _staff_context(camp_session) if camp_session else {}

    return render_template(
        'staff/child_detail.html',
        **ctx,
        child=child,
        registration=registration,
        checkin_history=checkin_history,
        title=child.full_name
    )
