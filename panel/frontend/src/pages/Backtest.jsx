import { useState, useEffect, useCallback } from 'react'
import { get, post } from '../lib/api.js'

// ── Outcome bar chart ─────────────────────────────────────────────────────
// Renders a horizontal stacked bar showing trade outcome proportions
function OutcomeBar({ counts, total }) {
  if (!counts || !total) return null

  // Color map for each outcome bucket
  const COLORS = {
    tp2:         { color: '#10b981', label: 'TP2 hit' },
    tp1:         { color: '#34d399', label: 'TP1 full' },
    tp1_partial: { color: '#6ee7b7', label: 'TP1 partial' },
    holding:     { color: '#fbbf24', label: 'Still holding' },
    expired:     { color: '#94a3b8', label: 'Expired (48h)' },
    stop_loss:   { color: '#f87171', label: 'Stop loss' },
    no_data:     { color: '#475569', label: 'No price data' },
  }

  const order = ['tp2', 'tp1', 'tp1_partial', 'holding', 'expired', 'stop_loss', 'no_data']

  return (
    <div>
      {/* Stacked bar */}
      <div style={{ display: 'flex', height: 28, borderRadius: 6,
        overflow: 'hidden', marginBottom: 12 }}>
        {order.map(key => {
          const n = counts[key] || 0
          if (!n) return null
          const pct = (n / total * 100).toFixed(1)
          const cfg = COLORS[key] || { color: '#64748b', label: key }
          return (
            <div key={key}
              title={`${cfg.label}: ${n} (${pct}%)`}
              style={{ width: `${pct}%`, background: cfg.color,
                minWidth: n > 0 ? 2 : 0, transition: 'width 0.4s' }} />
          )
        })}
      </div>
      {/* Legend */}
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: '6px 16px' }}>
        {order.map(key => {
          const n = counts[key] || 0
          if (!n) return null
          const cfg = COLORS[key] || { color: '#64748b', label: key }
          return (
            <div key={key} style={{ display: 'flex', alignItems: 'center',
              gap: 6, fontSize: 12, color: 'var(--text-2)' }}>
              <div style={{ width: 10, height: 10, borderRadius: 2,
                background: cfg.color, flexShrink: 0 }} />
              <span>{cfg.label}</span>
              <span style={{ color: 'var(--text-3)', fontVariantNumeric: 'tabular-nums' }}>
                {n} ({(n / total * 100).toFixed(0)}%)
              </span>
            </div>
          )
        })}
      </div>
    </div>
  )
}

// ── Stat card (reuse same pattern as Performance page) ───────────────────
function StatCard({ label, value, sub, positive, neutral }) {
  const color = neutral ? 'var(--text-1)'
    : positive === undefined ? 'var(--text-1)'
    : positive ? 'var(--green)'
    : 'var(--text-1)'
  return (
    <div style={{ flex: 1, minWidth: 130, background: 'var(--bg-2)',
      border: '1px solid var(--border)', borderRadius: 8, padding: '14px 16px' }}>
      <div style={{ fontSize: 11, color: 'var(--text-3)', textTransform: 'uppercase',
        letterSpacing: '0.07em', marginBottom: 6 }}>{label}</div>
      <div style={{ fontSize: 22, fontWeight: 700, color,
        fontVariantNumeric: 'tabular-nums' }}>{value}</div>
      {sub && <div style={{ fontSize: 12, color: 'var(--text-3)', marginTop: 4 }}>{sub}</div>}
    </div>
  )
}

// ── Helpers ───────────────────────────────────────────────────────────────
const fmt  = (n) => n == null ? '—' : (n >= 0 ? '+' : '') + Number(n).toFixed(2)
const fmtd = (iso) => {
  if (!iso) return '—'
  try { return new Date(iso).toLocaleString() } catch { return iso }
}

// Break-even win rate: loss_avg / (win_avg + |loss_avg|)
// e.g. avg_win=$36.71, avg_loss=-$16.21 → 16.21/(36.71+16.21) = 30.6%
function breakEven(avgWin, avgLoss) {
  if (!avgWin || !avgLoss) return null
  const absLoss = Math.abs(avgLoss)
  return (absLoss / (avgWin + absLoss) * 100).toFixed(1)
}

