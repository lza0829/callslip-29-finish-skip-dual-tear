import { useEffect, useState } from 'react'
import { api } from '../api'

export default function Kinds() {
  const [kinds, setKinds] = useState([])
  const [err, setErr] = useState('')
  const load = () => api.kinds().then(setKinds).catch(e => setErr(e.message))
  useEffect(() => { load() }, [])
  const toggle = async (k) => {
    setErr('')
    try { await api.setKindActive(k.id, !k.启用); load() } catch (e) { setErr(e.message) }
  }
  return (
    <div className="page">
      <header className="hero">
        <p className="eyebrow">号段前缀</p>
        <h1>业务种类</h1>
        <p className="lede">储蓄、对公、理财等号段种类，启停影响取号。</p>
      </header>
      {err && <p className="err">{err}</p>}
      <section className="panel">
        <ul className="plain">
          {kinds.map(k => (
            <li key={k.id}>
              <strong>{k.名称}</strong>
              <span className="tag">{k.前缀}</span>
              · 序号 {k.序号} · {k.启用 ? '启用' : '停用'}
              <div className="actions">
                <button type="button" className="ghost" onClick={() => toggle(k)}>
                  {k.启用 ? '停用' : '启用'}
                </button>
              </div>
            </li>
          ))}
        </ul>
      </section>
    </div>
  )
}
