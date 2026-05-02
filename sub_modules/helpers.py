'''
sub_modules/helpers.py
======================
All business logic as plain, testable functions.
No Flask request context required unless explicitly noted.

Sections:
    1  - Age & group utilities
    2  - Group splitting algorithm (admin group planner)
    3  - Head coach management
    4  - Check-in / check-out processing
    5  - Staff auto-checkout (called by APScheduler)
    6  - QR token generation
    7  - Waitlist utilities
    8  - GDPR retention utilities
    9  - Security / redirect utilities
    10 - General utilities
'''

import secrets
import statistics
from datetime import datetime, date, timedelta
from itertools import combinations
from typing import Optional

from flask import request, redirect, url_for, current_app
from flask_login import current_user
from urllib.parse import urlparse, urljoin
from werkzeug.security import generate_password_hash, check_password_hash


# =============================================================================
# [1] AGE & GROUP UTILITIES
# =============================================================================

def calculate_age_on(date_of_birth: date, reference_date: date) -> int:
    '''
    Calculate age in completed years on reference_date.
    reference_date is always camp start_date for group assignment.

    Edge case: birthday ON the reference date counts as that new age.
    This is the single most test-critical function in the app.

    Examples:
        DOB 2015-07-16, camp starts 2025-07-16 → age 10  (birthday today)
        DOB 2015-07-17, camp starts 2025-07-16 → age 9   (birthday tomorrow)
        DOB 2015-07-15, camp starts 2025-07-16 → age 10  (birthday was yesterday)
    '''
    years = reference_date.year - date_of_birth.year
    if (reference_date.month, reference_date.day) < (date_of_birth.month, date_of_birth.day):
        years -= 1
    return years


def find_age_group_for_age(age: int, age_groups: list) -> Optional[object]:
    '''
    Given a child's age and a list of AgeGroup objects, return the matching group.
    Returns None if age falls outside all defined ranges.

    age_groups should be ordered by display_order (which CampSession.age_groups already is).
    '''
    for group in age_groups:
        if group.min_age <= age <= group.max_age:
            return group
    return None


def assign_age_group(child, camp_session) -> Optional[object]:
    '''
    Auto-assign a child to an age group for a given camp session.
    Returns the AgeGroup object, or None if no group covers the child's age.

    This is the function called at registration time.
    Admin can override the result — see Registration.admin_override.
    '''
    age = calculate_age_on(child.date_of_birth, camp_session.start_date)
    return find_age_group_for_age(age, camp_session.age_groups)


# =============================================================================
# [2] GROUP SPLITTING ALGORITHM
# =============================================================================
#
# Admin workflow:
#   1. Admin chooses number of groups (1–6)
#   2. App calls recommend_group_split() to suggest age boundaries
#   3. UI shows slider — admin adjusts boundaries live
#   4. UI calls preview_group_split() on each slider move to show child counts
#   5. Admin confirms → save_group_split() writes AgeGroup rows to DB
#
# The algorithm finds the partition of the age range into K groups that
# minimises variance in group sizes (i.e. most balanced split).
# It respects natural age boundaries — never splits children of the same age
# across two groups if avoidable.

def get_age_distribution(camp_session) -> dict:
    '''
    Returns a dict mapping age → count of confirmed registered children
    for a given camp session.

    Example: {6: 4, 7: 3, 8: 8, 9: 6, 10: 5, ...}
    '''
    from sub_modules.models import Registration
    distribution = {}
    confirmed = Registration.query.filter_by(
        camp_session_id=camp_session.id,
        status='confirmed'
    ).all()
    for reg in confirmed:
        age = calculate_age_on(reg.child.date_of_birth, camp_session.start_date)
        distribution[age] = distribution.get(age, 0) + 1
    return distribution


def recommend_group_split(age_distribution: dict, num_groups: int) -> list:
    '''
    Given an age distribution and desired number of groups, return the
    recommended age boundaries as a list of group definitions.

    Returns a list of dicts:
    [
        {'min_age': 4, 'max_age': 7, 'count': 12, 'suggested_name': 'Gruppe 1'},
        {'min_age': 8, 'max_age': 11, 'count': 15, 'suggested_name': 'Gruppe 2'},
        ...
    ]

    Algorithm:
    - Enumerate all ways to place (num_groups - 1) cut points in the sorted age list
    - Score each partition by variance in group sizes
    - Return the partition with the lowest variance (most balanced)
    - Tie-break: prefer splits that align with standard U-group boundaries
    '''
    if not age_distribution:
        return []

    ages = sorted(age_distribution.keys())
    min_age = ages[0]
    max_age = ages[-1]

    if num_groups == 1:
        total = sum(age_distribution.values())
        return [{'min_age': min_age, 'max_age': max_age, 'count': total,
                 'suggested_name': 'Gruppe 1'}]

    if num_groups >= len(ages):
        # More groups than age values — one age per group, some may be empty
        result = []
        for i, age in enumerate(ages):
            result.append({
                'min_age': age,
                'max_age': age,
                'count': age_distribution.get(age, 0),
                'suggested_name': f'Gruppe {i + 1}'
            })
        return result

    # Find all ways to place (num_groups - 1) dividers between consecutive ages
    # A divider between index i and i+1 means ages[i] is the last age in one group
    # and ages[i+1] is the first age in the next group
    possible_divider_positions = list(range(1, len(ages)))  # positions between ages

    best_partition = None
    best_score = float('inf')

    for divider_positions in combinations(possible_divider_positions, num_groups - 1):
        # Build groups from divider positions
        groups = []
        prev_idx = 0
        for pos in divider_positions:
            group_ages = ages[prev_idx:pos]
            count = sum(age_distribution.get(a, 0) for a in group_ages)
            groups.append({
                'min_age': group_ages[0],
                'max_age': group_ages[-1],
                'count': count
            })
            prev_idx = pos
        # Last group
        group_ages = ages[prev_idx:]
        count = sum(age_distribution.get(a, 0) for a in group_ages)
        groups.append({
            'min_age': group_ages[0],
            'max_age': group_ages[-1],
            'count': count
        })

        # Score by variance — lower is better (more balanced groups)
        counts = [g['count'] for g in groups]
        score = statistics.variance(counts) if len(counts) > 1 else 0

        # Tie-break: prefer splits that align with even age boundaries
        # (e.g. splitting at 8/9 rather than 7/8 for a natural U10 feel)
        alignment_bonus = sum(
            0.1 for pos in divider_positions
            if ages[pos] % 2 == 0
        )
        score -= alignment_bonus

        if score < best_score:
            best_score = score
            best_partition = groups

    # Add suggested names
    for i, group in enumerate(best_partition):
        group['suggested_name'] = f'Gruppe {i + 1}'

    return best_partition


