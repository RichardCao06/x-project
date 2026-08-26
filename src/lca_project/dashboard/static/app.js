const app = document.querySelector('#app');
const title = document.querySelector('#page-title');
const index = document.querySelector('#page-index');
const drawer = document.querySelector('#drawer');
const drawerContent = document.querySelector('#drawer-content');
const backdrop = document.querySelector('#drawer-backdrop');
let autoRefresh = true;
let refreshTimer;
let activeJsonDocument = null;
const jobViewState = new Map();

const pages = {
  overview: ['01', '运行总览'], jobs: ['02', '任务与目标'], workflows: ['03', '工作流运行'],
  artifacts: ['04', '产物账本'], events: ['05', '事件流'], exceptions: ['06', '异常与修复'], system: ['07', '系统构成']
};
const labels = {
  planned:'已规划', ready:'就绪', running:'运行中', paused:'已暂停', succeeded:'已成功', failed:'失败', repairable:'可修复',
  retryable:'可重试', quarantined:'已隔离', blocked:'已阻塞', blocked_budget:'预算阻塞', published:'已发布',
  pending:'等待中', candidate:'候选', gated:'已门禁', applied:'已应用', pass:'通过', fail:'未通过', ok:'正常',
  diagnostic_preview:'诊断预览', evidence_limited:'证据受限'
};

