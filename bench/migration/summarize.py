"""計測結果を一覧にする。使い方: python summarize.py <元の DB 名>"""
import glob, json, os, sys
src = sys.argv[1]
rows = []
for f in glob.glob(f'results/{src}-*.json'):
    r = json.load(open(f))
    if r['label'].startswith('verify'):
        continue
    m = {x['revision'][:4]: x for x in r['migrations']}
    rows.append((os.path.getmtime(f), r['label'], r['total_seconds'], m['8452']['seconds'], m['8452'].get('wal_mb', 0),
                 m['9a1b']['seconds'], m['c1d2']['seconds'], sum(x.get('wal_mb', 0) for x in r['migrations'])))
print(f"{'label':12s} {'total':>7s} {'8452':>7s} {'8452WAL':>8s} {'9a1b':>7s} {'c1d2':>6s} {'WAL計':>7s}")
for _, label, total, a, awal, b, c, wal in sorted(rows):
    print(f'{label:12s} {total:7.0f} {a:7.0f} {awal:8.0f} {b:7.0f} {c:6.0f} {wal:7.0f}')
