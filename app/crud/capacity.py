from sqlalchemy.orm import Session
from sqlalchemy import func
from datetime import date, datetime
from typing import List, Optional, Dict

from app.models import (
    CapacityReservation, CapacityReservationDay, CapacityReservationEvent,
    CapacityDecision, ProductionCalendar,
)


class CRUDProductionCalendar:
    def get_for_date(
        self, db: Session, calendar_date: date, supplier_id: Optional[int]
    ) -> Optional[ProductionCalendar]:
        q = db.query(ProductionCalendar).filter(
            ProductionCalendar.calendar_date == calendar_date
        )
        if supplier_id is not None:
            row = q.filter(ProductionCalendar.supplier_id == supplier_id).first()
            if row:
                return row
        return q.filter(ProductionCalendar.supplier_id.is_(None)).first()

    def get_range_map(
        self, db: Session, start: date, end: date, supplier_id: Optional[int]
    ) -> Dict[date, ProductionCalendar]:
        """返回区间内的日历覆盖项；供应商专属覆盖全厂日历。"""
        rows = db.query(ProductionCalendar).filter(
            ProductionCalendar.calendar_date >= start,
            ProductionCalendar.calendar_date <= end,
        ).all()
        result: Dict[date, ProductionCalendar] = {}
        for row in rows:
            if row.supplier_id is None:
                result.setdefault(row.calendar_date, row)
        for row in rows:
            if supplier_id is not None and row.supplier_id == supplier_id:
                result[row.calendar_date] = row
        return result

    def get_multi(
        self, db: Session, supplier_id: Optional[int] = None,
        start: Optional[date] = None, end: Optional[date] = None
    ) -> List[ProductionCalendar]:
        q = db.query(ProductionCalendar)
        if supplier_id is not None:
            q = q.filter(ProductionCalendar.supplier_id == supplier_id)
        if start:
            q = q.filter(ProductionCalendar.calendar_date >= start)
        if end:
            q = q.filter(ProductionCalendar.calendar_date <= end)
        return q.order_by(ProductionCalendar.calendar_date).all()

    def upsert(
        self, db: Session, *, supplier_id: Optional[int], calendar_date: date,
        day_type: str, name: Optional[str]
    ) -> ProductionCalendar:
        existing = self.get_for_date(db, calendar_date, supplier_id)
        if existing:
            existing.day_type = day_type
            existing.name = name
            db.flush()
            return existing
        obj = ProductionCalendar(
            supplier_id=supplier_id, calendar_date=calendar_date,
            day_type=day_type, name=name
        )
        db.add(obj)
        db.flush()
        return obj

    def delete(self, db: Session, calendar_id: int) -> bool:
        obj = db.query(ProductionCalendar).filter(
            ProductionCalendar.id == calendar_id
        ).first()
        if not obj:
            return False
        db.delete(obj)
        db.flush()
        return True


crud_production_calendar = CRUDProductionCalendar()


