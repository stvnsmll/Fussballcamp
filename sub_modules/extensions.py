'''
sub_modules/extensions.py
==========================
All Flask extension instances and shared utility functions.

Kept in a separate file so that views, models, and helpers can all import
from here without creating circular imports through application.py.

Import pattern everywhere else in the codebase:
    from sub_modules.extensions import db, mail, limiter
    from sub_modules.extensions import log_analytics_event, _log_error
'''

from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate
from flask_login import LoginManager
from flask_mail import Mail
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_babel import Babel
from apscheduler.schedulers.background import BackgroundScheduler

# =============================================================================
# Extension instances
# Initialised here, bound to the app inside create_app() via .init_app(app)
# =============================================================================

db            = SQLAlchemy()
migrate       = Migrate()
login_manager = LoginManager()
mail          = Mail()
limiter       = Limiter(key_func=get_remote_address)
babel         = Babel()
scheduler     = BackgroundScheduler()


# =============================================================================
# Device detection helper
# =============================================================================

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


# =============================================================================
# Analytics event logger
# =============================================================================

def log_analytics_event(event_type: str, success: bool = None,
                        detail: str = None):
    '''
    Log a custom analytics event from anywhere in the app.

    Usage:
        from sub_modules.extensions import log_analytics_event
        log_analytics_event('checkin', success=True, detail='qr')
        log_analytics_event('login_fail')
    '''
    from flask import request
    from flask_login import current_user
    from sub_modules.models import AnalyticsEvent

    role = 'anonymous'
    try:
        if current_user and current_user.is_authenticated:
            role = current_user.role
    except Exception:
        pass

    try:
        device = _detect_device_type(request.headers.get('User-Agent', ''))
        event = AnalyticsEvent(
            event_type=event_type,
            route=request.path[:100] if request else None,
            method=request.method if request else None,
            user_role=role,
            device_type=device,
            success=success,
            detail=detail[:100] if detail else None,
        )
        db.session.add(event)
        db.session.commit()
    except Exception:
        try:
            db.session.rollback()
        except Exception:
            pass


# =============================================================================
# Error logger
# =============================================================================

def _log_error(error_type: str, message: str = None, status_code: int = None):
    '''
    Write an entry to the error_log table.
    Never stores user ID or personal data — role only.
    Safe to call from within error handlers (swallows its own exceptions).
    '''
    from flask import request, current_app
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
            status_code=status_code,
        )
        db.session.add(entry)
        db.session.commit()
    except Exception as inner:
        try:
            db.session.rollback()
        except Exception:
            pass
        try:
            current_app.logger.error(f'[ErrorLog] Failed to write error log: {inner}')
        except Exception:
            pass
