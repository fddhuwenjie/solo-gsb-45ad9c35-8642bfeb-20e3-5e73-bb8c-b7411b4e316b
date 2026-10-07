"""误记收款的整笔冲正：按账户与原收款单号办理，冲正后该笔不再计入有效实收。

口径（完全基于 app/ledger.py 落盘的已记凭证、app/payments.py 落盘的收款
核销、app/payouts.py 落盘的退付与本模块落盘的冲正行；凭证金额一律取入账
时冻结的计费栏，不重新核算）：
1. 调用方按账户提交**原收款单号**（账户由路径给出，请求体无需金额）；
2. 原收款单**不存在** → 404 payment_not_found；只接受原收款**所属账户**，
   账户不符 → 422 payment_account_mismatch（附原收款所属账户）；
3. 同一原收款单的**重复冲正**原样返回**首次冲正回执**（result=deduped，
   冲正金额、冲正时间与冲正后头寸快照都不随后续收款/退付/入账漂移）；
4. 冲正**整笔**生效：保留原收款主行、逐凭证分配与首次收款回执作为证据，
   只新增一行冲正记录；头寸重算（app/payments.account_position）时
   该笔的逐凭证核销不再计入有效实收（sum_cash_by_voucher 排除已冲正收款），
   累计实收随之下降；
5. **不回填历史分配**：已发生的后续收款分配与已付退付保持原样。应退抵减
   按「当前全部已记应退 − 已付退付」对**重算后的逐凭证未收余额**重新按
   原入账顺序 FIFO 计算（同一套 account_position 口径），因此逐凭证已收、
   账户累计实收，以及台账、收款、退付查询的当前尚欠和信用都随冲正重算；
6. **后续新收款**按重算后的尚欠 FIFO 分配；对原单号**重试收款**仍返回
   原收款回执（冲正不改变收款单号的幂等语义，原单不复活、不二次核销）；
7. 冲正核对与落盘和原入账、收款、退付**共用同一把 SQLite 写事务锁**
   （RLock + BEGIN IMMEDIATE）：冲正行以 payment_no 为主键，并发冲正同一
   笔只有一笔落盘，其余返回首次回执；任一核对失败事务回滚，**不留冲正
   记录**；进程重启后冲正仍可查询，重复冲正仍返回首次回执。

冲正后头寸（receipt 的 position）字段：total_received_yuan 有效累计实收、
total_paid_out_yuan 累计已付退付、outstanding_yuan / credit_yuan 最新
尚欠与信用（两者不同时为正）。

HTTP 错误码：
- payment_not_found：原收款单号全平台不存在（404，附 payment_no）；
- payment_account_mismatch：原收款属于别的账户（422，附 payment_no 与
  原登记 account_id）；本次不写入。
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Optional

from app.payments import _round6, account_position
from app.storage import SessionStore, get_store


class ReversalError(Exception):
    """冲正操作失败：code 决定 HTTP 错误体 error 字段，status_code 决定状态码。"""

    def __init__(
        self,
        code: str,
        reason: str,
        status_code: int = 422,
        extra: Optional[dict] = None,
    ):
        super().__init__(reason)
        self.code = code
        self.status_code = status_code
        # 附加到错误响应体顶层的结构化字段（如原收款所属账户）
        self.extra = extra or {}


def _utc_now_iso() -> str:
    # 与 sessions/ledger/payments/payouts._utc_now_iso 同口径：UTC、秒精度、带 Z。
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _reversal_status_fields(row) -> dict:
    """把冲正行组装成收款查询中展示的冲正状态字段。"""
    return {
        "reversed": True,
        "reversed_at": row["reversed_at"],
        "reversal_amount_yuan": _round6(float(row["amount_yuan"])),
    }


def reversal_map_for_account(conn, store: SessionStore, account_id: str) -> dict:
    """写事务内取账户内收款单号 → 冲正状态字段的映射（收款列表展示冲正状态）。"""
    return {
        row["payment_no"]: _reversal_status_fields(row)
        for row in store.list_reversal_rows_for_account(conn, account_id)
    }


def reverse_payment(
    account_id: str,
    payment_no: str,
    store: Optional[SessionStore] = None,
) -> dict:
    """按账户与原收款单号整笔冲正一笔误记收款，原子落盘，返回冲正回执。

    原单不存在 → 404；账户不符 → 422；重复冲正 → 首次冲正回执（deduped）。
    """
    store = store or get_store()

    with store.write_transaction() as conn:
        # 1) 原收款单必须存在，且只接受其所属账户办理。
        #    账户核对先于重复冲正判定：跨账户的重复冲正仍按账户不符拒绝，
        #    只有原收款所属账户的重复冲正才原样返回首次冲正回执。
        payment_row = store.get_payment(conn, payment_no)
        if payment_row is None:
            raise ReversalError(
                "payment_not_found",
                f"收款单号 {payment_no} 不存在，无法冲正；请核对原收款单号",
                404,
                extra={"payment_no": payment_no},
            )
        original_account = payment_row["account_id"]
        if original_account != account_id:
            raise ReversalError(
                "payment_account_mismatch",
                f"收款单号 {payment_no} 属于账户 {original_account}，冲正只接受"
                f"原收款所属账户办理，账户 {account_id} 不能冲正该笔；本次不写入",
                422,
                extra={
                    "payment_no": payment_no,
                    "account_id": original_account,
                    "requested_account_id": account_id,
                },
            )

        # 2) 同账户重复冲正：冲正行已在 → 原样取回首次冲正回执（不漂移）。
        existing_reversal = store.get_reversal(conn, payment_no)
        if existing_reversal is not None:
            receipt = json.loads(existing_reversal["receipt_json"])
            receipt["result"] = "deduped"
            return receipt

        # 3) 落冲正行（原收款主行与逐凭证核销保持原样）。冲正后头寸由
        #    account_position 重算：该笔核销因冲正行存在而被排除出有效实收，
        #    应退池对重算后的未收余额重新 FIFO 抵减；已付退付照常先扣。
        reversed_at = _utc_now_iso()
        amount = _round6(float(payment_row["amount_yuan"]))
        receipt_id = str(uuid.uuid4())
        # 先插入冲正行，使同一事务内随后的头寸重算能经 NOT EXISTS 排除本笔。
        # 回执中的头寸快照在插入后重算，再回填 receipt_json。
        store.insert_reversal(
            conn,
            reversal_id=receipt_id,
            payment_no=payment_no,
            account_id=account_id,
            amount_yuan=amount,
            receipt_json="{}",
            reversed_at=reversed_at,
        )

        position = account_position(conn, store, account_id)
        totals = position["totals"]
        receipt = {
            "account_id": account_id,
            "payment_no": payment_no,
            "result": "reversed",
            "reversal_id": receipt_id,
            "amount_yuan": amount,
            "reversed_at": reversed_at,
            "total_received_yuan": totals["total_received_yuan"],
            "total_paid_out_yuan": totals["total_paid_out_yuan"],
            "outstanding_yuan": totals["outstanding_yuan"],
            "credit_yuan": totals["credit_yuan"],
        }
        # 回填冻结回执 JSON（冲正重试时原样取回）。
        conn.execute(
            "UPDATE account_payment_reversals SET receipt_json = ? WHERE payment_no = ?",
            (json.dumps(receipt, ensure_ascii=False), payment_no),
        )
        return receipt
