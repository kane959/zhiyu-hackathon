# 本机接口契约 · 第一版

请求及响应为 JSON。先 GET /api/workspace 取得开发者 HttpOnly 会话。除明确公开的接口外，所有项目访问均在服务端检查所属会话。错误返回非 2xx 和 {"error":"可展示的中文原因"}。匹配的业务失败仍有 runId、可查询，不能仅看 HTTP 状态判断成功。

| 方法与路径 | 请求或响应 |
| --- | --- |
| GET /api/workspace | projects、example、mode=synthetic、liveStatus=not_configured |
| POST /api/projects | name、url、audience、material、task、output、limits；带 projectId 为更新。返回已保存项目 |
| GET /api/projects/:id | project、最新 run 或 null、历史 cards（含 active、publishedAt、runId、candidateId） |
| POST /api/projects/:id/runs | requestId、mode=synthetic/live；相同项目与 requestId 去重，切换 mode 返回 409 |
| GET /api/projects/:id/runs | runs[]：runId、requestId、mode、status、errorCode、candidateCount、createdAt；倒序最多 50 条，不含候选全文 |
| GET /api/projects/:id/runs/:runId | runId、requestId、mode、status、errorCode、candidates、projectSnapshot |
| GET /api/sources/:fixtureId | 公开的合成全文与引用；不代表知乎内容 |
| POST /api/projects/:id/publish | runId、candidateId、title、material、steps、criterion、question。核验归属、当前能力和候选判断后生成 slug/version。重复发布相同当前快照返回同一版本；每次发布记录来源 runId 与 candidateId，旧版本链接继续可用 |
| GET /api/cards/:slug | 公开快照：slug、version、tool、url、scene、mode 及上面的五项卡片内容；签发读者会话 |
| POST /api/cards/:slug/visit 或 /open | version；服务端由读者 Cookie + slug 去重；visit 返回既有 receipt 或 null |
| POST /api/cards/:slug/feedback | version、submissionId、status=done/blocked、step、note。blocked 必填 step；同会话同版本一份，返回 receipt；同一 `submissionId` 重放返回同一回执，换 `submissionId` 再提交返回 409 |
| POST /api/projects/:id/withdraw/:slug | 空对象；撤回读者链接，保留反馈 |
| GET /api/projects/:id/results | versions[]：slug、version、active、publishedAt、visits、opened、completed、feedback[]；按版本倒序，前端选择版本或汇总 |

前端路由：/#home、/#edit/new、/#edit/:projectId、/#match/:projectId、/#card/:projectId/:candidateId、/#published/:projectId/:slug、/#result/:projectId；匿名读者 /t/:slug；样例原工具 /sample；合成来源 /source/:fixtureId。

### 版本与链接的约定

每次发布生成新的 `slug` 与 `version`，旧版本不会因为新版本出现而失效——已经在读者手里的链接继续打开它当时看到的那个版本，反馈也记在那一版上。要让某个链接停止服务，必须显式调用 `withdraw`，它只影响传入的那一个 `slug`，其他版本和已有反馈都不动。

因此「最新版本」不是「唯一活着的版本」。看板按 `version` 倒序展示，`active` 为 false 的表示已撤回；F2 若要标注「当前推荐」，取 `versions[0]` 且 `active` 为 true 的那一条。

卡片记录了它从哪来：`GET /api/projects/:id` 返回的 `cards[]` 里带 `runId`、`candidateId`、`publishedAt`，用于回查是第几次匹配、哪个候选产出的。这三个字段只给所有者，公开卡不含。

### 事件与反馈的计数口径

`GET /api/projects/:id/results` 的三列各算各的，互不推导：

| 列 | 定义 | 说明 |
| --- | --- | --- |
| `visits` | `visits` 表中该 `slug` 的行数 | 每个读者会话一行，重复访问不累加 |
| `opened` | 上述行中 `opened=1` 的数量 | 点了「打开原工具」才算；只访问不计 |
| `completed` | `feedback` 表中该 `slug` 且 `status='done'` 的行数 | 报「卡住」不计入完成 |

两条由此推出的性质，前端可以依赖：`visits ≥ opened`，且 `visits ≥ completed`（提交反馈前必先落访问行）。`feedback.length` 是具体反馈条数，包含「卡住」的那些，所以它可能大于 `completed`。

提交规则：

