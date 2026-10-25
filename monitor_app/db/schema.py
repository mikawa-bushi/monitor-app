"""config.py の表定義と、実際のデータベースの表定義のずれ(スキーマドリフト)。

``create_all`` は無いテーブルを作るだけで、作成済みの表には手を付けない。そのため
稼働後に config.py へ列を足すと、その列を参照する CRUD が失敗する(DEPLOY-13)。
v0.x(pandas の ``to_sql``)で作った表には主キーが無く、追加した行の id が NULL に
なる(CRIT-05)。:func:`inspect_schema` はこうしたずれを読み取り専用で調べ、
:class:`SchemaReport` に日本語の対処法つきでまとめる。

:func:`apply_additive_migrations` は「足りない列を NULL 許容で足す」ことだけを行う。
列の削除・型の変更・主キーの付け替えは表の作り直しが必要なため、報告するだけにする。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    Integer,
    Numeric,
    String,
    inspect,
    text,
)
from sqlalchemy.schema import CreateColumn

from .bulk import sequence_fix_sql, sequence_lag
from .engine import Database
from .registry import TableRegistry

logger = logging.getLogger("monitor_app.db")

#: 問題の種類。
MISSING_TABLE = "missing_table"
MISSING_COLUMN = "missing_column"
EXTRA_COLUMN = "extra_column"
TYPE_MISMATCH = "type_mismatch"
PRIMARY_KEY = "primary_key"
FLOAT_PRECISION = "float_precision"
DATETIME_PRECISION = "datetime_precision"
SEQUENCE_BEHIND = "sequence_behind"


@dataclass
class SchemaProblem:
    """1 件のずれ。``level`` は warning(動作に影響する)か info(参考)。"""

    kind: str
    table: str
    message: str
    column: str | None = None
    expected: str | None = None
    actual: str | None = None
    level: str = "warning"
    #: 加算的に直せる場合の SQL(足りない列の ALTER TABLE ... ADD COLUMN)。
    fix_sql: str | None = None


@dataclass
class SchemaReport:
    """:func:`inspect_schema` の結果。"""

    problems: list[SchemaProblem] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """動作に影響するずれ(warning)が無いか。"""
        return not any(p.level == "warning" for p in self.problems)

    def missing_columns(self) -> dict[str, list[str]]:
        """テーブル名 → DB に無い列名(config.py の順)。"""
        out: dict[str, list[str]] = {}
        for p in self.problems:
            if p.kind == MISSING_COLUMN and p.column is not None:
                out.setdefault(p.table, []).append(p.column)
        return out

    def log(self, log: logging.Logger = logger) -> None:
        """問題ごとに 1 行ずつログに出す(warning は WARNING、info は INFO)。"""
        for p in self.problems:
            level = logging.WARNING if p.level == "warning" else logging.INFO
            log.log(level, "表定義のずれ: %s", p.message)


def type_family(sql_type: Any) -> str | None:
    """SQL 型を大まかな論理型(int/float/str/bool/date/datetime)に分類する。

    MySQL の真偽値は TINYINT(1) として返るため bool とみなす。分類できない型
    (時刻・バイナリ・SQLite の未知の型名など)は None(比較しない)。
    """
    if isinstance(sql_type, Boolean):
        return "bool"
    if (
        type(sql_type).__name__ == "TINYINT"
        and getattr(sql_type, "display_width", None) == 1
    ):
        return "bool"
    if isinstance(sql_type, Integer):
        return "int"
    if isinstance(sql_type, (Float, Numeric)):
        return "float"
    if isinstance(sql_type, DateTime):
        return "datetime"
    if isinstance(sql_type, Date):
        return "date"
    if isinstance(sql_type, String):
        return "str"
    return None


def _compatible(expected: str, actual: str | None) -> bool:
    if actual is None or expected == actual:
        return True
    # 真偽値を 0/1 の整数列で持つ DB がある(MySQL の BOOL = TINYINT(1) など)。
    return {expected, actual} == {"bool", "int"}


def _type_name(sql_type: Any, dialect: Any) -> str:
    try:
        return sql_type.compile(dialect=dialect)
    except Exception:  # noqa: BLE001 - 表示用(コンパイルできない型は型名で代用)
        return type(sql_type).__name__


def add_column_sql(db: Database, registry: TableRegistry, table: str, col: str) -> str:
    """``table`` に ``col`` を NULL 許容で足す ALTER TABLE 文(方言に合わせて引用)。"""
    dialect = db.engine.dialect
    sa_table = registry.get(table)
    column = sa_table.c[col]
    definition = str(CreateColumn(column).compile(dialect=dialect)).strip()
    qualified = dialect.identifier_preparer.format_table(sa_table)
    return f"ALTER TABLE {qualified} ADD COLUMN {definition}"


def inspect_schema(db: Database, registry: TableRegistry) -> SchemaReport:
    """設定上の各テーブルを実際の DB と比べる(読み取りのみ)。"""
    report = SchemaReport()
    dialect = db.engine.dialect
    inspector = inspect(db.engine)
    for name, table in registry.tables.items():
        if not inspector.has_table(name):
            report.problems.append(
                SchemaProblem(
                    MISSING_TABLE,
                    name,
                    f"テーブル '{name}' はまだデータベースにありません"
                    "(サーバーの起動時・取り込み時に作成されます)",
                    level="info",
                )
            )
            continue
        live = {c["name"]: c for c in inspector.get_columns(name)}
        declared_pk = registry.primary_key(name)
        live_pk = list(
            inspector.get_pk_constraint(name).get("constrained_columns") or []
        )

        for col in table.columns:
            if col.name in live:
                continue
            if col.name == declared_pk:
                continue  # 主キーの問題として下で報告する
            sql = add_column_sql(db, registry, name, col.name)
            report.problems.append(
                SchemaProblem(
                    MISSING_COLUMN,
                    name,
                    f"テーブル '{name}' に列 '{col.name}' がありません(config.py には"
                    f"あります)。この列を使う一覧・登録が失敗します。列を足す SQL: {sql}",
                    column=col.name,
                    fix_sql=sql,
                )
            )

        declared = set(table.columns.keys())
        for col_name, info in live.items():
            if col_name in declared:
                continue
            # NOT NULL で既定値も採番も無い列は、その列を知らない INSERT を失敗させる。
            required = (
                not info.get("nullable", True)
                and info.get("default") is None
                and info.get("autoincrement") is not True
                and not info.get("computed")
                and not info.get("identity")
            )
            if required:
                report.problems.append(
                    SchemaProblem(
                        EXTRA_COLUMN,
                        name,
                        f"テーブル '{name}' の列 '{col_name}' は config.py にありません。"
                        "NOT NULL で既定値が無いため、行の追加が失敗します。"
                        "config.py に列を足すか、DB から列を削除してください",
                        column=col_name,
                    )
                )
            else:
                report.problems.append(
                    SchemaProblem(
                        EXTRA_COLUMN,
                        name,
                        f"テーブル '{name}' の列 '{col_name}' は config.py にありません"
                        "(画面・API では使われません)",
                        column=col_name,
                        level="info",
                    )
                )

        tdef_types = {c.name: type_family(c.type) for c in table.columns}
        for col_name, expected in tdef_types.items():
            info = live.get(col_name)
            if info is None or expected is None:
                continue
            actual_type = info["type"]
            actual = type_family(actual_type)
            shown = _type_name(actual_type, dialect)
            if not _compatible(expected, actual):
                report.problems.append(
                    SchemaProblem(
                        TYPE_MISMATCH,
                        name,
                        f"テーブル '{name}' の列 '{col_name}' の型が一致しません"
                        f"(config.py: {expected} / DB: {shown})。作成済みの表の型は"
                        "自動では変わらないため、値は DB 側の型で保存されます。"
                        "表を作り直すか、ALTER TABLE で型を変更してください"
                        + _existing_values_hint(expected, actual),
                        column=col_name,
                        expected=expected,
                        actual=shown,
                    )
                )
            elif (
                expected == "float"
                and db.dialect in ("mysql", "mariadb")
                and type(actual_type).__name__ == "FLOAT"
            ):
                preparer = dialect.identifier_preparer
                fix = (
                    f"ALTER TABLE {preparer.format_table(table)} "
                    f"MODIFY {preparer.quote(col_name)} DOUBLE" + _not_null(info)
                )
                report.problems.append(
                    SchemaProblem(
                        FLOAT_PRECISION,
                        name,
                        f"テーブル '{name}' の列 '{col_name}' は MySQL の単精度 FLOAT"
                        "です(有効桁数は約 7 桁で、1234567.89 が 1234570 になります)。"
                        f"倍精度にする SQL: {fix}",
                        column=col_name,
                        expected="DOUBLE",
                        actual=shown,
                        fix_sql=fix,
                    )
                )
            elif (
                expected == "datetime"
                and db.dialect in ("mysql", "mariadb")
                and type(actual_type).__name__ == "DATETIME"
                and (getattr(actual_type, "fsp", None) or 0) < 6
            ):
                # MySQL の DATETIME は既定で小数秒を持たず、23:59:59.7 を翌日の
                # 00:00:00 に丸める(新しく作る表は DATETIME(6) — D3-05)。
                preparer = dialect.identifier_preparer
                fix = (
                    f"ALTER TABLE {preparer.format_table(table)} "
                    f"MODIFY {preparer.quote(col_name)} DATETIME(6)" + _not_null(info)
                )
                report.problems.append(
                    SchemaProblem(
                        DATETIME_PRECISION,
                        name,
                        f"テーブル '{name}' の列 '{col_name}' は小数秒を保存しない"
                        f" MySQL の {shown} です(23:59:59.7 が翌日の 00:00:00 に"
                        "丸められ、日時の主キーで同じ行を探せなくなります)。"
                        f"マイクロ秒まで保存する SQL: {fix}",
                        column=col_name,
                        expected="DATETIME(6)",
                        actual=shown,
                        fix_sql=fix,
                    )
                )

        if live_pk != [declared_pk]:
            if not live_pk:
                message = (
                    f"テーブル '{name}' に主キーがありません(config.py の主キー: "
                    f"{declared_pk})。v0.x で作成した古い表の可能性があり、行の追加・"
                    "更新・削除が正しく動きません。"
                    + _rebuild_steps(db, registry, name, live)
                )
            else:
                message = (
                    f"テーブル '{name}' の主キーが一致しません(config.py: {declared_pk}"
                    f" / DB: {', '.join(live_pk)})。config.py の primary_key を DB に"
                    "合わせるか、表を作り直してください"
                )
            report.problems.append(
                SchemaProblem(
                    PRIMARY_KEY,
                    name,
                    message,
                    column=declared_pk,
                    expected=declared_pk,
                    actual=", ".join(live_pk) or None,
                )
            )
        elif db.dialect == "postgresql":
            _check_sequence(db, table, name, declared_pk, report)
    return report


def _not_null(info: dict) -> str:
    """MODIFY で NOT NULL を落とさないための句(NULL を許す列なら空)。"""
    return "" if info.get("nullable", True) else " NOT NULL"


def _existing_values_hint(expected: str, actual: str | None) -> str:
    """文字列の列を日付・日時の列に変えたときの、既存の値についての注意(D3-04)。"""
    if actual != "str" or expected not in ("date", "datetime"):
        return ""
    sample = "2026-10-21" if expected == "date" else "2026-10-21 08:00:00"
    return (
        f"。既にある値は「{sample}」の形でないと、並べ替え・期間の絞り込み・保持期間の"
        "比較が正しく動きません(monitor-app import-csv で CSV から取り込み直すと、"
        "この形にそろいます)"
    )


def _rebuild_steps(
    db: Database, registry: TableRegistry, name: str, live: dict[str, Any]
) -> str:
    """主キーの無い表を作り直す具体的な手順(D5-09)。"""
    preparer = db.engine.dialect.identifier_preparer
    table = registry.get(name)
    old = f"{name}_v0"
    quoted = preparer.format_table(table)
    quoted_old = preparer.quote(old)
    common = [c.name for c in table.columns if c.name in live]
    cols = ", ".join(preparer.quote(c) for c in common) or "..."
    if db.backend == "sqlite":
        backup, client = "monitor-app backup でバックアップし", "sqlite3 などで"
    else:
        backup, client = "pg_dump / mysqldump でバックアップし", "psql / mysql で"
    return (
        f"表を作り直してください。手順: ① サーバーを止め、{backup} ② {client} "
        f"ALTER TABLE {quoted} RENAME TO {quoted_old}; ③ サーバーを 1 回起動するか "
        "monitor-app import-csv を実行して、主キーのある新しい表を作る(CSV から"
        f"取り込み直すなら ④ は不要) ④ INSERT INTO {quoted} ({cols}) SELECT {cols} "
        f"FROM {quoted_old}; ⑤ 中身を確かめてから DROP TABLE {quoted_old};"
    )


def _check_sequence(
    db: Database, table: Any, name: str, pk: str, report: SchemaReport
) -> None:
    """PostgreSQL の連番が表の最大値より遅れていれば報告する(D5-01)。"""
    try:
        with db.engine.connect() as conn:
            lag = sequence_lag(conn, table, pk)
    except Exception as exc:  # noqa: BLE001 - 点検できない表は飛ばす
        logger.debug("連番を確認できません '%s': %s", name, exc)
        return
    if lag is None:
        return
    fix = sequence_fix_sql(db.engine, table, pk)
    report.problems.append(
        SchemaProblem(
            SEQUENCE_BEHIND,
            name,
            f"テーブル '{name}' の連番({pk})が表の最大値より遅れています(連番 "
            f"{lag[0]} / 最大値 {lag[1]})。以前の版で id を指定して取り込んだ表で、"
            "このままでは画面・API からの追加が主キーの重複で失敗します。サーバーの"
            "起動時・CSV の取り込み時に自動で進めます(今すぐ直す SQL: "
            f"{fix})",
            column=pk,
            expected=str(lag[1]),
            actual=str(lag[0]),
            level="info",
            fix_sql=fix,
        )
    )


def apply_additive_migrations(
    db: Database, registry: TableRegistry, report: SchemaReport
) -> list[str]:
    """``report`` の「足りない列」を NULL 許容で追加し、実行した SQL を返す。

    列の削除・型の変更・主キーの変更は行わない(報告のみ)。全件を 1 トランザクション
    で実行する(PostgreSQL・SQLite は失敗すれば全て取り消される。MySQL の DDL は
    文ごとに確定する)。
    """
    statements = [
        p.fix_sql
        for p in report.problems
        if p.kind == MISSING_COLUMN and p.fix_sql is not None
    ]
    if not statements:
        return []
    with db.connect() as conn:
        for sql in statements:
            conn.execute(text(sql))
    for sql in statements:
        logger.info("列を追加しました: %s", sql)
    return statements
