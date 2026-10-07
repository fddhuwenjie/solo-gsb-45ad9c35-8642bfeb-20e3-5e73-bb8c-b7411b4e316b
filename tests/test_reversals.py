"""误记收款整笔冲正行为测试（按账户与原收款单号办理）。

覆盖：
- 整笔冲正：保留原收款、逐凭证分配与首次收款回执，冲正后该笔不再计入
  有效实收；冲正返回金额、时间与冲正后头寸（有效累计实收、尚欠/信用、
  累计退付）；
- 原单不存在 → 404 payment_not_found；账户不符 → 422
  payment_account_mismatch（附原登记账户）；重复冲正 → 首次冲正回执
  （result=deduped，金额/时间/头寸快照不漂移）；跨账户的重复冲正仍 422；
- 台账明细逐凭证已收、账户累计实收以及台账、收款、退付查询的当前尚欠与
  信用按有效收款、已记应退及已付退付重算；
- 不回填历史分配：冲正前已发生的后续收款分配与退付保持原样；冲正后应退
  抵减按重算后未收余额重新 FIFO；后续新收款按重算后尚欠分配；
- 原单号重试收款仍返回原收款回执（原单不复活、不二次核销）；
- 冲正与退付并存：已付退付保持原样、信用/尚欠重算，后续应补/应退按原规则；
- 收款查询展示冲正状态（reversed/reversed_at/reversal_amount_yuan）；
- 并发冲正同一笔只有一笔落盘（其余 deduped）；按同一路径重建存储模拟
  进程重启后冲正仍在、重复冲正仍返回首次回执、可继续登记新收款；
- 旧库（无冲正表）打开自动补建；既有入账、收款、退付、出单接口行为不变。
"""

import threading

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.reversals import reverse_payment
from app.storage import configure_store

client = TestClient(app)


DISPATCH = dict(
    prices=[0.0, 1.0, 1.0, 0.0],
    base_load_kw=[0.2, 0.2, 0.2, 0.2],
    pv_kw=[0.0, 0.0, 0.0, 0.0],
    capacity_kwh=0.4,
    max_charge_kw=1.0,
    max_discharge_kw=1.0,
    charge_efficiency=0.9,
    discharge_efficiency=0.9,
    initial_soc_kwh=0.0,
    min_terminal_soc_kwh=0.0,
    degradation_cost_per_kwh=0.2,
)

RECORDS = [
    dict(
        base_load_kw=0.2, pv_kw=0.0,
        charge_ac_energy_kwh=round(0.2 / 0.9, 6), discharge_ac_energy_kwh=0.0,
        grid_purchase_energy_kwh=round(0.1 + 0.2 / 0.9, 6),
        soc_start_kwh=0.0, soc_end_kwh=0.2,
    ),
    dict(
        base_load_kw=0.2, pv_kw=0.0,
        charge_ac_energy_kwh=0.0, discharge_ac_energy_kwh=0.09,
        grid_purchase_energy_kwh=0.01,
        soc_start_kwh=0.2, soc_end_kwh=0.1,
    ),
    dict(
        base_load_kw=0.2, pv_kw=0.0,
        charge_ac_energy_kwh=0.0, discharge_ac_energy_kwh=0.09,
        grid_purchase_energy_kwh=0.01,
        soc_start_kwh=0.1, soc_end_kwh=0.0,
    ),
    dict(
        base_load_kw=0.2, pv_kw=0.0,
        charge_ac_energy_kwh=0.0, discharge_ac_energy_kwh=0.0,
        grid_purchase_energy_kwh=0.1,
        soc_start_kwh=0.0, soc_end_kwh=0.0,
    ),
]

TOL = 0.01


@pytest.fixture(autouse=True)
def isolated_db(tmp_path):
    configure_store(str(tmp_path / "reversals.db"))
    yield
    configure_store(None)


def _plan():
    return client.post("/dispatch", json=DISPATCH).json()


