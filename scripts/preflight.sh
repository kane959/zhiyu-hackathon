#!/usr/bin/env bash
# Deploy preflight for Zhiyu. Read-only: it inspects, it never changes anything.
#
# Run it on the host after deploying and again before handing over the address:
#   sudo ZHIYU_APP_DIR=/srv/zhiyu/app ./scripts/preflight.sh
#
# Options:
#   --base URL     public address to probe, default reads ZHIYU_PUBLIC_ORIGIN
#   --skip-net     skip every network probe (works on an offline box)
#   --env-file P   default /etc/zhiyu.env
#   --unit-file P  default /etc/systemd/system/zhiyu.service
#   --app-dir P    default /srv/zhiyu/app
#
# Exit code is 1 when any check fails, 0 otherwise. Warnings never fail the run.
# Missing tools (no systemctl, no caddy) turn the affected checks into SKIP, so
# the script also runs on a laptop; that does not make them pass.

set -uo pipefail

ENVFILE="/etc/zhiyu.env"
UNIT="/etc/systemd/system/zhiyu.service"
APP_DIR="/srv/zhiyu/app"
BASE=""
SKIP_NET=0

# `shift 2` on a trailing option with no value consumes nothing and the parse
# loop spins forever; a typo on the host would hang the check instead of failing.
need_value() {
  [ "$2" -ge 2 ] || { echo "option $1 needs a value" >&2; exit 2; }
}

while [ $# -gt 0 ]; do
  case "$1" in
    --base) need_value "$1" "$#"; BASE="$2"; shift 2 ;;
    --base=*) BASE="${1#*=}"; shift ;;
    --env-file) need_value "$1" "$#"; ENVFILE="$2"; shift 2 ;;
    --env-file=*) ENVFILE="${1#*=}"; shift ;;
    --unit-file) need_value "$1" "$#"; UNIT="$2"; shift 2 ;;
    --unit-file=*) UNIT="${1#*=}"; shift ;;
    --app-dir) need_value "$1" "$#"; APP_DIR="$2"; shift 2 ;;
    --app-dir=*) APP_DIR="${1#*=}"; shift ;;
    --skip-net) SKIP_NET=1; shift ;;
    -h|--help) awk 'NR>1 && /^#/ { sub(/^# ?/, ""); print; next } NR>1 { exit }' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

PASS=0; WARN=0; FAIL=0; SKIP=0
FAILED=""
WARNED=""

pass() { PASS=$((PASS + 1)); printf '  [PASS] %s\n' "$*"; }
warn() { WARN=$((WARN + 1)); WARNED="$WARNED$*"$'\n'; printf '  [WARN] %s\n' "$*"; }
fail() { FAIL=$((FAIL + 1)); FAILED="$FAILED$*"$'\n'; printf '  [FAIL] %s\n' "$*"; }
skip() { SKIP=$((SKIP + 1)); printf '  [SKIP] %s\n' "$*"; }
have() { command -v "$1" >/dev/null 2>&1; }
group() { printf '\n%s\n' "$*"; }

# Read one key out of an EnvironmentFile without executing it.
env_get() {
  [ -f "$ENVFILE" ] || return 1
  local line
  line="$(grep -E "^[[:space:]]*$1=" "$ENVFILE" 2>/dev/null | tail -1)" || return 1
  [ -n "$line" ] || return 1
  line="${line#*=}"
  line="${line%\"}"; line="${line#\"}"
  line="${line%\'}"; line="${line#\'}"
  printf '%s' "$line"
}

# --- A. system and dependencies -------------------------------------------
group "A 系统与依赖"

if have python3; then
  pyok="$(python3 -c 'import sys;print("ok" if sys.version_info>=(3,8) else "old")' 2>/dev/null)"
  pyver="$(python3 -c 'import sys;print("%d.%d.%d"%sys.version_info[:3])' 2>/dev/null)"
  if [ "$pyok" = "ok" ]; then pass "A1 python3 $pyver"; else fail "A1 python3 $pyver 太旧，server.py 需要 3.8 以上"; fi
else
  fail "A1 找不到 python3"
fi

if have sqlite3; then pass "A2 sqlite3 $(sqlite3 --version 2>/dev/null | cut -d' ' -f1)"; else fail "A2 找不到 sqlite3，备份与恢复脚本都无法工作"; fi

