"""Add shared_chat table and migrate existing shares

Revision ID: c1d2e3f4a5b6
Revises: e1f2a3b4c5d6
Create Date: 2026-04-16 23:00:00.000000

"""

import logging
import time
import uuid

import sqlalchemy as sa
from alembic import op

revision = 'c1d2e3f4a5b6'
down_revision = 'e1f2a3b4c5d6'
branch_labels = None
depends_on = None

log = logging.getLogger(__name__)

# Lightweight table references for data migration (no ORM models needed)
chat_t = sa.table(
    'chat',
    sa.column('id', sa.Text),
    sa.column('user_id', sa.Text),
    sa.column('title', sa.Text),
    sa.column('chat', sa.JSON),
    sa.column('share_id', sa.Text),
    sa.column('created_at', sa.BigInteger),
    sa.column('updated_at', sa.BigInteger),
    sa.column('archived', sa.Boolean),
    sa.column('meta', sa.JSON),
)

shared_chat_t = sa.table(
    'shared_chat',
    sa.column('id', sa.Text),
    sa.column('chat_id', sa.Text),
    sa.column('user_id', sa.Text),
    sa.column('title', sa.Text),
    sa.column('chat', sa.JSON),
    sa.column('created_at', sa.BigInteger),
    sa.column('updated_at', sa.BigInteger),
)

chat_message_t = sa.table(
    'chat_message',
    sa.column('chat_id', sa.Text),
)

access_grant_t = sa.table(
    'access_grant',
    sa.column('id', sa.Text),
    sa.column('resource_type', sa.Text),
    sa.column('resource_id', sa.Text),
    sa.column('principal_type', sa.Text),
    sa.column('principal_id', sa.Text),
    sa.column('permission', sa.Text),
    sa.column('created_at', sa.BigInteger),
)


# PostgreSQL: move the shares with two statements instead of reading every
# shared chat document into memory and issuing several statements per share.
# The rules are those of the loop in upgrade(): a share is migrated only if its
# original chat still exists, an existing shared_chat row is left alone, and
# each original chat gets one user:*:read grant unless it already has one.
_PG_INSERT_SHARED_CHATS_SQL = """
INSERT INTO shared_chat (id, chat_id, user_id, title, chat, created_at, updated_at)
SELECT s.id, o.id, o.user_id, s.title, s.chat, s.created_at, s.updated_at
FROM chat s
JOIN chat o ON o.id = substr(s.user_id, 8)
WHERE s.user_id LIKE 'shared-%'
    AND NOT EXISTS (SELECT 1 FROM shared_chat x WHERE x.id = s.id)
"""

_PG_INSERT_GRANTS_SQL = """
INSERT INTO access_grant (id, resource_type, resource_id, principal_type, principal_id, permission, created_at)
SELECT
    gen_random_uuid()::text, 'shared_chat', t.chat_id, 'user', '*', 'read',
    COALESCE(NULLIF(t.created_at, 0), :now)
FROM (
    SELECT DISTINCT ON (o.id) o.id AS chat_id, s.created_at
    FROM chat s
    JOIN chat o ON o.id = substr(s.user_id, 8)
    WHERE s.user_id LIKE 'shared-%'
    ORDER BY o.id, s.id
) t
WHERE NOT EXISTS (
    SELECT 1 FROM access_grant g
    WHERE g.resource_type = 'shared_chat'
        AND g.resource_id = t.chat_id
        AND g.principal_type = 'user'
        AND g.principal_id = '*'
        AND g.permission = 'read'
)
"""


def _migrate_shares_postgresql(conn):
    """Return True if the shares were migrated; False to let the loop do it."""
    savepoint = conn.begin_nested()
    try:
        conn.execute(sa.text(_PG_INSERT_SHARED_CHATS_SQL))
        conn.execute(sa.text(_PG_INSERT_GRANTS_SQL), {'now': int(time.time())})
        savepoint.commit()
        return True
    except Exception as e:
        savepoint.rollback()
        log.warning(f'In-database share migration failed, migrating share by share: {e}')
        return False


def _chat_message_cascades(conn, inspector):
    """Whether deleting a chat is certain to delete its chat_message rows."""
    if conn.dialect.name != 'postgresql':
        # SQLite only enforces foreign keys when asked to
        return False
    for fk in inspector.get_foreign_keys('chat_message'):
        if (
            fk.get('referred_table') == 'chat'
            and fk.get('constrained_columns') == ['chat_id']
            and str((fk.get('options') or {}).get('ondelete', '')).upper() == 'CASCADE'
        ):
            return True
    return False


