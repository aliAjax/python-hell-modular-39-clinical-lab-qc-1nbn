# 临床实验室质量控制与结果拦截

只使用Python标准库和SQLite的模块化服务，默认端口`8339`。覆盖检测项目、质控品批次、质控规则、允许范围、仪器校准、连续偏差、趋势、失控、结果拦截、复测、调查、批次切换和历史更正。

## 模块结构

- `app.py`：参数解析、依赖组装和服务生命周期。
- `src/domain.py`：角色、领域异常和数据对象。
- `src/rules.py`：QC规则计算、状态机、校准与放行约束。
- `src/repository.py`：SQLite持久化、乐观锁、幂等和审计查询。
- `src/service.py`：用例编排、权限校验和版本控制。
- `src/http_api.py`：JSON接口和统一错误响应。
- `src/audit.py`：操作审计。
- `static/index.html`：最小演示页面。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8339
```

## 核心对象

`assay`为检测项目，`qc_lot`为质控品批次，`instrument`为仪器，`qc_run`为质控结果，`result_batch`为患者结果批次。

## 接口

- `GET /health`
- `GET /api/<kind>`，可用`?status=`过滤
- `GET /api/entities/<id>`
- `POST /api/<kind>`
- `POST /api/entities/<id>/actions`
- `GET /api/audit`
- `POST /api/entities/<assay_id>/actions` 且 `action=change_rules`：受控规则变更（需 `rule_config`、`reason`），返回 `{assay, rejudgment}`，自动发起该项目历史质控结果与已放行批次的受控重判。
- `POST /api/rejudgments/<id>/retry`：重判写入中断后从断点继续（`pending`/`failed` 可重试，`completed` 为空操作）。
- `GET /api/rejudgments/<id>/items`：重判台账（每个目标的处置结果，用于核对收回范围）。
- `GET /api/review_queue`：待复核队列，即重判收回后仍处于 `intercepted` 且 `review_state=pending` 的患者结果批次。
- 批次 `action=reconfirm`（需 `reviewer_id`）：复查确认后重新放行；请携带读取批次时的 `expected_version`，并发提交只有一方生效，另一方收到 `409 ConflictError` 版本冲突。

身份通过`X-User-Id`和`X-Role`请求头传入。可选`Idempotency-Key`防止重复创建。

## 受控重判（规则变更）

检测项目规则不是即改即生效：`change_rules` 会递增 `rule_version` 并生成一张 `rejudgment` 重判单，重判范围是该项目下所有已评估的 `qc_run` 和已 `released` 的 `result_batch`。

- 质控结果按新规则重算：判定翻转时更新状态并写 `rejudge_accept`/`rejudge_reject` 审计；结果不变的只在台账登记，不写实体也不写审计。
- 已放行批次所依据的质控运行按新规则失控时，批次被收回：`released → intercepted`、挂 `review_state=pending`，进入待复核队列；批次保留原 `released_at`/`released_by`。复查时可先 `retest` 登记合格替代运行（仍停留在拦截态），审核员 `reconfirm` 后才重新放行。
- 新规则下仍然受控的批次完全不触碰：状态、版本、`updated_at`、原放行时间和审核人均不变。
- 每个目标的"台账登记 + 实体更新 + 审计写入 + 重判单断点推进"在同一个 SQLite 事务内提交，台账以 `(rejudgment_id, entity_id)` 为唯一键。重试从断点恢复，已登记目标跳过，因此不会重复收回、不会重复写操作记录。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

质控规则为可运行的简化模型，包含1-3s、连续偏移和趋势检查，但不替代CLIA、ISO 15189、Westgard完整规则集或实验室信息系统接口。
