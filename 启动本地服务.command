#!/bin/zsh
set -euo pipefail
unsetopt bg_nice 2>/dev/null || true

cd "$(dirname "$0")"

FRONTEND_URL="http://127.0.0.1:5173"
HEALTH_URL="http://127.0.0.1:8000/health"

echo "准备启动 fund_advice 本地服务..."
echo "项目目录：$(pwd)"
echo

if ! command -v uv >/dev/null 2>&1; then
  echo "未找到 uv。请先安装 uv 后再重新双击本脚本。"
  echo "安装说明：https://docs.astral.sh/uv/"
  echo
  read -r "?按回车关闭窗口..."
  exit 1
fi

if ! command -v npm >/dev/null 2>&1; then
  echo "未找到 npm。请先安装 Node.js 24 LTS 后再重新双击本脚本。"
  echo "下载地址：https://nodejs.org/"
  echo
  read -r "?按回车关闭窗口..."
  exit 1
fi

if [[ ! -d ".venv" ]]; then
  echo "未找到 Python 依赖环境，正在自动安装..."
  uv sync --locked
  echo
fi

if [[ ! -d "frontend/node_modules" ]]; then
  echo "未找到前端依赖，正在自动安装..."
  npm --prefix frontend ci
  echo
fi

if curl --silent --fail --max-time 2 "$HEALTH_URL" >/dev/null 2>&1 \
  && curl --silent --fail --max-time 2 "$FRONTEND_URL" >/dev/null 2>&1; then
  echo "本地服务已经在运行，正在打开网页：$FRONTEND_URL"
  open "$FRONTEND_URL"
  exit 0
fi

open_when_ready() {
  local opened=0

  for _ in {1..90}; do
    if curl --silent --fail --max-time 2 "$HEALTH_URL" >/dev/null 2>&1 \
      && curl --silent --fail --max-time 2 "$FRONTEND_URL" >/dev/null 2>&1; then
      echo
      echo "服务已就绪，正在打开网页：$FRONTEND_URL"
      open "$FRONTEND_URL"
      opened=1
      break
    fi
    sleep 1
  done

  if [[ "$opened" -eq 0 ]]; then
    echo
    echo "服务启动时间比预期更久，请留意本窗口输出。"
    echo "你也可以稍后手动打开：$FRONTEND_URL"
  fi
}

open_when_ready &
OPENER_PID=$!

echo "正在启动数据服务和网页服务..."
echo "停止服务：回到这个窗口按 Ctrl+C。"
echo

uv run --locked --offline uvicorn backend.api.main:app --host 127.0.0.1 --port 8000 &
BACKEND_PID=$!

npm --prefix frontend run dev &
FRONTEND_PID=$!

cleanup() {
  for pid in "$OPENER_PID" "$BACKEND_PID" "$FRONTEND_PID"; do
    if kill -0 "$pid" >/dev/null 2>&1; then
      kill "$pid" >/dev/null 2>&1 || true
    fi
  done
}

trap cleanup INT TERM EXIT

while true; do
  if ! kill -0 "$BACKEND_PID" >/dev/null 2>&1; then
    wait "$BACKEND_PID"
    exit $?
  fi

  if ! kill -0 "$FRONTEND_PID" >/dev/null 2>&1; then
    wait "$FRONTEND_PID"
    exit $?
  fi

  sleep 1
done
