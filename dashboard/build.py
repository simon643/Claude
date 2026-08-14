import json

with open('dashboard/cashflow_data.json') as f:
    data = json.load(f)

DATA_JSON = json.dumps(data, separators=(',', ':'))

HTML = r'''<title>SDA Abodes Cashflow</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
:root{
  color-scheme: light;
  --bg:#f4f4f2; --surface:#fcfcfb; --surface-2:#f7f7f4; --border:#e4e3de;
  --ink:#0b0b0b; --ink-2:#52514e; --ink-3:#86857f;
  --blue:#2a78d6; --blue-soft:#cde2fb; --red:#e34948; --red-soft:#f8d4d3;
  --green:#1baf7a; --amber:#eda100;
  --pos:#2a78d6; --neg:#e34948;
  --grid:#ecebe6; --shadow:0 1px 2px rgba(0,0,0,.04),0 4px 16px rgba(0,0,0,.05);
  --mono:'SF Mono',ui-monospace,'Cascadia Code',Menlo,Consolas,monospace;
  --sans:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;
}
:root:where(:not([data-theme="light"])) { }
@media (prefers-color-scheme: dark){
  :root:where(:not([data-theme="light"])){
    color-scheme: dark;
    --bg:#131312; --surface:#1c1c1a; --surface-2:#232320; --border:#333330;
    --ink:#f5f4ef; --ink-2:#c3c2b7; --ink-3:#8f8e85;
    --blue:#3987e5; --blue-soft:#173a5e; --red:#e66767; --red-soft:#4a2321;
    --green:#199e70; --amber:#c98500;
    --pos:#3987e5; --neg:#e66767;
    --grid:#2a2a27; --shadow:0 1px 2px rgba(0,0,0,.3),0 4px 20px rgba(0,0,0,.35);
  }
}
:root[data-theme="dark"]{
  color-scheme: dark;
  --bg:#131312; --surface:#1c1c1a; --surface-2:#232320; --border:#333330;
  --ink:#f5f4ef; --ink-2:#c3c2b7; --ink-3:#8f8e85;
  --blue:#3987e5; --blue-soft:#173a5e; --red:#e66767; --red-soft:#4a2321;
  --green:#199e70; --amber:#c98500;
  --pos:#3987e5; --neg:#e66767;
  --grid:#2a2a27; --shadow:0 1px 2px rgba(0,0,0,.3),0 4px 20px rgba(0,0,0,.35);
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font-family:var(--sans);
  font-size:15px;line-height:1.5;-webkit-font-smoothing:antialiased}
.wrap{max-width:1180px;margin:0 auto;padding:28px 20px 80px}
header.top{display:flex;flex-wrap:wrap;align-items:flex-end;justify-content:space-between;gap:14px;margin-bottom:8px}
.brand{font-size:12px;letter-spacing:.14em;text-transform:uppercase;color:var(--ink-3);font-weight:600;margin:0 0 4px}
h1{font-size:27px;line-height:1.15;margin:0;font-weight:700;letter-spacing:-.01em}
.sub{color:var(--ink-2);font-size:14px;margin:6px 0 0}
.period-pill{display:inline-flex;align-items:center;gap:8px;background:var(--surface);border:1px solid var(--border);
  border-radius:999px;padding:7px 14px;font-size:13px;color:var(--ink-2);box-shadow:var(--shadow)}
.period-pill b{color:var(--ink);font-weight:600}

.alert{display:flex;gap:12px;align-items:flex-start;margin:20px 0 24px;padding:14px 16px;border-radius:12px;
  background:var(--red-soft);border:1px solid color-mix(in srgb,var(--red) 35%,transparent)}
.alert .ic{font-size:18px;line-height:1.3}
.alert p{margin:0;font-size:13.5px;color:var(--ink)}
.alert b{font-weight:700}

.kpis{display:grid;grid-template-columns:repeat(6,1fr);gap:12px;margin-bottom:24px}
@media(max-width:960px){.kpis{grid-template-columns:repeat(3,1fr)}}
@media(max-width:560px){.kpis{grid-template-columns:repeat(2,1fr)}}
.kpi{background:var(--surface);border:1px solid var(--border);border-radius:14px;padding:14px 15px;box-shadow:var(--shadow);
  position:relative;overflow:hidden}
.kpi .lab{font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:var(--ink-3);font-weight:600;margin-bottom:8px}
.kpi .val{font-size:21px;font-weight:700;font-family:var(--mono);letter-spacing:-.02em;line-height:1.1}
.kpi .note{font-size:11.5px;color:var(--ink-2);margin-top:5px}
.kpi.accent-neg .val{color:var(--neg)} .kpi.accent-pos .val{color:var(--green)}
.kpi .spark{position:absolute;right:0;bottom:0;opacity:.5}

.grid2{display:grid;grid-template-columns:1.55fr 1fr;gap:18px;margin-bottom:18px}
@media(max-width:900px){.grid2{grid-template-columns:1fr}}
.card{background:var(--surface);border:1px solid var(--border);border-radius:16px;padding:18px 18px 14px;box-shadow:var(--shadow)}
.card h2{font-size:15px;margin:0 0 2px;font-weight:700;letter-spacing:-.01em}
.card .cap{font-size:12.5px;color:var(--ink-2);margin:0 0 14px}
.legend{display:flex;flex-wrap:wrap;gap:14px;font-size:12px;color:var(--ink-2);margin-top:10px}
.legend span{display:inline-flex;align-items:center;gap:6px}
.legend i{width:11px;height:11px;border-radius:3px;display:inline-block}

svg{display:block;width:100%;overflow:visible;font-family:var(--sans)}
.axis text{fill:var(--ink-3);font-size:10.5px}
.axis line,.axis path{stroke:var(--grid)}
.gl{stroke:var(--grid);stroke-width:1}
.zero{stroke:var(--ink-3);stroke-width:1;stroke-dasharray:none;opacity:.55}

.tbl-wrap{overflow-x:auto;-webkit-overflow-scrolling:touch;border-radius:12px;border:1px solid var(--border)}
table.cf{border-collapse:collapse;width:100%;font-size:12.5px;min-width:1050px}
table.cf th,table.cf td{padding:7px 10px;text-align:right;white-space:nowrap;border-bottom:1px solid var(--border)}
table.cf th:first-child,table.cf td:first-child{text-align:left;position:sticky;left:0;background:var(--surface);z-index:2;
  border-right:1px solid var(--border);font-weight:500}
table.cf thead th{background:var(--surface-2);position:sticky;top:0;font-size:11px;color:var(--ink-2);font-weight:600;z-index:3}
table.cf thead th:first-child{z-index:4}
table.cf td{font-family:var(--mono);font-size:11.5px}
table.cf tr.section td{background:var(--surface-2);font-weight:700;font-family:var(--sans);color:var(--ink);text-transform:uppercase;
  font-size:10.5px;letter-spacing:.05em}
table.cf tr.total td{font-weight:700;border-top:2px solid var(--border)}
table.cf tr.grand td{font-weight:700;background:var(--surface-2)}
.neg{color:var(--neg)} .muted{color:var(--ink-3)}
.rowlab{display:inline-block}

.cols{display:grid;grid-template-columns:1fr 1fr;gap:18px;margin-bottom:18px}
@media(max-width:900px){.cols{grid-template-columns:1fr}}
.banklist{display:flex;flex-direction:column;gap:10px}
.bank{border:1px solid var(--border);border-radius:12px;padding:12px 14px;background:var(--surface-2)}
.bank .bh{display:flex;justify-content:space-between;align-items:baseline;gap:10px;margin-bottom:4px}
.bank .bn{font-weight:700;font-size:13.5px}
.bank .cards{font-size:11px;color:var(--ink-3);font-family:var(--mono)}
.bank .ent{font-size:12px;color:var(--ink-2);line-height:1.4;margin-bottom:8px}
.bank .flows{display:flex;gap:16px;font-size:12px;font-family:var(--mono)}
.bank .flows .lab{color:var(--ink-3);font-family:var(--sans);font-size:10.5px;display:block}
.pos-t{color:var(--green)} .neg-t{color:var(--neg)}

.commrow{display:flex;align-items:center;gap:10px;margin-bottom:9px}
.commrow .nm{width:70px;font-size:12.5px;font-weight:600}
.commrow .bar{flex:1;height:20px;background:var(--surface-2);border-radius:5px;overflow:hidden;position:relative}
.commrow .bar>span{position:absolute;left:0;top:0;bottom:0;background:var(--blue);border-radius:5px}
.commrow .amt{width:78px;text-align:right;font-family:var(--mono);font-size:12px}

.footer{margin-top:34px;padding-top:18px;border-top:1px solid var(--border);color:var(--ink-3);font-size:12px;line-height:1.6}
.tog{cursor:pointer;background:var(--surface);border:1px solid var(--border);border-radius:8px;color:var(--ink-2);
  padding:6px 12px;font-size:12.5px;font-weight:600}
.tip{position:fixed;pointer-events:none;background:var(--ink);color:var(--surface);padding:7px 10px;border-radius:8px;
  font-size:11.5px;line-height:1.45;box-shadow:0 4px 20px rgba(0,0,0,.35);opacity:0;transition:opacity .1s;z-index:50;max-width:230px}
.tip b{color:#fff}
:root[data-theme="dark"] .tip,.dk .tip{background:#000}
.tip .tk{font-family:var(--mono)}
.chip{display:inline-block;font-size:10px;padding:2px 7px;border-radius:999px;font-weight:600;letter-spacing:.03em}
.sec-head{font-size:12px;letter-spacing:.1em;text-transform:uppercase;color:var(--ink-3);font-weight:600;margin:30px 0 14px}
</style>

<div class="wrap">
  <header class="top">
    <div>
      <p class="brand">SDA Abodes Group</p>
      <h1>Consolidated Weekly Cashflow Forecast</h1>
      <p class="sub">13-week rolling projection across four ANZ entities &mdash; opening balances, inflows, outflows &amp; running cash position.</p>
    </div>
    <div class="period-pill">📅 <span><b>13 Jul</b> &rarr; <b>28 Dec 2026</b> &middot; 25 weeks</span></div>
  </header>

  <div class="alert" id="alertBox"></div>

  <div class="kpis" id="kpis"></div>

  <div class="grid2">
    <div class="card">
      <h2>Running cash position</h2>
      <p class="cap">Closing consolidated bank balance at the end of each week.</p>
      <div id="balchart"></div>
      <div class="legend">
        <span><i style="background:var(--blue)"></i>Closing balance</span>
        <span><i style="background:var(--ink-3);opacity:.6"></i>Zero line</span>
      </div>
    </div>
    <div class="card">
      <h2>Weekly net movement</h2>
      <p class="cap">Inflows minus outflows each week. Almost every week is cash-negative.</p>
      <div id="netchart"></div>
      <div class="legend">
        <span><i style="background:var(--blue)"></i>Cash surplus</span>
        <span><i style="background:var(--red)"></i>Cash deficit</span>
      </div>
    </div>
  </div>

  <div class="card" style="margin-bottom:18px">
    <h2>Where the cash goes &mdash; total outflows by category</h2>
    <p class="cap">Aggregated across all 25 weeks. Total outflows: <b id="outTot"></b>.</p>
    <div id="catchart"></div>
  </div>

  <div class="cols">
    <div class="card">
      <h2>Entity bank accounts</h2>
      <p class="cap">Observed 3-month activity (ANZ CSVs, 14 Apr &ndash; 10 Jul 2026). Identity inferred from payee patterns.</p>
      <div class="banklist" id="banks"></div>
    </div>
    <div class="card">
      <h2>Outstanding commissions &amp; referrals</h2>
      <p class="cap">Payable across sold projects, by recipient. Total <b id="commTot"></b> over <span id="projCount"></span> projects.</p>
      <div id="comms"></div>
      <p class="cap" style="margin:16px 0 0;font-size:11.5px">Funding lines flagged in workbook:
        <span id="funding"></span></p>
    </div>
  </div>

  <p class="sec-head">Full weekly cashflow detail</p>
  <p class="cap" style="margin:-4px 0 12px;color:var(--ink-3)">Scroll horizontally for all 25 weeks. Only line items with activity are shown.</p>
  <div class="tbl-wrap">
    <table class="cf" id="cftable"></table>
  </div>

  <div class="footer">
    <p><b>About this dashboard.</b> Rebuilt from <span class="tk" style="font-family:var(--mono)">SDA_Abodes_Weekly_Cashflow.xlsx</span> (&ldquo;Weekly Cashflow&rdquo;, &ldquo;Overview&rdquo; &amp; &ldquo;Sales&rdquo; sheets). All figures in AUD. Outflows are shown as negative. Balances are consolidated across ANZ accounts 64/65/66/67. Forecast assumptions live on the workbook&rsquo;s Assumptions tab and are not editable here.</p>
    <p>Account identity was inferred from payee patterns &mdash; confirm before relying on it. This is a forecast, not a statement of actuals.</p>
  </div>
</div>

<div class="tip" id="tip"></div>

<script>
const DATA = __DATA__;
const tip = document.getElementById('tip');
const AUD0 = n => (n<0?'-':'')+'$'+Math.abs(Math.round(n)).toLocaleString('en-AU');
const AUDk = n => { const a=Math.abs(n); let s; if(a>=1e6)s='$'+(a/1e6).toFixed(a>=1e7?1:2)+'M'; else if(a>=1e3)s='$'+(a/1e3).toFixed(0)+'k'; else s='$'+Math.round(a); return (n<0?'-':'')+s; };
const shortDate = iso => { const d=new Date(iso+'T00:00'); return d.toLocaleDateString('en-AU',{day:'numeric',month:'short'}); };
function showTip(html,x,y){ tip.innerHTML=html; tip.style.opacity=1; const w=tip.offsetWidth,h=tip.offsetHeight;
  let nx=x+14, ny=y-h-10; if(nx+w>window.innerWidth-8)nx=x-w-14; if(ny<8)ny=y+18; tip.style.left=nx+'px'; tip.style.top=ny+'px'; }
function hideTip(){ tip.style.opacity=0; }
const CS = getComputedStyle(document.documentElement);
const col = n => CS.getPropertyValue(n).trim();

/* ---------- KPIs ---------- */
const opening = DATA.opening[0];
const closing = DATA.closing[DATA.closing.length-1];
const totIn = DATA.total_inflows.reduce((a,b)=>a+b,0);
const totOut = DATA.total_outflows.reduce((a,b)=>a+b,0);
const totNet = DATA.net.reduce((a,b)=>a+b,0);
const trough = Math.min(...DATA.closing);
const troughWk = DATA.closing.indexOf(trough);
const firstNeg = DATA.closing.findIndex(v=>v<0);
document.getElementById('outTot').textContent = AUD0(totOut);

const kpis = [
  {lab:'Opening cash (W1)', val:AUD0(opening), note:shortDate(DATA.weeks[0]), cls:'accent-pos'},
  {lab:'Closing cash (W25)', val:AUDk(closing), note:shortDate(DATA.weeks[24]), cls:'accent-neg'},
  {lab:'Total inflows', val:AUDk(totIn), note:'over 25 weeks', cls:'accent-pos'},
  {lab:'Total outflows', val:AUDk(totOut), note:'over 25 weeks', cls:'accent-neg'},
  {lab:'Net movement', val:AUDk(totNet), note:'inflows − outflows', cls:'accent-neg'},
  {lab:'Peak cash deficit', val:AUDk(trough), note:'week of '+shortDate(DATA.weeks[troughWk]), cls:'accent-neg'},
];
document.getElementById('kpis').innerHTML = kpis.map(k=>
  `<div class="kpi ${k.cls}"><div class="lab">${k.lab}</div><div class="val">${k.val}</div><div class="note">${k.note}</div></div>`).join('');

document.getElementById('alertBox').innerHTML =
  `<span class="ic">⚠️</span><p>On the current forecast, consolidated cash turns <b>negative from ${shortDate(DATA.weeks[firstNeg])} (week ${firstNeg+1})</b> and never recovers, reaching a projected trough of <b>${AUD0(trough)}</b> by ${shortDate(DATA.weeks[troughWk])}. Outflows (${AUDk(totOut)}) exceed inflows (${AUDk(totIn)}) roughly ${(Math.abs(totOut)/totIn).toFixed(0)}&times; over the period &mdash; the plan depends on external funding / asset sales not yet in these lines.</p>`;

/* ---------- Balance line/area chart ---------- */
function balanceChart(){
  const W=680,H=280, m={t:14,r:16,b:34,l:52};
  const iw=W-m.l-m.r, ih=H-m.t-m.b;
  const vals=DATA.closing, n=vals.length;
  const step=1e6;
  const mn=Math.floor(Math.min(0,...vals)/step)*step, mx=Math.ceil(Math.max(0,...vals)/step)*step;
  const x=i=> m.l + (n===1?0:i*iw/(n-1));
  const y=v=> m.t + ih - (v-mn)/(mx-mn)*ih;
  const y0=y(0);
  // ticks
  const ticks=[];
  for(let v=mn; v<=mx+1; v+=step) ticks.push(v);
  let g='';
  ticks.forEach(t=> g+=`<line class="gl" x1="${m.l}" y1="${y(t).toFixed(1)}" x2="${W-m.r}" y2="${y(t).toFixed(1)}"/>`
    +`<text class="axis" x="${m.l-8}" y="${(y(t)+3.5).toFixed(1)}" text-anchor="end">${AUDk(t)}</text>`);
  // area path (fill toward zero)
  let area=`M ${x(0)} ${y0}`; vals.forEach((v,i)=> area+=` L ${x(i).toFixed(1)} ${y(v).toFixed(1)}`); area+=` L ${x(n-1)} ${y0} Z`;
  let line='M'; vals.forEach((v,i)=> line+=` ${x(i).toFixed(1)} ${y(v).toFixed(1)}`);
  let xlabs=''; for(let i=0;i<n;i+=4) xlabs+=`<text class="axis" x="${x(i)}" y="${H-m.b+18}" text-anchor="middle">${shortDate(DATA.weeks[i])}</text>`;
  let dots='',hit='';
  vals.forEach((v,i)=>{
    dots+=`<circle cx="${x(i).toFixed(1)}" cy="${y(v).toFixed(1)}" r="2.6" fill="var(--blue)"/>`;
    hit+=`<rect x="${(x(i)-iw/(n-1)/2).toFixed(1)}" y="${m.t}" width="${(iw/(n-1)).toFixed(1)}" height="${ih}" fill="transparent" `
      +`data-i="${i}"/>`;
  });
  const crosshair=`<line id="bx" x1="0" y1="${m.t}" x2="0" y2="${m.t+ih}" stroke="var(--ink-3)" stroke-width="1" stroke-dasharray="3 3" style="opacity:0"/>`;
  document.getElementById('balchart').innerHTML =
   `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Running cash balance line chart">
      <defs><linearGradient id="ga" x1="0" y1="0" x2="0" y2="1">
        <stop offset="0" stop-color="var(--blue)" stop-opacity=".22"/><stop offset="1" stop-color="var(--blue)" stop-opacity="0"/>
      </linearGradient></defs>
      ${g}
      <line class="zero" x1="${m.l}" y1="${y0.toFixed(1)}" x2="${W-m.r}" y2="${y0.toFixed(1)}"/>
      <path d="${area}" fill="url(#ga)"/>
      <path d="${line}" fill="none" stroke="var(--blue)" stroke-width="2.4" stroke-linejoin="round" stroke-linecap="round"/>
      ${dots}${crosshair}${xlabs}${hit}
    </svg>`;
  const svg=document.querySelector('#balchart svg'); const bx=svg.querySelector('#bx');
  svg.querySelectorAll('rect[data-i]').forEach(r=>{
    r.addEventListener('mousemove',e=>{ const i=+r.dataset.i; bx.setAttribute('x1',x(i)); bx.setAttribute('x2',x(i)); bx.style.opacity=1;
      showTip(`<b>Week ${i+1}</b> · ${shortDate(DATA.weeks[i])}<br>Closing: <span class="tk">${AUD0(vals[i])}</span><br>Net: <span class="tk">${AUD0(DATA.net[i])}</span>`, e.clientX,e.clientY); });
    r.addEventListener('mouseleave',()=>{ bx.style.opacity=0; hideTip(); });
  });
}

/* ---------- Net movement diverging bars ---------- */
function netChart(){
  const W=440,H=280, m={t:14,r:12,b:34,l:46};
  const iw=W-m.l-m.r, ih=H-m.t-m.b;
  const vals=DATA.net, n=vals.length;
  const step=2e5;
  const mn=Math.floor(Math.min(0,...vals)/step)*step, mx=Math.ceil(Math.max(0,...vals)/step)*step;
  const y=v=> m.t + ih - (v-mn)/(mx-mn)*ih;
  const y0=y(0);
  const bw=iw/n*0.72;
  const x=i=> m.l + (i+0.5)*iw/n;
  const ticks=[];
  for(let v=mn; v<=mx+1; v+=step) ticks.push(v);
  let g='';
  ticks.forEach(t=> g+=`<line class="gl" x1="${m.l}" y1="${y(t).toFixed(1)}" x2="${W-m.r}" y2="${y(t).toFixed(1)}"/>`
    +`<text class="axis" x="${m.l-7}" y="${(y(t)+3.5).toFixed(1)}" text-anchor="end">${AUDk(t)}</text>`);
  let bars='';
  vals.forEach((v,i)=>{
    const top=v>=0?y(v):y0, h=Math.abs(y(v)-y0), c=v>=0?'var(--blue)':'var(--red)';
    bars+=`<rect x="${(x(i)-bw/2).toFixed(1)}" y="${top.toFixed(1)}" width="${bw.toFixed(1)}" height="${Math.max(0.5,h).toFixed(1)}" `
      +`rx="2.5" fill="${c}" data-i="${i}"/>`;
  });
  let xlabs=''; for(let i=0;i<n;i+=4) xlabs+=`<text class="axis" x="${x(i).toFixed(1)}" y="${H-m.b+18}" text-anchor="middle">${shortDate(DATA.weeks[i])}</text>`;
  document.getElementById('netchart').innerHTML =
   `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Weekly net cash movement bar chart">
      ${g}<line class="zero" x1="${m.l}" y1="${y0.toFixed(1)}" x2="${W-m.r}" y2="${y0.toFixed(1)}"/>${bars}${xlabs}
    </svg>`;
  document.querySelectorAll('#netchart rect[data-i]').forEach(r=>{
    r.addEventListener('mousemove',e=>{ const i=+r.dataset.i;
      showTip(`<b>Week ${i+1}</b> · ${shortDate(DATA.weeks[i])}<br>Net movement: <span class="tk">${AUD0(DATA.net[i])}</span>`, e.clientX,e.clientY); });
    r.addEventListener('mouseleave',hideTip);
  });
}

/* ---------- Outflow categories horizontal bars ---------- */
function catChart(){
  const cats=Object.entries(DATA.outflows).map(([k,v])=>({k,v:v.reduce((a,b)=>a+b,0)}))
    .filter(c=>c.v!==0).sort((a,b)=>a.v-b.v); // most negative first
  const max=Math.max(...cats.map(c=>Math.abs(c.v)));
  const rowH=30, W=680, labW=190, barX=labW+6, barW=W-barX-90, top=6;
  const H=top+cats.length*rowH+6;
  let rows='';
  cats.forEach((c,i)=>{
    const y=top+i*rowH; const w=Math.abs(c.v)/max*barW;
    const shade=`hsl(210 ${45+ i/cats.length*20}% ${58-i/cats.length*8}%)`;
    rows+=`<text x="${labW}" y="${y+rowH/2+4}" text-anchor="end" font-size="12" fill="var(--ink-2)">${c.k}</text>`
      +`<rect x="${barX}" y="${y+5}" width="${Math.max(2,w).toFixed(1)}" height="${rowH-14}" rx="4" fill="var(--blue)" opacity="${(0.55+0.45*(Math.abs(c.v)/max)).toFixed(2)}" data-k="${c.k}" data-v="${c.v}"/>`
      +`<text x="${barX+w+8}" y="${y+rowH/2+4}" font-size="11.5" font-family="var(--mono)" fill="var(--ink)">${AUDk(c.v)}</text>`;
  });
  document.getElementById('catchart').innerHTML =
   `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Outflows by category">${rows}</svg>`;
  document.querySelectorAll('#catchart rect[data-k]').forEach(r=>{
    r.addEventListener('mousemove',e=>{ const pct=Math.abs(+r.dataset.v)/Math.abs(totOut)*100;
      showTip(`<b>${r.dataset.k}</b><br><span class="tk">${AUD0(+r.dataset.v)}</span><br>${pct.toFixed(1)}% of outflows`, e.clientX,e.clientY); });
    r.addEventListener('mouseleave',hideTip);
  });
}

/* ---------- Banks ---------- */
function banks(){
  document.getElementById('banks').innerHTML = DATA.banks.map(b=>{
    const shortEnt=b.entity.split('(')[0];
    return `<div class="bank"><div class="bh"><span class="bn">${b.file.replace(/__/,' ')}</span><span class="cards">${b.cards}</span></div>
      <div class="ent">${b.entity}</div>
      <div class="flows">
        <div><span class="lab">Inflow</span><span class="pos-t">${AUD0(b.inflow)}</span></div>
        <div><span class="lab">Outflow</span><span class="neg-t">${AUD0(b.outflow)}</span></div>
        <div><span class="lab">Net</span><span class="${b.net>=0?'pos-t':'neg-t'}">${AUD0(b.net)}</span></div>
      </div></div>`;
  }).join('');
}

/* ---------- Commissions ---------- */
function comms(){
  const t=DATA.commission_totals; const entries=Object.entries(t).sort((a,b)=>b[1]-a[1]);
  const max=Math.max(...entries.map(e=>e[1]));
  const tot=entries.reduce((a,e)=>a+e[1],0);
  document.getElementById('commTot').textContent=AUD0(tot);
  document.getElementById('projCount').textContent=DATA.projects.filter(p=>p.total>0).length;
  document.getElementById('comms').innerHTML = entries.map(([nm,v])=>
    `<div class="commrow"><span class="nm">${nm}</span><span class="bar"><span style="width:${(v/max*100).toFixed(1)}%"></span></span><span class="amt">${AUD0(v)}</span></div>`).join('');
  document.getElementById('funding').innerHTML = DATA.funding.map(f=>`<b style="color:var(--ink)">${f[0]}</b> ${AUD0(f[1])}`).join(' &middot; ');
}

/* ---------- Cashflow table ---------- */
function table(){
  const wk=DATA.weeks; const nW=wk.length;
  const head=`<thead><tr><th>Line item</th>${wk.map((w,i)=>`<th>W${i+1}<br><span class="muted" style="font-weight:400">${shortDate(w)}</span></th>`).join('')}</tr></thead>`;
  const cell=v=>`<td class="${v<0?'neg':(v===0?'muted':'')}">${v===0?'–':AUD0(v)}</td>`;
  const line=(lab,arr,cls='')=>`<tr class="${cls}"><td>${lab}</td>${arr.map(cell).join('')}</tr>`;
  const section=lab=>`<tr class="section"><td colspan="${nW+1}">${lab}</td></tr>`;
  let b='';
  b+=line('Opening cash balance',DATA.opening,'');
  b+=section('Inflows');
  b+=line('SDA Living Australia Mgt Fee',DATA.mgt_fee);
  b+=line('Total inflows',DATA.total_inflows,'total');
  b+=section('Outflows');
  for(const [k,v] of Object.entries(DATA.outflows)){
    if(v.some(x=>x!==0)) b+=line(k,v);
  }
  b+=line('Total outflows',DATA.total_outflows,'total');
  b+=line('Net movement',DATA.net,'total');
  b+=line('Closing cash balance',DATA.closing,'grand');
  document.getElementById('cftable').innerHTML=head+'<tbody>'+b+'</tbody>';
}

balanceChart(); netChart(); catChart(); banks(); comms(); table();
let rt; window.addEventListener('resize',()=>{ clearTimeout(rt); rt=setTimeout(()=>{ balanceChart(); netChart(); catChart(); },150); });
</script>
'''

HTML = HTML.replace('__DATA__', DATA_JSON)
with open('dashboard/sda_cashflow_dashboard.html','w') as f:
    f.write(HTML)
print('written', len(HTML), 'bytes')
