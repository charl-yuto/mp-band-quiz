import React, { useEffect, useMemo, useState } from 'react'
import Plot from 'react-plotly.js'

const ORBITAL_COLORS = { s: '#E69F00', p: '#009E73', d: '#CC79A7', f: '#7F7F7F' }
const ELEMENT_DASHES = ['solid', 'dash', 'dot', 'dashdot', 'longdash', 'longdashdot']
const ATOM_COLORS = ['#4477AA', '#EE6677', '#228833', '#CCBB44', '#66CCEE', '#AA3377', '#BBBBBB']

const DEFAULT_SETTINGS = {
  api_key: '',
  material_kind: 'any',
  metallicity: 'any',
  stability: 'stable_only',
  nelements_min: 1,
  nelements_max: 3,
  path_type: 'hinuma',
  prefer_cache: false,
  cache_only: false,
  save_cache: false,
  fast_mode: true,
  require_band_dos_props: true,
  use_candidate_cache: false,
  refresh_candidate_pool: true,
  candidate_pool_size: 120,
  candidate_num_chunks: 1,
  max_trials: 6,
  avoid_recent: true,
  recent_limit: 1000,
  energy_min: -8,
  energy_max: 8,
  data_energy_padding: 4,
}

const DEFAULT_VIEW = {
  font_size: 17,
  show_band_legend: false,
  show_dos_legend: true,
  show_band_grid: true,
  band_height: 560,
  show_structure_labels: true,
  show_structure_cell: true,
  show_structure_axes: false,
  supercell: 1,
  atom_size: 8,
  structure_label_size: 14,
  show_bz_edges: true,
  show_kpath: true,
  show_klabels: true,
  show_reciprocal_axes: false,
  show_bz_axes: false,
  kpath_width: 6,
  klabel_size: 15,
}

function normalizeAnswer(s) {
  return String(s || '').trim().replace(/\s+/g, '').replace(/[０-９]/g, d => String.fromCharCode(d.charCodeAt(0) - 0xfee0)).toLowerCase()
}

async function apiPost(path, body) {
  const res = await fetch(path, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  })
  const text = await res.text()
  let data
  try { data = JSON.parse(text) } catch { data = { ok: false, error: text || res.statusText } }
  if (!res.ok) {
    const msg = data.detail || data.error || res.statusText
    throw new Error(typeof msg === 'string' ? msg : JSON.stringify(msg))
  }
  return data
}

async function apiGet(path) {
  const res = await fetch(path)
  const text = await res.text()
  let data
  try { data = JSON.parse(text) } catch { data = { ok: false, error: text || res.statusText } }
  if (!res.ok) throw new Error(data.detail || data.error || res.statusText)
  return data
}

function Select({ value, onChange, options }) {
  return <select value={value} onChange={e => onChange(e.target.value)}>{options.map(o => <option key={o.value} value={o.value}>{o.label}</option>)}</select>
}
function NumberInput({ value, onChange, step = 1, min, max }) {
  const [text, setText] = useState(String(value ?? ''))
  useEffect(() => { setText(String(value ?? '')) }, [value])

  function clamp(n) {
    let x = n
    if (min !== undefined && x < min) x = min
    if (max !== undefined && x > max) x = max
    return x
  }

  function commit() {
    const s = String(text).trim()
    if (s === '' || s === '-' || s === '.' || s === '-.') {
      setText(String(value ?? ''))
      return
    }
    const n = Number(s)
    if (!Number.isFinite(n)) {
      setText(String(value ?? ''))
      return
    }
    const x = clamp(n)
    onChange(x)
    setText(String(x))
  }

  return <input
    type="text"
    inputMode="decimal"
    value={text}
    onChange={e => {
      const s = e.target.value
      // Allow intermediate states such as '-' or '-.' so negative numbers are easy to type.
      if (/^-?(\d+)?(\.\d*)?$/.test(s) || s === '') setText(s)
    }}
    onBlur={commit}
    onKeyDown={e => { if (e.key === 'Enter') commit() }}
    placeholder={String(value ?? '')}
  />
}
function Toggle({ label, checked, onChange, help }) {
  return <button className={'toggle ' + (checked ? 'on' : '')} onClick={() => onChange(!checked)} type="button"><span>{label}{help && <small>{help}</small>}</span><b>{checked ? 'ON' : 'OFF'}</b></button>
}
function Field({ label, children, note }) {
  return <label className="field"><span>{label}</span>{children}{note && <small>{note}</small>}</label>
}
function SettingsCard({ title, desc, children }) {
  return <section className="settingsCard"><div className="settingsHead"><h3>{title}</h3>{desc && <p>{desc}</p>}</div><div className="settingsBody">{children}</div></section>
}
function Pill({ children, tone='blue' }) { return <span className={'pill '+tone}>{children}</span> }

