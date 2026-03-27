import { useState, useEffect } from 'react'
import { get, post, del } from '../lib/api.js'

// ── helpers ───────────────────────────────────────────────────────────────────

function timeAgo(iso) {
  if (!iso) return '—'
  const diff = Date.now() - new Date(iso).getTime()
  const m = Math.floor(diff / 60000)
  if (m < 1)  return 'just now'
  if (m < 60) return `${m}m ago`
  const h = Math.floor(m / 60)
  if (h < 24) return `${h}h ago`
  return `${Math.floor(h / 24)}d ago`
}

function formatScore(score) {
  if (score === undefined || score === null) return '—'
  const n = Number(score)
  return n > 0 ? `+${n}` : String(n)
}

// ── sub-components ────────────────────────────────────────────────────────────

function EmptyState({ icon, title, sub }) {
  return (
    <div style={{
      display: 'flex', flexDirection: 'column', alignItems: 'center',
      padding: '48px 20px', color: 'var(--text-3)',
    }}>
      <div style={{ fontSize: 32, marginBottom: 12, opacity: 0.4 }}>{icon}</div>
      <div style={{ fontWeight: 500, marginBottom: 4, color: 'var(--text-2)' }}>{title}</div>
      <div style={{ fontSize: 12, textAlign: 'center', maxWidth: 280 }}>{sub}</div>
    </div>
  )
}

