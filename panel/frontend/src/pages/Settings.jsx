import { useState, useEffect } from 'react'
import { get, post } from '../lib/api.js'

// S81 P3: Basic fields are now driven by /api/config which returns the schema.
// The frontend renders types (float / int / bool / enum / string) from the
// server response — no hard-coded list here. The Advanced (credentials)
// section still uses the legacy /api/settings/advanced endpoint because
// API keys belong in deployment env, not bot_config.

const ADVANCED_GROUPS = [
  { label: 'Coinbase',          keys: ['COINBASE_API_KEY', 'COINBASE_SECRET_KEY'] },
  { label: 'Kraken',            keys: ['KRAKEN_API_KEY', 'KRAKEN_SECRET_KEY'] },
  { label: 'Bybit',             keys: ['BYBIT_API_KEY', 'BYBIT_SECRET_KEY'] },
  { label: 'Binance.US',        keys: ['BINANCE_API_KEY', 'BINANCE_SECRET_KEY'] },
  { label: 'Gemini',            keys: ['GEMINI_API_KEY', 'GEMINI_SECRET_KEY'] },
  { label: 'Telegram',          keys: ['TELEGRAM_BOT_TOKEN', 'TELEGRAM_CHAT_ID'] },
  { label: 'RPC endpoints',     keys: ['SOLANA_RPC_URL', 'EVM_BASE_RPC_URL'] },
  {
    label: 'Coinbase CFM perpetuals',
    keys: ['COINBASE_PERP_ENABLED'],
    note: 'CFTC-regulated — recommended venue for US residents. Enable to use Coinbase for funding arb.',
  },
  {
    label: 'Drift Protocol',
    keys: ['DRIFT_ENABLED', 'DRIFT_MARKET_PAIRS'],
    note: 'Decentralised perpetuals on Solana. No KYC. Uses existing Solana wallet. User assumes regulatory responsibility.',
  },
]


// ── Field renderer — picks the right input by type ──────────────────────────

function FieldInput({ field, value, onChange }) {
  const t = field.type

  if (t === 'bool') {
    return (
      <label style={{ display: 'inline-flex', alignItems: 'center', gap: 10, cursor: 'pointer' }}>
        <input
          type="checkbox"
          checked={!!value}
          onChange={e => onChange(e.target.checked)}
          style={{ width: 18, height: 18, cursor: 'pointer' }}
        />
        <span style={{ fontSize: 13, color: 'var(--text-2)' }}>
          {value ? 'On' : 'Off'}
        </span>
      </label>
    )
  }

  if (t === 'enum') {
    return (
      <select
        className="input"
        value={value ?? field.default}
        onChange={e => onChange(e.target.value)}
        style={{ minWidth: 180 }}
      >
        {field.options.map(opt => (
          <option key={opt} value={opt}>{opt}</option>
        ))}
      </select>
    )
  }

  // float / int / string — all render as text input
  return (
    <input
      type="text"
      className="input mono"
      value={value ?? ''}
      onChange={e => onChange(e.target.value)}
      placeholder={String(field.default)}
    />
  )
}


