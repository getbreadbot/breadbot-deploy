import { useState, useEffect, useCallback } from 'react'
import { get, post, del } from '../lib/api.js'

function fmt(ts) {
  if (!ts) return '\u2014'
  const d = new Date(ts.endsWith('Z') ? ts : ts + 'Z')
  return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' })
}

function fmtTs(ts) {
  if (!ts) return '\u2014'
  const d = new Date(ts.endsWith('Z') ? ts : ts + 'Z')
  return d.toLocaleString('en-US', {
    month: 'short', day: 'numeric',
    hour: '2-digit', minute: '2-digit', hour12: false,
  })
}

function ChannelRow({ ch, onRemove, removing }) {
  return (
    <tr>
      <td><span style={{ fontFamily: 'var(--mono)', fontSize: 12 }}>{ch.channel_id}</span></td>
      <td>{ch.label || <span style={{ opacity: 0.4 }}>\u2014</span>}</td>
      <td>{fmt(ch.added_at)}</td>
      <td>
        <span className={`score-badge ${ch.active ? 'high' : 'low'}`} style={{ fontSize: 10, padding: '2px 7px' }}>
          {ch.active ? 'Active' : 'Off'}
        </span>
      </td>
      <td>
        {ch.active && (
          <button
            className="btn btn-ghost btn-sm"
            style={{ color: 'var(--red)', fontSize: 11 }}
            disabled={removing === ch.channel_id}
            onClick={() => onRemove(ch.channel_id)}
          >
            {removing === ch.channel_id ? 'Removing\u2026' : 'Remove'}
          </button>
        )}
      </td>
    </tr>
  )
}

function HitRow({ hit }) {
  const addr = hit.market_id || '\u2014'
  const short = addr.length > 20 ? addr.slice(0, 8) + '\u2026' + addr.slice(-6) : addr
  return (
    <tr>
      <td style={{ fontSize: 11, opacity: 0.6 }}>{fmtTs(hit.timestamp)}</td>
      <td>
        <span
          style={{ fontFamily: 'var(--mono)', fontSize: 12, cursor: 'pointer' }}
          title={addr}
          onClick={() => navigator.clipboard?.writeText(addr)}
        >
          {short}
        </span>
      </td>
      <td style={{ fontSize: 12 }}>{hit.description || '\u2014'}</td>
      <td>
        {hit.scanner_triggered
          ? <span className="score-badge high" style={{ fontSize: 10, padding: '2px 7px' }}>Triggered</span>
          : <span className="score-badge" style={{ fontSize: 10, padding: '2px 7px', opacity: 0.5 }}>Logged</span>}
      </td>
    </tr>
  )
}

