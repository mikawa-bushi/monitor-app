# Monitor App 🚀

[![PyPI version](https://img.shields.io/pypi/v/monitor-app.svg)](https://pypi.org/project/monitor-app/)
[![Python versions](https://img.shields.io/pypi/pyversions/monitor-app.svg)](https://pypi.org/project/monitor-app/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://github.com/mikawa-bushi/monitor-app/blob/main/LICENSE)

**Drop in a CSV, write one config file, and get a live-updating Web UI + REST API.**

Monitor App is a FastAPI-based data monitoring tool designed for shop floors, labs, and small teams. Define your tables and views declaratively in a single `config.py` (validated by Pydantic at startup), and Monitor App serves a real-time dashboard, full CRUD REST API with Swagger docs, threshold alerts, and an Andon-style wallboard — no frontend build, no Node.js, fully offline-capable.

日本語のドキュメントは [README_ja.md](https://github.com/mikawa-bushi/monitor-app/blob/main/README_ja.md) を参照してください。

---

## ✨ Features

- **CSV → Web app in 3 commands** — place CSV files, run the server, see your data
- **Type-safe declarative config** — tables, views, styles, alerts in one Pydantic-validated `config.py`; typos are caught at startup with friendly messages (`monitor-app check`)
- **Live-updating views** — Server-Sent Events push changes to the browser; polling fallback
- **Full REST API** — CRUD per table, bulk ingest, automatic Swagger UI at `/docs`
- **SQLite / MySQL / PostgreSQL** — switch with a single `DATABASE_URL` environment variable
- **Conditional cell styling** — color-code values by thresholds, no code required
- **No frontend toolchain** — vanilla JS + bundled assets; works on offline factory networks

### 🏭 Built for the shop floor (v2.1)

| Feature | What it does | How to enable |
|---|---|---|
| Auto data ingest | Watches `csv/` for changes; `POST /api/ingest/{table}` for sensors/PLC/MES | `MONITOR_INGEST_WATCH` / always on |
| Threshold alerts | On-screen banner + browser notification, Webhook (Slack/Teams), LINE, e-mail; edge-triggered | `alerts=[AlertRule(...)]` |
| Andon wallboard | Full-screen rotating views with a green/amber/red status light at `/kiosk` | `kiosk=KioskConfig(...)` |
| Trend / SPC charts | Line & bar charts with UCL/LCL/target lines, out-of-control points highlighted | `ViewDef(chart=ChartDef(...))` |
| Dashboard | Home page shows mini-chart grid (columns=1–4) for chart-enabled views; non-chart views listed below | `dashboard=DashboardConfig(columns=2)` |
| Group navigation | Sidebar and view list support named groups; `ViewDef(group="...")` | `ViewDef(group=...)` |
| Dark theme | Toggle between light and dark; persisted in `localStorage["monitor-theme"]`; `<html data-theme="dark">` | theme-toggle button (☀️/🌙) |
| KPI cards | Scalar SQL (counts, rates, OEE) as color-coded cards on the home page | `kpis={...}` |
| Operator entry forms | Touch-friendly forms generated from your table schema at `/form/{table}` | `TableDef(form=FormDef(...))` |
| Audit log | Who changed what and when, for every write | `MONITOR_AUDIT_ENABLED` |
| Export | Download any view as CSV or Excel | `/api/views/{view}/export` |

---

## 🚀 Installation

```sh
pip install monitor-app
```

With Excel export support:

```sh
pip install "monitor-app[xlsx]"
```

## 🔧 Quick start

```sh
# 1. Create a project (config.py + sample CSVs, ready to run)
monitor-app startproject myproject
cd myproject

# 2. Import CSVs and start the server
monitor-app runserver --import-csv

# 3. Open http://127.0.0.1:9990  (Swagger UI at /docs)
```

### CLI commands

| Command | Description |
|---|---|
| `monitor-app startproject <name>` | Generate a project skeleton |
| `monitor-app runserver [--import-csv] [--reload] [--host H] [--port P]` | Start the web server |
| `monitor-app import-csv [--keep]` | Import `csv/` into the database (`--keep` appends) |
| `monitor-app check` | Validate `config.py` without starting the server |

## 📝 Configuration

Everything lives in one `config.py`. Mistakes are reported at startup, not at runtime:

```python
from monitor_app import (
    AlertRule, CellStyle, ChartDef, FormDef, KioskConfig,
    KpiCard, MonitorConfig, TableDef, Threshold, ViewDef,
)

config = MonitorConfig(
    app_title="Line Monitor",
    refresh_interval_ms=2000,
    # CRUD tables — column types are inferred (or written explicitly)
    tables={
        "measurements": TableDef(
            columns={"id": "int", "ts": "str", "temp": "float"},
            form=FormDef(labels={"temp": "Temperature"}),   # operator entry form
        ),
    },
    # Display views — any SELECT, including JOINs and aggregation
    views={
        "temp_trend": ViewDef(
            query="SELECT ts, temp FROM measurements ORDER BY id",
            title="Temperature trend",
            chart=ChartDef(type="line", x="ts", y="temp", ucl=80, lcl=20),
            styles={"temp": CellStyle(
                greater_than=Threshold(value=80, **{"class": "bg-danger text-white"}),
            )},
        ),
    },
    # Threshold alerts with notifications
    alerts=[
        AlertRule(view="temp_trend", column="temp", op=">", value=80,
                  level="critical", notify=["webhook"]),
    ],
    # KPI cards and Andon wallboard
    kpis={"count": KpiCard(title="Samples", query="SELECT COUNT(*) FROM measurements")},
    kiosk=KioskConfig(rotate_seconds=15, theme="dark"),
)
```

Environment-specific values (never in `config.py`) go in `.env`:

```sh
MONITOR_DATABASE_URL=postgresql+pg8000://user:pass@db:5432/monitor   # default: SQLite
MONITOR_API_KEY=change-me            # enables X-API-Key auth on writes
MONITOR_INGEST_WATCH=true            # auto-import csv/ on change
MONITOR_AUDIT_ENABLED=true           # record change history
MONITOR_WEBHOOK_URL=https://hooks.slack.com/services/...
```

## 🌐 Endpoints

| Path | Description |
|---|---|
| `/` | Home: dashboard with mini-charts, KPI cards, and view list |
| `/table/{view}` | Live table / chart page |
| `/kiosk` | Andon wallboard (full screen, auto-rotating) |
| `/form/{table}` | Operator entry form |
| `/docs`, `/redoc` | OpenAPI documentation |
| `/api/tables/{table}` | CRUD (GET/POST/PUT/DELETE) |
| `/api/ingest/{table}` | Bulk ingest for sensors/PLC |
| `/api/views/{view}` | View data (+ `/stream` for SSE, `/export` for CSV/Excel) |
| `/api/kpis`, `/api/alerts`, `/api/audit`, `/api/schema`, `/api/health` | KPIs, active alerts, audit log, schema, health |

## 🔌 External tool integrations (v2.2)

Connect procdiag, netdiag, and network-checker to Monitor App with a few lines — no changes to the external tools required (read-only, pull-based).

```python
from monitor_app.integrations import procdiag, netdiag, network_checker

config = procdiag.attach(config, db_path="/opt/procdiag/data/procdiag.db")
config = netdiag.attach(config)
config = network_checker.attach(config, hosts=["plc-01", "gateway"])
```

Each `attach()` call merges tables, views, KPIs, and alert rules into your existing config. Run `monitor-app sync-sources` to ingest on demand, or set `MONITOR_INGEST_WATCH=1` for continuous polling.

Full reference: [docs/integrations_v2_2.md](https://github.com/mikawa-bushi/monitor-app/blob/main/docs/integrations_v2_2.md)

---

## 🔄 Migrating from v0.x

v2 was rebuilt on FastAPI + Pydantic. The legacy dict-style `config.py`
(`ALLOWED_TABLES` / `VIEW_TABLES` / `TABLE_CELL_STYLES`) **still loads** through a
compatibility shim, but new projects should use `MonitorConfig`. Key changes:

- `python app.py runserver` → `monitor-app runserver` (the app no longer lives in your project)
- DB settings (`DB_TYPE`, passwords) → `MONITOR_DATABASE_URL` environment variable
- API paths: `/api/<table>` → `/api/tables/{table}`, `/api/view/<v>` → `/api/views/{view}`

Details: [redesign notes](https://github.com/mikawa-bushi/monitor-app/blob/main/docs/redesign_v2.md) ·
[v2.1 feature design](https://github.com/mikawa-bushi/monitor-app/blob/main/docs/feature_expansion_v2.1.md) ·
[v2.2 integrations](https://github.com/mikawa-bushi/monitor-app/blob/main/docs/integrations_v2_2.md) ·
[v2.3 UI redesign](https://github.com/mikawa-bushi/monitor-app/blob/main/docs/ui_redesign_v2_3.md)

## 🐳 Docker

```sh
docker build -t monitor-app -f docker/Dockerfile .
docker run -v $(pwd)/myproject:/project -p 9990:9990 monitor-app
```

## 🛠 Development

```sh
git clone https://github.com/mikawa-bushi/monitor-app.git
cd monitor-app
poetry install
poetry run pytest
```

Contributions are welcome — see [CONTRIBUTING.md](https://github.com/mikawa-bushi/monitor-app/blob/main/CONTRIBUTING.md).

## 📜 License

[MIT](https://github.com/mikawa-bushi/monitor-app/blob/main/LICENSE)