function makeBandPlot(quiz, view, settings) {
  if (!quiz?.band?.segments) return { data: [], layout: {} }
  const traces = []
  const spinStyle = { up: { color: '#005F9E', dash: 'solid', name: 'spin up' }, down: { color: '#D55E00', dash: 'dash', name: 'spin down' } }
  const usedSpin = new Set()
  for (const seg of quiz.band.segments) {
    const style = spinStyle[seg.spin] || { color: '#005F9E', dash: 'solid', name: seg.spin }
    for (const y of seg.bands) {
      traces.push({ x: seg.x, y, type: 'scattergl', mode: 'lines', line: { color: style.color, width: 1.45, dash: style.dash }, name: style.name, showlegend: view.show_band_legend && !usedSpin.has(seg.spin), hoverinfo: 'skip' })
      usedSpin.add(seg.spin)
    }
  }
  const ticks = quiz.band.ticks || { distance: [], label: [] }
  const shapes = [
    { type: 'line', xref: 'paper', x0: 0, x1: 1, y0: 0, y1: 0, line: { color: '#B00020', dash: 'dash', width: 1.3 } },
    ...ticks.distance.map(x => ({ type: 'line', x0: x, x1: x, yref: 'paper', y0: 0, y1: 1, line: { color: '#d5dce8', width: 0.8 } })),
  ]
  return {
    data: traces,
    layout: {
      margin: { l: 66, r: 14, t: 12, b: 62 }, height: view.band_height,
      paper_bgcolor: 'white', plot_bgcolor: 'white',
      font: { size: view.font_size, family: 'Inter, system-ui, sans-serif' },
      xaxis: { title: 'Wave vector', tickvals: ticks.distance, ticktext: ticks.label, showgrid: false, zeroline: false },
      yaxis: { title: 'E − EF (eV)', range: [settings.energy_min, settings.energy_max], showgrid: view.show_band_grid, gridcolor: '#eef2f7', zeroline: false },
      shapes, showlegend: view.show_band_legend,
      legend: { x: 0.98, y: 0.98, xanchor: 'right', bgcolor: 'rgba(255,255,255,0.88)' },
    },
    config: { responsive: true, displaylogo: false, scrollZoom: true },
  }
}