def _create_complete():
    sid = client.post(
        "/dispatch/sessions",
        json={"dispatch": DISPATCH, "plan_snapshot": _plan(), "audit_tolerance_kwh": TOL},
    ).json()["session_id"]
    assert client.post(
        f"/dispatch/sessions/{sid}/batches",
        json={"start_index": 0, "records": RECORDS[:2]},
    ).status_code == 200
    assert client.post(
        f"/dispatch/sessions/{sid}/batches",
        json={"start_index": 2, "records": RECORDS[2:]},
    ).status_code == 200
    return sid


def _rev(sid, expected, changes):
    return client.post(
        f"/dispatch/sessions/{sid}/revisions",
        json={"expected_revision": expected, "changes": changes},
    )


def _issue(sid, revision):
    return client.post(
        f"/dispatch/sessions/{sid}/vouchers",
        json={"expected_revision": revision},
    )


def _ch(index, record):
    return {"index": index, "record": record}


def _post_vouchers(account, items):
    return client.post(
        f"/accounts/{account}/ledger",
        json={"items": [{"session_id": s, "voucher_no": n} for s, n in items]},
    )


def _pay(account, payment_no, amount):
    return client.post(
        f"/accounts/{account}/payments",
        json={"payment_no": payment_no, "amount_yuan": amount},
    )


def _reverse(account, payment_no):
    return client.post(f"/accounts/{account}/payments/{payment_no}/reversal")


def _payout(account, payout_no, amount):
    return client.post(
        f"/accounts/{account}/payouts",
        json={"payout_no": payout_no, "amount_yuan": amount},
    )


def _ledger(account):
    return client.get(f"/accounts/{account}/ledger").json()


def _payments(account):
    return client.get(f"/accounts/{account}/payments").json()


def _payouts(account):
    return client.get(f"/accounts/{account}/payouts").json()


def _voucher_chain_full_supp_refund(sid):
    """凭证 1=full 0.10、2=supplement 0.01、3=refund 0.02（跳过 rev2 对 rev3 出单）。"""
    _issue(sid, 0)
    up1 = dict(RECORDS[1]); up1.update(base_load_kw=0.22, grid_purchase_energy_kwh=0.02)
    _rev(sid, 0, [_ch(1, up1)])
    _issue(sid, 1)
    up2 = dict(RECORDS[1]); up2.update(base_load_kw=0.24, grid_purchase_energy_kwh=0.03)
    _rev(sid, 1, [_ch(1, up2)])
    down = dict(RECORDS[1]); down.update(base_load_kw=0.18, grid_purchase_energy_kwh=0.0)
    _rev(sid, 2, [_ch(1, down)])
    v3 = _issue(sid, 3).json()
    assert v3["billing"]["type"] == "refund"
    assert v3["billing"]["amount_yuan"] == pytest.approx(0.02, abs=1e-9)


# ---------------------------------------------------------------------------
# 基本冲正：金额/时间/冲正后头寸，原证据保留，有效实收与尚欠重算
# ---------------------------------------------------------------------------


def test_reversal_whole_amount_recomputes_position_but_keeps_evidence():
    sid = _create_complete()
    _issue(sid, 0)  # full 0.10
    _post_vouchers("fam", [(sid, 1)])
    pay = _pay("fam", "P1", 0.05)
    assert pay.status_code == 200
    paid_at = pay.json()["received_at"]

    r = _reverse("fam", "P1")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["result"] == "reversed"
    assert body["payment_no"] == "P1"
    assert body["amount_yuan"] == pytest.approx(0.05, abs=1e-9)
    assert body["reversed_at"].endswith("Z")
    assert body["reversal_id"]
    # 冲正后头寸：有效实收归零，尚欠恢复为全额 0.10，无信用/退付
    assert body["total_received_yuan"] == 0.0
    assert body["total_paid_out_yuan"] == 0.0
    assert body["outstanding_yuan"] == pytest.approx(0.10, abs=1e-9)
    assert body["credit_yuan"] == 0.0

    # 台账：逐凭证已收（paid）与累计实收按有效收款重算为 0，尚欠恢复 0.10
    led = _ledger("fam")
    assert led["total_received_yuan"] == 0.0
    assert led["outstanding_yuan"] == pytest.approx(0.10, abs=1e-9)
    assert led["entries"][0]["paid_yuan"] == 0.0
    assert led["entries"][0]["outstanding_yuan"] == pytest.approx(0.10, abs=1e-9)

    # 原收款主行、逐凭证分配与首次收款回执保留可查（首收冻结值不变）
    lst = _payments("fam")
    assert lst["payment_count"] == 1
    p = lst["payments"][0]
    assert p["payment_no"] == "P1"
    assert p["amount_yuan"] == pytest.approx(0.05, abs=1e-9)
    assert p["received_at"] == paid_at
    assert p["total_received_yuan"] == pytest.approx(0.05, abs=1e-9)  # 当时快照
    assert p["allocations"][0]["allocated_yuan"] == pytest.approx(0.05, abs=1e-9)
    # 冲正状态展示
    assert p["reversed"] is True
    assert p["reversed_at"] == body["reversed_at"]
    assert p["reversal_amount_yuan"] == pytest.approx(0.05, abs=1e-9)
    # 账户级有效累计实收剔除被冲正笔
    assert lst["total_received_yuan"] == 0.0
    assert lst["outstanding_yuan"] == pytest.approx(0.10, abs=1e-9)


