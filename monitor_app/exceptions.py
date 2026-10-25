"""ドメイン例外。API 層で HTTP レスポンスに変換される(api/errors.py)。

サービス層が API 層に依存しないよう、例外型は中立なこのモジュールに置く。
"""

from __future__ import annotations


class MonitorAppError(Exception):
    """全ドメイン例外の基底。``code`` は機械可読なエラーコード。"""

    code = "MONITOR_APP_ERROR"
    status_code = 500

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class TableNotFoundError(MonitorAppError):
    code = "TABLE_NOT_FOUND"
    status_code = 404


class ViewNotFoundError(MonitorAppError):
    code = "VIEW_NOT_FOUND"
    status_code = 404


class RecordNotFoundError(MonitorAppError):
    code = "RECORD_NOT_FOUND"
    status_code = 404


class InvalidPayloadError(MonitorAppError):
    code = "INVALID_PAYLOAD"
    status_code = 422


class QueryExecutionError(MonitorAppError):
    code = "QUERY_EXECUTION_ERROR"
    status_code = 500


class ConflictError(MonitorAppError):
    """主キーの重複など、既存のデータと衝突して書き込めない。"""

    code = "CONFLICT"
    status_code = 409


class SchemaMismatchError(MonitorAppError):
    """データベースの表定義が config.py と一致しない(列・テーブル・主キーの欠落など)。

    500 系だが、利用者が直せる設定の問題なので案内(``monitor-app check``)を返す。
    """

    code = "DB_SCHEMA_MISMATCH"
    status_code = 500


class BackupError(MonitorAppError):
    """バックアップ・復元ができない(SQLite 以外の DB、ファイルの誤りなど)。"""

    code = "BACKUP_ERROR"
    status_code = 500


class CsvEncodingError(MonitorAppError):
    """CSV を設定の文字コード(``MONITOR_CSV_ENCODING``)で読めない。

    ``hint`` は利用者向けの対処(CLI はメッセージの下に表示する)。
    """

    code = "CSV_ENCODING_ERROR"
    status_code = 500

    def __init__(self, message: str, hint: str | None = None) -> None:
        super().__init__(message)
        self.hint = hint


class CsvFormatError(CsvEncodingError):
    """CSV の形式として読めない(見出しの行が壊れているなど)。``hint`` は対処。"""

    code = "CSV_FORMAT_ERROR"
