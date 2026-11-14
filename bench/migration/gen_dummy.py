"""0.7.2 のテーブル構成の PostgreSQL に、ダミーのユーザーとチャットを投入する。

使い方:
    python gen_dummy.py --dsn postgresql://postgres:bench@127.0.0.1:55432/owui_10k --chats 10000

チャットの形:
- 1 チャットのメッセージ数は 2〜200 件（対数正規分布、平均は約 20 件）
- 本文は 200〜2,000 文字。既定では自然な文章（日本語 6、英語 4）から切り出す（--text、--ja-ratio）
- 10% のチャットに、再生成で分かれた枝がある
- 1% は共有されている（0.7.2 の形式: user_id が "shared-<元の id>" の行をもう 1 行持つ）
- chat の JSON は、本物の 0.7.2 を動かして画面から作ったチャットの行に合わせてある
  （history.messages と、表示中の枝を並べた messages の両方。日本語は \\uXXXX にエスケープされる）
"""

import argparse
import io
import json
import math
import multiprocessing as mp
import random
import time
import uuid

import psycopg2

TAGS = ['technology', 'programming', 'database', 'general', 'question', 'docs', 'python', 'sql']
MODELS = ['gpt-4o', 'claude-sonnet', 'llama3.1:8b', 'gemini-pro', 'qwen2.5:14b']

JA = [
    'マイグレーションの実行時間は、テーブルの行数とインデックスの本数で決まります。',
    'まず現状を計測し、どの処理に時間がかかっているかを確かめてください。',
    'この関数は引数に受け取った一覧を順に処理し、結果を辞書にまとめて返します。',
    '原因は設定ファイルの読み込み順にあり、環境変数が上書きされていました。',
    '以下に手順を示します。順番どおりに実行すれば再現できます。',
    'ご質問の件ですが、結論から言うと二つ目の方法をおすすめします。',
    'トランザクションが長くなると、ロックの待ち時間と WAL の量が増えます。',
    '念のため、本番に適用する前に検証環境で同じ手順を試してください。',
    'エラーメッセージを見るかぎり、接続のタイムアウトが起きているようです。',
    '要約すると、読み込みを一度にまとめれば処理は半分の時間で終わります。',
]
EN = [
    'The migration reads every chat row and expands the message history into a new table.',
    'You can reduce the total time by creating the indexes after the bulk insert finishes.',
    'Here is a short example that shows how to batch the updates in a single statement.',
    'Make sure the connection pool is large enough for the number of concurrent workers.',
    'The error indicates that the request body exceeded the configured maximum size.',
    'In practice the difference is small unless the table has millions of rows.',
    'Let me know if you would like me to walk through the alternative approach as well.',
]
CODE = [
    '```python\nfor row in rows:\n    total += row["count"]\nprint(total)\n```',
    '```sql\nSELECT user_id, count(*) FROM chat GROUP BY user_id ORDER BY 2 DESC LIMIT 10;\n```',
    '```bash\ndocker compose up -d && docker compose logs -f backend\n```',
]
POOL = JA * 6 + EN * 4 + CODE


# 本文の作り方。'natural' は、リポジトリにある自然な文章（日本語の画面文言と英語の CHANGELOG）から
# 連続した範囲を切り出す。'pool' は上の 20 種類ほどの文をつなげる（同じ文が繰り返されるので、
# PostgreSQL の圧縮が効きすぎる。チャット 1 件で、自然な文章の 0.56 倍の大きさになった）。
TEXT_MODE = 'natural'
JA_RATIO = 0.6
_CORPUS = {}


def load_corpus():
    import os
    import re

    root = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..')
    with open(os.path.join(root, 'src/lib/i18n/locales/ja-JP/translation.json')) as f:
        _CORPUS['ja'] = ' '.join(v for v in json.load(f).values() if v)
    with open(os.path.join(root, 'CHANGELOG.md')) as f:
        _CORPUS['en'] = re.sub(r'\[#\d+\]\([^)]*\)|https?://\S+', '', f.read())


def _init_worker(text_mode, ja_ratio):
    global TEXT_MODE, JA_RATIO
    TEXT_MODE, JA_RATIO = text_mode, ja_ratio
    if TEXT_MODE == 'natural':
        load_corpus()


