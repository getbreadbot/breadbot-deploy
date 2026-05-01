import { useState, useEffect, useRef } from 'react'
import { get } from '../lib/api.js'

function ScoreBadge({ score }) {
  const cls = score >= 80 ? 'high' : score >= 60 ? 'med' : 'low'
  return <span className={`score-badge ${cls}`}>{score}/100</span>
}

function timeAgo(ts) {
  const diff = Math.floor(Date.now() / 1000) - ts
  if (diff < 60) return `${diff}s ago`
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`
  return `${Math.floor(diff / 3600)}h ago`
}

function fmt(n, prefix = '') {
  if (n == null) return '—'
  if (n >= 1_000_000) return `${prefix}${(n / 1_000_000).toFixed(1)}M`
  if (n >= 1_000) return `${prefix}${(n / 1_000).toFixed(0)}K`
  return `${prefix}${n}`
}

function AlertCard({ alert, onDecision }) {
  const [acting, setActing] = useState(false)
  const [decided, setDecided] = useState(alert.actioned ? alert.action : null)
  // S80 P6: two-click BUY confirm. First click arms; second confirms within 10s.
  const [confirmArmed, setConfirmArmed] = useState(false)
  const [confirmTimer, setConfirmTimer] = useState(null)
  const expired = !decided && alert.expires_at && Date.now() / 1000 > alert.expires_at
  const errorMsg = alert.errorMsg || null

  // S80 P6: decision_ack is now authoritative — sync from parent state.
  // Pre-fix this was set once at mount only, so a failed buy left the local
  // state out of sync with the parent.
  useEffect(() => {
    setDecided(alert.actioned ? alert.action : null)
  }, [alert.actioned, alert.action])

  function armConfirm() {
    if (decided || expired || acting) return
    setConfirmArmed(true)
    if (confirmTimer) clearTimeout(confirmTimer)
    const t = setTimeout(() => {
      setConfirmArmed(false)
      setConfirmTimer(null)
    }, 10000)
    setConfirmTimer(t)
  }

  function cancelConfirm() {
    setConfirmArmed(false)
    if (confirmTimer) {
      clearTimeout(confirmTimer)
      setConfirmTimer(null)
    }
  }

  async function decide(action) {
    if (decided || expired || acting) return
    if (action === 'buy') {
      // S80 P6: first click arms, second click executes
      if (!confirmArmed) {
        armConfirm()
        return
      }
      if (confirmTimer) clearTimeout(confirmTimer)
      setConfirmTimer(null)
      setConfirmArmed(false)
    }
    setActing(true)
    try {
      // Send decision via WS. Buy waits for decision_ack to set decided.
      // Skip is local-only and updates immediately.
      onDecision(alert.id, action)
      if (action === 'skip') {
        setDecided('skip')
      }
    } finally {
      setActing(false)
    }
  }

  return (
    <div className={`alert-card ${!decided && !expired ? 'new' : ''} ${expired ? 'expired' : ''}`}>
      <div className="alert-header">
        <span className="alert-token">{alert.token || alert.symbol || 'Unknown'}</span>
        <span className="alert-chain">{alert.chain}</span>
        <ScoreBadge score={alert.security_score} />
        {expired && <span className="tag tag-gray" style={{ marginLeft: 'auto' }}>Expired</span>}
        {decided === 'buy' && <span className="tag tag-green" style={{ marginLeft: 'auto' }}>Bought</span>}
        {decided === 'auto_buy' && <span className="tag tag-green" style={{ marginLeft: 'auto' }}>Auto-bought</span>}
        {decided === 'skip' && <span className="tag tag-gray" style={{ marginLeft: 'auto' }}>Skipped</span>}
        {decided === 'buy_logged_no_exec' && <span className="tag tag-gray" style={{ marginLeft: 'auto' }} title="Logged before S80 P4 fix - never executed">Logged (no exec)</span>}
        {decided === 'blocked' && <span className="tag tag-red" style={{ marginLeft: 'auto' }}>Blocked</span>}
        {decided === 'execute_failed' && <span className="tag tag-red" style={{ marginLeft: 'auto' }}>Failed</span>}
        {!decided && !expired && (
          <span className="alert-time">{timeAgo(alert.timestamp)}</span>
        )}
      </div>

      <div className="alert-grid">
        <div>
          <div className="alert-stat-label">Price</div>
          <div className="alert-stat-value">${alert.price?.toFixed(8) ?? '—'}</div>
        </div>
        <div>
          <div className="alert-stat-label">Liquidity</div>
          <div className="alert-stat-value">{fmt(alert.liquidity_usd, '$')}</div>
        </div>
        <div>
          <div className="alert-stat-label">24h Volume</div>
          <div className="alert-stat-value">{fmt(alert.volume_24h, '$')}</div>
        </div>
        <div>
          <div className="alert-stat-label">Market Cap</div>
          <div className="alert-stat-value">{fmt(alert.market_cap, '$')}</div>
        </div>
        <div>
          <div className="alert-stat-label">Age</div>
          <div className="alert-stat-value">{alert.age_hours ? `${alert.age_hours}h` : '—'}</div>
        </div>
        <div>
          <div className="alert-stat-label">Recommended</div>
          <div className="alert-stat-value" style={{ color: 'var(--amber)' }}>
            {alert.position_size_usd ? `$${alert.position_size_usd.toFixed(0)}` : '—'}
          </div>
        </div>
        <div>
          <div className="alert-stat-label">Contract</div>
          <div className="alert-stat-value" style={{ fontSize: 11 }}>
            {alert.contract ? (
              <button
                type="button"
                onClick={(e) => {
                  e.stopPropagation()
                  navigator.clipboard.writeText(alert.contract).then(() => {
                    const el = e.currentTarget
                    const orig = el.textContent
                    el.textContent = 'Copied!'
                    el.style.color = 'var(--green, #4ade80)'
                    setTimeout(() => {
                      el.textContent = orig
                      el.style.color = ''
                    }, 1200)
                  }).catch(() => {})
                }}
                title={alert.contract + ' (tap to copy)'}
                style={{
                  background: 'none',
                  border: 'none',
                  padding: 0,
                  font: 'inherit',
                  fontSize: 11,
                  color: 'inherit',
                  cursor: 'pointer',
                  textDecoration: 'underline dotted',
                  textUnderlineOffset: '2px',
                }}
              >
                {`${alert.contract.slice(0, 6)}…${alert.contract.slice(-4)}`}
              </button>
            ) : '—'}
          </div>
        </div>
        <div>
          <div className="alert-stat-label">Source</div>
          <div className="alert-stat-value">{alert.source || 'Scanner'}</div>
        </div>
      </div>

      {alert.flags?.length > 0 && (
        <div className="alert-flags">
          {alert.flags.map((f, i) => (
            <span
              key={i}
              className={`flag ${f.type === 'ok' ? 'ok' : f.type === 'warn' ? 'warn' : 'risk'}`}
            >
              {f.label}
            </span>
          ))}
        </div>
      )}

      {errorMsg && (
        <div style={{
          background: 'rgba(239, 68, 68, 0.1)',
          border: '1px solid rgba(239, 68, 68, 0.3)',
          color: '#fca5a5',
          padding: '8px 12px',
          borderRadius: 4,
          marginTop: 8,
          fontSize: 12,
          whiteSpace: 'pre-wrap',
        }}>
          {errorMsg}
        </div>
      )}
      {!decided && !expired && !confirmArmed && (
        <div className="alert-actions">
          <button
            className="btn btn-green"
            onClick={() => decide('buy')}
            disabled={acting}
          >
            ✓ Buy {alert.position_size_usd ? `$${alert.position_size_usd.toFixed(0)}` : ''}
          </button>
          <button
            className="btn btn-ghost"
            onClick={() => decide('skip')}
            disabled={acting}
          >
            Skip
          </button>
          {alert.contract && (
            <a
              href={
                alert.chain === 'solana'
                  ? `https://dexscreener.com/solana/${alert.contract}`
                  : alert.chain === 'base'
                  ? `https://dexscreener.com/base/${alert.contract}`
                  : `https://dexscreener.com/search?q=${alert.contract}`
              }
              target="_blank"
              rel="noopener"
              className="btn btn-ghost"
              style={{ marginLeft: 'auto' }}
            >
              View chart ↗
            </a>
          )}
        </div>
      )}
      {!decided && !expired && confirmArmed && (
        <div className="alert-actions" style={{ background: 'rgba(245, 158, 11, 0.08)', padding: 8, borderRadius: 4, border: '1px solid rgba(245, 158, 11, 0.3)' }}>
          <div style={{ fontSize: 12, color: '#fbbf24', flexBasis: '100%', marginBottom: 4 }}>
            ⚠️ Tap CONFIRM within 10s to execute
          </div>
          <button
            className="btn btn-green"
            onClick={() => decide('buy')}
            disabled={acting}
            style={{ fontWeight: 'bold' }}
          >
            ✅ CONFIRM ${alert.position_size_usd ? alert.position_size_usd.toFixed(0) : ''}
          </button>
          <button
            className="btn btn-ghost"
            onClick={() => cancelConfirm()}
            disabled={acting}
          >
            Cancel
          </button>
        </div>
      )}
    </div>
  )
}

