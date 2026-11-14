#!/bin/sh
# PostgreSQL の設定を変えながら、同じデータで計測を繰り返す。
# 使い方: bench/migration/run_matrix.sh <元の DB> <設定の一覧ファイル>
# 一覧ファイルは 1 行に「ラベル 設定=値 設定=値 ...」。先頭が # の行は飛ばす。
# ラベルが orig- で始まる行は、8452 のマイグレーションを本体の元の版（v0.13.3）に差し替えて測る。
# どの行も、まず共通の設定（BASE）を入れ、その上に行の設定を重ねる。
HERE="$(cd "$(dirname "$0")" && pwd)"; ROOT="$HERE/../.."
SRC="$1"; LIST="$2"
F="$ROOT/backend/open_webui/migrations/versions/8452d01d26d7_add_chat_message_table.py"
BASE="shared_buffers=3GB wal_buffers=64MB max_wal_size=16GB checkpoint_timeout=30min maintenance_work_mem=1GB"
P() { docker exec owui-mig-pg psql -U postgres -At "$@"; }
restore() { [ -f "$HERE/.matrix_backup.py" ] && cp "$HERE/.matrix_backup.py" "$F" && rm -f "$HERE/.matrix_backup.py"; return 0; }
trap restore EXIT

grep -v '^#' "$LIST" | grep -v '^$' | while read -r label settings; do
  echo "=== $(date +%T) $label: $settings"
  # 前の回でサーバーが異常終了していても続けられるよう、応答するまで待つ
  until P -c "select 1" >/dev/null 2>&1; do sleep 2; done
  P -c "ALTER SYSTEM RESET ALL" >/dev/null
  for kv in $BASE $settings; do P -c "ALTER SYSTEM SET ${kv%%=*} = '${kv#*=}'" >/dev/null; done
  # 既定の 10 秒で打ち切ると、停止が間に合わずに次の起動で復旧処理が走る。終わるまで待つ
  docker restart -t 600 owui-mig-pg >/dev/null
  until docker exec owui-mig-pg pg_isready -U postgres -h 127.0.0.1 >/dev/null 2>&1; do sleep 1; done
  case "$label" in orig-*) cp "$F" "$HERE/.matrix_backup.py"; git -C "$ROOT" show "v0.13.3:backend/open_webui/migrations/versions/8452d01d26d7_add_chat_message_table.py" > "$F";; esac
  "$HERE/run.sh" "$SRC" "$label" < /dev/null | grep -E "copied|8452|9a1b|c1d2|total|rror" || true
  restore
done
echo "=== $(date +%T) done"
