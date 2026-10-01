"""供应商产能预约领域服务。

核心规则：
1. 产能按“供应能力 × 物料 × 日期”管理，预约在创建时按生产日历展开，
   逐日固化到 capacity_reservation_days，之后节假日调整不会改写既有占用；
2. 所有核对与占用在同一数据库写事务内完成。SQLite 通过 BEGIN IMMEDIATE
   在事务起点获取写锁，多个采购小组并发时由数据库串行化，
   后到者读到的一定包含先到者已提交的占用；
3. 每次容量决定（预约/转单/改期/释放/失效）都写一条 capacity_decisions，
   保存当时的逐日产能台账、已有占用和规则版本。
"""
import json
import time
import uuid
from datetime import date, datetime, timedelta
from typing import List, Optional, Dict, Tuple

from sqlalchemy.orm import Session
from sqlalchemy.exc import OperationalError, IntegrityError

from app.models import (
    SupplyCapacity, PurchaseSuggestion, PurchaseOrder,
    CapacityReservation, CapacityReservationDay,
)
from app.crud.supplier import crud_supply_capacity
from app.crud.purchase import crud_purchase_suggestion, crud_purchase_order
from app.crud.capacity import (
    crud_production_calendar, crud_capacity_reservation, crud_capacity_decision,
)

RULE_VERSION = "v1"


class CapacityError(ValueError):
    """产能业务校验失败（不可重试）。"""


class CapacityUnavailableError(CapacityError):
    def __init__(self, message: str, ledger: Optional[List[dict]] = None,
                 shortage: int = 0):
        super().__init__(message)
        self.ledger = ledger or []
        self.shortage = shortage


def _gen_no(prefix: str) -> str:
    return f"{prefix}-{datetime.now().strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:8]}"


def run_with_lock_retry(attempt, retries: int = 5, base_delay: float = 0.05):
    """在 SQLite 写锁冲突时整体重试整个事务单元。

    busy_timeout 已经让多数冲突排队等待，这里只兜底极少数超时场景；
    业务错误（CapacityError）不重试。
    """
    last_error = None
    for i in range(retries):
        try:
            return attempt()
        except OperationalError as exc:  # pragma: no cover - 依赖调度时机
            if "locked" not in str(exc).lower() or i == retries - 1:
                raise
            last_error = exc
            time.sleep(base_delay * (i + 1))
    raise last_error  # type: ignore[misc]


