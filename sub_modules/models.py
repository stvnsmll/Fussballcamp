'''
sub_modules/models.py
=====================
SQLAlchemy models — one class per database table.
Matches schema.sql exactly. All relationships defined bidirectionally.

Design notes:
- Soft delete via is_deleted / deleted_at on User and Child
- GDPR fields (consent, retention) are first-class columns, not afterthoughts
- Business logic (age group calculation, head coach validation) lives in
  helpers.py as plain functions — not here — so it can be tested independently
- __repr__ methods are for debugging only
'''

from datetime import datetime, date
from flask_login import UserMixin
from sub_modules.extensions import db


# =============================================================================
# [1] USERS
# =============================================================================

class User(UserMixin, db.Model):
    __tablename__ = 'users'

    id                          = db.Column(db.Integer, primary_key=True)

    # Login credentials
    email                       = db.Column(db.String(255), nullable=False, unique=True)
    password_hash               = db.Column(db.String(255), nullable=False)

    # Basic identity
    first_name                  = db.Column(db.String(100), nullable=False)
    last_name                   = db.Column(db.String(100), nullable=False)
    phone                       = db.Column(db.String(30))
    address_street              = db.Column(db.String(255))
    address_city                = db.Column(db.String(100))
    address_postcode            = db.Column(db.String(20))
    address_country             = db.Column(db.String(100), default='Deutschland')

    # Role: 'parent', 'staff', 'admin'
    role                        = db.Column(db.String(20), nullable=False, default='parent')

    # Email verification
    email_verified              = db.Column(db.Boolean, nullable=False, default=False)
    verify_token                = db.Column(db.String(255))
    verify_token_expiry         = db.Column(db.DateTime)

    # Staff/admin invite tracking (NULL for self-registered parents)
    invited_by_user_id          = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='SET NULL'))
    invite_token                = db.Column(db.String(255))
    invite_token_expiry         = db.Column(db.DateTime)

    # Manual entry (paper signup form)
    # Admin creates account on behalf of parent from a paper form.
    # Account is pending until parent verifies email and sets password.
    # Parent must complete verification before registration is finalised.
    manually_entered            = db.Column(db.Boolean, nullable=False, default=False)
    manually_entered_by         = db.Column(db.Integer, db.ForeignKey('users.id',
                                            ondelete='SET NULL'))
    manually_entered_at         = db.Column(db.DateTime)

    # Email preferences
    email_announcements         = db.Column(db.Boolean, nullable=False, default=True)

    # GDPR consent
    consent_given_at            = db.Column(db.DateTime)
    consent_version             = db.Column(db.String(20))

    # GDPR retention tracking
    last_active_year            = db.Column(db.Integer)
    retention_flag_date         = db.Column(db.Date)
    deletion_warning_sent_at    = db.Column(db.DateTime)

    # Soft delete
    is_deleted                  = db.Column(db.Boolean, nullable=False, default=False)
    deleted_at                  = db.Column(db.DateTime)

    is_active                   = db.Column(db.Boolean, nullable=False, default=True)

    created_at                  = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at                  = db.Column(db.DateTime, nullable=False, default=datetime.utcnow,
                                            onupdate=datetime.utcnow)

    # ------------------------------------------------------------------
    # Relationships
    children                    = db.relationship('Child', back_populates='parent',
                                                  foreign_keys='Child.parent_user_id')
    staff_profile               = db.relationship('StaffProfile', back_populates='user',
                                                  uselist=False, cascade='all, delete-orphan')
    group_assignments           = db.relationship('GroupAssignment', back_populates='staff_user',
                                                  foreign_keys='GroupAssignment.staff_user_id')
    announcements               = db.relationship('Announcement', back_populates='author',
                                                  foreign_keys='Announcement.author_user_id')
    email_logs                  = db.relationship('EmailLog', back_populates='recipient_user',
                                                  foreign_keys='EmailLog.recipient_user_id')

    # ------------------------------------------------------------------
    # Flask-Login: respect soft delete
    def get_id(self):
        return str(self.id)

    @property
    def is_active_account(self):
        return self.is_active and not self.is_deleted

    # ------------------------------------------------------------------
    # Convenience role checks (use in templates and route guards)
    @property
    def is_parent(self):
        return self.role == 'parent'

    @property
    def is_staff(self):
        return self.role in ('staff', 'admin')

    @property
    def is_admin(self):
        return self.role == 'admin'

    @property
    def full_name(self):
        return f'{self.first_name} {self.last_name}'

    def __repr__(self):
        return f'<User {self.id} {self.email} [{self.role}]>'


# =============================================================================
# [2] STAFF PROFILES
# =============================================================================

