#!/usr/bin/env bash
# Сводка по access-логу BotAI с mnema: 499/aborted, 5xx, 403 на WebSocket удалённого экрана,
# долгие ответы и WS-соединения. Заменяет «гадание по 499/504» из инцидентов 09 §4.
#
# Лог пишет Caddy в /var/log/caddy/botai-access.log (см. ops/deploy/caddy-botai.conf):
# JSON-строки, query и секретные заголовки вырезаны, IP маскирован — сводку можно пересылать.
#
# Использование (на mnema, файл 0600 caddy:caddy, поэтому нужен sudo):
#   sudo bash caddy-access-report.sh                 # с начала текущих суток UTC
#   sudo bash caddy-access-report.sh --since 2026-10-31
#   sudo bash caddy-access-report.sh --file /var/log/caddy/botai-access.log --all
#
# Коды возврата: 0 — всё в пределах порогов; 2 — превышение (5xx, 403 на WS, много обрывов);
# 66 — лог недоступен. Пороги нужны для запуска из cron/systemd-timer с записью в журнал:
# превышение видно как failed unit, доставку уведомлений настраивает владелец (отдельного
# канала оповещений в проекте нет — см. docs/development/diagnostics.md).
set -euo pipefail

FILE=/var/log/caddy/botai-access.log
SINCE=""
ALL=0
while [ $# -gt 0 ]; do
  case "$1" in
    --file) FILE="${2:?--file needs a path}"; shift 2 ;;
    --since) SINCE="${2:?--since needs YYYY-MM-DD}"; shift 2 ;;
    --all) ALL=1; shift ;;
    -h|--help) sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 64 ;;
  esac
done

if [ ! -r "$FILE" ]; then
  echo "cannot read $FILE: run as root or with sudo" >&2
  exit 66
fi

python3 - "$FILE" "$SINCE" "$ALL" <<'PY'
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone

path, since_raw, all_raw = sys.argv[1], sys.argv[2], sys.argv[3]
since = None
if all_raw != "1":
    if since_raw:
        try:
            since = datetime.strptime(since_raw, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            print(f"bad --since {since_raw!r}: expected YYYY-MM-DD", file=sys.stderr)
            raise SystemExit(64)
    else:
        since = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)

total = 0
ws_total = 0
by_day = defaultdict(Counter)
slow = Counter()
bad5xx = Counter()
for raw in open(path, encoding="utf-8", errors="replace"):
    raw = raw.strip()
    if not raw:
        continue
    try:
        row = json.loads(raw)
    except json.JSONDecodeError:
        continue
    ts = row.get("ts")
    if not isinstance(ts, (int, float)):
        continue
    when = datetime.fromtimestamp(ts, timezone.utc)
    if since is not None and when < since:
        continue
    req = row.get("request") or {}
    uri = str(req.get("uri") or "")
    status = int(row.get("status") or 0)
    duration = float(row.get("duration") or 0.0)
    day = when.date().isoformat()
    total += 1
    c = by_day[day]
    c["total"] += 1
    if status >= 500:
        c["5xx"] += 1
        bad5xx[(uri.split("?", 1)[0], status)] += 1
    elif status == 0:
        c["aborted"] += 1  # соединение оборвано до ответа: клиент ушёл, как nginx 499
    elif 200 <= status < 300:
        c["2xx"] += 1
    elif 300 <= status < 400:
        c["3xx"] += 1
    elif status == 403:
        c["403"] += 1
        if "/login-sessions/" in uri and "/ws" in uri:
            c["ws_403"] += 1
    elif status == 499:
        c["499"] += 1
    else:
        c["4xx_other"] += 1
    if status == 101 or (req.get("headers") or {}).get("Upgrade"):
        c["ws"] += 1
        ws_total += 1
    if duration >= 10.0 and status != 101:
        c["slow_10s"] += 1
        slow[uri.split("?", 1)[0]] += 1
    if status == 200 and "/api/health" in uri:
        c["health_ok"] += 1

print(f"BotAI Caddy access report · {path}")
window = "all lines" if since is None else "since " + since.date().isoformat() + " UTC"
print(f"window: {window} · entries {total} · websockets {ws_total}")
if total == 0:
    print("no matching entries")
    raise SystemExit(0)
for day in sorted(by_day):
    c = by_day[day]
    print(
        f"{day}: total={c['total']} 2xx={c['2xx']} 3xx={c['3xx']} "
        f"4xx={c['403'] + c['499'] + c['4xx_other']} (403={c['403']} ws403={c['ws_403']} 499={c['499']}) "
        f"5xx={c['5xx']} aborted={c['aborted']} ws={c['ws']} "
        f"slow>=10s={c['slow_10s']} health_ok={c['health_ok']}"
    )

warn = False
if bad5xx:
    warn = True
    print("\n5xx by route:")
    for (route, code), count in bad5xx.most_common(10):
        print(f"  {code} {route}: {count}")
if slow:
    print("\nslowest routes (>=10s):")
    for route, count in slow.most_common(10):
        print(f"  {route}: {count}")
total_bad = sum(c["aborted"] for c in by_day.values())
total_ws_403 = sum(c["ws_403"] for c in by_day.values())
threshold_bad = max(10, int(total * 0.05))
if total_ws_403:
    warn = True
    print(f"\nWARN: 403 on /login-sessions/*/ws: {total_ws_403}")
if total_bad > threshold_bad:
    print(f"\nWARN: aborted {total_bad} > {threshold_bad} (5% of {total} or 10)")
    warn = True
print("\nresult:", "WARN" if warn else "ok")
raise SystemExit(2 if warn else 0)
PY
