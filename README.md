# Monitor App 🚀

[![PyPI version](https://img.shields.io/pypi/v/monitor-app.svg)](https://pypi.org/project/monitor-app/)
[![Python versions](https://img.shields.io/pypi/pyversions/monitor-app.svg)](https://pypi.org/project/monitor-app/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://github.com/mikawa-bushi/monitor-app/blob/main/LICENSE)

**Drop in a CSV, write one config file, and get a live-updating Web UI + REST API.**

Monitor App is a data monitoring tool for shop floors, labs and small teams. Point it at
a folder of CSV files and you get a dashboard in a minute. When you need more, describe
your tables, views, alerts and KPI cards in one `config.py` and Monitor App serves a
live dashboard, an Andon wallboard, operator entry forms and a REST API with Swagger
docs. It runs on a single PC or Raspberry Pi with SQLite, needs no frontend build and
works on offline factory networks.

日本語のドキュメントは [README_ja.md](https://github.com/mikawa-bushi/monitor-app/blob/main/README_ja.md) を参照してください。

> The command-line messages and the built-in UI labels are in Japanese. Titles, column
> labels and alert messages you write in `config.py` are shown as you write them, and
> the REST API is language-neutral JSON.

---

## 👀 See it first: a warehouse inventory demo

Open the inventory screen of a fictional distribution center, with nothing to prepare:

```sh
uvx monitor-app demo --open                                      # if installed: monitor-app demo --open
docker run --rm -p 9990:9990 ghcr.io/mikawa-bushi/monitor-app    # with Docker (mount nothing)
```

Open http://127.0.0.1:9990 (with Docker, http://localhost:9990). What to look at:

- **Stock list**: location, lot, best-before date, days left, cases and pieces. Stock is
  the SQL sum of a ledger of stock movements.
- **It moves on its own**: every 4 seconds a receipt, shipment or move happens, and the
  movement history, KPI cards and charts update by themselves.
- **Alerts**: right after start, banners show an item below safety stock and lots within
  2 days of their best-before date. About 90 seconds later a delivery arrives and the
  safety-stock banner clears.
- **Other screens**: `/kiosk` (wallboard) and `/form/movements` (entry form). When the
  demo runs on your own PC, `/settings` (edit settings in the browser) works too.

The data lives in a temporary database that is deleted on exit. At start the demo
prints where its `config.py` is. Docker shows times in UTC; add `-e TZ=Asia/Tokyo` (or
your zone) for local time.

To start your own project with the same configuration and fictional data:

```sh
monitor-app startproject my-warehouse --template warehouse
cd my-warehouse
monitor-app runserver --import-csv-if-empty
```

In a project, movements do not happen on their own. The project's `README.md` explains
how to replace the data with your own.

## ✨ What's new in 2.5

- **A demo with nothing to prepare.** `monitor-app demo` opens the inventory screen of a
  fictional distribution center. Every 4 seconds a receipt, shipment or move happens, so
  the KPI cards, charts and alerts change on their own. The data lives in a temporary
  SQLite database that is deleted on exit. Docker started with nothing mounted now runs
  this demo instead of the sample project. Mount an empty folder to get the sample
  project as before.
- **Start a project from the demo.** `monitor-app startproject NAME --template warehouse`
  writes the demo's `config.py`, master data and 7 days of fictional data.
- **Settings page.** `/settings` edits the display options, views (SQL, column labels,
  cell styles, charts), KPI cards, alerts, the wallboard and the dashboard without
  editing `config.py`. A save runs the same checks as `monitor-app check`, then applies
  at once without a restart. Only the difference from `config.py` is stored, in the
  database.
- **CSV upload.** `/upload` imports a CSV file into a table from the browser, with the
  same code as `monitor-app import-csv`. Choose append or replace.
- **`MONITOR_ADMIN_KEY`.** A key for the settings page and CSV upload only. When it is
  set, operators who log in with `MONITOR_API_KEY` cannot change the settings. Without
  it, `MONITOR_API_KEY` works, and with neither key the admin features work only from
  this PC.
- **Named entry forms.** `FormDef(title="...")` sets the form's heading, its browser tab
  title and its link on the home page.
- **Security.** The admin features refuse requests sent from pages on other sites
  (CSRF). SQL saved from the browser must be one statement with no `;`, and a KPI number
  format can only format the value. The name of an uploaded file loses its folder and
  drive parts on every OS, so it cannot point outside the temporary folder.

Full list: [CHANGELOG.md](https://github.com/mikawa-bushi/monitor-app/blob/main/CHANGELOG.md).
Coming from 2.4? Nothing needs changing beyond the **Upgrade notes** in the CHANGELOG.
Coming from 2.3? Read **Upgrading from 2.3.x** below.

## Features

| Feature | What it does | How |
|---|---|---|
| CSV dashboard without a config file | Types, keys, charts and KPI cards are inferred from the data | `monitor-app serve FOLDER` |
| One declarative config file | Tables, views (any SQL `SELECT`), cell styles, alerts, KPIs, forms; validated at startup, typos reported with the line number | `config.py` + `monitor-app check` |
| Live updates | Server-Sent Events push changes to the browser; polling fallback | default (`refresh_mode="sse"` or `"polling"`) |
| Dashboard | Home page with mini charts (1–4 columns), KPI cards and a grouped view list; light and dark theme | `dashboard=DashboardConfig(...)` |
| Andon wallboard | Full-screen rotating views with a green/amber/red status light | `/kiosk`, `kiosk=KioskConfig(...)` |
| Trend / SPC charts | Line and bar charts with UCL / LCL / target lines | `ViewDef(chart=ChartDef(...))` |
| Threshold alerts | Banner, browser notification, webhook (Slack, Teams), e-mail; notified once per change, retried on failure | `alerts=[AlertRule(...)]` |
| Operator entry forms | Touch-friendly forms generated from the table definition | `TableDef(form=FormDef(...))`, `/form/{table}` |
| Settings and CSV upload in the browser | Edit display options, views (SQL), cell styles, charts, KPIs, alerts and the wallboard in the browser; applied without a restart, and open pages reload themselves. Upload CSV files into tables | `/settings`, `/upload` |
| Data input | CSV import (UTF-8 or Excel cp932), folder watching (replace or append), bulk `POST` for PLC/MES/sensors, read-only `sources` (SQLite/JSONL/JSON) | `import-csv`, `MONITOR_INGEST_WATCH`, `/api/ingest/{table}` |
| REST API | CRUD per table, view data, CSV/Excel export, OpenAPI docs | `/api/...`, `/docs`, `/redoc` |
| Python API | Build the app from your own code, load and validate `config.py` | `create_app`, `load_config` (see **Python API**) |
| Data safety | SQLite WAL, online backup and restore, retention, schema checks and `migrate`, audit log | `monitor-app backup`, `MONITOR_AUDIT_ENABLED` |
| Databases | SQLite (default, nothing to install), PostgreSQL, MySQL | `MONITOR_DATABASE_URL` |
| Deployment | Docker image, Compose/systemd/Windows start-up files, sub-path behind a reverse proxy, health endpoint | `monitor-app deploy`, `/api/health` |
| Offline use | No Node.js and no build step. Bootstrap and Chart.js are bundled, so the dashboard needs no internet. Only the API documentation pages `/docs` and `/redoc` load Swagger UI / ReDoc (pinned versions) from a CDN; `MONITOR_API_DOCS=false` turns them off. | — |

---

## 🚀 Installation

Monitor App needs **Python 3.11 or newer**. Pick one of these ways:

```sh
pipx install monitor-app         # with pipx
uv tool install monitor-app      # or with uv (it downloads Python for you if needed)
monitor-app --version
```

Both install the `monitor-app` command into its own environment, so it cannot conflict
with other Python software. Upgrade later with `pipx upgrade monitor-app` or
`uv tool upgrade monitor-app`.

**Try it without installing** ([uv](https://docs.astral.sh/uv/) keeps everything in a cache):

```sh
uvx monitor-app serve ./my-csv-folder
```

**pip in a virtual environment** also works:

```sh
python3 -m venv .venv
. .venv/bin/activate             # Windows: .venv\Scripts\activate
pip install monitor-app
```

`python -m monitor_app <command>` is the same as `monitor-app <command>` (handy when the
command is not on your `PATH`).

### Windows

The easiest way is uv. It needs no administrator rights and installs a suitable Python
itself. In PowerShell:

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
# open a new PowerShell window, then:
uv tool install monitor-app
monitor-app --version
```

If you already use Python from python.org: `py -m pip install monitor-app`, then run
`py -m monitor_app runserver` (this works even when Python's `Scripts` folder is not
on your `PATH`).

### Debian 12+, Ubuntu 23.04+, Raspberry Pi OS

`pip install monitor-app` outside a virtual environment stops with
`error: externally-managed-environment` (PEP 668). Use pipx or uv instead:

```sh
sudo apt install pipx
pipx ensurepath                  # then open a new terminal
pipx install monitor-app
```

or install uv with `curl -LsSf https://astral.sh/uv/install.sh | sh` and run
`uv tool install monitor-app`.

**64-bit Raspberry Pi OS** works like any Linux PC (and the Docker image supports it).
**32-bit Raspberry Pi OS** has no prebuilt packages on PyPI for some dependencies
(uvloop, httptools, PyYAML). Raspberry Pi OS already configures pip to use
[piwheels](https://www.piwheels.org/), which has them, so `pipx install monitor-app`
works. If pip starts compiling a package, add `--pip-args=--prefer-binary`. uv does not
read pip's settings: set `UV_EXTRA_INDEX_URL=https://www.piwheels.org/simple` for uv.
The `[postgres]` extra is not available on 32-bit ARM. Use the built-in pg8000 driver
(`postgresql+pg8000://...`). There is no 32-bit Docker image.

### Optional extras

| Extra | Adds | Needed for |
|---|---|---|
| `xlsx` | openpyxl | Excel export: `/api/views/{view}/export?format=xlsx` |
| `postgres` | psycopg (binary) | the fast PostgreSQL driver `postgresql+psycopg://` (pg8000 and PyMySQL are always included) |

```sh
pipx install "monitor-app[xlsx,postgres]"       # pipx: add --force if already installed
uv tool install "monitor-app[xlsx,postgres]"    # uv: running it again adds the extras
```

### Offline / air-gapped PCs

Download the packages on a PC with internet access and copy them over. Set the target
PC's Python version and platform (here Python 3.12 on 64-bit Linux):

```sh
# On the PC with internet access (needs uv and pip):
echo "monitor-app[xlsx]" > requirements.in
uv pip compile requirements.in -o requirements.txt --only-binary :all: \
    --python-version 3.12 --python-platform x86_64-manylinux_2_28
python3 -m pip download --no-deps --only-binary=:all: -d wheelhouse -r requirements.txt \
    --python-version 3.12 --platform manylinux_2_28_x86_64 \
    --platform manylinux_2_17_x86_64 --platform manylinux2014_x86_64
# Copy requirements.txt and the wheelhouse folder (about 20 MB) to the target PC, then there:
python3 -m venv ~/monitor-app
~/monitor-app/bin/pip install --no-index --find-links wheelhouse "monitor-app[xlsx]"
~/monitor-app/bin/monitor-app --version
```

Other targets: 64-bit Raspberry Pi uses `--python-platform aarch64-manylinux_2_28` and
the same three `--platform` values with `aarch64` instead of `x86_64`. 64-bit Windows uses
`--python-platform x86_64-pc-windows-msvc` and `--platform win_amd64`, and on the target
`py -m venv monitor-app` and `monitor-app\Scripts\pip install ...`. `--only-binary :all:`
makes uv choose only versions that have a prebuilt package for the target. If
`pip download` still reports "No matching distribution" for a package, add a line to
`requirements.in` that stays below that version (for example `sqlalchemy<2.1.6` when
`sqlalchemy==2.1.6` failed) and run both commands again.

---

## 🔧 Quick start A: a folder of CSV files, no configuration (1 minute)

```sh
uvx monitor-app serve ./my-csv-folder     # or a single file: serve ./data/temperature.csv
```

Open http://127.0.0.1:9990 and press Ctrl+C to stop. (If you installed Monitor App,
leave out `uvx`.) Every CSV file becomes a table, a view and a row-count KPI card, plus a
line chart when the file has numeric columns. Monitor App infers:

- **The encoding of each file**: UTF-8 (with or without BOM) or Excel's
  「CSV(コンマ区切り)」 format (cp932). Japanese headers such as `温度(℃)` work.
- **Column types from the data**: whole numbers, decimals, `TRUE/FALSE/yes/no` (bool),
  dates and date-times (`2026-10-21 08:00`, `2026/10/21 8:00`). Codes with leading zeros
  such as `0012` stay text.
- **The primary key**: `id` when it is a unique whole number; otherwise an `_row_id`
  column is added.
- **Charts**: x = the first date/time column (else the row number), y = up to four numeric
  columns. Views of big files show the newest 5,000 rows; the home page shows the first 6
  charts.

The data goes into a temporary database that is deleted when you stop (`--db data.db`
keeps it). Changed CSV files are imported again automatically; `--append` imports only
the rows appended to the files (logs, measurements), `--no-watch` imports once. It
listens on this PC only; with `--host 0.0.0.0` other PCs can connect, so also set
`MONITOR_API_KEY` (see **Settings** below).

**Keep what it inferred** and continue with a normal project:

```sh
monitor-app startproject myproject --from-csv ./my-csv-folder   # config.py + a copy of your CSVs
cd myproject
monitor-app runserver --import-csv-if-empty
```

(`serve ./my-csv-folder --write-config config.py` instead writes only `config.py`, without
copying the CSV files, and then serves as usual. To run that file later, set
`MONITOR_CSV_DIR` to the CSV folder in `.env` and start
`monitor-app runserver --config config.py --import-csv-if-empty`, as the command prints.)

## 🔧 Quick start B: a project

```sh
monitor-app startproject myproject        # config.py, sample CSV files, .env.example, README
cd myproject
monitor-app runserver --import-csv-if-empty
```

Open http://127.0.0.1:9990 (API docs at http://127.0.0.1:9990/docs). After editing
`config.py`, restart the server. CSV files are imported only into tables that are still
empty (the first start), so after changing the files in `csv/`, load them with
`monitor-app import-csv` (see below).

**Which import option?** The CSV files fill the tables, but the tables can also get rows
from the entry forms, the UI and the API. Choose what should happen to those rows:

| Command | Rows entered in the UI, forms or API | Use it when |
|---|---|---|
| `runserver --import-csv-if-empty` | **kept**: CSV files are imported only into tables that have no rows yet (the first start) | recommended for everyday use |
| `runserver --import-csv` | **replaced on every start**: each table becomes the content of its CSV file | the CSV files are the master data |
| `import-csv` | replaced once, now | you updated a CSV file on purpose |
| `import-csv --keep` | kept; CSV rows whose primary key already exists are skipped | you added rows to a CSV file |
| `import-csv --if-empty` | kept (same as `--import-csv-if-empty`) | scripts |

Two safety rules: an import runs in one transaction, so a failure changes nothing; and a
replacing import leaves a table as it is when its CSV header lacks a column declared in
`config.py` (other than the primary key). The other tables are still imported, and
`import-csv` then exits with code 1. `--force` replaces anyway, and that column becomes
empty.
Note that `--keep` adds back rows you deleted in the UI if the CSV file still contains them.

## 🧰 CLI reference

| Command | What it does | Main options |
|---|---|---|
| `monitor-app demo` | Warehouse inventory demo, nothing to prepare (fictional data, deleted on exit) | `--host`, `--port`, `--open` |
| `monitor-app serve [PATH...]` | Dashboard straight from CSV files, no `config.py` | `--host`, `--port`, `--db FILE`, `--append`, `--no-watch`, `--write-config FILE`, `--open` |
| `monitor-app startproject NAME` | Create a project folder | `--template warehouse` (the demo's configuration and data), `--from-csv DIR`, `--deploy docker,systemd,windows` (or `all`) |
| `monitor-app runserver` | Start the web server | `--import-csv-if-empty`, `--import-csv`, `--force`, `--host`, `--port`, `--reload`, `--config` |
| `monitor-app check` | Check `config.py`, SQL, settings and the database tables without starting | `--data` (also the CSV files), `--strict` (warnings fail, for CI), `--fix-schema`, `--config` |
| `monitor-app import-csv` | Import `csv/` into the database (replace) | `--keep`, `--if-empty`, `--force`, `--config` |
| `monitor-app sync-sources` | Import all `sources` once (for cron) | `--config` |
| `monitor-app migrate` | Add columns you added to `config.py` to existing tables (and repair PostgreSQL id sequences left behind by 2.3) | `--dry-run`, `--config` |
| `monitor-app backup` | Consistent SQLite backup, also while the server runs | `--dest DIR`, `--keep N`, `--config` |
| `monitor-app restore FILE` | Restore a backup (stop the server first) | `--yes` / `-y`, `--config` |
| `monitor-app deploy [TARGET...]` | Write start-up files: `docker`, `systemd`, `windows`, `all`; generates an API key into `.env` | `--project DIR`, `--port N`, `--config FILE`, `--force` |

- `monitor-app --version` (`-V`), and `monitor-app --debug <command>` (or `MONITOR_DEBUG=1`)
  for a full traceback. Errors are otherwise reported in at most three lines on stderr.
- `--config` takes the path to `config.py` or the project folder (`~` works too). Run from
  another folder, the command also reads that project's `.env`. Relative paths in the
  settings (`instances/`, `csv/`, `backups/`) still count from the folder you are in. If
  the database (or, when importing, the CSV folder) exists only in the project's folder,
  the command stops with a hint to `cd` there instead of creating an empty database.
- `monitor-app <command> --help` lists every option. Exit codes: 0 success, 1 error, 2
  wrong usage, 143 when a one-off command such as `import-csv` was stopped by SIGTERM
  (`docker stop`, `systemctl stop`).

## 📝 Configuration (`config.py`)

`startproject` writes a commented `config.py`. A realistic one for a production line:

```python
from monitor_app import (
    AlertRule, CellStyle, ChartDef, DashboardConfig, FormDef, KioskConfig,
    KpiCard, MonitorConfig, Retention, TableDef, Threshold, ViewDef,
)

config = MonitorConfig(
    app_title="Line 1 Monitor",
    refresh_interval_ms=2000,
    tables={
        # One table per CSV file: csv/readings.csv -> table "readings".
        "readings": TableDef(
            # Declare every column type: "int" "float" "str" "bool" "date" "datetime"
            columns={"id": "int", "ts": "datetime", "line": "str",
                     "temp": "float", "ok": "bool"},
            primary_key="id",
            indexes=[["ts"]],                                # speeds up ORDER BY / WHERE ts
            retention=Retention(keep_days=90, column="ts"),  # delete rows older than 90 days
            form=FormDef(title="Enter readings",
                         labels={"temp": "Temperature (°C)"},
                         choices={"line": ["L1", "L2"]}),     # entry form at /form/readings
        ),
    },
    views={
        # Any SELECT works (JOIN, GROUP BY ...). Show the newest rows first.
        "temp_trend": ViewDef(
            title="Temperature (latest 500)",
            query="SELECT ts, line, temp FROM readings ORDER BY ts DESC LIMIT 500",
            chart=ChartDef(type="line", x="ts", y="temp", y_label="°C", ucl=80, lcl=20),
            styles={"temp": CellStyle(
                greater_than=Threshold(value=80, **{"class": "bg-danger text-white"}),
            )},
        ),
        "all_readings": ViewDef(
            title="All readings",
            query="SELECT * FROM readings ORDER BY ts DESC",
            max_rows=5000,  # never send more than 5000 rows to the browser
        ),
    },
    alerts=[
        AlertRule(view="temp_trend", column="temp", op=">", value=80,
                  level="critical", message="Temperature above 80 °C",
                  notify=["webhook"]),  # MONITOR_WEBHOOK_URL in .env
    ],
    kpis={
        "avg_temp": KpiCard(title="Average temperature", unit="°C", format="{:.1f}",
                            query="SELECT AVG(temp) FROM readings",
                            target=60, higher_is_better=False, link_view="temp_trend"),
    },
    kiosk=KioskConfig(views=["temp_trend", "all_readings"], rotate_seconds=15),
    dashboard=DashboardConfig(views=["temp_trend"], columns=2),
)
```

- **Column types**: always declare them as above. A plain list such as
  `columns=["id", "temp"]` still works, but then types are guessed from the names (the
  primary key and `*_id` are whole numbers, names containing `price` or `amount` are
  decimals, everything else is text). A `temp` column stored as text sorts and compares
  wrongly (`"9" > "10"`); `monitor-app check --data` warns about such columns.
- **`datetime`** columns accept ISO 8601 (`2026-10-21T08:00:00`, with `Z` or `+09:00`)
  and Excel's `2026/10/21 8:00`; times with a time zone are converted to the PC's local
  time. `date` columns accept `2026-10-21` and `2026/10/21`. SQLite stores datetimes as
  `2026-10-21 08:00:00` (with `.ffffff` only when there are fractions of a second), so raw
  SQL such as `ORDER BY ts` or `WHERE ts >= '2026-10-21 08:00:00'` works on the text.
  MySQL tables created by 2.4 keep fractions of a second (`DATETIME(6)`).
- **`float`** values `NaN`, `inf` and `Infinity` are stored as empty (NULL), so `AVG` and
  `MAX` stay correct on every database.
- **`max_rows`** caps what a view returns. Views that return more than 50,000 rows
  without it log a warning.
- **`Retention`**: `keep_days=` (needs a `date`/`datetime` column, or text in ISO format)
  and/or `keep_rows=` (keeps the newest N). It runs every 10 minutes, the first time 10
  minutes after the start (`MONITOR_RETENTION_INTERVAL_MINUTES`). On text columns, each
  row is compared in its own ISO form; empty and non-ISO values are never deleted.
- **Alerts** evaluate the full data of the view, in the background, even when nobody has
  the page open. Notification channels: `console`, `webhook`, `email`.
- Columns you add to a table later are added to an existing database by
  `monitor-app migrate` (tables are created at the first start and not changed after that).

**Check your work before you start the server:**

```sh
monitor-app check                 # config.py, SQL of every view and KPI, settings, database tables
monitor-app check --data          # also the CSV files: headers, types, encodings
monitor-app check --strict        # warnings also fail (exit code 1), e.g. in CI
```

A misspelled key is reported with its line, and the configuration still loads:

```text
警告(1 件):
  - config.py 15 行目: TableDef の未知のキー 'primay_key' は無視されます('primary_key' の誤りではありませんか?)
```

(“config.py line 15: unknown key 'primay_key' in TableDef is ignored — did you mean
'primary_key'?”) `check` also reports misspelled `MONITOR_*` names in `.env` (a typo in
`MONITOR_API_KEY` is an error, because authentication would stay off), API keys that are
example values (`change-me`), shorter than 16 characters or not ASCII, alert and chart
columns that do not exist in the view, and alert channels that are not configured.
`check --data` reads the CSV files exactly as `import-csv` does, so it reports the same
header matches, conversion problems and duplicate primary keys, and, with a PostgreSQL or
MySQL `MONITOR_DATABASE_URL`, values those databases would reject.

The legacy v1 dict-style `config.py` (`ALLOWED_TABLES` ...) still loads; see
**Migrating from v0.x** below.

## 🖱 Change settings and upload CSV files in the browser

The **⚙️ Settings** page (`/settings`) changes the following without editing
`config.py`. Saving applies the change at once, without restarting the server,
and open pages (the wallboard too) reload themselves.

| Tab | What you can change |
|---|---|
| General | App name, header and footer text, refresh interval and mode |
| Views | Title, description, group, row limit, SQL, column labels, cell styles, chart (control and target lines); add and delete views |
| KPIs | Title, SQL, unit, target, number format, linked view; add and delete KPIs |
| Alerts | View, column, condition, value, level, message, channels; add and delete alerts |
| Wallboard and dashboard | Wallboard rotation, theme and views; how many charts per row on the home page and which ones |

- Tables, `sources` and the favicon stay in `config.py` (changing them would
  rebuild tables or read files on the server).
- Only the difference from `config.py` is stored, in the database (internal
  table `_settings`), so it survives restarts and is part of SQLite backups.
  Fields changed in the browser win over the same fields in `config.py`; the
  rest still come from `config.py` (a list such as the alerts is replaced as a
  whole). **Return to config.py** deletes the stored difference.
- Before saving, the same checks as `monitor-app check` run (dry-run of the
  SQL, alert and chart columns, channels); errors block the save. **Check**
  runs them without saving. SQL entered in the browser must be a single
  `SELECT` with no `;` (not even inside quotes or comments; a trailing one is
  fine) and runs read-only. Put SQL that needs `;` in `config.py`.
- If someone else saved in the meantime, the save is refused and the page asks
  you to reload. With `MONITOR_AUDIT_ENABLED=true` changes go to the audit log
  (`/api/audit?table=_settings`).
- If the stored difference no longer fits `config.py` (for example a view was
  removed there), the server starts with `config.py` alone and the settings page
  says so.

**📤 Upload CSV** (`/upload`) imports a CSV file into a table with the same code
as `monitor-app import-csv`: encoding detection (UTF-8, Excel's Shift_JIS),
header variants, one transaction (nothing changes on failure) and refusing to
replace a table from a CSV that lacks its columns. Choose **append** (rows whose
key exists are skipped) or **replace** (all rows become the CSV's rows). The
uploaded file is not kept on the server.

**Who can use them.** Settings and CSV upload are *admin features*:

| Configuration | Who can use the admin features |
|---|---|
| `MONITOR_ADMIN_KEY` set | Whoever logs in with the admin key (or sends it as `X-API-Key`). Holders of `MONITOR_API_KEY` cannot |
| Only `MONITOR_API_KEY` | Whoever logs in with the API key (same as writes) |
| Neither | Only this PC (`MONITOR_HOST=127.0.0.1` and no reverse-proxy settings). Disabled when the server is exposed on the LAN or behind a proxy |

Requests sent from pages on other sites (CSRF) are refused. If operators log in
with the API key to use the entry forms, set a separate `MONITOR_ADMIN_KEY` so
they cannot change the settings.

## ⚙️ Settings (`MONITOR_*` / `.env`)

Settings that differ per PC (database URL, port, passwords) do not belong in `config.py`.
Put them in `.env` in the project folder (copy `.env.example`, which explains every
setting) or set them as environment variables, which win over `.env`.

| Variable | Default | Description |
|---|---|---|
| **Database** | | |
| `MONITOR_DATABASE_URL` | `sqlite:///instances/database.db` | Database URL (SQLAlchemy form). PostgreSQL: `postgresql+psycopg://…` (with `[postgres]`) or `postgresql+pg8000://…`; MySQL: `mysql+pymysql://…`. URL-encode `@ : /` in passwords (`@` → `%40`). |
| `MONITOR_SQLITE_JOURNAL_MODE` | `wal` | SQLite journal mode: `wal` (readers and writers don't wait for each other), `delete`, `truncate`, `persist`, `memory` or `off`. Databases on network shares use `delete` automatically. |
| `MONITOR_SQLITE_SYNCHRONOUS` | `normal` | `off`, `normal`, `full` or `extra`. `normal` applies only in WAL mode: a power cut can lose the last commits but never corrupts the file. Otherwise SQLite's `full` is used. |
| `MONITOR_SQLITE_BUSY_TIMEOUT_MS` | `5000` | How long SQLite waits for a lock, in milliseconds. |
| `MONITOR_DB_WAIT_TIMEOUT` | `15` | Seconds to wait at startup until PostgreSQL/MySQL accepts connections (`0` = don't wait). Wrong passwords fail at once. Each connection attempt gives up after 10 s unless the URL sets its own timeout. |
| **Server and web** | | |
| `MONITOR_HOST` | `127.0.0.1` | Address to listen on. `127.0.0.1` = this PC only; `0.0.0.0` = other PCs too (then set `MONITOR_API_KEY`). |
| `MONITOR_PORT` | `9990` | Port to listen on (0–65535). |
| `MONITOR_ROOT_PATH` | `""` | Sub-path behind a reverse proxy, e.g. `/monitor` (`monitor` and `/monitor/` are accepted too). |
| `MONITOR_FORWARDED_ALLOW_IPS` | (unset) | Proxy addresses whose `X-Forwarded-*` headers are trusted (comma list, `*` = any). Unset: uvicorn's default (`FORWARDED_ALLOW_IPS`, else `127.0.0.1`). |
| `MONITOR_COMPRESS_RESPONSES` | `true` | gzip responses of 1 KiB or more (live updates are never compressed). |
| `MONITOR_ACCESS_LOG` | `true` | One log line per HTTP request. |
| `MONITOR_API_DOCS` | `true` | Serve the API documentation (`/docs`, `/redoc`, `/openapi.json`). `false` removes all three. |
| **Security** | | |
| `MONITOR_API_KEY` | (unset) | Turns on authentication: writes (API, forms, ingest) need the key — `X-API-Key` header, or a login in the browser (the session lasts 12 hours). Use 16 or more random characters; `monitor-app deploy` writes one into `.env`. An empty value counts as unset. |
| `MONITOR_PROTECT_READS` | `false` | Require the key for reading too (pages, read APIs and the API documentation). |
| `MONITOR_ADMIN_KEY` | (unset) | Key for the admin features only (settings page, CSV upload). When set, they accept this key and nothing else; logging in with it also allows reading and writing. Unset: `MONITOR_API_KEY` works. See **Change settings and upload CSV files in the browser** above. |
| `MONITOR_ALLOWED_HOSTS` | (unset) | Host names the server answers to (comma list; `*.example.com` matches subdomains, `*` turns the check off). Unset: a server that listens on `127.0.0.1` refuses host names other than `localhost`, its own names and IP addresses (DNS-rebinding protection); LAN, Docker and the reverse-proxy setups below are not checked. A list is enforced on every interface. |
| `MONITOR_CORS_ORIGINS` | `[]` | Other web origins allowed to call the API, as JSON: `["http://localhost:3000"]`. Empty = same origin only. |
| **CSV import** | | |
| `MONITOR_CSV_DIR` | `csv` | Folder with the CSV files (relative to the folder you start the command in). |
| `MONITOR_CSV_ENCODING` | `auto` | `auto` = UTF-8 (with or without BOM), else the fallback encodings. Or a fixed encoding: `utf-8-sig`, `cp932`, `cp1252`, … |
| `MONITOR_CSV_ENCODING_FALLBACK` | `cp932` | Encodings `auto` tries after UTF-8 (comma list). |
| `MONITOR_INGEST_WATCH` | `false` | Watch the CSV folder and import changed files automatically (also polls `sources`). |
| `MONITOR_INGEST_INTERVAL` | `5` | Seconds between checks of the CSV folder. |
| `MONITOR_INGEST_MODE` | `replace` | `replace` (the table becomes the CSV's content) or `append` (only rows appended to the CSV; the read position is kept in the database, so rows appended while the server was stopped are imported at the next start). |
| **View cache** | | |
| `MONITOR_VIEW_CACHE_TTL_MS` | `1000` | How long a view result is shared between clients, in ms (`0` = no cache). Applies to PostgreSQL/MySQL and to time-dependent SQL. |
| `MONITOR_VIEW_CACHE_MAX_AGE_MS` | `15000` | File SQLite: changes are detected at once; an unchanged result is reused for at most this long (ms). |
| `MONITOR_VIEW_CACHE_MAX_MB` | `256` | Approximate memory limit of the view cache; the least recently used results are dropped above it (`0` = no size limit). Use less on small PCs such as a Raspberry Pi. |
| **Backup and retention** | | |
| `MONITOR_BACKUP_DIR` | (unset) | Folder for automatic SQLite backups. Unset = no scheduled backups. |
| `MONITOR_BACKUP_INTERVAL_HOURS` | `24` | Hours between automatic backups (`0` = off). An overdue backup is taken at startup. |
| `MONITOR_BACKUP_KEEP` | `7` | Backups to keep, newest first (`0` = keep all). Also the default of `backup --keep`. |
| `MONITOR_RETENTION_INTERVAL_MINUTES` | `10` | Minutes between `TableDef.retention` clean-ups (`0` = off). |
| **Alert notifications** | | |
| `MONITOR_WEBHOOK_URL` | (unset) | Webhook for `notify=["webhook"]` (Slack, Teams or any JSON POST). |
| `MONITOR_SMTP_HOST` | (unset) | SMTP server for `notify=["email"]`. |
| `MONITOR_SMTP_PORT` | `587` | SMTP port. When not set: 587 / 465 / 25 to match `MONITOR_SMTP_SECURITY`. |
| `MONITOR_SMTP_SECURITY` | `starttls` | `starttls`, `ssl` (SMTPS) or `none` (plain, for internal relays). |
| `MONITOR_SMTP_TLS_VERIFY` | `true` | Verify the SMTP server's certificate (`starttls`, `ssl`). For an in-house CA, set the environment variable `SSL_CERT_FILE` to the CA file (in `.env` it has no effect); use `false` only for a self-signed relay that cannot be verified. |
| `MONITOR_SMTP_USER` | (unset) | SMTP user name. |
| `MONITOR_SMTP_PASSWORD` | (unset) | SMTP password. |
| `MONITOR_ALERT_EMAIL_TO` | (unset) | Recipients (comma list). |
| `MONITOR_NOTIFY_RETRIES` | `2` | Retries after a failed notification (waits 1, 2, 4 … s, at most 30 s). |
| **Audit log** | | |
| `MONITOR_AUDIT_ENABLED` | `false` | Record changes made through the table API and the entry forms (who, when, before/after); read them at `/api/audit`. Bulk ingest, CSV imports, `sources` and retention are not recorded. |
| **Logging** | | |
| `MONITOR_LOG_LEVEL` | `INFO` | `critical`, `error`, `warning`, `info`, `debug` or `trace` (any case). |
| `MONITOR_LOG_FORMAT` | `text` | `text` or `json` (one JSON object per line, for log collectors). |

- Two variables work **only as environment variables**, not in `.env`:
  `MONITOR_DEBUG=1` (full tracebacks, same as `--debug`) and `MONITOR_CONFIG_PATH` (the
  `config.py` read by `uvicorn monitor_app.main:app_factory --factory`, also a project
  folder or a path starting with `~`; `runserver` sets it from `--config`).
- Wrap values that contain `$` in single quotes (`MONITOR_API_KEY='ab$cd'`): Docker
  Compose reads `.env` too.
- A wrong value is explained in a short message instead of a traceback, for example
  `MONITOR_PORT='abc'(整数を指定してください)` ("give a whole number"). Passwords, keys
  and URLs are never printed.
- `monitor-app check` reports `MONITOR_*` names that are not settings (typos).

## ⚡ Performance and data safety

**SQLite in WAL mode.** File databases use `journal_mode=wal` and `synchronous=normal`:
readers and writers no longer wait for each other and writes need far fewer disk syncs.
A power cut can lose the last few commits, but does not corrupt the database; set
`MONITOR_SQLITE_SYNCHRONOUS=full` if every commit must survive. WAL keeps two side files
next to the database (`database.db-wal`, `database.db-shm`): move or copy them together,
or better, use `monitor-app backup`. On network shares WAL does not work, so Monitor App
switches to `delete` automatically for UNC paths, mapped network drives, NFS/SMB mounts,
WSL's `/mnt/c` and Docker Desktop folders. macOS network shares are not detected: set
`MONITOR_SQLITE_JOURNAL_MODE=delete` there. `delete` is also the setting to get the
2.3 behaviour back.

**Backups.** Do not copy `database.db` while the server runs: in WAL mode the newest
rows may still be in the `-wal` file.

```sh
monitor-app backup                       # now, also while the server runs → backups/database-YYYYmmdd-HHMMSS.db
monitor-app restore backups/database-20261021-172955.db         # shows what would be replaced
monitor-app restore backups/database-20261021-172955.db --yes   # stop the server first
```

- Set `MONITOR_BACKUP_DIR=backups` in `.env` for automatic backups (every
  `MONITOR_BACKUP_INTERVAL_HOURS`, the newest `MONITOR_BACKUP_KEEP` are kept). A PC that
  is switched off at night gets its overdue backup at the next start.
- A backup is a single file, readable only by its owner. `restore` first checks the
  backup (SQLite `quick_check`; a damaged backup is refused and the database is left as
  it is), then copies the current database into the backup folder. It also repairs a
  database that no longer opens (for example after a power cut on an SD card): the
  damaged file is kept as `backups/database-damaged-YYYYmmdd-HHMMSS.db`. Files without
  tables (such as an empty file from a failed copy) are refused.
- Several projects may share one backup folder: each database marks its backups with an
  id in the file header, and rotation and the "last backup" time count only its own
  files. Temporary files left by an interrupted backup are deleted after an hour, and a
  backup dated in the future (wrong clock) does not stop the schedule.
- PostgreSQL and MySQL: use `pg_dump` / `mysqldump` (the commands tell you).

**Big and growing tables.** Give time-series tables a `Retention(...)`, add
`indexes=[["ts"]]` for the columns you sort or filter by, and limit what views return
(`ORDER BY ts DESC LIMIT 1000` or `max_rows=`). Behind the scenes, each view is computed
once and shared by all browsers, kiosks and alert checks. On SQLite, a change made by any
program (even `sync-sources` from cron) is noticed at once, and an unchanged result is
reused for up to `MONITOR_VIEW_CACHE_MAX_AGE_MS` (15 s). On PostgreSQL/MySQL, results are
reused for `MONITOR_VIEW_CACHE_TTL_MS` (1 s); writes made through the server itself (API,
forms, folder watching) show at once. The cache stays below `MONITOR_VIEW_CACHE_MAX_MB`
(256 MB). Unchanged data answers with `304 Not Modified`, and responses are
gzip-compressed; a view's compressed body is kept with the cached result, so it is
compressed once per change, not once per request.

**CSV encodings.** With the default `MONITOR_CSV_ENCODING=auto`, each file is read as
UTF-8 (with or without BOM) and, if that fails, as cp932 (Excel's
「CSV(コンマ区切り)」); a warning names the file and the encoding used. Caution:
Western-European Windows files (cp1252) are then also read as cp932 and get garbled
characters (`25°C` → `25ｰC`). For such files set `MONITOR_CSV_ENCODING=cp1252` (or
`MONITOR_CSV_ENCODING_FALLBACK=cp1252`). A few corrupt bytes in a UTF-8 file do not switch
it to cp932: only the rows that contain them are skipped, each with a warning that names
the line. NUL bytes (left by a power cut on SD cards) are removed, and rows that cannot be
parsed as CSV are skipped with a warning.

**Importing safely.** Every import runs in one transaction (all or nothing). Header
spelling variants (`Name` vs `name`, full-width letters, extra spaces, BOM) are matched to
the declared columns. A replacing import keeps a table as it is when a declared column
(other than the primary key) is missing from its header (`--force` overrides). CSV files
that match no table are listed, with a suggestion when only the letter case differs. Rows
whose values are all empty (Excel's `,,,` lines) are skipped. A primary key that appears
twice in one CSV stops the import with both line numbers. On PostgreSQL/MySQL, a value
that does not fit its column (text longer than 255 characters, a number out of range) skips
only that row, with a warning. On SQLite, a replacing import first reads the CSV into a
temporary table and locks the database only for the final swap, so forms and the API keep
working during a large import (it needs temporary disk space about the size of the table).

**Folder watching.** With `MONITOR_INGEST_WATCH=true`, changed CSV files are imported
while the server runs. `MONITOR_INGEST_MODE=replace` replaces the table (CSV files that
change together are replaced together); `append` reads only the bytes added to the file,
so a 1-million-row log costs nothing extra, and a half-written last line waits until it
is complete. If a file is rotated or rewritten, it is read again from the top; rows whose
primary key exists are skipped, but files without a primary-key column get those rows
again. The read position is saved in the database, so rows appended while the server was
stopped are imported at the next start. Two exceptions: right after a full import at
startup (`runserver --import-csv`, or the first start with `--import-csv-if-empty`), rows
appended during the few seconds before the watcher starts are not imported
(`monitor-app serve --append` has no such gap), and the first start after upgrading from
2.3 begins at the end of the file. A file whose import failed (for example "database is
locked") is tried again at the next check, then at growing intervals up to 60 s, and
`/api/health` shows the error until it succeeds.

**Stopping.** `docker stop`, `systemctl stop` and Ctrl+C stop running background work
(folder watching, retention, alert checks, `sources`) at its next SQL statement and roll
back its transaction. The server waits at most about 4.5 s for it, so a long import never
delays the shutdown until the container is killed.

## 🐳 Deploy

### Docker

```sh
docker run --rm -p 9990:9990 ghcr.io/mikawa-bushi/monitor-app       # warehouse demo, no files needed
```

Run your own project (the folder with `config.py`):

```sh
cd myproject
docker run -d --name monitor-app -p 9990:9990 -v "$PWD":/project \
    --restart unless-stopped ghcr.io/mikawa-bushi/monitor-app
```

(Windows PowerShell: `-v "${PWD}:/project"`.) The container switches to the owner of the
mounted folder and drops all Linux capabilities, so the database and backups belong to
you on the host whatever your user id is. It starts `runserver --import-csv-if-empty`, so
rows entered in the UI survive updates. An empty folder gets the sample project. With
nothing mounted, the warehouse demo above runs on a temporary database.
For a hardened container add `--cap-drop ALL --cap-add SETUID --cap-add SETGID
--cap-add CHOWN --security-opt no-new-privileges` (the generated `compose.yaml` sets
these). `docker run --rm ghcr.io/mikawa-bushi/monitor-app --version` prints the version.

With Docker Compose, let Monitor App write the file:

```sh
monitor-app deploy docker      # writes compose.yaml and .dockerignore
docker compose up -d           # http://localhost:9990/
docker compose ps              # (healthy) when the server answers
docker compose logs -f
docker compose pull && docker compose up -d     # update to the newest image
```

- `monitor-app deploy` writes a random `MONITOR_API_KEY` into `.env` when there is none
  (it prints where, never the key itself). Viewing stays open; adding, changing and
  deleting rows needs the key (log in on the page, or send `X-API-Key`).
- `.env` is read by the app inside the container as usual. The generated `compose.yaml`
  also takes `MONITOR_PORT` (port on the host), `TZ` (for example `TZ=Asia/Tokyo`),
  `MONITOR_IMAGE` (image name) and `MONITOR_IMAGE_TAG` (for example `2.5.0` to stay on a
  version) from `.env`.
- Other commands run the same way as the folder's owner:
  `docker compose run --rm monitor-app check`, `... backup`, `... import-csv`. Do not use
  `docker compose exec monitor-app monitor-app backup`: that runs as root and the backup
  file can then only be read by root.
- Image tags: `latest`, `2`, `2.5`, `2.5.0`; platforms `linux/amd64` and `linux/arm64`
  (64-bit Raspberry Pi OS). The image has a health check (`/api/health`) and stops within
  about a second.
- On Docker Desktop (Windows, macOS) keep `instances/` in a named volume, for speed and
  to avoid "database is locked" errors (see the comments in `compose.yaml`). Build the
  image yourself with `docker build -t monitor-app .`, then set `MONITOR_IMAGE=monitor-app`
  in `.env` and run `docker compose up -d` without `pull`. More:
  [docker/README.md](https://github.com/mikawa-bushi/monitor-app/blob/main/docker/README.md).

### Linux and Raspberry Pi (systemd)

Install Monitor App with pipx or `uv tool` first (not `uvx`: the service needs a Python
that stays where it is). Then, in the project folder:

```sh
monitor-app deploy systemd       # writes monitor-app.service for this folder, user and Python
sudo cp monitor-app.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now monitor-app
systemctl status monitor-app
journalctl -u monitor-app -f     # logs
```

The service runs `runserver --import-csv-if-empty` as your user, restarts after a crash
and reads the project's `.env` (where `deploy` put a generated API key; with `sudo`, the
new `.env` belongs to the user who ran `sudo`). `deploy systemd` stops when the folder has
no `config.py` (`--force` writes the unit anyway; `--config FILE` uses another settings
file). For more than one project, rename the unit file (`monitor-app-line2.service`).

### Windows (start with the PC)

In the project folder:

```powershell
monitor-app deploy windows       # writes start-monitor.bat and install-task.ps1
.\start-monitor.bat              # test it (log: logs\monitor.log)
# As administrator: start at every boot (runs as you; no password is stored)
powershell -ExecutionPolicy Bypass -File .\install-task.ps1
# Without administrator rights: start when you sign in
powershell -ExecutionPolicy Bypass -File .\install-task.ps1 -AtLogon
# Remove the task
powershell -ExecutionPolicy Bypass -File .\install-task.ps1 -Uninstall
```

Keep the project on a local drive (the boot task cannot use network drives). The script
warns when other users of the PC can change the project folder, `config.py` or
`start-monitor.bat`. `-AsSystem` runs the task as the SYSTEM account instead (not
recommended: anything in the project folder then runs with full rights). Task Scheduler
does not restart a server that crashed: check `logs\monitor.log`.

### Behind a reverse proxy, under a sub-path

To serve Monitor App as `https://server/monitor/`, set `MONITOR_ROOT_PATH=/monitor` in
`.env`. It works whether or not the proxy strips the prefix. nginx:

```nginx
location /monitor/ {
    proxy_pass http://127.0.0.1:9990/;    # with the trailing "/" nginx strips /monitor; without it, keeps it
    proxy_http_version 1.1;
    proxy_set_header Host $http_host;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_buffering off;                  # live updates (SSE) arrive immediately
    proxy_read_timeout 1h;
}
```

Caddy:

```caddyfile
# ":80" serves plain HTTP on port 80. Write your host name instead (for example
# monitor.example.com) and Caddy also gets an HTTPS certificate for it.
:80 {
    handle /monitor* {
        reverse_proxy 127.0.0.1:9990
    }
}
```

- Monitor App trusts `X-Forwarded-*` headers only from `MONITOR_FORWARDED_ALLOW_IPS`
  (default `127.0.0.1`). If the proxy runs on another machine, or Monitor App runs in
  Docker (connections then come from the Docker gateway, e.g. `172.17.0.1`), set it to
  the proxy's address, or to `*` when only the proxy can reach the port. Only then does
  the login cookie get the `Secure` flag behind HTTPS.
- Do not choose a sub-path that is also an app path (`/api`, `/static`, `/table`,
  `/form`, `/kiosk`, `/login`, `/docs`); a warning is logged if you do.
- The login cookie is limited to the sub-path, so several instances on one host keep
  separate sessions.
- A proxy on the same machine that serves Monitor App at the root path (no
  `MONITOR_ROOT_PATH`) and passes the original `Host` header to `127.0.0.1` needs
  `MONITOR_ALLOWED_HOSTS=<public name>`; otherwise requests get `400 Invalid host header`
  and the log names the setting.

### PostgreSQL and MySQL

```sh
pipx install "monitor-app[postgres]"   # psycopg, the fast PostgreSQL driver
```

```sh
# .env
MONITOR_DATABASE_URL=postgresql+psycopg://monitor:change-me@db-server:5432/monitor
# MONITOR_DATABASE_URL=postgresql+pg8000://monitor:change-me@db-server:5432/monitor   # no extra needed
# MONITOR_DATABASE_URL=mysql+pymysql://monitor:change-me@db-server:3306/monitor
```

- Write special characters in the password URL-encoded: `@` → `%40`, `:` → `%3A`,
  `/` → `%2F`. An unencoded `@` makes every command stop with a short message (the
  password is never printed). `postgres://...` URLs (as handed out by some cloud
  services) are rejected with a hint to write `postgresql+psycopg://...` (or
  `postgresql+pg8000://...` when psycopg is not installed).
- At startup Monitor App waits up to `MONITOR_DB_WAIT_TIMEOUT` (15 s) for the database
  server, so it can start together with it (Docker Compose, boot). Each connection
  attempt gives up after 10 s, also for a host that does not answer.
- Tables are created at the first start. `monitor-app check` compares `config.py` with
  the existing tables and `monitor-app migrate` adds new columns.
- **Tables created by 2.4** store whole-number columns other than keys as `BIGINT` (Unix
  millisecond timestamps fit) and, on MySQL, datetimes with fractions of a second
  (`DATETIME(6)`). In tables created earlier, a row whose value does not fit its column
  is skipped with a warning that names the file, line and column.
- **MySQL tables created by 2.3.x or earlier** store float columns as single precision
  (`1234567.89` becomes `1234570`). For each float column run
  `ALTER TABLE <table> MODIFY <column> DOUBLE;` — the startup warning and `migrate` show
  the exact statements (and `check` the `DATETIME(6)` statement for datetime columns).
  PostgreSQL and SQLite need nothing.
- **PostgreSQL databases filled by 2.3.x**: id sequences that lag behind the rows imported
  from CSV are moved forward once (first start, import, `sync-sources` or `migrate`), so
  form and API inserts no longer fail with a duplicate key. If the database user may not
  change the sequence, a warning shows the SQL for an administrator.
- On MariaDB, write `LIMIT` inside the view's SQL instead of relying on `max_rows` or
  `?limit=` (MariaDB may ignore an `ORDER BY` inside a subquery).
- The integration presets (see **External tool integrations** below) use SQLite date
  functions and need SQLite.

### Monitoring: `/api/health`

```sh
curl http://127.0.0.1:9990/api/health
```

```json
{"status":"ok","version":"2.5.0","database":"ok","tasks":{"alert scheduler":{"running":true,"last_tick":"2026-10-21T17:30:44+09:00","last_error":null}}}
```

It answers `200` when the database responds to `SELECT 1` and `503` (with
`"status":"error"`) when it does not, needs no login and also answers `HEAD` requests.
`tasks` shows the background jobs (`ingest watcher`, `alert scheduler`, `retention`,
`backup`): whether they run, their last successful run and the type of their last error.

## 🌐 Endpoints

| Path | Description |
|---|---|
| `/` | Home: dashboard with mini charts, KPI cards and the list of views |
| `/table/{view}` | Live table / chart page |
| `/kiosk` | Andon wallboard (full screen, rotating) |
| `/form/{table}` | Operator entry form |
| `/settings` | Settings page (admin feature) |
| `/upload` | CSV upload (admin feature) |
| `/login` | Login page (used when `MONITOR_API_KEY` or `MONITOR_ADMIN_KEY` is set) |
| `/docs`, `/redoc` | OpenAPI documentation (these two pages load pinned Swagger UI / ReDoc versions from a CDN; login needed with `MONITOR_PROTECT_READS`; off with `MONITOR_API_DOCS=false`) |
| `GET/POST /api/tables/{table}` | List rows (`?limit=`, `?offset=`; pages are ordered by primary key) / add a row |
| `GET/PUT/DELETE /api/tables/{table}/{id}` | One row |
| `POST /api/ingest/{table}` | Bulk insert (one object or a list). `?on_conflict=error` (default: duplicate key → `409`, nothing saved), `ignore` (skip existing keys; safe to resend), `update` (overwrite) |
| `GET /api/views/{view}` | View data (`?limit=`, `?offset=`); sends an `ETag`, answers `304` when unchanged |
| `GET /api/views/batch?names=a,b` | Several views in one request |
| `GET /api/views/{view}/stream` | Live updates (Server-Sent Events) |
| `GET /api/views/{view}/export` | `?format=csv` or `xlsx`; `?bom=1` adds a UTF-8 BOM so Excel shows Japanese text correctly. Text that starts with `=` `+` `-` `@` is exported so that spreadsheets do not run it as a formula; `?raw=1` exports the values exactly as stored |
| `GET /api/kpis`, `/api/alerts`, `/api/schema` | KPI values, active alerts, table definitions |
| `GET /api/audit` | Change history (`?table=`, `?limit=`) when `MONITOR_AUDIT_ENABLED=true` |
| `GET/PUT/DELETE /api/settings` | Settings editable in the browser / check and save (`{"settings": {...}, "revision": "..."}`; errors → `422`, saved by someone else first → `409`) / return to `config.py`. Admin feature |
| `POST /api/settings/check` | Check without saving (same rules as `monitor-app check`). Admin feature |
| `POST /api/upload/{table}` | CSV upload (multipart `file`, `mode=append` or `replace`, `force`). Admin feature |
| `GET/HEAD /api/health` | Health check (see above) |

With `MONITOR_API_KEY` set, send the key in the `X-API-Key` header:

```sh
curl -X POST "http://127.0.0.1:9990/api/ingest/readings?on_conflict=ignore" \
     -H "X-API-Key: YOUR-KEY" -H "Content-Type: application/json" \
     -d '[{"id": 100, "ts": "2026-10-21T08:00:00+09:00", "line": "L1", "temp": 25.5, "ok": true}]'
# {"success":true,"inserted":1,"skipped":0}
```

Errors come back as JSON with a code, for example
`{"detail": {"code": "CONFLICT", "message": "..."}}`: `404` (`TABLE_NOT_FOUND`,
`VIEW_NOT_FOUND`, `RECORD_NOT_FOUND`), `409 CONFLICT` (duplicate key, row still
referenced), `422 INVALID_PAYLOAD` (value too long, out of range or malformed),
`503 DATABASE_BUSY` (the database is locked, for example by a large import; retry after
the `Retry-After` seconds) and `500` (`DB_SCHEMA_MISMATCH`, `DATABASE_ERROR`,
`INTERNAL_ERROR`) with a `ref=` that points to the details in the server log. SQL and the
values you sent are never echoed.

## 🔌 External tool integrations

Connect procdiag, netdiag and network-checker to Monitor App with a few lines. The
external tools are not changed (Monitor App only reads their files).

```python
from monitor_app.integrations import procdiag, netdiag, network_checker

config = procdiag.attach(config, db_path="/opt/procdiag/data/procdiag.db")
config = netdiag.attach(config)
config = network_checker.attach(config, hosts=["plc-01", "gateway"])
```

Each `attach()` call merges tables, views, KPIs and alert rules into your config. Run
`monitor-app sync-sources` to import on demand (for example from cron), or set
`MONITOR_INGEST_WATCH=true` for continuous polling. Full reference (Japanese):
[docs/design/integrations_v2_2.md](https://github.com/mikawa-bushi/monitor-app/blob/main/docs/design/integrations_v2_2.md)

## 🐍 Python API

Most people only need the `monitor-app` command. To build Monitor App into your own
program, use the names below. They are covered by the compatibility tests and keep
working across 2.x releases:

| Name | Use |
|---|---|
| everything in `monitor_app.__all__` (`MonitorConfig`, `TableDef`, `ViewDef`, `Retention`, ...) | the models of `config.py` |
| `monitor_app.main.create_app(config, settings=None)` | the FastAPI app for a `MonitorConfig` and `AppSettings` |
| `monitor_app.main.app_factory` | the same, from `MONITOR_CONFIG_PATH` (`uvicorn monitor_app.main:app_factory --factory`) |
| `monitor_app.settings.loader.load_config(path)`, `ConfigError` | load and validate a `config.py` (or a project folder) |
| `monitor_app.settings.runtime.AppSettings` | the `MONITOR_*` settings |
| `monitor_app.integrations.merge`, `procdiag.attach`, `netdiag.attach`, `network_checker.attach` | the integration presets |

```python
import uvicorn
from monitor_app.main import create_app
from monitor_app.settings.loader import load_config
from monitor_app.settings.runtime import AppSettings

app = create_app(load_config("myproject"), AppSettings(database_url="sqlite:///data.db"))
uvicorn.run(app, host="127.0.0.1", port=9990)
```

The logger names (`monitor_app`, `monitor_app.importer`, ...) stay the same too. Every
other module (`monitor_app.services`, `monitor_app.db`, `monitor_app.checks`,
`monitor_app.cli`, ...) is internal and may change in any release.

---

## ⬆️ Upgrading from 2.3.x

```sh
pipx upgrade monitor-app          # or: uv tool upgrade monitor-app / pip install -U monitor-app
monitor-app check --data          # read-only: reports what to adjust
```

Existing projects and databases keep working. Things you may notice:

- **SQLite switches to WAL** at the first start: `database.db-wal` and `database.db-shm`
  appear next to the database. Back up with `monitor-app backup` instead of copying
  `database.db` alone. `MONITOR_SQLITE_JOURNAL_MODE=delete` restores the old mode.
- **CSV encoding is detected** (`MONITOR_CSV_ENCODING=auto`): cp932 files that used to
  fail now import. cp1252 files now import with garbled characters instead of failing;
  set `MONITOR_CSV_ENCODING=cp1252` for them.
- **Use `runserver --import-csv-if-empty`.** `--import-csv` still replaces the tables at
  every start and now warns when a table with an entry form is replaced. The Docker
  image uses `--import-csv-if-empty`.
- **Imports are stricter and safer**: a replacing import keeps the rows of a table whose
  CSV header lacks a declared column other than the primary key (`import-csv` exits 1;
  `--force` overrides), `--keep` skips existing keys instead of failing, and rows whose
  values are all empty are skipped.
- **Command output**: errors are short messages on stderr (`--debug` shows tracebacks);
  uvicorn's lines have timestamps, and access-log lines go to stderr instead of stdout.
  `runserver` checks that the port is free before importing, and always runs one worker
  process.
- **`/api/health`** returns `503` when the database is unreachable, and its body gained
  `version`, `database` and `tasks`.
- **Log in again once**: login sessions from 2.3.x are no longer accepted. A session now
  ends after 12 hours, and logging out ends it on the server too.
- **An empty `MONITOR_API_KEY=` line** now means "no key". Before, authentication looked
  switched on, but it could be bypassed. Set a real key or remove the line.
- **E-mail notifications verify the SMTP server's certificate.** A relay with a
  self-signed or otherwise invalid certificate fails until you set the environment
  variable `SSL_CERT_FILE` (in-house CA) or `MONITOR_SMTP_TLS_VERIFY=false`.
- **Host names are checked** when the server listens on `127.0.0.1` (see
  `MONITOR_ALLOWED_HOSTS`). Only a reverse proxy on the same machine that serves the app at
  the root path and passes the original `Host` header needs
  `MONITOR_ALLOWED_HOSTS=<public name>`.
- **PostgreSQL databases filled by 2.3.x** get their id sequences moved forward
  automatically at the first start (nothing to do).
- **Alerts** are evaluated on the full view data only: `?limit=` pages no longer fire or
  clear alerts (no more repeated notifications).
- **Responses are gzip-compressed** (`MONITOR_COMPRESS_RESPONSES=false` turns it off).
  Duplicate keys in `POST /api/ingest` return `409` JSON. On PostgreSQL/MySQL,
  `/api/views/batch` now returns DECIMAL values as strings, like `GET /api/views/{view}`.
  A locked database answers `503 DATABASE_BUSY` (was `500 DATABASE_ERROR`), other
  unexpected errors return JSON (`500 INTERNAL_ERROR`) instead of plain text, and exports
  protect text that starts with `=` `+` `-` `@` from spreadsheets (`?raw=1` turns it off).
- **`date` / `datetime` values** accept more formats (`2026/10/21`, `Z`, `+09:00`); values
  with a time zone are stored in the PC's local time.
- **config.py**: unknown keys now produce a warning (with `python -W error` an error);
  a trailing `;` in view and KPI SQL is allowed.
- **MySQL**: float columns of tables created by 2.3 are single precision; run the
  `ALTER TABLE ... DOUBLE` statements printed at startup.
- **Docker**: the Dockerfile moved to the repository root (`docker build -t monitor-app .`),
  `docker/docker-compose.yml` is gone (use `monitor-app deploy docker`), and the container
  runs as the owner of the project folder instead of a fixed user.
- **Removed** the unused images `img/background.webp` and `img/logo.webp`.
- **Going back to 2.3.x**: a `config.py` that uses `"datetime"` columns, `retention` or
  `max_rows`, or imports `Retention`, `DashboardConfig` or `SourceDef` from `monitor_app`,
  does not load in 2.3.x. If `config.py` uses `sources` or the procdiag / netdiag /
  network-checker presets, 2.4 has saved their read positions in a form 2.3.x cannot
  compare. **Do not delete the `_ingest_state` rows**: 2.3.x would import everything again
  and fail on every poll or add the rows twice. Instead, stop the server and convert the
  positions while 2.4 is still installed, in the project folder:

  ```sh
  python -c "from monitor_app.db.ingest_state import unwrap_typed_cursors as u; print(u('sqlite:///instances/database.db'))"
  ```

  Use the Python that has monitor-app 2.4 (the `python` of its venv; for pipx or
  `uv tool` installs, `uvx --from monitor-app python -c "..."` works), and for
  PostgreSQL/MySQL your `MONITOR_DATABASE_URL` instead of the SQLite URL. It prints how
  many rows it converted. The same in SQL:
  `UPDATE _ingest_state SET cursor = json_extract(cursor, '$.v') WHERE cursor LIKE '{"v":%'`
  (SQLite), `UPDATE _ingest_state SET cursor = (cursor::json ->> 'v') WHERE cursor LIKE '{"v":%'`
  (PostgreSQL),
  ``UPDATE _ingest_state SET `cursor` = JSON_UNQUOTE(JSON_EXTRACT(`cursor`, '$.v')) WHERE `cursor` LIKE '{"v":%'``
  (MySQL). Then install 2.3.x; it continues where 2.4 stopped. 2.4 reads the converted
  positions too, so upgrading again later is safe. WAL mode and the backup id in the
  SQLite file header do not bother 2.3.x.

Details and the reasons: [CHANGELOG.md](https://github.com/mikawa-bushi/monitor-app/blob/main/CHANGELOG.md).

## 🔄 Migrating from v0.x

v2 was rebuilt on FastAPI + Pydantic. The legacy dict-style `config.py`
(`ALLOWED_TABLES` / `VIEW_TABLES` / `TABLE_CELL_STYLES`) **still loads** through a
compatibility shim, but new projects should use `MonitorConfig`. Key changes:

- `python app.py runserver` → `monitor-app runserver` (the app no longer lives in your project)
- DB settings (`DB_TYPE`, passwords) → `MONITOR_DATABASE_URL` environment variable
- API paths: `/api/<table>` → `/api/tables/{table}`, `/api/view/<v>` → `/api/views/{view}`
- Tables created by v0.x may have no primary key: `monitor-app check` reports them and
  explains how to rebuild them.

Design notes (Japanese): [redesign](https://github.com/mikawa-bushi/monitor-app/blob/main/docs/design/redesign_v2.md) ·
[v2.1 features](https://github.com/mikawa-bushi/monitor-app/blob/main/docs/design/feature_expansion_v2.1.md) ·
[v2.2 integrations](https://github.com/mikawa-bushi/monitor-app/blob/main/docs/design/integrations_v2_2.md) ·
[v2.3 UI](https://github.com/mikawa-bushi/monitor-app/blob/main/docs/design/ui_redesign_v2_3.md) ·
[all documents](https://github.com/mikawa-bushi/monitor-app/blob/main/docs/README.md)

## 🛠 Development

```sh
git clone https://github.com/mikawa-bushi/monitor-app.git
cd monitor-app
uv sync --all-extras                  # .venv with all extras and the dev tools, from uv.lock
uv run pytest                         # full test suite
uv run pre-commit install             # hooks (see below; Python 3.11+)

# Try your working copy on a project in another folder:
uv run monitor-app startproject ../demo
cd ../demo
uv run --project ../monitor-app monitor-app runserver --import-csv-if-empty
```

See [CONTRIBUTING.md](https://github.com/mikawa-bushi/monitor-app/blob/main/CONTRIBUTING.md)
for the test markers, the compatibility tests and the coding conventions.

### Pre-push security & quality checks

A [pre-commit](https://pre-commit.com/) config scans for **secrets** and enforces
formatting locally, before code leaves your machine. Enable it once per clone:

```sh
uv run pre-commit install          # installs the commit + pre-push hooks
uv run pre-commit run --all-files  # optional: run across the repo now
```

- **gitleaks** — blocks hard-coded secrets (PyPI/AWS/GCP tokens, GitHub PATs, private keys)
- **detect-private-key / check-added-large-files / check-merge-conflict**, plus **black / ruff** (the hooks pin their own versions in `.pre-commit-config.yaml`; CI uses the versions in `uv.lock`)
- **check-commit-identity** (pre-push) — rejects commits whose author/committer e-mail is off the allowlist, keeping work/other identities out of history (`scripts/check_commit_identity.py`; extend via the `MONITOR_ALLOWED_EMAILS` env var)
- **check-ai-traces** (commit + pre-push) — flags traces of AI-agent tools (config dirs, file content, commit metadata) so they don't leak into the repo. Tool patterns are data-driven in `scripts/check_ai_traces.py` (extend the `TOOLS` list or `MONITOR_AI_TRACE_EXTRA`; ignore paths via `.ai-trace-ignore`)

Audit the **whole working tree and full git history** on demand:

```sh
python3 scripts/check_ai_traces.py --all   # exit 0 = no traces found
```

> ⚠️ Local hooks can be skipped with `git commit/push --no-verify`. For an
> unbypassable gate, also make `gitleaks` a required GitHub Actions check.

## 📜 License

[MIT](https://github.com/mikawa-bushi/monitor-app/blob/main/LICENSE)
