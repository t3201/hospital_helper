#!/bin/sh
# watchdog.sh — 掉线自愈(systemd 不可用时的兜底; 有 systemd 时也留着当第二道保险)
#
# crontab(用户 zhanghua):
#   */5 * * * * /home/zhanghua/hospital-helper/deploy_watchdog.sh >/dev/null 2>&1
#   @reboot     sleep 20; /home/zhanghua/hospital-helper/deploy_watchdog.sh
set -u
ROOT=$(cd "$(dirname "$0")" && pwd)
PORT=${HH_LISTEN_PORT:-51120}
LOCK="$ROOT/run/watchdog.lock"

# 健康 => 什么都不做
if curl -ksf -m 6 "https://127.0.0.1:$PORT/healthz" >/dev/null 2>&1; then
    exit 0
fi
if curl -sf -m 6 "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1; then
    exit 0
fi

# 防止 cron 与手工重启打架: 简单锁(60s 内不重复拉起)
mkdir -p "$ROOT/run"
if [ -f "$LOCK" ]; then
    _age=$(($(date +%s) - $(stat -c %Y "$LOCK" 2>/dev/null || echo 0)))
    [ $_age -lt 60 ] && exit 0
fi
echo "$(date '+%F %T') healthz 无响应, 拉起" >> "$ROOT/logs/watchdog.log"
sh "$ROOT/deploy.sh" --restart >> "$ROOT/logs/watchdog.log" 2>&1
