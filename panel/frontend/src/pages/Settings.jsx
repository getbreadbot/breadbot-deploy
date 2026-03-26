import { useState, useEffect } from 'react'
import { get, post } from '../lib/api.js'

const BASIC_FIELDS = [
  {
    key: 'MAX_POSITION_SIZE_PCT',
    label: 'Max position size',
    desc: 'Maximum % of your portfolio placed on any single trade. Default: 0.02 (2%).',
    placeholder: '0.02',
    suffix: '(e.g. 0.02 = 2%)',
  },
  {
    key: 'DAILY_LOSS_LIMIT_PCT',
    label: 'Daily loss limit',
    desc: 'Bot auto-pauses if losses hit this % of portfolio in one day. Default: 0.05 (5%).',
    placeholder: '0.05',
    suffix: '(e.g. 0.05 = 5%)',
  },
  {
    key: 'MIN_LIQUIDITY_USD',
    label: 'Minimum liquidity',
    desc: 'Tokens below this USD liquidity are filtered out. Default: 15000.',
    placeholder: '15000',
    suffix: 'USD',
  },
  {
    key: 'MIN_VOLUME_24H_USD',
    label: 'Minimum 24h volume',
    desc: 'Tokens below this 24-hour trading volume are filtered out. Default: 40000.',
    placeholder: '40000',
    suffix: 'USD',
  },
  {
    key: 'AUTO_EXECUTE_MIN_SCORE',
    label: 'Auto-execute minimum score',
    desc: 'Only auto-trade alerts above this security score. Default: 75.',
    placeholder: '75',
    suffix: 'out of 100',
  },
]

const ADVANCED_GROUPS = [
  {
    label: 'Coinbase',
    keys: ['COINBASE_API_KEY', 'COINBASE_SECRET_KEY'],
  },
  {
    label: 'Kraken',
    keys: ['KRAKEN_API_KEY', 'KRAKEN_SECRET_KEY'],
  },
  {
    label: 'Bybit',
    keys: ['BYBIT_API_KEY', 'BYBIT_SECRET_KEY'],
  },
  {
    label: 'Binance.US',
    keys: ['BINANCE_API_KEY', 'BINANCE_SECRET_KEY'],
  },
  {
    label: 'Gemini',
    keys: ['GEMINI_API_KEY', 'GEMINI_SECRET_KEY'],
  },
  {
    label: 'Telegram',
    keys: ['TELEGRAM_BOT_TOKEN', 'TELEGRAM_CHAT_ID'],
  },
  {
    label: 'RPC endpoints',
    keys: ['SOLANA_RPC_URL', 'EVM_BASE_RPC_URL'],
  },
]

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

export default function Settings() {
  const [basic, setBasic] = useState({})
  const [advancedMeta, setAdvancedMeta] = useState({ set: {} })
  const [loading, setLoading] = useState(true)
  const [showAdvanced, setShowAdvanced] = useState(false)
  const [saving, setSaving] = useState(false)
  const [saved, setSaved] = useState(false)
  const [dirty, setDirty] = useState(false)

  useEffect(() => {
    async function load() {
      try {
        const [b, a] = await Promise.all([get('/settings/basic'), get('/settings/advanced')])
        setBasic(b)
        setAdvancedMeta(a)
      } catch {}
      setLoading(false)
    }
    load()
  }, [])

  function update(key, val) {
    setBasic(prev => ({ ...prev, [key]: val }))
    setDirty(true)
  }

  async function saveBasic(e) {
    e.preventDefault()
    setSaving(true)
    try {
      await post('/settings/basic', { settings: basic })
      setSaved(true)
      setDirty(false)
      setTimeout(() => setSaved(false), 2500)
    } catch (err) {
      alert(err.message)
    } finally {
      setSaving(false)
    }
  }

  if (loading) return <div className="loading"><div className="spinner" />Loading settings...</div>

  return (
    <div>
      <div className="page-title">Settings</div>
      <div className="page-sub">Bot configuration. Changes take effect on the next scanner cycle.</div>

      {/* Basic */}
      <form onSubmit={saveBasic}>
        <div className="card" style={{ marginBottom: 16 }}>
          <div className="card-title">Risk & filter settings</div>
          {BASIC_FIELDS.map(f => (
            <div key={f.key} className="field">
              <label className="field-label">{f.label}</label>
              <div className="input-row">
                <input
                  type="text"
                  className="input mono"
                  value={basic[f.key] ?? ''}
                  onChange={e => update(f.key, e.target.value)}
                  placeholder={f.placeholder}
                />
                {f.suffix && (
                  <span style={{ fontSize: 12, color: 'var(--text-3)', whiteSpace: 'nowrap', lineHeight: '36px' }}>
                    {f.suffix}
                  </span>
                )}
              </div>
              <div className="field-desc">{f.desc}</div>
            </div>
          ))}
        </div>

        <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 10, marginBottom: 24 }}>
          {saved && <span style={{ fontSize: 13, color: 'var(--green)', lineHeight: '36px' }}>Settings saved</span>}
          <button type="submit" className="btn btn-amber" disabled={saving || !dirty}>
            {saving ? 'Saving...' : 'Save settings'}
          </button>
        </div>
      </form>

      {/* Advanced */}
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
            </div>

            {ADVANCED_GROUPS.map(group => (
              <div key={group.label} style={{ marginBottom: 20 }}>
                <div style={{
                  fontSize: 11,
                  fontWeight: 600,
                  letterSpacing: '0.08em',
                  textTransform: 'uppercase',
                  color: 'var(--text-3)',
                  marginBottom: 10,
                  marginTop: 10,
                }}>
                  {group.label}
                </div>
                {group.keys.map(k => (
                  <AdvancedField
                    key={k}
                    fieldKey={k}
                    setKey={advancedMeta?.set?.[k]}
                  />
                ))}
              </div>
            ))}
          </>
        )}
      </div>
    </div>
  )
}
