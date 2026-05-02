'''
sub_modules/emails.py
=====================
All outbound email functions using Flask-Mail (SendGrid SMTP).

Each function takes a User object (and any extra data needed) and sends
the appropriate email. All sends are logged to the email_log table.

Templates live in templates/email_templates/ as HTML files.
Plain-text fallback is included in each message.

Functions:
    send_verification_email      - Email address confirmation link
    send_password_reset_email    - Password reset link
    send_staff_invite_email      - Staff invite link
    send_registration_confirmation - Camp registration confirmed
    send_waitlist_notification   - Placed on waitlist
    send_camp_reminder           - Pre-camp reminder (admin-triggered)
    send_announcement_email      - New public announcement notification
    send_deletion_warning_email  - GDPR retention warning before deletion
    log_email                    - Write to email_log table
'''

from datetime import datetime
from flask import current_app, render_template, url_for
from flask_mail import Message

from sub_modules.extensions import db, mail
from sub_modules.models import EmailLog


# =============================================================================
# INTERNAL HELPERS
# =============================================================================

def _send(to_email: str, subject: str, html_body: str,
          text_body: str = None) -> bool:
    '''
    Send an email via Flask-Mail (SendGrid SMTP).
    Returns True on success, False on failure.
    Logs errors but does not raise — email failure should never crash a request.
    Respects MAIL_SUPPRESS_SEND config dynamically (for dev toggle support).
    '''
    from flask import current_app
    if current_app.config.get('MAIL_SUPPRESS_SEND', False):
        current_app.logger.info(f'[Email] Suppressed (dev toggle): {subject} → {to_email}')
        return True
    try:
        msg = Message(
            subject=subject,
            recipients=[to_email],
            html=html_body,
            body=text_body or _strip_html(html_body)
        )
        mail.send(msg)
        return True
    except Exception as e:
        current_app.logger.error(f'[Email] Failed to send to {to_email}: {e}')
        return False


def _strip_html(html: str) -> str:
    '''Very basic HTML stripping for plain-text fallback.'''
    import re
    return re.sub(r'<[^>]+>', '', html).strip()


def log_email(user_id: int, recipient_email: str, email_type: str,
              subject: str, status: str = 'sent',
              sendgrid_message_id: str = None):
    '''
    Write an entry to the email_log table.
    Call this after every send attempt, whether successful or not.
    '''
    try:
        log = EmailLog(
            recipient_user_id=user_id,
            recipient_email=recipient_email,
            email_type=email_type,
            subject=subject,
            status=status,
            sendgrid_message_id=sendgrid_message_id,
            sent_at=datetime.utcnow()
        )
        db.session.add(log)
        db.session.commit()
    except Exception as e:
        current_app.logger.error(f'[Email] Failed to log email: {e}')


# =============================================================================
# OUTBOUND EMAIL FUNCTIONS
# =============================================================================

def send_verification_email(user, token: str) -> bool:
    '''Send email address verification link after registration.'''
    verify_url = url_for('auth.verify_email', token=token, _external=True)
    camp_name = current_app.config['CAMP_NAME']

    html = render_template(
        'email_templates/verification.html',
        user=user,
        verify_url=verify_url,
        camp_name=camp_name
    )
    text = (
        f'Hallo {user.first_name},\n\n'
        f'Bitte bestätigen Sie Ihre E-Mail-Adresse:\n{verify_url}\n\n'
        f'Dieser Link ist 24 Stunden gültig.\n\n'
        f'Mit freundlichen Grüßen,\n{camp_name}'
    )
    return _send(
        user.email,
        f'{camp_name} — E-Mail-Adresse bestätigen',
        html,
        text
    )


def send_password_reset_email(user, token: str) -> bool:
    '''Send password reset link.'''
    reset_url = url_for('auth.password_reset_confirm', token=token, _external=True)
    camp_name = current_app.config['CAMP_NAME']

    html = render_template(
        'email_templates/password_reset.html',
        user=user,
        reset_url=reset_url,
        camp_name=camp_name
    )
    text = (
        f'Hallo {user.first_name},\n\n'
        f'Passwort zurücksetzen:\n{reset_url}\n\n'
        f'Dieser Link ist 2 Stunden gültig. Falls Sie kein neues Passwort '
        f'angefordert haben, ignorieren Sie diese E-Mail.\n\n'
        f'Mit freundlichen Grüßen,\n{camp_name}'
    )
    return _send(
        user.email,
        f'{camp_name} — Passwort zurücksetzen',
        html,
        text
    )




