# Plugin Hermes — gestão de loja iFood / 99Food

> **Estado verificado:** o plugin e os fluxos sintéticos locais foram validados. Nenhuma loja real foi conectada. A integração com Bitwarden e a automação em portais reais não estão implementadas/validadas.

## Em uma frase

- **Com API oficial autorizada:** o plugin lê a loja pelo iFood e faz read-back das capacidades implementadas. `REVIEW` é o padrão; `AUTO_APPLY` é opcional e exige configuração da loja e da capacidade exata.
- **Sem API:** o caminho planejado é usar Playwright em uma sessão local do Portal do Parceiro, mas **o fallback atual ainda não está pronto para alterar uma loja real**. Até ser completado e testado, use o portal manualmente; não confirme propostas Playwright com estado atual `null`.

## Como funcionaria com a API oficial

1. O aplicativo é cadastrado no Portal do Desenvolvedor do iFood e recebe os módulos necessários. O portal fornece um aplicativo de teste; a documentação orienta criar o aplicativo de produção depois da homologação.[1]
2. A loja precisa autorizar o aplicativo e aparecer entre os merchants autorizados para as credenciais utilizadas. O plugin não deve inferir autorização pelo nome, CNPJ ou URL.
3. Para o fluxo centralizado implementado no código, o backend usa `client_id` e `client_secret` para pedir um token OAuth `client_credentials`; o token fica em memória e é enviado como Bearer.[2] O cliente verifica se o `merchant_id` está na lista autorizada antes de chamar as operações.
4. O operador escolhe a loja e a informação a alterar. O plugin lê o estado atual, apresenta o antes/depois e pede aprovação para aquele valor.
5. Só após a aprovação envia uma escrita e lê o mesmo recurso novamente. Para horários, o `PUT` substitui a grade completa: é necessário preservar e mostrar todos os turnos, não apenas o dia editado.[3]
6. O resultado é registrado como confirmado, divergente ou incerto. Leituras `GET` tratam `429`/`5xx` com backoff e `Retry-After`; escritas não são repetidas automaticamente. Timeout após envio exige reconciliação antes de qualquer nova tentativa.[4]

O código atual lê credenciais do **ambiente do processo Hermes**, pelos nomes abaixo. Ele **não consulta o Bitwarden diretamente** e não existe integração Bitwarden→plugin no repositório.

## Onde guardar as credenciais no Bitwarden

Criar — quando o app iFood estiver disponível — um item separado por ambiente no cofre/coleção privada da empresa escolhida pelo dono:

- `iFood Developer API — Hermes Loja — HOMOLOGAÇÃO`
- `iFood Developer API — Hermes Loja — PRODUÇÃO` (somente depois de homologar e autorizar produção)

Tipo sugerido: **Secure Note**. Guardar campos identificados, sem colocar os valores neste README:

- nome/ID do aplicativo e ambiente;
- `client_id`;
- `client_secret` como campo sensível;
- `merchant_id` autorizado e nome/alias da loja;
- módulos/escopos aprovados, responsável e data de autorização.

**Não guarde token Bearer temporário como credencial permanente.** O fluxo OAuth do plugin gera/cacheia o token em memória. Não cole segredo no chat, em código, em planilha, neste README ou em `.env` dentro do projeto.

**Passagem para o runtime:** Bitwarden será o cofre de referência; o Dashboard precisa receber os valores por um mecanismo seguro do perfil Hermes/runtime. Hoje o plugin só lê variáveis de ambiente. Até existir e ser verificada uma ponte segura, não afirmar que o plugin “busca no Bitwarden” nem copiar o segredo para o repositório. O `.env` do perfil Hermes pode ser considerado apenas fora do projeto e depois de confirmar que o processo do Dashboard realmente o carrega.

Variáveis esperadas pelo código:

```text
IFOOD_LOJA_MERCHANT_ID
IFOOD_LOJA_CLIENT_ID
IFOOD_LOJA_CLIENT_SECRET
# alternativa temporária, se necessário:
IFOOD_LOJA_ACCESS_TOKEN
```

O token estático `IFOOD_LOJA_ACCESS_TOKEN` é uma alternativa técnica do código, não a opção preferida para uso contínuo. Homologação e produção devem ter itens e credenciais separados.

## Se não houver API: como será o Playwright

### Fluxo desejado (ainda não liberado para escrita real)

