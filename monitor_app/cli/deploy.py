"""``deploy``: systemd・Windows 向けの起動ファイルを生成する。"""

from __future__ import annotations

import shlex
from pathlib import Path

import typer

from ..deploy import (
    DeployError,
    ensure_api_key,
    foreign_targets,
    is_ephemeral_python,
    parse_targets,
    plan_deploy_files,
    write_deploy_files,
)
from ._common import fail

#: 対象ごとの見出し。
_TITLES = {"systemd": "Linux・systemd", "windows": "Windows"}
#: 対象ごとに、作り直す OS とコマンド(この OS 向けではない対象を生成したときの案内)。
_REGENERATE = {
    "systemd": ("Linux のサーバー", "monitor-app deploy systemd"),
    "windows": ("Windows の PC", "monitor-app deploy windows"),
}


def next_steps(project: Path, targets: list[str]) -> list[str]:
    """生成した対象ごとの「次のステップ」の行(startproject からも使う)。"""
    lines: list[str] = []
    if "systemd" in targets:
        unit = project / "monitor-app.service"
        lines += [
            f"[{_TITLES['systemd']}]",
            # systemd の手順は Linux のシェルで実行する。
            f"  sudo cp {shlex.quote(str(unit))} /etc/systemd/system/",
            "  sudo systemctl daemon-reload",
            "  sudo systemctl enable --now monitor-app",
            "  journalctl -u monitor-app -f   (ログ)",
        ]
    if "windows" in targets:
        bat = project / "start-monitor.bat"
        task = project / "install-task.ps1"
        lines += [
            f"[{_TITLES['windows']}]",
            f"  起動: {bat} をダブルクリック(ログ: logs\\monitor.log)",
            "  PC の起動時に自動で開始: 管理者として開いた PowerShell で",
            f'    powershell -ExecutionPolicy Bypass -File "{task}"',
            "    (実行したユーザーの権限で動く。管理者でなければ -AtLogon を付けると、"
            "サインインしたときに開始する)",
        ]
    lines.append(
        "ほかの PC から見るには、.env に MONITOR_HOST=0.0.0.0 を設定してください"
        "(書き込みに必要な MONITOR_API_KEY は .env にあります)"
    )
    return lines


def _ensure_api_key(project: Path) -> None:
    """.env に API キーが無ければ生成して書き、どこにあるかを表示する(#D6-17)。"""
    try:
        result = ensure_api_key(project)
    except DeployError as exc:
        typer.secho(
            f"⚠️  {exc}。.env に MONITOR_API_KEY=<推測されにくい文字列> を書いてください",
            fg=typer.colors.YELLOW,
        )
        return
    if result.generated:
        typer.secho(
            f"✅ API キーを生成して {result.path} に書きました(MONITOR_API_KEY)。",
            fg=typer.colors.GREEN,
        )
        typer.echo(
            "  データの追加・変更・削除には、画面のログインでこのキーを入力します"
            "(API では X-API-Key ヘッダ)。閲覧はキーなしでできます"
        )
    for problem in result.problems:
        typer.secho(
            f"⚠️  {result.path}: {problem.message}({problem.hint})",
            fg=typer.colors.YELLOW,
        )


