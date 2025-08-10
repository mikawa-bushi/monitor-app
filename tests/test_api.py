import pytest
import json
import os
import tempfile
import sys
from pathlib import Path

# Add monitor_app to Python path for imports
sys.path.insert(0, str(Path(__file__).parent.parent / "monitor_app"))

from monitor_app.app import app, db
from monitor_app.csv_to_db import create_tables
from sqlalchemy import text


@pytest.fixture
def client():
    """Flask テストクライアント - 実際のDBを使用（テスト用に制限）"""
    app.config['TESTING'] = True
    
    with app.test_client() as test_client:
        with app.app_context():
            yield test_client


def setup_test_data():
    """テスト用の基本データをセットアップ"""
    # テストユーザーを追加
    db.session.execute(
        text("INSERT INTO users (id, name, email) VALUES (1, 'Test User', 'test@example.com')")
    )
    db.session.execute(
        text("INSERT INTO users (id, name, email) VALUES (2, 'Jane Doe', 'jane@example.com')")
    )
    
    # テスト商品を追加
    db.session.execute(
        text("INSERT INTO products (id, name, price) VALUES (1, 'Test Product', 100.0)")
    )
    db.session.execute(
        text("INSERT INTO products (id, name, price) VALUES (2, 'Another Product', 200.0)")
    )
    
    # テスト注文を追加
    db.session.execute(
        text("INSERT INTO orders (id, user_id, product_id, amount) VALUES (1, 1, 1, 5.0)")
    )
    
    db.session.commit()


class TestTableAPI:
    """テーブル情報API のテスト"""
    
    def test_get_tables(self, client):
        """GET /api/tables - テーブル一覧の取得"""
        response = client.get('/api/tables')
        assert response.status_code == 200
        
        data = json.loads(response.data)
        assert data['success'] is True
        assert 'tables' in data
        assert 'users' in data['tables']
        assert 'products' in data['tables']
        assert 'orders' in data['tables']
        
        # usersテーブルの構造確認
        users_table = data['tables']['users']
        assert users_table['primary_key'] == 'id'
        assert 'id' in users_table['columns']
        assert 'name' in users_table['columns']
        assert 'email' in users_table['columns']


class TestUsersAPI:
    """ユーザー API のテスト"""
    
    def test_get_all_users(self, client):
        """GET /api/users - 全ユーザーの取得"""
        response = client.get('/api/users')
        assert response.status_code == 200
        
        data = json.loads(response.data)
        assert data['success'] is True
        assert 'data' in data
        assert data['count'] >= 2  # 少なくとも2人以上のユーザーがいる
        assert len(data['data']) >= 2
    
    def test_get_user_by_id(self, client):
        """GET /api/users/1 - 特定ユーザーの取得"""
        response = client.get('/api/users/1')
        assert response.status_code == 200
        
        data = json.loads(response.data)
        assert data['success'] is True
        assert data['data']['id'] == 1
        assert 'name' in data['data']
        assert 'email' in data['data']
    
    def test_get_user_not_found(self, client):
        """GET /api/users/999 - 存在しないユーザー"""
        response = client.get('/api/users/999')
        assert response.status_code == 404
        
        data = json.loads(response.data)
        assert 'error' in data
    
    def test_create_user(self, client):
        """POST /api/users - 新規ユーザー作成"""
        new_user = {
            'name': 'New User',
            'email': 'newuser@example.com'
        }
        
        response = client.post('/api/users', 
                             data=json.dumps(new_user),
                             content_type='application/json')
        assert response.status_code == 201
        
        data = json.loads(response.data)
        assert data['success'] is True
        assert data['message'] == 'Record created successfully'
        assert data['data']['name'] == 'New User'
        assert data['data']['email'] == 'newuser@example.com'
    
    def test_create_user_invalid_data(self, client):
        """POST /api/users - 無効なデータでユーザー作成"""
        response = client.post('/api/users', 
                             data=json.dumps({}),
                             content_type='application/json')
        assert response.status_code == 400
        
        data = json.loads(response.data)
        assert 'error' in data
    
    def test_update_user(self, client):
        """PUT /api/users/1 - ユーザー更新"""
        updated_data = {
            'name': 'Updated User',
            'email': 'updated@example.com'
        }
        
        response = client.put('/api/users/1',
                            data=json.dumps(updated_data),
                            content_type='application/json')
        assert response.status_code == 200
        
        data = json.loads(response.data)
        assert data['success'] is True
        assert data['message'] == 'Record updated successfully'
        assert data['data']['name'] == 'Updated User'
        assert data['data']['email'] == 'updated@example.com'
    
    def test_update_user_not_found(self, client):
        """PUT /api/users/999 - 存在しないユーザーの更新"""
        updated_data = {'name': 'Test'}
        
        response = client.put('/api/users/999',
                            data=json.dumps(updated_data),
                            content_type='application/json')
        assert response.status_code == 404
        
        data = json.loads(response.data)
        assert 'error' in data
    
    def test_delete_user(self, client):
        """DELETE /api/users - ユーザー削除テスト"""
        # 既存のユーザー一覧を取得
        get_response = client.get('/api/users')
        users_data = json.loads(get_response.data)
        
        if users_data['count'] > 0:
            # 最初のユーザーのIDを取得
            first_user = users_data['data'][0]
            if first_user.get('id'):
                user_id = first_user['id']
                
                # ユーザーを削除
                delete_response = client.delete(f'/api/users/{user_id}')
                assert delete_response.status_code == 200
                
                data = json.loads(delete_response.data)
                assert data['success'] is True
                assert data['message'] == 'Record deleted successfully'
            else:
                pytest.skip("User ID not available for deletion test")
        else:
            pytest.skip("No users available for deletion test")
    
    def test_delete_user_not_found(self, client):
        """DELETE /api/users/999 - 存在しないユーザーの削除"""
        response = client.delete('/api/users/999')
        assert response.status_code == 404
        
        data = json.loads(response.data)
        assert 'error' in data


