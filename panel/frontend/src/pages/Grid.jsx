import { useState, useEffect, useCallback } from 'react'
import { get, post } from '../lib/api.js'

function RsiGauge({ rsi }) {
  const clamped = Math.min(100, Math.max(0, rsi ?? 50))
  const inRange = clamped >= 35 && clamped <= 65
  const color = inRange ? 'var(--green)' : 'var(--amber)'
  const pct = clamped
  return (
    <div style={{ marginBottom: 16 }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 12, color: 'var(--text-3)', marginBottom: 6 }}>
        <span>RSI</span>
        <span style={{ color, fontWeight: 600 }}>{clamped.toFixed(1)}</span>
      </div>
      <div style={{ height: 8, background: 'var(--bg-3)', borderRadius: 4, overflow: 'hidden', position: 'relative' }}>
        {/* Safe zone band (35–65) */}
        <div style={{
          position: 'absolute', top: 0, bottom: 0,
          left: '35%', width: '30%',
          background: 'rgba(16,185,129,0.15)',
        }} />
        {/* Pointer */}
        <div style={{
          position: 'absolute', top: 0, bottom: 0,
          left: `${pct}%`, width: 3,
          background: color, borderRadius: 2,
          transform: 'translateX(-50%)',
        }} />
      </div>
      <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 10, color: 'var(--text-3)', marginTop: 4 }}>
        <span>0 — Oversold</span>
        <span style={{ color: 'var(--green)' }}>35–65 Safe zone</span>
        <span>100 — Overbought</span>
      </div>
    </div>
  )
}

function StatBox({ label, value, sub, color }) {
  return (
    <div style={{
      flex: 1, minWidth: 130,
      background: 'var(--bg-2)', border: '1px solid var(--border)',
      borderRadius: 8, padding: '14px 16px',
    }}>
      <div style={{ fontSize: 11, color: 'var(--text-3)', textTransform: 'uppercase', letterSpacing: '0.07em', marginBottom: 6 }}>
        {label}
      </div>
      <div style={{ fontSize: 22, fontWeight: 700, color: color || 'var(--text-1)', fontVariantNumeric: 'tabular-nums' }}>
        {value}
      </div>
      {sub && <div style={{ fontSize: 12, color: 'var(--text-3)', marginTop: 4 }}>{sub}</div>}
    </div>
  )
}

