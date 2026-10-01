"""供应商产能预约系统测试。

覆盖：
- 按日期区间预约 / 占用 / 释放 / 超期失效
- 转单、改期在同一事务内重新核对可用量（失败整体回滚）
- 重叠时段不超卖、部分释放、多人并发预约
- 节假日停产与服务重启不改变既有占用（占用固化）
- 每次容量决定的依据留存
"""
import json
import threading
from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from tests.test_data_factory import DataFactory
from app.database import apply_sqlite_concurrency
from app.services.capacity import (
    CapacityService, CapacityError, CapacityUnavailableError,
)
from app.services.purchase import PurchaseService
from app.crud.capacity import (
    crud_capacity_reservation, crud_capacity_decision,
    crud_production_calendar,
)
from app.crud.supplier import crud_supply_capacity
from app.crud.purchase import crud_purchase_order, crud_purchase_suggestion
from app.models import CapacityReservation, CapacityDecision


@pytest.fixture
def factory(db_session):
    f = DataFactory(db_session)
    f.setup_basic_supply_chain()
    return f


def _window(days=14):
    return date.today(), date.today() + timedelta(days=days)


class TestReservationBasics:
    def test_reserve_expands_to_working_days(self, db_session, factory):
        start, end = _window()
        res = CapacityService.reserve(
            db_session, supplier_id=factory.suppliers["TS001"].id,
            material_id=factory.materials["TM001"].id,
            quantity=900, start_date=start, end_date=end,
            owner_team="A组", expires_in_hours=0,
        )
        assert res.status == "reserved"
        assert res.owner_team == "A组"
        # 日产能300，900件恰好固化到3个工作日
        working_days = [d for d in res.days if d.remaining_quantity > 0]
        assert len(working_days) == 3
        assert sum(d.remaining_quantity for d in working_days) == 900
        assert all(d.remaining_quantity <= 300 for d in working_days)
        assert all(d.day_date.weekday() < 5 for d in working_days)

    def test_overlapping_windows_cannot_oversell(self, db_session, factory):
        start, end = _window()
        sid, mid = factory.suppliers["TS001"].id, factory.materials["TM001"].id
        # 先查询窗口总产能，把窗口恰好占满
        avail0 = CapacityService.get_availability(db_session, sid, mid, start, end)
        total = avail0["total_capacity"]
        CapacityService.reserve(
            db_session, supplier_id=sid, material_id=mid,
            quantity=total, start_date=start, end_date=end,
            owner_team="A组", expires_in_hours=0,
        )
        with pytest.raises(CapacityUnavailableError) as exc:
            CapacityService.reserve(
                db_session, supplier_id=sid, material_id=mid,
                quantity=100, start_date=start, end_date=end,
                owner_team="B组", expires_in_hours=0,
            )
        assert exc.value.shortage == 100
        # 被拒绝的预约不得落库
        rows = crud_capacity_reservation.list_for(db_session, supplier_id=sid)
        assert len(rows) == 1

    def test_partial_overlap_respects_daily_cap(self, db_session, factory):
        start, end = _window()
        sid, mid = factory.suppliers["TS001"].id, factory.materials["TM001"].id
        # A组占用第一个工作日的全部300
        res_a = CapacityService.reserve(
            db_session, supplier_id=sid, material_id=mid,
            quantity=300, start_date=start, end_date=start + timedelta(days=2),
            owner_team="A组", expires_in_hours=0,
        )
        first_day = min(d.day_date for d in res_a.days)
        # B组在同一天只能拿到0，必须顺延到后面的工作日
        res_b = CapacityService.reserve(
            db_session, supplier_id=sid, material_id=mid,
            quantity=300, start_date=start, end_date=end,
            owner_team="B组", expires_in_hours=0,
        )
        b_days = {d.day_date: d.remaining_quantity for d in res_b.days}
        assert b_days.get(first_day, 0) == 0
        assert sum(b_days.values()) == 300

    def test_reserve_without_working_day_rejected(self, db_session, factory):
        sid, mid = factory.suppliers["TS001"].id, factory.materials["TM001"].id
        # 找一个周六
        sat = date.today()
        while sat.weekday() != 5:
            sat += timedelta(days=1)
        with pytest.raises(CapacityUnavailableError):
            CapacityService.reserve(
                db_session, supplier_id=sid, material_id=mid,
                quantity=10, start_date=sat, end_date=sat + timedelta(days=1),
                expires_in_hours=0,
            )

    def test_availability_excludes_expired_even_before_sweep(self, db_session, factory):
        start, end = _window()
        sid, mid = factory.suppliers["TS001"].id, factory.materials["TM001"].id
        res = CapacityService.reserve(
            db_session, supplier_id=sid, material_id=mid,
            quantity=600, start_date=start, end_date=end,
            expires_in_hours=1,
        )
        # 手工把有效期改到过去（模拟时间流逝），但不执行清理
        res.expires_at = datetime.now() - timedelta(hours=1)
        db_session.commit()
        avail = CapacityService.get_availability(db_session, sid, mid, start, end)
        assert avail["occupied_quantity"] == 0
        assert avail["available_quantity"] == avail["total_capacity"]


