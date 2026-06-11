"""SQLAlchemy エンジンとコネクション管理。"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import Connection, create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.pool import StaticPool


class Database:
    """接続 URL を受け取り、エンジンとコネクションを供給する薄いラッパ。

    monitor-app は同期 SQLAlchemy を使う。FastAPI のスレッドプール上で
    十分な性能が出るうえ、非同期 DB ドライバよりデバッグが容易なため。
    """

    def __init__(self, url: str, echo: bool = False) -> None:
        self.url = url
        connect_args = {}
        engine_kwargs = {}
        if url.startswith("sqlite"):
            # FastAPI は複数スレッドからアクセスするため。
            connect_args["check_same_thread"] = False
            if ":memory:" in url or url == "sqlite://":
                # インメモリ DB は接続ごとに別物になる。単一コネクションを
                # 共有する StaticPool で全アクセスから同じ DB を見えるようにする。
                engine_kwargs["poolclass"] = StaticPool
        self.engine: Engine = create_engine(
            url, echo=echo, future=True, connect_args=connect_args, **engine_kwargs
        )

    @contextmanager
    def connect(self) -> Iterator[Connection]:
        """書き込み可能なトランザクション付きコネクション。"""
        with self.engine.begin() as conn:
            yield conn

    @contextmanager
    def readonly(self) -> Iterator[Connection]:
        """読み取り専用コネクション。ビューの生 SQL 実行で使う多重防御。

        SQLite の ``PRAGMA query_only`` はコネクション単位で、コネクションは
        プールに再利用される。終了時に必ず解除し、後続の書き込みを汚染しない。
        """
        with self.engine.connect() as conn:
            if self.url.startswith("sqlite"):
                conn.execute(text("PRAGMA query_only = ON"))
            elif self.url.startswith(("postgresql", "mysql")):
                conn.execute(text("SET TRANSACTION READ ONLY"))
            try:
                yield conn
            finally:
                if self.url.startswith("sqlite"):
                    conn.execute(text("PRAGMA query_only = OFF"))

    def dispose(self) -> None:
        self.engine.dispose()
