import { useState, useEffect } from 'react'
import { get } from '../lib/api.js'

function StatCard({ label, value, sub, positive }) {
  return (
    <div style={{ background: 'var(--bg-2)', borderRadius: 'var(--radius)', border: '1px solid var(--border)', padding: '14px 18px' }}>
      <div style={{ fontSize: 11, color: 'var(--text-3)', textTransform: 'uppercase', letterSpacing: '0.07em', marginBottom: 6 }}>{label}</div>
      <div className="mono" style={{ fontSize: 22, fontWeight: 700, color: positive === true ? 'var(--green)' : positive === false ? '#f87171' : 'var(--text)' }}>{value}</div>
      {sub && <div style={{ fontSize: 12, color: 'var(--text-3)', marginTop: 4 }}>{sub}</div>}
    </div>
  )
}

export default function Performance() {
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)

  useEffect(() => {
    async function load() {
      try { const d = await get('/bot/strategy/performance'); setData(d); setError(null) }
      catch { setError('Could not reach bot.') }
      setLoading(false)
    }
    load()
  }, [])

  if (loading) return <div className="loading"><div className="spinner" />Loading performance...</div>

  const grid = data?.grid ?? {}
  const arb = data?.funding_arb ?? {}
  const rebalancer = data?.yield_rebalancer ?? {}
  const days = data?.period_days ?? 30
  const totalIncome = (grid.profit_usd ?? 0) + (arb.funding_collected_usd ?? 0) + (rebalancer.yield_gained_usd ?? 0)

  return (
    <div>
      <div className="page-title">Strategy Performance</div>
      <div className="page-sub">Last {days} days across all three engines. Data accumulates as strategies run.</div>

      {error && <div className="card" style={{ borderColor: 'rgba(239,68,68,0.3)', background: 'rgba(239,68,68,0.05)', marginBottom: 16 }}><span style={{ color: '#f87171' }}>{error}</span></div>}

      <div className="card" style={{ marginBottom: 16 }}>
        <div className="card-title">Combined — last {days} days</div>
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(150px, 1fr))', gap: 12 }}>
          <StatCard label="Total income" value={`$${totalIncome.toFixed(4)}`} sub="All strategies" positive={totalIncome > 0} />
          <StatCard label="Grid cycles" value={grid.cycles ?? 0} sub={`$${(grid.profit_usd ?? 0).toFixed(4)} profit`} positive={(grid.profit_usd ?? 0) > 0} />
          <StatCard label="Funding collected" value={`$${(arb.funding_collected_usd ?? 0).toFixed(4)}`} sub={`${arb.open_positions ?? 0} open`} positive={(arb.funding_collected_usd ?? 0) > 0} />
          <StatCard label="Yield rebalances" value={rebalancer.rebalances ?? 0} sub={`$${(rebalancer.yield_gained_usd ?? 0).toFixed(4)} gained`} positive={(rebalancer.yield_gained_usd ?? 0) > 0} />
        </div>
      </div>

      <div className="card" style={{ marginBottom: 16 }}>
        <div style={{ display: 'flex', alignItems: 'center', marginBottom: 12 }}>
          <div className="card-title" style={{ marginBottom: 0, flex: 1 }}>Grid Trading</div>
          <span style={{ fontSize: 12, color: 'var(--text-3)' }}>BTC/USDT · Binance.US</span>
        </div>
        {(grid.cycles ?? 0) === 0
          ? <div style={{ fontSize: 13, color: 'var(--text-3)' }}>No cycles yet. Enable grid and wait for RSI 35–65 to activate.</div>
          : <div style={{ display: 'grid', gridTemplateColumns: 'repeat(3, 1fr)', gap: 12 }}>
              <StatCard label="Cycles" value={grid.cycles} />
              <StatCard label="Profit" value={`$${(grid.profit_usd ?? 0).toFixed(4)}`} positive={(grid.profit_usd ?? 0) > 0} />
              <StatCard label="Volume" value={`$${(grid.volume_usd ?? 0).toFixed(0)}`} />
            </div>
        }
      </div>

      <div className="card" style={{ marginBottom: 16 }}>
        <div style={{ display: 'flex', alignItems: 'center', marginBottom: 12 }}>
          <div className="card-title" style={{ marginBottom: 0, flex: 1 }}>Funding Rate Arb</div>
          <span style={{ fontSize: 12, color: 'var(--text-3)' }}>BTC · ETH · Bybit</span>
        </div>
        {(arb.funding_collected_usd ?? 0) === 0 && (arb.open_positions ?? 0) === 0
          ? <div style={{ fontSize: 13, color: 'var(--text-3)' }}>No arb activity yet. Enable and fund Bybit to start.</div>
          : <div style={{ display: 'grid', gridTemplateColumns: 'repeat(3, 1fr)', gap: 12 }}>
              <StatCard label="Open positions" value={arb.open_positions ?? 0} />
              <StatCard label="Funding collected" value={`$${(arb.funding_collected_usd ?? 0).toFixed(4)}`} positive={(arb.funding_collected_usd ?? 0) > 0} />
              <StatCard label="Closed PnL" value={`$${(arb.closed_pnl_usd ?? 0).toFixed(4)}`} positive={(arb.closed_pnl_usd ?? 0) > 0} />
            </div>
        }
      </div>

      <div className="card" style={{ marginBottom: 16 }}>
        <div style={{ display: 'flex', alignItems: 'center', marginBottom: 12 }}>
          <div className="card-title" style={{ marginBottom: 0, flex: 1 }}>Yield Rebalancer</div>
          <span style={{ fontSize: 12, color: 'var(--text-3)' }}>USDC · Multi-platform</span>
        </div>
        {(rebalancer.rebalances ?? 0) === 0
          ? <div style={{ fontSize: 13, color: 'var(--text-3)' }}>No rebalances yet. Enable the rebalancer in Settings.</div>
          : <div style={{ display: 'grid', gridTemplateColumns: 'repeat(2, 1fr)', gap: 12 }}>
              <StatCard label="Rebalances" value={rebalancer.rebalances} />
              <StatCard label="Yield gained" value={`$${(rebalancer.yield_gained_usd ?? 0).toFixed(4)}`} positive={(rebalancer.yield_gained_usd ?? 0) > 0} />
            </div>
        }
      </div>

      <div style={{ fontSize: 12, color: 'var(--text-3)', fontFamily: 'var(--mono)', marginTop: 8 }}>
        Live database. Refreshes on page load. All figures USD.
      </div>
    </div>
  )
}
