// Analyst proposal and preparation scenes shared with the launch controller.
const escape=s=>String(s??'').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;');
const num=v=>typeof v==='number'&&Number.isFinite(v);
 const clock=ms=>{const t=Math.max(0,Math.floor(ms/1000));return Math.floor(t/60)+':'+String(t%60).padStart(2,'0');};
 const jobActive=job=>!!job&&(job.status==='queued'||job.status==='running');
 const list=(items,cls)=>Array.isArray(items)&&items.length?'<ul class="'+(cls||'plain-list')+'">'+items.map(i=>'<li>'+escape(i)+'</li>').join('')+'</ul>':'';
 export const repoShort=url=>String(url||'').replace(/^https:\/\/github\.com\//,'');
 function contractSummary(r){
  const p=r&&r.proposal&&typeof r.proposal==='object'?r.proposal:null;
  if(!p) return '';
  const sz=p.sizes&&typeof p.sizes==='object'?p.sizes:{};
  const sizes=(num(sz.timing_size)?'timing '+sz.timing_size:'')+(Array.isArray(sz.correctness_sizes)&&sz.correctness_sizes.length?(num(sz.timing_size)?' · ':'')+'correctness '+sz.correctness_sizes.filter(num).join(', '):'');
  return '<div class="file-focus"><div class="nfw-mono">'+escape(p.target||'target not given')+'<small>Proposed target</small></div><span>Awaiting your decision</span></div>'+(p.description?'<p class="stage-sub">'+escape(p.description)+'</p>':'')+(Array.isArray(p.hints)&&p.hints.length?'<h3 class="mini-heading">Planned optimizations</h3>'+list(p.hints):'')+(Array.isArray(p.risks)&&p.risks.length?'<h3 class="mini-heading">Risks</h3>'+list(p.risks):'')+(sizes?'<p class="nfw-muted">Sizes · '+escape(sizes)+'</p>':'');
 }
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
 export function timelineHtml(steps){
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
 export function assumptionsHtml(o){
  return o.assumptions.length?'<h3 class="mini-heading">Assumptions the agent made</h3>'+list(o.assumptions):'';
 }
 export function outcomeHtml(view){
  const o=autoOutcome(view.analysis.outcome),repo=escape(repoShort(view.analysis.repoUrl));
  const cost=o.cost!==null?'<p class="nfw-muted">Spend reported by the agents: $'+o.cost.toFixed(2)+'</p>':'';
  const runs=o.runs.length?'<h3 class="mini-heading">Runs</h3><ul class="plain-list">'+o.runs.map(x=>'<li>'+escape(x.label)+' · '+escape(x.state)+'</li>').join('')+'</ul>':'';
  if(o.status==='not_feasible') return '<h2>Reading between the lines.</h2><div class="success-heading fail-heading"><span class="success-check">✕</span>Not feasible as asked</div><p class="stage-sub">'+escape(o.reason||'The analyst did not give a reason.')+'</p>'+(o.suggestions.length?'<h3 class="mini-heading">Possible optimizations / alternatives</h3>'+list(o.suggestions):'')+assumptionsHtml(o)+cost;
  if(o.status==='gave_up') return '<h2>No accepted result.</h2><div class="success-heading fail-heading"><span class="success-check">✕</span>The agents gave up on '+repo+'</div>'+(o.report?'<p class="stage-sub">'+escape(o.report)+'</p>':'')+(o.attempts.length?'<h3 class="mini-heading">Attempts</h3>'+list(o.attempts):'')+runs+(o.lastState?'<div class="end-state nfw-mono">Last run state: '+escape(o.lastState)+'</div>':'')+'<p class="stage-sub">No speedup is claimed.</p>'+assumptionsHtml(o)+cost;
  if(o.status==='verified_done') return '<h2>Result</h2><div class="success-heading fail-heading"><span class="success-check">!</span>Finished, but the audited result could not be shown</div><p class="stage-sub">The agents report verified_done, but no run ledger could be opened for it, so nothing is claimed here.</p>'+assumptionsHtml(o)+cost;
  return '<h2>Result</h2><div class="success-heading fail-heading"><span class="success-check">✕</span>Finished without a recognized outcome</div><p class="stage-sub">The autonomous run ended with status “'+escape(o.status||'unknown')+'”. No speedup is claimed.</p>'+(o.report?'<p class="stage-sub">'+escape(o.report)+'</p>':'')+assumptionsHtml(o)+cost;
 }
 export function autoTimeline(view){
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
 export function analystScene(view){
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
 export function notReached(view){
  return '<h2>Not reached yet.</h2><p class="stage-sub">No run has started for '+escape(repoShort(view.analysis.repoUrl))+'. These stages show real events once '+(view.analysis.auto?'the agents start a run.':'you approve a proposal and the live run begins.')+'</p><div class="scene-footer">Nothing is shown here until the run produces it</div>';
 }