def test_reversal_not_found_and_account_mismatch():
    sid = _create_complete()
    _issue(sid, 0)
    _post_vouchers("fam", [(sid, 1)])
    _pay("fam", "P1", 0.05)

    # 原单不存在 → 404
    r = _reverse("fam", "NO-SUCH")
    assert r.status_code == 404
    assert r.json()["error"] == "payment_not_found"
    assert r.json()["payment_no"] == "NO-SUCH"

    # 账户不符 → 422（附原登记账户），不留冲正
    r = _reverse("other", "P1")
    assert r.status_code == 422
    body = r.json()
    assert body["error"] == "payment_account_mismatch"
    assert body["payment_no"] == "P1"
    assert body["account_id"] == "fam"
    assert body["requested_account_id"] == "other"
    # 原账户仍可正常冲正（跨账户拒绝没有抢先占用）
    assert _reverse("fam", "P1").status_code == 200
    # 冲正后跨账户的重复冲正仍按账户不符拒绝（不返回回执）
    r2 = _reverse("other", "P1")
    assert r2.status_code == 422
    assert r2.json()["error"] == "payment_account_mismatch"


def test_repeated_reversal_returns_first_receipt_even_when_position_moves():
    sid = _create_complete()
    _issue(sid, 0)
    _post_vouchers("fam", [(sid, 1)])
    first_pay = _pay("fam", "P1", 0.05).json()

    first = _reverse("fam", "P1").json()
    assert first["result"] == "reversed"
    assert first["outstanding_yuan"] == pytest.approx(0.10, abs=1e-9)

    # 冲正后又登记新收款、再入账凭证，头寸已变化
    _pay("fam", "P2", 0.03)
    up = dict(RECORDS[1]); up.update(base_load_kw=0.22, grid_purchase_energy_kwh=0.02)
    _rev(sid, 0, [_ch(1, up)])
    _issue(sid, 1)  # supplement 0.01
    _post_vouchers("fam", [(sid, 2)])

    retry = _reverse("fam", "P1")
    assert retry.status_code == 200
    rb = retry.json()
    assert rb["result"] == "deduped"
    for key in ("reversal_id", "amount_yuan", "reversed_at", "total_received_yuan",
                "outstanding_yuan", "credit_yuan", "total_paid_out_yuan"):
        assert rb[key] == first[key]
    # 冲正记录只有一条（冲正次数不随重试增长，用有效实收侧面校验）
    assert _payments("fam")["payment_count"] == 2
    # 首次收款回执仍在且冻结
    assert first_pay["payment_no"] == "P1"