def send_magic_link_email(user, token: str) -> bool:
    '''Send a one-time magic login link (15 min expiry).'''
    magic_url = url_for('auth.magic_link_redeem', token=token, _external=True)
    camp_name = current_app.config['CAMP_NAME']

    text = (
        f'Hallo {user.first_name},\n\n'
        f'Hier ist Ihr Anmeldelink:\n{magic_url}\n\n'
        f'Dieser Link ist 15 Minuten gültig und kann nur einmal verwendet werden.\n'
        f'Falls Sie diesen Link nicht angefordert haben, ignorieren Sie diese E-Mail.\n\n'
        f'Mit freundlichen Grüßen,\n{camp_name}'
    )
    html = (
        f'<p>Hallo {user.first_name},</p>'
        f'<p><a href="{magic_url}" style="background:#1b3f2a;color:#b8f442;padding:12px 24px;'
        f'border-radius:8px;text-decoration:none;font-weight:700;display:inline-block">'
        f'Jetzt anmelden →</a></p>'
        f'<p style="color:#6b7280;font-size:.85rem">Dieser Link ist 15 Minuten gültig '
        f'und kann nur einmal verwendet werden.</p>'
    )
    return _send(
        user.email,
        f'{camp_name} — Anmeldelink',
        html,
        text
    )

def send_staff_invite_email(user, token: str, invited_by_user) -> bool:
    '''Send staff invite link. Called from admin blueprint.'''
    invite_url = url_for('auth.redeem_invite', token=token, _external=True)
    camp_name = current_app.config['CAMP_NAME']

    html = render_template(
        'email_templates/staff_invite.html',
        invite_url=invite_url,
        invited_by=invited_by_user,
        camp_name=camp_name
    )
    text = (
        f'Sie wurden zum {camp_name} eingeladen von '
        f'{invited_by_user.full_name}.\n\n'
        f'Bitte klicken Sie auf folgenden Link, um Ihr Konto einzurichten:\n'
        f'{invite_url}\n\n'
        f'Dieser Link ist {current_app.config.get("INVITE_TOKEN_EXPIRY_HOURS", 72)} '
        f'Stunden gültig.\n\n'
        f'Mit freundlichen Grüßen,\n{camp_name}'
    )
    return _send(
        user.email,
        f'{camp_name} — Einladung zum Team',
        html,
        text
    )


def send_registration_confirmation(user, child, registration, camp_session) -> bool:
    '''Send registration confirmation to parent.'''
    camp_name = current_app.config['CAMP_NAME']
    account_url = url_for('parents.dashboard', _external=True)

    html = render_template(
        'email_templates/registration_confirmation.html',
        user=user,
        child=child,
        registration=registration,
        camp_session=camp_session,
        account_url=account_url,
        camp_name=camp_name
    )
    text = (
        f'Hallo {user.first_name},\n\n'
        f'{child.full_name} wurde erfolgreich für das {camp_name} '
        f'({camp_session.year}) angemeldet.\n\n'
        f'Status: {"Bestätigt" if registration.is_confirmed else "Warteliste"}\n\n'
        f'Ihr Konto: {account_url}\n\n'
        f'Mit freundlichen Grüßen,\n{camp_name}'
    )
    success = _send(
        user.email,
        f'{camp_name} — Anmeldebestätigung für {child.full_name}',
        html,
        text
    )
    status = 'sent' if success else 'failed'
    log_email(user.id, user.email, 'confirmation',
              f'Anmeldebestätigung für {child.full_name}', status)
    return success


def send_waitlist_notification(user, child, registration, camp_session) -> bool:
    '''Notify parent their child has been placed on the waitlist.'''
    camp_name = current_app.config['CAMP_NAME']

    html = render_template(
        'email_templates/waitlist_notification.html',
        user=user,
        child=child,
        registration=registration,
        camp_session=camp_session,
        camp_name=camp_name
    )
    text = (
        f'Hallo {user.first_name},\n\n'
        f'{child.full_name} steht auf der Warteliste für das {camp_name} '
        f'({camp_session.year}).\n'
        f'Position: {registration.waitlist_position}\n\n'
        f'Wir werden Sie direkt kontaktieren, wenn ein Platz frei wird.\n\n'
        f'Mit freundlichen Grüßen,\n{camp_name}'
    )
    success = _send(
        user.email,
        f'{camp_name} — Warteliste: {child.full_name}',
        html,
        text
    )
    status = 'sent' if success else 'failed'
    log_email(user.id, user.email, 'confirmation',
              f'Warteliste: {child.full_name}', status)
    return success


