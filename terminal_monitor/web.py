"""Local web command center: dashboard assets, HTTP API, and SSE stream."""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .backends import validate_web_port
from .status import _public_event_line, _public_status

WEB_POST_BODY_LIMIT_BYTES = 64 * 1024


DASHBOARD_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Agent Command Center — Architecture & Verification Pipeline</title><style>
:root{
  --paper-site:#0b0908;--paper-raised:#15181c;--paper-card:#1c2026;
  --ink-site:#f8faf9;--ink-soft:#d2d7dc;--muted:#8e979d;--dim:#5c646a;
  --line:rgba(255,255,255,0.12);--line-hi:rgba(255,255,255,0.24);
  --accent-site:#fe6e00;--accent-soft:rgba(254,110,0,0.12);
  --emerald:#00c758;--emerald-soft:rgba(0,199,88,0.12);
  --yellow:#edb200;--red:#fb2c36;--blue:#3080ff;
  --font-display:'Inter',-apple-system,BlinkMacSystemFont,system-ui,sans-serif;
  --font-mono:ui-monospace,'JetBrains Mono',SFMono-Regular,Menlo,monospace;
  --ease:cubic-bezier(.22,1,.36,1);
}
*{box-sizing:border-box;margin:0;padding:0}
body{
  background:radial-gradient(circle at 85% 0,rgba(254,110,0,.15),transparent 36%),var(--paper-site);
  color:var(--ink-site);font:14px/1.5 var(--font-display);min-height:100vh;overflow-x:hidden;
}
.grid-bg{
  position:fixed;inset:0;pointer-events:none;z-index:0;
  background-image:linear-gradient(rgba(255,255,255,.03) 1px,transparent 1px),linear-gradient(90deg,rgba(255,255,255,.03) 1px,transparent 1px);
  background-size:48px 48px;mask-image:radial-gradient(ellipse 90% 60% at 50% 0%,black 15%,transparent 80%);
}
.shell{display:grid;grid-template-columns:240px 1fr;min-height:100vh;position:relative;z-index:1}
.side{padding:26px 20px;border-right:1px solid var(--line);background:rgba(11,9,8,.85);backdrop-filter:blur(20px)}
.brand{display:flex;align-items:center;gap:8px;font-weight:800;letter-spacing:.12em;font-size:14px;color:var(--ink-site)}
.brand b{color:var(--accent-site);animation:pulse 2s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}
.nav{margin-top:36px;display:flex;flex-direction:column;gap:6px}
.nav button{
  display:block;width:100%;padding:10px 12px;border:1px solid transparent;border-radius:6px;
  background:transparent;color:var(--muted);text-align:left;font:500 13px var(--font-display);cursor:pointer;
  transition:all .18s var(--ease);
}
.nav button:hover{color:var(--ink-site);background:rgba(255,255,255,.04)}
.nav button.active{color:var(--ink-site);background:var(--paper-raised);border-color:var(--line);border-left:3px solid var(--accent-site)}
.main{padding:30px 34px;min-width:0;display:flex;flex-direction:column;gap:20px}
.top{display:flex;justify-content:space-between;align-items:flex-end}
.lead-label{font:700 10px var(--font-mono);color:var(--accent-site);letter-spacing:.18em;text-transform:uppercase;margin-bottom:4px}
.top h1{font-size:26px;font-weight:700;letter-spacing:-.02em}
.live-badge{
  display:inline-flex;align-items:center;gap:6px;padding:6px 12px;border:1px solid var(--emerald);
  border-radius:999px;background:var(--emerald-soft);color:var(--emerald);font:700 11px var(--font-mono);letter-spacing:.1em;
}
.live-badge.reconnecting{border-color:var(--yellow);color:var(--yellow);background:rgba(237,178,0,.1)}
.live-badge:before{content:'';width:7px;height:7px;border-radius:50%;background:currentColor;animation:pulse 1.6s infinite}
.cards{display:grid;grid-template-columns:repeat(4,1fr);gap:14px}
.card{padding:16px;border:1px solid var(--line);background:var(--paper-raised);border-radius:10px;backdrop-filter:blur(14px)}
.card-label{font:600 10px var(--font-mono);color:var(--dim);letter-spacing:.14em;text-transform:uppercase}
.card-value{font-size:20px;font-weight:750;margin-top:8px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.accent{color:var(--accent-site)}
.action-bar{
  display:flex;align-items:center;justify-content:space-between;gap:12px;padding:12px 16px;
  border:1px solid var(--line-hi);background:var(--paper-raised);border-radius:10px;flex-wrap:wrap;
}
.action-pills{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.act-btn{
  display:inline-flex;align-items:center;gap:6px;padding:7px 13px;border:1px solid var(--line);
  border-radius:6px;background:var(--paper-site);color:var(--ink-soft);font:500 12px var(--font-display);
  cursor:pointer;transition:all .18s var(--ease);
}
.act-btn:hover{color:var(--ink-site);border-color:var(--accent-site);transform:translateY(-1px)}
.act-btn.primary{border-color:var(--emerald);color:var(--emerald);background:var(--emerald-soft)}
.act-btn.primary:hover{background:var(--emerald);color:#000}
.cmd-box{display:flex;align-items:center;gap:8px;flex:1;min-width:280px}
.cmd-input{
  flex:1;padding:7px 12px;border:1px solid var(--line);border-radius:6px;
  background:var(--paper-site);color:var(--ink-site);font:12px var(--font-mono);outline:none;
}
.cmd-input:focus{border-color:var(--accent-site)}
.cmd-submit{
  padding:7px 14px;border:1px solid var(--accent-site);border-radius:6px;
  background:var(--accent-site);color:#000;font:700 12px var(--font-display);cursor:pointer;transition:all .18s var(--ease);
}
.cmd-submit:hover{opacity:.9}
.terminal{border:1px solid var(--line);background:var(--paper-raised);border-radius:10px;overflow:hidden;display:flex;flex-direction:column}
.terminal-head{padding:12px 16px;border-bottom:1px solid var(--line);display:flex;justify-content:space-between;align-items:center;font:700 11px var(--font-mono)}
.log{padding:16px;overflow:auto;white-space:pre-wrap;word-break:break-word;font:13px/1.65 var(--font-mono);flex:1;max-height:360px}
.line{color:var(--muted)}.line.info{color:var(--blue)}.line.ok{color:var(--emerald)}.line.warn{color:var(--yellow)}.line.bad{color:var(--red)}.line.action{color:var(--accent-site)}
.view[hidden]{display:none}

/* Pipeline Architecture View */
.pipeline{display:grid;grid-template-columns:repeat(6,1fr);gap:1px;background:var(--line);border:1px solid var(--line-hi);border-radius:10px;overflow:hidden;margin-bottom:18px}
.pipe-step{padding:12px;background:var(--paper-raised);display:flex;flex-direction:column;gap:4px;position:relative}
.pipe-step.active{background:rgba(254,110,0,.1);border-bottom:2px solid var(--accent-site)}
.pipe-step.done{background:rgba(0,199,88,.05);border-bottom:2px solid var(--emerald)}
.pipe-num{font:700 9px var(--font-mono);color:var(--dim);letter-spacing:.12em}
.pipe-name{font:600 11px var(--font-mono);color:var(--ink-soft);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.pipe-badge{font:700 9px var(--font-mono);color:var(--muted);margin-top:2px}
.pipe-step.active .pipe-badge{color:var(--accent-site)}
.pipe-step.done .pipe-badge{color:var(--emerald)}

/* Task Filter & List */
.filter-bar{display:flex;align-items:center;justify-content:space-between;gap:12px;margin:16px 0 12px;flex-wrap:wrap}
.filter-pills{display:flex;gap:6px}
.fpill{
  padding:5px 11px;border:1px solid var(--line);border-radius:999px;background:transparent;
  color:var(--muted);font:600 10px var(--font-mono);cursor:pointer;transition:all .18s var(--ease);
}
.fpill.active,.fpill:hover{color:var(--ink-site);border-color:var(--ink-site);background:var(--paper-raised)}
.task-grid{display:grid;grid-template-columns:1fr;gap:8px}
.task-card{
  display:grid;grid-template-columns:46px 90px 1fr;align-items:center;gap:12px;padding:12px 16px;
  border:1px solid var(--line);border-radius:8px;background:var(--paper-raised);transition:all .18s var(--ease);
}
.task-card:hover{border-color:var(--line-hi);transform:translateX(2px)}
.task-idx{font:700 12px var(--font-mono);color:var(--dim)}
.task-badge{
  font:700 10px var(--font-mono);padding:3px 7px;border-radius:4px;border:1px solid currentColor;
  text-align:center;letter-spacing:.08em;
}
.task-badge.DONE{color:var(--emerald);border-color:var(--emerald);background:var(--emerald-soft)}
.task-badge.ACTIVE{color:var(--accent-site);border-color:var(--accent-site);background:var(--accent-soft);animation:pulse 2s infinite}
.task-badge.TODO{color:var(--muted);border-color:var(--line)}
.task-label{font-size:13px;color:var(--ink-site);font-weight:500}

/* Detail Section */
.detail{padding:22px;border:1px solid var(--line);border-radius:10px;background:var(--paper-raised)}
.detail h2{font-size:18px;margin-bottom:14px;font-weight:600}
.detail-grid{display:grid;grid-template-columns:repeat(2,1fr);gap:12px}
.detail-row{padding:12px;border:1px solid var(--line);border-radius:6px;color:var(--muted)}
.detail-row b{display:block;color:var(--ink-site);font-size:15px;margin-top:4px}
.detail-list{list-style:none;margin-top:8px}
.detail-list li{padding:10px 0;border-bottom:1px solid var(--line);font:12px/1.5 var(--font-mono);color:var(--muted)}
.toast{
  position:fixed;bottom:24px;right:24px;padding:10px 16px;border-radius:6px;
  background:var(--emerald);color:#000;font:700 12px var(--font-display);
  box-shadow:0 8px 24px rgba(0,0,0,.4);opacity:0;pointer-events:none;transition:opacity .2s var(--ease);z-index:999;
}
.toast.visible{opacity:1}
@media(max-width:920px){.shell{grid-template-columns:1fr}.side{display:none}.cards{grid-template-columns:repeat(2,1fr)}.pipeline{grid-template-columns:repeat(3,1fr)}}
</style></head><body><div class="grid-bg"></div><div class="shell">
<aside class="side"><div class="brand"><b>◉</b> AGENT // CENTER</div><nav class="nav" aria-label="Views">
<button class="active" type="button" data-view="live">Live Operations</button>
<button type="button" data-view="tasks">Task Plan</button>
<button type="button" data-view="process">Process Activity</button>
<button type="button" data-view="safety">Safety Events</button>
<button type="button" data-view="attempts">Attempt Ledger</button>
</nav></aside>
<main class="main">
<div class="top">
  <div><div class="lead-label">// AUTONOMOUS OPERATIONS CONSOLE</div><h1>Terminal Monitor</h1></div>
  <div style="display:flex;align-items:center;gap:10px">
    <div id="instances-box" style="display:flex;align-items:center;gap:6px">
      <span style="font:600 10px var(--font-mono);color:var(--dim)">INSTANCE:</span>
      <select id="instance-picker" style="background:var(--paper-site);color:var(--ink-site);border:1px solid var(--line);border-radius:4px;font:11px var(--font-mono);padding:4px 8px;outline:none" onchange="switchInstance(this.value)"><option value="">Default Instance</option></select>
    </div>
    <div class="live-badge" id="connection">LIVE ●</div>
  </div>
</div>

<!-- LIVE OPERATIONS VIEW -->
<section class="view" data-view-panel="live">
<section class="cards">
<div class="card"><div class="card-label">AGENT STATE</div><div class="card-value accent" id="state">—</div></div>
<div class="card"><div class="card-label">PROCESS &amp; CPU</div><div class="card-value" id="process">—</div></div>
<div class="card"><div class="card-label">TASK PROGRESS</div><div class="card-value" id="progress">—</div></div>
<div class="card"><div class="card-label">GIT BRANCH</div><div class="card-value" id="branch">—</div></div>
</section>

<!-- TEST SUITE PROGRESS (WHEN OBSERVED) -->
<div class="detail" id="test-progress-box" style="padding:14px 18px;margin-top:2px" hidden>
  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
    <span style="font:700 11px var(--font-mono);color:var(--dim);letter-spacing:.1em">TEST SUITE PROGRESS</span>
    <span style="font:600 12px var(--font-mono)"><b id="tp-passed" style="color:var(--emerald)">0</b> passed · <b id="tp-failed" style="color:var(--red)">0</b> failed / <span id="tp-total">0</span> total</span>
  </div>
  <div style="width:100%;height:6px;background:var(--paper-site);border:1px solid var(--line);border-radius:999px;overflow:hidden">
    <div id="tp-bar" style="height:100%;width:0%;background:linear-gradient(90deg,var(--emerald),var(--accent-site));transition:width .3s ease"></div>
  </div>
</div>

<div class="action-bar">
<div class="action-pills">
<button class="act-btn primary" type="button" onclick="sendAction('answer','yes')">✓ Approve (yes)</button>
<button class="act-btn" type="button" onclick="sendAction('answer','proceed')">▶ Continue</button>
<button class="act-btn" type="button" onclick="sendAction('key','tab')">⇥ Mode (Tab)</button>
<button class="act-btn" type="button" onclick="sendAction('answer','proceed with the next task')">⚡ Nudge</button>
</div>
<form class="cmd-box" onsubmit="event.preventDefault();submitCommand();">
<input class="cmd-input" id="cmd-input" type="text" placeholder="Type prompt or operator instruction…"/>
<button class="cmd-submit" type="submit">Send</button>
</form>
</div>

<section class="terminal"><div class="terminal-head"><span>LIVE OPERATIONAL LOG</span><span class="dots" style="color:var(--accent-site)">● ● ●</span></div><div class="log" id="log">Waiting for monitor events…</div></section>
<section class="terminal snapshot-terminal" style="margin-top:14px;height:240px">
  <div class="terminal-head">
    <span>AGENT TERMINAL SNAPSHOT (REDACTED)</span>
    <div style="display:flex;align-items:center;gap:12px">
      <label style="font:11px var(--font-mono);color:var(--dim);display:flex;align-items:center;gap:4px;cursor:pointer"><input type="checkbox" id="autoscroll-chk" checked/> Auto-scroll</label>
      <span style="color:var(--accent-site)">◉</span>
    </div>
  </div>
  <pre class="log" id="terminal" style="font-family:var(--font-mono)">Waiting for terminal output…</pre>
</section>
</section>

<!-- TASK PLAN VIEW (ARCHIFY STYLE) -->
<section class="view" data-view-panel="tasks" hidden>
<div class="pipeline" id="pipeline-steps">
<div class="pipe-step" data-step="TASK_RECEIVED"><div class="pipe-num">01</div><div class="pipe-name">TASK_RECEIVED</div><div class="pipe-badge">INIT</div></div>
<div class="pipe-step" data-step="EXECUTING"><div class="pipe-num">02</div><div class="pipe-name">EXECUTING</div><div class="pipe-badge">PENDING</div></div>
<div class="pipe-step" data-step="VERIFYING"><div class="pipe-num">03</div><div class="pipe-name">VERIFYING</div><div class="pipe-badge">PENDING</div></div>
<div class="pipe-step" data-step="PR_CREATED"><div class="pipe-num">04</div><div class="pipe-name">PR_CREATED</div><div class="pipe-badge">PENDING</div></div>
<div class="pipe-step" data-step="CI_CHECKS"><div class="pipe-num">05</div><div class="pipe-name">CI_CHECKS</div><div class="pipe-badge">PENDING</div></div>
<div class="pipe-step" data-step="MERGED"><div class="pipe-num">06</div><div class="pipe-name">MERGED</div><div class="pipe-badge">PENDING</div></div>
</div>

<div class="cards" style="margin-bottom:12px">
<div class="card"><div class="card-label">TOTAL TASKS</div><div class="card-value" id="task-total">0</div></div>
<div class="card"><div class="card-label">COMPLETED</div><div class="card-value" id="task-completed" style="color:var(--emerald)">0</div></div>
<div class="card"><div class="card-label">IN PROGRESS</div><div class="card-value" id="task-in-progress" style="color:var(--yellow)">0</div></div>
<div class="card"><div class="card-label">VELOCITY &amp; ETA</div><div class="card-value" id="task-eta" style="font-size:16px;color:var(--ink-soft)">—</div></div>
</div>

<div class="filter-bar">
<div class="filter-pills">
<button class="fpill active" onclick="filterTasks('all')">ALL (<span id="fc-all">0</span>)</button>
<button class="fpill" onclick="filterTasks('active')">ACTIVE (<span id="fc-act">0</span>)</button>
<button class="fpill" onclick="filterTasks('pending')">PENDING (<span id="fc-pen">0</span>)</button>
<button class="fpill" onclick="filterTasks('completed')">DONE (<span id="fc-don">0</span>)</button>
</div>
<input class="cmd-input" id="task-search" type="text" placeholder="Filter task titles…" oninput="renderTaskList()" style="max-width:240px"/>
</div>

<div class="task-grid" id="task-items"><div class="task-card"><div class="task-idx">—</div><div class="task-badge TODO">WAIT</div><div class="task-label">Waiting for task data…</div></div></div>
</section>

<!-- PROCESS ACTIVITY VIEW -->
<section class="view" data-view-panel="process" hidden>
<div class="detail"><h2>Process Activity</h2><div class="detail-grid">
<div class="detail-row">Agent<b id="process-agent">—</b></div>
<div class="detail-row">CPU<b id="process-cpu">—</b></div>
<div class="detail-row">Oldest command<b id="process-age">—</b></div>
<div class="detail-row">PIDs<b id="process-pids">—</b></div>
</div><h2 style="margin-top:24px">Current commands</h2><ul class="detail-list" id="process-commands"><li>Waiting for process data…</li></ul></div>
</section>

<!-- SAFETY VIEW -->
<section class="view" data-view-panel="safety" hidden>
<div class="detail"><h2>Safety Events</h2><ul class="detail-list" id="safety-list"><li>No safety events recorded.</li></ul></div>
</section>

<!-- ATTEMPTS VIEW -->
<section class="view" data-view-panel="attempts" hidden>
<div class="detail"><h2>Attempt Ledger</h2><ul class="detail-list" id="attempt-list"><li>No continuation attempts recorded.</li></ul></div>
</section>
</main></div>
<div class="toast" id="toast">Dispatched</div>
<script>
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let currentFilter='all',allTaskList=[],stages=['TASK_RECEIVED','EXECUTING','VERIFYING','PR_CREATED','CI_CHECKS','MERGED'];
function tone(s){s=s.toUpperCase();if(/SUCCESS|COMPLETED|GREEN|MERGED/.test(s))return'ok';if(/ATTENTION|PAUSE|WARN|QUEUED/.test(s))return'warn';if(/FAILED|ERROR|BLOCKED|REFUSED/.test(s))return'bad';if(/SEND|MODE|START|INTERRUPT|RECOVER/.test(s))return'action';return'info'}
function showToast(msg){const t=document.getElementById('toast');t.textContent=msg;t.classList.add('visible');setTimeout(()=>t.classList.remove('visible'),2200)}
async function sendAction(action,payload){try{const res=await fetch('/api/send',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action,payload,key:action==='key'?payload:''})});const d=await res.json();showToast(d.ok?'Action Dispatched ✓':'Error: '+(d.error||'Failed'))}catch(e){showToast('Network error')}}
function submitCommand(){const el=document.getElementById('cmd-input');if(el&&el.value.trim()){sendAction('answer',el.value.trim());el.value=''}}
function showView(view){document.querySelectorAll('[data-view-panel]').forEach(p=>{p.hidden=p.dataset.viewPanel!==view});document.querySelectorAll('[data-view]').forEach(b=>{const a=b.dataset.view===view;b.classList.toggle('active',a);b.setAttribute('aria-selected',String(a))})}
document.querySelectorAll('[data-view]').forEach(button=>button.addEventListener('click',()=>showView(button.dataset.view)));
function filterTasks(f){currentFilter=f;document.querySelectorAll('.fpill').forEach(p=>p.classList.remove('active'));event&&event.target&&event.target.classList.add('active');renderTaskList()}
function ansiToHtml(text){
  if(!text)return'';
  const c={'30':'#555','31':'#ff5c5c','32':'#00c758','33':'#edb200','34':'#4daafc','35':'#d180ff','36':'#00e5ff','37':'#e6e1d6','90':'#777','91':'#ff8a80','92':'#69f0ae','93':'#ffe57f','94':'#82b1ff','95':'#ea80fc','96':'#84ffff','97':'#ffffff'};
  let t=esc(text);
  return t.replace(/\\x1b\\[([0-9;]+)m/g,(m,p)=>{
    const s=p.split(';');let st='';
    for(let code of s){
      if(code==='0')return'</span>';
      if(code==='1')st+='font-weight:700;';
      if(code==='2')st+='opacity:0.7;';
      if(code==='4')st+='text-decoration:underline;';
      if(c[code])st+='color:'+c[code]+';';
    }
    return st?'<span style="'+st+'">':'';
  });
}
function renderTaskList(){
  const search=(document.getElementById('task-search')?.value||'').toLowerCase();
  const listEl=document.getElementById('task-items');
  if(!listEl)return;
  const filtered=allTaskList.filter(t=>{
    if(currentFilter==='active'&&t.state!=='in_progress')return false;
    if(currentFilter==='pending'&&t.state!=='pending')return false;
    if(currentFilter==='completed'&&t.state!=='completed')return false;
    if(search&&!t.label.toLowerCase().includes(search))return false;
    return true;
  });
  if(!filtered.length){listEl.innerHTML='<div class="task-card"><div class="task-idx">—</div><div class="task-badge TODO">EMPTY</div><div class="task-label">No matching tasks.</div></div>';return}
  listEl.innerHTML=filtered.map((t,i)=>{
    const st=t.state||'pending';
    const badgeText=st==='completed'?'DONE':st==='in_progress'?'ACTIVE':'TODO';
    const num=String(i+1).padStart(2,'0');
    const dur=t.duration_formatted?'<span style="margin-left:auto;font:600 11px var(--font-mono);color:var(--dim)">'+esc(t.duration_formatted)+'</span>':'';
    return '<div class="task-card"><div class="task-idx">'+num+'</div><div class="task-badge '+badgeText+'">'+badgeText+'</div><div class="task-label">'+esc(t.label)+'</div>'+dur+'</div>';
  }).join('');
}
function updatePipeline(curStage){
  const idx=stages.indexOf(curStage);
  stages.forEach((st,i)=>{
    const el=document.querySelector('[data-step="'+st+'"]');
    if(!el)return;
    el.classList.remove('active','done');
    const badge=el.querySelector('.pipe-badge');
    if(i<idx||curStage==='MERGED'){el.classList.add('done');if(badge)badge.textContent='PASSED ✓'}
    else if(i===idx){el.classList.add('active');if(badge)badge.textContent='ACTIVE ●'}
    else{if(badge)badge.textContent='PENDING'}
  });
}
function applySnapshot(s,e,terminalData){
  const activity=s.activity||{};
  state.textContent=s.state||'starting';
  process.textContent=(s.process||'agent')+' · '+(activity.cpu_percent??0)+'% CPU';
  const t=s.todo||{};
  progress.textContent=(t.completed||0)+' / '+(t.total||0);
  branch.textContent=(s.git||{}).branch||'—';
  log.innerHTML=(e.lines||[]).map(x=>'<div class="line '+tone(x)+'">'+esc(x)+'</div>').join('')||'Waiting for events…';
  log.scrollTop=log.scrollHeight;
  terminal.innerHTML=ansiToHtml(terminalData.snapshot||'Waiting for terminal output…');
  if(document.getElementById('autoscroll-chk')?.checked){terminal.scrollTop=terminal.scrollHeight}
  document.getElementById('process-agent').textContent=(s.process||'agent')+' · '+(activity.active?'active':'idle');
  document.getElementById('process-cpu').textContent=String(activity.cpu_percent??0)+'%';
  document.getElementById('process-age').textContent=String(activity.oldest_seconds??0)+'s';
  document.getElementById('process-pids').textContent=(s.pids||[]).join(', ')||'none';
  document.getElementById('process-commands').innerHTML=(activity.commands||[]).length?activity.commands.map(x=>'<li>'+esc(x)+'</li>').join(''):'<li>No active commands.</li>';
  const tp=activity.test_progress;
  const testEl=document.getElementById('test-progress-box');
  if(tp&&testEl){
    testEl.hidden=false;
    document.getElementById('tp-passed').textContent=String(tp.passed||0);
    document.getElementById('tp-failed').textContent=String(tp.failed||0);
    document.getElementById('tp-total').textContent=String(tp.total||0);
    document.getElementById('tp-bar').style.width=(tp.percent||0)+'%';
  }

  document.getElementById('task-total').textContent=String(t.total||0);
  document.getElementById('task-completed').textContent=String(t.completed||0);
  document.getElementById('task-in-progress').textContent=String(t.in_progress||0);
  document.getElementById('fc-all').textContent=String(t.total||0);
  document.getElementById('fc-act').textContent=String(t.in_progress||0);
  document.getElementById('fc-pen').textContent=String(t.pending||0);
  document.getElementById('fc-don').textContent=String(t.completed||0);
  const eta=t.eta_formatted?t.eta_formatted:'—';
  const vel=t.avg_duration_seconds?String(t.avg_duration_seconds)+'s/task':'';
  document.getElementById('task-eta').textContent=vel?vel+' · ETA '+eta:eta;
  allTaskList=t.items||[];
  renderTaskList();
  updatePipeline((s.task||{}).stage||'TASK_RECEIVED');
  const safety=(s.policy_decisions||[]).map(x=>(x.timestamp||'')+' · '+(x.decision||'event')).concat((e.lines||[]).filter(x=>/ATTENTION|BLOCK|REFUSED|UNSAFE|SAFETY/i.test(x)));
  document.getElementById('safety-list').innerHTML=safety.length?safety.map(x=>'<li>'+esc(x)+'</li>').join(''):'<li>No safety events recorded.</li>';
  const attempts=(s.attempts||[]).map(x=>(x.timestamp||'')+' · '+(x.status||'unknown')+' · '+(x.observed_state||''));
  document.getElementById('attempt-list').innerHTML=attempts.length?attempts.map(x=>'<li>'+esc(x)+'</li>').join(''):'<li>No continuation attempts recorded.</li>';
  connection.textContent='LIVE ●';connection.classList.remove('reconnecting');
}
async function pollFallback(){try{const [sr,er,tr]=await Promise.all([fetch('/api/status',{cache:'no-store'}),fetch('/api/events',{cache:'no-store'}),fetch('/api/terminal',{cache:'no-store'})]);const s=await sr.json(),e=await er.json(),terminalData=await tr.json();applySnapshot(s,e,terminalData)}catch(e){connection.textContent='RECONNECTING';connection.classList.add('reconnecting')}}
async function refreshInstances(){
  try{
    const res=await fetch('/api/instances',{cache:'no-store'});
    const d=await res.json();
    const picker=document.getElementById('instance-picker');
    if(picker&&d.instances&&d.instances.length){
      picker.innerHTML=d.instances.map(inst=>'<option value="'+esc(inst.web_url||'')+'">'+esc(inst.process||inst.id)+' ['+esc(inst.state)+']</option>').join('');
    }
  }catch(e){}
}
function switchInstance(url){if(url&&url!==window.location.href){window.location.href=url}}
function setupStream(){
  if(window.EventSource){
    const ev=new EventSource('/api/stream');
    ev.onmessage=m=>{try{const d=JSON.parse(m.data);applySnapshot(d.status||{},d.events||{},d.terminal||{})}catch(e){}};
    ev.onerror=()=>{connection.textContent='POLLING ●';connection.classList.remove('reconnecting');setInterval(pollFallback,1000)};
  }else{setInterval(pollFallback,1000)}
}
setupStream();pollFallback();refreshInstances();
</script></body></html>"""


class MonitorWebServer:
    """Local dashboard serving status, streaming events, and accepting manual operator actions."""

    def __init__(
        self,
        status_path: str,
        log_path: str,
        port: int = 8765,
        snapshot_path: str = "",
        *,
        answer_path: str = "",
        state_root: str = "/tmp/terminal-monitor",
    ) -> None:
        self.status_path = status_path
        self.log_path = log_path
        self.snapshot_path = snapshot_path
        self.answer_path = answer_path
        self.state_root = str(state_root)
        self.port = validate_web_port(port)
        self.httpd: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None

    def start(self) -> str:
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                if self.path == "/":
                    self._reply(200, "text/html; charset=utf-8", DASHBOARD_HTML.encode())
                elif self.path == "/api/status":
                    try:
                        status = json.loads(Path(owner.status_path).read_text(encoding="utf-8"))
                        if not isinstance(status, dict):
                            raise ValueError("status must be a JSON object")
                        payload = json.dumps(_public_status(status), sort_keys=True).encode()
                    except (OSError, ValueError, TypeError, UnicodeError, json.JSONDecodeError):
                        payload = b'{"state":"starting","pids":[]}'
                    self._reply(200, "application/json", payload)
                elif self.path == "/api/events":
                    try:
                        lines = [_public_event_line(line) for line in Path(owner.log_path).read_text(encoding="utf-8").splitlines()[-400:]]
                    except (OSError, UnicodeError):
                        lines = []
                    self._reply(200, "application/json", json.dumps({"lines": lines}).encode())
                elif self.path == "/api/terminal":
                    try:
                        snapshot = Path(owner.snapshot_path).read_text(encoding="utf-8") if owner.snapshot_path else ""
                    except (OSError, UnicodeError):
                        snapshot = ""
                    self._reply(200, "application/json", json.dumps({"snapshot": snapshot}).encode())
                elif self.path == "/api/instances":
                    instances = []
                    base_dir = Path(owner.state_root)
                    if base_dir.exists():
                        for p in base_dir.iterdir():
                            if p.is_dir() and (p / "status.json").exists():
                                try:
                                    st = json.loads((p / "status.json").read_text(encoding="utf-8"))
                                    instances.append({
                                        "id": p.name,
                                        "process": st.get("process", p.name),
                                        "branch": st.get("git", {}).get("branch", "-"),
                                        "state": st.get("state", "unknown"),
                                        "running": st.get("running", False),
                                        "tasks": f"{st.get('todo', {}).get('completed', 0)}/{st.get('todo', {}).get('total', 0)}",
                                        "web_url": st.get("web_url", ""),
                                    })
                                except Exception:
                                    pass
                    self._reply(200, "application/json", json.dumps({"instances": instances}).encode())
                elif self.path == "/api/stream":
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Cache-Control", "no-cache")
                    self.send_header("Connection", "keep-alive")
                    self.send_header("X-Accel-Buffering", "no")
                    self.end_headers()
                    last_hash = ""
                    try:
                        for _ in range(300):
                            try:
                                status_raw = Path(owner.status_path).read_text(encoding="utf-8") if owner.status_path and os.path.exists(owner.status_path) else "{}"
                                curr_hash = hashlib.sha256(status_raw.encode()).hexdigest()[:16]
                                if curr_hash != last_hash:
                                    last_hash = curr_hash
                                    status_obj = json.loads(status_raw) if status_raw.strip() else {}
                                    events_lines = []
                                    if owner.log_path and os.path.exists(owner.log_path):
                                        events_lines = [_public_event_line(entry) for entry in Path(owner.log_path).read_text(encoding="utf-8").splitlines()[-400:]]
                                    term_snap = Path(owner.snapshot_path).read_text(encoding="utf-8") if owner.snapshot_path and os.path.exists(owner.snapshot_path) else ""
                                    combined = {
                                        "status": _public_status(status_obj),
                                        "events": {"lines": events_lines},
                                        "terminal": {"snapshot": term_snap},
                                    }
                                    self.wfile.write(f"data: {json.dumps(combined)}\n\n".encode())
                                    self.wfile.flush()
                                else:
                                    self.wfile.write(b": ping\n\n")
                                    self.wfile.flush()
                            except (OSError, UnicodeError, json.JSONDecodeError):
                                pass
                            time.sleep(1.0)
                    except (BrokenPipeError, ConnectionResetError):
                        pass
                    return
                else:
                    self._reply(404, "application/json", b'{"error":"not found"}')

            def do_POST(self) -> None:
                if self.path in ("/api/send", "/api/answer"):
                    try:
                        length = int(self.headers.get("Content-Length", 0))
                        if length > WEB_POST_BODY_LIMIT_BYTES:
                            self._reply(413, "application/json", b'{"ok":false,"error":"payload too large"}')
                            return
                        body = self.rfile.read(length).decode("utf-8")
                        data = json.loads(body) if body else {}
                        action = str(data.get("action", "answer")).strip()
                        payload = str(data.get("payload", "")).strip()
                        key = str(data.get("key", "")).strip()
                        target_path = owner.answer_path or "/tmp/terminal-monitor/answer.txt"
                        Path(target_path).parent.mkdir(parents=True, exist_ok=True)
                        if action == "key" and key:
                            Path(target_path).write_text(f"KEY:{key}\n", encoding="utf-8")
                            self._reply(200, "application/json", b'{"ok":true,"dispatched":"key"}')
                            return
                        if payload:
                            Path(target_path).write_text(payload + "\n", encoding="utf-8")
                            self._reply(200, "application/json", b'{"ok":true,"dispatched":"payload"}')
                            return
                        self._reply(400, "application/json", b'{"ok":false,"error":"missing payload or key"}')
                    except Exception as exc:
                        self._reply(500, "application/json", json.dumps({"ok": False, "error": str(exc)}).encode())
                else:
                    self._reply(404, "application/json", b'{"error":"not found"}')

            def _reply(self, status: int, content_type: str, payload: bytes) -> None:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'")
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, _format: str, *_args: Any) -> None:
                return

        try:
            self.httpd = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        except OSError:
            self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = int(self.httpd.server_address[1])
        self.thread = threading.Thread(target=self.httpd.serve_forever, name="terminal-monitor-web", daemon=True)
        self.thread.start()
        return f"http://127.0.0.1:{self.port}/"

    def stop(self) -> None:
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=2.0)
