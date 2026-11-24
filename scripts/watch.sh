#!/usr/bin/env bash
# Health watch for Zhiyu. One shot per run; cron calls it every five minutes.
#
#   */5 * * * * /srv/zhiyu/app/scripts/watch.sh --url https://zhiyu.example.com/api/health
#
# Why not `curl -fsS ... || echo down | mail`: a single dropped packet during a
# demo is not an outage, and one mail per failed probe trains everyone to ignore
# the alert. This script keeps a small state file, so it stays quiet until N
# probes in a row fail, then speaks once, then speaks once more when the service
# comes back. Repeat failures do not re-alert.
#
# Modes (exactly one runs per invocation):
#   default       probe /api/health and check free disk space
#   --integrity   run PRAGMA integrity_check on the live database (weekly cron)
#   --reset       forget the recorded state, e.g. after planned maintenance
#
# Options:
#   --url URL        default $ZHIYU_HEALTH_URL, else loopback /api/health
#   --state FILE     default <db dir>/watch.state, else /srv/zhiyu/var/watch.state
#   --log FILE       default watch.log next to the state file
#   --threshold N    consecutive failed probes before alerting, default 3
#   --disk PATH      filesystem to check, default /
#   --disk-min PCT   alert when free space falls below this percent, default 20
#   --db FILE        database for --integrity, default $ZHIYU_DB
#   --notify CMD     shell command that receives the message on stdin
#   --verbose        print every run, not only alerts
#
# Exit codes: 0 healthy or below threshold; 1 alerting; 2 cannot run (bad usage,
# missing curl/sqlite3). Cron only mails on output, so a 0 with no output is the
# normal quiet case.

set -uo pipefail

MODE="probe"
URL="${ZHIYU_HEALTH_URL:-}"
STATE=""
LOG=""
THRESHOLD=3
DISK="/"
DISK_MIN=20
DB="${ZHIYU_DB:-/srv/zhiyu/var/zhiyu.sqlite3}"
NOTIFY_CMD="${ZHIYU_NOTIFY_CMD:-}"
VERBOSE=0

# `shift 2` on a trailing option with no value fails without consuming anything,
# so $1 stays the same and the loop spins forever. Under cron that is a hung
# process every five minutes and no alerts at all, so a missing value is fatal
# and loud instead.
need_value() {
  [ "$2" -ge 2 ] || { echo "option $1 needs a value" >&2; exit 2; }
}

while [ $# -gt 0 ]; do
  case "$1" in
    --integrity) MODE="integrity"; shift ;;
    --reset) MODE="reset"; shift ;;
    --url) need_value "$1" "$#"; URL="$2"; shift 2 ;;
    --url=*) URL="${1#*=}"; shift ;;
    --state) need_value "$1" "$#"; STATE="$2"; shift 2 ;;
    --state=*) STATE="${1#*=}"; shift ;;
    --log) need_value "$1" "$#"; LOG="$2"; shift 2 ;;
    --log=*) LOG="${1#*=}"; shift ;;
    --threshold) need_value "$1" "$#"; THRESHOLD="$2"; shift 2 ;;
    --threshold=*) THRESHOLD="${1#*=}"; shift ;;
    --disk) need_value "$1" "$#"; DISK="$2"; shift 2 ;;
    --disk=*) DISK="${1#*=}"; shift ;;
    --disk-min) need_value "$1" "$#"; DISK_MIN="$2"; shift 2 ;;
    --disk-min=*) DISK_MIN="${1#*=}"; shift ;;
    --db) need_value "$1" "$#"; DB="$2"; shift 2 ;;
    --db=*) DB="${1#*=}"; shift ;;
    --notify) need_value "$1" "$#"; NOTIFY_CMD="$2"; shift 2 ;;
    --notify=*) NOTIFY_CMD="${1#*=}"; shift ;;
    --verbose|-v) VERBOSE=1; shift ;;
    -h|--help) awk 'NR>1 && /^#/ { sub(/^# ?/, ""); print; next } NR>1 { exit }' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

