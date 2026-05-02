'''
FUSSBALLCAMP
============

A self-hostable, FOSS (MIT) Flask web app for managing a free youth soccer camp.
Designed for German-speaking audiences, GDPR-compliant, and forkable.

Project Structure:
~/fussballcamp
    |-- application.py              # main script (this file)
    |-- requirements.txt
    |-- Procfile                    # for Render/Railway deployment
    |-- .env.example                # template for environment variables
    |-- .gitignore
    |-- README.md
    |
    |__ /views                      # Flask blueprints (one per feature area)
    |    |-- __init__.py
    |    |-- auth.py                # register, login, logout, email verify, invites
    |    |-- parents.py             # child management, registration, account
    |    |-- staff.py               # check-in/out, roster, group view
    |    |-- admin.py               # camp mgmt, user mgmt, group overrides
    |    |-- announcements.py       # announcements feed (role-gated)
    |    |-- public.py              # landing page, registration open/closed
    |
    |__ /sub_modules                # helpers and supporting functions
    |    |-- __init__.py
    |    |-- config.py              # all camp-specific configuration
    |    |-- models.py              # SQLAlchemy models
    |    |-- helpers.py             # shared utility functions
    |    |-- emails.py              # SendGrid email functions
    |    |-- image_mgmt.py          # AWS S3 image upload/fetch
    |    |-- seed.py                # fake data generator for local dev/testing
    |
    |__ /admin_tools                # scripts run outside the app (cron/manual)
    |    |-- retention_check.py     # GDPR retention flagging and deletion warnings
    |
    |__ /templates                  # Jinja2 HTML templates
    |    |-- layout.html            # base layout (nav, footer, Google Translate)
    |    |__ /auth
    |    |__ /parents
    |    |__ /staff
    |    |__ /admin
    |    |__ /announcements
    |    |__ /public
    |    |__ /email_templates       # SendGrid email HTML templates
    |    |__ /errors                # 404, 500 etc.
    |
    |__ /static
    |    |__ /css
    |    |__ /js
    |    |__ /img
    |
    |__ /migrations                 # Flask-Migrate / Alembic migrations
'''


################################################################
# [1] IMPORTS
################################################################

import os
import time
from flask import Flask, render_template, g, request
from flask_babel import get_locale
from flask_wtf.csrf import CSRFProtect
import boto3
import botocore

################################################################
# [2] EXTENSIONS
# Declared in sub_modules/extensions.py to avoid circular imports.
# Imported here so the rest of the codebase can still do:
#   from application import db   (backwards compat)
################################################################

from sub_modules.extensions import (
    db, migrate, login_manager, mail, limiter, babel, scheduler,
    log_analytics_event, _log_error, _detect_device_type,
)
csrf = CSRFProtect()


################################################################
# [3] APP FACTORY
################################################################

