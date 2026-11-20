# 数据模型

数据库为单文件 SQLite，默认 `var/zhiyu.sqlite3`，WAL 模式。所有业务字段存在 `data` 列的 JSON 中，表本身只承担主键、归属、版本与计数。

## 表

| 表 | 一行代表 | 关键约束 |
| --- | --- | --- |
| `projects` | 一个登记的工具 | `id` 主键，`owner` 归属 |
| `runs` | 一次匹配请求 | `UNIQUE(pid,rid)` 保证同请求幂等 |
| `cards` | 一个不可变发布快照 | `UNIQUE(pid,version)`，`active` 控制撤回 |
| `visits` | 一个读者会话对一张卡的访问 | `PRIMARY KEY(slug,reader)` |
| `feedback` | 一份读者反馈 | `PRIMARY KEY(slug,reader)`、`UNIQUE(slug,sid)` |
| `limits` | 一个 IP 在一个分钟窗口的写次数 | `k` 主键 |
| `recovery` | 一个恢复码 | `code_hash` 主键，`owner` 反查 |
| `meta` | 内部状态 | `schema_version`、`demo_owner` |

## 字段

### projects

| 列 | 类型 | 说明 |
| --- | --- | --- |
| `id` | TEXT | 项目 ID |
| `owner` | TEXT | 开发者会话值，来自 HttpOnly Cookie |
| `data` | TEXT | JSON：`name,url,audience,material,task,output,limits` |
| `created_at` | INTEGER | Unix 秒，迁移前的旧行为 0 时回填 |
| `updated_at` | INTEGER | 每次保存更新 |

### runs

| 列 | 类型 | 说明 |
| --- | --- | --- |
| `id` | TEXT | 运行 ID |
| `pid` | TEXT | 所属项目 |
| `rid` | TEXT | 客户端 `requestId` |
| `data` | TEXT | JSON，见下 |
| `created_at` | INTEGER | Unix 秒 |

运行对象：

```json
{"runId":"...","requestId":"...","mode":"synthetic","status":"success",
 "errorCode":null,"candidates":[...],"projectSnapshot":{...}}
```

`status` 取值 `success`、`empty`、`failed`。`projectSnapshot` 用于发布时比对，项目改动后旧运行不可发布。

### cards

| 列 | 类型 | 说明 |
| --- | --- | --- |
| `slug` | TEXT | 公开链接标识 |
| `pid` | TEXT | 所属项目 |
| `version` | INTEGER | 项目内递增 |
| `signature` | TEXT | 内容签名，用于重复发布去重 |
| `data` | TEXT | 公开快照，不含归属信息 |
| `active` | INTEGER | 0 表示已撤回，反馈保留 |
| `published_at` | INTEGER | Unix 秒 |
| `run_id` | TEXT | 产出这张卡的运行，用于回查来源 |
| `candidate_id` | TEXT | 选中的候选 |

卡片快照字段：`slug,version,title,material,steps,criterion,question,tool,url,scene,mode`。`run_id` 与 `candidate_id` 只存在列里，不进快照，公开接口看不到。

发布新版本不会下线旧版本。旧 `slug` 继续可用，反馈记在对应 `version` 上；只有显式撤回才把某个 `slug` 置为不可读。理由是链接一旦发出去就收不回，改内容就让读者看到「被换掉的版本」比让旧链接 404 更糟。

### candidate（内嵌在 runs.data，不单独建表）

```json
{"id":"interview","title":"...","author":null,"url":"/source/interview",
 "sourceType":"synthetic","evidence":"...","judgment":"适配",
 "reason":"...","unknown":"..."}
```

`judgment` 取值 `适配`、`待核实`、`排除`。需要跨运行统计候选命中率时再拆表，触发条件写在规划书 5.4。

### feedback

