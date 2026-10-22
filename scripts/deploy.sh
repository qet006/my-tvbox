#!/usr/bin/env bash
# 授权后一键部署：建仓库 → 把产物地址换成线上 raw → 推送 → 触发定时任务
# 用法：bash scripts/deploy.sh [仓库名，默认 my-tvbox]
set -euo pipefail

REPO_NAME="${1:-my-tvbox}"
cd "$(dirname "$0")/.."

echo "== 0/5 检查登录 =="
if ! gh auth status >/dev/null 2>&1; then
  echo "未登录 GitHub，请先运行： gh auth login --hostname github.com --git-protocol https --web"
  exit 1
fi
OWNER="$(gh api user -q .login)"
echo "  账号: $OWNER"
RAW="https://raw.githubusercontent.com/$OWNER/$REPO_NAME/main"

echo "== 1/5 建仓库（已存在则跳过） =="
if gh repo view "$OWNER/$REPO_NAME" >/dev/null 2>&1; then
  echo "  仓库已存在: $OWNER/$REPO_NAME"
else
  gh repo create "$REPO_NAME" --public --description "实测驱动的 TVBox 源池：点播四级测活 + 直播深度验活，GitHub Actions 每日自动重建"
  echo "  已创建: $OWNER/$REPO_NAME"
fi

echo "== 2/5 把 README 里的 {owner} 换成真实账号 =="
python - "$OWNER" <<'PY'
import sys, pathlib
owner = sys.argv[1]
p = pathlib.Path("README.md")
t = p.read_text(encoding="utf-8")
t = t.replace("{owner}", owner)
p.write_text(t, encoding="utf-8")
print(f"  README 已替换为 {owner}")
PY

echo "== 3/5 用线上地址重建产物 =="
PUBLIC_BASE="$RAW" python scripts/build_config.py

echo "== 4/5 推送 =="
git -c user.name="my-tvbox-bot" -c user.email="bot@local" add -A
if git diff --cached --quiet; then
  echo "  无变更"
else
  git -c user.name="my-tvbox-bot" -c user.email="bot@local" commit -q -m "chore(deploy): 产物地址切换到 $RAW"
fi
git branch -M main
if git remote get-url origin >/dev/null 2>&1; then
  git remote set-url origin "https://github.com/$OWNER/$REPO_NAME.git"
else
  git remote add origin "https://github.com/$OWNER/$REPO_NAME.git"
fi
git push -u origin main --force

echo "== 5/5 触发一次定时任务（验证 Actions 能跑通） =="
sleep 3
gh workflow run healthcheck.yml --repo "$OWNER/$REPO_NAME" || echo "  （也可稍后在网页手动点 Run workflow）"

cat <<EOF

========================================
完成！

客户端配置地址（任选一条）：

  TVBox Desktop（推荐）
  $RAW/dist/tvbox-desktop.json

  TVBoxPC / 通用
  $RAW/dist/tvbox.json

  TVBoxPC 增强（含 jar 蜘蛛）
  $RAW/dist/tvbox-pc.json

  直播源（单独填）
  $RAW/dist/live.txt

仓库： https://github.com/$OWNER/$REPO_NAME
Actions： https://github.com/$OWNER/$REPO_NAME/actions
========================================
EOF
