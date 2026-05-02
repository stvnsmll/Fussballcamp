'''
views/feedback.py
=================
Bug report / feedback submission for all authenticated users.
Accessible from the settings/account menu regardless of role.

Routes:
    GET/POST  /feedback/bug-melden        Submit a bug report
    GET       /feedback/meine-meldungen   View own submitted reports

Security:
    - Login required on all routes
    - All text inputs sanitized with bleach before storage (no HTML allowed)
    - Page URL stripped to relative path only (no external URLs, no query strings)
    - Rate limited: 5 submissions per hour per user
    - No file attachments (reduces attack surface)
    - Admin notes field on reports also sanitized when admin saves
'''

from datetime import datetime

from flask import (Blueprint, render_template, redirect, url_for,
                   flash, request, current_app)
from flask_login import login_required, current_user
from flask_wtf import FlaskForm
from wtforms import StringField, TextAreaField, SelectField, HiddenField
from wtforms.validators import DataRequired, Length, Optional

from application import db, log_analytics_event, limiter
from sub_modules.models import BugReport
from sub_modules.helpers import sanitize_text, sanitize_url_path


feedback_bp = Blueprint('feedback', __name__)


# =============================================================================
# FORM
# =============================================================================

class BugReportForm(FlaskForm):
    subject     = StringField(
                    'Kurzbeschreibung',
                    validators=[DataRequired(), Length(min=5, max=120)],
                    description='Was ist das Problem? (max. 120 Zeichen)'
                  )
    description = TextAreaField(
                    'Beschreibung',
                    validators=[DataRequired(), Length(min=10, max=2000)],
                    description='Bitte beschreibe das Problem so genau wie möglich. '
                                'Was hast du erwartet? Was ist stattdessen passiert?'
                  )
    severity    = SelectField(
                    'Schweregrad',
                    choices=[
                        ('low',    'Gering — kleines Problem, stört kaum'),
                        ('medium', 'Mittel — etwas funktioniert nicht richtig'),
                        ('high',   'Hoch   — blockiert mich komplett'),
                    ],
                    default='medium'
                  )
    # Hidden field — populated by JavaScript with current page path
    # Validated server-side to relative path only before storage
    page_url    = HiddenField(validators=[Optional()])


# =============================================================================
# ROUTES
# =============================================================================

@feedback_bp.route('/bug-melden', methods=['GET', 'POST'])
@login_required
@limiter.limit('5 per hour', exempt_when=lambda: current_app.config.get('DISABLE_RATE_LIMIT', False))
def submit():
    '''
    Bug report submission form. Available to all authenticated users.
    Rate limited to 5 per hour to prevent spam.
    All text fields sanitized with bleach before storage.
    '''
    form = BugReportForm()

    # Pre-fill page_url from query param (set by the "Fehler melden" link
    # in the nav — passes current page as ?seite=/eltern/ etc.)
    if request.method == 'GET':
        referrer_path = sanitize_url_path(
            request.args.get('seite') or request.referrer or ''
        )
        form.page_url.data = referrer_path

    if form.validate_on_submit():
        # Check rate limit manually for cleaner flash messages
        # (Flask-Limiter decorator would return 429 with no flash)
        one_hour_ago = datetime.utcnow() - __import__('datetime').timedelta(hours=1)
        recent_count = BugReport.query.filter(
            BugReport.reporter_user_id == current_user.id,
            BugReport.created_at >= one_hour_ago
        ).count()

        if recent_count >= 5:
            flash(
                'Du hast in der letzten Stunde bereits 5 Meldungen eingereicht. '
                'Bitte warte etwas, bevor du weitere Meldungen einreichst.',
                'warning'
            )
            return render_template('feedback/submit.html', form=form,
                                   title='Fehler melden')

        # Sanitize all text fields — strip HTML, enforce length caps
        subject     = sanitize_text(form.subject.data,      max_length=120)
        description = sanitize_text(form.description.data,  max_length=2000)
        page_url    = sanitize_url_path(form.page_url.data)

        if not subject or not description:
            flash('Bitte fülle alle Pflichtfelder aus.', 'danger')
            return render_template('feedback/submit.html', form=form,
                                   title='Fehler melden')

        report = BugReport(
            reporter_user_id = current_user.id,
            reporter_email   = current_user.email,  # snapshot at submission time
            reporter_role    = current_user.role,
            subject          = subject,
            description      = description,
            page_url         = page_url,
            severity         = form.severity.data,
            status           = 'new',
        )
        db.session.add(report)
        db.session.commit()

        log_analytics_event('bug_report_submitted', success=True,
                            detail=form.severity.data)

        current_app.logger.info(
            f'[Feedback] New bug report #{report.id} from user {current_user.id} '
            f'({current_user.role}) severity={report.severity}'
        )

        flash(
            'Danke für deine Meldung! Wir schauen uns das so schnell wie '
            'möglich an.',
            'success'
        )
        return redirect(url_for('feedback.my_reports'))

    return render_template('feedback/submit.html', form=form,
                           title='Fehler melden')


@feedback_bp.route('/meine-meldungen')
@login_required
def my_reports():
    '''
    View the current user's own submitted bug reports and their status.
    Users can see whether their report has been acknowledged or resolved.
    They cannot see reports from other users.
    '''
    reports = BugReport.query.filter_by(
        reporter_user_id=current_user.id
    ).order_by(BugReport.created_at.desc()).all()

    return render_template('feedback/my_reports.html', reports=reports,
                           title='Meine Meldungen')