function makeDosPlot(quiz, view, settings) {
  if (!quiz?.dos?.curves) return { data: [], layout: {} }
  const curves = quiz.dos.curves
  const energy = quiz.dos.energy
  const elements = [...new Set(curves.map(c => c.label.split(' ')[0]))]
  const traces = curves.map(c => {
    const elem = c.label.split(' ')[0]
    const idx = elements.indexOf(elem)
    return { x: c.y, y: energy, type: 'scattergl', mode: 'lines', name: c.label, line: { color: ORBITAL_COLORS[c.orbital] || '#64748b', width: 2.6, dash: ELEMENT_DASHES[idx % ELEMENT_DASHES.length] }, showlegend: view.show_dos_legend, hovertemplate: `${c.label}<br>DOS=%{x:.3f}<br>E=%{y:.3f} eV<extra></extra>` }
  })
  return {
    data: traces,
    layout: { margin: { l: 16, r: 12, t: 12, b: 62 }, height: view.band_height, paper_bgcolor: 'white', plot_bgcolor: 'white', font: { size: view.font_size, family: 'Inter, system-ui, sans-serif' }, xaxis: { title: 'DOS', showgrid: true, gridcolor: '#eef2f7', zeroline: false }, yaxis: { range: [settings.energy_min, settings.energy_max], showticklabels: false, showgrid: view.show_band_grid, gridcolor: '#eef2f7', zeroline: false }, shapes: [{ type: 'line', xref: 'paper', x0: 0, x1: 1, y0: 0, y1: 0, line: { color: '#B00020', dash: 'dash', width: 1.3 } }], legend: { orientation: 'v', x: 0.98, y: 0.98, xanchor: 'right', bgcolor: 'rgba(255,255,255,0.88)', font: { size: Math.max(12, view.font_size - 2) } }, showlegend: view.show_dos_legend },
    config: { responsive: true, displaylogo: false, scrollZoom: true },
  }
}

function cellEdgeTraces(lattice, n = 1) {
  const a = lattice[0], b = lattice[1], c = lattice[2]
  const pts = []
  for (const i of [0, n]) for (const j of [0, n]) for (const k of [0, n]) pts.push([i,j,k])
  const corners = pts.map(([i,j,k]) => [i*a[0]+j*b[0]+k*c[0], i*a[1]+j*b[1]+k*c[1], i*a[2]+j*b[2]+k*c[2]])
  const idx = (i,j,k) => (i ? 4 : 0) + (j ? 2 : 0) + (k ? 1 : 0)
  const pairs = []
  for (const j of [0,1]) for (const k of [0,1]) pairs.push([idx(0,j,k), idx(1,j,k)])
  for (const i of [0,1]) for (const k of [0,1]) pairs.push([idx(i,0,k), idx(i,1,k)])
  for (const i of [0,1]) for (const j of [0,1]) pairs.push([idx(i,j,0), idx(i,j,1)])
  const x=[], y=[], z=[]
  for (const [p,q] of pairs) { x.push(corners[p][0], corners[q][0], null); y.push(corners[p][1], corners[q][1], null); z.push(corners[p][2], corners[q][2], null) }
  return { x, y, z, type: 'scatter3d', mode: 'lines', name: 'unit cell', line: { color: '#334155', width: 4 }, hoverinfo: 'skip' }
}

function makeStructurePlot(quiz, view) {
  const st = quiz?.structure
  if (!st?.sites) return { data: [], layout: {} }
  const n = view.supercell
  const lattice = st.lattice
  const sites = []
  for (let i=0;i<n;i++) for (let j=0;j<n;j++) for (let k=0;k<n;k++) {
    for (const s of st.sites) {
      const f = [s.frac[0]+i, s.frac[1]+j, s.frac[2]+k]
      const cart = [f[0]*lattice[0][0]+f[1]*lattice[1][0]+f[2]*lattice[2][0], f[0]*lattice[0][1]+f[1]*lattice[1][1]+f[2]*lattice[2][1], f[0]*lattice[0][2]+f[1]*lattice[1][2]+f[2]*lattice[2][2]]
      sites.push({ ...s, cart })
    }
  }
  const species = [...new Set(sites.map(s => s.species))]
  const traces = species.map((sp, idx) => {
    const ss = sites.filter(s => s.species === sp)
    return { x: ss.map(s => s.cart[0]), y: ss.map(s => s.cart[1]), z: ss.map(s => s.cart[2]), type: 'scatter3d', mode: view.show_structure_labels ? 'markers+text' : 'markers', text: view.show_structure_labels ? ss.map(() => sp) : undefined, textfont: { size: view.structure_label_size, color: '#0f172a' }, name: sp, marker: { size: view.atom_size, color: ATOM_COLORS[idx % ATOM_COLORS.length], opacity: 0.95 } }
  })
  if (view.show_structure_cell) traces.push(cellEdgeTraces(lattice, n))
  return { data: traces, layout: { height: 660, margin: { l: 0, r: 0, t: 0, b: 0 }, paper_bgcolor: 'white', font: { size: view.font_size }, scene: { xaxis: { visible: view.show_structure_axes }, yaxis: { visible: view.show_structure_axes }, zaxis: { visible: view.show_structure_axes }, aspectmode: 'data' }, legend: { x: 0, y: 1, bgcolor: 'rgba(255,255,255,0.8)' } }, config: { responsive: true, displaylogo: false, scrollZoom: true } }
}

