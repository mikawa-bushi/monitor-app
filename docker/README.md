# Running monitor-app with Docker

The image runs `monitor-app runserver` for one project mounted at `/project`
(a folder with `config.py`, `csv/` and `instances/`). Published images support
`linux/amd64` (PCs) and `linux/arm64` (64-bit Raspberry Pi OS):

```
ghcr.io/mikawa-bushi/monitor-app:latest     # newest release
ghcr.io/mikawa-bushi/monitor-app:2          # newest 2.x
ghcr.io/mikawa-bushi/monitor-app:2.5.0      # exact version
```

Files in this repository:

| File | Purpose |
|---|---|
| `Dockerfile` (repository root) | builds the image (uv + `uv.lock`, no compilers) |
| `.dockerignore` (repository root) | allowlist for the build context |
| `compose.yaml` (repository root) | builds from the checkout and runs the demo |
| `docker/entrypoint.sh` | container entrypoint (see [What the container does](#what-the-container-does)) |
| `monitor_app/scaffold/deploy/compose.yaml` | template written by `monitor-app deploy docker` |

## 1. Try the demo

```sh
docker run --rm -p 9990:9990 ghcr.io/mikawa-bushi/monitor-app
```

Open http://localhost:9990/. With nothing mounted, the container runs
`monitor-app demo`: the inventory screen of a fictional warehouse whose stock
moves every 4 seconds. The data lives in a temporary database inside the
container and nothing is written to the host. Add `-e TZ=Asia/Tokyo` (or your
zone) to see local times instead of UTC. Options such as `--port 8000` are
passed to the demo.

Mount an empty folder instead (`-v "$PWD":/project`) to get the sample project
(the same files as `monitor-app startproject`) on the host.

From a checkout of this repository (no registry needed), Compose builds the
image and runs the sample project in the `project` volume:

```sh
docker compose up -d --build    # http://localhost:9990/
docker compose logs -f
docker compose down             # keeps the data in the "project" volume
docker compose down -v          # also deletes the data
```

## 2. Run your own project

Create the deploy files in your project folder (the folder with `config.py`):

```sh
monitor-app deploy docker                 # or: monitor-app startproject myproj --deploy docker
docker compose up -d                      # http://localhost:9990/
docker compose ps                         # STATUS shows (healthy) when the server answers
```

The generated `compose.yaml` mounts the project folder at `/project`, so
`config.py`, `csv/` and the SQLite database in `instances/` stay on the host.
If `.env` has no `MONITOR_API_KEY`, `monitor-app deploy` writes a random one
there and prints where it is: viewing stays open, while adding, changing and
deleting rows needs the key (log in on the page, or send `X-API-Key`).
Without Compose, the same thing is:

```sh
docker run -d --name monitor-app -p 9990:9990 \
  --cap-drop ALL --cap-add SETUID --cap-add SETGID --cap-add CHOWN \
  --security-opt no-new-privileges \
  -v "$PWD":/project --restart unless-stopped ghcr.io/mikawa-bushi/monitor-app
```

The `--cap-drop`/`--cap-add`/`--security-opt` options (also set in the generated
`compose.yaml`) are optional hardening: the container keeps only what it needs
to switch to the owner of the folder.

Tip: mount an **empty** folder and the container writes the sample project
into it, ready to edit.

## What the container does

On every start, `docker/entrypoint.sh`:

1. **Switches to the owner of `/project`.** The container starts as root, reads
   the owner (uid:gid) of `/project` and drops to that user, with no
   capabilities and `no_new_privs` set. Files it creates (the database, logs)
   belong to you on the host, whatever your uid is (1000, 1001, ...).
   - If the folder is owned by root, the server keeps uid 0 but drops every
     capability. With rootless Docker, Podman and Docker Desktop, container root
     is your own account, so nothing else changes. With rootful Docker on Linux
     it prints a warning: the folder is owned by root on the host, typically
     because `-v` named a folder that did not exist and Docker created it as
     root. Run `sudo chown -R "$(id -u):$(id -g)" <folder>` on the host and the
     next start runs as you.
   - If you pass `--user` with a non-root user, it runs as that user unchanged.
   - If the container may not switch users (`--cap-drop ALL` without
     `--cap-add SETUID --cap-add SETGID`), it exits with a message naming the
     missing capabilities, because root without capabilities cannot write
     another user's folder. Add those capabilities (and `CHOWN`), or start with
     `--user <uid>:<gid>` of the folder's owner.
2. **Checks the project** (only for `runserver`):
   - `/project` is empty (a `lost+found` folder does not count): writes the
     sample project.
   - `/project` has files but no `config.py`: prints what to mount and exits 1.
     With `--config other.py`, that file is used instead of `config.py`.
   - The SQLite folder (from `MONITOR_DATABASE_URL`, default
     `instances/database.db`) or the database files are not writable: prints how
     to fix it and exits 1.
3. Starts `monitor-app runserver --import-csv-if-empty`. CSV files are imported
   only into tables that have no rows yet, so rows entered in the UI, the API or
   forms are **kept across restarts and updates**. To reload the CSV files on
   purpose, run `docker compose run --rm monitor-app import-csv`, or set
   `MONITOR_INGEST_WATCH=true` to import changed CSV files automatically.

Options passed after the image name go to `runserver`, for example
`docker run ... ghcr.io/mikawa-bushi/monitor-app --import-csv` (`--help` shows
the `runserver` help without creating the demo, and `--version` alone prints
the version). Other
`monitor-app` commands can be given by name, for example
`docker compose run --rm monitor-app check` or
`docker run --rm -v "$PWD":/project ghcr.io/mikawa-bushi/monitor-app check`
(they also run as the owner of the folder). Prefer this over
`docker compose exec monitor-app monitor-app ...`: `exec` bypasses the entrypoint
and runs as root, so files it creates (for example backups) belong to root.

To use another port on the host, change the published port
(`-p 8000:9990`, or `MONITOR_PORT` in `.env` with the generated `compose.yaml`).

The health check calls `/api/health` (`200` when the database answers, `503`
otherwise) on the port the server listens on (also with `--port`). It never goes
through `HTTP_PROXY`/`HTTPS_PROXY`, so proxy settings for notifications do not
make the container unhealthy. `docker stop` takes about a second, even with open
SSE streams or while CSV files are being imported at startup.

## Settings

The server reads `MONITOR_*` settings from `.env` in the project folder, exactly
as it does outside Docker (copy `.env.example` to `.env`). Two values are fixed
inside the container and override `.env`: `MONITOR_HOST=0.0.0.0` and
`MONITOR_PORT=9990`.

The generated `compose.yaml` also reads four variables from `.env` itself:

| Variable | Meaning | Default |
|---|---|---|
| `MONITOR_PORT` | port published on the host | `9990` |
| `TZ` | time zone of the container, e.g. `Asia/Tokyo` | the host's zone when the file was generated, else `UTC` |
| `MONITOR_IMAGE` | image name, e.g. `monitor-app` for an image you built yourself | `ghcr.io/mikawa-bushi/monitor-app` |
| `MONITOR_IMAGE_TAG` | image tag, e.g. `2.5.0` to pin a version | `latest` |

Notes:

- The `.env` file is not passed with Compose's `env_file:` on purpose: Compose
  expands `$` inside values, so a password such as `ab$cd` would reach the
  server as `ab`. If a value contains `$`, wrap it in single quotes
  (`MONITOR_API_KEY='ab$cd'`) so Compose does not warn about it.
- **Security**: the container listens on all interfaces. Keep
  `MONITOR_API_KEY` in `.env` (`monitor-app deploy` generates one); without it
  anyone who can open the page can add, change and delete rows. The server logs
  a warning at startup when the key is missing, is an example value such as
  `change-me`, or is shorter than 16 characters.
- Logs: `docker compose logs -f`. They use the `local` log driver with rotation
  (3 x 10 MB). `MONITOR_LOG_FORMAT=json` prints one JSON object per line, and
  `MONITOR_ACCESS_LOG=false` turns off the per-request lines.
- Run **one** container per project. Folder watching and alert evaluation run in
  the server process, so `--scale` or several workers would import rows and
  send notifications several times.

## Docker Desktop (Windows, macOS) and SQLite

On Docker Desktop, bind-mounted folders go through a file-sharing layer that is
slow for SQLite and can cause "database is locked" errors. Keep `config.py` and
`csv/` on the host but put `instances/` in a named volume: uncomment the
`instances:` lines in the generated `compose.yaml`:

```yaml
    volumes:
      - .:/project
      - instances:/project/instances
...
volumes:
  instances:
```

The database then lives in Docker's storage. Back it up as described below.

## PostgreSQL

The image includes the `psycopg` driver. The generated `compose.yaml` contains a
commented `db:` service: uncomment it and the `depends_on:` block, then set in
`.env`:

```sh
MONITOR_DATABASE_URL=postgresql+psycopg://monitor:change-me@db:5432/monitor
```

The server waits for the database at startup (`MONITOR_DB_WAIT_TIMEOUT`, 15
seconds by default) and `/api/health` reports `503` while it is unreachable.
MySQL works the same way with `mysql+pymysql://...`.

## Behind a reverse proxy

Publish the port only to the proxy (for example `127.0.0.1:9990:9990` in
`ports:`) and forward requests to it. With nginx:

```nginx
location / {
    proxy_pass http://127.0.0.1:9990;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_buffering off;              # server-sent events (live tables)
    proxy_read_timeout 1h;
}
```

The server trusts `X-Forwarded-*` headers only from the addresses in
`MONITOR_FORWARDED_ALLOW_IPS` (default: `127.0.0.1`). Inside a container,
connections never come from `127.0.0.1`: a proxy on the host arrives from the
Docker network's gateway (for example `172.17.0.1`), and a proxy in another
container from its own address. Set `MONITOR_FORWARDED_ALLOW_IPS` to that
address, or to `*` when only the proxy can reach the port (published on
`127.0.0.1` as above). Otherwise the server ignores `X-Forwarded-Proto` and
the client address in the logs is the proxy's. To serve the app under a sub-path
such as `/monitor/`, set `MONITOR_ROOT_PATH=/monitor` (see the main README).

## Backups

The SQLite database is `instances/database.db` in the project folder (or in the
`instances` volume). Do not copy the file while the server runs: in WAL mode
the newest rows are still in the `-wal` file. Instead:

- take a backup now, also while the server runs (written to `backups/` as the
  folder's owner):
  ```sh
  docker compose run --rm monitor-app backup
  ```
- let the server write consistent snapshots: set `MONITOR_BACKUP_DIR=backups`
  in `.env` (`MONITOR_BACKUP_INTERVAL_HOURS`, `MONITOR_BACKUP_KEEP` control the
  schedule and how many to keep), or
- stop it first: `docker compose stop`, copy `instances/`, `docker compose start`.

To restore a backup, stop the server, restore, and start it again:

```sh
docker compose stop
docker compose run --rm monitor-app restore backups/database-YYYYmmdd-HHMMSS.db --yes
docker compose start
```

For PostgreSQL use `pg_dump` (for example
`docker compose exec db pg_dump -U monitor monitor > backup.sql`).

## Upgrading

```sh
docker compose pull       # newest image for MONITOR_IMAGE_TAG (default: latest)
docker compose up -d      # recreates the container; data in the folder is kept
```

To stay on a known version, put `MONITOR_IMAGE_TAG=2.5.0` in `.env`. Read the
changelog before major upgrades. After you change `config.py`, run
`docker compose restart`.

## Building the image yourself

```sh
docker build -t monitor-app .                       # classic builder and BuildKit both work
docker build --build-arg PYTHON_VERSION=3.13 -t monitor-app .
```

Build arguments: `PYTHON_VERSION` (default `3.12`) and `UV_VERSION` (the pinned
uv used to install `uv.lock`). The image contains Python, the locked
dependencies with the `xlsx` and `postgres` extras, precompiled bytecode and the
sample project. It has no compilers, `curl` or `sqlite3`, and no setuid
programs.

To run your own build with the generated `compose.yaml`, put
`MONITOR_IMAGE=monitor-app` in `.env` (the name given to `docker build -t`;
`MONITOR_IMAGE_TAG` stays `latest`). Then use `docker compose up -d` without
`pull`, which would look for the image on Docker Hub.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `エラー: /project に config.py がありません` | Mount the folder that contains `config.py`, or an empty folder to get the sample project. |
| `エラー: /project の所有者(uid=...)に切り替えられません` | The container was started with `--cap-drop ALL`: add `--cap-add SETUID --cap-add SETGID --cap-add CHOWN`, or start with `--user <uid>:<gid>` of the folder's owner. |
| `monitor-app: /project の所有者が root のため、root のまま(権限を持たせずに)実行します` | The host folder is owned by root (Docker creates a missing `-v` folder as root). Run `sudo chown -R "$(id -u):$(id -g)" <folder>` on the host and restart. |
| `エラー: SQLite の保存先 ... に書き込めません` | The second line names the cause: started with another `--user` (start without it; the container switches to the folder's owner), files inside owned by someone else, for example after running as root once (`sudo chown -R "$(id -u):$(id -g)" <folder>`), or a read-only mount (`:ro`). |
| `port is already allocated` | Another program uses the port: set `MONITOR_PORT=9991` in `.env` (or change `ports:`). |
| Status stays `unhealthy` | `docker compose logs monitor-app`; `/api/health` returns `503` when the database cannot be reached. |
| Times are 9 hours off | Set `TZ=Asia/Tokyo` (or your zone) in `.env` and run `docker compose up -d`. |
| 32-bit Raspberry Pi OS | No image for `armv7`; install natively with `pip`/`uv` instead (see the main README). |