def send_camp_reminder(user, camp_session, children) -> bool:
    '''
    Pre-camp reminder email. Admin triggers this for all confirmed registrations.
    children: list of Child objects registered for this session.
    '''
    camp_name = current_app.config['CAMP_NAME']
    account_url = url_for('parents.dashboard', _external=True)

    # Respect opt-out preference
    if not user.email_announcements:
        return False

    html = render_template(
        'email_templates/camp_reminder.html',
        user=user,
        camp_session=camp_session,
        children=children,
        account_url=account_url,
        camp_name=camp_name
    )
    text = (
        f'Hallo {user.first_name},\n\n'
        f'Das {camp_name} beginnt bald!\n'
        f'Datum: {camp_session.start_date.strftime("%d.%m.%Y")} – '
        f'{camp_session.end_date.strftime("%d.%m.%Y")}\n'
        f'Ort: {camp_session.location}\n\n'
        f'Ihr Konto: {account_url}\n\n'
        f'Mit freundlichen Grüßen,\n{camp_name}'
    )
    success = _send(
        user.email,
        f'{camp_name} {camp_session.year} — Erinnerung',
        html,
        text
    )
    status = 'sent' if success else 'failed'
    log_email(user.id, user.email, 'reminder',
              f'{camp_name} {camp_session.year} — Erinnerung', status)
    return success


def send_announcement_email(user, announcement, camp_session=None) -> bool:
    '''
    Notify opted-in parents of a new public announcement.
    Respects user.email_announcements opt-out flag.
    '''
    if not user.email_announcements:
        return False

    camp_name = current_app.config['CAMP_NAME']
    announcements_url = url_for('announcements.feed', _external=True)
    unsubscribe_url = url_for('parents.email_preferences', _external=True)

    html = render_template(
        'email_templates/announcement.html',
        user=user,
        announcement=announcement,
        announcements_url=announcements_url,
        unsubscribe_url=unsubscribe_url,
        camp_name=camp_name
    )
    text = (
        f'Hallo {user.first_name},\n\n'
        f'Neue Nachricht: {announcement.title}\n\n'
        f'{announcement.body[:300]}...\n\n'
        f'Alle Neuigkeiten: {announcements_url}\n\n'
        f'Benachrichtigungen abbestellen: {unsubscribe_url}\n\n'
        f'Mit freundlichen Grüßen,\n{camp_name}'
    )
    success = _send(
        user.email,
        f'{camp_name} — {announcement.title}',
        html,
        text
    )
    status = 'sent' if success else 'failed'
    log_email(user.id, user.email, 'announcement',
              announcement.title, status)
    return success


def send_deletion_warning_email(user, deletion_date) -> bool:
    '''
    GDPR retention warning — sent before a stale account is deleted.
    deletion_date: the date the account will be anonymised if no action taken.
    '''
    camp_name = current_app.config['CAMP_NAME']
    login_url = url_for('auth.login', _external=True)

    html = render_template(
        'email_templates/deletion_warning.html',
        user=user,
        deletion_date=deletion_date,
        login_url=login_url,
        camp_name=camp_name
    )
    text = (
        f'Hallo {user.first_name},\n\n'
        f'Ihr Konto beim {camp_name} wird am '
        f'{deletion_date.strftime("%d.%m.%Y")} gemäß unserer '
        f'Datenschutzrichtlinie gelöscht.\n\n'
        f'Falls Sie Ihr Konto behalten möchten, melden Sie sich bitte '
        f'vor diesem Datum an:\n{login_url}\n\n'
        f'Mit freundlichen Grüßen,\n{camp_name}'
    )
    success = _send(
        user.email,
        f'{camp_name} — Hinweis zur Datenlöschung',
        html,
        text
    )
    status = 'sent' if success else 'failed'
    log_email(user.id, user.email, 'deletion_warning',
              f'{camp_name} — Hinweis zur Datenlöschung', status)

    if success:
        user.deletion_warning_sent_at = datetime.utcnow()
        db.session.commit()

    return success


def send_contact_message(sender_name: str, sender_email: str,
                          subject: str, message: str) -> bool:
    '''
    Forward a contact form submission to the camp contact email.
    '''
    from flask import current_app
    from flask_mail import Message
    from sub_modules.extensions import mail

    recipient = current_app.config.get('CAMP_CONTACT_EMAIL', '')
    if not recipient:
        return False

    if current_app.config.get('MAIL_SUPPRESS_SEND', False):
        current_app.logger.info(f'[Email] Suppressed (dev toggle): Kontaktformular → {recipient}')
        return True

    try:
        msg = Message(
            subject=f'[Kontaktformular] {subject}',
            recipients=[recipient],
            reply_to=sender_email,
            body=(
                f'Name:    {sender_name}\n'
                f'E-Mail:  {sender_email}\n'
                f'Betreff: {subject}\n\n'
                f'{message}'
            )
        )
        mail.send(msg)
        return True
    except Exception as e:
        current_app.logger.error(f'[Email] send_contact_message failed: {e}')
        return False