function AdvancedField({ fieldKey, setKey }) {
  const [revealed, setRevealed] = useState(false)
  const [value, setValue] = useState('••••••••')
  const [editing, setEditing] = useState(false)
  const [editVal, setEditVal] = useState('')
  const [saving, setSaving] = useState(false)
  const [saved, setSaved] = useState(false)

  async function reveal() {
    if (revealed) { setRevealed(false); setValue('••••••••'); return }
    try {
      const data = await get(`/settings/advanced/reveal/${fieldKey}`)
      setValue(data.value || '')
      setRevealed(true)
    } catch {}
  }

  async function save() {
    setSaving(true)
    try {
      await post('/settings/advanced', { key: fieldKey, value: editVal })
      setEditing(false)
      setRevealed(false)
      setValue('••••••••')
      setSaved(true)
      setTimeout(() => setSaved(false), 2000)
    } catch (err) {
      alert(err.message)
    } finally {
      setSaving(false)
    }
  }

  return (
    <div style={{ marginBottom: 12 }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 4 }}>
        <div className="field-label" style={{ marginBottom: 0, flex: 1 }}>
          {fieldKey}
          {saved && <span style={{ fontSize: 11, color: 'var(--green)', marginLeft: 8 }}>Saved</span>}
          {setKey && <span className="tag tag-green" style={{ marginLeft: 8, fontSize: 10 }}>Set</span>}
        </div>
        <button className="btn btn-ghost btn-sm" onClick={reveal}>{revealed ? 'Hide' : 'Reveal'}</button>
        <button className="btn btn-ghost btn-sm" onClick={() => { setEditing(!editing); setEditVal('') }}>
          {editing ? 'Cancel' : 'Edit'}
        </button>
      </div>
      {!editing ? (
        <div className="input mono" style={{ background: 'var(--bg-2)', cursor: 'default', fontSize: 12, letterSpacing: revealed ? 0 : 2 }}>
          {revealed ? value : '••••••••'}
        </div>
      ) : (
        <div className="input-row">
          <input
            type="text"
            className="input mono"
            value={editVal}
            onChange={e => setEditVal(e.target.value)}
            placeholder={`New value for ${fieldKey}`}
            autoFocus
          />
          <button className="btn btn-amber btn-sm" onClick={save} disabled={saving || !editVal}>
            {saving ? '...' : 'Save'}
          </button>
        </div>
      )}
    </div>
  )
}


// ── Main component ──────────────────────────────────────────────────────────

