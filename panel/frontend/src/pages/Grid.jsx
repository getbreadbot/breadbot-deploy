import { useState, useEffect } from 'react'
import { get, post } from '../lib/api.js'

function GridLadder({ upper, lower, entry, levels }) {
  if (!upper || !lower || !entry) return null
  const step = (upper - lower) / Math.max(levels - 1, 1)
  const gridLevels = []
  for (let i = 0; i < levels; i++) gridLevels.push(lower + i * step)
  const displayed = gridLevels.length > 10
    ? [...gridLevels.slice(0, 5), null, ...gridLevels.slice(-5)]
    : gridLevels
  return (
    <div style={{ fontFamily: 'var(--mono)', fontSize: 12, marginTop: 12 }}>
      {[...displayed].reverse().map((level, idx) => {
        if (level === null) return <div key="e" style={{ color: 'var(--text-3)', padding: '2px 0', textAlign: 'center' }}>···</div>
        const isAbove = level > entry
        const isEntry = Math.abs(level - entry) < step * 0.1
        return (
          <div key={idx} style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '3px 0', borderBottom: '1px solid var(--border)', opacity: isEntry ? 1 : 0.75 }}>
            <span style={{ width: 8, height: 8, borderRadius: '50%', background: isEntry ? 'var(--amber)' : (isAbove ? 'var(--green-dim)' : 'var(--text-3)'), flexShrink: 0 }} />
            <span style={{ flex: 1, color: isEntry ? 'var(--amber)' : 'var(--text-2)' }}>
              ${level.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
            </span>
            <span style={{ fontSize: 10, color: 'var(--text-3)' }}>{isEntry ? '← entry' : (isAbove ? 'SELL' : 'BUY')}</span>
          </div>
        )
      })}
    </div>
  )
}

export default function Grid() {
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(true)
  const [acting, setActing] = useState(false)
  const [error, setError] = useState(null)

  async function load() {
    try { const d = await get('/bot/grid/status'); setData(d); setError(null) }
    catch { setError('Could not reach bot.') }
    setLoading(false)
  }

  useEffect(() => { load(); const iv = setInterval(load, 30000); return () => clearInterval(iv) }, [])

  async function sendCommand(cmd) {
    setActing(true)
    try { const r = await post(`/bot/grid/${cmd}`); if (r.error) { alert(r.error); return }; await load() }
    catch (e) { alert(e.message) }
    finally { setActing(false) }
  }

  if (loading) return <div className="loading"><div className="spinner" />Loading grid engine...</div>

  const state = data?.state ?? 'STANDBY'
  const enabled = data?.enabled ?? false
  const blocked = data?.trend_guard_blocked ?? false
  const rsi = data?.rsi
  const isActive = state === 'ACTIVE'

  return (
    <div>
      <div className="page-title">Grid Trading</div>
      <div className="page-sub">Automated buy/sell ladder on {data?.pair ?? 'BTC/USDT'}. Profits from price oscillation within a range.</div>

      {error && <div className="card" style={{ borderColor: 'rgba(239,68,68,0.3)', background: 'rgba(239,68,68,0.05)', marginBottom: 16 }}><span style={{ color: '#f87171' }}>{error}</span></div>}

      {!enabled && (
        <div className="card" style={{ borderColor: 'var(--amber-dim)', background: 'rgba(245,166,35,0.05)', marginBottom: 16, fontSize: 13, color: 'var(--text-2)' }}>
          Grid trading is disabled. Go to <strong>Settings → Strategy activation</strong> to enable it.
        </div>
      )}

      <div className="card" style={{ marginBottom: 16 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 20, flexWrap: 'wrap' }}>
          <div style={{ flex: 1 }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 12 }}>
              <div style={{ padding: '3px 10px', borderRadius: 20, fontSize: 12, fontWeight: 600, background: isActive ? 'rgba(74,222,128,0.12)' : 'rgba(245,166,35,0.1)', color: isActive ? 'var(--green)' : 'var(--amber)', border: `1px solid ${isActive ? 'rgba(74,222,128,0.2)' : 'var(--amber-dim)'}` }}>{state}</div>
              {blocked && <div style={{ padding: '3px 10px', borderRadius: 20, fontSize: 12, background: 'rgba(239,68,68,0.08)', color: '#f87171', border: '1px solid rgba(239,68,68,0.2)' }}>Trend guard — RSI {rsi}</div>}
            </div>
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(130px, 1fr))', gap: 12 }}>
              {[{ label: 'Pair', value: data?.pair ?? '—' }, { label: 'Allocation', value: data?.allocation_usd ? `$${data.allocation_usd.toLocaleString()}` : '—' }, { label: 'Grid levels', value: data?.num_levels ?? '—' }, { label: 'RSI (4h)', value: rsi ?? '—' }, { label: 'Cycles done', value: data?.cycles_completed ?? 0 }, { label: 'Profit', value: `$${(data?.total_profit_usd ?? 0).toFixed(4)}` }].map(({ label, value }) => (
                <div key={label}>
                  <div style={{ fontSize: 11, color: 'var(--text-3)', textTransform: 'uppercase', letterSpacing: '0.06em', marginBottom: 2 }}>{label}</div>
                  <div className="mono" style={{ fontSize: 14, fontWeight: 500 }}>{value}</div>
                </div>
              ))}
            </div>
          </div>
          {enabled && (
            <div style={{ flexShrink: 0 }}>
              {!isActive
                ? <button className="btn btn-green" onClick={() => sendCommand('start')} disabled={acting || blocked} title={blocked ? `RSI ${rsi} outside 35–65. Wait for neutral trend.` : ''}>{acting ? 'Starting...' : 'Start grid'}</button>
                : <button className="btn btn-amber" onClick={() => sendCommand('stop')} disabled={acting}>{acting ? 'Stopping...' : 'Stop grid'}</button>}
            </div>
          )}
        </div>
      </div>

      {isActive && data?.upper_bound && (
        <div className="card" style={{ marginBottom: 16 }}>
          <div className="card-title">Price ladder</div>
          <div style={{ fontSize: 12, color: 'var(--text-2)', marginBottom: 4 }}>Range: ${data.lower_bound?.toLocaleString()} – ${data.upper_bound?.toLocaleString()} | Entry: ${data.entry_price?.toLocaleString()}</div>
          <GridLadder upper={data.upper_bound} lower={data.lower_bound} entry={data.entry_price} levels={data.num_levels} />
        </div>
      )}

      <div className="card" style={{ fontSize: 13, color: 'var(--text-2)' }}>
        <div className="card-title" style={{ marginBottom: 8 }}>How it works</div>
        <p style={{ margin: '0 0 10px' }}>The grid engine places buy and sell limit orders at equal intervals across a defined price range. Each time price moves up through a level the bot sells; when it moves down the bot buys. Every completed buy→sell cycle captures the grid spacing as profit.</p>
        <p style={{ margin: 0 }}>The trend guard checks 4-hour RSI before activation. If RSI is outside 35–65 the grid stays in standby. Strong directional moves cause losses in grid strategies — the guard prevents activation in those conditions. Allocation: <strong style={{ color: 'var(--text)' }}>${data?.allocation_usd?.toLocaleString() ?? 500}</strong> across <strong style={{ color: 'var(--text)' }}>{data?.num_levels ?? 20} levels</strong> on Binance.US. Adjust in Settings → Advanced.</p>
      </div>
    </div>
  )
}
