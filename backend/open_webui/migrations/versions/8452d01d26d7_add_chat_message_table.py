"""Add chat_message table

Revision ID: 8452d01d26d7
Revises: 374d2f66af06
Create Date: 2026-02-01 04:00:00.000000

"""

import json
import logging
import time
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

log = logging.getLogger(__name__)

# Progress is reported through Alembic's own logger: alembic.ini only lets
# INFO messages through for the "alembic" loggers, so they appear next to the
# "Running upgrade ..." lines.
progress_log = logging.getLogger('alembic.runtime.migration')

# Seconds between two progress lines during the backfill
PROGRESS_INTERVAL = 30

revision: str = '8452d01d26d7'
down_revision: Union[str, None] = '374d2f66af06'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

BATCH_SIZE = 5000

# Helper table handed over to revision 9a1b2c3d4e5f (add current_message_id to
# chat). That revision needs each chat's "current message", which this backfill
# can decide while it has the chat document and its messages at hand. Recording
# it here saves that revision from reading every chat document a second time
# and from looking every candidate up in chat_message. It drops the table when
# it is done, and works without it if the table is absent.
CURRENT_MESSAGE_HINT_TABLE = 'migration_chat_current_message_hint'

# Chats that had a message rejected (or removed as a duplicate) get no hint, so
# revision 9a1b2c3d4e5f works them out itself. If that ever concerns more chats
# than this, something is systematically wrong: discard every hint.
MAX_UNCERTAIN_CHATS = 50000

# PostgreSQL: remove later copies of a duplicated message id, keeping the row
# that was inserted first - the one a primary key would have let in.
_PG_REMOVE_DUPLICATES_SQL = """
DELETE FROM chat_message
WHERE ctid IN (
    SELECT ctid
    FROM (
        SELECT ctid, row_number() OVER (PARTITION BY id ORDER BY ctid) AS position
        FROM chat_message
    ) numbered
    WHERE position > 1
)
RETURNING chat_id
"""


def _dumps(value):
    """
    Serialize like the application does (ensure_ascii=False), so non-ASCII text
    is stored as UTF-8 instead of six-byte \\uXXXX escapes. This roughly halves
    the stored size of CJK text. A value containing an unpaired surrogate cannot
    be encoded as UTF-8; keep the escaped form for it.
    """
    text = json.dumps(value, ensure_ascii=False)
    try:
        text.encode('utf-8')
    except UnicodeEncodeError:
        text = json.dumps(value)
    return text


class _JSON(sa.types.TypeDecorator):
    """JSON column whose values are serialized with _dumps()."""

    impl = sa.JSON
    cache_ok = True

    def bind_processor(self, dialect):
        return _dumps


def _duration(seconds):
    """Seconds as a short human-readable duration, e.g. 1h05m or 3m20s."""
    seconds = int(seconds)
    if seconds >= 3600:
        return f'{seconds // 3600}h{seconds % 3600 // 60:02d}m'
    if seconds >= 60:
        return f'{seconds // 60}m{seconds % 60:02d}s'
    return f'{seconds}s'


def _timed_step(position, total, label, action):
    """Run one of the steps that follow the backfill and report how long it took."""
    started = time.monotonic()
    progress_log.info(f'chat_message: step {position} of {total}: {label} ...')
    action()
    progress_log.info(
        f'chat_message: step {position} of {total}: {label} done in {_duration(time.monotonic() - started)}'
    )


def _hint_candidate(value, prefix):
    """A current-message candidate as revision 9a1b2c3d4e5f normalizes it."""
    if not isinstance(value, str) or not value:
        return None
    return value[len(prefix) :] if value.startswith(prefix) else value