def preview_group_split(age_distribution: dict, boundaries: list) -> list:
    '''
    Given an age distribution and a list of (min_age, max_age) boundary tuples,
    return group preview data with child counts.

    Called on every slider move in the UI — must be fast.
    boundaries example: [(4, 7), (8, 11), (12, 15)]

    Returns:
    [
        {'min_age': 4, 'max_age': 7, 'count': 12},
        {'min_age': 8, 'max_age': 11, 'count': 15},
        {'min_age': 12, 'max_age': 15, 'count': 9},
    ]
    '''
    result = []
    for min_age, max_age in boundaries:
        count = sum(
            age_distribution.get(age, 0)
            for age in range(min_age, max_age + 1)
        )
        result.append({'min_age': min_age, 'max_age': max_age, 'count': count})
    return result


def save_group_split(camp_session, group_definitions: list, capacity_per_group: int = 20):
    '''
    Persist group definitions to the database.
    Replaces any existing age groups for this session.
    Re-assigns any existing confirmed registrations to their new groups.

    group_definitions: list of dicts with keys min_age, max_age, name
    e.g. [{'name': 'U8', 'min_age': 6, 'max_age': 7}, ...]

    Called when admin confirms the group split in the admin panel.
    Returns (success: bool, message: str)
    '''
    from sub_modules.extensions import db
    from sub_modules.models import AgeGroup, Registration

    try:
        # Delete existing age groups for this session
        AgeGroup.query.filter_by(camp_session_id=camp_session.id).delete()
        db.session.flush()

        # Create new groups
        new_groups = []
        for i, definition in enumerate(group_definitions):
            group = AgeGroup(
                camp_session_id=camp_session.id,
                name=definition['name'],
                min_age=definition['min_age'],
                max_age=definition['max_age'],
                capacity=definition.get('capacity', capacity_per_group),
                display_order=i + 1
            )
            db.session.add(group)
            new_groups.append(group)
        db.session.flush()

        # Re-assign existing registrations to new groups
        registrations = Registration.query.filter_by(
            camp_session_id=camp_session.id
        ).all()
        for reg in registrations:
            age = calculate_age_on(reg.child.date_of_birth, camp_session.start_date)
            new_group = find_age_group_for_age(age, new_groups)
            if new_group:
                reg.age_group_id = new_group.id
                if not reg.admin_override:
                    reg.auto_assigned_group = new_group.name
            else:
                # Child's age falls outside all new groups — flag it
                reg.age_group_id = None
            reg.updated_at = datetime.utcnow()

        db.session.commit()
        return True, 'Gruppen erfolgreich gespeichert.'

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f'save_group_split error: {e}')
        return False, f'Fehler beim Speichern der Gruppen: {str(e)}'


# =============================================================================
# [3] HEAD COACH MANAGEMENT
# =============================================================================

def assign_head_coach(age_group, staff_user, assigned_by_user):
    '''
    Assign a head coach to an age group.
    Removes any existing head coach for that group first.
    A trainer can be head coach of only one group per session.

    Returns (success: bool, message: str)
    '''
    from sub_modules.extensions import db
    from sub_modules.models import GroupAssignment

    try:
        # Remove existing head coach for this group
        GroupAssignment.query.filter_by(
            age_group_id=age_group.id,
            is_head_coach=True
        ).update({'is_head_coach': False})

        # Check if this staff member already has an assignment for this group
        existing = GroupAssignment.query.filter_by(
            age_group_id=age_group.id,
            staff_user_id=staff_user.id
        ).first()

        if existing:
            existing.is_head_coach = True
            existing.assigned_at = datetime.utcnow()
            existing.assigned_by_user_id = assigned_by_user.id
        else:
            assignment = GroupAssignment(
                camp_session_id=age_group.camp_session_id,
                age_group_id=age_group.id,
                staff_user_id=staff_user.id,
                is_head_coach=True,
                assigned_by_user_id=assigned_by_user.id
            )
            db.session.add(assignment)

        db.session.commit()
        return True, f'{staff_user.full_name} als Haupttrainer zugewiesen.'

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f'assign_head_coach error: {e}')
        return False, f'Fehler: {str(e)}'


def get_head_coach_conflicts(camp_session) -> list:
    '''
    Returns a list of AgeGroup objects that have no assigned head coach.
    Used to drive the orange warning banner visible to all staff.

    An empty list means no conflicts — no banner shown.
    '''
    conflicts = []
    for group in camp_session.age_groups:
        if group.head_coach is None:
            conflicts.append(group)
    return conflicts