class CapacityService:
    # ---------- 日历 ----------

    @staticmethod
    def is_working_day(db: Session, day: date, supplier_id: Optional[int]) -> bool:
        override = crud_production_calendar.get_for_date(db, day, supplier_id)
        if override is not None:
            return override.day_type == "working"
        # 默认日历：周一到周五生产，周六周日停产
        return day.weekday() < 5

    @staticmethod
    def working_days_in(db: Session, start: date, end: date,
                        supplier_id: Optional[int]) -> List[date]:
        days: List[date] = []
        calendar_map = crud_production_calendar.get_range_map(db, start, end, supplier_id)
        cur = start
        while cur <= end:
            override = calendar_map.get(cur)
            if override is not None:
                working = override.day_type == "working"
            else:
                working = cur.weekday() < 5
            if working:
                days.append(cur)
            cur += timedelta(days=1)
        return days

    # ---------- 可用量查询 ----------

    @staticmethod
    def get_availability(db: Session, supplier_id: int, material_id: int,
                         start: date, end: date,
                         exclude_reservation_id: Optional[int] = None,
                         required_quantity: Optional[int] = None,
                         now: Optional[datetime] = None) -> dict:
        if end < start:
            raise CapacityError("结束日期不能早于开始日期")
        capacity = crud_supply_capacity.get_by_supplier_and_material(
            db, supplier_id, material_id
        )
        if not capacity:
            raise CapacityError("该供应商未维护此物料的供应能力，无法核对产能")
        daily_cap = capacity.daily_capacity or 0
        working = CapacityService.working_days_in(db, start, end, supplier_id)
        occupancy = crud_capacity_reservation.active_occupancy_by_day(
            db, supplier_id=supplier_id, material_id=material_id,
            start=start, end=end, now=now,
            exclude_reservation_id=exclude_reservation_id,
        )
        calendar_map = crud_production_calendar.get_range_map(db, start, end, supplier_id)
        day_rows = []
        cur = start
        while cur <= end:
            override = calendar_map.get(cur)
            is_working = (override.day_type == "working") if override else cur.weekday() < 5
            cap_today = daily_cap if is_working else 0
            occupied = occupancy.get(cur, 0) if is_working else 0
            day_rows.append({
                "day_date": cur,
                "is_working_day": is_working,
                "daily_capacity": cap_today,
                "occupied_quantity": occupied,
                "available_quantity": max(0, cap_today - occupied),
            })
            cur += timedelta(days=1)
        total_cap = sum(r["daily_capacity"] for r in day_rows)
        total_occ = sum(r["occupied_quantity"] for r in day_rows)
        available = max(0, total_cap - total_occ)
        sufficient = (
            available >= required_quantity if required_quantity is not None else True
        )
        return {
            "supplier_id": supplier_id,
            "material_id": material_id,
            "start_date": start,
            "end_date": end,
            "supply_capacity_id": capacity.id,
            "daily_capacity": daily_cap,
            "total_capacity": total_cap,
            "occupied_quantity": total_occ,
            "available_quantity": available,
            "sufficient": sufficient,
            "working_days": len(working),
            "days": day_rows,
        }

    # ---------- 决策依据 ----------

    @staticmethod
    def _build_basis(db: Session, *, capacity: SupplyCapacity, start: date,
                     end: date, quantity: int, plan: Optional[Dict[date, int]],
                     exclude_reservation_id: Optional[int],
                     now: datetime) -> dict:
        avail = CapacityService.get_availability(
            db, capacity.supplier_id, capacity.material_id, start, end,
            exclude_reservation_id=exclude_reservation_id, now=now,
        )
        occupancy_details = crud_capacity_reservation.active_occupancy_details(
            db, supplier_id=capacity.supplier_id,
            material_id=capacity.material_id, start=start, end=end, now=now,
            exclude_reservation_id=exclude_reservation_id,
        )
        ledger = []
        plan_by_str = {d.isoformat(): q for d, q in (plan or {}).items()}
        for row in avail["days"]:
            requested = plan_by_str.get(row["day_date"].isoformat(), 0)
            ledger.append({
                "day_date": row["day_date"].isoformat(),
                "is_working_day": row["is_working_day"],
                "daily_capacity": row["daily_capacity"],
                "occupied_quantity": row["occupied_quantity"],
                "requested_quantity": requested,
                "available_after_request": row["daily_capacity"]
                                          - row["occupied_quantity"] - requested,
            })
        return {
            "rule_version": RULE_VERSION,
            "checked_at": now.isoformat(),
            "supply_capacity_id": capacity.id,
            "daily_capacity": capacity.daily_capacity,
            "window": {
                "start_date": start.isoformat(),
                "end_date": end.isoformat(),
                "working_days": [
                    r["day_date"].isoformat() for r in avail["days"]
                    if r["is_working_day"]
                ],
            },
            "requested_quantity": quantity,
            "plan": [
                {"day_date": d.isoformat(), "quantity": q}
                for d, q in sorted((plan or {}).items())
            ],
            "existing_occupancy": occupancy_details,
            "daily_ledger": ledger,
            "totals": {
                "total_capacity": avail["total_capacity"],
                "occupied_quantity": avail["occupied_quantity"],
                "available_quantity": avail["available_quantity"],
            },
        }

    @staticmethod
    def _record_decision(db: Session, *, decision_type: str, result: str,
                         capacity: SupplyCapacity, quantity: int,
                         start: Optional[date], end: Optional[date],
                         available_quantity: int, basis: dict,
                         reason: Optional[str] = None, actor: Optional[str] = None,
                         reservation: Optional[CapacityReservation] = None):
        return crud_capacity_decision.create(
            db,
            decision_no=_gen_no("DEC"),
            reservation_id=reservation.id if reservation else None,
            supplier_id=capacity.supplier_id,
            material_id=capacity.material_id,
            decision_type=decision_type,
            result=result,
            requested_quantity=quantity,
            start_date=start,
            end_date=end,
            available_quantity=available_quantity,
            rule_version=RULE_VERSION,
            basis_json=json.dumps(basis, ensure_ascii=False),
            reason=reason,
            actor=actor,
        )

    # ---------- 失效清理（必须在写锁内） ----------

    @staticmethod
    def _expire_due_locked(db: Session, now: datetime) -> List[CapacityReservation]:
        """把已过有效期的预约物化为 expired。占用聚合本身也会排除它们，
        这里仅做状态落地与事件留痕。"""
        expired = []
        for res in crud_capacity_reservation.find_due(db, now):
            res.status = "expired"
            res.expired_at = now
            for day in res.days:
                day.remaining_quantity = 0
            crud_capacity_reservation.add_event(
                db, reservation_id=res.id, event_type="expire",
                quantity=res.quantity, detail=f"超过有效期 {res.expires_at} 自动失效"
            )
            capacity = crud_supply_capacity.get_by_supplier_and_material(
                db, res.supplier_id, res.material_id
            )
            if capacity:
                basis = CapacityService._build_basis(
                    db, capacity=capacity, start=res.start_date, end=res.end_date,
                    quantity=res.quantity, plan=None,
                    exclude_reservation_id=res.id, now=now,
                )
                CapacityService._record_decision(
                    db, decision_type="expire", result="expired",
                    capacity=capacity, quantity=res.quantity,
                    start=res.start_date, end=res.end_date,
                    available_quantity=basis["totals"]["available_quantity"],
                    basis=basis, reason="预约超期未转单，自动失效",
                    reservation=res,
                )
            expired.append(res)
        if expired:
            db.flush()
        return expired

    @staticmethod
    def expire_due(db: Session, now: Optional[datetime] = None) -> int:
        now = now or datetime.now()

        def _attempt():
            try:
                expired = CapacityService._expire_due_locked(db, now)
                db.commit()
            except Exception:
                db.rollback()
                raise
            return len(expired)

        return run_with_lock_retry(_attempt)

    # ---------- 预约 ----------

    @staticmethod
    def _plan_allocation(availability: dict, quantity: int) -> Tuple[Dict[date, int], int]:
        """贪心排产：按日期先后把需求量填入每天剩余产能。"""
        plan: Dict[date, int] = {}
        remaining = quantity
        for row in availability["days"]:
            if remaining <= 0:
                break
            if not row["is_working_day"]:
                continue
            free = row["daily_capacity"] - row["occupied_quantity"]
            take = min(max(0, free), remaining)
            if take > 0:
                plan[row["day_date"]] = take
                remaining -= take
        return plan, remaining

    @staticmethod
    def reserve(db: Session, *, supplier_id: int, material_id: int,
                quantity: int, start_date: date, end_date: date,
                owner_team: str = "default",
                purchase_suggestion_id: Optional[int] = None,
                expires_in_hours: Optional[int] = 24,
                actor: Optional[str] = None,
                _decision_type: str = "reserve") -> CapacityReservation:
        if quantity <= 0:
            raise CapacityError("预约数量必须大于0")
        if end_date < start_date:
            raise CapacityError("结束日期不能早于开始日期")
        if start_date < date.today():
            raise CapacityError("预约开始日期不能早于今天")

        def _attempt():
            try:
                now = datetime.now()
                CapacityService._expire_due_locked(db, now)

                capacity = crud_supply_capacity.get_by_supplier_and_material_for_update(
                    db, supplier_id, material_id
                )
                if not capacity:
                    raise CapacityError("该供应商未维护此物料的供应能力，无法预约产能")
                availability = CapacityService.get_availability(
                    db, supplier_id, material_id, start_date, end_date, now=now
                )
                if availability["working_days"] == 0:
                    basis = CapacityService._build_basis(
                        db, capacity=capacity, start=start_date, end=end_date,
                        quantity=quantity, plan=None,
                        exclude_reservation_id=None, now=now,
                    )
                    CapacityService._record_decision(
                        db, decision_type=_decision_type, result="rejected",
                        capacity=capacity, quantity=quantity,
                        start=start_date, end=end_date,
                        available_quantity=0, basis=basis,
                        reason="区间内没有生产日（节假日/周末停产）", actor=actor,
                    )
                    db.commit()
                    raise CapacityUnavailableError(
                        "预约区间内没有生产日，无法安排生产",
                        ledger=basis["daily_ledger"], shortage=quantity,
                    )

                plan, shortage = CapacityService._plan_allocation(availability, quantity)
                basis = CapacityService._build_basis(
                    db, capacity=capacity, start=start_date, end=end_date,
                    quantity=quantity, plan=plan,
                    exclude_reservation_id=None, now=now,
                )
                if shortage > 0:
                    CapacityService._record_decision(
                        db, decision_type=_decision_type, result="rejected",
                        capacity=capacity, quantity=quantity,
                        start=start_date, end=end_date,
                        available_quantity=availability["available_quantity"],
                        basis=basis,
                        reason=f"可用产能不足，缺口{shortage}", actor=actor,
                    )
                    db.commit()
                    raise CapacityUnavailableError(
                        f"供应商{supplier_id}在{start_date}~{end_date}的可用产能"
                        f"{availability['available_quantity']}不足以覆盖预约{quantity}，"
                        f"缺口{shortage}",
                        ledger=basis["daily_ledger"], shortage=shortage,
                    )

                daily_quantity = max(plan.values())
                expires_at = (
                    now + timedelta(hours=expires_in_hours)
                    if expires_in_hours and expires_in_hours > 0 else None
                )
                reservation = CapacityReservation(
                    reservation_no=_gen_no("RSV"),
                    supplier_id=supplier_id,
                    material_id=material_id,
                    supply_capacity_id=capacity.id,
                    start_date=start_date,
                    end_date=end_date,
                    quantity=quantity,
                    daily_quantity=daily_quantity,
                    status="reserved",
                    owner_team=owner_team,
                    purchase_suggestion_id=purchase_suggestion_id,
                    expires_at=expires_at,
                )
                db.add(reservation)
                db.flush()
                for day_date, qty in sorted(plan.items()):
                    db.add(CapacityReservationDay(
                        reservation_id=reservation.id,
                        day_date=day_date, quantity=qty,
                        remaining_quantity=qty, is_working_day=True,
                    ))
                crud_capacity_reservation.add_event(
                    db, reservation_id=reservation.id, event_type="create",
                    quantity=quantity,
                    detail=f"{start_date}~{end_date} 预约{quantity}，"
                           f"有效期至{expires_at.isoformat() if expires_at else '长期'}",
                    actor=actor,
                )
                CapacityService._record_decision(
                    db, decision_type=_decision_type, result="approved",
                    capacity=capacity, quantity=quantity,
                    start=start_date, end=end_date,
                    available_quantity=availability["available_quantity"],
                    basis=basis, reason="预约成功", actor=actor,
                    reservation=reservation,
                )
                db.commit()
                db.refresh(reservation)
                return reservation
            except Exception:
                db.rollback()
                raise

        return run_with_lock_retry(_attempt)

    # ---------- 确认（转单后把预约转为正式长期占用） ----------

    @staticmethod
    def _shrink_to_quantity_locked(db: Session, reservation: CapacityReservation,
                                   keep_quantity: int, now: datetime,
                                   actor: Optional[str]) -> int:
        """转单量小于预约量时，保留最早生产日期上的 keep_quantity 占用，
        释放多余部分（部分释放），返回释放量。原始预约量保留在 quantity 列。"""
        active_days = sorted(
            (d for d in reservation.days if d.remaining_quantity > 0),
            key=lambda x: x.day_date,
        )
        total = sum(d.remaining_quantity for d in active_days)
        excess = total - keep_quantity
        if excess <= 0:
            return 0
        left = excess
        for day in reversed(active_days):  # 优先保留靠前的生产日期，从后往前释放
            take = min(left, day.remaining_quantity)
            day.remaining_quantity -= take
            left -= take
            if left <= 0:
                break
        crud_capacity_reservation.add_event(
            db, reservation_id=reservation.id, event_type="partial_release",
            quantity=excess,
            detail=f"转单确认量{keep_quantity}小于预约量{total}，"
                   f"自动释放多余占用{excess}",
            actor=actor,
        )
        kept = [d.remaining_quantity for d in active_days if d.remaining_quantity > 0]
        reservation.daily_quantity = max(kept) if kept else 0
        return excess

    @staticmethod
    def _confirm_locked(db: Session, reservation: CapacityReservation,
                        order: PurchaseOrder, now: datetime,
                        actor: Optional[str]):
        reservation.status = "reserved"
        reservation.purchase_order_id = order.id
        reservation.confirmed_at = now
        reservation.expires_at = None  # 已转单的占用不再因超期失效
        crud_capacity_reservation.add_event(
            db, reservation_id=reservation.id, event_type="confirm",
            quantity=reservation.quantity,
            detail=f"转单 {order.order_no} 确认占用，预约转为正式长期占用",
            actor=actor,
        )

    # ---------- 释放 ----------

    @staticmethod
    def release(db: Session, reservation_id: int, *,
                quantity: Optional[int] = None,
                dates: Optional[List[date]] = None,
                date_quantities: Optional[Dict[str, int]] = None,
                reason: Optional[str] = None, actor: Optional[str] = None,
                _commit: bool = True) -> CapacityReservation:
        """整单释放 / 指定日期释放 / 按数量从后往前部分释放。

        采用追加式更新：原始占用保留在 day.quantity，当前占用记录在
        day.remaining_quantity，全部动作另外写入事件流与决策记录。
        """

        def _attempt():
            try:
                now = datetime.now()
                CapacityService._expire_due_locked(db, now)
                res = crud_capacity_reservation.get(db, reservation_id)
                if not res:
                    raise CapacityError(f"预约不存在: {reservation_id}")
                if res.status == "expired":
                    raise CapacityError("预约已超期失效，无需释放")
                if res.status != "reserved":
                    raise CapacityError(f"预约状态为 {res.status}，不可释放")

                active_days = [d for d in sorted(res.days, key=lambda x: x.day_date)
                               if d.remaining_quantity > 0]
                total_remaining = sum(d.remaining_quantity for d in active_days)
                released_total = 0

                day_map = {d.day_date: d for d in active_days}

                if date_quantities:
                    for ds, qty in date_quantities.items():
                        target = date.fromisoformat(ds)
                        day = day_map.get(target)
                        if not day:
                            raise CapacityError(f"{target} 没有可释放的占用")
                        if qty <= 0 or qty > day.remaining_quantity:
                            raise CapacityError(
                                f"{target} 可释放{day.remaining_quantity}，请求释放{qty}"
                            )
                        day.remaining_quantity -= qty
                        released_total += qty
                    event_type = "partial_release"
                elif dates:
                    for target in dates:
                        day = day_map.get(target)
                        if not day:
                            raise CapacityError(f"{target} 没有可释放的占用")
                        released_total += day.remaining_quantity
                        day.remaining_quantity = 0
                    event_type = "partial_release"
                elif quantity is not None:
                    if quantity <= 0:
                        raise CapacityError("释放数量必须大于0")
                    if quantity > total_remaining:
                        raise CapacityError(
                            f"预约剩余占用{total_remaining}，不足以释放{quantity}"
                        )
                    left = quantity
                    for day in reversed(active_days):  # 从最晚的生产日期往回释放
                        take = min(left, day.remaining_quantity)
                        day.remaining_quantity -= take
                        left -= take
                        released_total += take
                        if left <= 0:
                            break
                    event_type = "partial_release"
                else:
                    released_total = total_remaining
                    for day in active_days:
                        day.remaining_quantity = 0
                    event_type = "release"

                if event_type == "release" or all(
                    d.remaining_quantity == 0 for d in res.days
                ):
                    res.status = "released"
                    res.released_at = now
                    res.release_reason = reason
                    final_event = "release"
                else:
                    final_event = "partial_release"

                crud_capacity_reservation.add_event(
                    db, reservation_id=res.id, event_type=final_event,
                    quantity=released_total,
                    detail=(reason or "释放产能"), actor=actor,
                )
                capacity = db.get(SupplyCapacity, res.supply_capacity_id)
                if not capacity:  # 正常受外键约束不会发生，防御性处理
                    raise CapacityError(
                        f"预约{reservation_id}关联的供应能力已不存在，无法记录释放依据"
                    )
                basis = CapacityService._build_basis(
                    db, capacity=capacity, start=res.start_date, end=res.end_date,
                    quantity=released_total, plan=None,
                    exclude_reservation_id=res.id, now=now,
                )
                CapacityService._record_decision(
                    db, decision_type=final_event,
                    result="released" if res.status == "released" else "partially_released",
                    capacity=capacity, quantity=released_total,
                    start=res.start_date, end=res.end_date,
                    available_quantity=basis["totals"]["available_quantity"],
                    basis=basis, reason=reason, actor=actor, reservation=res,
                )
                if _commit:
                    db.commit()
                    db.refresh(res)
                else:
                    db.flush()
                return res
            except Exception:
                if _commit:
                    db.rollback()
                raise

        return run_with_lock_retry(_attempt)

    # ---------- 转单：同一事务内重新核对可用量 ----------

    @staticmethod
    def convert_suggestion_with_capacity(
        db: Session, *, suggestion_id: int, order_no: str,
        supplier_id: Optional[int] = None, quantity: Optional[int] = None,
        start_date: Optional[date] = None, end_date: Optional[date] = None,
        expected_date: Optional[date] = None,
        reservation_id: Optional[int] = None, actor: Optional[str] = None
    ) -> Tuple[PurchaseOrder, CapacityReservation]:
        existing_order = crud_purchase_order.get_by_order_no(db, order_no)
        if existing_order:
            raise CapacityError("订单号已存在")

        def _attempt():
            try:
                now = datetime.now()
                CapacityService._expire_due_locked(db, now)

                # 锁定采购建议行，防止同一建议被两个请求并发转单
                suggestion = (
                    db.query(PurchaseSuggestion)
                    .filter(PurchaseSuggestion.id == suggestion_id)
                    .with_for_update()
                    .first()
                )
                if not suggestion:
                    raise CapacityError(f"采购建议不存在: {suggestion_id}")
                if suggestion.status not in ("pending", "sent_to_supplier"):
                    raise CapacityError(
                        f"采购建议状态为 {suggestion.status}，不可转单"
                    )

                final_qty = quantity or suggestion.suggested_quantity
                if final_qty <= 0:
                    raise CapacityError("转单数量必须大于0")
                final_supplier_id = supplier_id or suggestion.suggested_supplier_id
                if not final_supplier_id:
                    raise CapacityError("必须指定供应商")

                capacity = crud_supply_capacity.get_by_supplier_and_material_for_update(
                    db, final_supplier_id, suggestion.material_id
                )
                if not capacity:
                    raise CapacityError(
                        "该供应商未维护此物料的供应能力，无法核对产能后转单"
                    )

                final_end = end_date or expected_date or suggestion.expected_delivery_date
                final_start = start_date or date.today()
                if not final_end:
                    # 没有任何日期依据时，按日产能估算需要的生产天数
                    daily = max(1, capacity.daily_capacity or 1)
                    final_end = final_start + timedelta(
                        days=(final_qty + daily - 1) // daily + max(0, capacity.delivery_days or 0)
                    )
                if final_end < final_start:
                    raise CapacityError("交货日期不能早于占用开始日期")

                reservation = None
                if reservation_id is not None:
                    reservation = crud_capacity_reservation.get(db, reservation_id)
                    if not reservation:
                        raise CapacityError(f"预约不存在: {reservation_id}")
                    if reservation.status != "reserved":
                        raise CapacityError(
                            f"预约状态为 {reservation.status}，不能用于转单"
                        )
                    if reservation.expires_at and reservation.expires_at <= now:
                        raise CapacityError("预约已超期，请重新预约产能")
                    if reservation.supplier_id != final_supplier_id:
                        raise CapacityError("预约供应商与转单供应商不一致")
                    if reservation.material_id != suggestion.material_id:
                        raise CapacityError("预约物料与采购建议物料不一致")
                    if (reservation.purchase_suggestion_id
                            and reservation.purchase_suggestion_id != suggestion_id):
                        raise CapacityError("该预约属于其他采购建议")
                    active_remaining = sum(
                        d.remaining_quantity for d in reservation.days
                    )
                    if active_remaining < final_qty:
                        raise CapacityError(
                            f"预约剩余量{active_remaining}小于转单量{final_qty}，"
                            "请先调整预约"
                        )
                    if reservation.start_date != final_start or reservation.end_date != final_end:
                        raise CapacityError(
                            "预约日期区间与本次转单不一致，请释放后重新预约或使用改期"
                        )
                    # 关键：即使拿着既有预约，仍在当前写事务内重新核对区间可用量，
                    # 排除自身后不能超卖。
                    availability = CapacityService.get_availability(
                        db, final_supplier_id, suggestion.material_id,
                        final_start, final_end,
                        exclude_reservation_id=reservation.id, now=now,
                    )
                    # 转单量小于预约占用时，只保留与转单量匹配的逐日计划，
                    # 其余部分在同一事务内自动释放。
                    keep_plan: Dict[date, int] = {}
                    left_keep = final_qty
                    for day in sorted(reservation.days, key=lambda x: x.day_date):
                        if left_keep <= 0:
                            break
                        keep = min(day.remaining_quantity, left_keep)
                        if keep > 0:
                            keep_plan[day.day_date] = keep
                            left_keep -= keep
                    plan = keep_plan
                    basis = CapacityService._build_basis(
                        db, capacity=capacity, start=final_start, end=final_end,
                        quantity=final_qty, plan=plan,
                        exclude_reservation_id=reservation.id, now=now,
                    )
                    overbooked = [
                        r for r in basis["daily_ledger"]
                        if r["available_after_request"] < 0
                    ]
                    if overbooked:
                        CapacityService._record_decision(
                            db, decision_type="convert", result="rejected",
                            capacity=capacity, quantity=final_qty,
                            start=final_start, end=final_end,
                            available_quantity=availability["available_quantity"],
                            basis=basis, reason="重新核对发现产能已被其他预约占满",
                            actor=actor, reservation=reservation,
                        )
                        db.commit()
                        raise CapacityUnavailableError(
                            "转单前重新核对发现该区间产能已被占满，拒绝转单",
                            ledger=basis["daily_ledger"],
                        )
                else:
                    # 没有预约：现场核对并占用（同样在本事务内）
                    availability = CapacityService.get_availability(
                        db, final_supplier_id, suggestion.material_id,
                        final_start, final_end, now=now,
                    )
                    plan, shortage = CapacityService._plan_allocation(
                        availability, final_qty
                    )
                    basis = CapacityService._build_basis(
                        db, capacity=capacity, start=final_start, end=final_end,
                        quantity=final_qty, plan=plan,
                        exclude_reservation_id=None, now=now,
                    )
                    if shortage > 0:
                        CapacityService._record_decision(
                            db, decision_type="convert", result="rejected",
                            capacity=capacity, quantity=final_qty,
                            start=final_start, end=final_end,
                            available_quantity=availability["available_quantity"],
                            basis=basis,
                            reason=f"无预约直接转单，可用产能不足，缺口{shortage}",
                            actor=actor,
                        )
                        db.commit()
                        raise CapacityUnavailableError(
                            f"供应商{final_supplier_id}在{final_start}~{final_end}"
                            f"可用产能{availability['available_quantity']}，"
                            f"不足以覆盖{final_qty}，缺口{shortage}",
                            ledger=basis["daily_ledger"], shortage=shortage,
                        )
                    reservation = CapacityReservation(
                        reservation_no=_gen_no("RSV"),
                        supplier_id=final_supplier_id,
                        material_id=suggestion.material_id,
                        supply_capacity_id=capacity.id,
                        start_date=final_start,
                        end_date=final_end,
                        quantity=final_qty,
                        daily_quantity=max(plan.values()),
                        status="reserved",
                        owner_team=actor or "purchase",
                        purchase_suggestion_id=suggestion_id,
                        expires_at=None,
                    )
                    db.add(reservation)
                    db.flush()
                    for day_date, qty in sorted(plan.items()):
                        db.add(CapacityReservationDay(
                            reservation_id=reservation.id,
                            day_date=day_date, quantity=qty,
                            remaining_quantity=qty, is_working_day=True,
                        ))
                    crud_capacity_reservation.add_event(
                        db, reservation_id=reservation.id, event_type="create",
                        quantity=final_qty,
                        detail=f"转单 {order_no} 现场核对后占用 {final_start}~{final_end}",
                        actor=actor,
                    )

                # 核对通过 → 同一事务内落订单、确认占用、改建议状态。
                # 这里不能用 crud.create/update（它们内部会提前 commit，
                # 会把“核对+占用”拆成两个事务），全部 flush，由本事务统一提交。
                order = PurchaseOrder(
                    order_no=order_no,
                    supplier_id=final_supplier_id,
                    material_id=suggestion.material_id,
                    quantity=final_qty,
                    expected_date=final_end,
                    status="ordered",
                    remark=f"由采购建议#{suggestion_id}生成，占用预约{reservation.reservation_no}"
                )
                db.add(order)
                try:
                    db.flush()
                except IntegrityError:
                    raise CapacityError(f"订单号已存在: {order_no}")
                if reservation_id is not None:
                    # 既有预约：转单量小于预约占用时，同事务内释放多余部分
                    CapacityService._shrink_to_quantity_locked(
                        db, reservation, final_qty, now, actor
                    )
                CapacityService._confirm_locked(db, reservation, order, now, actor)
                suggestion.status = "converted"
                db.flush()
                CapacityService._record_decision(
                    db, decision_type="convert", result="approved",
                    capacity=capacity, quantity=final_qty,
                    start=final_start, end=final_end,
                    available_quantity=basis["totals"]["available_quantity"],
                    basis=basis, reason=f"转单 {order_no} 成功",
                    actor=actor, reservation=reservation,
                )
                db.commit()
                db.refresh(order)
                db.refresh(reservation)
                return order, reservation
            except Exception:
                db.rollback()
                raise

        return run_with_lock_retry(_attempt)

    # ---------- 改期 / 转供应商：同一事务内重新核对 ----------

    @staticmethod
    def reschedule_order(
        db: Session, *, order_id: int,
        new_supplier_id: Optional[int] = None,
        new_start_date: Optional[date] = None,
        new_end_date: Optional[date] = None,
        new_quantity: Optional[int] = None,
        reason: Optional[str] = None, actor: Optional[str] = None
    ) -> Tuple[PurchaseOrder, CapacityReservation]:
        def _attempt():
            try:
                now = datetime.now()
                CapacityService._expire_due_locked(db, now)

                order = crud_purchase_order.get(db, order_id)
                if not order:
                    raise CapacityError(f"采购订单不存在: {order_id}")
                final_supplier_id = new_supplier_id or order.supplier_id
                final_qty = new_quantity or order.quantity
                final_end = new_end_date or order.expected_date
                final_start = new_start_date or date.today()
                if final_qty <= 0:
                    raise CapacityError("数量必须大于0")
                if final_end < final_start:
                    raise CapacityError("新日期区间不合法")

                old_reservation = crud_capacity_reservation.get_by_order(db, order_id)

                capacity = crud_supply_capacity.get_by_supplier_and_material_for_update(
                    db, final_supplier_id, order.material_id
                )
                if not capacity:
                    raise CapacityError("目标供应商未维护此物料的供应能力")

                # 先按新方案核对可用量（旧预约仍占着，需要排除它自己；
                # 换供应商时旧预约在另一个供应能力上，天然不占新供应商的量）
                exclude_id = (
                    old_reservation.id
                    if old_reservation and old_reservation.supplier_id == final_supplier_id
                    else None
                )
                availability = CapacityService.get_availability(
                    db, final_supplier_id, order.material_id,
                    final_start, final_end,
                    exclude_reservation_id=exclude_id, now=now,
                )
                plan, shortage = CapacityService._plan_allocation(
                    availability, final_qty
                )
                basis = CapacityService._build_basis(
                    db, capacity=capacity, start=final_start, end=final_end,
                    quantity=final_qty, plan=plan,
                    exclude_reservation_id=exclude_id, now=now,
                )
                if shortage > 0 or not plan:
                    CapacityService._record_decision(
                        db, decision_type="reschedule", result="rejected",
                        capacity=capacity, quantity=final_qty,
                        start=final_start, end=final_end,
                        available_quantity=availability["available_quantity"],
                        basis=basis,
                        reason=f"改期/转单重新核对失败，产能缺口{shortage}",
                        actor=actor,
                        reservation=old_reservation,
                    )
                    db.commit()
                    raise CapacityUnavailableError(
                        f"改期后供应商{final_supplier_id}在{final_start}~{final_end}"
                        f"可用产能{availability['available_quantity']}，"
                        f"不足以覆盖{final_qty}，缺口{shortage}",
                        ledger=basis["daily_ledger"], shortage=shortage,
                    )

                # 核对通过：旧预约解除与订单的关联并释放（历史占用完整保留），
                # 新方案建立新预约并挂到订单上——全部同一事务。
                if old_reservation:
                    old_reservation.purchase_order_id = None
                    old_reservation.status = "released"
                    old_reservation.released_at = now
                    old_reservation.release_reason = f"改期释放：{reason or ''}"
                    for day in old_reservation.days:
                        day.remaining_quantity = 0
                    crud_capacity_reservation.add_event(
                        db, reservation_id=old_reservation.id,
                        event_type="release", quantity=old_reservation.quantity,
                        detail=f"订单 {order.order_no} 改期/转供应商，旧占用释放",
                        actor=actor,
                    )
                    old_capacity = db.get(SupplyCapacity, old_reservation.supply_capacity_id)
                    old_basis = CapacityService._build_basis(
                        db, capacity=old_capacity,
                        start=old_reservation.start_date, end=old_reservation.end_date,
                        quantity=old_reservation.quantity, plan=None,
                        exclude_reservation_id=old_reservation.id, now=now,
                    )
                    CapacityService._record_decision(
                        db, decision_type="reschedule_release_old",
                        result="released", capacity=old_capacity,
                        quantity=old_reservation.quantity,
                        start=old_reservation.start_date,
                        end=old_reservation.end_date,
                        available_quantity=old_basis["totals"]["available_quantity"],
                        basis=old_basis,
                        reason=reason or "改期释放旧占用",
                        actor=actor, reservation=old_reservation,
                    )
                    # 显式先落旧占用释放，避免与新预约的 purchase_order_id
                    # 唯一约束在同一 flush 中冲突
                    db.flush()

                new_reservation = CapacityReservation(
                    reservation_no=_gen_no("RSV"),
                    supplier_id=final_supplier_id,
                    material_id=order.material_id,
                    supply_capacity_id=capacity.id,
                    start_date=final_start,
                    end_date=final_end,
                    quantity=final_qty,
                    daily_quantity=max(plan.values()),
                    status="reserved",
                    owner_team=actor or "purchase",
                    purchase_suggestion_id=None,
                    purchase_order_id=order.id,
                    expires_at=None,
                    confirmed_at=now,
                )
                db.add(new_reservation)
                db.flush()
                for day_date, qty in sorted(plan.items()):
                    db.add(CapacityReservationDay(
                        reservation_id=new_reservation.id,
                        day_date=day_date, quantity=qty,
                        remaining_quantity=qty, is_working_day=True,
                    ))
                crud_capacity_reservation.add_event(
                    db, reservation_id=new_reservation.id, event_type="reschedule",
                    quantity=final_qty,
                    detail=f"订单 {order.order_no} 改期后新占用 {final_start}~{final_end}",
                    actor=actor,
                )

                order.supplier_id = final_supplier_id
                order.quantity = final_qty
                order.expected_date = final_end
                CapacityService._record_decision(
                    db, decision_type="reschedule", result="approved",
                    capacity=capacity, quantity=final_qty,
                    start=final_start, end=final_end,
                    available_quantity=basis["totals"]["available_quantity"],
                    basis=basis, reason=reason or "改期成功",
                    actor=actor, reservation=new_reservation,
                )
                db.commit()
                db.refresh(order)
                db.refresh(new_reservation)
                return order, new_reservation
            except Exception:
                db.rollback()
                raise

        return run_with_lock_retry(_attempt)
