"""Dashboard HTML shell (S12b) — self-contained, dark, ECharts-driven.

Design follows the dataviz method: validated dark categorical palette
(surface #1a1a19; series #3987e5/#d95926/#199e70/#c98500/#d55181 — validator
PASS), reserved status colors shipped with icon+label, one y-axis per chart,
legend for ≥2 series, honest staleness badges. Charts use ECharts served locally
(/static/echarts.min.js) — no CDN, no network at view time.

Two render modes share one template:
- ``render_page()`` — the live shell; JS fetches /api/* and subscribes /events.
- ``render_export(bootstrap, echarts_js)`` — a standalone snapshot: data is
  inlined as ``window.__BOOTSTRAP__`` and ECharts is inlined as a <script>, so it
  opens offline with charts intact and makes zero requests.
"""

from __future__ import annotations

import json
from typing import Any

_STYLE = """
:root{color-scheme:dark;
 --surface-0:#111110;--surface-1:#1a1a19;--surface-2:#232320;--line:#33332e;
 --text-primary:#ffffff;--text-secondary:#c3c2b7;--text-muted:#8a8a80;
 --s1:#3987e5;--s2:#d95926;--s3:#199e70;--s4:#c98500;--s5:#d55181;
 --good:#0ca30c;--warning:#fab219;--serious:#ec835a;--critical:#d03b3b;}
*{box-sizing:border-box}
body{margin:0;background:var(--surface-0);color:var(--text-primary);
 font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
header{display:flex;align-items:baseline;gap:16px;padding:16px 22px;border-bottom:1px solid var(--line)}
header h1{font-size:17px;margin:0;font-weight:650;letter-spacing:.2px}
header .sub{color:var(--text-muted);font-size:12px}
#conn{margin-left:auto;font-size:12px;color:var(--text-muted);display:flex;align-items:center;gap:6px}
#conn .dot{width:8px;height:8px;border-radius:50%;background:var(--text-muted)}
#conn.live .dot{background:var(--good)}#conn.stale .dot{background:var(--warning)}
main{padding:18px 22px;max-width:1360px;margin:0 auto}
.grid{display:grid;gap:14px}
.kpis{grid-template-columns:repeat(auto-fit,minmax(150px,1fr));margin-bottom:14px}
.tile{background:var(--surface-1);border:1px solid var(--line);border-radius:10px;padding:12px 14px}
.tile .label{color:var(--text-muted);font-size:11px;text-transform:uppercase;letter-spacing:.6px}
.tile .value{font-size:22px;font-weight:640;margin-top:4px;font-variant-numeric:tabular-nums}
.tile .value.small{font-size:15px;font-weight:560}
.tile .sub{color:var(--text-secondary);font-size:12px;margin-top:2px}
.badge{display:inline-flex;align-items:center;gap:5px;font-size:11px;font-weight:600;
 padding:2px 8px;border-radius:20px;border:1px solid transparent}
.badge.good{color:var(--good);border-color:color-mix(in oklab,var(--good) 55%,transparent)}
.badge.warn{color:var(--warning);border-color:color-mix(in oklab,var(--warning) 55%,transparent)}
.badge.crit{color:var(--critical);border-color:color-mix(in oklab,var(--critical) 55%,transparent)}
.badge.muted{color:var(--text-muted);border-color:var(--line)}
.gauges{grid-template-columns:repeat(auto-fit,minmax(220px,1fr));margin-bottom:14px}
.charts{grid-template-columns:repeat(auto-fit,minmax(420px,1fr))}
.card{background:var(--surface-1);border:1px solid var(--line);border-radius:12px;padding:14px 16px}
.card h2{font-size:13px;font-weight:620;margin:0 0 10px;color:var(--text-secondary);
 display:flex;align-items:center;gap:8px}
.chart{width:100%;height:260px}.gauge{width:100%;height:200px}
.sources{grid-template-columns:repeat(auto-fit,minmax(300px,1fr));margin-top:14px}
table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}
th,td{text-align:left;padding:4px 6px;border-bottom:1px solid var(--surface-2);font-size:12px}
th{color:var(--text-muted);font-weight:500}td.num{text-align:right;color:var(--text-primary)}
.muted{color:var(--text-muted)}
"""

