"""config.py の書き方の誤りへの案内(#DX-07)。

- 未知のキー(綴りの誤り)は読み込みを止めず ConfigWarning で警告し、近いキーを提案する
- config.py 実行中の ValidationError は、モデル名と行番号つきの日本語にする
- ビュー・KPI の SQL 末尾の ``;`` を取り除く
"""

from __future__ import annotations

import textwrap
import warnings
from pathlib import Path

import pytest
from contract import helpers, snapshot
from pydantic import ValidationError
from typer.testing import CliRunner

from monitor_app import (
    CellStyle,
    ChartDef,
    KpiCard,
    MonitorConfig,
    TableDef,
    Threshold,
    ViewDef,
)
from monitor_app.cli import app as cli_app
from monitor_app.integrations import netdiag, network_checker, procdiag
from monitor_app.settings import ConfigWarning
from monitor_app.settings.declarative import collect_config_warnings
from monitor_app.settings.loader import (
    ConfigError,
    load_config,
    load_config_with_warnings,
)

SCAFFOLD_CONFIG = (
    Path(__file__).parent.parent / "monitor_app" / "scaffold" / "config.py"
)


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    helpers.isolate_from_host(monkeypatch, tmp_path)


def _write(path: Path, source: str) -> Path:
    path.write_text(textwrap.dedent(source).lstrip(), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# 未知のキーの警告
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "build, model, key, suggestion",
    [
        (
            lambda: MonitorConfig(app_tittle="x"),
            "MonitorConfig",
            "app_tittle",
            "app_title",
        ),
        (
            lambda: MonitorConfig(refresh_interval=500),
            "MonitorConfig",
            "refresh_interval",
            "refresh_interval_ms",
        ),
        (
            lambda: TableDef(columns=["id"], primry_key="id"),
            "TableDef",
            "primry_key",
            "primary_key",
        ),
        (
            lambda: ViewDef(query="SELECT 1", tittle="t"),
            "ViewDef",
            "tittle",
            "title",
        ),
        (lambda: ChartDef(x="a", y="b", lcll=1), "ChartDef", "lcll", "lcl"),
        (
            lambda: MonitorConfig(kiosk={"rotate_second": 5}),
            "KioskConfig",
            "rotate_second",
            "rotate_seconds",
        ),
        (
            lambda: KpiCard(title="t", query="SELECT 1", Unit="件"),
            "KpiCard",
            "Unit",
            "unit",
        ),
    ],
)
def test_unknown_keys_warn_with_a_suggestion(build, model, key, suggestion):
    with pytest.warns(ConfigWarning) as record:
        build()
    warning = record[0].message
    assert isinstance(warning, ConfigWarning)
    assert (warning.model, warning.key, warning.suggestion) == (model, key, suggestion)
    assert f"'{key}'" in str(warning) and f"'{suggestion}'" in str(warning)
    assert record[0].filename == __file__  # 場所は利用者のコード(このテスト)の行


def test_unknown_key_without_a_close_match_lists_the_valid_keys():
    with pytest.warns(ConfigWarning) as record:
        CellStyle(colour="red")
    warning = record[0].message
    assert warning.suggestion is None
    assert "greater_than" in str(warning) and "bold" in str(warning)


def test_unknown_keys_still_load_and_keep_defaults():
    """契約: 2.x では未知のキーで読み込みを止めない(警告のみ)。"""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        config = MonitorConfig(
            refresh_intrval_ms=500,
            tables={"t": TableDef(columns=["id", "name"], primay_key="id")},
        )
    assert config.refresh_interval_ms == 2000
    assert config.tables["t"].primary_key == "id"
    assert "refresh_intrval_ms" not in config.model_dump()  # 誤ったキーは混ざらない


def test_threshold_alias_and_field_name_are_both_known():
    with warnings.catch_warnings():
        warnings.simplefilter("error", ConfigWarning)
        by_alias = Threshold(value=1, **{"class": "bg-danger"})
        by_name = Threshold(value=1, css_class="bg-danger")
        CellStyle.model_validate({"greater_than": {"value": 1, "class": "x"}})
    assert by_alias == by_name


