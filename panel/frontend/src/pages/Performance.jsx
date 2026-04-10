import { useState, useEffect, useCallback } from 'react'
import { get } from '../lib/api.js'

// ── Tiny SVG line chart — no external deps ────────────────────────────────
function LineChart({ data, width = 600, height = 160 }) {
  if (!data || data.length < 2) return (
    <div style={{ height, display: 'flex', alignItems: 'center',
      justifyContent: 'center', color: 'var(--text-3)', fontSize: 13 }}>
      No history yet — chart populates once the bot has completed trades.
    </div>
  )

  const values  = data.map(d => d.cumulative)
  const min     = Math.min(...values)
  const max     = Math.max(...values)
  const range   = max - min || 1
  const pad     = { top: 16, right: 12, bottom: 28, left: 52 }
  const W       = width  - pad.left - pad.right
  const H       = height - pad.top  - pad.bottom

  const x = (i) => pad.left + (i / (data.length - 1)) * W
  const y = (v) => pad.top  + H - ((v - min) / range) * H

  // Build SVG polyline points
  const pts = data.map((d, i) => `${x(i).toFixed(1)},${y(d.cumulative).toFixed(1)}`).join(' ')

  // Zero line
  const zeroY = y(0)
  const aboveZero = min >= 0
  const lineColor = values[values.length - 1] >= 0 ? '#10b981' : '#f87171'

  // X-axis labels: first, middle, last
  const labelIdxs = [0, Math.floor(data.length / 2), data.length - 1]

  return (
    <svg width="100%" viewBox={`0 0 ${width} ${height}`}
      style={{ overflow: 'visible', display: 'block' }}>

      {/* Zero reference line */}
      {!aboveZero && (
        <line x1={pad.left} y1={zeroY} x2={pad.left + W} y2={zeroY}
          stroke="rgba(255,255,255,0.1)" strokeWidth="1" strokeDasharray="4 3" />
      )}

      {/* Fill under curve */}
      <polygon
        points={`${x(0).toFixed(1)},${y(0).toFixed(1)} ${pts} ${x(data.length-1).toFixed(1)},${y(0).toFixed(1)}`}
        fill={lineColor}
        fillOpacity="0.08"
      />

      {/* Main line */}
      <polyline points={pts} fill="none" stroke={lineColor} strokeWidth="1.8"
        strokeLinejoin="round" strokeLinecap="round" />

      {/* Y-axis labels */}
      {[min, (min + max) / 2, max].map((v, i) => (
        <text key={i} x={pad.left - 6} y={y(v) + 4}
          textAnchor="end" fontSize="10" fill="var(--text-3)">
          {v >= 0 ? '+' : ''}{v.toFixed(2)}
        </text>
      ))}

      {/* X-axis labels */}
      {labelIdxs.map(i => (
        <text key={i} x={x(i)} y={height - 4}
          textAnchor="middle" fontSize="10" fill="var(--text-3)">
          {data[i]?.date?.slice(5) ?? ''}
        </text>
      ))}
    </svg>
  )
}

// ── Stat card ─────────────────────────────────────────────────────────────
function StatCard({ label, value, sub, positive, neutral }) {
  const color = neutral ? 'var(--text-1)'
    : positive === undefined ? 'var(--text-1)'
    : positive ? 'var(--green)'
    : value === 0 ? 'var(--text-3)'
    : '#f87171'
  return (
    <div style={{
      flex: 1, minWidth: 130,
      background: 'var(--bg-2)', border: '1px solid var(--border)',
      borderRadius: 8, padding: '14px 16px',
    }}>
      <div style={{ fontSize: 11, color: 'var(--text-3)', textTransform: 'uppercase',
        letterSpacing: '0.07em', marginBottom: 6 }}>{label}</div>
      <div style={{ fontSize: 22, fontWeight: 700, color,
        fontVariantNumeric: 'tabular-nums' }}>{value}</div>
      {sub && <div style={{ fontSize: 12, color: 'var(--text-3)', marginTop: 4 }}>{sub}</div>}
    </div>
  )
}

// ── Helpers ───────────────────────────────────────────────────────────────
const fmt  = (n) => { n = n ?? 0; return (n >= 0 ? '+' : '') + n.toFixed(2) }
const fmtn = (n) => (n ?? 0).toFixed(4)

