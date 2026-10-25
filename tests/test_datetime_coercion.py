"""日付・日時の表記ゆれの変換(CRIT-12)。

Excel の日本語 CSV(``2026/10/21 8:00``)、ISO 8601(``T`` か空白、秒・小数秒は任意、
``Z`` や ``+09:00``)を読み、タイムゾーン付きの値はこのコンピュータの現地時刻へ直して
tzinfo を外す。Python 3.10 でも同じ結果になる(``fromisoformat`` の版差に頼らない)。
"""

from __future__ import annotations

import datetime as dt

import pytest

from monitor_app import MonitorConfig, TableDef
from monitor_app.db.engine import Database
from monitor_app.db.registry import TableRegistry
from monitor_app.services.coercion import (
    CoercionError,
    coerce,
    parse_date,
    parse_datetime,
    to_local_naive,
)
from monitor_app.services.importer import CsvImporter
from monitor_app.services.rows import converter_for


def _local(aware: dt.datetime) -> dt.datetime:
    return aware.astimezone().replace(tzinfo=None)


UTC = dt.timezone.utc
JST = dt.timezone(dt.timedelta(hours=9))


@pytest.mark.parametrize(
    "text,expected",
    [
        ("2026-10-21T08:30:00", dt.datetime(2026, 10, 21, 8, 30)),
        ("2026-10-21 08:30:00", dt.datetime(2026, 10, 21, 8, 30)),
        ("2026-10-21 08:30", dt.datetime(2026, 10, 21, 8, 30)),
        ("2026-10-21t08:30", dt.datetime(2026, 10, 21, 8, 30)),
        ("2026-10-21  8:05:07", dt.datetime(2026, 10, 21, 8, 5, 7)),
        ("2026-10-21T08:30:00.5", dt.datetime(2026, 10, 21, 8, 30, 0, 500000)),
        ("2026-10-21T08:30:00,25", dt.datetime(2026, 10, 21, 8, 30, 0, 250000)),
        ("2026-10-21T08:30:00.123", dt.datetime(2026, 10, 21, 8, 30, 0, 123000)),
        # 7 桁以上(.NET など)は 6 桁(マイクロ秒)で切り捨てる
        ("2026-10-21T08:30:00.1234567", dt.datetime(2026, 10, 21, 8, 30, 0, 123456)),
        ("2026/10/21 8:00", dt.datetime(2026, 10, 21, 8, 0)),
        ("2026/1/5 23:59:59", dt.datetime(2026, 1, 5, 23, 59, 59)),
        ("2026/10/21", dt.datetime(2026, 10, 21)),
        ("2026-1-5", dt.datetime(2026, 1, 5)),
        ("2026-10-21", dt.datetime(2026, 10, 21)),
        ("  2026/10/21 8:00  ", dt.datetime(2026, 10, 21, 8, 0)),
    ],
)
def test_naive_datetime_formats(text, expected):
    assert coerce(text, "datetime") == expected
    assert parse_datetime(text.strip()) == expected
    assert converter_for("datetime")(text) == expected  # CSV 取り込みの速い経路


@pytest.mark.parametrize(
    "text,aware",
    [
        ("2026-10-21T08:00:00Z", dt.datetime(2026, 10, 21, 8, 0, tzinfo=UTC)),
        ("2026-10-21T08:00:00z", dt.datetime(2026, 10, 21, 8, 0, tzinfo=UTC)),
        ("2026-10-21 17:00+09:00", dt.datetime(2026, 10, 21, 17, 0, tzinfo=JST)),
        ("2026-10-21T17:00:00+0900", dt.datetime(2026, 10, 21, 17, 0, tzinfo=JST)),
        ("2026-10-21T17:00:00+09", dt.datetime(2026, 10, 21, 17, 0, tzinfo=JST)),
        (
            "2026-10-21T03:00:00.250-05:00",
            dt.datetime(
                2026,
                10,
                21,
                3,
                0,
                0,
                250000,
                tzinfo=dt.timezone(-dt.timedelta(hours=5)),
            ),
        ),
        ("2026/10/21 17:00 +09:00", dt.datetime(2026, 10, 21, 17, 0, tzinfo=JST)),
    ],
)
def test_aware_datetimes_become_local_naive(text, aware):
    value = coerce(text, "datetime")
    assert value.tzinfo is None
    assert value == _local(aware)
    assert converter_for("datetime")(text) == value