if have curl; then pass "A3 curl 可用"; else fail "A3 找不到 curl，健康检查与公网探测都用它"; fi

proxy=""
if have systemctl; then
  for unit in caddy nginx; do
    if systemctl is-active --quiet "$unit" 2>/dev/null; then proxy="$unit"; break; fi
  done
  if [ -n "$proxy" ]; then pass "A4 反向代理在运行：$proxy"; else
    # A proxy is only required for TLS. A plaintext direct-exposure deployment
    # (no domain, http origin) deliberately has none; a permanent FAIL there
    # would train everyone to ignore the preflight.
    po="$(env_get ZHIYU_PUBLIC_ORIGIN || echo '')"
    case "$po" in
      https://*) fail "A4 公网走 HTTPS 但没有反向代理在运行，TLS 终止没有着落" ;;
      http://*)  skip "A4 明文直连方案（$po），无需反向代理；无 TLS 是有意为之" ;;
      *) fail "A4 caddy 与 nginx 都没有运行，TLS 终止没有着落" ;;
    esac
  fi
else
  skip "A4 没有 systemctl，无法判断反向代理状态"
fi

VAR_DIR="$(dirname "$(env_get ZHIYU_DB || echo /srv/zhiyu/var/zhiyu.sqlite3)")"
if [ -d "$VAR_DIR" ]; then
  owner="$(stat -c '%U' "$VAR_DIR" 2>/dev/null)"
  perms="$(stat -c '%a' "$VAR_DIR" 2>/dev/null)"
  # Judge the owner bit rather than -w: this usually runs as root, and root can
  # write anywhere, so -w would pass on a directory the service cannot write.
  if [ "$owner" != "zhiyu" ]; then
    fail "A5 数据目录 $VAR_DIR 属主是 $owner，期望 zhiyu；服务以 zhiyu 运行，写入会失败"
  elif [ $(( 0$perms & 0200 )) -ne 0 ]; then
    pass "A5 数据目录 $VAR_DIR 属主 $owner 权限 $perms"
  else
    fail "A5 数据目录 $VAR_DIR 属主是 zhiyu 但属主位不可写（权限 $perms）"
  fi
else
  fail "A5 数据目录 $VAR_DIR 不存在"
fi

BACKUP_DIR="$(env_get ZHIYU_BACKUP_DIR || echo /srv/zhiyu/backup)"
if [ -d "$BACKUP_DIR" ]; then pass "A6 备份目录 $BACKUP_DIR 存在"; else fail "A6 备份目录 $BACKUP_DIR 不存在，cron 会每天报错"; fi

# The docs and crontab call these by path, so a lost exec bit fails silently
# at 03:00. Git stores it, but a copy or an unzip can drop it.
noexec=""
for s in backup.sh restore-check.sh preflight.sh watch.sh prune.py; do
  f="$APP_DIR/scripts/$s"
  if [ -f "$f" ] && [ ! -x "$f" ]; then noexec="${noexec:+$noexec }$s"; fi
done
if [ -n "$noexec" ]; then
  fail "A7 这些脚本没有执行权限：$noexec；文档与 crontab 是直接按路径调用的"
elif [ -d "$APP_DIR/scripts" ]; then
  pass "A7 脚本都有执行权限"
else
  skip "A7 找不到 $APP_DIR/scripts"
fi

# --- B. configuration ------------------------------------------------------
group "B 配置 /etc/zhiyu.env"

if [ -f "$ENVFILE" ]; then
  pass "B1 $ENVFILE 存在"
  mode="$(stat -c '%a' "$ENVFILE" 2>/dev/null)"
  owner="$(stat -c '%U' "$ENVFILE" 2>/dev/null)"
  if [ "$mode" = "600" ] && [ "$owner" = "root" ]; then pass "B2 权限 $mode 属主 $owner"
  else fail "B2 权限 $mode 属主 $owner，期望 600 root；恢复码与演示码等同于口令"
  fi
else
  fail "B1 $ENVFILE 不存在，systemd 会以默认值启动"
  skip "B2 没有配置文件，跳过权限检查"
fi

