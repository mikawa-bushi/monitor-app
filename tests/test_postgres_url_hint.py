"""``postgres://`` の接続 URL の案内が、インストール済みのドライバで動く書き方を示すこと。

psycopg は追加のインストール(monitor-app[postgres])が要る。入っていないのに
``postgresql+psycopg://`` を勧めると、書き換えた後に「ドライバ psycopg が
インストールされていません」でもう一度止まる。
"""

from __future__ import annotations

import importlib.util

from sqlalchemy.exc import NoSuchModuleError

from monitor_app.cli._common import explain_error


def _hint() -> str:
    explained = explain_error(
        NoSuchModuleError("Can't load plugin: sqlalchemy.dialects:postgres")
    )
    assert explained is not None
    return explained[1] or ""


def test_without_psycopg_the_bundled_pg8000_is_suggested(monkeypatch):
    real = importlib.util.find_spec

    def find_spec(name, *args, **kwargs):
        return None if name == "psycopg" else real(name, *args, **kwargs)

    monkeypatch.setattr(importlib.util, "find_spec", find_spec)
    hint = _hint()
    assert "postgresql+pg8000://(同梱のドライバ)に書き換えます" in hint
    assert 'pip install "monitor-app[postgres]"' in hint


def test_with_psycopg_it_is_suggested(monkeypatch):
    real = importlib.util.find_spec

    def find_spec(name, *args, **kwargs):
        if name == "psycopg":
            return object()
        return real(name, *args, **kwargs)

    monkeypatch.setattr(importlib.util, "find_spec", find_spec)
    assert "postgresql+psycopg:// に書き換えます" in _hint()