def create_app(config_name=None):
    app = Flask(__name__)

    # ----------------------------------------------------------
    # A - Select and load config class
    #
    # Priority: argument passed to create_app() > APP_ENV env var > 'development'
    # This means tests can call create_app('testing') directly,
    # the server sets APP_ENV=production, and local dev needs nothing at all.
    from sub_modules.config import config_by_name, DEFAULT_CONFIG
    if config_name is None:
        config_name = os.environ.get('APP_ENV', DEFAULT_CONFIG).lower()
    if config_name not in config_by_name:
        raise ValueError(
            f"Unknown APP_ENV '{config_name}'. "
            f"Valid values: {list(config_by_name.keys())}"
        )
    cfg = config_by_name[config_name]
    app.config.from_object(cfg)
    # Allow per-test DB override: if DATABASE_URL is set at call time,
    # apply it now — class-level config attributes are frozen at import time
    # so os.environ changes after import are invisible without this.
    if os.environ.get('DATABASE_URL'):
        app.config['SQLALCHEMY_DATABASE_URI'] = os.environ['DATABASE_URL']
    app.config['ENV_MODE'] = config_name
    print(f'[Config] APP_ENV={config_name} — DB: {app.config.get("SQLALCHEMY_DATABASE_URI", "not set")}')

    # ----------------------------------------------------------
    # B - Production safety check
    # Fail loudly at startup if critical production config is missing,
    # rather than failing silently on the first real request.
    if config_name == 'production':
        missing = []
        if not app.config.get('SQLALCHEMY_DATABASE_URI'):
            missing.append('DATABASE_URL')
        if not app.config.get('MAIL_PASSWORD'):
            missing.append('SENDGRID_API_KEY')
        if app.config.get('SECRET_KEY') == 'dev-only-change-in-production':
            missing.append('SECRET_KEY')
        if missing:
            raise RuntimeError(
                f"Production startup blocked — missing environment variables: "
                f"{', '.join(missing)}"
            )

    # ----------------------------------------------------------
    # C - S3 client (only initialised when a bucket is configured)
    # In development AWS_S3_BUCKET is None, so image_mgmt.py uses local storage.
    if app.config.get('AWS_S3_BUCKET'):
        s3_client = boto3.client(
            's3',
            aws_access_key_id=os.environ.get('AWS_ACCESS_KEY_ID'),
            aws_secret_access_key=os.environ.get('AWS_SECRET_ACCESS_KEY'),
            region_name=app.config['AWS_S3_REGION'],
            config=botocore.client.Config(signature_version='s3v4')
        )
        app.config['s3_client'] = s3_client
        app.config['S3_LOCATION'] = (
            f'https://{app.config["AWS_S3_BUCKET"]}.s3.amazonaws.com/'
        )
    else:
        app.config['s3_client'] = None
        app.config['S3_LOCATION'] = None

    # ----------------------------------------------------------
    # D - Bind extensions to app
    db.init_app(app)
    migrate.init_app(app, db)
    login_manager.init_app(app)
    mail.init_app(app)
    limiter.init_app(app)
    if app.config.get('WTF_CSRF_ENABLED', True):
        csrf.init_app(app)
    else:
        # When CSRF is disabled (tests), inject a dummy csrf_token() so templates
        # that call {{ csrf_token() }} don't raise UndefinedError
        @app.context_processor
        def inject_csrf_token():
            return {'csrf_token': lambda: ''}

    # Flask-Babel — locale selector
    # If language switcher is disabled, always use German (ignore session cookie).
    # If enabled, read from session then fall back to Accept-Language header.
    def _select_locale():
        from flask import session, request
        if not app.config.get('SHOW_LANGUAGE_SWITCHER', False):
            return app.config['BABEL_DEFAULT_LOCALE']  # always 'de'
        locale = session.get('locale')
        if locale in app.config['LANGUAGES']:
            return locale
        return request.accept_languages.best_match(
            app.config['LANGUAGES'],
            app.config['BABEL_DEFAULT_LOCALE']
        )

    babel.init_app(app, locale_selector=_select_locale)

    # Custom Jinja filter: tojson with ensure_ascii=False so German chars
    # remain readable in HTML source (needed for test assertions)
    import json as _json
    app.jinja_env.filters['tojson_unicode'] = lambda v: _json.dumps(v, ensure_ascii=False)

    login_manager.login_view = 'auth.login'
    login_manager.login_message = 'Bitte melden Sie sich an, um fortzufahren.'
    login_manager.login_message_category = 'info'

    # ----------------------------------------------------------
    # H - Import models (must happen after db.init_app so models register correctly)
    from sub_modules import models  # noqa

    # User loader for Flask-Login
    from sub_modules.models import User

    @login_manager.user_loader
    def load_user(user_id):
        user = User.query.get(int(user_id))
        # Returning None causes Flask-Login to clear the session and redirect to login
        if user is None or user.is_deleted or not user.is_active:
            return None
        return user

    # ----------------------------------------------------------
    # I - Register blueprints
    from views.public        import public_bp
    from views.auth          import auth_bp
    from views.parents       import parents_bp
    from views.staff         import staff_bp
    from views.admin         import admin_bp
    from views.announcements import announcements_bp
    from views.feedback      import feedback_bp

    app.register_blueprint(public_bp)
    app.register_blueprint(auth_bp,          url_prefix='/auth')
    app.register_blueprint(parents_bp,       url_prefix='/eltern')
    app.register_blueprint(staff_bp,         url_prefix='/team')
    app.register_blueprint(admin_bp,         url_prefix='/admin')
    app.register_blueprint(announcements_bp, url_prefix='/neuigkeiten')
    app.register_blueprint(feedback_bp,      url_prefix='/feedback')

    # ----------------------------------------------------------
    # J - Error handlers
    @app.errorhandler(404)
    def not_found(e):
        log_analytics_event('error_404', detail=request.path[:100])
        _log_error('http_404', status_code=404)
        return render_template('errors/404.html'), 404

    @app.errorhandler(403)
    def forbidden(e):
        log_analytics_event('error_403', detail=request.path[:100])
        _log_error('http_403', status_code=403)
        return render_template('errors/403.html'), 403

    @app.errorhandler(500)
    def server_error(e):
        # Log with full exception info for debugging
        app.logger.error(f'[500] {request.path} — {str(e)}', exc_info=True)
        log_analytics_event('error_500', detail=request.path[:100])
        _log_error('http_500', message=str(e), status_code=500)
        return render_template('errors/500.html'), 500

    # ----------------------------------------------------------
    # K - No-cache headers + analytics capture
    @app.before_request
    def before_request():
        # Record request start time for response_ms calculation
        g.request_start_time = time.time()
        # Make current locale available in all templates as g.locale
        g.locale = str(get_locale() or app.config['BABEL_DEFAULT_LOCALE'])
        # Make active camp available globally for nav (e.g. greying Check-In)
        from sub_modules.models import CampSession
        g.active_camp = CampSession.query.filter(
            CampSession.status.in_(['active', 'open'])
        ).first()
        # Apply persisted dev toggle state on every request (survives reloads)
        if app.debug:
            import json, os
            dev_state_path = os.path.join(app.instance_path, 'dev_toggles.json')
            try:
                with open(dev_state_path) as f:
                    state = json.load(f)
                app.config['DEV_OPEN_REGISTRATION']  = state.get('DEV_OPEN_REGISTRATION', False)
                app.config['MAIL_SUPPRESS_SEND']     = state.get('MAIL_SUPPRESS_SEND', False)
                app.config['SHOW_LANGUAGE_SWITCHER'] = state.get('SHOW_LANGUAGE_SWITCHER', False)
                app.config['DEV_CAMP_TODAY']         = state.get('DEV_CAMP_TODAY', False)
                app.config['SHOW_TEMPLATE_NAME']     = state.get('SHOW_TEMPLATE_NAME', False)
                app.config['DISABLE_RATE_LIMIT']     = state.get('DISABLE_RATE_LIMIT', False)
                app.config['TOAST_DURATION']         = state.get('TOAST_DURATION', 5)
            except (FileNotFoundError, Exception):
                pass

    @app.after_request
    def after_request(response):
        # No-cache headers
        response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
        response.headers['Expires'] = 0
        response.headers['Pragma'] = 'no-cache'

        # Analytics capture — skip static files and the analytics route itself
        try:
            path = request.path
            skip_prefixes = ('/static/', '/analytics')
            if not any(path.startswith(p) for p in skip_prefixes):
                _capture_page_view(response)
        except Exception:
            pass  # analytics must never break a real request

        return response

    # ----------------------------------------------------------
    # K2 - Template context processor
    # Injects `config` and `current_user` into every template automatically.
    # Avoids passing config=current_app.config in every single render_template().
    @app.context_processor
    def inject_globals():
        from datetime import datetime
        from flask_login import current_user as cu
        return {
            'config': app.config,
            'current_user': cu,
            'now': datetime.utcnow(),
        }

    # ----------------------------------------------------------
    # L - Scheduled jobs
    _start_scheduler(app)

    # ----------------------------------------------------------
    # M - CLI commands
    from sub_modules.seed import register_seed_commands
    from admin_tools.retention_check import register_retention_commands
    register_seed_commands(app)
    register_retention_commands(app)

    return app


