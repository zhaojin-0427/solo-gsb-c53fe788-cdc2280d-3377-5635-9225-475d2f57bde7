# 合同义务状态推演台

以版本化规则驱动、按事实事件确定性回放推演合同义务状态的本地 Web 应用。

- 后端：Python + FastAPI + SQLite
- 前端：原生 JavaScript（无构建步骤）
- 全部写操作使用基线版本 CAS，冲突时整体回滚，绝不部分写入

## 一键启动

```bash
docker compose up --build
```

访问地址：**http://localhost:8000**

依赖安装随镜像构建完成，数据库建表与示例数据初始化随应用启动自动完成，无需任何手工步骤。数据保存在名为 `app-data` 的卷中，刷新或重启后推演结果保持一致。

## 本地开发（可选）

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

## 功能说明

### 规则（版本化）

每条规则定义一类义务：

```json
{
  "rule_id": "pay-deposit",
  "name": "支付定金",
  "trigger": { "event_type": "contract.signed", "condition": [] },
  "deadline": { "amount": 3, "unit": "days" },
  "fulfill_event_type": "payment.deposit",
  "pause_event_types": ["performance.suspended"],
  "resume_event_types": ["performance.resumed"],
  "substitute_event_types": ["obligation.substituted"],
  "depends_on": []
}
```

- **触发**：`trigger.event_type` 匹配的事件在对应范围（`scope`）内创建义务实例；`condition` 为可选条件列表（`{"field":"a.b","op":"eq|ne|gt|gte|lt|lte|contains|exists","value":...}`）。
- **相对期限**：`deadline` 支持 `minutes/hours/days/weeks`，自义务进入「履行中」时起算。
- **暂停/恢复**：暂停期间时钟不走，恢复时截止时间自动顺延暂停时长。
- **替代**：替代事件使该范围内该规则所有未终结实例变为「被替代」。
- **依赖**：`depends_on` 所列规则的义务在本范围内「已履行」后，本义务才从「待触发」进入「履行中」。
- 履行/暂停/恢复/替代事件的 `payload` 若含 `rule_id`，则只作用于该规则；否则作用于所有配置了该事件类型的规则。

规则每次保存生成新的主线版本（v1、v2…），历史版本可在「规则版本」页查看。

### 事件

事件携带**有效时间**（`valid_time`，事实生效时刻）与**录入时间**（服务端记录）。回放按「有效时间 + 同刻创建序号」排序，结果确定，不依赖系统时钟。撤回事件后，其后的义务状态自动按新时间线重算；也可恢复已撤回事件。

导入格式（单个对象或数组）：

```json
{
  "event_type": "payment.deposit",
  "scope": "C-001",
  "valid_time": "2026-09-03T10:00:00Z",
  "payload": { "amount": 30000 }
}
```

### 义务状态机

`待触发 → 履行中 → 已履行`，履行中可因超时进入 `逾期`（逾期后仍可迟延履行），任意未终结状态可被 `被替代` 终止。每项义务附完整**依据链**：触发、激活、暂停、恢复、履行、逾期、替代各由哪个事件（或期限推算）引起，一目了然。

### 分叉与比较

在「事件时间线」中对任一事件点击「从此分叉」，即创建一个继承该事件（含）之前事实与当时规则的平行时间线。分叉内可独立导入/撤回事件、修改规则（分叉自带 CAS 修订号）。「分叉与比较」页任选两个视角（主线或分叉）对比每项义务的状态与截止时间差异。

### 并发控制（CAS）

- 主线写操作（改规则、导入事件、撤回/恢复）携带 `base_version`，须等于当前主线版本号，否则返回 409 且不写入任何数据。
- 分叉写操作携带 `base_rev`，语义相同。
- 所有多步写入包裹在单个 SQLite 事务中，冲突或校验失败即整体回滚。

## API 摘要

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/state?scenario=main\|{id}&as_of=` | 推演状态（义务 + 依据链 + 评估时刻） |
| GET | `/api/events?scenario=` | 事件时间线 |
| GET | `/api/rules?scenario=` / `GET /api/rules/versions` | 当前规则 / 版本历史 |
| POST | `/api/rules` | 保存规则新版本（CAS） |
| POST | `/api/events` | 批量导入事件（CAS，原子） |
| POST | `/api/events/{id}/retract` `/restore` | 撤回 / 恢复主线事件（CAS） |
| POST | `/api/scenarios` | 从指定事件序号分叉 |
| PUT | `/api/scenarios/{id}/rules` | 修改分叉规则（CAS） |
| POST | `/api/scenarios/{id}/events` `/retract` `/restore` | 分叉内事实变更（CAS） |
| GET | `/api/compare?a=&b=` | 两视角义务状态差异 |

## 项目结构

```
app/
  main.py        FastAPI 路由与 CAS 事务
  engine.py      确定性回放引擎（纯函数）
  db.py          SQLite 建表、初始化、查询助手
  static/        原生前端（index.html / app.js / style.css）
Dockerfile
docker-compose.yml
requirements.txt
```