def test_threshold_typo_of_the_alias_suggests_class():
    with (
        pytest.warns(ConfigWarning) as record,
        pytest.raises(ValidationError),  # class が無いので ValidationError
    ):
        Threshold(value=1, klass="bg-danger")
    assert record[0].message.suggestion == "class"


def test_nested_dicts_are_checked_too():
    with collect_config_warnings() as found, warnings.catch_warnings():
        warnings.simplefilter("ignore")
        MonitorConfig(tables={"t": {"columns": ["id"], "primay_key": "id"}})
    assert [(w.model, w.key) for w in found] == [("TableDef", "primay_key")]


def test_no_false_positives_on_shipped_configs_and_presets(tmp_path):
    """同梱の雛形・v2.3.1 の利用者の設定・v1 形式・連携プリセットで警告が出ない。"""
    with warnings.catch_warnings():
        warnings.simplefilter("error", ConfigWarning)
        for path in (
            SCAFFOLD_CONFIG,
            snapshot.GOLDEN_PROJECT / "config.py",
            snapshot.LEGACY_V1_CONFIG,
        ):
            config, found = load_config_with_warnings(path)
            assert found == [], path
        config = load_config(SCAFFOLD_CONFIG)
        config = procdiag.attach(config, db_path="p.db", findings_json="f.json")
        config = netdiag.attach(config)
        config = network_checker.attach(config, hosts=["plc-01"])
        MonitorConfig.model_validate(config.model_dump())
        MonitorConfig.model_validate(config.model_dump(by_alias=True))


def test_load_config_with_warnings_reports_the_line_of_the_typo(tmp_path):
    path = _write(
        tmp_path / "config.py",
        """
        from monitor_app import MonitorConfig, TableDef

        config = MonitorConfig(
            app_tittle="Line 1",
            tables={
                "users": TableDef(
                    columns=["id", "name"],
                    primay_key="id",
                ),
            },
        )
        """,
    )
    with warnings.catch_warnings():
        warnings.simplefilter("error", ConfigWarning)  # 表示はせず集めるだけ
        config, found = load_config_with_warnings(path)
    assert config.app_title == "Monitor App"
    assert found == [
        "config.py 8 行目: TableDef の未知のキー 'primay_key' は無視されます"
        "('primary_key' の誤りではありませんか?)",
        "config.py 4 行目: MonitorConfig の未知のキー 'app_tittle' は無視されます"
        "('app_title' の誤りではありませんか?)",
    ]


def test_load_config_shows_each_warning_once_per_process(tmp_path):
    path = _write(
        tmp_path / "config.py",
        """
        from monitor_app import MonitorConfig
        config = MonitorConfig(app_tittle="x")
        """,
    )
    with warnings.catch_warnings(record=True) as record:
        warnings.simplefilter("always")
        load_config(path)
        load_config(path)  # runserver は検証と app_factory で 2 回読む
    shown = [w for w in record if issubclass(w.category, ConfigWarning)]
    assert len(shown) == 1
    assert Path(shown[0].filename) == path and shown[0].lineno == 2


def test_warning_filters_set_by_config_py_are_kept(tmp_path):
    """config.py が自分で設定した警告のフィルタを、読み込みが元に戻さない。"""
    path = _write(
        tmp_path / "config.py",
        """
        import warnings
        warnings.filterwarnings("ignore", message="noisy-library-message")
        from monitor_app import MonitorConfig
        config = MonitorConfig(app_tittle="x")
        """,
    )

    def kept() -> bool:
        return any(
            f[0] == "ignore"
            and f[1] is not None
            and f[1].pattern == "noisy-library-message"
            for f in warnings.filters
        )

    with warnings.catch_warnings():
        with pytest.warns(ConfigWarning):  # pytest.warns は抜けるときにフィルタを戻す
            load_config(path)
            assert kept()
        load_config_with_warnings(path)
        assert kept()


