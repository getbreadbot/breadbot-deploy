import { useState, useEffect, useCallback, useRef } from 'react'
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
        <div style={{
          position: 'absolute', top: 0, bottom: 0,
          left: '35%', width: '30%',
          background: 'rgba(16,185,129,0.15)',
        }} />
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

const STATE_DISPLAY = {
  STANDBY:  { label: 'STANDBY',  color: 'var(--text-3)', dot: 'grey'  },
  STARTING: { label: 'STARTING', color: 'var(--amber)',  dot: 'amber' },
  ACTIVE:   { label: 'ACTIVE',   color: 'var(--green)',  dot: 'green' },
  PAUSED:   { label: 'PAUSED',   color: 'var(--amber)',  dot: 'amber' },
  STOPPING: { label: 'STOPPING', color: 'var(--amber)',  dot: 'amber' },
}

export default function Grid() {
  const [status, setStatus]   = useState(null)
  const [loading, setLoading] = useState(true)
  const [acting, setActing]   = useState(false)
  const [msg, setMsg]         = useState('')
  const [error, setError]     = useState('')
  const pollIntervalRef       = useRef(15000)

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

  // Adaptive polling: fast (3s) while in a transitional state, normal (15s) otherwise.
  useEffect(() => {
    load()
    const tick = () => load()
    let timer = setInterval(tick, pollIntervalRef.current)
    return () => clearInterval(timer)
  }, [load])

  useEffect(() => {
    const st = status?.state
    const fast = st === 'STARTING' || st === 'STOPPING'
    const next = fast ? 3000 : 15000
    if (next !== pollIntervalRef.current) {
      pollIntervalRef.current = next
    }
  }, [status?.state])

  async function doStart() {
    if (status?.trend_guard_blocked) {
      setMsg('RSI trend guard is active — wait for RSI to enter the 35–65 range before starting.')
      return
    }
    setActing(true); setMsg('Requesting activation...')
    try {
      const res = await post('/bot/grid/start')
      const message = res?.message || 'Activation requested. Scanner will start the grid within 60 seconds.'
      setMsg(message)
      // Bump to fast polling so the state flips from STANDBY -> STARTING -> ACTIVE visibly.
      pollIntervalRef.current = 3000
      await load()
    } catch (e) {
      setMsg(`Error: ${e.message}`)
    } finally {
      setActing(false)
    }
  }

  async function doStop() {
    setActing(true); setMsg('Requesting deactivation...')
    try {
      const res = await post('/bot/grid/stop')
      const message = res?.message || 'Deactivation requested. Scanner will cancel open orders within 60 seconds.'
      setMsg(message)
      pollIntervalRef.current = 3000
      await load()
    } catch (e) {
      setMsg(`Error: ${e.message}`)
    } finally {
      setActing(false)
    }
  }

  if (loading) return <div className="loading"><div className="spinner" />Loading grid status...</div>

  const uiState = status?.state ?? 'STANDBY'
  const display = STATE_DISPLAY[uiState] || STATE_DISPLAY.STANDBY
  const blocked = status?.trend_guard_blocked ?? true

  // Button enablement is driven by UI state, not the DB flag, so the buttons
  // actually do something when the user clicks them.
  //   STANDBY   — can Start (if trend guard not blocking)
  //   STARTING  — cannot Start (already in progress); Stop will cancel pending activation
  //   ACTIVE    — can Stop
  //   PAUSED    — can Stop (cancel orders); Start resumes via flag flip
  //   STOPPING  — cannot Start/Stop (already in progress)
  const canStart = !acting && uiState === 'STANDBY' && !blocked
  const canStop  = !acting && (uiState === 'ACTIVE' || uiState === 'STARTING' || uiState === 'PAUSED')

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
          <div className={`dot ${display.dot}`} />
          <span style={{ fontSize: 16, fontWeight: 700, color: display.color }}>{display.label}</span>
          <span style={{ fontSize: 13, color: 'var(--text-3)' }}>
            {status?.pair ?? '—'}
          </span>
          {uiState === 'STARTING' && (
            <span className="tag" style={{ background: 'rgba(240,180,41,0.15)', color: 'var(--amber)', marginLeft: 'auto' }}>
              scanner activating...
            </span>
          )}
          {uiState === 'STOPPING' && (
            <span className="tag" style={{ background: 'rgba(240,180,41,0.15)', color: 'var(--amber)', marginLeft: 'auto' }}>
              cancelling orders...
            </span>
          )}
        </div>

        <RsiGauge rsi={status?.rsi} />

        {blocked && uiState === 'STANDBY' && (
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
            disabled={!canStart}
            title={
              uiState === 'ACTIVE'   ? 'Grid is already active' :
              uiState === 'STARTING' ? 'Activation already in progress' :
              uiState === 'STOPPING' ? 'Wait for deactivation to complete' :
              blocked                ? 'Trend guard is blocking activation' :
              'Start the grid'
            }
          >
            {acting && uiState === 'STANDBY' ? 'Requesting...' :
             uiState === 'STARTING'           ? 'Starting...' :
             'Start grid'}
          </button>
          <button
            className="btn btn-ghost"
            onClick={doStop}
            disabled={!canStop}
            title={
              uiState === 'STANDBY'  ? 'Grid is not running' :
              uiState === 'STOPPING' ? 'Deactivation already in progress' :
              'Stop the grid and cancel open orders'
            }
          >
            {acting && uiState !== 'STANDBY' ? 'Requesting...' :
             uiState === 'STOPPING'           ? 'Stopping...' :
             'Stop grid'}
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

      {/* Range card — only shown when there's a live/recent session */}
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
    </div>
  )
}
