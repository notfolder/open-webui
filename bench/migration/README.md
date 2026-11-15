# マイグレーションの計測（0.7.2 → 現在の作業ツリー）

- **まとめの報告書: `REPORT.md`**（結果、効かなかったもの、時間の式、検証環境と本番の手順）
- 計測の生の表: `RESULTS.md`
- マイグレーション専用イメージ: `image/`（`build.sh` で作る。使い方は `REPORT.md` の 8 章）

チャットが多い環境で、0.7.2 から最新版へのマイグレーションにかかる時間を、PostgreSQL 上のダミーデータで測る道具。
本体へ PR を送るときは、このフォルダを含めない。

## 準備（一度だけ）

    bench/migration/setup.sh

## データを作る

    docker exec owui-mig-pg psql -U postgres -c "CREATE DATABASE owui_100k TEMPLATE owui_empty"
    bench/migration/venv072/bin/python bench/migration/gen_dummy.py \
        --dsn postgresql://postgres:bench@127.0.0.1:55432/owui_100k --chats 100000
    docker exec owui-mig-pg psql -U postgres -d owui_100k -c "VACUUM ANALYZE"

## 測る

    bench/migration/run.sh owui_100k <ラベル>

元の DB（`owui_100k`）は変更せず、`owui_100k_run` に複製してから 43 本を 1 本ずつ実行する。
結果は `results/owui_100k-<ラベル>.json`（マイグレーションごとの秒数と WAL の量）。

1 本だけをプロファイラ付きで動かすとき（`backend/open_webui` で、`run.sh` と同じ環境変数を付けて実行する）:

    python bench/migration/profile_one.py owui_10k <直前の revision> <対象の revision>

## PostgreSQL の設定

`docker-compose.yml` は既定の設定で起動する。計測で使った設定は次のとおり（`ALTER SYSTEM` のあと再起動）。

    ALTER SYSTEM SET shared_buffers = '3GB';
    ALTER SYSTEM SET wal_buffers = '64MB';
    ALTER SYSTEM SET max_wal_size = '16GB';
    ALTER SYSTEM SET checkpoint_timeout = '30min';
    ALTER SYSTEM SET maintenance_work_mem = '1GB';

既定に戻すときは `ALTER SYSTEM RESET ALL` のあと再起動する。