export default function Performance() {
  const [perf, setPerf]     = useState(null)
  const [history, setHist]  = useState([])
  const [range, setRange]   = useState(30)
  const [loading, setLoad]  = useState(true)
  const [error, setError]   = useState('')

  const load = useCallback(async (days) => {
    setLoad(true)
    try {
      const [p, h] = await Promise.all([
        get('/bot/strategy/performance'),
        get(`/bot/pnl/history?days=${days}`),
      ])
      setPerf(p)
      setHist(Array.isArray(h) ? h : [])
      setError('')
    } catch (e) {
      setError(e.message)
    } finally {
      setLoad(false)
    }
  }, [])

  useEffect(() => { load(range) }, [range, load])

  // Derived totals from history
  const totalNet   = history.reduce((s, d) => s + (d.net ?? d.pnl ?? 0), 0)
  const totalYield = history.reduce((s, d) => s + (d.yield_earned ?? 0), 0)
  const totalFees  = history.reduce((s, d) => s + (d.fees_paid ?? 0), 0)
  const totalTrades = history.reduce((s, d) => s + (d.trades ?? 0), 0)
  const latestCumulative = history.length > 0 ? (history[history.length - 1]?.cumulative ?? 0) : 0

  if (loading) return <div className="loading"><div className="spinner" />Loading performance...</div>

  return (
    <div>
      <div className="page-title">Performance</div>
      <div className="page-sub">Strategy P&L across scanner trades, yield rebalancer, grid, and funding arb.</div>

      {error && <div className="auth-error" style={{ marginBottom: 16 }}>{error}</div>}

      {/* Range selector */}
      <div style={{ display: 'flex', gap: 8, marginBottom: 16 }}>
        {[7, 14, 30, 90].map(d => (
          <button key={d}
            className={`btn btn-sm ${range === d ? 'btn-amber' : 'btn-ghost'}`}
            onClick={() => setRange(d)}
          >{d}d</button>
        ))}
      </div>

      {/* Headline stats */}
      <div style={{ display: 'flex', gap: 12, flexWrap: 'wrap', marginBottom: 16 }}>
        <StatCard
          label={`${range}d net P&L`}
          value={`${fmt(totalNet)}`}
          sub="realized + yield − fees"
          positive={totalNet > 0}
        />
        <StatCard
          label="Yield earned"
          value={`+$${totalYield.toFixed(4)}`}
          sub="rebalancer + staking"
          positive={true}
        />
        <StatCard
          label="Fees paid"
          value={`$${totalFees.toFixed(4)}`}
          sub="exchange + gas"
          neutral={true}
        />
        <StatCard
          label="Trades"
          value={totalTrades}
          sub={`${range}-day window`}
          neutral={true}
        />
      </div>

      {/* Chart */}
      <div className="card" style={{ marginBottom: 16 }}>
        <div style={{ display: 'flex', alignItems: 'center',
          justifyContent: 'space-between', marginBottom: 12 }}>
          <div className="card-title" style={{ marginBottom: 0 }}>
            Cumulative net P&L
          </div>
          <span style={{ fontSize: 18, fontWeight: 700,
            color: latestCumulative >= 0 ? 'var(--green)' : '#f87171',
            fontVariantNumeric: 'tabular-nums' }}>
            {fmt(latestCumulative)}
          </span>
        </div>
        <LineChart data={history} />
      </div>

      {/* Strategy breakdown */}
      <div className="card" style={{ marginBottom: 16 }}>
        <div className="card-title">Strategy breakdown — last 30 days</div>
        {perf && !perf.error ? (
          <table className="table" style={{ width: '100%' }}>
            <thead>
              <tr>
                <th>Strategy</th>
                <th>Activity</th>
                <th>P&L / Income</th>
                <th>Status</th>
              </tr>
            </thead>
            <tbody>
              <tr>
                <td style={{ fontWeight: 600 }}>Grid trading</td>
                <td style={{ color: 'var(--text-3)', fontSize: 13 }}>
                  {perf.grid?.cycles ?? 0} cycles · ${(perf.grid?.volume_usd ?? 0).toLocaleString()} volume
                </td>
                <td className="mono"
                  style={{ color: (perf.grid?.profit_usd ?? 0) > 0 ? 'var(--green)' : 'var(--text-3)' }}>
                  {fmt(perf.grid?.profit_usd ?? 0)}
                </td>
                <td>
                  {(perf.grid?.cycles ?? 0) > 0
                    ? <span className="tag tag-green">Active</span>
                    : <span className="tag" style={{ background:'var(--bg-3)', color:'var(--text-3)' }}>No cycles yet</span>}
                </td>
              </tr>
              <tr>
                <td style={{ fontWeight: 600 }}>Funding arb</td>
                <td style={{ color: 'var(--text-3)', fontSize: 13 }}>
                  {perf.funding_arb?.open_positions ?? 0} open ·{' '}
                  ${fmtn(perf.funding_arb?.funding_collected_usd ?? 0)} collected
                </td>
                <td className="mono"
                  style={{ color: (perf.funding_arb?.closed_pnl_usd ?? 0) > 0 ? 'var(--green)' : 'var(--text-3)' }}>
                  {fmt(perf.funding_arb?.closed_pnl_usd ?? 0)}
                </td>
                <td>
                  {(perf.funding_arb?.open_positions ?? 0) > 0
                    ? <span className="tag tag-green">Positions open</span>
                    : <span className="tag" style={{ background:'var(--bg-3)', color:'var(--text-3)' }}>No positions</span>}
                </td>
              </tr>
              <tr>
                <td style={{ fontWeight: 600 }}>Yield rebalancer</td>
                <td style={{ color: 'var(--text-3)', fontSize: 13 }}>
                  {perf.yield_rebalancer?.rebalances ?? 0} rebalances
                </td>
                <td className="mono"
                  style={{ color: (perf.yield_rebalancer?.yield_gained_usd ?? 0) > 0 ? 'var(--green)' : 'var(--text-3)' }}>
                  +${fmtn(perf.yield_rebalancer?.yield_gained_usd ?? 0)}
                </td>
                <td>
                  {(perf.yield_rebalancer?.rebalances ?? 0) > 0
                    ? <span className="tag tag-green">Active</span>
                    : <span className="tag" style={{ background:'var(--bg-3)', color:'var(--text-3)' }}>No moves yet</span>}
                </td>
              </tr>
            </tbody>
          </table>
        ) : (
          <div style={{ fontSize: 13, color: 'var(--text-3)' }}>
            {perf?.error ?? 'Unable to reach MCP server.'}
          </div>
        )}
      </div>

      {/* Daily table */}
      {history.length > 0 && (
        <div className="card">
          <div className="card-title">Daily breakdown</div>
          <div style={{ overflowX: 'auto' }}>
            <table className="table" style={{ width: '100%', fontSize: 12 }}>
              <thead>
                <tr>
                  <th>Date</th>
                  <th>Realized</th>
                  <th>Yield</th>
                  <th>Fees</th>
                  <th>Net</th>
                  <th>Cumulative</th>
                  <th>Trades</th>
                </tr>
              </thead>
              <tbody>
                {[...history].reverse().map(d => (
                  <tr key={d.date}>
                    <td style={{ color: 'var(--text-3)', fontVariantNumeric: 'tabular-nums' }}>
                      {d.date}
                    </td>
                    <td className="mono"
                      style={{ color: (d.realized_pnl ?? d.pnl ?? 0) >= 0 ? 'var(--green)' : '#f87171' }}>
                      {fmt(d.realized_pnl ?? d.pnl ?? 0)}
                    </td>
                    <td className="mono" style={{ color: 'var(--green)' }}>
                      {(d.yield_earned ?? 0) > 0 ? `+${(d.yield_earned ?? 0).toFixed(4)}` : '—'}
                    </td>
                    <td className="mono" style={{ color: 'var(--text-3)' }}>
                      {(d.fees_paid ?? 0) > 0 ? (d.fees_paid ?? 0).toFixed(4) : '—'}
                    </td>
                    <td className="mono"
                      style={{ fontWeight: 600,
                        color: (d.net ?? 0) > 0 ? 'var(--green)' : (d.net ?? 0) < 0 ? '#f87171' : 'var(--text-3)' }}>
                      {fmt(d.net ?? 0)}
                    </td>
                    <td className="mono"
                      style={{ color: (d.cumulative ?? 0) >= 0 ? 'var(--green)' : '#f87171' }}>
                      {fmt(d.cumulative ?? 0)}
                    </td>
                    <td style={{ color: 'var(--text-3)' }}>{d.trades}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  )
}