def test_warnings_as_errors_become_a_short_config_error(tmp_path):
    path = _write(
        tmp_path / "config.py",
        """
        from monitor_app import MonitorConfig
        config = MonitorConfig(app_tittle="strict")
        """,
    )
    with warnings.catch_warnings():
        warnings.simplefilter("error", ConfigWarning)
        with pytest.raises(ConfigError, match=r"config\.py 2 行目: MonitorConfig"):
            load_config(path)


# ---------------------------------------------------------------------------
# ValidationError の日本語化
# ---------------------------------------------------------------------------
def test_validation_error_names_the_model_line_and_typo_hint(tmp_path):
    path = _write(
        tmp_path / "config.py",
        """
        from monitor_app import MonitorConfig, TableDef

        config = MonitorConfig(
            tables={
                "users": TableDef(
                    colums=["id", "name"],
                ),
            },
        )
        """,
    )
    with pytest.raises(ConfigError) as info:
        load_config(path)
    message = str(info.value)
    assert message.splitlines() == [
        "config.py の設定に問題があります(config.py 5 行目、TableDef):",
        "  - columns: 必須の項目が指定されていません",
        "  ヒント: config.py 6 行目: TableDef の未知のキー 'colums' は無視されます"
        "('columns' の誤りではありませんか?)",
    ]
    assert "pydantic" not in message and "input_value" not in message


@pytest.mark.parametrize(
    "body, expected",
    [
        (
            'MonitorConfig(refresh_mode="fast")',
            "refresh_mode: 'sse' / 'polling' のいずれかを指定してください(指定値: 'fast')",
        ),
        (
            'MonitorConfig(tables={"t": TableDef(columns={"id": "int", "t": "double"})})',
            "columns → t: 'int' / 'float' / 'str' / 'bool' / 'date' / 'datetime' の"
            "いずれかを指定してください(指定値: 'double')",
        ),
        (
            'MonitorConfig(tables={"t": TableDef(columns=5)})',
            "columns: リスト [...] または 辞書 {...}を指定してください(指定値: 5)",
        ),
        (
            'MonitorConfig(views={"v": ViewDef(query="SELECT 1", chart=ChartDef(x="a", y=[1]))})',
            "y → 0: 文字列を指定してください(指定値: 1)",
        ),
        (
            "MonitorConfig(refresh_interval_ms='fast')",
            "refresh_interval_ms: 整数を指定してください(指定値: 'fast')",
        ),
        (
            "MonitorConfig(dashboard={'columns': 9})",
            "dashboard → columns: 4 以下の値を指定してください(指定値: 9)",
        ),
        (
            "MonitorConfig(alerts=[AlertRule(view='ghost', column='c', op='>', value=1)])",
            "alerts[0]: 監視対象ビュー 'ghost' が views に存在しません",
        ),
    ],
)
def test_validation_errors_are_explained_in_japanese(tmp_path, body, expected):
    path = _write(
        tmp_path / "config.py",
        "from monitor_app import AlertRule, ChartDef, MonitorConfig, TableDef, ViewDef\n"
        f"config = {body}\n",
    )
    with pytest.raises(ConfigError) as info:
        load_config(path)
    lines = str(info.value).splitlines()
    assert lines[0].startswith("config.py の設定に問題があります(config.py 2 行目、")
    assert f"  - {expected}" in lines


def test_syntax_error_shows_the_line(tmp_path):
    path = _write(
        tmp_path / "config.py",
        """
        from monitor_app import MonitorConfig
        config = MonitorConfig(
            app_title="x"
        """,
    )
    with pytest.raises(ConfigError) as info:
        load_config(path)
    assert str(info.value).startswith(
        "config.py の書き方(Python の文法)に誤りがあります(config.py "
    )