export default function Grid() {
  const [status, setStatus]   = useState(null)
  const [loading, setLoading] = useState(true)
  const [acting, setActing]   = useState(false)
  const [msg, setMsg]         = useState('')
  const [error, setError]     = useState('')

  const load = useCallback(async () => {
    try {
      const s = await get('/bot/grid/status')
      setStatus(s)
      setError('')
    } catch (e) {
      setError(e.message)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    load()
    const iv = setInterval(load, 15000)
    return () => clearInterval(iv)
  }, [load])

  async function doStart() {
    if (status?.trend_guard_blocked) {
      setMsg('RSI trend guard is active — wait for RSI to enter the 35–65 range before starting.')
      return
    }
    setActing(true); setMsg('')
    try {
      await post('/bot/grid/start')
      setMsg('Grid started.')
      await load()
    } catch (e) { setMsg(`Error: ${e.message}`) }
    finally { setActing(false) }
  }

  async function doStop() {
    setActing(true); setMsg('')
    try {
      await post('/bot/grid/stop')
      setMsg('Grid stopped. Open orders cancelled.')
      await load()
    } catch (e) { setMsg(`Error: ${e.message}`) }
    finally { setActing(false) }
  }

  if (loading) return <div className="loading"><div className="spinner" />Loading grid status...</div>

  const state    = status?.state ?? 'STANDBY'
  const blocked  = status?.trend_guard_blocked ?? true
  const isActive = state === 'ACTIVE'

  const stateColor = isActive ? 'var(--green)' : blocked ? 'var(--amber)' : 'var(--text-3)'
  const stateDot   = isActive ? 'green' : blocked ? 'amber' : 'grey'

  return (
    <div>
      <div className="page-title">Grid Trading</div>
      <div className="page-sub">
        Places buy/sell orders at preset price intervals. Profits from price oscillating within the range.
      </div>

      {error && (
        <div className="auth-error" style={{ marginBottom: 16 }}>{error}</div>
      )}

      {/* State card */}
      <div className="card" style={{ marginBottom: 16 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginBottom: 16 }}>
          <div className={`dot ${stateDot}`} />
          <span style={{ fontSize: 16, fontWeight: 700, color: stateColor }}>{state}</span>
          <span style={{ fontSize: 13, color: 'var(--text-3)' }}>
            {status?.pair ?? '—'}
          </span>
          {!status?.enabled && (
            <span className="tag" style={{ background: 'rgba(239,68,68,0.1)', color: '#ef4444', marginLeft: 'auto' }}>
              GRID_ENABLED=false
            </span>
          )}
        </div>

        <RsiGauge rsi={status?.rsi} />

        {blocked && (
          <div style={{
            padding: '10px 14px', marginBottom: 14,
            background: 'rgba(240,180,41,0.07)',
            border: '1px solid rgba(240,180,41,0.25)',
            borderRadius: 6, fontSize: 13, color: 'var(--text-2)',
          }}>
            Trend guard active — RSI {status?.rsi?.toFixed(1)} is outside the 35–65 safe zone.
            Grid will not start while a strong directional trend is detected.
          </div>
        )}

        <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', marginBottom: msg ? 12 : 0 }}>
          <button
            className="btn btn-amber"
            onClick={doStart}
            disabled={acting || isActive || !status?.enabled}
          >
            {acting && !isActive ? 'Starting...' : 'Start grid'}
          </button>
          <button
            className="btn btn-ghost"
            onClick={doStop}
            disabled={acting || !isActive}
          >
            {acting && isActive ? 'Stopping...' : 'Stop grid'}
          </button>
          <button className="btn btn-ghost btn-sm" onClick={load} style={{ marginLeft: 'auto' }}>
            Refresh
          </button>
        </div>

        {msg && (
          <div style={{ fontSize: 13, color: 'var(--text-2)', marginTop: 10 }}>{msg}</div>
        )}
      </div>

      {/* Stats */}
      <div style={{ display: 'flex', gap: 12, flexWrap: 'wrap', marginBottom: 16 }}>
        <StatBox
          label="Cycles completed"
          value={status?.cycles_completed ?? 0}
          sub="buy-sell pairs filled"
        />
        <StatBox
          label="Total profit"
          value={`$${(status?.total_profit_usd ?? 0).toFixed(4)}`}
          sub="current session"
          color={status?.total_profit_usd > 0 ? 'var(--green)' : undefined}
        />
        <StatBox
          label="Allocation"
          value={`$${(status?.allocation_usd ?? 0).toLocaleString()}`}
          sub="capital deployed"
        />
        <StatBox
          label="Grid levels"
          value={status?.num_levels ?? '—'}
          sub="buy + sell orders"
        />
      </div>

      {/* Range card — only shown when active */}
      {status?.entry_price && (
        <div className="card">
          <div className="card-title">Grid range</div>
          <div style={{ display: 'flex', gap: 24, flexWrap: 'wrap', fontSize: 13 }}>
            <div>
              <div style={{ color: 'var(--text-3)', fontSize: 11, marginBottom: 4 }}>ENTRY PRICE</div>
              <div className="mono">${status.entry_price?.toLocaleString('en-US', { maximumFractionDigits: 2 })}</div>
            </div>
            <div>
              <div style={{ color: 'var(--text-3)', fontSize: 11, marginBottom: 4 }}>UPPER BOUND</div>
              <div className="mono" style={{ color: 'var(--green)' }}>
                ${status.upper_bound?.toLocaleString('en-US', { maximumFractionDigits: 2 })}
              </div>
            </div>
            <div>
              <div style={{ color: 'var(--text-3)', fontSize: 11, marginBottom: 4 }}>LOWER BOUND</div>
              <div className="mono" style={{ color: 'var(--amber)' }}>
                ${status.lower_bound?.toLocaleString('en-US', { maximumFractionDigits: 2 })}
              </div>
            </div>
          </div>
          <div style={{ marginTop: 12, fontSize: 12, color: 'var(--text-3)' }}>
            Price must stay within range for the grid to profit. If price exits the lower bound,
            the risk manager treats the drawdown as a realised loss against the daily limit.
          </div>
        </div>
      )}

      {/* Config note */}
      {!status?.enabled && (
        <div className="card" style={{ marginTop: 16 }}>
          <div style={{ fontSize: 13, color: 'var(--text-2)' }}>
            Grid trading is disabled. Set{' '}
            <code style={{ fontSize: 11, background: 'var(--bg-3)', padding: '1px 5px', borderRadius: 3 }}>GRID_ENABLED=true</code>
            {' '}in Settings → Advanced, then reload this page.
          </div>
        </div>
      )}
    </div>
  )
}
