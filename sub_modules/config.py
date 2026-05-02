'''
config.py
=========
Environment-based configuration using a class hierarchy.

    BaseConfig          Shared values — camp identity, GDPR, tokens, etc.
    DevelopmentConfig   SQLite + Mailpit + local file storage + relaxed security
    TestingConfig       In-memory SQLite + all side effects suppressed
    ProductionConfig    PostgreSQL + SendGrid + S3 + strict security

The active config is selected by the APP_ENV environment variable:

    APP_ENV=development   (default if not set)
    APP_ENV=testing
    APP_ENV=production

create_app() in application.py reads APP_ENV and loads the right class.

Fork this project? The only section you need to touch is BaseConfig, under
the CAMP IDENTITY and AGE GROUPS headings. Everything else is controlled by
environment variables.
'''

import os


class BaseConfig:
    # -------------------------------------------------------------------------
    # CAMP IDENTITY — update when forking. All can be overridden via .env.
    # -------------------------------------------------------------------------
    CAMP_NAME               = os.environ.get('CAMP_NAME',          'Fußballcamp')
    CAMP_CONTACT_EMAIL      = os.environ.get('CAMP_CONTACT_EMAIL', 'info@example.de')
    CAMP_CONTACT_PHONE      = os.environ.get('CAMP_CONTACT_PHONE', '')
    CAMP_LOCATION           = os.environ.get('CAMP_LOCATION',      'Sportplatz')
    CAMP_ORGANISER_NAME     = os.environ.get('CAMP_ORGANISER_NAME',    CAMP_NAME)
    CAMP_ORGANISER_ADDRESS  = os.environ.get('CAMP_ORGANISER_ADDRESS', '')
    CAMP_LOGO_PATH          = os.environ.get('CAMP_LOGO_PATH',      None)
    CAMP_LOGO_DARK_PATH     = os.environ.get('CAMP_LOGO_DARK_PATH', None)
    PRIVACY_POLICY_LAST_UPDATED = os.environ.get('PRIVACY_POLICY_LAST_UPDATED', '2025-01-01')

    # -------------------------------------------------------------------------
    # AGE GROUPS — seeds the database for a new camp session.
    # -------------------------------------------------------------------------
    DEFAULT_AGE_GROUPS = [
        {'name': 'U6',  'min_age': 4,  'max_age': 5,  'capacity': 15, 'display_order': 1},
        {'name': 'U8',  'min_age': 6,  'max_age': 7,  'capacity': 20, 'display_order': 2},
        {'name': 'U10', 'min_age': 8,  'max_age': 9,  'capacity': 20, 'display_order': 3},
        {'name': 'U12', 'min_age': 10, 'max_age': 11, 'capacity': 20, 'display_order': 4},
        {'name': 'U14', 'min_age': 12, 'max_age': 13, 'capacity': 20, 'display_order': 5},
        {'name': 'U16', 'min_age': 14, 'max_age': 15, 'capacity': 20, 'display_order': 6},
    ]

    STAFF_CLASSES = {
        'trainer': 'Trainer',
        'general': 'Allgemein',
        'food':    'Verpflegung',
    }

    # -------------------------------------------------------------------------
    # INTERNATIONALISATION (Flask-Babel)
    # -------------------------------------------------------------------------
    LANGUAGES            = ['de', 'en', 'ar']
    BABEL_DEFAULT_LOCALE = os.environ.get('DEFAULT_LOCALE', 'de')
    BABEL_DEFAULT_TIMEZONE = 'Europe/Berlin'

    # -------------------------------------------------------------------------
    # FLASK CORE
    # -------------------------------------------------------------------------
    SECRET_KEY                     = os.environ.get('SECRET_KEY') or 'dev-only-change-in-production'
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SESSION_PERMANENT              = False
    TEMPLATES_AUTO_RELOAD          = True
    MAX_CONTENT_LENGTH             = int(os.environ.get('MAX_UPLOAD_MB', 8)) * 1024 * 1024

    # -------------------------------------------------------------------------
    # GDPR / DATA RETENTION
    # -------------------------------------------------------------------------
    DATA_RETENTION_YEARS     = int(os.environ.get('DATA_RETENTION_YEARS', 2))
    CURRENT_CONSENT_VERSION  = '1.0'
    ANALYTICS_RETENTION_DAYS = int(os.environ.get('ANALYTICS_RETENTION_DAYS', 365))

    # -------------------------------------------------------------------------
    # CHECK-IN / SCHEDULER
    # -------------------------------------------------------------------------
    STAFF_AUTO_CHECKOUT_TIME = os.environ.get('STAFF_AUTO_CHECKOUT_TIME', '18:00')
    DISABLE_SCHEDULER        = os.environ.get('DISABLE_SCHEDULER', '0') == '1'

    # -------------------------------------------------------------------------
    # TOKENS
    # -------------------------------------------------------------------------
    INVITE_TOKEN_EXPIRY_HOURS = int(os.environ.get('INVITE_TOKEN_EXPIRY_HOURS', 72))
    VERIFY_TOKEN_EXPIRY_HOURS = int(os.environ.get('VERIFY_TOKEN_EXPIRY_HOURS', 24))

    # -------------------------------------------------------------------------
    # FILE UPLOADS / S3
    # -------------------------------------------------------------------------
    ALLOWED_IMAGE_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp'}
    MAX_IMAGE_SIZE_MB         = 8
    AWS_S3_BUCKET             = os.environ.get('AWS_S3_BUCKET')
    AWS_S3_REGION             = os.environ.get('AWS_S3_REGION', 'eu-central-1')

    # -------------------------------------------------------------------------
    # SECURITY DEFAULTS
    # -------------------------------------------------------------------------
    SESSION_COOKIE_HTTPONLY  = True
    SESSION_COOKIE_SAMESITE  = 'Lax'
    SESSION_COOKIE_SECURE    = False   # True in production
    REMEMBER_COOKIE_SECURE   = False
    REMEMBER_COOKIE_HTTPONLY = True
    WTF_CSRF_TIME_LIMIT      = 3600   # 1 hour; None in dev for convenience

    # bcrypt rounds: 12 in prod (~300 ms, intentional), 4 in dev/test (~1 ms)
    BCRYPT_LOG_ROUNDS = 12


