"""``startproject``: 新規プロジェクトの雛形を生成する。

``--from-csv`` を付けると、雛形のサンプルの代わりに、手元の CSV のデータから推論した
config.py(列の型・ビュー・グラフ・KPI)を書き、その CSV を ``csv/`` にコピーする。
"""

from __future__ import annotations

import dataclasses
import importlib.resources as resources
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

import typer

from ..deploy import DeployError, parse_targets
from ._common import fail
from .deploy import generate, next_steps

if TYPE_CHECKING:
    from ..settings.infer import InferredProject

#: 雛形に必ず書く .gitignore(DB・秘密情報・ログを Git に入れない)。
GITIGNORE = """\
# monitor-app のプロジェクトで Git に入れないもの
instances/*.db*
backups/
.env
logs/
__pycache__/
"""


def _scaffold_dir() -> Path:
    return Path(str(resources.files("monitor_app") / "scaffold"))


def _infer_from_csv(source: Path, dest: Path) -> InferredProject:
    """``source``(CSV のフォルダかファイル)から、新しいプロジェクト用の設定を推論する。

    画面のタイトルはプロジェクト名にし、CSV のフォルダはプロジェクトの ``csv/`` にする。
    """
    from ..settings.infer import InferError, infer_project
    from ._common import load_settings
    from .zero import inference_encodings, print_inferred_project

    settings = load_settings()
    try:
        project = infer_project([source], encodings=inference_encodings(settings))
    except InferError as exc:
        fail(
            str(exc),
            "CSV のあるフォルダかファイルを --from-csv に指定してください",
        )
    print_inferred_project(project)
    title = dest.name
    config = project.config.model_copy(
        update={"app_title": title, "header_text": f"📊 {title}"}
    )
    return dataclasses.replace(project, config=config, csv_dir=dest / "csv")


def startproject(
    name: str = typer.Argument(..., help="作成するプロジェクト名"),
    deploy: str | None = typer.Option(
        None,
        "--deploy",
        metavar="TARGETS",
        help=(
            "デプロイ用のファイルも生成する(docker,systemd,windows,all を"
            "カンマ区切りで指定。例: --deploy docker,windows)"
        ),
    ),
    from_csv: Path | None = typer.Option(
        None,
        "--from-csv",
        metavar="DIR",
        help=(
            "手元の CSV のフォルダ(またはファイル)から列の型・ビュー・グラフを推論して"
            " config.py を作り、CSV を csv/ にコピーする(サンプルの CSV は入れない)"
        ),
        show_default=False,
    ),
) -> None:
    """新規プロジェクトの雛形(config.py・サンプル CSV)を生成する。

    --from-csv を付けると、サンプルの代わりに手元の CSV から推論した config.py と、
    その CSV でプロジェクトを作る。
    """
    dest = Path(name).resolve()
    # 対象名の誤りは、フォルダを作る前に止める。
    try:
        targets = parse_targets([deploy]) if deploy is not None else []
    except DeployError as exc:
        fail(str(exc))
    if dest.exists():
        fail(f"⚠️  '{dest}' は既に存在します")
    # CSV を読めないときも、フォルダを作る前に止める。
    inferred = _infer_from_csv(from_csv, dest) if from_csv is not None else None

    scaffold = _scaffold_dir()
    (dest / "csv").mkdir(parents=True)
    (dest / "instances").mkdir()
    (dest / "instances" / ".gitkeep").touch()

    if inferred is None:
        shutil.copy(scaffold / "config.py", dest / "config.py")
    else:
        from ..settings.infer import render_config_py

        (dest / "config.py").write_text(
            render_config_py(inferred, in_project=True), encoding="utf-8"
        )
    shutil.copy(scaffold / "env.example", dest / ".env.example")
    shutil.copy(scaffold / "README.md", dest / "README.md")
    (dest / ".gitignore").write_text(GITIGNORE, encoding="utf-8")
    if inferred is None:
        for csv_file in (scaffold / "csv").glob("*.csv"):
            shutil.copy(csv_file, dest / "csv" / csv_file.name)
    else:
        for item in inferred.files:
            shutil.copy2(item.path, dest / "csv" / item.path.name)

    typer.secho(f"✅ プロジェクトを作成しました: {dest}", fg=typer.colors.GREEN)
    if inferred is not None:
        typer.echo(
            f"  config.py: CSV {len(inferred.files)} ファイルから推論した設定"
            f"(CSV は {dest / 'csv'} にコピーしました)"
        )
    if targets:
        generate(dest, targets)
    typer.echo("次のステップ:")
    typer.echo(f"  cd {name}")
    typer.echo("  monitor-app runserver --import-csv-if-empty")
    if targets:
        for line in next_steps(dest, targets):
            typer.echo(line)
