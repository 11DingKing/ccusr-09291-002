import json
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from typing import List, Optional
from datetime import date

from app.database import get_db
from app.schemas import (
    CapacityReservation, CapacityReservationCreate,
    CapacityAvailability, CapacityAvailabilityQuery, CapacityDayAvailability,
    CapacityReleaseRequest, CapacityRescheduleRequest,
    CapacityDecisionBase, ConvertWithCapacityRequest,
    ProductionCalendar, ProductionCalendarUpsert,
    PurchaseOrder,
)
from app.services.capacity import (
    CapacityService, CapacityError, CapacityUnavailableError,
)
from app.crud.capacity import (
    crud_capacity_reservation, crud_capacity_decision,
    crud_production_calendar,
)

router = APIRouter(prefix="/capacity", tags=["供应商产能预约"])


def _handle(exc: Exception):
    if isinstance(exc, CapacityUnavailableError):
        return HTTPException(
            status_code=409,
            detail={
                "message": str(exc),
                "shortage": exc.shortage,
                "ledger": exc.ledger,
            },
        )
    if isinstance(exc, CapacityError):
        return HTTPException(status_code=400, detail=str(exc))
    return HTTPException(status_code=500, detail=str(exc))


# ---------- 生产日历（节假日停产 / 加班） ----------

@router.get("/calendar", response_model=List[ProductionCalendar])
def list_calendar(
    supplier_id: Optional[int] = None,
    start: Optional[date] = None,
    end: Optional[date] = None,
    db: Session = Depends(get_db),
):
    return crud_production_calendar.get_multi(db, supplier_id, start, end)


@router.put("/calendar", response_model=ProductionCalendar)
def upsert_calendar(item: ProductionCalendarUpsert, db: Session = Depends(get_db)):
    if item.day_type not in ("closed", "working"):
        raise HTTPException(status_code=400, detail="day_type 只能是 closed 或 working")
    try:
        row = crud_production_calendar.upsert(
            db, supplier_id=item.supplier_id,
            calendar_date=item.calendar_date,
            day_type=item.day_type, name=item.name,
        )
        db.commit()
        db.refresh(row)
        return row
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc))


@router.delete("/calendar/{calendar_id}")
def delete_calendar(calendar_id: int, db: Session = Depends(get_db)):
    ok = crud_production_calendar.delete(db, calendar_id)
    if not ok:
        raise HTTPException(status_code=404, detail="日历记录不存在")
    db.commit()
    return {"message": "已删除"}


# ---------- 可用量查询 ----------

@router.post("/availability", response_model=CapacityAvailability)
def query_availability(query: CapacityAvailabilityQuery, db: Session = Depends(get_db)):
    try:
        return CapacityService.get_availability(
            db, query.supplier_id, query.material_id,
            query.start_date, query.end_date,
            required_quantity=query.quantity,
        )
    except CapacityError as exc:
        raise _handle(exc)


@router.get("/availability", response_model=CapacityAvailability)
def query_availability_get(
    supplier_id: int, material_id: int,
    start_date: date, end_date: date,
    quantity: Optional[int] = None,
    db: Session = Depends(get_db),
):
    try:
        return CapacityService.get_availability(
            db, supplier_id, material_id, start_date, end_date,
            required_quantity=quantity,
        )
    except CapacityError as exc:
        raise _handle(exc)


# ---------- 预约 / 占用 / 释放 / 失效 ----------

@router.post("/reservations", response_model=CapacityReservation, status_code=201)
def create_reservation(item: CapacityReservationCreate, db: Session = Depends(get_db)):
    try:
        return CapacityService.reserve(
            db,
            supplier_id=item.supplier_id,
            material_id=item.material_id,
            quantity=item.quantity,
            start_date=item.start_date,
            end_date=item.end_date,
            owner_team=item.owner_team,
            purchase_suggestion_id=item.purchase_suggestion_id,
            expires_in_hours=item.expires_in_hours,
            actor=item.actor,
        )
    except (CapacityError, CapacityUnavailableError) as exc:
        raise _handle(exc)


