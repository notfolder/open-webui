#!/bin/sh
# マイグレーション専用イメージを作る。
# 使い方: bench/migration/image/build.sh [タグ] [プラットフォーム]
#   例: bench/migration/image/build.sh owui-migrate:0.13.3 linux/amd64
# 元にするイメージを変えるとき: BASE_IMAGE=<イメージ> bench/migration/image/build.sh ...
#   例（v0.12.3 用）: BASE_IMAGE=ghcr.io/open-webui/open-webui:v0.12.3 bench/migration/image/build.sh owui-migrate:0.12.3 linux/amd64
#   移行先の版と最終位置は、道具がイメージの中身から読むので、ほかに変える所は無い。
# プラットフォームを省くと、このマシンと同じものになる。本番が x86 のサーバーなら linux/amd64 を指定する。
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"; ROOT="$HERE/../../.."
TAG="${1:-owui-migrate:0.13.3}"
V="$ROOT/backend/open_webui/migrations/versions"
rm -rf "$HERE/files"; mkdir -p "$HERE/files"
cp "$V/8452d01d26d7_add_chat_message_table.py" "$V/9a1b2c3d4e5f_add_current_message_id_to_chat.py" "$V/c1d2e3f4a5b6_add_shared_chat_table.py" "$HERE/files/"
docker build ${2:+--platform "$2"} ${BASE_IMAGE:+--build-arg "BASE_IMAGE=$BASE_IMAGE"} -t "$TAG" "$HERE"
echo "作成: $TAG"
