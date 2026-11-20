# 运行手册

## 启停

```bash
sudo systemctl start zhiyu
sudo systemctl stop zhiyu
sudo systemctl restart zhiyu
systemctl status zhiyu --no-pager
```

本地：`python server.py --open`，`Ctrl+C` 停止。

## 健康检查

```bash
curl -s http://127.0.0.1:4173/api/health
```

正常返回 `{"ok":true,"version":"...","db":"ok","time":...}`。`version` 与服务器上 `git describe --tags --always` 应一致。503 表示数据库不可用，先看日志。

## 部署预检

一条命令把 DEPLOY.md 里靠人眼核对的项跑完，只读，不改任何东西：

```bash
cd /srv/zhiyu/app
sudo ./scripts/preflight.sh
```

43 项分七组：系统依赖、`/etc/zhiyu.env` 配置一致性、systemd 与回环健康、公网与证书、备份与定时任务（含探活是否挂上）、数据库容量与 `runs` 留存、版本标识。

结尾汇总 `通过 / 警告 / 失败 / 跳过`。**有失败退出码 1**，可以接进脚本。`SKIP` 不算通过——读不到 `zhiyu` 的 crontab（要 root）或机器上没有 systemctl 时会被跳过，那几项要手动看。

常用变体：

```bash
sudo ./scripts/preflight.sh --base https://zhiyu.example.com   # 指定探测地址
sudo ./scripts/preflight.sh --skip-net                        # 外网还没通
sudo ./scripts/preflight.sh --env-file /tmp/test.env --unit-file /tmp/zhiyu.service   # 演练配置改动
```

什么时候跑：部署完、交地址前、每次回退后。出问题先看 `FAIL` 那几行，它直接说明是哪一步漏了。

## 日志

```bash
journalctl -u zhiyu -f
journalctl -u zhiyu --since "1 hour ago"
tail -f /var/log/caddy/zhiyu.log
```

应用默认只记录存储异常。需要访问日志时设 `ZHIYU_ACCESS_LOG=1` 并重启，输出方法、路径、状态、耗时、IP。不记录 Cookie、恢复码与反馈正文。

## 备份

```bash
/srv/zhiyu/app/scripts/backup.sh
```

输出 `backup ok: ...` 表示备份通过完整性检查。保留 14 天。不要用 `cp` 复制运行中的数据库文件，WAL 模式下会拿到不完整副本。

## 监控与告警

`watch.sh` 每 5 分钟由 cron 调一次，探测 `/api/health`，同时看磁盘余量。

```bash
sudo -u zhiyu /srv/zhiyu/app/scripts/watch.sh --url https://<域名>/api/health --verbose
sudo -u zhiyu /srv/zhiyu/app/scripts/watch.sh --integrity --verbose   # 线上库完整性
sudo -u zhiyu /srv/zhiyu/app/scripts/watch.sh --reset                  # 清状态
```

行为约定，照着它看日志：

- 单次失败**不报**，连续 3 次才报一次；继续失败不重复报，避免整晚刷屏。
- 恢复时补一条「已恢复」，附中断时长。中断时长由第一次失败的时间戳算。
- `503` 单独判定：进程活着但数据库不可用，告警文案直接指向 `df -h` 与 `--integrity`。
- 磁盘低于 20%（`--disk-min` 可调）报一次，回到阈值以上记一次恢复。磁盘不做三次去抖——它不会自己好。
- 状态存在 `var/watch.state`，日志写 `var/watch.log`。计划内维护前跑一次 `--reset`，免得维护结束收到一条「已恢复」。

告警去向：默认只写日志。要有推送，写一个三行脚本并让 cron 带上 `--notify`：

```bash
# /srv/zhiyu/var/notify.sh  (chmod 750)
#!/usr/bin/env bash
msg="$(cat)"
curl -sS -X POST '<钉钉或企业微信机器人 webhook>' \
  -H 'Content-Type: application/json' \
  -d "{\"msgtype\":\"text\",\"text\":{\"content\":$(printf '%s' "$msg" | python3 -c 'import json,sys;print(json.dumps(sys.stdin.read()))')}}"
```

然后把 crontab 里的探活行改成：

```cron
*/5 * * * * /srv/zhiyu/app/scripts/watch.sh --url https://<域名>/api/health --notify /srv/zhiyu/var/notify.sh >> /srv/zhiyu/var/watch.out 2>&1
```

`ZHIYU_NOTIFY_CMD` 环境变量等价于 `--notify`。没有推送渠道时把 `MAILTO` 设成真实邮箱，至少留下一条可追溯的邮件；两者都没有，预检 E6 会报 `WARN`。

每周的 `--integrity` 只是把故障提前发现；真正损坏时的处置顺序是：停服 → 用最近备份跑 `restore-check.sh` → 确认计数 → 覆盖 `ZHIYU_DB` → 启动。

## 恢复演练（每周一次）

```bash
/srv/zhiyu/app/scripts/restore-check.sh /srv/zhiyu/backup/zhiyu-2026-11-28-0300.sqlite3
```

