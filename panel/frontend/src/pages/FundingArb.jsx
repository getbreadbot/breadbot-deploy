import { useState, useEffect, useCallback } from 'react'
import { get } from '../lib/api.js'

const VENUE_COLORS = {
  green: { dot: 'green', bg: 'rgba(16,185,129,0.08)', border: 'rgba(16,185,129,0.25)' },
  amber: { dot: 'amber', bg: 'rgba(240,180,41,0.08)', border: 'rgba(240,180,41,0.25)' },
  red:   { dot: 'red',   bg: 'rgba(239,68,68,0.08)',  border: 'rgba(239,68,68,0.25)' },
}

function VenueBadge({ label, color, legalUs }) {
  const c = VENUE_COLORS[color] || VENUE_COLORS.amber
  const legalText = legalUs === true ? 'US legal' : legalUs === false ? 'US geo-blocked' : 'DEX — user responsibility'
  const legalColor = legalUs === true ? 'var(--green)' : legalUs === false ? 'var(--red, #ef4444)' : 'var(--amber)'
  return (
    <div style={{
      display: 'inline-flex', alignItems: 'center', gap: 10,
      background: c.bg, border: `1px solid ${c.border}`,
      borderRadius: 8, padding: '10px 16px',
    }}>
      <div className={`dot ${c.dot}`} style={{ width: 10, height: 10 }} />
      <span style={{ fontWeight: 600, fontSize: 15 }}>{label}</span>
      <span style={{ fontSize: 12, color: legalColor, marginLeft: 4 }}>{legalText}</span>
    </div>
  )
}

function RateRow({ r, threshold }) {
  const above = r.above_entry
  return (
    <tr>
      <td style={{ fontWeight: 600 }}>{r.pair}/USDT</td>
      <td className="mono" style={{ color: r.rate_8h_pct >= 0 ? 'var(--green)' : 'var(--red, #ef4444)' }}>
        {r.rate_8h_pct >= 0 ? '+' : ''}{r.rate_8h_pct.toFixed(4)}%
      </td>
      <td className="mono">{r.annualized_pct.toFixed(2)}%</td>
      <td>
        {above
          ? <span className="tag tag-green">Above threshold</span>
          : <span className="tag" style={{ background: 'var(--bg-3)', color: 'var(--text-3)' }}>Below threshold</span>
        }
      </td>
    </tr>
  )
}

function PositionRow({ p }) {
  const pnl = p.funding_collected - 0
  return (
    <tr>
      <td style={{ fontWeight: 600 }}>{p.pair}/USDT</td>
      <td className="mono">${p.entry_price?.toLocaleString('en-US', { maximumFractionDigits: 2 }) ?? '—'}</td>
      <td className="mono">{p.quantity?.toFixed(6) ?? '—'}</td>
      <td className="mono" style={{ color: 'var(--green)' }}>
        ${p.funding_collected?.toFixed(4) ?? '0.0000'}
      </td>
      <td style={{ color: 'var(--text-3)', fontSize: 12 }}>{p.opened_at?.slice(0, 10) ?? '—'}</td>
    </tr>
  )
}