- 同一 `submissionId` 重复提交是幂等的，返回同一份 receipt，数据库只有一行。前端双击不会产生两条。
- 换了 `submissionId` 再提交会被拒绝（409）。同一会话对同一张卡片只有一份反馈；要补交或改口，需要开发者发布新版本，反馈按版本分开记。这条是为了避免「读者以为改了、看板还是旧答案」。
- `submissionId` 在卡片内唯一，不是会话内唯一。前端用 `crypto.randomUUID()` 生成即可，不要用递增序号，否则不同读者会撞号。
- 已撤回的卡片不再接受 `visit`、`open`、`feedback`，一律 404；已收到的访问与反馈保留在 `results` 里，撤回不删数据。

`ZHIYU_RATE_READER`（默认 30 次/分钟/IP）把 `visit`、`open`、`feedback` 合并计数。同一出口 IP 下的多个读者共用这个额度，部署时若走反向代理，务必按 `docs/DEPLOY.md` 配好 `ZHIYU_TRUST_PROXY`。

## 可复现的匹配结果

| 示例 | 触发方式 | 返回 |
| --- | --- | --- |
| 成功 | 访谈稿示例；运行本地匹配 | success；interview 适配（证据片段可在 /source/interview 定位），transcribe 排除（工具不处理录音）。字段 id/title/author/url/sourceType/evidence/judgment/reason/unknown；未知作者为 null |
| 空结果 | 另登记一项“输入盆栽照片、任务辨认花卉、产出花名、限制留空”的工具 | empty、candidates=[]、errorCode=null |
| 失败 | 已保存工具 → 检查真实模式 | failed、errorCode=not_configured、candidates=[]；可切回本地匹配测试 |

本版为同步本地计算，不模拟异步进度、真实限额和网络超时。接官方接口后再扩展队列、请求重试和错误码。读者会话标识保存在 HttpOnly Cookie 中，不对前端或开发者反馈列表暴露。

### 匹配实现的位置

匹配逻辑已从 `server.py` 切到 `matcher.py`，服务端只调用 `matcher.run(project, mode)` 并接收三个键：`status`、`errorCode`、`candidates`。

B1 接真实检索时替换该函数体即可，路由、存储、前端都不需要改。契约细节（输入字段、状态取值、错误码、耗时上限、异常不得外抛）写在 `matcher.py` 的文件头；`test_matcher.py` 的 7 组用例把这份契约钉住，替换实现后必须仍然全绿。

## v2 新增（部署相关，2026-11-19）

上表 11 组接口的路径与字段保持不变。以下三项为新增，F1/F2 可以按需接入，不接也不影响现有流程。

| 方法与路径 | 鉴权 | 请求或响应 |
| --- | --- | --- |
| GET /api/health | 无 | `{ok, version, db, time}`；数据库不可用时 503。供监控与版本核对 |
| POST /api/recovery | 开发者会话 | 生成或轮换恢复码，响应 `{code:"ZY-XXXX-XXXX"}`，明文只出现这一次；同一工作区只保留一个有效码 |
| POST /api/recovery/claim | 无 | 请求 `{code}`；成功返回 `{ok, projects}` 并下发开发者 Cookie。认领限 5 次/分钟/IP |

独立页面 `/recover` 提供两个入口：粘贴恢复码进入原工作区，或在当前浏览器生成新码。页面由后端直接提供，不依赖 `app.js`。

### 错误码

400 参数不合法；403 Host 不在白名单、Origin 不匹配或会话缺失；404 不存在或无权访问；409 版本或状态冲突；413 请求体超过 24 KB；415 非 JSON；429 触发限流；503 存储不可用。业务失败（空结果、真实模式未配置）仍返回 200，用 `status` 与 `errorCode` 表达。

### 限流

读者写 30 次/分钟/IP，开发者写 120 次/分钟/IP，恢复码认领 5 次/分钟/IP，读 600 次/分钟/IP。读者写与开发者写按 IP 落 `limits` 表，窗口 10 分钟后清理；读计数在进程内存中，重启清零。**被拒绝的请求同样计入配额**：计数先于业务校验提交，所以连续发非法请求会在到达上限后返回 429，而不是无限重试。

### 环境变量

完整清单见 `.env.example`。部署相关的最小集合：`ZHIYU_DB`、`ZHIYU_HOSTS`、`ZHIYU_PUBLIC_ORIGIN`、`ZHIYU_TRUST_PROXY`、`ZHIYU_SECURE_COOKIE`、`ZHIYU_BUILD`。

### 并发与写入

单进程内所有写请求经 `WRITE_LOCK` 串行执行，SQLite 只允许一个写者。读请求在 WAL 下并发。5 并发写时 p95 约 150 ms（本地 Windows 实测），超过 20 写/秒持续负载再考虑迁移数据库。
