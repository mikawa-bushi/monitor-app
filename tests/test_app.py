import pytest
from monitor_app.app import app


@pytest.fixture
def client():
    """Flask テストクライアント"""
    app.config["TESTING"] = True
    with app.test_client() as client:
        yield client


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
