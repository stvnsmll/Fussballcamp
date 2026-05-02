'''
sub_modules/seed.py
===================
Development seed data. Creates a realistic dataset for local testing.

Usage:
    flask seed          # create fresh seed data (drops existing)
    flask seed --keep   # add seed data without dropping

Creates:
    - 1 admin account         admin@example.com / admin1234
    - 5 staff accounts        trainer1@example.com / staff1234 etc.
    - 30 parent accounts      with 1-3 children each
    - 1 camp session          this year, registration open
    - Age groups seeded from DEFAULT_AGE_GROUPS
    - Registrations spread across groups (some waitlisted, some pending)
    - Sample announcements (public + staff-only)
    - Sample check-in events for today

All data is fake (Faker library). No real personal data.
'''

import random
from datetime import date, datetime, timedelta

import click
from faker import Faker
from flask import current_app
from flask.cli import with_appcontext

from application import db
from sub_modules.models import (User, StaffProfile, Child, EmergencyContact,
                                Registration, CampSession, AgeGroup,
                                QRToken, Announcement, ConsentVersion)
from sub_modules.helpers import (hash_password, assign_age_group,
                                 get_or_create_qr_token,
                                 get_next_waitlist_position)
from sub_modules.config import (DEFAULT_AGE_GROUPS, CURRENT_CONSENT_VERSION,
                                STAFF_CLASSES)

fake = Faker('de_DE')
random.seed(42)


def register_seed_commands(app):
    '''Register the `flask seed` CLI command with the app.'''
    app.cli.add_command(seed_command)


@click.command('seed')
@click.option('--keep', is_flag=True, default=False,
              help='Keep existing data (do not drop tables first)')
@click.option('--parents', default=50, show_default=True,
              help='Number of parent accounts to create')
@click.option('--add-kids', default=0,
              help='Add N extra children to existing parents without resetting')
@with_appcontext
def seed_command(keep, parents, add_kids):
    '''Seed the database with development data.'''

    # --add-kids: top up children without touching anything else
    if add_kids > 0:
        camp = CampSession.query.filter(
            CampSession.status.in_(['open', 'active', 'upcoming'])
        ).order_by(CampSession.start_date.desc()).first()
        if not camp:
            click.echo('No active camp found. Run flask seed first.', err=True)
            return
        existing_parents = User.query.filter_by(role='parent').all()
        if not existing_parents:
            click.echo('No parents found. Run flask seed first.', err=True)
            return
        added = _add_extra_children(add_kids, existing_parents, camp)
        db.session.commit()
        click.echo(f'Added {added} children to existing parents.')
        return

    if not keep:
        click.echo('Dropping and recreating all tables...')
        db.drop_all()
        db.create_all()

    click.echo('Seeding...')
    _seed_consent_version()
    camp = _seed_camp_session()
    admin = _seed_admin()
    staff_users = _seed_staff(5)
    parent_list = _seed_parents(parents, camp)
    _seed_announcements(camp, admin)
    _seed_sample_checkins(camp)
    db.session.commit()

    total_children = Child.query.count()
    click.echo(f'''
Seed complete.
  Admin:    admin@example.com  / admin1234
  Staff:    trainer1@example.com ... trainer5@example.com  / staff1234
  Parents:  {len(parent_list)} accounts
  Children: {total_children} total
  Camp:     {camp.name} ({camp.start_date} – {camp.end_date})

To add more kids later without resetting:
  flask seed --add-kids 20
''')


# =============================================================================
# SEEDERS
# =============================================================================

def _seed_consent_version():
    cv = ConsentVersion(
        version=CURRENT_CONSENT_VERSION,
        summary='Erstveröffentlichung der Datenschutzerklärung.',
        full_text='[Vollständiger Text der Datenschutzerklärung hier einfügen]',
        effective_date=date(2025, 1, 1),
    )
    db.session.add(cv)
    db.session.flush()