def head_coach_warnings_enabled(camp_session) -> bool:
    '''
    Returns True if head coach conflict warnings should be shown for this session.
    Admins can disable warnings per-session (for camps that intentionally
    operate without assigned head coaches).

    Stored as a flag on CampSession — added when we extend the model.
    Defaults to True (warnings on) for safety.
    '''
    return getattr(camp_session, 'require_head_coaches', True)


# =============================================================================
# [4] CHECK-IN / CHECK-OUT PROCESSING
# =============================================================================

def get_camp_day_number(camp_session, event_date: date) -> Optional[int]:
    '''
    Returns the camp day number (1-4) for a given date within the session.
    Returns None if the date is outside the camp window.
    1=Wednesday, 2=Thursday, 3=Friday, 4=Saturday
    '''
    delta = (event_date - camp_session.start_date).days
    if 0 <= delta <= (camp_session.end_date - camp_session.start_date).days:
        return delta + 1
    return None


def process_checkin(registration, staff_user, method: str = 'search') -> tuple:
    '''
    Record a child check-in event.

    Rules:
    - Only the earliest check-in per day is authoritative (is_duplicate=False)
    - Subsequent check-ins for the same registration+day are flagged is_duplicate=True
    - Both are recorded for audit purposes

    Returns (success: bool, message: str, log: CheckinLog)
    '''
    from sub_modules.extensions import db
    from sub_modules.models import CheckinLog

    today = date.today()
    now = datetime.utcnow()
    camp_day = get_camp_day_number(registration.camp_session, today)

    if camp_day is None:
        return False, 'Heute ist kein Camptag.', None

    # Check for existing non-voided check-in today
    existing = CheckinLog.query.filter_by(
        registration_id=registration.id,
        event_type='checkin',
        camp_day=camp_day,
        voided=False
    ).first()

    is_duplicate = existing is not None

    log = CheckinLog(
        registration_id=registration.id,
        staff_user_id=staff_user.id,
        camp_session_id=registration.camp_session_id,
        event_type='checkin',
        camp_day=camp_day,
        event_date=today,
        event_time=now,
        method=method,
        is_duplicate=is_duplicate
    )
    db.session.add(log)

    try:
        db.session.commit()
        if is_duplicate:
            return True, f'{registration.child.full_name} bereits eingecheckt (doppelter Scan).', log
        return True, f'{registration.child.full_name} erfolgreich eingecheckt.', log
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f'process_checkin error: {e}')
        return False, f'Fehler beim Einchecken: {str(e)}', None


def process_checkout(registration, staff_user, method: str = 'search') -> tuple:
    '''
    Record a child check-out event.

    During checkout the caller should display registration.independent_travel
    prominently before calling this function — that check happens in the route,
    not here, so it can be presented clearly in the UI before confirming.

    Returns (success: bool, message: str, log: CheckinLog,
             independent_travel: bool)
    '''
    from sub_modules.extensions import db
    from sub_modules.models import CheckinLog

    today = date.today()
    now = datetime.utcnow()
    camp_day = get_camp_day_number(registration.camp_session, today)

    if camp_day is None:
        return False, 'Heute ist kein Camptag.', None, False

    # Must be checked in first
    checkin = registration.checkin_for_day(camp_day)
    if not checkin:
        return False, f'{registration.child.full_name} ist nicht eingecheckt.', None, False

    # Check for existing non-voided checkout
    existing_checkout = registration.checkout_for_day(camp_day)
    if existing_checkout:
        return False, f'{registration.child.full_name} wurde bereits ausgecheckt.', None, False

    log = CheckinLog(
        registration_id=registration.id,
        staff_user_id=staff_user.id,
        camp_session_id=registration.camp_session_id,
        event_type='checkout',
        camp_day=camp_day,
        event_date=today,
        event_time=now,
        method=method,
        is_duplicate=False
    )
    db.session.add(log)

    try:
        db.session.commit()
        return (
            True,
            f'{registration.child.full_name} ausgecheckt.',
            log,
            registration.independent_travel
        )
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f'process_checkout error: {e}')
        return False, f'Fehler beim Auschecken: {str(e)}', None, False


def void_checkin_event(checkin_log, voided_by_user, reason: str = '') -> tuple:
    '''
    Void a check-in or check-out event.
    Never deletes — marks voided=True with audit trail.
    If the authoritative check-in is voided, promotes the next duplicate.

    Returns (success: bool, message: str)
    '''
    from sub_modules.extensions import db
    from sub_modules.models import CheckinLog

    if checkin_log.voided:
        return False, 'Dieser Eintrag wurde bereits storniert.'

    checkin_log.voided = True
    checkin_log.voided_by_user_id = voided_by_user.id
    checkin_log.voided_at = datetime.utcnow()
    checkin_log.void_reason = reason

    # If we voided the authoritative check-in, promote the earliest duplicate
    if (checkin_log.event_type == 'checkin' and
            not checkin_log.is_duplicate and
            checkin_log.registration_id):

        next_duplicate = CheckinLog.query.filter_by(
            registration_id=checkin_log.registration_id,
            event_type='checkin',
            camp_day=checkin_log.camp_day,
            is_duplicate=True,
            voided=False
        ).order_by(CheckinLog.event_time.asc()).first()

        if next_duplicate:
            next_duplicate.is_duplicate = False

    try:
        db.session.commit()
        return True, 'Eintrag erfolgreich storniert.'
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f'void_checkin_event error: {e}')
        return False, f'Fehler: {str(e)}'