class StaffProfile(db.Model):
    __tablename__ = 'staff_profiles'

    id              = db.Column(db.Integer, primary_key=True)
    user_id         = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='CASCADE'),
                                nullable=False, unique=True)

    # 'trainer', 'general', 'food'
    staff_class     = db.Column(db.String(20), nullable=False, default='general')

    # Independent of staff_class — any staff member can be first aid
    is_first_aid    = db.Column(db.Boolean, nullable=False, default=False)

    # Internal admin notes — not visible to the staff member
    notes           = db.Column(db.Text)

    created_at      = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at      = db.Column(db.DateTime, nullable=False, default=datetime.utcnow,
                                onupdate=datetime.utcnow)

    # ------------------------------------------------------------------
    # Relationships
    user            = db.relationship('User', back_populates='staff_profile')

    def __repr__(self):
        return f'<StaffProfile user={self.user_id} class={self.staff_class}>'


# =============================================================================
# [3] CAMP SESSIONS
# =============================================================================

class CampSession(db.Model):
    __tablename__ = 'camp_sessions'

    id                      = db.Column(db.Integer, primary_key=True)
    name                    = db.Column(db.String(255), nullable=False)
    year                    = db.Column(db.Integer, nullable=False)
    start_date              = db.Column(db.Date, nullable=False)   # Wednesday
    end_date                = db.Column(db.Date, nullable=False)   # Saturday
    location                = db.Column(db.String(255))
    description             = db.Column(db.Text)

    # Registration window
    registration_open       = db.Column(db.Boolean, nullable=False, default=False)
    registration_opens_at   = db.Column(db.DateTime)
    registration_closes_at  = db.Column(db.DateTime)

    # 'upcoming', 'open', 'active', 'completed', 'cancelled'
    status                  = db.Column(db.String(20), nullable=False, default='upcoming')

    # Camp-level capacity (0 = unlimited)
    max_registrants         = db.Column(db.Integer, nullable=False, default=0)

    # Camp-level age limits (0 = no limit on that end)
    min_age_limit           = db.Column(db.Integer, nullable=False, default=0)
    max_age_limit           = db.Column(db.Integer, nullable=False, default=0)

    # Head coach warnings — if True, an orange banner is shown to all staff
    # when any group lacks a head coach. Admin can disable per session
    # (e.g. for camps that intentionally operate without assigned head coaches).
    require_head_coaches    = db.Column(db.Boolean, nullable=False, default=True)

    created_at              = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at              = db.Column(db.DateTime, nullable=False, default=datetime.utcnow,
                                        onupdate=datetime.utcnow)

    # ------------------------------------------------------------------
    # Relationships
    age_groups              = db.relationship('AgeGroup', back_populates='camp_session',
                                              cascade='all, delete-orphan',
                                              order_by='AgeGroup.display_order')
    registrations           = db.relationship('Registration', back_populates='camp_session')
    group_assignments       = db.relationship('GroupAssignment', back_populates='camp_session',
                                              cascade='all, delete-orphan')
    announcements           = db.relationship('Announcement', back_populates='camp_session')
    checkin_logs            = db.relationship('CheckinLog', back_populates='camp_session')

    @property
    def total_confirmed(self):
        from sub_modules.models import Registration
        return Registration.query.filter_by(
            camp_session_id=self.id,
            status='confirmed'
        ).count()

    @property
    def total_waitlisted(self):
        from sub_modules.models import Registration
        return Registration.query.filter_by(
            camp_session_id=self.id,
            status='waitlisted'
        ).count()

    def is_age_eligible(self, child) -> tuple:
        '''
        Returns (eligible: bool, reason: str).
        Checks camp-level age limits, ignoring group-level age matching.
        '''
        age = child.age_on(self.start_date)
        if self.min_age_limit and age < self.min_age_limit:
            return False, f'Kind ist zu jung (Minimum: {self.min_age_limit} Jahre)'
        if self.max_age_limit and age > self.max_age_limit:
            return False, f'Kind ist zu alt (Maximum: {self.max_age_limit} Jahre)'
        return True, ''

    @property
    def is_camp_full(self):
        '''True if max_registrants is set and confirmed count has reached it.'''
        if not self.max_registrants:
            return False
        return self.total_confirmed >= self.max_registrants

    @property
    def is_registration_open(self):
        from datetime import datetime as _dt
        now = _dt.now()   # local time — matches what admin enters in the form
        if not self.registration_open:
            return False
        if self.registration_opens_at and now < self.registration_opens_at:
            return False
        if self.registration_closes_at and now > self.registration_closes_at:
            return False
        return True

    @property
    def camp_days(self):
        '''Returns list of (camp_day_number, date) tuples: [(1, Wed), (2, Thu), (3, Fri), (4, Sat)]'''
        from datetime import timedelta
        days = []
        current = self.start_date
        day_num = 1
        while current <= self.end_date:
            days.append((day_num, current))
            current += timedelta(days=1)
            day_num += 1
        return days

    def __repr__(self):
        return f'<CampSession {self.year} "{self.name}">'