class CRUDCapacityReservation:
    def get(self, db: Session, reservation_id: int) -> Optional[CapacityReservation]:
        return db.query(CapacityReservation).filter(
            CapacityReservation.id == reservation_id
        ).first()

    def get_by_no(self, db: Session, reservation_no: str) -> Optional[CapacityReservation]:
        return db.query(CapacityReservation).filter(
            CapacityReservation.reservation_no == reservation_no
        ).first()

    def get_by_order(self, db: Session, order_id: int) -> Optional[CapacityReservation]:
        return db.query(CapacityReservation).filter(
            CapacityReservation.purchase_order_id == order_id
        ).first()

    def list_for(
        self, db: Session, *, supplier_id: Optional[int] = None,
        material_id: Optional[int] = None, status: Optional[str] = None,
        include_days: bool = False
    ) -> List[CapacityReservation]:
        q = db.query(CapacityReservation)
        if supplier_id is not None:
            q = q.filter(CapacityReservation.supplier_id == supplier_id)
        if material_id is not None:
            q = q.filter(CapacityReservation.material_id == material_id)
        if status:
            q = q.filter(CapacityReservation.status == status)
        q = q.order_by(CapacityReservation.start_date.desc(), CapacityReservation.id.desc())
        rows = q.all()
        if include_days:
            for r in rows:
                _ = r.days
        return rows

    def active_occupancy_by_day(
        self,
        db: Session,
        *,
        supplier_id: int,
        material_id: int,
        start: date,
        end: date,
        now: Optional[datetime] = None,
        exclude_reservation_id: Optional[int] = None,
    ) -> Dict[date, int]:
        """聚合区间内仍然有效（reserved 且未过期）的逐日占用量。

        过期判断直接在 SQL 里完成：即使还没跑失效清理，
        过期预约也不会被计入可用量核对。
        """
        now = now or datetime.now()
        q = (
            db.query(
                CapacityReservationDay.day_date,
                func.coalesce(func.sum(CapacityReservationDay.remaining_quantity), 0),
            )
            .join(CapacityReservation, CapacityReservation.id == CapacityReservationDay.reservation_id)
            .filter(
                CapacityReservation.supplier_id == supplier_id,
                CapacityReservation.material_id == material_id,
                CapacityReservation.status == "reserved",
                CapacityReservationDay.day_date >= start,
                CapacityReservationDay.day_date <= end,
            )
            .filter(
                (CapacityReservation.expires_at.is_(None)) |
                (CapacityReservation.expires_at > now)
            )
            .group_by(CapacityReservationDay.day_date)
        )
        if exclude_reservation_id is not None:
            q = q.filter(CapacityReservation.id != exclude_reservation_id)
        return {day_date: int(total) for day_date, total in q.all()}

    def active_occupancy_details(
        self,
        db: Session,
        *,
        supplier_id: int,
        material_id: int,
        start: date,
        end: date,
        now: Optional[datetime] = None,
        exclude_reservation_id: Optional[int] = None,
    ) -> List[dict]:
        """逐日占用明细（决策依据用）：列出每个有效预约在每天占了多少。"""
        now = now or datetime.now()
        q = (
            db.query(
                CapacityReservation.reservation_no,
                CapacityReservation.owner_team,
                CapacityReservation.purchase_order_id,
                CapacityReservationDay.day_date,
                CapacityReservationDay.remaining_quantity,
            )
            .join(CapacityReservation, CapacityReservation.id == CapacityReservationDay.reservation_id)
            .filter(
                CapacityReservation.supplier_id == supplier_id,
                CapacityReservation.material_id == material_id,
                CapacityReservation.status == "reserved",
                CapacityReservationDay.remaining_quantity > 0,
                CapacityReservationDay.day_date >= start,
                CapacityReservationDay.day_date <= end,
            )
            .filter(
                (CapacityReservation.expires_at.is_(None)) |
                (CapacityReservation.expires_at > now)
            )
            .order_by(CapacityReservationDay.day_date, CapacityReservation.reservation_no)
        )
        if exclude_reservation_id is not None:
            q = q.filter(CapacityReservation.id != exclude_reservation_id)
        return [
            {
                "reservation_no": no,
                "owner_team": team,
                "purchase_order_id": po_id,
                "day_date": d.isoformat(),
                "remaining_quantity": int(qty),
            }
            for no, team, po_id, d, qty in q.all()
        ]

    def find_due(self, db: Session, now: datetime) -> List[CapacityReservation]:
        return db.query(CapacityReservation).filter(
            CapacityReservation.status == "reserved",
            CapacityReservation.expires_at.isnot(None),
            CapacityReservation.expires_at <= now,
        ).all()

    def add_event(
        self, db: Session, *, reservation_id: int, event_type: str,
        quantity: int = 0, detail: Optional[str] = None, actor: Optional[str] = None
    ) -> CapacityReservationEvent:
        event = CapacityReservationEvent(
            reservation_id=reservation_id, event_type=event_type,
            quantity=quantity, detail=detail, actor=actor
        )
        db.add(event)
        db.flush()
        return event


crud_capacity_reservation = CRUDCapacityReservation()


class CRUDCapacityDecision:
    def get(self, db: Session, decision_id: int) -> Optional[CapacityDecision]:
        return db.query(CapacityDecision).filter(
            CapacityDecision.id == decision_id
        ).first()

    def create(self, db: Session, **fields) -> CapacityDecision:
        obj = CapacityDecision(**fields)
        db.add(obj)
        db.flush()
        return obj

    def list_for(
        self, db: Session, *, reservation_id: Optional[int] = None,
        supplier_id: Optional[int] = None, limit: int = 100
    ) -> List[CapacityDecision]:
        q = db.query(CapacityDecision)
        if reservation_id is not None:
            q = q.filter(CapacityDecision.reservation_id == reservation_id)
        if supplier_id is not None:
            q = q.filter(CapacityDecision.supplier_id == supplier_id)
        return q.order_by(CapacityDecision.id.desc()).limit(limit).all()


crud_capacity_decision = CRUDCapacityDecision()
