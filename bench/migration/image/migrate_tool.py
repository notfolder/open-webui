"""0.7.2 から v0.13.3 へのマイグレーションを、アプリを起動せずに実行するための道具。

マイグレーション専用イメージの入口。接続先は、アプリと同じ環境変数 DATABASE_URL で渡す。

    estimate [--ram GB]   行数と大きさを調べ、時間の見込みと、推奨する PostgreSQL の設定を表示する（読むだけ）
    run [--yes] [--ram GB]  マイグレーションを実行する。進捗を時刻つきで表示する
    monitor [--every 秒]  実行中に、別の端末から進み具合を見る（読むだけ）
    check                 終わったあとの確認（読むだけ）

時間の式は、Mac mini（外付け SSD）での実測から作った。本番の時間は「式の値 × 速さの比 F」。
F は、検証環境で run を動かし、実際の時間 ÷ 式の値 で求める。
"""

import argparse
import math
import os
import subprocess
import sys
import time

import psycopg2

START_REVISION = 'c440947495f3'  # 0.7.2
HEAD_REVISION = '9f6c2a8d1b43'  # v0.13.3
BACKEND_DIR = os.environ.get('OPEN_WEBUI_BACKEND_DIR', '/app/backend')
GB = 1024**3


def connect():
    url = os.environ.get('DATABASE_URL')
    if not url:
        sys.exit('環境変数 DATABASE_URL がありません（アプリと同じ値を渡してください）')
    if url.startswith('postgres://'):
        url = 'postgresql://' + url[len('postgres://') :]
    if not url.startswith('postgresql'):
        sys.exit('この道具は PostgreSQL 用です')
    # SQLAlchemy 形式（postgresql+psycopg2://）のドライバ名を外す
    scheme, rest = url.split('://', 1)
    conn = psycopg2.connect(scheme.split('+')[0] + '://' + rest)
    conn.autocommit = True
    return conn


def query(conn, sql, args=None):
    cur = conn.cursor()
    cur.execute(sql, args)
    return cur.fetchall()


def one(conn, sql, args=None):
    rows = query(conn, sql, args)
    return rows[0][0] if rows else None


def duration(seconds):
    seconds = int(seconds)
    if seconds >= 3600:
        return f'{seconds // 3600} 時間 {seconds % 3600 // 60} 分'
    if seconds >= 60:
        return f'{seconds // 60} 分 {seconds % 60} 秒'
    return f'{seconds} 秒'


def formula_seconds(size_gb, shared_rows, ram_gb):
    """最終版の時間の式（Mac mini での秒）。bench/migration/REPORT.md を参照。"""
    if size_gb <= 0:
        return 0.0
    ratio = min(1.2, max(0.05, ram_gb / (1.66 * size_gb)))
    return size_gb * (70 - 17.5 * math.log(ratio)) + shared_rows * (0.03 if ratio >= 1 else 0.11)


def revision(conn):
    if not one(conn, "SELECT to_regclass('alembic_version')"):
        return None
    return one(conn, 'SELECT version_num FROM alembic_version')


def table_exists(conn, name):
    return one(conn, 'SELECT to_regclass(%s)', (name,)) is not None


def gather(conn):
    info = {'revision': revision(conn)}
    info['version'] = one(conn, 'SHOW server_version')
    info['settings'] = {
        name: one(conn, f'SHOW {name}')
        for name in (
            'shared_buffers',
            'max_wal_size',
            'checkpoint_timeout',
            'maintenance_work_mem',
            'wal_level',
            'max_parallel_maintenance_workers',
        )
    }
    info['db_bytes'] = one(conn, 'SELECT pg_database_size(current_database())')
    info['chat_bytes'] = one(conn, "SELECT pg_total_relation_size('chat')")
    info['chat_type'] = one(
        conn, "SELECT data_type FROM information_schema.columns WHERE table_name = 'chat' AND column_name = 'chat'"
    )
    info['chats'], info['shared'] = query(
        conn, "SELECT count(*), count(*) FILTER (WHERE user_id LIKE 'shared-%%') FROM chat"
    )[0]
    return info