def _seed_camp_session() -> CampSession:
    year = date.today().year
    # Camp starts on the first Wednesday in August
    aug_1 = date(year, 8, 1)
    days_until_wed = (2 - aug_1.weekday()) % 7
    start = aug_1 + timedelta(days=days_until_wed)
    end   = start + timedelta(days=3)  # Wed–Sat

    camp = CampSession(
        name=f'Fußballcamp {year}',
        year=year,
        start_date=start,
        end_date=end,
        location='Sportplatz Hauptstraße',
        description='Das kostenlose Fußballcamp für alle Kinder aus der Region!',
        status='open',
        registration_open=True,
        require_head_coaches=True,
    )
    db.session.add(camp)
    db.session.flush()

    for ag_def in DEFAULT_AGE_GROUPS:
        ag = AgeGroup(
            camp_session_id=camp.id,
            name=ag_def['name'],
            min_age=ag_def['min_age'],
            max_age=ag_def['max_age'],
            capacity=ag_def['capacity'],
            display_order=ag_def['display_order'],
        )
        db.session.add(ag)

    db.session.flush()
    return camp


def _seed_admin() -> User:
    admin = User(
        email='admin@example.com',
        password_hash=hash_password('admin1234'),
        first_name='Admin',
        last_name='Mustermann',
        role='admin',
        email_verified=True,
        is_active=True,
        consent_given_at=datetime.utcnow(),
        consent_version=CURRENT_CONSENT_VERSION,
    )
    db.session.add(admin)
    db.session.flush()

    profile = StaffProfile(
        user_id=admin.id,
        staff_class='trainer',
        is_first_aid=True,
    )
    db.session.add(profile)
    db.session.flush()
    return admin


def _seed_staff(n: int) -> list:
    staff_users = []
    classes = ['trainer', 'trainer', 'trainer', 'general', 'food']

    for i in range(1, n + 1):
        user = User(
            email=f'trainer{i}@example.com',
            password_hash=hash_password('staff1234'),
            first_name=fake.first_name(),
            last_name=fake.last_name(),
            role='staff',
            email_verified=True,
            is_active=True,
            consent_given_at=datetime.utcnow(),
            consent_version=CURRENT_CONSENT_VERSION,
        )
        db.session.add(user)
        db.session.flush()

        profile = StaffProfile(
            user_id=user.id,
            staff_class=classes[i - 1],
            is_first_aid=(i == 1),
        )
        db.session.add(profile)
        staff_users.append(user)

    db.session.flush()
    return staff_users


def _seed_parents(n: int, camp: CampSession) -> list:
    age_groups = {ag.id: ag for ag in camp.age_groups}
    parents = []

    for _ in range(n):
        parent = User(
            email=fake.unique.email(),
            password_hash=hash_password('parent1234'),
            first_name=fake.first_name(),
            last_name=fake.last_name(),
            phone=fake.phone_number(),
            address_street=fake.street_address(),
            address_city=fake.city(),
            address_postcode=fake.postcode(),
            role='parent',
            email_verified=True,
            is_active=True,
            consent_given_at=datetime.utcnow(),
            consent_version=CURRENT_CONSENT_VERSION,
        )
        db.session.add(parent)
        db.session.flush()

        num_children = random.choices([1, 2, 3], weights=[5, 3, 2])[0]

        for _ in range(num_children):
            # Random DOB to cover all age groups (5–16 years old)
            age_years = random.randint(4, 16)
            dob = camp.start_date - timedelta(days=age_years * 365 + random.randint(0, 364))

            child = Child(
                parent_user_id=parent.id,
                first_name=fake.first_name(),
                last_name=parent.last_name,
                date_of_birth=dob,
                medical_notes=fake.sentence() if random.random() < 0.15 else None,
                photo_consent_default=random.choice([True, False]),
            )
            db.session.add(child)
            db.session.flush()

            # Emergency contact
            ec = EmergencyContact(
                child_id=child.id,
                full_name=fake.name(),
                relationship=random.choice(['Mutter', 'Vater', 'Oma', 'Opa']),
                phone_primary=fake.phone_number(),
            )
            db.session.add(ec)

            # QR token
            qr = QRToken(
                child_id=child.id,
                token=__import__('secrets').token_urlsafe(32)
            )
            db.session.add(qr)
            db.session.flush()

            # Register if eligible
            age_group = assign_age_group(child, camp)
            if not age_group:
                continue

            is_full = age_group.confirmed_count >= age_group.capacity * 0.9
            if is_full:
                status = 'waitlisted'
                pos = get_next_waitlist_position(camp.id, age_group.id)
            else:
                status = 'confirmed'
                pos = None

            reg = Registration(
                child_id=child.id,
                camp_session_id=camp.id,
                age_group_id=age_group.id,
                status=status,
                waitlist_position=pos,
                auto_assigned_group=age_group.name,
                independent_travel=random.choice([True, False]),
                photo_consent=child.photo_consent_default,
                consent_version_at_registration=CURRENT_CONSENT_VERSION,
            )
            db.session.add(reg)

        parents.append(parent)

    db.session.flush()
    return parents