# =============================================================================
# [4] AGE GROUPS
# =============================================================================

class AgeGroup(db.Model):
    __tablename__ = 'age_groups'

    id                  = db.Column(db.Integer, primary_key=True)
    camp_session_id     = db.Column(db.Integer, db.ForeignKey('camp_sessions.id', ondelete='CASCADE'),
                                    nullable=False)

    name                = db.Column(db.String(20), nullable=False)     # e.g. 'U10'
    min_age             = db.Column(db.Integer, nullable=False)        # inclusive, age on start_date
    max_age             = db.Column(db.Integer, nullable=False)        # inclusive
    capacity            = db.Column(db.Integer, nullable=False, default=20)
    display_order       = db.Column(db.Integer, nullable=False, default=0)

    created_at          = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at          = db.Column(db.DateTime, nullable=False, default=datetime.utcnow,
                                    onupdate=datetime.utcnow)

    __table_args__      = (db.UniqueConstraint('camp_session_id', 'name'),)

    # ------------------------------------------------------------------
    # Relationships
    camp_session        = db.relationship('CampSession', back_populates='age_groups')
    registrations       = db.relationship('Registration', back_populates='age_group')
    group_assignments   = db.relationship('GroupAssignment', back_populates='age_group',
                                          cascade='all, delete-orphan')

    @property
    def confirmed_count(self):
        return Registration.query.filter_by(
            age_group_id=self.id,
            status='confirmed'
        ).count()

    @property
    def waitlist_count(self):
        return Registration.query.filter_by(
            age_group_id=self.id,
            status='waitlisted'
        ).count()

    @property
    def spots_remaining(self):
        if not self.capacity:
            return None  # unlimited
        return max(0, self.capacity - self.confirmed_count)

    @property
    def is_full(self):
        if not self.capacity:  # 0 = no limit
            return False
        return self.confirmed_count >= self.capacity

    @property
    def head_coach(self):
        assignment = GroupAssignment.query.filter_by(
            age_group_id=self.id,
            is_head_coach=True
        ).first()
        return assignment.staff_user if assignment else None

    def __repr__(self):
        return f'<AgeGroup {self.name} session={self.camp_session_id}>'


# =============================================================================
# [5] GROUP ASSIGNMENTS
# Links staff to age groups. Trainers can float (multiple rows).
# Exactly one head coach per group — enforced in helpers.py, not here.
# =============================================================================

class GroupAssignment(db.Model):
    __tablename__ = 'group_assignments'

    id                  = db.Column(db.Integer, primary_key=True)
    camp_session_id     = db.Column(db.Integer, db.ForeignKey('camp_sessions.id', ondelete='CASCADE'),
                                    nullable=False)
    age_group_id        = db.Column(db.Integer, db.ForeignKey('age_groups.id', ondelete='CASCADE'),
                                    nullable=False)
    staff_user_id       = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='RESTRICT'),
                                    nullable=False)

    is_head_coach       = db.Column(db.Boolean, nullable=False, default=False)

    # Audit trail for head coach swaps
    assigned_at         = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    assigned_by_user_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='SET NULL'))

    __table_args__      = (db.UniqueConstraint('camp_session_id', 'age_group_id', 'staff_user_id'),)

    # ------------------------------------------------------------------
    # Relationships
    camp_session        = db.relationship('CampSession', back_populates='group_assignments')
    age_group           = db.relationship('AgeGroup', back_populates='group_assignments')
    staff_user          = db.relationship('User', back_populates='group_assignments',
                                          foreign_keys=[staff_user_id])
    assigned_by         = db.relationship('User', foreign_keys=[assigned_by_user_id])

    def __repr__(self):
        return f'<GroupAssignment group={self.age_group_id} staff={self.staff_user_id} head={self.is_head_coach}>'


# =============================================================================
# [6] CHILDREN
# =============================================================================