_SCRIPT = r"""
const $=s=>document.querySelector(s);
const S={s1:'#3987e5',s2:'#d95926',s3:'#199e70',s4:'#c98500',s5:'#d55181',
 line:'#33332e',axis:'#8a8a80',text:'#c3c2b7',good:'#0ca30c',warn:'#fab219',crit:'#d03b3b'};
const BOOT=window.__BOOTSTRAP__||null;
const charts={};
function fmt(v,d=2){if(v==null||v==='')return '—';
 if(typeof v==='boolean')return v?'yes':'no';
 if(typeof v==='string'){const t=v.trim();
  if(/^0x[0-9a-fA-F]{8,}$/.test(t))return t.slice(0,6)+'…'+t.slice(-4);   // address → short hex
  if(!/^-?\d*\.?\d+([eE][-+]?\d+)?$/.test(t))return t;}                    // non-numeric string as-is
 const n=Number(v);if(!isFinite(n))return String(v);
 return n.toLocaleString(undefined,{maximumFractionDigits:d});}
function usd(v,d=2){return v==null?'—':'$'+fmt(v,d);}
function pct(v,d=2){return v==null?'—':fmt(v,d)+'%';}
function ago(s){if(s==null)return 'no data';if(s<60)return Math.round(s)+'s ago';
 if(s<3600)return Math.round(s/60)+'m ago';return (s/3600).toFixed(1)+'h ago';}
function ec(id){if(!charts[id]){charts[id]=echarts.init($('#'+id),null,{renderer:'canvas'});}return charts[id];}
const baseGrid={left:54,right:16,top:28,bottom:28};
const axisCommon={axisLine:{lineStyle:{color:S.line}},axisLabel:{color:S.axis},
 splitLine:{lineStyle:{color:S.line,type:'dashed'}}};

function lineChart(id,title,series){
 const opt={backgroundColor:'transparent',grid:baseGrid,
  tooltip:{trigger:'axis',backgroundColor:'#232320',borderColor:S.line,textStyle:{color:'#fff'},
   axisPointer:{type:'cross',label:{backgroundColor:'#333'}}},
  legend:{show:series.length>1,top:0,right:8,textStyle:{color:S.text},icon:'roundRect'},
  xAxis:{type:'time',...axisCommon},yAxis:{type:'value',scale:true,...axisCommon},
  series:series.map(s=>({name:s.name,type:'line',showSymbol:false,smooth:false,
   lineStyle:{width:2,color:s.color},itemStyle:{color:s.color},
   areaStyle:s.area?{color:s.color,opacity:0.10}:undefined,data:s.data}))};
 ec(id).setOption(opt,true);
}
function gauge(id,label,value,{min=0,max=100,unit='%',bands=[]}={}){
 const val=value==null?null:Number(value);
 const color=bands.length?bands:[[1,S.s1]];
 const opt={backgroundColor:'transparent',series:[{type:'gauge',min,max,radius:'92%',center:['50%','60%'],
  startAngle:210,endAngle:-30,axisLine:{lineStyle:{width:12,color}},
  progress:{show:false},pointer:{width:4,itemStyle:{color:S.text}},
  axisTick:{show:false},splitLine:{length:10,lineStyle:{color:S.line}},
  axisLabel:{color:S.axis,fontSize:10,distance:14},
  anchor:{show:true,size:8,itemStyle:{color:S.text}},
  title:{show:true,offsetCenter:[0,'34%'],color:S.text,fontSize:12},
  detail:{valueAnimation:false,offsetCenter:[0,'-2%'],color:'#fff',fontSize:20,
   formatter:v=>val==null?'—':fmt(v,1)+unit},
  data:[{value:val==null?min:val,name:label}]}]};
 ec(id).setOption(opt,true);
}

function badge(stale,as_of,staleness){
 if(as_of==null)return '<span class="badge muted">○ no data</span>';
 const cls=stale?'warn':'good';const mark=stale?'▲':'●';
 return `<span class="badge ${cls}">${mark} ${ago(staleness)}</span>`;
}

function renderDerived(d){
 if(!d){$('#kpis').innerHTML='<div class="tile"><div class="label">Derived</div><div class="value small muted">no priced LP observation yet</div></div>';return;}
 const a=d.analysis||{};
 const tiles=[
  ['Quadrant',a.quadrant??'—',a.range_position_pct!=null?pct(Number(a.range_position_pct),1)+' of range':''],
  ['Coverage (ETH)',a.coverage_ratio_eth!=null?fmt(a.coverage_ratio_eth,3)+'×':'—',a.coverage_notional_pct!=null?pct(a.coverage_notional_pct,1)+' notional':'no short'],
  ['Net Δ (ETH)',fmt((Number(a.lp_delta_eth||0)-Number(a.short_size_eth||0)),4),'LP '+fmt(a.lp_delta_eth,3)+' − short '+fmt(a.short_size_eth,3)],
  ['Hedge uPnL',usd(d.hedge_upnl_usd),''],
  ['Funding / day',d.funding_run_rate_usd_per_day!=null?usd(d.funding_run_rate_usd_per_day):'—',d.funding_run_rate_reason||''],
  ['Rebalance',a.rebalance_needed?'NEEDED':'ok','target '+fmt(a.delta_matched_target_eth,3)+' ETH'],
 ];
 $('#kpis').innerHTML=tiles.map(([l,v,s])=>{
  const cls=(l==='Rebalance'&&v==='NEEDED')?' style="color:var(--warning)"':'';
  return `<div class="tile"><div class="label">${l}</div><div class="value small"${cls}>${v}</div><div class="sub">${s||''}</div></div>`;}).join('');
 // gauges
 const dsl=a.distance_to_sl_pct, dliq=a.distance_to_floor_pct, rp=a.range_position_pct;
 gauge('g_sl','dist to SL %',dsl==null?null:Math.abs(Number(dsl)),{min:0,max:10,unit:'%',
  bands:[[0.2,S.crit],[0.5,S.warn],[1,S.good]]});
 gauge('g_floor','dist to floor %',dliq==null?null:Math.abs(Number(dliq)),{min:0,max:30,unit:'%',
  bands:[[0.15,S.crit],[0.4,S.warn],[1,S.good]]});
 gauge('g_range','in-range position',rp==null?null:Number(rp),{min:0,max:100,unit:'%',
  bands:[[1,S.s1]]});
}

function renderLatest(v){
 if(!v){return;}
 const conn=$('#conn');
 // header staleness = worst source
 let anyStale=false, newest=null;
 const boxes=[];
 for(const k of ['lp','hedge','defi','holdings']){
  const p=v.sources[k];if(!p)continue;if(p.stale&&p.as_of)anyStale=true;
  if(p.as_of&&(newest==null||p.as_of>newest))newest=p.as_of;
  const rows=(p.entries||[]).map(e=>{
   const st=e.state||{};const keys=Object.keys(st).filter(x=>typeof st[x]!=='object').slice(0,8);
   const cells=keys.map(x=>`<tr><th>${x}</th><td class="num">${fmt(st[x],4)}</td></tr>`).join('');
   return `<table>${cells}</table>`;
  }).join('')|| '<div class="muted" style="font-size:12px">no open positions</div>';
  boxes.push(`<div class="card"><h2>${k.toUpperCase()} ${badge(p.stale,p.as_of,p.staleness_seconds)}</h2>${rows}</div>`);
 }
 $('#sources').innerHTML=boxes.join('');
 conn.className=anyStale?'stale':(BOOT?'':'live');
 const ref=v.now?Date.parse(v.now):Date.now();  // view's reference time (consistent w/ badges)
 $('#updated').textContent=newest?('data '+ago((ref-Date.parse(newest))/1000)):'no data';
}

async function loadHistory(){
 async function h(kind,field){
  if(BOOT&&BOOT.history){return BOOT.history[kind+'.'+field]||[];}
  try{const r=await fetch(`/api/history?kind=${kind}&field=${field}`);if(!r.ok)return [];return await r.json();}
  catch(e){return [];}
 }
 const toXY=arr=>arr.filter(p=>p.value!=null).map(p=>[Date.parse(p.ts),Number(p.value)]);
 const [price,mark,size,vol,funding]=await Promise.all([
  h('lp','price_usd'),h('perp','mark_price'),h('perp','size_tokens'),
  h('lp','pool_volume_usd_24h'),h('perp','pending_funding_fees_usd')]);
 lineChart('c_price','Price',[
  {name:'LP price',color:S.s1,data:toXY(price)},
  {name:'Hedge mark',color:S.s2,data:toXY(mark)}]);
 lineChart('c_size','Hedge size (ETH)',[{name:'size ETH',color:S.s3,area:true,data:toXY(size)}]);
 lineChart('c_vol','Pool 24h volume (USD)',[{name:'volume',color:S.s4,area:true,data:toXY(vol)}]);
 lineChart('c_funding','Funding fees (USD, cum.)',[{name:'funding',color:S.s5,data:toXY(funding)}]);
}

async function refresh(){
 if(BOOT){renderLatest(BOOT.latest);renderDerived(BOOT.derived);await loadHistory();return;}
 try{
  const [lv,dv]=await Promise.all([fetch('/api/latest').then(r=>r.json()),
   fetch('/api/derived').then(r=>r.ok?r.json():null)]);
  renderLatest(lv);renderDerived(dv);await loadHistory();
 }catch(e){$('#conn').className='stale';}
}
function subscribe(){
 if(BOOT)return;
 try{const es=new EventSource('/events');
  es.addEventListener('update',()=>refresh());
  es.onerror=()=>{$('#conn').className='stale';};
 }catch(e){}
}
window.addEventListener('resize',()=>{for(const k in charts)charts[k].resize();});
refresh();subscribe();
"""