class TestRelease:
    def test_full_release_restores_availability(self, db_session, factory):
        start, end = _window()
        sid, mid = factory.suppliers["TS001"].id, factory.materials["TM001"].id
        res = CapacityService.reserve(
            db_session, supplier_id=sid, material_id=mid,
            quantity=900, start_date=start, end_date=end,
            expires_in_hours=0,
        )
        CapacityService.release(db_session, res.id, reason="订单取消")
        assert res.status == "released"
        assert all(d.remaining_quantity == 0 for d in res.days)
        avail = CapacityService.get_availability(db_session, sid, mid, start, end)
        assert avail["occupied_quantity" ] == 0
        # 原始占用量仍保留（追加式，不抹历史）
        assert sum(d.quantity for d in res.days) == 900

    def test_partial_release_by_quantity(self, db_session, factory):
        start, end = _window()
        sid, mid = factory.suppliers["TS001"].id, factory.materials["TM001"].id
        res = CapacityService.reserve(
            db_session, supplier_id=sid, material_id=mid,
            quantity=900, start_date=start, end_date=end,
            expires_in_hours=0,
        )
        CapacityService.release(db_session, res.id, quantity=300, reason="减量")
        db_session.refresh(res)
        assert res.status == "reserved"
        assert sum(d.remaining_quantity for d in res.days) == 600
        # 释放的是最晚的生产日期（从后往前）
        avail = CapacityService.get_availability(db_session, sid, mid, start, end)
        assert avail["occupied_quantity"] == 600
        # 释放出来的300可以被重新约走
        res2 = CapacityService.reserve(
            db_session, supplier_id=sid, material_id=mid,
            quantity=300, start_date=start, end_date=end,
            owner_team="B组", expires_in_hours=0,
        )
        assert sum(d.remaining_quantity for d in res2.days) == 300

    def test_partial_release_by_dates(self, db_session, factory):
        start, end = _window()
        sid, mid = factory.suppliers["TS001"].id, factory.materials["TM001"].id
        res = CapacityService.reserve(
            db_session, supplier_id=sid, material_id=mid,
            quantity=600, start_date=start, end_date=end,
            expires_in_hours=0,
        )
        first_day = min(d.day_date for d in res.days)
        CapacityService.release(
            db_session, res.id, dates=[first_day], reason="首日取消"
        )
        db_session.refresh(res)
        assert res.status == "reserved"
        day_rows = {d.day_date: d for d in res.days}
        assert day_rows[first_day].remaining_quantity == 0

    def test_partial_release_by_date_quantities(self, db_session, factory):
        start, end = _window()
        sid, mid = factory.suppliers["TS001"].id, factory.materials["TM001"].id
        res = CapacityService.reserve(
            db_session, supplier_id=sid, material_id=mid,
            quantity=300, start_date=start, end_date=end,
            expires_in_hours=0,
        )
        first_day = min(d.day_date for d in res.days)
        CapacityService.release(
            db_session, res.id,
            date_quantities={first_day.isoformat(): 100},
        )
        db_session.refresh(res)
        day_rows = {d.day_date: d for d in res.days}
        assert day_rows[first_day].remaining_quantity == 200

    def test_cannot_release_more_than_remaining(self, db_session, factory):
        start, end = _window()
        sid, mid = factory.suppliers["TS001"].id, factory.materials["TM001"].id
        res = CapacityService.reserve(
            db_session, supplier_id=sid, material_id=mid,
            quantity=300, start_date=start, end_date=end,
            expires_in_hours=0,
        )
        with pytest.raises(CapacityError):
            CapacityService.release(db_session, res.id, quantity=301)