def generate(
    project: Path,
    targets: list[str],
    *,
    force: bool = False,
    port: int | None = None,
    config: str | None = None,
) -> None:
    """ファイルを書き出し、作成・スキップしたファイルと注意を表示する。

    既にあるファイルは ``force=True`` でない限り残す。プロジェクトの .env に API キーが
    無ければ生成して書く。次のステップは :func:`next_steps` で別に表示する。
    """
    try:
        files = plan_deploy_files(project, targets, port=port, config=config)
    except DeployError as exc:
        fail(str(exc))
    try:
        written, skipped = write_deploy_files(files, force=force)
    except OSError as exc:
        fail(
            f"ファイルを書き込めません: {exc.filename}({exc.strerror or exc})",
            "フォルダの書き込み権限を確かめるか、--project で書き込めるフォルダを"
            "指定してください",
        )

    root = Path(project).expanduser().resolve()
    if written:
        typer.secho(
            f"✅ デプロイ用のファイルを作成しました: {root}", fg=typer.colors.GREEN
        )
        for file in written:
            typer.echo(f"  {file.path.name}({_TITLES[file.target]})")
    if skipped:
        names = ", ".join(file.path.name for file in skipped)
        typer.secho(
            f"⚠️  既にあるため上書きしませんでした: {names}(上書きするには --force)",
            fg=typer.colors.YELLOW,
        )
    if is_ephemeral_python():
        typer.secho(
            "⚠️  uvx などの一時的な環境の Python を起動ファイルに書きました。この環境が"
            "消えると起動できません。uv tool install monitor-app(または pip install)"
            "で入れてから、もう一度生成してください",
            fg=typer.colors.YELLOW,
        )
    for target in foreign_targets(dict.fromkeys(file.target for file in written)):
        where, command = _REGENERATE[target]
        names = ", ".join(f.path.name for f in written if f.target == target)
        typer.secho(
            f"⚠️  {names} には、この PC の Python とフォルダのパスを書きました。"
            f"{where}で「{command}」を実行して作り直してください",
            fg=typer.colors.YELLOW,
        )
    _ensure_api_key(root)


def _check_config(
    root: Path, targets: list[str], config: str | None, *, force: bool
) -> None:
    """設定ファイルが無ければ止める(--force なら警告だけにする)(#D5-10)。

    起動ファイルは、設定ファイルが無いと起動に失敗し続ける(systemd は 5 秒ごとに
    再起動を繰り返す)。
    """
    if config is not None:
        if not (root / config).is_file():
            fail(
                f"設定ファイルが見つかりません: {root.resolve() / config}",
                "--config にはプロジェクトのフォルダからの相対パスか絶対パスを指定して"
                "ください",
            )
        return
    if (root / "config.py").exists():
        return
    message = (
        f"{root.resolve()} に config.py がありません(プロジェクトのフォルダで"
        "実行するか、--project で指定してください。設定ファイルの名前・場所が違うなら"
        " --config)"
    )
    if not force:
        fail(
            message,
            f"このままでは {' / '.join(_TITLES[t] for t in targets)} の起動ファイルが"
            "起動に失敗します。それでも生成するなら --force を付けてください",
        )
    typer.secho(f"⚠️  {message}", fg=typer.colors.YELLOW)


def deploy_command(
    targets: list[str] | None = typer.Argument(
        None,
        metavar="[TARGET]...",
        help=(
            "生成する対象: systemd / windows / all"
            "(既定: この OS 向けのもの。Linux なら systemd、Windows なら windows)。"
            "複数指定やカンマ区切りもできる"
        ),
        show_default=False,
    ),
    project: Path = typer.Option(
        Path("."), "--project", help="プロジェクトのフォルダ(config.py のある場所)"
    ),
    force: bool = typer.Option(False, "--force", help="既にあるファイルも上書きする"),
    port: int | None = typer.Option(
        None,
        "--port",
        help="待ち受けるポート(既定: .env の MONITOR_PORT または 9990)",
    ),
    config: str | None = typer.Option(
        None,
        "--config",
        help=(
            "設定ファイル(プロジェクトのフォルダからの相対パスか絶対パス。"
            "既定: config.py)。起動ファイルの runserver に --config として渡す"
        ),
        show_default=False,
    ),
) -> None:
    """systemd・Windows 向けの起動ファイルを生成する。

    systemd のユニット、Windows の起動用 .bat とタスク登録用スクリプトを、
    プロジェクトのフォルダに書き出す。.env に API キーが無ければ、推測されにくい
    キーを生成して書く。
    """
    try:
        names = parse_targets(targets or [])
    except DeployError as exc:
        fail(str(exc))
    root = project.expanduser()
    if not root.is_dir():
        fail(
            f"フォルダが見つかりません: {root}",
            f"monitor-app startproject <name> --deploy {','.join(names)} で、"
            "デプロイ用ファイル付きのプロジェクトを作成できます",
        )
    _check_config(root, names, config, force=force)
    generate(root, names, force=force, port=port, config=config)
    typer.echo("次のステップ:")
    for line in next_steps(root.resolve(), names):
        typer.echo(line)