class Child(db.Model):
    __tablename__ = 'children'

    id                      = db.Column(db.Integer, primary_key=True)
    parent_user_id          = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='RESTRICT'),
                                        nullable=False)

    first_name              = db.Column(db.String(100), nullable=False)
    last_name               = db.Column(db.String(100), nullable=False)
    date_of_birth           = db.Column(db.Date, nullable=False)

    # Medical / safeguarding — visible to all staff
    medical_notes           = db.Column(db.Text)

    # Parent's general default photo consent preference
    # Confirmed explicitly per registration (see Registration.photo_consent)
    photo_consent_default        = db.Column(db.Boolean, nullable=False, default=False)
    independent_travel_default   = db.Column(db.Boolean, nullable=False, default=False)

    # GDPR soft delete
    is_deleted              = db.Column(db.Boolean, nullable=False, default=False)
    deleted_at              = db.Column(db.DateTime)

    created_at              = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at              = db.Column(db.DateTime, nullable=False, default=datetime.utcnow,
                                        onupdate=datetime.utcnow)

    # ------------------------------------------------------------------
    # Relationships
    parent                  = db.relationship('User', back_populates='children',
                                              foreign_keys=[parent_user_id])
    emergency_contacts      = db.relationship('EmergencyContact', back_populates='child',
                                              cascade='all, delete-orphan')
    registrations           = db.relationship('Registration', back_populates='child')
    qr_token                = db.relationship('QRToken', back_populates='child',
                                              uselist=False, cascade='all, delete-orphan')

    @property
    def full_name(self):
        return f'{self.first_name} {self.last_name}'

    def age_on(self, reference_date: date) -> int:
        '''
        Calculate age in years on a given reference date.
        Used for age group auto-assignment (reference_date = camp start_date).
        Edge case: birthday on the reference date counts as that age.
        '''
        dob = self.date_of_birth
        years = reference_date.year - dob.year
        # Subtract 1 if birthday hasn't occurred yet in reference year
        if (reference_date.month, reference_date.day) < (dob.month, dob.day):
            years -= 1
        return years

    @property
    def has_medical_notes(self):
        return bool(self.medical_notes and self.medical_notes.strip())

    def __repr__(self):
        return f'<Child {self.id} {self.full_name} dob={self.date_of_birth}>'


# =============================================================================
# [7] EMERGENCY CONTACTS
# =============================================================================

class EmergencyContact(db.Model):
    __tablename__ = 'emergency_contacts'

    id              = db.Column(db.Integer, primary_key=True)
    child_id        = db.Column(db.Integer, db.ForeignKey('children.id', ondelete='CASCADE'),
                                nullable=False)

    full_name       = db.Column(db.String(200), nullable=False)
    relationship    = db.Column(db.String(100))   # e.g. 'Mutter', 'Opa', 'Nachbar'
    phone_primary   = db.Column(db.String(30), nullable=False)
    phone_secondary = db.Column(db.String(30))

    created_at      = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at      = db.Column(db.DateTime, nullable=False, default=datetime.utcnow,
                                onupdate=datetime.utcnow)

    # ------------------------------------------------------------------
    # Relationships
    child           = db.relationship('Child', back_populates='emergency_contacts')

    def __repr__(self):
        return f'<EmergencyContact {self.full_name} for child={self.child_id}>'


# =============================================================================
# [8] REGISTRATIONS
# =============================================================================

