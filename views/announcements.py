'''
views/announcements.py
======================
Announcements blueprint. Visible to authenticated parents and staff.
Admins manage announcements via admin_bp — this blueprint is read-only
for parents and staff.

Routes:
    GET  /neuigkeiten/              Feed — role-filtered, pinned first
    GET  /neuigkeiten/<id>          Single announcement detail

Visibility rules (enforced in Announcement.is_visible_to()):
    'public' announcements  → parents whose children are in the target groups
                              + all staff + all admin
    'staff'  announcements  → staff and admin only, never parents

Target groups:
    If target_age_groups is set, only parents with children in those groups
    see the announcement. Empty target_age_groups = everyone in the role.

Parents whose children are only on the waitlist do not see group-targeted
public announcements — only confirmed registrations count.
'''

from flask import (Blueprint, render_template, abort)
from flask_login import login_required, current_user

from sub_modules.extensions import log_analytics_event
from sub_modules.models import Announcement, CampSession


announcements_bp = Blueprint('announcements', __name__)


# =============================================================================
# FEED
# =============================================================================

@announcements_bp.route('/')
def feed():
    '''
    Announcement feed. Unauthenticated users see only open/public items.
    Logged-in users see all items visible to their role.
    '''
    from flask_login import current_user

    active_session = CampSession.query.filter(
        CampSession.status.in_(['open', 'upcoming', 'active'])
    ).order_by(CampSession.start_date.desc()).first()

    all_announcements = Announcement.query.order_by(
        Announcement.is_pinned.desc(),
        Announcement.created_at.desc()
    ).all()

    user = current_user if current_user.is_authenticated else None
    visible = [a for a in all_announcements if a.is_visible_to(user)]

    if user:
        log_analytics_event('page_view', detail='announcements_feed')

    return render_template(
        'announcements/feed.html',
        announcements=visible,
        active_session=active_session,
        title='Neuigkeiten'
    )


# =============================================================================
# DETAIL
# =============================================================================

@announcements_bp.route('/<int:announcement_id>')
def detail(announcement_id):
    from flask_login import current_user
    announcement = Announcement.query.get_or_404(announcement_id)
    user = current_user if current_user.is_authenticated else None
    if not announcement.is_visible_to(user):
        abort(403)

    # Enforce visibility — 403 rather than 404 to avoid leaking that a
    # staff-only post exists at all to a parent trying to guess URLs
    if not announcement.is_visible_to(current_user):
        abort(403)

    log_analytics_event('announcement_view', success=True,
                        detail=str(announcement_id))

    return render_template(
        'announcements/detail.html',
        announcement=announcement,
        title=announcement.title
    )
