"""
tests/test_roster.py
====================
Integration tests for the printable roster feature.

Covers:
  - GET /admin/camps/<id>/teilnehmerliste: full roster for admin
  - GET /admin/camps/<id>/teilnehmerliste: blocked for staff and parent
  - GET /team/gruppe/<id>/drucken: per-group roster for staff
  - GET /team/gruppe/<id>/drucken: blocked for parent
  - Correct children appear / are excluded (confirmed only, not waitlisted)
  - Emergency contact data appears
  - Independent travel badge appears when flag set
  - Empty group handled gracefully
  - Multi-group ordering in full roster
"""

import pytest
from datetime import date


def _session(client, user):
    with client.session_transaction() as sess:
        sess['_user_id'] = str(user.id)
        sess['_fresh'] = True


def _make_camp(db, status='published'):
    from sub_modules.models import CampSession
    c = CampSession(name='Testcamp 2023', year=2023,
                    start_date=date(2023, 7, 19),
                    end_date=date(2023, 7, 22),
                    status=status)
    db.session.add(c)
    db.session.flush()
    return c


def _make_group(db, camp, name='U10', min_age=8, max_age=9, capacity=20):
    from sub_modules.models import AgeGroup
    g = AgeGroup(camp_session_id=camp.id, name=name,
                 min_age=min_age, max_age=max_age, capacity=capacity)
    db.session.add(g)
    db.session.flush()
    return g


def _make_child(db, parent, first='Kind', last='Muster'):
    from sub_modules.models import Child
    c = Child(parent_user_id=parent.id,
              first_name=first, last_name=last,
              date_of_birth=date(2014, 3, 1))
    db.session.add(c)
    db.session.flush()
    return c


def _make_emergency_contact(db, child, name='Erika Muster', phone='0171 999999'):
    from sub_modules.models import EmergencyContact
    ec = EmergencyContact(child_id=child.id,
                          full_name=name,
                          relationship='Mutter',
                          phone_primary=phone)
    db.session.add(ec)
    db.session.flush()
    return ec


def _make_registration(db, child, camp, group, status='confirmed',
                       independent_travel=False):
    from sub_modules.models import Registration
    r = Registration(child_id=child.id, camp_session_id=camp.id,
                     age_group_id=group.id, status=status,
                     independent_travel=independent_travel)
    db.session.add(r)
    db.session.flush()
    return r


# =============================================================================
# Full camp roster — admin access control
# =============================================================================

class TestFullRosterAccessControl:

    def test_admin_can_access(self, client, admin, db):
        _session(client, admin)
        camp = _make_camp(db)
        db.session.commit()
        resp = client.get(f'/admin/camps/{camp.id}/teilnehmerliste')
        assert resp.status_code == 200

    def test_staff_blocked(self, client, staff, db):
        _session(client, staff)
        camp = _make_camp(db)
        db.session.commit()
        resp = client.get(f'/admin/camps/{camp.id}/teilnehmerliste')
        assert resp.status_code == 403

    def test_parent_blocked(self, client, parent, db):
        _session(client, parent)
        camp = _make_camp(db)
        db.session.commit()
        resp = client.get(f'/admin/camps/{camp.id}/teilnehmerliste')
        assert resp.status_code == 403

    def test_unauthenticated_redirected(self, client, db):
        camp = _make_camp(db)
        db.session.commit()
        resp = client.get(f'/admin/camps/{camp.id}/teilnehmerliste')
        assert resp.status_code == 302


# =============================================================================
# Full camp roster — content
# =============================================================================