def sample(conn, chats):
    """チャットを抜き取って、1 件あたりのメッセージ数と大きさを調べる（約 2,000 件）。"""
    if not chats:
        return None
    percent = min(100.0, max(0.01, 200000.0 / chats))
    row = query(
        conn,
        f"""
        WITH picked AS (
            SELECT chat FROM chat TABLESAMPLE BERNOULLI ({percent}) WHERE user_id NOT LIKE 'shared-%%'
        ), measured AS (
            SELECT pg_column_size(chat) AS stored,
                   octet_length(chat::text) AS raw,
                   CASE WHEN json_typeof(chat::json -> 'history' -> 'messages') = 'object'
                        THEN (SELECT count(*) FROM json_object_keys(chat::json -> 'history' -> 'messages'))
                        ELSE 0 END AS messages
            FROM picked
        )
        SELECT count(*), avg(messages), max(messages), avg(stored), avg(raw), max(raw) FROM measured
        """,
    )[0]
    if not row[0]:
        return None
    return dict(zip(('picked', 'messages', 'max_messages', 'stored', 'raw', 'max_raw'), (float(v) for v in row)))


def largest(conn, limit=5):
    return query(
        conn,
        """
        SELECT c.id, t.stored, octet_length(c.chat::text)
        FROM (SELECT id, pg_column_size(chat) AS stored FROM chat ORDER BY 2 DESC LIMIT %s) t
        JOIN chat c ON c.id = t.id ORDER BY 2 DESC
        """,
        (limit,),
    )


