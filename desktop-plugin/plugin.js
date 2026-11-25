/**
 * Loja iFood / Sheets — página NATIVA do app desktop (metade desktop do plugin).
 *
 * Mesmo backend Python do painel web (`plugins/ifood-loja/dashboard/plugin_api.py`)
 * via `ctx.rest`, escopado em /api/plugins/ifood-loja. Nada de regra de negócio
 * é recalculado aqui: status, política e capacidades vêm da API.
 *
 * Seções: visão geral (prontidão + sessões dos portais) · fila de mudanças
 * (confirmar/rejeitar/reconciliar) · auditoria · planilha (sincronizar + Apps Script).
 */

import {
  ROUTES_AREA,
  SIDEBAR_NAV_AREA,
  PALETTE_AREA,
  host,
  useQuery
} from '@hermes/plugin-sdk'
import { useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const ID = 'ifood-loja'
let pluginCtx = null

const STATUS_LABEL = {
  PENDING: 'Aguardando revisão',
  EXECUTING: 'Em execução',
  EXECUTED: 'Executado',
  APPLIED: 'Aplicado',
  CONFIRMED: 'Confirmado',
  UNCERTAIN: 'Resultado incerto',
  BLOCKED: 'Bloqueado',
  FAILED: 'Falhou',
  REJECTED: 'Rejeitado',
  STALE: 'Expirado',
  EXPIRED: 'Expirado'
}
const STATUS_TONE = {
  PENDING: 'border-amber-500/40 text-amber-400',
  EXECUTING: 'border-sky-500/40 text-sky-400',
  EXECUTED: 'border-emerald-500/40 text-emerald-400',
  APPLIED: 'border-emerald-500/40 text-emerald-400',
  CONFIRMED: 'border-emerald-500/40 text-emerald-400',
  UNCERTAIN: 'border-orange-500/40 text-orange-400',
  BLOCKED: 'border-rose-500/40 text-rose-400',
  FAILED: 'border-rose-500/40 text-rose-400',
  REJECTED: 'border-(--ui-stroke-secondary) text-(--ui-text-tertiary)',
  STALE: 'border-(--ui-stroke-secondary) text-(--ui-text-tertiary)',
  EXPIRED: 'border-(--ui-stroke-secondary) text-(--ui-text-tertiary)'
}
const DOMAIN_LABEL = {
  OPENING_HOURS: 'Horários',
  CATALOG: 'Cardápio',
  STORE_STATUS: 'Status da loja',
  PREP_TIME: 'Tempo de preparo'
}
const CHANNEL_LABEL = { IFOOD: 'iFood', '99FOOD': '99Food', NINETY_NINE: '99Food', ANOTA_AI: 'Anota AI' }
const WEEKDAY = ['dom', 'seg', 'ter', 'qua', 'qui', 'sex', 'sáb']

/* ── utilidades ─────────────────────────────────────────────────────────── */

function rest(path, options) {
  if (!pluginCtx) return Promise.reject(new Error('Plugin ainda não carregou.'))
  return pluginCtx.rest(path, { timeoutMs: 60000, ...options })
}

function when(iso) {
  if (!iso) return '—'
  const d = new Date(iso)
  return Number.isNaN(d.getTime())
    ? String(iso)
    : d.toLocaleString('pt-BR', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' })
}

function money(cents) {
  if (cents === null || cents === undefined) return '—'
  return (cents / 100).toLocaleString('pt-BR', { style: 'currency', currency: 'BRL' })
}

function fmtState(value) {
  if (value === null || value === undefined || value === '') return 'sem leitura'
  if (typeof value === 'string') return value
  if (typeof value === 'object') {
    return value.state || value.status || value.reason || value.detail || 'registrado'
  }
  return String(value)
}

function tenantQuery(tenant) {
  return tenant ? `?tenant_key=${encodeURIComponent(tenant)}` : ''
}

/* ── leitura legível da mudança proposta ────────────────────────────────── */

function hoursLines(current, proposed) {
  const fmt = rows =>
    (rows || [])
      .map(s => `${WEEKDAY[s.dayOfWeek] ?? s.dayOfWeek} ${s.start}+${s.duration}min`)
      .join(' · ') || 'sem turnos'
  return [`antes: ${fmt(current)}`, `depois: ${fmt(proposed)}`]
}

function catalogLines(current, proposed) {
  const before = new Map((current || []).map(i => [i.item_id, i]))
  const lines = []
  for (const item of proposed || []) {
    const old = before.get(item.item_id)
    const bits = []
    if (!old) bits.push('novo')
    if (old && old.price_cents !== item.price_cents) {
      bits.push(`${money(old.price_cents)} → ${money(item.price_cents)}`)
    }
    if (old && old.available !== item.available) {
      bits.push(item.available ? 'volta ao cardápio' : 'sai do cardápio')
    }
    if (!old && item.available === false) bits.push('indisponível')
    if (bits.length) lines.push(`${item.item_name || item.item_id}: ${bits.join(' · ')}`)
  }
  const after = new Set((proposed || []).map(i => i.item_id))
  for (const old of current || []) {
    if (!after.has(old.item_id)) lines.push(`${old.item_name || old.item_id}: fora da proposta`)
  }
  return lines.length ? lines : ['sem diferença em relação ao estado atual']
}

function statusLines(current, proposed) {
  const p = proposed || {}
  const action = {
    PAUSE: 'pausar a loja',
    RESUME: 'reabrir a loja',
    OPEN: 'abrir a loja',
    CLOSE: 'fechar a loja'
  }[String(p.status_action || '').toUpperCase()]
  const bits = [action || `ação ${p.status_action || 'não informada'}`]
  if (p.pause_reason) bits.push(`motivo: ${p.pause_reason}`)
  if (p.pause_start_iso) bits.push(`de ${when(p.pause_start_iso)}`)
  if (p.pause_end_iso) bits.push(`até ${when(p.pause_end_iso)}`)
  const was = current && current.status_action ? `antes: ${current.status_action}` : 'antes: sem pausa registrada'
  return [was, `depois: ${bits.join(' · ')}`]
}

function prepLines(current, proposed) {
  const before = current && current.prep_time_minutes != null ? `${current.prep_time_minutes} min` : 'não informado'
  const after = proposed && proposed.prep_time_minutes != null ? `${proposed.prep_time_minutes} min` : 'não informado'
  return [`antes: ${before}`, `depois: ${after}`]
}

function changeLines(proposal) {
  const domain = proposal.domain
  const current = proposal.current
  const proposed = proposal.proposed
  if (domain === 'OPENING_HOURS') return hoursLines(current, proposed)
  if (domain === 'CATALOG') return catalogLines(current, proposed)
  if (domain === 'STORE_STATUS') return statusLines(current, proposed)
  if (domain === 'PREP_TIME') return prepLines(current, proposed)
  return [JSON.stringify(proposed)]
}

/* ── átomos de UI ───────────────────────────────────────────────────────── */

const CARD = 'rounded-lg border border-(--ui-stroke-secondary) px-3 py-2.5'
const HINT = 'text-[0.6875rem] text-(--ui-text-quaternary)'
const MUTED = 'text-(--ui-text-secondary)'
const INPUT = [
  'h-9 w-full rounded-md border border-(--ui-stroke-secondary) px-2 text-sm',
  'text-foreground outline-none focus:border-(--ui-accent)'
].join(' ')

function Button(props) {
  return jsx('button', {
    type: 'button',
    disabled: props.disabled,
    onClick: props.onClick,
    className: [
      'h-8 shrink-0 rounded-md border border-(--ui-stroke-secondary) px-3 text-[0.8125rem] font-medium',
      'text-foreground transition-colors hover:bg-(--chrome-action-hover)',
      props.disabled ? 'opacity-50' : '',
      props.primary ? 'border-emerald-500/50 text-emerald-300' : '',
      props.danger ? 'border-rose-500/40 text-rose-300' : ''
    ].join(' '),
    children: props.children
  })
}

function Metric(props) {
  return jsxs('div', {
    className: 'flex flex-col gap-0.5 rounded-lg border border-(--ui-stroke-secondary) px-3 py-2',
    children: [
      jsx('div', { className: 'text-[0.625rem] uppercase tracking-wide text-(--ui-text-quaternary)', children: props.label }),
      jsx('div', { className: 'text-lg font-semibold text-foreground', children: props.value }),
      jsx('div', { className: HINT, children: props.hint })
    ]
  })
}

function Row(props) {
  return jsxs('div', {
    className: 'flex flex-wrap items-center justify-between gap-2 border-b border-(--ui-stroke-secondary) py-1.5 last:border-b-0',
    children: [
      jsx('span', { className: 'text-[0.8125rem] ' + MUTED, children: props.label }),
      jsx('span', {
        className: 'text-[0.8125rem] font-medium ' + (props.tone || 'text-foreground'),
        children: props.value
      })
    ]
  })
}

function Section(props) {
  return jsxs('div', {
    className: CARD + ' flex flex-col gap-1',
    children: [
      jsx('div', { className: 'text-[0.625rem] uppercase tracking-wide text-(--ui-text-quaternary)', children: props.title }),
      props.children
    ]
  })
}

function yes(value) {
  return value ? 'configurado' : 'pendente'
}

function tone(value) {
  return value ? 'text-emerald-400' : 'text-amber-400'
}

/* ── página ─────────────────────────────────────────────────────────────── */

function IfoodPage() {
  const [tab, setTab] = useState('visao')
  const [tenant, setTenant] = useState('')
  const [error, setError] = useState('')
  const [msg, setMsg] = useState('')
  const [busy, setBusy] = useState('')
  const [dryRun, setDryRun] = useState(true)
  const [appScript, setAppScript] = useState(null)

  const status = useQuery({ queryKey: [ID, 'status'], queryFn: () => rest('/status'), refetchInterval: 60000 })
  const tenants = useQuery({ queryKey: [ID, 'tenants'], queryFn: () => rest('/tenants') })
  const overview = useQuery({
    queryKey: [ID, 'overview', tenant],
    queryFn: () => rest('/sync/overview' + tenantQuery(tenant)),
    refetchInterval: 20000
  })
  const audit = useQuery({
    queryKey: [ID, 'audit', tenant],
    queryFn: () => rest(`/sync/audit?limit=60${tenant ? `&tenant_key=${encodeURIComponent(tenant)}` : ''}`),
    refetchInterval: 60000
  })

  const tenantList = tenants.data || []
  const data = overview.data || {}
  const readiness = data.readiness || {}
  const proposals = data.proposals || []
  const audits = audit.data || data.audit_events || []
  const sessions = data.portal_sessions || readiness.portal_sessions || {}
  const capabilities = readiness.write_capabilities || []
  const pending = proposals.filter(p => p.status === 'PENDING')
  const uncertain = proposals.filter(p => p.status === 'UNCERTAIN')

  const refresh = () => {
    status.refetch()
    tenants.refetch()
    overview.refetch()
    audit.refetch()
  }

  const act = async (key, fn) => {
    setBusy(key)
    setError('')
    setMsg('')
    try {
      const out = await fn()
      refresh()
      return out
    } catch (e) {
      setError(e && e.message ? e.message : String(e))
      return null
    } finally {
      setBusy('')
    }
  }

  const runSync = () => act('sync', async () => {
    const out = await rest('/sheets/sync', {
      method: 'POST',
      body: { dry_run: dryRun, tenant_key: tenant || null }
    })
    const outcome = out && out.outcome ? out.outcome : 'concluído'
    setMsg(`Sincronização ${dryRun ? 'simulada' : 'executada'}: ${outcome}.`)
    return out
  })

  const confirmProposal = p => act('cf-' + p.proposal_id, async () => {
    const out = await rest(`/sync/proposals/${p.proposal_id}/confirm`, {
      method: 'POST',
      body: { override_guardrail: false, tenant_key: p.tenant_key }
    })
    setMsg(`Proposta ${p.proposal_id} confirmada.`)
    return out
  })

  const rejectProposal = p => act('rj-' + p.proposal_id, async () => {
    const out = await rest(`/sync/proposals/${p.proposal_id}/reject`, {
      method: 'POST',
      body: { reason: 'rejeitada pelo operador no desktop', tenant_key: p.tenant_key }
    })
    setMsg(`Proposta ${p.proposal_id} rejeitada.`)
    return out
  })

  const reconcileProposal = p => act('rc-' + p.proposal_id, async () => {
    const out = await rest(
      `/sync/proposals/${p.proposal_id}/reconcile${p.tenant_key ? `?tenant_key=${encodeURIComponent(p.tenant_key)}` : ''}`,
      { method: 'POST' }
    )
    setMsg(`Proposta ${p.proposal_id} reconciliada.`)
    return out
  })

  const loadAppScript = () => act('gs', async () => {
    const out = await rest('/sheets/apps-script')
    setAppScript(out || { content: '' })
    return out
  })

  const copyAppScript = async () => {
    if (!appScript || !appScript.content) return
    const ok = pluginCtx && pluginCtx.os ? await pluginCtx.os.writeClipboard(appScript.content) : false
    setMsg(ok ? 'Code.gs copiado para a área de transferência.' : 'Não foi possível copiar automaticamente.')
  }

  /* ── abas ── */

  function renderVisao() {
    return jsxs('div', { className: 'grid grid-cols-1 gap-2 lg:grid-cols-2', children: [
      jsx(Section, {
        title: 'Prontidão das integrações',
        children: jsxs('div', { className: 'flex flex-col', children: [
          jsx(Row, { label: 'Plugin habilitado', value: yes(readiness.enabled), tone: tone(readiness.enabled) }),
          jsx(Row, {
            label: 'Google Sheets API',
            value: yes(readiness.sheets_api_configured),
            tone: tone(readiness.sheets_api_configured)
          }),
          jsx(Row, {
            label: 'Planilha da loja',
            value: readiness.sheets_spreadsheet_id ? 'informada' : 'não informada',
            tone: tone(Boolean(readiness.sheets_spreadsheet_id))
          }),
          jsx(Row, { label: 'Webhook HMAC', value: yes(readiness.webhook_configured), tone: tone(readiness.webhook_configured) }),
          jsx(Row, {
            label: 'Poller Google Sheets',
            value: readiness.sheets_poller_enabled
              ? `ativo (${fmtState(readiness.sheets_poller_runtime)})`
              : `desligado (${fmtState(readiness.sheets_poller_runtime)})`,
            tone: readiness.sheets_poller_enabled ? 'text-emerald-400' : MUTED
          }),
          jsx(Row, { label: 'iFood API', value: yes(readiness.ifood_api_configured), tone: tone(readiness.ifood_api_configured) }),
          jsx(Row, { label: 'Playwright (portais)', value: yes(readiness.playwright_configured), tone: tone(readiness.playwright_configured) })
        ] })
      }),
      jsx(Section, {
        title: 'Política de escrita',
        children: jsxs('div', { className: 'flex flex-col', children: [
          jsx(Row, { label: 'Modo de sincronização', value: readiness.default_sync_mode || 'REVIEW' }),
          jsx(Row, { label: 'Backend preferido', value: readiness.default_execution_mode || '—' }),
          jsx(Row, {
            label: 'Capacidades liberadas',
            value: capabilities.length ? capabilities.join(', ') : 'todas bloqueadas',
            tone: capabilities.length ? 'text-emerald-400' : 'text-amber-400'
          }),
          jsx(Row, {
            label: 'Limite de variação de preço',
            value: readiness.max_price_change_pct != null ? `${readiness.max_price_change_pct}%` : '—'
          })
        ] })
      }),
      jsx(Section, {
        title: 'Sessões dos portais',
        children: jsx('div', { className: 'flex flex-col', children: ['IFOOD', '99FOOD', 'ANOTA_AI'].map(key => {
          const perTenant = sessions[tenant || data.tenant_key] || Object.values(sessions)[0] || {}
          const s = perTenant[key] || (key === '99FOOD' ? perTenant.NINETY_NINE : null) || {}
          const state = fmtState(s.state)
          const ok = state === 'OK' || state === 'CONFIGURED'
          return jsx(Row, {
            label: CHANNEL_LABEL[key] || key,
            value: state + (s.last_error ? ' · ' + s.last_error : ''),
            tone: ok ? 'text-emerald-400' : 'text-amber-400'
          }, key)
        }) })
      }),
      jsx(Section, {
        title: 'Último lote',
        children: jsxs('div', { className: 'flex flex-col', children: [
          jsx(Row, { label: 'Lotes registrados', value: String((data.snapshots || []).length || 0) }),
          jsx(Row, { label: 'Poller', value: fmtState(data.sheets_poller && data.sheets_poller.state) }),
          jsx(Row, { label: 'Eventos de auditoria', value: String((data.audit_events || []).length || 0) })
        ] })
      })
    ] })
  }

  function renderFila() {
    if (!proposals.length) {
      return jsx('div', { className: CARD + ' ' + HINT, children: 'Nenhuma proposta registrada. Ela aparece aqui quando a planilha divergir do estado atual da loja.' })
    }
    return jsx('div', { className: 'flex flex-col gap-2', children: proposals.map(p => {
      const lines = changeLines(p)
      const warnings = p.guardrail_warnings || []
      const canConfirm = p.status === 'PENDING'
      const canReconcile = p.status === 'UNCERTAIN'
      return jsxs('div', {
        className: CARD + ' flex flex-col gap-1.5',
        children: [
          jsxs('div', {
            className: 'flex flex-wrap items-center gap-2',
            children: [
              jsx('span', {
                className: ['rounded-md border px-2 py-0.5 text-[0.6875rem]', STATUS_TONE[p.status] || 'border-(--ui-stroke-secondary)'].join(' '),
                children: STATUS_LABEL[p.status] || p.status
              }),
              jsx('span', { className: 'text-sm font-medium text-foreground', children: DOMAIN_LABEL[p.domain] || p.domain }),
              jsx('span', { className: 'text-sm ' + MUTED, children: p.store_key }),
              jsx('span', { className: HINT, children: `${CHANNEL_LABEL[p.channel] || p.channel} · ${p.execution_mode}` }),
              jsx('span', { className: HINT, children: `criada ${when(p.created_at)} · expira ${when(p.expires_at)}` })
            ]
          }),
          jsx('div', {
            className: 'flex flex-col gap-0.5 text-[0.75rem] ' + MUTED,
            children: lines.map((line, i) => jsx('div', { children: line }, i))
          }),
          warnings.length
            ? jsx('div', { className: 'text-[0.75rem] text-amber-400', children: `Avisos: ${warnings.join(' | ')}` })
            : null,
          jsxs('div', {
            className: 'flex flex-wrap items-center gap-2 pt-0.5',
            children: [
              canConfirm
                ? jsx(Button, { primary: true, disabled: busy === 'cf-' + p.proposal_id, onClick: () => confirmProposal(p), children: 'Confirmar' })
                : null,
              canConfirm
                ? jsx(Button, { danger: true, disabled: busy === 'rj-' + p.proposal_id, onClick: () => rejectProposal(p), children: 'Rejeitar' })
                : null,
              canReconcile
                ? jsx(Button, { disabled: busy === 'rc-' + p.proposal_id, onClick: () => reconcileProposal(p), children: 'Reconciliar' })
                : null,
              jsx('span', { className: HINT, children: p.proposal_id })
            ]
          })
        ]
      }, p.proposal_id)
    }) })
  }

  function renderAuditoria() {
    if (!audits.length) {
      return jsx('div', { className: CARD + ' ' + HINT, children: 'Sem eventos de auditoria para esta loja.' })
    }
    return jsx('div', { className: 'flex flex-col gap-1', children: audits.map(e => jsxs('div', {
      className: CARD + ' flex flex-wrap items-center gap-2',
      children: [
        jsx('span', { className: HINT, children: when(e.timestamp_utc) }),
        jsx('span', { className: 'text-[0.75rem] font-medium text-foreground', children: e.event_type || e.action || 'evento' }),
        jsx('span', { className: 'text-[0.75rem] ' + MUTED, children: e.domain || '' }),
        jsx('span', { className: 'text-[0.75rem] ' + MUTED, children: e.actor_id || '' }),
        jsx('span', { className: HINT, children: e.detail || e.message || e.proposal_id || '' })
      ]
    }, e.id || `${e.timestamp_utc}-${e.event_type}`)) })
  }

  function renderPlanilha() {
    return jsxs('div', { className: 'flex flex-col gap-2', children: [
      jsx(Section, {
        title: 'Sincronizar a planilha',
        children: jsxs('div', { className: 'flex flex-col gap-2', children: [
          jsx('div', {
            className: 'text-[0.75rem] ' + MUTED,
            children: 'Lê a planilha (fonte da verdade), compara com o estado atual da loja e produz propostas para revisão. Escrita no canal continua sujeita às capacidades liberadas.'
          }),
          jsxs('div', { className: 'flex flex-wrap items-center gap-2', children: [
            jsx(Button, {
              disabled: busy === 'sync',
              onClick: () => setDryRun(v => !v),
              children: dryRun ? 'Modo: simulação' : 'Modo: aplicar'
            }),
            jsx(Button, { primary: true, disabled: busy === 'sync', onClick: runSync, children: busy === 'sync' ? 'Sincronizando…' : 'Sincronizar agora' }),
            jsx(Button, { disabled: busy === 'gs', onClick: loadAppScript, children: 'Ver Apps Script' })
          ] })
        ] })
      }),
      appScript
        ? jsx(Section, {
            title: 'apps_script/Code.gs',
            children: jsxs('div', { className: 'flex flex-col gap-2', children: [
              jsxs('div', { className: 'flex items-center gap-2', children: [
                jsx(Button, { onClick: copyAppScript, children: 'Copiar' }),
                jsx('span', { className: HINT, children: appScript.filename || 'Code.gs' })
              ] }),
              jsx('pre', {
                className: 'max-h-72 overflow-auto rounded-md border border-(--ui-stroke-secondary) p-2 text-[0.6875rem] ' + MUTED,
                children: appScript.content || 'arquivo vazio'
              })
            ] })
          })
        : null
    ] })
  }

  const tabs = [
    { id: 'visao', label: 'Visão geral' },
    { id: 'fila', label: `Fila de mudanças (${pending.length})` },
    { id: 'auditoria', label: 'Auditoria' },
    { id: 'planilha', label: 'Planilha' }
  ]

  return jsxs('div', {
    className: 'flex h-full flex-col overflow-hidden',
    children: [
      jsxs('div', {
        className: 'flex flex-wrap items-start justify-between gap-3 border-b border-(--ui-stroke-secondary) px-5 py-4',
        children: [
          jsxs('div', {
            className: 'flex flex-col gap-1',
            children: [
              jsx('div', { className: HINT, children: 'Hermes · operação de lojas' }),
              jsx('h2', { className: 'text-lg font-semibold text-foreground', children: 'Loja iFood / Sheets' }),
              jsx('p', {
                className: 'max-w-2xl text-[0.8125rem] ' + MUTED,
                children: 'A planilha é o comando; o canal é a verdade. Cada divergência vira proposta revisada, com aviso no Telegram — nada é escrito sem capacidade liberada.'
              })
            ]
          }),
          jsxs('div', {
            className: 'flex flex-wrap items-center gap-2',
            children: [
              tenantList.length > 1
                ? jsx('select', {
                    className: INPUT + ' w-48',
                    'aria-label': 'Selecionar padaria',
                    style: { colorScheme: 'dark', backgroundColor: '#101c23', color: '#eaf4f0' },
                    value: tenant,
                    onChange: e => setTenant(e.target.value),
                    children: [
                      jsx('option', {
                        value: '',
                        style: { backgroundColor: '#17232c', color: '#eaf4f0' },
                        children: 'escolha a loja'
                      }, 'ph'),
                      ...tenantList.map(t => jsx('option', {
                        value: t.tenant_key,
                        style: { backgroundColor: '#17232c', color: '#eaf4f0' },
                        children: t.tenant_key
                      }, t.tenant_key))
                    ]
                  })
                : jsx('span', {
                    className: 'rounded-md border border-(--ui-stroke-secondary) px-2 py-1 text-[0.75rem] text-(--ui-text-secondary)',
                    children: `Padaria: ${tenant || tenantList[0]?.tenant_key || 'não configurada'}`
                  }),
              jsx('span', {
                className: 'rounded-md border border-emerald-500/40 px-2 py-1 text-[0.6875rem] text-emerald-400',
                children: 'Acesso restrito ao operador'
              }),
              jsx(Button, { onClick: refresh, children: 'Atualizar' })
            ]
          })
        ]
      }),

      jsxs('div', {
        className: 'flex flex-wrap gap-1 border-b border-(--ui-stroke-secondary) px-5 py-2',
        children: tabs.map(t => jsx('button', {
          type: 'button',
          onClick: () => setTab(t.id),
          'aria-current': tab === t.id ? 'page' : undefined,
          className: [
            'h-9 rounded-md px-3 text-[0.8125rem] font-medium transition-colors',
            tab === t.id ? 'text-foreground' : 'text-(--ui-text-tertiary) hover:text-foreground'
          ].join(' '),
          style: tab === t.id ? { backgroundColor: '#1b6e55', color: '#ffffff' } : undefined,
          children: t.label
        }, t.id))
      }),

      jsxs('div', {
        className: 'flex-1 overflow-y-auto px-5 py-4',
        children: [
          status.isError || overview.isError
            ? jsx('div', {
                className: 'mb-3 rounded-md border border-rose-500/40 px-3 py-2 text-[0.75rem] text-rose-300',
                children: 'O backend do plugin não respondeu. Confira se ifood-loja está habilitado e se IFOOD_LOJA_ENABLED=true no .env.'
              })
            : null,
          error ? jsx('div', { className: 'mb-3 rounded-md border border-rose-500/40 px-3 py-2 text-[0.75rem] text-rose-300', children: error }) : null,
          msg ? jsx('div', { className: 'mb-3 rounded-md border border-(--ui-stroke-secondary) px-3 py-2 text-[0.75rem] ' + MUTED, children: msg }) : null,

          jsxs('div', {
            className: 'mb-4 grid grid-cols-2 gap-2 md:grid-cols-4',
            children: [
              jsx(Metric, { label: 'Aguardando revisão', value: pending.length, hint: 'propostas com decisão pendente' }),
              jsx(Metric, { label: 'Resultado incerto', value: uncertain.length, hint: 'exigem reconciliação' }),
              jsx(Metric, { label: 'Lojas', value: tenantList.length || 1, hint: 'isoladas por tenant' }),
              jsx(Metric, { label: 'Auditoria', value: audits.length, hint: 'eventos mais recentes' })
            ]
          }),

          tab === 'visao' ? renderVisao() : null,
          tab === 'fila' ? renderFila() : null,
          tab === 'auditoria' ? renderAuditoria() : null,
          tab === 'planilha' ? renderPlanilha() : null,

          jsx('div', {
            className: 'mt-6 text-[0.6875rem] text-(--ui-text-quaternary)',
            children: 'Política REVIEW: a proposta nunca escreve sozinha. Confirmação sem capacidade liberada responde 403 de propósito — liberar exige homologação do contrato do canal.'
          })
        ]
      })
    ]
  })
}

export default {
  id: ID,
  name: 'Loja iFood / Sheets',
  register(ctx) {
    pluginCtx = ctx
    ctx.registerMany([
      { id: 'page', area: ROUTES_AREA, data: { path: '/ifood-loja' }, render: () => jsx(IfoodPage, {}) },
      { id: 'nav', area: SIDEBAR_NAV_AREA, data: { path: '/ifood-loja', label: 'Loja iFood', codicon: 'package' } },
      {
        id: 'open',
        area: PALETTE_AREA,
        data: {
          id: 'ifood-loja.open',
          label: 'Abrir Loja iFood / Sheets',
          keywords: ['ifood', 'loja', 'cardapio', 'cardápio', 'planilha', 'sheets', 'padaria', '99food', 'delivery'],
          run: () => host.navigate('/ifood-loja')
        }
      }
    ])
    return () => { pluginCtx = null }
  }
}

