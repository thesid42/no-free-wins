import { phaseNames, phaseNotes, recordedCode, proof } from './demo-data.js';
 const escape=s=>String(s).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;');
 function ascii(lines){return '<pre class="ascii" aria-label="Illustrative pipeline view">'+escape(lines.join('\n'))+'</pre>';}
 function top(phase,view){
  return '<div class="stage-titlebar"><span class="eyebrow">'+('Preview / '+phaseNames[phase])+'</span><div class="scene-controls"><button type="button" data-play class="cursor-interaction">'+(view.playing?'Pause preview':'Play preview')+'</button><button type="button" data-renderer aria-label="Toggle pipeline view">'+(view.options.renderer==='ASCII'?'Line graph':'ASCII')+'</button></div></div>';
 }
 function workerScene(view){
  const graph='<div class="route-in nfw-mono">Analysis complete</div><div class="workers"><div class="worker"><div class="worker-top"><span class="worker-avatar">A</span><div><h3>Worker A</h3><small>Isolated sandbox A</small></div></div><pre class="worker-code nfw-mono">regex.fullmatch(line)\naggregate in one pass</pre><div class="worker-status"><span class="status-dot"></span>Building candidate <span class="flow-dots">···</span></div></div><div class="worker"><div class="worker-top"><span class="worker-avatar">B</span><div><h3>Worker B</h3><small>Isolated sandbox B</small></div></div><pre class="worker-code nfw-mono">validate each token\nreduce repeated work</pre><div class="worker-status"><span class="status-dot"></span>Building candidate <span class="flow-dots">···</span></div></div></div><div class="route-out nfw-mono">└──────────┬──────────┘<br>Independent review · next</div>';
  const text=ascii(['          [analysis ✓]','               │','       ┌───────┴───────┐','   [worker A]     [worker B]','    sandbox A      sandbox B','    regex pass     token parser','    ◉ building     ◉ building','       └───────┬───────┘','          [verifier] next']);
  return '<h2>Two ways to get faster.</h2><p class="stage-sub">Different strategies. The same correctness contract.</p>'+(view.options.renderer==='ASCII'?text:graph)+'<div class="scene-footer"><span class="status-dot"></span>Illustrative sandbox scene · chat stays available</div>';
 }
 function raceScene(view){
  const graph='<div class="race-lane"><div class="race-label"><span>Original</span><span>baseline</span></div><div class="race-track"><span class="runner">›</span></div></div><div class="race-lane"><div class="race-label"><span>Optimized</span><span>candidate A</span></div><div class="race-track"><span class="runner fast">››</span></div></div>';
  const text=ascii(['PAIR 18 / 30','', 'original   ━━━━━━━▶············┃', 'optimized  ━━━━━━━━━━━━━━━━━▶·┃', '', 'outputs    ✓ match','same input / same machine']);
  return '<h2>Let the code race.</h2><p class="stage-sub">30 matched pairs. Every output still has to match.</p>'+(view.options.renderer==='ASCII'?text:graph)+'<div class="race-readout"><span>Pair 18 / 30 · illustrative replay</span><span>Outputs matching ✓</span></div><div class="scene-footer">A fast screen alone doesn’t finish the run.</div>';
 }
 function zooScene(view){
  const animals=[['🦊','Clock tamper'],['🦝','Hardcoding'],['🐇','Early exit'],['🦥','Lazy return']];
  const graph='<div class="zoo">'+animals.map(a=>'<div class="animal"><span class="creature" aria-hidden="true">'+a[0]+'</span><span class="animal-name">'+a[1]+'</span><span class="animal-verdict">Rejected ✓</span></div>').join('')+'</div>';
  const text=ascii(['🦊 clock tamper ........ rejected','🦝 hardcoding .......... rejected','🐇 early exit .......... rejected','🦥 lazy return ......... rejected','','🐢 honest controls .... 4 passed']);
  return '<h2>A quick visit to the zoo.</h2><p class="stage-sub">Known bad implementations test the defenses. Honest controls must pass too.</p>'+(view.options.renderer==='ASCII'?text:graph)+'<div class="zoo-score"><span>4 / 4 planted cases rejected</span><span>4 / 4 honest controls passed</span></div><div class="scene-footer">Separate development defense suite</div>';
 }
 function resultScene(){
  return '<div class="success-heading"><span class="success-check">✓</span>Recorded verified example</div><div class="speed">3.75×</div><div class="speed-label">faster, with matching outputs</div><div class="interval">95% confidence interval <strong>3.52–3.81×</strong></div><div class="result-checks"><span>2,000 hidden cases</span><span>30 timing pairs</span><span>Sandboxes deleted</span></div><div class="result-actions"><button type="button" class="nfw-primary" data-download="code">↓ Example optimized.py</button><span class="nfw-muted">From the recorded development run</span></div><details><summary class="cursor-interaction">See code &amp; what changed</summary><p class="nfw-muted">Repeated per-user scans became one-pass aggregation. Exact line validation and the output schema are preserved.</p><pre class="code-preview nfw-mono">'+escape(recordedCode)+'</pre></details><details><summary class="cursor-interaction">View proof · receipt audited ✓</summary><div class="proof-data">MEASURED DEVELOPMENT RESULT<br>Median: 3.7468× · pairs: 30<br>95% CI: [3.5225×, 3.8081×]<br>Receipt SHA-256:<br>'+proof.receipt_sha256+'<br><br>Source SHA-256:<br>'+proof.source_sha256+'</div><button type="button" class="nfw-secondary proof-download" data-download="proof">↓ Proof summary</button></details><div class="scene-footer">Recorded development example · not a new benchmark</div>';
 }
 export function render(view,animate){
  const app=view.element,phase=view.phase;
  app.dataset.phase=String(phase);
  const nav=app.querySelector('.sequence');
  nav.innerHTML=phaseNames.map((label,i)=>'<button type="button" class="cursor-interaction'+(i<phase?' done':'')+'" data-phase="'+i+'"'+(i===phase?' aria-current="step"':'')+'><span class="step-symbol">'+(i<phase?'✓':i===phase?'●':'·')+'</span>'+label+'</button>').join('');
  let content='';
  if(phase===0) content='<div class="input-scene"><div class="big-symbol">nfw↗</div><h2>Give your code a fair race.</h2><p class="stage-sub">Paste a link. Tell me what needs to change.<br>Preview the pipeline and a recorded example result.</p><button type="button" class="nfw-secondary" data-example>Play example ↗</button><div class="supported">UI skeleton · code links are only stored in this page’s memory</div></div>';
  if(phase===1) content='<h2>Reading between the lines.</h2><p class="stage-sub">Example source only. Your pasted link has not been fetched.</p><div class="file-focus"><div class="nfw-mono">↗ parser.py<small>Python · solve(lines)</small></div><span>Example source</span></div>'+(view.options.renderer==='ASCII'?ascii(['[code link] → [source] → [contract]','','hot path: repeated scans per user','preserve: exact input + output']):'<div class="scan-lines" aria-hidden="true"><span></span><span></span><span></span></div>')+'<div class="check-list"><div class="check-line"><span>Input and output contract</span><span>Understood ✓</span></div><div class="check-line"><span>Goal</span><span>≥3× faster</span></div></div>';
  if(phase===2) content=workerScene(view);
  if(phase===3) content='<h2>A second set of eyes.</h2><p class="stage-sub">An independent verifier reviews the selected candidate.</p><div class="reviewer"><span class="worker-avatar">V</span><div><h3>Verifier</h3><span class="nfw-muted">Reviewing candidate A</span></div></div>'+(view.options.renderer==='ASCII'?ascii(['[candidate A] → [verifier] → [timing]',' source bound     ◉ reviewing    next']):'<div class="file-focus"><span class="nfw-mono">optimized.py</span><span class="nfw-muted">Candidate A · hash bound</span></div>')+'<div class="check-list"><div class="check-line"><span>Input validation preserved</span><span>✓</span></div><div class="check-line"><span>General implementation</span><span>✓</span></div><div class="check-line"><span>Evidence bound to this code</span><span>✓</span></div></div><div class="scene-footer">Approval leads to independent performance testing.</div>';
  if(phase===4) content=raceScene(view);
  if(phase===5) content=zooScene(view);
  if(phase===6) content=resultScene();
  const surface=app.querySelector('.stage-surface');
  surface.innerHTML=top(phase,view)+content;
  surface.classList.remove('nfw-motion');
  if(animate&&!window.matchMedia('(prefers-reduced-motion: reduce)').matches){
   requestAnimationFrame(()=>surface.classList.add('nfw-motion'));
  }
  app.querySelector('.preview-status').textContent = view.playing ? 'Playing local demo · '+phaseNames[phase] : 'Local demo · '+phaseNames[phase];
  const status=app.querySelector('.history .agent .chat-body');
  status.textContent=phaseNotes[phase];
  app.querySelector('.history').hidden=phase===0;
  app.querySelector('.suggestions').hidden=phase===0;
  app.querySelector('.source-tray').hidden=!(view.sourceOpen||phase===0);
  const input=app.querySelector('textarea');
  input.placeholder=phase===0?'Tell me what you want changed…':'Ask about this run, or tell me what’s next…';
  const send=app.querySelector('.send');
  send.textContent=phase===0?'Preview flow ↗':'↑';
  send.setAttribute('aria-label',phase===0?'Start local preview':'Send message');
 }