export default function Alerts() {
  const [alerts, setAlerts] = useState([])
  const [filter, setFilter] = useState('all') // pending | all
  const [sortBy, setSortBy] = useState('time_desc') // field_direction
  const [loading, setLoading] = useState(true)
  const [connected, setConnected] = useState(false)
  const wsRef = useRef(null)

  // Load history on mount
  useEffect(() => {
    get('/bot/alerts/history').then(data => {
      if (data?.alerts) setAlerts([...data.alerts].reverse())
    }).catch(e => {
      console.error('Failed to load alerts:', e)
    }).finally(() => setLoading(false))
  }, [])

  // WebSocket for real-time alerts
  useEffect(() => {
    function connect() {
      const proto = location.protocol === 'https:' ? 'wss' : 'ws'
      const ws = new WebSocket(`${proto}://${location.host}/api/ws/alerts`)
      wsRef.current = ws

      ws.onopen = () => setConnected(true)
      ws.onclose = () => {
        setConnected(false)
        setTimeout(connect, 3000)
      }
      ws.onmessage = (e) => {
        try {
          const msg = JSON.parse(e.data)
          if (msg.type === 'alert') {
            setAlerts(prev => {
              const exists = prev.find(a => a.id === msg.data.id)
              if (exists) return prev
              return [msg.data, ...prev]
            })
          }
          if (msg.type === 'decision_ack') {
            // S80 P6: ack now carries success flag + user_message. We mark
            // actioned=true ONLY if success=true (or skip — which always succeeds).
            // Failures surface as an inline error message that the AlertCard
            // can display.
            const ok = msg.action === 'skip' || msg.success === true
            setAlerts(prev => prev.map(a => {
              if (a.id !== msg.alert_id) return a
              if (ok) {
                // Use decision_value if backend supplies it (e.g. 'buy_logged_no_exec')
                const finalAction = msg.decision_value || msg.action
                return { ...a, actioned: true, action: finalAction, errorMsg: null }
              }
              // Failure — clear any prior error and surface the new one
              return { ...a, actioned: false, errorMsg: msg.user_message || 'Buy failed' }
            }))
          }
        } catch {}
      }
    }
    connect()
    return () => wsRef.current?.close()
  }, [])

  function sendDecision(alertId, action) {
    if (wsRef.current?.readyState !== WebSocket.OPEN) {
      // S80 P6: WS not connected — surface error immediately rather than
      // silently swallowing the click as the prior code did.
      setAlerts(prev => prev.map(a =>
        a.id === alertId ? { ...a, errorMsg: 'WebSocket disconnected — try again.' } : a
      ))
      return
    }
    wsRef.current.send(JSON.stringify({ type: 'decision', alert_id: alertId, action }))
    // S80 P6: NO optimistic update for buy. Wait for decision_ack to arrive
    // with success/failure verdict. Skip stays optimistic (it's local-only).
    if (action === 'skip') {
      setAlerts(prev => prev.map(a =>
        a.id === alertId ? { ...a, actioned: true, action: 'skip' } : a
      ))
    }
  }

  const filtered = filter === 'pending'
    ? alerts.filter(a => !a.actioned && (!a.expires_at || Date.now() / 1000 < a.expires_at))
    : alerts

  // Sort
  const SORT_FNS = {
    time_desc:  (a, b) => (b.timestamp ?? 0) - (a.timestamp ?? 0),
    time_asc:   (a, b) => (a.timestamp ?? 0) - (b.timestamp ?? 0),
    score_desc: (a, b) => (b.security_score ?? 0) - (a.security_score ?? 0),
    score_asc:  (a, b) => (a.security_score ?? 0) - (b.security_score ?? 0),
    liq_desc:   (a, b) => (b.liquidity_usd ?? 0) - (a.liquidity_usd ?? 0),
    vol_desc:   (a, b) => (b.volume_24h ?? 0) - (a.volume_24h ?? 0),
    mcap_desc:  (a, b) => (b.market_cap ?? 0) - (a.market_cap ?? 0),
    price_desc: (a, b) => (b.price ?? 0) - (a.price ?? 0),
  }
  const displayed = [...filtered].sort(SORT_FNS[sortBy] || SORT_FNS.time_desc)

  const pendingCount = alerts.filter(a => !a.actioned && (!a.expires_at || Date.now() / 1000 < a.expires_at)).length

  return (
    <div>
      <div style={{ display: 'flex', alignItems: 'baseline', gap: 12, marginBottom: 4 }}>
        <div className="page-title">Alerts</div>
        {pendingCount > 0 && (
          <span className="tag tag-amber">{pendingCount} pending</span>
        )}
      </div>
      <div className="page-sub">
        Scanner alerts — Buy or Skip each one. Alerts expire after 15 minutes.
      </div>

      <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginBottom: 20 }}>
        <div style={{ display: 'flex', gap: 8 }}>
          <button
            className={`btn btn-sm ${filter === 'pending' ? 'btn-amber' : 'btn-ghost'}`}
            onClick={() => setFilter('pending')}
          >
            Pending
          </button>
          <button
            className={`btn btn-sm ${filter === 'all' ? 'btn-amber' : 'btn-ghost'}`}
            onClick={() => setFilter('all')}
          >
            All
          </button>
        </div>
        <div style={{ display: 'flex', gap: 4, marginLeft: 16, alignItems: 'center' }}>
          <span style={{ fontSize: 11, color: 'var(--text-3)', marginRight: 4 }}>Sort:</span>
          {[
            ['time_desc', 'Newest'],
            ['score_desc', 'Score ↓'],
            ['score_asc', 'Score ↑'],
            ['liq_desc', 'Liquidity'],
            ['vol_desc', 'Volume'],
            ['mcap_desc', 'MCap'],
          ].map(([key, label]) => (
            <button key={key}
              className={`btn btn-sm ${sortBy === key ? 'btn-amber' : 'btn-ghost'}`}
              style={{ fontSize: 11, padding: '2px 8px' }}
              onClick={() => setSortBy(key)}
            >{label}</button>
          ))}
        </div>
        <div style={{ marginLeft: 'auto', display: 'flex', alignItems: 'center', gap: 6 }}>
          <div className={`dot ${connected ? 'green' : 'red'}`} />
          <span style={{ fontSize: 12, color: 'var(--text-2)', fontFamily: 'var(--mono)' }}>
            {connected ? 'Live' : 'Reconnecting...'}
          </span>
        </div>
      </div>

      {loading ? (
        <div className="loading"><div className="spinner" />Loading alerts...</div>
      ) : displayed.length === 0 ? (
        <div className="empty">
          <div className="empty-icon">◎</div>
          {filter === 'pending' ? 'No pending alerts. The scanner runs every 5 minutes.' : 'No alerts yet.'}
        </div>
      ) : (
        displayed.map(alert => (
          <AlertCard key={alert.id} alert={alert} onDecision={sendDecision} />
        ))
      )}
    </div>
  )
}
