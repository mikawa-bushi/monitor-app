"""ビュー性能(#17 ページング / #18 共有キャッシュ / #19 バッチ)の回帰テスト。

conftest.py の ``client`` フィクスチャ(users 5 件 + users_view / orders_summary)を使う。
"""

from __future__ import annotations


# --- #18 V2: 書き込みで共有キャッシュが無効化され鮮度が保たれる ---------------
def test_view_cache_invalidated_on_write(client):
    before = client.get("/api/views/users_view").json()
    n0 = len(before["data"])

    res = client.post("/api/tables/users", json={"name": "Zoe", "email": "z@e.com"})
    assert res.status_code == 201

    after = client.get("/api/views/users_view").json()
    assert len(after["data"]) == n0 + 1  # キャッシュ無効化されず古いままなら n0


# --- #19 V3: 複数ビューを 1 リクエストでまとめて返す -------------------------
def test_batch_returns_multiple_views(client):
    data = client.get(
        "/api/views/batch?names=users_view,orders_summary,ghost"
    ).json()
    # 存在しない 'ghost' は黙って除外される
    assert set(data["views"].keys()) == {"users_view", "orders_summary"}
    assert data["views"]["users_view"]["data"]


# --- #17 V1: ビューの limit/offset --------------------------------------------
def test_view_limit_offset(client):
    full = client.get("/api/views/users_view").json()["data"]
    assert len(full) >= 3
    page = client.get("/api/views/users_view?limit=2&offset=1").json()["data"]
    assert [r["id"] for r in page] == [r["id"] for r in full[1:3]]


# --- #17 V1: テーブルの limit/offset ------------------------------------------
def test_table_limit_offset(client):
    full = client.get("/api/tables/users").json()
    assert full["count"] == 5
    page = client.get("/api/tables/users?limit=2").json()
    assert page["count"] == 2
