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

`assay`为检测项目，`qc_lot`为质控品批次，`instrument`为仪器，`qc_run`为质控结果，`result_batch`为患者结果批次，`rejudgment`为规则变更触发的受控重判任务。

## 受控重判

检测项目规则变更（`POST /api/assays/<id>/actions`，`action=change_rules`，携带新的`rule_config`）会更新项目规则并创建一个`rejudgment`重判任务。重判按新规则逐条重算该项目的历史质控结果：

- 按新规则判为失控的已放行批次，**收回放行并回到拦截**（`recall`），同时保留原放行审核人与放行时间，批次进入待复核（`rejudgment_review.status=pending`）。
- 仍在控的批次**保留原放行时间与审核人**，不做任何改动。

审核员对拦截批次做复查确认（`POST /api/result_batches/<id>/actions`，`action=reconfirm`）后批次重新放行。复查确认使用乐观锁（`expected_version`）：两名审核员同时提交同一批次时只有一方生效，另一方收到`409 ConflictError`，可通过`GET /api/reviews/pending`查看待复核项的当前状态。

重判任务记录断点（`checkpoint_index`），每个质控结果处理后立即落盘。写入失败后任务置为`failed`，可通过`POST /api/rejudgments/<id>/actions`（`action=run`）从断点重试：已收回的批次按重判任务幂等跳过，**不会重复收回，也不会重复写操作记录**。

## 接口

- `GET /health`
- `GET /api/<kind>`，可用`?status=`过滤
- `GET /api/entities/<id>`
- `POST /api/<kind>`
- `POST /api/entities/<id>/actions`
- `POST /api/assays/<id>/actions`（`change_rules`）
- `POST /api/rejudgments/<id>/actions`（`run`）
- `POST /api/result_batches/<id>/actions`（`reconfirm`）
- `GET /api/reviews/pending`（可用`?rejudgment_id=`过滤）
- `GET /api/audit`

身份通过`X-User-Id`和`X-Role`请求头传入。可选`Idempotency-Key`防止重复创建。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

质控规则为可运行的简化模型，包含1-3s、连续偏移和趋势检查，但不替代CLIA、ISO 15189、Westgard完整规则集或实验室信息系统接口。
