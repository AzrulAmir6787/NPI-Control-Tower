// Actual native overlay installer, with simulated DOM geometry and observer lifecycle.
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const script=fs.readFileSync(process.argv[2],'utf8');
function target(){return {handlers:{},addEventListener(type,fn){(this.handlers[type]??=[]).push(fn)},removeEventListener(type,fn){this.handlers[type]=(this.handlers[type]||[]).filter(f=>f!==fn)}};}
const callbacks=[],observers=[];let scrollTop=0;
const rect=(left,top,width,height)=>({left,top,right:left+width,bottom:top+height,width,height});
const body={getBoundingClientRect:()=>rect(20,30,1400,2000),scrollLeft:5,scrollTop:10,clientLeft:2,clientTop:3};
const header={style:{}};
const wrapper={style:{},parentElement:body,getAttribute:k=>k==='data-testid'?'stElementContainer':null};
const panel={style:{setProperty(k,v){this[k]=v}},parentElement:wrapper,offsetParent:body,querySelector:()=>header,querySelectorAll:()=>[],contains:()=>false};
const scroll=Object.assign(target(),{clientHeight:606,scrollHeight:606,getBoundingClientRect:()=>rect(0,0,1280,606)});
const slot={getBoundingClientRect:()=>rect(948,12-scrollTop,320,220)};
const api={owner:'opaque-test-owner',getLayout:()=>({mode:'desktop',rects:{enquiry:{left:948,top:12}}})};
const frameWindow=Object.assign(target(),{__npiDashboardLayoutV5:api});
const frame={contentWindow:frameWindow,contentDocument:{querySelector:()=>slot,getElementById:()=>scroll},getBoundingClientRect:()=>rect(100,200,1280,624)};
const parentDoc=Object.assign(target(),{body,querySelector:()=>panel,querySelectorAll:()=>[frame]});
const parentWin=Object.assign(target(),{document:parentDoc,
 requestAnimationFrame:fn=>{callbacks.push(fn);return callbacks.length},cancelAnimationFrame:()=>{},
 setTimeout:()=>1,clearTimeout:()=>{},
 MutationObserver:function(fn){this.fn=fn;this.observe=()=>{};this.disconnect=()=>this.disconnected=true;observers.push(this)}});
const installerWrapper={style:{},parentElement:body,getAttribute:k=>k==='data-testid'?'stElementContainer':null};
const installer={style:{},parentElement:installerWrapper,getAttribute:()=>null};
const win={parent:parentWin,frameElement:installer};
function drain(){let n=0;while(callbacks.length){callbacks.shift()();assert(++n<30)}}
vm.runInNewContext(script,{window:win});drain();
assert.equal(panel.style.left,'1031px');assert.equal(panel.style.top,'189px');assert.equal(panel.style.width,'320px');assert.equal(panel.style.height,'220px');
assert.equal(panel.style.visibility,'visible');assert.equal(panel.style.resize,'both');assert.equal(wrapper.style.display,'contents');
console.log('PASS: native form kept in place; overlay coordinates include frame, offset parent, borders and scrolling');
scrollTop=100;scroll.handlers.scroll[0]();drain();assert.equal(panel.style.clipPath,'inset(88px 0px 0px 0px)');
scrollTop=300;scroll.handlers.scroll[0]();drain();assert.equal(panel.style.visibility,'hidden');
console.log('PASS: dashboard scrolling clips/hides enquiry overlay before it can cover boundary or Drilldown');
const oldListeners=parentDoc.handlers.pointerdown.length;
vm.runInNewContext(script,{window:win});drain();assert.equal(parentDoc.handlers.pointerdown.length,oldListeners);assert.equal(observers[0].disconnected,true);
console.log('PASS: rerun disposes old handlers and observers instead of accumulating controllers');
vm.runInNewContext(script.replace('"opaque-test-owner"','"another-user-hash"'),{window:win});drain();assert.equal(panel.style.visibility,'hidden');
api.owner='another-user-hash';api.getLayout=()=>({mode:'mobile',rects:{enquiry:{left:948,top:12}}});scrollTop=0;
observers.at(-1).fn();drain();assert.equal(panel.style.visibility,'visible');assert.equal(panel.style.resize,'none');
console.log('PASS: other-account frame rejected; account switch finds matching owner; mobile disables resize');
