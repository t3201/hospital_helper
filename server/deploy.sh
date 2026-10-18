#!/bin/sh
# deploy.sh — hh-server 幂等部署/重启(服务器端执行, 只用 sh + curl + python3, 零依赖)
#
# 为什么需要它(261014 的两次事故):
#   1) 51120 上同时挂着两个进程: 手工 nohup 起第二个时第一个没死, 请求被随机分走,
#      表现为"改了代码但线上还是旧行为"。=> 启动前必须确认端口**真空出来**。
#   2) 进程掉过一次(21:19~21:35 连接被拒), 没人拉起。=> 本脚本被 deploy_watchdog.sh 复用。
#   3) 261016 新坑: 线上进程属主是 root, 而部署账号是 zhanghua —— 非 root 时
#      `ss -ltnp` 看不到别人的进程, 只看 pidfile/ss 会误判"没在跑"然后抢启第二个实例。
#      => 端口占用一律用 port_listening() 直接连一次, 与属主无关。
#
# 用法: sh deploy.sh [--restart|--stop|--status]   (默认 --restart)
set -u

ROOT=$(cd "$(dirname "$0")" && pwd)
PORT=${HH_LISTEN_PORT:-51120}
PY=${HH_PYTHON:-python3}
LOGDIR="$ROOT/logs"
RUNDIR="$ROOT/run"
PIDF="$RUNDIR/hh-server.pid"
mkdir -p "$LOGDIR" "$RUNDIR"

log() { echo "[deploy] $*"; }

# 端口是否**真的**有人监听(与属主无关) —— 一手证据, 见上面注释 3)
port_listening() {
    "$PY" -c 'import socket,sys
s = socket.socket(); s.settimeout(1.5)
try:
    s.connect(("127.0.0.1", int(sys.argv[1]))); print(1)
except OSError:
    pass
finally:
    s.close()' "$PORT" 2>/dev/null
}

pids_on_port() {
    if command -v ss >/dev/null 2>&1; then
        ss -ltnp 2>/dev/null | grep -E ":$PORT([[:space:]]|$)" | sed -n 's/.*pid=\([0-9]*\).*/\1/p'
    elif command -v netstat >/dev/null 2>&1; then
        netstat -ltnp 2>/dev/null | grep -E ":$PORT([[:space:]]|$)" | awk '{print $NF}' | cut -d/ -f1
    fi
}

all_pids() {
    # pidfile + 端口上能看见的监听者(去重, 排除自己)
    { [ -f "$PIDF" ] && cat "$PIDF" 2>/dev/null; pids_on_port; } | tr ' ' '\n' | grep -E '^[0-9]+$' | sort -u
}

# 端口还占着 => 是谁占的, 能不能停
stuck_report() {
    log "!! 端口 $PORT 仍被占用, 但我看不见/停不掉它 —— 多半属主不是 $(id -un)"
    log "   可见 pid: $(echo ${_p:-无})   pidfile: $(cat "$PIDF" 2>/dev/null || echo 无)"
    log "   处理: 用属主账号(或 root)执行  sh $ROOT/deploy.sh --stop"
    log "   之后建议把服务改由 $(id -un) 运行, 部署就不再需要 root(端口 >1024 无需特权)"
}

stop_all() {
    _p=$(all_pids)
    if [ -z "$_p" ]; then
        if [ -n "$(port_listening)" ]; then
            _p=""; stuck_report; return 1
        fi
        log "没有在跑的实例"
        return 0
    fi
    log "停止: $(echo $_p)"
    for p in $_p; do [ "$p" = "$$" ] || kill -TERM "$p" 2>/dev/null; done
    _i=0
    while [ $_i -lt 6 ]; do
        [ -z "$(port_listening)" ] && break
        sleep 0.5; _i=$((_i + 1))
    done
    if [ -n "$(port_listening)" ]; then
        log "TERM 没退干净, KILL: $(echo $_p)"
        for p in $_p; do [ "$p" = "$$" ] || kill -KILL "$p" 2>/dev/null; done
        sleep 0.5
    fi
    rm -f "$PIDF"
    if [ -n "$(port_listening)" ]; then stuck_report; return 1; fi
    log "端口 $PORT 已释放"
    return 0
}

health() {
    _body=$(curl -ksf -m 6 "https://127.0.0.1:$PORT/healthz" 2>/dev/null)
    if [ -z "$_body" ]; then
        _body=$(curl -sf -m 6 "http://127.0.0.1:$PORT/healthz" 2>/dev/null)
    fi
    echo "$_body" | head -c 400
}

status() {
    _p=$(pids_on_port)
    log "监听 $PORT 的进程(能看见的): ${_p:-无}"
    log "端口实际状态: $([ -n "$(port_listening)" ] && echo 有人监听 || echo 空闲)"
    log "healthz: $(health)"
}

start_one() {
    cd "$ROOT" || return 1
    # 只起一个: 先确认端口真空(防止 deploy 被并发调用, 也防止抢启 root 占着的端口)
    if [ -n "$(port_listening)" ]; then
        _p=$(pids_on_port); stuck_report
        return 1
    fi
    nohup "$PY" server.py >> "$LOGDIR/server.log" 2>&1 &
    echo $! > "$PIDF"
    log "已启动 pid=$(cat "$PIDF")  用户=$(id -un)  日志=$LOGDIR/server.log"
    _i=0
    while [ $_i -lt 12 ]; do
        _h=$(health)
        if [ -n "$_h" ]; then log "健康检查通过: $_h"; return 0; fi
        sleep 0.5; _i=$((_i + 1))
    done
    log "!! 启动后 6s 内 healthz 无响应, 最近日志:"
    tail -20 "$LOGDIR/server.log" 2>/dev/null | sed 's/^/[deploy]   /'
    return 1
}

case "${1:---restart}" in
    --stop) stop_all ;;
    --status) status ;;
    --restart) stop_all && start_one ;;
    *) echo "用法: $0 [--restart|--stop|--status]"; exit 2 ;;
esac