class DevelopmentConfig(BaseConfig):
    '''
    Local development. Active when APP_ENV=development (the default).

    - SQLite database at dev.db — no server needed, gitignored
    - Mailpit catches all email — nothing reaches real inboxes
      Install: https://mailpit.axllent.org  |  Run: mailpit
      SMTP on localhost:1025, web UI at http://localhost:8025
    - Images saved to /tmp/fussballcamp_dev_uploads/ instead of S3
    - Debug mode on, CSRF never expires, cookies work over HTTP
    - Scheduler off — staff auto-checkout won't fire unexpectedly
    - bcrypt at 4 rounds — logins fast during development
    '''
    ENV   = 'development'
    DEBUG = True

    SQLALCHEMY_DATABASE_URI = os.environ.get('DATABASE_URL', 'sqlite:///dev.db')

    # Mailpit
    MAIL_SERVER         = os.environ.get('MAIL_SERVER',   'localhost')
    MAIL_PORT           = int(os.environ.get('MAIL_PORT', 1025))
    MAIL_USE_TLS        = False
    MAIL_USE_SSL        = False
    MAIL_USERNAME       = None
    MAIL_PASSWORD       = None
    MAIL_DEFAULT_SENDER = os.environ.get('MAIL_DEFAULT_SENDER', 'dev@localhost')
    MAIL_SUPPRESS_SEND  = os.environ.get('MAIL_SUPPRESS_SEND', '0') == '1'
    MAIL_DEBUG          = False

    AWS_S3_BUCKET          = None   # triggers local file fallback in image_mgmt.py
    SESSION_COOKIE_SECURE  = False
    REMEMBER_COOKIE_SECURE = False
    WTF_CSRF_TIME_LIMIT    = None   # CSRF tokens never expire in dev
    BCRYPT_LOG_ROUNDS      = 4
    DISABLE_SCHEDULER      = os.environ.get('DISABLE_SCHEDULER', '1') == '1'