def test_missing_import_is_explained(tmp_path):
    path = _write(
        tmp_path / "config.py",
        """
        from monitor_app import MonitorConfig
        config = MonitorConfig(tables={"t": TableDef(columns=["id"])})
        """,
    )
    with pytest.raises(ConfigError) as info:
        load_config(path)
    message = str(info.value)
    assert "(config.py 2 行目): NameError" in message
    assert "from monitor_app import TableDef を追加してください" in message


def test_misspelled_name_suggests_the_right_one(tmp_path):
    path = _write(
        tmp_path / "config.py",
        """
        from monitor_app import MonitorConfig, TableDef
        config = MonitorConfig(tables={"t": TableDf(columns=["id"])})
        """,
    )
    with pytest.raises(ConfigError) as info:
        load_config(path)
    assert "'TableDef'" in str(info.value)  # Python 3.10 でも 3.13 でも提案する


def test_check_command_prints_the_friendly_error(tmp_path, monkeypatch):
    path = _write(
        tmp_path / "config.py",
        """
        from monitor_app import MonitorConfig, TableDef
        config = MonitorConfig(tables={"t": TableDef(colums=["id"])})
        """,
    )
    result = CliRunner().invoke(cli_app, ["check", "--config", str(path)])
    assert result.exit_code == 1
    assert "TableDef" in result.output and "2 行目" in result.output
    assert "'columns' の誤りではありませんか?" in result.output
    assert "Traceback" not in result.output


def test_v1_validation_errors_use_the_same_formatter(tmp_path):
    path = _write(
        tmp_path / "config.py",
        """
        ALLOWED_TABLES = {"users": {"columns": ["id"], "primary_key": "missing"}}
        """,
    )
    with pytest.raises(ConfigError) as info:
        load_config(path)
    assert str(info.value).startswith("config.py の設定に問題があります(TableDef):")
    assert "primary_key 'missing' は columns" in str(info.value)


# ---------------------------------------------------------------------------
# SQL 末尾の ;
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "query, expected",
    [
        ("SELECT * FROM t;", "SELECT * FROM t"),
        ("SELECT * FROM t ;  \n", "SELECT * FROM t"),
        ("SELECT * FROM t;;", "SELECT * FROM t"),
        ("SELECT * FROM t\n", "SELECT * FROM t\n"),  # ; が無ければそのまま
        ("SELECT ';' AS s FROM t", "SELECT ';' AS s FROM t"),
    ],
)
def test_trailing_semicolons_are_stripped(query, expected):
    assert ViewDef(query=query).query == expected
    assert KpiCard(title="k", query=query).query == expected


def test_view_with_trailing_semicolon_can_be_paged():
    from fastapi.testclient import TestClient

    from monitor_app.main import create_app
    from monitor_app.settings.runtime import AppSettings

    config = MonitorConfig(
        tables={"t": TableDef(columns={"id": "int", "v": "float"})},
        views={"v": ViewDef(query="SELECT id, v FROM t ORDER BY id;")},
        kpis={"k": KpiCard(title="k", query="SELECT COUNT(*) FROM t;")},
    )
    app = create_app(config, AppSettings(_env_file=None, database_url="sqlite://"))
    try:
        client = TestClient(app)
        client.post("/api/ingest/t", json=[{"id": 1, "v": 1.5}, {"id": 2, "v": 2.5}])
        res = client.get("/api/views/v", params={"limit": 1})
        assert res.status_code == 200, res.text
        assert res.json()["data"] == [{"id": 1, "v": 1.5}]
        assert client.get("/api/kpis").json()["kpis"][0]["value"] == 2
    finally:
        app.state.services.db.dispose()  # インメモリ DB は close() では閉じない


