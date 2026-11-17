"""Lineage API tests: entity identity, rebind attribution, termination, overflow."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.lineage import (
    STATUS_NET_AMOUNT_OVERFLOW,
    STATUS_OK,
    STATUS_ORDER_NOT_FOUND,
    trace_lineage,
)
from app.main import app
from app.models import Batch, InitialData

from conftest import op_delete, op_insert, op_update

INT64_MAX = 2**63 - 1
INT64_MIN = -(2**63)


def _initial() -> InitialData:
    return InitialData.model_validate(
        {
            "orders": [
                {"order_id": 1, "amount": 1000},
                {"order_id": 2, "amount": 500},
            ],
            "refunds": [
                {"refund_id": 10, "order_id": 1, "amount": 200},
                {"refund_id": 11, "order_id": 2, "amount": 50},
            ],
        }
    )


def _batch(batch_id: int, *changes: dict) -> Batch:
    return Batch.model_validate({"batch_id": batch_id, "changes": list(changes)})


def _trace(batches: list[Batch], order_id: int = 1, initial: InitialData | None = None):
    return trace_lineage(initial or _initial(), batches, order_id)


# ---------------------------------------------------------------------------
# basic snapshots
# ---------------------------------------------------------------------------

def test_initial_snapshot_only_when_no_batches():
    result = _trace([])
    assert result.status == STATUS_OK
    assert len(result.snapshots) == 1
    snap = result.snapshots[0]
    assert snap.batch_id is None
    assert snap.order == {"order_id": 1, "amount": 1000}
    assert snap.refunds == [{"refund_id": 10, "order_id": 1, "amount": 200}]
    assert snap.net_amount == 800
    assert snap.change_seqs == []
    assert result.terminated is False


def test_net_and_seqs_across_batches():
    batches = [
        _batch(7, op_update("orders", {"order_id": 1}, {"order_id": 1, "amount": 1200})),
        _batch(
            8,
            op_insert("refunds", refund_id=12, order_id=1, amount=100),
            op_insert("refunds", refund_id=13, order_id=2, amount=999),
        ),
        _batch(9, op_delete("refunds", refund_id=10)),
    ]
    result = _trace(batches)
    assert result.status == STATUS_OK
    assert [s.batch_id for s in result.snapshots] == [None, 7, 8, 9]

    snap7 = result.snapshots[1]
    assert snap7.order == {"order_id": 1, "amount": 1200}
    assert snap7.net_amount == 1000  # 1200 - 200
    assert snap7.change_seqs == [0]

    snap8 = result.snapshots[2]
    # Refund 13 belongs to order 2: not attributed, its seq is not listed.
    assert [r["refund_id"] for r in snap8.refunds] == [10, 12]
    assert snap8.net_amount == 900  # 1200 - 200 - 100
    assert snap8.change_seqs == [0]

    snap9 = result.snapshots[3]
    assert [r["refund_id"] for r in snap9.refunds] == [12]
    assert snap9.net_amount == 1100  # 1200 - 100
    assert snap9.change_seqs == [0]


def test_unrelated_changes_produce_empty_seqs():
    batches = [
        _batch(
            5,
            op_update("orders", {"order_id": 2}, {"order_id": 2, "amount": 700}),
            op_insert("refunds", refund_id=12, order_id=2, amount=10),
            op_delete("refunds", refund_id=11),
        )
    ]
    result = _trace(batches)
    snap = result.snapshots[1]
    assert snap.order == {"order_id": 1, "amount": 1000}
    assert snap.net_amount == 800
    assert snap.change_seqs == []


# ---------------------------------------------------------------------------
# renumbering (改号) and id reuse
# ---------------------------------------------------------------------------

def test_rename_follows_entity():
    batches = [
        _batch(
            3,
            op_update("orders", {"order_id": 1}, {"order_id": 5, "amount": 1000}),
            op_update(
                "refunds",
                {"refund_id": 10},
                {"refund_id": 10, "order_id": 5, "amount": 200},
            ),
        ),
        _batch(4, op_insert("refunds", refund_id=12, order_id=5, amount=300)),
    ]
    result = _trace(batches)
    assert [s.batch_id for s in result.snapshots] == [None, 3, 4]
    snap3 = result.snapshots[1]
    assert snap3.order == {"order_id": 5, "amount": 1000}
    assert snap3.net_amount == 800
    snap4 = result.snapshots[2]
    # The new refund binds to the renamed id and is attributed to the entity.
    assert [r["refund_id"] for r in snap4.refunds] == [10, 12]
    assert snap4.net_amount == 500
    assert snap4.change_seqs == [0]


def test_rename_requires_refund_sync_in_same_batch():
    # Renaming without moving the refunds breaks the FK at batch end -> 422
    # at the API edge; trace-level callers validate first.
    batches = [
        _batch(
            3,
            op_update("orders", {"order_id": 1}, {"order_id": 5, "amount": 1000}),
            op_update(
                "refunds",
                {"refund_id": 10},
                {"refund_id": 10, "order_id": 5, "amount": 200},
            ),
        )
    ]
    result = _trace(batches)
    snap = result.snapshots[1]
    assert snap.order == {"order_id": 5, "amount": 1000}
    assert snap.refunds == [{"refund_id": 10, "order_id": 5, "amount": 200}]
    assert snap.net_amount == 800
    # Both the rename (seq 0) and the refund rebind (seq 1) are reported.
    assert snap.change_seqs == [0, 1]


def test_rename_chain_within_one_batch():
    batches = [
        _batch(
            3,
            op_update("orders", {"order_id": 1}, {"order_id": 6, "amount": 1000}),
            op_update("orders", {"order_id": 6}, {"order_id": 7, "amount": 1100}),
            op_update(
                "refunds",
                {"refund_id": 10},
                {"refund_id": 10, "order_id": 7, "amount": 200},
            ),
        )
    ]
    result = _trace(batches)
    snap = result.snapshots[1]
    assert snap.order == {"order_id": 7, "amount": 1100}
    assert snap.net_amount == 900
    assert snap.change_seqs == [0, 1, 2]


def test_later_batch_reusing_old_id_is_not_attached():
    """后批复用旧号：新订单不得接到旧沿革。"""
    batches = [
        _batch(
            3,
            op_update("orders", {"order_id": 1}, {"order_id": 5, "amount": 1000}),
            op_update(
                "refunds",
                {"refund_id": 10},
                {"refund_id": 10, "order_id": 5, "amount": 200},
            ),
        ),
        _batch(
            4,
            op_insert("orders", order_id=1, amount=42),
            op_insert("refunds", refund_id=20, order_id=1, amount=7),
            op_update("orders", {"order_id": 1}, {"order_id": 1, "amount": 43}),
        ),
    ]
    result = _trace(batches)
    assert [s.batch_id for s in result.snapshots] == [None, 3, 4]
    snap4 = result.snapshots[2]
    # The entity is order 5; the new order 1 and its refund are invisible.
    assert snap4.order == {"order_id": 5, "amount": 1000}
    assert snap4.refunds == [{"refund_id": 10, "order_id": 5, "amount": 200}]
    assert snap4.net_amount == 800
    assert snap4.change_seqs == []


def test_same_batch_delete_reinsert_is_not_attached():
    """同批删后重插：重插订单是新实体，旧沿革在删除处终止。"""
    batches = [
        _batch(
            3,
            op_delete("refunds", refund_id=10),
            op_delete("orders", order_id=1),
            op_insert("orders", order_id=1, amount=77),
        ),
        _batch(4, op_update("orders", {"order_id": 1}, {"order_id": 1, "amount": 88})),
    ]
    result = _trace(batches)
    assert result.terminated is True
    assert result.terminated_batch_id == 3
    # Snapshots stop at the deletion batch; batch 4 is not reported.
    assert [s.batch_id for s in result.snapshots] == [None, 3]
    terminal = result.snapshots[-1]
    assert terminal.order is None
    assert terminal.refunds == []
    assert terminal.net_amount is None
    # seq 0 removed the entity's refund, seq 1 deleted the order; the
    # re-insert at seq 2 belongs to a new entity and is not listed.
    assert terminal.change_seqs == [0, 1]


def test_delete_then_later_batch_reinsert_keeps_lineage_closed():
    batches = [
        _batch(
            3,
            op_delete("refunds", refund_id=10),
            op_delete("orders", order_id=1),
        ),
        _batch(4, op_insert("orders", order_id=1, amount=55)),
    ]
    result = _trace(batches)
    assert result.terminated is True
    assert [s.batch_id for s in result.snapshots] == [None, 3]
    assert result.snapshots[-1].change_seqs == [0, 1]


# ---------------------------------------------------------------------------
# refund rebind (退款改绑)
# ---------------------------------------------------------------------------

def test_refund_rebind_away_from_entity():
    batches = [
        _batch(
            5,
            op_update(
                "refunds",
                {"refund_id": 10},
                {"refund_id": 10, "order_id": 2, "amount": 200},
            ),
        )
    ]
    result = _trace(batches)
    snap = result.snapshots[1]
    # Batch-end attribution: refund 10 now belongs to order 2.
    assert snap.refunds == []
    assert snap.net_amount == 1000
    # The rebind touched a refund attributed at batch start -> listed.
    assert snap.change_seqs == [0]


def test_refund_rebind_into_entity():
    batches = [
        _batch(
            5,
            op_update(
                "refunds",
                {"refund_id": 11},
                {"refund_id": 11, "order_id": 1, "amount": 50},
            ),
        )
    ]
    result = _trace(batches)
    snap = result.snapshots[1]
    assert [r["refund_id"] for r in snap.refunds] == [10, 11]
    assert snap.net_amount == 750  # 1000 - 200 - 50
    assert snap.change_seqs == [0]


def test_refund_rebind_away_and_back_within_batch_is_reported():
    batches = [
        _batch(
            5,
            op_update(
                "refunds",
                {"refund_id": 10},
                {"refund_id": 10, "order_id": 2, "amount": 200},
            ),
            op_update(
                "refunds",
                {"refund_id": 10},
                {"refund_id": 10, "order_id": 1, "amount": 250},
            ),
        )
    ]
    result = _trace(batches)
    snap = result.snapshots[1]
    # Attributed at both boundaries; both touching seqs are listed.
    assert snap.refunds == [{"refund_id": 10, "order_id": 1, "amount": 250}]
    assert snap.net_amount == 750
    assert snap.change_seqs == [0, 1]


def test_refund_insert_and_delete_within_batch_is_not_listed():
    """批内先插后删的退款在两个边界都不归属实体：不进快照、不占序号。"""
    batches = [
        _batch(
            5,
            op_insert("refunds", refund_id=12, order_id=1, amount=10),
            op_delete("refunds", refund_id=12),
        )
    ]
    result = _trace(batches)
    snap = result.snapshots[1]
    assert snap.refunds == [{"refund_id": 10, "order_id": 1, "amount": 200}]
    assert snap.net_amount == 800
    assert snap.change_seqs == []


def test_refund_amount_update_while_bound():
    batches = [
        _batch(
            5,
            op_update(
                "refunds",
                {"refund_id": 10},
                {"refund_id": 10, "order_id": 1, "amount": 350},
            ),
        )
    ]
    result = _trace(batches)
    snap = result.snapshots[1]
    assert snap.net_amount == 650
    assert snap.change_seqs == [0]


def test_refund_renamed_while_bound_stays_attributed():
    batches = [
        _batch(
            5,
            op_update(
                "refunds",
                {"refund_id": 10},
                {"refund_id": 30, "order_id": 1, "amount": 200},
            ),
        )
    ]
    result = _trace(batches)
    snap = result.snapshots[1]
    assert snap.refunds == [{"refund_id": 30, "order_id": 1, "amount": 200}]
    assert snap.net_amount == 800
    assert snap.change_seqs == [0]


def test_refund_rename_and_rebind_into_entity_in_one_op():
    # Refund 11 (order 2) becomes refund 99 bound to the entity in a single
    # update: the batch-end attribution must catch it even though neither its
    # old nor new id alone matches a single boundary set.
    batches = [
        _batch(
            5,
            op_update(
                "refunds",
                {"refund_id": 11},
                {"refund_id": 99, "order_id": 1, "amount": 50},
            ),
        )
    ]
    result = _trace(batches)
    snap = result.snapshots[1]
    assert [r["refund_id"] for r in snap.refunds] == [10, 99]
    assert snap.net_amount == 750
    assert snap.change_seqs == [0]


def test_refund_id_reuse_within_batch_not_misattributed():
    # The entity's refund 10 is renamed to 20 and rebound away (seq 0); an
    # unrelated refund 50 then takes the freed id 10 (seq 1).  Seq 1 touches a
    # *different* refund entity and must not appear in the lineage.
    initial = InitialData.model_validate(
        {
            "orders": [{"order_id": 1, "amount": 1000}, {"order_id": 2, "amount": 500}],
            "refunds": [
                {"refund_id": 10, "order_id": 1, "amount": 200},
                {"refund_id": 50, "order_id": 2, "amount": 60},
            ],
        }
    )
    batches = [
        _batch(
            5,
            op_update(
                "refunds",
                {"refund_id": 10},
                {"refund_id": 20, "order_id": 2, "amount": 200},
            ),
            op_update(
                "refunds",
                {"refund_id": 50},
                {"refund_id": 10, "order_id": 2, "amount": 60},
            ),
        )
    ]
    result = _trace(batches, initial=initial)
    snap = result.snapshots[1]
    assert snap.refunds == []
    assert snap.net_amount == 1000
    assert snap.change_seqs == [0]


# ---------------------------------------------------------------------------
# termination
# ---------------------------------------------------------------------------

def test_delete_terminates_and_skips_later_batches():
    batches = [
        _batch(3, op_insert("refunds", refund_id=12, order_id=1, amount=100)),
        _batch(
            4,
            op_delete("refunds", refund_id=10),
            op_delete("refunds", refund_id=12),
            op_delete("orders", order_id=1),
        ),
        _batch(5, op_insert("orders", order_id=9, amount=1)),
    ]
    result = _trace(batches)
    assert result.terminated is True
    assert result.terminated_batch_id == 4
    assert [s.batch_id for s in result.snapshots] == [None, 3, 4]
    terminal = result.snapshots[-1]
    assert terminal.order is None
    assert terminal.net_amount is None
    # Both refund deletes touched attributed refunds; the order delete ends it.
    assert terminal.change_seqs == [0, 1, 2]


# ---------------------------------------------------------------------------
# not found / overflow / illegal input
# ---------------------------------------------------------------------------

def test_order_not_found():
    result = _trace([], order_id=999)
    assert result.status == STATUS_ORDER_NOT_FOUND
    assert result.snapshots == []


def test_net_amount_overflow_aborts_whole_query():
    initial = InitialData.model_validate(
        {
            "orders": [{"order_id": 1, "amount": INT64_MAX}],
            "refunds": [],
        }
    )
    batches = [
        _batch(3, op_insert("refunds", refund_id=10, order_id=1, amount=-1)),
        _batch(4, op_delete("refunds", refund_id=10)),
    ]
    result = _trace(batches, order_id=1, initial=initial)
    # INT64_MAX - (-1) = 2**63 -> out of range; nothing is delivered, not
    # even the (in-range) initial snapshot.
    assert result.status == STATUS_NET_AMOUNT_OVERFLOW
    assert result.snapshots == []
    assert "批次 3" in result.notes[0]


def test_initial_state_overflow():
    initial = InitialData.model_validate(
        {
            "orders": [{"order_id": 1, "amount": INT64_MAX}],
            "refunds": [{"refund_id": 10, "order_id": 1, "amount": -1}],
        }
    )
    result = _trace([], order_id=1, initial=initial)
    assert result.status == STATUS_NET_AMOUNT_OVERFLOW
    assert result.snapshots == []
    assert "初态" in result.notes[0]


def test_negative_overflow_also_detected():
    initial = InitialData.model_validate(
        {
            "orders": [{"order_id": 1, "amount": INT64_MIN}],
            "refunds": [],
        }
    )
    batches = [_batch(3, op_insert("refunds", refund_id=10, order_id=1, amount=1))]
    result = _trace(batches, order_id=1, initial=initial)
    assert result.status == STATUS_NET_AMOUNT_OVERFLOW
    assert result.snapshots == []


# ---------------------------------------------------------------------------
# API surface
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(app)


def _payload(batches: list[dict], order_id: int = 1) -> dict:
    return {
        "initial": {
            "orders": [
                {"order_id": 1, "amount": 1000},
                {"order_id": 2, "amount": 500},
            ],
            "refunds": [
                {"refund_id": 10, "order_id": 1, "amount": 200},
                {"refund_id": 11, "order_id": 2, "amount": 50},
            ],
        },
        "batches": batches,
        "order_id": order_id,
    }


def test_api_lineage_roundtrip(client: TestClient):
    payload = _payload(
        [
            {
                "batch_id": 3,
                "changes": [
                    op_update("orders", {"order_id": 1}, {"order_id": 5, "amount": 1000}),
                    op_update(
                        "refunds",
                        {"refund_id": 10},
                        {"refund_id": 10, "order_id": 5, "amount": 200},
                    ),
                ],
            },
            {"batch_id": 4, "changes": [op_insert("orders", order_id=1, amount=42)]},
        ]
    )
    response = client.post("/v1/lineage", json=payload)
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["order_id"] == 1
    assert body["terminated"] is False
    assert body["terminated_batch_id"] is None
    assert [s["batch_id"] for s in body["snapshots"]] == [None, 3, 4]
    assert body["snapshots"][1]["order"] == {"order_id": 5, "amount": 1000}
    assert body["snapshots"][1]["change_seqs"] == [0, 1]
    assert body["snapshots"][2]["change_seqs"] == []


def test_api_illegal_batch_is_422_and_delivers_nothing(client: TestClient):
    payload = _payload(
        [{"batch_id": 3, "changes": [op_delete("refunds", refund_id=999)]}]
    )
    response = client.post("/v1/lineage", json=payload)
    assert response.status_code == 422
    assert "不存在" in response.json()["detail"]


def test_api_broken_fk_batch_is_422(client: TestClient):
    # Rename the order without moving its refund: FK breaks at batch end.
    payload = _payload(
        [
            {
                "batch_id": 3,
                "changes": [
                    op_update("orders", {"order_id": 1}, {"order_id": 5, "amount": 1000})
                ],
            }
        ]
    )
    assert client.post("/v1/lineage", json=payload).status_code == 422


def test_api_order_not_found(client: TestClient):
    response = client.post("/v1/lineage", json=_payload([], order_id=12345))
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "order_not_found"
    assert body["snapshots"] == []


def test_api_overflow_delivers_no_lineage(client: TestClient):
    payload = {
        "initial": {"orders": [{"order_id": 1, "amount": INT64_MAX}], "refunds": []},
        "batches": [
            {
                "batch_id": 3,
                "changes": [op_insert("refunds", refund_id=10, order_id=1, amount=-1)],
            }
        ],
        "order_id": 1,
    }
    response = client.post("/v1/lineage", json=payload)
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "net_amount_overflow"
    assert body["snapshots"] == []


def test_api_duplicate_batch_ids_rejected(client: TestClient):
    payload = _payload(
        [
            {"batch_id": 3, "changes": [op_insert("orders", order_id=8, amount=1)]},
            {"batch_id": 3, "changes": [op_insert("orders", order_id=9, amount=1)]},
        ]
    )
    assert client.post("/v1/lineage", json=payload).status_code == 422


def test_swagger_lists_lineage(client: TestClient):
    assert client.get("/docs").status_code == 200
    schema = client.get("/openapi.json").json()
    assert "/v1/lineage" in schema["paths"]
    # Original endpoints are preserved.
    assert "/v1/minimize" in schema["paths"]
    assert "/v1/validate" in schema["paths"]