def cmd_estimate(args):
    conn = connect()
    info = gather(conn)
    size_gb = info['chat_bytes'] / GB
    print('== いまの状態')
    print(f"PostgreSQL {info['version']} / マイグレーションの位置: {info['revision']}", end='')
    print('（0.7.2）' if info['revision'] == START_REVISION else '（0.7.2 ではありません。式は 0.7.2 からの移行が前提です）')
    print(f"chat テーブル（S）: {size_gb:.2f} GB / DB 全体: {info['db_bytes'] / GB:.2f} GB / chat 列の型: {info['chat_type']}")
    print(f"チャット: {info['chats']:,} 件（うち共有チャット N: {info['shared']:,} 件）")
    print('設定: ' + ', '.join(f'{k}={v}' for k, v in info['settings'].items()))

    picked = sample(conn, info['chats'])
    if picked:
        messages_total = picked['messages'] * (info['chats'] - info['shared'])
        per_message = info['chat_bytes'] / messages_total if messages_total else 0
        print(f"\n== 抜き取り（{int(picked['picked']):,} 件）")
        print(f"1 チャットあたり: メッセージ {picked['messages']:.1f} 件（最大 {int(picked['max_messages'])}）、"
              f"保存後 {picked['stored'] / 1024:.0f} KB、展開後 {picked['raw'] / 1024:.0f} KB（圧縮率 {picked['raw'] / max(picked['stored'], 1):.2f}）")
        print(f'メッセージの総数の見込み: {messages_total:,.0f} 件 / メッセージ 1 件あたり chat テーブル上で {per_message / 1024:.2f} KB'
              '（計測用のデータは 3.2 KB。これより小さいと、式より遅くなる方向）')
    print('\n== 大きいチャット（上位 5 件。移行は 1,000 件ずつ読むので、アプリ側のメモリに効く）')
    top = largest(conn)
    for chat_id, stored, raw in top:
        print(f'  {chat_id}  保存後 {stored / 1048576:.1f} MB / 展開後 {raw / 1048576:.1f} MB')
    if picked:
        typical = picked['raw'] * 1000 * 3 / GB
        worst = (top[0][2] if top else 0) * 6 / GB
        print(f'移行するプロセスのメモリの目安: ふつうは {max(typical, 0.3):.1f} GB 前後、最大のチャットを読む瞬間に +{worst:.1f} GB 程度')

    print('\n== 時間の見込み（43 本の合計。Mac mini での値。本番は × 速さの比 F）')
    rams = sorted({4.0, 8.0, 16.0, 32.0, 64.0, round(2 * size_gb, 0)} | ({args.ram} if args.ram else set()))
    print(f"  {'PostgreSQL のメモリ':>18}  {'F=1':>14}  {'F=3':>14}  {'F=10':>14}")
    for ram in rams:
        if ram <= 0:
            continue
        seconds = formula_seconds(size_gb, info['shared'], ram)
        mark = ' ← 指定' if args.ram and ram == args.ram else (' ← 2 × S' if ram == round(2 * size_gb, 0) else '')
        print(f'  {ram:>15.0f} GB  {duration(seconds):>14}  {duration(seconds * 3):>14}  {duration(seconds * 10):>14}{mark}')
    print('  ※ メモリが 1.66 × S を超えると速い領域に入る（データがキャッシュに載る）。同じ条件でも 10〜20% ぶれる。')

    ram = args.ram or 8.0
    shared_buffers = max(1.0, ram / 4)
    print(f'\n== 推奨する設定（PostgreSQL のコンテナのメモリを {ram:.0f} GB とした場合。--ram で変えられる）')
    print(f"  ALTER SYSTEM SET shared_buffers = '{int(shared_buffers * 1024)}MB';   -- コンテナのメモリの 25%（最低 1 GB）")
    print("  ALTER SYSTEM SET max_wal_size = '16GB';")
    print("  ALTER SYSTEM SET checkpoint_timeout = '30min';")
    print("  ALTER SYSTEM SET maintenance_work_mem = '1GB';")
    print('  -- そのあと PostgreSQL を再起動。コンテナの shm_size は 1g 以上にする（インデックスの並列作成が /dev/shm を使う）')
    print('  -- 元に戻すとき: ALTER SYSTEM RESET ALL; のあと再起動')
    print(f'\n== 必要なディスク: いまより約 {size_gb * 0.66 + 16:.0f} GB 多く使う（chat_message とインデックスで S × 0.66、WAL で最大 16 GB）')
    print(f'   書き込む量の目安: データ {size_gb * 0.66:.0f} GB ＋ WAL {size_gb * 0.69:.0f} GB。読む量: S（{size_gb:.0f} GB）')


