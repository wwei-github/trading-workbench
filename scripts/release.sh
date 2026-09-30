#!/usr/bin/env bash
# ============================================================
# 服务器端发布脚本（CICD 2026-09-19）
# 用法: scripts/release.sh deploy  <backend|frontend|all>   # 拉代码→SHA 构建→up→健康检查→失败自动回滚
#       scripts/release.sh rollback [<sha>]               # 回滚到指定 SHA（默认上一个成功版本）
#       scripts/release.sh status                          # 当前部署版本与镜像列表
#
# 设计：
# - 镜像按 git SHA 打标签（compose 里 IMAGE_TAG），回滚 = 切标签 + up -d，秒级、免重建
# - 构建失败时旧容器未动，无需回滚；up 之后的健康检查失败才触发自动回滚
# - 健康检查：容器 Up + 前端/后端 HTTP 200 + beat 启动日志无崩溃（防 DuplicateTable 类事故）
# - .deploy_state 记录最近一次成功 SHA 与上一版 SHA（仅服务器本地，不入库）
# ============================================================
set -euo pipefail

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; CYAN='\033[0;36m'; NC='\033[0m'
info()  { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*"; }
step()  { echo -e "${CYAN}==>${NC} $*"; }

# 服务器仓库根目录（脚本位于 repo/scripts/ 下）
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"

STATE_FILE=".deploy_state"          # 记录 LAST_SHA 和 PREV_SHA
BACKEND_SERVICES="backend celery_worker celery_beat"
FRONTEND_SERVICES="frontend"

services_for_scope() {
    case "$1" in
        backend)  echo "$BACKEND_SERVICES" ;;
        frontend) echo "$FRONTEND_SERVICES" ;;
        all)      echo "$BACKEND_SERVICES $FRONTEND_SERVICES" ;;
        *)        error "未知 scope: $1（可选 backend|frontend|all）"; exit 2 ;;
    esac
}

compose() { docker compose "$@"; }

# 健康检查：参数为本次部署的 scope（决定检查哪些项）。
# bash 惯例：返回 0 = 健康，非 0 = 存在失败项
health_check() {
    local scope="$1" failed=0
    step "健康检查（scope=$scope）..."

    # 1. 容器全部 Up
    local down
    down=$(compose ps --format '{{.Name}} {{.Status}}' | grep -v 'Up ' | grep -v '^$' || true)
    if [ -n "$down" ]; then
        error "存在非 Up 容器:"
        echo "$down"
        failed=1
    fi

    # 2. 后端 API（backend 8082 直连）——backend/all 或纯前端部署都查（frontend 依赖 backend）
    if ! curl -sf -o /dev/null --max-time 10 http://localhost:8082/api/scans/config; then
        error "后端 API /api/scans/config 探活失败"
        failed=1
    fi

    # 3. 前端首页（frontend 8088）——frontend/all 才查
    if [ "$scope" != "backend" ]; then
        if ! curl -sf -o /dev/null --max-time 10 http://localhost:8088/; then
            error "前端首页 :8088 探活失败"
            failed=1
        fi
    fi

    # 4. beat 启动后日志无崩溃特征（仅 backend/all）
    if [ "$scope" != "frontend" ]; then
        sleep 20  # 给 beat 一点启动时间再翻日志
        if compose logs celery_beat --since 3m 2>/dev/null | grep -qiE 'Traceback|DuplicateTable|crashed'; then
            error "celery_beat 启动日志发现崩溃特征"
            compose logs celery_beat --since 3m 2>/dev/null | grep -iE 'Traceback|DuplicateTable|crashed' | head -5
            failed=1
        fi
        if [ "$(compose ps -q celery_worker celery_beat | xargs -r docker inspect --format '{{.State.Running}}' 2>/dev/null | grep -c '^true$')" != "2" ]; then
            error "celery_worker / celery_beat 未处于运行状态"
            failed=1
        fi
    fi

    return $failed
}