class Registration(db.Model):
    __tablename__ = 'registrations'

    id                                  = db.Column(db.Integer, primary_key=True)
    child_id                            = db.Column(db.Integer, db.ForeignKey('children.id',
                                                    ondelete='RESTRICT'), nullable=False)
    camp_session_id                     = db.Column(db.Integer, db.ForeignKey('camp_sessions.id',
                                                    ondelete='RESTRICT'), nullable=False)
    age_group_id                        = db.Column(db.Integer, db.ForeignKey('age_groups.id',
                                                    ondelete='SET NULL'))

    # 'pending_verification' — manual entry, awaiting parent email confirmation
    # 'confirmed'            — registered and spot secured
    # 'waitlisted'           — registered but group full
    # 'cancelled'            — withdrawn
    # 'attended'             — marked attended after camp completes
    status                              = db.Column(db.String(20), nullable=False, default='confirmed')

    # Waitlist — managed manually by admin, no auto-promotion
    waitlist_position                   = db.Column(db.Integer)

    # Age group assignment
    auto_assigned_group                 = db.Column(db.String(20))
    admin_override                      = db.Column(db.Boolean, nullable=False, default=False)
    admin_override_reason               = db.Column(db.Text)
    admin_override_by                   = db.Column(db.Integer, db.ForeignKey('users.id',
                                                    ondelete='SET NULL'))
    admin_override_at                   = db.Column(db.DateTime)

    # Independent travel (darf alleine nach Hause gehen)
    independent_travel                  = db.Column(db.Boolean, nullable=False, default=False)
    independent_travel_approved_by      = db.Column(db.Integer, db.ForeignKey('users.id',
                                                    ondelete='SET NULL'))
    independent_travel_approved_at      = db.Column(db.DateTime)

    # Photo consent confirmed per registration year
    photo_consent                       = db.Column(db.Boolean, nullable=False, default=False)

    # Paper consent (recorded by admin from physical form)
    paper_consent_data_processing       = db.Column(db.Boolean, nullable=False, default=False)
    paper_consent_photo                 = db.Column(db.Boolean, nullable=False, default=False)
    paper_consent_independent_travel    = db.Column(db.Boolean, nullable=False, default=False)
    entered_by_user_id                  = db.Column(db.Integer, db.ForeignKey('users.id',
                                                    ondelete='SET NULL'))
    entered_at                          = db.Column(db.DateTime)

    # GDPR consent snapshot
    consent_version_at_registration     = db.Column(db.String(20))

    confirmation_sent_at                = db.Column(db.DateTime)

    registered_at                       = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at                          = db.Column(db.DateTime, nullable=False, default=datetime.utcnow,
                                                    onupdate=datetime.utcnow)

    __table_args__  = (db.UniqueConstraint('child_id', 'camp_session_id'),)

    # ------------------------------------------------------------------
    # Relationships
    child                               = db.relationship('Child', back_populates='registrations')
    camp_session                        = db.relationship('CampSession', back_populates='registrations')
    age_group                           = db.relationship('AgeGroup', back_populates='registrations')
    override_by_user                    = db.relationship('User', foreign_keys=[admin_override_by])
    travel_approved_by_user             = db.relationship('User',
                                                          foreign_keys=[independent_travel_approved_by])
    checkin_logs                        = db.relationship('CheckinLog', back_populates='registration')
    absence_reports                     = db.relationship('AbsenceReport', back_populates='registration',
                                                          cascade='all, delete-orphan')

    def absence_for_day(self, camp_day: int):
        '''Return the active (non-cancelled) AbsenceReport for this registration on a given camp day.'''
        from sub_modules.models import AbsenceReport
        return AbsenceReport.query.filter_by(
            registration_id=self.id,
            camp_day=camp_day,
            cancelled_at=None
        ).first()

    @property
    def is_confirmed(self):
        return self.status == 'confirmed'

    @property
    def is_waitlisted(self):
        return self.status == 'waitlisted'

    def checkin_for_day(self, camp_day: int):
        '''
        Returns the authoritative (earliest, non-voided) check-in log
        for this registration on a given camp day. None if not checked in.
        '''
        return CheckinLog.query.filter_by(
            registration_id=self.id,
            event_type='checkin',
            camp_day=camp_day,
            is_duplicate=False,
            voided=False
        ).first()

    def checkout_for_day(self, camp_day: int):
        '''
        Returns the non-voided checkout log for this registration on a given
        camp day. None if not yet checked out.
        '''
        return CheckinLog.query.filter_by(
            registration_id=self.id,
            event_type='checkout',
            camp_day=camp_day,
            voided=False
        ).first()

    def is_present_on_day(self, camp_day: int) -> bool:
        '''
        True if child has a valid check-in but no checkout on this day.
        Used for the end-of-day "still checked in" alert view.
        '''
        return (
            self.checkin_for_day(camp_day) is not None and
            self.checkout_for_day(camp_day) is None
        )

    def __repr__(self):
        return f'<Registration child={self.child_id} session={self.camp_session_id} status={self.status}>'


# =============================================================================
# [9] QR TOKENS
# =============================================================================

# =============================================================================
# [10] ABSENCE REPORTS
# Parent-submitted absence notifications visible to staff.
# One row per registration × camp day.
# =============================================================================

class AbsenceReport(db.Model):
    __tablename__ = 'absence_reports'

    id              = db.Column(db.Integer, primary_key=True)
    registration_id = db.Column(db.Integer, db.ForeignKey('registrations.id',
                                ondelete='CASCADE'), nullable=False)

    # Camp day number (1=Wednesday … 4=Saturday)
    camp_day        = db.Column(db.Integer, nullable=False)

    # 'sick' | 'other'
    reason          = db.Column(db.String(20), nullable=False)

    # Optional short parent note (e.g. "hat Fieber")
    note            = db.Column(db.String(300))

    reported_at     = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    # Null = report is active.  Set when parent cancels ("Kind kommt doch")
    cancelled_at    = db.Column(db.DateTime)

    __table_args__ = (
        db.UniqueConstraint('registration_id', 'camp_day',
                            name='uq_absence_registration_day'),
    )

    # ------------------------------------------------------------------
    # Relationships
    registration    = db.relationship('Registration', back_populates='absence_reports')

    @property
    def is_active(self):
        return self.cancelled_at is None

    @property
    def reason_label(self):
        return {'sick': 'Krank', 'other': 'Anderer Grund'}.get(self.reason, self.reason)

    def __repr__(self):
        return f'<AbsenceReport reg={self.registration_id} day={self.camp_day} reason={self.reason}>'


