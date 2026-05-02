'''
admin_tools/retention_check.py
==============================
GDPR data retention enforcement. Runs annually.

Two-phase process:

  Phase 1 — WARN
    Parents who have not registered a child in the past DATA_RETENTION_YEARS-1
    years (default: 1 year) receive a warning email. Their account is flagged
    with retention_warning_sent_at so Phase 2 knows to act next cycle.

  Phase 2 — DELETE
    Parents who were warned at least DATA_RETENTION_YEARS-1 years ago and still
    have not engaged are soft-deleted. All child records anonymised. Registration
    history retained for statistics but fully de-identified.

Usage:
    flask retention-check              # live run (warns + deletes)
    flask retention-check --dry-run    # preview only, no changes made
    flask retention-check --warn-only  # only send warnings, skip deletions
    flask retention-check --delete-only # only delete, skip warnings

Safety:
    --dry-run is strongly recommended before first live run.
    Soft deletes are reversible by an admin with direct DB access
    within a reasonable window.

GDPR basis:
    Article 5(1)(e) — storage limitation principle.
    Retention period configurable via DATA_RETENTION_YEARS in config.py.
    Warning email sent at DATA_RETENTION_YEARS - 1 years inactivity.
    Deletion at DATA_RETENTION_YEARS years inactivity.
'''

import click
from datetime import datetime, date, timedelta
from flask import current_app
from flask.cli import with_appcontext

from application import db
from sub_modules.models import User, Child, Registration
from sub_modules.helpers import soft_delete_user
from sub_modules.emails import send_deletion_warning_email, log_email
from sub_modules.config import DATA_RETENTION_YEARS


def register_retention_commands(app):
    '''Register `flask retention-check` CLI command with the app.'''
    app.cli.add_command(retention_check_command)


@click.command('retention-check')
@click.option('--dry-run', is_flag=True, default=False,
              help='Preview actions without making any changes.')
@click.option('--warn-only', is_flag=True, default=False,
              help='Send warnings only. Skip deletions.')
@click.option('--delete-only', is_flag=True, default=False,
              help='Process deletions only. Skip warnings.')
@with_appcontext
def retention_check_command(dry_run, warn_only, delete_only):
    '''Run the annual GDPR data retention check.'''
    runner = RetentionRunner(
        dry_run=dry_run,
        warn_only=warn_only,
        delete_only=delete_only,
    )
    runner.run()


# =============================================================================
# RUNNER
# =============================================================================