| 列 | 类型 | 说明 |
| --- | --- | --- |
| `slug` | TEXT | 卡片 |
| `reader` | TEXT | 读者会话 |
| `sid` | TEXT | 客户端 `submissionId` |
| `status` | TEXT | `done` 或 `blocked` |
| `step` | TEXT | 卡住的步骤，`blocked` 时必填 |
| `note` | TEXT | 反馈正文，不超过 1000 字符 |
| `at` | INTEGER | Unix 秒 |

## 索引

`projects(owner, updated_at)`、`runs(pid, created_at)`、`cards(pid, version)`、`visits(slug)`、`feedback(slug, at)`、`limits(window)`、`recovery(owner)`。

## 不变量

1. 归属：项目接口必须匹配 `owner`，否则 404，不区分不存在与无权访问。
2. 快照不可变：`cards.data` 发布后只读，撤回只改 `active`；新旧版本并存，不互相覆盖。
3. 版本单调：新版本为项目内最大值加一。
4. 幂等：同 `(pid,rid)` 返回原运行，但 `mode` 变了返回 409；同内容重复发布返回原版本；同 `(slug,sid)` 只落一条反馈。
5. 反馈唯一：同 `(slug,reader)` 只有一行。同 `submissionId` 重放返回原回执（200），换 `submissionId` 再提交返回 409，不做覆盖。
6. 计数：访问数 = `visits` 行数；进入工具数 = `opened=1` 行数；完成数 = `feedback.status='done'` 行数。`visits ≥ opened` 且 `visits ≥ completed` 恒成立。
7. 撤回：卡片 `active=0` 后不再接受新事件（404），但 `visits` 与 `feedback` 原样保留，`results` 仍统计该 `slug`。

## 迁移

`init()` 每次启动执行，幂等。

1. `CREATE TABLE IF NOT EXISTS` 建缺失的表。
2. 用 `PRAGMA table_info` 检查列，缺列则 `ALTER TABLE ADD COLUMN`。
3. 时间戳为 0 的旧行回填为当前时间。
4. 写入 `meta.schema_version=2`。
5. 最后创建索引，避免旧库在补列前因索引引用新列而失败。

三个必须遵守的 SQLite 兼容点：

- 不用 `unixepoch()`，Ubuntu 22.04 自带 3.37 不支持；统一 `int(time.time())`。
- `ADD COLUMN` 默认值必须为常量，所以先 `DEFAULT 0` 再回填。
- 不在 DDL 里写 `CHECK(json_valid(...))`，JSON1 编译选项不统一，校验放在 Python 层。

## 演示数据

`ZHIYU_DEMO=1` 时，首次启动写入一个项目、一张已发布卡片、两条反馈（一条完成、一条卡住），并生成恢复码写入 `var/DEMO_CODE`。以 `meta.demo_owner` 为幂等标记，重复启动不会重复写入。

## 容量

单条约 1 KB，一万条约 10 MB。SQLite 单进程写者，当前实测 5 并发写 p95 约 150 ms。持续写超过 20 次/秒再评估迁移。

`runs` 是唯一会自己长大的表：每次匹配都写一条，内含完整候选列表与项目快照，实测单条 3 至 6 KB，是其他表的数倍。因此单独设留存策略：

| 对象 | 策略 | 理由 |
| --- | --- | --- |
| `runs` 未被卡片引用且超过 90 天 | 可删 | 只用于回看历史草稿，过期无价值 |
| `runs` 被 `cards.run_id` 引用 | 永久保留 | 反馈要能追回当时展示的候选，删了链路就断 |
| `projects`、`cards`、`visits`、`feedback`、`recovery`、`limits` | 不自动删 | 前三者是业务数据与计数口径；`limits` 由限流逻辑按 10 分钟窗口自清 |

执行：`python scripts/prune.py` 先看会删几条，确认后 `--apply`。默认窗口 90 天，可用 `--days` 或 `ZHIYU_RUN_RETENTION_DAYS` 调整。窗口设为零以下没有意义，`prune` 会按 1 天处理。
