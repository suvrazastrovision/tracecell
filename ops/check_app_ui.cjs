const fs=require('fs'),vm=require('vm'),assert=require('assert');
class Element{
 constructor(tag='div'){this.tag=tag;this.options=[];this._value='';this.children=[];this.disabled=false;this.checked=false;this.listeners={};}
 get value(){return this._value||this.options[0]?.value||'';}
 set value(v){this._value=String(v);}
 replaceChildren(...items){this.options=[];this.children=items;this._value='';}
 add(option){this.options.push(option);}
 append(...items){this.children.push(...items);}
 setAttribute(key,value){this[key]=value;}
 addEventListener(key,callback){this.listeners[key]=callback;}
 scrollIntoView(){}
 showModal(){this.open=true;}close(){this.open=false;}
 contains(target){return this.children.includes(target);}
}
const elements=new Map();const get=id=>{if(!elements.has(id))elements.set(id,new Element());return elements.get(id);};
const inspection={matrices:{X:{cells:2700,features:32738}},gene_columns:['index','gene_ids']};
let fixture=[{id:'human-old',filename:'old-human.h5ad',status:'ready',inspection}],postResponse=null,posts=[];
const examples=JSON.parse(fs.readFileSync('examples/app-datasets.json','utf8'));
const context=vm.createContext({document:{cookie:'csrf=test',getElementById:get,createElement:tag=>new Element(tag),createElementNS:()=>new Element()},
 Option:class{constructor(label,value){this.label=label;this.value=value;}},console,location:{reload(){}},confirm:()=>false,
 fetch:async(path,options={})=>{
  if(options.method==='POST'){posts.push(path);return postResponse;}
  return {ok:true,json:async()=>path==='/api/session'?{signed_in:false,local:false}:path==='/api/uploads'?fixture:path==='/api/queue-status'?{blocked:false,paused:false}:[]};
 },setInterval(){},crypto:require('crypto').webcrypto});
