import { phaseNames, phaseNotes, recordedCode, proof } from './demo-data.js';
 const escape=s=>String(s).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;');
 function ascii(lines,label){return '<pre class="ascii" aria-label="'+(label||'Illustrative pipeline view')+'">'+escape(lines.join('\n'))+'</pre>';}
 const REAL_ASCII='Recorded pipeline view';
 const num=v=>typeof v==='number'&&Number.isFinite(v);
 const fx=v=>num(v)?v.toFixed(2)+'×':'—';
 const short=(s,n=12)=>typeof s==='string'?s.slice(0,n):'';
 const workerName=key=>'Worker '+(String(key).replace(/^worker_?/i,'').toUpperCase()||'?');
 const stateClass=state=>state==='verified_done'?'ok':String(state).startsWith('failed')?'bad':state==='running'?'warn':'';
 const chip=(text,kind,title)=>'<span class="chip'+(kind?' chip-'+kind:'')+'"'+(title?' title="'+escape(title)+'"':'')+'>'+escape(text)+'</span>';
 function when(iso){const t=Date.parse(iso);return Number.isNaN(t)?String(iso||''):new Date(t).toLocaleString([],{dateStyle:'medium',timeStyle:'short'});}
 function ciText(ci){return Array.isArray(ci)&&num(ci[0])&&num(ci[1])?ci[0].toFixed(2)+'–'+ci[1].toFixed(2)+'×':'';}
 const clock=ms=>{const t=Math.max(0,Math.floor(ms/1000));return Math.floor(t/60)+':'+String(t%60).padStart(2,'0');};
 const jobActive=job=>!!job&&(job.status==='queued'||job.status==='running');
 const list=(items,cls)=>Array.isArray(items)&&items.length?'<ul class="'+(cls||'plain-list')+'">'+items.map(i=>'<li>'+escape(i)+'</li>').join('')+'</ul>':'';
 export const repoShort=url=>String(url||'').replace(/^https:\/\/github\.com\//,'');
 const isObj=v=>v!==null&&typeof v==='object'&&!Array.isArray(v);
 const strs=v=>Array.isArray(v)?v.filter(x=>typeof x==='string'&&x.trim()).map(x=>x.trim()):[];
 const STEP_ICON={started:'◌',ok:'✓',failed:'✕'};
 const STEP_LABEL={analyze:'Analyze',repair:'Repair proposal',prepare:'Prepare sandbox',run:'Run',lessons:'Lessons learned',done:'Done'};
 const known=(table,key)=>Object.hasOwn(table,key);
 /** Collapses the autonomous job's progress events into one row per step: analyze, repair x n, prepare, run k, lessons, done. */
 export function timelineModel(steps){
  const rows=[];
  for(const e of Array.isArray(steps)?steps:[]){
   if(!isObj(e)||typeof e.step!=='string'||!known(STEP_ICON,e.status)) continue;
   const attempt=Number.isInteger(e.attempt)?e.attempt:null,key=e.step+':'+attempt;
   let row=null; // a finished step is closed by the matching ok/failed event; a later start of the same step is a new row
   for(let i=rows.length-1;i>=0&&!row;i--) if(rows[i].key===key&&rows[i].status==='started') row=rows[i];
   if(!row){
    const base=known(STEP_LABEL,e.step)?STEP_LABEL[e.step]:String(e.step).slice(0,30);
    const label=e.step==='repair'?(attempt?'Repair #'+attempt:base):e.step==='run'?(attempt?'Run '+attempt:base):attempt>1?base+' · try '+attempt:base;
    row={key,label,status:e.status,message:''};
    rows.push(row);
   }
   row.status=e.status;
   row.message=typeof e.message==='string'?e.message.slice(0,600):'';
  }
  return rows;
 }
 export const stepIcon=status=>known(STEP_ICON,status)?STEP_ICON[status]:'·';
 function timelineHtml(steps){
  const rows=timelineModel(steps);
  if(!rows.length) return '<p class="nfw-muted">Waiting for the first step…</p>';
  return '<ol class="timeline" aria-label="Autonomous run steps">'+rows.map(r=>'<li class="tl-'+r.status+'"><span class="tl-icon" role="img" aria-label="'+(r.status==='ok'?'done':r.status==='failed'?'failed':'in progress')+'">'+STEP_ICON[r.status]+'</span><span class="tl-body"><strong>'+escape(r.label)+'</strong>'+(r.message?'<span class="tl-msg">'+escape(r.message)+'</span>':'')+'</span></li>').join('')+'</ol>';
 }
 function attemptLine(a){
  if(typeof a==='string') return a.slice(0,300);
  if(!isObj(a)) return '';
  const parts=['attempt','step','kind','status','state','outcome','message','error','reason'].filter(k=>typeof a[k]==='string'||num(a[k])).map(k=>(['message','error','reason'].includes(k)?'':k+' ')+String(a[k]));
  return (parts.length?parts.join(' · '):JSON.stringify(a)).slice(0,300);
 }
 /** Normalizes the final JSON of `agent_team auto`. Nothing here is a speedup claim: speedups come only from the audited result. */
 export function autoOutcome(result){
  const r=isObj(result)?result:{};
  const status=typeof r.status==='string'?r.status:'';
  const report=typeof r.report==='string'?r.report.trim():'';
  const runs=(Array.isArray(r.runs)?r.runs:[]).filter(isObj).map((x,i)=>({label:'Run '+(i+1),state:typeof x.state==='string'&&x.state?x.state:'unknown',id:typeof x.run_id==='string'?x.run_id:''}));
  const reason=typeof r.reason==='string'&&r.reason.trim()?r.reason.trim():status==='not_feasible'?report:'';
  return {status,report:report===reason?'':report,reason,suggestions:strs(r.suggestions),assumptions:strs(r.assumptions),attempts:(Array.isArray(r.attempts)?r.attempts:[]).slice(0,20).map(attemptLine).filter(Boolean),runs,lastState:runs.length?runs[runs.length-1].state:'',cost:num(r.total_cost_usd)?r.total_cost_usd:null};
 }
 function assumptionsHtml(o){
  return o.assumptions.length?'<h3 class="mini-heading">Assumptions the agent made</h3>'+list(o.assumptions):'';
 }
 function outcomeHtml(view){
  const o=autoOutcome(view.analysis.outcome),repo=escape(repoShort(view.analysis.repoUrl));
  const cost=o.cost!==null?'<p class="nfw-muted">Spend reported by the agents: $'+o.cost.toFixed(2)+'</p>':'';
  const runs=o.runs.length?'<h3 class="mini-heading">Runs</h3><ul class="plain-list">'+o.runs.map(x=>'<li>'+escape(x.label)+' · '+escape(x.state)+'</li>').join('')+'</ul>':'';
  if(o.status==='not_feasible') return '<h2>Reading between the lines.</h2><div class="success-heading fail-heading"><span class="success-check">✕</span>Not feasible as asked</div><p class="stage-sub">'+escape(o.reason||'The analyst did not give a reason.')+'</p>'+(o.suggestions.length?'<h3 class="mini-heading">Possible optimizations / alternatives</h3>'+list(o.suggestions):'')+assumptionsHtml(o)+cost;
  if(o.status==='gave_up') return '<h2>No accepted result.</h2><div class="success-heading fail-heading"><span class="success-check">✕</span>The agents gave up on '+repo+'</div>'+(o.report?'<p class="stage-sub">'+escape(o.report)+'</p>':'')+(o.attempts.length?'<h3 class="mini-heading">Attempts</h3>'+list(o.attempts):'')+runs+(o.lastState?'<div class="end-state nfw-mono">Last run state: '+escape(o.lastState)+'</div>':'')+'<p class="stage-sub">No speedup is claimed.</p>'+assumptionsHtml(o)+cost;
  if(o.status==='verified_done') return '<h2>Result</h2><div class="success-heading fail-heading"><span class="success-check">!</span>Finished, but the audited result could not be shown</div><p class="stage-sub">The agents report verified_done, but no run ledger could be opened for it, so nothing is claimed here.</p>'+assumptionsHtml(o)+cost;
  return '<h2>Result</h2><div class="success-heading fail-heading"><span class="success-check">✕</span>Finished without a recognized outcome</div><p class="stage-sub">The autonomous run ended with status “'+escape(o.status||'unknown')+'”. No speedup is claimed.</p>'+(o.report?'<p class="stage-sub">'+escape(o.report)+'</p>':'')+assumptionsHtml(o)+cost;
 }
 function autoTimeline(view){
  const a=view.analysis;
  return a&&a.auto&&view.job?'<h3 class="mini-heading">Autonomous steps</h3>'+timelineHtml(view.job.steps):'';
 }
 function autoScene(view){
  const a=view.analysis,job=view.job,repo=escape(repoShort(a.repoUrl)),head='<h2>Working autonomously.</h2>';
  if(jobActive(job)){
   const rows=timelineModel(job.steps),current=rows.length?rows[rows.length-1]:null;
   return head+'<p class="stage-sub">The agents are analyzing '+repo+', repairing their own proposal if the sandbox rejects it, running, and re-planning within the configured limits. No questions will be asked.</p><div class="job-status" role="status"><span class="status-dot"></span><span>'+escape(current?current.label+(current.message?' · '+current.message:''):'Starting the autonomous run')+'<span class="flow-dots"> ···</span></span><span class="nfw-mono" aria-hidden="true" data-elapsed>'+clock(Date.now()-job.startedAt)+'</span></div>'+timelineHtml(job.steps)+'<div class="result-actions"><button type="button" class="nfw-secondary" data-act="cancel-job"'+(job.cancelling?' disabled':'')+'>'+(job.cancelling?'Cancelling…':'Cancel')+'</button></div><div class="scene-footer">Live job · spending is capped by auto.max_total_cost_usd in nofreewins.config.json</div>';
  }
  if(a.outcome) return outcomeHtml(view)+(job&&job.steps.length?'<h3 class="mini-heading">Autonomous steps</h3>'+timelineHtml(job.steps):'');
  const last=a.last,steps=job&&job.steps.length?'<h3 class="mini-heading">Autonomous steps</h3>'+timelineHtml(job.steps):'';
  if(last&&last.type==='failed') return '<h2>The autonomous run failed.</h2><div class="success-heading fail-heading"><span class="success-check">✕</span>Failed</div><div class="end-state nfw-mono">'+escape(last.error||'The job failed.')+'</div><p class="stage-sub">Nothing was claimed. Use the chat to start over.</p>'+steps;
  if(last&&last.type==='cancelled') return '<h2>Cancelled.</h2><p class="stage-sub">The autonomous run was cancelled. Nothing was claimed.</p>'+steps;
  return head+'<p class="stage-sub">Waiting for the first step.</p>';
 }
 function waiting(run){return run.status==='streaming'?'Waiting for events…':'Not reached in this run.';}
 const liveNotes=[
  'Choose a recorded run to replay, or paste a repo to analyze.',
  'Planner output from this run’s live ledger: the models and strategies it uses.',
  'Worker events as they arrive from the live run.',
  'Correctness screen and independent verifier verdict as they arrive.',
  'Timing measurement. Numbers are only claimed once the audited result is in.',
  'The zoo is a separate development defense suite. It is not part of this run.',
  'The outcome appears only after the run ends and its result is audited.',
 ];
 const realNotes=[
  'Choose a recorded run to replay. A replay is a recording, not a new run.',
  'Recorded planner output: the models and strategies this run used.',
  'Recorded worker output: each worker’s strategy, candidate and model attempts.',
  'Recorded correctness screen and independent verifier verdict for each candidate.',
  'Recorded timing measurement for the candidate that reached acceptance.',
  'The zoo is a separate development defense suite. It is not part of this run.',
  'Recorded outcome for this run. A replay does not change it.',
 ];
 function top(phase,view){
  const renderer='<button type="button" data-renderer aria-label="Toggle pipeline view">'+(view.options.renderer==='ASCII'?'Line graph':'ASCII')+'</button>';
  let controls='<button type="button" data-play class="cursor-interaction">'+(view.playing?'Pause preview':'Play preview')+'</button>'+renderer;
  if(view.run){
   const streaming=view.run.status==='streaming',isLive=!!view.run.live;
   const cancel=isLive&&jobActive(view.job)&&(view.job.kind==='approve'||view.job.kind==='auto')?'<button type="button" data-act="cancel-job" class="cursor-interaction"'+(view.job.cancelling?' disabled':'')+'>'+(view.job.cancelling?'Cancelling…':view.job.kind==='auto'?'Cancel autonomous run':'Cancel run')+'</button>':'';
   controls=cancel+(isLive?'':'<span class="speed-toggle" role="group" aria-label="Replay speed">'+[['1','1×'],['8','8×'],['instant','Instant']].map(s=>'<button type="button" data-speed="'+s[0]+'" aria-pressed="'+(String(view.speed)===s[0])+'">'+s[1]+'</button>').join('')+'</span>')+'<button type="button" data-replay class="cursor-interaction">'+(streaming?(isLive?'Stop following':'Stop replay'):'Replay run')+'</button>'+renderer;
  } else if(view.analysis) controls='';
  const lead=view.run?(view.run.live?'Live run / ':'Replay / '):view.analysis?'Analyst / ':'Preview / ';
  return '<div class="stage-titlebar"><span class="eyebrow">'+(lead+phaseNames[phase])+'</span><div class="scene-controls">'+controls+'</div></div>';
 }
 function runItem(view,r){
  const official=r.kind==='official',featured=official&&r.verified===true;
  const badge=official?(featured?'Official · verified':'Official'):String(r.kind||'run').replace(/^./,c=>c.toUpperCase());
  const pending=r.verified==null?'recorded state; verified when opened':'';
  return '<li><button type="button" class="run-item'+(featured?' featured':'')+'" data-run="'+escape(r.runId)+'"'+(view.run&&view.run.id===r.runId?' aria-current="true"':'')+'><span class="run-main"><strong>'+escape(r.label||r.runId)+'</strong><span class="badge'+(featured?' badge-official':'')+'">'+escape(badge)+'</span>'+chip(r.state||'unknown',stateClass(r.state),pending)+'</span><span class="run-meta"><span class="nfw-mono">'+escape(r.runId)+'</span><span>'+escape(when(r.startedUtc))+'</span></span>'+(pending?'<span class="run-meta">'+pending+'</span>':'')+'</button></li>';
 }
 function startScene(view){
  if(view.api==='up'){
   const launch=view.launch===true;
   const runs=view.runsError?'<p class="run-empty" role="status">'+escape(view.runsError)+'</p>':view.runs.length?'<ul class="run-list" aria-label="Recorded runs">'+view.runs.map(r=>runItem(view,r)).join('')+'</ul>':'<p class="run-empty">No recorded runs found.</p>';
   if(launch){
    const auto=view.mode!=='manual';
    const toggle='<div class="mode-toggle" role="group" aria-label="How to run"><button type="button" data-mode="auto" aria-pressed="'+auto+'">Autonomous (default)</button><button type="button" data-mode="manual" aria-pressed="'+!auto+'">Step-by-step</button></div>';
    const sub=auto?'Paste a public repo link below and say what should be faster. After one spending confirmation the agents analyze it, fix their own proposal if the sandbox rejects it, run, and re-plan within the configured limits, with no further questions or approvals. Repo code only ever runs in the Daytona sandbox.':'Paste a public repo link below and say what should be faster. An analyst agent reads the repo and proposes a contract. Nothing runs until you approve it, and repo code only ever runs in the Daytona sandbox.';
    return '<div class="input-scene"><div class="big-symbol">nfw↗</div><h2>Optimize a GitHub repo.</h2>'+toggle+'<p class="stage-sub">'+sub+'</p><h3 class="run-heading">Recorded runs</h3>'+runs+'<button type="button" class="nfw-secondary" data-example>Play example · scripted demo ↗</button><div class="supported">Launch enabled · '+(auto?'one confirmation, then fully autonomous':'analysis uses Crusoe credits')+' · replays are recordings, not new runs</div></div>';
   }
   return '<div class="input-scene"><div class="big-symbol">nfw↗</div><h2>Replay a recorded run.</h2><p class="stage-sub">Pick a run recorded on this machine and watch it move through the pipeline. A replay is a recording, not a new run.</p><h3 class="run-heading">Recorded runs</h3>'+runs+'<button type="button" class="nfw-secondary" data-example>Play example · scripted demo ↗</button><div class="supported">Replay only · links are not fetched and nothing is launched</div></div>';
  }
  return '<div class="input-scene"><div class="big-symbol">nfw↗</div><h2>Give your code a fair race.</h2><p class="stage-sub">Paste a link. Tell me what needs to change.<br>Preview the pipeline and a recorded example result.</p><button type="button" class="nfw-secondary" data-example>Play example ↗</button><div class="supported">UI skeleton · code links are only stored in this page’s memory</div></div>';
 }
 function contractSummary(r){
  const p=r&&r.proposal&&typeof r.proposal==='object'?r.proposal:null;
  if(!p) return '';
  const sz=p.sizes&&typeof p.sizes==='object'?p.sizes:{};
  const sizes=(num(sz.timing_size)?'timing '+sz.timing_size:'')+(Array.isArray(sz.correctness_sizes)&&sz.correctness_sizes.length?(num(sz.timing_size)?' · ':'')+'correctness '+sz.correctness_sizes.filter(num).join(', '):'');
  return '<div class="file-focus"><div class="nfw-mono">'+escape(p.target||'target not given')+'<small>Proposed target</small></div><span>Awaiting your decision</span></div>'+(p.description?'<p class="stage-sub">'+escape(p.description)+'</p>':'')+(Array.isArray(p.hints)&&p.hints.length?'<h3 class="mini-heading">Planned optimizations</h3>'+list(p.hints):'')+(Array.isArray(p.risks)&&p.risks.length?'<h3 class="mini-heading">Risks</h3>'+list(p.risks):'')+(sizes?'<p class="nfw-muted">Sizes · '+escape(sizes)+'</p>':'');
 }
 function analystScene(view){
  if(view.analysis.auto) return autoScene(view);
  const a=view.analysis,job=view.job,repo=escape(repoShort(a.repoUrl));
  const head='<h2>Reading between the lines.</h2>';
  if(jobActive(job)){
   const approve=job.kind==='approve';
   const label=approve?'Preparing sandbox: generating hidden cases and recording the original function’s outputs':job.kind==='answer'?'The analyst is revising its proposal with your answer':'The analyst agent is reading the repo';
   return head+'<p class="stage-sub">'+(approve?'Approved. Hidden cases are generated and the original function’s outputs are recorded inside the Daytona sandbox. This usually takes about 2 minutes.':'The analyst reads '+repo+' and your prompt. Nothing from the repo runs on your machine.')+'</p><div class="job-status" role="status"><span class="status-dot"></span><span>'+escape(label)+'<span class="flow-dots"> ···</span></span><span class="nfw-mono" aria-hidden="true" data-elapsed>'+clock(Date.now()-job.startedAt)+'</span></div>'+(job.phase?'<p class="nfw-muted">Phase · '+escape(String(job.phase).slice(0,120))+'</p>':'')+'<div class="result-actions"><button type="button" class="nfw-secondary" data-act="cancel-job"'+(job.cancelling?' disabled':'')+'>'+(job.cancelling?'Cancelling…':'Cancel')+'</button></div><div class="scene-footer">Live job · uses Crusoe credits within the configured cap</div>';
  }
  const last=a.last;
  if(last&&last.type==='failed') return head+'<div class="success-heading fail-heading"><span class="success-check">✕</span>'+(last.kind==='approve'?'Prepare failed':'Analysis failed')+'</div><div class="end-state nfw-mono">'+escape(last.error||'The job failed.')+'</div><p class="stage-sub">Nothing was claimed. Use the chat to retry'+(last.kind==='approve'?' or ask the analyst to fix it':'')+'.</p>';
  if(last&&last.type==='cancelled') return head+'<p class="stage-sub">The job was cancelled. Nothing was claimed.</p>';
  const r=last&&last.type==='result'?last.result:null;
  if(!r) return head+'<p class="stage-sub">Waiting for the analyst.</p>';
  if(r.status==='proposal') return head+'<p class="stage-sub">The analyst proposed a contract for '+repo+'. Approve it or suggest changes in the chat.</p>'+contractSummary(r);
  if(r.status==='not_feasible') return head+'<div class="success-heading fail-heading"><span class="success-check">✕</span>Not feasible as asked</div><p class="stage-sub">'+escape(r.reason||'The analyst did not give a reason.')+'</p>'+(Array.isArray(r.suggestions)&&r.suggestions.length?'<h3 class="mini-heading">Possible optimizations / alternatives</h3>'+list(r.suggestions):'');
  return head+'<p class="stage-sub">'+escape(r.reason||'The analyst needs more information. Answer in the chat.')+'</p>'+(Array.isArray(r.questions)&&r.questions.length?'<h3 class="mini-heading">Questions</h3><ol class="plain-list">'+r.questions.map(q=>'<li>'+escape(q)+'</li>').join('')+'</ol>':'');
 }
 export function analysisNote(view){
  const a=view.analysis,job=view.job;
  if(a.auto){
   if(jobActive(job)) return 'Running autonomously. No questions will be asked; you can cancel at any time.';
   if(a.outcome){const st=autoOutcome(a.outcome).status;return st==='verified_done'?'The agents report a verified result. Only the audited result counts.':st==='not_feasible'?'The analyst says this is not feasible as asked. See the reason and alternatives.':'The agents gave up. No speedup is claimed.';}
   return a.last&&a.last.type==='cancelled'?'The autonomous run was cancelled.':a.last?'The autonomous run failed. Details are in the chat.':'Waiting for the first step.';
  }
  if(jobActive(job)) return job.kind==='approve'?'Approved. Preparing the sandbox before the live run starts.':'The analyst agent is working. You can answer once it responds.';
  const last=a.last;
  if(!last) return 'Waiting for the analyst.';
  if(last.type==='failed') return 'The job failed. Details are in the chat.';
  if(last.type==='cancelled') return 'The job was cancelled.';
  const s=last.result&&last.result.status;
  return s==='proposal'?'The analyst proposed a contract. Nothing runs until you approve.':s==='not_feasible'?'The analyst says this is not feasible as asked. See the alternatives.':'The analyst has questions. Your next message is sent as the answer.';
 }
 function notReached(view){
  return '<h2>Not reached yet.</h2><p class="stage-sub">No run has started for '+escape(repoShort(view.analysis.repoUrl))+'. These stages show real events once '+(view.analysis.auto?'the agents start a run.':'you approve a proposal and the live run begins.')+'</p><div class="scene-footer">Nothing is shown here until the run produces it</div>';
 }
 function analyzeReal(view){
  const run=view.run,d=run.data,roles=d.models&&typeof d.models==='object'?Object.keys(d.models):[];
  const models=roles.map(k=>{const m=d.models[k]||{};return '<div class="check-line stack"><span>'+escape(k)+'</span><span class="nfw-mono">'+escape(m.primary||'—')+(m.fallback?' → '+escape(m.fallback):'')+'</span></div>';}).join('');
  const strategies=(d.strategyIds||[]).map((id,i)=>'<div class="check-line stack"><span>Strategy '+(i+1)+'</span><span class="nfw-mono">'+escape(id)+'</span></div>').join('');
  const limits=(num(d.maxRounds)||num(d.deadline))?'<div class="check-line"><span>Limits</span><span>'+(num(d.maxRounds)?d.maxRounds+' rounds':'')+(num(d.maxRounds)&&num(d.deadline)?' · ':'')+(num(d.deadline)?d.deadline+' s deadline':'')+'</span></div>':'';
  const graph='<div class="file-focus"><div class="nfw-mono">'+escape(d.sessionId||run.meta.sessionId||run.id)+'<small>Session'+(run.meta.taskId?' · task '+escape(run.meta.taskId):'')+'</small></div><span>Recorded run</span></div>';
  const text=ascii(['session  '+(d.sessionId||run.meta.sessionId||run.id),'task     '+(run.meta.taskId||'—'),'',...(d.strategyIds||[]).map((id,i)=>'strategy '+(i+1)+'  '+id)],REAL_ASCII);
  return '<h2>Reading between the lines.</h2><p class="stage-sub">'+(run.live?'Planner output from this run’s live ledger.':'Recorded planner output. No source was fetched for this replay.')+'</p>'+(view.options.renderer==='ASCII'?text:graph)+'<div class="check-list">'+(models?'<h3>Models · primary → fallback</h3>'+models:'')+(strategies?'<h3>Strategies planned</h3>'+strategies:'')+limits+(models||strategies?'':'<div class="check-line"><span>'+waiting(run)+'</span></div>')+'</div>';
 }

 function workerScene(view){
  if(view.run) return workerReal(view);
  const graph='<div class="route-in nfw-mono">Analysis complete</div><div class="workers"><div class="worker"><div class="worker-top"><span class="worker-avatar">A</span><div><h3>Worker A</h3><small>Isolated sandbox A</small></div></div><pre class="worker-code nfw-mono">regex.fullmatch(line)\naggregate in one pass</pre><div class="worker-status"><span class="status-dot"></span>Building candidate <span class="flow-dots">···</span></div></div><div class="worker"><div class="worker-top"><span class="worker-avatar">B</span><div><h3>Worker B</h3><small>Isolated sandbox B</small></div></div><pre class="worker-code nfw-mono">validate each token\nreduce repeated work</pre><div class="worker-status"><span class="status-dot"></span>Building candidate <span class="flow-dots">···</span></div></div></div><div class="route-out nfw-mono">└──────────┬──────────┘<br>Independent review · next</div>';
  const text=ascii(['          [analysis ✓]','               │','       ┌───────┴───────┐','   [worker A]     [worker B]','    sandbox A      sandbox B','    regex pass     token parser','    ◉ building     ◉ building','       └───────┬───────┘','          [verifier] next']);
  return '<h2>Two ways to get faster.</h2><p class="stage-sub">Different strategies. The same correctness contract.</p>'+(view.options.renderer==='ASCII'?text:graph)+'<div class="scene-footer"><span class="status-dot"></span>Illustrative sandbox scene · chat stays available</div>';
 }
 function workerReal(view){
  const run=view.run,d=run.data,keys=Object.keys(d.workers);
  const faults=d.faults.length?'<p class="fault-note" role="note">Fault drill · '+d.faults.map(escape).join(' · ')+'</p>':'';
  const cards=keys.map(k=>{
   const w=d.workers[k],attempts=(w.attempts||[]).map(a=>chip((a.kind||'attempt')+' · '+(a.outcome||'unknown'),a.outcome==='ok'?'ok':'bad',a.model_id)).join('');
   return '<div class="worker"><div class="worker-top"><span class="worker-avatar">'+escape(workerName(k).slice(-1))+'</span><div><h3>'+escape(workerName(k))+'</h3><small>'+(w.strategyId?escape(w.strategyId):'Strategy not recorded')+'</small></div></div><pre class="worker-code nfw-mono">'+(w.candidateId?'candidate '+escape(w.candidateId)+(w.sha?'\nsha256 '+escape(short(w.sha,16))+'…':''):'no candidate yet')+'</pre>'+(attempts?'<div class="chips" aria-label="Model attempts">'+attempts+'</div>':'')+'<div class="worker-status"><span class="status-dot"></span>'+(w.candidateId?'Candidate created':run.status==='streaming'?'Building candidate <span class="flow-dots">···</span>':'No candidate recorded')+'</div></div>';
  }).join('');
  const graph=keys.length?'<div class="route-in nfw-mono">Analysis complete</div><div class="workers">'+cards+'</div><div class="route-out nfw-mono">└──────────┬──────────┘<br>Independent review · next</div>':'<p class="run-empty">'+waiting(run)+'</p>';
  const text=ascii(keys.length?keys.flatMap(k=>{const w=d.workers[k];return [workerName(k)+'  '+(w.strategyId||'—'),'  candidate '+(w.candidateId||'—'),'  attempts  '+((w.attempts||[]).map(a=>a.kind+':'+a.outcome).join(' → ')||'—'),''];}):[waiting(run)],REAL_ASCII);
  return '<h2>Two ways to get faster.</h2><p class="stage-sub">Recorded workers: different strategies, the same correctness contract.</p>'+faults+(view.options.renderer==='ASCII'?text:graph)+'<div class="scene-footer"><span class="status-dot"></span>Recorded worker events · chat stays available</div>';
 }
 function verifyScene(view){
  const run=view.run;
  if(!run) return '<h2>A second set of eyes.</h2><p class="stage-sub">An independent verifier reviews the selected candidate.</p><div class="reviewer"><span class="worker-avatar">V</span><div><h3>Verifier</h3><span class="nfw-muted">Reviewing candidate A</span></div></div>'+(view.options.renderer==='ASCII'?ascii(['[candidate A] → [verifier] → [timing]',' source bound     ◉ reviewing    next']):'<div class="file-focus"><span class="nfw-mono">optimized.py</span><span class="nfw-muted">Candidate A · hash bound</span></div>')+'<div class="check-list"><div class="check-line"><span>Input validation preserved</span><span>✓</span></div><div class="check-line"><span>General implementation</span><span>✓</span></div><div class="check-line"><span>Evidence bound to this code</span><span>✓</span></div></div><div class="scene-footer">Approval leads to independent performance testing.</div>';
  const d=run.data,ids=Object.keys(d.candidates);
  const rows=ids.map(id=>{
   const c=d.candidates[id],sc=d.screens[id],vd=d.verdicts[id];
   const screen=sc?chip('screen · '+(sc.status||'unknown'),'')+(sc.correctness_passed===true?chip('correctness passed','ok'):sc.correctness_passed===false?chip('correctness failed','bad'):chip('correctness not recorded',''))+chip(num(sc.screen_median_speedup)?fx(sc.screen_median_speedup)+' · ranking only':'no screen timing',''):chip('no screen result','');
   const verdict=vd?(chip(vd.approved===true?'verifier approved':vd.approved===false?'verifier rejected':'verifier verdict unclear',vd.approved===true?'ok':vd.approved===false?'bad':'')+'<span class="nfw-muted">'+(c.sha?' hash-bound to '+escape(short(c.sha,16))+'…':' candidate hash not recorded')+'</span>'+(vd.mismatch?'<span class="nfw-muted"> · mismatch: '+escape(typeof vd.mismatch==='string'?vd.mismatch:JSON.stringify(vd.mismatch))+'</span>':'')):'<span class="nfw-muted">'+(run.status==='streaming'?'Verifier verdict pending…':'No verifier verdict recorded.')+'</span>';
   return '<div class="candidate"><div class="nfw-mono">'+escape(id)+'<small>'+escape((c.worker?workerName(c.worker):'Candidate')+(c.strategyId?' · '+c.strategyId:''))+'</small></div><div class="chips">'+screen+'</div><div class="verdict-line"><span class="worker-avatar">V</span><div>'+verdict+'</div></div></div>';
  }).join('');
  const text=ascii(ids.length?ids.flatMap(id=>{const sc=d.screens[id],vd=d.verdicts[id];return [id,'  screen    '+(sc?sc.status+' · correctness '+(sc.correctness_passed===true?'passed':sc.correctness_passed===false?'failed':'n/a')+(num(sc.screen_median_speedup)?' · '+fx(sc.screen_median_speedup)+' (ranking only)':' · no screen timing'):'—'),'  verifier  '+(vd?(vd.approved?'approved':'rejected'):'—'),''];}):[waiting(run)],REAL_ASCII);
  return '<h2>A second set of eyes.</h2><p class="stage-sub">Recorded screen results and the independent verifier verdict. The screen speedup is for ranking only.</p>'+(ids.length?(view.options.renderer==='ASCII'?text:'<div class="candidates">'+rows+'</div>'):'<p class="run-empty">'+waiting(run)+'</p>')+'<div class="scene-footer">Approval leads to independent performance testing.</div>';
 }
 function raceScene(view){
  if(view.run) return raceReal(view);
  const graph='<div class="race-lane"><div class="race-label"><span>Original</span><span>baseline</span></div><div class="race-track"><span class="runner">›</span></div></div><div class="race-lane"><div class="race-label"><span>Optimized</span><span>candidate A</span></div><div class="race-track"><span class="runner fast">››</span></div></div>';
  const text=ascii(['PAIR 18 / 30','', 'original   ━━━━━━━▶············┃', 'optimized  ━━━━━━━━━━━━━━━━━▶·┃', '', 'outputs    ✓ match','same input / same machine']);
  return '<h2>Let the code race.</h2><p class="stage-sub">30 matched pairs. Every output still has to match.</p>'+(view.options.renderer==='ASCII'?text:graph)+'<div class="race-readout"><span>Pair 18 / 30 · illustrative replay</span><span>Outputs matching ✓</span></div><div class="scene-footer">A fast screen alone doesn’t finish the run.</div>';
 }
 function raceReal(view){
  const run=view.run,list=run.data.acceptances,a=list.find(x=>x.accepted===true)||list[list.length-1],r=run.result;
  if(!a) return '<h2>Let the code race.</h2><p class="stage-sub">Matched timing pairs against the original.</p><p class="run-empty">'+(run.status==='streaming'?'Waiting for the timing measurement…':'No timing race was recorded in this run.')+'</p>';
  // Event numbers are not chain-verified: show them only once /result confirms acceptance and the receipt audit.
  const ok=!!r&&r.accepted===true&&r.receiptAuditValid===true,settled=run.status!=='streaming'&&run.resultState!=='loading'&&(!!r||run.resultState==='error');
  const ci=ok?ciText(r.confidenceInterval95)||'—':'—';
  const note=ok?'':settled?(a.accepted===true?'Recorded, but receipt audit failed':'Recorded acceptance event · not accepted'):'Recorded acceptance event · audit pending';
  const graph='<div class="race-lane"><div class="race-label"><span>Original</span><span>baseline</span></div><div class="race-track"><span class="runner">›</span></div></div><div class="race-lane"><div class="race-label"><span>Optimized</span><span>'+escape(a.candidate_id||'candidate')+'</span></div><div class="race-track"><span class="runner fast">››</span></div></div>';
  const text=ascii(['candidate  '+(a.candidate_id||'—'),...(ok?['pairs      '+(num(r.timingPairs)?r.timingPairs:'—'),'median     '+fx(r.medianSpeedup),'95% CI     '+ci,'status     accepted · receipt audited']:['status     '+note])],REAL_ASCII);
  const readout=ok?'<span>'+(num(r.timingPairs)?escape(r.timingPairs)+' pairs · ':'')+'median '+fx(r.medianSpeedup)+' · 95% CI '+ci+'</span><span>Accepted ✓</span>':'<span>'+escape(note)+'</span>';
  return '<h2>Let the code race.</h2><p class="stage-sub">'+(ok&&num(r.timingPairs)?escape(r.timingPairs)+' matched pairs. ':'Matched pairs. ')+'Every output still has to match.</p>'+(view.options.renderer==='ASCII'?text:graph)+'<div class="race-readout">'+readout+'</div><div class="scene-footer">Recorded measurement'+(typeof a.origin==='string'?' · origin '+escape(a.origin):'')+(ok&&r.receiptSha256?' · receipt '+escape(short(r.receiptSha256,16))+'…':'')+' · the race animation is decorative</div>';
 }
 function zooScene(view){
  const animals=[['🦊','Clock tamper'],['🦝','Hardcoding'],['🐇','Early exit'],['🦥','Lazy return']];
  const graph='<div class="zoo">'+animals.map(a=>'<div class="animal"><span class="creature" aria-hidden="true">'+a[0]+'</span><span class="animal-name">'+a[1]+'</span><span class="animal-verdict">Rejected ✓</span></div>').join('')+'</div>';
  const text=ascii(['🦊 clock tamper ........ rejected','🦝 hardcoding .......... rejected','🐇 early exit .......... rejected','🦥 lazy return ......... rejected','','🐢 honest controls .... 4 passed']);
  if(view.run) return '<h2>A quick visit to the zoo.</h2><p class="stage-sub">Known bad implementations test the defenses. Honest controls must pass too. This suite is never part of the run you are replaying.</p>'+(view.options.renderer==='ASCII'?text:graph)+'<div class="zoo-score"><span>Separate defense suite (development pilot): 4/4 planted attacks rejected, 4/4 honest controls passed</span></div><div class="scene-footer">Not a result of this run</div>';
  return '<h2>A quick visit to the zoo.</h2><p class="stage-sub">Known bad implementations test the defenses. Honest controls must pass too.</p>'+(view.options.renderer==='ASCII'?text:graph)+'<div class="zoo-score"><span>4 / 4 planted cases rejected</span><span>4 / 4 honest controls passed</span></div><div class="scene-footer">Separate development defense suite</div>';
 }
 function resultScene(view){
  const a=view.analysis;
  if(a&&a.auto&&a.outcome){
   const o=autoOutcome(a.outcome);
   // Only the audited /result may show a speedup, so verified_done with a run keeps the unchanged honesty-gated scene.
   if(o.status==='verified_done'&&view.run) return resultReal(view)+assumptionsHtml(o);
   return outcomeHtml(view);
  }
  if(view.run) return resultReal(view);
  return '<div class="success-heading"><span class="success-check">✓</span>Recorded verified example</div><div class="speed">3.75×</div><div class="speed-label">faster, with matching outputs</div><div class="interval">95% confidence interval <strong>3.52–3.81×</strong></div><div class="result-checks"><span>2,000 hidden cases</span><span>30 timing pairs</span><span>Sandboxes deleted</span></div><div class="result-actions"><button type="button" class="nfw-primary" data-download="code">↓ Example optimized.py</button><span class="nfw-muted">From the recorded development run</span></div><details><summary class="cursor-interaction">See code &amp; what changed</summary><p class="nfw-muted">Repeated per-user scans became one-pass aggregation. Exact line validation and the output schema are preserved.</p><pre class="code-preview nfw-mono">'+escape(recordedCode)+'</pre></details><details><summary class="cursor-interaction">View proof · receipt audited ✓</summary><div class="proof-data">MEASURED DEVELOPMENT RESULT<br>Median: 3.7468× · pairs: 30<br>95% CI: [3.5225×, 3.8081×]<br>Receipt SHA-256:<br>'+proof.receipt_sha256+'<br><br>Source SHA-256:<br>'+proof.source_sha256+'</div><button type="button" class="nfw-secondary proof-download" data-download="proof">↓ Proof summary</button></details><div class="scene-footer">Recorded development example · not a new benchmark</div>';
 }
 function resultReal(view){
  const run=view.run,r=run.result,t=run.terminal;
  const label=(r&&r.label)||run.data.label||run.meta.label;
  const labelLine=label?'<p class="run-label">'+escape(label)+'</p>':'';
  const foot='<div class="scene-footer">'+(run.live?'Run '+escape(run.id)+' · launched from this page · claims come only from the audited result':'Recorded run '+escape(run.id)+' · a replay is not a new run or a new benchmark')+'</div>';
  if(!r&&run.resultState!=='error'&&!t) return '<h2>Result</h2>'+labelLine+'<p class="run-empty">'+(run.status==='streaming'||run.resultState==='loading'?'Waiting for the run to finish…':run.live?'No result was recorded for this run.':'No result was recorded for this replay.')+'</p>'+foot;
  if(!(r&&r.accepted===true)&&(run.status==='streaming'||run.resultState==='loading')) return '<h2>Result</h2>'+labelLine+'<p class="run-empty">Waiting for the run to finish…</p>'+foot;
  if(r&&r.accepted===true){
   const audited=r.receiptAuditValid===true;
   const links=[['optimizedArtifactId','↓ Optimized code','nfw-primary'],['originalArtifactId','↓ Original code','nfw-secondary'],['proofArtifactId','↓ Proof','nfw-secondary']].map(x=>{try{return '<a class="nfw-link '+x[2]+'" href="'+escape(view.artifactUrl(r[x[0]]))+'" download>'+x[1]+'</a>';}catch{return '';}}).join('');
   const checks=(num(r.hiddenCases)?'<span>'+r.hiddenCases.toLocaleString()+' hidden cases</span>':'')+(num(r.timingPairs)?'<span>'+r.timingPairs+' timing pairs</span>':'')+(r.sandboxDeleted===true?'<span>sandbox deleted</span>':'')+(audited?'<span>receipt audited ✓</span>':'');
   const ci=ciText(r.confidenceInterval95);
   return '<div class="success-heading"><span class="success-check">'+(audited?'✓':'!')+'</span>'+(audited?(run.live?'Verified result':'Recorded verified result'):'Accepted · receipt audit not confirmed')+'</div>'+labelLine+'<div class="speed">'+fx(r.medianSpeedup)+'</div><div class="speed-label">median speedup, with matching outputs</div>'+(ci?'<div class="interval">95% confidence interval <strong>'+ci+'</strong></div>':'')+'<div class="result-checks">'+checks+'</div>'+(audited?'':'<p class="run-warn" role="note">The receipt audit did not validate for this run, so treat this result as unverified.</p>')+(r.ledgerVerified===false?'<p class="run-warn" role="note">The run ledger hash chain did not verify.</p>':'')+'<div class="result-actions">'+links+'</div><details><summary class="cursor-interaction">View proof'+(audited?' · receipt audited ✓':'')+'</summary><div class="proof-data">'+[['Receipt SHA-256',r.receiptSha256],['Source SHA-256',r.sourceSha256],['Candidate',r.candidateId],['Session',r.sessionId],['Task',r.taskId],['Ledger final SHA-256',r.ledgerFinalSha256]].filter(x=>x[1]).map(x=>escape(x[0])+':<br>'+escape(x[1])).join('<br><br>')+(r.ledgerVerified===true?'<br><br>Ledger hash chain verified ✓':'')+'</div></details>'+foot;
  }
  const state=(r&&r.state)||(t&&t.state)||run.endState||run.meta.state,reason=t&&t.reason;
  const broken=!!r&&(r.state==='ledger_invalid'||r.ledgerVerified===false);
  return '<div class="success-heading fail-heading"><span class="success-check">✕</span>No accepted result</div>'+labelLine+'<div class="end-state nfw-mono">'+(broken?'Ledger hash chain did not verify':escape(state||'unknown')+(reason?': '+escape(reason):''))+'</div><p class="stage-sub">This run did not produce an accepted speedup, so no speedup is claimed.'+(run.resultState==='error'&&!r?' The result details could not be loaded.':'')+'</p>'+foot;
 }
 export function statusText(view){
  const run=view.run;
  if(!run&&view.analysis){
   const job=view.job;
   if(jobActive(job)) return (job.kind==='approve'?'Preparing sandbox':job.kind==='auto'?'Running autonomously':'Analyst agent working')+' · '+repoShort(view.analysis.repoUrl);
   return 'Analyst · '+repoShort(view.analysis.repoUrl)+' · '+(view.analysis.last?analysisNote(view):'waiting');
  }
  if(!run&&view.api==='up'&&!view.playing&&view.phase===0) return view.launch===true?'Connected · launch enabled · paste a repo below or choose a recorded run':'Connected · choose a recorded run to replay';
  if(!run) return view.playing?'Playing local demo · '+phaseNames[view.phase]:'Local demo · '+phaseNames[view.phase];
  const speed=view.speed==='instant'?'instant':view.speed+'× speed';
  if(run.live){
   if(run.status==='streaming') return 'Live run '+run.id+' · following the run ledger';
   if(run.status==='stopped') return 'Stopped following live run '+run.id;
   if(run.status==='error') return 'Live stream interrupted · '+run.id+(run.error?' · '+run.error:'');
   return 'Run finished · '+run.id+' · launched from this page';
  }
  if(run.status==='streaming') return 'Replaying '+run.id+' · '+speed;
  if(run.status==='stopped') return 'Replay stopped · '+run.id+' · recorded run, not a new run';
  if(run.status==='error') return 'Replay interrupted · '+run.id+(run.error?' · '+run.error:'');
  return 'Replay finished · '+run.id+' · recorded run, not a new run';
 }
 function navButton(view,i,label){
  const run=view.run,phase=view.phase,stage=['start','analyze','workers','verify','race','zoo','result'][i];
  const failed=!!run&&i!==phase&&run.failed.has(stage);
  const done=run?i!==phase&&!failed&&(i===0||run.seen.has(stage)):i<phase;
  return '<button type="button" class="cursor-interaction'+(done?' done':'')+(failed?' failed':'')+'" data-phase="'+i+'"'+(i===phase?' aria-current="step"':'')+'><span class="step-symbol">'+(failed?'✕':done?'✓':i===phase?'●':'·')+'</span>'+label+'</button>';
 }
 export function render(view,animate){
  const app=view.element,phase=view.phase,run=view.run;
  const active=document.activeElement,focusKey=active&&app.contains(active)?['data-phase','data-speed','data-replay','data-play','data-renderer','data-run','data-act','data-mode'].map(a=>active.hasAttribute(a)?'['+a+'="'+CSS.escape(active.getAttribute(a))+'"]':'').find(Boolean):'';
  app.dataset.phase=String(phase);
  const nav=app.querySelector('.sequence');
  nav.innerHTML=phaseNames.map((label,i)=>navButton(view,i,label)).join('');
  let content='';
  if(phase===0) content=startScene(view);
  if(phase===1) content=run?analyzeReal(view)+autoTimeline(view):view.analysis?analystScene(view):'<h2>Reading between the lines.</h2><p class="stage-sub">Example source only. Your pasted link has not been fetched.</p><div class="file-focus"><div class="nfw-mono">↗ parser.py<small>Python · solve(lines)</small></div><span>Example source</span></div>'+(view.options.renderer==='ASCII'?ascii(['[code link] → [source] → [contract]','','hot path: repeated scans per user','preserve: exact input + output']):'<div class="scan-lines" aria-hidden="true"><span></span><span></span><span></span></div>')+'<div class="check-list"><div class="check-line"><span>Input and output contract</span><span>Understood ✓</span></div><div class="check-line"><span>Goal</span><span>≥3× faster</span></div></div>';
  const unreached=!run&&!!view.analysis&&phase>=2&&!(view.analysis.auto&&view.analysis.outcome&&phase===6);
  if(unreached) content=notReached(view);
  else if(phase===2) content=workerScene(view);
  else if(phase===3) content=verifyScene(view);
  else if(phase===4) content=raceScene(view);
  else if(phase===5) content=zooScene(view);
  else if(phase===6) content=resultScene(view);
  const surface=app.querySelector('.stage-surface');
  surface.innerHTML=top(phase,view)+content;
  surface.classList.remove('nfw-motion');
  if(animate&&!window.matchMedia('(prefers-reduced-motion: reduce)').matches){
   requestAnimationFrame(()=>surface.classList.add('nfw-motion'));
  }
  if(focusKey){const next=app.querySelector(focusKey);if(next)next.focus({preventScroll:true});}
  app.querySelector('.preview-status').textContent=statusText(view);
  const status=app.querySelector('.history .agent .chat-body');
  status.textContent=run?(run.live?liveNotes[phase]:realNotes[phase]):view.analysis?analysisNote(view):phaseNotes[phase];
  app.querySelector('.history').hidden=phase===0;
  app.querySelector('.suggestions').hidden=phase===0||(!run&&!!view.analysis);
  app.querySelector('.source-tray').hidden=!(view.sourceOpen||phase===0)||(!run&&!!view.analysis);
 }