1. O operador autentica manualmente uma sessão **local e dedicada** do Portal do Parceiro; senha, 2FA e CAPTCHA nunca são entregues ao plugin nem ao Hermes.
2. Playwright se conecta somente ao navegador local configurado por CDP em `127.0.0.1`/`localhost` ou a um `user_data_dir` dedicado. Não reutilizar o perfil pessoal do navegador nem copiar cookies para arquivos.
3. Primeiro faz uma leitura sem escrita e confirma a identidade da loja e o valor atual.
4. Mostra a proposta completa e aguarda aprovação explícita.
5. Depois da aprovação, executa uma única alteração, relê o campo no portal e registra o resultado.
6. Login/2FA pendente, CAPTCHA, WAF, HTTP 403 ou 429 interrompem o fluxo. Não há stealth, proxy, solver nem contorno de proteção.

### Limitação real do código atual

O fluxo de leitura, proposta, execução e releitura foi implementado e testado em portal local simulado. Para uma loja real, ainda faltam os seletores e a validação autorizada do portal:

- O executor usa seletores de teste como `data-hermes-field` e `data-hermes-action`. Os testes usam um **portal HTTP local simulado**, não o Portal do Parceiro iFood nem o 99Food.
- O canal `99FOOD` não tem cliente de API implementado neste plugin; depende do caminho Playwright ainda não validado em portal real.
- Playwright continua bloqueado sem domínio validado, identidade e estado atual verificado.
- A configuração aceita CDP local ou `user_data_dir`, mas isso não comprova que uma sessão real funciona. É necessário mapear os seletores reais e validar o read-back antes de habilitar escrita.

Sem API e sem adaptador real validado, faça a alteração manualmente no Portal do Parceiro. Uma proposta com `Estado Atual` nulo não representa um diff verificado e não pode ser aprovada para escrita.

Variáveis que o código reconhece para a futura integração do navegador:

```text
IFOOD_LOJA_PLAYWRIGHT_CDP_URL       # somente endpoint local de depuração
IFOOD_LOJA_PLAYWRIGHT_USER_DATA_DIR # perfil local dedicado e previamente autenticado
IFOOD_LOJA_PLAYWRIGHT_HEADLESS       # o código usa true por padrão
IFOOD_LOJA_ALLOWED_PORTAL_DOMAINS   # allowlist; padrão definido em ifood/config.py
```

O plugin não herda automaticamente cookies da janela normal do Chrome. A autenticação inicial precisa ocorrer manualmente no perfil dedicado ou na sessão CDP que será reutilizada. Se o portal redirecionar para login, o código retorna `LOGIN_REQUIRED`; o fluxo atual não mantém uma tela interativa de login dentro do plugin.

## Política de escrita e capacidades bloqueadas

- `REVIEW` continua sendo o padrão. `IFOOD_LOJA_DEFAULT_SYNC_MODE=AUTO_APPLY` permite aplicação direta somente quando a loja/canal/capacidade também constam no mapa de tenants; a planilha pode reduzir uma loja para `REVIEW`.
- `IFOOD_LOJA_VERIFIED_WRITE_CAPABILITIES` limita as capacidades globais. Cada loja precisa declarar `channel`, `merchant_id` e `write_capabilities` em `IFOOD_LOJA_TENANTS_JSON`/`FILE` para aplicação automática e para escrita 99Food. O valor da planilha não pode ampliar essa permissão.
- Escritas de catálogo continuam bloqueadas no motor: a API atual usa batches assíncronos e pode variar por contexto de catálogo. O código não acompanha o batch nem prova o read-back de cada contexto.
- Playwright só é selecionado para domínios em `IFOOD_LOJA_VALIDATED_PORTAL_DOMAINS`. iFood e 99Food usam seletor próprio por canal via `IFOOD_LOJA_PORTAL_SELECTORS_FILE`; sem mapa real com `validated_real_portal=true`, a leitura/escrita é bloqueada. Seletores `data-hermes-*` só são usados nos testes HTTP locais.
- `OPEN` e substituição da grade completa exigem confirmação reforçada. Toda proposta exige leitura ao vivo; falha de leitura bloqueia.
- O poller segue opt-in (`IFOOD_LOJA_SHEETS_POLL_SECONDS=0`) e o painel mostra seu estado no runtime do plugin, quando observado.
- Em configuração multi-padaria, o poller percorre cada `sheet_id` configurado. As rotas HTTP exigem `tenant_key` quando há mais de uma padaria; o webhook o deriva do `spreadsheet_id` assinado.
- Um item ausente da planilha entra em carência de 30 minutos. Se voltar antes do prazo, a carência é cancelada. Após o prazo, o motor propõe sua indisponibilidade (`available=false`), sujeita à aprovação explícita e às capacidades de escrita validadas. Ele não apaga o item do portal.
- Mudar `available` na linha existente não exige aprovação reforçada. AUTO_APPLY mantém limite de preço, releitura antes/depois, lock de execução e `IFOOD_LOJA_MAX_AUTO_WRITES_PER_MINUTE` (padrão 10 unidades por loja em 60 segundos). Lotes Playwright usam um contexto sob demanda e conferem cada item após a escrita; falhas parciais deixam o lote `UNCERTAIN` para reconciliação.
- Avisos de proposta, sessão expirada/login, execução incerta e início da carência são registrados por padaria. Para enviar ao Telegram, configure `telegram_chat_id` no tenant (ou `IFOOD_LOJA_TELEGRAM_CHAT_ID` global) e `IFOOD_LOJA_TELEGRAM_BOT_TOKEN` no ambiente. Entregas falhas são tentadas novamente quando o mesmo evento reaparece.