def _fetch_shared_rows(conn):
    return conn.execute(
        sa.select(
            chat_t.c.id,
            chat_t.c.user_id,
            chat_t.c.title,
            chat_t.c.chat,
            chat_t.c.created_at,
            chat_t.c.updated_at,
        ).where(chat_t.c.user_id.like('shared-%'))
    ).fetchall()


def upgrade():
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    tables = inspector.get_table_names()

    # 1. Create shared_chat table (idempotent)
    if 'shared_chat' not in tables:
        op.create_table(
            'shared_chat',
            sa.Column('id', sa.Text(), primary_key=True),
            sa.Column('chat_id', sa.Text(), sa.ForeignKey('chat.id', ondelete='CASCADE'), nullable=False),
            sa.Column('user_id', sa.Text(), nullable=False),
            sa.Column('title', sa.Text(), nullable=True),
            sa.Column('chat', sa.JSON(), nullable=True),
            sa.Column('created_at', sa.BigInteger(), nullable=True),
            sa.Column('updated_at', sa.BigInteger(), nullable=True),
        )

    # 2. Migrate existing shared-* rows
    if conn.dialect.name == 'postgresql' and _migrate_shares_postgresql(conn):
        shared_rows = []
    else:
        shared_rows = _fetch_shared_rows(conn)

    for row in shared_rows:
        share_token = row.id
        original_chat_id = row.user_id.replace('shared-', '', 1)

        # Verify original chat still exists
        original = conn.execute(sa.select(chat_t.c.user_id).where(chat_t.c.id == original_chat_id)).fetchone()

        if not original:
            continue

        # Check if shared_chat record already exists (idempotent)
        existing_shared = conn.execute(
            sa.select(shared_chat_t.c.id).where(shared_chat_t.c.id == share_token)
        ).fetchone()

        if not existing_shared:
            # Insert snapshot into shared_chat
            conn.execute(
                shared_chat_t.insert().values(
                    id=share_token,
                    chat_id=original_chat_id,
                    user_id=original.user_id,
                    title=row.title,
                    chat=row.chat,
                    created_at=row.created_at,
                    updated_at=row.updated_at,
                )
            )

        # Check if access_grant record already exists (idempotent)
        existing_grant = conn.execute(
            sa.select(access_grant_t.c.id).where(
                sa.and_(
                    access_grant_t.c.resource_type == 'shared_chat',
                    access_grant_t.c.resource_id == original_chat_id,
                    access_grant_t.c.principal_type == 'user',
                    access_grant_t.c.principal_id == '*',
                    access_grant_t.c.permission == 'read',
                )
            )
        ).fetchone()

        if not existing_grant:
            # Create user:*:read grant for backward compat
            conn.execute(
                access_grant_t.insert().values(
                    id=str(uuid.uuid4()),
                    resource_type='shared_chat',
                    resource_id=original_chat_id,
                    principal_type='user',
                    principal_id='*',
                    permission='read',
                    created_at=row.created_at or int(time.time()),
                )
            )

    # 3. Clean up old phantom rows
    #
    # Deleting the shared-* chats below also deletes their messages when
    # chat_message has its ON DELETE CASCADE foreign key to chat. In that case
    # the explicit delete is skipped: it finds its rows by reading all of
    # chat_message, which on a large table takes minutes to remove what the
    # cascade removes anyway.
    if not _chat_message_cascades(conn, inspector):
        conn.execute(
            chat_message_t.delete().where(
                chat_message_t.c.chat_id.in_(sa.select(chat_t.c.id).where(chat_t.c.user_id.like('shared-%')))
            )
        )
    conn.execute(chat_t.delete().where(chat_t.c.user_id.like('shared-%')))


def downgrade():
    conn = op.get_bind()

    shared_rows = conn.execute(
        sa.select(
            shared_chat_t.c.id,
            shared_chat_t.c.chat_id,
            shared_chat_t.c.user_id,
            shared_chat_t.c.title,
            shared_chat_t.c.chat,
            shared_chat_t.c.created_at,
            shared_chat_t.c.updated_at,
        )
    ).fetchall()

    for row in shared_rows:
        conn.execute(
            chat_t.insert().values(
                id=row.id,
                user_id=f'shared-{row.chat_id}',
                title=row.title,
                chat=row.chat,
                created_at=row.created_at,
                updated_at=row.updated_at,
                archived=False,
                meta={},
            )
        )

    conn.execute(access_grant_t.delete().where(access_grant_t.c.resource_type == 'shared_chat'))
    op.drop_table('shared_chat')
