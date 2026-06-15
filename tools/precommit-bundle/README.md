# precommit-bundle — push 前セキュリティ/品質チェックの配布版

monitor-app で使っている **push 前のローカルゲート**を、他の Python/poetry
リポジトリへ「コピーして導入」するための自己完結バンドルです。

## 中身

| ファイル | 役割 |
|---|---|
| `pre-commit-config.yaml` | gitleaks / detect-private-key / large-files / merge-conflict / black / ruff / 自作2フック |
| `scripts/check_commit_identity.py` | author/committer メールの許可リスト検査（pre-push） |
| `scripts/check_ai_traces.py` | AI エージェントツールの痕跡検出（commit + pre-push、`--all` 監査） |
| `install.sh` | 上記を対象リポジトリへ配置し pre-commit を有効化 |

## 導入

```sh
# 対象リポジトリのルートで（このバンドルを置いた場所から実行）
/path/to/precommit-bundle/install.sh
# もしくは対象パスを引数で
/path/to/precommit-bundle/install.sh /path/to/other-repo
```

`install.sh` は次を行います:
1. `scripts/check_*.py` を対象の `scripts/` へコピー
2. `.pre-commit-config.yaml` を配置（既存なら `*.bundle` として保存し手動マージを促す）
3. `.ai-trace-ignore` 雛形を作成（無ければ）
4. `pre-commit` を導入（poetry があれば dev 依存へ）し commit / pre-push フックを有効化
5. `python3 scripts/check_ai_traces.py --all` で現状を監査

## 導入先での調整

- **black / ruff の `rev`** を対象リポジトリの CI バージョンに合わせる（食い違い防止）。
- **メール許可リスト**: `scripts/check_commit_identity.py` の `ALLOWED_EXACT` か、
  環境変数 `MONITOR_ALLOWED_EMAILS="you@example.com,..."`。
- **検出対象ツール**: `scripts/check_ai_traces.py` の `TOOLS`、`MONITOR_AI_TRACE_EXTRA`、
  除外は `.ai-trace-ignore`。

## 限界

- ローカルフックは `git commit/push --no-verify` で回避可能・クローンごとに
  `pre-commit install` が必要。確実な担保には同じ gitleaks を CI 必須チェックに
  入れる「CI バックストップ」を併用してください。
- 秘密スキャナはファイル内容が対象で、コミットの著者メタデータは見ません
  （そこは `check_commit_identity.py` が担当）。
