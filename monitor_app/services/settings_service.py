"""画面(設定ページ)での設定の編集。

config.py の設定(``base``)に、画面で変えた差分(:mod:`~monitor_app.settings.overrides`)を
重ねたものを「使う設定」(:attr:`SettingsEditor.config`)とする。差分は DB の
``_settings`` 表に保存し、再起動しても残る。

保存の前に ``monitor-app check`` と同じ点検(ビュー・KPI の SQL の空実行・列の照合・
通知先)を行い、エラーがあれば保存しない。画面から入れた SQL と KPI の表示形式は、
さらに次のものを断る(config.py と同じ値なら対象外。サーバーの管理者が書いたもの
だから):

- 複数の文を含む SQL(読み取り専用の制限を抜けられる — :func:`has_multiple_statements`)
- 値の属性・添字を読む表示形式(``{0.__class__}`` など。値の書式だけを許す)

保存とリセットは 1 つずつ行い、画面が読み込んだときの版(:func:`config_revision`)と
違えば保存しない(ほかの人が先に保存した変更を、古い画面の内容で上書きしない)。
"""

from __future__ import annotations

import logging
import string
import threading
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from ..checks import Issue, check_ui_settings, has_errors, sort_issues
from ..db.settings_store import SettingsStore
from ..exceptions import ConflictError
from ..security import has_multiple_statements
from ..settings.declarative import MonitorConfig, collect_config_warnings
from ..settings.infer import UNSAFE_NAME_CHARS, has_control_chars
from ..settings.loader import validation_messages
from ..settings.overrides import (
    apply_overrides,
    build_config,
    config_revision,
    diff_patch,
    editable_settings,
)
from ..settings.runtime import AppSettings
from .audit_service import AuditService
from .notifiers import KNOWN_CHANNELS, build_notifiers

logger = logging.getLogger("monitor_app.settings")

#: ``_settings`` 表で、画面で変えた設定(config.py との差分)を保存する名前。
SETTINGS_KEY = "ui"


class StaleSettingsError(ConflictError):
    """画面が読み込んだ後に、ほかの人が設定を保存した(読み込み直してから保存する)。"""

    code = "STALE_SETTINGS"

    def __init__(self) -> None:
        super().__init__(
            "ほかの人(またはほかの画面)が先に設定を保存しました。"
            "読み込み直してから変更してください"
        )


@dataclass
class EditResult:
    """点検・保存の結果。"""

    #: 点検に通った設定(エラーがあれば None。保存しない)。
    config: MonitorConfig | None
    #: 見つかった問題(重要度の順)。
    issues: list[Issue] = field(default_factory=list)
    #: ビュー名 → 結果の列名(画面の入力欄の候補)。
    view_columns: dict[str, list[str]] = field(default_factory=dict)


def _plain_format(fmt: str) -> bool:
    """値の書式(``{:.1f}`` など)だけの表示形式か(属性・添字・入れ子の書式を使わない)。

    書式として読めない形式は True(KPI は値をそのまま表示し、点検が警告する)。
    """
    try:
        fields = [
            (name, spec, conversion)
            for _text, name, spec, conversion in string.Formatter().parse(fmt)
            if name is not None
        ]
    except ValueError:
        return True
    if sum(1 for name, _spec, _conv in fields if name == "") > 1:
        return False  # "{}{}" は 2 つ目の値が無く、表示に失敗する
    return all(
        name in ("", "0") and conversion is None and "{" not in (spec or "")
        for name, spec, conversion in fields
    )


def _usable_name(name: str) -> bool:
    """画面で追加するビュー・KPI の名前として使えるか(URL・SQL・一括取得を壊さない)。"""
    return (
        bool(name)
        and name == name.strip()
        and not any(ch in UNSAFE_NAME_CHARS for ch in name)
        and not has_control_chars(name)
    )