class RetentionRunner:

    def __init__(self, dry_run=False, warn_only=False, delete_only=False):
        self.dry_run     = dry_run
        self.warn_only   = warn_only
        self.delete_only = delete_only

        self.warn_threshold   = DATA_RETENTION_YEARS - 1
        self.delete_threshold = DATA_RETENTION_YEARS

        self.today         = date.today()
        self.warn_cutoff   = self.today - timedelta(days=self.warn_threshold * 365)
        self.delete_cutoff = self.today - timedelta(days=self.delete_threshold * 365)

        self.warned  = []
        self.deleted = []
        self.skipped = []
        self.errors  = []

    def run(self):
        self._print_header()

        if not self.delete_only:
            self._phase_warn()

        if not self.warn_only:
            self._phase_delete()

        self._print_report()

    # -------------------------------------------------------------------------
    # PHASE 1 — WARN
    # -------------------------------------------------------------------------

    def _phase_warn(self):
        click.echo(
            f'\n[Phase 1 — Warn] Inactive >{self.warn_threshold}y '
            f'— cutoff: {self.warn_cutoff}'
        )

        candidates = User.query.filter(
            User.role == 'parent',
            User.is_deleted == False,
            User.retention_warning_sent_at == None,
            User.last_active_year != None,
        ).all()

        warn_year_threshold = self.warn_cutoff.year

        for user in candidates:
            if user.last_active_year > warn_year_threshold:
                continue
            try:
                self._warn_user(user)
            except Exception as e:
                self._record_error(user, 'warn', e)

    def _warn_user(self, user: User):
        years_inactive = self.today.year - user.last_active_year
        deletion_year  = self.today.year + (self.delete_threshold - self.warn_threshold)

        if self.dry_run:
            click.echo(
                f'  [DRY RUN] Would warn: {user.email} '
                f'(inactive {years_inactive}y, deletion due {deletion_year})'
            )
            self.warned.append({'user': user, 'dry_run': True})
            return

        try:
            send_deletion_warning_email(user, deletion_year)
            log_email(user.id, user.email, 'deletion_warning',
                      f'Datenlöschung geplant für {deletion_year}')
        except Exception as e:
            # Log email failure but still flag the account —
            # better to flag than to silently skip the deletion cycle
            current_app.logger.error(
                f'[Retention] Warning email failed for {user.email}: {e}'
            )
            self._record_error(user, 'warn_email', e)

        user.retention_warning_sent_at = datetime.utcnow()
        db.session.commit()

        current_app.logger.info(
            f'[Retention] Warned user {user.id} ({user.email}) — '
            f'inactive since {user.last_active_year}, deletion due {deletion_year}'
        )
        self.warned.append({
            'user':           user,
            'inactive_since': user.last_active_year,
            'deletion_year':  deletion_year,
            'dry_run':        False,
        })

    # -------------------------------------------------------------------------
    # PHASE 2 — DELETE
    # -------------------------------------------------------------------------

    def _phase_delete(self):
        click.echo(
            f'\n[Phase 2 — Delete] Warned >{self.warn_threshold}y ago '
            f'— cutoff: {self.delete_cutoff}'
        )

        delete_datetime_cutoff = datetime.combine(
            self.delete_cutoff, datetime.min.time()
        )

        candidates = User.query.filter(
            User.role == 'parent',
            User.is_deleted == False,
            User.retention_warning_sent_at != None,
            User.retention_warning_sent_at < delete_datetime_cutoff,
        ).all()

        for user in candidates:
            # Re-engaged after warning — reset their clock and skip
            if user.last_active_year and \
               user.last_active_year >= self.warn_cutoff.year:
                # Clear the warning flag so the next cycle starts fresh
                user.retention_warning_sent_at = None
                db.session.commit()
                self.skipped.append({
                    'user':   user,
                    'reason': f'Re-engaged in {user.last_active_year} after warning — clock reset'
                })
                current_app.logger.info(
                    f'[Retention] Skipping user {user.id} — '
                    f're-engaged in {user.last_active_year}, warning cleared'
                )
                continue

            # Guard: should not have active registrations, but check anyway
            active_regs = Registration.query.join(Child).filter(
                Child.parent_user_id == user.id,
                Registration.status.in_(['confirmed', 'waitlisted',
                                          'pending_verification'])
            ).count()

            if active_regs > 0:
                self.skipped.append({
                    'user':   user,
                    'reason': f'{active_regs} active registration(s) — requires admin review'
                })
                current_app.logger.warning(
                    f'[Retention] Skipping user {user.id} — '
                    f'{active_regs} active registration(s), manual review needed'
                )
                continue

            try:
                self._delete_user(user)
            except Exception as e:
                self._record_error(user, 'delete', e)

    def _delete_user(self, user: User):
        warned_at   = user.retention_warning_sent_at.date() \
                      if user.retention_warning_sent_at else 'unknown'
        child_count = Child.query.filter_by(parent_user_id=user.id).count()

        if self.dry_run:
            click.echo(
                f'  [DRY RUN] Would delete: {user.email} '
                f'(warned {warned_at}, {child_count} child record(s))'
            )
            self.deleted.append({'user': user, 'warned_at': warned_at, 'dry_run': True})
            return

        success, message = soft_delete_user(user, reason='retention_auto')

        if not success:
            raise RuntimeError(message)

        current_app.logger.info(
            f'[Retention] Deleted user {user.id} — '
            f'warned {warned_at}, {child_count} child record(s) anonymised'
        )
        self.deleted.append({
            'user':      user,
            'warned_at': warned_at,
            'children':  child_count,
            'dry_run':   False,
        })

    # -------------------------------------------------------------------------
    # REPORT
    # -------------------------------------------------------------------------

    def _record_error(self, user: User, phase: str, exc: Exception):
        self.errors.append({
            'user_id': user.id,
            'email':   user.email,
            'phase':   phase,
            'error':   str(exc),
        })
        current_app.logger.error(
            f'[Retention] Error in phase={phase} for user {user.id}: {exc}'
        )

    def _print_header(self):
        flags = []
        if self.dry_run:     flags.append('DRY RUN')
        if self.warn_only:   flags.append('WARN ONLY')
        if self.delete_only: flags.append('DELETE ONLY')
        mode = ' | '.join(flags) if flags else 'LIVE'

        click.echo('=' * 60)
        click.echo(f'  GDPR Retention Check  [{mode}]')
        click.echo(f'  Date:             {self.today}')
        click.echo(f'  Retention period: {self.delete_threshold} year(s)')
        click.echo(f'  Warn cutoff:      {self.warn_cutoff}  (>{self.warn_threshold}y inactive)')
        click.echo(f'  Delete cutoff:    {self.delete_cutoff}  (>{self.delete_threshold}y inactive)')
        click.echo('=' * 60)

    def _print_report(self):
        click.echo('\n' + '=' * 60)
        click.echo('  REPORT')
        click.echo('=' * 60)

        click.echo(f'\nWarnings sent:    {len(self.warned)}')
        for e in self.warned:
            tag = '[DRY RUN] ' if e.get('dry_run') else ''
            suffix = (f'inactive since {e.get("inactive_since")}, '
                      f'deletion due {e.get("deletion_year")}') \
                     if not e.get('dry_run') else ''
            click.echo(f'  {tag}{e["user"].email}' + (f' — {suffix}' if suffix else ''))

        click.echo(f'\nAccounts deleted: {len(self.deleted)}')
        for e in self.deleted:
            tag = '[DRY RUN] ' if e.get('dry_run') else ''
            suffix = (f'warned {e.get("warned_at")}, '
                      f'{e.get("children", "?")} child record(s)') \
                     if not e.get('dry_run') else ''
            click.echo(f'  {tag}{e["user"].email}' + (f' — {suffix}' if suffix else ''))

        if self.skipped:
            click.echo(f'\nSkipped (review): {len(self.skipped)}')
            for e in self.skipped:
                click.echo(f'  {e["user"].email} — {e["reason"]}')

        if self.errors:
            click.echo(f'\nErrors:           {len(self.errors)}')
            for e in self.errors:
                click.echo(
                    f'  user_id={e["user_id"]} ({e["email"]}) '
                    f'[{e["phase"]}]: {e["error"]}'
                )

        click.echo('')
        if self.dry_run:
            click.echo('  *** DRY RUN — no changes were made ***')
            click.echo('  Run without --dry-run to apply.')
        elif self.errors:
            click.echo('  Completed with errors — review log output above.')
        else:
            click.echo('  Completed successfully.')
        click.echo('=' * 60)
