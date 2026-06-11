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
