# マイグレーション専用イメージ。公式のイメージ（既定は v0.13.3。BASE_IMAGE で替える）に、速くしたマイグレーション 3 本と実行用の道具を重ねる。
# アプリは起動しない。build.sh から作る（3 本のファイルを files/ に集めてからビルドする）。
ARG BASE_IMAGE=ghcr.io/open-webui/open-webui:v0.13.3
FROM ${BASE_IMAGE}
COPY files/*.py /app/backend/open_webui/migrations/versions/
COPY migrate_tool.py /migrate/migrate_tool.py
ENV ENABLE_OLLAMA_API=false OFFLINE_MODE=true ANONYMIZED_TELEMETRY=false DO_NOT_TRACK=true SCARF_NO_ANALYTICS=true
# 元のイメージの死活確認は、アプリの応答を見る。アプリを起動しないので外す（外さないと、
# 実行の仕組みによっては「異常」と見なされて、途中で止められる）
HEALTHCHECK NONE
ENTRYPOINT ["python", "/migrate/migrate_tool.py"]
CMD ["--help"]
