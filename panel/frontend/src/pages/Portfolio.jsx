import { useState, useEffect } from 'react'
import { get } from '../lib/api.js'

export default function Portfolio() {
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)

  async function load() {
    try {
      const d = await get('/bot/portfolio')
      setData(d)
      setError(null)
    } catch (e) {
      setError(e.message)
    }
    setLoading(false)
  }

  useEffect(() => {
    load()
    const iv = setInterval(load, 60000)
    return () => clearInterval(iv)
  }, [])

  if (loading) return <div className="loading"><div className="spinner" />Loading portfolio...</div>
  if (error) return <div className="card" style={{ color: 'var(--red)' }}>Error: {error}</div>
  if (!data) return null

  const totalWallet = data.balances.reduce((s, b) => s + (b.value_usd || 0), 0)

  return (
    <div>
      <div className="page-title">Portfolio</div>
      <div className="page-sub">Live wallet balances across all chains</div>

      <div className="grid-3" style={{ marginBottom: 24 }}>
        <div className="card">
          <div className="card-title">Total portfolio</div>
          <div className="stat-value">${data.total_usd.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}</div>
        </div>
        <div className="card">
          <div className="card-title">Wallet balances</div>
          <div className="stat-value">${totalWallet.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}</div>
        </div>
        <div className="card">
          <div className="card-title">Open positions</div>
          <div className="stat-value">
            {data.open_positions_count}
            {data.open_positions_value > 0 && (
              <span style={{ fontSize: 14, color: 'var(--text-2)', marginLeft: 8 }}>
                (${data.open_positions_value.toFixed(2)})
              </span>
            )}
          </div>
        </div>
      </div>

      <div className="card" style={{ padding: 0, overflow: 'hidden' }}>
        <table className="table">
          <thead>
            <tr>
              <th>Asset</th>
              <th>Chain</th>
              <th>Amount</th>
              <th>Price</th>
              <th>Value</th>
              <th>Wallet</th>
            </tr>
          </thead>
          <tbody>
            {data.balances.map((b, i) => (
              <tr key={i}>
                <td>
                  <span style={{ fontWeight: 600 }}>{b.asset}</span>
                </td>
                <td>
                  <span className="tag tag-gray">{b.chain}</span>
                </td>
                <td className="mono">
                  {b.asset === 'USDC'
                    ? `$${b.amount.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
                    : b.amount.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 6 })}
                </td>
                <td className="mono">
                  {b.price_usd === 1 ? '$1.00' : `$${b.price_usd.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`}
                </td>
                <td className="mono" style={{ fontWeight: 600 }}>
                  ${b.value_usd.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
                </td>
                <td className="mono" style={{ fontSize: 11, color: 'var(--text-3)' }}>
                  {b.wallet}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div style={{ marginTop: 12, fontSize: 12, color: 'var(--text-3)', textAlign: 'right' }}>
        Refreshes every 60 seconds
      </div>
    </div>
  )
}