case "$THRESHOLD" in ''|*[!0-9]*) echo "threshold must be a number: $THRESHOLD" >&2; exit 2 ;; esac
case "$DISK_MIN" in ''|*[!0-9]*) echo "disk-min must be a number: $DISK_MIN" >&2; exit 2 ;; esac
[ "$THRESHOLD" -ge 1 ] || { echo "threshold must be at least 1" >&2; exit 2; }

[ -n "$URL" ] || URL="http://127.0.0.1:${ZHIYU_PORT:-4173}/api/health"
[ -n "$STATE" ] || STATE="$(dirname "$DB")/watch.state"
[ -n "$LOG" ] || LOG="$(dirname "$STATE")/watch.log"
mkdir -p "$(dirname "$STATE")" 2>/dev/null || true

# Native binaries need a Windows path under Git Bash; on Linux cygpath is absent
# and the path passes through untouched.
winpath() {
  if command -v cygpath >/dev/null 2>&1; then cygpath -w "$1"; else printf '%s' "$1"; fi
}

stamp() { date '+%F %T'; }
logline() {
  printf '%s %s\n' "$(stamp)" "$*" >> "$LOG" 2>/dev/null || true
  if [ "$VERBOSE" = "1" ]; then printf '%s\n' "$*"; fi
}

# Why a shell command and not SMTP: the host has no mail credentials, and the
# team already has a DingTalk group. ZHIYU_NOTIFY_CMD points at a three-line
# script that curls the webhook; see RUNBOOK "监控与告警".
notify() {
  local msg="$1"
  [ -n "$NOTIFY_CMD" ] || return 0
  if ! printf '%s\n' "$msg" | bash -c "$NOTIFY_CMD" >/dev/null 2>&1; then
    logline "notify command failed: $NOTIFY_CMD"
  fi
}

# --- state -----------------------------------------------------------------
# Four independent facts: how many probes in a row failed, whether that alarm
# has been sent, whether the disk alarm has been sent, and when we last saw a
# healthy service. Parsed as plain key=value so a truncate'd file cannot execute
# anything.
FAILS=0; ALERTED=0; DISK_LOW=0; LAST_OK=""; FIRST_FAIL=""; LAST_CODE=""

load_state() {
  [ -f "$STATE" ] || return 0
  local k v
  while IFS='=' read -r k v; do
    case "$k" in
      fails) case "$v" in ''|*[!0-9]*) v=0 ;; esac; FAILS="$v" ;;
      alerted) case "$v" in 0|1) ALERTED="$v" ;; esac ;;
      disk_low) case "$v" in 0|1) DISK_LOW="$v" ;; esac ;;
      last_ok) LAST_OK="$v" ;;
      first_fail) FIRST_FAIL="$v" ;;
      last_code) LAST_CODE="$v" ;;
    esac
  done < "$STATE"
}

save_state() {
  local tmp="$STATE.tmp.$$"
  # `mv file dir` succeeds by moving the file inside the directory, which would
  # look like a saved state that is never read back.
  [ -d "$STATE" ] && { rm -f "$tmp" 2>/dev/null; return 1; }
  { printf 'fails=%s\n' "$FAILS"
    printf 'alerted=%s\n' "$ALERTED"
    printf 'disk_low=%s\n' "$DISK_LOW"
    printf 'last_ok=%s\n' "$LAST_OK"
    printf 'first_fail=%s\n' "$FIRST_FAIL"
    printf 'last_code=%s\n' "$LAST_CODE"
  } > "$tmp" 2>/dev/null || { rm -f "$tmp" 2>/dev/null; return 1; }
  mv "$tmp" "$STATE" 2>/dev/null || { rm -f "$tmp" 2>/dev/null; return 1; }
}