def make_text(rng: random.Random, lo: int = 200, hi: int = 2000) -> str:
    if TEXT_MODE == 'natural':
        n = rng.randint(lo, hi)
        src = _CORPUS['ja'] if rng.random() < JA_RATIO else _CORPUS['en']
        i = rng.randrange(0, max(1, len(src) - n))
        return src[i : i + n]
    target = rng.randint(lo, hi)
    parts = []
    size = 0
    while size < target:
        s = rng.choice(POOL)
        parts.append(s)
        size += len(s) + 1
    return '\n'.join(parts)[:target]


def new_id(rng: random.Random) -> str:
    return str(uuid.UUID(int=rng.getrandbits(128), version=4))


def make_chat(rng: random.Random, base_ts: int):
    """(chat の JSON, タイトル, 作成時刻, 更新時刻) を返す。"""
    pairs = int(rng.lognormvariate(math.log(10) - 0.32, 0.8))
    pairs = max(1, min(100, pairs))
    model = rng.choice(MODELS)
    messages = {}
    ts = base_ts
    parent = None
    branch_at = rng.randrange(pairs) if rng.random() < 0.10 else -1

    for i in range(pairs):
        uid = new_id(rng)
        ts += rng.randint(5, 600)
        messages[uid] = {
            'id': uid,
            'parentId': parent,
            'childrenIds': [],
            'role': 'user',
            'content': make_text(rng),
            'timestamp': ts,
            'models': [model],
        }
        if parent:
            messages[parent]['childrenIds'].append(uid)

        # 再生成があるチャットでは、同じ質問に対する回答を 2 つ作り、後のほうから続ける
        answers = 2 if i == branch_at else 1
        for _ in range(answers):
            aid = new_id(rng)
            ts += rng.randint(2, 60)
            content = make_text(rng)
            # 項目と並びは、本物の 0.7.2 の画面が保存した行に合わせてある
            # （lastSentence は読み上げ用に本文の末尾を複製したもの、followUps は関連質問の候補）
            msg = {
                'parentId': uid,
                'id': aid,
                'childrenIds': [],
                'role': 'assistant',
                'content': content,
                'model': model,
                'modelName': model,
                'modelIdx': 0,
                'timestamp': ts,
                'lastSentence': content[-rng.randint(60, 200):].replace('\n', ' '),
                'usage': {
                    'prompt_tokens': rng.randint(50, 4000),
                    'completion_tokens': rng.randint(50, 1500),
                    'total_tokens': rng.randint(100, 5500),
                },
                'done': True,
                'followUps': [make_text(rng, 15, 50).replace('\n', ' ') for _ in range(3)],
            }
            if rng.random() < 0.02:
                msg['annotation'] = {'rating': rng.choice([1, -1]), 'tags': ['test']}
                msg['feedbackId'] = new_id(rng)
            if rng.random() < 0.1:
                msg['statusHistory'] = [{'done': True, 'action': 'web_search', 'description': 'Searched 3 sites'}]
            if rng.random() < 0.05:
                msg['sources'] = [
                    {'source': {'name': 'doc.pdf'}, 'document': [make_text(rng, 100, 400)], 'metadata': [{'page': 1}]}
                ]
            messages[aid] = msg
            messages[uid]['childrenIds'].append(aid)
        parent = aid

    current_id = parent
    # 表示中の枝を、末尾から親をたどって並べる（フロントエンドの createMessagesList と同じ）
    branch = []
    cursor = current_id
    while cursor:
        branch.append(messages[cursor])
        cursor = messages[cursor]['parentId']
    branch.reverse()

    title = make_text(rng, 10, 40).replace('\n', ' ')
    chat = {
        'id': '',
        'title': title,
        'models': [model],
        'params': {},
        'history': {'messages': messages, 'currentId': current_id},
        'messages': branch,
        'tags': [],
        'timestamp': base_ts * 1000,
        'files': [],
    }
    return chat, title, base_ts, ts


def copy_escape(s: str) -> str:
    return s.replace('\\', '\\\\').replace('\t', '\\t').replace('\n', '\\n').replace('\r', '\\r')