class SettingsEditor:
    """config.py の設定に画面で変えた差分を重ね、点検・保存・リセットを行う。"""

    def __init__(
        self,
        base: MonitorConfig,
        store: SettingsStore,
        settings: AppSettings,
        audit: AuditService | None = None,
    ) -> None:
        self.base = base
        self.store = store
        self.settings = settings
        self.audit = audit
        #: config.py の設定のうち画面で編集できる項目(差分の基準。動いている間は変わらない)。
        self._base_settings = editable_settings(base)
        self.config = base
        #: 保存してある差分(無ければ None)。
        self.overrides: dict[str, Any] | None = None
        self.updated_at: str | None = None
        self.updated_by: str | None = None
        #: 保存してある差分を使えなかった理由(使えていれば None)。
        self.load_error: str | None = None
        self._lock = threading.Lock()
        self._load()

    # ------------------------------------------------------------------
    # 読み込み
    # ------------------------------------------------------------------
    def _load(self) -> None:
        try:
            stored = self.store.load(SETTINGS_KEY)
        except (SQLAlchemyError, ValueError) as exc:
            self._fail_load(f"保存してある内容を読めません({type(exc).__name__})")
            return
        if stored is None:
            return
        self.overrides = stored.value
        self.updated_at, self.updated_by = stored.updated_at, stored.updated_by
        try:
            with collect_config_warnings(display=False):
                self.config = apply_overrides(self.base, stored.value)
        except ValidationError as exc:
            self._fail_load("; ".join(validation_messages(exc)))

    def _fail_load(self, reason: str) -> None:
        self.load_error = (
            "画面で変えた設定を config.py の設定に重ねられないため、config.py の設定"
            f"だけで動いています: {reason}"
        )
        logger.warning("%s", self.load_error)

    # ------------------------------------------------------------------
    # 画面に渡す内容
    # ------------------------------------------------------------------
    @property
    def config(self) -> MonitorConfig:
        """使う設定(config.py + 画面で変えた差分)。"""
        return self._config

    @config.setter
    def config(self, config: MonitorConfig) -> None:
        self._config = config
        #: 使う設定の版(画面の再読み込み・保存の競合の判定に使う)。
        self.revision = config_revision(config)

    def snapshot(self) -> dict[str, Any]:
        """設定ページが読む内容(使う設定・config.py の設定・入力欄の候補など)。"""
        notifiers = build_notifiers(self.settings)
        return {
            "revision": self.revision,
            "settings": editable_settings(self.config),
            "config_py": self._base_settings,
            "customized": bool(self.overrides),
            "updated_at": self.updated_at,
            "updated_by": self.updated_by,
            "load_error": self.load_error,
            "tables": list(self.base.tables),
            "notify_channels": [
                {"name": name, "ready": name in notifiers} for name in KNOWN_CHANNELS
            ],
        }

    # ------------------------------------------------------------------
    # 点検・保存
    # ------------------------------------------------------------------
    def check(self, desired: dict[str, Any]) -> EditResult:
        """画面の内容 ``desired`` から設定を組み立てて点検する(保存はしない)。"""
        with collect_config_warnings(display=False) as found:
            try:
                config = build_config(self.base, desired)
            except ValidationError as exc:
                return EditResult(
                    None,
                    [
                        Issue("error", "invalid-setting", line)
                        for line in validation_messages(exc)
                    ],
                )
        issues = [Issue("warning", "unknown-key", str(w)) for w in found]
        issues.extend(self._untrusted_issues(config))
        checked, columns = check_ui_settings(config, self.settings)
        issues.extend(checked)
        issues = sort_issues(issues)
        return EditResult(None if has_errors(issues) else config, issues, columns)

    def _untrusted_issues(self, config: MonitorConfig) -> list[Issue]:
        """画面から入れた(config.py と違う)SQL と表示形式の問題。"""
        issues: list[Issue] = []
        for kind, items, originals in (
            ("ビュー", config.views, self.base.views),
            ("KPI", config.kpis, self.base.kpis),
        ):
            for name, item in items.items():
                original = originals.get(name)
                if original is None and not _usable_name(name):
                    issues.append(
                        Issue(
                            "error",
                            "invalid-name",
                            f"{kind}の名前 '{name}' は使えません",
                            "空欄・前後の空白と、' \" / \\ , : # ? % は使えません"
                            "(URL と SQL を壊すため。英数字と _ をおすすめします)",
                        )
                    )
                if original is not None and original.query == item.query:
                    continue
                if has_multiple_statements(item.query):
                    issues.append(
                        Issue(
                            "error",
                            "sql-multiple-statements",
                            f"{kind} '{name}' の SQL に複数の文があります"
                            "(; で区切られています)",
                            "画面からは SELECT 文を 1 つだけ保存できます。文字列の中の ; は"
                            " ' で囲んでください",
                        )
                    )
        for name, card in config.kpis.items():
            original = self.base.kpis.get(name)
            if original is not None and original.format == card.format:
                continue
            if not _plain_format(card.format):
                issues.append(
                    Issue(
                        "error",
                        "kpi-format-fields",
                        f"KPI '{name}' の表示形式 '{card.format}' は使えません"
                        "(値の書式だけを書けます)",
                        "例: '{:.0f}'(整数)・'{:.1f} %'(小数 1 桁)・'{:,.0f}'(桁区切り)",
                    )
                )
        return issues

    def save(
        self, desired: dict[str, Any], actor: str, *, revision: str | None = None
    ) -> EditResult:
        """点検に通れば、config.py との差分を保存して使う設定を差し替える。

        ``revision`` が今の版と違えば :class:`StaleSettingsError`(ほかの人が先に保存した)。
        画面の内容が config.py と同じなら、保存してある差分を消す。
        """
        with self._lock:
            if revision is not None and revision != self.revision:
                raise StaleSettingsError
            result = self.check(desired)
            if result.config is None:
                return result
            patch = diff_patch(self._base_settings, editable_settings(result.config))
            self._store(patch or None, actor, action="update")
            self.config = result.config
            return result

    def reset(self, actor: str, *, revision: str | None = None) -> None:
        """画面で変えた設定を消し、config.py の設定に戻す。"""
        with self._lock:
            if revision is not None and revision != self.revision:
                raise StaleSettingsError
            self._store(None, actor, action="reset")
            self.config = self.base

    def _store(self, patch: dict[str, Any] | None, actor: str, *, action: str) -> None:
        before = self.overrides
        if patch:
            stored = self.store.save(SETTINGS_KEY, patch, actor)
            self.updated_at, self.updated_by = stored.updated_at, stored.updated_by
        else:
            self.store.delete(SETTINGS_KEY)
            self.updated_at = self.updated_by = None
        self.overrides = patch
        self.load_error = None
        if self.audit is not None:
            self.audit.record(
                "_settings",
                SETTINGS_KEY,
                action,
                before=before,
                after=patch,
                actor=actor,
            )
        logger.info("画面で設定を変更しました(%s, %s)", action, actor)
