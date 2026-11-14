"""指定したマイグレーションをプロファイラ付きで実行し、時間の内訳を出す。
使い方（backend/open_webui で、run.sh と同じ環境変数を付けて）:
    python profile_many.py <元の DB> <revision> [<revision> ...]
元の DB を <元の DB>_prof に複製し、0.7.2 の状態から順に実行する。指定した revision だけ計測する。
内訳は、データベースの応答待ち（execute / fetch）と、それ以外（Python の処理）に分けて出す。
"""
import cProfile, os, pstats, sys, time
import psycopg2

BASE = 'postgresql://postgres:bench@127.0.0.1:55432'
START = 'c440947495f3'
src, targets = sys.argv[1], sys.argv[2:]
db = f'{src}_prof'
c = psycopg2.connect(f'{BASE}/postgres'); c.autocommit = True
c.cursor().execute(f'DROP DATABASE IF EXISTS {db} WITH (FORCE)')
c.cursor().execute(f'CREATE DATABASE {db} TEMPLATE {src} STRATEGY FILE_COPY'); c.close()
os.environ['DATABASE_URL'] = f'{BASE}/{db}'
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

cfg = Config('alembic.ini')
revs = list(ScriptDirectory.from_config(cfg).iterate_revisions(targets[-1], START)); revs.reverse()
for rev in revs:
    if rev.revision not in targets:
        command.upgrade(cfg, rev.revision)
        continue
    pr = cProfile.Profile(); t = time.time(); pr.enable()
    command.upgrade(cfg, rev.revision)
    pr.disable(); total = time.time() - t
    st = pstats.Stats(pr)
    buckets = {}
    for (fname, line, func), (cc, nc, tt, ct, callers) in st.stats.items():
        key = None
        if "psycopg2" in func or 'psycopg2' in fname:
            key = f'DB: {func}'
        elif func in ("<method 'execute' of 'psycopg2.extensions.cursor' objects>", "<method 'fetchmany' of 'psycopg2.extensions.cursor' objects>"):
            key = f'DB: {func}'
        if key:
            buckets[key] = buckets.get(key, 0) + tt
    db_time = sum(buckets.values())
    print(f'\n##### {rev.revision}  合計 {total:.1f} 秒 / データベースの応答待ち {db_time:.1f} 秒 / それ以外（Python） {total - db_time:.1f} 秒')
    for k, v in sorted(buckets.items(), key=lambda kv: -kv[1])[:6]:
        print(f'   {v:8.1f} 秒  {k[:90]}')
    print('  --- 関数そのものの時間が長い順（上位 14）')
    for (fname, line, func), (cc, nc, tt, ct, callers) in sorted(st.stats.items(), key=lambda kv: -kv[1][2])[:14]:
        print(f'   {tt:8.1f} 秒  呼び出し {nc:>9}  {os.path.basename(fname)}:{line} {func[:70]}')
