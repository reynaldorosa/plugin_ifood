# Relatório — Plugin Padarias — FATIA 2: Playwright lê, propõe e executa com conferência

Data: 2026-11-21  
Projeto: `D:\PLUGINSHERMES\iffod`

## 1. Resumo das Alterações Realizadas

Esta entrega implementa **exclusivamente a FATIA 2**: torna o caminho de navegador (`PLAYWRIGHT`) funcional ponta a ponta (leitura de estado por portal, geração de proposta revisável `PENDING`, execução unitária com conferência imediata por releitura/read-back e exposição do estado de sessão por padaria × canal no `overview`), sem tocar em conta/portal real e sem inventar seletores reais.

1. **Mapa de seletores por canal (`ifood/portal_selectors.py`)**:
   - Criado módulo versionado `ifood/portal_selectors.py` com `PortalChannelSelectors`.
   - Mantém os seletores `data-hermes-*` como default do servidor mock HTTP local de testes (`MOCK_HERMES_SELECTORS`).
   - Os seletores de portais reais (`REAL_PORTAL_SELECTORS_UNVALIDATED` para `IFOOD`, `99FOOD`, `ANOTA_AI`) permanecem explicitamente vazios e documentados como `validated_real_portal=False`.

2. **Leitura de estado separada da escrita (`ifood/playwright_executor.py`)**:
   - Implementado `PlaywrightStoreExecutor.read_domain_state(store=..., domain=..., desired_payload=...)` para `CATALOG`, `PREP_TIME`, `OPENING_HOURS` e `STORE_STATUS` no mesmo formato canônico usado pelo backend de API.
   - Abre a página com a sessão configurada (`playwright_cdp_url` loopback ou `playwright_user_data_dir` persistente / contexto dedicado).
   - Passa por `detect_portal_gate()` antes de qualquer extração (`LOGIN_REQUIRED`, `CAPTCHA_OR_BOT_BLOCK`).
   - Confirma inequivocamente a identidade da loja na página (`merchant_id` e `store_name` quando presente) via `_verify_page_store_identity`; divergência retorna erro explícito (`observed=False`), nunca "melhor esforço".
   - Devolve `(state, observed=True)` somente quando todos os campos exigidos pelo domínio e itens solicitados estão presentes e válidos; campo ausente, item não encontrado ou página parcial devolve `observed=False` com motivo explícito.

3. **Proposta nasce no caminho navegador (`ifood/sync_engine.py`)**:
   - `_observe_current()` agora chama `executor.read_domain_state(...)` quando `backend == BackendUsed.PLAYWRIGHT`, respeitando `tenant_key`.
   - Quando `observed=True`, segue o fluxo normal: cálculo de diff, avaliação de guardrails e criação de proposta `PENDING` (ou `BLOCKED` se houver alerta de guardrail) com auditoria `PENDING_REVIEW`.
   - Quando `observed=False`, registra o motivo real da falha (`reason`) e o código específico (`LOGIN_REQUIRED`, `CAPTCHA_OR_BOT_BLOCK`, `UNSUPPORTED`, `ERROR` ou `BLOCKED_GUARDRAIL`) no resumo e na auditoria, sem criar proposta.

4. **Escrita com conferência por releitura (`ifood/playwright_executor.py`, `ifood/sync_engine.py`, `ifood/config.py`)**:
   - Exige `domain.value in config.validated_portal_domains`, capability explícita em `config.write_capabilities`, permissão `approved_execution` vinculada ao `merchant_id` e proposta, além de estado observado antes de iniciar a escrita.
   - Executa **uma** alteração de item por vez no catálogo (bloqueia se a proposta contiver mais de um item alterado).
   - Faz reload e releitura imediata (`_extract_domain_state_from_page`) comparando com o proposto:
     - Bateu ⇒ `CONFIRMED` (proposta marcada como `APPLIED` e snapshot atualizado);
     - Não bateu ⇒ `UNCERTAIN` (proposta marcada como `UNCERTAIN`, travando novas tentativas para aquela loja/domínio até reconciliação read-only);
     - Erro de rede/timeout após início da escrita ⇒ `UNCERTAIN`.

5. **Estado de sessão visível no `overview` (`ifood/storage.py`, `ifood/sync_engine.py`, `ifood/hermes_tools.py`, `dashboard/plugin_api.py`)**:
   - Persistência em `portal_session_status` (`tenant_key`, `channel`, `state`, `last_error`, `updated_at`) e agregador `build_portal_session_overview(config, storage)` expondo por `padaria × canal`: `OK` / `LOGIN_REQUIRED` / `EXPIRED` / `NÃO CONFIGURADO` com `last_error`.

