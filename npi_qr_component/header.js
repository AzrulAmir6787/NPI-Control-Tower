(function () {
    'use strict';
    const button = document.getElementById('scan-qr'), status = document.getElementById('status');
    const targets = new Map(), queue = [], queued = new Set(), outcomes = new Map();
    let bridgeId = null, builder = '', resources = null, inFlight = null, savedScroll = null;
    const id = () => crypto.randomUUID();
    const deviceAllowed = () => {
        const ua = navigator.userAgent || '';
        return /Android|iPhone|iPad|iPod/i.test(ua)
            || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1)
            || !!(navigator.userAgentData && navigator.userAgentData.mobile);
    };
    function send(type, extra) {
        window.parent.postMessage(Object.assign({isStreamlitMessage:true, type:type}, extra || {}), '*');
    }
    function frameHeight() {
        const allowed = deviceAllowed();
        // A hidden frame leaves no extra header row on desktop.
        try {
            let wrapper = window.frameElement;
            while (wrapper && wrapper.getAttribute('data-testid') !== 'stElementContainer') wrapper = wrapper.parentElement;
            if (wrapper) wrapper.style.display = allowed ? '' : 'none';
        } catch (_) {}
        document.body.style.display = allowed ? 'flex' : 'none';
        send('streamlit:setFrameHeight', {height:allowed ? 44 : 0});
        button.disabled = !allowed || !resources || !bridgeId || !builder;
    }
    function captureScroll() {
        try {
            const d = parent.document, main = d.querySelector('[data-testid="stMain"]');
            savedScroll = {main, top:main ? main.scrollTop : 0, left:main ? main.scrollLeft : 0,
                x:parent.scrollX, y:parent.scrollY};
        } catch (_) {}
    }
    function restoreScroll() {
        if (!savedScroll) return;
        const saved = savedScroll; savedScroll = null;
        requestAnimationFrame(() => {
            try {
                if (saved.main && saved.main.isConnected) {saved.main.scrollTop=saved.top; saved.main.scrollLeft=saved.left;}
                parent.scrollTo(saved.x, saved.y);
            } catch (_) {}
        });
    }
    function flush() {
        if (inFlight || !queue.length) return;
        inFlight = queue.shift(); captureScroll();
        send('streamlit:setComponentValue', {value:inFlight, dataType:'json'});
    }
    function publish(result) {
        const target = targets.get(result.scan_id || result.popup_id);
        if (!target || target.window.closed) return;
        target.window.postMessage({type:'npi_qr_delivery', nonce:target.nonce, result:result}, location.origin);
        // M6 Save runtime receives its unchanged acknowledgment protocol.
        if (result.type === 'npi_commitment_ack') target.window.postMessage(result, location.origin);
    }
    function enqueue(event) {
        const requestId = event.payload.request_id;
        const result = outcomes.get(requestId);
        if (result) {publish(result); return;}
        if (queued.has(requestId)) {
            if (inFlight && inFlight.payload.request_id === requestId
                && Number(event.delivery_attempt) > Number(inFlight.delivery_attempt || 0)) {
                inFlight=event;
                send('streamlit:setComponentValue', {value:inFlight, dataType:'json'});
            }
            return;
        }
        queued.add(requestId); queue.push(event); flush();
    }
    function accept(event) {
        if (event.origin !== location.origin) return;
        const data = event.data || {};
        if (data.type === 'npi_qr_lookup') {
            const target=targets.get(data.scan_id);
            if (!target || target.window !== event.source || target.nonce !== data.nonce || !data.request_id) return;
            enqueue({type:'npi_qr_lookup', bridge_id:bridgeId,
                payload:{scan_id:data.scan_id, request_id:data.request_id, part_number:data.part_number}});
        } else if (data.type === 'npi_commitment_save') {
            const payload=data.payload || {}, target=targets.get(payload.popup_id);
            if (!target || target.window !== event.source || payload.bridge_id !== bridgeId || !payload.request_id) return;
            enqueue({type:'npi_commitment_save', bridge_id:bridgeId, payload:payload,
                delivery_attempt:Number(data.delivery_attempt || 0)});
        }
    }
    window.addEventListener('message', function (event) {
        const data=event.data || {};
        if (event.source === window.parent && data.type === 'streamlit:render') {
            const args=data.args || {};
            if (bridgeId && args.bridge_id !== bridgeId) {
                for (const target of targets.values()) {
                    if (!target.window.closed) target.window.postMessage({type:'npi_qr_invalidated',nonce:target.nonce},location.origin);
                }
                targets.clear(); queue.length=0; queued.clear(); outcomes.clear(); inFlight=null;
            }
            bridgeId=args.bridge_id; builder=args.detail_builder || '';
            for (const result of args.results || []) {
                if (result.bridge_id !== bridgeId || !result.request_id) continue;
                outcomes.set(result.request_id,result); publish(result);
                if (inFlight && inFlight.payload.request_id === result.request_id) inFlight=null;
            }
            flush(); restoreScroll(); frameHeight(); return;
        }
        accept(event);
    });
    const safeScript = value => value.replace(/<\/script/gi, '<\\/script');
    button.addEventListener('click', function () {
        if (button.disabled || !deviceAllowed()) return;
        // All resources and configuration are ready BEFORE the user's click.
        const scanId=id(), nonce=id();
        const config=JSON.stringify({scan_id:scanId,nonce:nonce,bridge_id:bridgeId}).replace(/</g,'\\u003c');
        const scripts='<scr'+'ipt>'+safeScript(resources.decoder)+'</scr'+'ipt>'
            +'<scr'+'ipt>'+safeScript(builder)+'</scr'+'ipt>'
            +'<scr'+'ipt>const npiQRConfig='+config+';'+safeScript(resources.scanner)+'</scr'+'ipt>';
        const html=resources.document.replace('<!--NPI_QR_BOOTSTRAP-->',scripts);
        const url=URL.createObjectURL(new Blob([html],{type:'text/html;charset=utf-8'}));
        let scanner;
        try {scanner=window.open(url,'_blank');} catch (_) {}
        if (!scanner) {URL.revokeObjectURL(url); status.textContent='Scanner blocked. Allow pop-ups for this app and retry.'; return;}
        targets.set(scanId,{window:scanner,nonce:nonce});
        status.textContent='';
        // Remove only closed references; active Part Detail bridges stay available.
        for (const [key,target] of targets) if (target.window.closed) targets.delete(key);
    });
    async function prepare() {
        try {
            const paths=['scanner.html','scanner.js','vendor/jsQR.js'];
            const [document,scanner,decoder]=await Promise.all(paths.map(async path=>{
                const r=await fetch(new URL(path,documentBase()),{credentials:'same-origin'});
                if (!r.ok) throw Error('resource'); return r.text();
            }));
            resources={document,scanner,decoder}; status.textContent='';
        } catch (_) {status.textContent='Unable to load scanner. Refresh the dashboard to retry.';}
        frameHeight();
    }
    function documentBase() {return document.baseURI;}
    send('streamlit:componentReady',{apiVersion:1}); frameHeight(); prepare();
})();