DB="$(env_get ZHIYU_DB || echo '')"
case "$DB" in
  "") fail "B3 ZHIYU_DB 未设置，运行时用仓库内相对路径，在 ProtectSystem=strict 下建库失败" ;;
  /*) pass "B3 ZHIYU_DB 是绝对路径：$DB" ;;
  *) fail "B3 ZHIYU_DB 是相对路径（$DB），ProtectSystem=strict 下会落到只读的 app 目录并启动失败" ;;
esac

if [ -f "$UNIT" ]; then
  RW="$(grep -E '^ReadWritePaths=' "$UNIT" 2>/dev/null | head -1 | cut -d= -f2-)"
  if [ -z "$RW" ]; then
    fail "B4 unit 里没有 ReadWritePaths，ProtectSystem=strict 会让数据库无法写入"
  else
    hit=0
    for p in $RW; do
      case "$DB" in "$p"/*) hit=1 ;; esac
    done
    if [ "$hit" = "1" ]; then pass "B4 数据库路径落在 ReadWritePaths 内（$RW）"
    else fail "B4 数据库 $DB 不在 ReadWritePaths（$RW）里，启动后写入会被拒绝"
    fi
  fi
else
  skip "B4 没有 $UNIT，跳过 ReadWritePaths 比对"
fi

HOSTS="$(env_get ZHIYU_HOSTS || echo '')"
if [ -z "$HOSTS" ]; then
  fail "B5 ZHIYU_HOSTS 未设置，只有默认的 localhost 与 127.0.0.1 被放行，公网请求全部 403"
else
  bad=""
  IFS=',' read -r -a hostlist <<< "$HOSTS"
  for h in "${hostlist[@]}"; do
    h="$(printf '%s' "$h" | tr -d '[:space:]')"
    case "$h" in
      "") continue ;;
      *://*|*:*) bad="${bad:+$bad }$h" ;;
    esac
  done
  if [ -n "$bad" ]; then fail "B5 ZHIYU_HOSTS 里有带协议或端口的项：$bad；代码只比对 Host 头的主机名部分，这些项永远匹配不上"
  else pass "B5 ZHIYU_HOSTS 只含主机名：$HOSTS"
  fi
fi

ORIGIN="$(env_get ZHIYU_PUBLIC_ORIGIN || echo '')"
if [ -z "$ORIGIN" ]; then
  fail "B6 ZHIYU_PUBLIC_ORIGIN 未设置，跨站来源校验会拒绝所有写请求"
else
  case "$ORIGIN" in
    https://*|http://*) ;;
    *) fail "B6 ZHIYU_PUBLIC_ORIGIN 不以 http(s):// 开头：$ORIGIN"; ORIGIN="" ;;
  esac
  if [ -n "$ORIGIN" ]; then
    oh="${ORIGIN#*://}"; oh="${oh%%/*}"; oh="${oh%%:*}"
    if printf '%s' ",$HOSTS," | grep -q ",$oh,"; then pass "B6 ZHIYU_PUBLIC_ORIGIN $ORIGIN，主机名与 ZHIYU_HOSTS 一致"
    else fail "B6 ZHIYU_PUBLIC_ORIGIN 主机名 $oh 不在 ZHIYU_HOSTS 里，请求会 403"
    fi
    if [ -z "$BASE" ]; then BASE="$ORIGIN"; fi
  fi
fi

SC="$(env_get ZHIYU_SECURE_COOKIE || echo '0')"
case "$ORIGIN" in
  https://*)
    if [ "$SC" = "1" ]; then pass "B7 SECURE_COOKIE=1，与 HTTPS 一致"
    else fail "B7 走 HTTPS 但 ZHIYU_SECURE_COOKIE=$SC，Cookie 缺少 Secure，认领会话可能被明文带出"
    fi ;;
  http://*)
    if [ "$SC" = "1" ]; then fail "B7 走明文 HTTP 却设了 SECURE_COOKIE=1，浏览器不会回传 Cookie，会话直接失效"
    else warn "B7 明文 HTTP 路线：Cookie 无 Secure，只在没有备案域名时接受"
    fi ;;
  *)
    skip "B7 ZHIYU_PUBLIC_ORIGIN 还没有可用地址，判断不了 Cookie 策略" ;;
esac

TP="$(env_get ZHIYU_TRUST_PROXY || echo '0')"
if [ -n "$proxy" ]; then
  if [ "$TP" = "1" ]; then pass "B8 TRUST_PROXY=1，与反代同时生效，读者各自按真实 IP 计数"
  else fail "B8 有反代但 ZHIYU_TRUST_PROXY=$TP，所有读者共用反代 IP，30 次/分钟会误伤整场演示"
  fi
else
  skip "B8 没有检测到反代，跳过 TRUST_PROXY 判定"
fi

LISTEN="$(env_get ZHIYU_HOST || echo '127.0.0.1')"
if [ "$LISTEN" = "127.0.0.1" ] || [ "$LISTEN" = "localhost" ]; then
  pass "B9 监听地址 $LISTEN，只允许本机与反代访问"
else
  warn "B9 监听地址是 $LISTEN，端口暴露到网络；确认安全组没有放行 4173，且 ZHIYU_HOSTS 覆盖了可访问地址"
fi

BUILD="$(env_get ZHIYU_BUILD || echo 'dev')"
if [ "$BUILD" = "dev" ]; then
  warn "B10 ZHIYU_BUILD 仍是 dev，验收时无法用 /api/health 核对版本"
else
  pass "B10 ZHIYU_BUILD=$BUILD"
fi

# --- C. process ------------------------------------------------------------
group "C 进程与回环健康"

PORT="$(env_get ZHIYU_PORT || echo 4173)"

if have systemctl; then
  if [ -f "$UNIT" ]; then pass "C1 $UNIT 已安装"; else fail "C1 没有 $UNIT，服务没有托管，重启后不会自启"; fi
  if systemctl is-enabled --quiet zhiyu 2>/dev/null; then pass "C2 已 enable，开机会自启"
  else fail "C2 zhiyu.service 没有 enable，主机关机或重启后服务不会回来"
  fi
  if systemctl is-active --quiet zhiyu 2>/dev/null; then pass "C3 zhiyu.service 正在运行"
  else fail "C3 zhiyu.service 未运行，检查 journalctl -u zhiyu -n 50"
  fi
else
  skip "C1-C3 没有 systemctl，无法检查 systemd 托管"
fi

if have curl; then
  # Loopback must bypass any proxy in the environment: with http_proxy set, curl
  # sends 127.0.0.1 to the proxy and gets its error page back, which reads as
  # "service down" on a perfectly healthy host.
  health="$(curl -fsS --noproxy '*' --max-time 5 "http://127.0.0.1:$PORT/api/health" 2>/dev/null)"
  if [ -n "$health" ]; then
    pass "C4 回环 /api/health 有响应"
    hv="$(printf '%s' "$health" | sed -n 's/.*"version"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')"
    if [ "$BUILD" = "dev" ]; then
      skip "C5 环境里 BUILD 是 dev，不比对版本"
    elif [ "$hv" = "$BUILD" ]; then pass "C5 health 返回的版本与配置一致：$hv"
    else fail "C5 health 返回 version=$hv，配置是 $BUILD，说明跑的不是这份代码或没重启"
    fi
  else
    fail "C4 回环 /api/health 无响应，服务没起来或端口不是 $PORT"
  fi
else
  skip "C4-C5 没有 curl"
fi

if have ss; then
  line="$(ss -ltn 2>/dev/null | grep -E "[:.]$PORT[[:space:]]" | head -1)"
  if [ -z "$line" ]; then skip "C6 没有进程监听 $PORT"
  else
    case "$line" in
      127.0.0.1:$PORT*|"[::1]:$PORT"*) pass "C6 监听在回环 $PORT" ;;
      *0.0.0.0:$PORT*|*"*:$PORT"*) warn "C6 监听在 0.0.0.0:$PORT，端口对网络开放；生产应只绑 127.0.0.1" ;;
      *) pass "C6 监听 $PORT：$line" ;;
    esac
  fi
else
  skip "C6 没有 ss，跳过监听地址检查"
fi

# --- D. public reachability ------------------------------------------------
group "D 公网可达性"

if [ "$SKIP_NET" = "1" ]; then
  skip "D1-D6 按要求跳过网络探测"
elif [ -z "$BASE" ]; then
  fail "D1 没有可用地址：ZHIYU_PUBLIC_ORIGIN 为空，也没有传 --base"
else
  host="${BASE#*://}"; host="${host%%/*}"; host="${host%%:*}"
  code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 10 "$BASE/" 2>/dev/null)"
  if [ "$code" = "200" ]; then pass "D1 $BASE/ 返回 200"; else fail "D1 $BASE/ 返回 ${code:-无响应}"; fi

  if have openssl && [ "${BASE%%://*}" = "https" ]; then
    end="$(echo | openssl s_client -servername "$host" -connect "$host:443" 2>/dev/null | openssl x509 -noout -enddate 2>/dev/null | cut -d= -f2)"
    if [ -n "$end" ]; then
      left=$(( ( $(date -d "$end" +%s 2>/dev/null || echo 0) - $(date +%s) ) / 86400 ))
      if [ "$left" -gt 14 ]; then pass "D2 证书还有 $left 天"
      else fail "D2 证书只剩 $left 天，Caddy 自动续期可能没生效，检查 80 端口是否放行"
      fi
    else
      fail "D2 取不到证书到期时间，TLS 握手失败"
    fi
  else
    skip "D2 非 HTTPS 或没有 openssl，跳过证书检查"
  fi

  if [ "${BASE%%://*}" = "https" ]; then
    hcode="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 10 "http://$host/" 2>/dev/null)"
    case "$hcode" in
      301|302|307|308) pass "D3 http 会跳转到 https（$hcode）" ;;
      000|"") fail "D3 http://$host/ 无响应，80 端口没放行，证书续期也会失败" ;;
      *) warn "D3 http://$host/ 返回 $hcode 而不是跳转，明文入口没有关掉" ;;
    esac
  else
    skip "D3 非 HTTPS，跳过明文跳转检查"
  fi

  hsts="$(curl -sS -D - -o /dev/null --max-time 10 "$BASE/" 2>/dev/null | tr -d '\r' | grep -i '^strict-transport-security:')"
  if [ -n "$hsts" ]; then pass "D4 有 HSTS 头"
  elif [ "${BASE%%://*}" = "https" ]; then fail "D4 没有 HSTS 头，检查 Caddyfile 的 header 段是否被覆盖"
  else skip "D4 非 HTTPS，HSTS 不适用"
  fi

  pcode="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 10 "$BASE/api/health" 2>/dev/null)"
  if [ "$pcode" = "200" ]; then pass "D5 经反代的 /api/health 返回 200"
  else fail "D5 经反代的 /api/health 返回 ${pcode:-无响应}，反代没有把请求送到 $PORT"
  fi

  srv="$(curl -sS -D - -o /dev/null --max-time 10 "$BASE/" 2>/dev/null | tr -d '\r' | grep -i '^server:' | head -1)"
  if printf '%s' "$srv" | grep -Eqi 'python|php|nginx/[0-9]|apache/[0-9]|[0-9]+\.[0-9]+\.[0-9]+'; then
    warn "D6 响应暴露实现版本：$srv；应只保留产品标识"
  elif [ "$code" = "200" ]; then
    pass "D6 响应头不暴露实现版本${srv:+（仅 $srv）}"
  else
    skip "D6 站点本身没返回 200，响应头无从判断"
  fi
fi

# --- E. backups ------------------------------------------------------------
group "E 备份与定时任务"

# Read the crontab once; every scheduled-job check below works off this text.
# CRON_READ separates "installed but empty" (a real failure) from "cannot read
# without root" (a skip, and the next person must look by hand).
CRON=""
CRON_READ=0
if [ -f "/var/spool/cron/crontabs/zhiyu" ]; then
  CRON="$(cat "/var/spool/cron/crontabs/zhiyu" 2>/dev/null)"; CRON_READ=1
elif have crontab; then
  if CRON="$(crontab -l -u zhiyu 2>/dev/null)"; then CRON_READ=1; fi
fi

if [ "$CRON_READ" != "1" ]; then
  skip "E1/E4-E6 读不到 zhiyu 的 crontab（需要 root），手动确认：crontab -u zhiyu -l"
elif printf '%s' "$CRON" | grep -q 'backup.sh'; then
  pass "E1 定时任务里有 backup.sh"
else
  fail "E1 定时任务里没有 backup.sh，不会自动备份"
fi

if [ -d "$BACKUP_DIR" ]; then
  newest="$(find "$BACKUP_DIR" -maxdepth 1 -name 'zhiyu-*.sqlite3' -type f 2>/dev/null | sort | tail -1)"
  if [ -z "$newest" ]; then fail "E2 $BACKUP_DIR 里没有备份文件，立刻手动跑一次 scripts/backup.sh"
  else
    fresh="$(find "$BACKUP_DIR" -maxdepth 1 -name 'zhiyu-*.sqlite3' -type f -mtime -2 2>/dev/null | head -1)"
    if [ -n "$fresh" ]; then pass "E2 有 48 小时内的备份：$newest"
    else warn "E2 最新备份是 $newest，超过 48 小时，cron 可能没在跑"
    fi
    if have sqlite3; then
      ic="$(sqlite3 "$newest" 'PRAGMA integrity_check;' 2>/dev/null)"
      if [ "$ic" = "ok" ]; then pass "E3 最新备份完整性检查通过"
      else fail "E3 最新备份完整性检查失败：${ic:-无法读取}"
      fi
    else
      skip "E3 没有 sqlite3，跳过备份完整性检查"
    fi
  fi
else
  skip "E2-E3 备份目录不存在"
fi

# A backup nobody checks is a backup nobody has; a watch nobody alerts to is the
# same. These three look at the scheduled jobs that only exist after cron is
# installed, which is the step most often skipped on deployment day.
if [ "$CRON_READ" = "1" ]; then
  wline="$(printf '%s' "$CRON" | grep 'watch.sh' | grep -v -e '--integrity' | head -1)"
  if [ -n "$wline" ]; then
    pass "E4 定时任务里有探活 watch.sh"
  else
    fail "E4 定时任务里没有 watch.sh，服务挂了没人知道；照 deploy/crontab 装一份"
    skip "E5-E6 没有探活行，地址与告警渠道无从判断"
  fi

  if [ -n "$wline" ]; then
    wurl="$(printf '%s' "$wline" | sed -n 's/.*--url[= ]\{1,\}\([^ ]*\).*/\1/p')"
    case "$wurl" in
      "") warn "E5 探活没写 --url，只探回环；反代或证书挂了不会算作故障" ;;
      *example.com*|*example.org*|*example.net*) fail "E5 探活地址还是占位域名 $wurl，装到主机前换成真实地址" ;;
      http://127.0.0.1*|http://localhost*) warn "E5 探活只打回环（$wurl），反代或证书挂了不会告警" ;;
      https://*) pass "E5 探活走公网 https：$wurl" ;;
      http://*) warn "E5 探活走明文 http：$wurl；没有备案域名时可以接受，有域名就改 https" ;;
      *) warn "E5 认不出探活地址：$wurl" ;;
    esac

    if printf '%s' "$CRON" | grep -q 'notify'; then
      pass "E6 告警挂了通知渠道"
    elif printf '%s' "$CRON" | grep -qE '^[[:space:]]*MAILTO=[^"[:space:]]'; then
      pass "E6 告警走 cron 邮件（MAILTO 非空）"
    else
      warn "E6 告警只写本地日志：既没有 --notify，MAILTO 也是空的；演示周内出问题没人会收到"
    fi
  fi