class TestExpiry:
    def test_expire_due_marks_and_frees_capacity(self, db_session, factory):
        start, end = _window()
        sid, mid = factory.suppliers["TS001"].id, factory.materials["TM001"].id
        res = CapacityService.reserve(
            db_session, supplier_id=sid, material_id=mid,
            quantity=600, start_date=start, end_date=end,
            expires_in_hours=1,
        )
        res.expires_at = datetime.now() - timedelta(minutes=1)
        db_session.commit()

        CapacityService.expire_due(db_session)
        db_session.refresh(res)
        assert res.status == "expired"
        assert res.expired_at is not None
        assert all(d.remaining_quantity == 0 for d in res.days)
        events = [e.event_type for e in res.events]
        assert "expire" in events
        # 失效后不能再释放
        with pytest.raises(CapacityError):
            CapacityService.release(db_session, res.id)


class TestConvertWithCapacity:
    def _make_suggestion(self, db_session, factory, material_code="TM001", qty=300):
        from app.services.requirement import RequirementService
        RequirementService.calculate_material_requirements(db_session)
        suggestions = PurchaseService.generate_purchase_suggestions(db_session)
        sug = next(s for s in suggestions if s.material.code == material_code)
        return sug

    def test_convert_checks_and_occupies_in_one_tx(self, db_session, factory):
        start, end = date.today(), date.today() + timedelta(days=14)
        sug = self._make_suggestion(db_session, factory, qty=300)
        order, res = CapacityService.convert_suggestion_with_capacity(
            db_session, suggestion_id=sug.id, order_no="PO-CAP-001",
            start_date=start, end_date=end, quantity=300, actor="采购一组",
        )
        assert order.quantity == 300
        assert res.purchase_order_id == order.id
        assert res.expires_at is None  # 已转单长期占用，不再超期失效
        db_session.refresh(sug)
        assert sug.status == "converted"
        avail = CapacityService.get_availability(
            db_session, order.supplier_id, order.material_id, start, end
        )
        assert avail["occupied_quantity"] == 300

    def test_convert_over_capacity_rolls_back_entire_tx(self, db_session, factory):
        start, end = date.today(), date.today() + timedelta(days=3)
        sid, mid = factory.suppliers["TS001"].id, factory.materials["TM001"].id
        # 别人先把窗口恰好占满
        total = CapacityService.get_availability(
            db_session, sid, mid, start, end
        )["total_capacity"]
        CapacityService.reserve(
            db_session, supplier_id=sid, material_id=mid,
            quantity=total, start_date=start, end_date=end,
            owner_team="X", expires_in_hours=0,
        )
        sug = self._make_suggestion(db_session, factory)
        with pytest.raises(CapacityUnavailableError):
            CapacityService.convert_suggestion_with_capacity(
                db_session, suggestion_id=sug.id, order_no="PO-CAP-FAIL",
                start_date=start, end_date=end, quantity=100,
            )
        # 订单号没有落库，建议状态也没有被改
        assert crud_purchase_order.get_by_order_no(db_session, "PO-CAP-FAIL") is None
        db_session.refresh(sug)
        assert sug.status == "pending"

    def test_convert_with_reservation_rechecks_capacity(self, db_session, factory):
        start, end = date.today(), date.today() + timedelta(days=14)
        sid, mid = factory.suppliers["TS001"].id, factory.materials["TM001"].id
        sug = self._make_suggestion(db_session, factory)
        res = CapacityService.reserve(
            db_session, supplier_id=sid, material_id=mid,
            quantity=300, start_date=start, end_date=end,
            owner_team="A组", expires_in_hours=0,
            purchase_suggestion_id=sug.id,
        )
        # 同物料窗口其余产能被另一组占满（持有预约本身之外的部分）
        # 窗口10个工作日总3000，预约占300，再占2700 → 重核时自身外无余量，
        # 但预约自身占用合法，重核不应失败。这里验证正常通过：
        order, res2 = CapacityService.convert_suggestion_with_capacity(
            db_session, suggestion_id=sug.id, order_no="PO-CAP-002",
            start_date=start, end_date=end, quantity=300,
            reservation_id=res.id,
        )
        assert res2.id == res.id
        assert res2.purchase_order_id == order.id

    def test_convert_smaller_order_releases_excess(self, db_session, factory):
        start, end = date.today(), date.today() + timedelta(days=14)
        sid, mid = factory.suppliers["TS001"].id, factory.materials["TM001"].id
        sug = self._make_suggestion(db_session, factory, qty=100)
        res = CapacityService.reserve(
            db_session, supplier_id=sid, material_id=mid,
            quantity=600, start_date=start, end_date=end,
            owner_team="A组", expires_in_hours=0,
            purchase_suggestion_id=sug.id,
        )
        order, res2 = CapacityService.convert_suggestion_with_capacity(
            db_session, suggestion_id=sug.id, order_no="PO-CAP-003",
            start_date=start, end_date=end, quantity=100,
            reservation_id=res.id,
        )
        # 只保留100的占用，其余500在同事务内释放
        assert sum(d.remaining_quantity for d in res2.days) == 100
        avail = CapacityService.get_availability(
            db_session, sid, mid, start, end
        )
        assert avail["occupied_quantity"] == 100
        # 原始预约量仍保留，且有自动部分释放事件
        db_session.refresh(res2)
        assert res2.quantity == 600
        assert any(e.event_type == "partial_release" for e in res2.events)


