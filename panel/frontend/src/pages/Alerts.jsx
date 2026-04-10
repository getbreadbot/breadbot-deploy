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
  const expired = !decided && alert.expires_at && Date.now() / 1000 > alert.expires_at

  async function decide(action) {
    if (decided || expired || acting) return
    setActing(true)
    try {
      onDecision(alert.id, action)
      setDecided(action)
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
        {decided === 'skip' && <span className="tag tag-gray" style={{ marginLeft: 'auto' }}>Skipped</span>}
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
            {alert.contract ? `${alert.contract.slice(0, 6)}…${alert.contract.slice(-4)}` : '—'}
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

      {!decided && !expired && (
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
              href={`https://dexscreener.com/search?q=${alert.contract}`}
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
    </div>
  )
}

export default function Alerts() {
  const [alerts, setAlerts] = useState([])
  const [filter, setFilter] = useState('all') // pending | all
  const [connected, setConnected] = useState(false)
  const wsRef = useRef(null)

  // Load history on mount
  useEffect(() => {
    get('/bot/alerts/history').then(data => {
      if (data?.alerts) setAlerts(data.alerts.reverse())
    }).catch(() => {})
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
            setAlerts(prev => prev.map(a =>
              a.id === msg.alert_id ? { ...a, actioned: true, action: msg.action } : a
            ))
          }
        } catch {}
      }
    }
    connect()
    return () => wsRef.current?.close()
  }, [])

  function sendDecision(alertId, action) {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({ type: 'decision', alert_id: alertId, action }))
    }
    setAlerts(prev => prev.map(a =>
      a.id === alertId ? { ...a, actioned: true, action } : a
    ))
  }

  const displayed = filter === 'pending'
    ? alerts.filter(a => !a.actioned && (!a.expires_at || Date.now() / 1000 < a.expires_at))
    : alerts

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
        <div style={{ marginLeft: 'auto', display: 'flex', alignItems: 'center', gap: 6 }}>
          <div className={`dot ${connected ? 'green' : 'red'}`} />
          <span style={{ fontSize: 12, color: 'var(--text-2)', fontFamily: 'var(--mono)' }}>
            {connected ? 'Live' : 'Reconnecting...'}
          </span>
        </div>
      </div>

      {displayed.length === 0 ? (
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