fi

# --- F. data and capacity --------------------------------------------------
group "F 数据与容量"

if [ -z "$DB" ]; then
  skip "F1-F4 ZHIYU_DB 未设置，无法定位数据库（见 B3）"
elif [ -f "$DB" ]; then
  pass "F1 数据库存在：$DB（$(du -h "$DB" 2>/dev/null | cut -f1)）"
  if have sqlite3; then
    jm="$(sqlite3 "$DB" 'PRAGMA journal_mode;' 2>/dev/null)"
    if [ "$jm" = "wal" ]; then pass "F2 journal_mode=wal"
    else warn "F2 journal_mode=$jm，期望 wal；备份脚本按 WAL 写的注释"
    fi
    runs="$(sqlite3 "$DB" 'select count(*) from runs;' 2>/dev/null)"
    oldest="$(sqlite3 "$DB" 'select min(created_at) from runs;' 2>/dev/null)"
    case "$oldest" in ''|*[!0-9]*) oldest="" ;; esac
    if [ -z "$runs" ]; then
      fail "F3 读不到 runs 表，库可能没有初始化或路径指错"
    elif [ -z "$oldest" ]; then
      pass "F3 runs 有 $runs 行，没有可判断时间的记录"
    else
      now_ts="$(date +%s)"
      keep="$(env_get ZHIYU_RUN_RETENTION_DAYS || echo 90)"
      case "$keep" in ''|*[!0-9]*) keep=90 ;; esac
      if [ "$oldest" -gt "$now_ts" ]; then
        warn "F3 runs 最老一条的时间戳 $oldest 比当前时间还晚，系统时钟或写入的时间不对"
      else
        days=$(( (now_ts - oldest) / 86400 ))
        if [ "$days" -gt "$keep" ]; then warn "F3 runs 有 $runs 行，最老一条 $days 天前，超过保留期 $keep 天；跑 scripts/prune.py 看看能删多少"
        else pass "F3 runs 有 $runs 行，最老 $days 天前，在 $keep 天保留期内"
        fi
      fi
    fi
    lim="$(sqlite3 "$DB" 'select count(*) from limits;' 2>/dev/null)"
    case "$lim" in ''|*[!0-9]*) lim="" ;; esac
    if [ -n "$lim" ]; then
      if [ "$lim" -gt 200000 ]; then warn "F4 limits 有 $lim 行，限流窗口清理可能没生效"
      else pass "F4 limits 有 $lim 行"
      fi
    fi
  else
    skip "F2-F4 没有 sqlite3"
  fi
