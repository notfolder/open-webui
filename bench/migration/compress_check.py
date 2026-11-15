"""ダミーデータの圧縮率が自然な文章とどれくらい違うかを調べる。
自然な文章の代わりに、リポジトリの日本語の画面文言（ja-JP/translation.json）と英語の CHANGELOG から、
連続した範囲を切り出して本文にしたチャットを作り、PostgreSQL に入れて圧縮後の大きさを測る。
"""

import json, random, re, sys
import psycopg2
import gen_dummy as g

root = '../..'
ja = ' '.join(v for v in json.load(open(f'{root}/src/lib/i18n/locales/ja-JP/translation.json')).values() if v)
en = re.sub(r'\[#\d+\]\([^)]*\)|https?://\S+', '', open(f'{root}/CHANGELOG.md').read())
print('corpus chars: ja', len(ja), 'en', len(en))


def natural_text(rng, lo=200, hi=2000):
    n = rng.randint(lo, hi)
    src = ja if rng.random() < 0.6 else en
    i = rng.randrange(0, max(1, len(src) - n))
    return src[i : i + n]


conn = psycopg2.connect('postgresql://postgres:bench@127.0.0.1:55432/owui')
cur = conn.cursor()
cur.execute(
    'DROP TABLE IF EXISTS t_comp; CREATE TABLE t_comp (kind text, chat json); DROP TABLE IF EXISTS t_msg; CREATE TABLE t_msg (kind text, content json)'
)
for kind, fn in (('dummy', g.make_text), ('natural', natural_text)):
    rng = random.Random(7)
    g.make_text = fn
    for _ in range(150):
        chat, *_ = g.make_chat(rng, 1700000000)
        cur.execute('INSERT INTO t_comp VALUES (%s, %s)', (kind, json.dumps(chat)))
        for m in list(chat['history']['messages'].values())[:8]:
            cur.execute('INSERT INTO t_msg VALUES (%s, %s)', (kind + ':escaped', json.dumps(m['content'])))
            cur.execute(
                'INSERT INTO t_msg VALUES (%s, %s)', (kind + ':utf8', json.dumps(m['content'], ensure_ascii=False))
            )
conn.commit()
for title, sql in (
    (
        'chat 列（チャット 1 件の JSON 全体）',
        'SELECT kind, count(*), round(avg(octet_length(chat::text))), round(avg(pg_column_size(chat))), round(avg(octet_length(chat::text))::numeric/avg(pg_column_size(chat)),2) FROM t_comp GROUP BY 1 ORDER BY 1',
    ),
    (
        'chat_message.content（メッセージ 1 件の本文）',
        'SELECT kind, count(*), round(avg(octet_length(content::text))), round(avg(pg_column_size(content))), round(avg(octet_length(content::text))::numeric/avg(pg_column_size(content)),2) FROM t_msg GROUP BY 1 ORDER BY 1',
    ),
):
    cur.execute(sql)
    print(title)
    for r in cur.fetchall():
        print(f'  {r[0]:16s} 件数 {r[1]:5d}  元 {int(r[2]):7d} バイト  保存後 {int(r[3]):7d} バイト  圧縮率 {r[4]} 倍')
cur.execute('DROP TABLE t_comp; DROP TABLE t_msg')
conn.commit()
