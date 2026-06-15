# AI 導入プロンプト — push 前セキュリティ/品質ゲート

このファイルは、**対象リポジトリで動かす AI コーディングエージェント（Claude Code 等）に
貼り付ける用のプロンプト**です。

使い方:
1. このバンドル（`precommit-bundle/`）を対象リポジトリから参照できる場所に置く。
2. 下の「===== プロンプト =====」以降をコピーし、`{{BUNDLE_PATH}}` を実際のパスに置換。
3. 対象リポジトリのルートで起動した AI エージェントに貼り付けて実行させる。

> 注意: このプロンプトはツールの**導入と調整・検証まで**を AI に任せるものです。
> commit / push は人間の許可を得てから行うよう指示しています。

---

===== プロンプト =====

あなたはこのリポジトリに「**push 前セキュリティ/品質ゲート**」を導入する担当エンジニアです。
ゴールは、**credential（秘密情報）・社用/別 identity のメール・AI エージェントツールの痕跡**が
push される前に検出・ブロックされる状態にすること。バンドルは `{{BUNDLE_PATH}}` にあります
（`install.sh` / `scripts/` / `pre-commit-config.yaml` / `README.md` を含む）。

## 前提の確認
- カレントディレクトリが git リポジトリで、Python/poetry プロジェクトであることを確認する。
- 既存の `.pre-commit-config.yaml`、`pyproject.toml`（black/ruff の有無・バージョン）、CI 設定
  （`.github/workflows/*`）を読み、現状を把握する。

## 手順
1. **インストール実行**: `{{BUNDLE_PATH}}/install.sh .` を実行する。これは
   `scripts/` 配置 → `.pre-commit-config.yaml` 設置（既存は上書きせず `*.bundle` に退避）
   → `pre-commit` を poetry の dev 依存へ → commit / pre-push フック有効化 → `--all` 監査、
   までを行う。失敗したら原因を要約して停止する。
2. **既存 config のマージ**: `.pre-commit-config.yaml.bundle` が作られた場合は、既存 config に
   gitleaks・detect-private-key・check-added-large-files・check-merge-conflict と、local フック
   （`check-commit-identity` / `check-ai-traces` / `check-ai-traces-commits`）を統合し、`.bundle` を削除する。
3. **バージョン整合**: `.pre-commit-config.yaml` の `black` / `ruff` の `rev` を、このリポジトリが
   実際に使う black/ruff のバージョン（pyproject の dev 依存や CI）に**一致**させる。食い違いは
   ローカルと CI で整形結果がズレる原因になる。
4. **ruff スコープの確認（重要）**: `pyproject.toml` に `[tool.ruff.lint] select` が無い場合、ruff は
   デフォルト規則を適用し、未定義名(F821)など想定外に広く失敗する。リポジトリの方針を尊重し、
   最低限 import 整列の `select = ["I"]` を設定するか、既存方針があればそれに合わせる。判断がつか
   なければユーザーに確認する。
5. **identity 許可リスト**: `git config user.email` を確認し、`scripts/check_commit_identity.py` の
   `ALLOWED_EXACT`（または環境変数 `MONITOR_ALLOWED_EMAILS`）が、このリポジトリで許可すべき
   メールのみを通す内容になっているか調整する。社用メール等は弾く方針にする。
6. **検出対象ツールの調整**: 必要に応じて `scripts/check_ai_traces.py` の `TOOLS`、
   環境変数 `MONITOR_AI_TRACE_EXTRA`、除外設定 `.ai-trace-ignore` を調整する。
7. **検証**:
   - `pre-commit run --all-files` を実行（整形/lint/秘密スキャン）。整形差分が出たら内容を確認・報告。
   - `python3 scripts/check_ai_traces.py --all` を実行し、**exit 0（痕跡なし）**を確認。痕跡があれば一覧で報告。
   - 一時ファイルに検出対象文字列を入れて commit がブロックされること、正常な変更の commit が
     通ることを軽く確認し、**作った一時ファイル・テストコミットは必ず後始末**する。
8. **安全**:
   - **commit / push はユーザーの明示的な許可を得てから**行う。勝手に push しない。
   - 既存ファイル（特に既存 `.pre-commit-config.yaml`）を破壊しない。
   - 変更内容（追加ファイル・pyproject の dev 依存追加・調整した設定）を要約して提示する。

## 完了条件（このチェックを満たすこと）
- [ ] `.pre-commit-config.yaml` に gitleaks / detect-private-key / check-added-large-files /
      check-merge-conflict / black / ruff / check-commit-identity / check-ai-traces(+commits) が入っている
- [ ] `pre-commit install` 済みで **commit と pre-push の両フック**が有効
- [ ] black/ruff のバージョンがこのリポジトリの CI と一致している
- [ ] `python3 scripts/check_ai_traces.py --all` が **exit 0**
- [ ] 一時的に作ったテスト痕跡/コミットを残していない
- [ ] commit / push はユーザー許可を得てから実施（または保留して報告）

## 前提知識（踏まえること）
- ローカルフックは `git commit/push --no-verify` で回避可能で、クローンごとに `pre-commit install`
  が必要。回避不可の担保が要るなら、同じ gitleaks を GitHub Actions の必須チェックにする
  「CI バックストップ」を併用する（このプロンプトの範囲外。必要ならユーザーに提案）。
- 秘密スキャナはファイル内容のみを検査し、コミットの著者メタデータは見ない（そこは
  `check_commit_identity.py` が担当）。
- 検出対象ツール名を**正当に**含むファイル（ドキュメント等）は `.ai-trace-ignore` に追加して
  誤検出を避ける。

===== プロンプトここまで =====
