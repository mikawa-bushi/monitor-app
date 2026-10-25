"""ゴールデンアップグレードフィクスチャの DB(v2.3.1 の利用者の DB)を生成する。

**v2.3.1 のコードでのみ実行すること。** このフィクスチャは「v2.3.1 の利用者が実際に
持っている DB」の写しであり、リファクタ後のコードで再生成すると意味を失う
(tests/test_upgrade_v2_3_1.py は、この DB を後続の版で開けることを検証する)。

使い方(リポジトリ直下で)::

    uv run python tests/contract/_make_upgrade_fixture.py

入力(コミット済み): ``fixtures/v2_3_1_project/`` の config.py・.env・csv/・sources/
出力: ``fixtures/v2_3_1_project/database.sql``(sqlite3 の iterdump 形式のテキスト)

生成手順は利用者の操作をなぞる:

1. ``monitor-app import-csv`` で csv/ を取り込む
2. ``monitor-app sync-sources`` で sources(JSONL)を取り込み、``_ingest_state`` に
   取り込みカーソルを残す
3. .env の ``MONITOR_AUDIT_ENABLED=true`` を効かせたアプリへ、API 経由で CRUD を
   書き込む(作業者の入力と監査ログが残る)
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

_TESTS_DIR = Path(__file__).resolve().parents[1]
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))  # `contract` パッケージを解決できるようにする

from contract import snapshot  # noqa: E402

#: API 経由の書き込み(作業者がフォームや外部システムから入れたデータを模す)。
CRUD_WRITES = [
    ("POST", "/api/tables/orders", {"user_id": 2, "product_id": 1, "amount": 3}),
    ("PUT", "/api/tables/orders/6", {"amount": 4}),
    ("POST", "/api/tables/users", {"name": "山田 太郎", "email": "yamada@example.com"}),
    ("DELETE", "/api/tables/orders/2", None),
]


def _run_cli(project: Path, *args: str) -> None:
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("MONITOR_")}
    env.update(PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    proc = subprocess.run(
        [sys.executable, "-m", "monitor_app", *args],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if proc.returncode != 0:
        raise SystemExit(f"monitor-app {' '.join(args)} が失敗しました:\n{proc.stderr}")
    print(proc.stdout.strip())


def _write_via_api(project: Path) -> None:
    from fastapi.testclient import TestClient

    from monitor_app.main import create_app
    from monitor_app.settings.loader import load_config
    from monitor_app.settings.runtime import AppSettings

    cwd = Path.cwd()
    os.chdir(project)  # 既定の DB URL(相対パス)と .env をプロジェクト基準で解決する
    try:
        with snapshot.monitor_environ():
            settings = AppSettings()
        if not settings.audit_enabled:
            raise SystemExit(".env の MONITOR_AUDIT_ENABLED=true が効いていません")
        app = create_app(load_config("config.py"), settings)
        with TestClient(app) as client:
            for method, path, body in CRUD_WRITES:
                res = client.request(method, path, json=body)
                if res.status_code >= 400:
                    raise SystemExit(f"{method} {path} が失敗しました: {res.text}")
                print(f"  {method} {path} → {res.status_code}")
        app.state.db.dispose()
    finally:
        os.chdir(cwd)


def _tidy(statement: str) -> str:
    """DDL(CREATE 文)の行末空白を落とす(SQLAlchemy が出力する意味のない空白)。

    データ(INSERT 文)は値の中身に触れないよう、そのまま残す。
    """
    if not statement.startswith("CREATE "):
        return statement
    return "\n".join(line.rstrip() for line in statement.splitlines())


def main() -> int:
    import monitor_app

    if monitor_app.__version__ != "2.3.1":
        raise SystemExit(
            f"v2.3.1 のコードで実行してください(現在 {monitor_app.__version__})。"
            "後続の版で再生成するとアップグレード検証の意味がなくなります。"
        )
    output = snapshot.GOLDEN_PROJECT / "database.sql"
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp) / "project"
        shutil.copytree(
            snapshot.GOLDEN_PROJECT, project, ignore=shutil.ignore_patterns("*.sql")
        )
        (project / "instances").mkdir()
        _run_cli(project, "import-csv")
        _run_cli(project, "sync-sources")
        _write_via_api(project)
        conn = sqlite3.connect(project / "instances" / "database.db")
        try:
            dump = "\n".join(_tidy(statement) for statement in conn.iterdump())
        finally:
            conn.close()
    header = (
        f"-- monitor-app {monitor_app.__version__} で生成したゴールデン DB"
        "(tests/contract/_make_upgrade_fixture.py)。\n"
        "-- 手で編集しないこと。tests/test_upgrade_v2_3_1.py が後続の版で開けることを検証する。\n"
    )
    output.write_text(header + dump + "\n", encoding="utf-8", newline="\n")
    print(f"書き出しました: {output.relative_to(_TESTS_DIR.parent)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