function makeBZPlot(quiz, view) {
  const bz = quiz?.bz
  if (!bz?.vertices) return { data: [], layout: {} }
  const traces = []
  if (view.show_bz_edges && bz.edges?.length) {
    const x=[], y=[], z=[]
    for (const [i,j] of bz.edges) { const a=bz.vertices[i], b=bz.vertices[j]; x.push(a[0],b[0],null); y.push(a[1],b[1],null); z.push(a[2],b[2],null) }
    traces.push({ x,y,z, type:'scatter3d', mode:'lines', name:'BZ edges', line:{color:'#64748b',width:3}, hoverinfo:'skip' })
  }
  if (view.show_kpath) {
    for (const br of bz.branches || []) traces.push({ x: br.points.map(p=>p[0]), y: br.points.map(p=>p[1]), z: br.points.map(p=>p[2]), type:'scatter3d', mode:'lines+markers', name:`path ${br.branch_index}`, line:{color:'#B00020',width:view.kpath_width}, marker:{size:3,color:'#B00020'}, hoverinfo:'skip', showlegend:false })
  }
  if (view.show_klabels) {
    const labs = bz.labels || []
    traces.push({ x: labs.map(l=>l.point[0]), y: labs.map(l=>l.point[1]), z: labs.map(l=>l.point[2]), text: labs.map(l=>l.label), type:'scatter3d', mode:'text', name:'labels', textfont:{size:view.klabel_size,color:'#111827'}, hoverinfo:'skip', showlegend:false })
  }
  return { data: traces, layout: { height: 660, margin:{l:0,r:0,t:0,b:0}, paper_bgcolor:'white', font:{size:view.font_size}, scene:{ xaxis:{visible:view.show_bz_axes}, yaxis:{visible:view.show_bz_axes}, zaxis:{visible:view.show_bz_axes}, aspectmode:'data' }, showlegend:false }, config:{responsive:true,displaylogo:false,scrollZoom:true} }
}

function SectionTitle({ title, subtitle }) { return <div className="sectionTitle"><h2>{title}</h2>{subtitle && <p>{subtitle}</p>}</div> }
function EmptyPlot() { return <div className="emptyPlot"><b>まだ問題がありません</b><span>「ライブランダム出題」を押してください。</span></div> }