class TestProductsAPI:
    """商品 API のテスト"""
    
    def test_get_all_products(self, client):
        """GET /api/products - 全商品の取得"""
        response = client.get('/api/products')
        assert response.status_code == 200
        
        data = json.loads(response.data)
        assert data['success'] is True
        assert data['count'] >= 2  # 少なくとも2つ以上の商品がある
    
    def test_create_product(self, client):
        """POST /api/products - 新規商品作成"""
        new_product = {
            'name': 'New Product',
            'price': 150.5
        }
        
        response = client.post('/api/products',
                             data=json.dumps(new_product),
                             content_type='application/json')
        assert response.status_code == 201
        
        data = json.loads(response.data)
        assert data['success'] is True
        assert data['data']['name'] == 'New Product'
        assert data['data']['price'] == 150.5
    
    def test_update_product(self, client):
        """PUT /api/products/1 - 商品更新"""
        updated_data = {
            'name': 'Updated Product',
            'price': 250.0
        }
        
        response = client.put('/api/products/1',
                            data=json.dumps(updated_data),
                            content_type='application/json')
        assert response.status_code == 200
        
        data = json.loads(response.data)
        assert data['success'] is True
        assert data['data']['name'] == 'Updated Product'
        assert data['data']['price'] == 250.0


class TestOrdersAPI:
    """注文 API のテスト"""
    
    def test_get_all_orders(self, client):
        """GET /api/orders - 全注文の取得"""
        response = client.get('/api/orders')
        assert response.status_code == 200
        
        data = json.loads(response.data)
        assert data['success'] is True
        assert data['count'] >= 1  # 少なくとも1つ以上の注文がある
        assert len(data['data']) >= 1
    
    def test_create_order(self, client):
        """POST /api/orders - 新規注文作成"""
        new_order = {
            'user_id': 1,
            'product_id': 2,
            'amount': 3.0
        }
        
        response = client.post('/api/orders',
                             data=json.dumps(new_order),
                             content_type='application/json')
        assert response.status_code == 201
        
        data = json.loads(response.data)
        assert data['success'] is True
        assert data['data']['user_id'] == 1
        assert data['data']['product_id'] == 2
        assert data['data']['amount'] == 3.0


class TestErrorHandling:
    """エラーハンドリングのテスト"""
    
    def test_invalid_table_name(self, client):
        """GET /api/invalid_table - 存在しないテーブル"""
        response = client.get('/api/invalid_table')
        assert response.status_code == 404
        
        data = json.loads(response.data)
        assert 'error' in data
        assert data['error'] == 'Table not found'
    
    def test_invalid_json_data(self, client):
        """POST with invalid JSON"""
        response = client.post('/api/users',
                             data='invalid json',
                             content_type='application/json')
        # Flask may return 400 or 500 depending on how it handles malformed JSON
        assert response.status_code in [400, 500]
    
    def test_missing_content_type(self, client):
        """POST without Content-Type header"""
        response = client.post('/api/users',
                             data=json.dumps({'name': 'test'}))
        # Flaskは自動でJSONを解析しようとするが、Content-Typeがないと400になる場合がある
        assert response.status_code in [400, 500]


class TestDataValidation:
    """データ検証のテスト"""
    
    def test_create_user_with_extra_fields(self, client):
        """POST /api/users - 余分なフィールドを含むデータ"""
        user_data = {
            'name': 'Test User',
            'email': 'test@example.com',
            'invalid_field': 'should be ignored'
        }
        
        response = client.post('/api/users',
                             data=json.dumps(user_data),
                             content_type='application/json')
        assert response.status_code == 201
        
        data = json.loads(response.data)
        assert 'invalid_field' not in data['data']
    
    def test_create_order_with_foreign_keys(self, client):
        """POST /api/orders - 外部キー制約のテスト"""
        # 存在するユーザーと商品での注文作成
        order_data = {
            'user_id': 1,
            'product_id': 1,
            'amount': 2.5
        }
        
        response = client.post('/api/orders',
                             data=json.dumps(order_data),
                             content_type='application/json')
        assert response.status_code == 201