################################################################
# [4] SCHEDULED JOBS
# APScheduler runs inside the Flask process.
# Staff auto-checkout fires daily at STAFF_AUTO_CHECKOUT_TIME.
# Set DISABLE_SCHEDULER=1 in .env to turn off during development.
################################################################

def _start_scheduler(app):
    if app.config.get('DISABLE_SCHEDULER'):
        print('[Scheduler] Disabled (DISABLE_SCHEDULER=True in config)')
        return

    hour, minute = [int(x) for x in app.config['STAFF_AUTO_CHECKOUT_TIME'].split(':')]

    def staff_auto_checkout_job():
        '''
        Insert a checkout event for every staff member who checked in today
        but has no checkout record yet.
        Marks events with is_auto_checkout=True so they are distinguishable
        from real manual checkouts.
        '''
        with app.app_context():
            from sub_modules.helpers import run_staff_auto_checkout
            run_staff_auto_checkout()

    scheduler.add_job(
        staff_auto_checkout_job,
        trigger='cron',
        hour=hour,
        minute=minute,
        id='staff_auto_checkout',
        replace_existing=True
    )

    # Analytics purge job — runs daily at 03:00
    # Deletes events older than ANALYTICS_RETENTION_DAYS if > 0
    def analytics_purge_job():
        with app.app_context():
            retention_days = app.config.get('ANALYTICS_RETENTION_DAYS', 365)
            if retention_days <= 0:
                return
            from datetime import datetime, timedelta
            from sub_modules.models import AnalyticsEvent
            cutoff = datetime.utcnow() - timedelta(days=retention_days)
            try:
                deleted = AnalyticsEvent.query.filter(
                    AnalyticsEvent.occurred_at < cutoff
                ).delete()
                db.session.commit()
                if deleted:
                    app.logger.info(f'[Analytics] Purged {deleted} events older than {retention_days} days')
            except Exception as e:
                db.session.rollback()
                app.logger.error(f'[Analytics] Purge failed: {e}')

    scheduler.add_job(
        analytics_purge_job,
        trigger='cron',
        hour=3,
        minute=0,
        id='analytics_purge',
        replace_existing=True
    )

    if not scheduler.running:
        scheduler.start()
        print(f'[Scheduler] Started — staff auto-checkout daily at {app.config["STAFF_AUTO_CHECKOUT_TIME"]}')