def test_original_payment_no_retry_still_returns_first_payment_receipt():
    """冲正不改变收款单号幂等语义：原单号重试收款仍返回原收款回执，
    原单不复活、不二次核销。"""
    sid = _create_complete()
    _issue(sid, 0)
    _post_vouchers("fam", [(sid, 1)])
    first = _pay("fam", "P1", 0.05).json()
    assert _reverse("fam", "P1").status_code == 200

    retry = _pay("fam", "P1", 0.05)
    assert retry.status_code == 200
    rb = retry.json()
    assert rb["result"] == "deduped"
    assert rb["received_at"] == first["received_at"]
    assert rb["total_received_yuan"] == first["total_received_yuan"]
    assert rb["allocations"] == first["allocations"]
    # 有效实收仍为 0：重试没有让被冲正收款复活
    assert _ledger("fam")["total_received_yuan"] == 0.0
    assert _payments("fam")["payment_count"] == 1


# ---------------------------------------------------------------------------
# 不回填历史分配：后续收款 / 应退抵减 / 退付按重算口径处理
# ---------------------------------------------------------------------------


def test_reversal_keeps_later_allocations_but_refund_reapplied_to_recomputed_due():
    """full 0.10、supplement 0.01、refund 0.02 已入账（初始应付 0.09：
    应退先抵 full 0.02 后 full 欠 0.08、supplement 欠 0.01）。P1 收 0.08
    付清 full；P2 收 0.01 付清 supplement。冲正 P1 后：
    - P2 对 supplement 的 0.01 分配保持原样（不回填历史分配）；
    - 应退 0.02 改按重算后未收余额重新 FIFO：full 现金为 0 → 抵 full 0.02，
      supplement 现金 0.01 已付清受保护 → 不再抵；full 尚欠 0.08；
    - 有效累计实收只剩 P2 的 0.01。"""
    sid = _create_complete()
    _voucher_chain_full_supp_refund(sid)
    _post_vouchers("fam", [(sid, 1), (sid, 2), (sid, 3)])
    assert _pay("fam", "P1", 0.08).status_code == 200
    assert _pay("fam", "P2", 0.01).status_code == 200
    led0 = _ledger("fam")
    assert led0["outstanding_yuan"] == 0.0
    assert led0["total_received_yuan"] == pytest.approx(0.09, abs=1e-9)

    assert _reverse("fam", "P1").status_code == 200
    led = _ledger("fam")
    e1, e2, e3 = led["entries"]
    # P1 的现金核销不再有效：full 已收为 0；P2 对 supplement 的分配原样保留
    assert e1["paid_yuan"] == 0.0
    assert e2["paid_yuan"] == pytest.approx(0.01, abs=1e-9)
    # 应退重新 FIFO：0.02 全部抵到现金为 0 的 full（supplement 已被 P2 付清）
    assert e1["refund_applied_yuan"] == pytest.approx(0.02, abs=1e-9)
    assert e2["refund_applied_yuan"] == 0.0
    assert e1["outstanding_yuan"] == pytest.approx(0.08, abs=1e-9)
    assert e2["outstanding_yuan"] == 0.0
    assert led["outstanding_yuan"] == pytest.approx(0.08, abs=1e-9)
    assert led["credit_yuan"] == 0.0
    # 有效累计实收只剩 P2
    assert led["total_received_yuan"] == pytest.approx(0.01, abs=1e-9)

    # P2 的冻结回执不被改写（P2 分配发生时 full 尚欠 0、0.01 核到 supplement）
    p2 = [p for p in _payments("fam")["payments"] if p["payment_no"] == "P2"][0]
    assert p2["reversed"] is False
    assert p2["total_received_yuan"] == pytest.approx(0.09, abs=1e-9)
    assert p2["allocations"][0]["voucher_no"] == 2
    assert p2["allocations"][0]["allocated_yuan"] == pytest.approx(0.01, abs=1e-9)


