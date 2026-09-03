#!/bin/bash

set -euo pipefail
cd "$(dirname "$0")"

pause_on_error() {
  local status=$?
  if [ "$status" -ne 0 ]; then
    echo
    read -r -p "启动失败。按回车键关闭窗口..."
  fi
}
trap pause_on_error EXIT

if ! command -v docker >/dev/null 2>&1; then
  echo "没有找到 Docker。请先安装并启动 Docker Desktop："
  echo "https://www.docker.com/products/docker-desktop/"
  exit 1
fi

if ! docker compose version >/dev/null 2>&1; then
  echo "当前 Docker 没有 Compose 插件，请更新 Docker Desktop。"
  exit 1
fi

if ! docker info >/dev/null 2>&1; then
  echo "正在启动 Docker Desktop..."
  open -a Docker
  for _ in $(seq 1 60); do
    if docker info >/dev/null 2>&1; then
      break
    fi
    sleep 2
  done
fi

if ! docker info >/dev/null 2>&1; then
  echo "Docker Desktop 尚未就绪，请确认它已经完成启动。"
  exit 1
fi

if [ ! -f .env ]; then
  cp .env.example .env
fi

current_key="$(sed -n 's/^DEEPSEEK_API_KEY=//p' .env | head -n 1)"
if [ -z "$current_key" ] || [[ "$current_key" == 填* ]]; then
  echo
  read -r -s -p "请输入你的 DeepSeek API Key（输入内容不会显示）：" api_key
  echo
  if [ -z "$api_key" ] || [[ "$api_key" == *$'\n'* ]]; then
    echo "API Key 不能为空。"
    exit 1
  fi
  temp_env=".env.tmp.$$"
  awk -v key="$api_key" '
    BEGIN { updated = 0 }
    /^DEEPSEEK_API_KEY=/ { print "DEEPSEEK_API_KEY=" key; updated = 1; next }
    { print }
    END { if (!updated) print "DEEPSEEK_API_KEY=" key }
  ' .env > "$temp_env"
  mv "$temp_env" .env
fi
chmod 600 .env 2>/dev/null || true

echo
echo "启动模式："
echo "  1. 普通版（推荐，启动更快）"
echo "  2. OCR 版（用于扫描 PDF，首次下载较大）"
read -r -p "请选择 [1]：" mode

compose_args=(-f compose.yaml)
ocr_enabled=false
if [ "${mode:-1}" = "2" ]; then
  ocr_enabled=true
  compose_args+=(-f compose.ocr.yaml)
fi

echo
echo "正在下载并启动 Paper Parallel Reader..."
if docker compose "${compose_args[@]}" pull; then
  docker compose "${compose_args[@]}" up -d
else
  echo
  read -r -p "预构建镜像下载失败，是否改为在本机编译？这会更慢。[y/N] " build_locally
  if [[ "$build_locally" != "y" && "$build_locally" != "Y" ]]; then
    exit 1
  fi
  if [ "$ocr_enabled" = true ]; then
    compose_args+=(-f compose.build-ocr.yaml)
  else
    compose_args+=(-f compose.build.yaml)
  fi
  docker compose "${compose_args[@]}" up -d --build
fi

port="$(sed -n 's/^APP_PORT=//p' .env | tail -n 1 | tr -d '[:space:]')"
port="${port:-8000}"
url="http://127.0.0.1:${port}/viewer/"

echo "正在等待服务就绪..."
for _ in $(seq 1 90); do
  if curl -fsS "http://127.0.0.1:${port}/api/health" >/dev/null 2>&1; then
    echo "启动成功：${url}"
    open "$url"
    exit 0
  fi
  sleep 2
done

echo "服务启动超时。请执行 docker compose logs reader 查看原因。"
exit 1
