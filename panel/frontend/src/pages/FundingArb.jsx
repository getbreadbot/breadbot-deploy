import { useState, useEffect } from 'react'
import { get } from '../lib/api.js'

function RateBar({ rate, threshold, max = 0.05 }) {
  const pct = Math.min(100, (Math.abs(rate) / max) * 100)
  const aboveThreshold = rate >= threshold
  const isPositive = rate >= 0
  return (
    <div style={{ flex: 1, height: 8, background: 'var(--bg-2)', borderRadius: 4, overflow: 'hidden' }}>
      <div style={{ width: `${pct}%`, height: '100%', borderRadius: 4, background: aboveThreshold ? 'var(--green)' : (isPositive ? 'var(--amber)' : '#f87171'), transition: 'width 0.4s ease' }} />
    </div>
  )
}

export default function FundingArb() {
  const [rates, setRates] = useState(null)
  const [positions, setPositions] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)

  async function load() {
    try {
      const [r, p] = await Promise.all([get('/bot/funding/rates'), get('/bot/funding/positions')])
      setRates(r); setPositions(p); setError(null)
    } catch { setError('Could not reach bot.') }
    setLoading(false)
  }

  useEffect(() => { load(); const iv = setInterval(load, 60000); return () => clearInterval(iv) }, [])

  if (loading) return <div className="loading"><div className="spinner" />Loading funding rates...</div>

  const enabled = rates?.arb_enabled ?? false
  const threshold = rates?.entry_threshold_pct ?? 0.01
  const rateList = rates?.rates ?? []
  const openPositions = positions?.positions ?? []

  return (
    <div>
      <div className="page-title">Funding Rate Arb</div>
      <div className="page-sub">Market-neutral. Long spot + short perp = collect funding payments with zero price exposure.</div>

      {error && <div className="card" style={{ borderColor: 'rgba(239,68,68,0.3)', background: 'rgba(239,68,68,0.05)', marginBottom: 16 }}><span style={{ color: '#f87171' }}>{error}</span></div>}

      {!enabled && (
        <div className="card" style={{ borderColor: 'var(--amber-dim)', background: 'rgba(245,166,35,0.05)', marginBottom: 16, fontSize: 13, color: 'var(--text-2)' }}>
          Funding arb is disabled. Go to <strong>Settings → Strategy activation</strong> to enable it. Requires a funded Bybit account.
        </div>
      )}

      <div className="card" style={{ marginBottom: 16 }}>
        <div className="card-title">Live funding rates</div>
        <div style={{ fontSize: 12, color: 'var(--text-3)', marginBottom: 16 }}>Entry threshold: {threshold}%/8h ({(threshold * 3 * 365).toFixed(1)}% ann.)</div>
        {rateList.length === 0
          ? <div style={{ color: 'var(--text-3)', fontSize: 13 }}>No rate data. Enable and connect Bybit.</div>
          : rateList.map(r => (
            <div key={r.pair} style={{ marginBottom: 16 }}>
              <div style={{ display: 'flex', alignItems: 'baseline', gap: 12, marginBottom: 6 }}>
                <span style={{ fontWeight: 600 }}>{r.pair}/USDT</span>
                <span className="mono" style={{ fontSize: 20, fontWeight: 700, color: r.above_entry ? 'var(--green)' : 'var(--text)' }}>
                  {r.rate_8h_pct >= 0 ? '+' : ''}{r.rate_8h_pct.toFixed(4)}%<span style={{ fontSize: 12, fontWeight: 400, color: 'var(--text-3)' }}>/8h</span>
                </span>
                <span style={{ fontSize: 13, color: 'var(--text-3)' }}>{r.annualized_pct.toFixed(1)}% ann.</span>
                {r.above_entry && <span className="tag tag-green">Above threshold</span>}
              </div>
              <div style={{ display: 'flex', gap: 12, alignItems: 'center' }}>
                <RateBar rate={r.rate_8h_pct} threshold={threshold} />
              </div>
            </div>
          ))
        }
      </div>

      <div className="card" style={{ marginBottom: 16 }}>
        <div style={{ display: 'flex', alignItems: 'center', marginBottom: 12 }}>
          <div className="card-title" style={{ marginBottom: 0, flex: 1 }}>Open positions</div>
          {(positions?.total_funding_collected_usd ?? 0) > 0 && <span style={{ fontSize: 13, color: 'var(--green)', fontWeight: 600 }}>+${positions.total_funding_collected_usd.toFixed(4)} collected</span>}
        </div>
        {openPositions.length === 0
          ? <div style={{ color: 'var(--text-3)', fontSize: 13 }}>{enabled ? 'Waiting for rate above threshold.' : 'Enable funding arb to start.'}</div>
          : <table className="table"><thead><tr><th>Pair</th><th>Entry</th><th>Qty</th><th>Funding collected</th><th>Opened</th></tr></thead>
            <tbody>{openPositions.map(p => (
              <tr key={p.id}>
                <td className="mono">{p.pair}/USDT</td>
                <td className="mono">${p.entry_price?.toLocaleString()}</td>
                <td className="mono">{p.quantity?.toFixed(6)}</td>
                <td className="mono" style={{ color: 'var(--green)' }}>+${p.funding_collected?.toFixed(4)}</td>
                <td style={{ fontSize: 12, color: 'var(--text-3)' }}>{p.opened_at?.slice(0, 10)}</td>
              </tr>
            ))}</tbody></table>
        }
      </div>

      <div className="card" style={{ fontSize: 13, color: 'var(--text-2)' }}>
        <div className="card-title" style={{ marginBottom: 8 }}>How it works</div>
        <p style={{ margin: '0 0 10px' }}>Perpetual futures pay funding every 8 hours to balance longs and shorts. When longs outnumber shorts, longs pay shorts. Holding long spot alongside short perp cancels price exposure — only funding income remains.</p>
        <p style={{ margin: 0 }}>Engine monitors BTC and ETH. When 8h rate exceeds <strong style={{ color: 'var(--text)' }}>{threshold}%</strong> (≈{(threshold * 3 * 365).toFixed(0)}% ann.) it opens a pair trade automatically and closes when rates drop below exit threshold. Risk: negative funding rates. Bot closes before that happens. Recommended test allocation: <strong style={{ color: 'var(--text)' }}>$1,000</strong>.</p>
      </div>
    </div>
  )
}
