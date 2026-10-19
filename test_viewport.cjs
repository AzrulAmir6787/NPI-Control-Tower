// Offline component regression: execute the shipped script with a simulated iframe.
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const source=fs.readFileSync(__dirname+'/npi_drilldown_component/index.html','utf8').match(/<script>([\s\S]*?)<\/script>/)[1];
function setup(){
 const messages=[], handlers={}, loads={};let reloads=0,channelListener;
 let root={scrollTop:0,scrollLeft:0},documentScroll={scrollTop:0,scrollLeft:0};
 const frame={style:{},addEventListener:(type,fn)=>loads[type]=fn,
  get contentDocument(){return {querySelector:()=>root,scrollingElement:documentScroll}},
  contentWindow:{scrollTo:(x,y)=>{documentScroll.scrollLeft=x;documentScroll.scrollTop=y}}};
 Object.defineProperty(frame,'srcdoc',{set(value){reloads++;root={scrollTop:0,scrollLeft:0};documentScroll={scrollTop:0,scrollLeft:0}}});
 const window={parent:{postMessage:m=>messages.push(m)},addEventListener:(type,fn)=>handlers[type]=fn};
 function BroadcastChannel(){this.addEventListener=(type,fn)=>channelListener=fn}
 window.BroadcastChannel=BroadcastChannel;
 vm.runInNewContext(source,{window,document:{getElementById:()=>frame},BroadcastChannel,Number,Object});
 return {messages,root:()=>root,doc:()=>documentScroll,reloads:()=>reloads,
  load:()=>loads.load(),render:(key,html)=>handlers.message({data:{type:'streamlit:render',args:{viewport_key:key,html_content:html,height:760}}}),
  popup:data=>channelListener({data}),save:data=>handlers.message({data})};
}
const t=setup();
t.render('page1','old');t.load();
Object.assign(t.root(),{scrollTop:6200,scrollLeft:1800});Object.assign(t.doc(),{scrollTop:120,scrollLeft:15});
t.render('page1','latest');t.load();
assert.equal(t.root().scrollTop,6200);assert.equal(t.root().scrollLeft,1800);assert.equal(t.doc().scrollTop,120);
console.log('PASS: changed Latest HTML restores vertical/horizontal and inner document scroll');
const count=t.reloads();t.render('page1','latest');assert.equal(t.reloads(),count);
console.log('PASS: unchanged HTML leaves live inputs and scroll untouched');
t.render('page1','intermediate');t.render('page1','latest2');t.load();assert.equal(t.root().scrollTop,6200);
console.log('PASS: successive pre-save/post-save renders retain pending snapshot');
t.render('page2','latest2');t.load();assert.equal(t.root().scrollTop,0);assert.equal(t.root().scrollLeft,0);
console.log('PASS: page navigation resets even if HTML is identical');
t.root().scrollTop=750;t.render('page2','next');t.render('new-customer','next');t.load();assert.equal(t.root().scrollTop,0);
console.log('PASS: filter/customer change discards pending old-view scroll');
const event={type:'npi_commitment_save',payload:{request_id:'popup-test',part_key:'A'}};
t.popup(event);t.save(event);
assert.equal(t.messages.filter(x=>x.type==='streamlit:setComponentValue').length,2);
console.log('PASS: BroadcastChannel popup and normal save forwarding unchanged');
