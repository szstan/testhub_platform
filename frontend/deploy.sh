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
PREVIOUS_DIST_DIR=""

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

check_dist_dir() {
    local dist_dir="$1"
    [[ -f "${dist_dir}/index.html" ]] || { print_error "找不到 ${dist_dir}/index.html"; return 1; }
    local selinux_mode
    selinux_mode="$(getenforce 2>/dev/null || true)"
    if [[ "${selinux_mode}" == "Enforcing" ]] && ! command -v restorecon >/dev/null 2>&1; then
        print_error "SELinux 正在强制模式下运行，但找不到 restorecon"
        return 1
    fi
    if command -v restorecon >/dev/null 2>&1; then
        restorecon -R "${dist_dir}" || { print_error "无法恢复 ${dist_dir} 的 SELinux 上下文"; return 1; }
    fi
    [[ -r "${dist_dir}/index.html" ]] || { print_error "${dist_dir}/index.html 不可读"; return 1; }
    if id nginx >/dev/null 2>&1 && command -v runuser >/dev/null 2>&1; then
        if ! find "${dist_dir}" -type d -exec bash -c \
            'for artifact_path in "$@"; do runuser -u nginx -- test -x "$artifact_path" || exit 1; done' _ '{}' +; then
            print_error "无法完整检查 nginx 对 ${dist_dir} 的目录访问权限"
            return 1
        fi
        if ! find "${dist_dir}" -type f -exec bash -c \
            'for artifact_path in "$@"; do runuser -u nginx -- test -r "$artifact_path" || exit 1; done' _ '{}' +; then
            print_error "无法完整检查 nginx 对 ${dist_dir} 的文件读取权限"
            return 1
        fi
    fi
}

set_dist_permissions() {
    local dist_dir="$1"
    find "${dist_dir}" -type d -exec chmod 0755 '{}' + && find "${dist_dir}" -type f -exec chmod 0644 '{}' +
}

rollback_dist_swap() {
    local remove_current="${1:-yes}"
    local dist_dir="${SCRIPT_DIR}/dist"

    if [[ "${remove_current}" == "yes" && ( -e "${dist_dir}" || -L "${dist_dir}" ) ]]; then
        if ! rm -rf -- "${dist_dir}"; then
            print_error "无法移除失败的静态资源，上一版保留在 ${PREVIOUS_DIST_DIR:-未备份}"
            return 1
        fi
    elif [[ "${remove_current}" != "yes" && ( -e "${dist_dir}" || -L "${dist_dir}" ) ]]; then
        print_error "dist 已被其他进程创建，未覆盖它；上一版保留在 ${PREVIOUS_DIST_DIR:-未备份}"
        return 1
    fi

    if [[ -n "${PREVIOUS_DIST_DIR}" && ( -e "${PREVIOUS_DIST_DIR}" || -L "${PREVIOUS_DIST_DIR}" ) ]]; then
        if ! mv -- "${PREVIOUS_DIST_DIR}" "${dist_dir}"; then
            print_error "无法恢复上一版 dist；备份保留在 ${PREVIOUS_DIST_DIR}"
            return 1
        fi
        PREVIOUS_DIST_DIR=""
        if ! check_dist_dir "${dist_dir}"; then
            print_error "上一版 dist 已恢复，但权限或 SELinux 检查未通过"
            return 1
        fi
    fi
}

check_dist() {
    check_dist_dir "${SCRIPT_DIR}/dist"
}

# Compose 仅挂载 dist，因此生产构建必须在宿主机完成。
build_frontend() {
    check_node

    print_step "安装前端依赖并构建生产静态资源..."
    local staging_dir
    staging_dir="$(mktemp -d "${SCRIPT_DIR}/.dist.staging.XXXXXX")"
    (
        cd "${SCRIPT_DIR}"
        npm ci --no-audit --no-fund &&
            npm run build -- --outDir "${staging_dir}"
    ) || {
        rm -rf "${staging_dir}"
        die "前端构建失败，保留当前 dist"
    }

    set_dist_permissions "${staging_dir}" || {
        rm -rf -- "${staging_dir}"
        die "无法设置静态资源目录权限，保留当前 dist"
    }
    if [[ ! -f "${staging_dir}/index.html" ]]; then
        rm -rf -- "${staging_dir}"
        die "找不到 ${staging_dir}/index.html，保留当前 dist"
    fi
    PREVIOUS_DIST_DIR=""
    if [[ -e "${SCRIPT_DIR}/dist" || -L "${SCRIPT_DIR}/dist" ]]; then
        PREVIOUS_DIST_DIR="$(mktemp -d "${SCRIPT_DIR}/.dist.previous.XXXXXX")" || {
            rm -rf -- "${staging_dir}"
            die "无法分配上一版 dist 的备份路径"
        }
        if ! rmdir "${PREVIOUS_DIST_DIR}"; then
            rm -rf -- "${staging_dir}" "${PREVIOUS_DIST_DIR}"
            PREVIOUS_DIST_DIR=""
            die "无法准备上一版 dist 的备份路径"
        fi
        mv "${SCRIPT_DIR}/dist" "${PREVIOUS_DIST_DIR}" \
            || { rm -rf "${staging_dir}"; die "无法保存上一版 dist"; }
    fi
    if ! mv -- "${staging_dir}" "${SCRIPT_DIR}/dist"; then
        rm -rf -- "${staging_dir}"
        if ! rollback_dist_swap no; then
            print_error "新 dist 切换失败且自动恢复未完成"
        fi
        die "无法切换新的 dist"
    fi
    if ! check_dist_dir "${SCRIPT_DIR}/dist"; then
        if ! rollback_dist_swap yes; then
            print_error "静态资源检查失败且自动恢复未完成"
        fi
        die "静态资源检查失败，已尝试恢复上一版 dist"
    fi
    if [[ -n "${PREVIOUS_DIST_DIR}" ]]; then
        print_info "✓ 前端静态资源已原子切换，上一版保留在 ${PREVIOUS_DIST_DIR}"
    else
        print_info "✓ 前端静态资源已原子切换（首次构建，无上一版 dist）"
    fi
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

    check_dist || return 1

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
    # Recreate so Docker resolves the newly promoted host bind-mount directory.
    if ! compose up -d --force-recreate || ! wait_for_health; then
        print_warn "新版本启动或健康检查失败，恢复上一版静态资源"
        if rollback_dist_swap yes; then
            compose up -d --force-recreate || true
        else
            print_error "自动恢复未完成，请检查 dist 和 ${PREVIOUS_DIST_DIR:-上一版备份}"
        fi
        die "前端生产环境启动或健康检查失败"
    fi

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
