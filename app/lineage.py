"""Order-entity lineage tracing (订单实体沿革).

Given the canonical initial state, the ordered batches and an initial-state
order id, :func:`trace_lineage` follows one *order entity* from the initial
state to the end of every batch and reports, for each point:

* the entity's order row (``None`` once the entity is deleted),
* the refunds attributed to the entity at that point,
* the net amount ``order.amount - sum(attributed refund amounts)``,
* the in-batch change sequence numbers (``seq``) that touched the entity's
  order row or one of its attributed refunds.

Entity-identity rules (mirroring the task statement):

* An ``update`` that renumbers the order keeps the entity: the lineage
  follows the new id, including renames chained inside one batch.
* A ``delete`` ends the entity.  Re-inserting the same id later in the same
  batch, or in a later batch, creates a **new** order that is never attached
  to the old lineage.  After deletion the record terminates: the deletion
  batch is the last snapshot (order row ``None``).
* Refund attribution is computed from the state at each snapshot point
  ("退款改绑按批末归属计算"): a refund belongs to the entity iff its
  ``order_id`` equals the entity's id at that batch end.

A change ``seq`` is reported for batch B iff it

* updates/deletes the entity's order row (identity-tracked through the
  batch), or
* touches a refund *entity* attributed to the order entity at the previous
  snapshot (batch start) **or** at this batch end.  Refund entities are
  followed through in-batch renames by identity token, so a rename+rebind in
  one ``update`` is still attributed correctly and an in-batch reuse of a
  refund id by a different refund is never mis-attributed.  A rebind into or
  out of the entity is therefore always visible, while an insert+delete of a
  refund inside one batch (attributed at neither boundary) is not reported.

If any snapshot's net amount falls outside the signed 64-bit range, the
whole query delivers **no** lineage (``net_amount_overflow``); likewise an
illegal batch makes the whole query fail (surfaced as 422 at the API edge).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .models import Batch, InitialData
from .validate import (
    INT64_MAX,
    INT64_MIN,
    CdcRecord,
    SourceState,
    apply_batch_recording,
    build_initial_state,
)

STATUS_OK = "ok"
STATUS_ORDER_NOT_FOUND = "order_not_found"
STATUS_NET_AMOUNT_OVERFLOW = "net_amount_overflow"

#: Stable identity of a refund entity inside one batch: ``("start", rid)``
#: for a refund that existed at batch start, ``("insert", seq)`` for one
#: created inside the batch.  Renames move the token; deletes drop it.
_RefundToken = tuple[str, int]


@dataclass
class Snapshot:
    """The entity's state at one point (initial state or a batch end)."""

    batch_id: int | None  # None = initial state
    order: dict[str, Any] | None  # None once the entity is deleted
    refunds: list[dict[str, Any]]
    net_amount: int | None
    change_seqs: list[int]


@dataclass
class LineageResult:
    status: str
    order_id: int
    snapshots: list[Snapshot] = field(default_factory=list)
    terminated: bool = False
    terminated_batch_id: int | None = None
    notes: list[str] = field(default_factory=list)


def _attributed_refunds(state: SourceState, order_id: int) -> list[dict[str, Any]]:
    rows = [
        dict(refund)
        for refund in state.refunds.values()
        if refund["order_id"] == order_id
    ]
    rows.sort(key=lambda row: row["refund_id"])
    return rows


def _net_amount(order: dict[str, Any], refunds: list[dict[str, Any]]) -> int | None:
    """Exact net amount, or ``None`` when it leaves the int64 range."""
    net = order["amount"] - sum(refund["amount"] for refund in refunds)
    if net < INT64_MIN or net > INT64_MAX:
        return None
    return net


def _snapshot(
    state: SourceState, entity_id: int, batch_id: int | None, change_seqs: list[int]
) -> Snapshot | None:
    """Build one snapshot; ``None`` signals a net-amount overflow."""
    order = dict(state.orders[entity_id])
    refunds = _attributed_refunds(state, entity_id)
    net = _net_amount(order, refunds)
    if net is None:
        return None
    return Snapshot(
        batch_id=batch_id,
        order=order,
        refunds=refunds,
        net_amount=net,
        change_seqs=change_seqs,
    )


def _overflow_result(order_id: int, where: str) -> LineageResult:
    return LineageResult(
        status=STATUS_NET_AMOUNT_OVERFLOW,
        order_id=order_id,
        snapshots=[],
        notes=[f"{where}的净额超出 64 位整数范围，整次查询不交付沿革"],
    )


