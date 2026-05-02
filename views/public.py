'''
views/public.py
===============
Public-facing blueprint. No login required.

Routes:
    GET  /          Landing page — camp info, registration open/closed banner
    GET  /datenschutz   Datenschutzerklärung (privacy policy)
    GET  /impressum     Impressum (legal notice, required in Germany)
    GET  /kontakt       Contact page
'''

from flask import Blueprint, render_template, request, flash, redirect, url_for, current_app, session
from flask_login import current_user
from flask_wtf import FlaskForm
from wtforms import StringField, TextAreaField
from wtforms.validators import DataRequired, Email, Length, Optional

from sub_modules.models import CampSession, Announcement
# Config values accessed via current_app.config in routes


public_bp = Blueprint('public', __name__)


class ContactForm(FlaskForm):
    name    = StringField('Ihr Name', validators=[DataRequired(), Length(max=100)])
    email   = StringField('E-Mail', validators=[DataRequired(), Email(check_deliverability=False)])
    subject = StringField('Betreff', validators=[Optional(), Length(max=200)])
    message = TextAreaField('Nachricht', validators=[DataRequired(), Length(min=10, max=2000)])
    # Honeypot
    website = StringField('Website', validators=[Optional()])



import json
import os
from datetime import datetime

def _load_sponsors():
    """
    Load sponsors from static/data/sponsors.json.
    Returns an empty list if the file is missing or malformed.
    Filters to current year automatically so old sponsors don't persist.
    """
    sponsors_path = os.path.join(
        current_app.static_folder, 'data', 'sponsors.json'
    )
    try:
        with open(sponsors_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        current_year = datetime.utcnow().year
        # Include sponsors with matching year OR no year specified
        return [s for s in data if s.get('year') in (current_year, None)]
    except (FileNotFoundError, json.JSONDecodeError, Exception):
        return []

@public_bp.route('/')
def index():
    '''
    Landing page.
    Shows camp info, dates, and a registration open/closed banner.
    Pinned public announcements shown if any exist.
    No login required — parents land here first.
    '''
    active_session = CampSession.query.filter(
        CampSession.status.in_(['open', 'upcoming', 'active'])
    ).order_by(CampSession.start_date.desc()).first()

    # Only 'open' announcements (no login needed) shown on landing page
    # 'public' = requires login; 'open' = truly public
    pinned = Announcement.query.filter_by(
        visibility='open',
        is_pinned=True
    ).order_by(Announcement.created_at.desc()).limit(3).all()

    return render_template(
        'public/index.html',
        active_session=active_session,
        pinned_announcements=pinned,
        config=current_app.config,
        title=current_app.config.get('CAMP_NAME', 'Fußballcamp')
    )


@public_bp.route('/datenschutz')
def datenschutz():
    '''
    Datenschutzerklärung — GDPR-required privacy policy page.
    Content uses template variables from config.py so forks can
    customise without touching the template.
    '''
    return render_template(
        'public/datenschutz.html',
        config=current_app.config,
        title='Datenschutzerklärung'
    )


@public_bp.route('/impressum')
def impressum():
    '''
    Impressum — legally required in Germany for any web presence.
    Content uses config.py variables.
    '''
    return render_template(
        'public/impressum.html',
        config=current_app.config,
        title='Impressum'
    )


@public_bp.route('/kontakt', methods=['GET', 'POST'])
def kontakt():
    from flask_login import current_user

    # PRG — show success message via query param after redirect
    if request.args.get('sent'):
        return render_template('public/kontakt.html', form=ContactForm(),
                               form_sent=True, title='Kontakt')

    form = ContactForm()

    # Pre-fill name and email for logged-in users on GET
    if request.method == 'GET' and current_user.is_authenticated:
        form.name.data  = current_user.full_name
        form.email.data = current_user.email

    if form.validate_on_submit():
        if bool((form.website.data or '').strip()):
            return redirect(url_for('public.kontakt') + '?sent=1')

        # If logged in, always use the authenticated user's identity
        sender_name  = current_user.full_name if current_user.is_authenticated else form.name.data
        sender_email = current_user.email     if current_user.is_authenticated else form.email.data

        from sub_modules.emails import send_contact_message
        try:
            send_contact_message(
                sender_name=sender_name,
                sender_email=sender_email,
                subject=form.subject.data or 'Kontaktanfrage',
                message=form.message.data,
            )
        except Exception as e:
            current_app.logger.error(f'[Contact] Email error: {e}')

        return redirect(url_for('public.kontakt') + '?sent=1')

    return render_template(
        'public/kontakt.html',
        form=form,
        form_sent=False,
        title='Kontakt'
    )


@public_bp.route('/sprache/<locale>')
def set_language(locale):
    '''
    Set the user's preferred language via a session cookie.
    Redirects back to the referring page (or home if no referrer).
    Supported: de (Deutsch), en (English), ar (العربية)
    '''
    if locale in current_app.config.get('LANGUAGES', ['de', 'en', 'ar']):
        session['locale'] = locale
    return redirect(request.referrer or url_for('public.index'))


@public_bp.route('/ueber-uns')
def about():
    '''Public about page — visible without login.'''
    return render_template('public/about.html', title='Über uns')

