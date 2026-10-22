#!/usr/bin/env bash
# ============================================================
# 服务器端发布脚本（CICD 2026-09-19）
# 用法: scripts/release.sh deploy  <backend|frontend|all> [expected_sha] [bundle_tar]
#                                    # 拉代码→前端产物解压（构建外移）→SHA 构建→up→健康检查→失败自动回滚
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
        # 崩溃瞬间容器在重启循环，日志是唯一根因线索（2026-10-22 init_db 事故 CI 全程无 traceback）
        compose logs backend --since 5m 2>/dev/null | tail -40 || true
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
    local scope="${1:?用法: release.sh deploy <backend|frontend|all> [expected_sha] [bundle_tar]}"
    local expected_sha="${2:-}"
    local bundle_tar="${3:-}"   # CI 构建的前端 dist 产物（tar.gz，顶层 dist/；构建外移见下）
    local services
    services=$(services_for_scope "$scope")

    step "git 同步（Deploy Key，硬对齐）..."
    # 部署机仓库不允许本地改动，硬对齐远端——防 force push 改写历史后
    # git pull --ff-only 因分叉 abort 卡死部署（2026-09-30 实测）。
    # 注意必须分开两条写：写在 && 列表里的 fetch 失败会被 set -e 的
    # 「列表内非末位命令豁免」吞掉（2026-10-11 实测：.git/objects 权限
    # 污染致 fetch 失败，脚本静默拿旧 HEAD 构建，CI 却报部署成功）
    git fetch origin
    git reset --hard origin/main
    local sha
    sha=$(git rev-parse --short HEAD)
    if [ -n "$expected_sha" ] && [ "$sha" != "$expected_sha" ]; then
        # 服务器 HEAD 必须包含期望提交：HEAD 比期望新（后续 push 追平）可接受；
        # 落后或分叉（fetch 失败的典型症状）一律中止，绝不拿旧代码冒充部署成功
        if ! git merge-base --is-ancestor "$expected_sha" "$sha" 2>/dev/null; then
            error "服务器仓库不含期望提交 $expected_sha（HEAD=$sha），git 同步未生效，中止部署"
            echo "DEPLOY_FAILED stage=sync sha=$sha expected=$expected_sha"
            exit 1
        fi
        warn "期望部署 $expected_sha，远端 HEAD 已追平至 $sha（后续 push），以实际 HEAD 为准"
    fi
    info "本次部署版本: $sha（scope=$scope，服务: $services）"

    local prev_sha=""
    [ -f "$STATE_FILE" ] && prev_sha=$(grep '^LAST_SHA=' "$STATE_FILE" | cut -d= -f2 || true)

    # 前端产物就位（2026-10-12 构建外移）：1.6G 内存服务器无法完成含 mermaid
    # 的 vite build（峰值 ~2.3GB，连续两次部署卡死 2h+ 后 SSH 断连失败）。
    # 改由 GitHub Actions runner 构建 dist，经部署通道传入 tar.gz，本机只解压
    # 打包（frontend/Dockerfile 仅 COPY dist，秒级）。backend-only 不需要产物。
    # 放在 git reset 之后：reset 不动未跟踪的 frontend/dist。
    if [ "$scope" != "backend" ]; then
        step "前端产物就位（CI 构建产物解压）..."
        if [ -z "$bundle_tar" ] || [ ! -f "$bundle_tar" ]; then
            error "缺前端构建产物（bundle_tar=${bundle_tar:-<未传>}）：前端构建已外移至 GitHub Actions，请经 workflow 部署"
            echo "DEPLOY_FAILED stage=bundle sha=$sha"
            exit 1
        fi
        rm -rf frontend/dist
        tar -xzf "$bundle_tar" -C frontend
        if [ ! -f frontend/dist/index.html ]; then
            error "产物解压异常：frontend/dist/index.html 不存在（bundle 内容非预期）"
            echo "DEPLOY_FAILED stage=bundle sha=$sha"
            exit 1
        fi
        info "前端产物就位: frontend/dist（$(du -sh frontend/dist | cut -f1)）"
        rm -f "$bundle_tar"   # 已解压落位，临时包不留在服务器 /tmp
    fi

    # 构建（失败时旧容器未动，直接退出由 workflow 通知，无需回滚）
    step "构建镜像（IMAGE_TAG=$sha）..."
    if ! IMAGE_TAG="$sha" compose build $services; then
        error "镜像构建失败，旧版本容器未受影响，跳过回滚"
        echo "DEPLOY_FAILED stage=build sha=$sha"
        exit 1
    fi

    # 切换到新版本
    step "启动新版本容器..."
    # frontend-only 必须 --no-deps（2026-10-18 事故）：compose 会把 depends_on 的
    # backend 一起拉起，而本次只构建了前端镜像，backend 的 :$sha 标签不存在 →
    # "No such image: trading-workbench-backend:$sha" → up 失败、回滚（docs-only 部署实测）。
    # backend/all 不能用 --no-deps：需要依赖 db/redis 的 healthy 门。
    local dep_flag=""
    if [ "$scope" == "frontend" ]; then dep_flag="--no-deps"; fi
    if ! IMAGE_TAG="$sha" compose up -d --no-build $dep_flag $services; then
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
    # 调用方有两种传法：cmd_deploy 传空格分隔的服务串（"backend celery_worker
    # celery_beat frontend"），cmd_rollback 传多个独立参数。统一按空白切分成
    # 数组——2026-10-22 回滚事故：原样存入数组会展开成单个含空格的服务名，
    # compose 报 "no such service: backend celery_worker celery_beat frontend"，
    # 自动回滚失效、线上停在重启循环。
    local -a services
    read -r -a services <<< "$*"
    step "回滚容器到 $target_sha ..."
    # 与 up 一致（2026-10-18）：仅前端服务时 --no-deps，否则会连带拉起 backend，
    # 而 :$target_sha 的 backend 镜像未必存在，会让回滚本身失败、容器停在半启动态
    local dep_flag=""
    if [ "${services[*]}" == "frontend" ]; then dep_flag="--no-deps"; fi
    if IMAGE_TAG="$target_sha" compose up -d --no-build $dep_flag "${services[@]}"; then
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