def process_staff_checkin(staff_user, camp_session, method: str = 'search') -> tuple:
    '''
    Record a voluntary staff check-in event.
    registration_id is None for staff events.
    Multiple check-ins per day are flagged as duplicates.

    Returns (success: bool, message: str)
    '''
    from sub_modules.extensions import db
    from sub_modules.models import CheckinLog

    today = date.today()
    camp_day = get_camp_day_number(camp_session, today)

    if camp_day is None:
        return False, 'Heute ist kein Camptag.'

    existing = CheckinLog.query.filter_by(
        staff_user_id=staff_user.id,
        registration_id=None,
        event_type='checkin',
        camp_day=camp_day,
        voided=False
    ).first()

    is_duplicate = existing is not None

    log = CheckinLog(
        registration_id=None,
        staff_user_id=staff_user.id,
        camp_session_id=camp_session.id,
        event_type='checkin',
        camp_day=camp_day,
        event_date=today,
        event_time=datetime.utcnow(),
        method=method,
        is_duplicate=is_duplicate
    )
    db.session.add(log)

    try:
        db.session.commit()
        return True, 'Eingecheckt.'
    except Exception as e:
        db.session.rollback()
        return False, f'Fehler: {str(e)}'


def get_still_present_registrations(camp_session, camp_day: int) -> list:
    '''
    Returns a list of Registration objects for children who are currently
    checked in but not yet checked out on the given camp day.

    Used for the end-of-day "still checked in" alert view.
    Sorted by age group display order, then child last name.
    '''
    from sub_modules.models import Registration, CheckinLog

    # Get all registrations with a check-in today
    checked_in_reg_ids = {
        log.registration_id
        for log in CheckinLog.query.filter_by(
            camp_session_id=camp_session.id,
            event_type='checkin',
            camp_day=camp_day,
            is_duplicate=False,
            voided=False
        ).all()
    }

    # Get all registrations with a checkout today
    checked_out_reg_ids = {
        log.registration_id
        for log in CheckinLog.query.filter_by(
            camp_session_id=camp_session.id,
            event_type='checkout',
            camp_day=camp_day,
            voided=False
        ).all()
    }

    still_present_ids = checked_in_reg_ids - checked_out_reg_ids

    registrations = Registration.query.filter(
        Registration.id.in_(still_present_ids)
    ).all()

    # Sort by age group display_order, then child last name
    return sorted(
        registrations,
        key=lambda r: (
            r.age_group.display_order if r.age_group else 999,
            r.child.last_name
        )
    )


def resolve_checkin_by_qr_token(token: str, camp_session) -> Optional[object]:
    '''
    Look up a Registration by QR token for the given camp session.
    Returns the Registration if found and confirmed, None otherwise.
    '''
    from sub_modules.models import QRToken, Registration

    qr = QRToken.query.filter_by(token=token).first()
    if not qr:
        return None

    return Registration.query.filter(
        Registration.child_id == qr.child_id,
        Registration.camp_session_id == camp_session.id,
        Registration.status.in_(['confirmed', 'waitlisted'])
    ).first()


# =============================================================================
# [5] STAFF AUTO-CHECKOUT
# Called daily by APScheduler at STAFF_AUTO_CHECKOUT_TIME
# =============================================================================

def run_staff_auto_checkout():
    '''
    Insert checkout events for all staff who checked in today but have no
    checkout record. Marks events with is_auto_checkout=True.

    Runs inside app context (provided by APScheduler job in application.py).
    Only runs on active camp days.
    '''
    from sub_modules.extensions import db
    from sub_modules.models import CampSession, CheckinLog

    today = date.today()
    now = datetime.utcnow()

    # Find active sessions with today as a camp day
    active_sessions = CampSession.query.filter_by(status='active').all()

    for session in active_sessions:
        camp_day = get_camp_day_number(session, today)
        if not camp_day:
            continue

        # Find staff with a check-in today but no checkout
        checked_in = CheckinLog.query.filter_by(
            camp_session_id=session.id,
            event_type='checkin',
            camp_day=camp_day,
            registration_id=None,   # staff events only
            is_duplicate=False,
            voided=False
        ).all()

        checked_out_user_ids = {
            log.staff_user_id
            for log in CheckinLog.query.filter_by(
                camp_session_id=session.id,
                event_type='checkout',
                camp_day=camp_day,
                registration_id=None,
                voided=False
            ).all()
        }

        for checkin_log in checked_in:
            if checkin_log.staff_user_id not in checked_out_user_ids:
                checkout = CheckinLog(
                    registration_id=None,
                    staff_user_id=checkin_log.staff_user_id,
                    camp_session_id=session.id,
                    event_type='checkout',
                    camp_day=camp_day,
                    event_date=today,
                    event_time=now,
                    method='search',
                    is_auto_checkout=True
                )
                db.session.add(checkout)

    try:
        db.session.commit()
        current_app.logger.info(f'[AutoCheckout] Completed for {today}')
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f'[AutoCheckout] Failed: {e}')


# =============================================================================
# [6] QR TOKEN GENERATION
# =============================================================================

def generate_qr_token() -> str:
    '''
    Generate a cryptographically secure QR token.
    Uses secrets.token_urlsafe — 256 bits of entropy, URL-safe.
    '''
    return secrets.token_urlsafe(32)


def get_or_create_qr_token(child) -> object:
    '''
    Return existing QR token for a child, or create one if it doesn't exist.
    '''
    from sub_modules.extensions import db
    from sub_modules.models import QRToken

    if child.qr_token:
        return child.qr_token

    token = QRToken(
        child_id=child.id,
        token=generate_qr_token()
    )
    db.session.add(token)
    db.session.commit()
    return token


