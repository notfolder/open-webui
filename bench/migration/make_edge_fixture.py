"""マイグレーションの書き換えで結果が変わらないことを確かめるための、わざと壊れた形のチャットを入れる。
使い方: python make_edge_fixture.py <DB 名>   （0.7.2 の空の状態から複製した DB に対して実行する）
"""

import json
import sys

import psycopg2

db = sys.argv[1]
conn = psycopg2.connect(f'postgresql://postgres:bench@127.0.0.1:55432/{db}')
cur = conn.cursor()
T = 1700000000


def msg(mid, **kw):
    base = {'id': mid, 'parentId': None, 'childrenIds': [], 'role': 'user', 'content': 'x', 'timestamp': T}
    base.update(kw)
    return base


def chat_doc(messages, current=None, **top):
    doc = {
        'id': '',
        'title': 't',
        'models': ['m'],
        'params': {},
        'history': {'messages': messages, 'currentId': current},
        'messages': [],
        'tags': [],
        'timestamp': T * 1000,
        'files': [],
    }
    doc.update(top)
    return doc


M = {}
# 時刻のいろいろな形
ts_cases = {
    'ts_missing': None,
    'ts_null': 'NULL',
    'ts_str_int': '1700000001',
    'ts_str_float': ' 1700000002.9 ',
    'ts_ms': 1700000003123,
    'ts_ms_str': '1700000004123',
    'ts_float': 1700000005.7,
    'ts_exp': 1.700000006e9,
    'ts_abc': 'abc',
    'ts_true': True,
    'ts_neg': -5,
    'ts_old': 1000000000,
    'ts_future': 99999999999999999,
    'ts_empty': '',
    'ts_list': [1],
    'ts_obj': {'a': 1},
    'ts_zero': 0,
    'ts_plus': '+1700000007',
    'ts_dot': '1700000008.',
    'ts_nl': '1700000009\n',
    'ts_big_exp': '1e400',
    'ts_inf': 'inf',
    'ts_nan': 'nan',
}
for k, v in ts_cases.items():
    m = msg(k)
    if v is None:
        del m['timestamp']
    elif v == 'NULL':
        m['timestamp'] = None
    else:
        m['timestamp'] = v
    M[k] = m
# 役割のいろいろな形
for k, v in {
    'role_empty': '',
    'role_null': None,
    'role_zero': 0,
    'role_num': 5,
    'role_true': True,
    'role_false': False,
    'role_obj': {'a': 1},
    'role_list': [],
    'role_sys': 'system',
}.items():
    M[k] = msg(k, role=v)
m = msg('role_missing')
del m['role']
M['role_missing'] = m
# そのほかの項目
M['parent_num'] = msg('parent_num', parentId=7)
M['parent_obj'] = msg('parent_obj', parentId={'a': 1})
M['parent_str'] = msg('parent_str', parentId='ts_missing')
M['model_obj'] = msg('model_obj', role='assistant', model={'id': 'x'})
M['model_num'] = msg('model_num', role='assistant', model=3)
M['done_false'] = msg('done_false', role='assistant', model='m', done=False)
M['done_null'] = msg('done_null', role='assistant', model='m', done=None)
M['done_true'] = msg('done_true', role='assistant', model='m', done=True)
M['done_num'] = msg('done_num', role='assistant', model='m', done=1)
M['done_str'] = msg('done_str', role='assistant', model='m', done='abc')
M['done_zero'] = msg('done_zero', role='assistant', model='m', done=0)
M['done_two'] = msg('done_two', role='assistant', model='m', done=2)
M['content_obj'] = msg('content_obj', content={'a': [1, 2, {'b': None}]})
M['content_list'] = msg('content_list', content=[{'type': 'text', 'text': 'こんにちは'}])
M['content_null'] = msg('content_null', content=None)
# NUL 文字を含む本文は、別のチャット（edge-nul）に分けてある。同じチャットに入れると、
# チャット全体が Python の処理に回り、SQL の処理を確かめられなくなるため。
# 対になっていないサロゲート（UTF-8 に変換できない文字）。エスケープしたまま書く処理の確認用
M['content_surrogate'] = msg('content_surrogate', content='a\ud83db')
M['content_ja'] = msg('content_ja', content='日本語と emoji 🎉 と "引用" と \\ と \n 改行')
M['full'] = msg(
    'full',
    role='assistant',
    model='m',
    modelName='m',
    done=True,
    output=[{'type': 'message'}],
    files=[{'id': 'f1'}],
    sources=[{'source': {'name': 'd'}}],
    embeds=['<iframe>'],
    statusHistory=[{'done': True}],
    error={'content': 'e'},
    usage={'total_tokens': 3},
)
M['not_a_dict_str'] = 'just a string'
M['not_a_dict_num'] = 5
M['not_a_dict_null'] = None
M['not_a_dict_list'] = [1, 2]