else
  fail "F1 数据库文件不存在：$DB；首次启动会新建，部署后应先访问一次 /api/health"
fi

DEMO="$(env_get ZHIYU_DEMO || echo '0')"
CODE=""
[ -n "$DB" ] && CODE="$(dirname "$DB")/DEMO_CODE"
if [ "$DEMO" != "1" ]; then
  skip "F5 ZHIYU_DEMO=0，演示码未启用（提交前才需要打开）"
elif [ -z "$CODE" ]; then
  skip "F5 ZHIYU_DB 未设置，无法定位 DEMO_CODE"
elif [ -f "$CODE" ]; then
  cmode="$(stat -c '%a' "$CODE" 2>/dev/null)"
  if [ "$cmode" = "600" ]; then pass "F5 DEMO_CODE 存在且权限 600"
  else fail "F5 DEMO_CODE 权限是 $cmode，应为 600；它等同于演示口令"
  fi
else
  fail "F5 ZHIYU_DEMO=1 但没有 $CODE，重启一次让种子里生成"
fi

# --- G. build identity -----------------------------------------------------
group "G 版本标识"

if [ -d "$APP_DIR/.git" ] && have git; then
  head="$(git -C "$APP_DIR" log -1 --format='%h %s' 2>/dev/null)"
  tag="$(git -C "$APP_DIR" describe --tags --always 2>/dev/null)"
  branch="$(git -C "$APP_DIR" rev-parse --abbrev-ref HEAD 2>/dev/null)"
  pass "G1 $APP_DIR 在 $branch 上，HEAD=$head"
  if [ "$BUILD" = "dev" ]; then
    warn "G2 ZHIYU_BUILD 没写，建议用 $tag 加构建时间"
  elif printf '%s' "$BUILD" | grep -q "$tag"; then pass "G2 ZHIYU_BUILD 与 git describe 对得上：$BUILD"
  else warn "G2 ZHIYU_BUILD=$BUILD，git describe=$tag，两者不一致，验收时以哪个为准要先说清"
  fi
  if [ -n "$(git -C "$APP_DIR" status --porcelain 2>/dev/null)" ]; then
    warn "G3 $APP_DIR 有未提交的改动，部署的不是干净的提交"
  else
    pass "G3 工作区干净，部署的是提交内容"
  fi
