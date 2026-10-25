# Documentation / ドキュメント一覧

How to install, configure, run and deploy Monitor App is in the main
[README.md](../README.md) ([日本語](../README_ja.md)). Docker details:
[docker/README.md](../docker/README.md). Release notes: [CHANGELOG.md](../CHANGELOG.md).
Development: [CONTRIBUTING.md](../CONTRIBUTING.md).

The documents below are in Japanese.

## User guide / 利用ガイド

- [design/integrations_v2_2.md](design/integrations_v2_2.md) — 外部ツール連携ガイド: `sources`(SQLite / JSONL / JSON の取り込み)と procdiag・netdiag・network-checker のプリセット

## For contributors / 開発者向け

- [design/refactor_2_4_ids.md](design/refactor_2_4_ids.md) — コードとテストのコメントに出てくる 2.4 の内部 ID(`PERF-06`・`DX-08`・`D3-01` など)の一覧と意味

## Design notes / 設計ドキュメント(当時の記録)

- [design/redesign_v2.md](design/redesign_v2.md) — v2 再設計: Flask 版から FastAPI + Pydantic への作り直し
- [design/feature_expansion_v2.1.md](design/feature_expansion_v2.1.md) — v2.1 製造現場向け機能(自動取り込み・アラート・大型表示・SPC グラフ・KPI・入力フォーム・監査ログ)
- [design/integrations_v2_2.md](design/integrations_v2_2.md) — v2.2 外部ツール連携(上の利用ガイドと同じ文書)
- [design/ui_redesign_v2_3.md](design/ui_redesign_v2_3.md) — v2.3 UI 刷新: ダッシュボード・ダークテーマ・ビューページ
- [design/improvement_review_2026-06.md](design/improvement_review_2026-06.md) — 2026-06 のコード改善レビュー(2.3.0 の修正のもとになった指摘)

## Work-package specs / 実装タスク(当時の記録)

v2.2(T)と v2.3(U)を実装したときの作業指示書です。文中のファイル構成やコマンドは当時のものです。

- [design/tasks/00-overview.md](design/tasks/00-overview.md) — v2.2 外部ツール出力の可視化: 実装計画と共通の契約
- [design/tasks/T01-foundation.md](design/tasks/T01-foundation.md) — T01 基盤: SourceDef・IngestStateStore・integrations.merge
- [design/tasks/T02-connectors.md](design/tasks/T02-connectors.md) — T02 コネクタ: sqlite / jsonl / json ソースの取り込み
- [design/tasks/T03-watcher-cli.md](design/tasks/T03-watcher-cli.md) — T03 IngestWatcher の一般化・`sync-sources`・アプリへの組み込み
- [design/tasks/T04-procdiag-preset.md](design/tasks/T04-procdiag-preset.md) — T04 procdiag(ホストリソース診断)のプリセット
- [design/tasks/T05-netdiag-preset.md](design/tasks/T05-netdiag-preset.md) — T05 netdiag(ネットワーク診断 watch アラート)のプリセット
- [design/tasks/T06-netcheck-preset.md](design/tasks/T06-netcheck-preset.md) — T06 network_checker(ping 監視 DB)のプリセット
- [design/tasks/T07-docs-scaffold.md](design/tasks/T07-docs-scaffold.md) — T07 v2.2 のドキュメント・scaffold 更新
- [design/tasks/U00-overview.md](design/tasks/U00-overview.md) — v2.3 UI 刷新: 実装計画
- [design/tasks/U01-foundation.md](design/tasks/U01-foundation.md) — U01 基盤: group・DashboardConfig・ページのコンテキスト
- [design/tasks/U02-base-theme.md](design/tasks/U02-base-theme.md) — U02 ベースレイアウト: サイドバー・ダークテーマ
- [design/tasks/U03-dashboard.md](design/tasks/U03-dashboard.md) — U03 トップページのダッシュボード化
- [design/tasks/U04-viewpage.md](design/tasks/U04-viewpage.md) — U04 ビューページ: グラフ主役・折りたたみテーブル・数値整形
- [design/tasks/U05-docs.md](design/tasks/U05-docs.md) — U05 v2.3 のドキュメント・scaffold 更新

## Legacy / 旧バージョン

- [legacy/qiita_usage_guide.md](legacy/qiita_usage_guide.md) — v0.x(Flask 版)の紹介記事。v2 以降には当てはまりません