# Losing the counter is not a cosmetic problem: with no state file every run
# starts at "one failure", so it never reaches the threshold and the service can
# be down for a week without a single alert. Fail loudly instead - cron captures
# this into watch.out, and the preflight checks the directory's writability.
persist() {
  save_state && return 0
  printf '%s\n' "[知遇] 探活无法记录状态：$STATE
目录不可写或磁盘已满，告警计数与去重都会失效（连续失败永远不会达到阈值）。
时间：$(stamp)
下一步：chown zhiyu /srv/zhiyu/var；df -h 看是否写满。" >&2
  logline "state write FAILED: $STATE"
  exit 2
}

load_state

if [ "$MODE" = "reset" ]; then
  rm -f "$STATE"
  logline "state reset: $STATE"
  exit 0
fi

# --- integrity mode --------------------------------------------------------
if [ "$MODE" = "integrity" ]; then
  command -v sqlite3 >/dev/null 2>&1 || { echo "sqlite3 not installed" >&2; exit 2; }
  [ -f "$DB" ] || { echo "database not found: $DB" >&2; exit 2; }

  check="$(sqlite3 "$(winpath "$DB")" 'PRAGMA integrity_check;' 2>&1)"
  if [ "$check" = "ok" ]; then
    logline "integrity ok: $DB"
    exit 0
  fi

  msg="[知遇] 数据库完整性检查未通过
库：$DB
结果：$(printf '%s' "$check" | head -5 | tr '\n' ' ')
时间：$(stamp)
下一步：停服，用 scripts/restore-check.sh 验证最近备份，再按 RUNBOOK「恢复演练」覆盖数据库。"
  printf '%s\n' "$msg" | sed 's/^/ /' >&2
  logline "integrity FAILED: $DB"
  notify "$msg"
  exit 1
fi

# --- probe mode ------------------------------------------------------------
command -v curl >/dev/null 2>&1 || { echo "curl not installed" >&2; exit 2; }

# Disk is not a three-strike thing: it either has room or it does not, and it
# will not fix itself. Check it on every run, alert on the transition only.
# 503 alerts also point here, which is why this runs before the probe result is
# interpreted rather than after it.
check_disk() {
  command -v df >/dev/null 2>&1 || return 0
  local used free
  used="$(df -Pk "$DISK" 2>/dev/null | awk 'NR==2 {gsub(/%/,"",$5); print $5}')"
  case "$used" in
    ''|*[!0-9]*)
      # A wrong --disk used to turn the check off quietly: df failed, the
      # function returned, and nobody ever learned that disk monitoring was
      # never running. A typo in a config flag deserves a hard stop.
      printf '%s\n' "[知遇] 磁盘检查读不到 $DISK：路径不存在或 df 不可用，磁盘告警实际上是关掉的
时间：$(stamp)
下一步：改成真实的挂载点，例如 --disk / 或 --disk /srv" >&2
      logline "disk check FAILED: cannot read $DISK"
      exit 2
      ;;
  esac
  free=$((100 - used))

  if [ "$free" -lt "$DISK_MIN" ]; then
    if [ "$DISK_LOW" != "1" ]; then
      local msg="[知遇] 磁盘剩余空间不足
挂载点：$DISK
已用：${used}%，剩余 ${free}%（阈值 ${DISK_MIN}%）
时间：$(stamp)
下一步：清理 /srv/zhiyu/backup 里的旧备份与 journal，或扩容。删备份前先确认最近一份完整性检查通过。"
      printf '%s\n' "$msg" | sed 's/^/ /' >&2
      logline "disk alert: ${used}% used on $DISK"
      notify "$msg"
      DISK_LOW=1
      persist
    fi
  elif [ "$DISK_LOW" = "1" ]; then
    logline "disk recovered: ${free}% free on $DISK"
    DISK_LOW=0
    persist
  fi
}

check_disk

# Loopback must never go through a proxy: with http_proxy set (common where
# egress needs one) curl hands 127.0.0.1 to the proxy and comes back with the
# proxy's own error page, so a healthy service looks down. The exclusion list is
# passed unconditionally - naming loopback hosts costs nothing for a public URL.
# (An unquoted `--noproxy *` would be glob-expanded into the working directory.)
NOPROXY="127.0.0.1,localhost,::1"