else
  skip "G1 没有 $APP_DIR/.git 或没有 git，无法核对分支"
  skip "G2 没有 $APP_DIR/.git 或没有 git，无法核对构建标识"
  skip "G3 没有 $APP_DIR/.git 或没有 git，无法核对工作区是否干净"
fi

# --- summary ---------------------------------------------------------------
total=$((PASS + WARN + FAIL + SKIP))
printf '\n%s\n' "----------"
printf '合计 %d 项：通过 %d、警告 %d、失败 %d、跳过 %d\n' "$total" "$PASS" "$WARN" "$FAIL" "$SKIP"

if [ "$FAIL" -gt 0 ]; then
  printf '\n必须处理：\n'
  printf '%s' "$FAILED" | sed 's/^/  - /'
fi
if [ "$WARN" -gt 0 ]; then
  printf '\n建议看一眼：\n'
  printf '%s' "$WARNED" | sed 's/^/  - /'
fi

if [ "$FAIL" -gt 0 ]; then
  printf '\n预检未通过，按上面逐条修完再交地址。\n'
  exit 1
fi
if [ "$WARN" -gt 0 ]; then
  printf '\n预检通过，有 %d 条警告，交地址前逐条确认。\n' "$WARN"
else
  printf '\n预检通过，无警告。\n'
fi
exit 0