---

## 2. Arquivos Criados / Modificados (Caminhos Absolutos)

- `D:\PLUGINSHERMES\iffod\ifood\portal_selectors.py` *(novo)*
- `D:\PLUGINSHERMES\iffod\ifood\playwright_executor.py`
- `D:\PLUGINSHERMES\iffod\ifood\sync_engine.py`
- `D:\PLUGINSHERMES\iffod\ifood\storage.py`
- `D:\PLUGINSHERMES\iffod\ifood\config.py`
- `D:\PLUGINSHERMES\iffod\ifood\hermes_tools.py`
- `D:\PLUGINSHERMES\iffod\dashboard\plugin_api.py` *(apenas exposição de `portal_sessions` e `tenant_key` em `/sync/overview`)*
- `D:\PLUGINSHERMES\iffod\tests\test_playwright_executor.py`

*(O arquivo `D:\PLUGINSHERMES\iffod\ifood\client.py` e os arquivos de `D:\PLUGINSHERMES\iffod\frontend\*` permaneceram intocados).*

---

## 3. Saída Real dos Comandos de Verificação

### `py -3.12 -m ruff check .`

```text
All checks passed!
```

### `py -3.12 -m pytest -q`

```text
..........................................                               [100%]
42 passed in 29.75s
```

### Novos Testes Obrigatórios Executados (`tests/test_playwright_executor.py`)
1. `test_playwright_read_generates_pending_proposal_with_current_and_audit` — **leitura gera proposta**: mock HTTP local com cardápio, backend `PLAYWRIGHT` ⇒ proposta `PENDING` criada, `current` preenchido com os dados lidos do DOM, auditoria `PENDING_REVIEW` e sessão `OK`.
2. `test_playwright_portal_with_login_returns_login_required_without_proposal` — **portal com login**: mock servindo formulário de login ⇒ `LOGIN_REQUIRED`, nenhuma proposta criada, nada gravado e sessão marcada como `LOGIN_REQUIRED` com `last_error`.
3. `test_playwright_page_missing_field_blocks_guardrail_with_explicit_reason` — **página sem o campo**: mock sem o input `price_brl` ⇒ `observed=False`, resultado `BLOCKED_GUARDRAIL` com motivo explícito indicando o campo e o `item_id` ausente, sem proposta criada.
4. `test_playwright_divergent_store_identity_returns_explicit_error_without_proposal` — **identidade divergente**: mock exibindo `merchant_id` de outra loja ⇒ erro explícito de divergência de identidade, sem proposta criada.
5. `test_playwright_write_confirmed_updates_mock_and_verifies_readback` — **escrita confirmada**: proposta aprovada + mock que grava ⇒ `CONFIRMED`, releitura idêntica ao proposto (`price_cents == 3490`) e proposta resolvida como `APPLIED`.
6. `test_playwright_write_divergent_marks_uncertain_and_locks_further_attempts` — **escrita divergente**: mock que ignora a gravação ⇒ releitura detecta divergência, retorna `UNCERTAIN`, proposta fica em `UNCERTAIN` e nova sincronização na mesma loja/domínio fica travada até reconciliação.
7. `test_playwright_proposal_isolated_across_tenants_and_overview_exposes_sessions` — **isolamento e overview**: proposta criada em `padaria-a` não pode ser confirmada por `padaria-b` (`TenantMismatchError`, banco e portal inalterados) e `sync_overview` expõe `portal_sessions` por `padaria × canal` (`OK` / `NÃO CONFIGURADO`).

---

## 4. Lista Explícita do que Segue NÃO VALIDADO

Conforme as restrições de segurança e escopo desta fatia, os itens abaixo **não foram validados contra ambientes externos** e permanecem bloqueados por configuração padrão:

1. **Seletores reais do Portal do Parceiro iFood (`REAL_PORTAL_SELECTORS_UNVALIDATED["IFOOD"]`)**: não mapeados e não validados (`validated_real_portal=False`).
2. **Seletores reais do portal 99Food (`REAL_PORTAL_SELECTORS_UNVALIDATED["99FOOD"]`)**: não mapeados e não validados.
3. **Seletores reais do portal Anota AI (`REAL_PORTAL_SELECTORS_UNVALIDATED["ANOTA_AI"]`)**: não mapeados e não validados.
4. **Portal real e sessão real autenticada de operador**: toda a validação desta fatia foi executada exclusivamente contra servidor mock HTTP local (`127.0.0.1`) com Chromium headless; nenhuma conta, credencial ou portal real foi acessado.
5. **Janela de exclusão em 30 minutos e notificações via Telegram**: fora de escopo desta fatia (reservados para a fatia seguinte).