脚本从只读备份制作独立副本，在空闲回环端口启动服务，验证健康状态，并逐表比较项目、匹配、卡片、访问、反馈数量。任何不一致均返回非零状态。Windows 也可直接运行 `python scripts/dbops.py restore <备份路径>`。

## 演示码

```bash
sudo cat /srv/zhiyu/var/DEMO_CODE
```

丢失后删除 `meta` 里的 `demo_owner` 不会自动重建，直接删库重启会清空全部数据，不要在生产上做。需要重新生成时，用工作台所在浏览器打开 `/recover`，点“在当前浏览器生成恢复码”。

## 常见故障

| 现象 | 可能原因 | 处理 |
| --- | --- | --- |
| 全站 403，提示域名不在列表 | `ZHIYU_HOSTS` 未加域名 | 加域名并重启 |
| 发布或反馈 403 来源不匹配 | `ZHIYU_PUBLIC_ORIGIN` 与访问地址不一致 | 改成 `https://域名` |
| 所有读者很快 429 | `ZHIYU_TRUST_PROXY` 未开，限流按反代 IP 计 | 设为 1，确认反代传 `X-Forwarded-For` |
| 页面打不开，反代 502 | 进程未启动或端口不符 | `systemctl status zhiyu`，核对 4173 |
| 重启后数据消失 | `ZHIYU_DB` 指向临时目录，或 systemd 未放行路径 | 指向 `/srv/zhiyu/var`，检查 `ReadWritePaths` |
| 反馈时间不对 | 服务器时区为 UTC | 设 `TZ=Asia/Shanghai`，接口本身用 Unix 秒 |
| 证书未签发 | 80 端口被封或域名未解析 | 检查安全组与 DNS，`journalctl -u caddy` |
| 提交提示存储不可用 | 磁盘满或数据库损坏 | `df -h`，`sqlite3 <db> 'PRAGMA integrity_check;'` |

## 发布

1. 本地 `python -m unittest -v test_flow test_deploy` 全绿。
2. 打 tag：`v0.9.0-test`（11/26）、`v1.0.0-rc1`（12/04）、`v1.0.0`（12/06）。
3. 服务器 `git checkout <tag>`，更新 `ZHIYU_BUILD`，重启。
4. `/api/health` 核对版本，跑一遍发布、读者、反馈、看板四步。
5. 记录到本文件的发布记录。

## 回退

同一数据库版本的回退，先在副本上验证旧代码能读写。退回 v1 代码时，必须配套使用升级前的 v1 备份：旧代码的 INSERT 不兼容 v2 新列。停写并保留当前库及 WAL，恢复到新目录，核对后再切换 `ZHIYU_DB`。回退会缺少备份之后的写入，先确认数据取舍；不要覆盖运行中的库。

## 压测

```bash
ZHIYU_RATE_DEV=100000 python server.py --port 4173
python scripts/bench.py --concurrency 1,3,5 --n 20 --out bench.csv
```

不放开 `ZHIYU_RATE_DEV` 会测到 429 而不是匹配。脚本会打印错误码分布，出现 429 时按提示重跑。结果同时交给 B1 记录到计划书。

## 移动端视觉检查

HTTP 200 不代表页面能用。窄屏下的横向溢出、按钮被压扁、文字截断只有看图才发现。

```bash
python server.py --port 4173          # 另开一个终端
python scripts/shot.py http://127.0.0.1:4173/recover out/recover-390.png 390 844 --mobile
python scripts/shot.py http://127.0.0.1:4173/t/<slug> out/card-320.png 320 800 --mobile
python scripts/shot.py http://127.0.0.1:4173/ out/home-1280.png 1280 900
```

带 `--eval "js"` 可以先操作页面再截图，用来检查报错态与成功态，例如：

```bash
python scripts/shot.py http://127.0.0.1:4173/recover out/err.png 390 844 --mobile \
  --eval "document.querySelector('#code').value='ZY-2222-2222';document.querySelector('#claim-form').requestSubmit();"
```

**不要用 `chrome --headless --window-size=390,844` 代替。** Windows 上 Chrome 有最小窗口宽度，页面实际按约 512 CSS 像素排版，得到的 PNG 只是被裁掉右边的一条——看起来像溢出，其实没坏。本脚本走 DevTools 协议设定设备指标，才是真实视口。

## 数据清理

限流窗口每 10 分钟自动清理。读者反馈与访问记录按项目保留，不做自动删除。提交前如需演示库干净，重新生成演示数据即可，不要删生产库。

匹配运行（`runs`）需要定期清理，因为它每次匹配都写一条且体积最大。安全规则：被已发布卡片引用的运行永远不删（反馈要靠它追溯当时展示的内容），访问与反馈计数也一行不动。

```bash
python scripts/prune.py            # 只看会删几条，默认保留 90 天
python scripts/prune.py --apply    # 真的删
```

建议每周一次，放在夜间备份之后：

```cron
30 4 * * 0 /srv/zhiyu/app/scripts/prune.py --apply >> /var/log/zhiyu-prune.log 2>&1
```

先跑一次不带 `--apply` 的，把 `candidates` 数字和备份日志对一下再挂 cron。

## 发布记录

| 日期 | 版本 | 操作人 | 结果 |
| --- | --- | --- | --- |
| 待填 | | | |
