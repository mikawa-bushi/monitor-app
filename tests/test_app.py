import pytest
import sys
import tempfile
import os
from pathlib import Path

# Add project root to Python path for imports
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

# Ensure monitor_app directory is in path
monitor_app_dir = project_root / "monitor_app"
sys.path.insert(0, str(monitor_app_dir))

from monitor_app.app import app
from monitor_app.csv_to_db import create_tables


@pytest.fixture
def client():
    """Flask テストクライアント"""
    # Configure test database
    app.config['TESTING'] = True
    app.config['WTF_CSRF_ENABLED'] = False
    
    # Use in-memory SQLite for tests
    test_db_path = tempfile.mktemp(suffix='.db')
    app.config['DATABASE_URL'] = f'sqlite:///{test_db_path}'
    
    with app.test_client() as test_client:
        with app.app_context():
            try:
                # Initialize database for tests
                create_tables()
                yield test_client
            finally:
                # Cleanup test database
                if os.path.exists(test_db_path):
                    os.unlink(test_db_path)


def test_index_page(client):
    """ルートページ `/` が正常に表示されるか"""
    response = client.get("/")
    assert response.status_code == 200
    assert b"Monitor Dashboard" in response.data


def test_table_page(client):
    """許可されたテーブルページが表示されるか"""
    response = client.get("/table/users")
    assert response.status_code == 200
    assert b"users" in response.data