def test_aware_datetime_objects_are_converted_too():
    aware = dt.datetime(2026, 10, 21, 8, 0, tzinfo=UTC)
    assert coerce(aware, "datetime") == _local(aware)
    naive = dt.datetime(2026, 10, 21, 8, 0)
    assert coerce(naive, "datetime") is naive


def test_local_conversion_falls_back_when_the_os_cannot_convert():
    class Stubborn(dt.datetime):
        def astimezone(self, tz=None):
            if tz is None:
                raise OSError(22, "Invalid argument")  # Windows の 1970 年より前など
            return super().astimezone(tz)

    old = Stubborn(1900, 1, 1, 0, 0, tzinfo=UTC)
    local = to_local_naive(old)
    offset = dt.datetime.now().astimezone().utcoffset()
    assert local == dt.datetime(1900, 1, 1) + offset


@pytest.mark.parametrize(
    "text",
    [
        "not a time",
        "2026-13-01 08:00",
        "2026-02-30",
        "2026/10/21 24:00",
        "2026/10/21 8:60",
        "2026-10/21",  # 区切りが混在
        "2026-10-21T08:00+25:00",
        "26/10/21 8:00",  # 2 桁の年は受け付けない
    ],
)
def test_invalid_datetimes_keep_the_message_format(text):
    with pytest.raises(CoercionError) as info:
        coerce(text, "datetime")
    assert str(info.value) == f"'{text}' を datetime に変換できません"
    with pytest.raises(CoercionError) as info:
        converter_for("datetime")(text)
    assert str(info.value) == f"'{text}' を datetime に変換できません"


@pytest.mark.parametrize(
    "text,expected",
    [
        ("2026-10-21", dt.date(2026, 10, 21)),
        ("2026/10/21", dt.date(2026, 10, 21)),
        ("2026/1/5", dt.date(2026, 1, 5)),
        ("2026-1-5", dt.date(2026, 1, 5)),
        (" 2026/01/05 ", dt.date(2026, 1, 5)),
    ],
)
def test_date_accepts_slashes_and_unpadded_parts(text, expected):
    assert coerce(text, "date") == expected
    assert parse_date(text.strip()) == expected
    assert converter_for("date")(text) == expected


@pytest.mark.parametrize("text", ["2026/13/01", "2026/2/30", "2026-10/21", "x"])
def test_invalid_dates_keep_the_message_format(text):
    with pytest.raises(CoercionError) as info:
        coerce(text, "date")
    assert str(info.value) == f"'{text}' を date に変換できません"
    with pytest.raises(CoercionError) as info:
        converter_for("date")(text)
    assert str(info.value) == f"'{text}' を date に変換できません"


def test_date_does_not_accept_a_time_part():
    with pytest.raises(CoercionError):
        coerce("2026/10/21 8:00", "date")


def test_excel_timestamps_sort_chronologically_after_import(tmp_path):
    """Excel の「2026/10/21 8:00」を datetime 列に取り込むと時刻順に並ぶ(文字列の
    並びでは 10:00 が 8:00 より前になっていた)。"""
    config = MonitorConfig(
        tables={"m": TableDef(columns={"id": "int", "ts": "datetime", "v": "float"})}
    )
    csv_dir = tmp_path / "csv"
    csv_dir.mkdir()
    (csv_dir / "m.csv").write_bytes(
        "id,ts,v\n1,2026/10/21 8:00,1\n2,2026/10/21 10:00,2\n3,2026/10/21 9:00,3\n"
        "4,2026/10/20 23:30,4\n".encode("cp932")
    )
    db = Database(f"sqlite:///{(tmp_path / 'app.db').as_posix()}")
    registry = TableRegistry(config)
    report = CsvImporter(config, registry, db, csv_dir).import_all()
    assert report.total_inserted == 4
    with db.connect() as conn:
        ids = [row[0] for row in conn.exec_driver_sql("SELECT id FROM m ORDER BY ts")]
    assert ids == [4, 1, 3, 2]
    db.dispose()