@router.get("/reservations", response_model=List[CapacityReservation])
def list_reservations(
    supplier_id: Optional[int] = None,
    material_id: Optional[int] = None,
    status: Optional[str] = None,
    db: Session = Depends(get_db),
):
    return crud_capacity_reservation.list_for(
        db, supplier_id=supplier_id, material_id=material_id, status=status
    )


@router.get("/reservations/{reservation_id}", response_model=CapacityReservation)
def get_reservation(reservation_id: int, db: Session = Depends(get_db)):
    res = crud_capacity_reservation.get(db, reservation_id)
    if not res:
        raise HTTPException(status_code=404, detail="预约不存在")
    return res


@router.post("/reservations/{reservation_id}/release",
             response_model=CapacityReservation)
def release_reservation(
    reservation_id: int, item: CapacityReleaseRequest,
    db: Session = Depends(get_db),
):
    try:
        return CapacityService.release(
            db, reservation_id,
            quantity=item.quantity,
            dates=item.dates,
            date_quantities=item.date_quantities,
            reason=item.reason, actor=item.actor,
        )
    except (CapacityError, CapacityUnavailableError) as exc:
        raise _handle(exc)


@router.post("/expire-due")
def expire_due(db: Session = Depends(get_db)):
    """手动触发超期失效；服务重启后也可由启动任务自动执行。"""
    remaining = CapacityService.expire_due(db)
    return {"message": "超期失效处理完成", "still_due": remaining}


# ---------- 转单（同一事务内重新核对可用量） ----------

@router.post("/suggestions/{suggestion_id}/convert-with-capacity",
             response_model=dict)
def convert_with_capacity(
    suggestion_id: int, item: ConvertWithCapacityRequest,
    db: Session = Depends(get_db),
):
    try:
        order, reservation = CapacityService.convert_suggestion_with_capacity(
            db,
            suggestion_id=suggestion_id,
            order_no=item.order_no,
            supplier_id=item.supplier_id,
            quantity=item.quantity,
            start_date=item.start_date,
            end_date=item.end_date,
            expected_date=item.expected_date,
            reservation_id=item.reservation_id,
            actor=item.actor,
        )
        return {
            "message": "转单成功，产能已在同一事务内核对并占用",
            "order": PurchaseOrder.model_validate(order).model_dump(),
            "reservation": CapacityReservation.model_validate(reservation).model_dump(),
        }
    except (CapacityError, CapacityUnavailableError) as exc:
        raise _handle(exc)


# ---------- 改期 / 转供应商 ----------

@router.post("/orders/{order_id}/reschedule", response_model=dict)
def reschedule_order(
    order_id: int, item: CapacityRescheduleRequest,
    db: Session = Depends(get_db),
):
    try:
        order, reservation = CapacityService.reschedule_order(
            db,
            order_id=order_id,
            new_supplier_id=item.new_supplier_id,
            new_start_date=item.new_start_date,
            new_end_date=item.new_end_date,
            new_quantity=item.new_quantity,
            reason=item.reason, actor=item.actor,
        )
        return {
            "message": "改期成功，旧占用已释放、新占用在同一事务内核验并建立",
            "order": PurchaseOrder.model_validate(order).model_dump(),
            "reservation": CapacityReservation.model_validate(reservation).model_dump(),
        }
    except (CapacityError, CapacityUnavailableError) as exc:
        raise _handle(exc)


# ---------- 决策依据留存 ----------

@router.get("/decisions", response_model=List[CapacityDecisionBase])
def list_decisions(
    reservation_id: Optional[int] = None,
    supplier_id: Optional[int] = None,
    limit: int = Query(100, le=500),
    db: Session = Depends(get_db),
):
    return crud_capacity_decision.list_for(
        db, reservation_id=reservation_id, supplier_id=supplier_id, limit=limit
    )


@router.get("/decisions/{decision_id}/basis")
def get_decision_basis(decision_id: int, db: Session = Depends(get_db)):
    row = crud_capacity_decision.get(db, decision_id)
    if not row:
        raise HTTPException(status_code=404, detail="决定记录不存在")
    return {
        "id": row.id,
        "decision_no": row.decision_no,
        "decision_type": row.decision_type,
        "result": row.result,
        "rule_version": row.rule_version,
        "reason": row.reason,
        "actor": row.actor,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "basis": json.loads(row.basis_json),
    }
