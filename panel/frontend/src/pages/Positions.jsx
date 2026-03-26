import { useState, useEffect } from 'react'
import { get, post } from '../lib/api.js'

function ConfirmModal({ position, onConfirm, onCancel }) {
  return (
    <div className="modal-backdrop" onClick={onCancel}>
      <div className="modal" onClick={e => e.stopPropagation()}>
        <div className="modal-title">Close position?</div>
        <div className="modal-body">
          This will place a market sell order for <strong>{position.token}</strong> on {position.chain}.<br /><br />
          Current P&L: <span style={{ color: position.pnl_usd >= 0 ? 'var(--green)' : 'var(--red)' }}>
            {position.pnl_usd >= 0 ? '+' : ''}${position.pnl_usd?.toFixed(2)}
          </span><br />
          This action cannot be undone.
        </div>
        <div className="modal-actions">
          <button className="btn btn-ghost" onClick={onCancel}>Cancel</button>
          <button className="btn btn-red" onClick={onConfirm}>Close position</button>
        </div>
      </div>
    </div>
  )
}

export default function Positions() {
  const [positions, setPositions] = useState([])
  const [loading, setLoading] = useState(true)
  const [closing, setClosing] = useState(null) // position being confirmed

  async function load() {
    try {
      const data = await get('/bot/positions')
      setPositions(data?.positions ?? [])
    } catch {}
    setLoading(false)
  }

  useEffect(() => {
    load()
    const iv = setInterval(load, 20000)
    return () => clearInterval(iv)
  }, [])

  async function closePosition(position) {
    try {
      await post('/bot/positions/close', { position_id: position.id })
      setPositions(prev => prev.filter(p => p.id !== position.id))
    } catch (err) {
      alert(err.message)
    } finally {
      setClosing(null)
    }
  }

  const totalPnl = positions.reduce((sum, p) => sum + (p.pnl_usd ?? 0), 0)
  const totalValue = positions.reduce((sum, p) => sum + (p.value_usd ?? 0), 0)

  if (loading) return <div className="loading"><div className="spinner" />Loading positions...</div>

  return (
    <div>
      <div className="page-title">Positions</div>
      <div className="page-sub">Open meme coin positions</div>

      {positions.length > 0 && (
        <div className="grid-3" style={{ marginBottom: 20 }}>
          <div className="card">
            <div className="card-title">Open positions</div>
            <div className="stat-value">{positions.length}</div>
          </div>
          <div className="card">
            <div className="card-title">Total value</div>
            <div className="stat-value">${totalValue.toFixed(2)}</div>
          </div>
          <div className="card">
            <div className="card-title">Unrealized P&L</div>
            <div className={`stat-value ${totalPnl >= 0 ? 'green' : 'red'}`}>
              {totalPnl >= 0 ? '+' : ''}${totalPnl.toFixed(2)}
            </div>
          </div>
        </div>
      )}

      {positions.length === 0 ? (
        <div className="card empty">
          <div className="empty-icon">◈</div>
          No open positions.
        </div>
      ) : (
        <div className="card" style={{ padding: 0, overflow: 'hidden' }}>
          <table className="table">
            <thead>
              <tr>
                <th>Token</th>
                <th>Chain</th>
                <th>Entry</th>
                <th>Current</th>
                <th>Stop loss</th>
                <th>Value</th>
                <th>P&L</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {positions.map(p => {
                const pnlPct = p.entry_price && p.current_price
                  ? ((p.current_price - p.entry_price) / p.entry_price * 100)
                  : null

                return (
                  <tr key={p.id}>
                    <td>
                      <div className="mono" style={{ fontWeight: 600 }}>{p.token}</div>
                      {p.contract && (
                        <div style={{ fontSize: 10, color: 'var(--text-3)', fontFamily: 'var(--mono)' }}>
                          {p.contract.slice(0, 8)}…
                        </div>
                      )}
                    </td>
                    <td><span className="tag tag-gray">{p.chain}</span></td>
                    <td className="mono">${p.entry_price?.toFixed(8) ?? '—'}</td>
                    <td className="mono">${p.current_price?.toFixed(8) ?? '—'}</td>
                    <td className="mono" style={{ color: 'var(--red)' }}>
                      ${p.stop_loss?.toFixed(8) ?? '—'}
                    </td>
                    <td className="mono">${p.value_usd?.toFixed(2) ?? '—'}</td>
                    <td>
                      <span className={`mono ${p.pnl_usd >= 0 ? '' : ''}`}
                        style={{ color: p.pnl_usd >= 0 ? 'var(--green)' : 'var(--red)' }}
                      >
                        {p.pnl_usd >= 0 ? '+' : ''}${p.pnl_usd?.toFixed(2) ?? '—'}
                        {pnlPct != null && (
                          <span style={{ fontSize: 11, marginLeft: 4 }}>
                            ({pnlPct >= 0 ? '+' : ''}{pnlPct.toFixed(1)}%)
                          </span>
                        )}
                      </span>
                    </td>
                    <td>
                      <button
                        className="btn btn-ghost btn-sm"
                        onClick={() => setClosing(p)}
                        style={{ color: 'var(--red)', borderColor: 'rgba(239,68,68,0.3)' }}
                      >
                        Close
                      </button>
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      )}

      {closing && (
        <ConfirmModal
          position={closing}
          onConfirm={() => closePosition(closing)}
          onCancel={() => setClosing(null)}
        />
      )}
    </div>
  )
}
