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
 export function analystScene(view){
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
  if(jobActive(job)) return job.kind==='approve'?'Approved. Preparing the sandbox before the live run starts.':'The analyst agent is working. You can answer once it responds.';
  const last=a.last;
  if(!last) return 'Waiting for the analyst.';
  if(last.type==='failed') return 'The job failed. Details are in the chat.';
  if(last.type==='cancelled') return 'The job was cancelled.';
  const s=last.result&&last.result.status;
  return s==='proposal'?'The analyst proposed a contract. Nothing runs until you approve.':s==='not_feasible'?'The analyst says this is not feasible as asked. See the alternatives.':'The analyst has questions. Your next message is sent as the answer.';
 }
 export function notReached(view){
  return '<h2>Not reached yet.</h2><p class="stage-sub">No run has started for '+escape(repoShort(view.analysis.repoUrl))+'. These stages show real events once you approve a proposal and the live run begins.</p><div class="scene-footer">Nothing is shown here until the run produces it</div>';
 }