export default function Settings() {
  const [fields, setFields] = useState([])         // schema array from backend
  const [values, setValues] = useState({})         // current values keyed by field.key
  const [advancedMeta, setAdvancedMeta] = useState({ set: {} })
  const [loading, setLoading] = useState(true)
  const [showAdvanced, setShowAdvanced] = useState(false)
  const [saving, setSaving] = useState(false)
  const [saved, setSaved] = useState(false)
  const [dirty, setDirty] = useState(false)
  const [errors, setErrors] = useState({})

  useEffect(() => {
    async function load() {
      try {
        const [cfg, adv] = await Promise.all([
          get('/config'),
          get('/settings/advanced'),
        ])
        setFields(cfg.fields || [])
        setValues(cfg.values || {})
        setAdvancedMeta(adv)
      } catch (err) {
        console.error('Settings load failed:', err)
      }
      setLoading(false)
    }
    load()
  }, [])

  function update(key, val) {
    setValues(prev => ({ ...prev, [key]: val }))
    setDirty(true)
    setErrors(prev => {
      const next = { ...prev }
      delete next[key]
      return next
    })
  }

  async function saveBasic(e) {
    e.preventDefault()
    setSaving(true)
    setErrors({})
    try {
      const result = await post('/config', { settings: values })
      if (result.errors && Object.keys(result.errors).length > 0) {
        setErrors(result.errors)
      } else {
        setSaved(true)
        setDirty(false)
        setTimeout(() => setSaved(false), 2500)
        // Reload to get canonical values back (in case of coercion)
        const cfg = await get('/config')
        setValues(cfg.values || {})
      }
    } catch (err) {
      alert(err.message)
    } finally {
      setSaving(false)
    }
  }

  if (loading) return <div className="loading"><div className="spinner" />Loading settings...</div>

  // Group fields by their `group` attribute, preserving order of first appearance
  const groupOrder = []
  const grouped = {}
  for (const f of fields) {
    const g = f.group || 'Other'
    if (!(g in grouped)) {
      grouped[g] = []
      groupOrder.push(g)
    }
    grouped[g].push(f)
  }

  return (
    <div>
      <div className="page-title">Settings</div>
      <div className="page-sub">Bot configuration. Changes take effect immediately — no restart needed.</div>

      <form onSubmit={saveBasic}>
        {groupOrder.map(groupName => (
          <div key={groupName} className="card" style={{ marginBottom: 16 }}>
            <div className="card-title">{groupName}</div>
            {grouped[groupName].map(f => (
              <div key={f.key} className="field">
                <label className="field-label">{f.label}</label>
                <div className="input-row">
                  <FieldInput
                    field={f}
                    value={values[f.key]}
                    onChange={val => update(f.key, val)}
                  />
                  {f.suffix && f.type !== 'bool' && f.type !== 'enum' && (
                    <span style={{
                      fontSize: 12,
                      color: 'var(--text-3)',
                      whiteSpace: 'nowrap',
                      lineHeight: '36px',
                    }}>
                      {f.suffix}
                    </span>
                  )}
                </div>
                <div className="field-desc">{f.desc}</div>
                {errors[f.key] && (
                  <div style={{ fontSize: 12, color: 'var(--red, #ef4444)', marginTop: 4 }}>
                    {errors[f.key]}
                  </div>
                )}
              </div>
            ))}
          </div>
        ))}

        <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 10, marginBottom: 24 }}>
          {saved && <span style={{ fontSize: 13, color: 'var(--green)', lineHeight: '36px' }}>Settings saved</span>}
          <button type="submit" className="btn btn-amber" disabled={saving || !dirty}>
            {saving ? 'Saving...' : 'Save settings'}
          </button>
        </div>
      </form>

      {/* Advanced — API keys (still routed through Railway env via legacy endpoint) */}
      <div className="card">
        <div
          style={{ display: 'flex', alignItems: 'center', cursor: 'pointer', userSelect: 'none' }}
          onClick={() => setShowAdvanced(!showAdvanced)}
        >
          <div className="card-title" style={{ marginBottom: 0, flex: 1 }}>
            Advanced — API keys and credentials
          </div>
          <span style={{ fontSize: 12, color: 'var(--text-3)' }}>
            {showAdvanced ? '▲ Hide' : '▼ Show'}
          </span>
        </div>

        {showAdvanced && (
          <>
            <div style={{
              margin: '14px 0',
              padding: '10px 14px',
              background: 'rgba(239,68,68,0.06)',
              border: '1px solid rgba(239,68,68,0.2)',
              borderRadius: 'var(--radius)',
              fontSize: 12,
              color: 'var(--text-2)',
            }}>
              Only edit these if you know what you are doing. Incorrect values will stop the bot.
              Values are masked by default — click Reveal to view, Edit to change.
              These keys live in deployment env and require a service restart to take effect.
            </div>

            {ADVANCED_GROUPS.map(group => (
              <div key={group.label} style={{ marginBottom: 20 }}>
                <div style={{
                  fontSize: 11,
                  fontWeight: 600,
                  letterSpacing: '0.08em',
                  textTransform: 'uppercase',
                  color: 'var(--text-3)',
                  marginBottom: group.note ? 6 : 10,
                  marginTop: 10,
                }}>
                  {group.label}
                </div>
                {group.note && (
                  <div style={{ fontSize: 12, color: 'var(--text-3)', marginBottom: 10, lineHeight: 1.5 }}>
                    {group.note}
                  </div>
                )}
                {group.keys.map(k => (
                  <AdvancedField
                    key={k}
                    fieldKey={k}
                    setKey={advancedMeta?.set?.[k]}
                  />
                ))}
                {['Coinbase', 'Kraken', 'Bybit', 'Binance.US', 'Gemini'].includes(group.label) && (
                  <div style={{
                    fontSize: 11,
                    color: 'var(--text-3)',
                    marginTop: 6,
                    paddingTop: 6,
                    borderTop: '1px solid var(--border)',
                  }}>
                    By connecting this exchange, you confirm you have the legal right to use it in your jurisdiction.
                  </div>
                )}
              </div>
            ))}
          </>
        )}
      </div>
    </div>
  )
}
