"""2 つの DB の同じテーブルを 1 行ずつ突き合わせる。
使い方: python compare_dbs.py <基準の DB> <比べる DB> <テーブル> <主キー> [列,列,...]
- JSON の列は文字列としての一致と、意味としての一致（jsonb）を分けて数える
- created_at / updated_at が「マイグレーションを実行した時刻」で埋まった行は、実行ごとに値が違うので同じとみなす
"""
import sys

import psycopg2

a_db, b_db, table, pk = sys.argv[1:5]
BASE = 'postgresql://postgres:bench@127.0.0.1:55432'
RUN_T0 = 1790000000  # これ以降の時刻は「実行時刻で埋めた値」とみなす（ダミーデータの時刻はこれより前）


def rows(db):
    conn = psycopg2.connect(f'{BASE}/{db}')
    cur = conn.cursor()
    cur.execute("SELECT column_name, data_type FROM information_schema.columns WHERE table_name=%s ORDER BY ordinal_position", (table,))
    cols = cur.fetchall()
    if len(sys.argv) > 5:
        want = sys.argv[5].split(',')
        cols = [c for c in cols if c[0] in want or c[0] == pk]
    exprs = []
    for name, typ in cols:
        if name in ('created_at', 'updated_at'):
            exprs.append(f'CASE WHEN {name} >= {RUN_T0} THEN -1 ELSE {name} END')
        elif typ in ('json', 'jsonb'):
            exprs.append(f'{name}::text')
        else:
            exprs.append(f'{name}::text')
    named = conn.cursor('stream')
    named.itersize = 20000
    named.execute(f'SELECT {pk}, md5(ROW({", ".join(exprs)})::text) FROM {table} ORDER BY {pk} COLLATE "C"')
    return conn, named, [c[0] for c in cols]


ca, ra, cols = rows(a_db)
cb, rb, _ = rows(b_db)
only_a = only_b = diff = same = 0
examples = []
x = ra.fetchone(); y = rb.fetchone()
while x or y:
    if y is None or (x is not None and x[0] < y[0]):
        only_a += 1; examples.append(('基準だけ', x[0])); x = ra.fetchone()
    elif x is None or y[0] < x[0]:
        only_b += 1; examples.append(('比較だけ', y[0])); y = rb.fetchone()
    else:
        if x[1] == y[1]:
            same += 1
        else:
            diff += 1; examples.append(('内容が違う', x[0]))
        x = ra.fetchone(); y = rb.fetchone()
print(f'{table}: 一致 {same} / 内容が違う {diff} / 基準だけ {only_a} / 比較だけ {only_b}')
for kind, key in examples[:15]:
    print('  ', kind, key)
sys.exit(0 if not (diff or only_a or only_b) else 1)
