const app = document.querySelector('#app');
const title = document.querySelector('#page-title');
const index = document.querySelector('#page-index');
const drawer = document.querySelector('#drawer');
const drawerContent = document.querySelector('#drawer-content');
const backdrop = document.querySelector('#drawer-backdrop');
let autoRefresh = true;
let refreshTimer;

const pages = {
  overview: ['01', '运行总览'], jobs: ['02', '任务与目标'], workflows: ['03', '工作流运行'],
  artifacts: ['04', '产物账本'], events: ['05', '事件流'], exceptions: ['06', '异常与修复'], system: ['07', '系统构成']
};
const labels = {
  planned:'已规划', ready:'就绪', running:'运行中', paused:'已暂停', succeeded:'已成功', failed:'失败', repairable:'可修复',
  retryable:'可重试', quarantined:'已隔离', blocked:'已阻塞', blocked_budget:'预算阻塞', published:'已发布',
  pending:'等待中', candidate:'候选', gated:'已门禁', applied:'已应用', pass:'通过', fail:'未通过', ok:'正常'
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
function openDrawer(content) { drawerContent.innerHTML = content; drawer.classList.add('open'); drawer.setAttribute('aria-hidden','false'); backdrop.hidden=false; }
function closeDrawer() { drawer.classList.remove('open'); drawer.setAttribute('aria-hidden','true'); backdrop.hidden=true; }

function stateBars(states) {
  const entries = Object.entries(states || {}); const total = entries.reduce((sum,[,n])=>sum+n,0);
  if (!entries.length) return empty('暂无状态分布');
  return `<div class="state-bars">${entries.map(([state,count]) => `<div class="state-row"><span>${h(labels[state]||state)}</span><div class="bar"><i style="width:${pct(count,total)}%"></i></div><b>${count}</b></div>`).join('')}</div>`;
}
function eventList(items, limit=20) {
  if (!items?.length) return empty('暂无事件');
  return `<div class="timeline">${items.slice(0,limit).map(item => `<div class="timeline-item"><span class="sequence">#${item.sequence}</span><div><b>${h(item.event_type)}</b><p>${h(item.aggregate_type)} · ${h(short(item.aggregate_id,22))} · ${h(item.actor||'system')} · ${fmtDate(item.occurred_at)}</p></div></div>`).join('')}</div>`;
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
  form.addEventListener('submit',async e=>{e.preventDefault();const button=form.querySelector('.create-submit');button.disabled=true;button.textContent='正在提交…';try{const request={};for(const input of form.querySelectorAll('.schema-input')){const type=input.dataset.type,name=input.dataset.name;let value;if(type==='boolean')value=input.checked;else if(type==='array'){const raw=input.value.trim();if(!raw)continue;value=raw.split(/[\n,，]+/).map(x=>x.trim()).filter(Boolean);if(input.dataset.itemType==='number')value=value.map(Number);else if(input.dataset.itemType==='integer')value=value.map(x=>parseInt(x,10));}else if(type==='object'){if(!input.value.trim())continue;value=JSON.parse(input.value);}else if(type==='integer'){if(!input.value)continue;value=parseInt(input.value,10);}else if(type==='number'){if(!input.value)continue;value=Number(input.value);}else{if(!input.value.trim())continue;value=input.value.trim();}request[name]=value;}if(form.querySelector('#autonomous-supervision').checked){const target=request.nodes?.[0]||request.target||'request';const spec={schema_version:'autonomous-job-campaign-v1',name:`${current.name}:${target}`,skill:current.name,requests:[request],max_concurrency:1,max_auto_repairs_per_job:3,poll_seconds:2,stop_on_failure:false};const result=await api('/api/autonomy',{method:'POST',body:JSON.stringify({spec,start:true})});const item=result.items?.[0];toast('自治 Campaign 已启动');location.hash=item?.job_id?`#/jobs/${encodeURIComponent(item.job_id)}`:'#/jobs';return;}const payload={skill:current.name,request,materialize:form.querySelector('#auto-materialize').checked};const key=form.querySelector('#idempotency-key').value.trim();if(key)payload.idempotency_key=key;const result=await api('/api/jobs',{method:'POST',body:JSON.stringify(payload)});toast(result.deduplicated?'已找到相同任务，未重复创建':'任务创建成功');location.hash=`#/jobs/${encodeURIComponent(result.job_id)}`;}catch(err){toast(err instanceof SyntaxError?'JSON 字段格式不正确':err.message,true);button.disabled=false;button.textContent='提交任务 →';}});
}

async function jobDetail(jobId) {
  setPage('jobs','JOB DETAIL'); const data=await api(`/api/jobs/${encodeURIComponent(jobId)}`);
  const job=data.job, payload=job.payload||{}, tasks=data.tasks||[], done=tasks.filter(t=>t.status==='succeeded').length;
  const canMaterialize=!data.run;
  const canPause=['planned','ready','leased','running','stalled','retryable','repairable','manual_review','blocked_budget'].includes(job.status);
  app.innerHTML=`<div class="reveal">
    <section class="job-hero"><div><span class="section-kicker">${h(job.workflow_id||'UNMATERIALIZED')}</span><h2>${h(payload.target||job.id)}</h2><p class="mono">${h(job.id)}</p><div class="job-meta"><span>状态 <b>${h(labels[job.status]||job.status)}</b></span><span>策略 <b>${h(payload.policy_version||'—')}</b></span><span>风险 <b>${h(payload.risk||'standard')}</b></span><span>更新 <b>${fmtDate(job.updated_at)}</b></span></div></div>${data.run?`<div><div class="progress-ring" style="--progress:${pct(done,tasks.length)}%"><strong>${done}/${tasks.length}</strong><small>COMPLETED</small></div><div class="job-actions">${job.status==='paused'?`<button class="action-button" id="resume-job">恢复任务</button>`:canPause?`<button class="action-button" id="run-worker">执行下一步</button><button class="action-button secondary" id="pause-job">暂停任务</button>`:''}</div></div>`:`<button class="action-button" id="materialize">物化 Workflow</button>`}</section>
    <section class="panel"><header class="panel-head"><div><h2>执行图</h2><p>${data.run?h(data.run.run_id):'尚未创建持久化 Workflow Run'}</p></div>${data.run?badge(data.run.status):''}</header><div class="panel-body">${tasks.length?taskCards(tasks,data.run):empty('等待物化','Planner 尚未将这个 Job 展开为持久化 Tasks。')}</div></section>
    <section class="panel" style="margin-top:18px"><header class="panel-head"><div><h2>目标对齐与自我修复</h2><p>质量轨迹、偏离诊断、分级修复与系统变更候选</p></div><button class="action-button secondary" id="goal-audit">立即审计并修复</button></header><div class="panel-body">${alignmentMini(data.goal_alignment)}</div></section>
    <div class="split" style="margin-top:18px"><section class="panel"><header class="panel-head"><div><h2>关联产物</h2><p>输入与 Task 输出 Hash</p></div></header><div class="panel-body">${artifactMini(data.artifacts)}</div></section><section class="panel"><header class="panel-head"><div><h2>Gate 与异常</h2><p>发布资格与失败事实</p></div></header><div class="panel-body">${gateMini(data.gates,data.exceptions)}</div></section></div>
    <section class="panel" style="margin-top:18px"><header class="panel-head"><div><h2>任务事件</h2><p>Job 与 Workflow Run 的审计轨迹</p></div></header><div class="panel-body">${eventList(data.events,100)}</div></section>
  </div>`;
  app.querySelector('#materialize')?.addEventListener('click', async e=>{e.currentTarget.disabled=true;try{await api(`/api/jobs/${encodeURIComponent(jobId)}/materialize`,{method:'POST',body:'{}'});toast('Workflow 已物化');await jobDetail(jobId);}catch(err){toast(err.message,true);e.currentTarget.disabled=false;}});
  app.querySelector('#run-worker')?.addEventListener('click', async e=>{e.currentTarget.disabled=true;e.currentTarget.textContent='正在启动…';try{const result=await api(`/api/jobs/${encodeURIComponent(jobId)}/worker`,{method:'POST',body:'{}'});toast(result.status==='already_running'?'后台 Worker 已在运行':'后台 Worker 已启动');setTimeout(()=>jobDetail(jobId),800);}catch(err){toast(err.message,true);e.currentTarget.disabled=false;e.currentTarget.textContent='执行下一步';}});
  app.querySelector('#pause-job')?.addEventListener('click',async e=>{if(!confirm('确认暂停该 Job？当前正在执行的单步会安全结束，但不会领取下一步。'))return;e.currentTarget.disabled=true;try{await api(`/api/jobs/${encodeURIComponent(jobId)}/pause`,{method:'POST',body:JSON.stringify({confirm:true})});toast('任务已请求暂停');await jobDetail(jobId);}catch(err){toast(err.message,true);e.currentTarget.disabled=false;}});
  app.querySelector('#resume-job')?.addEventListener('click',async e=>{e.currentTarget.disabled=true;try{await api(`/api/jobs/${encodeURIComponent(jobId)}/resume`,{method:'POST',body:JSON.stringify({confirm:true})});toast('任务已恢复');await jobDetail(jobId);}catch(err){toast(err.message,true);e.currentTarget.disabled=false;}});
  app.querySelector('#goal-audit')?.addEventListener('click',async e=>{e.currentTarget.disabled=true;try{const result=await api(`/api/jobs/${encodeURIComponent(jobId)}/goal-audit`,{method:'POST',body:JSON.stringify({auto_repair:true})});toast(`目标审计完成：${result.deviations.length} 个偏离，${result.actions.length} 个动作`);await jobDetail(jobId);}catch(err){toast(err.message,true);e.currentTarget.disabled=false;}});
  bindArtifactLinks(); bindRecover();
}

function taskCards(tasks,run) {
  return `<div class="task-grid">${tasks.map((task,i)=>`<article class="task-card ${h(task.status)}"><span class="task-number">${String(i+1).padStart(2,'0')} / ${String(tasks.length).padStart(2,'0')}</span><h4>${h(task.task_id)}</h4><p>${h(task.capability_id)}<br>依赖：${h((task.dependencies||[]).join(', ')||'无')}<br>尝试：${task.attempt}</p><footer>${badge(task.status)}${task.status==='repairable'?`<button class="action-button secondary" data-recover="${h(run.run_id)}|${h(task.task_id)}">修复重试</button>`:task.output_hash?`<button class="quiet-button" data-artifact="${h(task.output_hash)}">${h(short(task.output_hash))}</button>`:''}</footer></article>`).join('')}</div>`;
}
function artifactMini(items) { return !items?.length?empty('尚无关联产物'):`<div class="stack">${items.map(a=>`<button class="action-button secondary clip" data-artifact="${h(a.digest)}">${h(a.metadata?.schema||a.media_type)} · ${h(short(a.digest,18))}</button>`).join('')}</div>`; }
function gateMini(gates,exceptions) {
  if (!gates?.length&&!exceptions?.length) return empty('暂无 Gate 或异常');
  return `<div class="stack">${(gates||[]).map(g=>`<div><span class="section-kicker">${h(g.gate_name)}</span><p>${badge(g.verdict)} <span class="mono">${h(short(g.evidence_digest,16))}</span></p></div>`).join('')}${(exceptions||[]).map(x=>`<div><span class="section-kicker">${h(x.error_code)}</span><p>${badge(x.status)} ${h(JSON.stringify(x.payload||{}))}</p></div>`).join('')}</div>`;
}
function alignmentMini(value) {
  const quality=value?.quality_observations?.[0]?.payload, deviations=value?.deviations||[], plans=value?.repair_plans||[], changes=value?.change_candidates||[], triage=value?.failure_triage_runs||[], repairs=value?.system_repair_runs||[];
  if(!quality&&!deviations.length&&!plans.length&&!changes.length&&!triage.length&&!repairs.length)return empty('等待首次目标审计','Worker 每个步骤结束后会自动生成质量向量。');
  const dims=quality?.dimensions||{};
  return `<div class="stack">${quality?`<div><span class="section-kicker">QUALITY VECTOR · ${Math.round((quality.score||0)*100)}%</span><div class="state-bars">${Object.entries(dims).map(([name,score])=>`<div class="state-row"><span>${h(name)}</span><div class="bar"><i style="width:${Math.round(score*100)}%"></i></div><b>${Math.round(score*100)}</b></div>`).join('')}</div></div>`:''}${deviations.slice(0,5).map(d=>`<div><span class="section-kicker">${h(d.deviation_type)} · ${h(d.severity)}</span><p>${h(d.payload?.summary||d.fingerprint)}</p></div>`).join('')}${triage.slice(0,5).map(t=>`<div><span class="section-kicker">AGENT TRIAGE · ${h(t.status)}</span><p>${h(t.payload?.result?.summary||t.last_error||t.triage_run_id)}</p></div>`).join('')}${plans.slice(0,5).map(p=>`<div><span class="section-kicker">${h(p.repair_level)} · ${h(p.status)}</span><p>${h(p.action)}</p></div>`).join('')}${changes.slice(0,5).map(c=>`<div><span class="section-kicker">SYSTEM CHANGE · ${h(c.status)}</span><p>${h(c.target)} · risk ${h(c.risk)}</p></div>`).join('')}${repairs.slice(0,5).map(r=>`<div><span class="section-kicker">CODING AGENT · ${h(r.status)}</span><p>${h(r.model)} · ${h(r.patch_hash?short(r.patch_hash,18):(r.last_error||r.repair_run_id))}</p></div>`).join('')}</div>`;
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
async function artifactDetail(digest){try{const data=await api(`/api/artifacts/${digest}`),a=data.artifact;openDrawer(`<span class="section-kicker">ARTIFACT DETAIL</span><h2>${h(a.metadata?.schema||a.media_type)}</h2><div class="definition-grid"><div class="definition"><label>HASH</label><span class="mono">${h(a.digest)}</span></div><div class="definition"><label>SIZE</label><span>${fmtBytes(a.size)}</span></div><div class="definition"><label>CREATED</label><span>${fmtDate(a.created_at)}</span></div></div><h3>Metadata</h3>${jsonView(a.metadata)}<h3>血缘边</h3>${data.edges.length?jsonView(data.edges):empty('暂无血缘边')}<h3>内容预览</h3>${data.preview_type==='text'?`<pre class="json-view">${h(data.preview)}</pre>`:data.preview_type==='json'?jsonView(data.preview):empty('不可预览','二进制或大于 500 KB 的产物不在浏览器中展开。')}`);}catch(err){toast(err.message,true)}}

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