class TestFullRosterContent:

    def test_shows_confirmed_child(self, client, admin, parent, db):
        _session(client, admin)
        camp = _make_camp(db)
        group = _make_group(db, camp)
        child = _make_child(db, parent, last='Schumacher')
        _make_registration(db, child, camp, group, status='confirmed')
        db.session.commit()
        resp = client.get(f'/admin/camps/{camp.id}/teilnehmerliste')
        assert resp.status_code == 200
        assert 'Schumacher' in resp.get_data(as_text=True)

    def test_excludes_waitlisted_child(self, client, admin, parent, db):
        _session(client, admin)
        camp = _make_camp(db)
        group = _make_group(db, camp)
        child = _make_child(db, parent, last='Wartend')
        _make_registration(db, child, camp, group, status='waitlisted')
        db.session.commit()
        resp = client.get(f'/admin/camps/{camp.id}/teilnehmerliste')
        assert 'Wartend' not in resp.get_data(as_text=True)

    def test_excludes_cancelled_child(self, client, admin, parent, db):
        _session(client, admin)
        camp = _make_camp(db)
        group = _make_group(db, camp)
        child = _make_child(db, parent, last='Storniert')
        _make_registration(db, child, camp, group, status='cancelled')
        db.session.commit()
        resp = client.get(f'/admin/camps/{camp.id}/teilnehmerliste')
        assert 'Storniert' not in resp.get_data(as_text=True)

    def test_shows_emergency_contact(self, client, admin, parent, db):
        _session(client, admin)
        camp = _make_camp(db)
        group = _make_group(db, camp)
        child = _make_child(db, parent)
        _make_emergency_contact(db, child, name='Notfallkontakt Muster',
                                phone='0151 888888')
        _make_registration(db, child, camp, group)
        db.session.commit()
        html = client.get(f'/admin/camps/{camp.id}/teilnehmerliste').get_data(as_text=True)
        assert 'Notfallkontakt Muster' in html
        assert '0151 888888' in html

    def test_shows_independent_travel_badge(self, client, admin, parent, db):
        _session(client, admin)
        camp = _make_camp(db)
        group = _make_group(db, camp)
        child = _make_child(db, parent)
        _make_registration(db, child, camp, group, independent_travel=True)
        db.session.commit()
        html = client.get(f'/admin/camps/{camp.id}/teilnehmerliste').get_data(as_text=True)
        assert 'travel-badge' in html

    def test_no_travel_shows_nein(self, client, admin, parent, db):
        _session(client, admin)
        camp = _make_camp(db)
        group = _make_group(db, camp)
        child = _make_child(db, parent)
        _make_registration(db, child, camp, group, independent_travel=False)
        db.session.commit()
        html = client.get(f'/admin/camps/{camp.id}/teilnehmerliste').get_data(as_text=True)
        assert 'Nein' in html

    def test_shows_total_count(self, client, admin, parent, db):
        _session(client, admin)
        camp = _make_camp(db)
        group = _make_group(db, camp)
        c1 = _make_child(db, parent, last='Alpha')
        c2 = _make_child(db, parent, last='Beta')
        _make_registration(db, c1, camp, group)
        _make_registration(db, c2, camp, group)
        db.session.commit()
        html = client.get(f'/admin/camps/{camp.id}/teilnehmerliste').get_data(as_text=True)
        assert '2' in html

    def test_multiple_groups_all_appear(self, client, admin, parent, db):
        _session(client, admin)
        camp = _make_camp(db)
        u8 = _make_group(db, camp, 'U8', min_age=6, max_age=7)
        u10 = _make_group(db, camp, 'U10', min_age=8, max_age=9)
        c1 = _make_child(db, parent, last='Jung')
        c2 = _make_child(db, parent, last='Gross')
        _make_registration(db, c1, camp, u8)
        _make_registration(db, c2, camp, u10)
        db.session.commit()
        html = client.get(f'/admin/camps/{camp.id}/teilnehmerliste').get_data(as_text=True)
        assert 'U8' in html
        assert 'U10' in html
        assert 'Jung' in html
        assert 'Gross' in html

    def test_empty_camp_renders_gracefully(self, client, admin, db):
        _session(client, admin)
        camp = _make_camp(db)
        db.session.commit()
        resp = client.get(f'/admin/camps/{camp.id}/teilnehmerliste')
        assert resp.status_code == 200

    def test_empty_group_renders_gracefully(self, client, admin, db):
        _session(client, admin)
        camp = _make_camp(db)
        _make_group(db, camp)   # group with no registrations
        db.session.commit()
        resp = client.get(f'/admin/camps/{camp.id}/teilnehmerliste')
        assert resp.status_code == 200
        assert 'Keine bestätigten' in resp.get_data(as_text=True)


# =============================================================================
# Per-group staff roster — access control
# =============================================================================

class TestGroupRosterAccessControl:

    def test_staff_can_access(self, client, staff, db):
        _session(client, staff)
        camp = _make_camp(db)
        group = _make_group(db, camp)
        db.session.commit()
        resp = client.get(f'/team/gruppe/{group.id}/drucken')
        assert resp.status_code == 200

    def test_admin_can_access(self, client, admin, db):
        _session(client, admin)
        camp = _make_camp(db)
        group = _make_group(db, camp)
        db.session.commit()
        resp = client.get(f'/team/gruppe/{group.id}/drucken')
        assert resp.status_code == 200

    def test_parent_blocked(self, client, parent, db):
        _session(client, parent)
        camp = _make_camp(db)
        group = _make_group(db, camp)
        db.session.commit()
        resp = client.get(f'/team/gruppe/{group.id}/drucken')
        assert resp.status_code == 403

    def test_unauthenticated_redirected(self, client, db):
        camp = _make_camp(db)
        group = _make_group(db, camp)
        db.session.commit()
        resp = client.get(f'/team/gruppe/{group.id}/drucken')
        assert resp.status_code == 302


