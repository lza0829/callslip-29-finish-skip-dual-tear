import { useEffect, useState } from 'react'
import { api } from '../api'

export default function Take() {
  const [kinds, setKinds] = useState([])
  const [kid, setKid] = useState('')
  const [slip, setSlip] = useState(null)
  const [err, setErr] = useState('')
  const load = () => api.kinds().then(ks => {
    setKinds(ks.filter(k => k.启用))
    if (!kid && ks.length) setKid(String(ks.find(k => k.启用)?.id || ''))
  }).catch(e => setErr(e.message))
  useEffect(() => { load() }, [])
  const onTake = async () => {
    setErr('')
    try {
      const t = await api.take(Number(kid))
      setSlip(t)
      load()
    } catch (e) { setErr(e.message) }
  }
  return (
    <div className="page">
      <header className="hero">
        <p className="eyebrow">发号入队</p>
        <h1>取号</h1>
        <p className="lede">按业务种类发放当日号牌，写入等候队列。</p>
      </header>
      <section className="panel">
        <div className="form row">
          <label>业务种类
            <select value={kid} onChange={e => setKid(e.target.value)}>
              {kinds.map(k => <option key={k.id} value={k.id}>{k.名称}（{k.前缀}）</option>)}
            </select>
          </label>
          <button type="button" onClick={onTake} disabled={!kid}>取号</button>
        </div>
        {err && <p className="err">{err}</p>}
        {slip && (
          <div className="slip" style={{ marginTop: 16 }}>
            <div className="no">{slip.号牌}</div>
            <div className="meta">{slip.业务种类} · {slip.状态} · 位次 {slip.位次}</div>
          </div>
        )}
      </section>
    </div>
  )
}