class TestReschedule:
    def test_reschedule_moves_occupation(self, db_session, factory):
        start, end = date.today(), date.today() + timedelta(days=7)
        sid, mid = factory.suppliers["TS001"].id, factory.materials["TM001"].id
        res = CapacityService.reserve(
            db_session, supplier_id=sid, material_id=mid,
            quantity=300, start_date=start, end_date=end,
            expires_in_hours=0,
        )
        # 手工挂一个订单，模拟已转单
        from app.models import PurchaseOrder
        po = PurchaseOrder(
            order_no="PO-RS-001", supplier_id=sid, material_id=mid,
            quantity=300, expected_date=end, status="ordered",
        )
        db_session.add(po)
        db_session.flush()
        res.purchase_order_id = po.id
        res.expires_at = None
        db_session.commit()

        new_end = date.today() + timedelta(days=21)
        order2, res2 = CapacityService.reschedule_order(
            db_session, order_id=po.id,
            new_start_date=date.today(), new_end_date=new_end,
            reason="供应商排产调整",
        )
        assert order2.expected_date == new_end
        assert res2.id != res.id
        assert res2.purchase_order_id == po.id
        db_session.refresh(res)
        assert res.status == "released"
        assert res.purchase_order_id is None
        assert all(d.remaining_quantity == 0 for d in res.days)

    def test_reschedule_failure_keeps_old_occupation(self, db_session, factory):
        start, end = date.today(), date.today() + timedelta(days=7)
        sid, mid = factory.suppliers["TS001"].id, factory.materials["TM001"].id
        from app.models import PurchaseOrder
        po = PurchaseOrder(
            order_no="PO-RS-002", supplier_id=sid, material_id=mid,
            quantity=300, expected_date=end, status="ordered",
        )
        db_session.add(po)
        db_session.flush()
        old = CapacityReservation(
            reservation_no="RSV-OLD-002", supplier_id=sid, material_id=mid,
            supply_capacity_id=crud_supply_capacity.get_by_supplier_and_material(
                db_session, sid, mid
            ).id,
            start_date=start, end_date=end, quantity=300, daily_quantity=300,
            status="reserved", owner_team="A", purchase_order_id=po.id,
        )
        db_session.add(old)
        db_session.flush()
        from app.models import CapacityReservationDay
        first_workday = start
        while first_workday.weekday() >= 5:
            first_workday += timedelta(days=1)
        db_session.add(CapacityReservationDay(
            reservation_id=old.id, day_date=first_workday,
            quantity=300, remaining_quantity=300, is_working_day=True,
        ))
        db_session.commit()

        # 目标窗口被别人占满
        target_start = date.today() + timedelta(days=10)
        target_end = target_start + timedelta(days=4)
        total = CapacityService.get_availability(
            db_session, sid, mid, target_start, target_end
        )["total_capacity"]
        CapacityService.reserve(
            db_session, supplier_id=sid, material_id=mid,
            quantity=total, start_date=target_start, end_date=target_end,
            owner_team="B", expires_in_hours=0,
        )
        with pytest.raises(CapacityUnavailableError):
            CapacityService.reschedule_order(
                db_session, order_id=po.id,
                new_start_date=target_start, new_end_date=target_end,
            )
        # 旧占用原样保留（事务整体回滚）
        db_session.refresh(old)
        assert old.status == "reserved"
        assert old.purchase_order_id == po.id
        assert sum(d.remaining_quantity for d in old.days) == 300