# =============================================================================
# Per-group staff roster — content
# =============================================================================

class TestGroupRosterContent:

    def test_shows_confirmed_children(self, client, staff, parent, db):
        _session(client, staff)
        camp = _make_camp(db)
        group = _make_group(db, camp)
        child = _make_child(db, parent, last='Meier')
        _make_registration(db, child, camp, group, status='confirmed')
        db.session.commit()
        html = client.get(f'/team/gruppe/{group.id}/drucken').get_data(as_text=True)
        assert 'Meier' in html

    def test_excludes_waitlisted(self, client, staff, parent, db):
        _session(client, staff)
        camp = _make_camp(db)
        group = _make_group(db, camp)
        child = _make_child(db, parent, last='Wartend')
        _make_registration(db, child, camp, group, status='waitlisted')
        db.session.commit()
        html = client.get(f'/team/gruppe/{group.id}/drucken').get_data(as_text=True)
        assert 'Wartend' not in html

    def test_shows_emergency_contact_name_and_phone(self, client, staff, parent, db):
        _session(client, staff)
        camp = _make_camp(db)
        group = _make_group(db, camp)
        child = _make_child(db, parent)
        _make_emergency_contact(db, child, name='Peter Notfall', phone='0172 123456')
        _make_registration(db, child, camp, group)
        db.session.commit()
        html = client.get(f'/team/gruppe/{group.id}/drucken').get_data(as_text=True)
        assert 'Peter Notfall' in html
        assert '0172 123456' in html

    def test_child_with_no_emergency_contact(self, client, staff, parent, db):
        _session(client, staff)
        camp = _make_camp(db)
        group = _make_group(db, camp)
        child = _make_child(db, parent, last='OhneKontakt')
        _make_registration(db, child, camp, group)
        db.session.commit()
        html = client.get(f'/team/gruppe/{group.id}/drucken').get_data(as_text=True)
        assert 'OhneKontakt' in html
        assert 'Kein Kontakt hinterlegt' in html

    def test_independent_travel_shown(self, client, staff, parent, db):
        _session(client, staff)
        camp = _make_camp(db)
        group = _make_group(db, camp)
        child = _make_child(db, parent)
        _make_registration(db, child, camp, group, independent_travel=True)
        db.session.commit()
        html = client.get(f'/team/gruppe/{group.id}/drucken').get_data(as_text=True)
        assert 'travel-badge' in html

    def test_sorted_by_last_name(self, client, staff, parent, db):
        _session(client, staff)
        camp = _make_camp(db)
        group = _make_group(db, camp)
        c1 = _make_child(db, parent, last='Zimmermann')
        c2 = _make_child(db, parent, last='Ahrens')
        _make_registration(db, c1, camp, group)
        _make_registration(db, c2, camp, group)
        db.session.commit()
        html = client.get(f'/team/gruppe/{group.id}/drucken').get_data(as_text=True)
        # Ahrens should appear before Zimmermann in the rendered HTML
        pos_ahrens = html.find('Ahrens')
        pos_zimmermann = html.find('Zimmermann')
        assert pos_ahrens < pos_zimmermann

    def test_shows_group_name_and_head_coach(self, client, staff, parent, db):
        from sub_modules.models import GroupAssignment
        _session(client, staff)
        camp = _make_camp(db)
        group = _make_group(db, camp, name='U12')
        ga = GroupAssignment(camp_session_id=camp.id, age_group_id=group.id,
                             staff_user_id=staff.id, is_head_coach=True)
        db.session.add(ga)
        db.session.commit()
        html = client.get(f'/team/gruppe/{group.id}/drucken').get_data(as_text=True)
        assert 'U12' in html
        assert staff.last_name in html

    def test_empty_group_renders_gracefully(self, client, staff, db):
        _session(client, staff)
        camp = _make_camp(db)
        group = _make_group(db, camp)
        db.session.commit()
        resp = client.get(f'/team/gruppe/{group.id}/drucken')
        assert resp.status_code == 200
        assert 'Keine bestätigten' in resp.get_data(as_text=True)

    def test_nonexistent_group_returns_404(self, client, staff):
        _session(client, staff)
        resp = client.get('/team/gruppe/99999/drucken')
        assert resp.status_code == 404
