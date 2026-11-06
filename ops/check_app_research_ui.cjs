// Exercise the real research UI with API fixtures; no model calls or browser automation.
const fs=require('fs'),vm=require('vm'),assert=require('assert');
class Element{
 constructor(tag='div'){this.tag=tag;this.options=[];this.children=[];this.listeners={};this._value='';this.checked=false;}
 get value(){return this._value||this.options[0]?.value||'';}set value(value){this._value=String(value);}
 replaceChildren(...items){this.children=items;this.options=[];this._value='';}append(...items){this.children.push(...items);}
 add(option){this.options.push(option);}setAttribute(key,value){this[key]=value;}addEventListener(key,fn){this.listeners[key]=fn;}
}
const elements=new Map(),$=id=>{if(!elements.has(id))elements.set(id,new Element());return elements.get(id);};
const text='Human blood cells retained at least 200 genes, fewer than 2500 genes, and mitochondrial percentage below 10.';
const paper={id:'b'.repeat(32),run_id:'a'.repeat(32),filename:'methods.pdf',page_count:1,pages:[{page:1,text}]};
const rules=[{field:'min_genes',value:200,operator:'>=',page:1,quote:text},{field:'max_genes',value:2500,operator:'<',page:1,quote:text},{field:'max_mito_pct',value:10,operator:'<',page:1,quote:text}];
let papers=[],status={configured:false,external:false},requests=[],selected='a'.repeat(32);
const api=async(path,options={})=>{
 const body=typeof options.body==='string'?JSON.parse(options.body):null;requests.push({path,options,body});
 if(path==='/api/assistant/status')return status;
 if(options.method==='DELETE'){papers=[];return {status:'deleted'};}
 if(path.includes('/papers?filename=')){papers=[paper];return paper;}
 if(path.endsWith('/papers'))return papers;
 if(path.endsWith('/suggest'))return {rules,species:'human',tissue:'blood',note:'Draft: review each quote.',confirmed:false};
 if(path.endsWith('/compare'))return {cells_total:2700,current_pass:2638,reference_pass:2689,both_pass:2638,only_current:0,only_reference:51,warnings:['Technical reference only.']};
 if(path.endsWith('/chat'))return {mode:status.configured?'model_interpretation':'evidence_lookup',answer:'A sourced explanation.',sources:[{id:'evidence:metrics',title:'Verified metrics',url:'/verified',text:'{"cells_pass":2638}'}],verified_metrics:{cells_pass:2638,cells_fail:62,cells_total:2700}};
 return paper;
};
const context=vm.createContext({document:{createElement:tag=>new Element(tag)},Option:class{constructor(label,value){this.label=label;this.value=value;}},console});
vm.runInContext(fs.readFileSync('src/pbmc_platform/app/static/research.js','utf8').replace('export function','function'),context);
const table=container=>{container.replaceChildren();container.append(new Element('table'));};
const ui=context.createResearchUI({$,api,message:()=>{},renderFacts:table,resultTable:table,selectedRun:()=>selected});
const submit=id=>$(id).onsubmit({preventDefault(){}});
(async()=>{
 await ui.initialize();await ui.show(selected);assert.match($('model-status').textContent,/not connected/);
 $('question').value='Why were cells excluded?';await submit('chat-form');assert.equal(requests.at(-1).body.reference,null);
 assert.match($('chat-answer').children[0].textContent,/no model used/);
 const beforeOversize=requests.length;
 $('paper-file').files=[{name:'too-large.pdf',size:100000001,arrayBuffer:async()=>{throw Error('Oversize file must not be read');}}];await submit('paper-upload-form');
 assert.equal(requests.length,beforeOversize);assert.match($('paper-status').textContent,/100 MB/);
 // Simulate the exact size boundary without allocating a large browser fixture.
 $('paper-file').files=[{name:'methods.pdf',size:100000000,arrayBuffer:async()=>new ArrayBuffer(50)}];await submit('paper-upload-form');
 assert.equal($('paper-select').value,paper.id);assert.equal($('paper-content').hidden,false);assert.equal($('paper-page-text').value,text);assert.equal($('paper-suggest').disabled,true);
 status={configured:true,external:true,provider:'openai',model:'test-small-model'};await ui.initialize();assert.equal($('model-sharing').hidden,false);
 await $('paper-suggest').onclick();assert.equal($('reference-confirm').checked,false);assert.equal($('ref-value-max_mito_pct').value,'10');
 $('reference-confirm').checked=true;$('paper-reference-form').listeners.input({target:{id:'reference-confirm'}});assert.equal($('reference-confirm').checked,true);
 await submit('paper-reference-form');assert.equal($('paper-comparison').children[0].tag,'table');
 $('allow-external').checked=true;$('question').value='Compare the confirmed paper rules with my run.';await submit('chat-form');
 assert.equal(requests.at(-1).body.reference.rules.length,3);assert.equal(requests.at(-1).body.paper_id,paper.id);assert.equal(requests.at(-1).body.allow_external,true);
 $('paper-reference-form').listeners.input({target:{id:'ref-value-max_mito_pct'}});assert.equal($('reference-confirm').checked,false);
 await submit('chat-form');assert.equal(requests.at(-1).body.reference,null);
 await $('paper-remove').onclick();assert.equal($('paper-select').value,'');assert.equal($('paper-content').hidden,true);
 console.log('PASS: question box, honest model status, paper upload, PDF text, draft review, confirmation, cited comparison context and paper removal.');
})().catch(error=>{console.error(error);process.exitCode=1;});
