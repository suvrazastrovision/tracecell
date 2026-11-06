const stages=['qc','load','dbt','knowledge'];
const stageNames={qc:'Calculating cell quality',load:'Preparing analysis tables',dbt:'Checking data consistency',knowledge:'Verifying and publishing results'};
const statuses={queued:'Queued',running:'Running',published:'Finalizing',succeeded:'Completed',failed:'Failed',cancelled:'Cancelled'};
const secondsBetween=(end,start)=>Number.isFinite(Date.parse(end))&&Number.isFinite(Date.parse(start))?Math.max(0,(Date.parse(end)-Date.parse(start))/1000):0;
export function formatDuration(seconds){
 const total=Math.max(0,Math.floor(seconds));return total<60?total+'s':Math.floor(total/60)+'m '+String(total%60).padStart(2,'0')+'s';
}
export function runStageLabel(run){
 const index=stages.indexOf(run.stage);
 return {status:statuses[run.status]||run.status,stage:index>=0?'Step '+(index+1)+' of 4: '+stageNames[run.stage]:run.status==='queued'?'Waiting to start':run.stage||''};
}
export function estimateRun(run,history,elapsed,local){
 const durations=history.filter(other=>other.status==='succeeded'&&other.id!==run.id&&
  ((run.upload_id&&other.upload_id===run.upload_id)||(run.input?.sha256&&other.input?.sha256===run.input.sha256)))
  .map(other=>secondsBetween(other.updated_at,other.started_at)).filter(value=>value>0&&value<86400).sort((a,b)=>a-b);
 let low,high,basis;
 if(durations.length){const middle=Math.floor(durations.length/2),median=durations.length%2?durations[middle]:(durations[middle-1]+durations[middle])/2;
  low=Math.max(5,median*.7);high=Math.max(20,median*1.8);basis='Based on '+durations.length+' completed run'+(durations.length===1?'':'s')+' of this file.';
 }else{low=local?30:120;high=local?300:900;basis='Initial guide only; no completed run of this file yet. Sample size and startup time can change this.';}
 if(elapsed>=high)return 'Taking longer than the estimate; still processing. '+basis;
 if(elapsed>=low)return 'Estimated remaining: up to about '+formatDuration(high-elapsed)+'. '+basis;
 return 'Estimated remaining: '+formatDuration(Math.max(0,low-elapsed))+' to '+formatDuration(high-elapsed)+'. '+basis;
}

export function createProcessingUI({$,getRuns,getUploads,getSession}){
 let queue={},lastChecked=null;
 const set=(id,text)=>{if($(id).textContent!==text)$(id).textContent=text;};
 function tick(now=Date.now()){
  const runs=getRuns(),uploads=getUploads(),session=getSession();
  const activeRun=runs.find(run=>['running','published'].includes(run.status));
  const activeUpload=uploads.find(upload=>['inspecting','inspected'].includes(upload.status));
  const queuedRun=runs.filter(run=>run.status==='queued').sort((a,b)=>(a.created_at||'').localeCompare(b.created_at||''))[0];
  const queuedUpload=uploads.find(upload=>upload.status==='queued_inspection');
  const run=activeRun||(!activeUpload?queuedRun:null);
  const upload=activeUpload||(!run?queuedUpload:null);
  const recent=runs.find(item=>!item.history_hidden);
  const panel=$('processing-live');
  panel.hidden=!run&&!upload&&!recent&&!queue.blocked;
  if(panel.hidden)return;
  let title,detail,timing='',busy=false,state='idle';
  if(run){
   const labels=runStageLabel(run),sample=uploads.find(item=>item.id===run.upload_id)?.filename||run.configuration?.species||'sample';
   const elapsed=secondsBetween(new Date(now).toISOString(),run.started_at||run.created_at);
   if(run.status==='queued'){
    title=(queue.blocked?'Blocked':queue.paused?'Paused':'Queued')+': '+sample;
    detail=queue.blocked?'A failed operation needs operator recovery. Waiting alone will not complete this run.':queue.paused?'Processing is paused. This run is saved in the queue.':'Waiting for the processing slot. One operation runs at a time.';
    timing='Waiting: '+formatDuration(elapsed)+'. The completion estimate starts when this run begins.';
    busy=!queue.blocked&&!queue.paused;state=queue.blocked?'blocked':queue.paused?'paused':'queued';
   }else{
    title=(run.status==='published'?'Finalizing':'Running')+': '+sample;detail=labels.stage;
    if(run.status==='published')detail='Step 4 of 4: Waiting for final workflow verification';
    timing='Elapsed: '+formatDuration(elapsed)+'. '+estimateRun(run,runs,elapsed,!!session?.local);busy=true;state='running';
   }
   detail+=' | Run ID: '+run.id.slice(0,8);
  }else if(upload){
   const elapsed=secondsBetween(new Date(now).toISOString(),upload.started_at||upload.created_at);
   const waiting=upload.status==='queued_inspection';
   title=(queue.blocked?'Blocked':waiting&&queue.paused?'Paused':waiting?'Queued':upload.status==='inspected'?'Finalizing':'Running')+': sample inspection';
   detail=queue.blocked?'A failed operation needs operator recovery.':waiting?'Waiting to inspect '+upload.filename:upload.status==='inspected'?'Waiting for final workflow verification of '+upload.filename:'Reading '+upload.filename+' and checking its count matrix';
   timing=(waiting?'Waiting: ':'Elapsed: ')+formatDuration(elapsed)+'. '+(waiting?'The QC run starts after inspection and your confirmation.':elapsed>60?'Inspection is taking longer than the initial estimate.':'Initial inspection guide: a few seconds to about one minute; larger files can take longer.');
   busy=!queue.blocked&&!(waiting&&queue.paused);state=queue.blocked?'blocked':waiting?'queued':'running';
  }else if(queue.blocked){title='Processing blocked';detail='A failed operation needs operator recovery before more work can start.';state='blocked';
  }else if(recent){
   const labels=runStageLabel(recent);title=labels.status;
   detail=recent.status==='succeeded'?'Quality check completed. Your plots and downloads are ready.':recent.status==='failed'?'Processing stopped: '+(recent.error||'See the failed run below.'):'No operation is currently running.';
   if(recent.started_at&&recent.updated_at)timing=(recent.status==='succeeded'?'Completed in ':'Processing time: ')+formatDuration(secondsBetween(recent.updated_at,recent.started_at))+'.';
   state=recent.status==='failed'?'blocked':recent.status==='succeeded'?'complete':'idle';
  }
  if((run||upload)&&lastChecked!==null&&now-lastChecked>15000){
   title='Status updates delayed';detail='Last confirmed: '+titleForState(state)+'. Trying to reconnect to the backend.';
   timing='Last successful status check: '+formatDuration((now-lastChecked)/1000)+' ago. Remaining time unavailable until updates resume.';busy=false;state='paused';
  }
  panel.className='processing-panel state-'+state;
  set('processing-title',title);set('processing-stage',detail);set('processing-time',timing);
  $('processing-loading').hidden=!busy;$('processing-loading').setAttribute('aria-valuetext',title+'. '+detail);
 }
 function titleForState(state){return state==='running'?'running':state==='queued'?'queued':state;}
 return {update:(value,now=Date.now())=>{queue=value;lastChecked=now;tick(now);},tick,describe:runStageLabel};
}
