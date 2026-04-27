import { useState, useEffect, useCallback } from 'react'
import { Link } from 'react-router-dom'
import { get, post } from '../lib/api.js'

// Per-strategy headline metric extractor. Each engine reports slightly
// different fields via get_strategy_performance, so we pick the most
// useful single number for the index card. Detail pages (Grid.jsx,
// FundingArb.jsx) keep the full breakdown.
function headlineMetric(name, m) {
  if (!m || typeof m !== 'object') return { label: '—', value: '—' }
  switch (name) {
    case 'grid':
      return {
        label: 'Cycles / Profit',
        value: `${m.cycles ?? 0} / $${(m.profit_usd ?? m.pnl ?? 0).toFixed(2)}`,
      }
    case 'funding_arb':
      return {
        label: 'Funding collected',
        value: `$${(m.funding_collected_usd ?? m.collected ?? 0).toFixed(2)}`,
      }
    case 'yield_rebalance':
      return {
        label: 'Yield gained',
        value: `$${(m.yield_gained_usd ?? 0).toFixed(2)} · ${m.rebalances ?? 0} moves`,
      }
    case 'pendle':
      return {
        label: 'Locked positions',
        value: m.positions !== undefined
          ? `${m.positions} · $${(m.locked_usd ?? 0).toFixed(2)}`
          : '—',
      }
    default:
      return { label: '—', value: '—' }
  }
}

function StrategyCard({ name, data, busy, onToggle }) {
  const enabled = !!data.enabled
  const dot = enabled ? 'green' : 'grey'
  const status = enabled ? 'Enabled' : 'Disabled'
  const statusColor = enabled ? 'var(--green)' : 'var(--text-3)'
  const metric = headlineMetric(name, data.metrics)

  return (
    <div className="card" style={{ marginBottom: 16 }}>
      {/* Header row: dot, label, status, configure link */}
      <div style={{
        display: 'flex', alignItems: 'center', gap: 12,
        marginBottom: 10, flexWrap: 'wrap',
      }}>
        <div className={`dot ${dot}`} style={{
          background: enabled ? 'var(--green)' : 'var(--bg-3)',
          boxShadow: enabled ? '0 0 6px var(--green)' : 'none',
        }} />
        <span style={{ fontSize: 16, fontWeight: 700 }}>{data.label}</span>
        <span style={{
          fontSize: 12, color: statusColor, fontWeight: 600,
          textTransform: 'uppercase', letterSpacing: '0.06em',
        }}>
          {status}
        </span>
        <Link
          to={data.configure_path}
          className="btn btn-ghost btn-sm"
          style={{ marginLeft: 'auto', textDecoration: 'none' }}
        >
          Configure →
        </Link>
      </div>

      {/* Description */}
      <div style={{ fontSize: 13, color: 'var(--text-2)', marginBottom: 12 }}>
        {data.description}
      </div>

      {/* Metric + venue + toggle row */}
      <div style={{
        display: 'flex', alignItems: 'center', gap: 24,
        flexWrap: 'wrap',
        paddingTop: 10,
        borderTop: '1px solid var(--border)',
      }}>
        <div style={{ minWidth: 160 }}>
          <div style={{
            fontSize: 11, color: 'var(--text-3)',
            textTransform: 'uppercase', letterSpacing: '0.07em',
            marginBottom: 4,
          }}>
            {metric.label}
          </div>
          <div className="mono" style={{ fontSize: 15, fontWeight: 600 }}>
            {metric.value}
          </div>
        </div>
        <div style={{ minWidth: 160 }}>
          <div style={{
            fontSize: 11, color: 'var(--text-3)',
            textTransform: 'uppercase', letterSpacing: '0.07em',
            marginBottom: 4,
          }}>
            Venue
          </div>
          <div style={{ fontSize: 13 }}>{data.venue}</div>
        </div>
        <div className="toggle-row" style={{ marginLeft: 'auto', gap: 12 }}>
          <label className="toggle">
            <input
              type="checkbox"
              checked={enabled}
              onChange={e => onToggle(name, e.target.checked)}
              disabled={busy}
            />
            <span className="toggle-slider" />
          </label>
          <div>
            <div className="toggle-label" style={{ fontWeight: 600 }}>
              {enabled ? 'On' : 'Off'}
              {busy && (
                <span style={{ fontSize: 11, color: 'var(--text-3)', marginLeft: 8 }}>
                  Saving...
                </span>
              )}
            </div>
            <div className="toggle-desc">
              {enabled
                ? 'Engine is running. Toggle off to stop.'
                : 'Toggle on to start the engine.'}
            </div>
          </div>
        </div>
      </div>
    </div>
  )
}

export default function Strategies() {
  const [data, setData]       = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError]     = useState('')
  const [busy, setBusy]       = useState({})  // { [name]: true } while toggling
  const [msg, setMsg]         = useState('')

  const load = useCallback(async () => {
    try {
      const res = await get('/bot/strategy/all')
      setData(res?.strategies || {})
      setError('')
    } catch (e) {
      setError(e.message || 'Failed to load strategy state')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    load()
    // Poll every 30s — engine state lags ~60s behind the flag flip,
    // so the dot/status will catch up to the toggle on the next tick.
    const iv = setInterval(load, 30000)
    return () => clearInterval(iv)
  }, [load])

  async function onToggle(name, enabled) {
    setBusy(b => ({ ...b, [name]: true }))
    setMsg('')
    try {
      const res = await post(`/bot/strategy/${name}/enable`, { enabled })
      setMsg(res?.message || 'Updated')
      // Optimistic local update so the toggle visually flips instantly.
      // The next poll will reconcile if the write somehow rolled back.
      setData(d => ({
        ...d,
        [name]: { ...(d?.[name] || {}), enabled },
      }))
    } catch (e) {
      setMsg(`Error: ${e.message}`)
      // Revert by reloading fresh state
      await load()
    } finally {
      setBusy(b => {
        const next = { ...b }
        delete next[name]
        return next
      })
    }
  }

  if (loading) {
    return <div className="loading"><div className="spinner" />Loading strategies...</div>
  }

  const order = ['grid', 'funding_arb', 'yield_rebalance', 'pendle']
  const enabledCount = order.filter(n => data?.[n]?.enabled).length

  return (
    <div>
      <div className="page-title">Strategies</div>
      <div className="page-sub">
        Four independent engines. Each runs in-process inside the scanner
        and polls its enable flag every ~60s. Toggle on or off here, then
        use the Configure link for per-strategy settings and detail.
      </div>

      {error && (
        <div className="auth-error" style={{ marginBottom: 16 }}>{error}</div>
      )}

      {/* Summary line */}
      <div style={{
        display: 'flex', alignItems: 'center', gap: 16,
        marginBottom: 16, flexWrap: 'wrap',
        fontSize: 13, color: 'var(--text-2)',
      }}>
        <span>
          <strong style={{ color: 'var(--text-1)' }}>{enabledCount}</strong>
          {' '}of {order.length} enabled
        </span>
        <span style={{ opacity: 0.4 }}>·</span>
        <span>Engines pick up changes within ~60s.</span>
        {msg && (
          <span style={{ marginLeft: 'auto', color: 'var(--green)' }}>
            {msg}
          </span>
        )}
      </div>

      {order.map(name => (
        <StrategyCard
          key={name}
          name={name}
          data={data?.[name] || {}}
          busy={!!busy[name]}
          onToggle={onToggle}
        />
      ))}
    </div>
  )
}