class QRToken(db.Model):
    __tablename__ = 'qr_tokens'

    id              = db.Column(db.Integer, primary_key=True)
    child_id        = db.Column(db.Integer, db.ForeignKey('children.id', ondelete='CASCADE'),
                                nullable=False, unique=True)
    token           = db.Column(db.String(255), nullable=False, unique=True)

    created_at      = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    regenerated_at  = db.Column(db.DateTime)

    # ------------------------------------------------------------------
    # Relationships
    child           = db.relationship('Child', back_populates='qr_token')

    def __repr__(self):
        return f'<QRToken child={self.child_id}>'


# =============================================================================
# [10] CHECKIN LOGS
# =============================================================================

class CheckinLog(db.Model):
    __tablename__ = 'checkin_logs'

    id                  = db.Column(db.Integer, primary_key=True)

    # Child event: registration_id set, staff_user_id = operator
    # Staff event: registration_id None, staff_user_id = the staff member
    registration_id     = db.Column(db.Integer, db.ForeignKey('registrations.id',
                                    ondelete='RESTRICT'))
    staff_user_id       = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='SET NULL'))

    camp_session_id     = db.Column(db.Integer, db.ForeignKey('camp_sessions.id',
                                    ondelete='RESTRICT'), nullable=False)

    # 'checkin' or 'checkout'
    event_type          = db.Column(db.String(10), nullable=False)

    # 1=Wednesday, 2=Thursday, 3=Friday, 4=Saturday
    camp_day            = db.Column(db.Integer, nullable=False)
    event_date          = db.Column(db.Date, nullable=False)
    event_time          = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    # 'search' or 'qr'
    method              = db.Column(db.String(10), nullable=False, default='search')

    # Only earliest check-in per day is authoritative
    is_duplicate        = db.Column(db.Boolean, nullable=False, default=False)

    # Void handling — never hard deleted, kept for audit
    voided              = db.Column(db.Boolean, nullable=False, default=False)
    voided_by_user_id   = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='SET NULL'))
    voided_at           = db.Column(db.DateTime)
    void_reason         = db.Column(db.Text)

    # True = inserted by APScheduler auto-checkout job, not a real departure
    is_auto_checkout    = db.Column(db.Boolean, nullable=False, default=False)

    notes               = db.Column(db.Text)

    # ------------------------------------------------------------------
    # Relationships
    registration        = db.relationship('Registration', back_populates='checkin_logs')
    staff_user          = db.relationship('User', foreign_keys=[staff_user_id])
    camp_session        = db.relationship('CampSession', back_populates='checkin_logs')
    voided_by           = db.relationship('User', foreign_keys=[voided_by_user_id])

    @property
    def is_staff_event(self):
        return self.registration_id is None

    @property
    def is_child_event(self):
        return self.registration_id is not None

    def __repr__(self):
        target = f'reg={self.registration_id}' if self.is_child_event else f'staff={self.staff_user_id}'
        return f'<CheckinLog {self.event_type} {target} day={self.camp_day}>'


# =============================================================================
# [11] ANNOUNCEMENTS
# =============================================================================

class Announcement(db.Model):
    __tablename__ = 'announcements'

    id                  = db.Column(db.Integer, primary_key=True)
    author_user_id      = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='RESTRICT'),
                                    nullable=False)
    camp_session_id     = db.Column(db.Integer, db.ForeignKey('camp_sessions.id', ondelete='SET NULL'))

    title               = db.Column(db.String(255), nullable=False)
    body                = db.Column(db.Text, nullable=False)

    # 'public' or 'staff'
    visibility          = db.Column(db.String(10), nullable=False, default='public')

    # Comma-separated age group names e.g. 'U8,U10' — None means all groups
    target_age_groups   = db.Column(db.String(100))

    # Comma-separated staff tags e.g. 'trainer,first_aid' — None for public posts
    staff_tags          = db.Column(db.String(100))

    is_pinned           = db.Column(db.Boolean, nullable=False, default=False)
    photo_s3_key        = db.Column(db.String(255))

    @property
    def image_url(self):
        '''Return a URL for the announcement photo, or None.'''
        if not self.photo_s3_key:
            return None
        from flask import current_app
        s3_loc = current_app.config.get('S3_LOCATION')
        if s3_loc:
            return f'{s3_loc}{self.photo_s3_key}'
        # Local dev fallback path
        return f'/static/uploads/{self.photo_s3_key}'
    email_sent_at       = db.Column(db.DateTime)

    created_at          = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at          = db.Column(db.DateTime, nullable=False, default=datetime.utcnow,
                                    onupdate=datetime.utcnow)

    # ------------------------------------------------------------------
    # Relationships
    author              = db.relationship('User', back_populates='announcements',
                                          foreign_keys=[author_user_id])
    camp_session        = db.relationship('CampSession', back_populates='announcements')

    @property
    def is_staff_only(self):
        return self.visibility == 'staff'

    @property
    def is_open_public(self):
        '''Visible without login — shown on public homepage/feed.'''
        return self.visibility == 'open'

    @property
    def target_groups_list(self):
        '''Returns list of group name strings, or empty list if targeting all.'''
        if not self.target_age_groups:
            return []
        return [g.strip() for g in self.target_age_groups.split(',')]

    @property
    def staff_tags_list(self):
        if not self.staff_tags:
            return []
        return [t.strip() for t in self.staff_tags.split(',')]

    def is_visible_to(self, user) -> bool:
        '''
        Check if this announcement should be visible to a given user.
        user may be None (unauthenticated) for open/public announcements.
        '''
        if self.is_open_public:
            return True   # anyone, including unauthenticated

        if user is None:
            return False  # everything else requires login

        if self.is_staff_only:
            return user.is_staff

        # 'public' — all logged-in users; check age group targeting
        if not self.target_groups_list:
            return True

        if user.is_staff:
            return True

        for child in user.children:
            for reg in child.registrations:
                if reg.age_group and reg.age_group.name in self.target_groups_list:
                    return True
        return False

    def __repr__(self):
        return f'<Announcement {self.id} "{self.title}" [{self.visibility}]>'