def test_new_payment_after_reversal_allocated_by_recomputed_outstanding():
    sid = _create_complete()
    _issue(sid, 0)  # full 0.10
    _post_vouchers("fam", [(sid, 1)])
    _pay("fam", "P1", 0.05)
    assert _reverse("fam", "P1").status_code == 200

    # 冲正后尚欠恢复 0.10：新收款按重算后尚欠 FIFO，全部分给 full
    new = _pay("fam", "P2", 0.07)
    assert new.status_code == 200, new.text
    b = new.json()
    assert b["total_received_yuan"] == pytest.approx(0.07, abs=1e-9)
    assert b["outstanding_yuan"] == pytest.approx(0.03, abs=1e-9)
    assert len(b["allocations"]) == 1
    assert b["allocations"][0]["voucher_no"] == 1
    assert b["allocations"][0]["allocated_yuan"] == pytest.approx(0.07, abs=1e-9)
    assert b["allocations"][0]["outstanding_before_yuan"] == pytest.approx(0.10, abs=1e-9)
    assert b["allocations"][0]["outstanding_after_yuan"] == pytest.approx(0.03, abs=1e-9)

    # 超过重算后应付仍整笔拒绝
    over = _pay("fam", "P3", 0.05)
    assert over.status_code == 422
    assert over.json()["error"] == "payment_exceeds_outstanding"
    assert over.json()["outstanding_yuan"] == pytest.approx(0.03, abs=1e-9)


def test_reversal_with_existing_payout_keeps_payout_and_recomputes_credit():
    """付清后入账应补 0.01 与应退 0.02：应退先抵未付清的应补 0.01，剩 0.01
    形成信用 → 退付 0.01；再冲正全部收款：
    已付退付保持原样（不退回首寸），应退池先扣已退付 0.01 后剩 0.01，
    对重算后未收的 full/supplement 重新 FIFO 抵减（抵 full 0.01）；
    有效实收归零，尚欠 0.10、信用 0。"""
    sid = _create_complete()
    _issue(sid, 0)  # full 0.10
    _post_vouchers("fam", [(sid, 1)])
    _pay("fam", "P1", 0.10)  # 先付清
    # 之后入账应补 0.01、应退 0.02（凭证 2 supplement、凭证 3 refund）
    up = dict(RECORDS[1]); up.update(base_load_kw=0.22, grid_purchase_energy_kwh=0.02)
    _rev(sid, 0, [_ch(1, up)])
    _issue(sid, 1)
    up2 = dict(RECORDS[1]); up2.update(base_load_kw=0.24, grid_purchase_energy_kwh=0.03)
    _rev(sid, 1, [_ch(1, up2)])
    down = dict(RECORDS[1]); down.update(base_load_kw=0.18, grid_purchase_energy_kwh=0.0)
    _rev(sid, 2, [_ch(1, down)])
    _issue(sid, 3)  # refund 0.02
    _post_vouchers("fam", [(sid, 2), (sid, 3)])
    led = _ledger("fam")
    # full 已被现金付清受保护，应退 0.02 先抵应补 0.01，余 0.01 为信用
    assert led["credit_yuan"] == pytest.approx(0.01, abs=1e-9)
    po = _payout("fam", "PO1", 0.01)
    assert po.status_code == 200

    rv = _reverse("fam", "P1")
    assert rv.status_code == 200
    b = rv.json()
    # 已付退付 0.01 原样保留；应退池 0.02 先扣 0.01 退付，剩 0.01 抵重算后
    # 未收余额中最早的 full：full 尚欠 0.09、应补尚欠 0.01
    assert b["total_paid_out_yuan"] == pytest.approx(0.01, abs=1e-9)
    assert b["total_received_yuan"] == 0.0
    assert b["outstanding_yuan"] == pytest.approx(0.10, abs=1e-9)
    assert b["credit_yuan"] == 0.0

    # 台账、收款、退付三处查询的当前头寸一致
    led2 = _ledger("fam")
    assert led2["outstanding_yuan"] == pytest.approx(0.10, abs=1e-9)
    assert led2["entries"][0]["refund_applied_yuan"] == pytest.approx(0.01, abs=1e-9)
    assert _payments("fam")["outstanding_yuan"] == pytest.approx(0.10, abs=1e-9)
    pos_po = _payouts("fam")
    assert pos_po["total_paid_out_yuan"] == pytest.approx(0.01, abs=1e-9)
    assert pos_po["outstanding_yuan"] == pytest.approx(0.10, abs=1e-9)
    assert pos_po["payout_count"] == 1
    # 退付记录的冻结回执不被冲正改写（退付当时信用快照为 0）
    assert pos_po["payouts"][0]["credit_yuan"] == 0.0

    # 信用已为 0：再退付任何正金额都拒绝
    assert _payout("fam", "PO2", 0.001).status_code == 422