def _order_change_seqs(
    records: list[CdcRecord], entity_id: int
) -> tuple[list[int], int | None]:
    """Follow the entity's order row through one batch.

    Returns the affecting order-change seqs and the entity's id at batch end
    (``None`` once deleted — a same-batch re-insert is a different order and
    must not re-attach).
    """
    current: int | None = entity_id
    seqs: list[int] = []
    for record in records:
        if record.table != "orders":
            continue
        if record.op == "update" and record.old["order_id"] == current:
            seqs.append(record.seq)
            current = record.new["order_id"]
        elif record.op == "delete" and record.old["order_id"] == current:
            seqs.append(record.seq)
            current = None
            break
    return seqs, current


def _final_refund_tokens(
    records: list[CdcRecord], start_refund_ids: set[int]
) -> dict[int, _RefundToken]:
    """Map each refund id surviving at batch end to its identity token."""
    token_of: dict[int, _RefundToken] = {
        rid: ("start", rid) for rid in start_refund_ids
    }
    for record in records:
        if record.table != "refunds":
            continue
        if record.op == "insert":
            token_of[record.new["refund_id"]] = ("insert", record.seq)
        elif record.op == "delete":
            token_of.pop(record.old["refund_id"], None)
        else:  # update, possibly renaming the primary key
            token = token_of.pop(record.old["refund_id"])
            token_of[record.new["refund_id"]] = token
    return token_of


def _refund_change_seqs(
    records: list[CdcRecord],
    start_refund_ids: set[int],
    prev_attributed: set[int],
    end_attributed: set[int],
) -> list[int]:
    """Seqs of refund changes touching a refund attributed at either boundary."""
    final_tokens = _final_refund_tokens(records, start_refund_ids)
    relevant = {("start", rid) for rid in prev_attributed}
    relevant |= {final_tokens[rid] for rid in end_attributed if rid in final_tokens}
    if not relevant:
        return []

    seqs: list[int] = []
    token_of: dict[int, _RefundToken] = {
        rid: ("start", rid) for rid in start_refund_ids
    }
    for record in records:
        if record.table != "refunds":
            continue
        if record.op == "insert":
            token = ("insert", record.seq)
            token_of[record.new["refund_id"]] = token
        elif record.op == "delete":
            token = token_of.pop(record.old["refund_id"])
        else:
            token = token_of.pop(record.old["refund_id"])
            token_of[record.new["refund_id"]] = token
        if token in relevant:
            seqs.append(record.seq)
    return seqs


def trace_lineage(
    initial: InitialData, batches: list[Batch], order_id: int
) -> LineageResult:
    """Trace one order entity from the initial state through the batches.

    The caller must have validated the input (``validate.validate_all``);
    illegal batches are rejected at the API edge before tracing starts.
    """
    state = build_initial_state(initial)
    if order_id not in state.orders:
        return LineageResult(
            status=STATUS_ORDER_NOT_FOUND,
            order_id=order_id,
            notes=[f"初态中不存在 order_id={order_id} 的订单，无实体可追踪"],
        )

    entity_id = order_id
    snapshot = _snapshot(state, entity_id, batch_id=None, change_seqs=[])
    if snapshot is None:
        return _overflow_result(order_id, "初态")
    snapshots = [snapshot]
    # Refund ids attributed to the entity at the previous snapshot.
    prev_attributed = {row["refund_id"] for row in snapshot.refunds}

    for batch in batches:
        start_refund_ids = set(state.refunds)
        new_state, records = apply_batch_recording(
            state, batch, where=f"批次 {batch.batch_id}"
        )

        order_seqs, current = _order_change_seqs(records, entity_id)
        end_attributed: set[int] = set()
        if current is not None:
            end_attributed = {
                refund_id
                for refund_id, refund in new_state.refunds.items()
                if refund["order_id"] == current
            }
        refund_seqs = _refund_change_seqs(
            records, start_refund_ids, prev_attributed, end_attributed
        )
        change_seqs = sorted(order_seqs + refund_seqs)
        state = new_state

        if current is None:
            snapshots.append(
                Snapshot(
                    batch_id=batch.batch_id,
                    order=None,
                    refunds=[],
                    net_amount=None,
                    change_seqs=change_seqs,
                )
            )
            return LineageResult(
                status=STATUS_OK,
                order_id=order_id,
                snapshots=snapshots,
                terminated=True,
                terminated_batch_id=batch.batch_id,
                notes=[
                    f"实体在批次 {batch.batch_id} 中被删除，沿革终止；"
                    "此后同号订单属于新实体，不再追踪"
                ],
            )

        entity_id = current
        snapshot = _snapshot(state, entity_id, batch.batch_id, change_seqs)
        if snapshot is None:
            return _overflow_result(order_id, f"批次 {batch.batch_id} 末")
        snapshots.append(snapshot)
        prev_attributed = end_attributed

    return LineageResult(
        status=STATUS_OK,
        order_id=order_id,
        snapshots=snapshots,
        notes=["实体在全部批次后仍存续"],
    )