def _flush_batch(conn, table, batch, uncertain_chats):
    """
    Insert a batch of messages, falling back to row-by-row on error.

    Tries a single bulk insert first (fast path). If that fails (e.g. due to
    a duplicate key), falls back to individual inserts wrapped in savepoints
    so the rest of the batch can still succeed. Chats that had a message
    rejected are added to uncertain_chats.
    """
    savepoint = conn.begin_nested()
    try:
        conn.execute(sa.insert(table), batch)
        savepoint.commit()
        return len(batch), 0
    except Exception:
        savepoint.rollback()
        # Batch failed - insert one-by-one to isolate the bad row(s)
        inserted = 0
        failed = 0
        for msg in batch:
            sp = conn.begin_nested()
            try:
                conn.execute(sa.insert(table).values(**msg))
                sp.commit()
                inserted += 1
            except Exception as e:
                sp.rollback()
                failed += 1
                uncertain_chats.add(msg['chat_id'])
                log.warning(f'Failed to insert message {msg["id"]}: {e}')
        return inserted, failed


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    existing_tables = set(inspector.get_table_names())

    if 'chat_message' in existing_tables:
        return  # Already created — skip everything

    # Step 1: Create table
    #
    # The indexes and the foreign key are added after the backfill (Step 3).
    # Maintaining them row by row during a large backfill means random reads
    # and writes all over each index once it no longer fits in memory; building
    # them once at the end, from sorted data, is much cheaper.
    #
    # On PostgreSQL this includes the primary key. Its job during the backfill
    # is to reject a duplicated message id; Step 3 removes such duplicates
    # before creating it, keeping the row inserted first, which gives the same
    # result. Other databases keep the primary key from the start.
    #
    # SQLite cannot add a foreign key to an existing table, so there the
    # constraint is declared inline as before.
    is_sqlite = conn.dialect.name == 'sqlite'
    is_postgresql = conn.dialect.name == 'postgresql'

    table_args = [
        sa.Column('id', sa.Text(), primary_key=not is_postgresql, nullable=False),
        sa.Column('chat_id', sa.Text(), nullable=False),
        sa.Column('user_id', sa.Text()),
        sa.Column('role', sa.Text(), nullable=False),
        sa.Column('parent_id', sa.Text(), nullable=True),
        sa.Column('content', sa.JSON(), nullable=True),
        sa.Column('output', sa.JSON(), nullable=True),
        sa.Column('model_id', sa.Text(), nullable=True),
        sa.Column('files', sa.JSON(), nullable=True),
        sa.Column('sources', sa.JSON(), nullable=True),
        sa.Column('embeds', sa.JSON(), nullable=True),
        sa.Column('done', sa.Boolean(), default=True),
        sa.Column('status_history', sa.JSON(), nullable=True),
        sa.Column('error', sa.JSON(), nullable=True),
        sa.Column('usage', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.BigInteger()),
        sa.Column('updated_at', sa.BigInteger()),
    ]
    if is_sqlite:
        table_args.append(sa.ForeignKeyConstraint(['chat_id'], ['chat.id'], ondelete='CASCADE'))
    op.create_table('chat_message', *table_args)

    op.create_table(
        CURRENT_MESSAGE_HINT_TABLE,
        sa.Column('chat_id', sa.Text(), primary_key=True),
        sa.Column('message_id', sa.Text(), nullable=False),
    )

    # Step 2: Backfill from existing chats
    chat_table = sa.table(
        'chat',
        sa.column('id', sa.Text()),
        sa.column('user_id', sa.Text()),
        sa.column('chat', sa.JSON()),
    )

    chat_message_table = sa.table(
        'chat_message',
        sa.column('id', sa.Text()),
        sa.column('chat_id', sa.Text()),
        sa.column('user_id', sa.Text()),
        sa.column('role', sa.Text()),
        sa.column('parent_id', sa.Text()),
        sa.column('content', _JSON()),
        sa.column('output', _JSON()),
        sa.column('model_id', sa.Text()),
        sa.column('files', _JSON()),
        sa.column('sources', _JSON()),
        sa.column('embeds', _JSON()),
        sa.column('done', sa.Boolean()),
        sa.column('status_history', _JSON()),
        sa.column('error', _JSON()),
        sa.column('usage', _JSON()),
        sa.column('created_at', sa.BigInteger()),
        sa.column('updated_at', sa.BigInteger()),
    )

    hint_table = sa.table(
        CURRENT_MESSAGE_HINT_TABLE,
        sa.column('chat_id', sa.Text()),
        sa.column('message_id', sa.Text()),
    )
    hints_batch = []
    uncertain_chats = set()

    # Number of chats to go through, for the progress lines. This reads the
    # chat rows without their documents, so it is quick even on a large table.
    total_chats = conn.execute(
        sa.select(sa.func.count()).select_from(chat_table).where(~chat_table.c.user_id.like('shared-%'))
    ).scalar()
    progress_log.info(
        f'chat_message backfill: {total_chats:,} chats to go through; '
        f'progress is reported every {PROGRESS_INTERVAL} seconds'
    )

    # Stream rows instead of loading all into memory:
    # - yield_per: fetches rows in chunks via cursor.fetchmany() (all backends)
    # - stream_results: enables server-side cursors on PostgreSQL (no-op on SQLite)
    result = conn.execute(
        sa.select(chat_table.c.id, chat_table.c.user_id, chat_table.c.chat)
        .where(~chat_table.c.user_id.like('shared-%'))
        .execution_options(yield_per=1000, stream_results=True)
    )

    now = int(time.time())
    messages_batch = []
    total_inserted = 0
    total_failed = 0
    chats_done = 0
    backfill_started = time.monotonic()
    last_progress = backfill_started

    for chat_row in result:
        chats_done += 1
        if chats_done % 100 == 0:
            moment = time.monotonic()
            if moment - last_progress >= PROGRESS_INTERVAL:
                last_progress = moment
                elapsed = moment - backfill_started
                remaining = elapsed * (total_chats - chats_done) / chats_done if total_chats > chats_done else 0
                progress_log.info(
                    f'chat_message backfill: {chats_done:,} of {total_chats:,} chats '
                    f'({100 * chats_done / max(total_chats, 1):.1f}%), '
                    f'{total_inserted + len(messages_batch):,} messages, '
                    f'{_duration(elapsed)} elapsed, about {_duration(remaining)} left for the backfill '
                    f'(the indexes are built after it)'
                )

        chat_id = chat_row[0]
        user_id = chat_row[1]
        chat_data = chat_row[2]

        if not chat_data:
            continue

        # Handle both string and dict chat data
        if isinstance(chat_data, str):
            try:
                chat_data = json.loads(chat_data)
            except Exception:
                continue

        history = chat_data.get('history', {})
        if not isinstance(history, dict):
            continue

        messages = history.get('messages', {})
        if not isinstance(messages, dict):
            continue

        backfilled_ids = set()

        for message_id, message in messages.items():
            if not isinstance(message, dict):
                continue

            role = message.get('role')
            if not role:
                continue

            timestamp = message.get('timestamp', now)

            try:
                timestamp = int(float(timestamp))
            except Exception as e:
                timestamp = now

            # Normalize timestamp: convert ms to seconds, validate range
            if timestamp > 10_000_000_000:
                timestamp = timestamp // 1000
            # Must be after 2020 and not too far in the future
            if timestamp < 1577836800 or timestamp > now + 86400:
                timestamp = now

            backfilled_ids.add(message_id)
            messages_batch.append(
                {
                    'id': f'{chat_id}-{message_id}',
                    'chat_id': chat_id,
                    'user_id': user_id,
                    'role': role,
                    'parent_id': message.get('parentId'),
                    'content': message.get('content'),
                    'output': message.get('output'),
                    'model_id': message.get('model'),
                    'files': message.get('files'),
                    'sources': message.get('sources'),
                    'embeds': message.get('embeds'),
                    'done': message.get('done', True),
                    'status_history': message.get('statusHistory'),
                    'error': message.get('error'),
                    'usage': message.get('usage'),
                    'created_at': timestamp,
                    'updated_at': timestamp,
                }
            )

            # Flush batch when full
            if len(messages_batch) >= BATCH_SIZE:
                inserted, failed = _flush_batch(conn, chat_message_table, messages_batch, uncertain_chats)
                total_inserted += inserted
                total_failed += failed
                messages_batch.clear()

        # Record the chat's current message for revision 9a1b2c3d4e5f: the first
        # of its candidates (in that revision's order) that is one of the
        # messages just backfilled. Chats without such a candidate get no hint
        # and are resolved by that revision's own fallback rules.
        prefix = f'{chat_id}-'
        for candidate in (
            _hint_candidate(history.get('currentId'), prefix),
            _hint_candidate(chat_data.get('currentId'), prefix),
            _hint_candidate(chat_data.get('branchPointMessageId'), prefix),
        ):
            if candidate is not None and candidate in backfilled_ids:
                hints_batch.append({'chat_id': chat_id, 'message_id': candidate})
                break
        if len(hints_batch) >= BATCH_SIZE:
            conn.execute(sa.insert(hint_table), hints_batch)
            hints_batch.clear()

    # Flush remaining messages
    if messages_batch:
        inserted, failed = _flush_batch(conn, chat_message_table, messages_batch, uncertain_chats)
        total_inserted += inserted
        total_failed += failed

    if hints_batch:
        conn.execute(sa.insert(hint_table), hints_batch)

    backfill_seconds = time.monotonic() - backfill_started
    progress_log.info(
        f'chat_message backfill: finished {chats_done:,} chats, {total_inserted:,} messages '
        f'in {_duration(backfill_seconds)}; building the indexes next'
    )
    steps_total = 9 if is_postgresql else 7

    # Step 3: Build the indexes and the foreign key once, over the loaded data
    if is_postgresql:
        key_started = time.monotonic()
        progress_log.info(f'chat_message: step 1 of {steps_total}: primary key ...')
        # Without duplicated ids - the normal case - this is a single sorted
        # index build. Only if it fails are the duplicates looked for.
        savepoint = conn.begin_nested()
        try:
            op.create_primary_key('chat_message_pkey', 'chat_message', ['id'])
            savepoint.commit()
        except Exception:
            savepoint.rollback()
            removed = conn.execute(sa.text(_PG_REMOVE_DUPLICATES_SQL)).fetchall()
            for row in removed:
                uncertain_chats.add(row[0])
                log.warning(f'Removed a duplicated message id of chat {row[0]}')
            total_inserted -= len(removed)
            total_failed += len(removed)
            op.create_primary_key('chat_message_pkey', 'chat_message', ['id'])
        progress_log.info(
            f'chat_message: step 1 of {steps_total}: primary key done in {_duration(time.monotonic() - key_started)}'
        )

    log.info(f'Backfilled {total_inserted} messages into chat_message table ({total_failed} failed)')

    # A hint is only trustworthy if every message of its chat made it into
    # chat_message. Drop the hints of the chats where that is not certain.
    if len(uncertain_chats) > MAX_UNCERTAIN_CHATS:
        conn.execute(sa.delete(hint_table))
    elif uncertain_chats:
        uncertain = sorted(uncertain_chats)
        for start in range(0, len(uncertain), BATCH_SIZE):
            conn.execute(sa.delete(hint_table).where(hint_table.c.chat_id.in_(uncertain[start : start + BATCH_SIZE])))

    indexes = [
        ('ix_chat_message_chat_id', ['chat_id']),
        ('ix_chat_message_user_id', ['user_id']),
        ('ix_chat_message_model_id', ['model_id']),
        ('ix_chat_message_created_at', ['created_at']),
        ('chat_message_chat_parent_idx', ['chat_id', 'parent_id']),
        ('chat_message_model_created_idx', ['model_id', 'created_at']),
        ('chat_message_user_created_idx', ['user_id', 'created_at']),
    ]
    position = steps_total - len(indexes) - (0 if is_sqlite else 1)
    for name, columns in indexes:
        position += 1
        _timed_step(
            position,
            steps_total,
            f'index {name}',
            lambda name=name, columns=columns: op.create_index(name, 'chat_message', columns),
        )

    if not is_sqlite:
        _timed_step(
            steps_total,
            steps_total,
            'foreign key to chat',
            lambda: op.create_foreign_key(
                'chat_message_chat_id_fkey',
                'chat_message',
                'chat',
                ['chat_id'],
                ['id'],
                ondelete='CASCADE',
            ),
        )

    progress_log.info(
        f'chat_message: all done in {_duration(time.monotonic() - backfill_started)} '
        f'(backfill {_duration(backfill_seconds)})'
    )


def downgrade() -> None:
    op.execute(f'DROP TABLE IF EXISTS {CURRENT_MESSAGE_HINT_TABLE}')
    op.drop_index('chat_message_user_created_idx', table_name='chat_message')
    op.drop_index('chat_message_model_created_idx', table_name='chat_message')
    op.drop_index('chat_message_chat_parent_idx', table_name='chat_message')
    op.drop_table('chat_message')
