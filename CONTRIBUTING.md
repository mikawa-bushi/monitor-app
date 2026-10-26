# Contributing to Monitor App 🚀

Thank you for your interest in contributing to Monitor App! We welcome contributions from
developers of all skill levels: bug reports, documentation, tests and code. This document
explains how to set up the project, run the tests and get a change merged.

Contents: Code of Conduct · Getting started · Running the tests · Backward compatibility ·
Coding conventions · Commits and pull requests · Documentation · Releasing · Reporting
issues · Community

## 📜 Code of Conduct

By participating in this project, you agree to abide by our
[Code of Conduct](https://github.com/mikawa-bushi/monitor-app/blob/main/CODE_OF_CONDUCT.md).
We are committed to providing a welcoming and inspiring community for all.

- **Be respectful** and inclusive in your communication
- **Be collaborative** and help others learn
- **Be patient** with newcomers and different skill levels
- **Focus on constructive feedback** rather than criticism
- **Respect different viewpoints** and experiences

## 🚀 Getting started

### Prerequisites

- Git
- [uv](https://docs.astral.sh/uv/) 0.12.9 or newer (the version CI and the Dockerfile use).
  uv installs the Python versions it needs; Monitor App supports Python 3.11–3.14.
- Optional: Docker (PostgreSQL/MySQL tests, image builds), Node.js 18+ (JavaScript unit
  tests), Chrome or Chromium (browser tests).

### Setup

1. **Fork the repository** on GitHub and clone your fork:
   ```bash
   git clone https://github.com/YOUR-USERNAME/monitor-app.git
   cd monitor-app
   ```
2. **Install** the package (editable), all extras and the dev tools from `uv.lock`:
   ```bash
   uv sync --all-extras
   ```
3. **Enable the git hooks** (secret scan, formatting, commit identity; see the README):
   ```bash
   uv run pre-commit install
   ```
4. **Run the tests** to make sure everything works:
   ```bash
   uv run pytest -q
   ```

### Trying your changes

Run your working copy on a project in another folder, so that the test database and CSV
files stay out of the repository:

```bash
uv run monitor-app startproject ../demo
cd ../demo
uv run --project ../monitor-app monitor-app runserver --import-csv-if-empty
```

Restart the server after changing Python code: `--reload` would watch only the project
folder (`../demo`), not `monitor_app/`. Static files are served with a version that
includes a hash of their content, computed when the server starts. After editing
JavaScript or CSS, restart the server (or reload the page with Ctrl+Shift+R).

### Where things are

| Path | Contents |
|---|---|
| `monitor_app/cli/` | Typer commands (`serve`, `runserver`, `check`, `import-csv`, `backup`, `deploy`, ...); `_errors.py` holds the short error messages (`explain_error`, `MonitorGroup`), `_signals.py` the stop signals, `_common.py` the shared helpers (it re-exports both) |
| `monitor_app/settings/` | `runtime.py` (`AppSettings`, every `MONITOR_*` setting), `declarative.py` (the `config.py` models), `loader.py`, `infer.py` (type inference for `serve`) |
| `monitor_app/db/` | engine and SQLite tuning, schema checks and migrations, bulk inserts, backup/restore |
| `monitor_app/services/` | CSV importer (`importer.py`, with `csv_encoding.py` and `csv_tail.py`) and folder watcher, view cache, alerts and notifiers, KPIs, retention, periodic tasks |
| `monitor_app/security.py`, `cancel.py` | API-key and Host-header rules shared by the HTTP layer, the CLI and `check`; cooperative stop of background work (`stop_requested()`) |
| `monitor_app/api/`, `monitor_app/web/` | FastAPI routers; templates, JavaScript and CSS of the bundled UI |
| `monitor_app/container.py`, `main.py` | service wiring (`build_services`) and `create_app` / `app_factory` |
| `monitor_app/scaffold/` | files written by `startproject` and `deploy` |
| `Dockerfile`, `compose.yaml`, `docker/` | container image, entrypoint and health check |
| `tests/` | the test suite (see below) |

## 🧪 Running the tests

```bash
uv run pytest -q                       # the whole suite (tests that need a service skip themselves)
uv run pytest -q --cov=monitor_app     # with coverage; CI fails below 88 % (pyproject.toml)
uv run pytest -q -m "not e2e"          # without the tests that start real server processes
uv run pytest -q tests/test_view_cache.py
uv run --python 3.11 --isolated --all-extras --with-editable . --group dev python -m pytest -q
```

The last line runs the suite on Python 3.11, the oldest supported version (use it when you
touch version-sensitive APIs). CI runs Python 3.11–3.14 on Linux, 3.12 on Windows and 3.13
on macOS.

### Test markers and opt-in suites

- **`e2e`** (`tests/e2e/`): starts real `uvicorn` server processes (startup, live updates,
  SIGTERM shutdown, logging). They run by default; skip them with `-m "not e2e"`.
- **`server_db`** (`tests/server_db/`): runs against real PostgreSQL and MySQL, with the
  pg8000, psycopg and PyMySQL drivers. Skipped unless `MONITOR_TEST_PG_URL` /
  `MONITOR_TEST_MYSQL_URL` are set. **Every test drops all tables of the database it is
  given**, so use throwaway containers:
  ```bash
  docker run -d --name mon-pg -e POSTGRES_USER=monitor -e POSTGRES_PASSWORD=monitor \
      -e POSTGRES_DB=monitor -p 127.0.0.1:5432:5432 postgres:16-alpine
  docker run -d --name mon-mysql -e MYSQL_ROOT_PASSWORD=rootpw -e MYSQL_USER=monitor \
      -e MYSQL_PASSWORD=monitor -e MYSQL_DATABASE=monitor -p 127.0.0.1:3306:3306 mysql:8.4
  until docker exec mon-pg pg_isready -U monitor -d monitor -h 127.0.0.1; do sleep 1; done
  until docker exec mon-mysql mysql -umonitor -pmonitor -h127.0.0.1 -e 'SELECT 1' monitor; do sleep 1; done
  MONITOR_TEST_PG_URL=postgresql://monitor:monitor@127.0.0.1:5432/monitor \
  MONITOR_TEST_MYSQL_URL=mysql+pymysql://monitor:monitor@127.0.0.1:3306/monitor \
      uv run pytest -q -m server_db tests/server_db
  docker rm -f mon-pg mon-mysql
  ```
  (The MySQL check must go over TCP: during its first start the container runs a
  temporary server that accepts only local socket connections.)
- **Browser tests** (`tests/e2e/test_browser_ui.py`, opt-in): drive the bundled UI in a
  headless Chrome through the DevTools protocol (contrast in both themes, kiosk, polling,
  sub-path hosting). Set `MONITOR_TEST_CHROME` to a Chrome, Chromium or
  chrome-headless-shell executable; in containers and on Ubuntu 24.04+ also
  `MONITOR_TEST_CHROME_ARGS=--no-sandbox`:
  ```bash
  MONITOR_TEST_CHROME=/usr/bin/chromium uv run pytest -q tests/e2e/test_browser_ui.py
  ```
- **JavaScript unit tests** (`tests/js/*.test.mjs`): run by `tests/test_frontend_js.py`
  when Node.js 18+ is on the `PATH` (or `MONITOR_TEST_NODE` points to `node`), skipped
  otherwise. Directly: `node --test tests/js/*.test.mjs`.
- Windows-only tests (the generated `.bat` file) and the PowerShell syntax check run only
  where Windows or PowerShell is available.

### Writing tests

- New behaviour comes with tests; a bug fix comes with a test that fails without the fix.
- Tests are independent of your machine: an autouse fixture in `tests/conftest.py`
  removes your shell's `MONITOR_*` variables (`MONITOR_TEST_*` are kept), runs every test
  in an empty temporary folder (so a `.env` in the repository is never read) and closes
  the databases a test opened. Use `tmp_path` for files. A test that needs the working
  folder to be `tmp_path` and the logging setup left alone calls
  `contract.helpers.isolate_from_host(monkeypatch, tmp_path)`.
- `runserver`'s check that the port is free is stubbed out for all tests
  (`tests/conftest.py`); a test that needs the real check requests the `real_port_probe`
  fixture.
- Prefer a new test file named after the feature over growing an unrelated one.

## 🛡 Backward compatibility (safety-net tests)

Existing users must be able to upgrade without changing anything. These tests guard that
and must stay green:

- **`tests/test_contract.py`** compares the code with the recorded public contract in
  `tests/contract/baseline/*.json`: OpenAPI paths, parameters and schemas, CLI commands
  and options, `MONITOR_*` names and defaults, the `config.py` models, `monitor_app.__all__`
  and response shapes. Additions pass; removals and incompatible changes fail.
  **Never regenerate the baselines to make a failing test pass.** Run
  `uv run python tests/contract/_make_baseline.py` only for a contract change that was
  agreed in an issue or pull request, review the diff, and describe the change in
  `CHANGELOG.md`. An intentional change of a setting's default additionally goes into
  `INTENTIONAL_DEFAULT_CHANGES` in `tests/test_contract.py`, together with migration notes.
- **`tests/test_upgrade_v2_3_1.py`** opens a frozen v2.3.1 project and database
  (`tests/contract/fixtures/v2_3_1_project/`) with the current code. Never edit the
  fixtures; `tests/contract/_make_upgrade_fixture.py` only runs on the v2.3.1 code (it
  refuses to run on any other version).
- **`tests/e2e/test_e2e_server.py`** and **`tests/test_cli_smoke.py`** boot a real server
  (live-update frame, shutdown on SIGTERM in under 8 s) and smoke-test the CLI.
- **`tests/test_known_issues.py`** holds *tripwires*: known bugs, written as tests of the
  correct behaviour and marked `@pytest.mark.xfail(strict=True)` with the issue id in
  `reason`. When your change fixes one, that test passes unexpectedly (XPASS) and, because
  it is strict, fails the run: remove its `xfail` marker and keep the test. To record a
  new known bug, add a tripwire there.

What counts as public: everything in `monitor_app.__all__`, both `config.py` formats
(`MonitorConfig` and the legacy v1 dict style), `MONITOR_*` names and defaults, REST
paths, parameters and response shapes, CLI commands and options,
`monitor_app.main.create_app` / `app_factory` with `MONITOR_CONFIG_PATH`, logger names,
and databases created by earlier versions.

The supported Python API is exactly what `tests/contract/baseline/public_api.json` records
(the README's **Python API** section lists it): `monitor_app.__all__`, `create_app` /
`app_factory`, `settings.loader.load_config` / `ConfigError`, `settings.runtime.AppSettings`,
the `settings.declarative` models, `integrations.merge` and the presets' `attach()`, and
`cli.app`. Every other module and name (`services`, `db`, `checks`, `deploy`, `container`,
`settings.infer`, `cli._common`, ...) is internal: you may rename or change it, as long as
the tests and the public behaviour above stay the same.

## 📏 Coding conventions

- **Python 3.11+**, with `from __future__ import annotations` as in the existing modules.
- **Formatting and lint**: black (88 columns) and ruff, configured in `pyproject.toml`
  and pinned in `uv.lock`:
  ```bash
  uv run black .
  uv run ruff check --fix .
  ```
  CI runs `black --check` and `ruff check` with those `uv.lock` versions. The pre-commit
  hooks pin their own versions in `.pre-commit-config.yaml`; when the two disagree, the
  `uv run` commands above are what CI checks.
- **Comments, docstrings, log messages and CLI messages are in Japanese**, in the style and
  density of the surrounding code. Internal ids such as `(#E8)` or `(PERF-06)` may appear
  in comments, but not in docstrings or `Field` descriptions that end up in the OpenAPI
  document (users read those at `/docs`). Each id must be listed in
  [docs/design/refactor_2_4_ids.md](https://github.com/mikawa-bushi/monitor-app/blob/main/docs/design/refactor_2_4_ids.md)
  (`tests/test_internal_ids_documented.py` checks it); prefer writing the reason itself.
- **Complexity**: ruff's C901 check fails functions above McCabe complexity 20. Split the
  function, or add `# noqa: C901 - <reason>` when one place is clearer.
- **Moving code between modules**: re-export the moved names from the old module, and
  update the tests that patch them (`monkeypatch.setattr(module, "NAME", ...)`) in the
  same change. A re-export keeps imports working, but a patch of the old module's global
  no longer affects code that now lives elsewhere.
- **Errors for users** are short (at most three lines), say what to do, and never print
  passwords, API keys or database URLs. CLI commands report them through `fail()` and the
  mappings in `monitor_app/cli/_errors.py`; `--debug` / `MONITOR_DEBUG=1` shows the
  traceback.
- **Background work** (folder watching, retention, alert checks) runs in `PeriodicTask`
  threads. At shutdown such a task is stopped at its next SQL statement and its
  transaction is rolled back; a long loop that does not run SQL checks
  `monitor_app.cancel.stop_requested()` itself.
- **New settings** go into `AppSettings` (`monitor_app/settings/runtime.py`) with a `#:`
  comment, into `monitor_app/scaffold/env.example`, and into the settings tables of
  `README.md` and `README_ja.md`. Settings that differ per PC belong there, not in
  `config.py`.
- **Dependencies**: add them to `pyproject.toml` with a lower bound you have tested (CI
  also runs the suite with the lowest allowed versions) and run `uv lock`. Commit
  `uv.lock`; CI checks it with `uv lock --check`.

## 🔄 Commits and pull requests

1. **Open an issue first** for larger changes, so that the approach can be discussed.
2. **Create a branch** from `main`:
   ```bash
   git checkout -b feature/your-feature-name    # or fix/..., docs/...
   ```
3. **Write commit messages in English.** CI (`code-quality-check.yml`) rejects commit
   messages that contain Japanese, Chinese, Korean, Cyrillic or Arabic characters, or less
   than 80 % ASCII. (Code comments stay Japanese.) Example: `Add retention to the readings preset`.
4. **Push** to your fork and open a pull request against `main`. Describe what changed and
   why, link the issue (`Closes #123`), and add screenshots for UI changes.

CI runs on every pull request: lint (`uv lock --check`, black, ruff), the tests on the
Python/OS matrix with the coverage gate, the lowest-dependency job, the PostgreSQL 16 /
MySQL 8.4 job, a smoke test of the built wheel and a smoke test of the Docker image.
`dependency-audit.yml` runs pip-audit on the dependencies locked in `uv.lock`, on every pull
request and weekly. The workflows pin every action by commit SHA (the version is in a comment)
and check out without persisting credentials; Dependabot (`.github/dependabot.yml`) proposes
action and Python dependency updates weekly, and security fixes as soon as an advisory is
published. Keep both when you edit a workflow.

### Pull request checklist

```markdown
- [ ] `uv run pytest -q` passes (also `-m server_db` if I touched the database layer or the importer)
- [ ] `uv run black --check .` and `uv run ruff check .` are clean
- [ ] `uv lock --check` passes (if I changed dependencies)
- [ ] the contract baselines are unchanged, or the agreed contract change is explained
- [ ] new behaviour has tests; a fixed bug has a regression test (and its tripwire marker is removed)
- [ ] user-visible changes are in CHANGELOG.md, README.md and README_ja.md (and env.example for settings)
- [ ] commit messages are in English
```

## 📖 Documentation

- `README.md` (English) and `README_ja.md` (Japanese) have the same structure and content:
  change both. Command examples must work as written; check options with
  `uv run monitor-app <command> --help`.
- `CHANGELOG.md` is written in Japanese, in the style of the existing entries (Added /
  Changed / Fixed / ... with migration hints).
- `docs/README.md` indexes the design notes in `docs/design/`; `docker/README.md` covers
  the container image.

## 📦 Releasing (maintainers)

1. Set the version in `pyproject.toml`, run `uv lock`, and add the `CHANGELOG.md` entry.
2. Merge to `main`, then tag and push: `git tag v2.4.0 && git push origin v2.4.0`.
3. `build.yml` runs the full test workflow, checks that the tag matches the version, builds
   the sdist and wheel, publishes them to PyPI (Trusted Publishing, environment `pypi`) and
   pushes the `linux/amd64` + `linux/arm64` image to `ghcr.io/mikawa-bushi/monitor-app`
   (tags `X.Y.Z`, `X.Y`, `X`, `latest`). After the very first image push, make the GHCR
   package public.
4. In the repository settings, give the `pypi` environment required reviewers and allow
   deployments from `v*` tags only, so that a publish always needs a person's approval.

## 🐛 Reporting issues

Search the existing issues first, and try the latest version. A good bug report contains:

```markdown
## Bug Description
A clear and concise description of the bug.

## Steps to Reproduce
1. Run '...'
2. Open '...'
3. See error

## Expected Behavior
What you expected to happen.

## Actual Behavior
What actually happened (the error message, or the output of `monitor-app --debug <command>`).
Remove passwords and API keys before posting.

## Environment
- OS: [e.g. Ubuntu 24.04, Windows 11, Raspberry Pi OS 64-bit]
- Python version: [e.g. 3.12.4]
- Monitor App version: [output of `monitor-app --version`]
- Installed with: [pipx / uv / pip / Docker]
- Database: [SQLite / PostgreSQL 16 / MySQL 8.4]
```

For feature requests, describe the problem you want to solve, the solution you would like,
and the alternatives you considered.

## 👥 Community

- **GitHub Issues**: bugs, feature requests and questions
- **Documentation**: [README.md](https://github.com/mikawa-bushi/monitor-app/blob/main/README.md)
  ([日本語](https://github.com/mikawa-bushi/monitor-app/blob/main/README_ja.md)) and
  [docs/](https://github.com/mikawa-bushi/monitor-app/blob/main/docs/README.md)

Be respectful, give context when you ask for help, share what you learn, and follow up on
your issues and pull requests. Contributors are credited in the release notes for
significant contributions.

## 📞 Contact

- **Project Maintainer**: [@hardwork9047](https://github.com/hardwork9047)
- **Issues**: [GitHub Issues](https://github.com/mikawa-bushi/monitor-app/issues)

---

## 🙏 Thank You

Thank you for taking the time to contribute to Monitor App! Every contribution, no matter
how small, helps make this project better for everyone.

**Happy coding!** 🚀