export default function App() {
  const [settings, setSettings] = useState(DEFAULT_SETTINGS)
  const [view, setView] = useState(DEFAULT_VIEW)
  const [quiz, setQuiz] = useState(null)
  const [answer, setAnswer] = useState('')
  const [result, setResult] = useState(null)
  const [revealed, setRevealed] = useState(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [tab, setTab] = useState('band')
  const [lastInfo, setLastInfo] = useState('')
  const [cacheCount, setCacheCount] = useState(null)

  const bandPlot = useMemo(() => makeBandPlot(quiz, view, settings), [quiz, view, settings.energy_min, settings.energy_max])
  const dosPlot = useMemo(() => makeDosPlot(quiz, view, settings), [quiz, view, settings.energy_min, settings.energy_max])
  const structurePlot = useMemo(() => makeStructurePlot(quiz, view), [quiz, view])
  const bzPlot = useMemo(() => makeBZPlot(quiz, view), [quiz, view])

  const setS = (key, val) => setSettings(s => ({ ...s, [key]: val }))
  const setV = (key, val) => setView(s => ({ ...s, [key]: val }))

  async function refreshCacheCount() {
    try { const data = await apiGet('/api/cache/list'); setCacheCount(data.items?.length ?? 0) } catch {}
  }

  async function newQuiz(cacheOnlyOverride = null, overrides = {}) {
    setLoading(true); setError(''); setResult(null); setRevealed(null); setAnswer('')
    const t0 = performance.now()
    try {
      const payload = { ...settings, ...overrides }
      if (cacheOnlyOverride !== null) payload.cache_only = cacheOnlyOverride
      const data = await apiPost('/api/quiz/new', payload)
      setQuiz(data)
      setLastInfo(`取得 ${((performance.now()-t0)/1000).toFixed(2)} 秒 / backend ${data.timing_sec ?? '?'} 秒`)
      refreshCacheCount()
    } catch (e) { setError(e.message) }
    finally { setLoading(false) }
  }

  async function check() {
    if (!quiz) return
    setError('')
    try {
      const data = await apiPost('/api/quiz/check', { quiz_id: quiz.quiz_id, answer })
      setResult(data.correct ? 'correct' : 'wrong')
    } catch(e) { setError(e.message) }
  }

  async function reveal() {
    if (!quiz) return
    try { setRevealed(await apiPost('/api/quiz/reveal', { quiz_id: quiz.quiz_id })) }
    catch(e) { setError(e.message) }
  }

  const hint = quiz?.hint
  return <div className="app">
    <header className="topbar">
      <div>
        <span className="badge">MP Band Quiz v8</span>
        <h1>バンド図・DOS クイズ</h1>
        <p>MP の実データを使い、匿名組成式・結晶情報・band/DOS から物質を当てます。候補検索を軽くし、問題キャッシュなしでランダム出題します。</p>
      </div>
      <div className="topActions">
        <button className="primary" onClick={() => newQuiz(false)} disabled={loading}>{loading ? '取得中...' : 'ライブランダム出題'}</button>
        <button onClick={() => newQuiz(false, { refresh_candidate_pool: true, use_candidate_cache: false })} disabled={loading}>候補を再検索して出題</button>
      </div>
    </header>

    {error && <div className="toast error">{error}</div>}
    {lastInfo && <div className="toast info">{lastInfo}{cacheCount !== null && ` / cache ${cacheCount} 件`}</div>}

    <main className="mainGrid">
      <section className="leftPane">
        <div className="card answerCard">
          <SectionTitle title="回答" subtitle="答えは元素記号または化学式で入力できます。" />
          {hint ? <div className="hintBox">
            <div><span>匿名組成式</span><b>{hint.anonymous_formula}</b></div>
            <div><span>結晶系</span><b>{hint.crystal_system || '-'}</b></div>
            <div><span>空間群</span><b>{hint.spacegroup_symbol || '-'} No. {hint.spacegroup_number || '-'}</b></div>
            <div><span>サイト数</span><b>{hint.nsites ?? '-'}</b></div>
            <div><span>性質</span><b>{hint.is_metal ? 'metal' : 'nonmetal'}</b></div>
          </div> : <div className="hintBox blank">まだ出題されていません。</div>}
          <div className="answerRow"><input value={answer} onChange={e=>{setAnswer(e.target.value); setResult(null)}} onKeyDown={e=>{if(e.key==='Enter') check()}} placeholder="例: Cr, Si, H2O" /><button onClick={check} disabled={!quiz}>回答</button></div>
          <div className="answerTools"><button onClick={reveal} disabled={!quiz}>答え表示</button><button onClick={() => {setAnswer(''); setResult(null); setRevealed(null)}} disabled={!quiz}>もう一度</button></div>
          {result === 'correct' && <div className="judge correct">○ 正解です</div>}
          {result === 'wrong' && <div className="judge wrong">× 違います。もう一度。</div>}
          {revealed && <div className="reveal">答え: <b>{revealed.answer}</b><br/>MP-ID: {revealed.mpid}<br/>A対応: {JSON.stringify(revealed.alias_map)}</div>}
        </div>

        <SettingsCard title="出題条件" desc="ここだけが MP 検索に効きます。">
          <Field label="MP API key"><input type="password" value={settings.api_key} onChange={e=>setS('api_key', e.target.value)} placeholder="空ならサーバー側 MP_API_KEY" /></Field>
          <div className="grid2"><Field label="種類"><Select value={settings.material_kind} onChange={v=>setS('material_kind', v)} options={[{value:'any',label:'なんでも'},{value:'element',label:'単体のみ'},{value:'compound',label:'化合物のみ'}]} /></Field><Field label="金属性"><Select value={settings.metallicity} onChange={v=>setS('metallicity', v)} options={[{value:'any',label:'なんでも'},{value:'metal',label:'金属のみ'},{value:'nonmetal',label:'非金属のみ'}]} /></Field></div>
          <div className="grid2"><Field label="安定性"><Select value={settings.stability} onChange={v=>setS('stability', v)} options={[{value:'stable_only',label:'安定のみ'},{value:'any',label:'問わない'}]} /></Field><Field label="k-path"><Select value={settings.path_type} onChange={v=>setS('path_type', v)} options={[{value:'hinuma',label:'Hinuma'},{value:'setyawan_curtarolo',label:'Setyawan-Curtarolo'},{value:'latimer_munro',label:'Latimer-Munro'}]} /></Field></div>
          <div className="grid2"><Field label="元素数 min"><NumberInput value={settings.nelements_min} onChange={v=>setS('nelements_min',v)} min={1}/></Field><Field label="元素数 max"><NumberInput value={settings.nelements_max} onChange={v=>setS('nelements_max',v)} min={1}/></Field></div>
        </SettingsCard>

        <SettingsCard title="検索高速化" desc="問題の丸ごとキャッシュや事前取得は使わず、MP検索そのものを軽くします。">
          <Toggle label="band/DOSがある候補に絞る" checked={settings.require_band_dos_props} onChange={v=>setS('require_band_dos_props', v)} help="失敗候補を減らす。通常ON推奨" />
          <Toggle label="最近出た物質を避ける" checked={settings.avoid_recent} onChange={v=>setS('avoid_recent', v)} help="同じmp-idの連続出題を避ける" />
          <Toggle label="候補リストを保存" checked={settings.use_candidate_cache} onChange={v=>setS('use_candidate_cache', v)} help="mp-id一覧だけ保存。OFFなら毎回MPを探しに行く" />
          <Toggle label="候補リストを毎回作り直す" checked={settings.refresh_candidate_pool} onChange={v=>setS('refresh_candidate_pool', v)} help="候補リスト保存ONのときだけ意味があります" />
          <div className="grid2"><Field label="候補数"><NumberInput value={settings.candidate_pool_size} onChange={v=>setS('candidate_pool_size',v)} min={20}/></Field><Field label="候補チャンク数"><NumberInput value={settings.candidate_num_chunks} onChange={v=>setS('candidate_num_chunks',v)} min={1} max={20}/></Field></div>
          <div className="grid2"><Field label="試行数"><NumberInput value={settings.max_trials} onChange={v=>setS('max_trials',v)} min={1}/></Field><Field label="最近回避数"><NumberInput value={settings.recent_limit} onChange={v=>setS('recent_limit',v)} min={10}/></Field></div>
          <p className="smallNote">推奨: band/DOS候補絞りON、候補数80〜150、チャンク数1、試行数4〜8。キャッシュを使わずランダム性を保つ設定です。</p>
        </SettingsCard>
      </section>

      <section className="rightPane">
        <nav className="tabs"><button className={tab==='band'?'active':''} onClick={()=>setTab('band')}>バンド/DOS</button><button className={tab==='structure'?'active':''} onClick={()=>setTab('structure')}>結晶構造</button><button className={tab==='bz'?'active':''} onClick={()=>setTab('bz')}>BZ/k-path</button></nav>

        {tab === 'band' && <>
          <SettingsCard title="バンド/DOS 表示設定" desc="ここは表示だけを変えます。MP APIには再アクセスしません。">
            <div className="grid4"><Field label="E下限"><NumberInput value={settings.energy_min} onChange={v=>setS('energy_min',v)} step={0.5}/></Field><Field label="E上限"><NumberInput value={settings.energy_max} onChange={v=>setS('energy_max',v)} step={0.5}/></Field><Field label="フォント"><NumberInput value={view.font_size} onChange={v=>setV('font_size',v)} min={10} max={28}/></Field><Field label="高さ"><NumberInput value={view.band_height} onChange={v=>setV('band_height',v)} min={360} max={900} step={20}/></Field></div>
            <div className="switchRow"><Toggle label="DOS凡例" checked={view.show_dos_legend} onChange={v=>setV('show_dos_legend', v)} /><Toggle label="Band凡例" checked={view.show_band_legend} onChange={v=>setV('show_band_legend', v)} /><Toggle label="grid" checked={view.show_band_grid} onChange={v=>setV('show_band_grid', v)} /></div>
          </SettingsCard>
          {quiz ? <div className="plotGrid"><div className="plotCard"><Plot {...bandPlot} style={{width:'100%'}} /></div><div className="plotCard"><Plot {...dosPlot} style={{width:'100%'}} /></div></div> : <EmptyPlot />}
        </>}

        {tab === 'structure' && <>
          <SettingsCard title="結晶構造 表示設定" desc="ドラッグで回転、ホイールでズームできます。">
            <div className="grid4"><Field label="supercell"><NumberInput value={view.supercell} onChange={v=>setV('supercell', Math.max(1, Math.min(3, v)))} min={1} max={3}/></Field><Field label="原子サイズ"><NumberInput value={view.atom_size} onChange={v=>setV('atom_size',v)} min={2} max={20}/></Field><Field label="ラベルサイズ"><NumberInput value={view.structure_label_size} onChange={v=>setV('structure_label_size',v)} min={8} max={28}/></Field><Field label="フォント"><NumberInput value={view.font_size} onChange={v=>setV('font_size',v)} min={10} max={28}/></Field></div>
            <div className="switchRow"><Toggle label="原子ラベル" checked={view.show_structure_labels} onChange={v=>setV('show_structure_labels', v)} /><Toggle label="単位胞枠" checked={view.show_structure_cell} onChange={v=>setV('show_structure_cell', v)} /><Toggle label="軸表示" checked={view.show_structure_axes} onChange={v=>setV('show_structure_axes', v)} /></div>
          </SettingsCard>
          {quiz ? <div className="plotCard"><Plot {...structurePlot} style={{width:'100%'}} /></div> : <EmptyPlot />}
        </>}

        {tab === 'bz' && <>
          <SettingsCard title="BZ/k-path 表示設定" desc="k-path の形を確認するための表示です。">
            <div className="grid4"><Field label="線太さ"><NumberInput value={view.kpath_width} onChange={v=>setV('kpath_width',v)} min={1} max={12}/></Field><Field label="ラベルサイズ"><NumberInput value={view.klabel_size} onChange={v=>setV('klabel_size',v)} min={8} max={28}/></Field><Field label="フォント"><NumberInput value={view.font_size} onChange={v=>setV('font_size',v)} min={10} max={28}/></Field></div>
            <div className="switchRow"><Toggle label="BZ辺" checked={view.show_bz_edges} onChange={v=>setV('show_bz_edges', v)} /><Toggle label="k-path" checked={view.show_kpath} onChange={v=>setV('show_kpath', v)} /><Toggle label="k点ラベル" checked={view.show_klabels} onChange={v=>setV('show_klabels', v)} /><Toggle label="数値軸" checked={view.show_bz_axes} onChange={v=>setV('show_bz_axes', v)} /></div>
          </SettingsCard>
          {quiz ? <div className="plotCard"><Plot {...bzPlot} style={{width:'100%'}} /></div> : <EmptyPlot />}
        </>}
      </section>
    </main>
  </div>
}
