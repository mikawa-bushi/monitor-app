"""CSV のアップロード(管理の機能)。

ブラウザから送った CSV を、``monitor-app import-csv`` と同じ処理(:class:`CsvImporter`)で
テーブルへ取り込む。文字コードの自動判別・見出しの表記ゆれ・1 トランザクションでの
取り込み(失敗すれば何も変わらない)・列が丸ごと無い CSV での置き換えの拒否も同じ。
送られたファイルは一時フォルダに置き、取り込んだら消す(csv/ フォルダには残さない)。
"""

from __future__ import annotations

import dataclasses
import re
import shutil
import tempfile
from functools import partial
from pathlib import Path, PureWindowsPath
from typing import TYPE_CHECKING, Any, BinaryIO, Literal

import anyio
from fastapi import APIRouter, Depends, File, Form, Request, UploadFile

from ...exceptions import CsvEncodingError, InvalidPayloadError, TableNotFoundError
from ...services.importer import TableImportResult
from ...settings.infer import has_control_chars
from ..deps import get_actor, require_admin

if TYPE_CHECKING:
    from ...container import Services

router = APIRouter(prefix="/api/upload", tags=["Upload"])

#: 取り込みの結果・警告に出すファイル名の最大の長さ。
_MAX_NAME = 120

#: Windows の装置の名前。拡張子が付いていても、どのフォルダの中でも装置を指す。
_WINDOWS_DEVICE_RE = re.compile(
    r"CON|PRN|AUX|NUL|CONIN\$|CONOUT\$|COM[0-9¹²³]|LPT[0-9¹²³]", re.IGNORECASE
)


def _upload_name(filename: str | None, table_name: str) -> str:
    """一時フォルダに置くファイル名(送られた名前からフォルダとドライブを除いたもの)。

    取り込みの警告(``data.csv 12行目: ...``)に出るので、送られた名前をなるべく残す。
    どの OS でも Windows の規則(``/`` と ``\\`` の区切り・``D:`` のドライブ)で切り出す。
    Windows で ``D:config.py`` を一時フォルダにつなぐと D ドライブの今のフォルダを指し、
    一時フォルダの外(プロジェクトの config.py など)に書いてしまうため。``:`` を含む名前
    (NTFS の代替データストリーム)・装置の名前(``CON.csv`` など)・点と空白だけの名前は
    テーブル名の ``.csv`` にする。
    """
    name = PureWindowsPath(filename or "").name.strip()[-_MAX_NAME:]
    if (
        not name.strip(". ")
        or ":" in name
        or has_control_chars(name)
        or _WINDOWS_DEVICE_RE.fullmatch(name.split(".", 1)[0].rstrip())
    ):
        return f"{table_name}.csv"
    return name


def _import_upload(
    services: Services,
    source: BinaryIO,
    name: str,
    table_name: str,
    mode: str,
    force: bool,
    actor: str,
) -> TableImportResult:
    """送られた CSV を一時フォルダに置いて取り込み、監査ログに残す(スレッドで呼ぶ)。"""
    with tempfile.TemporaryDirectory(prefix="monitor-upload-") as folder:
        path = Path(folder) / name
        with path.open("wb") as out:
            shutil.copyfileobj(source, out)
        try:
            result = services.csv_importer(path.parent).import_table(
                table_name, path, mode=mode, force=force
            )
        except CsvEncodingError as exc:
            message = exc.message if not exc.hint else f"{exc.message}({exc.hint})"
            raise InvalidPayloadError(message) from exc
    services.audit_service.record(
        table_name,
        None,
        "import",
        after={
            "file": name,
            "mode": mode,
            "inserted": result.inserted,
            "skipped": result.skipped,
            "refused": result.refused,
        },
        actor=actor,
    )
    return result


@router.post(
    "/{table_name}",
    responses={
        404: {"description": "テーブルが config.py に無い"},
        409: {"description": "主キーが重複した(取り込みは取り消した)"},
        422: {"description": "CSV を読めない・値が列に入らない(取り込みは取り消した)"},
    },
)
async def upload_csv(
    request: Request,
    table_name: str,
    file: UploadFile = File(..., description="取り込む CSV ファイル"),
    mode: Literal["append", "replace"] = Form(
        "append",
        description="append=追記(主キーが既にある行は読み飛ばす)、"
        "replace=テーブルの行をすべて CSV の行に置き換える",
    ),
    force: bool = Form(
        False,
        description="replace で、config.py の列が見出しに丸ごと無い CSV でも置き換える"
        "(その列は空になる)",
    ),
    actor: str = Depends(get_actor),
    _: None = Depends(require_admin),
) -> dict[str, Any]:
    """CSV を 1 テーブルへ取り込む。結果(取り込んだ行数・読み飛ばした行・警告)を返す。

    ``refused`` が true なら、見出しに config.py の列が丸ごと無いため置き換えていない
    (既存の行はそのまま。``force`` で置き換えられる)。
    """
    services = request.app.state.services
    if table_name not in services.config.tables:
        raise TableNotFoundError(f"テーブル '{table_name}' は定義されていません")
    name = _upload_name(file.filename, table_name)
    result = await anyio.to_thread.run_sync(
        partial(
            _import_upload,
            services,
            file.file,
            name,
            table_name,
            mode,
            force,
            actor,
        )
    )
    return {"file": name, "mode": mode, **dataclasses.asdict(result)}