# =============================================================================
# [12] GDPR CONSENT VERSIONS
# =============================================================================

class ConsentVersion(db.Model):
    __tablename__ = 'consent_versions'

    version         = db.Column(db.String(20), primary_key=True)
    summary         = db.Column(db.Text, nullable=False)
    full_text       = db.Column(db.Text, nullable=False)
    effective_date  = db.Column(db.Date, nullable=False)
    created_at      = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    def __repr__(self):
        return f'<ConsentVersion {self.version} effective={self.effective_date}>'


# =============================================================================
# [13] EMAIL LOG
# =============================================================================

class EmailLog(db.Model):
    __tablename__ = 'email_log'

    id                  = db.Column(db.Integer, primary_key=True)
    recipient_user_id   = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='SET NULL'))
    recipient_email     = db.Column(db.String(255), nullable=False)

    # 'verification', 'confirmation', 'reminder', 'announcement',
    # 'deletion_warning', 'invite'
    email_type          = db.Column(db.String(30), nullable=False)

    subject             = db.Column(db.String(255))
    sendgrid_message_id = db.Column(db.String(255))

    # 'sent', 'failed', 'bounced'
    status              = db.Column(db.String(20), nullable=False, default='sent')

    sent_at             = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    # ------------------------------------------------------------------
    # Relationships
    recipient_user      = db.relationship('User', back_populates='email_logs',
                                          foreign_keys=[recipient_user_id])

    def __repr__(self):
        return f'<EmailLog {self.email_type} to={self.recipient_email} [{self.status}]>'


# =============================================================================
# [14] ANALYTICS EVENTS
#
# NOTE: All analytics events are used exclusively for internal site
# performance monitoring and improvement. No personal data is ever
# stored here. User identity is role only ('parent', 'staff', 'admin',
# 'anonymous') — never a user ID, name, or email address. Device type
# is inferred and the User-Agent string is immediately discarded.
# No cookies are used. No GDPR consent banner is required for this data.
# =============================================================================

class AnalyticsEvent(db.Model):
    __tablename__ = 'analytics_events'

    id              = db.Column(db.Integer, primary_key=True)

    # What happened
    # Page views:    'page_view'
    # Auth:          'login_success', 'login_fail', 'register', 'logout'
    # Registration:  'child_register_confirmed', 'child_register_waitlisted'
    # Check-in:      'checkin', 'checkout', 'checkin_void'
    # Announcements: 'announcement_view', 'announcement_create'
    # Admin:         'group_split_saved', 'head_coach_assigned', 'invite_sent'
    # Errors:        'error_404', 'error_403', 'error_500'
    event_type      = db.Column(db.String(40), nullable=False)

    # Page / action
    route           = db.Column(db.String(100))     # e.g. '/team/checkin'
    method          = db.Column(db.String(10))       # GET / POST

    # Who — role only, never user ID
    user_role       = db.Column(db.String(20))       # 'parent','staff','admin','anonymous'

    # Device — inferred from User-Agent header, UA string itself never stored
    device_type     = db.Column(db.String(10))       # 'mobile', 'tablet', 'desktop'

    # Outcome
    status_code     = db.Column(db.Integer)          # HTTP response code
    success         = db.Column(db.Boolean)          # for action events

    # Optional non-identifying context
    # e.g. 'qr' or 'search' for checkin method, age group name, error type
    detail          = db.Column(db.String(100))

    # Performance
    response_ms     = db.Column(db.Integer)          # server-side response time ms

    occurred_at     = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    def __repr__(self):
        return f'<AnalyticsEvent {self.event_type} {self.route} {self.occurred_at}>'


