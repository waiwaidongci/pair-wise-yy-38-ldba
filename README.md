# 水库防汛调度与操作确认

根据库位、入库流量、下游警戒和施工限制生成复核授权的泄洪指令。

## 模块结构

- `app.py`：参数解析、依赖组装和HTTP服务启动。
- `src/domain.py`：数据结构、错误、状态和基础校验。
- `src/rules.py`：状态机、角色矩阵、优先级、期限和关闭不变量。
- `src/dispatch.py`：闸门可用性判定、下游安全流量取值、洪峰建议组合与偏差折算。
- `src/repository.py`：SQLite建表、事务、版本控制和审计链。
- `src/service.py`：权限检查、用例编排、并发控制和审计。
- `src/http_api.py`：JSON路由和统一错误响应。
- `src/audit.py`：UTC时间和SHA-256审计事件。
- `static/index.html`：最小演示页。
- `tests/`：完整流程、规则和失败测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8315
```

默认端口为`8315`，首次启动自动建库。使用`X-Actor`和`X-Role`请求头传递身份。

## 主要接口

- `GET /health`
- `GET /api/items`
- `POST /api/items`
- `GET /api/items/{id}`
- `POST /api/items/{id}/records`
- `POST /api/items/{id}/transition`，必须提交`expected_version`
- `GET /api/audit`

### 闸门与调度方案

- `GET /api/gates`、`POST /api/gates`：登记闸门及最大泄量。
- `POST /api/gates/{id}/windows`：登记可用（`available`）或检修（`maintenance`）时段。
- `GET /api/safety-limits`、`POST /api/safety-limits`：登记下游安全流量（警戒）时段。
- `GET /api/plans`、`POST /api/plans`：按洪峰到达时刻生成建议闸门组合、泄量与执行缺口。
- `GET /api/plans/{id}`、`GET /api/plans/{id}/reports`
- `POST /api/plans/{id}/transition`，必须提交`expected_version`；待复核方案授权时必须附`rationale`（总工取舍）。
- `POST /api/plans/{id}/report`：调度员回报实际开闸与泄量，系统记录偏差并折算下轮建议。

允许角色：duty_officer, chief_engineer, dispatcher, viewer。库位超过汛限或入库流量上升时提升紧迫度；授权前必须有复核记录，执行后仍要闭环现场反馈。洪峰到达时无闸可用或需求泄量越过下游安全流量的方案留在待复核（pending_review），由总工写明取舍后才能授权；执行回报按实际/建议偏差折算闸门有效泄量，影响下轮建议。

## 测试

```bash
python3 -m unittest discover -s tests -v
```
