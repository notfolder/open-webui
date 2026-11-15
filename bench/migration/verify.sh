#!/bin/sh
# 書き換えたマイグレーションが、基準のコミットと同じ結果を出すかを確かめる。
# 使い方: bench/migration/verify.sh <元の DB> <基準のコミット> <どこまで実行するか> <ファイル名...>
#   例: bench/migration/verify.sh owui_10k_r 3f9dccd95 8452d01d26d7 8452d01d26d7_add_chat_message_table.py
# 元の DB を 2 つ複製し、片方は基準のコミットの版のファイルで、もう片方は作業ツリーの版で実行して、
# chat_message と chat.current_message_id を 1 行ずつ突き合わせる。
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"; ROOT="$HERE/../.."
SRC="$1"; REF="$2"; TO="$3"; shift 3
VERS="$ROOT/backend/open_webui/migrations/versions"
P() { docker exec owui-mig-pg psql -U postgres -At "$@"; }
export DATA_DIR="$HERE/data" WEBUI_SECRET_KEY=bench PYTHONPATH="$ROOT/backend"
run() { (cd "$ROOT/backend/open_webui" && "$HERE/venvnew/bin/python" "$HERE/bench_run.py" --source "$SRC" --label "$1" --to "$TO" 2>&1 | grep -E "total|rror|ailed" || true); }

# 基準: 対象のファイルを基準のコミットの版に差し替えて実行し、終わったら必ず戻す
mkdir -p "$HERE/.verify_backup"
for f in "$@"; do cp "$VERS/$f" "$HERE/.verify_backup/$f"; git -C "$ROOT" show "$REF:backend/open_webui/migrations/versions/$f" > "$VERS/$f"; done
trap 'for f in "$@"; do [ -f "$HERE/.verify_backup/$f" ] && cp "$HERE/.verify_backup/$f" "$VERS/$f"; done' EXIT
run "verify-ref"
for f in "$@"; do cp "$HERE/.verify_backup/$f" "$VERS/$f"; done
P -c "DROP DATABASE IF EXISTS ${SRC}_vref WITH (FORCE)" -c "ALTER DATABASE ${SRC}_run RENAME TO ${SRC}_vref" >/dev/null

run "verify-new"
P -c "DROP DATABASE IF EXISTS ${SRC}_vnew WITH (FORCE)" -c "ALTER DATABASE ${SRC}_run RENAME TO ${SRC}_vnew" >/dev/null

"$HERE/venvnew/bin/python" "$HERE/compare_dbs.py" "${SRC}_vref" "${SRC}_vnew" chat_message id
"$HERE/venvnew/bin/python" "$HERE/compare_dbs.py" "${SRC}_vref" "${SRC}_vnew" chat id user_id,title,share_id,archived,pinned
if P -d "${SRC}_vnew" -c "select 1 from information_schema.tables where table_name='shared_chat'" | grep -q 1; then
  "$HERE/venvnew/bin/python" "$HERE/compare_dbs.py" "${SRC}_vref" "${SRC}_vnew" shared_chat id
  # access_grant の id は実行ごとに違う乱数なので、付けた相手（resource_id）で突き合わせる
  "$HERE/venvnew/bin/python" "$HERE/compare_dbs.py" "${SRC}_vref" "${SRC}_vnew" access_grant resource_id resource_type,principal_type,principal_id,permission,created_at
fi
if P -d "${SRC}_vnew" -c "select 1 from information_schema.columns where table_name='chat' and column_name='current_message_id'" | grep -q 1; then
  "$HERE/venvnew/bin/python" "$HERE/compare_dbs.py" "${SRC}_vref" "${SRC}_vnew" chat id current_message_id
fi
