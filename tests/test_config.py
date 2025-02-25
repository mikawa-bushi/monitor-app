import os
import pytest
from monitor_app.config.config import (
    SQLALCHEMY_DATABASE_URI,
    ALLOWED_TABLES,
    APP_TITLE,
    HEADER_TEXT,
    FOOTER_TEXT,
)


def test_database_uri():
    """データベースの URI が設定されているか"""
    assert SQLALCHEMY_DATABASE_URI is not None
    assert SQLALCHEMY_DATABASE_URI.startswith(
        ("sqlite:///", "mysql://", "postgresql://")
    )


def test_allowed_tables():
    """許可されたテーブルが設定されているか"""
    assert isinstance(ALLOWED_TABLES, dict)
    assert "users" in ALLOWED_TABLES
    assert "orders" in ALLOWED_TABLES
    assert "products" in ALLOWED_TABLES


def test_app_metadata():
    """アプリのタイトル、ヘッダー、フッターが設定されているか"""
    assert APP_TITLE == "Monitor App"
    assert isinstance(HEADER_TEXT, str)
    assert isinstance(FOOTER_TEXT, str)