rows = [
    ('edge-main', 'user-1', chat_doc(M, 'full')),
    ('edge-empty-doc', 'user-1', {}),
    ('edge-no-history', 'user-1', {'title': 't'}),
    ('edge-history-list', 'user-1', {'history': [1, 2]}),
    ('edge-history-null', 'user-1', {'history': None}),
    ('edge-messages-list', 'user-1', {'history': {'messages': [msg('a')]}}),
    ('edge-messages-null', 'user-1', {'history': {'messages': None}}),
    ('edge-messages-empty', 'user-1', {'history': {'messages': {}, 'currentId': None}}),
    # チャットの JSON 全体が配列や数値のものは入れない。本体の元の処理が
    # 'list' object has no attribute 'get' で異常終了し、マイグレーション全体が止まるため。
    # id の衝突: 'a-b' + 'c' と 'a' + 'b-c' はどちらも 'a-b-c' になる
    ('a-b', 'user-1', chat_doc({'c': msg('c')}, 'c')),
    ('a', 'user-1', chat_doc({'b-c': msg('b-c')}, 'b-c')),
    # 共有されたチャット（0.7.2 の形式）は対象外
    ('share-token-1', 'shared-edge-main', chat_doc({'s1': msg('s1')}, 's1')),
    # 共有のいろいろな形（c1d2 の確認用）: 同じチャットの 2 つ目の共有、元のチャットが無い共有、ほかのチャットの共有
    ('share-token-2', 'shared-edge-main', chat_doc({'s2': msg('s2')}, 's2')),
    ('share-orphan', 'shared-no-such-chat', chat_doc({'s3': msg('s3')}, 's3')),
    ('share-token-3', 'shared-a', chat_doc({'s4': msg('s4', content='日本語の共有')}, 's4')),
    # currentId のいろいろな形（9a1b の確認用）
    (
        'cur-prefixed',
        'user-1',
        chat_doc({'m1': msg('m1'), 'm2': msg('m2', parentId='m1', timestamp=T + 5)}, 'cur-prefixed-m2'),
    ),
    ('cur-missing-id', 'user-1', chat_doc({'m1': msg('m1'), 'm2': msg('m2', parentId='m1', timestamp=T + 5)}, 'nope')),
    (
        'cur-top-level',
        'user-1',
        chat_doc({'m1': msg('m1'), 'm2': msg('m2', parentId='m1', timestamp=T + 5)}, None, currentId='m1'),
    ),
    (
        'cur-branch-point',
        'user-1',
        chat_doc({'m1': msg('m1'), 'm2': msg('m2', parentId='m1', timestamp=T + 5)}, None, branchPointMessageId='m1'),
    ),
    (
        'cur-none-two-leaves',
        'user-1',
        chat_doc(
            {
                'm1': msg('m1'),
                'm2': msg('m2', parentId='m1', timestamp=T + 5),
                'm3': msg('m3', parentId='m1', timestamp=T + 9),
            },
            None,
        ),
    ),
    ('cur-number', 'user-1', chat_doc({'m1': msg('m1')}, 5)),
    ('cur-empty-string', 'user-1', chat_doc({'m1': msg('m1')}, '')),
    ('cur-old-list-format', 'user-1', {'messages': [msg('l1'), msg('l2', parentId='l1', timestamp=T + 3)]}),
]
cur.execute(
    """INSERT INTO "user" (id, name, email, role, profile_image_url, created_at, updated_at, last_active_at)
               VALUES ('user-1','U','u@example.com','admin','/u.png',%s,%s,%s) ON CONFLICT DO NOTHING""",
    (T, T, T),
)
for cid, uid, doc in rows:
    cur.execute(
        'INSERT INTO chat (id, user_id, title, archived, created_at, updated_at, chat, pinned, meta) '
        "VALUES (%s,%s,'t',false,%s,%s,%s,false,'{}')",
        (cid, uid, T, T, json.dumps(doc)),
    )
# 二重にエンコードされたチャット（JSON の文字列の中に JSON が入っている）と、中身が壊れているもの
cur.execute(
    'INSERT INTO chat (id, user_id, title, archived, created_at, updated_at, chat, pinned, meta) '
    "VALUES ('edge-double','user-1','t',false,%s,%s,%s,false,'{}')",
    (
        T,
        T,
        json.dumps(
            json.dumps(chat_doc({'d1': msg('d1'), 'd2': msg('d2', role='assistant', model='m', parentId='d1')}, 'd2'))
        ),
    ),
)
cur.execute(
    'INSERT INTO chat (id, user_id, title, archived, created_at, updated_at, chat, pinned, meta) '
    "VALUES ('edge-double-broken','user-1','t',false,%s,%s,%s,false,'{}')",
    (T, T, json.dumps('{not json')),
)
cur.execute(
    'INSERT INTO chat (id, user_id, title, archived, created_at, updated_at, chat, pinned, meta) '
    "VALUES ('edge-sql-null','user-1','t',false,%s,%s,NULL,false,'{}')",
    (T, T),
)
conn.commit()
# NUL 文字を含むチャットをもう 1 件（こちらは SQL では扱えず、Python の処理に回る）
cur.execute(
    'INSERT INTO chat (id, user_id, title, archived, created_at, updated_at, chat, pinned, meta) '
    "VALUES ('edge-nul','user-1','t',false,%s,%s,%s,false,'{}')",
    (T, T, json.dumps(chat_doc({'n1': msg('n1', content='a\x00b'), 'n2': msg('n2', parentId='n1')}, 'n2'))),
)
conn.commit()
print('rows', len(rows) + 4)
