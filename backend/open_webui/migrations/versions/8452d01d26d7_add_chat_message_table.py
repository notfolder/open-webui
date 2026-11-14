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

revision: str = '8452d01d26d7'
down_revision: Union[str, None] = '374d2f66af06'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

BATCH_SIZE = 5000

# Helper table handed over to revision 9a1b2c3d4e5f (add current_message_id to
# chat). That revision needs each chat's "current message" candidates, which
# live in the same chat document this backfill already reads. Recording them
# here saves it from reading every chat document a second time. It drops the
# table when it is done, and works without it if the table is absent.
CURRENT_MESSAGE_HINT_TABLE = 'migration_chat_current_message_hint'


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


def _hint_candidate(value, prefix):
    """A current-message candidate as revision 9a1b2c3d4e5f normalizes it."""
    if not isinstance(value, str) or not value:
        return None
    return value[len(prefix) :] if value.startswith(prefix) else value


def _flush_batch(conn, table, batch):
    """
    Insert a batch of messages, falling back to row-by-row on error.

    Tries a single bulk insert first (fast path). If that fails (e.g. due to
    a duplicate key), falls back to individual inserts wrapped in savepoints
    so the rest of the batch can still succeed.
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
    # The secondary indexes and the foreign key are added after the backfill
    # (Step 3). Maintaining them row by row during a large backfill multiplies
    # the amount of data and WAL written; building them once at the end is much
    # cheaper. The primary key stays, because the backfill relies on it to
    # reject duplicate message ids.
    #
    # SQLite cannot add a foreign key to an existing table, so there the
    # constraint is declared inline as before.
    is_sqlite = conn.dialect.name == 'sqlite'

    table_args = [
        sa.Column('id', sa.Text(), primary_key=True),
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
        sa.Column('history_current_id', sa.Text(), nullable=True),
        sa.Column('chat_current_id', sa.Text(), nullable=True),
        sa.Column('branch_point_message_id', sa.Text(), nullable=True),
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
        sa.column('history_current_id', sa.Text()),
        sa.column('chat_current_id', sa.Text()),
        sa.column('branch_point_message_id', sa.Text()),
    )
    hints_batch = []

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

    for chat_row in result:
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

        # Record the current-message candidates for revision 9a1b2c3d4e5f
        prefix = f'{chat_id}-'
        hints_batch.append(
            {
                'chat_id': chat_id,
                'history_current_id': _hint_candidate(
                    history.get('currentId') if isinstance(history, dict) else None, prefix
                ),
                'chat_current_id': _hint_candidate(chat_data.get('currentId'), prefix),
                'branch_point_message_id': _hint_candidate(chat_data.get('branchPointMessageId'), prefix),
            }
        )
        if len(hints_batch) >= BATCH_SIZE:
            conn.execute(sa.insert(hint_table), hints_batch)
            hints_batch.clear()

        if not isinstance(history, dict):
            continue

        messages = history.get('messages', {})
        if not isinstance(messages, dict):
            continue

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
                inserted, failed = _flush_batch(conn, chat_message_table, messages_batch)
                total_inserted += inserted
                total_failed += failed
                if total_inserted % 50000 < BATCH_SIZE:
                    log.info(f'Migration progress: {total_inserted} messages inserted...')
                messages_batch.clear()

    # Flush remaining messages
    if messages_batch:
        inserted, failed = _flush_batch(conn, chat_message_table, messages_batch)
        total_inserted += inserted
        total_failed += failed

    if hints_batch:
        conn.execute(sa.insert(hint_table), hints_batch)

    log.info(f'Backfilled {total_inserted} messages into chat_message table ({total_failed} failed)')

    # Step 3: Build the indexes and the foreign key once, over the loaded data
    op.create_index('ix_chat_message_chat_id', 'chat_message', ['chat_id'])
    op.create_index('ix_chat_message_user_id', 'chat_message', ['user_id'])
    op.create_index('ix_chat_message_model_id', 'chat_message', ['model_id'])
    op.create_index('ix_chat_message_created_at', 'chat_message', ['created_at'])
    op.create_index('chat_message_chat_parent_idx', 'chat_message', ['chat_id', 'parent_id'])
    op.create_index('chat_message_model_created_idx', 'chat_message', ['model_id', 'created_at'])
    op.create_index('chat_message_user_created_idx', 'chat_message', ['user_id', 'created_at'])

    if not is_sqlite:
        op.create_foreign_key(
            'chat_message_chat_id_fkey',
            'chat_message',
            'chat',
            ['chat_id'],
            ['id'],
            ondelete='CASCADE',
        )


def downgrade() -> None:
    op.execute(f'DROP TABLE IF EXISTS {CURRENT_MESSAGE_HINT_TABLE}')
    op.drop_index('chat_message_user_created_idx', table_name='chat_message')
    op.drop_index('chat_message_model_created_idx', table_name='chat_message')
    op.drop_index('chat_message_chat_parent_idx', table_name='chat_message')
    op.drop_table('chat_message')