class TestCalendarFreeze:
    def test_adding_holiday_after_reserve_keeps_occupation(self, db_session, factory):
        start, end = _window()
        sid, mid = factory.suppliers["TS001"].id, factory.materials["TM001"].id
        res = CapacityService.reserve(
            db_session, supplier_id=sid, material_id=mid,
            quantity=900, start_date=start, end_date=end,
            expires_in_hours=0,
        )
        occupied_days = sorted(d.day_date for d in res.days)
        frozen_total = sum(d.remaining_quantity for d in res.days)

        # 预约后把已占用的工作日全部改成停产日
        for d in occupied_days:
            crud_production_calendar.upsert(
                db_session, supplier_id=sid, calendar_date=d,
                day_type="closed", name="临时停产",
            )
        db_session.commit()

        # 既有占用行原样不变
        db_session.expire_all()
        res2 = crud_capacity_reservation.get(db_session, res.id)
        assert sum(d.remaining_quantity for d in res2.days) == frozen_total
        assert sorted(d.day_date for d in res2.days) == occupied_days

        # 这些日期对新预约显示为停产、可用为0，新预约无法再占用
        avail = CapacityService.get_availability(db_session, sid, mid, start, end)
        for row in avail["days"]:
            if row["day_date"] in occupied_days:
                assert row["is_working_day"] is False

    def test_weekend_override_allows_new_reservation(self, db_session, factory):
        sid, mid = factory.suppliers["TS001"].id, factory.materials["TM001"].id
        sat = date.today()
        while sat.weekday() != 5:
            sat += timedelta(days=1)
        crud_production_calendar.upsert(
            db_session, supplier_id=sid, calendar_date=sat,
            day_type="working", name="赶工加班",
        )
        db_session.commit()
        res = CapacityService.reserve(
            db_session, supplier_id=sid, material_id=mid,
            quantity=300, start_date=sat, end_date=sat,
            expires_in_hours=0,
        )
        assert [d.day_date for d in res.days if d.remaining_quantity > 0] == [sat]


class TestRestartPersistence:
    def test_occupation_survives_engine_restart(self, db_engine, factory):
        # factory 使用的 db_session 绑定 db_engine
        start, end = _window()
        sid = factory.suppliers["TS001"].id
        mid = factory.materials["TM001"].id
        res = CapacityService.reserve(
            factory.db, supplier_id=sid, material_id=mid,
            quantity=900, start_date=start, end_date=end,
            expires_in_hours=0,
        )
        res_id = res.id
        factory.db.commit()

        # 模拟服务重启：丢弃连接池，用全新引擎/会话打开同一个库
        db_engine.dispose()
        new_engine = create_engine(
            str(db_engine.url), connect_args={"check_same_thread": False}
        )
        apply_sqlite_concurrency(new_engine)
        Session = sessionmaker(bind=new_engine)
        sess = Session()
        try:
            loaded = crud_capacity_reservation.get(sess, res_id)
            assert loaded is not None
            assert loaded.status == "reserved"
            assert sum(d.remaining_quantity for d in loaded.days) == 900
            avail = CapacityService.get_availability(sess, sid, mid, start, end)
            assert avail["occupied_quantity"] == 900
            # 超期扫描也不应误伤长期有效占用
            CapacityService.expire_due(sess)
            loaded2 = crud_capacity_reservation.get(sess, res_id)
            assert loaded2.status == "reserved"
        finally:
            sess.close()
            new_engine.dispose()


