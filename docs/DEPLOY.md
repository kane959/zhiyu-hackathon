# 部署手册

目标：一台阿里云主机，Caddy 终止 TLS，Python 进程只监听回环，数据落在持久目录，每日备份。

## 0. 前置条件

| 项 | 要求 | 由谁提供 |
| --- | --- | --- |
| 主机 | 2 vCPU / 2 GiB / 40 GiB，Ubuntu 22.04 LTS | P |
| 公网 IP 与 SSH | 可登录，安全组可改 | P |
| 域名 | 已解析到该 IP | P |
| 备案 | 大陆地域需 ICP 备案；无备案走第 1 节的 B 或 C | P |
| 安装许可 | 允许安装 `caddy`、`sqlite3` | 主机所有者 |

## 1. 先定 HTTPS 路线

大陆地域服务器绑定未备案域名时，80/443 会被拦截，备案周期 7 至 20 个工作日，赶不上 12/07。

| 路线 | 条件 | 做法 |
| --- | --- | --- |
| A | 有已备案域名 | A 记录指向主机，Caddy 自动签发证书 |
| B | 无备案域名 | 换中国香港或海外地域主机，或用其轻量应用服务器 |
| C | 暂无 HTTPS 入口 | 先在本机演示，公网验收标为未完成。不要公开 4173 端口。 |

不要在未备案的情况下先上线再补。

## 2. 主机初始化

```bash
sudo apt update
sudo apt install -y python3 sqlite3 caddy
sudo adduser --system --group --home /srv/zhiyu zhiyu
sudo mkdir -p /srv/zhiyu/var /srv/zhiyu/backup /var/log/caddy
sudo chown -R zhiyu:zhiyu /srv/zhiyu
sudo chown caddy:caddy /var/log/caddy
```

安全组入方向：22 仅本人 IP，80 与 443 对 0.0.0.0/0。不使用 4173 对公网开放。

## 3. 代码

从团队主仓获取代码；部署时记录经过验收的具体提交：

```bash
sudo -u zhiyu git clone --branch main https://github.com/kane959/zhiyu-hackathon.git /srv/zhiyu/app
cd /srv/zhiyu/app
git rev-parse HEAD
```

## 4. 环境变量

```bash
sudo install -m 600 -o root -g root .env.example /etc/zhiyu.env
sudo editor /etc/zhiyu.env
```

生产至少改这几项：

```ini
ZHIYU_DB=/srv/zhiyu/var/zhiyu.sqlite3
ZHIYU_HOSTS=zhiyu.example.com,127.0.0.1
ZHIYU_PUBLIC_ORIGIN=https://zhiyu.example.com
ZHIYU_TRUST_PROXY=1
ZHIYU_SECURE_COOKIE=1
ZHIYU_BUILD=v1.0.0-rc1+<short-sha>
ZHIYU_RATE_READER=30
ZHIYU_RATE_DEV=120
```

`ZHIYU_TRUST_PROXY=1` 必须与反代同时生效，否则所有读者共用一个 IP，30 次/分钟会误伤。`ZHIYU_HOSTS` 只写主机名，不带协议和端口。

## 5. 进程托管

```bash
sudo cp deploy/zhiyu.service /etc/systemd/system/zhiyu.service
sudo systemctl daemon-reload
sudo systemctl enable --now zhiyu
systemctl status zhiyu --no-pager
curl -s http://127.0.0.1:4173/api/health
```

`ProtectSystem=strict` 会把文件系统设为只读，`ReadWritePaths=/srv/zhiyu/var` 是写入放行，删掉会导致 SQLite 无法建库。因此 `ZHIYU_DB` 必须是绝对路径，写成相对的 `var/...` 会落到只读的 app 目录，启动即失败。

## 6. 反向代理

Caddy：

```bash
sudo cp deploy/Caddyfile /etc/caddy/Caddyfile
sudo editor /etc/caddy/Caddyfile   # 把 zhiyu.example.com 换成真实域名
sudo systemctl reload caddy
```

nginx 备选：