def worker(args):
    dsn, seed, start, count, users, chunk = args
    rng = random.Random(seed * 1_000_003 + start)
    conn = psycopg2.connect(dsn)
    cur = conn.cursor()
    cols = '(id, user_id, title, share_id, archived, created_at, updated_at, chat, pinned, meta, folder_id)'
    done = 0
    messages = 0
    while done < count:
        buf = io.StringIO()
        for _ in range(min(chunk, count - done)):
            base_ts = rng.randint(1704067200, 1767225600)  # 2024-01-01 〜 2026-01-01
            chat, title, created, updated = make_chat(rng, base_ts)
            messages += len(chat['history']['messages'])
            chat_id = new_id(rng)
            user_id = f'user-{rng.randrange(users):07d}'
            # 0.7.2 の本体は json.dumps の既定（ensure_ascii=True）で保存するので、同じ形にする
            body = copy_escape(json.dumps(chat))
            share_id = new_id(rng) if rng.random() < 0.01 else None
            archived = 't' if rng.random() < 0.02 else 'f'
            pinned = 't' if rng.random() < 0.01 else 'f'
            meta = json.dumps({'tags': rng.sample(TAGS, rng.randint(1, 3))}) if rng.random() < 0.8 else '{}'
            row = [chat_id, user_id, copy_escape(title), share_id or '\\N', archived, created, updated, body, pinned, meta, '\\N']
            buf.write('\t'.join(map(str, row)) + '\n')
            if share_id:
                row = [share_id, f'shared-{chat_id}', copy_escape(title), '\\N', 'f', created, updated, body, 'f', meta, '\\N']
                buf.write('\t'.join(map(str, row)) + '\n')
            done += 1
        buf.seek(0)
        cur.copy_expert(f'COPY chat {cols} FROM STDIN', buf)
        conn.commit()
    conn.close()
    return done, messages


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dsn', required=True)
    ap.add_argument('--chats', type=int, required=True)
    ap.add_argument('--users', type=int, default=0, help='省略時はチャット 100 件につき 1 人（最低 10 人）')
    ap.add_argument('--workers', type=int, default=8)
    ap.add_argument('--seed', type=int, default=1)
    ap.add_argument('--chunk', type=int, default=200)
    ap.add_argument('--text', choices=['natural', 'pool'], default='natural', help='本文の作り方（上の TEXT_MODE の説明を参照）')
    ap.add_argument('--ja-ratio', type=float, default=0.6, help='natural のとき、日本語から切り出す割合')
    a = ap.parse_args()
    global TEXT_MODE, JA_RATIO
    TEXT_MODE, JA_RATIO = a.text, a.ja_ratio
    if TEXT_MODE == 'natural':
        load_corpus()
    users = a.users or max(10, a.chats // 100)

    t0 = time.time()
    conn = psycopg2.connect(a.dsn)
    cur = conn.cursor()
    now = int(time.time())
    buf_u, buf_a = io.StringIO(), io.StringIO()
    for i in range(users):
        uid = f'user-{i:07d}'
        role = 'admin' if i == 0 else 'user'
        buf_u.write(f'{uid}\tUser {i}\tuser{i}@example.com\t{role}\t/user.png\t{now}\t{now}\t{now}\n')
        buf_a.write(f'{uid}\tuser{i}@example.com\tx\tt\n')
    buf_u.seek(0)
    buf_a.seek(0)
    cur.copy_expert(
        'COPY "user" (id, name, email, role, profile_image_url, created_at, updated_at, last_active_at) FROM STDIN', buf_u
    )
    cur.copy_expert('COPY auth (id, email, password, active) FROM STDIN', buf_a)
    conn.commit()
    conn.close()

    per = math.ceil(a.chats / a.workers)
    jobs = []
    start = 0
    while start < a.chats:
        n = min(per, a.chats - start)
        jobs.append((a.dsn, a.seed, start, n, users, a.chunk))
        start += n
    # macOS では子プロセスが親の変数を引き継がないので、本文の作り方を子プロセスごとに設定し直す
    with mp.Pool(a.workers, initializer=_init_worker, initargs=(a.text, a.ja_ratio)) as pool:
        results = pool.map(worker, jobs)
    chats = sum(r[0] for r in results)
    messages = sum(r[1] for r in results)
    print(json.dumps({'users': users, 'chats': chats, 'messages': messages, 'seconds': round(time.time() - t0, 1)}))


if __name__ == '__main__':
    main()