def cmd_run(args):
    conn = connect()
    info = gather(conn)
    size_gb = info['chat_bytes'] / GB
    print(f"マイグレーションの位置: {info['revision']} / chat テーブル {size_gb:.2f} GB / チャット {info['chats']:,} 件", flush=True)
    print('設定: ' + ', '.join(f'{k}={v}' for k, v in info['settings'].items()), flush=True)
    if info['revision'] == HEAD_REVISION:
        print('すでに最新です。何もしません。')
        return 0
    if info['revision'] != START_REVISION:
        print(f'注意: 0.7.2（{START_REVISION}）ではありません。続きから実行します。', flush=True)
    others = one(conn, "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() AND pid <> pg_backend_pid() AND backend_type = 'client backend'")
    if others:
        print(f'注意: この DB にほかの接続が {others} 本あります。アプリを止めてから実行してください。', flush=True)
    if not args.yes:
        if not sys.stdin.isatty():
            sys.exit('実行するには --yes を付けてください（バックアップを取ってあること、アプリを止めてあることを確かめてから）')
        if input('バックアップを取り、アプリを止めましたか。実行するなら yes: ').strip() != 'yes':
            sys.exit('中止しました')
    conn.close()

    env = dict(os.environ)
    if not env.get('WEBUI_SECRET_KEY'):
        # 設定の読み込みが要求するだけで、マイグレーションでは使わない（イメージでは空で定義されている）
        env['WEBUI_SECRET_KEY'] = 'migration-only'
    env['PYTHONUNBUFFERED'] = '1'
    started = time.time()
    print(f'{time.strftime("%H:%M:%S")} 開始。全体が 1 つのトランザクションなので、途中で止めると 0.7.2 の状態に戻ります。', flush=True)
    # アプリの起動時と同じ入口（設定の読み込みの中でマイグレーションが走る）を、アプリを起動せずに通す
    process = subprocess.Popen(
        [sys.executable, '-c', 'import open_webui.config'],
        cwd=BACKEND_DIR,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    shown_all = os.environ.get('MIGRATE_VERBOSE')
    hidden = []
    for line in process.stdout:
        line = line.rstrip()
        if shown_all or any(key in line for key in ('alembic', 'chat_message', 'rror', 'ARN', 'Traceback', 'igrat')) or line.startswith(' '):
            print(f'{time.strftime("%H:%M:%S")} {line}', flush=True)
        else:
            hidden = (hidden + [line])[-60:]
    code = process.wait()
    elapsed = time.time() - started
    if code != 0:
        # 失敗したときは、表示を省いていた行も出す
        for line in hidden:
            print(f'         {line}', flush=True)
        print(f'{time.strftime("%H:%M:%S")} 失敗しました（終了コード {code}、{duration(elapsed)}）。DB は開始前の状態に戻っています。', flush=True)
        return code
    print(f'{time.strftime("%H:%M:%S")} 完了。かかった時間: {duration(elapsed)}（{elapsed:.0f} 秒）', flush=True)
    for ram in ([args.ram] if args.ram else (8, 16, 32, 64)):
        predicted = formula_seconds(size_gb, info['shared'], ram)
        if predicted:
            print(f'  PostgreSQL のメモリが {ram} GB なら、式の値は {predicted:.0f} 秒 → 速さの比 F = {elapsed / predicted:.2f}')
    conn = connect()
    return report_check(conn)


def cmd_monitor(args):
    conn = connect()
    chat_bytes = one(conn, "SELECT pg_total_relation_size('chat')")
    first = None
    print(f'chat テーブル {chat_bytes / GB:.2f} GB。chat_message の展開で、DB は約 {chat_bytes * 0.66 / GB:.1f} GB 増える見込み。Ctrl+C で終了。')
    while True:
        size = one(conn, 'SELECT pg_database_size(current_database())')
        moment = time.time()
        if first is None:
            first = (moment, size)
        grown = size - first[1]
        rate = grown / (moment - first[0]) if moment > first[0] else 0
        line = f'{time.strftime("%H:%M:%S")} DB {size / GB:.2f} GB（見始めてから +{grown / GB:.2f} GB、{rate / 1048576:.1f} MB/秒）'
        for pid, age, state, wait, text in query(
            conn,
            """
            SELECT pid, now() - xact_start, state, coalesce(wait_event_type || ':' || wait_event, '-'), left(query, 70)
            FROM pg_stat_activity
            WHERE datname = current_database() AND pid <> pg_backend_pid() AND xact_start IS NOT NULL
              AND backend_type = 'client backend'
            ORDER BY xact_start LIMIT 3
            """,
        ):
            line += f'\n    接続 {pid}: 開始から {str(age).split(".")[0]}、{state}、待ち {wait}、{" ".join(text.split())}'
        for phase, blocks_done, blocks_total, tuples_done, tuples_total in query(
            conn,
            'SELECT phase, blocks_done, blocks_total, tuples_done, tuples_total FROM pg_stat_progress_create_index',
        ):
            done = f'{100 * blocks_done / blocks_total:.0f}%' if blocks_total else (f'{100 * tuples_done / tuples_total:.0f}%' if tuples_total else '-')
            line += f'\n    インデックス作成中: {phase}（{done}）'
        print(line, flush=True)
        if args.once:
            return 0
        time.sleep(args.every)


def report_check(conn):
    problems = []
    current = revision(conn)
    print(f'\n== 確認\nマイグレーションの位置: {current}', '（v0.13.3 の最新）' if current == HEAD_REVISION else '')
    if current != HEAD_REVISION:
        problems.append('最新まで進んでいない')
    if table_exists(conn, 'chat_message'):
        chats = one(conn, 'SELECT count(*) FROM chat')
        messages = one(conn, 'SELECT count(*) FROM chat_message')
        with_messages = one(conn, 'SELECT count(*) FROM chat c WHERE EXISTS (SELECT 1 FROM chat_message m WHERE m.chat_id = c.id)')
        columns = [r[0] for r in query(conn, "SELECT column_name FROM information_schema.columns WHERE table_name = 'chat'")]
        print(f'チャット {chats:,} 件 / メッセージ {messages:,} 件 / メッセージのあるチャット {with_messages:,} 件')
        if 'current_message_id' in columns:
            unresolved = one(conn, 'SELECT count(*) FROM chat c WHERE current_message_id IS NULL AND EXISTS (SELECT 1 FROM chat_message m WHERE m.chat_id = c.id)')
            print(f'メッセージがあるのに表示中のメッセージが決まっていないチャット: {unresolved:,} 件')
            if unresolved:
                problems.append('表示中のメッセージが決まっていないチャットがある')
        indexes = query(conn, "SELECT i.relname, x.indisvalid FROM pg_index x JOIN pg_class i ON i.oid = x.indexrelid WHERE x.indrelid = 'chat_message'::regclass")
        invalid = [name for name, valid in indexes if not valid]
        keys = one(conn, "SELECT count(*) FROM pg_constraint WHERE conrelid = 'chat_message'::regclass AND contype = 'f'")
        print(f'chat_message のインデックス {len(indexes)} 本（無効 {len(invalid)} 本）/ 外部キー {keys} 本')
        if invalid or not keys or not any(name == 'chat_message_pkey' for name, _ in indexes):
            problems.append('chat_message のインデックス・主キー・外部キーがそろっていない')
        if table_exists(conn, 'migration_chat_current_message_hint'):
            problems.append('補助テーブル migration_chat_current_message_hint が残っている')
        if table_exists(conn, 'shared_chat'):
            left = one(conn, "SELECT count(*) FROM chat WHERE user_id LIKE 'shared-%%'")
            print(f"共有チャット: shared_chat に {one(conn, 'SELECT count(*) FROM shared_chat'):,} 件 / chat に残った共有の行 {left:,} 件")
        sizes = query(conn, "SELECT pg_total_relation_size('chat'), pg_total_relation_size('chat_message'), pg_database_size(current_database())")[0]
        print(f'大きさ: chat {sizes[0] / GB:.2f} GB / chat_message {sizes[1] / GB:.2f} GB / DB 全体 {sizes[2] / GB:.2f} GB')
    else:
        problems.append('chat_message テーブルがない')
    print('結果: ' + ('問題なし' if not problems else '要確認 — ' + '、'.join(problems)))
    return 1 if problems else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('estimate')
    p.add_argument('--ram', type=float, help='PostgreSQL のコンテナが使えるメモリ（GB）')
    p = sub.add_parser('run')
    p.add_argument('--yes', action='store_true', help='確認を省く')
    p.add_argument('--ram', type=float, help='PostgreSQL のコンテナが使えるメモリ（GB）。速さの比 F の計算に使う')
    p = sub.add_parser('monitor')
    p.add_argument('--every', type=int, default=30)
    p.add_argument('--once', action='store_true')
    sub.add_parser('check')
    args = parser.parse_args()
    if args.command == 'estimate':
        return cmd_estimate(args)
    if args.command == 'run':
        return cmd_run(args)
    if args.command == 'monitor':
        return cmd_monitor(args)
    return report_check(connect())


if __name__ == '__main__':
    sys.exit(main())
