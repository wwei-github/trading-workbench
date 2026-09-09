#!/usr/bin/env bash
# ============================================================
# Trading Workbench 一键部署脚本
# 用法: ./deploy.sh [up|down|build|logs|restart|status]
#   up      - 构建并启动所有服务（默认）
#   down    - 停止并移除所有容器
#   build   - 仅重新构建镜像（不启动）
#   logs    - 查看所有服务日志（跟随）
#   restart - 重启所有服务
#   status  - 查看服务状态
# ============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# ---------- 颜色输出 ----------
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
CYAN='\033[0;36m'
NC='\033[0m'

info()  { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*"; }
step()  { echo -e "${CYAN}==>${NC} $*"; }

# ---------- 前置检查 ----------
check_prerequisites() {
    step "检查 Docker 环境..."
    if ! command -v docker &>/dev/null; then
        error "未检测到 docker，请先安装 Docker Desktop: https://www.docker.com/products/docker-desktop"
        exit 1
    fi
    if ! docker info &>/dev/null 2>&1; then
        error "Docker 守护进程未运行，请先启动 Docker Desktop"
        exit 1
    fi
    if ! command -v docker-compose &>/dev/null && ! docker compose version &>/dev/null 2>&1; then
        error "未检测到 docker-compose，请安装 Docker Compose"
        exit 1
    fi
    info "Docker 环境检查通过"
}

# 兼容 docker-compose v1 和 docker compose v2
compose_cmd() {
    if docker compose version &>/dev/null 2>&1; then
        docker compose "$@"
    else
        docker-compose "$@"
    fi
}

# ---------- 命令实现 ----------
cmd_up() {
    check_prerequisites
    step "开始构建并启动服务..."
    compose_cmd up -d --build
    info "服务已启动"
    echo ""
    step "访问地址:"
    echo "  前端:  http://localhost:8088"
    echo "  后端:  http://localhost:8082"
    echo "  API 文档: http://localhost:8082/docs"
    echo ""
    step "查看日志: ./deploy.sh logs"
}

cmd_down() {
    step "停止并移除容器..."
    compose_cmd down
    info "已停止"
}

cmd_build() {
    check_prerequisites
    step "重新构建镜像..."
    compose_cmd build --no-cache
    info "构建完成"
}

cmd_logs() {
    step "查看日志（Ctrl+C 退出）..."
    compose_cmd logs -f --tail=100
}

cmd_restart() {
    step "重启服务..."
    compose_cmd restart
    info "已重启"
}

cmd_status() {
    step "服务状态:"
    compose_cmd ps
}

# ---------- 入口 ----------
ACTION="${1:-up}"
case "$ACTION" in
    up)      cmd_up ;;
    down)   cmd_down ;;
    build)  cmd_build ;;
    logs)   cmd_logs ;;
    restart) cmd_restart ;;
    status) cmd_status ;;
    *)
        echo "用法: $0 [up|down|build|logs|restart|status]"
        echo "  up      - 构建并启动所有服务（默认）"
        echo "  down    - 停止并移除所有容器"
        echo "  build   - 重新构建镜像（不启动）"
        echo "  logs    - 查看日志"
        echo "  restart - 重启所有服务"
        echo "  status  - 查看服务状态"
        exit 1
        ;;
esac
