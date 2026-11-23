import { useEffect, useState } from 'react'
import { api } from '../api'

export default function Finished() {
  const [rows, setRows] = useState([])
  const [ledgers, setLedgers] = useState({})
  const [openId, setOpenId] = useState(null)
  const [err, setErr] = useState('')

  async function load() {
    try {
      setErr('')
      setRows(await api.tickets('已办结'))
    } catch (e) {
      setErr(e.message)
    }
  }

  useEffect(() => { load() }, [])

  async function toggleLedger(id) {
    if (openId === id) { setOpenId(null); return }
    setOpenId(id)
    if (!ledgers[id]) {
      try {
        const led = await api.intents(id)
        setLedgers((m) => ({ ...m, [id]: led }))
      } catch (e) {
        setErr(e.message)
      }
    }
  }

  return (
    <div className="page">
      <header className="hero">
        <p className="eyebrow">终态留痕</p>
        <h1>办结页</h1>
        <p className="lede">
          口径「{`办结写口优先`}」：仅已办结号牌进入本页；已办结不会回流过号回收。
        </p>
      </header>
      {err && <p className="err">{err}</p>}
      <section className="panel">
        <div className="card-grid">
          {rows.length === 0 && <p className="muted">暂无已办结号牌</p>}
          {rows.map((t) => (
            <div className="slip" key={t.id}>
              <div className="no">{t.号牌}</div>
              <div className="meta">
                {t.业务种类} · {t.窗口 || '未分窗'} · {t.状态}
              </div>
              <div className="meta">办结时刻：{t.办结时刻 || '—'}</div>
              <div className="actions">
                <button type="button" className="ghost" onClick={() => toggleLedger(t.id)}>
                  {openId === t.id ? '收起台账' : '意图台账'}
                </button>
              </div>
              {openId === t.id && (
                <ul className="plain" style={{ marginTop: 8 }}>
                  {(ledgers[t.id] || []).map((i) => (
                    <li key={i.意图编号}>
                      #{i.意图编号} {i.意图} · {i.结局}
                      {i.拒因 ? ` · 拒因：${i.拒因}` : ''}
                      {i.胜方意图编号 ? ` · 胜方#${i.胜方意图编号}` : ''}
                      <br />
                      <span className="muted">
                        争用前快照：{i.争用前状态}（窗口 {i.争用前窗口 ?? '—'}） · {i.时刻}
                      </span>
                    </li>
                  ))}
                  {ledgers[t.id]?.length === 0 && <li className="muted">无意图记录</li>}
                </ul>
              )}
            </div>
          ))}
        </div>
      </section>
      <button type="button" className="ghost" onClick={load}>刷新</button>
    </div>
  )
}
