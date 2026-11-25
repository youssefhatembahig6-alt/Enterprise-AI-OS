"""M6 review round 3: attempts made before SMTP-start records existed count as possibly sent.

Workers older than 0014 sent email without writing ``email_smtp_attempts`` rows, so
after upgrading, a missing record does not prove an attempt never reached SMTP. This
migration closes that gap conservatively for every outbox row that had any attempt
before the upgrade and is not finished:

* ``dispatching`` and ``uncertain`` rows (a crash or an ambiguous SMTP outcome), and
* ``pending`` rows with ``attempts > 0``: an attempt re-queued by the old
  reconciliation (``reconciled_absent_retry``) or after a pre-DATA failure. The row
  keeps only its latest reason, so an earlier attempt of the same row may have been
  ambiguous; all of them are treated alike.

For each such row it records every attempt ``1..attempts`` as possibly sent and moves
the row to ``uncertain`` (``pre_0014_attempt_possibly_sent``). Reconciliation then
marks it delivered if Mailpit still holds the Message-ID, and otherwise keeps it
uncertain without another SMTP call. Rows with no attempt (``pending``, attempts 0)
and finished rows are untouched; new attempts get records from the running code, so
attempts that provably stop before SMTP are still retried automatically.

Run it with workers stopped (Compose runs migrations before starting them). The data
change is not reverted on downgrade: un-marking attempts could cause a resend.
"""

from alembic import op

revision = "0015_backfill_smtp_attempts"
down_revision = "0014_email_smtp_attempts"
branch_labels = None
depends_on = None

REASON = "pre_0014_attempt_possibly_sent"


def upgrade() -> None:
    op.get_bind().exec_driver_sql(
        f"""
        CREATE TEMP TABLE m6_possibly_sent ON COMMIT DROP AS
            SELECT execution_key, tenant_id, action_id, attempts
              FROM email_outbox
             WHERE attempts > 0
               AND (status IN ('dispatching', 'uncertain') OR status = 'pending');

        INSERT INTO email_smtp_attempts (tenant_id, execution_key, attempt)
        SELECT p.tenant_id, p.execution_key, n
          FROM m6_possibly_sent p, generate_series(1, p.attempts) AS n
        ON CONFLICT DO NOTHING;

        UPDATE email_outbox o
           SET status = 'uncertain',
               status_reason = '{REASON}',
               uncertain_since = COALESCE(o.uncertain_since, clock_timestamp()),
               dispatched_at = NULL,
               updated_at = clock_timestamp()
          FROM m6_possibly_sent p
         WHERE o.execution_key = p.execution_key;

        UPDATE email_actions a
           SET status = 'uncertain', status_reason = '{REASON}', updated_at = clock_timestamp()
          FROM m6_possibly_sent p
         WHERE a.tenant_id = p.tenant_id AND a.id = p.action_id;

        INSERT INTO audit_events (tenant_id, event_type, action, decision, reason, details)
        SELECT p.tenant_id, 'action.email', 'email.deliver', 'info', '{REASON}',
               jsonb_build_object('action_id', p.action_id::text,
                                  'execution_key', p.execution_key::text,
                                  'attempt', p.attempts, 'source', 'migration_0015')
          FROM m6_possibly_sent p;
        """
    )


def downgrade() -> None:
    # Deliberately keeps the records and states: removing them could cause a resend.
    pass
