import {useEffect, useState} from 'react'

type Row=Record<string,unknown>
type Series={at:string;bot_jpy:string;benchmark_jpy:string|null;bot_return:string;benchmark_return:string|null}
type Document={schema_version:1;state:'PREPARING'|'ACTIVE';updated_at:string;started_at:string;
 summary:Row;series:Series[];holdings:Row[];trades:Row[];decisions:Row[];usage:Row;warnings:string[]}
type Config={url:string;key:string}
const number=(value:unknown)=>value===null || value===undefined || value===''?null:Number(value)
const amount=(v:unknown,currency='JPY')=>{const n=number(v);return n===null || !Number.isFinite(n)?'—':new Intl.NumberFormat('ja-JP',{style:'currency',currency,maximumFractionDigits:currency==='JPY'?0:2}).format(n)}
const percent=(v:unknown)=>{const n=number(v);return n===null||!Number.isFinite(n)?'—':`${(n*100).toFixed(2)}%`}
const time=(v:unknown)=>typeof v==='string'&&!Number.isNaN(Date.parse(v))?new Date(v).toLocaleString('ja-JP',{timeZone:'Asia/Tokyo',hour12:false})+' JST':'—'
const text=(v:unknown)=>typeof v==='string'?v:'—'
const nested=(v:unknown,key:string)=>v&&typeof v==='object'?(v as Row)[key]:null
function validate(input:unknown):Document {
 if (!input||typeof input!=='object') throw Error('invalid public data')
 const d=input as Document
 if(d.schema_version!==1 || !['ACTIVE','PREPARING'].includes(d.state) || !d.summary || !d.usage ||
   !Number.isFinite(Date.parse(d.updated_at)) || ![d.series,d.holdings,d.trades,d.decisions,d.warnings].every(Array.isArray)) throw Error('invalid public data')
 if(d.series.length>5000 || d.series.some(s=>!Number.isFinite(Date.parse(s.at)) ||
   ![s.bot_jpy,s.bot_return].every(v=>v!==null&&Number.isFinite(Number(v))) ||
   ![s.benchmark_jpy,s.benchmark_return].every(v=>v===null||Number.isFinite(Number(v))))) throw Error('invalid series')
 const isRow=(r:unknown):r is Row=>Boolean(r&&typeof r==='object'&&!Array.isArray(r))
 const numeric=(v:unknown)=>typeof v==='string'&&v.trim()!==''&&Number.isFinite(Number(v))
 const currency=(v:unknown)=>v==='JPY'||v==='USD'
 const stamp=(v:unknown)=>typeof v==='string'&&Number.isFinite(Date.parse(v))
 if(d.holdings.length>300||d.trades.length>100||d.decisions.length>100||
  d.holdings.some(r=>!isRow(r)||typeof r.symbol!=='string'||typeof r.name!=='string'||!currency(r.currency)||
   !Number.isInteger(r.quantity)||Number(r.quantity)<0||![r.average_cost,r.market_value_jpy,r.unrealized_pnl,r.ratio].every(numeric)||!stamp(r.at))||
  d.trades.some(r=>!isRow(r)||!['BUY','SELL'].includes(String(r.side))||!currency(r.currency)||
   ![r.price,r.commission,r.realized_pnl].every(numeric)||!stamp(r.at)||typeof r.reason!=='string')||
  d.decisions.some(r=>!isRow(r)||!stamp(r.at)||!Array.isArray(r.items)||r.items.length>300||
   r.items.some((v:unknown)=>!isRow(v)||typeof v.symbol!=='string'||!['BUY','SELL','HOLD'].includes(String(v.action))||
    typeof v.reason!=='string'||(v.skipped!==null&&v.skipped!==undefined&&typeof v.skipped!=='string')))) throw Error('invalid public records')
 return d
}
async function read():Promise<Document|null>{
 const controller=new AbortController(); const timeout=setTimeout(()=>controller.abort(),12000)
 try {
  const configResponse=await fetch(`${import.meta.env.BASE_URL}runtime-config.json`,{signal:controller.signal,cache:'no-cache'})
  if(!configResponse.ok) throw Error('configuration unavailable')
  const config:Config=await configResponse.json()
  if(!config.url||!config.key) return null
  if(new URL(config.url).protocol!=='https:') throw Error('configuration invalid')
  const response=await fetch(`${config.url.replace(/\/$/,'')}/rest/v1/bot_dashboard?id=eq.paper-live&select=document`,{
   headers:{apikey:config.key},signal:controller.signal,cache:'no-cache'})
  if(!response.ok) throw Error('data unavailable')
  const rows:unknown=await response.json()
  if(!Array.isArray(rows)) throw Error('invalid data')
  return rows.length?validate(rows[0].document):null
 } finally {clearTimeout(timeout)}
}
const tabs=['概要','資産推移','保有銘柄','売買履歴','AI判断','API利用']
function Chart({data,mode}:{data:Series[];mode:'assets'|'returns'}){
 const [period,setPeriod]=useState(0)
 const last=data.at(-1)
 const rows=last?data.filter(s=>!period||Date.parse(s.at)>=Date.parse(last.at)-period*86400000):[]
 const keys=mode==='assets'?['bot_jpy','benchmark_jpy'] as const:['bot_return','benchmark_return'] as const
 const points=rows.flatMap(s=>keys.map(k=>number(s[k]))).filter((n):n is number=>n!==null&&Number.isFinite(n))
 const min=points.length?Math.min(...points):0,max=points.length?Math.max(...points):1,span=max-min||Math.max(Math.abs(max)*0.02,0.01)
 const coords=(k:typeof keys[number])=>rows.map((r,i)=>({v:number(r[k]),x:50+i*610/Math.max(rows.length-1,1),y:0,at:Date.parse(r.at)})).map(p=>({...p,y:190-((p.v??min)-min)/span*150}))
 const line=(k:typeof keys[number])=>coords(k).map((p,i,a)=>p.v===null?'':`${i===0||a[i-1].v===null||p.at-a[i-1].at>86400000*1.5?'M':'L'}${p.x},${p.y}`).join(' ')
 return <section className="dash-panel chart-panel" aria-label={mode==='assets'?'資産推移グラフ':'累積リターングラフ'}>
  <div className="dash-panel-head"><div><p className="eyebrow">{mode==='assets'?'ASSET HISTORY':'CUMULATIVE RETURN'}</p><h2>{mode==='assets'?'資産の推移':'ふたつの運用、その差を追う。'}</h2></div>
  <div className="periods" aria-label="表示期間">{[[0,'全期間'],[30,'30日'],[7,'7日']].map(([n,label])=><button key={n} aria-pressed={period===n} onClick={()=>setPeriod(Number(n))}>{label}</button>)}</div></div>
  {rows.length?<><svg role="img" aria-label={mode==='assets'?'AIとオルカンの資産推移':'AIとオルカンの累積リターン'} viewBox="0 0 720 240">
  {[40,115,190].map(y=><line key={y} x1="50" x2="680" y1={y} y2={y} stroke="currentColor" opacity=".12"/>)}
  {keys.map((k,index)=><g key={k}><path d={line(k)} fill="none" stroke={index?'#d7b878':'#d8eea5'} strokeWidth="2.5"/>{coords(k).filter(p=>p.v!==null).map((p,i)=><circle key={i} cx={p.x} cy={p.y} r="3.5" fill={index?'#d7b878':'#d8eea5'}/>)}</g>)}
  <text x="50" y="225" fill="currentColor" fontSize="11">{new Date(rows[0].at).toLocaleDateString('ja-JP',{timeZone:'Asia/Tokyo'})}</text><text x="660" y="225" fill="currentColor" fontSize="11" textAnchor="end">{new Date(rows.at(-1)!.at).toLocaleDateString('ja-JP',{timeZone:'Asia/Tokyo'})}</text>
  <text x="50" y="25" fill="currentColor" fontSize="11">{mode==='assets'?amount(max):percent(max)} → {mode==='assets'?amount(min):percent(min)}</text></svg>
  <details className="chart-values"><summary>評価値を表で確認</summary><div className="table-scroll"><table><thead><tr><th>基準日時 JST</th><th>AI</th><th>オルカン</th></tr></thead><tbody>{rows.map(r=><tr key={r.at}><td>{time(r.at)}</td><td>{mode==='assets'?amount(r.bot_jpy):percent(r.bot_return)}</td><td>{mode==='assets'?amount(r.benchmark_jpy):percent(r.benchmark_return)}</td></tr>)}</tbody></table></div></details></>:<div className="chart-empty"><div className="chart-grid"/><p>まだ比較できる評価履歴がありません</p><span>実データが揃うと、ここに推移を表示します。</span></div>}
  <div className="legend"><span><i/>Gemini Bot</span><span><i className="benchmark-dot"/>オルカン</span><span>欠損日は補間しません</span></div>
 </section>
}
function Empty({children}:{children:string}){return <p className="dash-empty">{children}</p>}
export function Dashboard(){
 const [document,setDocument]=useState<Document|null>(null),[loading,setLoading]=useState(true),[error,setError]=useState(false),[tab,setTab]=useState(0)
 const reload=()=>{setLoading(true);setError(false);read().then(setDocument).catch(()=>setError(true)).finally(()=>setLoading(false))}
 useEffect(()=>{reload()},[])
 const summary=document?.summary
 const preparing=!document||document.state==='PREPARING'
 return <section className="dashboard" id="dashboard" aria-label="仮想運用ダッシュボード">
  <div className="dash-top"><div><p className="eyebrow"><span className="status-dot"/> LIVE PAPER EXPERIMENT</p><h2>運用の現在地</h2><p className="muted">実資金を使わず、同じ100万円から比較する。</p></div><button className="refresh" disabled={loading} onClick={reload}>{loading?'読み込み中…':'最新データに更新'} <span aria-hidden="true">↻</span></button></div>
  <div className="dash-status" role="status"><span>{preparing?'まだ運用開始前':'仮想運用中'}</span><span>最終正常更新：{time(document?.updated_at)}</span></div>
  {error&&<p className="dash-alert" role="alert">公開データを取得できませんでした。{document?'最後に取得した情報を表示しています。':'時間をおいて再度お試しください。'}</p>}
  {!document&&!loading&&!error&&<p className="dash-notice">公開データを準備しています。架空の運用成績は表示しません。</p>}
  <nav className="dash-tabs" aria-label="運用データの表示">{tabs.map((t,i)=><button key={t} aria-pressed={tab===i} onClick={()=>setTab(i)}>{t}</button>)}</nav>
  {tab===0&&<><div className="metric-grid">{[
   ['GEMINI BOT','仮想総資産',amount(summary?.bot_jpy),`累積損益 ${amount(summary?.bot_pnl_jpy)} / ${percent(summary?.bot_return)}`],
   ['ALL COUNTRY','オルカン評価額',amount(summary?.benchmark_jpy),`累積リターン ${percent(summary?.benchmark_return)}`],
   ['DIFFERENCE','超過リターン',percent(summary?.excess_return),`超過損益 ${amount(summary?.excess_pnl_jpy)}`],
   ['RISK / ACTIVITY','最大ドローダウン',percent(summary?.maximum_drawdown),`約定 ${summary?.trade_count??0} 件`],
  ].map(([en,label,value,note])=><article className="metric" key={en}><p className="eyebrow">{en}</p><h3>{label}</h3><p className="metric-value">{value}</p><p className="muted">{note}</p></article>)}</div>
  <Chart data={document?.series??[]} mode="returns"/>
  <section className="dash-panel"><h2>資産構成と売買の記録</h2><dl className="usage-grid">{[
   ['株式比率',percent(summary?.stock_ratio)],['現金比率',percent(summary?.cash_ratio)],
   ['日本株 / 米国株',`${percent(nested(summary?.market_ratios,'JP'))} / ${percent(nested(summary?.market_ratios,'US'))}`],
   ['確定損益 JPY / USD',`${amount(nested(summary?.realized_pnl,'JPY'))} / ${amount(nested(summary?.realized_pnl,'USD'),'USD')}`],
   ['含み損益 JPY / USD',`${amount(nested(summary?.unrealized_pnl,'JPY'))} / ${amount(nested(summary?.unrealized_pnl,'USD'),'USD')}`],
   ['売買コスト JPY / USD',`${amount(nested(summary?.costs,'JPY'))} / ${amount(nested(summary?.costs,'USD'),'USD')}`],
   ['売却約定の勝率',percent(summary?.win_rate)],['USD/JPY 観測時刻',time(summary?.fx_at)],
  ].map(([label,value])=><div key={label}><dt>{label}</dt><dd>{value}</dd></div>)}</dl></section>
  <div className="dashboard-notes"><p>評価通貨：JPY · 税引前・売買コスト反映後</p><p>オルカン基準価額・公開許諾・評価日が揃わない場合、比較値は「—」になります。</p><p>判断は注文・約定を意味しません。手動実行のみで、定期自動売買は開始していません。</p></div></>}
  {tab===1&&<Chart data={document?.series??[]} mode="assets"/>}
  {tab===2&&<section className="dash-panel"><h2>仮想保有銘柄</h2>{!document?.holdings.length?<Empty>保有銘柄はありません。</Empty>:<div className="record-grid">{document.holdings.map((r,i)=><article className="record" key={i}><h3>{text(r.name)} <small>{text(r.symbol)} · {text(r.market)}</small></h3><dl><dt>保有株数</dt><dd>{String(r.quantity)} 株</dd><dt>平均取得単価</dt><dd>{amount(r.average_cost,text(r.currency))}</dd><dt>評価額 JPY</dt><dd>{amount(r.market_value_jpy)}</dd><dt>含み損益</dt><dd>{amount(r.unrealized_pnl,text(r.currency))}</dd><dt>保有比率</dt><dd>{percent(r.ratio)}</dd><dt>評価時刻</dt><dd>{time(r.at)}</dd></dl></article>)}</div>}</section>}
  {tab===3&&<section className="dash-panel"><h2>仮想売買履歴</h2>{!document?.trades.length?<Empty>まだ約定した取引はありません。</Empty>:<div className="record-grid">{document.trades.map((r,i)=><article className="record" key={i}><p className="eyebrow">{text(r.side)} · {time(r.at)}</p><h3>{text(r.symbol)} · {String(r.quantity)} 株</h3><dl><dt>約定価格</dt><dd>{amount(r.price,text(r.currency))}</dd><dt>手数料</dt><dd>{amount(r.commission,text(r.currency))}</dd><dt>確定損益</dt><dd>{amount(r.realized_pnl,text(r.currency))}</dd></dl><p>{text(r.reason)}</p></article>)}</div>}</section>}
  {tab===4&&<section className="dash-panel"><h2>AI判断履歴</h2>{!document?.decisions.length?<Empty>まだ保存された判断はありません。</Empty>:document.decisions.map((r,i)=><article className="record" key={i}><p className="eyebrow">{text(r.market)} · {text(r.provider)} · {time(r.at)}</p><p>状態：{text(r.status)} / 注文登録 {String(r.registered_count)} 件</p>{Array.isArray(r.items)&&r.items.map((d:Row,j:number)=><div className="decision-item" key={j}><strong>{text(d.symbol)} <span>{text(d.action)}</span></strong><p>{text(d.reason)}</p>{Boolean(d.skipped)&&<p className="muted">見送り：{text(d.skipped)}</p>}</div>)}</article>)}</section>}
  {tab===5&&<section className="dash-panel"><p className="eyebrow">GEMINI API</p><h2>API利用状況</h2><dl className="usage-grid">{[
   ['モデル',Array.isArray(document?.usage.models)?document.usage.models.join(', '):'—'],
   ['判断回数',document?.usage.decision_count??0],['API通信回数',document?.usage.requests??0],
   ['入力トークン',document?.usage.input_tokens??'不明'],['出力トークン',document?.usage.output_tokens??'不明'],
   ['合計トークン',document?.usage.total_tokens??'不明'],['推定費用 USD',document?.usage.estimated_cost_usd??'未確認'],
  ].map(([label,value])=><div key={String(label)}><dt>{String(label)}</dt><dd>{String(value)}</dd></div>)}</dl><p className="muted">使用量は永続保存された予約・失敗も含みます。無料枠と実際の課金はGoogle側の設定に依存します。</p></section>}
 </section>
}
