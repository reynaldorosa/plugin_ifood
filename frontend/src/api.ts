import { fetchJSON } from './hermesReact';

export const API_BASE = '/api/plugins/ifood-loja';

export interface SyncReadiness {
  enabled: boolean;
  sheets_api_configured: boolean;
  sheets_spreadsheet_id: string;
  webhook_configured: boolean;
  sheets_poller_enabled: boolean;
  ifood_api_configured: boolean;
  playwright_configured: boolean;
  default_sync_mode: string;
  default_execution_mode: string;
  max_price_change_pct: number;
  write_capabilities: string[];
  validated_portal_domains: string[];
  write_capabilities_configured: boolean;
  automation_policy: string;
  sheets_poller_runtime: { state: string; detail: string | null };
}

export interface StoreSnapshotRecord {
  store_key: string;
  domain: string;
  last_applied_hash: string;
  last_state: unknown;
  last_backend: string;
  last_outcome: string;
  updated_at: string;
}

export interface SyncProposalRecord {
  proposal_id: string;
  store_key: string;
  merchant_id: string;
  channel: string;
  domain: string;
  actor_id: string;
  trigger_source: string;
  execution_mode: string;
  backend_planned: string;
  status: string;
  current: unknown;
  proposed: unknown;
  diff: Record<string, unknown>;
  guardrail_warnings: string[];
  created_at: string;
  expires_at: string;
  execution_id: string;
  metadata: Record<string, unknown>;
  resolved_at: string | null;
  outcome: Record<string, unknown> | null;
}

export interface AuditEventItem {
  id: string;
  timestamp_utc: string;
  actor_id: string;
  store_key: string;
  domain: string;
  trigger_source: string;
  backend_used: string;
  outcome: string;
  summary: string;
}

export interface SyncOverviewResponse {
  tenant_key: string;
  portal_sessions: Record<string, Record<string, {state: string; last_error: string | null; updated_at: string | null}>>;
  readiness: SyncReadiness;
  snapshots: StoreSnapshotRecord[];
  proposals: SyncProposalRecord[];
  audit_events: AuditEventItem[];
  sheets_poller: {
    last_success_at: string | null;
    last_error: string | null;
    consecutive_failures: number;
    last_summary: string | null;
  };
}

export interface LegacyHoursResponse {
  merchant_id: string;
  opening_hours: unknown;
}

export interface LegacyProposalResponse {
  proposal_id: string;
  merchant_id: string;
  current: unknown;
  proposed: unknown;
  replaces_entire_schedule: boolean;
}

export const fetchTenants = () => fetchJSON<Array<{tenant_key: string; sheet_configured: boolean}>>(`${API_BASE}/tenants`);
export const fetchSyncOverview = (tenant_key: string) =>
  fetchJSON<SyncOverviewResponse>(`${API_BASE}/sync/overview?tenant_key=${encodeURIComponent(tenant_key)}`);

export const triggerSheetSync = (payload: {
  stores_rows?: unknown[];
  hours_rows?: unknown[];
  catalog_rows?: unknown[];
  dry_run?: boolean;
  tenant_key: string;
}) =>
  fetchJSON<{
    snapshot_hash: string;
    trigger_source: string;
    row_errors: Array<Record<string, unknown>>;
    results: Array<Record<string, unknown>>;
    write_back_warning?: string | null;
    summary: string;
  }>(`${API_BASE}/sheets/sync`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });

export const confirmSyncProposal = (proposal_id: string, tenant_key: string, override_guardrail = false) =>
  fetchJSON<{ proposal: SyncProposalRecord; execution: Record<string, unknown> }>(
    `${API_BASE}/sync/proposals/${proposal_id}/confirm`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ override_guardrail, tenant_key }),
    },
  );

export const rejectSyncProposal = (proposal_id: string, reason: string, tenant_key: string) =>
  fetchJSON<SyncProposalRecord>(`${API_BASE}/sync/proposals/${proposal_id}/reject`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ reason, tenant_key }),
  });

export const reconcileSyncProposal = (proposal_id: string, tenant_key: string) =>
  fetchJSON<{ proposal: SyncProposalRecord; outcome: string }>(
    `${API_BASE}/sync/proposals/${proposal_id}/reconcile?tenant_key=${encodeURIComponent(tenant_key)}`,
    { method: 'POST' },
  );

export const fetchAppsScript = () =>
  fetchJSON<{ filename: string; content: string }>(`${API_BASE}/sheets/apps-script`);

export const fetchLegacyOpeningHours = () =>
  fetchJSON<LegacyHoursResponse>(`${API_BASE}/opening-hours`);

export const proposeLegacyOpeningHours = (shifts: unknown[]) =>
  fetchJSON<LegacyProposalResponse>(`${API_BASE}/opening-hours/proposals`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ shifts }),
  });

export const confirmLegacyOpeningHours = (proposal_id: string) =>
  fetchJSON<{ status: string }>(`${API_BASE}/opening-hours/confirm`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ proposal_id }),
  });
