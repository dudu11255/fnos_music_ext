#!/bin/sh
# gomusicdl-service：按 CPU 架构选择预编译的 go-music-dl 二进制（music-dl web 模式）。
# supervisor 的 command 直接调本脚本；native 部署（systemd）同样可用。
#
# 数据目录：GOMUSICDL_DATA_DIR 非空时本脚本 mkdir -p 后切入该目录，
# go 侧相对路径 data/（settings.db 等配置库）因此落在数据卷上而不是代码目录；
# 为空时沿用当前工作目录（旧行为）。
set -eu

BIN_DIR="$(cd "$(dirname "$0")" && pwd)"
ARCH="$(uname -m)"

case "$ARCH" in
    x86_64|amd64)
        BIN="${BIN_DIR}/music-dl-linux-amd64"
        ;;
    aarch64|arm64)
        BIN="${BIN_DIR}/music-dl-linux-arm64"
        ;;
    *)
        echo "[run-music-dl] unsupported arch: $ARCH" >&2
        exit 1
        ;;
esac

if [ ! -f "$BIN" ]; then
    echo "[run-music-dl] binary missing: $BIN" >&2
    exit 1
fi

# 仓库检出/解压可能丢失可执行位（Windows 检出、core.fileMode=false），自愈一次
if [ ! -x "$BIN" ]; then
    chmod 0755 "$BIN" 2>/dev/null || {
        echo "[run-music-dl] binary not executable: $BIN" >&2
        exit 1
    }
fi

DATA_DIR="${GOMUSICDL_DATA_DIR:-}"
if [ -n "$DATA_DIR" ]; then
    mkdir -p "$DATA_DIR" || {
        echo "[run-music-dl] cannot create data dir: $DATA_DIR" >&2
        exit 1
    }
    cd "$DATA_DIR"
fi

PORT="${GOMUSICDL_GO_PORT:-8006}"
exec "$BIN" web -p "$PORT" --no-browser
