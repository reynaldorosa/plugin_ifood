# Relatório — Plugin iFood — FATIA 1: Isolamento por Padaria (`tenant_key`)

Data: 2026-11-21  
Projeto: `D:\PLUGINSHERMES\iffod`

## 1. Resumo das Alterações Realizadas

Esta entrega implementa **exclusivamente a FATIA 1 (isolamento por padaria / `tenant_key`)**, preservando `store_key` como a chave da loja dentro de cada padaria e mantendo inalteradas as políticas de escrita (`REVIEW` e bloqueios de capacidade).

1. **Modelos (`ifood/models.py`)**:
   - Introduzido `tenant_key: str = Field(default="default", min_length=1, max_length=100)` com validação de string não vazia em `SheetStoreConfig`, `SheetCatalogItem`, `StoreDesiredState`, `SnapshotRecord`, `ProposalRecord` e `ParsedSheetSnapshot`.
   - Mantido `store_key` intacto como identificador da loja dentro da padaria.
2. **Persistência e Migração SQLite (`ifood/storage.py`)**:
   - Adicionada coluna `tenant_key TEXT NOT NULL DEFAULT 'default'` em `proposals`, `sync_snapshots` (com chave primária composta `(tenant_key, store_key, domain)`), `audit_events` e tabelas auxiliares (`webhook_deliveries`, `sync_locks`, `sheets_poller_status`).
   - Criados índices por `(tenant_key, status)` (`idx_proposals_tenant_status`) e por `(tenant_key, store_key, domain)` (`idx_proposals_tenant_store_domain`, `idx_snapshots_tenant_store_domain`, `idx_audit_tenant_store_domain`).
   - Implementada migração aditiva e idempotente (`_migrate_snapshots_table_if_needed` e `_ensure_tenant_column`) que atualiza bancos existentes sem perda de linhas, preenchendo `tenant_key = 'default'`.
   - Todas as consultas de leitura e escrita em `SyncStorage` agora exigem `tenant_key` explícito via `_require_tenant_key(tenant_key)` e filtram por ele; chamadas sem `tenant_key` (ou com valor vazio/`None`) levantam `ValueError` explícito.
3. **Configuração Multi-Padaria (`ifood/config.py`)**:
   - Suporte a configuração multi-padaria via arquivo JSON (`IFOOD_LOJA_TENANTS_FILE`) ou variável de ambiente (`IFOOD_LOJA_TENANTS_JSON`) no formato `{"padaria-a": {"sheet_id": "...", "stores": {...}}, "padaria-b": {...}}`.
   - Rejeição explícita (`ValueError`) caso qualquer chave de segredo (`secret`, `token`, `password`, `private_key`, `client_secret`, `api_key`, `webhook_secret`, `cookie`, `authorization`) esteja presente no JSON de tenants.
   - Quando ausente, preserva o comportamento padrão com o tenant `"default"`.
4. **Motor de Sincronização (`ifood/sync_engine.py`, `ifood/sheets_parser.py`, `ifood/poller.py`, `ifood/hermes_tools.py`)**:
   - `process_snapshot`, `confirm_proposal`, `reject_proposal` e `reconcile_uncertain` recebem/derivam `tenant_key` e o repassam a todas as chamadas do `SyncStorage`.
   - Se o `tenant_key` da proposta divergir do `tenant_key` solicitado em `confirm_proposal`, `reject_proposal` ou `reconcile_uncertain`, é levantado `TenantMismatchError` sem alterar nenhuma linha no banco.

---

## 2. Arquivos Modificados (Caminhos Absolutos)

- `D:\PLUGINSHERMES\iffod\ifood\models.py`
- `D:\PLUGINSHERMES\iffod\ifood\config.py`
- `D:\PLUGINSHERMES\iffod\ifood\storage.py`
- `D:\PLUGINSHERMES\iffod\ifood\sync_engine.py`
- `D:\PLUGINSHERMES\iffod\ifood\sheets_parser.py`
- `D:\PLUGINSHERMES\iffod\ifood\poller.py`
- `D:\PLUGINSHERMES\iffod\ifood\hermes_tools.py`
- `D:\PLUGINSHERMES\iffod\tests\test_storage_claims.py`
- `D:\PLUGINSHERMES\iffod\tests\test_sheets_sync.py`

### Arquivos Proibidos nesta Fatia (Confirmados Intocados)
- `D:\PLUGINSHERMES\iffod\ifood\playwright_executor.py` — não modificado
- `D:\PLUGINSHERMES\iffod\ifood\client.py` — não modificado
- `D:\PLUGINSHERMES\iffod\dashboard\plugin_api.py` — não modificado
- `D:\PLUGINSHERMES\iffod\frontend\*` — não modificado

---

## 3. Saída Real dos Comandos de Verificação

### `py -3.12 -m ruff check .`

```text
All checks passed!
```

### `py -3.12 -m pytest -q`

```text
...................................                                      [100%]
35 passed in 6.96s
```

### Novos Testes Obrigatórios Executados (`tests/test_storage_claims.py`)
1. `test_cross_tenant_proposal_confirmation_raises_and_leaves_db_untouched`: cria proposta em `padaria-a` e tenta confirmar com `padaria-b` → levanta `TenantMismatchError`, zero escritas e banco inalterado.
2. `test_list_proposals_isolates_tenants_and_requires_tenant_key`: `list_proposals("padaria-a")` retorna apenas propostas de `padaria-a` (nada de `padaria-b`), e chamar `list_proposals()` sem `tenant_key` levanta `ValueError`.
3. `test_legacy_database_migration_preserves_rows_and_sets_default_tenant`: banco SQLite legado criado apenas com `store_key` (sem coluna `tenant_key`) preserva todas as linhas de `sync_snapshots`, `proposals` e `audit_events` com `tenant_key='default'` e cria os índices `idx_proposals_tenant_status` e `idx_proposals_tenant_store_domain`.
4. `test_multi_bakery_tenants_config_loaded_from_json_file`: carrega `IFOOD_LOJA_TENANTS_FILE` com `padaria-a` e `padaria-b`, valida os tenants produzidos e rejeita arquivo JSON contendo segredos.

---

## 4. O Que Ficou Fora de Escopo (Conforme Restrições da Fatia 1)

- Rotas HTTP e payloads do dashboard (`dashboard/plugin_api.py`) e interface web (`frontend/`).
- Cliente HTTP oficial iFood (`ifood/client.py`) e executor Playwright (`ifood/playwright_executor.py`).
- Alterações na política de escrita (`ifood/write_policy.py` — modo `REVIEW` e bloqueios de capacidade permanecem inalterados).
- Integrações de portal, Anota AI / 99Food ou escrita direta em catálogo.