# ---------------------------------------------------------------------------
# レビューで見つかった問題の回帰テスト
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "body, expected",
    [
        (
            'MonitorConfig(tables={"t": TableDef(columns={"id": "int", "温度[℃]": "x"})})',
            "columns → 温度[℃]: 'int' / 'float' / 'str' / 'bool' / 'date' / "
            "'datetime' のいずれかを指定してください(指定値: 'x')",
        ),
        (
            'MonitorConfig(tables={"t": TableDef(columns={"id": "int", "str": "x"})})',
            "columns → str: 'int' / 'float' / 'str' / 'bool' / 'date' / "
            "'datetime' のいずれかを指定してください(指定値: 'x')",
        ),
        (
            'MonitorConfig(views={"ライン[1]": {"query": 5}})',
            "views → ライン[1] → query: 文字列を指定してください(指定値: 5)",
        ),
    ],
)
def test_names_that_look_like_type_tags_are_kept_in_errors(tmp_path, body, expected):
    """``温度[℃]`` のような列名・``str`` という列名を Union の候補名と取り違えない。"""
    path = _write(
        tmp_path / "config.py",
        f"from monitor_app import MonitorConfig, TableDef\nconfig = {body}\n",
    )
    with pytest.raises(ConfigError) as info:
        load_config(path)
    assert str(info.value).splitlines()[1:] == [f"  - {expected}"]


V1_WITH_TYPO = """
ALLOWED_TABLES = {"users": {"columns": ["id", "name"], "primary_key": "id"}}
VIEW_TABLES = {"users_view": {"query": "SELECT id, name FROM users"}}
TABLE_CELL_STYLES = {
    "users_view": {
        "name": {
            "colour": "red",
            "bold": True,
        },
    },
}
"""


def test_v1_typos_point_at_the_line_in_config_py(tmp_path):
    path = _write(tmp_path / "config.py", V1_WITH_TYPO)
    with warnings.catch_warnings(record=True) as record:
        warnings.simplefilter("always")
        config = load_config(path)
    assert config.views["users_view"].styles["name"].bold is True
    (shown,) = [w for w in record if issubclass(w.category, ConfigWarning)]
    assert (Path(shown.filename), shown.lineno) == (path, 6)
    _config, found = load_config_with_warnings(path)
    assert found == [
        "config.py 6 行目: CellStyle の未知のキー 'colour' は無視されます"
        "(指定できるキー: greater_than, less_than, equal_to, width, font_size, "
        "align, bold)"
    ]


def test_v1_typos_with_warnings_as_errors_are_a_config_error(tmp_path):
    path = _write(tmp_path / "config.py", V1_WITH_TYPO)
    with warnings.catch_warnings():
        warnings.simplefilter("error", ConfigWarning)
        with pytest.raises(ConfigError, match=r"config\.py 6 行目: CellStyle"):
            load_config(path)


def test_v1_validation_error_includes_the_typo_hint(tmp_path):
    path = _write(
        tmp_path / "config.py",
        'ALLOWED_TABLES = {"users": {"columns": ["id"]}}\n'
        'VIEW_TABLES = {"v": {"query": "SELECT 1"}}\n'
        'TABLE_CELL_STYLES = {"v": {"id": {"greater_than": {"valu": 1, "class": "x"}}}}\n',
    )
    with pytest.raises(ConfigError) as info:
        load_config(path)
    message = str(info.value)
    assert message.startswith("config.py の設定に問題があります(")
    assert "ヒント: config.py 3 行目: Threshold の未知のキー 'valu'" in message


def test_typo_lines_follow_a_rewritten_config_py(tmp_path):
    """同じプロセスで書き換えられた config.py でも、古い行の内容で探さない。"""
    path = tmp_path / "config.py"
    path.write_text(
        "from monitor_app import MonitorConfig\nconfig = MonitorConfig(app_tittle='a')\n",
        encoding="utf-8",
    )
    assert load_config_with_warnings(path)[1][0].startswith("config.py 2 行目")
    path.write_text(
        "from monitor_app import MonitorConfig\n\n\nconfig = MonitorConfig(\n"
        "    footer_text='x',\n    app_tittle='b',\n)\n",
        encoding="utf-8",
    )
    assert load_config_with_warnings(path)[1][0].startswith("config.py 6 行目")
