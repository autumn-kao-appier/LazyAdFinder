#!/usr/bin/env bash
# deploy_pages.sh — 重產報告平台並部署到 GitHub Pages（gh-pages 分支根目錄）。
#   https://autumn-kao-appier.github.io/LazyAdFinder/
#
# 用法：./deploy_pages.sh
# 不動 main 工作區（用 git worktree 操作 gh-pages）。
set -euo pipefail
cd "$(dirname "$0")"

# mktemp 不會加副檔名；建立後改名成 .html，讓 cleanup 刪到的正是實際檔案（否則每次洩漏一個 temp）。
TMP_INDEX="$(mktemp -t ladf_index.XXXXXX)"
mv "$TMP_INDEX" "$TMP_INDEX.html"
TMP_INDEX="$TMP_INDEX.html"
WT="$(mktemp -d -t ladf_ghpages.XXXXXX)"
cleanup() { git worktree remove "$WT" --force 2>/dev/null || true; rm -f "$TMP_INDEX"; }
trap cleanup EXIT

echo "[1/4] 重產平台（standalone 完整 HTML）..."
python3 build_platform.py --out artifact-platform.html --standalone "$TMP_INDEX"

# sanity gate：壞的 discovery（如全部誤判成空 cell）產出的退化頁面不得覆蓋線上好頁面。
MIN_BYTES=51200
size=$(wc -c < "$TMP_INDEX")
if [ "$size" -lt "$MIN_BYTES" ]; then
  echo "  [中止] 產出的 index 只有 ${size} bytes（< ${MIN_BYTES}），疑似退化，未部署。" >&2
  exit 1
fi
if ! grep -q "已就緒" "$TMP_INDEX"; then
  echo "  [中止] 產出的平台沒有任何『已就緒』卡片（0 個 live report），未部署。" >&2
  exit 1
fi

echo "[2/4] 取得 gh-pages 分支 ..."
git fetch -q origin gh-pages
git worktree prune                 # 清掉前次 crash 殘留的 gh-pages worktree 註冊，避免 add 失敗
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