code="$(curl -s -o /dev/null -w '%{http_code}' --noproxy "$NOPROXY" --max-time 10 "$URL" 2>/dev/null)"
[ -n "$code" ] || code="000"
# Anything that is not a plain three-digit code (curl noise, several URLs) is
# treated as no answer rather than trusted as a status.
case "$code" in [0-9][0-9][0-9]) ;; *) code="000" ;; esac
LAST_CODE="$code"

# 200 means alive. 503 is a failure like any other — the health endpoint reports
# it exactly when the database check fails, so a reader would get an error page —
# but the alert text points at the database instead of the socket.
case "$code" in
  200) HEALTHY=1 ;;
  *) HEALTHY=0 ;;
esac

if [ "$HEALTHY" = "1" ]; then
  # Only announce a recovery if an alert was actually sent. A blip that never
  # crossed the threshold should not produce a "recovered" message: nobody knew
  # it was down, and the message would just teach people the alerts are noise.
  if [ "$ALERTED" = "1" ]; then
    down_min=0
    case "$FIRST_FAIL" in
      ''|*[!0-9]*) : ;;
      *) down_min=$(( ( $(date +%s) - FIRST_FAIL ) / 60 )) ;;
    esac
    msg="[知遇] 服务已恢复
地址：$URL
HTTP：$code
中断约 ${down_min} 分钟
时间：$(stamp)"
    printf '%s\n' "$msg" | sed 's/^/ /' >&2
    logline "recovered after ${down_min}min, http=$code"
    notify "$msg"
  elif [ "$FAILS" -gt 0 ]; then
    logline "back to ok after $FAILS failed probe(s), below threshold, no alert sent"
  else
    logline "ok: http=$code"
  fi
  FAILS=0; ALERTED=0; FIRST_FAIL=""; LAST_OK="$(date +%s)"
  persist
  exit 0
fi

# Failing.
FAILS=$((FAILS + 1))
[ -n "$FIRST_FAIL" ] || FIRST_FAIL="$(date +%s)"

if [ "$FAILS" -lt "$THRESHOLD" ]; then
  logline "probe failed $FAILS/$THRESHOLD: http=$code"
  persist
  exit 0
fi

if [ "$ALERTED" = "1" ]; then
  logline "still down ($FAILS failures, http=$code), alert already sent"
  persist
  exit 1
fi

since="$(date -d "@$FIRST_FAIL" '+%F %T' 2>/dev/null || printf '%s' "$FIRST_FAIL")"
last_ok="未记录"
case "$LAST_OK" in
  ''|*[!0-9]*) : ;;
  *) last_ok="$(date -d "@$LAST_OK" '+%F %T' 2>/dev/null || printf '%s' "$LAST_OK")" ;;
esac

if [ "$code" = "503" ]; then
  what="进程在，但 /api/health 返回 503：数据库不可用（磁盘满、库损坏或路径不对）"
  hint="先看 df -h 与 journalctl -u zhiyu -n 50，再跑 scripts/watch.sh --integrity"
elif case "$code" in 3??) true ;; *) false ;; esac; then
  # A redirect is not an outage, and "check systemctl" would send the reader to
  # the wrong place: the usual cause is a probe URL that is missing its https.
  what="探活地址返回 ${code} 重定向：服务本身大概率正常，是 --url 指错了"
  hint="把 --url 换成跳转后的最终地址（通常是 https 开头），再跑一次 curl -sI $URL 确认"
else
  what="连续 $FAILS 次探测失败，HTTP ${code}"
  hint="先 systemctl status zhiyu，再 curl -s $URL"
fi

msg="[知遇] 服务健康检查失败
地址：$URL
现象：$what
第一次失败：$since
最近一次成功：$last_ok
时间：$(stamp)
排查：$hint
恢复后会再收到一条「已恢复」。"
printf '%s\n' "$msg" | sed 's/^/ /' >&2
logline "ALERT after $FAILS failures: http=$code"
notify "$msg"
ALERTED=1
persist

exit 1
