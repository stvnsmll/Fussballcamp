# Fußballcamp

A self-hostable, open-source web app for managing a free youth soccer camp.  
Built with Flask, PostgreSQL, and Bootstrap 5. Designed for German-speaking clubs, GDPR-compliant, and fully forkable.

---

## Features

- Parent accounts with email verification
- Multi-child registration per parent account (children reusable across years)
- Automatic age group assignment (based on age on camp start date) with admin override
- Waitlist management (manual — admin communicates with families directly)
- Staff accounts (Trainer, Allgemein, Verpflegung) via invite link
- First aid flag for any staff member
- Head coach assignment per age group, swappable by admin
- Mobile-first check-in / check-out (name search + QR code)
- Staff auto-checkout at configurable daily time
- Announcements with photo attachments, pinning, and age group targeting
- Internal staff messaging with tags
- GDPR-compliant: consent versioning, data retention tracking, soft deletion, Datenschutzerklärung page
- Google Translate widget for multilingual access
- Fake data seeder for local development and testing

---

## Self-Hosting (Fork This Project)

To run your own instance:

1. **Clone the repo**
2. **Update `sub_modules/config.py`** with your camp name, contact email, age groups, etc.
3. **Fill in `.env`** (copy from `.env.example`)
4. **Deploy** to Render, Railway, or your own server

That's it. No other files need changing for a basic setup.

---

## Local Development Setup

### Prerequisites

- Python 3.11+
- [Mailpit](https://mailpit.axllent.org) — catches all outgoing email in development

### First-time setup

```bash
# Clone and enter the project
git clone https://github.com/yourname/fussballcamp.git
cd fussballcamp

# Create and activate a virtual environment
python -m venv venv
source venv/bin/activate      # Mac/Linux
venv\Scripts\activate         # Windows

# Install dependencies
pip install -r requirements.txt

# Copy the example env file and set a secret key
cp .env.example .env
# Open .env and set SECRET_KEY to any random string.
# Everything else works with the defaults for local development.

# Create the database and run migrations
flask db upgrade

# Populate with realistic fake data
flask seed
# Creates: 1 admin, 5 staff, 30 parents with children,
#          1 camp session, registrations at various states,
#          sample check-ins for today.
#
# Credentials printed to terminal. Default:
#   Admin:  admin@example.com / admin1234
#   Staff:  trainer1@example.com / staff1234
```

### Running the app

Open two terminals:

```bash
# Terminal 1 — email catcher (install once from https://mailpit.axllent.org)
mailpit
# SMTP on localhost:1025 | Web UI at http://localhost:8025

# Terminal 2 — Flask dev server
flask run
# App at http://localhost:5000
```

Any email the app sends (verification, password reset, invites) will appear
in the Mailpit inbox at **http://localhost:8025** instead of going anywhere real.

### Resetting to a clean state

```bash
flask seed          # Wipes and reseeds (prompts for confirmation)
flask seed --keep   # Reseeds without touching your own test data
```

### Environment modes

`APP_ENV` in your `.env` controls which config is loaded:

| `APP_ENV`     | Database          | Email         | S3 / images       |
|---------------|-------------------|---------------|-------------------|
| `development` | SQLite (`dev.db`) | Mailpit       | Local `/tmp/`     |
| `testing`     | In-memory SQLite  | Suppressed    | None              |
| `production`  | PostgreSQL        | SendGrid      | AWS S3            |

You never need to change which database driver is installed or swap out
config variables manually — just change `APP_ENV`.

### Running tests

```bash
# pytest sets APP_ENV=testing automatically via conftest.py
pytest
pytest tests/test_helpers.py -v   # single file
pytest -k "age_group"             # by name pattern
```

---

## Production Deployment (Render)

1. Push your repo to GitHub
2. Create a new Web Service on [render.com](https://render.com)
3. Set environment variables in Render dashboard (see `.env.example`)
4. Add a PostgreSQL database via Render's Add-on (free tier)
5. Set `DATABASE_URL` to the Render PostgreSQL connection string
6. Deploy — Render runs `gunicorn application:app` automatically via Procfile

---

## Project Structure

```
fussballcamp/
├── application.py          # Flask app factory, extensions, scheduler
├── requirements.txt
├── Procfile
├── .env.example
├── views/                  # Blueprints (one per feature area)
│   ├── auth.py             # Register, login, verify, invites
│   ├── parents.py          # Child management, registration
│   ├── staff.py            # Check-in/out, roster
│   ├── admin.py            # Camp & user management
│   ├── announcements.py    # Announcements feed
│   └── public.py           # Landing page
├── sub_modules/
│   ├── config.py           # All camp-specific config (fork here)
│   ├── models.py           # SQLAlchemy models
│   ├── helpers.py          # Shared utilities, auto-checkout logic
│   ├── emails.py           # SendGrid email functions
│   ├── image_mgmt.py       # S3 image upload/fetch
│   └── seed.py             # Fake data generator
├── admin_tools/
│   └── retention_check.py  # GDPR retention script (run annually)
├── templates/              # Jinja2 HTML templates
└── static/                 # CSS, JS, images
```

---

## GDPR Notes

- Parents give explicit consent (unchecked checkbox) at signup
- Consent version is recorded against each account
- Accounts are flagged for deletion 2 years after last camp participation
- A warning email is sent before deletion
- Parents can request data export or deletion from their account page
- Run `python admin_tools/retention_check.py` annually to flag and notify stale accounts
- `DATA_RETENTION_YEARS` is configurable in `.env`

---

## License

MIT — free to use, fork, and adapt. Attribution appreciated but not required.
