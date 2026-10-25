-- monitor-app 2.3.1 で生成したゴールデン DB(tests/contract/_make_upgrade_fixture.py)。
-- 手で編集しないこと。tests/test_upgrade_v2_3_1.py が後続の版で開けることを検証する。
BEGIN TRANSACTION;
CREATE TABLE _audit (
	id INTEGER NOT NULL,
	ts DATETIME,
	table_name VARCHAR(255),
	record_id VARCHAR(255),
	action VARCHAR(16),
	"before" TEXT,
	"after" TEXT,
	actor VARCHAR(255),
	PRIMARY KEY (id)
);
INSERT INTO "_audit" VALUES(1,'2026-10-21 09:56:48.001620','orders','6','create',NULL,'{"id": 6, "user_id": 2, "product_id": 1, "amount": 3.0}','anon@testclient');
INSERT INTO "_audit" VALUES(2,'2026-10-21 09:56:48.005481','orders','6','update','{"id": 6, "user_id": 2, "product_id": 1, "amount": 3.0}','{"id": 6, "user_id": 2, "product_id": 1, "amount": 4.0}','anon@testclient');
INSERT INTO "_audit" VALUES(3,'2026-10-21 09:56:48.008617','users','6','create',NULL,'{"id": 6, "name": "山田 太郎", "email": "yamada@example.com"}','anon@testclient');
INSERT INTO "_audit" VALUES(4,'2026-10-21 09:56:48.011366','orders','2','delete','{"id": 2, "user_id": 2, "product_id": 3, "amount": 1.0}',NULL,'anon@testclient');
CREATE TABLE _ingest_state (
	source VARCHAR NOT NULL,
	cursor VARCHAR NOT NULL,
	updated_at VARCHAR NOT NULL,
	PRIMARY KEY (source)
);
INSERT INTO "_ingest_state" VALUES('line_events','210','2026-10-21T09:56:47.758363+00:00');
CREATE TABLE line_events (
	id INTEGER NOT NULL,
	ts VARCHAR(255),
	line VARCHAR(255),
	value FLOAT,
	PRIMARY KEY (id)
);
INSERT INTO "line_events" VALUES(1,'2026-10-01T08:00:00','第1ライン',25.5);
INSERT INTO "line_events" VALUES(2,'2026-10-01T08:01:00','第1ライン',26.1);
INSERT INTO "line_events" VALUES(3,'2026-10-01T08:02:00','第2ライン',98.7);
CREATE TABLE orders (
	id INTEGER NOT NULL,
	user_id INTEGER,
	product_id INTEGER,
	amount FLOAT,
	PRIMARY KEY (id),
	FOREIGN KEY(user_id) REFERENCES users (id),
	FOREIGN KEY(product_id) REFERENCES products (id)
);
INSERT INTO "orders" VALUES(1,1,1,2.0);
INSERT INTO "orders" VALUES(3,1,2,5.0);
INSERT INTO "orders" VALUES(4,3,1,12.0);
INSERT INTO "orders" VALUES(5,2,2,4.0);
INSERT INTO "orders" VALUES(6,2,1,4.0);
CREATE TABLE products (
	id INTEGER NOT NULL,
	name VARCHAR(255),
	price FLOAT,
	PRIMARY KEY (id)
);
INSERT INTO "products" VALUES(1,'Apple',10000.0);
INSERT INTO "products" VALUES(2,'Banana',50.0);
INSERT INTO "products" VALUES(3,'Cherry',800.0);
CREATE TABLE users (
	id INTEGER NOT NULL,
	name VARCHAR(255),
	email VARCHAR(255),
	PRIMARY KEY (id)
);
INSERT INTO "users" VALUES(1,'John Doe','john@example.com');
INSERT INTO "users" VALUES(2,'Jane Smith','jane@example.com');
INSERT INTO "users" VALUES(3,'Robert Brown','robert@example.com');
INSERT INTO "users" VALUES(4,'Emily Davis','emily@example.com');
INSERT INTO "users" VALUES(5,'Michael Wilson','michael@example.com');
INSERT INTO "users" VALUES(6,'山田 太郎','yamada@example.com');
CREATE INDEX ix_line_events_ts ON line_events (ts);
CREATE INDEX ix_orders_user_id ON orders (user_id);
CREATE INDEX ix_orders_product_id ON orders (product_id);
COMMIT;
