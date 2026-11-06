export function createResearchUI({$,api,message,renderFacts,resultTable,selectedRun}){
 let status={configured:false,external:false},paper=null,pages=[],confirmedReference=null,loadedRun=null;
 const rules=[['min_genes','Minimum detected genes','>='],['max_genes','Maximum detected genes','<'],['max_mito_pct','Maximum mitochondrial percentage','<']];
 for(const [field,title,operator] of rules){
  const card=document.createElement('div');card.className='paper-rule';
  card.innerHTML=`<h4>${title}</h4><div class="rule-controls"><label>Retain cells with<select id="ref-op-${field}">${(field==='min_genes'?['>=','>']:['<','<=']).map(op=>`<option value="${op.replaceAll('<','&lt;')}">${op.replaceAll('<','&lt;')}</option>`).join('')}</select></label><label>Threshold (optional)<input id="ref-value-${field}" type="number" min="0" max="${field==='max_mito_pct'?100:40001}" step="${field==='max_mito_pct'?'any':'1'}"></label><label>PDF page<input id="ref-page-${field}" type="number" min="1" max="40"></label></div><label>Exact supporting quotation<textarea id="ref-quote-${field}" rows="2" maxlength="1000"></textarea></label>`;
  $('paper-rules').append(card);
 }
 function resetReference(){confirmedReference=null;$('reference-confirm').checked=false;$('paper-comparison').replaceChildren();}
 function fillReference(draft={rules:[],species:'unspecified',tissue:'unspecified'}){
  resetReference();$('reference-species').value=draft.species;$('reference-tissue').value=draft.tissue;
  for(const [field,,operator] of rules){const rule=draft.rules.find(x=>x.field===field);$('ref-op-'+field).value=rule?.operator||operator;for(const key of ['value','page','quote'])$('ref-'+key+'-'+field).value=rule?.[key]??'';}
 }
 function referenceInput(){
  const entered=[];
  for(const [field] of rules){const value=$('ref-value-'+field).value;if(value==='')continue;const quote=$('ref-quote-'+field).value.trim(),page=Number($('ref-page-'+field).value);
   if(!quote||!page)throw Error('Add an exact quotation and PDF page for each entered threshold.');
   entered.push({field,value:Number(value),operator:$('ref-op-'+field).value,page,quote});
  }
  if(!entered.length)throw Error('Enter at least one rule explicitly stated in the paper.');
  if(!$('reference-confirm').checked)throw Error('Review the rules and tick the confirmation box first.');
  return {rules:entered,species:$('reference-species').value,tissue:$('reference-tissue').value.trim(),confirmed:true};
 }
 async function initialize(){
  status=await api('/api/assistant/status');$('model-status').textContent=status.configured?`Small model: ${status.model} (${status.provider}). Connection checked when you ask.`:'Small model not connected. Verified evidence lookup and manual paper comparison are available.';
  $('model-sharing').hidden=!status.external;$('chat-button').textContent=status.configured?'Ask the knowledge assistant':'Find verified facts';
 }
 async function loadPapers(run,id=null){
  const list=await api(`/api/runs/${run}/papers`);if(selectedRun()!==run)return;
  const select=$('paper-select');select.replaceChildren();select.add(new Option('No paper selected',''));
  for(const item of list)select.add(new Option(item.filename+' ('+item.page_count+' PDF pages)',item.id));
  select.value=list.some(x=>x.id===id)?id:'';await loadSelectedPaper();
 }
 function showPage(){const page=pages.find(x=>String(x.page)===$('paper-page').value);$('paper-page-text').value=page?.text||'';}
 async function loadSelectedPaper(){
  const run=selectedRun(),id=$('paper-select').value;paper=null;pages=[];resetReference();fillReference();$('paper-content').hidden=true;
  if(!id)return;
  const result=await api(`/api/runs/${run}/papers/${id}`);if(selectedRun()!==run||$('paper-select').value!==id)return;
  paper=result;pages=result.pages;$('paper-content').hidden=false;$('paper-open').href=`/api/runs/${run}/papers/${id}/pdf`;
  $('paper-page').replaceChildren();for(const page of pages)$('paper-page').add(new Option('PDF page '+page.page,String(page.page)));showPage();
  $('paper-suggest').disabled=!status.configured;$('paper-status').textContent='Paper ready. Review a methods passage, then suggest or enter its explicit QC rules.';
 }
 async function show(run){
  if(run===loadedRun)return;loadedRun=run;paper=null;pages=[];confirmedReference=null;
  $('chat-answer').replaceChildren();$('paper-status').textContent='';$('paper-content').hidden=true;$('paper-select').replaceChildren();
  await loadPapers(run);
 }
 $('paper-select').onchange=()=>loadSelectedPaper().catch(error=>{$('paper-status').textContent=error.message;});
 $('paper-page').onchange=showPage;
 $('paper-reference-form').addEventListener('input',event=>{if(event.target.id!=='reference-confirm')resetReference();});
 $('paper-upload-form').onsubmit=async event=>{event.preventDefault();const run=selectedRun();if(!run)return;const button=$('paper-upload-button');button.disabled=true;
  try{const file=$('paper-file').files[0];if(!file||file.size>100*1000*1000)throw Error('Choose a PDF up to 100 MB.');$('paper-status').textContent='Reading the PDF text…';
   const result=await api(`/api/runs/${run}/papers?filename=${encodeURIComponent(file.name)}`,{method:'POST',headers:{'Content-Type':'application/pdf'},body:await file.arrayBuffer()});
   if(selectedRun()!==run)return;await loadPapers(run,result.id);
  }catch(error){$('paper-status').textContent=error.message;}finally{button.disabled=false;}
 };
 $('paper-remove').onclick=async()=>{if(!paper)return;const run=selectedRun(),id=paper.id;try{await api(`/api/runs/${run}/papers/${id}`,{method:'DELETE'});await loadPapers(run);$('paper-status').textContent='Paper removed.';}catch(error){$('paper-status').textContent=error.message;}};
 $('paper-suggest').onclick=async()=>{if(!paper)return;const run=selectedRun(),id=paper.id;const button=$('paper-suggest');button.disabled=true;
  try{$('paper-status').textContent='Reading relevant passages with the model…';const draft=await api(`/api/runs/${run}/papers/${id}/suggest`,{method:'POST',body:JSON.stringify({allow_external:$('allow-external').checked})});
   if(selectedRun()!==run||paper?.id!==id)return;fillReference(draft);$('paper-status').textContent=draft.note;
  }catch(error){$('paper-status').textContent=error.message;}finally{button.disabled=!status.configured;}
 };
 $('paper-reference-form').onsubmit=async event=>{event.preventDefault();if(!paper)return;const run=selectedRun(),id=paper.id;const button=$('paper-compare');button.disabled=true;
  try{const reference=referenceInput();const result=await api(`/api/runs/${run}/papers/${id}/compare`,{method:'POST',body:JSON.stringify(reference)});
   if(selectedRun()!==run||paper?.id!==id)return;confirmedReference=reference;
   resultTable($('paper-comparison'),['Calculated measure','Number of cells'],[['Cells in this sample',result.cells_total],['Pass current QC settings',result.current_pass],['Meet confirmed paper rules',result.reference_pass],['Pass both',result.both_pass],['Kept only by current settings',result.only_current],['Kept only by paper rules',result.only_reference]]);
   const warning=document.createElement('div');warning.className='paper-warning';renderFacts(warning,result.warnings);$('paper-comparison').append(warning);
   const citations=document.createElement('ul');citations.className='source-list';for(const rule of reference.rules){const item=document.createElement('li'),link=document.createElement('a');link.href=`/api/runs/${run}/papers/${id}/pdf#page=${rule.page}`;link.target='_blank';link.rel='noopener noreferrer';link.textContent=rules.find(x=>x[0]===rule.field)[1]+' '+rule.operator+' '+rule.value+' — PDF page '+rule.page;const quote=document.createElement('blockquote');quote.textContent=rule.quote;item.append(link,quote);citations.append(item);}$('paper-comparison').append(citations);
   $('paper-status').textContent='Calculated using the confirmed rules. You can now ask the assistant about this comparison.';
  }catch(error){$('paper-status').textContent=error.message;}finally{button.disabled=false;}
 };
 $('chat-form').onsubmit=async event=>{event.preventDefault();const run=selectedRun();if(!run)return;const button=$('chat-button');button.disabled=true;
  try{$('chat-answer').textContent=status.configured?'Consulting the selected evidence…':'Looking up verified facts…';
   const result=await api(`/api/runs/${run}/chat`,{method:'POST',body:JSON.stringify({question:$('question').value,paper_id:paper?.id||null,reference:confirmedReference,allow_external:$('allow-external').checked})});
   if(selectedRun()!==run)return;$('chat-answer').replaceChildren();const label=document.createElement('strong');label.textContent=result.mode==='model_interpretation'?'AI interpretation — check the cited sources':'Verified evidence lookup — no model used';const answer=document.createElement('p');answer.className='assistant-text';answer.textContent=result.answer;$('chat-answer').append(label,answer);
   const list=document.createElement('ul');list.className='source-list';for(const source of result.sources){const item=document.createElement('li'),link=document.createElement('a');link.href=source.url;link.textContent=source.title+(source.pointer?' ('+source.pointer+')':'');link.target='_blank';link.rel='noopener noreferrer';const quote=document.createElement('blockquote');if(result.mode==='evidence_lookup'&&source.id.startsWith('evidence:'))renderFacts(quote,JSON.parse(source.text));else quote.textContent=source.text.slice(0,1500);item.append(link,quote);list.append(item);}$('chat-answer').append(list);
   const facts=document.createElement('p');facts.className='help';facts.textContent='Verified QC: '+result.verified_metrics.cells_pass.toLocaleString()+' pass; '+result.verified_metrics.cells_fail.toLocaleString()+' excluded; '+result.verified_metrics.cells_total.toLocaleString()+' total cells.';$('chat-answer').append(facts);
  }catch(error){$('chat-answer').textContent=error.message;}finally{button.disabled=false;}
 };
 return {initialize,show};
}
