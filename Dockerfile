# monitor-app のコンテナイメージ。
#
#   docker build -t monitor-app .
#   docker run --rm -p 9990:9990 monitor-app                      # デモ(設定なしで画面が出る)
#   docker run -d -p 9990:9990 -v "$PWD":/project monitor-app     # 自分のプロジェクト
#
# - 依存は uv.lock から入れる(コンパイラ不要。従来のビルダーでも BuildKit でもビルドできる)
# - 実行用のステージには仮想環境だけをコピーする(curl・sqlite3・コンパイラは入れない)
# - エントリポイントが /project の所有者に切り替えて動く(docker/entrypoint.sh)
# 詳しくは docker/README.md を参照。

ARG PYTHON_VERSION=3.12
# uv はバージョンを固定する(ビルドの再現性のため。更新するときはここだけ変える)。
ARG UV_VERSION=0.12.9

FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv

# --- ビルド: 依存 → アプリの順に入れ、依存の層を uv.lock が変わるまで使い回す ---
FROM python:${PYTHON_VERSION}-slim AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_CACHE=1 \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv
WORKDIR /src
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project --extra xlsx --extra postgres
COPY README.md LICENSE ./
COPY monitor_app ./monitor_app
# extras は依存の層と同じにする(sync は指定外のパッケージを削除するため)。
# デモプロジェクト(空の /project にエントリポイントがコピーする)も作っておく。
# --deploy が生成する .env(API キー)は消す(イメージに焼き込むと全員が同じキーになる)。
RUN uv sync --frozen --no-dev --no-editable --extra xlsx --extra postgres \
 && /opt/venv/bin/monitor-app startproject /opt/monitor-app/demo --deploy docker > /dev/null \
 && rm -f /opt/monitor-app/demo/.env

# --- 実行 ---
FROM python:${PYTHON_VERSION}-slim AS runtime
LABEL org.opencontainers.image.title="monitor-app" \
      org.opencontainers.image.description="Drop in a CSV, write one config file, and get a live-updating Web UI + REST API." \
      org.opencontainers.image.url="https://github.com/mikawa-bushi/monitor-app" \
      org.opencontainers.image.source="https://github.com/mikawa-bushi/monitor-app" \
      org.opencontainers.image.documentation="https://github.com/mikawa-bushi/monitor-app/blob/main/docker/README.md" \
      org.opencontainers.image.licenses="MIT"
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    MONITOR_HOST=0.0.0.0 \
    MONITOR_PORT=9990
# /project はマウントしないときのデモ用(uid 1000 の所有。名前付きボリュームにも引き継がれる)。
RUN groupadd --gid 1000 monitor \
 && useradd --uid 1000 --gid 1000 --no-create-home --home-dir /project \
        --shell /usr/sbin/nologin monitor \
 && install -d -o 1000 -g 1000 /project
COPY --from=build /opt/venv /opt/venv
COPY --from=build /opt/monitor-app/demo /opt/monitor-app/demo
COPY docker/entrypoint.sh /usr/local/bin/monitor-app-entrypoint
COPY docker/healthcheck.py /opt/monitor-app/healthcheck.py
# - ビルドした手元のチェックアウトの権限(umask 002 なら group も書き込める)に左右されない
#   よう権限を決める(root で動く HEALTHCHECK のスクリプトを、アプリが書き換えられないように)
# - setuid / setgid のプログラム(su・mount など。アプリは使わない)の特権の印を外す
RUN chmod 0755 /usr/local/bin/monitor-app-entrypoint \
 && chmod 0644 /opt/monitor-app/healthcheck.py \
 && find / -xdev -perm /6000 -type f -exec chmod a-s {} +
WORKDIR /project
EXPOSE 9990
# /api/health を Python の標準ライブラリで確かめる(curl は入れない)。DB に接続できなければ
# 503 で unhealthy。ポートはサーバーの --port(docker run <image> --port 8000)、なければ MONITOR_PORT。
# -I: PYTHONPATH などの環境変数・作業フォルダのモジュールを読まない(root で動くため)。
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "-I", "/opt/monitor-app/healthcheck.py"]
ENTRYPOINT ["/bin/sh", "/usr/local/bin/monitor-app-entrypoint"]
# 行が 1 件も無いテーブルにだけ CSV を取り込む(再起動しても入力済みの行は消えない)。
CMD ["monitor-app", "runserver", "--import-csv-if-empty"]
