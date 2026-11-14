#!/bin/sh
# 計測の準備を一度だけ行う。
#   1. PostgreSQL を起動する
#   2. 0.7.2 のコードを取り出し、0.7.2 時点のテーブル構成を作る（DB: owui → owui_empty に複製）
#   3. 現在の作業ツリーのマイグレーションを動かす実行環境を作る
# 前提: uv、Docker、リポジトリに v0.7.2 のタグがあること（git fetch upstream --tags）
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$HERE/../.."
cd "$HERE"

docker compose up -d --wait

mkdir -p v072 data
git -C "$ROOT" archive v0.7.2 backend/open_webui/migrations backend/open_webui/internal backend/open_webui/models \
  backend/open_webui/env.py backend/open_webui/constants.py backend/open_webui/alembic.ini \
  backend/open_webui/utils backend/open_webui/config.py | tar -x -C v072
: > v072/backend/open_webui/__init__.py
git -C "$ROOT" show v0.7.2:CHANGELOG.md > v072/backend/open_webui/CHANGELOG.md
git -C "$ROOT" show v0.7.2:package.json > v072/package.json

# マイグレーションの読み込みに必要なものだけを入れる（本体の requirements.txt 全部は入れない）
COMMON="pydantic typing_extensions markdown beautifulsoup4 cryptography aiohttp python-mimeparse typer uvicorn"
uv venv -q --python 3.12 venv072
uv pip install -q --python venv072/bin/python sqlalchemy==2.0.45 alembic==1.17.2 peewee==3.18.3 \
  peewee-migrate==1.14.3 psycopg2-binary==2.9.11 $COMMON
uv venv -q --python 3.12 venvnew
uv pip install -q --python venvnew/bin/python 'sqlalchemy[asyncio]==2.0.50' 'psycopg[binary]==3.3.4' alembic==1.18.4 \
  psycopg2-binary==2.9.12 python-engineio orjson bcrypt fastapi pillow $COMMON

export DATABASE_URL=postgresql://postgres:bench@127.0.0.1:55432/owui DATA_DIR="$HERE/data" WEBUI_SECRET_KEY=bench
(cd v072/backend/open_webui && PYTHONPATH=.. "$HERE/venv072/bin/python" -m alembic upgrade head)
docker exec owui-mig-pg psql -U postgres -c "CREATE DATABASE owui_empty TEMPLATE owui"
echo "準備ができました。README.md の「データを作る」へ進んでください。"