Documentação oficial consultada para o contrato geral: [Merchant best practices](https://developer.ifood.com.br/en-US/docs/food/guides/modules/merchant/best-practices-troubleshooting), [Catalog endpoints](https://developer.ifood.com.br/en-US/docs/food/guides/modules/catalog/endpoints), [Catalog workflow e batches](https://developer.ifood.com.br/en-US/docs/food/guides/modules/catalog/workflow). Isso confirma rotas e semântica documentadas, mas não módulos contratados, escopos ou autorização de uma conta piloto.

| Capacidade | Leitura no código | Escrita no código | Situação |
|---|---|---|---|
| Horários | `GET /merchant/v1.0/merchants/{merchantId}/opening-hours` | `PUT` da grade integral | Exige sete dias, leitura antes/depois e `opening_hours`; allowlist vazia por padrão. |
| Pausa | `GET /merchant/v1.0/merchants/{merchantId}/interruptions` | `POST .../interruptions` | Só permite pausa sem interrupção existente, com read-back da própria pausa; exige `store_pause`. |
| Reabertura | Requer leitura de status e interrupções; a leitura atual não comprova o estado completo | `DELETE .../interruptions/{id}` | Bloqueada até confirmar estado `reopenable` e read-back do status para a conta piloto. |
| Catálogo | `GET /catalog/v2.0/merchants/{merchantId}/catalogs` e categorias | `PATCH .../items/price` e `PATCH .../items/status` | Escrita bloqueada: o código ainda não acompanha batches assíncronos nem aprova contexto por catálogo. |
| 99Food | Playwright com mapa de seletores por canal | Playwright com read-back | Caminho testado apenas no portal local simulado; sem seletores reais homologados retorna `UNSUPPORTED`. |
| Tempo de preparo | Sem GET/PUT API implementado | Nenhuma API | Playwright sem leitura atual validada; fica bloqueado. |

`IFOOD_LOJA_VERIFIED_WRITE_CAPABILITIES` não comprova sozinho permissões. O dono deve liberar uma capacidade somente depois de confirmar o contrato oficial, o módulo e os escopos da conta própria de homologação. Escrita via API de catálogo, fallback automático API→Playwright e portais reais ainda não estão validados.

Para o primeiro piloto, manter `REVIEW`, uma loja e uma operação escolhida. O código atual já cobre mais domínios e múltiplas lojas; isso não significa que esse escopo esteja aprovado ou validado.

## Configuração reconhecida

A configuração é lida de variáveis de ambiente por `ifood/config.py`. O plugin começa desativado por padrão; não habilite em perfil remoto ou de produção só para testar.

| Finalidade | Variáveis |
|---|---|
| Habilitação e operador | `IFOOD_LOJA_ENABLED`, `IFOOD_LOJA_OPERATORS`, `IFOOD_LOJA_AGENT_OPERATOR_ID` |
| API iFood | `IFOOD_LOJA_MERCHANT_ID`, `IFOOD_LOJA_CLIENT_ID`, `IFOOD_LOJA_CLIENT_SECRET` ou `IFOOD_LOJA_ACCESS_TOKEN` |
| Google Sheets | `IFOOD_LOJA_SHEETS_ID`, `IFOOD_LOJA_SHEETS_WEBHOOK_SECRET`, `IFOOD_LOJA_GOOGLE_SERVICE_ACCOUNT_FILE` (ou JSON/token) |
| Poller | `IFOOD_LOJA_SHEETS_POLL_SECONDS` — `0` mantém desativado |
| Playwright | `IFOOD_LOJA_PLAYWRIGHT_CDP_URL`, `IFOOD_LOJA_PLAYWRIGHT_USER_DATA_DIR`, `IFOOD_LOJA_PLAYWRIGHT_HEADLESS` |
| Allowlist escrita após validação | `IFOOD_LOJA_VERIFIED_WRITE_CAPABILITIES`, `IFOOD_LOJA_VALIDATED_PORTAL_DOMAINS` |
| Guardrail de preço | `IFOOD_LOJA_MAX_PRICE_CHANGE_PCT` |
| Aplicação direta | `IFOOD_LOJA_DEFAULT_SYNC_MODE=AUTO_APPLY`, `IFOOD_LOJA_MAX_AUTO_WRITES_PER_MINUTE` |
| Seletores dos portais | `IFOOD_LOJA_PORTAL_SELECTORS_FILE` (JSON `version: 1`, por canal) |
| Multi-padaria | `IFOOD_LOJA_TENANTS_JSON` ou `IFOOD_LOJA_TENANTS_FILE` com `sheet_id` e `telegram_chat_id` por padaria |
| Telegram | `IFOOD_LOJA_TELEGRAM_BOT_TOKEN` e, opcionalmente, `IFOOD_LOJA_TELEGRAM_CHAT_ID` global |

Exemplo de permissão por loja no JSON de tenants (sem credenciais):

```json
{"padaria-a":{"sheet_id":"SHEET_ID","stores":{"loja-centro":{"merchant_id":"MERCHANT_ID","channel":"IFOOD","write_capabilities":["catalog_availability"]}}}}
```

O mapa de seletores começa em [`portal_selectors.example.json`](portal_selectors.example.json) com `validated_real_portal=false`. Preencha o mapa correspondente ao canal somente após inspecionar o portal próprio; o arquivo é lido a cada execução, sem recompilar o plugin.

**Não coloque os valores secretos na tabela, no README ou no Git.** Para conferir se uma configuração foi aplicada, leia o estado do processo Dashboard sem imprimir os segredos.

Este plugin não integra Bitwarden diretamente. Prefira o gerenciador de segredos do runtime; se indisponível, use um processo lançador que injete variáveis em ambiente restrito. Um arquivo de perfil protegido persiste o segredo em disco e exige ACL e rotação. Separe credenciais de homologação e produção. Não registre variáveis, tokens, payloads de autorização ou exceções HTTP completas.

## O que foi validado e o que não foi

O código pode ser validado localmente com dados sintéticos. Nenhuma autorização de merchant ou escrita real foi executada nesta implementação.

Validado nesta execução:

- `py -3.12 -m pytest -q`: 56 testes passaram.
- `python -m ruff check .` passou.
- `npm run build` passou, incluindo a verificação TypeScript, e gerou `dashboard/dist/index.js`.

Os testes incluem o portal local simulado, isolamento por padaria, webhook assinado, poller multi-padaria, carência de catálogo e falha/reenvio do Telegram simulado. O teste HTTP local confirma que uma chamada anônima recebe `401`; os testes de identidade usam principals sintéticos. A validação com o middleware real depende do Hermes instalado e não foi comprovada nesta execução. API real, permissões de merchant, portal/seletores reais, Bitwarden em runtime, entrega ao Telegram real e qualquer escrita real permanecem não validados.

Limites atuais da Fatia 4: o catálogo exige que todos os itens desejados sejam observáveis na leitura inicial. Se um item já estiver ausente nessa etapa, a proposta inteira é bloqueada antes da escrita; falhas por item durante a execução do lote ficam registradas como `UNCERTAIN`. O perfil persistente é separado por padaria, loja e canal; uma conexão CDP compartilhada usa a sessão do navegador do operador e não oferece esse isolamento de perfil.

## Fontes de verdade no código

- `ifood/config.py`: variáveis, padrões e configuração do perfil.
- `ifood/auth.py`: fluxo OAuth e cache em memória.
- `ifood/client.py`: chamadas à API oficial e verificação do merchant autorizado.
- `ifood/sync_engine.py`: política `REVIEW`, capacidades, propostas, releitura e reconciliação de execução incerta.
- `ifood/playwright_executor.py`: allowlist, verificação de login/CAPTCHA e seletores atualmente usados.
- `tests/test_playwright_executor.py`: fixture de portal local simulado.
- `tests/test_sheets_sync.py`: modo automático simulado e bloqueio seguro de confirmação Playwright.

[1] https://developer.ifood.com.br/pt-BR/docs/getting-started/first-steps/create-app
[2] https://developer.ifood.com.br/pt-BR/docs/guides/modules/authentication/centralized
[3] https://developer.ifood.com.br/pt-BR/docs/guides/modules/merchant/endpoints
[4] https://developer.ifood.com.br/pt-BR/docs/getting-started/documentation/rate-limit

## Sources

[1] https://developer.ifood.com.br/pt-BR/docs/getting-started/first-steps/create-app — iFood Developer — Criar um aplicativo
[2] https://developer.ifood.com.br/pt-BR/docs/guides/modules/authentication/centralized — iFood Developer — Fluxo para aplicativos centralizados
[3] https://developer.ifood.com.br/pt-BR/docs/guides/modules/merchant/endpoints — iFood Developer — Endpoints Merchant
[4] https://developer.ifood.com.br/pt-BR/docs/getting-started/documentation/rate-limit — iFood Developer — Rate limit