cmd_deploy() {
    local scope="${1:?用法: release.sh deploy <backend|frontend|all> [expected_sha]}"
    local expected_sha="${2:-}"
    local services
    services=$(services_for_scope "$scope")

    step "git 同步（Deploy Key，硬对齐）..."
    # 部署机仓库不允许本地改动，硬对齐远端——防 force push 改写历史后
    # git pull --ff-only 因分叉 abort 卡死部署（2026-09-30 实测）
    git fetch origin && git reset --hard origin/main
    local sha
    sha=$(git rev-parse --short HEAD)
    if [ -n "$expected_sha" ] && [ "$sha" != "$expected_sha" ]; then
        warn "期望部署 $expected_sha，但远端 HEAD 已是 $sha（可能被后续 push 追平），以实际 HEAD 为准"
    fi
    info "本次部署版本: $sha（scope=$scope，服务: $services）"

    local prev_sha=""
    [ -f "$STATE_FILE" ] && prev_sha=$(grep '^LAST_SHA=' "$STATE_FILE" | cut -d= -f2 || true)

    # 构建（失败时旧容器未动，直接退出由 workflow 通知，无需回滚）
    step "构建镜像（IMAGE_TAG=$sha）..."
    if ! IMAGE_TAG="$sha" compose build $services; then
        error "镜像构建失败，旧版本容器未受影响，跳过回滚"
        echo "DEPLOY_FAILED stage=build sha=$sha"
        exit 1
    fi

    # 切换到新版本
    step "启动新版本容器..."
    if ! IMAGE_TAG="$sha" compose up -d --no-build $services; then
        error "up -d 失败，尝试回滚"
        echo "DEPLOY_FAILED stage=up sha=$sha"
        [ -n "$prev_sha" ] && rollback_services "$prev_sha" "$services" || true
        exit 1
    fi

    # 健康检查（backend 部署给 beat 留启动时间）
    if [ "$scope" == "frontend" ]; then sleep 5; else sleep 25; fi
    if health_check "$scope"; then
        {
            echo "PREV_SHA=$prev_sha"
            echo "LAST_SHA=$sha"
        } > "$STATE_FILE"
        info "健康检查全部通过，部署成功: $sha"
        echo "DEPLOY_OK sha=$sha scope=$scope"
    else
        error "健康检查失败，执行自动回滚..."
        echo "DEPLOY_FAILED stage=health sha=$sha"
        if [ -n "$prev_sha" ]; then
            rollback_services "$prev_sha" "$services"
        else
            warn "无上一个成功版本可回滚，保持当前状态待人工处理"
        fi
        exit 1
    fi
}

rollback_services() {
    local target_sha="$1"; shift
    local services=("$@")
    step "回滚容器到 $target_sha ..."
    if IMAGE_TAG="$target_sha" compose up -d --no-build "${services[@]}"; then
        info "已回滚到 $target_sha"
        echo "ROLLED_BACK_TO=$target_sha"
    else
        error "回滚失败（镜像 trading-workbench-*:$target_sha 可能不存在），需人工处理"
        return 1
    fi
}

cmd_rollback() {
    local target_sha="${1:-}"
    if [ -z "$target_sha" ]; then
        [ -f "$STATE_FILE" ] || { error "无 $STATE_FILE，且未指定 sha，无法回滚"; exit 2; }
        target_sha=$(grep '^PREV_SHA=' "$STATE_FILE" | cut -d= -f2)
        [ -n "$target_sha" ] || { error "状态文件里没有上一版本记录"; exit 2; }
    fi
    rollback_services "$target_sha" $BACKEND_SERVICES $FRONTEND_SERVICES
    echo "DEPLOY_OK sha=$target_sha scope=rollback"
}

cmd_status() {
    echo "== 容器状态 =="
    compose ps
    echo "== 当前版本 =="
    if [ -f "$STATE_FILE" ]; then
        grep -E '^(LAST|PREV)_SHA=' "$STATE_FILE"
        grep '^LAST_SHA=' "$STATE_FILE" | cut -d= -f2 | xargs -I{} echo "线上版本: {}"
    else
        warn "无部署状态记录（可能尚未通过本脚本部署过）"
    fi
    echo "== SHA 镜像（最近 10 个）=="
    docker images --format '{{.Repository}}:{{.Tag}} {{.CreatedAt}}' \
        | grep -E 'trading-workbench-(backend|frontend):[0-9a-f]{7,}$' | head -10 || true
}

case "${1:-}" in
    deploy)   shift; cmd_deploy "$@" ;;
    rollback) shift; cmd_rollback "$@" ;;
    status)   cmd_status ;;
    *) sed -n '2,16p' "$0"; exit 2 ;;
esac
