import { useState, useEffect } from 'react'
import { get, post } from '../lib/api.js'

function YieldRow({ platform, apy, type, best, current }) {
  const maxApy = 12 // visual scale max
  const barWidth = Math.min(100, (apy / maxApy) * 100)

  return (
    <tr>
      <td>
        <div style={{ fontWeight: 500 }}>{platform}</div>
        <div style={{ fontSize: 11, color: 'var(--text-3)' }}>{type}</div>
      </td>
      <td>
        <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
          <span className="mono" style={{
            fontSize: 16,
            fontWeight: 600,
            color: best ? 'var(--amber)' : 'var(--text)',
            minWidth: 60,
          }}>
            {apy?.toFixed(2)}%
          </span>
          <div style={{ flex: 1, maxWidth: 160 }}>
            <div className="yield-bar-track">
              <div className="yield-bar-fill" style={{ width: `${barWidth}%` }} />
            </div>
          </div>
        </div>
      </td>
      <td>
        {current && <span className="tag tag-amber">Current</span>}
        {best && !current && <span className="tag tag-green">Best</span>}
      </td>
    </tr>
  )
}

export default function Yields() {
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(true)
  const [confirming, setConfirming] = useState(false)
  const [rebalancing, setRebalancing] = useState(false)

  async function load() {
    try {
      const d = await get('/bot/yields')
      setData(d)
    } catch {}
    setLoading(false)
  }

  useEffect(() => {
    load()
    const iv = setInterval(load, 60000)
    return () => clearInterval(iv)
  }, [])

  async function confirmRebalance() {
    setRebalancing(true)
    try {
      await post('/bot/rebalance/confirm')
      await load()
    } catch (err) {
      alert(err.message)
    } finally {
      setRebalancing(false)
      setConfirming(false)
    }
  }

  if (loading) return <div className="loading"><div className="spinner" />Loading yields...</div>

  const platforms = data?.platforms ?? []
  const sorted = [...platforms].sort((a, b) => b.apy - a.apy)
  const best = sorted[0]
  const current = platforms.find(p => p.current)
  const spread = best && current ? (best.apy - current.apy).toFixed(2) : null
  const rebalanceReady = spread && parseFloat(spread) >= (data?.rebalance_threshold ?? 1.5)

  return (
    <div>
      <div className="page-title">Yields</div>
      <div className="page-sub">Stablecoin APY across all monitored platforms. Updated every hour.</div>

      {rebalanceReady && (
        <div className="card" style={{
          borderColor: 'var(--amber-dim)',
          background: 'rgba(245,166,35,0.05)',
          marginBottom: 20,
        }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 16 }}>
            <div style={{ flex: 1 }}>
              <div style={{ fontWeight: 600, marginBottom: 4 }}>Rebalance opportunity</div>
              <div style={{ fontSize: 13, color: 'var(--text-2)' }}>
                Moving from <strong style={{ color: 'var(--text)' }}>{current?.platform}</strong> ({current?.apy?.toFixed(2)}%)
                to <strong style={{ color: 'var(--amber)' }}>{best?.platform}</strong> ({best?.apy?.toFixed(2)}%)
                gains <strong style={{ color: 'var(--green)' }}>{spread}%</strong> APY.
              </div>
            </div>
            {!confirming ? (
              <button className="btn btn-amber" onClick={() => setConfirming(true)}>
                Review rebalance
              </button>
            ) : (
              <div style={{ display: 'flex', gap: 10 }}>
                <button className="btn btn-ghost" onClick={() => setConfirming(false)}>Cancel</button>
                <button className="btn btn-amber" onClick={confirmRebalance} disabled={rebalancing}>
                  {rebalancing ? 'Moving funds...' : 'Confirm rebalance'}
                </button>
              </div>
            )}
          </div>
        </div>
      )}

      <div className="card" style={{ padding: 0, overflow: 'hidden' }}>
        <table className="table">
          <thead>
            <tr>
              <th>Platform</th>
              <th>APY</th>
              <th>Status</th>
            </tr>
          </thead>
          <tbody>
            {sorted.map(p => (
              <YieldRow
                key={p.platform}
                platform={p.platform}
                apy={p.apy}
                type={p.type}
                best={p.platform === best?.platform}
                current={p.current}
              />
            ))}
          </tbody>
        </table>
      </div>

      {data?.last_updated && (
        <div style={{ marginTop: 12, fontSize: 12, color: 'var(--text-3)', fontFamily: 'var(--mono)' }}>
          Last updated {new Date(data.last_updated * 1000).toLocaleTimeString()}
        </div>
      )}
    </div>
  )
}
