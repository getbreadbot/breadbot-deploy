import { useState, useEffect } from 'react'
import { get, post } from '../lib/api.js'
import PriceChart from '../components/PriceChart.jsx'

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
  const [tab, setTab] = useState('open')
  const [positions, setPositions] = useState([])
  const [history, setHistory] = useState([])
  const [loading, setLoading] = useState(true)
  const [historyLoading, setHistoryLoading] = useState(false)
  const [closing, setClosing] = useState(null)
  const [expandedId, setExpandedId] = useState(null)

  async function loadOpen() {
    try {
      const data = await get('/bot/positions')
      setPositions(data?.positions ?? [])
    } catch {}
    setLoading(false)
  }

  async function loadHistory() {
    setHistoryLoading(true)
    try {
      const data = await get('/bot/positions/history')
      setHistory(data?.positions ?? [])
    } catch {}
    setHistoryLoading(false)
  }

  useEffect(() => {
    loadOpen()
    const iv = setInterval(loadOpen, 20000)
    return () => clearInterval(iv)
  }, [])

  useEffect(() => {
    if (tab === 'history' && history.length === 0) loadHistory()
  }, [tab])

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
      <div className="page-sub">Open positions and trade history</div>

      {/* Tab switcher */}
      <div style={{
        display: 'flex', gap: 0, marginBottom: 20,
        borderBottom: '1px solid var(--border)',
      }}>
        {[
          { key: 'open', label: 'Open', count: positions.length },
          { key: 'history', label: 'History' },
        ].map(t => (
          <button
            key={t.key}
            onClick={() => setTab(t.key)}
            style={{
              padding: '10px 20px',
              background: 'transparent',
              border: 'none',
              borderBottom: tab === t.key ? '2px solid var(--amber)' : '2px solid transparent',
              color: tab === t.key ? 'var(--text-1)' : 'var(--text-3)',
              fontWeight: tab === t.key ? 600 : 400,
              fontSize: 14,
              cursor: 'pointer',
              transition: 'all 0.15s',
            }}
          >
            {t.label}
            {t.count != null && <span style={{
              marginLeft: 6, fontSize: 11, padding: '1px 6px',
              borderRadius: 10, background: 'var(--bg-3)', color: 'var(--text-2)',
            }}>{t.count}</span>}
          </button>
        ))}
      </div>

      {tab === 'open' && (
        <>
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
                    const isExpanded = expandedId === p.id

                    return [
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
                          <span className="mono"
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
                        <td style={{ whiteSpace: 'nowrap' }}>
                          <button
                            className="btn btn-ghost btn-sm"
                            title={isExpanded ? 'Hide chart' : 'Show chart'}
                            onClick={() => setExpandedId(isExpanded ? null : p.id)}
                            style={{ marginRight: 6 }}
                          >
                            {isExpanded ? '▴ Chart' : '▾ Chart'}
                          </button>
                          <button
                            className="btn btn-ghost btn-sm"
                            onClick={() => setClosing(p)}
                            style={{ color: 'var(--red)', borderColor: 'rgba(239,68,68,0.3)' }}
                          >
                            Close
                          </button>
                        </td>
                      </tr>,
                      isExpanded && (
                        <tr key={`chart-${p.id}`} className="chart-row">
                          <td colSpan={8} style={{ padding: '12px 16px', background: 'var(--bg-2)' }}>
                            <PriceChart positionId={p.id} />
                          </td>
                        </tr>
                      ),
                    ]
                  })}
                </tbody>
              </table>
            </div>
          )}
        </>
      )}

      {tab === 'history' && (
        <>
          {historyLoading ? (
            <div className="loading"><div className="spinner" />Loading history...</div>
          ) : history.length === 0 ? (
            <div className="card empty">
              <div className="empty-icon">◈</div>
              No closed trades yet.
            </div>
          ) : (
            <>
              {/* Summary cards */}
              {(() => {
                const wins = history.filter(h => h.pnl_usd > 0)
                const losses = history.filter(h => h.pnl_usd <= 0)
                const totalHist = history.reduce((s, h) => s + (h.pnl_usd ?? 0), 0)
                const winRate = history.length > 0 ? (wins.length / history.length * 100) : 0
                return (
                  <div className="grid-3" style={{ marginBottom: 20 }}>
                    <div className="card">
                      <div className="card-title">Total trades</div>
                      <div className="stat-value">{history.length}</div>
                      <div style={{ fontSize: 12, color: 'var(--text-3)', marginTop: 2 }}>
                        {wins.length}W / {losses.length}L
                      </div>
                    </div>
                    <div className="card">
                      <div className="card-title">Win rate</div>
                      <div className="stat-value">{winRate.toFixed(0)}%</div>
                    </div>
                    <div className="card">
                      <div className="card-title">Realized P&L</div>
                      <div className={`stat-value ${totalHist >= 0 ? 'green' : 'red'}`}>
                        {totalHist >= 0 ? '+' : ''}${totalHist.toFixed(2)}
                      </div>
                    </div>
                  </div>
                )
              })()}

              <div className="card" style={{ padding: 0, overflow: 'hidden' }}>
                <table className="table">
                  <thead>
                    <tr>
                      <th>#</th>
                      <th>Token</th>
                      <th>Chain</th>
                      <th>Entry</th>
                      <th>Exit</th>
                      <th>Cost</th>
                      <th>P&L</th>
                      <th>Held</th>
                      <th>Closed</th>
                    </tr>
                  </thead>
                  <tbody>
                    {history.map(h => (
                      <tr key={h.id}>
                        <td className="mono" style={{ color: 'var(--text-3)', fontSize: 12 }}>
                          {h.id}
                        </td>
                        <td>
                          <div className="mono" style={{ fontWeight: 600 }}>{h.token}</div>
                        </td>
                        <td><span className="tag tag-gray">{h.chain}</span></td>
                        <td className="mono" style={{ fontSize: 12 }}>
                          ${h.entry_price?.toFixed(8) ?? '—'}
                        </td>
                        <td className="mono" style={{ fontSize: 12 }}>
                          {h.exit_price ? `$${h.exit_price.toFixed(8)}` : '—'}
                        </td>
                        <td className="mono">${h.cost_basis?.toFixed(2) ?? '—'}</td>
                        <td>
                          <span className="mono"
                            style={{ color: h.pnl_usd >= 0 ? 'var(--green)' : 'var(--red)' }}
                          >
                            {h.pnl_usd >= 0 ? '+' : ''}${h.pnl_usd?.toFixed(2)}
                            <span style={{ fontSize: 11, marginLeft: 4 }}>
                              ({h.pnl_pct >= 0 ? '+' : ''}{h.pnl_pct}%)
                            </span>
                          </span>
                        </td>
                        <td className="mono" style={{ fontSize: 12, color: 'var(--text-2)' }}>
                          {h.duration || '—'}
                        </td>
                        <td style={{ fontSize: 12, color: 'var(--text-3)' }}>
                          {h.closed_at ? new Date(h.closed_at + 'Z').toLocaleString([], {
                            month: 'short', day: 'numeric',
                            hour: '2-digit', minute: '2-digit',
                          }) : '—'}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </>
          )}
        </>
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
