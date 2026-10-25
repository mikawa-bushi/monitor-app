"""コンテナの HEALTHCHECK: サーバーの ``/api/health`` が 200 を返すかを確かめる。

標準ライブラリだけで動く(イメージに curl は入れない)。確かめるポートは、
サーバー(PID 1)の引数の ``--port``、なければ環境変数 ``MONITOR_PORT``、なければ 9990。
「docker run <イメージ> --port 8000」のようにエントリポイントへオプションを渡して
起動しても、実際に待ち受けているポートを確かめる(DB に接続できなければ 503 で失敗)。

- コンテナ内の自分自身(127.0.0.1)へ問い合わせるため、HTTP_PROXY などのプロキシの
  環境変数は使わない(社内のプロキシの設定があるとプロキシへ送られて失敗する)。
- HEALTHCHECK は root で動くため、root なら nobody(65534)に切り替えてから問い合わせる
  (切り替えられない --cap-drop ALL などではそのまま。権限は要らない)。
"""

from __future__ import annotations

import os
import sys
import urllib.request

DEFAULT_PORT = "9990"
#: 応答を待つ秒数(HEALTHCHECK の --timeout より短くする)。
TIMEOUT = 4
#: root で起動されたときに切り替えるユーザー(nobody / nogroup)。
UNPRIVILEGED_ID = 65534


def port_from_args(args: list[str], environ: dict[str, str]) -> str:
    """サーバーのコマンドライン引数と環境変数から、待ち受けポートを決める。"""
    port = None
    for index, arg in enumerate(args):
        if arg == "--port" and index + 1 < len(args):
            port = args[index + 1]
        elif arg.startswith("--port="):
            port = arg.split("=", 1)[1]
    return port or environ.get("MONITOR_PORT") or DEFAULT_PORT


def server_args(cmdline: str = "/proc/1/cmdline") -> list[str]:
    """PID 1(エントリポイントが exec したサーバー)の引数。読めなければ空。"""
    try:
        with open(cmdline, "rb") as fh:
            raw = fh.read()
    except OSError:
        return []
    return [part.decode("utf-8", "replace") for part in raw.split(b"\0") if part]


def drop_privileges() -> None:
    """root なら nobody に切り替える(切り替えられなければそのまま続ける)。"""
    if not hasattr(os, "geteuid") or os.geteuid() != 0:
        return
    try:
        os.setgroups([])
        os.setgid(UNPRIVILEGED_ID)
        os.setuid(UNPRIVILEGED_ID)
    except OSError:
        pass


def fetch_status(url: str) -> int:
    """``url`` の HTTP ステータス。プロキシの環境変数(HTTP_PROXY など)は使わない。"""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(url, timeout=TIMEOUT) as response:
        return response.status


def main() -> int:
    port = port_from_args(server_args(), dict(os.environ))
    drop_privileges()
    url = f"http://127.0.0.1:{port}/api/health"
    try:
        status = fetch_status(url)
    except Exception as exc:  # noqa: BLE001 - 理由を問わず「異常」として終了コード 1
        print(f"{url}: {exc}", file=sys.stderr)
        return 1
    return 0 if status == 200 else 1


if __name__ == "__main__":
    sys.exit(main())
