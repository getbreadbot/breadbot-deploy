import { useState, useEffect, useCallback } from 'react'
import { get, post, del } from '../lib/api.js'

// ── Demo-mode detection ───────────────────────────────────────────────────
// Demo (demo.breadbot.app) doesn't have the buy or watchlist endpoints —
// hide the action buttons there. Detection is hostname-based and runs
// once at module load.
const IS_DEMO = typeof window !== 'undefined'
  && window.location.hostname.startsWith('demo.')

const fmtUsd = (n) => {
  if (n == null) return '—'
  if (n >= 1e6) return '$' + (n / 1e6).toFixed(2) + 'M'
  if (n >= 1e3) return '$' + (n / 1e3).toFixed(1) + 'k'
  if (n >= 1)   return '$' + n.toFixed(2)
  if (n > 0)    return '$' + n.toPrecision(4)
  return '$0'
}
const fmtPrice = (n) => {
  if (n == null || n === 0) return '—'
  if (n >= 1)    return '$' + n.toFixed(4)
  if (n >= 0.01) return '$' + n.toFixed(6)
  return '$' + n.toPrecision(4)
}
const fmtAge = (iso) => {
  if (!iso) return '—'
  const ms = Date.now() - new Date(iso + 'Z').getTime()
  const m  = Math.floor(ms / 60000)
  if (m < 60)  return `${m}m ago`
  const h = Math.floor(m / 60)
  if (h < 24)  return `${h}h ago`
  return `${Math.floor(h / 24)}d ago`
}

function ScoreHero({ score }) {
  const color = score >= 80 ? 'var(--green)'
              : score >= 60 ? '#fbbf24'
              :               '#f87171'
  const tier  = score >= 80 ? 'LOW RISK'
              : score >= 60 ? 'ELEVATED RISK'
              :               'HIGH RISK'
  return (
    <div style={{
      textAlign: 'center', padding: '24px 16px', background: 'var(--bg-2)',
      border: `1px solid ${color}`, borderRadius: 12, marginBottom: 16,
    }}>
      <div style={{ fontSize: 56, fontWeight: 700, color,
                    fontVariantNumeric: 'tabular-nums', lineHeight: 1 }}>
        {score}
      </div>
      <div style={{ fontSize: 12, color: 'var(--text-3)', marginTop: 4 }}>/ 100</div>
      <div style={{ marginTop: 12, padding: '4px 12px', display: 'inline-block',
                    fontSize: 12, fontWeight: 700, letterSpacing: '0.08em',
                    color, border: `1px solid ${color}`, borderRadius: 4 }}>
        {tier}
      </div>
    </div>
  )
}

function StatRow({ label, value, danger }) {
  return (
    <div style={{ display: 'flex', justifyContent: 'space-between',
                  padding: '6px 0', borderBottom: '1px solid var(--bg-3)',
                  fontSize: 13 }}>
      <span style={{ color: 'var(--text-3)' }}>{label}</span>
      <span style={{ fontVariantNumeric: 'tabular-nums',
                     color: danger ? '#f87171' : 'var(--text-1)' }}>
        {value}
      </span>
    </div>
  )
}

// ── Copyable contract address row + chart link ────────────────────────────
function CopyableAddress({ address, chain }) {
  const [copied, setCopied] = useState(false)
  const handleCopy = () => {
    navigator.clipboard.writeText(address).then(() => {
      setCopied(true)
      setTimeout(() => setCopied(false), 1400)
    }).catch(() => {})
  }
  // Solana addresses are base58 (no 0x); Base addresses are 0x... hex.
  const dexUrl = chain === 'solana'
    ? `https://dexscreener.com/solana/${address}`
    : `https://dexscreener.com/base/${address}`
  return (
    <div style={{ borderBottom: '1px solid var(--bg-3)', padding: '8px 0' }}>
      <div style={{ display: 'flex', justifyContent: 'space-between',
                    alignItems: 'center', marginBottom: 4 }}>
        <span style={{ color: 'var(--text-3)', fontSize: 13 }}>Contract</span>
        <button
          onClick={handleCopy}
          style={{ fontSize: 11, padding: '2px 8px', borderRadius: 4,
                   border: '1px solid var(--bg-3)',
                   background: copied ? '#16a34a' : 'var(--bg-2)',
                   color: copied ? '#fff' : 'var(--text-2)',
                   cursor: 'pointer' }}
          title="Copy full contract address">
          {copied ? '✓ Copied' : 'Copy'}
        </button>
      </div>
      <div
        onClick={handleCopy}
        title={address + ' (click to copy)'}
        style={{ fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace',
                 fontSize: 12, color: 'var(--text-1)',
                 background: 'var(--bg-2)', padding: '6px 8px',
                 borderRadius: 4, lineHeight: 1.4, cursor: 'pointer',
                 userSelect: 'all' }}>
        {address.length > 16
          ? address.slice(0, 8) + '…' + address.slice(-6)
          : address}
      </div>
      <div style={{ marginTop: 6 }}>
        <a href={dexUrl} target="_blank" rel="noopener noreferrer"
           style={{ fontSize: 12, color: '#60a5fa', textDecoration: 'none' }}>
          📈 View chart on DEXScreener →
        </a>
      </div>
    </div>
  )
}