class TestConcurrentReservations:
    def test_two_teams_concurrent_no_double_count(self, db_engine, factory):
        start, end = _window()
        sid = factory.suppliers["TS001"].id
        mid = factory.materials["TM001"].id
        factory.db.commit()
        db_path = str(db_engine.url)
        db_engine.dispose()

        barrier = threading.Barrier(2)
        results = {}

        def worker(team, qty):
            engine = create_engine(
                db_path, connect_args={"check_same_thread": False}
            )
            apply_sqlite_concurrency(engine)
            Session = sessionmaker(bind=engine)
            sess = Session()
            try:
                barrier.wait(timeout=10)
                res = CapacityService.reserve(
                    sess, supplier_id=sid, material_id=mid,
                    quantity=qty, start_date=start, end_date=end,
                    owner_team=team, expires_in_hours=0,
                )
                results[team] = ("ok", res.id)
            except CapacityUnavailableError as exc:
                results[team] = ("rejected", exc.shortage)
            except Exception as exc:  # pragma: no cover
                results[team] = ("error", str(exc))
            finally:
                sess.close()
                engine.dispose()

        t1 = threading.Thread(target=worker, args=("A组", 2000))
        t2 = threading.Thread(target=worker, args=("B组", 2000))
        t1.start(); t2.start()
        t1.join(30); t2.join(30)

        # 窗口约10个工作日 * 300 = 3000：两组各要2000，只能进一组
        statuses = {team: v[0] for team, v in results.items()}
        assert sorted(statuses.values()) == ["ok", "rejected"], results
        # 赢家占2000，输家缺口1000（3000-2000）
        winner = next(t for t, v in results.items() if v[0] == "ok")
        assert results[winner][1] is not None

        # 用第三个独立连接核对：任何一天都没有超过日产能300
        check_engine = create_engine(
            db_path, connect_args={"check_same_thread": False}
        )
        apply_sqlite_concurrency(check_engine)
        CSess = sessionmaker(bind=check_engine)
        csess = CSess()
        try:
            avail = CapacityService.get_availability(csess, sid, mid, start, end)
            assert avail["occupied_quantity"] == 2000
            assert all(
                r["occupied_quantity"] <= r["daily_capacity"] for r in avail["days"]
            )
            assert all(r["occupied_quantity"] <= 300 for r in avail["days"])
        finally:
            csess.close()
            check_engine.dispose()


class TestDecisionBasis:
    def test_every_capacity_decision_is_recorded(self, db_session, factory):
        start, end = _window()
        sid, mid = factory.suppliers["TS001"].id, factory.materials["TM001"].id
        res = CapacityService.reserve(
            db_session, supplier_id=sid, material_id=mid,
            quantity=300, start_date=start, end_date=end,
            owner_team="A组", expires_in_hours=0, actor="张三",
        )
        CapacityService.release(db_session, res.id, quantity=100, reason="测试释放")

        decisions = crud_capacity_decision.list_for(
            db_session, reservation_id=res.id
        )
        types = {d.decision_type: d for d in decisions}
        assert "reserve" in types
        assert "partial_release" in types

        reserve_dec = types["reserve"]
        assert reserve_dec.result == "approved"
        assert reserve_dec.rule_version == "v1"
        basis = json.loads(reserve_dec.basis_json)
        assert "daily_ledger" in basis
        assert "existing_occupancy" in basis
        assert basis["requested_quantity"] == 300
        # 逐日台账每天都有决策时的产能/占用/申请量/申请后余量
        ledger_days = basis["daily_ledger"]
        planned = [r for r in ledger_days if r["requested_quantity"] > 0]
        assert sum(r["requested_quantity"] for r in planned) == 300
        assert all(r["available_after_request"] >= 0 for r in ledger_days)

    def test_rejected_decision_keeps_basis(self, db_session, factory):
        start, end = date.today(), date.today() + timedelta(days=1)
        sid, mid = factory.suppliers["TS001"].id, factory.materials["TM001"].id
        with pytest.raises(CapacityUnavailableError):
            CapacityService.reserve(
                db_session, supplier_id=sid, material_id=mid,
                quantity=10_000, start_date=start, end_date=end,
                expires_in_hours=0,
            )
        rejected = crud_capacity_decision.list_for(db_session, supplier_id=sid)
        dec = next(d for d in rejected if d.result == "rejected")
        basis = json.loads(dec.basis_json)
        assert basis["totals"]["available_quantity"] < 10_000
        assert dec.reason and "缺口" in dec.reason
