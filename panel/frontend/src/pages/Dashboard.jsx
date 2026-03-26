import { useState, useEffect } from 'react'
import { get } from '../lib/api.js'

function StatCard({ label, value, sub, color }) {
  return (
    <div className="card">
      <div className="card-title">{label}</div>
      <div className={`stat-value ${color || ''}`}>{value ?? '—'}</div>
      {sub && <div className="stat-label">{sub}</div>}
    </div>
  )
}

export default function Dashboard() {
  const [status, setStatus] = useState(null)
  const [pnl, setPnl] = useState(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    async function load() {
      try {
        const [s, p] = await Promise.all([get('/bot/status'), get('/bot/pnl')])
        setStatus(s)
        setPnl(p)
      } catch {}
      setLoading(false)
    }
    load()
    const iv = setInterval(load, 30000)
    return () => clearInterval(iv)
  }, [])

  if (loading) return <div className="loading"><div className="spinner" />Loading dashboard...</div>

  const lossUsed = status?.daily_loss_limit_used_pct ?? 0
  const lossLimit = status?.daily_loss_limit_pct ?? 5
  const lossPercent = Math.min(100, (lossUsed / lossLimit) * 100)

  return (
    <div>
      <div className="page-title">Dashboard</div>
      <div className="page-sub">Bot status and today's summary</div>

      <div className="grid-4" style={{ marginBottom: 16 }}>
        <StatCard
          label="Bot status"
          value={status?.trading_active ? 'Active' : 'Paused'}
          color={status?.trading_active ? 'green' : 'amber'}
          sub={status?.trading_active ? 'Running normally' : 'Trading suspended'}
        />
        <StatCard
          label="Today's P&L"
          value={pnl ? `${pnl.total_pnl >= 0 ? '+' : ''}$${pnl.total_pnl?.toFixed(2)}` : '—'}
          color={pnl?.total_pnl >= 0 ? 'green' : 'red'}
          sub={`${pnl?.trade_count ?? 0} trades today`}
        />
        <StatCard
          label="Open positions"
          value={status?.open_positions ?? 0}
          sub={`of ${status?.max_positions ?? '—'} max`}
        />
        <StatCard
          label="Scan interval"
          value="5 min"
          sub={status?.last_scan ? `Last: ${new Date(status.last_scan * 1000).toLocaleTimeString()}` : 'Waiting...'}
        />
      </div>

      <div className="grid-2">
        <div className="card">
          <div className="card-title">Daily loss limit</div>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline' }}>
            <div className={`stat-value ${lossPercent > 80 ? 'red' : 'amber'}`}>
              {lossUsed?.toFixed(1)}%
            </div>
            <div className="stat-label">limit: {lossLimit}%</div>
          </div>
          <div className="progress-track">
            <div
              className={`progress-fill ${lossPercent > 80 ? 'danger' : ''}`}
              style={{ width: `${lossPercent}%` }}
            />
          </div>
          <div className="stat-label" style={{ marginTop: 8 }}>
            ${status?.daily_loss_remaining_usd?.toFixed(2) ?? '—'} remaining before auto-pause
          </div>
        </div>

        <div className="card">
          <div className="card-title">Bot configuration</div>
          <table className="table">
            <tbody>
              <tr>
                <td style={{ color: 'var(--text-2)' }}>Auto-execute</td>
                <td className="mono">
                  <span className={`tag ${status?.auto_execute ? 'tag-green' : 'tag-gray'}`}>
                    {status?.auto_execute ? 'ON' : 'OFF'}
                  </span>
                </td>
              </tr>
              <tr>
                <td style={{ color: 'var(--text-2)' }}>MEV protection</td>
                <td className="mono">
                  <span className={`tag ${status?.mev_enabled ? 'tag-green' : 'tag-amber'}`}>
                    {status?.mev_enabled ? 'ON' : 'OFF'}
                  </span>
                </td>
              </tr>
              <tr>
                <td style={{ color: 'var(--text-2)' }}>Max position size</td>
                <td className="mono">{status?.max_position_size_pct ? `${(status.max_position_size_pct * 100).toFixed(0)}%` : '—'}</td>
              </tr>
              <tr>
                <td style={{ color: 'var(--text-2)' }}>Alert channel</td>
                <td className="mono">
                  <span className="tag tag-amber">{status?.alert_channel ?? 'Telegram'}</span>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>
    </div>
  )
}