def regenerate_qr_token(child) -> object:
    '''
    Replace a child's QR token with a new one.
    Called by admin if a token is lost or compromised.
    '''
    from sub_modules.extensions import db
    from sub_modules.models import QRToken

    if child.qr_token:
        child.qr_token.token = generate_qr_token()
        child.qr_token.regenerated_at = datetime.utcnow()
    else:
        token = QRToken(child_id=child.id, token=generate_qr_token())
        db.session.add(token)

    db.session.commit()
    return child.qr_token


# =============================================================================
# [7] WAITLIST UTILITIES
# =============================================================================

def get_next_waitlist_position(camp_session_id: int, age_group_id: int) -> int:
    '''
    Returns the next waitlist position number for a given group.
    Waitlist positions start at 1 and are sequential.
    '''
    from sub_modules.models import Registration
    from sqlalchemy import func

    max_pos = Registration.query.filter_by(
        camp_session_id=camp_session_id,
        age_group_id=age_group_id,
        status='waitlisted'
    ).with_entities(func.max(Registration.waitlist_position)).scalar()

    return (max_pos or 0) + 1


def promote_from_waitlist(registration) -> tuple:
    '''
    Manually promote a waitlisted registration to confirmed.
    Admin calls this after communicating with the family externally.
    Reorders remaining waitlist positions.

    Returns (success: bool, message: str)
    '''
    from sub_modules.extensions import db
    from sub_modules.models import Registration

    if registration.status != 'waitlisted':
        return False, 'Diese Anmeldung steht nicht auf der Warteliste.'

    promoted_position = registration.waitlist_position

    # Check capacity
    if registration.age_group and registration.age_group.is_full:
        return False, 'Die Gruppe ist noch voll. Bitte zuerst einen Platz freigeben.'

    registration.status = 'confirmed'
    registration.waitlist_position = None
    registration.updated_at = datetime.utcnow()

    # Shift remaining waitlist positions down
    if promoted_position:
        remaining = Registration.query.filter(
            Registration.camp_session_id == registration.camp_session_id,
            Registration.age_group_id == registration.age_group_id,
            Registration.status == 'waitlisted',
            Registration.waitlist_position > promoted_position
        ).all()

        for reg in remaining:
            reg.waitlist_position -= 1

    try:
        db.session.commit()
        return True, f'{registration.child.full_name} von der Warteliste bestätigt.'
    except Exception as e:
        db.session.rollback()
        return False, f'Fehler: {str(e)}'


# =============================================================================
# [8] GDPR RETENTION UTILITIES
# =============================================================================

def flag_stale_accounts(retention_years: int = 2) -> tuple:
    '''
    Flag user accounts that have been inactive for more than retention_years.
    Called by admin_tools/retention_check.py annually.

    Flags accounts by setting retention_flag_date = today.
    Does NOT delete or modify user data.
    Returns (flagged_count: int, user_ids: list)
    '''
    from sub_modules.extensions import db
    from sub_modules.models import User

    cutoff_year = datetime.utcnow().year - retention_years

    stale_users = User.query.filter(
        User.last_active_year <= cutoff_year,
        User.is_deleted == False,
        User.retention_flag_date == None,
        User.role == 'parent'
    ).all()

    flagged = []
    for user in stale_users:
        user.retention_flag_date = date.today()
        flagged.append(user.id)

    try:
        db.session.commit()
        return len(flagged), flagged
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f'flag_stale_accounts error: {e}')
        return 0, []


def soft_delete_user(user, reason: str = 'gdpr_retention') -> tuple:
    '''
    Anonymise and soft-delete a user account for GDPR erasure.
    Replaces personal data with anonymised placeholders.
    Preserves the user ID and camp participation history (child attended = true).

    Returns (success: bool, message: str)
    '''
    from sub_modules.extensions import db

    user.email             = f'deleted_{user.id}@deleted.invalid'
    user.password_hash     = ''
    user.first_name        = '[gelöscht]'
    user.last_name         = '[gelöscht]'
    user.phone             = None
    user.address_street    = None
    user.address_city      = None
    user.address_postcode  = None
    user.is_deleted        = True
    user.deleted_at        = datetime.utcnow()
    user.is_active         = False

    # Anonymise children too
    for child in user.children:
        child.first_name     = '[gelöscht]'
        child.last_name      = '[gelöscht]'
        child.medical_notes  = None
        child.is_deleted     = True
        child.deleted_at     = datetime.utcnow()
        for ec in child.emergency_contacts:
            ec.full_name       = '[gelöscht]'
            ec.phone_primary   = '[gelöscht]'
            ec.phone_secondary = None

    try:
        db.session.commit()
        return True, f'Konto {user.id} erfolgreich gelöscht.'
    except Exception as e:
        db.session.rollback()
        return False, f'Fehler: {str(e)}'


def update_last_active_year(user, year: int):
    '''
    Update a user's last_active_year and reset retention flag.
    Called when a parent completes registration for a camp session.
    '''
    from sub_modules.extensions import db

    user.last_active_year = year
    user.retention_flag_date = None
    user.deletion_warning_sent_at = None
    db.session.commit()


# =============================================================================
# [9] SECURITY / REDIRECT UTILITIES
# =============================================================================