```bash
sudo cp deploy/nginx.conf /etc/nginx/sites-available/zhiyu
sudo ln -s /etc/nginx/sites-available/zhiyu /etc/nginx/sites-enabled/zhiyu
sudo certbot --nginx -d zhiyu.example.com
sudo nginx -t && sudo systemctl reload nginx
```

## 7. 备份

```bash
sudo -u zhiyu crontab -e
# 0 3 * * * /srv/zhiyu/app/scripts/backup.sh >> /srv/zhiyu/backup/cron.log 2>&1
```

首次手动跑一次，确认 `backup/` 出现文件且日志无报错。异地副本可选：`ossutil cp` 到 OSS，或每周手动下载。

## 8. 验收

1. `https://<域名>/` 打开，证书有效。
2. `/api/health` 返回 200，`version` 与 tag 一致。
3. 桌面 Chrome、Edge 各完成一次发布与反馈。
4. 手机 Safari、Chrome 打开卡片并提交反馈。
5. 双击提交只多一条。
6. 撤回后读者链接 404，看板仍有历史反馈。
7. `sudo systemctl restart zhiyu` 后数据不变。
8. `scripts/restore-check.sh <最新备份>` 输出计数一致。
9. 第 31 次读者写入返回 429，1 分钟后恢复。
10. 回退到上一 tag 后服务可用，再切回。

上机后可先跑一次只读预检，把配置、进程、网络、数据、备份逐项核对：

```bash
sudo ./scripts/preflight.sh --skip-net            # 机器还没通外网时
sudo ./scripts/preflight.sh                       # 公网就绪后跑完整版
```

它只读，不改任何东西。满配共 43 项，分七组；分组与判据见 `docs/RUNBOOK.md`。

### 8.1 演示期端口验收记录

路线 C 的安全原则不变：不长期公开 4173，公网验收按演示期记录。以下是在中国香港节点（`http://47.238.166.125:4173`，无域名）临时放行 4173 后实测留存的数据，用于证明功能可用；验收完成后应关闭该端口的公网入方向。

| 项目 | 实测结果 |
| --- | --- |
| 本地单元测试 | 69 组全通过 |
| 公网黑盒验收 | 30/30 |
| 完整部署预检 | 43 项 0 失败（30 通过 / 4 警告 / 9 跳过） |
| 备份 / 完整性 / 恢复演练 | 通过（隔离实例数据一致） |
| 手机真机全链路 | 匹配 → 做卡 → 发布 → 反馈 → 看板统计，全部通过 |
| 探活告警实弹演练 | 三连败触发、不重复、恢复补报，全部通过 |
| 并发基准（1/3/5 并发 × 20 次） | p95 = 17.8 / 49.8 / 86.5 ms |
| 写限流边界 | 开发者 120/min、读者 30/min、恢复码认领 5/min，均按配置触发 429 |

线上构建号 `f168c63-hk-20261120`。以上端口为演示验收期临时开放，不是常态部署配置。

## 9. 发布与回退

```bash
cd /srv/zhiyu/app
git fetch --tags
git checkout v1.0.0-rc1
sudo systemctl restart zhiyu
curl -s https://<域名>/api/health
```

同一数据库版本的回退，先在副本上验证旧代码能读写。退回 v1 代码时，必须配套使用升级前的 v1 备份：旧代码的 INSERT 不兼容 v2 新列。停写并保留当前库及 WAL，恢复到新目录，核对后再切换 `ZHIYU_DB`。回退会缺少备份之后的写入，先确认数据取舍；不要覆盖运行中的库。

## 10. 演示账号

```bash
sudo sed -i 's/^ZHIYU_DEMO=0/ZHIYU_DEMO=1/' /etc/zhiyu.env
sudo systemctl restart zhiyu
sudo cat /srv/zhiyu/var/DEMO_CODE
```

`DEMO_CODE` 权限 600，只发给评委，不写进公开仓库。提交表单填：站点地址 + 恢复码 + “打开 /recover 粘贴恢复码”三步。
