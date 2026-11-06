import {createProcessingUI} from '/static/processing.js';
import {createResearchUI} from '/static/research.js';
const $ = id => document.getElementById(id);
let session, uploads=[], runs=[], selectedRun=null, pendingSelection=null, pendingConfiguration=null;
let preparing=false, selectionFailed=false, loadingResults=false, followedRun=null;
const exampleButtons=[];
function selectionControls(){
 const busy=preparing||!!pendingSelection;
 $('config-fields').disabled=busy;
 $('run-button').disabled=busy||selectionFailed||!uploads.some(x=>x.id===$('upload').value&&x.status==='ready');
 $('upload-button').disabled=busy;
 for(const button of exampleButtons)button.disabled=busy;
}
function applySettings(settings){for(const [key,value] of Object.entries(settings))if($(key))$(key).value=value;}
async function useExample(key,spec){
 if(preparing||pendingSelection)return;
 preparing=true;selectionFailed=false;$('confirm').checked=false;
 applySettings(spec.example_configuration);
 const label=spec.species==='human'?'human PBMC':'mouse brain';
 $('sample-status').textContent='Preparing '+label+' sample... Settings loaded automatically.';
 selectionControls();
 try{
  const created=await api('/api/examples/'+key,{method:'POST',body:'{}'});
  pendingSelection=created.id;pendingConfiguration=spec.example_configuration;
  await refresh();
 }catch(e){selectionFailed=true;$('sample-status').textContent='Could not prepare the '+label+' sample. Try selecting it again.';message(e.message);}
 finally{preparing=false;selectionControls();}
}
const message = text => {$('message').textContent=text;};
async function api(path, options={}) {
  const csrf=document.cookie.split('; ').find(x=>x.startsWith('csrf='))?.split('=')[1] || '';
  const response=await fetch(path,{credentials:'same-origin',...options,headers:{'Content-Type':'application/json','X-CSRF-Token':csrf,...options.headers}});
  if(!response.ok){let data;try{data=await response.json();}catch{data={detail:'Request failed'};}throw Error(typeof data.detail==='string'?data.detail:JSON.stringify(data.detail));}
  return response.json();
}
function options(id,values,selected){const el=$(id);el.replaceChildren();for(const [value,label] of values){const option=new Option(label,value);el.add(option);}if(values.some(x=>x[0]===selected))el.value=selected;}
function configuration(){const upload=uploads.find(x=>x.id===$('upload').value);if(!upload)return;const prior={counts:$('counts').value,gene_ids:$('gene_ids').value,gene_symbols:$('gene_symbols').value};const info=upload.inspection;
options('counts',Object.entries(info.matrices).map(([k,v])=>[k,`${k}: ${v.cells.toLocaleString()} cells; ${v.features.toLocaleString()} genes`]),prior.counts);
options('gene_ids',info.gene_columns.map(x=>[x,x==='index'?'index (gene table row labels)':x]),prior.gene_ids|| (info.gene_columns.includes('gene_ids')?'gene_ids':'index'));
options('gene_symbols',info.gene_columns.map(x=>[x,x==='index'?'index (gene table row labels)':x]),prior.gene_symbols||'index');$('inspection').textContent='Inspected file: '+upload.filename+' | Upload ID: '+upload.id;
}
async function refresh(){
 let queue;
 [uploads,runs,queue]=await Promise.all([api('/api/uploads'),api('/api/runs'),api('/api/queue-status')]);
 const ready=uploads.filter(x=>x.status==='ready');
 const previous=ready.some(x=>x.id===pendingSelection)?pendingSelection:$('upload').value;
 options('upload',ready.map(x=>[x.id,x.filename+' - Upload ID (short): '+x.id.slice(0,8)]),previous);
 if(pendingSelection){
  const pending=uploads.find(x=>x.id===pendingSelection);
  if(pending?.status==='ready'){
   configuration();
   if(pendingConfiguration)for(const key of ['counts','gene_ids','gene_symbols'])$(key).value=pendingConfiguration[key];
   $('sample-status').textContent='Ready: '+pending.filename+'. Review the settings, then run the quality check.';
   pendingSelection=null;pendingConfiguration=null;selectionFailed=false;
  }else if(pending&&['failed','deleted'].includes(pending.status)){
   $('sample-status').textContent='Sample preparation failed. Select an example again or upload another file.';
   message(pending.error||'The sample is unavailable.');pendingSelection=null;pendingConfiguration=null;selectionFailed=true;
  }
 }else if(!preparing&&!selectionFailed)configuration();
 selectionControls();
 const saved=[...uploads].sort((a,b)=>(b.created_at||'').localeCompare(a.created_at||''));
 $('stored-summary').textContent=saved.length+' saved uploads across '+new Set(saved.map(x=>x.filename)).size+' filenames. Expand below only if you want to remove a saved copy.';
 options('delete-upload',saved.map(x=>[x.id,x.filename+' | '+(x.created_at?new Date(x.created_at).toLocaleString():'Date unavailable')+' | Upload ID: '+x.id.slice(0,8)+' | '+x.status]),$('delete-upload').value||$('upload').value);
 $('delete').disabled=!saved.length;
 processingUI.update(queue);
 $('runs').replaceChildren();
 const visibleRuns=runs.filter(x=>$('show-cleared').checked||!x.history_hidden);
 $('clear-history').disabled=!runs.some(x=>['succeeded','failed','cancelled'].includes(x.status)&&!x.history_hidden);
 if(!visibleRuns.length)$('runs').textContent='No processing entries to display.';
 for(const run of visibleRuns){const row=document.createElement('div');row.className='run';const text=document.createElement('div');const progress=processingUI.describe(run);text.textContent=`${run.configuration.species} - ${progress.status} - ${progress.stage}`;const detail=document.createElement('small');detail.textContent='Run ID: '+run.id+' - '+new Date(run.created_at).toLocaleString()+(run.error?' - '+run.error:'');text.append(detail);row.append(text);
 if(run.status==='succeeded'){const button=document.createElement('button');button.textContent='View results';button.onclick=()=>showResults(run.id).catch(e=>message(e.message));row.append(button);}
 if(run.status==='queued'){const button=document.createElement('button');button.textContent='Cancel queued run';button.onclick=()=>api(`/api/runs/${run.id}/cancel`,{method:'POST',body:'{}'}).then(refresh).catch(e=>message(e.message));row.append(button);}
 $('runs').append(row);}
 updateComparison();
 const pending=uploads.filter(x=>!['ready','deleted'].includes(x.status));
 if(queue.blocked){
  message('Processing is blocked after a failed operation. Operator recovery is required; waiting alone will not complete it.');
  if(pendingSelection)$('sample-status').textContent='Waiting: the processing queue needs operator recovery.';
 }else if(queue.paused)message('Processing is paused. Your sample is saved and will wait until processing resumes.');
 else if(pending.length)message(pending.map(x=>x.filename+': '+x.status+(x.error?' - '+x.error:'')).join(' | '));
 else if(runs[0]?.status==='running')message('Quality check in progress: '+runs[0].stage+'. SQL checks and result verification follow QC.');
 else if(runs[0]?.status==='failed')message('Quality check failed: '+runs[0].error+'. No plots were produced for this run.');
 else if(runs[0]?.status==='succeeded')message('Quality check complete. Your QC plots and downloads are ready below.');
 const completed=followedRun?runs.find(x=>x.id===followedRun&&x.status==='succeeded'):(!selectedRun?runs.find(x=>x.status==='succeeded'):null);
 if(completed&&!loadingResults){loadingResults=true;try{await showResults(completed.id);followedRun=null;}finally{loadingResults=false;}}

}
$('upload').onchange=()=>{if(preparing||pendingSelection)return;selectionFailed=false;$('confirm').checked=false;configuration();$('sample-status').textContent='Selected: '+(uploads.find(x=>x.id===$('upload').value)?.filename||'no sample');selectionControls();};
$('species').onchange=()=>{$('mito_value').value=$('species').value==='mouse'?'mt-':'MT-';};
$('upload-form').onsubmit=async event=>{event.preventDefault();if(preparing||pendingSelection)return;preparing=true;selectionFailed=false;$('confirm').checked=false;selectionControls();const button=event.submitter;button.disabled=true;try{const file=$('file').files[0];if(file.size>250*1024*1024)throw Error('File exceeds 250 MiB.');const request=await api('/api/uploads',{method:'POST',body:JSON.stringify({filename:file.name,size:file.size,public_data:true})});$('upload-progress').hidden=false;await new Promise((resolve,reject)=>{const xhr=new XMLHttpRequest();xhr.open('PUT',request.upload_url);xhr.setRequestHeader('Content-Type','application/octet-stream');if(request.upload_url.startsWith('/'))xhr.setRequestHeader('X-CSRF-Token',document.cookie.split('; ').find(x=>x.startsWith('csrf='))?.split('=')[1]||'');xhr.upload.onprogress=e=>{$('upload-progress').value=e.lengthComputable?e.loaded/e.total*100:0;};xhr.onload=()=>xhr.status>=200&&xhr.status<300?resolve():reject(Error('Upload failed: '+xhr.status));xhr.onerror=()=>reject(Error('Upload interrupted.'));xhr.send(file);});await api(`/api/uploads/${request.upload.id}/finalize`,{method:'POST',body:'{}'});pendingSelection=request.upload.id;pendingConfiguration=null;message('Uploaded. Inspection is queued.');await refresh();}catch(e){message(e.message);}finally{preparing=false;selectionControls();}};
$('config-form').onsubmit=async event=>{event.preventDefault();if(preparing||pendingSelection||selectionFailed||!uploads.some(x=>x.id===$('upload').value&&x.status==='ready'))return;event.submitter.disabled=true;try{const configuration={schema_version:'2.0.0',public_data:true,single_sample:true};for(const k of ['species','counts','gene_ids','gene_symbols','mito_mode','mito_value','tissue','reference_genome','attribution','source'])configuration[k]=$(k).value;for(const k of ['min_genes','max_genes','max_mito_pct'])configuration[k]=Number($(k).value);const submitted=await api('/api/runs',{method:'POST',body:JSON.stringify({upload_id:$('upload').value,idempotency_key:crypto.randomUUID(),configuration})});followedRun=submitted.id;message('Run submitted. You can change the parameters and submit a second policy for comparison.');await refresh();}catch(e){message(e.message);}finally{selectionControls();}};
let viewerActualSize=false;
function openPlotViewer(id,kind,title,scale){
 viewerActualSize=false;$('plot-viewer-stage').className='viewer-stage';$('plot-viewer-zoom').textContent='Actual size';
 $('plot-viewer-title').textContent=title;$('plot-viewer-status').textContent='Loading high-resolution plot...';
 const image=$('plot-viewer-image');image.alt=title+'; blue: passes QC, orange: excluded';
 image.onload=()=>{$('plot-viewer-status').textContent='';};image.onerror=()=>{$('plot-viewer-status').textContent='This plot could not load. Close and reopen it to retry.';};
 image.src=`/api/runs/${id}/plots/${kind}.png?scale=${scale}&preview=true`;
 for(const format of ['png','jpeg','pdf'])$('plot-viewer-'+format).href=`/api/runs/${id}/plots/${kind}.${format}?scale=${scale}`;
 if(!$('plot-viewer').open)$('plot-viewer').showModal();
}
$('plot-viewer-close').onclick=()=>$('plot-viewer').close();
$('plot-viewer-zoom').onclick=()=>{viewerActualSize=!viewerActualSize;$('plot-viewer-stage').className='viewer-stage'+(viewerActualSize?' actual-size':'');$('plot-viewer-zoom').textContent=viewerActualSize?'Fit to window':'Actual size';};
function renderPlots(id){
 const scale=$('plot-scale').value||'linear';
 const figures=[['counts','Counts per cell'],['genes','Detected genes per cell'],['mitochondrial','Mitochondrial percentage'],['scatter','Counts and detected genes']];
 $('plots').replaceChildren();
 for(const [kind,title] of figures){
  const card=document.createElement('article');card.className='plot-card';
  const image=document.createElement('img');image.src=`/api/runs/${id}/plots/${kind}.svg?scale=${scale}`;image.alt=title+'; blue: passes QC, orange: excluded';image.width=800;image.height=540;
  image.onerror=()=>message('Could not load '+title+'. Reopen the results to retry.');
  const links=document.createElement('div');links.className='plot-downloads';
  const large=document.createElement('button');large.type='button';large.className='secondary';large.textContent='Enlarge';large.onclick=()=>openPlotViewer(id,kind,title,scale);large.setAttribute('aria-label','View '+title+' at full size');links.append(large);const label=document.createElement('span');label.textContent='Download:';links.append(label);
  for(const format of ['png','jpeg','pdf']){
   const link=document.createElement('a');link.href=`/api/runs/${id}/plots/${kind}.${format}?scale=${scale}`;link.textContent=format.toUpperCase();link.setAttribute('aria-label','Download '+title+' as '+format.toUpperCase());links.append(link);
  }
  card.append(image,links);$('plots').append(card);
 }
}
$('plot-scale').onchange=()=>{if(selectedRun)renderPlots(selectedRun);};
async function showResults(id){const data=await api(`/api/runs/${id}/results`);$('results').hidden=false;$('metrics').replaceChildren();for(const k of ['cells_total','cells_pass','cells_fail','features_total']){const box=document.createElement('div');box.className='stat';const n=document.createElement('strong');n.textContent=data.metrics[k].toLocaleString();const label=document.createElement('span');label.textContent=k.replaceAll('_',' ');box.append(n,label);$('metrics').append(box);}$('downloads').replaceChildren();for(const name of ['report.md','knowledge.json','validation.json','publication.json','passing_cells.csv','plots.json']){const a=document.createElement('a');a.className='downloads';a.href=`/api/runs/${id}/download/${name}`;a.textContent=name;$('downloads').append(a);}renderPlots(id);selectedRun=id;$('results').scrollIntoView({behavior:'smooth'});renderFacts($('answer'),data.limitations);$('evidence-details').hidden=true;$('results-run').textContent='Run ID: '+id;updateTopicHelp();await researchUI.show(id);}
const friendlyNames={cells_total:'Cells in the sample',cells_pass:'Cells passing QC',cells_fail:'Cells excluded',features_total:'Genes in the file',mitochondrial_features:'Mitochondrial genes',total_counts:'Total original counts',qc_pass_fraction:'Fraction of cells passing QC',min_genes:'Minimum detected genes (inclusive)',max_genes:'Maximum detected genes (exclusive)',max_mito_pct:'Maximum mitochondrial percentage (exclusive)',gene_ids:'Gene ID column',gene_symbols:'Gene symbol column',counts:'Original count matrix',mito_mode:'Mitochondrial mapping method',mito_value:'Mitochondrial prefix or column',reference_genome:'Reference genome',models:'SQL models checked',tests:'Data tests checked'};
const readable=key=>friendlyNames[key]||key.replaceAll('_',' ');
function displayValue(value){return value===null?'Not available':typeof value==='number'?(Number.isInteger(value)?value.toLocaleString():Number(value.toPrecision(5)).toLocaleString()):typeof value==='boolean'?(value?'Yes':'No'):String(value);}
function resultTable(container,headings,rows){
 container.replaceChildren();const table=document.createElement('table');table.className='facts-table';
 const thead=document.createElement('thead'),head=document.createElement('tr');
 for(const heading of headings){const th=document.createElement('th');th.scope='col';th.textContent=heading;head.append(th);}thead.append(head);table.append(thead);
 const body=document.createElement('tbody');for(const row of rows){const tr=document.createElement('tr');row.forEach((value,index)=>{const cell=document.createElement(index===0?'th':'td');if(index===0)cell.scope='row';cell.textContent=displayValue(value);tr.append(cell);});body.append(tr);}table.append(body);container.append(table);
}
function renderFacts(container,facts){
 if(Array.isArray(facts)){container.replaceChildren();const list=document.createElement('ul');for(const fact of facts){const item=document.createElement('li');item.textContent=typeof fact==='object'?JSON.stringify(fact):displayValue(fact);list.append(item);}container.append(list);return;}
 const rows=[];function walk(value,prefix=''){for(const [key,item] of Object.entries(value||{})){const label=prefix?prefix+' / '+readable(key):readable(key);if(item&&typeof item==='object'&&!Array.isArray(item))walk(item,label);else rows.push([label,Array.isArray(item)?item.map(displayValue).join(', '):item]);}}walk(facts);resultTable(container,['Verified fact','Value'],rows);
}
const topicHelp={qc:'How many cells passed or were excluded by this run.',summary:'The number of cells, genes and original counts in this sample.',exclusions:'Counts of cells flagged by each QC rule. A cell can fail more than one rule, so these counts can overlap.',policy:'The exact settings saved with this run. Editing the form above does not change an existing result.',dbt:'SQL model and data-test checks used to verify that the processed data agree.',limitations:'Boundaries of this analysis: QC is not cell-type annotation or biological validation.'};
function updateTopicHelp(){$('topic-help').textContent=topicHelp[$('topic').value]||topicHelp.qc;}
$('topic').onchange=()=>{updateTopicHelp();$('answer').textContent='Click Explain this topic to view the selected facts.';$('evidence-details').hidden=true;};
$('ask').onclick=async()=>{const id=selectedRun,topic=$('topic').value;if(!id)return;try{const result=await api(`/api/runs/${id}/answer?topic=${encodeURIComponent(topic)}`);if(selectedRun!==id||$('topic').value!==topic)return;renderFacts($('answer'),result.facts);$('evidence-json').textContent=JSON.stringify({source:result.json_pointer,evidence:result.evidence},null,2);$('evidence-details').hidden=false;}catch(error){message(error.message);}};
const mappingKeys=['species','counts','gene_ids','gene_symbols','mito_mode','mito_value'];
function comparisonIdentity(run){return JSON.stringify([run.upload_id,...['key','generation','sha256','size'].map(key=>run.input?.[key]),...mappingKeys.map(key=>run.configuration[key])]);}
function runLabel(run){const upload=uploads.find(x=>x.id===run.upload_id);return (upload?.filename||run.configuration.species)+' | Run ID: '+run.id.slice(0,8)+' | mitochondrial < '+run.configuration.max_mito_pct+'%';}
let comparisonPair=null;
function updateComparison(){
 const successful=runs.filter(x=>x.status==='succeeded');options('left',successful.map(x=>[x.id,runLabel(x)]),$('left').value);
 const left=successful.find(x=>x.id===$('left').value);
 const compatible=left?successful.filter(x=>x.id!==left.id&&comparisonIdentity(x)===comparisonIdentity(left)):[];
 options('right',compatible.map(x=>[x.id,runLabel(x)]),$('right').value);
 $('compare').disabled=!compatible.length;$('prepare-comparison').disabled=!left||preparing||!!pendingSelection;
 $('comparison-help').textContent=!left?'Complete a quality check first.':!compatible.length?'No second compatible run yet. Prepare a second run below, change a QC threshold, then run the quality check.':'Both lists refer to the same uploaded sample and gene/count mappings. Choose two runs with different QC thresholds.';
 const pair=$('left').value+'|'+$('right').value;if(comparisonPair&&pair!==comparisonPair){$('comparison').replaceChildren();comparisonPair=null;}
}
$('left').onchange=updateComparison;
$('right').onchange=()=>{$('comparison').replaceChildren();comparisonPair=null;};
$('prepare-comparison').onclick=()=>{
 if(preparing||pendingSelection)return;const first=runs.find(x=>x.id===$('left').value&&x.status==='succeeded');if(!first)return;
 $('upload').value=first.upload_id;configuration();applySettings(first.configuration);selectionFailed=false;$('confirm').checked=false;selectionControls();
 $('sample-status').textContent='Loaded settings from Run ID: '+first.id+'. Change QC thresholds only to create a comparable run.';
 $('config-form').scrollIntoView({behavior:'smooth'});message('Same uploaded sample selected. Change a QC threshold, review and confirm, then click Run quality check.');
};
$('compare').onclick=async()=>{const left=$('left').value,right=$('right').value;if(!left||!right||left===right)return;try{
 const data=await api(`/api/compare?left=${encodeURIComponent(left)}&right=${encodeURIComponent(right)}`);
 if($('left').value!==left||$('right').value!==right)return;
 const rows=[['Run ID',left,right],...['min_genes','max_genes','max_mito_pct'].map(key=>[readable(key),data.left_policy[key],data.right_policy[key]]),['Cells passing QC',data.left_pass,data.right_pass],['Cells passing both runs',data.both_pass,data.both_pass],['Cells kept only by this run',data.only_left,data.only_right]];
 resultTable($('comparison'),['Measure','First run','Second run'],rows);const note=document.createElement('p');note.className='help';note.textContent='Both: cells kept by both sets of settings. Only: cells kept by one set but excluded by the other. '+data.limitation;$('comparison').append(note);comparisonPair=left+'|'+right;
 }catch(error){message(error.message);}};
