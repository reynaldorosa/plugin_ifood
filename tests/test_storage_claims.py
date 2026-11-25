from __future__ import annotations

import json
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from pathlib import Path

import pytest

from ifood.config import Config
from ifood.sheets_parser import parse_sheet_snapshot
from ifood.storage import SyncStorage
from ifood.storage import connection_scope as storage_scope
from ifood.sync_engine import SyncEngine, TenantMismatchError


def test_webhook_delivery_claim_is_atomic(tmp_path):
    storage = SyncStorage(tmp_path / "sync.db")
    with ThreadPoolExecutor(max_workers=8) as pool:
        claims = list(
            pool.map(
                partial(storage.claim_webhook_delivery, tenant_key="default"),
                ["same-body"] * 8,
            )
        )
    assert claims.count(True) == 1
    storage.record_webhook_delivery("same-body", {"status": "DONE"}, tenant_key="default")
    delivery = storage.get_webhook_delivery("same-body", tenant_key="default")
    assert delivery is not None
    assert delivery["summary"] == {"status": "DONE"}


def test_proposal_claim_is_atomic_and_expiry_survives_reopen(tmp_path):
    db = tmp_path / "sync.db"
    storage = SyncStorage(db)
    proposal = storage.create_proposal(
        tenant_key="default",
        store_key="store", merchant_id="merchant", channel="IFOOD",
        domain="CATALOG", actor_id="operator", trigger_source="TEST",
        execution_mode="API_FIRST", backend_planned="IFOOD_API",
        status="PENDING", current_hash="a", proposed_hash="b",
        current_state=[], proposed_state=[], diff={}, guardrail_warnings=[],
    )
    reopened = SyncStorage(db)
    assert reopened.find_active_identical_proposal(
        "store", "CATALOG", "b", tenant_key="default"
    ) is not None
    with ThreadPoolExecutor(max_workers=8) as pool:
        claims = list(
            pool.map(
                partial(reopened.claim_proposal, tenant_key="default"),
                [proposal["proposal_id"]] * 8,
            )
        )
    assert claims.count(True) == 1


def test_expired_webhook_and_execution_leases_are_recoverable(tmp_path):
    db = tmp_path / "leases.db"
    storage = SyncStorage(db)
    assert storage.claim_webhook_delivery("delivery", lease_seconds=60, tenant_key="default")
    with storage_scope(db) as conn:
        conn.execute(
            "UPDATE webhook_deliveries SET lease_until_epoch=? WHERE delivery_hash='delivery'",
            (time.time() - 1,),
        )
    assert storage.claim_webhook_delivery("delivery", tenant_key="default")

    proposal = storage.create_proposal(
        tenant_key="default",
        store_key="s", merchant_id="m", channel="IFOOD", domain="CATALOG",
        actor_id="operator", trigger_source="TEST", execution_mode="API_FIRST",
        backend_planned="IFOOD_API", status="PENDING", current_hash="a",
        proposed_hash="b", current_state=[], proposed_state=[], diff={}, guardrail_warnings=[],
    )
    assert storage.claim_proposal(proposal["proposal_id"], tenant_key="default")
    with storage_scope(db) as conn:
        conn.execute(
            "UPDATE proposals SET execution_lease_until=? WHERE proposal_id=?",
            (time.time() - 1, proposal["proposal_id"]),
        )
    assert not storage.claim_proposal(proposal["proposal_id"], tenant_key="default")


def test_proposal_and_audit_creation_roll_back_together(tmp_path):
    storage = SyncStorage(tmp_path / "atomic-create.db")
    try:
        storage.create_proposal(
            tenant_key="default",
            store_key="store", merchant_id="merchant", channel="IFOOD",
            domain="CATALOG", actor_id="operator", trigger_source="TEST",
            execution_mode="API_ONLY", backend_planned="IFOOD_API", status="PENDING",
            current_hash="a", proposed_hash="b", current_state=[], proposed_state=[],
            diff={}, guardrail_warnings=[], audit_event={},
        )
    except KeyError:
        pass
    else:
        raise AssertionError("Evento de auditoria incompleto foi aceito")
    assert storage.list_proposals("default") == []
    assert storage.list_audit_events("default") == []