function AddChannelModal({ onAdd, onClose }) {
  const [channelId, setChannelId] = useState('')
  const [label, setLabel]         = useState('')
  const [saving, setSaving]       = useState(false)
  const [error, setError]         = useState('')

  async function submit() {
    const id = channelId.trim()
    if (!id) { setError('Channel ID is required'); return }
    // Accept both raw numeric IDs and t.me/username style
    setSaving(true)
    setError('')
    try {
      await onAdd(id, label.trim())
      onClose()
    } catch (err) {
      setError(err.message || 'Failed to add channel')
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal" onClick={e => e.stopPropagation()} style={{ maxWidth: 440 }}>
        <div className="modal-title">Add alpha channel</div>
        <div className="modal-body">
          <div style={{ marginBottom: 16 }}>
            <label className="field-label">Telegram channel ID</label>
            <input
              className="field-input"
              placeholder="-1001234567890 or @channelname"
              value={channelId}
              onChange={e => setChannelId(e.target.value)}
              onKeyDown={e => e.key === 'Enter' && submit()}
              autoFocus
            />
            <div style={{ fontSize: 11, color: 'var(--text-3)', marginTop: 4 }}>
              Forward any message from the channel to @userinfobot to get the numeric ID.
            </div>
          </div>
          <div style={{ marginBottom: 16 }}>
            <label className="field-label">Label <span style={{ fontWeight: 400, color: 'var(--text-3)' }}>(optional)</span></label>
            <input
              className="field-input"
              placeholder="e.g. Solana Alpha Calls"
              value={label}
              onChange={e => setLabel(e.target.value)}
              onKeyDown={e => e.key === 'Enter' && submit()}
            />
          </div>
          {error && (
            <div style={{
              background: 'rgba(255,80,80,0.08)', border: '1px solid rgba(255,80,80,0.25)',
              borderRadius: 'var(--radius)', padding: '8px 12px',
              fontSize: 12, color: '#ff5050', marginBottom: 12,
            }}>{error}</div>
          )}
          <div style={{ fontSize: 12, color: 'var(--text-3)', lineHeight: 1.6 }}>
            The bot must be a member of the channel to monitor it. Public channels can be joined
            without approval. Private channels require an invite.
          </div>
        </div>
        <div className="modal-actions">
          <button className="btn btn-ghost" onClick={onClose}>Cancel</button>
          <button className="btn btn-amber" onClick={submit} disabled={saving}>
            {saving ? 'Adding…' : 'Add channel'}
          </button>
        </div>
      </div>
    </div>
  )
}

// ── main page ─────────────────────────────────────────────────────────────────

export default function SignalChannels() {
  const [channels, setChannels]     = useState([])
  const [hits, setHits]             = useState([])
  const [loading, setLoading]       = useState(true)
  const [showAdd, setShowAdd]       = useState(false)
  const [removing, setRemoving]     = useState(null) // channel_id being removed

  async function load() {
    try {
      const [ch, h] = await Promise.all([
        get('/bot/channels'),
        get('/bot/channels/hits'),
      ])
      setChannels(ch?.channels ?? [])
      setHits(h?.hits ?? [])
    } catch {}
    setLoading(false)
  }

  useEffect(() => { load() }, [])

  async function handleAdd(channelId, label) {
    await post('/bot/channels', { channel_id: channelId, label })
    await load()
  }

  async function handleRemove(channelId) {
    setRemoving(channelId)
    try {
      await del(`/bot/channels/${encodeURIComponent(channelId)}`)
      await load()
    } catch (err) {
      alert(err.message || 'Failed to remove channel')
    } finally {
      setRemoving(null)
    }
  }

  if (loading) return (
    <div className="loading"><div className="spinner" />Loading channels…</div>
  )

  const activeChannels   = channels.filter(c => c.active)
  const inactiveChannels = channels.filter(c => !c.active)

  return (
    <div>
      <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', marginBottom: 24 }}>
        <div>
          <div className="page-title">Signal Channels</div>
          <div className="page-sub">
            Telegram alpha channels monitored for contract address mentions.
            Multi-channel hits boost scanner security scores.
          </div>
        </div>
        <button className="btn btn-amber" style={{ flexShrink: 0, marginTop: 4 }} onClick={() => setShowAdd(true)}>
          + Add channel
        </button>
      </div>

      {/* How it works banner — shown only when no channels yet */}
      {channels.length === 0 && (
        <div style={{
          background: 'rgba(245,166,35,0.05)',
          border: '1px solid var(--amber-dim)',
          borderRadius: 'var(--radius)',
          padding: '16px 20px',
          marginBottom: 24,
          fontSize: 13,
          color: 'var(--text-2)',
          lineHeight: 1.7,
        }}>
          <div style={{ fontWeight: 600, color: 'var(--text-1)', marginBottom: 6 }}>How signal channels work</div>
          When the bot sees a Solana or Base contract address mentioned in two or more monitored channels
          within 15 minutes, it adds a <span style={{ color: 'var(--amber)' }}>+{8} multi-channel alpha</span> flag
          to the scanner alert and boosts the urgency score. Known smart-money wallet activity detected
          via Arkham adds a further <span style={{ color: 'var(--amber)' }}>+{8} smart-money signal</span>.
          Add at least two channels to enable cross-channel confirmation.
        </div>
      )}

      {/* Active channels */}
      <div className="card" style={{ marginBottom: 16 }}>
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 16 }}>
          <div className="card-title" style={{ marginBottom: 0 }}>
            Active channels
            <span style={{
              marginLeft: 8, fontSize: 11, fontWeight: 500,
              background: activeChannels.length > 0 ? 'var(--amber-glow)' : 'var(--bg-3)',
              color: activeChannels.length > 0 ? 'var(--amber)' : 'var(--text-3)',
              border: `1px solid ${activeChannels.length > 0 ? 'var(--amber-dim)' : 'var(--border)'}`,
              borderRadius: 10, padding: '1px 8px',
            }}>
              {activeChannels.length}
            </span>
          </div>
          {activeChannels.length >= 2 && (
            <div style={{ fontSize: 12, color: 'var(--green)', display: 'flex', alignItems: 'center', gap: 4 }}>
              <span>●</span> Cross-channel detection active
            </div>
          )}
          {activeChannels.length === 1 && (
            <div style={{ fontSize: 12, color: 'var(--amber)' }}>
              Add one more channel to enable cross-channel detection
            </div>
          )}
        </div>

        {activeChannels.length === 0 ? (
          <EmptyState
            icon="◎"
            title="No channels yet"
            sub="Add a Telegram alpha channel to start monitoring for contract address mentions."
          />
        ) : (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 1 }}>
            {activeChannels.map(ch => (
              <div key={ch.channel_id} style={{
                display: 'flex', alignItems: 'center', gap: 12,
                padding: '12px 0',
                borderBottom: '1px solid var(--border)',
              }}>
                <div style={{
                  width: 36, height: 36, borderRadius: '50%',
                  background: 'var(--amber-glow)', border: '1px solid var(--amber-dim)',
                  display: 'flex', alignItems: 'center', justifyContent: 'center',
                  fontSize: 14, color: 'var(--amber)', flexShrink: 0,
                }}>
                  ◎
                </div>
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={{ fontWeight: 500, fontSize: 13, marginBottom: 2 }}>
                    {ch.label || ch.channel_id}
                  </div>
                  <div style={{ fontSize: 11, color: 'var(--text-3)', fontFamily: 'monospace' }}>
                    {ch.channel_id}
                  </div>
                </div>
                <div style={{ fontSize: 11, color: 'var(--text-3)', flexShrink: 0 }}>
                  Added {timeAgo(ch.added_at)}
                </div>
                <button
                  className="btn btn-ghost btn-sm"
                  style={{ color: '#ff5050', flexShrink: 0 }}
                  disabled={removing === ch.channel_id}
                  onClick={() => handleRemove(ch.channel_id)}
                >
                  {removing === ch.channel_id ? '…' : 'Remove'}
                </button>
              </div>
            ))}
          </div>
        )}
      </div>

      {/* Recent hits */}
      <div className="card" style={{ marginBottom: inactiveChannels.length > 0 ? 16 : 0 }}>
        <div className="card-title">Recent alpha hits</div>
        <div style={{ fontSize: 12, color: 'var(--text-3)', marginBottom: 16 }}>
          Contract addresses that appeared in two or more channels within a 15-minute window.
        </div>

        {hits.length === 0 ? (
          <EmptyState
            icon="⬡"
            title="No hits recorded yet"
            sub="Multi-channel hits will appear here when the same contract address is spotted across multiple channels."
          />
        ) : (
          <div>
            {/* Table header */}
            <div style={{
              display: 'grid',
              gridTemplateColumns: '1fr 80px 80px 100px',
              gap: 8, padding: '0 0 8px',
              borderBottom: '1px solid var(--border)',
              fontSize: 11, color: 'var(--text-3)', fontWeight: 600,
              textTransform: 'uppercase', letterSpacing: '0.05em',
            }}>
              <span>Contract</span>
              <span style={{ textAlign: 'right' }}>Channels</span>
              <span style={{ textAlign: 'right' }}>Score Δ</span>
              <span style={{ textAlign: 'right' }}>Seen</span>
            </div>
            {hits.map((hit, i) => (
              <div key={i} style={{
                display: 'grid',
                gridTemplateColumns: '1fr 80px 80px 100px',
                gap: 8, padding: '10px 0',
                borderBottom: '1px solid var(--border)',
                fontSize: 12, alignItems: 'center',
              }}>
                <div>
                  <div style={{
                    fontFamily: 'monospace', fontSize: 11,
                    overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
                    color: 'var(--text-1)',
                  }}>
                    {hit.market_id || hit.description || '—'}
                  </div>
                  {hit.description && hit.market_id && (
                    <div style={{ fontSize: 11, color: 'var(--text-3)', marginTop: 2 }}>
                      {hit.description}
                    </div>
                  )}
                </div>
                <div style={{ textAlign: 'right', color: 'var(--text-2)' }}>
                  {hit.channel_count ?? '—'}
                </div>
                <div style={{
                  textAlign: 'right',
                  color: hit.value_shift > 0 ? 'var(--green)' : 'var(--text-2)',
                  fontWeight: hit.value_shift > 0 ? 600 : 400,
                }}>
                  {formatScore(hit.value_shift)}
                </div>
                <div style={{ textAlign: 'right', color: 'var(--text-3)' }}>
                  {timeAgo(hit.timestamp)}
                </div>
              </div>
            ))}
          </div>
        )}
      </div>

      {/* Inactive / previously removed channels */}
      {inactiveChannels.length > 0 && (
        <div className="card">
          <div className="card-title">Inactive channels</div>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 1 }}>
            {inactiveChannels.map(ch => (
              <div key={ch.channel_id} style={{
                display: 'flex', alignItems: 'center', gap: 12,
                padding: '10px 0', borderBottom: '1px solid var(--border)',
                opacity: 0.5,
              }}>
                <div style={{ flex: 1 }}>
                  <div style={{ fontWeight: 500, fontSize: 13 }}>{ch.label || ch.channel_id}</div>
                  <div style={{ fontSize: 11, color: 'var(--text-3)', fontFamily: 'monospace' }}>{ch.channel_id}</div>
                </div>
                <button
                  className="btn btn-ghost btn-sm"
                  onClick={() => handleAdd(ch.channel_id, ch.label)}
                >
                  Reactivate
                </button>
              </div>
            ))}
          </div>
        </div>
      )}

      {showAdd && (
        <AddChannelModal
          onAdd={handleAdd}
          onClose={() => setShowAdd(false)}
        />
      )}
    </div>
  )
}