def is_safe_redirect_url(target: str) -> bool:
    '''
    Validate that a redirect target URL is on our own domain.
    Prevents open redirect attacks where ?next= points to an external site.

    Only accepts relative paths or URLs matching the current host.
    '''
    if not target:
        return False

    host_url = urlparse(request.host_url)
    redirect_url = urlparse(urljoin(request.host_url, target))

    return (
        redirect_url.scheme in ('http', 'https') and
        host_url.netloc == redirect_url.netloc
    )


def redirect_after_login(user, next_page: str = None):
    '''
    Redirect user to the appropriate page after login.
    Checks next_page is safe before using it.
    Falls back to role-appropriate dashboard.
    '''
    if next_page and is_safe_redirect_url(next_page):
        return redirect(next_page)

    if user.role == 'admin':
        return redirect(url_for('admin.dashboard'))
    if user.role == 'staff':
        return redirect(url_for('staff.dashboard'))

    # Show welcome modal on first login if parent has no children
    if user.role == 'parent' and not user.children:
        from flask import session as flask_session
        flask_session['show_welcome_modal'] = True

    return redirect(url_for('parents.dashboard'))


def hash_password(password: str) -> str:
    return generate_password_hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    return check_password_hash(password_hash, password)


def generate_token() -> str:
    '''
    Generate a secure random token for email verification or invite links.
    '''
    return secrets.token_urlsafe(32)


# =============================================================================
# [10] GENERAL UTILITIES
# =============================================================================

def honeypot_triggered(form_data: dict, field_name: str = 'website') -> bool:
    '''
    Check if a honeypot field was filled in — strong indicator of a bot.
    The honeypot field should be hidden via CSS in the template and
    never filled in by real users.

    field_name should match the hidden input name in the form.
    '''
    return bool(form_data.get(field_name, '').strip())


def submission_too_fast(form_start_time: float, min_seconds: int = 3) -> bool:
    '''
    Check if a form was submitted suspiciously fast.
    form_start_time is a Unix timestamp embedded as a hidden field
    when the page was rendered.

    Real users take at least a few seconds to fill in a form.
    Bots typically submit instantly.
    '''
    import time
    elapsed = time.time() - form_start_time
    return elapsed < min_seconds


def allowed_image_file(filename: str) -> bool:
    '''
    Check if an uploaded file has an allowed image extension.
    NOTE: extension check only — always re-encode with Pillow before storing.
    '''
    from sub_modules.config import ALLOWED_IMAGE_EXTENSIONS
    return (
        '.' in filename and
        filename.rsplit('.', 1)[1].lower() in ALLOWED_IMAGE_EXTENSIONS
    )


def paginate_query(query, page: int, per_page: int = 20):
    '''
    Wrapper around SQLAlchemy paginate for consistent pagination across views.
    '''
    return query.paginate(page=page, per_page=per_page, error_out=False)


# =============================================================================
# [11] MANUAL ENTRY (PAPER SIGNUP FORMS)
# =============================================================================