// ── Watchlist sidebar ─────────────────────────────────────────────────────
function WatchlistPanel({ items, onSelect, onRemove }) {
  if (IS_DEMO) return null
  if (!items || items.length === 0) {
    return (
      <div className="card">
        <div className="card-title">Watchlist</div>
        <div style={{ fontSize: 13, color: 'var(--text-3)' }}>
          No coins watched yet. Research a contract, then click Watch to add it.
        </div>
      </div>
    )
  }
  return (
    <div className="card">
      <div className="card-title">Watchlist ({items.length})</div>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
        {items.map(it => {
          const score = it.last_score ?? 0
          const sc = score >= 80 ? 'var(--green)'
                   : score >= 60 ? '#fbbf24'
                   :               '#f87171'
          return (
            <div key={it.id} style={{ display: 'flex', alignItems: 'center',
                                       gap: 10, padding: 10, background: 'var(--bg-2)',
                                       border: '1px solid var(--bg-3)', borderRadius: 6 }}>
              <button
                className="btn btn-ghost btn-sm"
                style={{ flex: 1, justifyContent: 'flex-start',
                         fontSize: 12, padding: '6px 8px', textAlign: 'left' }}
                onClick={() => onSelect(it.address)}
              >
                <div style={{ fontWeight: 600 }}>
                  {it.symbol || it.address.slice(0, 6) + '…' + it.address.slice(-4)}
                </div>
                <div style={{ fontSize: 11, color: 'var(--text-3)', marginTop: 2 }}>
                  {it.chain.toUpperCase()}
                  {it.last_price ? ' · ' + fmtPrice(it.last_price) : ''}
                  {it.last_checked_at ? ' · ' + fmtAge(it.last_checked_at) : ''}
                </div>
              </button>
              <span style={{ fontSize: 12, fontWeight: 700, color: sc,
                             fontVariantNumeric: 'tabular-nums', minWidth: 32,
                             textAlign: 'right' }}>
                {it.last_score ?? '—'}
              </span>
              <button
                className="btn btn-ghost btn-sm"
                style={{ padding: '4px 8px', fontSize: 16, lineHeight: 1 }}
                onClick={() => onRemove(it.id)}
                title="Remove"
              >×</button>
            </div>
          )
        })}
      </div>
    </div>
  )
}