def _page(bootstrap_json: str, inline_echarts: str | None) -> str:
    echarts_tag = (
        f"<script>{inline_echarts}</script>"
        if inline_echarts is not None
        else '<script src="/static/echarts.min.js"></script>'
    )
    boot = f"<script>window.__BOOTSTRAP__={bootstrap_json};</script>" if bootstrap_json else ""
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>dexpaprika — LP/hedge dashboard</title><style>{_STYLE}</style></head>
<body><header><h1>LP + Hedge</h1><span class="sub" id="updated">…</span>
<span id="conn"><span class="dot"></span><span>live</span></span></header>
<main>
<section class="grid kpis" id="kpis"></section>
<section class="grid gauges">
 <div class="card"><h2>Distance to stop-loss</h2><div class="gauge" id="g_sl"></div></div>
 <div class="card"><h2>Distance to floor</h2><div class="gauge" id="g_floor"></div></div>
 <div class="card"><h2>In-range position</h2><div class="gauge" id="g_range"></div></div>
</section>
<section class="grid charts">
 <div class="card"><h2>Price — LP vs hedge mark</h2><div class="chart" id="c_price"></div></div>
 <div class="card"><h2>Hedge size (ETH)</h2><div class="chart" id="c_size"></div></div>
 <div class="card"><h2>Pool 24h volume</h2><div class="chart" id="c_vol"></div></div>
 <div class="card"><h2>Funding fees (cumulative)</h2><div class="chart" id="c_funding"></div></div>
</section>
<section class="grid sources" id="sources"></section>
</main>
{echarts_tag}{boot}<script>{_SCRIPT}</script></body></html>"""


def render_page() -> str:
    """The live dashboard shell (fetches /api/* + subscribes /events)."""
    return _page("", None)


def render_export(bootstrap: dict[str, Any], echarts_js: str) -> str:
    """A standalone snapshot: data + ECharts inlined, zero external requests."""
    return _page(json.dumps(bootstrap, default=str), echarts_js)
