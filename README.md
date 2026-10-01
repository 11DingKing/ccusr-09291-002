# 零部件供应协同系统

这是一个以 FastAPI 提供 HTTP 接口、以 SQLite 保存业务数据的 Python 服务端项目。项目覆盖多个相互关联的业务边界，所有验收都可在单个 Linux 应用容器中离线完成，不依赖浏览器、设备或额外运行服务。

## 本地测试

```bash
python -m pip install -r requirements.txt -r requirements-dev.txt
python -m pytest -q tests
```

## 编译检查

```bash
python -m compileall -q .
```

## 容器运行

```bash
docker build -t partforge-service .
docker run --rm -p 8000:8000 partforge-service
```

启动后可访问 `GET /health` 或根路径确认服务状态。运行测试会使用临时或本地 SQLite 文件，不需要外部数据库。

## 供应商产能预约

为解决“同一供应商日产能被两个采购小组重复计入、下单后才发现承诺冲突”的问题，系统提供按日期区间的产能预约/占用/释放/失效能力：

- `PUT /api/v1/capacity/calendar`：维护生产日历（`closed` 节假日停产、`working` 周末加班，可全厂或供应商专属）。
- `GET/POST /api/v1/capacity/availability`：查询区间逐日产能、占用与可用量（支持传 `quantity` 判定是否足够）。
- `POST /api/v1/capacity/reservations`：按区间预约，占用在创建时**按生产日历逐日固化**；默认 24 小时未转单自动失效（`expires_in_hours=0` 长期有效）。
- `POST /api/v1/capacity/reservations/{id}/release`：整单释放、按数量从后往前部分释放、按日期/日期数量释放。
- `POST /api/v1/capacity/expire-due`：手动触发超期失效（服务启动时也会自动执行一次）。
- `POST /api/v1/capacity/suggestions/{id}/convert-with-capacity`：采购建议转单，在**同一事务**内重新核对可用量并占用，失败整体回滚；持有预约转单时差额部分自动释放。
- `POST /api/v1/capacity/orders/{id}/reschedule`：改期/转供应商，同一事务内先按新方案核对、通过后释放旧占用并建立新占用，失败则旧占用原样保留。
- `GET /api/v1/capacity/decisions`、`GET /api/v1/capacity/decisions/{id}/basis`：留存每次容量决定的依据（逐日产能台账、当时已有占用、申请量、申请后余量、规则版本）。

关键保障：

1. **并发安全**：SQLite 使用 WAL + `BEGIN IMMEDIATE`，写事务起点获取写锁后再核对占用；迁移到 PostgreSQL/MySQL 时由供应能力行级锁（`SELECT … FOR UPDATE`）串行化同一供应商×物料的核对，两个小组不可能同时成功超卖。
2. **占用固化**：占用逐日落库，之后新增节假日、重叠预约、部分释放、并发预约或服务重启都不会改写既有占用（节假日仅影响新预约的展开）。
3. **追加式释放**：原始占用保留在 `quantity` 列，释放只减少 `remaining_quantity`，全部动作另写事件流水（`capacity_reservation_events`）和决策记录（`capacity_decisions`），可完整审计。

