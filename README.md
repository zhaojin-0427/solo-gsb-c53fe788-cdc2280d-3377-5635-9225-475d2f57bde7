# 合同义务状态推演台

用**版本化规则**定义“事件触发义务、相对期限、暂停与恢复、替代、依赖”，导入带**有效时间**与**录入时间**的事实事件后，系统按确定性回放给出每项义务的 **待触发 / 履行中 / 已履行 / 逾期 / 被替代** 状态与完整**依据链**；支持撤回重算、从任一事件分叉、修改事实或规则后比较差异。

- 后端：Python 3.11 · FastAPI · SQLite（单库、WAL、单 worker + 进程写锁）
- 前端：原生 JavaScript（无框架、无构建、无 CDN，离线可用）
- 一致性：所有写操作走 **CAS（Compare-And-Swap）基线**，整事务提交，冲突时**绝不部分写入**；推演结果只依赖持久化数据，刷新 / 重启完全一致

## 一条命令启动

```bash
docker compose up --build -d
```

启动即自动完成依赖安装、数据库建表与演示数据初始化。

访问地址：

- 应用界面：<http://localhost:8000/>
- 健康检查：<http://localhost:8000/api/health>
- OpenAPI 文档：<http://localhost:8000/docs>

停止 / 清空重来：

```bash
docker compose down          # 停止（保留数据卷）
docker compose down -v       # 停止并删除数据卷，下次启动重新初始化
```

不使用 Compose 时：`docker build -t obligation-desk . && docker run -p 8000:8000 -v obligation-data:/data obligation-desk`

## 快速体验（内置 2026 年演示数据）

打开界面后，主干分支在默认推演时点（最远事件时间 **2026-03-12**）上可看到全部五种状态：

| 义务 | 状态 | 说明 |
|---|---|---|
| DELIVER | 已履行 | 签订后 15 日期限；01-15 暂停、01-20 恢复，期限顺延 5 天至 01-30，01-29 按时履行 |
| PAY | 被替代 | 03-01 “付款方式变更”触发替代规则，由 PAY_NOTE 取代 |
| PAY_NOTE | 已履行 | 替代产生，替代后 10 日内（03-11）于 03-05 按时履行 |
| ONSITE | 已履行（逾期履行） | 到货后 7 日期限 02-04，03-03 才履行，依据链标注“逾期” |
| TAX | 逾期 | 票据开具后 10 日期限 03-11，推演时点 03-12 已逾期 |
| AUDIT | 待触发 | 已触发但依赖义务 LICENSE 从未履行，保持待触发 |

建议操作路径：

1. **规则版本** 页：直接编辑规则 JSON → “校验并创建新版本”（携带最新版本号做 CAS），再把分支切换到新版本。
2. **事实事件** 页：导入事件（有效时间 + 同刻序号 seq）；对任一事件“撤回”，其后状态自动重算。
3. **状态推演** 页：任选 as-of 时点重放；展开义务卡片查看按时间排序的**依据链**（触发依据 / 暂停 / 恢复 / 履行 / 逾期 / 被替代 / 依赖解除）。
4. **分叉对比** 页：从任一事件分叉出新分支（自动复制该事件及之前的全部事件），在新分支改事实或换规则版本，然后与基线分支做逐项差异比较。

## 模型与规则

### 事件

| 字段 | 含义 |
|---|---|
| `etype` | 事件类型（如 `合同签订`、`履行`、`付款方式变更`） |
| `valid_at` | **有效时间**（ISO8601，UTC，建议以 `Z` 结尾） |
| `recorded_at` | **录入时间**（审计用；缺省取有效时间） |
| `seq` | 同一有效时刻的创建序号（**同刻确定性排序的第二键**） |
| `code` | 业务编码（同刻排序第三键） |
| `payload.obligation` | 履行事件所指定义务编码；暂停事件未写死义务时也从该字段取 |

回放顺序固定为：`valid_at 升序 → seq 升序 → code 升序 → 事件ID`，因此结果完全确定。
事件内部阶段顺序：**触发 → 暂停 → 恢复 → 履行 → 替代**。