def test_reversal_then_later_refund_and_supplement_follow_original_rules():
    """冲正后后续入账的应补/应退仍按原规则改变最新尚欠/信用。"""
    sid = _create_complete()
    _issue(sid, 0)  # full 0.10
    _post_vouchers("fam", [(sid, 1)])
    _pay("fam", "P1", 0.10)
    assert _reverse("fam", "P1").status_code == 200
    assert _ledger("fam")["outstanding_yuan"] == pytest.approx(0.10, abs=1e-9)

    # 后续入账应退 0.02 → 抵 full 0.02，尚欠 0.08
    up = dict(RECORDS[1]); up.update(base_load_kw=0.22, grid_purchase_energy_kwh=0.02)
    _rev(sid, 0, [_ch(1, up)])
    _issue(sid, 1)  # supplement 0.01
    up2 = dict(RECORDS[1]); up2.update(base_load_kw=0.24, grid_purchase_energy_kwh=0.03)
    _rev(sid, 1, [_ch(1, up2)])
    down = dict(RECORDS[1]); down.update(base_load_kw=0.18, grid_purchase_energy_kwh=0.0)
    _rev(sid, 2, [_ch(1, down)])
    _issue(sid, 3)  # refund 0.02
    _post_vouchers("fam", [(sid, 2), (sid, 3)])
    led = _ledger("fam")
    assert led["outstanding_yuan"] == pytest.approx(0.09, abs=1e-9)
    assert led["entries"][0]["refund_applied_yuan"] == pytest.approx(0.02, abs=1e-9)
    # 新收款按重算后尚欠分配
    pay = _pay("fam", "P2", 0.09)
    assert pay.status_code == 200
    assert pay.json()["outstanding_yuan"] == 0.0


# ---------------------------------------------------------------------------
# 并发、重启、旧库
# ---------------------------------------------------------------------------


