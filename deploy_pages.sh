#!/usr/bin/env bash
# deploy_pages.sh — 重產報告平台並部署到 GitHub Pages（gh-pages 分支根目錄）。
#   https://autumn-kao-appier.github.io/LazyAdFinder/
#
# 用法：./deploy_pages.sh
# 不動 main 工作區（用 git worktree 操作 gh-pages）。
set -euo pipefail
cd "$(dirname "$0")"

TMP_INDEX="$(mktemp -t ladf_index.XXXXXX).html"
WT="$(mktemp -d -t ladf_ghpages.XXXXXX)"
cleanup() { git worktree remove "$WT" --force 2>/dev/null || true; rm -f "$TMP_INDEX"; }
trap cleanup EXIT

echo "[1/4] 重產平台（standalone 完整 HTML）..."
python3 build_platform.py --out artifact-platform.html --standalone "$TMP_INDEX"

echo "[2/4] 取得 gh-pages 分支 ..."
git fetch -q origin gh-pages
git worktree add -q -B gh-pages "$WT" origin/gh-pages

echo "[3/4] 更新 index.html ..."
cp "$TMP_INDEX" "$WT/index.html"
( cd "$WT"
  git add index.html
  if git diff --cached --quiet; then
    echo "     （內容無變化，跳過提交）"; exit 0
  fi
  git commit -q -m "deploy: 更新 SDK 測試報告平台 ($(date +%Y-%m-%d\ %H:%M))

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
  git push -q origin gh-pages
  echo "     已推送 gh-pages"
)

echo "[4/4] 完成 → https://autumn-kao-appier.github.io/LazyAdFinder/"
echo "     （Pages rebuild 約需 1–2 分鐘）"
