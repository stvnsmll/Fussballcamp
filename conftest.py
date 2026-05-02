"""
conftest.py
===========
pytest configuration and shared fixtures.
"""

import os
import pytest

os.environ.setdefault('APP_ENV', 'testing')
os.environ.setdefault('SECRET_KEY', 'test-secret-key')


@pytest.fixture(scope='function')
def app(tmp_path):
    """Fresh Flask app + SQLite file per test."""
    import os as _os
    from application import create_app
    from sub_modules.extensions import db as _database

    db_path = str(tmp_path / 'test.db')

    # Set the DB path BEFORE create_app so the engine is built with the right URI
    _os.environ['DATABASE_URL'] = f'sqlite:///{db_path}'

    application = create_app('testing')

    # Force test-critical config after create_app() in case class attrs were frozen
    application.config['WTF_CSRF_ENABLED'] = False
    application.config['WTF_CSRF_CHECK_DEFAULT'] = False
    application.config['WTF_CSRF_METHODS'] = []
    application.config['RATELIMIT_ENABLED'] = False

    with application.app_context():
        _database.create_all()
        yield application
        _database.session.remove()
        _database.drop_all()

    # Clean up env var so it doesn't bleed into other tests
    _os.environ.pop('DATABASE_URL', None)


@pytest.fixture(scope='function')
def db(app):
    """Return the db instance bound to the current app context."""
    from sub_modules.extensions import db as _database
    return _database


@pytest.fixture(scope='function')
def client(app, db):
    """Flask test client."""
    with app.test_client() as c:
        yield c


@pytest.fixture(scope='function')
def runner(app):
    """Flask CLI runner."""
    return app.test_cli_runner()


@pytest.fixture(scope='function')
def admin(db):
    from sub_modules.models import User
    from sub_modules.helpers import hash_password
    user = User(
        first_name='Test', last_name='Admin',
        email='admin@test.com',
        password_hash=hash_password('testpass123'),
        role='admin', email_verified=True,
        is_active=True, consent_version='1.0',
    )
    db.session.add(user)
    db.session.commit()
    return user


@pytest.fixture(scope='function')
def parent(db):
    from sub_modules.models import User
    from sub_modules.helpers import hash_password
    user = User(
        first_name='Test', last_name='Elternteil',
        email='parent@test.com',
        password_hash=hash_password('testpass123'),
        role='parent', email_verified=True,
        is_active=True, consent_version='1.0',
    )
    db.session.add(user)
    db.session.commit()
    return user


@pytest.fixture(scope='function')
def staff(db):
    from sub_modules.models import User, StaffProfile
    from sub_modules.helpers import hash_password
    user = User(
        first_name='Test', last_name='Trainer',
        email='staff@test.com',
        password_hash=hash_password('testpass123'),
        role='staff', email_verified=True,
        is_active=True, consent_version='1.0',
    )
    db.session.add(user)
    db.session.flush()
    db.session.add(StaffProfile(user_id=user.id, staff_class='trainer'))
    db.session.commit()
    return user


@pytest.fixture(scope='function')
def logged_in_admin(client, admin):
    with client.session_transaction() as sess:
        sess['_user_id'] = str(admin.id)
        sess['_fresh'] = True
    return client


@pytest.fixture(scope='function')
def logged_in_parent(client, parent):
    with client.session_transaction() as sess:
        sess['_user_id'] = str(parent.id)
        sess['_fresh'] = True
    return client