def create_manual_entry(admin_user, form_data: dict) -> tuple:
    '''
    Admin creates a parent + child account from a paper signup form.

    Flow:
        1. Admin fills in parent details, child details, and paper consent checkboxes
        2. Account is created with email_verified=False, manually_entered=True
        3. Registration is created with status='pending_verification'
        4. Verification email is sent to parent
        5. Parent clicks link → sets password → verifies email
        6. Registration status automatically promoted to 'confirmed' or 'waitlisted'
           (handled in auth.verify_email route)

    Paper consent fields recorded:
        - paper_consent_data_processing  (Datenschutz — required on paper form)
        - paper_consent_photo            (Fotoerlaubnis)
        - paper_consent_independent_travel (Heimweg alleine)

    These are the admin's record of what the parent signed on paper.
    The parent's own digital consent is still required via email verification.

    form_data keys:
        parent_email, parent_first_name, parent_last_name, parent_phone,
        parent_address_street, parent_address_city, parent_address_postcode,
        child_first_name, child_last_name, child_dob (date object),
        child_medical_notes,
        emergency_contact_name, emergency_contact_relationship,
        emergency_contact_phone,
        camp_session_id,
        paper_consent_data, paper_consent_photo, paper_consent_travel,
        force_age_group_id (optional — admin override)

    Returns (success: bool, message: str, user: User|None)
    '''
    from sub_modules.extensions import db
    from sub_modules.models import (User, Child, EmergencyContact,
                                    Registration, QRToken)
    from sub_modules.emails import send_verification_email, log_email
    from sub_modules.config import CURRENT_CONSENT_VERSION

    email = form_data.get('parent_email', '').lower().strip()
    phone = form_data.get('parent_phone', '').strip()

    # Validate required fields
    if not phone:
        return False, 'Telefonnummer ist erforderlich.', None

    # Check if parent account already exists
    existing_user = User.query.filter_by(email=email).first()

    try:
        if existing_user:
            # Parent already has an account — just add the child and registration
            parent = existing_user
            is_new_parent = False
        else:
            # Create new parent account — no password yet, pending verification
            token = generate_token()
            from datetime import timedelta
            from sub_modules.config import VERIFY_TOKEN_EXPIRY_HOURS
            expiry = datetime.utcnow() + timedelta(hours=VERIFY_TOKEN_EXPIRY_HOURS)

            parent = User(
                email=email,
                password_hash='',               # set when parent verifies
                first_name=form_data.get('parent_first_name', '').strip(),
                last_name=form_data.get('parent_last_name', '').strip(),
                phone=form_data.get('parent_phone', '').strip() or None,
                address_street=form_data.get('parent_address_street', '').strip() or None,
                address_city=form_data.get('parent_address_city', '').strip() or None,
                address_postcode=form_data.get('parent_address_postcode', '').strip() or None,
                role='parent',
                email_verified=False,
                verify_token=token,
                verify_token_expiry=expiry,
                manually_entered=True,
                manually_entered_by=admin_user.id,
                manually_entered_at=datetime.utcnow(),
                # No consent recorded yet — parent gives consent on email verification
                consent_given_at=None,
                consent_version=None,
                is_active=True,
            )
            db.session.add(parent)
            db.session.flush()
            is_new_parent = True

        # Create child record
        child = Child(
            parent_user_id=parent.id,
            first_name=form_data.get('child_first_name', '').strip(),
            last_name=form_data.get('child_last_name', '').strip(),
            date_of_birth=form_data.get('child_dob'),
            medical_notes=form_data.get('child_medical_notes', '').strip() or None,
            photo_consent_default=form_data.get('paper_consent_photo', False),
        )
        db.session.add(child)
        db.session.flush()

        # Emergency contact (optional)
        ec_name = form_data.get('emergency_contact_name', '').strip()
        ec_phone = form_data.get('emergency_contact_phone', '').strip()
        if ec_name and ec_phone:
            ec = EmergencyContact(
                child_id=child.id,
                full_name=ec_name,
                relationship=form_data.get('emergency_contact_relationship', '').strip() or None,
                phone_primary=ec_phone,
            )
            db.session.add(ec)

        # QR token
        qr = QRToken(child_id=child.id, token=generate_qr_token())
        db.session.add(qr)

        # Determine age group
        from sub_modules.models import CampSession
        camp_session = CampSession.query.get(form_data.get('camp_session_id'))
        if not camp_session:
            db.session.rollback()
            return False, 'Camp-Session nicht gefunden.', None

        force_group_id = form_data.get('force_age_group_id')
        auto_group = assign_age_group(child, camp_session)
        admin_override = force_group_id is not None

        if force_group_id:
            age_group_id = force_group_id
            auto_group_name = auto_group.name if auto_group else None
        else:
            age_group_id = auto_group.id if auto_group else None
            auto_group_name = auto_group.name if auto_group else None

        # Determine if confirmed or waitlisted
        if auto_group and auto_group.is_full:
            status = 'waitlisted'
            waitlist_pos = get_next_waitlist_position(
                camp_session.id, age_group_id
            )
        else:
            status = 'pending_verification'  # promotes to confirmed on email verify
            waitlist_pos = None

        registration = Registration(
            child_id=child.id,
            camp_session_id=camp_session.id,
            age_group_id=age_group_id,
            status=status,
            waitlist_position=waitlist_pos,
            auto_assigned_group=auto_group_name,
            admin_override=admin_override,
            admin_override_by=admin_user.id if admin_override else None,
            admin_override_at=datetime.utcnow() if admin_override else None,
            independent_travel=form_data.get('paper_consent_travel', False),
            photo_consent=form_data.get('paper_consent_photo', False),
            # Paper consent fields — admin's record of what parent signed
            paper_consent_data_processing=form_data.get('paper_consent_data', False),
            paper_consent_photo=form_data.get('paper_consent_photo', False),
            paper_consent_independent_travel=form_data.get('paper_consent_travel', False),
            entered_by_user_id=admin_user.id,
            entered_at=datetime.utcnow(),
            # Digital consent version recorded when parent verifies email
            consent_version_at_registration=None,
        )
        db.session.add(registration)
        db.session.commit()

        # Send verification email to parent
        if is_new_parent:
            try:
                send_verification_email(parent, parent.verify_token)
                log_email(parent.id, parent.email, 'verification',
                          'E-Mail-Adresse bestätigen (manuelle Eingabe)')
            except Exception as e:
                from flask import current_app
                current_app.logger.error(
                    f'[ManualEntry] Failed to send verification email to '
                    f'{parent.email}: {e}'
                )

        action = 'Elternteil und Kind' if is_new_parent else 'Kind'
        return (
            True,
            f'{action} erfolgreich eingetragen. '
            f'Bestätigungsmail an {parent.email} gesendet.',
            parent
        )

    except Exception as e:
        db.session.rollback()
        from flask import current_app
        current_app.logger.error(f'[ManualEntry] Error: {e}')
        return False, f'Fehler bei der manuellen Eingabe: {str(e)}', None


def finalise_manual_registration(user) -> tuple:
    '''
    Called from auth.verify_email when a manually-entered parent
    completes email verification.

    Promotes all pending_verification registrations to confirmed or
    waitlisted, and records digital consent.
    '''
    from sub_modules.extensions import db
    from sub_modules.models import Registration
    from sub_modules.config import CURRENT_CONSENT_VERSION

    promoted = []
    try:
        pending = Registration.query.join(Child).filter(
            Child.parent_user_id == user.id,
            Registration.status == 'pending_verification'
        ).all()

        for reg in pending:
            if reg.age_group and reg.age_group.is_full:
                reg.status = 'waitlisted'
                reg.waitlist_position = get_next_waitlist_position(
                    reg.camp_session_id, reg.age_group_id
                )
            else:
                reg.status = 'confirmed'

            # Record digital consent version at point of verification
            reg.consent_version_at_registration = CURRENT_CONSENT_VERSION
            promoted.append(reg)

        db.session.commit()
        return True, promoted

    except Exception as e:
        db.session.rollback()
        from flask import current_app
        current_app.logger.error(f'[ManualEntry] finalise error: {e}')
        return False, []


# =============================================================================
# [12] PARENT & CHILD SEARCH (manual entry duplicate prevention)
# =============================================================================

