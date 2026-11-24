import { useEffect, useState } from 'react'
import { api, requestKey } from '../api'

export default function Call() {
  const [wins, setWins] = useState([])
  const [kinds, setKinds] = useState([])
  const [wid, setWid] = useState('')
  const [kid, setKid] = useState('')
  const [events, setEvents] = useState([])
  const [msg, setMsg] = useState('')
  const [err, setErr] = useState('')
  const load = async () => {
    try {
      const [w, k, e] = await Promise.all([api.windows(), api.kinds(), api.events()])
      setWins(w)
      setKinds(k)
      setEvents(e)
      if (!wid && w.length) setWid(String(w[0].id))
    } catch (ex) { setErr(ex.message) }
  }
  useEffect(() => { load() }, [])
  const onCall = async () => {
    setErr(''); setMsg('')
    try {
      const t = await api.call(Number(wid), kid ? Number(kid) : null)
      setMsg(`已叫 ${t.号牌}，窗口 ${t.窗口}`)
      load()
    } catch (ex) { setErr(ex.message) }
  }
  const onFinish = async (id) => {
    setErr(''); setMsg('')
    try {
      const r = await api.finish(id, requestKey())
      setMsg(r.重放 ? `重放原结局：${r.号牌.号牌} 已办结` : `${r.号牌.号牌} 已办结，窗口已释放`)
      load()
    } catch (ex) { setErr(ex.message) }
  }
  const onSkip = async (id) => {
    setErr(''); setMsg('')
    try {
      const r = await api.skip(id, requestKey())
      setMsg(r.重放 ? `重放原结局：${r.号牌.号牌} 已过号` : `${r.号牌.号牌} 已过号，进入回收列表`)
      load()
    } catch (ex) {
      // 争用/硬拒：展示后端中文拒因（如已办结则过号整笔拒绝）
      setErr(ex.status === 409 ? `写口被拒：${ex.message}` : ex.message)
    }
  }
  const current = wins.find(w => String(w.id) === String(wid))?.当前号
  return (
    <div className="page">
      <header className="hero">
        <p className="eyebrow">窗口推进</p>
        <h1>叫号</h1>
        <p className="lede">选择窗口叫下一等候号，占窗办理，可办结或过号。</p>
      </header>
      <section className="panel">
        <div className="form row">
          <label>窗口
            <select value={wid} onChange={e => setWid(e.target.value)}>
              {wins.map(w => <option key={w.id} value={w.id}>{w.名称}{w.开放 ? '' : '（关）'}</option>)}
            </select>
          </label>
          <label>限定种类（可空）
            <select value={kid} onChange={e => setKid(e.target.value)}>
              <option value="">不限</option>
              {kinds.map(k => <option key={k.id} value={k.id}>{k.名称}</option>)}
            </select>
          </label>
          <button type="button" onClick={onCall}>叫下一号</button>
        </div>
        {err && <p className="err">{err}</p>}
        {msg && <p className="muted">{msg}</p>}
        {current && (
          <div className="slip" style={{ marginTop: 16 }}>
            <div className="no">{current.号牌}</div>
            <div className="meta">{current.业务种类} · {current.状态}</div>
            <div className="actions">
              <button type="button" onClick={() => onFinish(current.id)}>办结</button>
              <button type="button" className="ghost" onClick={() => onSkip(current.id)}>过号</button>
            </div>
          </div>
        )}
      </section>
      <section className="panel">
        <h2>叫号流水</h2>
        <ul className="plain">
          {events.map(e => (
            <li key={e.id}>{e.时刻} · {e.窗口} · {e.动作} · {e.号牌}</li>
          ))}
        </ul>
      </section>
    </div>
  )
}
