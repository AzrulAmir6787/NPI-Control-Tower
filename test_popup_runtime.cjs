// Execute actual popup and bridge JavaScript in independent VM contexts.
// This simulates messages/iframe reload; it is not a browser DOM lifecycle test.
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const app=fs.readFileSync(__dirname+'/app.py','utf8');
const main=app.slice(app.indexOf('function npiPopupMain(config)'),app.indexOf("            const root = document.querySelector('.npi-drilldown-wrap')"));
assert(main && !main.includes('window.opener'));
const bridgeCode=fs.readFileSync(__dirname+'/npi_drilldown_component/index.html','utf8').match(/<script>([\s\S]*?)<\/script>/)[1];
const channels=[];
function BroadcastChannel(){
 this.listeners=[];channels.push(this);
 this.addEventListener=(type,fn)=>this.listeners.push(fn);
 this.postMessage=data=>channels.filter(c=>c!==this && !c.closed).forEach(c=>c.listeners.forEach(fn=>fn({data})));
 this.close=()=>this.closed=true;
}
const parentMessages=[],listeners={},loadListeners={};let root={scrollTop:0,scrollLeft:0},domScroll={scrollTop:0,scrollLeft:0};
let reloads=0;
const frame={style:{},addEventListener:(t,fn)=>loadListeners[t]=fn,
 get contentDocument(){return {querySelector:()=>root,scrollingElement:domScroll}},
 contentWindow:{scrollTo:(x,y)=>Object.assign(domScroll,{scrollLeft:x,scrollTop:y})}};
Object.defineProperty(frame,'srcdoc',{set(html){reloads++;root={scrollTop:0,scrollLeft:0};domScroll={scrollTop:0,scrollLeft:0}}});
const bridgeWindow={BroadcastChannel,parent:{postMessage:m=>parentMessages.push(m)},addEventListener:(type,fn)=>listeners[type]=fn};
vm.runInNewContext(bridgeCode,{window:bridgeWindow,document:{getElementById:()=>frame},BroadcastChannel});
const outcomes=[];
function render(html='initial'){
 listeners.message({data:{type:'streamlit:render',args:{popup_bridge_id:'bridge',viewport_key:'page2',html_content:html,popup_results:outcomes,height:760}}});
 if(loadListeners.load)loadListeners.load();
}
function makePopup(id){
 const fields={};for(const n of ['owner','commitment-date','completion-date','remark'])fields['detail-'+n]={value:'',disabled:false,addEventListener:()=>{}};
 const status={textContent:''};let click;const button={disabled:false,textContent:'Save Commitment',addEventListener:(t,fn)=>click=fn};
 const handlers={},timers=new Map();let nextTimer=0,uuid=0;
 const popupWindow={BroadcastChannel,closed:false,crypto:{randomUUID:()=>id+'-r'+(++uuid)},addEventListener:(t,fn)=>handlers[t]=fn};
 popupWindow.postMessage=data=>handlers.message({data});
 popupWindow.npiCommitmentBridge={closed:false,postMessage:data=>listeners.message({data,source:popupWindow})};
 const document={getElementById:n=>n==='detail-save-commitment'?button:n==='detail-save-status'?status:fields[n]};
 vm.runInNewContext('('+main+')('+JSON.stringify({bridge_id:'bridge',popup_id:id,part_key:'A'})+')',
  {window:popupWindow,document,Date,Math,Object,BroadcastChannel,setTimeout:fn=>{timers.set(++nextTimer,fn);return nextTimer},clearTimeout:n=>timers.delete(n)});
 return {window:popupWindow,fields,button,status,click:()=>click(),timeout:()=>{const f=[...timers.values()].at(-1);if(f)f()}};
}
function requests(){return parentMessages.filter(m=>m.type==='streamlit:setComponentValue').map(m=>m.value)}
function ack(request,status='saved',values){return {type:'npi_commitment_ack',bridge_id:'bridge',popup_id:request.payload.popup_id,request_id:request.payload.request_id,status,values}}
render();const p=makePopup('popup1');root.scrollTop=6500;root.scrollLeft=1400;
for(let i=1;i<=4;i++){
 p.fields['detail-owner'].value='Owner'+i;p.click();
 assert.equal(p.button.disabled,true);assert.notEqual(p.status.textContent,'Saved');
 // Double click while pending must not create a fresh request.
 const count=requests().length;p.click();assert.equal(requests().length,count);
 const request=requests().at(-1);
 p.window.postMessage({...ack(request),request_id:'wrong'});assert.notEqual(p.status.textContent,'Saved');
 outcomes.push(ack(request,'saved',{owner:'Owner'+i}));render('updated'+i);
 assert.equal(p.status.textContent,'Saved');assert.equal(p.button.disabled,false);
 assert.equal(root.scrollTop,6500);assert.equal(root.scrollLeft,1400);
}
assert.equal(new Set(requests().map(r=>r.payload.request_id)).size,4);
console.log('PASS: 4 consecutive popup saves with iframe replacement; fresh IDs; server-only Saved; scroll retained');
const original=outcomes[0];p.window.postMessage(original);assert.equal(p.fields['detail-owner'].value,'Owner4');
console.log('PASS: stale/unmatched acknowledgments cannot overwrite current popup');
p.click();let pending=requests().at(-1),before=requests().length;
p.timeout();assert.equal(p.button.textContent,'Retry confirmation');p.click();
assert.equal(requests().length,before+1);assert.equal(requests().at(-1).payload.request_id,pending.payload.request_id);
assert.equal(requests().at(-1).delivery_attempt,2);
outcomes.push({...ack(pending,'unknown'),message:'Outcome unconfirmed'});render('unknown');
assert.equal(p.status.textContent,'Outcome unconfirmed');assert.notEqual(p.status.textContent,'Saved');
before=requests().length;p.click();assert.equal(requests().length,before);assert.equal(p.status.textContent,'Outcome unconfirmed');
console.log('PASS: lost-ack retry reuses ID; cached uncertain outcome does not resend write or show Saved');
const q=makePopup('popup2');q.click();const failed=requests().at(-1);
outcomes.push({...ack(failed,'error'),message:'Access denied; nothing saved'});render('error');
assert.equal(q.button.disabled,false);assert.equal(q.fields['detail-owner'].disabled,false);q.click();
assert.notEqual(requests().at(-1).payload.request_id,failed.payload.request_id);
console.log('PASS: definite no-write failure permits fresh retry');
const retry=requests().at(-1);const other=makePopup('popup3');before=requests().length;other.click();assert.equal(requests().length,before);
outcomes.push(ack(retry));render('queue');assert.equal(requests().at(-1).payload.popup_id,'popup3');
console.log('PASS: simultaneous popup requests are queued until prior server outcome');
outcomes.push(ack(requests().at(-1)));render('queue-done');
const fallback=makePopup('popup4');fallback.window.npiCommitmentBridge=null;
fallback.click();const channelRequest=requests().at(-1);assert.equal(channelRequest.payload.popup_id,'popup4');
outcomes.push(ack(channelRequest));render('channel');assert.equal(fallback.status.textContent,'Saved');
console.log('PASS: popup BroadcastChannel fallback receives correlated server acknowledgment');
assert(reloads>=5);console.log('LIMIT: simulated VM contexts and DOM; actual popup lifecycle, browser delivery and Supabase not exercised');