def test_resolution_and_audit_roll_back_together(tmp_path):
    storage = SyncStorage(tmp_path / "atomic-resolve.db")
    proposal = storage.create_proposal(
        tenant_key="default",
        store_key="store", merchant_id="merchant", channel="IFOOD",
        domain="OPENING_HOURS", actor_id="operator", trigger_source="TEST",
        execution_mode="API_ONLY", backend_planned="IFOOD_API", status="PENDING",
        current_hash="a", proposed_hash="b", current_state=[], proposed_state=[],
        diff={}, guardrail_warnings=[],
    )
    try:
        storage.resolve_proposal(
            proposal["proposal_id"], status="REJECTED", outcome={},
            expected_status="PENDING", audit_event={}, tenant_key="default",
        )
    except KeyError:
        pass
    else:
        raise AssertionError("Resolução sem auditoria foi aceita")
    assert storage.get_proposal(proposal["proposal_id"], tenant_key="default")["status"] == "PENDING"
    assert storage.list_audit_events("default") == []


def test_expired_execution_becomes_uncertain_and_is_never_reclaimed(tmp_path):
    db = tmp_path / "uncertain.db"
    storage = SyncStorage(db)
    proposal = storage.create_proposal(
        tenant_key="default",
        store_key="store", merchant_id="merchant", channel="IFOOD",
        domain="OPENING_HOURS", actor_id="operator", trigger_source="TEST",
        execution_mode="API_ONLY", backend_planned="IFOOD_API", status="PENDING",
        current_hash="a", proposed_hash="b", current_state=[], proposed_state=[],
        diff={}, guardrail_warnings=[],
    )
    assert storage.claim_proposal(proposal["proposal_id"], tenant_key="default")
    with storage_scope(db) as conn:
        conn.execute(
            "UPDATE proposals SET execution_lease_until=? WHERE proposal_id=?",
            (time.time() - 1, proposal["proposal_id"]),
        )
    reopened = SyncStorage(db)
    record = reopened.get_proposal(proposal["proposal_id"], tenant_key="default")
    assert record["status"] == "UNCERTAIN"
    assert not reopened.claim_proposal(proposal["proposal_id"], tenant_key="default")
    unresolved = reopened.find_unresolved_execution("store", "OPENING_HOURS", tenant_key="default")
    assert unresolved["proposal_id"] == proposal["proposal_id"]


def test_cross_tenant_proposal_confirmation_raises_and_leaves_db_untouched(tmp_path: Path) -> None:
    merchant_id = "550e8400-e29b-41d4-a716-446655440000"
    cfg = Config(
        True,
        frozenset({"operator"}),
        merchant_id,
        "https://merchant-api.ifood.com.br",
        "ifood-token",
        data_dir=str(tmp_path),
        write_capabilities=frozenset({"opening_hours"}),
    )
    storage = SyncStorage(cfg.db_path)
    writes: list[str] = []

    weekdays = ["MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY", "SUNDAY"]
    live_shifts = [{"dayOfWeek": day, "start": "08:00:00", "duration": 480} for day in weekdays]

    class FakeApi:
        def __init__(self, _config: Config) -> None:
            pass

        def opening_hours(self, merchant_id=None):
            return [{"shifts": live_shifts}]

        def put_opening_hours(self, shifts, merchant_id=None):
            writes.append("written")

    engine = SyncEngine(cfg, storage=storage, api_client_factory=FakeApi)
    snapshot = parse_sheet_snapshot(
        stores_rows=[{
            "store_key": "loja-centro",
            "merchant_id": merchant_id,
            "execution_mode": "API_ONLY",
        }],
        hours_rows=[{
            "store_key": "loja-centro",
            "day_of_week": day,
            "start_time": "09:00",
            "end_time": "18:00",
        } for day in weekdays],
        tenant_key="padaria-a",
    )
    assert snapshot.row_errors == []
    planned = engine.process_snapshot(
        snapshot=snapshot,
        trigger_source="TEST",
        actor_id="sheets",
        tenant_key="padaria-a",
        write_back=False,
    )
    proposal_id = planned["results"][0]["proposal_id"]
    assert proposal_id is not None
    before_proposal = storage.get_proposal(proposal_id, tenant_key="padaria-a")
    before_audits_a = storage.list_audit_events("padaria-a")
    before_audits_b = storage.list_audit_events("padaria-b")

    with pytest.raises(TenantMismatchError, match="padaria-b"):
        engine.confirm_proposal(
            proposal_id=proposal_id,
            actor_id="operator",
            tenant_key="padaria-b",
            override_guardrail=True,
            write_back=False,
        )

    assert writes == []
    after_proposal = storage.get_proposal(proposal_id, tenant_key="padaria-a")
    assert after_proposal == before_proposal
    assert after_proposal["status"] == before_proposal["status"]
    assert storage.list_audit_events("padaria-a") == before_audits_a
    assert storage.list_audit_events("padaria-b") == before_audits_b


