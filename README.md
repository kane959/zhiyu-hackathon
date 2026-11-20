# 知遇 · 让工具遇见需要它的人

这是黑客松 v3 方案的**本地可运行首版**。开发者登记自己已有的 AI 工具，匹配的是读者的使用场景。读者凭独立链接打开试用卡、去原工具做一个任务并反馈。

![知遇开发者工作台：试跑访谈稿示例与场景摘录](交付/工作台预览.jpg)

## 运行与演示

需要 Python 3.10+，不需要 pip、npm、网络或密钥。Windows 双击同目录的 **启动知遇.cmd**；也可在项目目录执行：

```powershell
python server.py --open
```

默认地址 http://127.0.0.1:4173/ 。请始终使用同一主机名与开发者浏览器，localhost 与 127.0.0.1 的 Cookie 不共享。端口冲突可用 `python server.py --port 4180 --open`。用 Ctrl+C 停止服务。直接双击 HTML 或使用静态服务器无法调用接口。

要让同一网段的手机访问本机实例：`python server.py --host 0.0.0.0 --port 4199`，同时把手机要用的那个 IP 加进 `ZHIYU_HOSTS`（否则每个请求都 403）。明文 HTTP 下原生 `crypto.randomUUID()` 不可用，但 `app.js` 顶部已加兜底，点示例按钮不会报错（详见 `docs/DEPLOY.md` 第 1 节）。注意静态资源缓存 5 分钟，改过前端后手机要强刷才生效。

1. 在工作台点 **试跑访谈稿示例**。示例工具“拾句”接收脱敏的文字访谈稿；对照两个场景的证据，查看为何推荐核对引文、排除音频转写。也可登记自己的工具。
2. 在适配场景点 **选这个场景做卡片**。编辑材料、步骤、完成标准及反馈问题，预览并发布。发布是保存到本机，不会发帖。
3. 打开版本化的读者链接，也可复制到同一台机器的另一浏览器。点 **打开原工具试用**，可在示例工具内查找、定位候选原话；切回卡片，选“做完了”或“卡住了”，回答该卡的问题。
4. 回工作台看 **读者反馈**，按项目和版本切换。访问、进入工具、自报完成分别计数；重复点击与提交不重复计数。可从场景页查看旧版本及撤回公开链接。

## 数据从哪里来

- **本地真实运行**：SQLite 保存项目、运行、不可变的发布快照、会话计数和反馈。初始没有访问量和用户反馈。看板来自实际的本地操作。
- **合成样例**：三个问答场景均为本项目编写，可打开样例全文，证据片段可在全文定位。没有伪造的知乎链接、作者或匹配百分比。规则依据输入及预期任务筛选，最多三条，可能为空；并非模型推理。示例原工具按字符重合度挑选文本行，不能判断引文是否支持结论。读者文字稿仅在示例工具的浏览器内处理。
- **尚未接入**：知乎官方检索、真实模型排序、知乎站内分享、账号体系。公网部署的代码、配置与脚本已就绪，但尚未落到真实主机，见 [docs/DEPLOY.md](docs/DEPLOY.md)。点击“检查真实模式”会保留项目并返回 `not_configured`。新工具若无相应样例，显示空结果。
- **使用范围**：程序只监听本机回环地址。开发者身份为当前浏览器的本机会话，不能当作正式登录；清除 Cookie 后可用恢复码换回工作区（见下节）。读者计数按浏览器会话和版本去重，不代表独立自然人。
- 数据默认保存于 `var/zhiyu.sqlite3`，不会进入 Git。可通过 `ZHIYU_DB` 指定测试副本。备份用 `scripts/backup.sh`（SQLite backup API），不要直接复制运行中的数据库文件，不要提交读者数据。

## 恢复工作区与演示数据

浏览器清 Cookie 或换设备后，打开 http://127.0.0.1:4173/recover ，粘贴恢复码即可回到原项目。恢复码在工作台所在浏览器首次生成时显示一次，之后只能轮换，不能找回。

需要一份带数据的演示工作区时，用演示模式启动：

```powershell
$env:ZHIYU_DEMO='1'; python server.py --open
```

首次启动会写入一个示例项目、一次匹配运行、一张已发布卡片和两条反馈，恢复码保存在 `var/DEMO_CODE`（权限 600，不进 Git）。删除数据库并重启会重新生成。

## 部署与运维

| 事项 | 入口 |
| --- | --- |
| 环境变量 | `.env.example` |
| 部署步骤 | `docs/DEPLOY.md` |
| 日常运维与故障处理 | `docs/RUNBOOK.md` |
| 数据模型与迁移 | `docs/DATA.md` |
| 进程与反向代理 | `deploy/zhiyu.service`、`deploy/Caddyfile`、`deploy/nginx.conf` |
| 备份与恢复演练 | `scripts/backup.sh`、`scripts/restore-check.sh` |
| 探活告警与定时任务 | `scripts/watch.sh`、`deploy/crontab` |
| 部署预检（主机上一条命令查 43 项，只读） | `scripts/preflight.sh` |
| 并发基准 | `scripts/bench.py` |
| 黑盒验收 | `scripts/accept.py`，判读方法见 `docs/验收.md` |

## 团队接手

| 模块 | 当前入口 | 下一步 |
| --- | --- | --- |
| F1 开发者工作台 | app.js 的 home / edit / match / cardEditor | 用实际工具和长文本复核；可在侧栏链接 `/recover` |
| F2 读者与看板 | drawReader / feedbackForm / dashboard | 开展手机真人试用 |
| B1 场景筛选 | `matcher.py` 的 `run()`（当前委托 `fixtures.rank`） | 替换 `run()` 实现接官方授权检索和模型；保留真实证据与失败状态，契约见文件头与 `test_matcher.py` |
| B2 数据与发布 | server.py、API.md、docs/、deploy/、scripts/ | 等 P 提供主机与域名后执行部署 |
| P 产品验证 | 上面的四步脚本 | 获取真实授权样本和试用反馈，核对站内分享入口 |

接口与三类匹配样例见 [API.md](API.md)。测试使用临时数据库与独立 Cookie，不写工作区的实际数据：

```powershell
python -m unittest -v test_flow test_deploy test_matcher test_publish test_events test_frontend_contract   # 65 组单元、集成与契约测试
python scripts/accept.py                      # 30 项黑盒验收，含 403/413/429 边界
python scripts/accept.py --base https://域名   # 对线上地址复核，会留下测试数据
python scripts/prune.py                       # 匹配运行留存报告，--apply 才删
```

验收的判读标准见 [docs/验收.md](docs/验收.md)。