def test_concurrent_reversal_only_one_lands():
    sid = _create_complete()
    _issue(sid, 0)
    _post_vouchers("race", [(sid, 1)])
    _pay("race", "P1", 0.10)
    outcomes = []

    def worker():
        try:
            res = reverse_payment("race", "P1")
            outcomes.append(res["result"])
        except Exception as exc:  # noqa: BLE001
            outcomes.append(getattr(exc, "code", "error"))

    threads = [threading.Thread(target=worker) for _ in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert outcomes.count("reversed") == 1
    assert outcomes.count("deduped") == 11
    led = _ledger("race")
    assert led["total_received_yuan"] == 0.0
    assert led["outstanding_yuan"] == pytest.approx(0.10, abs=1e-9)


def test_reversal_persists_across_restart(tmp_path):
    db_path = str(tmp_path / "persist-rev.db")
    configure_store(db_path)
    sid = _create_complete()
    _issue(sid, 0)
    _post_vouchers("fam", [(sid, 1)])
    _pay("fam", "P1", 0.05)
    first = _reverse("fam", "P1").json()

    configure_store(db_path)  # 模拟进程重启
    led = _ledger("fam")
    assert led["total_received_yuan"] == 0.0
    assert led["outstanding_yuan"] == pytest.approx(0.10, abs=1e-9)
    p = _payments("fam")["payments"][0]
    assert p["reversed"] is True
    assert p["reversed_at"] == first["reversed_at"]

    # 重启后重复冲正仍返回首次回执
    retry = _reverse("fam", "P1")
    assert retry.status_code == 200
    rb = retry.json()
    assert rb["result"] == "deduped"
    assert rb["reversal_id"] == first["reversal_id"]
    assert rb["reversed_at"] == first["reversed_at"]
    assert rb["outstanding_yuan"] == first["outstanding_yuan"]

    # 原单号重试收款仍是原收款回执（重启后也不复活）
    pr = _pay("fam", "P1", 0.05).json()
    assert pr["result"] == "deduped"

    # 重启后可继续登记新收款，按重算后尚欠分配，序号接续
    cont = _pay("fam", "P2", 0.10)
    assert cont.status_code == 200
    assert cont.json()["payment_count"] == 2
    assert cont.json()["outstanding_yuan"] == 0.0


def test_legacy_db_reversal_table_auto_created(tmp_path):
    """旧库（无冲正表）打开即补建：冲正可落盘、状态可查。"""
    import json
    import sqlite3

    from app.storage import get_store

    db_path = str(tmp_path / "legacy-rev.db")
    plan = client.post("/dispatch", json=DISPATCH).json()
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE sessions (
            session_id TEXT PRIMARY KEY, dispatch_json TEXT NOT NULL,
            plan_snapshot_json TEXT NOT NULL, audit_tolerance_kwh REAL NOT NULL,
            num_periods INTEGER NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE session_records (
            session_id TEXT NOT NULL, idx INTEGER NOT NULL, record_json TEXT NOT NULL,
            stored_at TEXT NOT NULL, PRIMARY KEY (session_id, idx));
        """
    )
    sid = "legacy-rev-session"
    conn.execute(
        "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?)",
        (sid, json.dumps(DISPATCH), json.dumps(plan), TOL, 4, "2026-01-01T00:00:00Z"),
    )
    for idx, rec in enumerate(RECORDS):
        conn.execute(
            "INSERT INTO session_records VALUES (?, ?, ?, ?)",
            (sid, idx, json.dumps(rec), "2026-01-01T00:00:01Z"),
        )
    conn.commit()
    conn.close()

    configure_store(db_path)
    _issue(sid, 0)
    _post_vouchers("new-acct", [(sid, 1)])
    assert _pay("new-acct", "LEGACY-PAY", 0.10).status_code == 200
    r = _reverse("new-acct", "LEGACY-PAY")
    assert r.status_code == 200, r.text
    assert r.json()["outstanding_yuan"] == pytest.approx(0.10, abs=1e-9)
    store = get_store()
    with store._connect() as c:
        names = {
            row[0]
            for row in c.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    assert "account_payment_reversals" in names


def test_failed_reversal_leaves_no_record():
    """账户不符 / 原单不存在的冲正整次拒绝，不留下冲正记录（头寸不变）。"""
    sid = _create_complete()
    _issue(sid, 0)
    _post_vouchers("fam", [(sid, 1)])
    _pay("fam", "P1", 0.05)

    assert _reverse("other", "P1").status_code == 422
    assert _reverse("fam", "MISSING").status_code == 404
    # 原收款仍有效、未被冲正
    led = _ledger("fam")
    assert led["total_received_yuan"] == pytest.approx(0.05, abs=1e-9)
    assert led["outstanding_yuan"] == pytest.approx(0.05, abs=1e-9)
    p = _payments("fam")["payments"][0]
    assert p["reversed"] is False
    assert p["reversed_at"] is None
    assert p["reversal_amount_yuan"] is None

    # 仍可正常冲正一次
    assert _reverse("fam", "P1").status_code == 200


def test_existing_flows_unchanged_after_reversal_feature():
    """新增冲正不影响入账、出单、收款、退付的既有行为（端到端抽验）。"""
    s1 = _create_complete()
    _issue(s1, 0)
    assert _post_vouchers("fam", [(s1, 1)]).status_code == 200
    pay = _pay("fam", "Q1", 0.04)
    assert pay.status_code == 200
    assert pay.json()["outstanding_yuan"] == pytest.approx(0.06, abs=1e-9)
    # 未冲正收款查询字段齐全且为未冲正
    item = _payments("fam")["payments"][0]
    assert item["reversed"] is False
    assert item["reversed_at"] is None
    # 退付查询字段不受影响（无退付）
    po = _payouts("fam")
    assert po == {
        "account_id": "fam",
        "payout_count": 0,
        "total_paid_out_yuan": 0.0,
        "outstanding_yuan": pytest.approx(0.06, abs=1e-9),
        "credit_yuan": 0.0,
        "payouts": [],
    }
    # 凭证列表仍可查
    vouchers = client.get(f"/dispatch/sessions/{s1}/vouchers")
    assert [v["voucher_no"] for v in vouchers.json()["vouchers"]] == [1]