class TestingConfig(BaseConfig):
    '''
    Used by pytest. Active when APP_ENV=testing.

    - In-memory SQLite — created fresh each run, nothing persists to disk
    - All external side effects suppressed (email, S3, APScheduler)
    - CSRF disabled — test clients submit forms without tokens
    - SERVER_NAME set so url_for() works outside a request context
    - Short token expiry so expiry-path tests don't need time.sleep()
    '''
    ENV     = 'testing'
    TESTING = True
    DEBUG   = True

    SQLALCHEMY_DATABASE_URI = os.environ.get('DATABASE_URL', 'sqlite:///:memory:')

    MAIL_SUPPRESS_SEND = True
    MAIL_SERVER        = 'localhost'
    MAIL_PORT          = 1025
    MAIL_USE_TLS       = False
    MAIL_USE_SSL       = False

    AWS_S3_BUCKET = None

    WTF_CSRF_ENABLED        = False   # forms submit without tokens in tests
    WTF_CSRF_CHECK_DEFAULT  = False   # belt-and-suspenders: disable check too
    BCRYPT_LOG_ROUNDS = 4
    DISABLE_SCHEDULER = True
    RATELIMIT_ENABLED = False        # prevent 429s during tests
    # SERVER_NAME intentionally not set — setting it causes Flask to enforce
    # hostname matching in the test client, turning valid requests into 302s.

    INVITE_TOKEN_EXPIRY_HOURS = 1
    VERIFY_TOKEN_EXPIRY_HOURS = 1


class ProductionConfig(BaseConfig):
    '''
    Live server. Active when APP_ENV=production.

    All sensitive values must be environment variables — never hardcoded.
    Missing DATABASE_URL or SENDGRID_API_KEY will cause explicit startup errors.
    '''
    ENV   = 'production'
    DEBUG = False

    # Render and Railway provide DATABASE_URL as postgres:// (legacy scheme).
    # SQLAlchemy 1.4+ requires postgresql://. Fix it silently here.
    _db_url = os.environ.get('DATABASE_URL', '')
    if _db_url.startswith('postgres://'):
        _db_url = _db_url.replace('postgres://', 'postgresql://', 1)
    SQLALCHEMY_DATABASE_URI = _db_url or None

    MAIL_SERVER         = 'smtp.sendgrid.net'
    MAIL_PORT           = 465
    MAIL_USE_TLS        = False
    MAIL_USE_SSL        = True
    MAIL_USERNAME       = 'apikey'   # SendGrid requires the literal string 'apikey'
    MAIL_PASSWORD       = os.environ.get('SENDGRID_API_KEY')
    MAIL_DEFAULT_SENDER = os.environ.get('MAIL_DEFAULT_SENDER', BaseConfig.CAMP_CONTACT_EMAIL)
    MAIL_SUPPRESS_SEND  = os.environ.get('MAIL_SUPPRESS_SEND', '0') == '1'

    SESSION_COOKIE_SECURE  = True
    REMEMBER_COOKIE_SECURE = True
    DISABLE_SCHEDULER      = False


# =============================================================================
# REGISTRY — imported by create_app() in application.py
# =============================================================================

config_by_name = {
    'development': DevelopmentConfig,
    'testing':     TestingConfig,
    'production':  ProductionConfig,
}

DEFAULT_CONFIG = 'development'


# =============================================================================
# COMPATIBILITY SHIM
# Views import bare names like `from sub_modules.config import STAFF_CLASSES`.
# These module-level aliases keep those imports working regardless of environment.
# The values come from BaseConfig (non-sensitive, always the same).
# =============================================================================

CAMP_NAME                   = BaseConfig.CAMP_NAME
CAMP_CONTACT_EMAIL          = BaseConfig.CAMP_CONTACT_EMAIL
CAMP_CONTACT_PHONE          = BaseConfig.CAMP_CONTACT_PHONE
CAMP_LOCATION               = BaseConfig.CAMP_LOCATION
CAMP_ORGANISER_NAME         = BaseConfig.CAMP_ORGANISER_NAME
CAMP_ORGANISER_ADDRESS      = BaseConfig.CAMP_ORGANISER_ADDRESS
PRIVACY_POLICY_LAST_UPDATED = BaseConfig.PRIVACY_POLICY_LAST_UPDATED
DEFAULT_AGE_GROUPS          = BaseConfig.DEFAULT_AGE_GROUPS
STAFF_CLASSES               = BaseConfig.STAFF_CLASSES
CURRENT_CONSENT_VERSION     = BaseConfig.CURRENT_CONSENT_VERSION
DATA_RETENTION_YEARS        = BaseConfig.DATA_RETENTION_YEARS
INVITE_TOKEN_EXPIRY_HOURS   = BaseConfig.INVITE_TOKEN_EXPIRY_HOURS
VERIFY_TOKEN_EXPIRY_HOURS   = BaseConfig.VERIFY_TOKEN_EXPIRY_HOURS
ANALYTICS_RETENTION_DAYS    = BaseConfig.ANALYTICS_RETENTION_DAYS
ALLOWED_IMAGE_EXTENSIONS    = BaseConfig.ALLOWED_IMAGE_EXTENSIONS