def _seed_announcements(camp: CampSession, admin: User):
    announcements = [
        Announcement(
            author_user_id=admin.id,
            camp_session_id=camp.id,
            title='Willkommen beim Fußballcamp!',
            body='Wir freuen uns auf ein tolles Camp mit euch. '
                 'Bitte bringt Sportkleidung und ausreichend Trinken mit.',
            visibility='public',
            is_pinned=True,
        ),
        Announcement(
            author_user_id=admin.id,
            camp_session_id=camp.id,
            title='Wichtig: Sonnenschutz nicht vergessen!',
            body='Der Wetterbericht sagt viel Sonne voraus. '
                 'Bitte Sonnencreme und Kopfbedeckung mitbringen.',
            visibility='public',
            is_pinned=False,
        ),
        Announcement(
            author_user_id=admin.id,
            camp_session_id=camp.id,
            title='Trainer-Meeting Dienstagabend',
            body='Alle Trainer treffen sich am Dienstag um 19:00 Uhr '
                 'in der Vereinsgaststätte zur Vorbesprechung.',
            visibility='staff',
            is_pinned=False,
        ),
    ]
    for a in announcements:
        db.session.add(a)
    db.session.flush()


def _seed_sample_checkins(camp: CampSession):
    '''Add check-in events for today if today is a camp day.'''
    from sub_modules.models import CheckinLog
    from sub_modules.helpers import get_camp_day_number

    today = date.today()
    camp_day = get_camp_day_number(camp, today)
    if not camp_day:
        return  # not a camp day, skip

    confirmed = Registration.query.filter_by(
        camp_session_id=camp.id,
        status='confirmed'
    ).all()

    # Check in ~70% of confirmed children
    for reg in random.sample(confirmed, k=int(len(confirmed) * 0.7)):
        checkin = CheckinLog(
            registration_id=reg.id,
            camp_session_id=camp.id,
            event_type='checkin',
            event_time=datetime.combine(
                today,
                __import__('datetime').time(
                    random.randint(8, 10),
                    random.randint(0, 59)
                )
            ),
            event_date=today,
            camp_day=camp_day,
            method=random.choice(['search', 'qr']),
        )
        db.session.add(checkin)

def _add_extra_children(n: int, parents: list, camp: CampSession) -> int:
    '''
    Add n extra children distributed across existing parent accounts.
    Useful for bumping up participant numbers without a full reseed.
    '''
    added = 0
    for _ in range(n):
        parent = random.choice(parents)
        age_years = random.randint(4, 16)
        dob = camp.start_date - timedelta(days=age_years * 365 + random.randint(0, 364))

        child = Child(
            parent_user_id=parent.id,
            first_name=fake.first_name(),
            last_name=parent.last_name,
            date_of_birth=dob,
            medical_notes=fake.sentence() if random.random() < 0.15 else None,
            photo_consent_default=random.choice([True, False]),
        )
        db.session.add(child)
        db.session.flush()

        ec = EmergencyContact(
            child_id=child.id,
            full_name=fake.name(),
            relationship=random.choice(['Mutter', 'Vater', 'Oma', 'Opa']),
            phone_primary=fake.phone_number(),
        )
        db.session.add(ec)

        qr = QRToken(
            child_id=child.id,
            token=__import__('secrets').token_urlsafe(32)
        )
        db.session.add(qr)
        db.session.flush()

        age_group = assign_age_group(child, camp)
        if not age_group:
            continue

        is_full = age_group.confirmed_count >= age_group.capacity * 0.9
        status = 'waitlisted' if is_full else 'confirmed'
        pos = get_next_waitlist_position(camp.id, age_group.id) if is_full else None

        reg = Registration(
            child_id=child.id,
            camp_session_id=camp.id,
            age_group_id=age_group.id,
            status=status,
            waitlist_position=pos,
            auto_assigned_group=age_group.name,
            independent_travel=random.choice([True, False]),
            photo_consent=child.photo_consent_default,
            consent_version_at_registration=CURRENT_CONSENT_VERSION,
        )
        db.session.add(reg)
        added += 1

    return added