def test_list_proposals_isolates_tenants_and_requires_tenant_key(tmp_path: Path) -> None:
    storage = SyncStorage(tmp_path / "tenants.db")
    prop_a = storage.create_proposal(
        tenant_key="padaria-a",
        store_key="loja-1",
        merchant_id="m-a",
        channel="IFOOD",
        domain="CATALOG",
        actor_id="op-a",
        trigger_source="TEST",
        execution_mode="API_FIRST",
        backend_planned="IFOOD_API",
        status="PENDING",
        current_hash="h1",
        proposed_hash="h2",
        current_state=[],
        proposed_state=[],
        diff={},
        guardrail_warnings=[],
    )
    prop_b = storage.create_proposal(
        tenant_key="padaria-b",
        store_key="loja-1",
        merchant_id="m-b",
        channel="IFOOD",
        domain="CATALOG",
        actor_id="op-b",
        trigger_source="TEST",
        execution_mode="API_FIRST",
        backend_planned="IFOOD_API",
        status="PENDING",
        current_hash="h3",
        proposed_hash="h4",
        current_state=[],
        proposed_state=[],
        diff={},
        guardrail_warnings=[],
    )

    list_a = storage.list_proposals("padaria-a")
    list_b = storage.list_proposals("padaria-b")

    assert [p["proposal_id"] for p in list_a] == [prop_a["proposal_id"]]
    assert all(p["tenant_key"] == "padaria-a" for p in list_a)
    assert [p["proposal_id"] for p in list_b] == [prop_b["proposal_id"]]
    assert all(p["tenant_key"] == "padaria-b" for p in list_b)
    assert storage.get_proposal(prop_b["proposal_id"], tenant_key="padaria-a") is None

    with pytest.raises(ValueError, match="tenant_key"):
        storage.list_proposals()  # type: ignore[misc]
    with pytest.raises(ValueError, match="tenant_key"):
        storage.list_proposals("")


