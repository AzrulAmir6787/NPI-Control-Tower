(function () {
    'use strict';
    const config=npiQRConfig, bridge=window.opener, video=document.getElementById('preview');
    const status=document.getElementById('status'), part=document.getElementById('part');
    const retry=document.getElementById('retry'), open=document.getElementById('open-detail');
    const canvas=document.createElement('canvas'), context=canvas.getContext('2d',{willReadFrequently:true});
    let stream=null, frame=0, generation=0, state='idle', detector=null, nativeReady=false;
    let busy=false, lastDecode=0, pending=null, detailURL=null, transitioning=false;
    const id=()=>crypto.randomUUID();
    function stopCamera() {
        generation++; cancelAnimationFrame(frame); frame=0;
        if(stream) stream.getTracks().forEach(track=>track.stop());
        stream=null; video.srcObject=null;
    }
    function message(text, action) {
        status.textContent=text; retry.hidden=!action;
        if(action) retry.textContent=action;
    }
    function fail(text) {state='error';stopCamera();message(text,'Retry camera');}
    function close() {
        state='closed';pending=null;stopCamera();
        if(detailURL) URL.revokeObjectURL(detailURL);
        window.close(); message('Scanner closed. You can return to the dashboard.');
    }
    async function prepareDecoder() {
        if ('BarcodeDetector' in window) {
            try {
                const formats=await BarcodeDetector.getSupportedFormats();
                if(formats.includes('qr_code')) {detector=new BarcodeDetector({formats:['qr_code']});nativeReady=true;}
            } catch (_) {}
        }
        if(!nativeReady && typeof jsQR!=='function') throw Error('decoder');
    }
    async function startCamera() {
        if(state==='requesting' || state==='closed' || transitioning) return;
        stopCamera();pending=null;part.textContent='';open.hidden=true;state='requesting';
        const epoch=generation;message('Allow camera access to scan a Part Number.');
        if(!bridge || bridge.closed) {fail('Dashboard connection is unavailable. Close this tab and reopen Scan QR.');return;}
        if(!window.isSecureContext || !navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
            fail('Camera requires a supported browser and HTTPS. Open the secure app link in Chrome.');return;
        }
        let acquired;
        try {
            acquired=await navigator.mediaDevices.getUserMedia({audio:false,video:{facingMode:{ideal:'environment'}}});
            if(epoch!==generation || state==='closed' || document.visibilityState==='hidden') {
                acquired.getTracks().forEach(track=>track.stop());
                if(state!=='closed') {state='paused';message('Camera paused. Tap Resume Camera.','Resume Camera');}
                return;
            }
            stream=acquired;video.srcObject=stream;await video.play();
            await prepareDecoder();
            if(epoch!==generation || state==='closed') return;
            stream.getVideoTracks().forEach(track=>track.addEventListener('ended',()=>{
                if(state==='scanning') fail('Camera disconnected or permission was removed.');
            },{once:true}));
            state='scanning';message('Scanning… Keep the QR code inside the frame.');
            frame=requestAnimationFrame(tick);
        } catch(error) {
            if(acquired && acquired!==stream) acquired.getTracks().forEach(track=>track.stop());
            if(epoch!==generation || state==='closed') return;
            const descriptions={NotAllowedError:'Camera permission denied. Allow camera access in browser site settings, then retry.',
                NotFoundError:'No available camera was found.',NotReadableError:'Camera is busy or unavailable. Close other camera apps and retry.',
                OverconstrainedError:'The camera could not use the requested settings.'};
            fail(descriptions[error.name] || 'Unable to start the camera or QR decoder. Please retry.');
        }
    }
    async function decode() {
        if(nativeReady) {
            try {const values=await detector.detect(video);return values.length ? values[0].rawValue : null;}
            catch (_) {nativeReady=false;}
        }
        const ratio=Math.min(1,640/video.videoWidth);canvas.width=Math.round(video.videoWidth*ratio);canvas.height=Math.round(video.videoHeight*ratio);
        context.drawImage(video,0,0,canvas.width,canvas.height);
        const pixels=context.getImageData(0,0,canvas.width,canvas.height);
        const result=jsQR(pixels.data,pixels.width,pixels.height,{inversionAttempts:'attemptBoth'});
        return result ? result.data : null;
    }
    async function tick(now) {
        if(state!=='scanning') return;
        if(!busy && now-lastDecode>=180 && video.readyState>=2 && video.videoWidth) {
            const epoch=generation;busy=true;lastDecode=now;
            try {
                const text=await decode();
                if(epoch===generation && state==='scanning' && text!=null) detected(text);
            } catch (_) {if(epoch===generation) fail('QR decoding failed. Please retry.');}
            finally {busy=false;}
        }
        if(state==='scanning') frame=requestAnimationFrame(tick);
    }
    function detected(value) {
        const text=String(value).trim();
        if(!text || text.length>1024 || /[\x00-\x1f]/.test(text)) {
            state='invalid';stopCamera();message('QR must contain one Part Number only.','Scan Again');return;
        }
        state='matching';cancelAnimationFrame(frame);part.textContent=text;
        pending=id();message('Finding Part Number…');
        if(!bridge || bridge.closed) {fail('Dashboard connection is unavailable. Reopen Scan QR from the dashboard.');return;}
        bridge.postMessage({type:'npi_qr_lookup',scan_id:config.scan_id,nonce:config.nonce,
            request_id:pending,part_number:text},location.origin);
    }
    function navigate() {
        if(!detailURL) return;
        stopCamera();transitioning=true;
        try {location.replace(detailURL);} catch (_) {
            transitioning=false;message('Part found. Tap Open Part Detail.');open.hidden=false;
        }
    }
    window.addEventListener('message',function(event) {
        if(event.source!==bridge || event.origin!==location.origin) return;
        const data=event.data || {};
        if(data.nonce!==config.nonce) return;
        if(data.type==='npi_qr_invalidated') {pending=null;fail('Session changed. Close this tab and sign in again on the dashboard.');return;}
        if(data.type!=='npi_qr_delivery') return;
        const result=data.result || {};
        if(state!=='matching' || result.type!=='npi_qr_result' || result.scan_id!==config.scan_id
            || result.bridge_id!==config.bridge_id || result.request_id!==pending) return;
        pending=null;
        if(result.status!=='matched') {
            state='result';stopCamera();message(result.message || 'Part Number not found.','Scan Again');return;
        }
        state='matched';stopCamera();
        try {
            const html=npiBuildDetail(result.detail,config.bridge_id,config.scan_id);
            detailURL=URL.createObjectURL(new Blob([html],{type:'text/html;charset=utf-8'}));
            // Fallback is ready before navigation. No asynchronous new-tab opening.
            open.hidden=false;message('Part found. Opening Part Detail…');navigate();
        } catch (_) {message('Unable to prepare Part Detail. Close this tab and retry from the dashboard.');}
    });
    retry.addEventListener('click',startCamera);open.addEventListener('click',navigate);
    document.getElementById('cancel').addEventListener('click',close);
    document.getElementById('close').addEventListener('click',close);
    document.addEventListener('visibilitychange',function() {
        if(document.visibilityState==='hidden') {
            stopCamera();
            if(state==='scanning' || state==='requesting') {state='paused';message('Camera paused. Tap Resume Camera.','Resume Camera');}
        }
    });
    window.addEventListener('pagehide',function() {
        stopCamera();URL.revokeObjectURL(location.href);
        if(!transitioning && detailURL) URL.revokeObjectURL(detailURL);
    },{once:true});
    startCamera();
})();
