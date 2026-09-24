import { useEffect, useState } from 'react'
import { createRoot } from 'react-dom/client'
import { BarChart, Bar, CartesianGrid, XAxis, YAxis, Tooltip, ResponsiveContainer } from 'recharts'
import './styles.css'

const API = 'http://localhost:8000'
const faultTypes = ['ApplicationCrash', 'OOMKilled', 'ImagePullError', 'ConfigError', 'PendingScheduling', 'CPUThrottle', 'NetworkLatency']
const pretty = value => String(value || 'unknown').replace(/([A-Z])/g, ' $1').trim()
const time = value => value ? new Date(value).toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }) : '—'

function useApi(path, interval = 0) {
  const [data, setData] = useState(null), [error, setError] = useState('')
  const load = () => fetch(API + path).then(r => { if (!r.ok) throw Error(`Request failed (${r.status})`); return r.json() }).then(setData).catch(e => setError(e.message))
  useEffect(() => { load(); if (interval) { const id = setInterval(load, interval); return () => clearInterval(id) } }, [path, interval])
  return { data, error, reload: load }
}

function Status({ children, tone = '' }) { return <span className={`pill ${tone}`}>{pretty(children)}</span> }
function Empty({ error }) { return <div className="empty">{error ? `Could not reach the API: ${error}` : 'No incidents recorded yet.'}</div> }

function Feed({ incidents, selected, setSelected }) {
  return <section className="panel feed"><div className="panel-head"><div><span className="eyebrow">Live stream</span><h2>Incident feed</h2></div><span className="live"><i /> polling every 10s</span></div>
    {!incidents ? <Empty error="Loading incidents…" /> : incidents.length === 0 ? <Empty /> : <div className="table-wrap"><table><thead><tr><th>Workload</th><th>Error class</th><th>Severity</th><th>Detected</th><th>Status</th></tr></thead><tbody>{incidents.map(item => <tr className={selected?.id === item.id ? 'active' : ''} key={item.id} onClick={() => setSelected(item)}><td><strong>{item.pod_name || 'Unknown workload'}</strong><small>{item.namespace || 'cluster'}</small></td><td>{pretty(item.root_cause_category || item.fault_type)}</td><td><Status tone={`severity-${item.severity}`}>{item.severity}</Status></td><td>{time(item.detected_at)}</td><td><Status>{item.remediation_status}</Status></td></tr>)}</tbody></table></div>}
  </section>
}

function Detail({ incident }) {
  if (!incident) return <section className="panel detail"><span className="eyebrow">Incident detail</span><h2>Select an incident</h2><p className="muted">Choose a row from the live feed to inspect the diagnosis.</p></section>
  let trace = {}; try { trace = JSON.parse(incident.raw_diagnosis_json || '{}') } catch { trace = { raw: incident.raw_diagnosis_json } }
  return <section className="panel detail"><div className="panel-head"><div><span className="eyebrow">Incident #{incident.id}</span><h2>{pretty(incident.root_cause_category || incident.fault_type)}</h2></div><Status tone={`severity-${incident.severity}`}>{incident.severity}</Status></div><div className="facts"><div><label>Error class</label><strong>{pretty(incident.root_cause_category || incident.fault_type)}</strong></div><div><label>Error description</label><strong>{incident.diagnosed_error || incident.root_cause || 'Unavailable'}</strong></div><div><label>Root cause</label><strong>{incident.root_cause || 'Unavailable'}</strong></div><div><label>Confidence</label><strong>{pretty(incident.confidence)}</strong></div><div><label>Remediation</label><strong>{pretty(incident.remediation_status)}</strong></div></div><div className="action"><label>Recommended action</label><code>{incident.recommended_action || 'No action supplied'}</code></div><div className="trace"><label>LLM reasoning trace</label><pre>{JSON.stringify(trace, null, 2)}</pre></div></section>
}

function Metrics({ metrics }) {
  if (!metrics) return <section className="panel metrics"><span className="eyebrow">Telemetry</span><h2>Metrics</h2><div className="empty">Loading metrics…</div></section>
  const faults = faultTypes.map(name => ({ name: pretty(name), count: metrics.by_fault_type?.[name] || 0 })), times = metrics.incidents_over_time || [], timing = [{ name: 'MTTD', seconds: metrics.mttd_seconds_avg || 0 }, { name: 'MTTR', seconds: metrics.mttr_seconds_avg || 0 }]
  return <section className="panel metrics"><div className="metric-total"><div><span className="eyebrow">Telemetry</span><h2>Metrics</h2></div><strong>{metrics.total_incidents || 0}<small> total incidents</small></strong></div><Chart title="By fault type" data={faults} dataKey="count" xKey="name" /><Chart title="Mean response time (seconds)" data={timing} dataKey="seconds" xKey="name" /><Chart title="Incidents over time" data={times} dataKey="count" xKey="bucket" /></section>
}
function Chart({ title, data, dataKey, xKey }) { return <div className="chart"><h3>{title}</h3><ResponsiveContainer width="100%" height={170}><BarChart data={data} margin={{ top: 8, right: 12, left: -18, bottom: 30 }}><CartesianGrid strokeDasharray="3 3" vertical={false} stroke="#dfe5e2" /><XAxis dataKey={xKey} angle={-24} textAnchor="end" height={48} tick={{ fontSize: 10 }} /><YAxis allowDecimals={false} tick={{ fontSize: 10 }} /><Tooltip /><Bar dataKey={dataKey} fill="#e36d4c" radius={[3, 3, 0, 0]} /></BarChart></ResponsiveContainer></div> }

function App() {
  const [tab, setTab] = useState('feed'), [selected, setSelected] = useState(null)
  const incidents = useApi('/incidents', 10000), metrics = useApi('/metrics', 30000), health = useApi('/health', 10000)
  const choose = item => { setSelected(item); setTab('detail'); fetch(`${API}/incidents/${item.id}`).then(r => r.json()).then(setSelected).catch(() => {}) }
  return <main><header><div><span className="eyebrow">Kubernetes / self-healing control room</span><h1>Incident observatory</h1><p>LLM-assisted diagnosis across your cluster.</p></div><div className="health"><i className={health.data?.status === 'ok' ? 'ok' : ''} />{health.data?.status === 'ok' ? 'Daemon online' : 'Checking daemon…'}<small>{health.data?.last_check_timestamp ? `Last check ${time(health.data.last_check_timestamp)}` : ''}</small></div></header><nav>{[['feed','Live feed'],['detail','Incident detail'],['metrics','Metrics']].map(([id, label]) => <button className={tab === id ? 'selected' : ''} onClick={() => setTab(id)} key={id}>{label}</button>)}</nav>{tab === 'feed' && <Feed incidents={incidents.data} selected={selected} setSelected={choose} />}{tab === 'detail' && <Detail incident={selected} />}{tab === 'metrics' && <Metrics metrics={metrics.data} />}{(incidents.error || metrics.error) && <div className="toast">{incidents.error || metrics.error}</div>}</main>
}

createRoot(document.getElementById('root')).render(<App />)
