"""E2E テスト用の pytest 設定: マーカー ``e2e`` をこのディレクトリで登録する。

pyproject.toml と既存の tests/conftest.py を変更しないための選択。ディレクトリの
conftest.py は、そのディレクトリのテストモジュールを import する前に必ず読み込まれる
(``pytest_configure`` は後から登録されたプラグインにも呼ばれる)。そのため全体実行・
単一ファイル実行・``-m "not e2e"``・``--strict-markers`` のいずれでも、未登録マーカーの
警告やエラーにならない。
"""


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "e2e: 実サーバ(uvicorn サブプロセス)を起動する E2E テスト。"
        "-m 'not e2e' で除外できる",
    )
