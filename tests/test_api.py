"""REST API のテスト。"""

import json


class TestSchema:
    def test_schema_lists_tables(self, client):
        data = client.get("/api/schema").json()
        assert set(data["tables"]) == {"users", "products", "orders"}
        assert data["tables"]["orders"]["foreign_keys"]["user_id"] == "users.id"

    def test_health(self, client):
        assert client.get("/api/health").json() == {"status": "ok"}


class TestRead:
    def test_list_users(self, client):
        data = client.get("/api/tables/users").json()
        assert data["count"] == 5
        assert data["columns"] == ["id", "name", "email"]

    def test_get_one(self, client):
        data = client.get("/api/tables/users/1").json()
        assert data["data"]["name"] == "John Doe"

    def test_get_missing_record(self, client):
        assert client.get("/api/tables/users/999").status_code == 404

    def test_missing_table(self, client):
        res = client.get("/api/tables/ghost")
        assert res.status_code == 404
        assert res.json()["detail"]["code"] == "TABLE_NOT_FOUND"


class TestWrite:
    def test_create_filters_unknown_columns(self, client):
        res = client.post(
            "/api/tables/products", json={"name": "Date", "price": "9.5", "junk": "x"}
        )
        assert res.status_code == 201
        body = res.json()["data"]
        assert body["name"] == "Date"
        assert body["price"] == 9.5
        assert "junk" not in body

    def test_create_rejects_empty_payload(self, client):
        assert client.post("/api/tables/users", json={"junk": 1}).status_code == 422

    def test_update(self, client):
        res = client.put("/api/tables/products/1", json={"price": 1234})
        assert res.json()["data"]["price"] == 1234.0

    def test_update_missing(self, client):
        assert (
            client.put("/api/tables/products/999", json={"price": 1}).status_code == 404
        )

    def test_delete(self, client):
        assert client.delete("/api/tables/products/1").status_code == 200
        assert client.get("/api/tables/products/1").status_code == 404

    def test_delete_missing(self, client):
        assert client.delete("/api/tables/products/999").status_code == 404


class TestViews:
    def test_view_returns_joined_data(self, client):
        data = client.get("/api/views/orders_summary").json()
        assert data["columns"] == ["id", "user", "amount"]
        assert len(data["data"]) == 5
        assert data["data"][0]["user"] == "John Doe"

    def test_missing_view(self, client):
        res = client.get("/api/views/ghost")
        assert res.status_code == 404
        assert res.json()["detail"]["code"] == "VIEW_NOT_FOUND"


class TestOpenAPI:
    def test_docs_available(self, client):
        assert client.get("/docs").status_code == 200
        assert client.get("/openapi.json").status_code == 200
