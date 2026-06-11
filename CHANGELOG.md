# Changelog

## [2.1.0] - 2026-06-10

Complete rewrite on **FastAPI + Pydantic** (from Flask), plus manufacturing-focused features.

### Added (shop-floor features)
- **Auto data ingest** — `csv/` folder watching (`MONITOR_INGEST_WATCH`) and bulk
  `POST /api/ingest/{table}` for sensors / PLC / MES
- **Threshold alerts** — declarative `AlertRule`s, edge-triggered notifications
  (console / Webhook / LINE / e-mail), on-screen banner with sound, `/api/alerts`
- **Andon wallboard** — `/kiosk` full-screen rotating views with green/amber/red status light
- **Trend / SPC charts** — `ChartDef` with UCL/LCL/target lines (bundled Chart.js, offline-capable)
- **KPI cards** — scalar-SQL `KpiCard`s with target comparison, `/api/kpis`
- **Operator entry forms** — touch-friendly forms generated from table schema at `/form/{table}`
- **Audit log** — before/after change history with actor (`MONITOR_AUDIT_ENABLED`), `/api/audit`
- **Export** — `/api/views/{view}/export?format=csv|xlsx` (`pip install monitor-app[xlsx]`)

### Changed (v2 architecture)
- Flask → **FastAPI**; flasgger's hand-written Swagger YAML → automatic OpenAPI (`/docs`, `/redoc`)
- dict-based config → **type-safe `MonitorConfig`** (Pydantic). Typos are caught at startup;
  `monitor-app check` validates without starting the server. The legacy v1 dict format
  (`ALLOWED_TABLES` etc.) still loads via a compatibility shim
- Raw f-string SQL → **SQLAlchemy 2.0 Core expression builder** (structural injection safety)
- pandas CSV import → stdlib `csv` with config-driven schema (no more schema clobbering;
  pandas dependency removed)
- `startproject` no longer copies application source — only `config.py`, `.env.example`
  and sample CSVs; the app updates via `pip install -U monitor-app`
- DB settings (`DB_TYPE`, credentials) → `MONITOR_DATABASE_URL` environment variable / `.env`
- API paths: `/api/<table>` → `/api/tables/{table}`, `/api/view/<v>` → `/api/views/{view}`,
  schema list → `/api/schema`
- Polling-only refresh → **Server-Sent Events** with polling fallback
- Vue (CDN) frontend → vanilla JS + bundled assets; modern minimal UI
- click CLI → **Typer** (`runserver`, `import-csv`, `check`, `startproject`)
- Optional API-key auth (`MONITOR_API_KEY`, `X-API-Key` header) for write endpoints;
  CORS now defaults to same-origin (configurable)

### Removed
- Flask, flask-sqlalchemy, flask-cors, flasgger, pandas, toml dependencies
- jQuery / DataTables assets

## [0.1.9] - earlier
- Final Flask-based release.