$('show-cleared').onchange=()=>refresh().catch(error=>message(error.message));
$('clear-history').onclick=async()=>{const button=$('clear-history');button.disabled=true;try{const result=await api('/api/history/clear',{method:'POST',body:'{}'});$('show-cleared').checked=false;await refresh();$('history-status').textContent=result.cleared+' finished history entries cleared. Files and saved results were kept.'+(result.retained_blocked?' A blocked operation remains visible and needs operator recovery.':'');}catch(error){$('history-status').textContent=error.message;}finally{button.disabled=!runs.some(x=>['succeeded','failed','cancelled'].includes(x.status)&&!x.history_hidden);}};
$('delete').onclick=async()=>{const id=$('delete-upload').value,upload=uploads.find(x=>x.id===id);if(!upload)return;if(!confirm('Delete '+upload.filename+' (Upload ID: '+id+') and its results and papers? Other uploaded copies are kept. This cannot be undone.'))return;try{await api('/api/uploads/'+id,{method:'DELETE'});if(runs.some(x=>x.id===selectedRun&&x.upload_id===id)){$('results').hidden=true;selectedRun=null;if($('plot-viewer').open)$('plot-viewer').close();}message('Deletion requested. Cloud cleanup runs daily.');await refresh();}catch(e){message(e.message);}};
$('login').onclick=async()=>{try{const {initializeApp}=await import('https://www.gstatic.com/firebasejs/11.0.2/firebase-app.js');const {getAuth,GoogleAuthProvider,signInWithPopup,signOut}=await import('https://www.gstatic.com/firebasejs/11.0.2/firebase-auth.js');const auth=getAuth(initializeApp(session.firebase));const result=await signInWithPopup(auth,new GoogleAuthProvider());const token=await result.user.getIdToken();await api('/api/session',{method:'POST',body:JSON.stringify({id_token:token})});await signOut(auth);location.reload();}catch(e){message(e.message);}};
$('logout').onclick=()=>api('/api/session',{method:'DELETE'}).then(()=>location.reload()).catch(e=>message(e.message));
function renderExamples(examples){
 for(const [key,spec] of Object.entries(examples)){
  const card=document.createElement('article');card.className='example-card';
  const title=document.createElement('h3');title.textContent=spec.species==='human'?'Human: PBMC':'Mouse: embryonic brain';
  const detail=document.createElement('p');detail.className='help';detail.textContent=spec.cells.toLocaleString()+' cells; '+spec.reference_genome;
  const source=document.createElement('a');source.href=spec.source;source.textContent='Source: 10x Genomics';source.target='_blank';source.rel='noopener noreferrer';
  const attribution=document.createElement('p');attribution.className='help';attribution.textContent=spec.example_configuration.attribution;
  const button=document.createElement('button');button.type='button';button.className='secondary';button.textContent='Use this sample';button.setAttribute('aria-label','Use '+title.textContent+' sample');button.onclick=()=>useExample(key,spec);
  exampleButtons.push(button);card.append(title,detail,source,attribution,button);$('examples').append(card);
 }
}
// Reveal invalid advanced fields so native validation can focus them.
$('config-form').addEventListener('invalid',event=>{if($('advanced-settings').contains(event.target))$('advanced-settings').open=true;},true);
async function start(){
 session=await api('/api/session');$('workspace').hidden=!session.signed_in;$('login').hidden=session.signed_in;$('logout').hidden=!session.signed_in||session.local;
 $('run-limit').textContent=session.run_quota_exempt?'Owner testing: unlimited daily QC runs.':`QC run limit: ${session.run_limit_per_day??5} per UTC day.`;
 if(session.local)message('Local development mode: no cloud sign-in, no cloud billing.');
 if(session.signed_in){await researchUI.initialize();renderExamples(await api('/api/examples'));await refresh();setInterval(()=>refresh().catch(e=>message(e.message)),5000);setInterval(()=>processingUI.tick(),1000);}
 else message('Sign in with an invited Google account to begin.');
}
const processingUI=createProcessingUI({$,getRuns:()=>runs,getUploads:()=>uploads,getSession:()=>session});
const researchUI=createResearchUI({$,api,message,renderFacts,resultTable,selectedRun:()=>selectedRun});
start().catch(e=>message(e.message));
