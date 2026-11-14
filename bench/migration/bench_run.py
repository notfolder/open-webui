"""0.7.2 の状態の DB を複製し、最新までのマイグレーションを 1 本ずつ実行して時間を測る。

使い方（リポジトリの backend/open_webui で実行する。run.sh が面倒を見る）:
    python bench_run.py --source owui_10k [--label baseline]

- 元の DB（--source）は変更しない。`<source>_run` という名前で複製してから実行する。
- 本番は全マイグレーションを 1 つのトランザクションで実行するが、ここでは 1 本ごとにコミットする。
  1 本ごとの時間を測るためで、処理の中身は同じ。
- 結果は bench/migration/results/<source>-<label>.json に書く。
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

import psycopg2

BASE = 'postgresql://postgres:bench@127.0.0.1:55432'
START = 'c440947495f3'  # 0.7.2 の最終マイグレーション
HERE = Path(__file__).resolve().parent


def admin(sql, db='postgres', fetch=False):
    conn = psycopg2.connect(f'{BASE}/{db}')
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute(sql)
    rows = cur.fetchall() if fetch else None
    conn.close()
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--source', required=True)
    ap.add_argument('--label', default='baseline')
    ap.add_argument('--to', default='head')
    a = ap.parse_args()

    run_db = f'{a.source}_run'
    t0 = time.time()
    admin(f'DROP DATABASE IF EXISTS {run_db} WITH (FORCE)')
    admin(f'CREATE DATABASE {run_db} TEMPLATE {a.source}')
    copy_seconds = time.time() - t0
    print(f'copied {a.source} -> {run_db} in {copy_seconds:.1f}s', flush=True)

    os.environ['DATABASE_URL'] = f'{BASE}/{run_db}'
    from alembic import command
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    cfg = Config('alembic.ini')
    script = ScriptDirectory.from_config(cfg)
    revisions = [r for r in script.iterate_revisions(a.to, START)]
    revisions.reverse()

    stats = admin(
        "SELECT count(*), count(*) FILTER (WHERE user_id LIKE 'shared-%'), pg_total_relation_size('chat') FROM chat",
        db=run_db,
        fetch=True,
    )[0]
    result = {
        'source': a.source,
        'label': a.label,
        'chat_rows': stats[0],
        'shared_rows': stats[1],
        'chat_bytes': stats[2],
        'copy_seconds': round(copy_seconds, 1),
        'migrations': [],
    }

    total = 0.0
    def wal_lsn():
        return admin('SELECT pg_current_wal_lsn() - \'0/0\'::pg_lsn', db=run_db, fetch=True)[0][0]

    for rev in revisions:
        wal0 = wal_lsn()
        t = time.time()
        command.upgrade(cfg, rev.revision)
        dt = time.time() - t
        wal_mb = float(wal_lsn() - wal0) / 1048576
        total += dt
        name = Path(rev.path).stem
        result['migrations'].append({'revision': rev.revision, 'name': name, 'seconds': round(dt, 2), 'wal_mb': round(wal_mb, 1)})
        print(f'{dt:9.2f}s  WAL {wal_mb:9.1f} MB  {name}', flush=True)

    result['total_seconds'] = round(total, 1)
    msgs = admin(
        "SELECT count(*), pg_total_relation_size('chat_message'), pg_database_size(current_database()) FROM chat_message",
        db=run_db,
        fetch=True,
    )[0]
    result['chat_message_rows'] = msgs[0]
    result['chat_message_bytes'] = msgs[1]
    result['db_bytes_after'] = msgs[2]

    out = HERE / 'results'
    out.mkdir(exist_ok=True)
    path = out / f'{a.source}-{a.label}.json'
    path.write_text(json.dumps(result, indent=2, ensure_ascii=False))
    print(f'total {total:.1f}s, chat_message rows {msgs[0]}, wrote {path}', flush=True)


if __name__ == '__main__':
    sys.exit(main())
