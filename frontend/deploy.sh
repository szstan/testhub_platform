#!/usr/bin/env bash

# TestHub 前端生产部署脚本
# 用法: ./deploy.sh [build|start|stop|restart|logs|status|clean]

set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE_FILE="${SCRIPT_DIR}/docker-compose.yml"
FRONTEND_URL="http://127.0.0.1:8003"
HEALTH_URL="${FRONTEND_URL}/health"
HEALTH_ATTEMPTS="${TESTHUB_HEALTH_ATTEMPTS:-30}"
HEALTH_INTERVAL="${TESTHUB_HEALTH_INTERVAL:-1}"

# 颜色输出
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

print_info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

print_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

print_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

print_step() {
    echo -e "${BLUE}[STEP]${NC} $1"
}

die() {
    print_error "$1"
    exit 1
}

check_docker() {
    command -v docker >/dev/null 2>&1 || die "Docker 未安装，请先安装 Docker"

    if ! docker compose version >/dev/null 2>&1; then
        die "Docker Compose 插件不可用，请先安装 Docker Compose"
    fi

    [[ -f "${COMPOSE_FILE}" ]] || die "找不到 Compose 文件: ${COMPOSE_FILE}"
}

check_node() {
    command -v npm >/dev/null 2>&1 || die "npm 未安装，请先安装 Node.js"
    [[ -f "${SCRIPT_DIR}/package-lock.json" ]] || die "找不到 frontend/package-lock.json"
}

compose() {
    docker compose --project-directory "${SCRIPT_DIR}" -f "${COMPOSE_FILE}" "$@"
}

# Compose 仅挂载 dist，因此生产构建必须在宿主机完成。
build_frontend() {
    check_node

    print_step "安装前端依赖并构建生产静态资源..."
    (
        cd "${SCRIPT_DIR}"
        npm ci --no-audit --no-fund
        npm run build
    )

    [[ -f "${SCRIPT_DIR}/dist/index.html" ]] || die "前端构建完成但未找到 dist/index.html"
    print_info "✓ 前端静态资源构建完成"
}

health_request() {
    if command -v curl >/dev/null 2>&1; then
        curl --fail --silent --show-error --max-time 5 --output /dev/null "${HEALTH_URL}"
    elif command -v wget >/dev/null 2>&1; then
        wget --quiet --timeout=5 --tries=1 --spider "${HEALTH_URL}"
    else
        return 2
    fi
}

wait_for_health() {
    local attempts="${1:-${HEALTH_ATTEMPTS}}"
    local attempt

    if ! command -v curl >/dev/null 2>&1 && ! command -v wget >/dev/null 2>&1; then
        print_error "未找到 curl 或 wget，无法执行前端健康检查"
        return 1
    fi

    for ((attempt = 1; attempt <= attempts; attempt++)); do
        if health_request; then
            print_info "✓ 前端健康检查通过"
            return 0
        fi
        sleep "${HEALTH_INTERVAL}"
    done

    print_error "前端健康检查失败"
    compose ps
    return 1
}

start_production() {
    check_docker
    build_frontend

    print_step "启动前端生产环境..."
    compose up -d
    wait_for_health

    print_info ""
    print_info "========================================="
    print_info "前端生产环境已启动！"
    print_info "========================================="
    print_info "访问地址: ${FRONTEND_URL}"
    print_info "健康检查: ${HEALTH_URL}"
    print_info "========================================="
}

stop_services() {
    check_docker
    print_step "停止前端服务..."
    compose down
    print_info "✓ 服务已停止"
}

restart_services() {
    check_docker
    print_step "重启前端服务..."
    compose restart
    wait_for_health
    print_info "✓ 服务已重启"
}

view_logs() {
    check_docker
    print_info "查看服务日志 (Ctrl+C 退出)..."
    compose logs -f
}

check_status() {
    check_docker
    print_info "服务状态:"
    compose ps
    wait_for_health 1
}

clean() {
    check_docker
    print_warn "清理前端容器和卷..."
    compose down -v
    print_info "✓ 清理完成（保留本地 dist 和 nginx 镜像）"
}

show_menu() {
    echo ""
    echo "========================================="
    echo "   TestHub 前端部署工具"
    echo "========================================="
    echo "1. 构建前端静态资源"
    echo "2. 启动生产环境"
    echo "3. 停止服务"
    echo "4. 重启服务"
    echo "5. 查看日志"
    echo "6. 查看状态"
    echo "7. 清理容器和卷"
    echo "0. 退出"
    echo "========================================="
    read -r -p "请选择操作 [0-7]: " choice

    case "${choice}" in
        1) build_frontend ;;
        2) start_production ;;
        3) stop_services ;;
        4) restart_services ;;
        5) view_logs ;;
        6) check_status ;;
        7) clean ;;
        0) exit 0 ;;
        *) print_error "无效的选择" ;;
    esac
}

main() {
    if [[ $# -eq 0 ]]; then
        while true; do
            show_menu
        done
    fi

    case "$1" in
        build) build_frontend ;;
        start) start_production ;;
        stop) stop_services ;;
        restart) restart_services ;;
        logs) view_logs ;;
        status) check_status ;;
        clean) clean ;;
        *)
            print_error "未知命令: $1"
            echo "用法: $0 [build|start|stop|restart|logs|status|clean]"
            exit 1
            ;;
    esac
}

main "$@"
