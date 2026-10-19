// Actual generated layout engine, storage and boundary code with simulated DOM.
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const script=fs.readFileSync(process.argv[2],'utf8');
const engine=script.slice(script.indexOf('    // LAYOUT V5:'),script.indexOf('    /* ========================================================\n       FIND STREAMLIT BRIDGE BUTTON'));
const heights=script.slice(script.indexOf('    let DEFAULT_DASHBOARD_HEIGHT'),script.indexOf('    function syncDrilldownGap'));
const apply=script.slice(script.indexOf('    function applyDashboardHeight'),script.indexOf('    function setupDashboardBoundary'));
const storage=new Map([['npi_control_tower_top01_layout_v2','legacy-panels'],['npiEnquiryFinderLayoutV1','legacy-enquiry'],['npi_control_tower_dashboard_stage_height_v2','300']]);
function setup(owner,width=1280){
 const listeners={},panels={},style=()=>({setProperty(k,v){this[k]=v}});
 const stage={style:style(),dataset:{}},canvas={style:style()},scroll={clientWidth:width};
 const donut={style:style()};
 for(const id of ['delayCommitment','enquiry','executive','delayBucket','hotspot'])panels[id]={style:style(),querySelector:()=>id==='executive'?donut:null};
 const document={getElementById:id=>({'npi-floating-stage':stage,'npi-layout-canvas':canvas,'npi-dashboard-scroll':scroll}[id]),
 querySelector:s=>panels[s.match(/data-layout-id="(.*?)"/)[1]]};
 const window={parent:{localStorage:{getItem:k=>storage.get(k)||null,setItem:(k,v)=>storage.set(k,v),removeItem:k=>storage.delete(k)}},
 addEventListener:(type,fn)=>listeners[type]=fn,dispatchEvent:()=>{}};
 const context=vm.createContext({window,document,Event:function(){},ResizeObserver:undefined,clearTimeout:()=>{},setTimeout:()=>{}});
 vm.runInContext(engine.replace('"opaque-test-owner"',JSON.stringify(owner))+heights+apply,context);
 return {eval:s=>vm.runInContext(s,context),scroll,stage,canvas,window};
}
const a=setup('hash-user-A');
for(const width of [320,360,412,760,761,900,1199,1200,1280,1600,1920]){
 assert.equal(a.eval('validRects(defaultLayout('+width+').rects,'+width+')'),true);
 const mode=a.eval('defaultLayout('+width+').mode');
 assert.equal(mode,width>=1200?'desktop':width>760?'tablet':'mobile');
}
console.log('PASS: all five widgets fit without overlap at 11 widths, with desktop/tablet/mobile breakpoints');
a.eval('loadLayout()');assert.equal(a.stage.style.height,'624px');assert.equal(storage.size,3);
assert.equal(a.eval('currentLayout.rects.delayCommitment.top'),12);
assert(a.eval('currentLayout.rects.hotspot.left>currentLayout.rects.delayBucket.left'));
console.log('PASS: dominant top commitment, top-right enquiry and bottom-right hotspot; defaults do not save preferences');
a.eval('applyDashboardHeight(1,true)');assert.equal(a.stage.style.height,'300px');
assert(a.eval('currentLayout.height')>300);
assert.equal(a.eval('readDashboardHeight()'),300);
const b=setup('hash-user-B');b.eval('loadLayout()');assert.equal(b.stage.style.height,'624px');
b.eval('applyDashboardHeight(680,true)');
console.log('PASS: boundary min300 and scroll canvas remain; user B cannot load user A preference');
a.scroll.clientWidth=900;a.eval('reflowLayout()');assert.equal(a.stage.style.height,'1086px');
a.eval('applyDashboardHeight(700,true)');
a.scroll.clientWidth=1280;a.eval('reflowLayout()');assert.equal(a.stage.style.height,'300px');
a.scroll.clientWidth=900;a.eval('reflowLayout()');assert.equal(a.stage.style.height,'700px');
console.log('PASS: tablet content sets a readable default; viewport-specific boundary preferences restore independently');
a.scroll.clientWidth=1280;a.eval('reflowLayout()');
assert.equal(a.eval("updatePanel('enquiry',{width:310},true)"),true);
assert.equal(a.eval("updatePanel('enquiry',{left:12,top:12},true)"),false);
const restored=setup('hash-user-A');restored.eval('loadLayout()');assert.equal(restored.eval('currentLayout.rects.enquiry.width'),310);
const differentWidth=setup('hash-user-A',1350);differentWidth.eval('loadLayout()');assert.equal(differentWidth.eval('currentLayout.rects.enquiry.width'),320);
console.log('PASS: explicit custom resize persists, overlap rejected; another width uses safe defaults');
a.eval('resetLayout()');assert.equal(a.stage.style.height,'624px');
assert.equal(a.eval('currentLayout.rects.enquiry.width'),320);
assert.equal(storage.size,4);
assert.equal(setup('hash-user-B').eval('loadLayout();readDashboardHeight()'),680);
assert.equal(storage.get('npiEnquiryFinderLayoutV1'),'legacy-enquiry');
console.log('PASS: Reset clears all five widget/boundary preferences across modes for current user; legacy data untouched');
const afterReset=setup('hash-user-A',900);afterReset.eval('loadLayout()');assert.equal(afterReset.stage.style.height,'1086px');
const mobile=setup('hash-user-A',390);mobile.eval('loadLayout()');assert.equal(mobile.stage.style.height,'1780px');
const mobileRects=JSON.parse(mobile.eval('JSON.stringify(currentLayout.rects)'));
assert.deepEqual(Object.entries(mobileRects).sort((a,b)=>a[1].top-b[1].top).map(x=>x[0]),['delayCommitment','executive','hotspot','delayBucket','enquiry']);
console.log('PASS: mobile natural height avoids clipping; order correct; reset extends to tablet');