export default function Channels() {
  const [channels, setChannels] = useState([])
  const [hits, setHits]         = useState([])
  const [loading, setLoading]   = useState(true)
  const [error, setError]       = useState(null)

  const [adding, setAdding]         = useState(false)
  const [newId, setNewId]           = useState('')
  const [newLabel, setNewLabel]     = useState('')
  const [addErr, setAddErr]         = useState(null)
  const [addLoading, setAddLoading] = useState(false)
  const [removing, setRemoving]     = useState(null)

  const load = useCallback(async () => {
    try {
      const [ch, h] = await Promise.all([
        get('/bot/channels'),
        get('/bot/channels/hits'),
      ])
      setChannels(ch.channels || [])
      setHits(h.hits || [])
      setError(null)
    } catch (e) {
      setError(e.message)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { load() }, [load])

  async function handleAdd(e) {
    e.preventDefault()
    setAddErr(null)
    const id = newId.trim()
    if (!id) { setAddErr('Channel ID is required.'); return }
    if (!/^-?\d+$/.test(id)) { setAddErr('Channel ID must be a number (e.g. -1001234567890).'); return }
    setAddLoading(true)
    try {
      await post('/bot/channels', { channel_id: id, label: newLabel.trim() })
      setNewId(''); setNewLabel(''); setAdding(false)
      await load()
    } catch (e) {
      setAddErr(e.message)
    } finally {
      setAddLoading(false)
    }
  }

  async function handleRemove(channelId) {
    setRemoving(channelId)
    try {
      await del(`/bot/channels/${encodeURIComponent(channelId)}`)
      await load()
    } catch (e) {
      setError(e.message)
    } finally {
      setRemoving(null)
    }
  }

  const active   = channels.filter(c => c.active)
  const inactive = channels.filter(c => !c.active)

  return (
    <div className="page">
      <div className="page-header">
        <div>
          <h1 className="page-title">Signal Channels</h1>
          <p className="page-sub">Telegram alpha channels monitored by the scanner</p>
        </div>
        <button className="btn btn-primary btn-sm" onClick={() => { setAdding(a => !a); setAddErr(null) }}>
          {adding ? 'Cancel' : '+ Add Channel'}
        </button>
      </div>

      {adding && (
        <div className="card" style={{ marginBottom: 20 }}>
          <div className="card-title">Add Channel</div>
          <form onSubmit={handleAdd} style={{ display: 'flex', gap: 10, alignItems: 'flex-end', flexWrap: 'wrap' }}>
            <div style={{ flex: '1 1 200px' }}>
              <label className="field-label">Telegram Channel ID</label>
              <input className="input" placeholder="-1001234567890" value={newId} onChange={e => setNewId(e.target.value)} autoFocus />
              <div style={{ fontSize: 11, opacity: 0.5, marginTop: 4 }}>Negative number from Telegram. Use @userinfobot or Rose bot.</div>
            </div>
            <div style={{ flex: '1 1 160px' }}>
              <label className="field-label">Label (optional)</label>
              <input className="input" placeholder="e.g. Solana Alpha" value={newLabel} onChange={e => setNewLabel(e.target.value)} />
            </div>
            <button className="btn btn-primary" type="submit" disabled={addLoading}>
              {addLoading ? 'Adding\u2026' : 'Add'}
            </button>
          </form>
          {addErr && <div style={{ marginTop: 8, fontSize: 13, color: 'var(--red)' }}>{addErr}</div>}
        </div>
      )}

      {error && <div style={{ marginBottom: 16, fontSize: 13, color: 'var(--red)' }}>{error}</div>}

      {loading ? (
        <div className="loading"><div className="spinner" /><span>Loading channels\u2026</span></div>
      ) : (
        <>
          <div className="card" style={{ marginBottom: 20 }}>
            <div className="card-header">
              <div className="card-title">
                Active Channels
                <span className="score-badge high" style={{ marginLeft: 8, fontSize: 10, padding: '2px 7px' }}>{active.length}</span>
              </div>
            </div>
            {active.length === 0
              ? <div className="empty">No active channels. Add one above to start monitoring alpha.</div>
              : <div style={{ overflowX: 'auto' }}>
                  <table className="table">
                    <thead><tr><th>Channel ID</th><th>Label</th><th>Added</th><th>Status</th><th></th></tr></thead>
                    <tbody>{active.map(ch => <ChannelRow key={ch.channel_id} ch={ch} onRemove={handleRemove} removing={removing} />)}</tbody>
                  </table>
                </div>
            }
          </div>

          {inactive.length > 0 && (
            <div className="card" style={{ marginBottom: 20, opacity: 0.65 }}>
              <div className="card-title">
                Deactivated
                <span className="score-badge low" style={{ marginLeft: 8, fontSize: 10, padding: '2px 7px' }}>{inactive.length}</span>
              </div>
              <div style={{ overflowX: 'auto' }}>
                <table className="table">
                  <thead><tr><th>Channel ID</th><th>Label</th><th>Added</th><th>Status</th><th></th></tr></thead>
                  <tbody>{inactive.map(ch => <ChannelRow key={ch.channel_id} ch={ch} onRemove={handleRemove} removing={removing} />)}</tbody>
                </table>
              </div>
            </div>
          )}

          <div className="card">
            <div className="card-header">
              <div className="card-title">Recent Alpha Hits</div>
              <button className="btn btn-ghost btn-sm" onClick={load}>Refresh</button>
            </div>
            {hits.length === 0
              ? <div className="empty">No hits yet. Hits appear when the scanner flags a contract address seen across monitored channels.</div>
              : <div style={{ overflowX: 'auto' }}>
                  <table className="table">
                    <thead><tr><th>Time</th><th>Contract</th><th>Description</th><th>Result</th></tr></thead>
                    <tbody>{hits.map((h, i) => <HitRow key={i} hit={h} />)}</tbody>
                  </table>
                </div>
            }
          </div>
        </>
      )}
    </div>
  )
}