> 约定：只有携带 `payload.obligation` 的事件才是“履行事件”；事件类型若已被暂停/替代规则注册，则以专门规则为准，不会误触发挥霍义务。

### 规则（四种类型，按版本整体快照）

```jsonc
// 1) trigger：事件触发义务 + 相对/固定期限
{ "rid": "R-PAY", "rtype": "trigger",
  "params": { "event_type": "合同签订", "obligation": "PAY", "deadline_days": 30 } }
// 也可用 "deadline_at": "2026-02-09T00:00:00Z" 表达固定期限

// 2) suspend：暂停 / 恢复（暂停区间内期限顺延；区间为闭区间）
{ "rid": "R-PAUSE", "rtype": "suspend",
  "params": { "pause_event_type": "交货暂停通知",
              "resume_event_type": "交货恢复通知", "obligation": "DELIVER" } }
// 或单事件形式：{ "event_type": "...", "action": "suspend|resume",
//                 "obligation_from_payload": true }

// 3) substitute：事件以新义务替代旧义务，可给新期限与新依赖
{ "rid": "R-SUB", "rtype": "substitute",
  "params": { "event_type": "付款方式变更", "old_obligation": "PAY",
              "new_obligation": "PAY_NOTE", "new_deadline_days": 10,
              "new_depends_on": ["SIGN"] } }

// 4) dependency：义务必须等被依赖义务全部履行后才进入“履行中”
{ "rid": "R-DEP", "rtype": "dependency",
  "params": { "obligation": "AUDIT", "depends_on": ["LICENSE"] } }
```

有效期限 = 基础期限 + 所有“在基础期限之前开始”的暂停区间长度（未恢复的暂停一直顺延到推演时点）。逾期判定为严格比较：`as_of > 有效期限`。晚于有效期限的履行记为**已履行（逾期履行）**。

### 义务对齐键（跨分支 / 跨规则版本）

`obligation | 谱系根事件ID | 同键实例序号`。分叉时 `lineage_root_id` 保持指向最初来源事件，因此两侧义务可稳定对齐；规则被替换时仍按义务编码对齐并展示规则差异。

## CAS 与一致性

- **分支版本号**：分支每接受一次写操作 +1。导入 / 修改 / 撤回 / 切换规则版本均须携带 `expected_version`，不匹配返回 `409 Conflict`，整批不写入。
- **规则版本号**：创建新版本须携带当前最新版本号 `expected_version`，不匹配返回 409。
- 所有写操作在**进程互斥锁 + 单 SQLite 事务**中完成（`requirements` 为单 uvicorn worker），要么全部成功，要么全部回滚。
- 状态推演是纯函数 `simulate(规则集, 事件集合, as_of)`，不落库任何派生数据；默认 as-of 为当前分支非撤回事件的最大有效时间，刷新与重启结果一致。

## HTTP API 摘要

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/health` | 健康检查 |
| GET/POST | `/api/rule-versions` | 列出 / 创建规则版本（POST 带 `expected_version` CAS） |
| GET | `/api/branches` | 分支列表 |
| POST | `/api/branches` | 新建分支；带 `fork_from_event_id` 即从该事件分叉 |
| PUT | `/api/branches/{id}/rule-version` | 切换分支规则版本（分支版本 CAS） |
| GET/POST | `/api/branches/{id}/events` | 事件列表 / 批量导入（分支版本 CAS，单事务） |
| PATCH | `/api/events/{id}` | 修改事件（分支版本 CAS） |
| POST | `/api/events/{id}/withdraw` | 撤回事件并重算（分支版本 CAS） |
| GET | `/api/branches/{id}/state?as_of=...` | 确定性回放结果 + 依据链 |
| POST | `/api/compare` | 两个分支（可各自不同规则版本）的状态差异 |

## 本地开发（无 Docker 时）

```bash
pip install -r backend/requirements.txt
DB_PATH=data/obligations.db uvicorn app.main:app --reload --app-dir backend
# 纯标准库冒烟测试（引擎 / 存储 / CAS / 分叉）
python3 smoke_test.py
```