################################################################
# [5] DEV ENTRY POINT
# Run locally:  python application.py
# Flask CLI:    flask run
# With env:     python-dotenv loads .env automatically if installed
################################################################

app = create_app()

if __name__ == '__main__':
    app.run(
        host='0.0.0.0',
        port=int(os.environ.get('PORT', 5000)),
        debug=(os.environ.get('FLASK_ENV') == 'development')
    )

################################################################
# [6] ANALYTICS HELPERS
#
# NOTE: All analytics collected here are used exclusively for
# internal site performance monitoring and improvement.
# No personal data is ever stored in analytics records.
# User identity is represented only as a role ('parent', 'staff',
# 'admin', 'anonymous') — never as a user ID, name, or email.
# Device type is inferred from User-Agent and the UA string is
# immediately discarded. No cookies are used for analytics.
# This data requires no GDPR consent banner.
################################################################

def _detect_device_type(user_agent: str) -> str:
    '''
    Infer device type from User-Agent string.
    Returns 'mobile', 'tablet', or 'desktop'.
    UA string itself is never stored.
    '''
    ua = (user_agent or '').lower()
    if any(t in ua for t in ('ipad', 'tablet', 'kindle', 'playbook')):
        return 'tablet'
    if any(t in ua for t in ('iphone', 'android', 'mobile', 'blackberry',
                              'windows phone', 'opera mini')):
        return 'mobile'
    return 'desktop'


def _capture_page_view(response):
    '''
    Record an anonymous page view event after each HTTP response.
    Called from the after_request hook — must never raise.

    Stores: route, method, user role, device type, status code,
            response time. Never stores: user ID, IP address, UA string.
    '''
    from flask_login import current_user
    from sub_modules.models import AnalyticsEvent

    elapsed_ms = int((time.time() - g.get('request_start_time', time.time())) * 1000)

    role = 'anonymous'
    if current_user and current_user.is_authenticated:
        role = current_user.role

    device = _detect_device_type(request.headers.get('User-Agent', ''))

    event = AnalyticsEvent(
        event_type='page_view',
        route=request.path[:100],
        method=request.method,
        user_role=role,
        device_type=device,
        status_code=response.status_code,
        response_ms=elapsed_ms
    )
    try:
        db.session.add(event)
        db.session.commit()
    except Exception:
        db.session.rollback()


def log_analytics_event(event_type: str, success: bool = None,
                        detail: str = None):
    '''
    Log a custom analytics event from anywhere in the app.
    Call this for meaningful actions beyond simple page views.

    Usage:
        from application import log_analytics_event
        log_analytics_event('checkin', success=True, detail='qr')
        log_analytics_event('login_fail')
        log_analytics_event('child_register_waitlisted', detail='U10')
    '''
    from flask_login import current_user
    from sub_modules.models import AnalyticsEvent

    role = 'anonymous'
    if current_user and current_user.is_authenticated:
        role = current_user.role

    device = _detect_device_type(request.headers.get('User-Agent', ''))

    event = AnalyticsEvent(
        event_type=event_type,
        route=request.path[:100] if request else None,
        method=request.method if request else None,
        user_role=role,
        device_type=device,
        success=success,
        detail=detail[:100] if detail else None
    )
    try:
        db.session.add(event)
        db.session.commit()
    except Exception:
        db.session.rollback()


################################################################
# [7] ERROR LOGGING HELPER
#
# NOTE: Error logs are used exclusively for diagnosing application
# issues. No personal data is stored — user role only, never user
# ID, name, email, or any identifying information. Error messages
# are stored as-is from exceptions; care should be taken in the
# rest of the codebase never to include personal data in exception
# messages or log calls.
################################################################

def _log_error(error_type: str, message: str = None,
               status_code: int = None):
    '''
    Write an entry to the error_log table.
    Called from error handlers and catch blocks throughout the app.

    Never stores user ID or personal data — role only.
    Safe to call from within error handlers (swallows its own exceptions).
    '''
    from flask_login import current_user
    from sub_modules.models import ErrorLog

    role = 'anonymous'
    try:
        if current_user and current_user.is_authenticated:
            role = current_user.role
    except Exception:
        pass

    try:
        entry = ErrorLog(
            error_type=error_type,
            route=request.path[:100] if request else None,
            method=request.method if request else None,
            user_role=role,
            message=str(message)[:500] if message else None,
            status_code=status_code
        )
        db.session.add(entry)
        db.session.commit()
    except Exception as inner:
        # Never let error logging crash the app
        try:
            db.session.rollback()
        except Exception:
            pass
        try:
            current_app.logger.error(f'[ErrorLog] Failed to write error log: {inner}')
        except Exception:
            pass