# =============================================================================
# [15] ERROR LOG
# Application error tracking — separate from analytics.
# Analytics answers "how often". Error log answers "why".
# Survives server restarts (unlike text log files on Render/Railway).
# No personal data stored — user role only, never user ID.
# =============================================================================

class ErrorLog(db.Model):
    __tablename__ = 'error_log'

    id          = db.Column(db.Integer, primary_key=True)

    # Error classification
    # 'http_404', 'http_403', 'http_500', 'db_error',
    # 'email_fail', 's3_error', 'scheduler_error', 'auth_error'
    error_type  = db.Column(db.String(30), nullable=False)

    # Where it happened
    route       = db.Column(db.String(100))
    method      = db.Column(db.String(10))

    # Who was affected — role only, never user ID or personal data
    user_role   = db.Column(db.String(20))

    # What went wrong — exception message or description
    # Scrubbed of any personal data before storing
    message     = db.Column(db.Text)

    # HTTP status code if applicable
    status_code = db.Column(db.Integer)

    occurred_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    def __repr__(self):
        return f'<ErrorLog {self.error_type} {self.route} {self.occurred_at}>'


# =============================================================================
# [16] BUG REPORTS / FEEDBACK
# Submitted by any authenticated user via the settings menu.
# All text fields are HTML-stripped before storage (see helpers.sanitize_text).
# Reporter identity preserved by role + stored email at submission time,
# so the report remains useful even if the account is later deleted.
# =============================================================================

class BugReport(db.Model):
    __tablename__ = 'bug_reports'

    id                  = db.Column(db.Integer, primary_key=True)

    # Reporter — user_id nullable in case account is later deleted
    reporter_user_id    = db.Column(db.Integer,
                            db.ForeignKey('users.id', ondelete='SET NULL'),
                            nullable=True)
    # Stored at submission time so report stays useful after account deletion
    reporter_email      = db.Column(db.String(255), nullable=False)
    reporter_role       = db.Column(db.String(20),  nullable=False)

    # Report content — all sanitized before storage
    subject             = db.Column(db.String(120), nullable=False)
    description         = db.Column(db.Text,        nullable=False)

    # Where they were when they submitted — relative path only, validated
    page_url            = db.Column(db.String(200))

    # User-selected severity
    # 'low'    — minor annoyance, cosmetic issue
    # 'medium' — something doesn't work as expected
    # 'high'   — blocking issue, data looks wrong
    severity            = db.Column(db.String(10), nullable=False, default='medium')

    # Admin-managed status
    # 'new'          — just submitted, not yet reviewed
    # 'acknowledged' — admin has seen it
    # 'in_progress'  — being worked on
    # 'resolved'     — fixed
    # 'wontfix'      — known limitation or not actionable
    status              = db.Column(db.String(20), nullable=False, default='new')

    # Internal admin notes — also sanitized on save
    admin_notes         = db.Column(db.Text)
    resolved_by_user_id = db.Column(db.Integer,
                            db.ForeignKey('users.id', ondelete='SET NULL'),
                            nullable=True)
    resolved_at         = db.Column(db.DateTime)

    created_at          = db.Column(db.DateTime, nullable=False,
                            default=datetime.utcnow)
    updated_at          = db.Column(db.DateTime, nullable=False,
                            default=datetime.utcnow, onupdate=datetime.utcnow)

    # ------------------------------------------------------------------
    # Relationships
    reporter            = db.relationship('User',
                            foreign_keys=[reporter_user_id],
                            backref='bug_reports')
    resolved_by         = db.relationship('User',
                            foreign_keys=[resolved_by_user_id])

    @property
    def is_open(self):
        return self.status in ('new', 'acknowledged', 'in_progress')

    @property
    def severity_label(self):
        return {'low': 'Gering', 'medium': 'Mittel', 'high': 'Hoch'}.get(
            self.severity, self.severity
        )

    @property
    def status_label(self):
        return {
            'new':          'Neu',
            'acknowledged': 'Gesehen',
            'in_progress':  'In Bearbeitung',
            'resolved':     'Gelöst',
            'wontfix':      'Kein Fix geplant',
        }.get(self.status, self.status)

    def __repr__(self):
        return f'<BugReport {self.id} [{self.severity}/{self.status}] "{self.subject[:40]}">'