export default function FundingArb() {
  const [rates, setRates]         = useState(null)
  const [positions, setPositions] = useState(null)
  const [loading, setLoading]     = useState(true)
  const [error, setError]         = useState('')

  const load = useCallback(async () => {
    try {
      const [r, p] = await Promise.all([
        get('/bot/funding/rates'),
        get('/bot/funding/positions'),
      ])
      setRates(r)
      setPositions(p)
      setError('')
    } catch (e) {
      setError(e.message)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    load()
    const iv = setInterval(load, 60000)
    return () => clearInterval(iv)
  }, [load])

  if (loading) return <div className="loading"><div className="spinner" />Loading funding data...</div>

  return (
    <div>
      <div className="page-title">Funding Rate Arbitrage</div>
      <div className="page-sub">
        Market-neutral strategy — long spot + short perpetual. Collects funding payments when rates are positive.
      </div>

      {/* Venue indicator */}
      <div className="card" style={{ marginBottom: 16 }}>
        <div className="card-title">Active venue</div>

        {rates && !rates.error ? (
          <>
            <div style={{ marginBottom: 14 }}>
              <VenueBadge
                label={rates.venue_label ?? rates.arb_exchange ?? 'Unknown'}
                color={rates.venue_color ?? 'amber'}
                legalUs={rates.venue_legal_us}
              />
            </div>

            {/* CFM recommendation */}
            {rates.venue_legal_us !== true && (
              <div style={{
                padding: '10px 14px',
                background: 'rgba(16,185,129,0.05)',
                border: '1px solid rgba(16,185,129,0.2)',
                borderRadius: 6,
                fontSize: 13,
                color: 'var(--text-2)',
                marginBottom: 10,
              }}>
                <span style={{ color: 'var(--green)', fontWeight: 600 }}>Coinbase CFM</span>
                {' '}is the recommended venue for US residents — CFTC-regulated, no geo-block.
                Set <code style={{ fontSize: 11, background: 'var(--bg-3)', padding: '1px 5px', borderRadius: 3 }}>FUNDING_ARB_EXCHANGE=coinbase_cfm</code> in
                Settings → Advanced to switch.
              </div>
            )}

            <div style={{ display: 'flex', gap: 24, flexWrap: 'wrap', fontSize: 13, color: 'var(--text-3)', marginTop: 8 }}>
              <span>Strategy: <strong style={{ color: 'var(--text-1)' }}>{rates.arb_enabled ? 'Enabled' : 'Disabled'}</strong></span>
              <span>Entry threshold: <strong style={{ color: 'var(--text-1)' }}>{rates.entry_threshold_pct}%/8h</strong></span>
              <span>Exit threshold: <strong style={{ color: 'var(--text-1)' }}>{rates.exit_threshold_pct}%/8h</strong></span>
              <span>Entry annualised: <strong style={{ color: 'var(--text-1)' }}>
                {(rates.entry_threshold_pct * 3 * 365).toFixed(1)}%/yr
              </strong></span>
            </div>

            {!rates.arb_enabled && (
              <div style={{ marginTop: 12, fontSize: 13, color: 'var(--amber)' }}>
                Strategy is disabled. Enable via Settings → Advanced → FUNDING_ARB_ENABLED.
              </div>
            )}
          </>
        ) : (
          <div style={{ color: 'var(--text-3)', fontSize: 13 }}>
            {error || 'Unable to reach MCP server.'}
          </div>
        )}
      </div>

      {/* Funding rates */}
      <div className="card" style={{ marginBottom: 16 }}>
        <div className="card-title">Current funding rates</div>
        {rates?.rates?.length > 0 ? (
          <table className="table" style={{ width: '100%' }}>
            <thead>
              <tr>
                <th>Pair</th>
                <th>Rate / 8h</th>
                <th>Annualised</th>
                <th>Status</th>
              </tr>
            </thead>
            <tbody>
              {rates.rates.map(r => (
                <RateRow key={r.pair} r={r} threshold={rates.entry_threshold_pct} />
              ))}
            </tbody>
          </table>
        ) : (
          <div style={{ color: 'var(--text-3)', fontSize: 13 }}>
            {rates?.error
              ? `Error fetching rates: ${rates.error}`
              : 'No rate data available. Rates fetch every 60 seconds.'}
          </div>
        )}

        {/* Bear market context note */}
        <div style={{
          marginTop: 14, padding: '10px 14px',
          background: 'var(--bg-2)', borderRadius: 6,
          fontSize: 12, color: 'var(--text-3)', lineHeight: 1.6,
        }}>
          In bear markets, funding rates compress to 2–5% annualised. Current Morpho USDC yield (8.28%) 
          likely outperforms. Infrastructure is ready — deploy capital when BTC rates sustain above 
          {' '}{rates?.entry_threshold_pct ?? 0.01}%/8h ({((rates?.entry_threshold_pct ?? 0.01) * 3 * 365).toFixed(0)}%/yr).
        </div>
      </div>

      {/* Open positions */}
      <div className="card">
        <div className="card-title">
          Open arb positions
          {positions?.count > 0 && (
            <span className="badge" style={{ marginLeft: 8 }}>{positions.count}</span>
          )}
        </div>

        {positions?.positions?.length > 0 ? (
          <>
            <table className="table" style={{ width: '100%', marginBottom: 12 }}>
              <thead>
                <tr>
                  <th>Pair</th>
                  <th>Entry price</th>
                  <th>Quantity</th>
                  <th>Funding collected</th>
                  <th>Opened</th>
                </tr>
              </thead>
              <tbody>
                {positions.positions.map(p => (
                  <PositionRow key={p.id} p={p} />
                ))}
              </tbody>
            </table>
            <div style={{ fontSize: 13, color: 'var(--text-2)' }}>
              Total funding collected:{' '}
              <strong style={{ color: 'var(--green)' }}>
                ${positions.total_funding_collected_usd?.toFixed(4)}
              </strong>
            </div>
          </>
        ) : (
          <div style={{ color: 'var(--text-3)', fontSize: 13 }}>
            No open positions. The engine opens a pair trade when the funding rate exceeds the entry threshold.
          </div>
        )}
      </div>
    </div>
  )
}