export default function Research() {
  const [addr,    setAddr]    = useState('')
  const [data,    setData]    = useState(null)
  const [loading, setLoading] = useState(false)
  const [error,   setError]   = useState('')
  const [buying,  setBuying]  = useState(false)
  const [buyMsg,  setBuyMsg]  = useState('')
  const [watching, setWatching] = useState(false)
  const [items,   setItems]   = useState([])

  const loadList = useCallback(async () => {
    if (IS_DEMO) return
    try {
      const list = await get('/research/watchlist/list')
      setItems(Array.isArray(list) ? list : [])
    } catch { /* silently empty — likely 401 if cookie expired */ }
  }, [])

  useEffect(() => { loadList() }, [loadList])

  const runResearch = useCallback(async (overrideAddr) => {
    const target = (overrideAddr || addr).trim()
    if (!target) { setError('Enter a contract address'); return }
    setAddr(target)
    setError(''); setBuyMsg(''); setData(null); setLoading(true)
    try {
      const res = await get(`/research/${encodeURIComponent(target)}`)
      setData(res)
    } catch (e) {
      setError(e.message || 'Research failed')
    } finally {
      setLoading(false)
    }
  }, [addr])

  const handleBuy = useCallback(async () => {
    if (!data) return
    setBuying(true); setBuyMsg('')
    try {
      const dex = data.dexscreener || {}
      const res = await post('/research/buy', {
        address:    data.token_addr,
        chain:      data.chain,
        symbol:     dex.symbol || null,
        score:      data.rug_score || 0,
        market_cap: dex.market_cap || 0,
        price_usd:  dex.price_usd  || 0,
      })
      setBuyMsg(`Order submitted: ${res.reason}`)
    } catch (e) {
      setBuyMsg(`Buy failed: ${e.message}`)
    } finally {
      setBuying(false)
    }
  }, [data])

  const handleWatch = useCallback(async () => {
    if (!data) return
    setWatching(true)
    try {
      const dex = data.dexscreener || {}
      await post('/research/watchlist/add', {
        address: data.token_addr,
        chain:   data.chain,
        symbol:  dex.symbol || null,
        name:    dex.name   || null,
      })
      await loadList()
    } catch (e) {
      setError(`Watch failed: ${e.message}`)
    } finally {
      setWatching(false)
    }
  }, [data, loadList])

  const handleRemove = useCallback(async (id) => {
    try {
      await del(`/research/watchlist/${id}`)
      await loadList()
    } catch (e) { setError(e.message) }
  }, [loadList])

  const dex = data?.dexscreener || {}
  const gp  = data?.goplus || {}
  const rugRisks = data?.rugcheck?.risks || []
  const scannerAlert = data?.scanner_alert || null
  // Allow the buy button only when we have a real price (else execute_trade
  // will fail server-side anyway) and the Solana/Base chains where the bot
  // executor is wired.
  const buyable = data
    && (data.chain === 'solana' || data.chain === 'base')
    && dex.price_usd > 0

  return (
    <div>
      <div className="page-title">Research</div>
      <div className="page-sub">
        Paste a Solana or Base contract address. Runs GoPlus + RugCheck +
        DEXScreener checks — same logic the scanner uses.
      </div>

      {/* Address input + actions */}
      <div className="card" style={{ marginBottom: 16 }}>
        <div style={{ display: 'flex', gap: 8, marginBottom: 8 }}>
          <input
            type="text"
            value={addr}
            onChange={e => setAddr(e.target.value)}
            onKeyDown={e => { if (e.key === 'Enter') runResearch() }}
            placeholder="Paste contract address (Solana or 0x… on Base)"
            style={{ flex: 1, padding: '10px 12px', fontSize: 13,
                     background: 'var(--bg-2)', color: 'var(--text-1)',
                     border: '1px solid var(--bg-3)', borderRadius: 6,
                     fontFamily: 'monospace' }}
          />
          <button
            className="btn btn-amber"
            onClick={() => runResearch()}
            disabled={loading}
          >{loading ? 'Analyzing…' : 'Analyze'}</button>
        </div>
        {error && (
          <div style={{ fontSize: 12, color: '#f87171', marginTop: 4 }}>
            {error}
          </div>
        )}
      </div>

      {/* Two-column: results + watchlist */}
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 320px',
                    gap: 16, alignItems: 'start' }}>

        {/* Left column: results */}
        <div>
          {loading && (
            <div className="card">
              <div className="loading"><div className="spinner" />Running checks…</div>
            </div>
          )}

          {data && !loading && (
            <>
              <ScoreHero score={data.rug_score ?? 0} />

              {/* Action row */}
              {!IS_DEMO && (
                <div style={{ display: 'flex', gap: 8, marginBottom: 16,
                              flexWrap: 'wrap' }}>
                  <button
                    className="btn btn-amber"
                    onClick={handleBuy}
                    disabled={!buyable || buying}
                    title={buyable ? '' : 'Buy requires Solana/Base + DEXScreener price'}
                  >
                    {buying ? 'Submitting…' : 'Buy via bot'}
                  </button>
                  <button
                    className="btn btn-ghost"
                    onClick={handleWatch}
                    disabled={watching}
                  >
                    {watching ? 'Adding…' : 'Add to watchlist'}
                  </button>
                </div>
              )}
              {buyMsg && (
                <div style={{ fontSize: 12, color: 'var(--text-3)',
                              marginBottom: 12 }}>{buyMsg}</div>
              )}

              {/* DEXScreener block */}
              <div className="card" style={{ marginBottom: 16 }}>
                <div className="card-title">DEXScreener — Token info</div>
                <CopyableAddress address={data.token_addr} chain={data.chain} />
                {dex.name || dex.symbol
                  ? <>
                      {dex.name   && <StatRow label="Name"       value={dex.name} />}
                      {dex.symbol && <StatRow label="Symbol"     value={dex.symbol} />}
                      <StatRow label="Price"      value={fmtPrice(dex.price_usd)} />
                      <StatRow label="Liquidity"  value={fmtUsd(dex.liquidity)} />
                      <StatRow label="Volume 24h" value={fmtUsd(dex.volume_24h)} />
                      <StatRow label="Market cap" value={fmtUsd(dex.market_cap)} />
                      <StatRow label="Chain"      value={data.chain.toUpperCase()} />
                    </>
                  : <div style={{ fontSize: 13, color: 'var(--text-3)' }}>
                      No DEXScreener data found for this address.
                    </div>}
              </div>

              {/* GoPlus block */}
              <div className="card" style={{ marginBottom: 16 }}>
                <div className="card-title">GoPlus — On-chain mechanics</div>
                <StatRow
                  label="Honeypot"
                  value={gp.is_honeypot ? '✕ Yes' : '✓ No'}
                  danger={gp.is_honeypot}
                />
                <StatRow
                  label="Buy tax"
                  value={gp.buy_tax != null ? gp.buy_tax + '%' : '—'}
                  danger={(gp.buy_tax || 0) > 5}
                />
                <StatRow
                  label="Sell tax"
                  value={gp.sell_tax != null ? gp.sell_tax + '%' : '—'}
                  danger={(gp.sell_tax || 0) > 5}
                />
                <StatRow
                  label="Owner"
                  value={
                    gp.owner_address === '0x0000000000000000000000000000000000000000'
                      ? '✓ Renounced'
                      : gp.owner_address
                          ? gp.owner_address.slice(0, 10) + '…'
                          : '—'
                  }
                />
              </div>

              {/* Flags */}
              {(data.flags || []).length > 0 && (
                <div className="card" style={{ marginBottom: 16 }}>
                  <div className="card-title">Risk flags</div>
                  <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
                    {data.flags.map((f, i) => (
                      <span key={i} style={{ fontSize: 12, padding: '4px 10px',
                                              background: 'var(--bg-2)',
                                              border: '1px solid #f87171',
                                              color: '#f87171', borderRadius: 4 }}>
                        {f}
                      </span>
                    ))}
                  </div>
                </div>
              )}

              {/* RugCheck risks */}
              {rugRisks.length > 0 && (
                <div className="card" style={{ marginBottom: 16 }}>
                  <div className="card-title">RugCheck — Risk factors</div>
                  <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
                    {rugRisks.map((r, i) => (
                      <span key={i} style={{ fontSize: 12, padding: '4px 10px',
                                              background: 'var(--bg-2)',
                                              border: '1px solid var(--bg-3)',
                                              color: 'var(--text-2)', borderRadius: 4 }}>
                        {r}
                      </span>
                    ))}
                  </div>
                </div>
              )}

              {/* Scanner cached rationale (S71 P2) */}
              {scannerAlert && scannerAlert.flags && scannerAlert.flags.length > 0 && (
                <div className="card" style={{ marginBottom: 16 }}>
                  <div className="card-title" style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                    <span>Scanner — Why we'd alert</span>
                    <span style={{ fontSize: 11, color: 'var(--text-3)', fontWeight: 'normal' }}>
                      score {scannerAlert.score} · {scannerAlert.alerted_at}
                    </span>
                  </div>
                  <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
                    {scannerAlert.flags.map((f, i) => (
                      <span key={i} style={{ fontSize: 12, padding: '4px 10px',
                                              background: 'var(--bg-2)',
                                              border: '1px solid var(--bg-3)',
                                              color: 'var(--text-2)', borderRadius: 4 }}>
                        {f}
                      </span>
                    ))}
                  </div>
                </div>
              )}
            </>
          )}

          {!loading && !data && !error && (
            <div className="card">
              <div style={{ fontSize: 13, color: 'var(--text-3)' }}>
                Enter a contract address above to begin. Bot scanner uses the
                same checks against every alert it sends.
              </div>
            </div>
          )}
        </div>

        {/* Right column: watchlist */}
        <WatchlistPanel
          items={items}
          onSelect={(a) => runResearch(a)}
          onRemove={handleRemove}
        />
      </div>
    </div>
  )
}