export default function Backtest() {
  const [results, setResults]   = useState(null)
  const [loading, setLoading]   = useState(true)
  const [error, setError]       = useState('')
  const [running, setRunning]   = useState(false)
  const [runMsg, setRunMsg]     = useState('')

  // Trigger parameters
  const [mode,     setMode]     = useState('all')
  const [minScore, setMinScore] = useState(75)
  const [days,     setDays]     = useState(30)

  const loadResults = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
      const data = await get('/bot/backtest/results')
      if (data?.error) {
        setError(data.error)
        setResults(null)
      } else {
        setResults(data)
      }
    } catch (e) {
      setError(e.message)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { loadResults() }, [loadResults])

  async function triggerRun() {
    setRunning(true)
    setRunMsg('')
    try {
      const resp = await post('/bot/backtest/trigger', { mode, min_score: minScore, days })
      if (resp?.status === 'launched') {
        setRunMsg(
          `Run started (PID ${resp.pid}). A full all-alerts sweep takes ~30–45 min ` +
          `due to API rate limits. Refresh this page when done.`
        )
      } else if (resp?.status === 'error') {
        setRunMsg(`Error: ${resp.message}`)
      } else {
        setRunMsg(JSON.stringify(resp))
      }
    } catch (e) {
      setRunMsg(`Error: ${e.message}`)
    } finally {
      setRunning(false)
    }
  }

  const be = results ? breakEven(results.avg_win_usd, results.avg_loss_usd) : null
  const winRatePct = results?.win_rate_pct
  const isAboveBreakEven = be && winRatePct && parseFloat(winRatePct) >= parseFloat(be)

  return (
    <div>
      <div className="page-title">Backtest</div>
      <div className="page-sub">
        Replay scanner alerts against historical price data to evaluate signal quality.
        Exit rules: −20% stop loss · +50% TP1 (sell half) · +100% TP2 · 48h max hold.
      </div>

      {/* ── Cached results ─────────────────────────────────────────── */}
      {loading ? (
        <div className="loading"><div className="spinner" />Loading results...</div>
      ) : error ? (
        <div className="card" style={{ marginBottom: 16 }}>
          <div style={{ color: 'var(--text-3)', fontSize: 13 }}>{error}</div>
        </div>
      ) : results && (
        <>
          {/* Run metadata */}
          <div style={{ display: 'flex', alignItems: 'center', gap: 12,
            marginBottom: 14, flexWrap: 'wrap' }}>
            <span className="tag" style={{ background: 'var(--bg-3)',
              color: 'var(--text-2)', fontSize: 12 }}>
              Mode: {results.mode} · Score ≥ {results.min_score} · Last {results.days}d
            </span>
            <span style={{ fontSize: 12, color: 'var(--text-3)' }}>
              Completed {fmtd(results.completed_at)}
            </span>
            <button className="btn btn-ghost btn-sm" onClick={loadResults}>
              Refresh
            </button>
          </div>

          {/* Headline stats */}
          <div style={{ display: 'flex', gap: 12, flexWrap: 'wrap', marginBottom: 16 }}>
            <StatCard
              label="Alerts simulated"
              value={results.total_trades?.toLocaleString() ?? '—'}
              sub={`${results.wins} W / ${results.losses} L / ${results.no_data} no-data`}
              neutral={true}
            />
            <StatCard
              label="Win rate"
              value={`${results.win_rate_pct}%`}
              sub={be ? `Break-even: ${be}% — ${isAboveBreakEven ? 'above ✓' : 'below ✗'}` : ''}
              positive={isAboveBreakEven}
            />
            <StatCard
              label="Total PnL"
              value={fmt(results.total_pnl_usd)}
              sub={`${results.total_trades} trades · $${results.portfolio_usd?.toLocaleString()} portfolio`}
              positive={results.total_pnl_usd > 0}
            />
            <StatCard
              label="Avg win / loss"
              value={`${fmt(results.avg_win_usd)} / ${fmt(results.avg_loss_usd)}`}
              sub={`Ratio: ${results.avg_win_usd && results.avg_loss_usd
                ? (results.avg_win_usd / Math.abs(results.avg_loss_usd)).toFixed(2) + 'x' : '—'}`}
              neutral={true}
            />
          </div>

          {/* Outcome breakdown */}
          <div className="card" style={{ marginBottom: 16 }}>
            <div className="card-title">Outcome breakdown</div>
            <OutcomeBar
              counts={results.outcome_counts}
              total={results.total_trades}
            />
          </div>

          {/* Exit params */}
          <div className="card" style={{ marginBottom: 16 }}>
            <div className="card-title">Simulation parameters</div>
            <table className="table" style={{ width: '100%', fontSize: 13 }}>
              <tbody>
                <tr>
                  <td style={{ color: 'var(--text-3)' }}>Stop loss</td>
                  <td className="mono">−{((results.stop_loss_pct ?? 0.20) * 100).toFixed(0)}%</td>
                </tr>
                <tr>
                  <td style={{ color: 'var(--text-3)' }}>TP1 (sell half)</td>
                  <td className="mono">+{((results.tp1_pct ?? 0.50) * 100).toFixed(0)}%</td>
                </tr>
                <tr>
                  <td style={{ color: 'var(--text-3)' }}>TP2 (remainder)</td>
                  <td className="mono">+{((results.tp2_pct ?? 1.00) * 100).toFixed(0)}%</td>
                </tr>
                <tr>
                  <td style={{ color: 'var(--text-3)' }}>Max hold</td>
                  <td className="mono">48 hours</td>
                </tr>
                <tr>
                  <td style={{ color: 'var(--text-3)' }}>Portfolio size</td>
                  <td className="mono">${results.portfolio_usd?.toLocaleString()}</td>
                </tr>
              </tbody>
            </table>
          </div>
        </>
      )}

      {/* ── Run a new backtest ────────────────────────────────────── */}
      <div className="card">
        <div className="card-title">Run new backtest</div>
        <div style={{ fontSize: 13, color: 'var(--text-3)', marginBottom: 16 }}>
          Launches a background process on the server. A full all-alerts sweep (~1,000+
          alerts) takes 30–45 minutes due to GeckoTerminal rate limits. The cached
          results above update when the run completes.
        </div>

        {/* Controls */}
        <div style={{ display: 'flex', gap: 16, flexWrap: 'wrap', marginBottom: 16 }}>
          <div>
            <div style={{ fontSize: 11, color: 'var(--text-3)', marginBottom: 6,
              textTransform: 'uppercase', letterSpacing: '0.07em' }}>Mode</div>
            <div style={{ display: 'flex', gap: 6 }}>
              {['actual', 'all'].map(m => (
                <button key={m}
                  className={`btn btn-sm ${mode === m ? 'btn-amber' : 'btn-ghost'}`}
                  onClick={() => setMode(m)}>
                  {m === 'actual' ? 'Actual buys only' : 'All alerts'}
                </button>
              ))}
            </div>
          </div>

          <div>
            <div style={{ fontSize: 11, color: 'var(--text-3)', marginBottom: 6,
              textTransform: 'uppercase', letterSpacing: '0.07em' }}>
              Min score — {minScore}
            </div>
            <input type="range" min={60} max={95} step={5} value={minScore}
              onChange={e => setMinScore(Number(e.target.value))}
              style={{ width: 160, accentColor: 'var(--amber)' }} />
            <div style={{ display: 'flex', justifyContent: 'space-between',
              fontSize: 10, color: 'var(--text-3)', width: 160, marginTop: 2 }}>
              <span>60</span><span>95</span>
            </div>
          </div>

          <div>
            <div style={{ fontSize: 11, color: 'var(--text-3)', marginBottom: 6,
              textTransform: 'uppercase', letterSpacing: '0.07em' }}>Look-back window</div>
            <div style={{ display: 'flex', gap: 6 }}>
              {[7, 14, 30, 60].map(d => (
                <button key={d}
                  className={`btn btn-sm ${days === d ? 'btn-amber' : 'btn-ghost'}`}
                  onClick={() => setDays(d)}>
                  {d}d
                </button>
              ))}
            </div>
          </div>
        </div>

        <button
          className="btn btn-amber"
          onClick={triggerRun}
          disabled={running}
          style={{ opacity: running ? 0.6 : 1 }}>
          {running ? 'Launching...' : '▶ Start backtest'}
        </button>

        {runMsg && (
          <div style={{ marginTop: 12, fontSize: 13, color: 'var(--text-3)',
            background: 'var(--bg-3)', borderRadius: 6, padding: '10px 12px' }}>
            {runMsg}
          </div>
        )}
      </div>
    </div>
  )
}