vm.runInContext(fs.readFileSync('src/pbmc_platform/app/static/processing.js','utf8').replaceAll('export function','function'),context);
vm.runInContext(fs.readFileSync('src/pbmc_platform/app/static/research.js','utf8').replace('export function','function'),context);
vm.runInContext(fs.readFileSync('src/pbmc_platform/app/static/app.js','utf8').replace(/^import[^\n]*\n/gm,''),context);
const run=code=>vm.runInContext(code,context);
context.examples=examples;
(async()=>{
 await run('refresh()');run('renderExamples(examples)');
 const cards=get('examples').children;assert.equal(cards.length,2);
 for(const [i,spec] of Object.values(examples).entries()){
  const card=cards[i],link=card.children.find(x=>x.tag==='a'),button=card.children.find(x=>x.tag==='button');
  assert.equal(link.href,spec.source);assert.equal(link.target,'_blank');assert.equal(link.rel,'noopener noreferrer');
  assert.equal(button.textContent,'Use this sample');assert.match(button['aria-label'],i===0?/Human/:/Mouse/);
 }
 for(const [key,spec] of Object.entries(examples)){
  let release;postResponse=new Promise(resolve=>{release=resolve;});
  get('confirm').checked=true;
  const loading=run(`useExample('${key}',examples['${key}'])`);
  for(const k of ['species','min_genes','max_genes','max_mito_pct','mito_value','source','tissue','reference_genome'])assert.equal(get(k).value,String(spec.example_configuration[k]));
  assert.equal(get('confirm').checked,false);assert.equal(get('run-button').disabled,true);assert.equal(get('config-fields').disabled,true);
  assert(cards.every(card=>card.children.find(x=>x.tag==='button').disabled));
  const before=posts.length;await run("useExample('mouse_brain_1k',examples.mouse_brain_1k)");assert.equal(posts.length,before);
  const id=key+'-new';fixture.push({id,filename:key+'.h5ad',status:'queued_inspection'});
  release({ok:true,json:async()=>({id})});await loading;
  await run('refresh()');assert.equal(get('species').value,spec.species);assert.equal(get('run-button').disabled,true);
  await get('config-form').onsubmit({preventDefault(){},submitter:get('run-button')});assert(!posts.includes('/api/runs'));
  fixture.find(x=>x.id===id).status='ready';fixture.find(x=>x.id===id).inspection=inspection;
  await run('refresh()');assert.equal(get('upload').value,id);assert.equal(get('run-button').disabled,false);
  for(const k of ['counts','gene_ids','gene_symbols'])assert.equal(get(k).value,spec.example_configuration[k]);
  get('max_genes').value='2345';await run('refresh()');assert.equal(get('max_genes').value,'2345');
 }
 postResponse=Promise.resolve({ok:true,json:async()=>({id:'broken'})});fixture.push({id:'broken',filename:'broken.h5ad',status:'failed',error:'Inspection failed'});
 await run("useExample('mouse_brain_1k',examples.mouse_brain_1k)");assert.equal(get('run-button').disabled,true);assert.equal(get('config-fields').disabled,false);
 assert.match(get('sample-status').textContent,/failed/);assert(cards.every(card=>!card.children.find(x=>x.tag==='button').disabled));
 postResponse=Promise.resolve({ok:false,json:async()=>({detail:'Network unavailable'})});
 await run("useExample('human_pbmc3k',examples.human_pbmc3k)");assert.equal(get('run-button').disabled,true);assert.match(get('message').textContent,/Network unavailable/);
 get('upload').value='human-old';get('upload').onchange();assert.equal(get('run-button').disabled,false);assert.equal(get('confirm').checked,false);
 const invalid=new Element();get('advanced-settings').children.push(invalid);get('config-form').listeners.invalid({target:invalid});assert.equal(get('advanced-settings').open,true);
 const html=fs.readFileSync('src/pbmc_platform/app/static/index.html','utf8');
 assert(!html.includes('pbmc-preset'));assert(html.includes('<details id="advanced-settings">'));assert(!html.includes('advanced-settings" open'));
 const ids=[...html.matchAll(/\bid="([^"]+)"/g)].map(x=>x[1]);assert.equal(new Set(ids).size,ids.length);
 context.fetch=async path=>({ok:true,json:async()=>path==='/api/uploads'?fixture:path==='/api/runs'?[{id:'finished',status:'succeeded',configuration:{species:'human'},created_at:'2026-11-06T13:00:00Z'}]:path==='/api/queue-status'?{blocked:false,paused:false}:path.endsWith('/results')?{metrics:{cells_total:2,cells_pass:1,cells_fail:1,features_total:10},limitations:['Research data only']}:path.endsWith('/plots.json')?{total_counts:[10,20],detected_genes:[3,4],mitochondrial_pct:[1,2],passing:[true,false]}:[]});
 await run('refresh()');assert.equal(get('plots').children.length,4);assert.equal(get('results').hidden,false);assert.equal(run('selectedRun'),'finished');
 for(const card of get('plots').children){
  const [image,downloads]=card.children;assert(image.src.includes('.svg?scale=linear'));
  assert.deepEqual(downloads.children.slice(2).map(link=>link.textContent),['PNG','JPEG','PDF']);
 }
 const enlarge=get('plots').children[0].children[1].children[0];assert.equal(enlarge.tag,'button');enlarge.onclick();
 assert.equal(get('plot-viewer').open,true);assert(get('plot-viewer-image').src.includes('.png?scale=linear&preview=true'));
 get('plot-viewer-image').onload();assert.equal(get('plot-viewer-status').textContent,'');
 get('plot-viewer-zoom').onclick();assert.equal(get('plot-viewer-stage').className,'viewer-stage actual-size');
 get('plot-viewer-zoom').onclick();assert.equal(get('plot-viewer-stage').className,'viewer-stage');
 get('plot-viewer-close').onclick();assert.equal(get('plot-viewer').open,false);
 get('plot-scale').value='log';get('plot-scale').onchange();
 assert(get('plots').children.every(card=>card.children[0].src.endsWith('scale=log')));
 assert(get('plots').children.every(card=>card.children[1].children.slice(2).every(link=>link.href.endsWith('scale=log'))));

 context.fetch=async path=>({ok:true,json:async()=>path==='/api/uploads'?fixture:path==='/api/queue-status'?{blocked:true,paused:false}:[]});
 await run('refresh()');assert.match(get('message').textContent,/Operator recovery/);
 const first={id:'first',upload_id:'human-old',input:{key:'source',generation:'v1',sha256:'hash',size:123},status:'succeeded',configuration:examples.human_pbmc3k.example_configuration,created_at:'2026-11-06T13:00:00Z'};
 const second={...first,id:'second',configuration:{...first.configuration,max_mito_pct:10},history_hidden:true};
 const wrongSample={...second,id:'other-file',upload_id:'another-upload',history_hidden:false};
 const wrongMapping={...second,id:'wrong-mapping',history_hidden:false,configuration:{...second.configuration,gene_symbols:'other-column'}};
 let history=[first,second,wrongSample,wrongMapping];let blocked=false;
 context.fetch=async(path,options={})=>({ok:true,json:async()=>{
  if(path==='/api/uploads')return fixture;
  if(path==='/api/runs')return history;
  if(path==='/api/queue-status')return {blocked,paused:false};
  if(path==='/api/history/clear'){history=history.map(x=>({...x,history_hidden:true}));return {cleared:3,retained_blocked:0};}
  if(path.includes('/answer?'))return {facts:{cells_total:2700,cells_pass:2638},json_pointer:'/metrics',evidence:{source:'verified source'}};
  if(path.startsWith('/api/compare?'))return {left_policy:first.configuration,right_policy:second.configuration,left_pass:2638,right_pass:2689,both_pass:2638,only_left:0,only_right:51,limitation:'Technical QC comparison.'};
  return [];
 }});
 get('left').value='first';await run('refresh()');
 assert.equal(get('right').options.length,1);assert.equal(get('right').value,'second');assert.equal(get('compare').disabled,false);
 assert.equal(get('runs').children.length,3);get('show-cleared').checked=true;await get('show-cleared').onchange();assert.equal(get('runs').children.length,4);
 get('prepare-comparison').onclick();assert.equal(get('upload').value,'human-old');assert.equal(get('max_mito_pct').value,'5');assert.equal(get('confirm').checked,false);
 await get('compare').onclick();assert.equal(get('comparison').children[0].tag,'table');assert.equal(run('comparisonPair'),'first|second');
 get('topic').value='qc';await get('ask').onclick();assert.equal(get('answer').children[0].tag,'table');assert.equal(get('evidence-details').hidden,false);assert.match(get('evidence-json').textContent,/verified source/);
 await get('clear-history').onclick();assert.equal(get('show-cleared').checked,false);assert.match(get('history-status').textContent,/saved results were kept/);assert.equal(get('right').value,'second');
 assert.equal(get('runs').textContent,'No processing entries to display.');
 assert(html.includes('gene table row labels'));assert(html.includes('How to create a comparison'));
 assert(!posts.includes('/api/runs'));console.log('PASS: both sample cards, source links, immediate presets, correct inspected file, loading/race guards, edit persistence, failure recovery, advanced-field validation, automatic four-plot display, blocked-queue feedback, guided compatible comparisons, readable evidence and history clearing.');
})().catch(error=>{console.error(error);process.exitCode=1;});