def search_existing_parents(query: str, limit: int = 10) -> list:
    '''
    Search existing parent accounts by name, email, or phone.
    Used in the admin manual entry form to prevent duplicate accounts.

    query: free text — matched against first_name, last_name, email, phone
    Returns a list of dicts safe to serialise as JSON for the AJAX endpoint.

    Searches are case-insensitive. Partial matches supported.
    Deleted accounts are excluded.
    '''
    from sub_modules.models import User
    from sqlalchemy import or_, func

    if not query or len(query.strip()) < 2:
        return []

    q = query.strip().lower()

    # Build search — try to match any of the key fields
    matches = User.query.filter(
        User.role == 'parent',
        User.is_deleted == False,
        or_(
            func.lower(User.email).contains(q),
            func.lower(User.first_name).contains(q),
            func.lower(User.last_name).contains(q),
            User.phone.contains(q),
            # Full name match (first + last combined)
            func.lower(
                User.first_name + ' ' + User.last_name
            ).contains(q)
        )
    ).limit(limit).all()

    return [
        {
            'id': u.id,
            'full_name': u.full_name,
            'email': u.email,
            'phone': u.phone or '',
            'address': f'{u.address_street or ""}, {u.address_city or ""}'.strip(', '),
            'email_verified': u.email_verified,
            'children': [
                {
                    'id': c.id,
                    'full_name': c.full_name,
                    'dob': c.date_of_birth.strftime('%d.%m.%Y'),
                    'has_medical_notes': c.has_medical_notes,
                }
                for c in u.children
                if not c.is_deleted
            ]
        }
        for u in matches
    ]


def get_parent_for_manual_entry(user_id: int) -> dict:
    '''
    Return full parent details for auto-filling the manual entry form
    when an existing parent is selected from the search results.

    Returns a dict of all fields needed to pre-populate the form.
    Returns None if user not found or not a parent.
    '''
    from sub_modules.models import User

    user = User.query.filter_by(
        id=user_id,
        role='parent',
        is_deleted=False
    ).first()

    if not user:
        return None

    return {
        'id': user.id,
        'email': user.email,
        'first_name': user.first_name,
        'last_name': user.last_name,
        'phone': user.phone or '',
        'address_street': user.address_street or '',
        'address_city': user.address_city or '',
        'address_postcode': user.address_postcode or '',
        'email_verified': user.email_verified,
        # First child's emergency contact for pre-populating new child form
        'emergency_contact': (lambda ec: {
            'full_name': ec.full_name,
            'relationship': ec.relationship or '',
            'phone_primary': ec.phone_primary,
            'phone_secondary': ec.phone_secondary or '',
        })(user.children[0].emergency_contacts[0])
        if user.children and not user.children[0].is_deleted and user.children[0].emergency_contacts
        else None,
        'children': [
            {
                'id': c.id,
                'full_name': c.full_name,
                'first_name': c.first_name,
                'last_name': c.last_name,
                'dob': c.date_of_birth.isoformat(),
                'dob_display': c.date_of_birth.strftime('%d.%m.%Y'),
                'medical_notes': c.medical_notes or '',
                'has_medical_notes': c.has_medical_notes,
                'emergency_contacts': [
                    {
                        'full_name': ec.full_name,
                        'relationship': ec.relationship or '',
                        'phone_primary': ec.phone_primary,
                        'phone_secondary': ec.phone_secondary or '',
                    }
                    for ec in c.emergency_contacts
                ]
            }
            for c in user.children
            if not c.is_deleted
        ]
    }


def child_already_registered(child_id: int, camp_session_id: int) -> bool:
    '''
    Check if a child already has a registration for a given camp session.
    Used to warn admin before creating a duplicate registration.
    The DB unique constraint catches it anyway, but a clear UI warning is better.
    '''
    from sub_modules.models import Registration

    return Registration.query.filter(
        Registration.child_id == child_id,
        Registration.camp_session_id == camp_session_id,
        Registration.status.in_(['confirmed', 'waitlisted', 'pending_verification'])
    ).first() is not None


# =============================================================================
# [13] CAMP DAY UTILITIES
# =============================================================================

def sanitize_text(text: str, max_length: int = None) -> str:
    '''
    Strip all HTML tags and dangerous characters from a string.
    Truncates to max_length if provided.

    Used on all free-text fields before database storage:
    - Bug report subject, description, admin notes
    - Any other user-supplied text that will be rendered in an admin UI

    bleach.clean() with no allowed tags strips everything.
    strip=True removes the tag markup rather than escaping it.
    '''
    import bleach

    if not text:
        return ''

    # Strip all HTML — no tags allowed at all
    cleaned = bleach.clean(str(text), tags=[], attributes={}, strip=True)

    # Collapse excessive whitespace (a common injection padding technique)
    import re
    cleaned = re.sub(r'\s{3,}', '  ', cleaned).strip()

    if max_length:
        cleaned = cleaned[:max_length]

    return cleaned


def sanitize_url_path(url: str) -> str:
    '''
    Validate and return a relative URL path for storage in bug reports.
    Strips scheme, host, query strings, and fragments.
    Returns '/' if the input is not a valid relative path.

    Prevents stored XSS via javascript: or data: URIs and
    avoids storing user-identifying query parameters.
    '''
    if not url:
        return ''

    from urllib.parse import urlparse
    try:
        parsed = urlparse(url)
        # Reject anything with a scheme or netloc (absolute URLs)
        if parsed.scheme or parsed.netloc:
            return '/'
        # Return path only — no query string, no fragment
        path = parsed.path[:200]
        if not path.startswith('/'):
            return '/'
        return path
    except Exception:
        return '/'
