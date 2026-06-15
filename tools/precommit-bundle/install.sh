#!/usr/bin/env bash
# push 前セキュリティ/品質チェックを別リポジトリへ展開するインストーラ。
#
#   使い方:  tools/precommit-bundle/install.sh [対象リポジトリのパス]
#            （省略時はカレントディレクトリ。対象は git リポジトリであること）
#
# 行うこと:
#   1. check_commit_identity.py / check_ai_traces.py を <target>/scripts/ へ配置
#   2. .pre-commit-config.yaml を配置（既存なら上書きせず .bundle として保存）
#   3. .ai-trace-ignore が無ければ雛形を作成
#   4. pre-commit を導入（poetry があれば dev 依存に追加）してフックを有効化
#   5. python3 scripts/check_ai_traces.py --all で現状を監査
set -euo pipefail

BUNDLE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET="$(cd "${1:-$PWD}" && pwd)"

say() { printf '\033[1m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[33m[!] %s\033[0m\n' "$*"; }
die() {
	printf '\033[31m[x] %s\033[0m\n' "$*" >&2
	exit 1
}

git -C "$TARGET" rev-parse --git-dir >/dev/null 2>&1 ||
	die "対象がgitリポジトリではありません: $TARGET"
say "対象リポジトリ: $TARGET"

# 1) スクリプト配置 ----------------------------------------------------------
say "scripts/ を配置"
mkdir -p "$TARGET/scripts"
cp "$BUNDLE_DIR/scripts/check_commit_identity.py" "$TARGET/scripts/"
cp "$BUNDLE_DIR/scripts/check_ai_traces.py" "$TARGET/scripts/"
chmod +x "$TARGET/scripts/check_commit_identity.py" "$TARGET/scripts/check_ai_traces.py"

# 2) pre-commit 設定 ---------------------------------------------------------
if [ -f "$TARGET/.pre-commit-config.yaml" ]; then
	cp "$BUNDLE_DIR/pre-commit-config.yaml" "$TARGET/.pre-commit-config.yaml.bundle"
	warn ".pre-commit-config.yaml が既にあります。上書きせず .pre-commit-config.yaml.bundle"
	warn "として保存しました。local フック(check-commit-identity / check-ai-traces*)を"
	warn "手動でマージしてください。"
else
	cp "$BUNDLE_DIR/pre-commit-config.yaml" "$TARGET/.pre-commit-config.yaml"
	say ".pre-commit-config.yaml を配置"
fi

# 3) .ai-trace-ignore 雛形 ---------------------------------------------------
if [ ! -f "$TARGET/.ai-trace-ignore" ]; then
	cat >"$TARGET/.ai-trace-ignore" <<'EOF'
# check_ai_traces の検査から除外するパス（1 行 1 glob、# はコメント）。
# 例: ツール名を正当に含むドキュメントを除外する場合
# docs/ai-tooling-notes.md
EOF
	say ".ai-trace-ignore 雛形を作成"
fi

# 4) pre-commit 導入 + フック有効化 ------------------------------------------
if command -v pre-commit >/dev/null 2>&1; then
	PC=(pre-commit)
elif command -v poetry >/dev/null 2>&1 && [ -f "$TARGET/pyproject.toml" ]; then
	say "pre-commit を poetry の dev 依存へ追加"
	(cd "$TARGET" && poetry add --group dev pre-commit >/dev/null)
	PC=(poetry run pre-commit)
else
	die "pre-commit が見つかりません。pipx install pre-commit などで導入後に再実行してください。"
fi

say "フックを有効化（pre-commit + pre-push）"
(cd "$TARGET" && "${PC[@]}" install --install-hooks)
# gitleaks / pre-commit-hooks を最新タグへ（black/ruff は据え置き、失敗は無視）
(cd "$TARGET" && "${PC[@]}" autoupdate \
	--repo https://github.com/gitleaks/gitleaks \
	--repo https://github.com/pre-commit/pre-commit-hooks >/dev/null 2>&1) || true

# 5) 現状監査 ----------------------------------------------------------------
say "全履歴の痕跡監査（python3 scripts/check_ai_traces.py --all）"
if (cd "$TARGET" && python3 scripts/check_ai_traces.py --all); then
	say "監査クリーン（痕跡なし）"
else
	warn "既存の痕跡を検出しました（上記参照）。"
fi

cat <<EOF

✅ 導入完了。
次の対応:
  - git の identity を確認: git -C "$TARGET" config user.email
    許可リスト外なら scripts/check_commit_identity.py を編集するか
    MONITOR_ALLOWED_EMAILS=you@example.com を設定。
  - 検出対象ツールの調整: scripts/check_ai_traces.py の TOOLS / MONITOR_AI_TRACE_EXTRA
  - 全体整形チェック: (cd "$TARGET" && ${PC[*]} run --all-files)
  - black/ruff の rev は対象リポジトリの CI バージョンに合わせてください。
注意: ローカルフックは --no-verify で回避可能。確実化には CI 必須チェック併用を推奨。
EOF
