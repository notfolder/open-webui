"""1 本のマイグレーションだけをプロファイラ付きで実行する。
使い方: python profile_one.py <元の DB> <直前の revision> <対象の revision>
"""

import cProfile, os, pstats, sys, time
import psycopg2

BASE = 'postgresql://postgres:bench@127.0.0.1:55432'
src, before, target = sys.argv[1:4]
db = f'{src}_prof'
c = psycopg2.connect(f'{BASE}/postgres')
c.autocommit = True
c.cursor().execute(f'DROP DATABASE IF EXISTS {db} WITH (FORCE)')
c.cursor().execute(f'CREATE DATABASE {db} TEMPLATE {src}')
c.close()
os.environ['DATABASE_URL'] = f'{BASE}/{db}'
from alembic import command
from alembic.config import Config

cfg = Config('alembic.ini')
command.upgrade(cfg, before)
pr = cProfile.Profile()
t = time.time()
pr.enable()
command.upgrade(cfg, target)
pr.disable()
print(f'elapsed {time.time() - t:.1f}s')
pstats.Stats(pr).sort_stats('cumulative').print_stats(28)
