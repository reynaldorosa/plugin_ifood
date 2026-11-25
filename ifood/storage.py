"""Persistência SQLite WAL para snapshots, propostas de mudança e auditoria com isolamento por padaria (tenant_key)."""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA_SQL = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS sync_snapshots (
    tenant_key TEXT NOT NULL DEFAULT 'default',
    store_key TEXT NOT NULL,
    domain TEXT NOT NULL,
    last_applied_hash TEXT NOT NULL,
    last_state_json TEXT NOT NULL,
    last_backend TEXT NOT NULL,
    last_outcome TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (tenant_key, store_key, domain)
);

CREATE TABLE IF NOT EXISTS proposals (
    proposal_id TEXT PRIMARY KEY,
    tenant_key TEXT NOT NULL DEFAULT 'default',
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

CREATE INDEX IF NOT EXISTS idx_proposals_status ON proposals(status, created_at DESC);

CREATE TABLE IF NOT EXISTS webhook_deliveries (
    delivery_hash TEXT PRIMARY KEY,
    tenant_key TEXT NOT NULL DEFAULT 'default',
    received_at TEXT NOT NULL,
    summary_json TEXT NOT NULL,
    lease_until_epoch REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS sync_locks (
    lock_key TEXT PRIMARY KEY,
    tenant_key TEXT NOT NULL DEFAULT 'default',
    owner_id TEXT NOT NULL,
    expires_epoch REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS sheets_poller_status (
    tenant_key TEXT PRIMARY KEY,
    last_success_at TEXT,
    last_error TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    last_summary TEXT
);

CREATE TABLE IF NOT EXISTS audit_events (
    id TEXT PRIMARY KEY,
    tenant_key TEXT NOT NULL DEFAULT 'default',
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

CREATE INDEX IF NOT EXISTS idx_audit_events_ts ON audit_events(timestamp_utc DESC);

CREATE TABLE IF NOT EXISTS portal_session_status (
    tenant_key TEXT NOT NULL DEFAULT 'default',
    channel TEXT NOT NULL,
    state TEXT NOT NULL,
    last_error TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (tenant_key, channel)
);

CREATE TABLE IF NOT EXISTS catalog_removal_grace (
    tenant_key TEXT NOT NULL,
    store_key TEXT NOT NULL,
    item_id TEXT NOT NULL,
    first_missing_epoch REAL NOT NULL,
    PRIMARY KEY (tenant_key, store_key, item_id)
);

CREATE TABLE IF NOT EXISTS sync_notifications (
    id TEXT PRIMARY KEY,
    tenant_key TEXT NOT NULL,
    event_key TEXT NOT NULL,
    event_type TEXT NOT NULL,
    message_text TEXT NOT NULL,
    delivery_status TEXT NOT NULL,
    error_message TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (tenant_key, event_key)
);

CREATE TABLE IF NOT EXISTS auto_write_budget (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_key TEXT NOT NULL,
    store_key TEXT NOT NULL,
    created_epoch REAL NOT NULL,
    units INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_auto_write_budget_window
    ON auto_write_budget(tenant_key,store_key,created_epoch);
"""

TENANT_INDEXES_SQL = """
CREATE INDEX IF NOT EXISTS idx_proposals_tenant_status ON proposals(tenant_key, status);
CREATE INDEX IF NOT EXISTS idx_proposals_tenant_store_domain ON proposals(tenant_key, store_key, domain);
CREATE INDEX IF NOT EXISTS idx_snapshots_tenant_store_domain ON sync_snapshots(tenant_key, store_key, domain);
CREATE INDEX IF NOT EXISTS idx_audit_tenant_store_domain ON audit_events(tenant_key, store_key, domain);
"""


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _require_tenant_key(tenant_key: str | None) -> str:
    if not isinstance(tenant_key, str) or not tenant_key.strip():
        raise ValueError(
            "tenant_key é obrigatório nas consultas de leitura/escrita do SyncStorage."
        )
    return tenant_key.strip()


def _get_connection(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA busy_timeout=5000;")
    return conn


@contextmanager
def connection_scope(db_path: Path) -> Generator[sqlite3.Connection, None, None]:
    conn = _get_connection(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _migrate_snapshots_table_if_needed(conn: sqlite3.Connection) -> None:
    snap_info = conn.execute("PRAGMA table_info(sync_snapshots)").fetchall()
    col_names = {row[1] for row in snap_info}
    pk_cols = {row[1] for row in snap_info if row[5] > 0}
    if "tenant_key" in col_names and "tenant_key" in pk_cols:
        conn.execute(
            "UPDATE sync_snapshots SET tenant_key = 'default' WHERE tenant_key IS NULL OR TRIM(tenant_key) = ''"
        )
        return

    if "tenant_key" not in col_names:
        conn.execute(
            "ALTER TABLE sync_snapshots ADD COLUMN tenant_key TEXT NOT NULL DEFAULT 'default'"
        )
    conn.execute(
        """
        CREATE TABLE sync_snapshots_migrated (
            tenant_key TEXT NOT NULL DEFAULT 'default',
            store_key TEXT NOT NULL,
            domain TEXT NOT NULL,
            last_applied_hash TEXT NOT NULL,
            last_state_json TEXT NOT NULL,
            last_backend TEXT NOT NULL,
            last_outcome TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (tenant_key, store_key, domain)
        )
        """
    )
    conn.execute(
        """
        INSERT INTO sync_snapshots_migrated (
            tenant_key, store_key, domain, last_applied_hash,
            last_state_json, last_backend, last_outcome, updated_at
        )
        SELECT
            COALESCE(NULLIF(TRIM(tenant_key), ''), 'default'),
            store_key, domain, last_applied_hash,
            last_state_json, last_backend, last_outcome, updated_at
        FROM sync_snapshots
        """
    )
    conn.execute("DROP TABLE sync_snapshots")
    conn.execute("ALTER TABLE sync_snapshots_migrated RENAME TO sync_snapshots")


def _ensure_tenant_column(conn: sqlite3.Connection, table_name: str) -> None:
    cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table_name})")}
    if "tenant_key" not in cols:
        conn.execute(
            f"ALTER TABLE {table_name} ADD COLUMN tenant_key TEXT NOT NULL DEFAULT 'default'"
        )
    conn.execute(
        f"UPDATE {table_name} SET tenant_key = 'default' WHERE tenant_key IS NULL OR TRIM(tenant_key) = ''"
    )


def _migrate_poller_status_if_needed(conn: sqlite3.Connection) -> None:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(sheets_poller_status)")}
    if "id" not in cols:
        return
    if "tenant_key" not in cols:
        conn.execute("ALTER TABLE sheets_poller_status ADD COLUMN tenant_key TEXT NOT NULL DEFAULT 'default'")
    conn.execute("""CREATE TABLE sheets_poller_status_new (
        tenant_key TEXT PRIMARY KEY, last_success_at TEXT, last_error TEXT,
        consecutive_failures INTEGER NOT NULL DEFAULT 0, last_summary TEXT
    )""")
    conn.execute("""INSERT INTO sheets_poller_status_new
        (tenant_key,last_success_at,last_error,consecutive_failures,last_summary)
        SELECT COALESCE(NULLIF(TRIM(tenant_key),''),'default'),last_success_at,last_error,
               consecutive_failures,last_summary FROM sheets_poller_status""")
    conn.execute("DROP TABLE sheets_poller_status")
    conn.execute("ALTER TABLE sheets_poller_status_new RENAME TO sheets_poller_status")


class SyncStorage:
    """Repositório SQLite WAL para estado de sincronização, propostas e auditoria isolado por tenant."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        with connection_scope(self.db_path) as conn:
            conn.executescript(SCHEMA_SQL)
            _migrate_snapshots_table_if_needed(conn)
            _migrate_poller_status_if_needed(conn)
            for tbl in (
                "proposals",
                "audit_events",
                "webhook_deliveries",
                "sync_locks",
                "sheets_poller_status",
                "portal_session_status",
            ):
                _ensure_tenant_column(conn, tbl)

            columns = {row[1] for row in conn.execute("PRAGMA table_info(proposals)")}
            if "expires_epoch" not in columns:
                conn.execute(
                    "ALTER TABLE proposals ADD COLUMN expires_epoch REAL NOT NULL DEFAULT 0"
                )
            if "execution_lease_until" not in columns:
                conn.execute(
                    "ALTER TABLE proposals ADD COLUMN execution_lease_until REAL NOT NULL DEFAULT 0"
                )
            delivery_columns = {
                row[1] for row in conn.execute("PRAGMA table_info(webhook_deliveries)")
            }
            if "lease_until_epoch" not in delivery_columns:
                conn.execute(
                    "ALTER TABLE webhook_deliveries ADD COLUMN lease_until_epoch REAL NOT NULL DEFAULT 0"
                )

            conn.executescript(TENANT_INDEXES_SQL)

            # Um processo interrompido durante a chamada externa nunca pode
            # voltar a executar a mesma proposta quando o lease vencer.
            expired = conn.execute(
                "SELECT proposal_id,tenant_key,actor_id,store_key,domain,backend_planned "
                "FROM proposals WHERE status='EXECUTING' AND execution_lease_until < ?",
                (time.time(),),
            ).fetchall()
            for row in expired:
                now = _utc_now()
                outcome = {
                    "outcome": "UNCERTAIN",
                    "message": "Processo interrompido; reconciliação obrigatória.",
                }
                conn.execute(
                    "UPDATE proposals SET status='UNCERTAIN', resolved_at=?, outcome_json=?, "
                    "execution_lease_until=0 WHERE proposal_id=? AND status='EXECUTING'",
                    (now, json.dumps(outcome, ensure_ascii=False), row["proposal_id"]),
                )
                conn.execute(
                    "INSERT INTO audit_events (id,tenant_key,timestamp_utc,actor_id,store_key,domain,"
                    "trigger_source,backend_used,outcome,summary,details_json) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        str(uuid.uuid4()),
                        row["tenant_key"] or "default",
                        now,
                        row["actor_id"],
                        row["store_key"],
                        row["domain"],
                        "SYSTEM_RECOVERY",
                        row["backend_planned"],
                        "UNCERTAIN",
                        "Lease de execução expirado; reconciliação obrigatória.",
                        json.dumps({"proposal_id": row["proposal_id"]}),
                    ),
                )

    def get_snapshot(
        self,
        store_key: str,
        domain: str,
        tenant_key: str | None = None,
    ) -> dict[str, Any] | None:
        tenant = _require_tenant_key(tenant_key)
        with connection_scope(self.db_path) as conn:
            row = conn.execute(
                "SELECT * FROM sync_snapshots WHERE tenant_key = ? AND store_key = ? AND domain = ?",
                (tenant, store_key, domain),
            ).fetchone()
        if row is None:
            return None
        return {
            "tenant_key": row["tenant_key"],
            "store_key": row["store_key"],
            "domain": row["domain"],
            "last_applied_hash": row["last_applied_hash"],
            "last_state": json.loads(row["last_state_json"]),
            "last_backend": row["last_backend"],
            "last_outcome": row["last_outcome"],
            "updated_at": row["updated_at"],
        }

    def review_catalog_removals(
        self, tenant_key: str, store_key: str, previous_items: list[dict[str, Any]],
        desired_items: list[dict[str, Any]], *, now_epoch: float | None = None,
    ) -> tuple[list[str], list[str], list[str]]:
        """Marca ausências; devolve IDs em carência e IDs com 30 minutos vencidos."""
        tenant = _require_tenant_key(tenant_key)
        now = time.time() if now_epoch is None else now_epoch
        previous = {str(item["item_id"]) for item in previous_items if item.get("item_id")}
        desired = {str(item["item_id"]) for item in desired_items if item.get("item_id")}
        missing = previous - desired
        new_missing: list[str] = []
        with connection_scope(self.db_path) as conn:
            for item_id in missing:
                inserted = conn.execute(
                    """INSERT OR IGNORE INTO catalog_removal_grace
                    (tenant_key,store_key,item_id,first_missing_epoch) VALUES (?,?,?,?)""",
                    (tenant, store_key, item_id, now),
                )
                if inserted.rowcount == 1:
                    new_missing.append(item_id)
            if desired:
                conn.executemany(
                    "DELETE FROM catalog_removal_grace WHERE tenant_key=? AND store_key=? AND item_id=?",
                    [(tenant, store_key, item_id) for item_id in desired],
                )
            rows = conn.execute(
                """SELECT item_id,first_missing_epoch FROM catalog_removal_grace
                WHERE tenant_key=? AND store_key=?""", (tenant, store_key),
            ).fetchall()
        waiting = sorted(row["item_id"] for row in rows if row["item_id"] in missing
                         and now - row["first_missing_epoch"] < 1800)
        due = sorted(row["item_id"] for row in rows if row["item_id"] in missing
                     and now - row["first_missing_epoch"] >= 1800)
        return waiting, due, sorted(new_missing)

    def create_notification(self, tenant_key: str, event_key: str,
                            event_type: str, message: str) -> dict[str, Any] | None:
        tenant = _require_tenant_key(tenant_key)
        note_id = str(uuid.uuid4())
        with connection_scope(self.db_path) as conn:
            cur = conn.execute(
                """INSERT OR IGNORE INTO sync_notifications
                (id,tenant_key,event_key,event_type,message_text,delivery_status,created_at)
                VALUES (?,?,?,?,?,'PENDING',?)""",
                (note_id, tenant, event_key, event_type, message, _utc_now()),
            )
            if cur.rowcount == 1:
                return {"id": note_id, "message_text": message}
            row = conn.execute(
                "SELECT id FROM sync_notifications WHERE tenant_key=? AND event_key=?",
                (tenant, event_key),
            ).fetchone()
            if row is not None:
                claimed = conn.execute(
                    "UPDATE sync_notifications SET delivery_status='PENDING', error_message=NULL "
                    "WHERE id=? AND delivery_status IN ('FAILED','UNCONFIGURED')", (row["id"],),
                )
                if claimed.rowcount == 1:
                    return {"id": row["id"], "message_text": message}
        return None

    def set_notification_delivery(self, note_id: str, status: str,
                                  error: str | None = None) -> None:
        with connection_scope(self.db_path) as conn:
            conn.execute("UPDATE sync_notifications SET delivery_status=?, error_message=? WHERE id=?",
                         (status, error, note_id))

    def list_notifications(self, tenant_key: str, limit: int = 100) -> list[dict[str, Any]]:
        tenant = _require_tenant_key(tenant_key)
        with connection_scope(self.db_path) as conn:
            return [dict(row) for row in conn.execute(
                "SELECT * FROM sync_notifications WHERE tenant_key=? ORDER BY created_at DESC LIMIT ?",
                (tenant, max(1, min(limit, 500))),
            )]

    def claim_auto_write_budget(self, tenant_key: str, store_key: str,
                                units: int, limit: int) -> bool:
        """Reserva limite de alterações por minuto atomicamente entre workers."""
        tenant = _require_tenant_key(tenant_key)
        if units < 1 or units > limit:
            return False
        now = time.time()
        with connection_scope(self.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM auto_write_budget WHERE created_epoch < ?", (now - 60,))
            used = conn.execute(
                "SELECT COALESCE(SUM(units),0) FROM auto_write_budget "
                "WHERE tenant_key=? AND store_key=? AND created_epoch>=?",
                (tenant, store_key, now - 60),
            ).fetchone()[0]
            if used + units > limit:
                return False
            conn.execute(
                "INSERT INTO auto_write_budget(tenant_key,store_key,created_epoch,units) VALUES(?,?,?,?)",
                (tenant, store_key, now, units),
            )
        return True

    def list_snapshots(self, tenant_key: str | None = None) -> list[dict[str, Any]]:
        tenant = _require_tenant_key(tenant_key)
        with connection_scope(self.db_path) as conn:
            rows = conn.execute(
                "SELECT * FROM sync_snapshots WHERE tenant_key = ? ORDER BY store_key ASC, domain ASC",
                (tenant,),
            ).fetchall()
        return [
            {
                "tenant_key": row["tenant_key"],
                "store_key": row["store_key"],
                "domain": row["domain"],
                "last_applied_hash": row["last_applied_hash"],
                "last_state": json.loads(row["last_state_json"]),
                "last_backend": row["last_backend"],
                "last_outcome": row["last_outcome"],
                "updated_at": row["updated_at"],
            }
            for row in rows
        ]

    def upsert_snapshot(
        self,
        *,
        tenant_key: str | None = None,
        store_key: str,
        domain: str,
        last_applied_hash: str,
        last_state: Any,
        last_backend: str,
        last_outcome: str,
    ) -> None:
        tenant = _require_tenant_key(tenant_key)
        now = _utc_now()
        with connection_scope(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO sync_snapshots (
                    tenant_key, store_key, domain, last_applied_hash, last_state_json,
                    last_backend, last_outcome, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(tenant_key, store_key, domain) DO UPDATE SET
                    last_applied_hash = excluded.last_applied_hash,
                    last_state_json = excluded.last_state_json,
                    last_backend = excluded.last_backend,
                    last_outcome = excluded.last_outcome,
                    updated_at = excluded.updated_at
                """,
                (
                    tenant,
                    store_key,
                    domain,
                    last_applied_hash,
                    json.dumps(last_state, ensure_ascii=False),
                    last_backend,
                    last_outcome,
                    now,
                ),
            )

    def create_proposal(
        self,
        *,
        tenant_key: str | None = None,
        store_key: str,
        merchant_id: str,
        channel: str,
        domain: str,
        actor_id: str,
        trigger_source: str,
        execution_mode: str,
        backend_planned: str,
        status: str,
        current_hash: str,
        proposed_hash: str,
        current_state: Any,
        proposed_state: Any,
        diff: dict[str, Any],
        guardrail_warnings: list[str],
        metadata: dict[str, Any] | None = None,
        audit_event: dict[str, Any] | None = None,
        ttl_seconds: int = 3600,
    ) -> dict[str, Any]:
        tenant = _require_tenant_key(tenant_key)
        proposal_id = str(uuid.uuid4())
        now = _utc_now()
        expires_mono = time.monotonic() + max(60, ttl_seconds)
        with connection_scope(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO proposals (
                    proposal_id, tenant_key, store_key, merchant_id, channel, domain,
                    actor_id, trigger_source, execution_mode, backend_planned,
                    status, current_hash, proposed_hash, current_json,
                    proposed_json, diff_json, guardrail_warnings_json,
                    metadata_json, created_at, expires_monotonic, expires_epoch
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    proposal_id,
                    tenant,
                    store_key,
                    merchant_id,
                    channel,
                    domain,
                    actor_id,
                    trigger_source,
                    execution_mode,
                    backend_planned,
                    status,
                    current_hash,
                    proposed_hash,
                    json.dumps(current_state, ensure_ascii=False),
                    json.dumps(proposed_state, ensure_ascii=False),
                    json.dumps(diff, ensure_ascii=False),
                    json.dumps(guardrail_warnings, ensure_ascii=False),
                    json.dumps(metadata or {}, ensure_ascii=False),
                    now,
                    expires_mono,
                    time.time() + max(60, ttl_seconds),
                ),
            )
            if audit_event is not None:
                details = dict(audit_event.get("details") or {})
                details["proposal_id"] = proposal_id
                conn.execute(
                    "INSERT INTO audit_events (id,tenant_key,timestamp_utc,actor_id,store_key,domain,"
                    "trigger_source,backend_used,outcome,summary,details_json) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        str(uuid.uuid4()),
                        tenant,
                        now,
                        audit_event["actor_id"],
                        audit_event["store_key"],
                        audit_event["domain"],
                        audit_event["trigger_source"],
                        audit_event["backend_used"],
                        audit_event["outcome"],
                        audit_event["summary"],
                        json.dumps(details, ensure_ascii=False),
                    ),
                )
        loaded = self.get_proposal(proposal_id, tenant_key=tenant)
        assert loaded is not None
        return loaded

    def find_active_identical_proposal(
        self,
        store_key: str,
        domain: str,
        proposed_hash: str,
        tenant_key: str | None = None,
    ) -> dict[str, Any] | None:
        tenant = _require_tenant_key(tenant_key)
        now_epoch = time.time()
        with connection_scope(self.db_path) as conn:
            row = conn.execute(
                """
                SELECT * FROM proposals
                WHERE tenant_key = ? AND store_key = ? AND domain = ? AND proposed_hash = ?
                  AND status IN ('PENDING', 'BLOCKED')
                  AND expires_epoch >= ?
                ORDER BY created_at DESC LIMIT 1
                """,
                (tenant, store_key, domain, proposed_hash, now_epoch),
            ).fetchone()
        return self._row_to_proposal(row) if row is not None else None

    def find_unresolved_execution(
        self, store_key: str, domain: str, tenant_key: str | None = None
    ) -> dict[str, Any] | None:
        tenant = _require_tenant_key(tenant_key)
        with connection_scope(self.db_path) as conn:
            row = conn.execute(
                "SELECT * FROM proposals WHERE tenant_key=? AND store_key=? AND domain=? "
                "AND status IN ('EXECUTING','UNCERTAIN') ORDER BY created_at DESC LIMIT 1",
                (tenant, store_key, domain),
            ).fetchone()
        return self._row_to_proposal(row) if row is not None else None

    def has_other_unresolved_execution(
        self,
        store_key: str,
        domain: str,
        proposal_id: str,
        tenant_key: str | None = None,
    ) -> bool:
        tenant = _require_tenant_key(tenant_key)
        with connection_scope(self.db_path) as conn:
            row = conn.execute(
                "SELECT 1 FROM proposals WHERE tenant_key=? AND store_key=? AND domain=? "
                "AND proposal_id<>? AND status IN ('EXECUTING','UNCERTAIN') LIMIT 1",
                (tenant, store_key, domain, proposal_id),
            ).fetchone()
        return row is not None

    def get_proposal_tenant(self, proposal_id: str) -> str | None:
        """Retorna apenas o tenant_key dono da proposta para validação de cruzamento de tenants."""
        with connection_scope(self.db_path) as conn:
            row = conn.execute(
                "SELECT tenant_key FROM proposals WHERE proposal_id = ?",
                (proposal_id,),
            ).fetchone()
        return str(row["tenant_key"]) if row is not None else None

    def get_proposal(
        self, proposal_id: str, tenant_key: str | None = None
    ) -> dict[str, Any] | None:
        tenant = _require_tenant_key(tenant_key)
        with connection_scope(self.db_path) as conn:
            row = conn.execute(
                "SELECT * FROM proposals WHERE proposal_id = ? AND tenant_key = ?",
                (proposal_id, tenant),
            ).fetchone()
        return self._row_to_proposal(row) if row is not None else None

    def list_proposals(
        self,
        tenant_key: str | None = None,
        *,
        status: str | None = None,
        store_key: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        tenant = _require_tenant_key(tenant_key)
        query = "SELECT * FROM proposals WHERE tenant_key = ?"
        params: list[Any] = [tenant]
        if status:
            query += " AND status = ?"
            params.append(status.upper())
        if store_key:
            query += " AND store_key = ?"
            params.append(store_key)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(max(1, min(limit, 500)))
        with connection_scope(self.db_path) as conn:
            rows = conn.execute(query, params).fetchall()
        return [self._row_to_proposal(r) for r in rows]

    def resolve_proposal(
        self,
        proposal_id: str,
        *,
        tenant_key: str | None = None,
        status: str,
        outcome: dict[str, Any],
        expected_status: str | tuple[str, ...] = "EXECUTING",
        audit_event: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        tenant = _require_tenant_key(tenant_key)
        now = _utc_now()
        allowed = (expected_status,) if isinstance(expected_status, str) else expected_status
        placeholders = ",".join("?" for _ in allowed)
        with connection_scope(self.db_path) as conn:
            result = conn.execute(
                f"""
                UPDATE proposals
                SET status = ?, resolved_at = ?, outcome_json = ?, execution_lease_until = 0
                WHERE proposal_id = ? AND tenant_key = ? AND status IN ({placeholders})
                """,
                (
                    status,
                    now,
                    json.dumps(outcome, ensure_ascii=False),
                    proposal_id,
                    tenant,
                    *allowed,
                ),
            )
            if result.rowcount != 1:
                return None
            if audit_event is not None:
                conn.execute(
                    "INSERT INTO audit_events (id,tenant_key,timestamp_utc,actor_id,store_key,domain,"
                    "trigger_source,backend_used,outcome,summary,details_json) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        str(uuid.uuid4()),
                        tenant,
                        now,
                        audit_event["actor_id"],
                        audit_event["store_key"],
                        audit_event["domain"],
                        audit_event["trigger_source"],
                        audit_event["backend_used"],
                        audit_event["outcome"],
                        audit_event["summary"],
                        json.dumps(audit_event.get("details") or {}, ensure_ascii=False),
                    ),
                )
        return self.get_proposal(proposal_id, tenant_key=tenant)

    def claim_proposal(
        self, proposal_id: str, tenant_key: str | None = None
    ) -> bool:
        tenant = _require_tenant_key(tenant_key)
        with connection_scope(self.db_path) as conn:
            result = conn.execute(
                "UPDATE proposals SET status = 'EXECUTING', execution_lease_until = ? "
                "WHERE proposal_id = ? AND tenant_key = ? "
                "AND status IN ('PENDING', 'BLOCKED') AND expires_epoch >= ?",
                (time.time() + 900, proposal_id, tenant, time.time()),
            )
            return result.rowcount == 1

    def acquire_sync_lock(
        self,
        store_key: str,
        domain: str,
        owner_id: str,
        lease_seconds: int = 900,
        *,
        tenant_key: str | None = None,
    ) -> bool:
        tenant = _require_tenant_key(tenant_key)
        key = f"{tenant}:{store_key}:{domain}"
        now = time.time()
        with connection_scope(self.db_path) as conn:
            result = conn.execute(
                "INSERT INTO sync_locks(lock_key, tenant_key, owner_id, expires_epoch) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(lock_key) DO UPDATE SET owner_id=excluded.owner_id, expires_epoch=excluded.expires_epoch "
                "WHERE (sync_locks.expires_epoch < ? AND NOT EXISTS ("
                "SELECT 1 FROM proposals WHERE proposals.proposal_id=sync_locks.owner_id "
                "AND proposals.tenant_key=? AND proposals.status='EXECUTING')) OR sync_locks.owner_id = excluded.owner_id",
                (key, tenant, owner_id, now + lease_seconds, now, tenant),
            )
            return result.rowcount == 1

    def release_sync_lock(
        self,
        store_key: str,
        domain: str,
        owner_id: str,
        *,
        tenant_key: str | None = None,
    ) -> None:
        tenant = _require_tenant_key(tenant_key)
        with connection_scope(self.db_path) as conn:
            conn.execute(
                "DELETE FROM sync_locks WHERE lock_key=? AND tenant_key=? AND owner_id=?",
                (f"{tenant}:{store_key}:{domain}", tenant, owner_id),
            )

    def update_poller_status(
        self,
        *,
        tenant_key: str | None = None,
        last_success_at: str | None,
        last_error: str | None,
        consecutive_failures: int,
        last_summary: str | None,
    ) -> None:
        tenant = _require_tenant_key(tenant_key)
        with connection_scope(self.db_path) as conn:
            conn.execute(
                """INSERT INTO sheets_poller_status
                   (tenant_key, last_success_at, last_error, consecutive_failures, last_summary)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(tenant_key) DO UPDATE SET
                     last_success_at=excluded.last_success_at,
                     last_error=excluded.last_error,
                     consecutive_failures=excluded.consecutive_failures,
                     last_summary=excluded.last_summary""",
                (tenant, last_success_at, last_error, max(0, consecutive_failures), last_summary),
            )

    def get_poller_status(self, tenant_key: str | None = None) -> dict[str, Any]:
        tenant = _require_tenant_key(tenant_key)
        with connection_scope(self.db_path) as conn:
            row = conn.execute(
                "SELECT * FROM sheets_poller_status WHERE tenant_key = ?",
                (tenant,),
            ).fetchone()
        if row is None:
            return {
                "tenant_key": tenant,
                "last_success_at": None,
                "last_error": None,
                "consecutive_failures": 0,
                "last_summary": None,
            }
        return dict(row)

    def update_portal_session_status(
        self,
        *,
        tenant_key: str | None = None,
        channel: str,
        state: str,
        last_error: str | None = None,
    ) -> dict[str, Any]:
        tenant = _require_tenant_key(tenant_key)
        channel_key = (channel or "").strip().upper()
        if not channel_key:
            raise ValueError("channel é obrigatório para status de sessão do portal.")
        now = _utc_now()
        with connection_scope(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO portal_session_status (tenant_key, channel, state, last_error, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(tenant_key, channel) DO UPDATE SET
                    state = excluded.state,
                    last_error = excluded.last_error,
                    updated_at = excluded.updated_at
                """,
                (tenant, channel_key, state, last_error, now),
            )
        return {
            "tenant_key": tenant,
            "channel": channel_key,
            "state": state,
            "last_error": last_error,
            "updated_at": now,
        }

    def list_portal_session_status(
        self, tenant_key: str | None = None
    ) -> list[dict[str, Any]]:
        tenant = _require_tenant_key(tenant_key)
        with connection_scope(self.db_path) as conn:
            rows = conn.execute(
                """
                SELECT tenant_key, channel, state, last_error, updated_at
                FROM portal_session_status
                WHERE tenant_key = ?
                ORDER BY channel ASC
                """,
                (tenant,),
            ).fetchall()
        return [dict(row) for row in rows]

    def claim_webhook_delivery(
        self,
        delivery_hash: str,
        lease_seconds: int = 300,
        *,
        tenant_key: str | None = None,
    ) -> bool:
        tenant = _require_tenant_key(tenant_key)
        now = time.time()
        with connection_scope(self.db_path) as conn:
            result = conn.execute(
                "INSERT INTO webhook_deliveries (delivery_hash, tenant_key, received_at, summary_json, lease_until_epoch) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT(delivery_hash) DO UPDATE SET "
                "received_at=excluded.received_at, summary_json=excluded.summary_json, "
                "lease_until_epoch=excluded.lease_until_epoch "
                "WHERE webhook_deliveries.tenant_key=? "
                "AND json_extract(webhook_deliveries.summary_json, '$.status')='PROCESSING' "
                "AND webhook_deliveries.lease_until_epoch < ?",
                (
                    delivery_hash,
                    tenant,
                    _utc_now(),
                    json.dumps({"status": "PROCESSING"}),
                    now + lease_seconds,
                    tenant,
                    now,
                ),
            )
            return result.rowcount == 1

    def get_webhook_delivery(
        self, delivery_hash: str, tenant_key: str | None = None
    ) -> dict[str, Any] | None:
        tenant = _require_tenant_key(tenant_key)
        with connection_scope(self.db_path) as conn:
            row = conn.execute(
                "SELECT * FROM webhook_deliveries WHERE delivery_hash = ? AND tenant_key = ?",
                (delivery_hash, tenant),
            ).fetchone()
        if row is None:
            return None
        return {
            "delivery_hash": row["delivery_hash"],
            "tenant_key": row["tenant_key"],
            "received_at": row["received_at"],
            "summary": json.loads(row["summary_json"]),
        }

    def record_webhook_delivery(
        self,
        delivery_hash: str,
        summary: dict[str, Any],
        *,
        tenant_key: str | None = None,
    ) -> None:
        tenant = _require_tenant_key(tenant_key)
        with connection_scope(self.db_path) as conn:
            conn.execute(
                """
                UPDATE webhook_deliveries
                SET summary_json = ?, lease_until_epoch = 0
                WHERE delivery_hash = ? AND tenant_key = ?
                """,
                (json.dumps(summary, ensure_ascii=False), delivery_hash, tenant),
            )

    def log_audit(
        self,
        *,
        tenant_key: str | None = None,
        actor_id: str,
        store_key: str,
        domain: str,
        trigger_source: str,
        backend_used: str,
        outcome: str,
        summary: str,
        details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        tenant = _require_tenant_key(tenant_key)
        event_id = str(uuid.uuid4())
        ts = _utc_now()
        details_dict = details or {}
        with connection_scope(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO audit_events (
                    id, tenant_key, timestamp_utc, actor_id, store_key, domain,
                    trigger_source, backend_used, outcome, summary, details_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    tenant,
                    ts,
                    actor_id,
                    store_key,
                    domain,
                    trigger_source,
                    backend_used,
                    outcome,
                    summary,
                    json.dumps(details_dict, ensure_ascii=False),
                ),
            )
        return {
            "id": event_id,
            "tenant_key": tenant,
            "timestamp_utc": ts,
            "actor_id": actor_id,
            "store_key": store_key,
            "domain": domain,
            "trigger_source": trigger_source,
            "backend_used": backend_used,
            "outcome": outcome,
            "summary": summary,
            "details": details_dict,
        }

    def list_audit_events(
        self,
        tenant_key: str | None = None,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        tenant = _require_tenant_key(tenant_key)
        safe_limit = max(1, min(limit, 500))
        safe_offset = max(0, offset)
        with connection_scope(self.db_path) as conn:
            rows = conn.execute(
                """
                SELECT * FROM audit_events
                WHERE tenant_key = ?
                ORDER BY timestamp_utc DESC
                LIMIT ? OFFSET ?
                """,
                (tenant, safe_limit, safe_offset),
            ).fetchall()
        return [
            {
                "id": row["id"],
                "tenant_key": row["tenant_key"],
                "timestamp_utc": row["timestamp_utc"],
                "actor_id": row["actor_id"],
                "store_key": row["store_key"],
                "domain": row["domain"],
                "trigger_source": row["trigger_source"],
                "backend_used": row["backend_used"],
                "outcome": row["outcome"],
                "summary": row["summary"],
                "details": json.loads(row["details_json"]),
            }
            for row in rows
        ]

    @staticmethod
    def _row_to_proposal(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "proposal_id": row["proposal_id"],
            "execution_id": row["proposal_id"],
            "tenant_key": row["tenant_key"],
            "store_key": row["store_key"],
            "merchant_id": row["merchant_id"],
            "channel": row["channel"],
            "domain": row["domain"],
            "actor_id": row["actor_id"],
            "trigger_source": row["trigger_source"],
            "execution_mode": row["execution_mode"],
            "backend_planned": row["backend_planned"],
            "status": row["status"],
            "current_hash": row["current_hash"],
            "proposed_hash": row["proposed_hash"],
            "current": json.loads(row["current_json"]),
            "proposed": json.loads(row["proposed_json"]),
            "diff": json.loads(row["diff_json"]),
            "guardrail_warnings": json.loads(row["guardrail_warnings_json"]),
            "metadata": json.loads(row["metadata_json"]),
            "created_at": row["created_at"],
            "expires_at": datetime.fromtimestamp(row["expires_epoch"], UTC).isoformat(),
            "expires_monotonic": row["expires_monotonic"],
            "resolved_at": row["resolved_at"],
            "outcome": json.loads(row["outcome_json"]) if row["outcome_json"] else None,
        }
