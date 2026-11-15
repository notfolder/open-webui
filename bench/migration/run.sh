#!/bin/sh
# 使い方: bench/migration/run.sh <元の DB 名> [ラベル] [--single-transaction]    例: bench/migration/run.sh owui_10k baseline
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
export DATA_DIR="$HERE/data" WEBUI_SECRET_KEY=bench PYTHONPATH="$HERE/../../backend"
cd "$HERE/../../backend/open_webui"
exec "$HERE/venvnew/bin/python" "$HERE/bench_run.py" --source "$1" --label "${2:-baseline}" $3 2>&1 | grep --line-buffered -vE '^(INFO|WARNI|DEBUG)|^\s*$'