def test_legacy_database_migration_preserves_rows_and_sets_default_tenant(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(
            """
            CREATE TABLE sync_snapshots (
                store_key TEXT NOT NULL,
                domain TEXT NOT NULL,
                last_applied_hash TEXT NOT NULL,
                last_state_json TEXT NOT NULL,
                last_backend TEXT NOT NULL,
                last_outcome TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (store_key, domain)
            );
            CREATE TABLE proposals (
                proposal_id TEXT PRIMARY KEY,
                store_key TEXT NOT NULL,
                merchant_id TEXT NOT NULL,
                channel TEXT NOT NULL,
                domain TEXT NOT NULL,
                actor_id TEXT NOT NULL,
                trigger_source TEXT NOT NULL,
                execution_mode TEXT NOT NULL,
                backend_planned TEXT NOT NULL,
                status TEXT NOT NULL,
                current_hash TEXT NOT NULL,
                proposed_hash TEXT NOT NULL,
                current_json TEXT NOT NULL,
                proposed_json TEXT NOT NULL,
                diff_json TEXT NOT NULL,
                guardrail_warnings_json TEXT NOT NULL DEFAULT '[]',
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                expires_monotonic REAL NOT NULL,
                expires_epoch REAL NOT NULL DEFAULT 0,
                execution_lease_until REAL NOT NULL DEFAULT 0,
                resolved_at TEXT,
                outcome_json TEXT
            );
            CREATE TABLE audit_events (
                id TEXT PRIMARY KEY,
                timestamp_utc TEXT NOT NULL,
                actor_id TEXT NOT NULL,
                store_key TEXT NOT NULL,
                domain TEXT NOT NULL,
                trigger_source TEXT NOT NULL,
                backend_used TEXT NOT NULL,
                outcome TEXT NOT NULL,
                summary TEXT NOT NULL,
                details_json TEXT NOT NULL DEFAULT '{}'
            );
            """
        )
        conn.execute(
            """
            INSERT INTO sync_snapshots (
                store_key, domain, last_applied_hash, last_state_json,
                last_backend, last_outcome, updated_at
            ) VALUES (
                'loja-legada', 'CATALOG', 'hash-1', '[]',
                'IFOOD_API', 'CONFIRMED', '2026-11-20T00:00:00+00:00'
            )
            """
        )
        conn.execute(
            """
            INSERT INTO proposals (
                proposal_id, store_key, merchant_id, channel, domain,
                actor_id, trigger_source, execution_mode, backend_planned,
                status, current_hash, proposed_hash, current_json,
                proposed_json, diff_json, guardrail_warnings_json,
                metadata_json, created_at, expires_monotonic, expires_epoch
            ) VALUES (
                'prop-legada-1', 'loja-legada', 'm-1', 'IFOOD', 'CATALOG',
                'operator', 'WEBHOOK', 'API_FIRST', 'IFOOD_API',
                'PENDING', 'h-old', 'h-new', '[]', '[]', '{}', '[]',
                '{}', '2026-11-20T00:00:00+00:00', 0, 4102444800.0
            )
            """
        )
        conn.execute(
            """
            INSERT INTO audit_events (
                id, timestamp_utc, actor_id, store_key, domain,
                trigger_source, backend_used, outcome, summary, details_json
            ) VALUES (
                'evt-legado-1', '2026-11-20T00:00:00+00:00', 'operator',
                'loja-legada', 'CATALOG', 'WEBHOOK', 'IFOOD_API',
                'PROPOSED', '1 item alterado', '{}'
            )
            """
        )
        conn.commit()
    finally:
        conn.close()

    # Run migration twice to prove idempotency
    storage = SyncStorage(db_path)
    storage = SyncStorage(db_path)

    snap = storage.get_snapshot("loja-legada", "CATALOG", tenant_key="default")
    assert snap is not None
    assert snap["tenant_key"] == "default"
    assert snap["store_key"] == "loja-legada"

    proposals = storage.list_proposals("default")
    assert len(proposals) == 1
    assert proposals[0]["proposal_id"] == "prop-legada-1"
    assert proposals[0]["tenant_key"] == "default"

    audits = storage.list_audit_events("default")
    assert len(audits) == 1
    assert audits[0]["id"] == "evt-legado-1"
    assert audits[0]["tenant_key"] == "default"

    # Verify required indexes exist
    with storage_scope(db_path) as check_conn:
        index_rows = check_conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        ).fetchall()
        index_names = {row["name"] for row in index_rows}
    assert "idx_proposals_tenant_status" in index_names
    assert "idx_proposals_tenant_store_domain" in index_names


def test_multi_bakery_tenants_config_loaded_from_json_file(monkeypatch, tmp_path: Path) -> None:
    tenants_payload = {
        "padaria-a": {
            "sheet_id": "sheet-padaria-a",
            "stores": {
                "loja-centro": {"merchant_id": "550e8400-e29b-41d4-a716-446655440000"}
            },
        },
        "padaria-b": {
            "sheet_id": "sheet-padaria-b",
            "stores": {
                "loja-sul": {"merchant_id": "660e8400-e29b-41d4-a716-446655440001"}
            },
        },
    }
    tenants_file = tmp_path / "tenants.json"
    tenants_file.write_text(json.dumps(tenants_payload), encoding="utf-8")

    monkeypatch.setenv("IFOOD_LOJA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("IFOOD_LOJA_TENANTS_FILE", str(tenants_file))
    config = Config.from_env()

    assert set(config.tenants.keys()) == {"padaria-a", "padaria-b"}
    assert config.get_tenant("padaria-a").sheet_id == "sheet-padaria-a"
    assert config.tenants["padaria-a"]["stores"]["loja-centro"]["merchant_id"] == (
        "550e8400-e29b-41d4-a716-446655440000"
    )
    assert config.get_tenant("padaria-b").sheet_id == "sheet-padaria-b"

    # Rejects secrets inside tenants JSON file
    bad_file = tmp_path / "tenants-with-secret.json"
    bad_file.write_text(
        json.dumps({
            "padaria-a": {
                "sheet_id": "sheet-padaria-a",
                "access_token": "should-not-be-here",
                "stores": {},
            }
        }),
        encoding="utf-8",
    )
    monkeypatch.setenv("IFOOD_LOJA_TENANTS_FILE", str(bad_file))
    with pytest.raises(ValueError, match="Segredo"):
        Config.from_env()