const h = value => String(value ?? '').replace(/[&<>'"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));
const short = (value, n=11) => value ? `${String(value).slice(0,n)}${String(value).length > n ? '…' : ''}` : '—';
const fmtDate = value => {
  if (!value) return '—';
  const date = new Date(value); return Number.isNaN(date.valueOf()) ? h(value) : date.toLocaleString('zh-CN', {hour12:false});
};
const fmtBytes = bytes => bytes < 1024 ? `${bytes} B` : bytes < 1048576 ? `${(bytes/1024).toFixed(1)} KB` : `${(bytes/1048576).toFixed(1)} MB`;
const badge = value => `<span class="status ${h(String(value||'').toLowerCase())}">${h(labels[value] || value || 'unknown')}</span>`;
const empty = (name, detail='当前没有可展示的持久化记录。') => `<div class="empty"><b>${h(name)}</b>${h(detail)}</div>`;
const jsonView = value => `<pre class="json-view">${h(JSON.stringify(value ?? {}, null, 2))}</pre>`;
const pct = (part,total) => total ? Math.round(part/total*100) : 0;
const relativeTime = value => {
  if (!value) return '未记录';
  const elapsed = Date.now() - new Date(value).valueOf();
  if (!Number.isFinite(elapsed)) return fmtDate(value);
  const seconds = Math.max(0, Math.floor(elapsed / 1000));
  if (seconds < 60) return `${seconds} 秒前`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes} 分钟前`;
  const hours = Math.floor(minutes / 60);
  return hours < 48 ? `${hours} 小时前` : fmtDate(value);
};

async function api(path, options={}) {
  const response = await fetch(path, {headers:{'Content-Type':'application/json'}, ...options});
  const body = await response.json().catch(() => ({message:'服务器返回了不可解析的响应'}));
  if (!response.ok) throw new Error(body.message || `HTTP ${response.status}`);
  return body;
}

function setPage(name, subtitle='') {
  const [number, text] = pages[name] || pages.overview;
  index.textContent = `CONTROL / ${number}${subtitle ? ` · ${subtitle}` : ''}`;
  title.textContent = text;
  document.querySelectorAll('.nav a').forEach(node => node.classList.toggle('active', node.dataset.route === name));
}

function loading() { app.innerHTML = '<div class="loading-stage"><span></span><p>正在读取控制平面…</p></div>'; }
function failure(error) { app.innerHTML = `<div class="error-stage"><h2>读取失败</h2><p>${h(error.message)}</p><button class="action-button" data-retry>重新读取</button></div>`; app.querySelector('[data-retry]')?.addEventListener('click', route); }
function toast(message, error=false) {
  const node = document.createElement('div'); node.className = `toast${error?' error':''}`; node.textContent = message;
  document.querySelector('#toasts').append(node); setTimeout(() => node.remove(), 4200);
}
function openDrawer(content, mode='detail') { drawerContent.innerHTML = content; drawer.classList.toggle('json-mode',mode==='json'); drawer.classList.add('open'); drawer.setAttribute('aria-hidden','false'); backdrop.hidden=false; }
function closeDrawer() { drawer.classList.remove('open','json-mode'); drawer.setAttribute('aria-hidden','true'); backdrop.hidden=true; activeJsonDocument=null; }

function jsonHighlight(value, query) {
  const raw=String(value??''), needle=String(query||'').toLowerCase();
  if(!needle)return h(raw);
  let cursor=0, output='', lower=raw.toLowerCase(), found;
  while((found=lower.indexOf(needle,cursor))!==-1){output+=h(raw.slice(cursor,found))+`<mark>${h(raw.slice(found,found+needle.length))}</mark>`;cursor=found+needle.length;}
  return output+h(raw.slice(cursor));
}
function jsonMatches(value,query,key='') {
  const needle=String(query||'').trim().toLowerCase(); if(!needle)return true;
  if(String(key).toLowerCase().includes(needle))return true;
  if(value===null||typeof value!=='object')return String(value).toLowerCase().includes(needle);
  return Object.entries(value).some(([childKey,child])=>jsonMatches(child,needle,childKey));
}
function jsonMatchCount(value,query,key='') {
  const needle=String(query||'').trim().toLowerCase(); if(!needle)return 0;
  let count=String(key).toLowerCase().includes(needle)?1:0;
  if(value===null||typeof value!=='object')return count+(String(value).toLowerCase().includes(needle)?1:0);
  return count+Object.entries(value).reduce((sum,[childKey,child])=>sum+jsonMatchCount(child,needle,childKey),0);
}
function jsonScalar(value,query) {
  if(value===null)return '<span class="json-null">null</span>';
  if(typeof value==='string')return `<span class="json-string">&quot;${jsonHighlight(value,query)}&quot;</span>`;
  if(typeof value==='number')return `<span class="json-number">${h(value)}</span>`;
  if(typeof value==='boolean')return `<span class="json-boolean">${h(value)}</span>`;
  return `<span>${jsonHighlight(value,query)}</span>`;
}
function jsonTree(value,query='',key='ROOT',path='$',depth=0) {
  if(value===null||typeof value!=='object')return `<div class="json-leaf"><code>${jsonHighlight(key,query)}</code><span>:</span>${jsonScalar(value,query)}<small>${h(path)}</small></div>`;
  const entries=Object.entries(value), keyHit=String(key).toLowerCase().includes(String(query||'').toLowerCase());
  const visible=query&&!keyHit?entries.filter(([childKey,child])=>jsonMatches(child,query,childKey)):entries;
  const array=Array.isArray(value), open=query||depth<1?'open':'';
  return `<details class="json-branch" ${open}><summary><i>${array?'[ ]':'{ }'}</i><code>${jsonHighlight(key,query)}</code><b>${entries.length}</b><small>${h(path)}</small></summary><div>${visible.length?visible.map(([childKey,child])=>jsonTree(child,keyHit?'':query,childKey,array?`${path}[${childKey}]`:`${path}.${childKey}`,depth+1)).join(''):`<div class="json-no-match">${query?'该分支没有匹配项':'空对象 / 数组'}</div>`}</div></details>`;
}
function jsonViewerMarkup(document) {
  const meta=document.metadata||{};
  return `<div class="json-inspector"><header class="json-inspector-head"><div><span class="section-kicker">UNIFIED JSON INSPECTOR</span><h2>${h(document.filename||'document.json')}</h2><p>${h(document.source_label_zh)} · ${fmtBytes(Number(document.size||0))}</p></div><span class="json-verified">✓ HASH VERIFIED</span></header><div class="json-proof"><span><b>SHA-256</b><code>${h(document.digest)}</code></span><span><b>验证说明</b>${h(document.verification_zh)}</span><span><b>逻辑路径</b>${h(document.logical_path||'CAS 根文档')}</span></div><div class="json-toolbar"><label><span>搜索 Key、Value 或路径</span><input class="field" id="json-search" placeholder="例如：decision、BLOCKED、confirmed_urls"></label><div class="json-toolbar-actions"><button class="quiet-button" id="json-collapse" type="button">全部折叠</button><button class="quiet-button" id="json-expand" type="button">全部展开</button><button class="quiet-button" id="json-copy" type="button">复制 JSON</button><button class="action-button secondary" id="json-download" type="button">下载 .json</button></div></div><div class="json-view-modes"><button class="active" type="button" data-json-mode="tree">结构树</button><button type="button" data-json-mode="raw">原始文本</button><span id="json-search-status">${Object.keys(meta).length} 项来源元数据</span></div><section class="json-tree" id="json-tree">${jsonTree(document.value)}</section><pre class="json-raw" id="json-raw" hidden>${h(JSON.stringify(document.value,null,2))}</pre><details class="json-source-meta"><summary>查看来源元数据</summary>${jsonView(meta)}</details></div>`;
}
function bindJsonViewer(document) {
  activeJsonDocument=document;
  const tree=drawerContent.querySelector('#json-tree'), raw=drawerContent.querySelector('#json-raw'), search=drawerContent.querySelector('#json-search'), status=drawerContent.querySelector('#json-search-status');
  let timer;
  const render=()=>{const query=search.value.trim();tree.innerHTML=jsonTree(document.value,query);const count=jsonMatchCount(document.value,query);status.textContent=query?`${count} 个 Key / Value 命中`:`${fmtBytes(Number(document.size||0))} · 完整 JSON`;};
  search.addEventListener('input',()=>{clearTimeout(timer);timer=setTimeout(render,120)});
  drawerContent.querySelector('#json-collapse').addEventListener('click',()=>tree.querySelectorAll('details').forEach(node=>node.open=false));
  drawerContent.querySelector('#json-expand').addEventListener('click',()=>tree.querySelectorAll('details').forEach(node=>node.open=true));
  drawerContent.querySelectorAll('[data-json-mode]').forEach(button=>button.addEventListener('click',()=>{const isRaw=button.dataset.jsonMode==='raw';drawerContent.querySelectorAll('[data-json-mode]').forEach(node=>node.classList.toggle('active',node===button));tree.hidden=isRaw;raw.hidden=!isRaw;}));
  drawerContent.querySelector('#json-copy').addEventListener('click',async()=>{const text=JSON.stringify(document.value,null,2);try{await navigator.clipboard.writeText(text);}catch{const area=document.createElement('textarea');area.value=text;document.body.append(area);area.select();document.execCommand('copy');area.remove();}toast('JSON 已复制到剪贴板');});
  drawerContent.querySelector('#json-download').addEventListener('click',()=>{const blob=new Blob([JSON.stringify(document.value,null,2)+'\n'],{type:'application/json;charset=utf-8'}),url=URL.createObjectURL(blob),link=document.createElement('a');link.href=url;link.download=String(document.filename||'document.json').replace(/[^a-zA-Z0-9._-]+/g,'-');document.body.append(link);link.click();link.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);toast(`已下载 ${link.download}`);});
}
async function openJsonViewer(endpoint) { try{const document=await api(endpoint);openDrawer(jsonViewerMarkup(document),'json');bindJsonViewer(document);}catch(err){toast(err.message,true);} }
const jsonArtifactViewer=digest=>openJsonViewer(`/api/json/artifacts/${encodeURIComponent(digest)}`);
const jsonSnapshotViewer=(jobId,taskId,attemptId,path)=>openJsonViewer(`/api/json/snapshots/${encodeURIComponent(jobId)}/${encodeURIComponent(taskId)}/${encodeURIComponent(attemptId)}?path=${encodeURIComponent(path)}`);
function bindJsonLinks(){document.querySelectorAll('[data-json-artifact],[data-json-snapshot]').forEach(node=>{if(node.dataset.jsonBound)return;node.dataset.jsonBound='1';const open=()=>node.dataset.jsonArtifact?jsonArtifactViewer(node.dataset.jsonArtifact):jsonSnapshotViewer(node.dataset.jsonJob,node.dataset.jsonTask,node.dataset.jsonAttempt,node.dataset.jsonPath);node.addEventListener('click',open);node.addEventListener('keydown',event=>{if(event.key==='Enter')open();});});}

function stateBars(states) {
  const entries = Object.entries(states || {}); const total = entries.reduce((sum,[,n])=>sum+n,0);
  if (!entries.length) return empty('暂无状态分布');
  return `<div class="state-bars">${entries.map(([state,count]) => `<div class="state-row"><span>${h(labels[state]||state)}</span><div class="bar"><i style="width:${pct(count,total)}%"></i></div><b>${count}</b></div>`).join('')}</div>`;
}
const eventTypeNames={
  'workflow.materialized':'工作流已物化','task.claimed':'阶段已被 Worker 领取','task.succeeded':'阶段执行成功',
  'task.failed':'阶段执行失败','workflow.rewound':'工作流已受控回卷','job.transitioned':'Job 状态已变化',
  'goal_alignment.audited':'目标对齐审计完成','goal_alignment.supervision_requested':'已请求自治监督',
  'governance.execution_admission':'治理策略完成执行准入判定'
};
function eventSummary(item){
  const p=item.payload||{}, parts=[];
  if(p.task_id)parts.push(`阶段 ${p.task_id}`);
  if(p.worker_id)parts.push(`Worker ${p.worker_id}`);
  if(p.failure_code)parts.push(`失败 ${p.failure_code}`);
  if(p.repair_action)parts.push(`处置 ${p.repair_action}`);
  if(p.from_task)parts.push(`从 ${p.from_task} 回卷`);
  if(p.reason)parts.push(p.reason);
  if(Array.isArray(p.invalidated_tasks))parts.push(`失效 ${p.invalidated_tasks.length} 个阶段`);
  return parts.join(' · ')||`${item.aggregate_type} · ${short(item.aggregate_id,22)} · ${item.actor||'system'}`;
}
function eventList(items, limit=20) {
  if (!items?.length) return empty('暂无事件');
  return `<div class="timeline">${items.slice(0,limit).map(item => `<div class="timeline-item"><span class="sequence">#${item.sequence}</span><div><b>${h(eventTypeNames[item.event_type]||item.event_type)}</b><small>${h(item.event_type)}</small><p>${h(eventSummary(item))} · ${fmtDate(item.occurred_at)}</p></div></div>`).join('')}</div>`;
}

async function overview() {
  setPage('overview'); const data = await api('/api/overview'); const c = data.counts;
  const active = (data.job_states.running||0)+(data.job_states.ready||0)+(data.job_states.planned||0);
  const healthy = c.open_exceptions === 0;
  app.innerHTML = `<div class="reveal">
    <section class="hero-strip">
      <div class="hero-primary"><span class="section-kicker">DESIRED / OBSERVED</span><h2>让每一次生产，都留下可以证明的轨迹。</h2><p>从 Job 意图到 Workflow、Artifact、Gate 与 Release，控制平面只接受持久化事实。当前数据更新时间 ${fmtDate(data.generated_at)}。</p></div>
      <div class="hero-side"><div class="system-orbit"><i></i><strong>${healthy?'稳定':'关注'}</strong><small>${healthy?'NO OPEN EXCEPTIONS':`${c.open_exceptions} OPEN EXCEPTIONS`}</small></div></div>
    </section>
    <section class="metric-grid">
      <article class="metric" style="--fill:${Math.min(c.jobs*8,100)}%"><label>持久化任务</label><strong>${c.jobs}</strong><small>${active} 个处于活动状态</small></article>
      <article class="metric" style="--fill:${pct(data.task_states.succeeded||0,c.tasks)}%;--tone:var(--blue)"><label>工作流任务</label><strong>${c.tasks}</strong><small>${data.task_states.succeeded||0} 个已成功</small></article>
      <article class="metric" style="--fill:${Math.min(c.artifacts*2,100)}%;--tone:var(--amber)"><label>不可变产物</label><strong>${c.artifacts}</strong><small>CAS 内容寻址记录</small></article>
      <article class="metric" style="--fill:${healthy?100:25}%;--tone:${healthy?'var(--mint)':'var(--red)'}"><label>未解决异常</label><strong>${c.open_exceptions}</strong><small>${c.active_leases} 个活动 Lease</small></article>
    </section>
    <section class="dashboard-grid">
      <article class="panel"><header class="panel-head"><div><h2>最近任务</h2><p>按最后更新时间排序</p></div><a class="action-button secondary" href="#/jobs">查看全部</a></header><div class="panel-body table-wrap">${jobTable(data.recent_jobs)}</div></article>
      <article class="panel"><header class="panel-head"><div><h2>Job 状态</h2><p>${c.jobs} 个任务的当前投影</p></div></header><div class="panel-body">${stateBars(data.job_states)}</div></article>
      <article class="panel"><header class="panel-head"><div><h2>Task 状态</h2><p>持久化 DAG 执行面</p></div></header><div class="panel-body">${stateBars(data.task_states)}</div></article>
      <article class="panel"><header class="panel-head"><div><h2>最新事件</h2><p>Event Log 的最近写入</p></div><a class="action-button secondary" href="#/events">打开事件流</a></header><div class="panel-body">${eventList(data.recent_events,8)}</div></article>
      <article class="panel"><header class="panel-head"><div><h2>自治 Campaign</h2><p>自动创建、监测、修复和验证</p></div></header><div class="panel-body">${autonomyMini(data.autonomy)}</div></article>
    </section>
  </div>`;
  bindRows();
}

function jobTable(items) {
  if (!items?.length) return empty('暂无 Job','通过 Skill 或 Desired State 提交的任务会显示在这里。');
  return `<table class="data-table"><thead><tr><th>目标</th><th>状态</th><th>Workflow</th><th>Policy</th><th>更新时间</th></tr></thead><tbody>${items.map(item => `<tr data-href="#/jobs/${encodeURIComponent(item.id)}"><td><b>${h(item.target)}</b><div class="mono clip">${h(item.id)}</div></td><td>${badge(item.status)}</td><td class="mono">${h(item.workflow_id||'—')}</td><td class="mono">${h(item.policy_version||'—')}</td><td>${fmtDate(item.updated_at)}</td></tr>`).join('')}</tbody></table>`;
}
function bindRows() { document.querySelectorAll('[data-href]').forEach(row => row.addEventListener('click', () => { location.hash=row.dataset.href; })); }
function autonomyMini(data) {
  const items=data?.items||[]; if(!items.length)return empty('暂无自治 Campaign','创建任务时可启用“交给自治 Supervisor”。');
  return `<div class="stack">${items.map(v=>{const c=v.campaign,done=v.items.filter(x=>['succeeded','evidence_limited'].includes(x.status)).length;return `<div><span class="section-kicker">${h(c.name)}</span><p>${badge(c.status)} ${done}/${v.items.length} 完成 · ${h(c.skill)}</p></div>`}).join('')}</div>`;
}

async function jobs() {
  setPage('jobs');
  app.innerHTML = `<div class="toolbar"><input class="field search-field" id="job-q" placeholder="搜索节点、Job ID、Workflow…"><select class="field" id="job-status"><option value="">全部状态</option></select><a class="action-button create-link" href="#/jobs/new">＋ 创建任务</a></div><section class="panel"><div class="panel-body table-wrap" id="job-results"></div></section>`;
  const q=app.querySelector('#job-q'), status=app.querySelector('#job-status'), target=app.querySelector('#job-results');
  let timer;
  async function load() {
    target.innerHTML='<div class="loading-stage"><span></span></div>';
    const data=await api(`/api/jobs?q=${encodeURIComponent(q.value)}&status=${encodeURIComponent(status.value)}`);
    if (status.options.length===1) data.states.forEach(s=>status.insertAdjacentHTML('beforeend',`<option value="${h(s.status)}">${h(labels[s.status]||s.status)} · ${s.count}</option>`));
    target.innerHTML=`<div class="panel-head"><div><h2>${data.total} 个持久化任务</h2><p>点击任务查看完整 DAG、证据和事件</p></div></div>${jobTable(data.items)}`; bindRows();
  }
  q.addEventListener('input',()=>{clearTimeout(timer);timer=setTimeout(load,250)}); status.addEventListener('change',load); await load();
}

function schemaField(name, spec, required) {
  const id=`field-${name}`, label=`${h(name)}${required?'<em>必填</em>':''}`;
  const hint=[spec.description, spec.pattern?`格式 ${spec.pattern}`:'', spec.minItems?`至少 ${spec.minItems} 项`:'', spec.maxItems?`最多 ${spec.maxItems} 项`:''].filter(Boolean).join(' · ');
  let control='';
  if(Array.isArray(spec.enum)) {
    control=`<select class="field schema-input" id="${h(id)}" data-name="${h(name)}" data-type="${h(spec.type||'string')}"><option value="">请选择</option>${spec.enum.map(value=>`<option value="${h(value)}" ${spec.default===value?'selected':''}>${h(value)}</option>`).join('')}</select>`;
  } else if(spec.type==='boolean') {
    control=`<label class="switch"><input class="schema-input" id="${h(id)}" data-name="${h(name)}" data-type="boolean" type="checkbox" ${spec.default===true?'checked':''}><i></i><span>启用</span></label>`;
  } else if(spec.type==='array') {
    const initial=Array.isArray(spec.default)?spec.default.join('\n'):'';
    control=`<textarea class="field schema-input schema-textarea" id="${h(id)}" data-name="${h(name)}" data-type="array" data-item-type="${h(spec.items?.type||'string')}" placeholder="每行一项；也可用逗号分隔">${h(initial)}</textarea>`;
  } else if(spec.type==='object') {
    control=`<textarea class="field schema-input schema-textarea" id="${h(id)}" data-name="${h(name)}" data-type="object" placeholder="输入 JSON 对象">${spec.default?h(JSON.stringify(spec.default,null,2)):''}</textarea>`;
  } else {
    const inputType=['integer','number'].includes(spec.type)?'number':'text';
    control=`<input class="field schema-input" id="${h(id)}" data-name="${h(name)}" data-type="${h(spec.type||'string')}" type="${inputType}" value="${h(spec.default??'')}" ${spec.pattern?`pattern="${h(spec.pattern)}"`:''} ${spec.minLength!=null?`minlength="${spec.minLength}"`:''}>`;
  }
  return `<div class="schema-field"><label for="${h(id)}">${label}</label>${control}${hint?`<small>${h(hint)}</small>`:''}</div>`;
}

async function createJob() {
  setPage('jobs','CREATE'); const catalog=await api('/api/skills');
  if(!catalog.items.length){app.innerHTML=empty('没有可用 Skill','Skill Registry 中没有可提交的机器可读路由。');return;}
  app.innerHTML=`<div class="create-layout reveal"><section class="skill-rail"><span class="section-kicker">REGISTERED INTENTS</span><h2>选择生产能力</h2><p>每个入口都固定绑定 Workflow、Policy 与请求契约。</p><div class="skill-list" id="skill-list">${catalog.items.map((skill,i)=>`<button class="skill-choice ${i===0?'active':''}" type="button" data-skill="${h(skill.name)}"><b>${h(skill.name)}</b><span>${h(skill.workflow)}</span></button>`).join('')}</div></section><section class="panel create-panel"><header class="panel-head"><div><h2 id="create-title">创建任务</h2><p id="create-description"></p></div><span class="status ready">SCHEMA CONTROLLED</span></header><form class="panel-body create-form" id="create-form"><div class="route-strip" id="route-strip"></div><div class="schema-grid" id="schema-fields"></div><details class="advanced"><summary>高级提交选项</summary><div class="schema-grid"><div class="schema-field"><label for="idempotency-key">幂等键</label><input class="field" id="idempotency-key" placeholder="可选；留空时按 Skill 与请求内容计算"><small>相同幂等键不会创建第二个 Job。</small></div><div class="schema-field"><label>后续处理</label><label class="switch"><input id="auto-materialize" type="checkbox" checked><i></i><span>创建后立即物化 Workflow</span></label><small>只展开持久化 DAG，不会直接执行 Capability。</small></div><div class="schema-field"><label>自治执行</label><label class="switch"><input id="autonomous-supervision" type="checkbox"><i></i><span>交给自治 Supervisor</span></label><small>Supervisor 将幂等创建、物化、执行、监测、有限修复并验证该 Job。</small></div></div></details><div class="submit-band"><div><span class="section-kicker">AUTHORITY BOUNDARY</span><p>提交将通过 SkillInvoker 再次校验，页面不能绕过 Registry 创建底层 Job。</p></div><button class="action-button create-submit" type="submit">提交任务 →</button></div></form></section></div>`;
  const list=app.querySelector('#skill-list'), fields=app.querySelector('#schema-fields'), form=app.querySelector('#create-form');let current;
  function select(name){current=catalog.items.find(item=>item.name===name);if(!current)return;list.querySelectorAll('.skill-choice').forEach(node=>node.classList.toggle('active',node.dataset.skill===name));app.querySelector('#create-title').textContent=current.name;app.querySelector('#create-description').textContent=current.description||'通过注册的 Skill 路由创建持久化任务。';app.querySelector('#route-strip').innerHTML=`<div><label>WORKFLOW</label><b>${h(current.workflow)}</b></div><div><label>POLICY</label><b>${h(current.policy)}</b></div><div><label>INPUT SCHEMA</label><b>${h(current.input_schema)}</b></div><div><label>SKILL VERSION</label><b>v${h(current.version)}</b></div>`;const required=new Set(current.schema.required||[]);fields.innerHTML=Object.entries(current.schema.properties||{}).map(([key,spec])=>schemaField(key,spec,required.has(key))).join('')||empty('Schema 没有字段');}
  list.addEventListener('click',e=>{const button=e.target.closest('[data-skill]');if(button)select(button.dataset.skill)});select(catalog.items[0].name);
  form.addEventListener('submit',async e=>{e.preventDefault();const button=form.querySelector('.create-submit');button.disabled=true;button.textContent='正在提交…';try{const request={};for(const input of form.querySelectorAll('.schema-input')){const type=input.dataset.type,name=input.dataset.name;let value;if(type==='boolean')value=input.checked;else if(type==='array'){const raw=input.value.trim();if(!raw)continue;value=raw.split(/[\n,，]+/).map(x=>x.trim()).filter(Boolean);if(input.dataset.itemType==='number')value=value.map(Number);else if(input.dataset.itemType==='integer')value=value.map(x=>parseInt(x,10));}else if(type==='object'){if(!input.value.trim())continue;value=JSON.parse(input.value);}else if(type==='integer'){if(!input.value)continue;value=parseInt(input.value,10);}else if(type==='number'){if(!input.value)continue;value=Number(input.value);}else{if(!input.value.trim())continue;value=input.value.trim();}request[name]=value;}if(form.querySelector('#autonomous-supervision').checked){const target=request.nodes?.[0]||request.target||'request';const completionGoal=request.publication_mode==='reviewed'?'reviewed_publication':'lca_modeling_ready';const spec={schema_version:'autonomous-job-campaign-v1',name:`${current.name}:${target}`,skill:current.name,requests:[request],completion_goal:completionGoal,max_concurrency:1,max_auto_repairs_per_job:3,poll_seconds:2,stop_on_failure:false};const result=await api('/api/autonomy',{method:'POST',body:JSON.stringify({spec,start:true})});const item=result.items?.[0];toast('自治 Campaign 已启动');location.hash=item?.job_id?`#/jobs/${encodeURIComponent(item.job_id)}`:'#/jobs';return;}const payload={skill:current.name,request,materialize:form.querySelector('#auto-materialize').checked};const key=form.querySelector('#idempotency-key').value.trim();if(key)payload.idempotency_key=key;const result=await api('/api/jobs',{method:'POST',body:JSON.stringify(payload)});toast(result.deduplicated?'已找到相同任务，未重复创建':'任务创建成功');location.hash=`#/jobs/${encodeURIComponent(result.job_id)}`;}catch(err){toast(err instanceof SyntaxError?'JSON 字段格式不正确':err.message,true);button.disabled=false;button.textContent='提交任务 →';}});
}

async function jobDetail(jobId, {background=false}={}) {
  setPage('jobs','JOB DETAIL');
  const data=await api(`/api/jobs/${encodeURIComponent(jobId)}`);
  const prior=jobViewState.get(jobId)||{};
  const repair=data.execution_trace?.repair_activity?.latest;
  const renderVersion=[data.job?.updated_at,data.run?.updated_at,repair?.repair_run_id,repair?.status,repair?.updated_at,data.events?.[0]?.sequence].join('|');
  if(background&&prior.renderVersion===renderVersion){scheduleJobDetailRefresh(jobId,data);return;}
  const previousScroll=background?window.scrollY:0;
  const job=data.job, payload=job.payload||{}, tasks=data.tasks||[], preview=data.preview, done=tasks.filter(t=>t.status==='succeeded').length;
  const canMaterialize=!data.run;
  const canPause=['planned','ready','leased','running','stalled','retryable','repairable','manual_review','blocked_budget'].includes(job.status);
  app.innerHTML=`<div class="reveal">
    <section class="job-hero"><div><span class="section-kicker">${h(job.workflow_id||'UNMATERIALIZED')}</span><h2>${h(payload.target||job.id)}</h2><p class="mono">${h(job.id)}</p><div class="job-meta"><span>状态 <b>${h(labels[job.status]||job.status)}</b></span><span>策略 <b>${h(payload.policy_version||'—')}</b></span><span>风险 <b>${h(payload.risk||'standard')}</b></span><span>更新 <b>${fmtDate(job.updated_at)}</b></span></div></div>${data.run?`<div><div class="progress-ring" style="--progress:${pct(done,tasks.length)}%"><strong>${done}/${tasks.length}</strong><small>成功阶段</small></div><div class="job-actions">${preview?`<a class="action-button" href="${h(preview.url)}" target="_blank" rel="noopener">打开 Preview ↗</a>`:''}${job.status==='paused'?`<button class="action-button" id="resume-job">恢复任务</button>`:canPause?`<button class="action-button" id="run-worker">执行下一步</button><button class="action-button secondary" id="pause-job">暂停任务</button>`:''}</div></div>`:`<button class="action-button" id="materialize">物化 Workflow</button>`}</section>
    ${repairActivityBanner(data.execution_trace?.repair_activity)}
    ${preview?`<section class="panel" style="margin-bottom:18px"><header class="panel-head"><div><h2>Wiki Preview 已就绪</h2><p>${h(preview.start_node||'')} · ${h(labels[preview.maturity]||preview.maturity||preview.mode)}</p></div><a class="action-button" href="${h(preview.url)}" target="_blank" rel="noopener">查看预览</a></header></section>`:''}
    <section class="panel execution-panel" style="margin-top:18px"><header class="panel-head audit-panel-head"><div><span class="section-kicker">EXECUTION OBSERVATORY</span><h2>执行过程、Gate 与逻辑审查</h2><p>Gate 判断流程能否推进；只读逻辑审查独立复查行动、前提与结论；目标偏离审计才拥有修复入口。</p></div><div class="audit-action-group"><button class="action-button secondary" id="logic-audit">运行只读逻辑审查</button><button class="action-button secondary danger-action" id="goal-audit">目标偏离审计与修复</button></div></header><div class="panel-body">${executionObservatory(data.execution_trace,data.goal_alignment,data.logic_audit)}</div></section>
    <div class="split raw-audit-grid" style="margin-top:18px"><section class="panel"><header class="panel-head"><div><h2>原始产物账本</h2><p>供工程审计使用的不可变 Hash；阶段结论已在上方中文解释。</p></div></header><div class="panel-body">${artifactMini(data.artifacts)}</div></section><section class="panel"><header class="panel-head"><div><h2>持久化 Gate 与异常表</h2><p>兼容旧记录；阶段产物内的 Gate 事实已归入阶段审计。</p></div></header><div class="panel-body">${gateMini(data.gates,data.exceptions)}</div></section></div>
    <section class="panel raw-event-panel" style="margin-top:18px"><header class="panel-head"><div><h2>原始审计事件</h2><p>中文摘要用于阅读，原始 Event Type 和 Payload 仍完整保留在事件流中。</p></div><a class="action-button secondary" href="#/events">打开完整事件流</a></header><div class="panel-body">${eventList(data.events,100)}</div></section>
  </div>`;
  app.querySelector('#materialize')?.addEventListener('click', async e=>{e.currentTarget.disabled=true;try{await api(`/api/jobs/${encodeURIComponent(jobId)}/materialize`,{method:'POST',body:'{}'});toast('Workflow 已物化');await jobDetail(jobId);}catch(err){toast(err.message,true);e.currentTarget.disabled=false;}});
  app.querySelector('#run-worker')?.addEventListener('click', async e=>{e.currentTarget.disabled=true;e.currentTarget.textContent='正在启动…';try{const result=await api(`/api/jobs/${encodeURIComponent(jobId)}/worker`,{method:'POST',body:'{}'});toast(result.status==='already_running'?'后台 Worker 已在运行':'后台 Worker 已启动');setTimeout(()=>jobDetail(jobId),800);}catch(err){toast(err.message,true);e.currentTarget.disabled=false;e.currentTarget.textContent='执行下一步';}});
  app.querySelector('#pause-job')?.addEventListener('click',async e=>{if(!confirm('确认暂停该 Job？当前正在执行的单步会安全结束，但不会领取下一步。'))return;e.currentTarget.disabled=true;try{await api(`/api/jobs/${encodeURIComponent(jobId)}/pause`,{method:'POST',body:JSON.stringify({confirm:true})});toast('任务已请求暂停');await jobDetail(jobId);}catch(err){toast(err.message,true);e.currentTarget.disabled=false;}});
  app.querySelector('#resume-job')?.addEventListener('click',async e=>{e.currentTarget.disabled=true;try{await api(`/api/jobs/${encodeURIComponent(jobId)}/resume`,{method:'POST',body:JSON.stringify({confirm:true})});toast('任务已恢复');await jobDetail(jobId);}catch(err){toast(err.message,true);e.currentTarget.disabled=false;}});
  app.querySelector('#logic-audit')?.addEventListener('click',async e=>{e.currentTarget.disabled=true;try{const result=await api(`/api/jobs/${encodeURIComponent(jobId)}/logic-audit`,{method:'POST',body:'{}'});toast(result.dispatched?.length?`已异步启动 ${result.dispatched.length} 个只读逻辑审查`:'当前阶段快照已审查或正在审查');await jobDetail(jobId);}catch(err){toast(err.message,true);e.currentTarget.disabled=false;}});
  app.querySelector('#goal-audit')?.addEventListener('click',async e=>{e.currentTarget.disabled=true;try{const result=await api(`/api/jobs/${encodeURIComponent(jobId)}/goal-audit`,{method:'POST',body:JSON.stringify({auto_repair:true})});toast(`目标审计完成：${result.deviations.length} 个偏离，${result.actions.length} 个动作`);await jobDetail(jobId);}catch(err){toast(err.message,true);e.currentTarget.disabled=false;}});
  bindExecutionTrace(jobId,prior.tracePane||'stages'); bindArtifactLinks(); bindJsonLinks(); bindRecover();
  app.querySelector('[data-open-repairs]')?.addEventListener('click',()=>app.querySelector('[data-trace-tab="repairs"]')?.click());
  jobViewState.set(jobId,{...(jobViewState.get(jobId)||prior),renderVersion});
  scheduleJobDetailRefresh(jobId,data);
  if(background)requestAnimationFrame(()=>window.scrollTo({top:previousScroll,behavior:'auto'}));
}

function scheduleJobDetailRefresh(jobId,data){
  clearTimeout(refreshTimer);
  if(!autoRefresh)return;
  const repairActive=data.execution_trace?.repair_activity?.active===true;
  const jobActive=['planned','ready','leased','running','stalled','retryable','repairable','manual_review','blocked_budget','candidate','gated','applied'].includes(data.job?.status);
  if(repairActive||jobActive)refreshTimer=setTimeout(()=>jobDetail(jobId,{background:true}).catch(failure),7000);
}

function taskCards(tasks,run) {
  return `<div class="task-grid">${tasks.map((task,i)=>`<article class="task-card ${h(task.status)}"><span class="task-number">${String(i+1).padStart(2,'0')} / ${String(tasks.length).padStart(2,'0')}</span><h4>${h(task.task_id)}</h4><p>${h(task.capability_id)}<br>依赖：${h((task.dependencies||[]).join(', ')||'无')}<br>尝试：${task.attempt}</p><footer>${badge(task.status)}${task.status==='repairable'?`<button class="action-button secondary" data-recover="${h(run.run_id)}|${h(task.task_id)}">修复重试</button>`:task.output_hash?`<button class="quiet-button" data-artifact="${h(task.output_hash)}">${h(short(task.output_hash))}</button>`:''}</footer></article>`).join('')}</div>`;
}
function artifactMini(items) { return !items?.length?empty('尚无关联产物'):`<div class="stack">${items.map(a=>`<button class="action-button secondary clip" data-artifact="${h(a.digest)}">${h(a.metadata?.schema||a.media_type)} · ${h(short(a.digest,18))}</button>`).join('')}</div>`; }
function gateMini(gates,exceptions) {
  if (!gates?.length&&!exceptions?.length) return empty('暂无 Gate 或异常');
  return `<div class="stack">${(gates||[]).map(g=>`<div><span class="section-kicker">${h(g.gate_name)}</span><p>${badge(g.verdict)} <span class="mono">${h(short(g.evidence_digest,16))}</span></p></div>`).join('')}${(exceptions||[]).map(x=>`<div><span class="section-kicker">${h(x.error_code)}</span><p>${badge(x.status)} ${h(JSON.stringify(x.payload||{}))}</p></div>`).join('')}</div>`;
}

const dimensionNames={claim_provenance_coverage:'引用溯源',data_readiness:'数据就绪',editorial_coherence:'编辑一致性',gap_provenance:'缺口溯源',identity_fidelity:'节点身份',reader_utility:'阅读价值',semantic_closure:'语义闭合',source_role_coverage:'来源角色',table_contract_validity:'表格契约'};
const stageNames={plan:'任务规划',prepare:'工作区准备',research_plan:'研究计划',research_plan_gate:'研究计划门禁',research_ready:'检索候选生成',search_execution_gate:'检索执行门禁',verify:'证据核验',terminology_verify:'术语核验',source_diversity_gate:'来源多样性门禁',freeze:'证据冻结',content_blueprint:'内容蓝图',content_compose:'内容生成',content_closure_gate:'内容闭合门禁',editorial_review:'编辑审校',draft_content_gate:'草稿内容门禁',draft_apply:'草稿应用',table_collect:'表格数据检索',table_search_execution_gate:'表格检索门禁',table_verify:'表格证据核验',table_population_gate:'表格填充门禁',table_apply:'表格应用',maturity_gate:'成熟度门禁',preview:'生成预览',release_gate:'发布门禁',reviewed_apply:'审核应用',publish:'正式发布'};
const actionNames={triage:'故障诊断',repair_plan:'修复计划',system_change:'系统变更候选',code_repair:'受控代码修复'};
const repairStatusNames={queued:'等待修复执行',coding:'正在修改代码',validating:'正在执行测试与 Canary',awaiting_scm_publication:'等待提交 PR',awaiting_approval:'等待授权应用',promoted:'修复已应用',awaiting_outcome_validation:'正在正式运行验证',effective:'修复有效',partially_effective:'部分有效',ineffective:'修复无效',failed:'修复失败',rejected:'修复已拒绝',rolled_back:'修复已回滚'};
const repairCauseNames={
  DIVERSITY_REPAIR_REUSES_FAILED_BINDINGS:'新候选没有进入实际证据绑定',
  DIVERSITY_REPAIR_SCOUT_NOT_PROPAGATED_TO_EVIDENCE_PIPELINE:'修复检索发现的新候选没有传入证据流水线',
  PROTECTED_BINDING_REFRESH_TOCTOU_FALSE_ATTRIBUTION:'并发刷新被错误归因成 Agent 越权修改',
  CRITICAL_QUESTION_CLOSURE_BINDS_NON_EXTERNAL_CLAIMS:'内部建模判断被错误要求使用外部文献证明',
  REPAIR_PLAN_PROJECTION_LOSS:'修复计划在投影过程中丢失',
};
const repairCauseDescriptions={
  DIVERSITY_REPAIR_REUSES_FAILED_BINDINGS:'系统已经发现新的候选来源，但后续提名与证据队列仍复用了此前失败的来源绑定，因此重新执行也无法产生新的确认性证据。',
  DIVERSITY_REPAIR_SCOUT_NOT_PROPAGATED_TO_EVIDENCE_PIPELINE:'修复检索已经发现新的候选来源，但后续搜索执行、候选绑定和证据队列没有消费这批结果，导致正式核验仍然重复使用旧输入。',
  PROTECTED_BINDING_REFRESH_TOCTOU_FALSE_ATTRIBUTION:'受保护文件在 Agent 执行期间被其他进程刷新，旧的前后快照检查无法识别真实写入者，因而把并发变化错误归因给当前 Agent。',
  CRITICAL_QUESTION_CLOSURE_BINDS_NON_EXTERNAL_CLAIMS:'研究契约把只能由内部建模过程产生的判断绑定到了外部来源 Gate，形成无法满足的前置条件。',
};
const verdictNames={CONFIRMED:'已采纳',INSUFFICIENT:'证据不足',NOT_FOUND:'未找到',NOT_REVIEWED:'未审核',accepted:'已采纳',rejected:'已拒绝',candidate:'候选',confirmed_citation:'已选引用',sent_to_verification:'送交核验'};
const outcomeNames={accepted:'已采用',rejected:'内容未通过',technical_failure:'技术失败 · 未评估',pending:'待评估'};
const reasonNames={field_specific_observation:'抽取到该字段的专属证据并通过选择规则',payload_not_fetched:'没有成功抓取文档正文',payload_or_hash_missing:'缺少正文或内容哈希，无法进入证据评估',no_field_specific_observation:'未抽取到能够支持该字段的专属观察',no_observation:'未抽取到可用观察',document_type_not_supported:'文档类型不受当前抽取器支持',source_class_not_allowed:'来源类型不满足该字段的证据策略',node_identity_mismatch:'来源对象与当前节点身份不一致',duplicate_candidate:'与已有候选重复',lower_ranked_candidate:'相关性或证据强度低于已采用候选',excluded_by_diversity_repair:'为打破来源重复而被多样性修复明确排除',not_selected_for_fetch:'搜索已返回该结果，但候选绑定阶段未选择抓取'};
const decisionStageNames={discovery:'发现候选',fetch_or_extraction:'抓取 / 解析',evidence_selection:'字段证据选择',verification:'等待声明核验',claim_verification:'声明核验'};
const gateCheckNames={
  bilingual_tracks_declared:'已声明中英文检索轨道',canonical_chinese_present:'存在规范中文术语',english_discovery_terms_present:'存在英文辅助检索词',english_field_translation_coverage_complete:'英文字段翻译覆盖完整',english_terms_are_actually_english:'英文检索词确为英文',english_translation_audited:'英文翻译经过审核',research_questions_complete:'研究问题覆盖完整',research_question_contracts_valid:'研究问题契约结构与 Hash 有效',critical_questions_closed:'关键研究问题的全部前置要求已闭合',source_role_contract_complete:'来源角色契约完整',all_queries_reached_terminal_search_state:'所有查询均到达终态',at_least_one_query_executed:'至少执行了一条查询',has_external_queries:'包含外部数据源查询',no_planned_queries:'没有停留在仅计划状态的查询',candidate_source_roles_and_diversity:'候选来源角色及多样性满足要求',reviewed_confirmed_urls:'存在审核确认的来源地址',reviewed_distinct_domains:'确认来源域名数量满足要求',reviewed_language_tracks:'确认来源覆盖所需语言轨道',reviewed_technical_sources:'存在审核确认的技术来源',adjacent_distinction_source_role:'相邻流程区分来源角色完整',identity_source_role:'身份定义来源角色完整',preview_distinct_domains:'预览来源域名数量满足要求',preview_language_tracks:'预览覆盖所需语言轨道',preview_primary_sources:'预览包含一手来源',preview_technical_source:'预览包含技术来源',process_boundary_source_role:'过程边界来源角色完整'
};
const failureCodeNames={RESEARCH_PLAN_INVALID:'研究计划不满足当前契约',SOURCE_DIVERSITY_BLOCKED:'关键研究问题尚未获得充分证据'};
const gateDecisionNames={PASS:'通过',PASS_WITH_DEBT:'通过，但保留质量债',RESEARCH_MORE:'需要改变检索策略后继续研究',EVIDENCE_LIMITED:'证据受限',BLOCKED:'阻断',REPAIR:'需要修复',LIMITED:'受限继续',PENDING:'尚未判定'};
const readableCode=value=>gateCheckNames[value]||failureCodeNames[value]||String(value||'未记录').replaceAll('_',' ');
const decisionClass=value=>['CONFIRMED','accepted','selected','confirmed_citation'].includes(String(value))?'accepted':['INSUFFICIENT','NOT_FOUND','rejected'].includes(String(value))?'rejected':String(value)==='technical_failure'?'technical':'candidate';
const decisionPill=value=>`<span class="decision-pill ${decisionClass(value)}">${h(verdictNames[value]||labels[value]||value||'未分类')}</span>`;
const outcomePill=value=>`<span class="decision-pill ${decisionClass(value)}">${h(outcomeNames[value]||value||'待评估')}</span>`;
const reasonLabel=value=>{const raw=typeof value==='string'?value:(value?.message||value?.code||JSON.stringify(value));return {raw,label:reasonNames[raw]||raw};};
const sourceLink=(url,label)=>url?`<a class="source-link" href="${h(url)}" target="_blank" rel="noopener noreferrer">${h(label||url)} ↗</a>`:'<span class="muted">无可打开地址</span>';

function repairActivityBanner(activity){
  const item=activity?.latest;
  if(!item)return '';
  const cause=item.cause_code||'UNCLASSIFIED_REPAIR';
  const statusLabel=repairStatusNames[item.status]||labels[item.status]||item.status||'状态未知';
  const summaryZh=repairCauseDescriptions[cause]||item.summary||item.last_error||'系统已建立修复任务，等待进一步诊断信息。';
  const scm=item.scm||{};
  const links=[
    scm.issue_url?`<a class="repair-scm-link" href="${h(scm.issue_url)}" target="_blank" rel="noopener noreferrer">Issue #${h(scm.issue_number||'—')} ↗</a>`:'',
    scm.pr_url?`<a class="repair-scm-link primary" href="${h(scm.pr_url)}" target="_blank" rel="noopener noreferrer">PR #${h(scm.pr_number||'—')} ↗</a>`:'',
  ].filter(Boolean).join('');
  const steps=(item.steps||[]).map((step,index)=>`<li class="${h(step.state||'pending')}"><i>${step.state==='done'?'✓':step.state==='failed'?'×':String(index+1).padStart(2,'0')}</i><span>${h(step.label)}</span><small>${step.state==='done'?'已完成':step.state==='active'?'进行中':step.state==='failed'?'失败':'等待'}</small></li>`).join('');
  return `<section class="repair-activity ${activity.active?'active':'terminal'}" aria-label="当前阻断与自治修复">
    <header><div><span class="section-kicker">LIVE BLOCKER · AUTONOMOUS REPAIR</span><h2>当前阻断与自治修复</h2></div><div class="repair-live-state"><i></i><span>${h(statusLabel)}</span>${badge(item.status)}</div></header>
    <div class="repair-activity-main"><div class="repair-cause"><span>系统发现的问题</span><h3>${h(repairCauseNames[cause]||readableCode(cause))}</h3><code>${h(cause)}</code><p>${h(summaryZh)}</p><div class="repair-context"><span>失败阶段 <b>${h(stageNames[item.failed_task]||item.failed_task||'未记录')}</b></span><span>计划回卷 <b>${h(stageNames[item.recovery_task]||item.recovery_task||'待确定')}</b></span><span>修复尝试 <b>${h(item.attempt||1)}</b></span></div></div><aside><span>修复记录</span><code>${h(item.repair_run_id||'—')}</code><small>最后进展 ${relativeTime(item.updated_at)}</small><small>${fmtDate(item.updated_at)}</small><div class="repair-links">${links||'<span>尚未形成 Issue / PR</span>'}</div><button class="quiet-button" type="button" data-open-repairs>查看完整诊断与修复记录 →</button></aside></div>
    <ol class="repair-progress">${steps}</ol>
    ${item.last_error?`<div class="repair-error"><span>当前失败原因</span><strong>${h(item.last_error)}</strong></div>`:''}
  </section>`;
}

function executionObservatory(trace,value,logicAudit) {
  const hasTrace=trace&&trace.schema_version, quality=trace?.quality||value?.quality_observations?.[0]?.payload||{}, summary=trace?.summary||{};
  const logicSummary=logicAudit?.summary||{}, logicFindings=logicAudit?.findings||[];
  if(!hasTrace&&!quality?.score&&!logicSummary.runs)return empty('等待首次执行审计','任务开始运行后，这里会展示阶段、检索、证据选择、逻辑观察和修复动作。');
  const dims=quality.dimensions||{}, stages=trace?.stages||[], searches=trace?.searches||[], citations=trace?.citations||[], fields=trace?.table_fields||[], issues=trace?.issues||[], actions=trace?.actions||[], researchGovernance=trace?.research_question_governance||{};
  const providers=[...new Set((trace?.providers||[]).filter(Boolean))], defaultPane='stages';
  const bottleneck=summary.table_fields&&summary.populated_fields===0
    ?`表格字段 0/${summary.table_fields} 已填充：候选来源存在，但没有证据通过选择规则。`
    :summary.confirmed_citations===0&&citations.length?`已核验 ${citations.length} 条声明，但尚无文献被确认采纳。`
    :summary.open_issues?`仍有 ${summary.open_issues} 组未关闭问题，需要继续修复或人工判定。`
    :'当前未检测到阻断性质量偏离。';
  return `<div class="trace-shell">
    <nav class="trace-tabs" aria-label="执行审计视图">
      <button class="trace-tab active" type="button" data-trace-tab="stages" aria-selected="true">阶段执行审计 <b>${stages.length}</b></button>
      <button class="trace-tab" type="button" data-trace-tab="questions" aria-selected="false">研究问题闭合 <b>${researchGovernance?.questions?.length||0}</b></button>
      <button class="trace-tab" type="button" data-trace-tab="searches" aria-selected="false">查询与数据源 <b>${searches.length}</b></button>
      <button class="trace-tab" type="button" data-trace-tab="evidence" aria-selected="false">引用与表格 <b>${citations.length+fields.length}</b></button>
      <button class="trace-tab" type="button" data-trace-tab="logic" aria-selected="false">只读逻辑审查 <b>${logicFindings.length}</b></button>
      <button class="trace-tab" type="button" data-trace-tab="repairs" aria-selected="false">问题与修复 <b>${issues.length+actions.length}</b></button>
      <button class="trace-tab" type="button" data-trace-tab="summary" aria-selected="false">目标与质量总览</button>
    </nav>
    <section class="trace-pane active" data-trace-pane="stages">${stageAuditLedger(stages,trace?.job_id)}</section>
    <section class="trace-pane" data-trace-pane="questions">${researchGovernanceBoard(researchGovernance)}</section>
    <section class="trace-pane" data-trace-pane="searches">${searchExplorer(searches,providers,citations)}</section>
    <section class="trace-pane" data-trace-pane="evidence">${evidenceLedger(citations,fields)}</section>
    <section class="trace-pane" data-trace-pane="logic">${logicAuditLedger(logicAudit)}</section>
    <section class="trace-pane" data-trace-pane="repairs">${repairLedger(issues,actions)}</section>
    <section class="trace-pane" data-trace-pane="summary">${traceSummary(summary,bottleneck,dims,stages,trace)}</section>
  </div>`;
}

const questionStatusNames={confirmed:'已闭合',partially_supported:'部分支撑',unresolved:'未解决',contradicted:'存在矛盾',explicit_gap:'明确证据缺口',planned:'已计划 · 待核验'};
const criticalityNames={required_for_model:'建模必需',recommended:'质量建议',contextual:'情境问题',legacy:'旧版问题'};

function researchGovernanceBoard(governance){
  const artifacts=governance?.artifacts||{};
  const artifactButton=(label,item)=>item?.digest?`<button class="quiet-button json-open-button" data-json-artifact="${h(item.digest)}">${h(label)} · ${h(String(item.path||'JSON').split('/').pop())}</button>`:'';
  if(!governance?.available){
    const legacy=governance?.legacy_questions||[];
    const message=governance?.reason==='legacy_research_plan_without_question_contract'
      ?'这个 Job 使用旧版研究计划，只保存了维度关键词，没有生成受 Hash 保护的逐问题契约。Dashboard 不会事后猜测其语义；从 research_plan 受控回卷后才会产生可审计的 v2 闭合链。'
      :'研究计划尚未生成逐问题契约，当前没有可以投影的闭合链。';
    return `<div class="research-governance-empty"><span class="section-kicker">QUESTION CONTRACT · UNAVAILABLE</span><h3>逐问题闭合视图尚不可用</h3><p>${h(message)}</p>${legacy.length?`<div class="legacy-question-list">${legacy.map(item=>`<code>${h(item)}</code>`).join('')}</div>`:''}<div class="research-artifact-actions">${artifactButton('查看原始研究计划',artifacts.plan)}</div></div>`;
  }
  const metrics=governance.metrics||{}, gate=governance.gate||{}, questions=governance.questions||[];
  const integrity=governance.contract_integrity===true?'契约 Hash 与证据账本一致':governance.contract_integrity===false?'契约 Hash 不一致':'证据 Gate 尚未写入契约 Hash';
  const integrityClass=governance.contract_integrity===false?'failed':governance.contract_integrity===true?'passed':'pending';
  const decisionZh=gateDecisionNames[gate.decision]||gate.decision||'等待证据 Gate';
  return `<div class="research-governance-board">
    <header class="research-governance-head"><div><span class="section-kicker">CONTRACT → QUERY → EVIDENCE → GATE</span><h3>研究问题逐项闭合图</h3><p>以 <code>question_id</code> 为主键，把稳定问题、绑定 requirement、实际查询、逐条证据和 Gate 结论放在同一条因果链上。</p></div><div class="research-artifact-actions">${artifactButton('研究计划契约',artifacts.plan)}${artifactButton('问题证据 Gate',artifacts.gate)}</div></header>
    <div class="research-governance-summary"><article><span>必需问题闭合</span><strong>${Number(metrics.required_questions_confirmed||0)} / ${Number(metrics.required_questions_total||0)}</strong><small>只有必需问题的全部绑定 requirement 被确认，才满足闭合规则。</small></article><article><span>已执行查询</span><strong>${Number(metrics.questions_with_queries||0)} / ${Number(metrics.questions_total||0)}</strong><small>按问题统计是否留下真实查询记录。</small></article><article><span>已有证据</span><strong>${Number(metrics.questions_with_evidence||0)} / ${Number(metrics.questions_total||0)}</strong><small>证据存在不等于已确认，仍需看 verdict。</small></article><article class="${integrityClass}"><span>契约完整性</span><strong>${h(integrity)}</strong><small><code>${h(short(governance.contract_sha256||'—',20))}</code></small></article></div>
    <div class="research-gate-ribbon ${gate.passed?'passed':gate.decision?'blocked':'pending'}"><div><span>当前问题证据 Gate</span><strong>${h(decisionZh)}</strong><small>${h(gate.reason_zh||'尚未形成 Gate 结论。')}</small></div><div><span>流程效果</span><strong>${gate.pipeline_continue===true?'允许继续':gate.pipeline_continue===false?'暂不允许继续':'等待判定'}</strong><small>成熟度上限：${h(gate.maturity_ceiling||'尚未设置')}</small></div></div>
    <div class="research-question-list">${questions.map(researchQuestionCard).join('')}</div>
    ${governance.unmapped_claims?.length?`<details class="unmapped-claims"><summary>${governance.unmapped_claims.length} 条声明无法映射到问题契约</summary>${jsonView(governance.unmapped_claims)}</details>`:''}
  </div>`;
}

function researchQuestionCard(item,index){
  const execution=item.execution||{}, status=item.status||'planned', required=item.required_for_model===true;
  const requirements=item.requirement_ids||[], confirmed=new Set(item.confirmed_requirement_ids||[]), missing=new Set(item.missing_requirement_ids||[]);
  const intents=item.query_intents||[], queries=execution.queries||[], evidence=item.evidence||[];
  const requirementRows=requirements.length?requirements.map(value=>`<li class="${confirmed.has(value)?'confirmed':missing.has(value)?'missing':'pending'}"><i>${confirmed.has(value)?'✓':missing.has(value)?'×':'·'}</i><code>${h(value)}</code><span>${confirmed.has(value)?'已有确认性证据':missing.has(value)?'仍缺确认性证据':'等待证据 Gate 核验'}</span></li>`).join(''):'<li class="not-bound"><i>—</i><span>当前问题没有绑定 Workflow requirement，不参与建模必需闭合。</span></li>';
  const intentRows=intents.length?intents.map(intent=>`<article><div><code>${h(intent.intent_id||'未命名意图')}</code><span>优先级 ${h(intent.priority??'—')}</span></div><strong>${h(intent.purpose||'未记录目的')}</strong><small>中文种子：${h((intent.seed_terms?.zh||[]).join(' · ')||'—')}</small><small>英文种子：${h((intent.seed_terms?.en||[]).join(' · ')||'—')}</small></article>`).join(''):empty('没有查询意图','契约未声明该问题的检索策略。');
  const queryRows=queries.length?queries.map(query=>`<article><div><span>${h(query.language||'未标语言')}</span><code>${h(query.intent_id||'未记录 intent')}</code></div><strong>${h(query.query||'未记录关键词')}</strong><small>${(query.providers||[]).map(provider=>h(provider.provider||provider)).join(' · ')||'未记录 Provider'} · ${Number(query.candidate_count||0)} 个候选</small></article>`).join(''):empty('尚无实际查询','问题已经规划，但还没有留下与 question_id 绑定的查询记录。');
  const evidenceRows=evidence.length?evidence.map(row=>`<article class="${decisionClass(row.verdict)}"><header><code>${h(row.requirement_id||'未绑定 requirement')}</code>${decisionPill(row.verdict)}</header><strong>${h(row.claim_id||'未记录 claim')}</strong><small>支持类型：${h(row.support_type||'未记录')} · 声明类型：${h(row.claim_kind||'未记录')}</small>${row.url?sourceLink(row.url,row.url):'<span class="muted">未记录证据 URL</span>'}</article>`).join(''):empty('尚无逐条证据','没有 claim 被映射并写入该问题的证据账本。');
  return `<details class="research-question-card ${h(status)}" ${required&&status!=='confirmed'?'open':''}><summary><span class="research-question-number">Q${String(index+1).padStart(2,'0')}</span><div><span class="section-kicker">${h(item.dimension||'未分类')} · ${h(criticalityNames[item.criticality]||item.criticality||'未分类')}</span><strong>${h(item.question?.zh||item.question_id)}</strong><code>${h(item.question_id)}</code></div><div class="question-closure-count"><b>${(item.confirmed_requirement_ids||[]).length}/${requirements.length}</b><span>requirement</span></div><span class="question-status ${h(status)}">${h(questionStatusNames[status]||status)}</span></summary><div class="research-question-body"><div class="question-conclusion"><span>${required?'阻断性问题':'非阻断质量问题'}</span><strong>${h(item.conclusion_zh||'尚无结论')}</strong><small>闭合规则：${h(item.closure_rule||'未记录')}</small></div><div class="question-contract-grid"><section><header><span class="section-kicker">BOUND REQUIREMENTS</span><h5>必须证明什么</h5></header><ul class="requirement-proof-list">${requirementRows}</ul></section><section><header><span class="section-kicker">SOURCE CONTRACT</span><h5>允许用什么来源证明</h5></header><p>来源角色：${h((item.source_role_requirements||[]).join(' · ')||'未限定')}</p><p>优先来源类型：${h((item.preferred_source_classes||[]).join(' · ')||'未限定')}</p></section></div><div class="question-flow-grid"><section><header><span class="section-kicker">PLANNED INTENTS</span><h5>计划怎么查</h5></header><div class="question-intent-list">${intentRows}</div></section><section><header><span class="section-kicker">EXECUTED QUERIES</span><h5>实际查了什么</h5></header><div class="question-query-list">${queryRows}</div></section></div><section class="question-evidence-section"><header><div><span class="section-kicker">VERIFIED EVIDENCE</span><h5>哪些证据被接受、拒绝或判为不足</h5></div><div><b>${Number(execution.accepted_count||0)}</b> 采用 · <b>${Number(execution.rejected_count||0)}</b> 拒绝/失败 · <b>${Number(execution.verified_claim_count||0)}</b> 已核验声明</div></header><div class="question-evidence-list">${evidenceRows}</div></section></div></details>`;
}

const logicAssessmentNames={coherent:'逻辑关系基本连贯',questionable:'存在待澄清问题',insufficient:'前置条件不足',contradictory:'存在逻辑矛盾',not_assessable:'现有记录不足以审查'};
const logicFindingNames={conclusion_without_premises:'结论缺少前提',precondition_unproven:'前置条件未证明',scope_overreach:'结论超出证据范围',quantifier_escalation:'量词被不当放大',identity_join_incomplete:'对象身份连接不完整',non_sequitur_transition:'阶段流转缺少逻辑承接',contradictory_premises:'前提互相矛盾',circular_justification:'循环论证',heuristic_presented_as_fact:'启发式判断被当作事实',unresolved_presented_as_pass:'未解决事项被表述为通过',implicit_question_decomposition:'研究问题拆解未显式固化',plan_execution_coverage_gap:'计划与执行覆盖不一致',decision_reason_missing:'结论缺少理由',concept_drift:'概念发生漂移',alternative_unexamined:'替代解释未被审查',insufficient_observability:'缺少可审查记录',other:'其他逻辑观察'};

function logicAuditLedger(audit){
  const summary=audit?.summary||{}, runs=audit?.runs||[], findings=audit?.findings||[];
  const runById=new Map(runs.map(item=>[item.audit_run_id,item]));
  return `<div class="logic-authority-banner"><div><span class="section-kicker">READ-ONLY · NON-BLOCKING</span><strong>逻辑审查只提出观察和问题，不决定 Gate，也不会触发重试、回卷或修复。</strong><small>只有点击“提升为正式调查”并再次确认，Finding 才会进入既有偏离调查边界；提升后仍不会自动修复。</small></div><div><span><b>${Number(summary.completed||0)}</b> 已完成</span><span><b>${Number(summary.reviewing||0)+Number(summary.queued||0)}</b> 排队 / 审查中</span><span><b>${Number(summary.open_findings||0)}</b> 开放观察</span><span><b>${Number(summary.promoted_findings||0)}</b> 已人工提升</span></div></div>
    ${runs.length?`<div class="logic-run-strip">${runs.map(run=>{const result=run.result||{};return `<article class="${h(run.status)}"><div><span>${run.scope==='cross_stage'?'跨阶段复查':h(stageNames[run.stage_id]||run.stage_id)}</span>${badge(run.status)}</div><strong>${h(logicAssessmentNames[result.assessment]||result.assessment||'等待审查')}</strong><small>${h(result.summary_zh||run.last_error||'快照已固化，等待只读审查 Agent。')}</small><code>${h(short(run.subject_hash,16))}</code></article>`;}).join('')}</div>`:empty('尚未运行逻辑审查','新阶段到达终态时会自动排队；历史 Job 可点击页面顶部按钮按需执行。')}
    <div class="logic-findings">${findings.length?findings.map(item=>logicFindingCard(item,runById.get(item.audit_run_id))).join(''):empty('没有逻辑观察','这不等于 Gate PASS，只表示当前审查快照没有提出额外问题。')}</div>`;
}

function logicFindingCard(item,run){
  const refs=(label,values)=>values?.length?`<div><span>${label}</span>${values.map(value=>`<code>${h(value)}</code>`).join('')}</div>`:'';
  const hasRefs=[item.premise_refs,item.conclusion_refs,item.artifact_refs].some(values=>values?.length);
  return `<article class="logic-finding severity-${h(item.severity||'info')}"><header><div><span class="section-kicker">${h(logicFindingNames[item.finding_type]||item.finding_type)} · ${h(item.severity||'info')} · ${h(item.source||'unknown')}</span><h4>${h(item.title_zh||'未命名逻辑观察')}</h4><small>${h(run?.scope==='cross_stage'?'跨阶段复查':stageNames[item.stage_id]||item.stage_id||'未知阶段')} · 置信度 ${Math.round(Number(item.confidence||0)*100)}% · ${fmtDate(item.created_at)}</small></div>${item.status==='promoted'?badge('promoted'):badge('open')}</header><div class="logic-observation"><span>观察</span><p>${h(item.observation_zh||'未记录')}</p></div><div class="logic-question"><span>待调查问题</span><p>${h(item.question_zh||'未记录')}</p></div>${hasRefs?`<details class="logic-references"><summary>查看前提、结论与 Artifact 引用</summary>${refs('前提引用',item.premise_refs)}${refs('结论引用',item.conclusion_refs)}${refs('Artifact 引用',item.artifact_refs)}</details>`:''}<footer><small>PIPELINE EFFECT · NONE</small>${item.status==='promoted'?`<span>已提升为偏离调查 <code>${h(item.promoted_deviation_id||'—')}</code></span>`:`<button class="quiet-button" data-promote-logic="${h(item.finding_id)}">提升为正式调查</button>`}</footer></article>`;
}

function stageAuditLedger(stages,jobId){
  if(!stages.length)return empty('尚无执行阶段','Workflow 物化后，每个阶段的动作、结论和 Gate 依据会显示在这里。');
  const failed=stages.filter(v=>['failed','repairable','retryable','manual_review','quarantined','blocked','blocked_budget'].includes(v.status)).length;
  const pending=stages.filter(v=>['pending','planned','ready'].includes(v.status)).length;
  const gates=stages.filter(v=>v.gate).length;
  return `<div class="stage-audit-intro"><div><span class="section-kicker">CAUSAL STAGE LEDGER</span><strong>每个阶段都回答：谁做了什么、得出什么结论、为什么能够继续。</strong><small>默认展开失败或隔离阶段；成功阶段可逐项查看 Gate 规则和不可变产物。</small></div><div><span><b>${stages.length}</b> 全部阶段</span><span class="danger"><b>${failed}</b> 失败 / 隔离</span><span><b>${pending}</b> 尚未执行</span><span><b>${gates}</b> Gate</span></div></div>
    <div class="stage-audit-toolbar"><label><span>阶段状态</span><select class="field" id="stage-status-filter"><option value="">全部状态</option><option value="attention">失败 / 隔离 / 待审核</option><option value="succeeded">成功</option><option value="pending">尚未执行</option></select></label><label><span>阶段类型</span><select class="field" id="stage-kind-filter"><option value="">全部类型</option><option value="gate">Gate</option><option value="agent">Agent</option><option value="executor">确定性执行器</option></select></label><div class="query-count"><strong id="stage-visible-count">${stages.length}</strong><span>个阶段可见</span></div></div>
    <div class="stage-audit-ledger">${stages.map(stage=>stageAuditCard(stage,jobId)).join('')}</div>`;
}

function stageAuditCard(stage,jobId){
  const attention=['failed','repairable','retryable','manual_review','quarantined','blocked','blocked_budget'].includes(stage.status), pending=['pending','planned','ready'].includes(stage.status);
  const kind=stage.gate?'gate':String(stage.capability_id||'').startsWith('agent.')?'agent':'executor';
  const output=stage.output||{}, documents=output.documents||[], attempts=stage.attempts||[], actor=stage.agent||{}, conclusion=stage.conclusion||{}, transition=stage.transition||{};
  const open=attention?'open':'';
  return `<details class="stage-audit-card ${h(stage.status)}" data-stage-audit data-stage-status="${attention?'attention':pending?'pending':'succeeded'}" data-stage-kind="${h(kind)}" ${open}><summary><span class="stage-audit-index">${String(stage.ordinal).padStart(2,'0')}</span><div class="stage-audit-title"><div><strong>${h(stage.name_zh||stageNames[stage.task_id]||stage.task_id)}</strong>${stage.gate?'<span class="stage-type gate">GATE</span>':`<span class="stage-type">${kind==='agent'?'AGENT':'EXECUTOR'}</span>`}</div><small>${h(stage.task_id)} · ${h(actor.logical_actor_zh||stage.capability_id||'—')}</small></div><div class="stage-audit-meta"><span>${stage.attempt_count||0} 次尝试</span><span>${(actor.worker_ids||[]).length} 个 Worker</span></div>${badge(stage.status)}</summary>
    <div class="stage-audit-body"><div class="stage-explanation-grid"><article><span>本阶段目标</span><strong>${h(stage.purpose_zh||'未记录阶段目标')}</strong></article><article><span>Agent / 执行器做了什么</span><strong>${h(stage.action_zh||'未记录执行动作')}</strong></article><article class="${attention?'danger':''}"><span>阶段结论</span><strong>${h(conclusion.summary_zh||'尚无结论')}</strong>${conclusion.raw_reason?`<small>原始依据：${h(conclusion.raw_reason)}</small>`:''}</article><article class="${transition.allowed?'success':attention?'danger':''}"><span>为什么${transition.allowed?'能够继续':'没有进入下一环节'}</span><strong>${h(transition.reason_zh||'未记录流转理由')}</strong></article></div>
      <div class="stage-identity-strip"><span><b>执行主体</b>${h(actor.logical_actor_zh||'—')}</span><span><b>能力</b>${h(actor.capability_id||'—')}</span><span><b>运行配置</b>${h(actor.runtime_profile||'默认配置')}</span><span><b>Worker</b>${h((actor.worker_ids||[]).join(' · ')||'尚未领取')}</span><span><b>时间</b>${fmtDate(stage.started_at||stage.updated_at)} → ${fmtDate(stage.finished_at)}</span></div>
      ${stage.gate?gateAudit(stage.gate,documents,output.protocol==='task-output-manifest-v1'?output.digest:null):''}
      <div class="stage-detail-columns"><section><header><span class="section-kicker">ATTEMPT EVENTS</span><h4>每次尝试发生了什么</h4></header>${attempts.length?`<div class="attempt-ledger">${attempts.map(attemptCard).join('')}</div>`:empty('尚无执行尝试','该阶段尚未被 Worker 领取。')}</section><section><header><span class="section-kicker">IMMUTABLE OUTPUT</span><h4>阶段产生了什么</h4></header>${outputAudit(output,conclusion,stage,jobId)}</section></div>
    </div></details>`;
}

function gateAudit(gate,documents,digest){
  const failed=(gate.checks||[]).filter(v=>!v.passed), passed=(gate.checks||[]).filter(v=>v.passed);
  const source=gate.evidence_source==='hash_verified_attempt_snapshot'?'尝试归档快照 · 文件 Hash 已核验':'Task 不可变输出清单';
  const decisionZh=gateDecisionNames[gate.decision]||gate.decision||'未判定';
  return `<section class="gate-audit ${gate.passed?'passed':'blocked'}"><header><div><span class="section-kicker">GATE DECISION</span><h4>${gate.passed?'Gate 已放行':'Gate 未放行'} · ${h(decisionZh)} <code>${h(gate.decision||'PENDING')}</code></h4><p>${h(gate.reason_zh||'未记录 Gate 理由')}</p><small class="gate-evidence-source">判定证据：${h(source)}</small></div>${gate.passed?badge('pass'):badge('fail')}</header><div class="gate-proof-summary"><span><b>${passed.length}</b> 检查通过</span><span><b>${failed.filter(v=>v.blocking).length}</b> 阻断项未通过</span><span><b>${failed.filter(v=>v.advisory).length}</b> 建议项未通过</span><span><b>${h(gate.maturity_ceiling||'未限制')}</b> 成熟度上限</span></div><div class="gate-check-grid">${(gate.checks||[]).length?(gate.checks||[]).map(check=>`<article class="${check.passed?'passed':check.advisory?'advisory':'failed'}"><i>${check.passed?'✓':check.advisory?'!':'×'}</i><div><strong>${h(readableCode(check.name))}</strong><code>${h(check.name)}</code><small>${check.advisory?'质量建议：失败会形成质量债，但不单独阻断流程':'阻断项：必须满足才能进入下一环节'} · 实际值 ${h(String(check.actual))}</small></div></article>`).join(''):empty('没有逐条检查记录','当前只能依据阶段终态判断 Gate 结论。')}</div>${questionLedgerAudit(gate.question_evidence_ledger)}${gate.failed_requirement_ids?.length?`<div class="gate-missing-requirements"><span>当前未闭合的关键问题</span>${gate.failed_requirement_ids.map(item=>`<code>${h(item)}</code>`).join('')}</div>`:''}${digest?`<button class="quiet-button gate-artifact-link" data-artifact="${h(digest)}">查看 Gate 不可变产物 ${h(short(digest,18))}</button>`:''}</section>`;
}

function questionLedgerAudit(ledger){
  const questions=ledger?.questions||[];
  if(!questions.length)return '';
  const statusNames={confirmed:'已闭合',partially_supported:'部分支撑',unresolved:'未解决',contradicted:'存在矛盾',explicit_gap:'已声明证据缺口'};
  return `<div class="question-ledger"><header><span class="section-kicker">QUESTION EVIDENCE LEDGER</span><h5>逐问题证据闭合</h5><small>${ledger.critical_questions_closed?'关键问题已全部闭合':'仍有关键问题缺少前置证据'}</small></header><div>${questions.map(item=>`<article class="${h(item.status||'unresolved')}"><div><strong>${h(item.question?.zh||item.question_id)}</strong><code>${h(item.question_id)}</code></div><span>${h(statusNames[item.status]||item.status||'未判定')}</span><small>绑定 ${item.bound_requirement_ids?.length||0} 项 · 已确认 ${item.confirmed_requirement_ids?.length||0} 项 · 证据 ${Number(item.evidence_count||0)} 条</small>${item.missing_requirement_ids?.length?`<p>缺失：${item.missing_requirement_ids.map(value=>`<code>${h(value)}</code>`).join('')}</p>`:''}</article>`).join('')}</div></div>`;
}

function researchContractAudit(documents){
  const facts=(documents||[]).map(item=>item.facts||{}).find(item=>item.research_question_contracts?.length);
  const contracts=facts?.research_question_contracts||[];
  if(!contracts.length)return '';
  return `<div class="research-contract-audit"><header><span class="section-kicker">RESEARCH QUESTION CONTRACT · ${h(facts.research_question_contract_version||'v2')}</span><h5>关键词被稳定拆解成哪些实际问题</h5><small>问题文本、绑定的内容要求、闭合规则与查询意图均已固化并受 Hash 保护。</small></header>${contracts.map(contract=>`<details><summary><strong>${h(contract.dimension)}</strong><span>${h(contract.criticality)}</span></summary><div>${(contract.subquestions||[]).map(question=>`<article><strong>${h(question.question?.zh||question.question_id)}</strong><code>${h(question.question_id)}</code><small>闭合规则：${h(question.closure_rule||'未记录')}</small><p>${question.requirement_ids?.length?'绑定要求：'+question.requirement_ids.join(' · '):'当前没有绑定的 Workflow requirement'}</p><p>查询意图：${h((question.query_intents||[]).map(intent=>typeof intent==='string'?intent:intent.intent_id).filter(Boolean).join(' · ')||'未记录')}</p></article>`).join('')}</div></details>`).join('')}</div>`;
}

function attemptCard(item){
  const failed=item.failure_code||!['succeeded','running'].includes(item.status), repair=item.repair_action;
  return `<details class="attempt-card ${failed?'failed':'succeeded'}"><summary><span>尝试 ${h(item.attempt??'—')}</span><strong>${h(item.status_zh||labels[item.status]||item.status)}</strong><small>${h(item.worker_id||'未记录 Worker')} · ${fmtDate(item.started_at)}</small></summary><div><ol class="attempt-events">${(item.events||[]).map(event=>`<li><time>${fmtDate(event.at)}</time><b>${h(event.event_zh)}</b><code>${h(event.event)}</code></li>`).join('')}</ol>${item.failure_code?`<div class="attempt-failure"><span>失败原因</span><strong>${h(readableCode(item.failure_code))}</strong><code>${h(item.failure_code)}</code>${item.failure_message?`<p>${h(item.failure_message)}</p>`:''}${repair?`<p>修复策略：${h(repair)}${item.repair_reason?` · ${h(item.repair_reason)}`:''}</p>`:''}${item.invalidates?.length?`<p>回卷失效：${h(item.invalidates.join('、'))}</p>`:''}</div>`:''}<details class="raw-attempt"><summary>查看本次输入输出 Hash</summary>${jsonView({attempt_id:item.attempt_id,input_hashes:item.input_hashes,output_hash:item.output_hash,failure_fingerprint:item.failure_fingerprint,preserves:item.preserves})}</details></div></details>`;
}

function outputAudit(output,conclusion,stage,jobId){
  if(!output?.digest)return empty('没有输出产物','尚未执行或该阶段没有形成可审计输出。');
  const docs=output.documents||[], files=output.files||[];
  const integrity=output.integrity==='verified'?'✓ Task 输出已验证':output.protocol==='failure-envelope-v1'?'✓ 失败事实已持久化':'! '+h(output.integrity||'未知');
  const diagnostic=output.diagnostic_integrity==='hash_verified_attempt_snapshot'?' · Gate 诊断快照 Hash 已核验':'';
  const rootButton=`<button class="quiet-button json-open-button" data-json-artifact="${h(output.digest)}">查看 Task JSON · ${h(short(output.digest,16))}</button>`;
  const fileList=files.length?files.map(file=>`<article><div><strong>${h(String(file.path||'未命名文件').split('/').pop())}</strong><small>${h(file.role||file.media_type||'产物')} · ${fmtBytes(Number(file.size||0))}</small></div>${file.digest?(file.media_type==='application/json'?`<button class="quiet-button json-open-button" data-json-artifact="${h(file.digest)}">查看 JSON</button>`:`<button class="quiet-button" data-artifact="${h(file.digest)}">${h(short(file.digest,15))}</button>`):''}</article>`).join(''):empty('没有清单文件');
  const snapshots=docs.filter(doc=>doc.integrity==='hash_verified_attempt_snapshot'&&doc.attempt_id&&doc.path);
  const snapshotList=snapshots.length?`<div class="output-snapshot-list">${snapshots.map(doc=>`<article><div><strong>${h(String(doc.path).split('/').pop())}</strong><small>失败尝试 ${h(doc.attempt_id)} · Hash 已核验</small></div><button class="quiet-button json-open-button" data-json-snapshot="1" data-json-job="${h(jobId)}" data-json-task="${h(stage.task_id)}" data-json-attempt="${h(doc.attempt_id)}" data-json-path="${h(doc.path)}">查看快照 JSON</button></article>`).join('')}</div>`:'';
  return `<div class="output-audit"><div class="output-integrity"><span>${integrity}${diagnostic}</span>${rootButton}</div>${conclusion.decision?`<div class="output-conclusion"><span>产物结论</span><strong>${h(conclusion.decision)}</strong>${Object.keys(conclusion.metrics||{}).length?jsonView(conclusion.metrics):''}</div>`:''}${researchContractAudit(docs)}<div class="output-file-list">${fileList}</div>${snapshotList}${docs.length?`<details class="output-facts"><summary>查看 ${docs.length} 份协议产物的结构化事实</summary>${jsonView(docs.map(v=>({path:v.path,integrity:v.integrity,source:v.source,facts:v.facts})))}</details>`:''}</div>`;
}

function traceSummary(summary,bottleneck,dims,stages,trace) {
  const goal=trace?.goal_status||{}, blockers=goal.blockers||[];
  const goalLabel=goal.goal_id==='reviewed_publication'?'正式审核发布':goal.goal_id==='workflow_delivery'?'流程交付':'LCA 建模';
  const goalProof=goal.goal_id==='reviewed_publication'?`${goal.modeling_ready?'建模已就绪':'建模未就绪'} · ${goal.publication_proof_valid?'发布证明有效':'尚无发布证明'}`:`${goal.maturity||'尚无成熟度'} · ${goal.data_readiness||'尚无数据就绪证明'}`;
  const metrics=[['执行阶段',`${summary.tasks_succeeded||0}/${summary.tasks||0}`,'已成功'],['实际查询',summary.queries||0,`${summary.candidate_results||0} 个候选结果`],['数据来源',summary.source_domains||0,`${summary.providers||0} 个检索服务`],['确认引用',summary.confirmed_citations||0,'通过逐条核验'],['表格填充',`${summary.populated_fields||0}/${summary.table_fields||0}`,'字段通过证据门禁'],['修复动作',summary.repair_actions||0,`${summary.open_issues||0} 组开放问题`]];
  return `<div class="trace-summary-grid">${metrics.map(([label,value,note])=>`<article class="trace-stat"><label>${h(label)}</label><strong>${h(value)}</strong><small>${h(note)}</small></article>`).join('')}</div>
    <div class="goal-proof-strip"><article class="${goal.workflow_complete?'complete':'incomplete'}"><span>WORKFLOW</span><strong>${goal.workflow_complete?'执行图已结束':'执行图未结束'}</strong><small>${h(labels[trace?.run_status]||trace?.run_status||'—')}</small></article><article class="${goal.goal_complete?'complete':'incomplete'}"><span>DECLARED GOAL · ${h(goalLabel)}</span><strong>${goal.goal_complete?`${h(goalLabel)}目标已完成`:`${h(goalLabel)}目标未完成`}</strong><small>${h(goalProof)}</small></article><article class="${goal.pipeline_continue?'active':'inactive'}"><span>NEXT ACTION</span><strong>${h(goal.next_action||'无自动路径或等待授权')}</strong><small>采纳证据 ${Number(goal.accepted_evidence||0)} · 填充字段 ${Number(goal.populated_fields||0)}</small></article></div>
    ${blockers.length?`<div class="goal-blockers"><span>尚未完成的目标条件</span><ol>${blockers.map(item=>`<li>${h(item)}</li>`).join('')}</ol></div>`:''}
    <div class="trace-alert ${summary.open_issues||summary.populated_fields===0?'attention':''}"><span>当前判断</span><strong>${h(bottleneck)}</strong><small>运行状态 ${h(labels[trace?.run_status]||trace?.run_status||'—')} · 质量分 ${Math.round(Number(trace?.quality?.score||0)*100)}%</small></div>
    <div class="trace-overview-grid"><article class="trace-block"><header><span class="section-kicker">QUALITY VECTOR</span><h3>质量向量</h3></header><div class="state-bars">${Object.entries(dims).length?Object.entries(dims).map(([name,score])=>`<div class="state-row"><span title="${h(name)}">${h(dimensionNames[name]||name)}</span><div class="bar"><i style="width:${Math.round(Number(score||0)*100)}%"></i></div><b>${Math.round(Number(score||0)*100)}</b></div>`).join(''):empty('暂无质量向量')}</div></article>
    <article class="trace-block"><header><span class="section-kicker">PERSISTED STAGES</span><h3>阶段与尝试</h3></header>${stageLane(stages)}</article></div>
    ${trace?.batch?`<details class="trace-provenance"><summary>查看本次审计数据批次</summary><code>${h(trace.batch)}</code><p>${h((trace.providers||[]).join(' · ')||'无检索服务')} · ${h((trace.source_domains||[]).slice(0,12).join(' · ')||'无来源域名')}</p></details>`:''}`;
}

function stageLane(stages) {
  if(!stages.length)return empty('尚无执行阶段');
  return `<ol class="stage-lane">${stages.map(stage=>`<li class="stage-node ${h(stage.status)}"><span class="stage-index">${String(stage.ordinal).padStart(2,'0')}</span><div><b>${h(stageNames[stage.task_id]||stage.task_id)}</b><small>${h(stage.capability_id||'—')} · ${stage.attempt_count||0} 次尝试${stage.failed_attempts?` · ${stage.failed_attempts} 次失败`:''}</small><time>${fmtDate(stage.started_at||stage.updated_at)} → ${fmtDate(stage.finished_at)}</time>${stage.failure_code?`<em>${h(stage.failure_code)}</em>`:''}</div>${badge(stage.status)}</li>`).join('')}</ol>`;
}

function searchExplorer(searches,providers,citations=[]) {
  if(!searches.length)return empty('没有记录查询','当前批次尚未生成可审计的搜索矩阵。');
  const selected=citations.filter(item=>item.selected), allResults=searches.flatMap(item=>item.results||[]);
  const outcomeCount=key=>allResults.filter(item=>(item.outcome||'pending')===key).length;
  return `${selected.length?`<div class="selected-evidence-strip"><div><span class="section-kicker">FINAL CITATION SELECTION</span><strong>最终采用 ${selected.length} 条引用</strong><small>这些来源已通过声明核验，并实际进入产出。</small></div>${selected.map(item=>`<article><div>${decisionPill(item.verdict)}<span>${h(item.section||item.claim_id||'未分组声明')}</span></div><b>${h(item.claim_text||'未记录声明正文')}</b>${sourceLink(item.url,item.domain||item.url)}</article>`).join('')}</div>`:''}
    <div class="query-outcome-strip"><div class="accepted"><strong>${outcomeCount('accepted')}</strong><span>已采用</span></div><div class="rejected"><strong>${outcomeCount('rejected')}</strong><span>内容未通过</span></div><div class="technical"><strong>${outcomeCount('technical_failure')}</strong><span>技术失败 / 未评估</span></div><div class="pending"><strong>${outcomeCount('pending')}</strong><span>待评估</span></div></div>
    <div class="query-toolbar"><label><span>搜索查询词、字段、域名或错误</span><input class="field" id="trace-query-filter" placeholder="例如：Cisco、pdftotext、碳排放"></label><label><span>数据源</span><select class="field" id="trace-provider-filter"><option value="">全部数据源</option>${providers.map(v=>`<option value="${h(v.toLowerCase())}">${h(v)}</option>`).join('')}</select></label><label><span>查询类型</span><select class="field" id="trace-kind-filter"><option value="">全部查询</option><option value="table_field">表格字段</option><option value="claim_evidence">文献声明</option></select></label><label><span>候选结论</span><select class="field" id="trace-outcome-filter"><option value="">全部结论</option><option value="accepted">已采用</option><option value="rejected">内容未通过</option><option value="technical_failure">技术失败 / 未评估</option><option value="pending">待评估</option></select></label><div class="query-count"><strong id="trace-query-count">${searches.length}</strong><span>条查询可见</span></div></div>
    <div class="query-guide"><span>每条查询均显示</span><b>查询关键词</b><i>→</i><b>实际数据源</b><i>→</i><b>候选网页</b><i>→</i><b>采纳 / 拒绝理由</b></div>
    <div class="query-ledger">${searches.map((item,index)=>searchCard(item,index)).join('')}</div>`;
}

function searchCard(item,index) {
  const providers=(item.providers||[]).filter(v=>v.provider), providerText=providers.map(v=>v.provider).join(' '), selected=(item.results||[]).filter(v=>v.selected).length, technical=(item.results||[]).filter(v=>v.outcome==='technical_failure').length;
  const haystack=[item.query,item.field,item.table,providerText,...(item.results||[]).flatMap(v=>[v.domain,v.title,v.source_class,v.fetch_status,v.technical_error?.code,v.technical_error?.message,...(v.reasons||[])])].filter(Boolean).join(' ').toLowerCase();
  return `<details class="query-card" data-query-card data-kind="${h(item.kind||'')}" data-provider="${h(providerText.toLowerCase())}" data-search="${h(haystack)}" ${index===0?'open':''}><summary><span class="query-seq">Q${String(index+1).padStart(3,'0')}</span><div class="query-heading"><strong>${h(item.query||'未记录查询词')}</strong><small>${h(item.kind==='table_field'?'表格字段查询':'声明证据查询')} · ${h(item.table||item.field||'未绑定字段')} · ${h(item.language||item.strategy||'—')}${technical?` · ${technical} 个技术失败`:''}</small></div><div class="query-result-count"><b>${(item.results||[]).length}</b><span>候选</span></div><div class="query-result-count selected"><b>${selected}</b><span>采纳</span></div></summary><div class="query-body"><div class="provider-run"><span>实际数据源</span>${providers.length?providers.map(v=>`<div><b>${h(v.provider)}</b>${decisionPill(v.status||'unknown')}<small>${Number(v.results||0)} 个结果${v.cache_hit?' · 命中缓存':''}</small></div>`).join(''):'<div><b>未记录 Provider</b></div>'}</div><div class="source-results">${(item.results||[]).length?(item.results||[]).map(sourceResult).join(''):empty('查询无返回结果','这条查询已执行，但数据源没有返回候选。')}</div></div></details>`;
}

function sourceResult(result) {
  const outcome=result.outcome||'pending', reasons=(result.reasons||[]).map(reasonLabel);
  const defaultReason=outcome==='accepted'?'该候选通过了持久化的证据选择或声明核验。':outcome==='rejected'?'持久化审计记录了拒绝结论，但没有提供更细的原因代码。':outcome==='technical_failure'?'候选在抓取或解析阶段失败，尚未完成内容相关性评估。':'候选仍在等待后续评估或没有终态选择记录。';
  const fetchState=result.fetch_status||result.status||'未记录', assessment=result.evaluation_completed?(outcome==='accepted'?'通过':'未通过'):(outcome==='technical_failure'?'未执行':'等待中');
  const metadata=[result.provider&&`数据源 ${result.provider}`,result.source_class&&`来源类别 ${result.source_class}`,result.content_type&&`内容类型 ${result.content_type}`,result.document_type&&`文档类型 ${result.document_type}`,result.document_route&&`解析路由 ${result.document_route}`,result.extraction_support&&`抽取支持 ${result.extraction_support}`,result.public_extractability&&`可抽取性 ${result.public_extractability}`].filter(Boolean);
  const verifications=result.verifications||[];
  return `<article class="source-result outcome-${h(outcome)} ${result.selected?'selected':''}" data-source-outcome="${h(outcome)}"><div class="source-decision">${outcomePill(outcome)}${result.selected?'<span class="chosen-mark">用于产出</span>':''}<small>${h(decisionStageNames[result.decision_stage]||result.decision_stage||'阶段未记录')}</small></div><div class="source-identity"><b>${h(result.title||result.domain||'未命名来源')}</b><small>${h(result.domain||'未知域名')}</small>${sourceLink(result.url,result.url)}</div><div class="source-pipeline"><span class="done"><i>1</i><b>检索命中</b><small>${h(result.provider||'来源未记录')}</small></span><span class="${outcome==='technical_failure'?'failed':'done'}"><i>2</i><b>抓取 / 解析</b><small>${h(fetchState)}</small></span><span class="${result.evaluation_completed?'done':outcome==='technical_failure'?'blocked':'pending'}"><i>3</i><b>内容评估</b><small>${h(assessment)}</small></span><span class="${outcome==='accepted'?'done':outcome==='rejected'?'failed':'pending'}"><i>4</i><b>最终选择</b><small>${h(verdictNames[result.decision]||result.decision||'待定')}</small></span></div><div class="reason-ledger"><span>采用 / 拒绝依据</span><ol>${reasons.length?reasons.map(item=>`<li><b>${h(item.label)}</b>${item.label!==item.raw?`<code>${h(item.raw)}</code>`:''}</li>`).join(''):`<li><b>${h(defaultReason)}</b></li>`}</ol></div>${result.technical_error?`<div class="technical-failure"><span>底层技术异常</span><b>${h(result.technical_error.code||'fetch_error')}</b><code>${h(result.technical_error.message||'未记录错误消息')}</code><p>该异常发生在内容判断之前，因此不能解释为“文献内容不相关”。</p></div>`:''}${metadata.length?`<div class="source-metadata">${metadata.map(item=>`<span>${h(item)}</span>`).join('')}<span>抽取观察 ${Number(result.observation_count||0)} 条</span><span>原始决策 ${h(result.decision||'—')}</span></div>`:''}${verifications.length?`<div class="verification-ledger"><span>逐条声明核验</span>${verifications.map(item=>`<article>${decisionPill(item.verdict)}<div><b>${h(item.claim_text||item.claim_id||'未命名声明')}</b><p>${h(item.reasoning||'未记录核验理由')}</p>${item.supporting_quote?`<blockquote>${h(item.supporting_quote)}</blockquote>`:''}</div></article>`).join('')}</div>`:''}${result.observations?.length?`<details class="source-observations"><summary>查看 ${result.observations.length} 条字段观察</summary>${jsonView(result.observations)}</details>`:''}${result.snippet?`<details class="source-observations"><summary>查看抓取片段</summary><p>${h(String(result.snippet).slice(0,1200))}</p></details>`:''}</article>`;
}

function evidenceLedger(citations,fields) {
  const selected=citations.filter(v=>v.selected).length, populated=fields.filter(v=>['accepted','populated','selected'].includes(v.decision)).length;
  return `<div class="evidence-banner"><div><span class="section-kicker">CITATION DECISIONS</span><strong>${selected}/${citations.length}</strong><small>声明核验后被选择为引用</small></div><div><span class="section-kicker">TABLE FIELD DECISIONS</span><strong>${populated}/${fields.length}</strong><small>字段通过证据门禁并完成填充</small></div></div><div class="evidence-columns"><section><header class="ledger-heading"><h3>文献引用选择</h3><p>每条声明都展示核验结论、来源和支撑片段。</p></header><div class="evidence-list">${citations.length?citations.map(citationCard).join(''):empty('暂无声明核验')}</div></section><section><header class="ledger-heading"><h3>表格字段填充</h3><p>保留空值并不是静默失败；这里解释候选为何没有通过。</p></header><div class="evidence-list">${fields.length?fields.map(fieldCard).join(''):empty('暂无表格字段审计')}</div></section></div>`;
}

function citationCard(item) {
  return `<details class="evidence-card ${item.selected?'selected':''}"><summary><div>${decisionPill(item.verdict)}<small>${h(item.section||item.claim_kind||item.claim_id||'未分组声明')}</small></div><strong>${h(item.claim_text||'未记录声明正文')}</strong></summary><div class="evidence-detail">${sourceLink(item.url,item.domain||item.url)}<dl><dt>核验理由</dt><dd>${h(item.reasoning||'未记录')}</dd><dt>支撑原文</dt><dd>${h(item.supporting_quote||'没有可用支撑片段')}</dd><dt>节点一致性</dt><dd>${h(item.node_alignment||'未记录')}</dd></dl></div></details>`;
}

function fieldCard(item) {
  const rejected=item.rejected_urls||[];
  return `<details class="evidence-card ${decisionClass(item.decision)}"><summary><div>${decisionPill(item.decision)}<small>${h(item.table||'未分组表格')} · ${Number(item.candidate_count||0)} 个候选</small></div><strong>${h(item.field||'未命名字段')}</strong></summary><div class="evidence-detail"><dl><dt>判定理由</dt><dd>${h(item.reason||'未记录')}</dd><dt>查询 Hash</dt><dd class="mono">${h((item.query_hashes||[]).join(' · ')||'—')}</dd><dt>已拒绝来源</dt><dd>${rejected.length?rejected.map(url=>sourceLink(url,url)).join('<br>'):'没有记录拒绝地址'}</dd></dl></div></details>`;
}

function repairLedger(issues,actions) {
  return `<div class="repair-grid"><section><header class="ledger-heading"><span class="section-kicker">GROUPED DEVIATIONS</span><h3>系统发现了什么问题</h3><p>相同偏离会合并显示并保留出现次数，不再重复铺满页面。</p></header><div class="issue-list">${issues.length?issues.map(issueCard).join(''):empty('没有目标偏离')}</div></section><section><header class="ledger-heading"><span class="section-kicker">CAUSAL ACTION LOG</span><h3>系统实际执行了什么操作</h3><p>展示诊断、修复计划、系统变更与代码修复的因果输入和结果。</p></header><div class="action-ledger">${actions.length?actions.map(actionCard).join(''):empty('尚无修复动作')}</div></section></div>`;
}

function issueCard(item) {
  return `<details class="issue-cluster ${h(item.severity||'medium')}"><summary><div><span class="section-kicker">${h(item.deviation_type||'UNCLASSIFIED')} · ${h(item.severity||'unknown')}</span><strong>${h(item.summary)}</strong></div><span class="issue-count">× ${item.count||1}</span></summary><div class="issue-detail"><p>首次 ${fmtDate(item.first_seen)} · 最近 ${fmtDate(item.last_seen)} · 状态 ${h((item.statuses||[]).map(v=>labels[v]||v).join(' / '))}</p>${Object.keys(item.evidence||{}).length?jsonView(item.evidence):'<p class="muted">没有附加证据。</p>'}</div></details>`;
}

function actionCard(item,index) {
  const details=Object.entries(item.details||{});
  const scm=item.details?.scm||{};
  const scmLinks=[scm.issue_url?sourceLink(scm.issue_url,`Issue #${scm.issue_number||'—'}`):'',scm.pr_url?sourceLink(scm.pr_url,`PR #${scm.pr_number||'—'}`):''].filter(Boolean).join(' ');
  const title=item.kind==='code_repair'?(repairCauseNames[item.title]||item.title):item.title;
  return `<article class="action-item ${item.kind==='code_repair'?'code-repair-action':''}"><span class="action-step">${String(index+1).padStart(2,'0')}</span><div><div class="action-head"><span class="section-kicker">${h(actionNames[item.kind]||item.kind)}</span>${badge(item.status||'unknown')}</div><h4>${h(title||'未命名动作')}</h4>${item.kind==='code_repair'&&repairCauseNames[item.title]?`<code class="action-cause-code">${h(item.title)}</code>`:''}<p>${h(item.summary||'该动作没有提供摘要。')}</p><small>${fmtDate(item.created_at)}${item.updated_at?` → 更新 ${fmtDate(item.updated_at)}`:''}${item.risk?` · 风险 ${h(item.risk)}`:''} · ${h(item.id||'—')}</small>${scmLinks?`<div class="action-scm-links">${scmLinks}</div>`:''}${details.length?`<details class="action-proof"><summary>查看修改内容与证明约束</summary><dl>${details.map(([key,val])=>`<dt>${h(key)}</dt><dd>${typeof val==='string'?h(val):jsonView(val)}</dd>`).join('')}</dl></details>`:''}</div></article>`;
}

function bindExecutionTrace(jobId,selectedPane='stages') {
  const tabs=[...document.querySelectorAll('[data-trace-tab]')];
  const activate=tab=>{tabs.forEach(node=>{const active=node===tab;node.classList.toggle('active',active);node.setAttribute('aria-selected',String(active));});document.querySelectorAll('[data-trace-pane]').forEach(pane=>pane.classList.toggle('active',pane.dataset.tracePane===tab.dataset.traceTab));const prior=jobViewState.get(jobId)||{};jobViewState.set(jobId,{...prior,tracePane:tab.dataset.traceTab});};
  tabs.forEach(tab=>tab.addEventListener('click',()=>activate(tab)));
  activate(tabs.find(tab=>tab.dataset.traceTab===selectedPane)||tabs[0]);
  document.querySelectorAll('[data-promote-logic]').forEach(button=>button.addEventListener('click',async()=>{const findingId=button.dataset.promoteLogic;if(!confirm('确认把这条只读逻辑观察提升为正式偏离调查？\n\n该操作只建立调查记录，不会自动修改 Job、Gate、代码或触发修复。'))return;button.disabled=true;try{const result=await api(`/api/logic-audit/findings/${encodeURIComponent(findingId)}/promote`,{method:'POST',body:JSON.stringify({confirm:true})});toast(`已建立正式调查 ${result.deviation_id}`);await jobDetail(jobId);}catch(err){toast(err.message,true);button.disabled=false;}}));
  const stageStatus=document.querySelector('#stage-status-filter'), stageKind=document.querySelector('#stage-kind-filter'), stageCount=document.querySelector('#stage-visible-count');
  if(stageStatus&&stageKind&&stageCount){const applyStages=()=>{let visible=0;document.querySelectorAll('[data-stage-audit]').forEach(card=>{const show=(!stageStatus.value||card.dataset.stageStatus===stageStatus.value)&&(!stageKind.value||card.dataset.stageKind===stageKind.value);card.hidden=!show;if(show)visible+=1;});stageCount.textContent=visible;};stageStatus.addEventListener('change',applyStages);stageKind.addEventListener('change',applyStages);}
  const text=document.querySelector('#trace-query-filter'), provider=document.querySelector('#trace-provider-filter'), kind=document.querySelector('#trace-kind-filter'), outcome=document.querySelector('#trace-outcome-filter'), count=document.querySelector('#trace-query-count');
  if(!text||!provider||!kind||!outcome)return;
  const apply=()=>{let visible=0;document.querySelectorAll('[data-query-card]').forEach(card=>{const results=[...card.querySelectorAll('[data-source-outcome]')];results.forEach(node=>node.hidden=Boolean(outcome.value)&&node.dataset.sourceOutcome!==outcome.value);const outcomeMatch=!outcome.value||results.some(node=>node.dataset.sourceOutcome===outcome.value);const show=(!text.value.trim()||card.dataset.search.includes(text.value.trim().toLowerCase()))&&(!provider.value||card.dataset.provider.includes(provider.value))&&(!kind.value||card.dataset.kind===kind.value)&&outcomeMatch;card.hidden=!show;if(show)visible+=1;});count.textContent=visible;};
  text.addEventListener('input',apply);provider.addEventListener('change',apply);kind.addEventListener('change',apply);outcome.addEventListener('change',apply);
}
function bindRecover(){document.querySelectorAll('[data-recover]').forEach(btn=>btn.addEventListener('click',async()=>{const [run,task]=btn.dataset.recover.split('|');if(!confirm(`确认重新放行 ${task}？本次尝试会绑定上一份失败 Artifact。`))return;try{await api(`/api/workflows/${encodeURIComponent(run)}/tasks/${encodeURIComponent(task)}/recover`,{method:'POST',body:JSON.stringify({confirm:true})});toast(`${task} 已重新放行`);route();}catch(err){toast(err.message,true)}}));}

async function workflows() {
  setPage('workflows'); const data=await api('/api/workflows');
  app.innerHTML=`<div class="reveal"><p class="section-kicker">PERSISTENT DAG RUNS</p><h2 class="section-title">${data.total} 个可恢复的执行图</h2><div class="stack">${data.items.length?data.items.map(run=>{const progress=pct(run.succeeded_count||0,run.task_count||0);return `<article class="panel"><header class="panel-head"><div><h2>${h(run.target)}</h2><p class="mono">${h(run.workflow_ref)} · ${h(run.run_id)}</p></div>${badge(run.status)}</header><div class="panel-body"><div class="state-row"><span>完成进度</span><div class="bar"><i style="width:${progress}%"></i></div><b>${progress}%</b></div><div class="job-meta"><span>成功 <b>${run.succeeded_count||0}</b></span><span>就绪 <b>${run.ready_count||0}</b></span><span>Tasks <b>${run.task_count||0}</b></span><span>更新 <b>${fmtDate(run.updated_at)}</b></span></div><a class="action-button secondary" href="#/jobs/${encodeURIComponent(run.job_id)}">查看执行图</a></div></article>`}).join(''):empty('暂无 Workflow Run')}</div></div>`;
}

async function artifacts() {
  setPage('artifacts'); app.innerHTML=`<div class="toolbar"><input class="field search-field" id="artifact-q" placeholder="搜索 Hash 或 Schema…"><select class="field" id="artifact-type"><option value="">全部媒体类型</option></select></div><div id="artifact-results"></div>`;
  const q=app.querySelector('#artifact-q'), type=app.querySelector('#artifact-type'), target=app.querySelector('#artifact-results');let timer;
  async function load(){const data=await api(`/api/artifacts?q=${encodeURIComponent(q.value)}&media_type=${encodeURIComponent(type.value)}`);if(type.options.length===1)data.types.forEach(t=>type.insertAdjacentHTML('beforeend',`<option value="${h(t.media_type)}">${h(t.media_type)} · ${t.count}</option>`));target.innerHTML=`<p class="section-kicker">CONTENT-ADDRESSED STORAGE</p><h2 class="section-title">${data.total} 份不可变记录</h2>${data.items.length?`<div class="artifact-grid">${data.items.map(artifactCard).join('')}</div>`:empty('未找到产物')}`;bindArtifactLinks();}
  q.addEventListener('input',()=>{clearTimeout(timer);timer=setTimeout(load,250)});type.addEventListener('change',load);await load();
}
function artifactCard(a){return `<article class="artifact-card" tabindex="0" data-artifact="${h(a.digest)}"><span class="artifact-icon">⌁</span><h3>${h(a.digest)}</h3><p>${h(a.metadata?.schema||a.metadata?.kind||a.media_type)}</p><div class="artifact-meta"><span>${fmtBytes(a.size)}</span><span>${fmtDate(a.created_at)}</span></div></article>`;}
function bindArtifactLinks(){document.querySelectorAll('[data-artifact]').forEach(node=>{const open=()=>artifactDetail(node.dataset.artifact);node.addEventListener('click',open);node.addEventListener('keydown',e=>{if(e.key==='Enter')open()});});}
async function artifactDetail(digest){try{const data=await api(`/api/artifacts/${digest}`),a=data.artifact;if(a.media_type==='application/json'){await jsonArtifactViewer(digest);return;}openDrawer(`<span class="section-kicker">ARTIFACT DETAIL</span><h2>${h(a.metadata?.schema||a.media_type)}</h2><div class="definition-grid"><div class="definition"><label>HASH</label><span class="mono">${h(a.digest)}</span></div><div class="definition"><label>SIZE</label><span>${fmtBytes(a.size)}</span></div><div class="definition"><label>CREATED</label><span>${fmtDate(a.created_at)}</span></div></div><h3>Metadata</h3>${jsonView(a.metadata)}<h3>血缘边</h3>${data.edges.length?jsonView(data.edges):empty('暂无血缘边')}<h3>内容预览</h3>${data.preview_type==='text'?`<pre class="json-view">${h(data.preview)}</pre>`:empty('不可预览','二进制或大于 500 KB 的产物不在浏览器中展开。')}`);}catch(err){toast(err.message,true)}}

async function events() {
  setPage('events'); app.innerHTML=`<div class="toolbar"><input class="field search-field" id="event-q" placeholder="搜索聚合对象、Actor、Event Type…"><select class="field" id="event-type"><option value="">全部事件类型</option></select></div><section class="panel"><div class="panel-body" id="event-results"></div></section>`;
  const q=app.querySelector('#event-q'),type=app.querySelector('#event-type'),target=app.querySelector('#event-results');let timer;
  async function load(){const data=await api(`/api/events?q=${encodeURIComponent(q.value)}&event_type=${encodeURIComponent(type.value)}&limit=300`);if(type.options.length===1)data.types.forEach(t=>type.insertAdjacentHTML('beforeend',`<option value="${h(t.event_type)}">${h(t.event_type)} · ${t.count}</option>`));target.innerHTML=eventList(data.items,300);target.querySelectorAll('.timeline-item').forEach((node,i)=>node.addEventListener('click',()=>openDrawer(`<span class="section-kicker">EVENT #${data.items[i].sequence}</span><h2>${h(data.items[i].event_type)}</h2>${jsonView(data.items[i])}`)));}
  q.addEventListener('input',()=>{clearTimeout(timer);timer=setTimeout(load,250)});type.addEventListener('change',load);await load();
}

async function exceptions() {
  setPage('exceptions'); const data=await api('/api/exceptions');
  const all=[...data.items.map(x=>({...x,source:'control-plane',when:x.opened_at,code:x.error_code})),...data.faults.map(x=>({...x,source:'wiki-runtime',when:x.created_at,code:x.code,status:x.classification}))];
  app.innerHTML=`<div class="reveal"><p class="section-kicker">EXCEPTION & REPAIR</p><h2 class="section-title">${all.length} 条失败事实</h2><section class="panel"><div class="panel-body table-wrap">${all.length?`<table class="data-table"><thead><tr><th>错误</th><th>状态</th><th>Run</th><th>来源</th><th>时间</th></tr></thead><tbody>${all.map(x=>`<tr><td><b>${h(x.code)}</b><div class="mono">${h(x.id)}</div></td><td>${badge(x.status)}</td><td class="mono">${h(short(x.run_id,22))}</td><td>${h(x.source)}</td><td>${fmtDate(x.when)}</td></tr>`).join('')}</tbody></table>`:empty('没有异常','当前控制平面没有记录结构化异常或 Wiki Runtime Fault。')}</div></section></div>`;
}

async function system() {
  setPage('system'); const data=await api('/api/system');
  const valid=data.workflows.filter(w=>w.status==='valid').length;
  app.innerHTML=`<div class="reveal"><section class="job-hero"><div><span class="section-kicker">PLATFORM COMPOSITION</span><h2>控制平面构成</h2><p>${h(data.root)}</p><div class="job-meta"><span>Capabilities <b>${data.capabilities.length}</b></span><span>Skills <b>${data.skills.length}</b></span><span>Workflows <b>${valid}/${data.workflows.length} valid</b></span><span>Proofs <b>${data.proofs.length}</b></span></div></div><div class="system-orbit"><i></i><strong>${valid===data.workflows.length?'一致':'漂移'}</strong><small>DECLARATIVE SURFACE</small></div></section>
    <div class="split"><section class="panel"><header class="panel-head"><div><h2>Capabilities</h2><p>允许执行的版本化能力</p></div></header><div class="panel-body table-wrap">${simpleTable(['ID','执行器','副作用','状态'],data.capabilities.map(c=>[c.id,c.executor,c.side_effects,badge(c.production_ready?'ok':'blocked')]))}</div></section><section class="panel"><header class="panel-head"><div><h2>Skills</h2><p>意图路由与 Workflow 绑定</p></div></header><div class="panel-body table-wrap">${simpleTable(['Skill','版本','Workflow','Policy'],data.skills.map(s=>[s.name,s.version,s.workflow,s.policy]))}</div></section></div>
    <section class="panel" style="margin-top:18px"><header class="panel-head"><div><h2>Workflow Registry</h2><p>静态 DAG 编译结果</p></div></header><div class="panel-body table-wrap">${simpleTable(['Workflow','版本','Tasks','状态'],data.workflows.map(w=>[w.ref,w.version||'—',w.tasks||'—',badge(w.status)]))}</div></section>
    <div class="split" style="margin-top:18px"><section class="panel"><header class="panel-head"><div><h2>预算</h2><p>保留与消耗额度</p></div></header><div class="panel-body">${data.budgets.length?jsonView(data.budgets):empty('尚未配置预算')}</div></section><section class="panel"><header class="panel-head"><div><h2>Lease 与 Stage</h2><p>运行时活动记录</p></div></header><div class="panel-body">${data.leases.length||data.stages.length?jsonView({leases:data.leases,stages:data.stages}):empty('没有活动记录')}</div></section></div>
  </div>`;
}
function simpleTable(headers,rows){if(!rows.length)return empty('暂无记录');return `<table class="data-table"><thead><tr>${headers.map(x=>`<th>${h(x)}</th>`).join('')}</tr></thead><tbody>${rows.map(row=>`<tr>${row.map(cell=>`<td>${typeof cell==='string'&&cell.startsWith('<span class="status')?cell:h(cell)}</td>`).join('')}</tr>`).join('')}</tbody></table>`;}

async function route() {
  closeDrawer(); clearTimeout(refreshTimer); loading();
  const parts=location.hash.replace(/^#\/?/,'').split('/').filter(Boolean); const name=parts[0]||'overview';
  try {
    if(name==='overview') await overview(); else if(name==='jobs'&&parts[1]==='new') await createJob(); else if(name==='jobs'&&parts[1]) await jobDetail(decodeURIComponent(parts[1]));
    else if(name==='jobs') await jobs(); else if(name==='workflows') await workflows(); else if(name==='artifacts') await artifacts();
    else if(name==='events') await events(); else if(name==='exceptions') await exceptions(); else if(name==='system') await system();
    else { location.hash='#/overview'; return; }
  } catch(error) { failure(error); }
  if(autoRefresh && ['overview','workflows','exceptions'].includes(name)) refreshTimer=setTimeout(route,30000);
  app.focus({preventScroll:true});
}

document.querySelector('#refresh').addEventListener('click',route);
document.querySelector('#refresh-toggle').addEventListener('click',e=>{autoRefresh=!autoRefresh;e.currentTarget.textContent=`自动刷新 · ${autoRefresh?'开':'关'}`;toast(`自动刷新已${autoRefresh?'开启':'关闭'}`);route();});
document.querySelector('#drawer-close').addEventListener('click',closeDrawer);backdrop.addEventListener('click',closeDrawer);
document.addEventListener('keydown',e=>{if(e.key==='Escape')closeDrawer();});
window.addEventListener('hashchange',route);
setInterval(()=>{document.querySelector('#clock').textContent=new Date().toLocaleTimeString('zh-CN',{hour12:false});},1000);
route();
