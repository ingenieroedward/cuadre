if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js").catch(() => {});

function b64(k){const p="=".repeat((4-k.length%4)%4),s=(k+p).replace(/-/g,"+").replace(/_/g,"/"),r=atob(s);return Uint8Array.from([...r].map(c=>c.charCodeAt(0)));}

async function estadoPush(){
  const el=document.getElementById("push-estado"); if(!el) return;
  const btn=document.getElementById("push-btn"), probar=document.getElementById("push-probar");
  if(!("serviceWorker" in navigator)||!("PushManager" in window)){
    el.textContent="Este navegador no admite avisos. En iPhone: Compartir → Añadir a pantalla de inicio, y abre Cuadre desde ahí.";
    btn.hidden=true; return; }
  const reg=await navigator.serviceWorker.ready; const sub=await reg.pushManager.getSubscription();
  if(sub&&Notification.permission==="granted"){el.textContent="Avisos activados en este dispositivo";btn.hidden=true;probar.hidden=false;}
  else if(Notification.permission==="denied"){el.textContent="Bloqueaste los avisos. Actívalos en los ajustes del navegador para este sitio.";btn.hidden=true;}
  else {el.textContent="Avisos desactivados en este dispositivo";btn.hidden=false;probar.hidden=true;}
}
async function activarPush(){
  const perm=await Notification.requestPermission(); if(perm!=="granted") return estadoPush();
  const reg=await navigator.serviceWorker.ready;
  const {key}=await (await fetch("/push/clave")).json();
  const sub=await reg.pushManager.subscribe({userVisibleOnly:true,applicationServerKey:b64(key)});
  await fetch("/push/suscribir",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(sub)});
  estadoPush();
}
async function probarPush(){const r=await fetch("/push/probar",{method:"POST"});const j=await r.json();
  alert(j.enviados?"Te enviamos una notificación de prueba":"No se pudo enviar. Activa los avisos de nuevo.");}
document.addEventListener("DOMContentLoaded",estadoPush);

/* Feedback al enviar: el botón muestra un spinner y se bloquea para que se note que está trabajando */
(function(){
  var TXT={'/asesor/analizar':'Analizando…','/asesor/preguntar':'Pensando…','/subir':'Leyendo el extracto…'};
  function libre(){document.querySelectorAll('button.ocupado').forEach(function(b){b.classList.remove('ocupado');b.disabled=false;if(b.dataset.h){b.innerHTML=b.dataset.h;}});}
  window.addEventListener('pageshow',libre);
  document.addEventListener('submit',function(e){
    var f=e.target;if(!f||f.tagName!=='FORM'||e.defaultPrevented)return;
    if((f.method||'get').toLowerCase()!=='post')return;
    var b=e.submitter||f.querySelector('button:not([type=button])');if(!b||b.classList.contains('chk'))return;
    var ruta=(f.getAttribute('action')||location.pathname).split('?')[0];
    var txt=TXT[ruta]||b.dataset.ocupado;
    setTimeout(function(){
      b.dataset.h=b.innerHTML;b.classList.add('ocupado');
      b.innerHTML='<span class="spin"></span>'+(txt||'Guardando…');
      b.disabled=true;
      f.querySelectorAll('input[name=pregunta]').forEach(function(i){i.readOnly=true});
      if(TXT[ruta]&&ruta!=='/subir'||ruta==='/subir'){
        var n=document.getElementById('ocupado-nota');
        if(!n){n=document.createElement('div');n.id='ocupado-nota';n.className='ocupado-nota';n.setAttribute('role','status');document.body.appendChild(n);}
        n.textContent=ruta==='/subir'?'Leyendo tus extractos, puede tardar unos segundos…':'Esto puede tardar unos segundos, no cierres la página…';
      }
    },0);
  });
})();


/* Preloader (solo la primera vez de la sesión), barra de progreso al navegar y conteo de cifras grandes */
(function(){
  var pl=document.getElementById('pl'),bar=document.getElementById('nprog');
  function quitar(){if(!pl)return;pl.classList.add('out');try{sessionStorage.setItem('pl','1')}catch(e){}setTimeout(function(){pl.remove()},600);}
  if(pl&&!document.documentElement.classList.contains('nopl')){
    var t0=performance.now(),listo=function(){setTimeout(quitar,Math.max(0,1500-(performance.now()-t0)))};
    if(document.readyState==='complete')listo();else window.addEventListener('load',listo);
    setTimeout(quitar,4000);
  }else if(pl){pl.remove();}
  function ir(){if(!bar)return;bar.classList.remove('fin');void bar.offsetWidth;bar.classList.add('on');}
  document.addEventListener('click',function(e){var a=e.target.closest&&e.target.closest('a[href]');
    if(!a||e.defaultPrevented||e.metaKey||e.ctrlKey||e.shiftKey||a.target==='_blank'||a.hasAttribute('download'))return;
    var h=a.getAttribute('href');if(!h||h[0]==='#'||h.indexOf('javascript:')===0||/^https?:/.test(h)&&a.host!==location.host)return;ir();});
  document.addEventListener('submit',function(e){if(!e.defaultPrevented)ir();});
  window.addEventListener('pageshow',function(){if(bar){bar.classList.remove('on');bar.classList.add('fin');}});
  /* conteo: de 0 a la cifra en ~0,7 s */
  if(matchMedia('(prefers-reduced-motion:reduce)').matches)return;
  document.querySelectorAll('.hero .big,.cnt').forEach(function(el){
    var m=el.textContent.trim().match(/^(-?)\$\s?(-?)([\d.]+),(\d{2})$/);if(!m)return;
    var neg=(m[1]+m[2])==='-',fin=parseFloat(m[3].replace(/\./g,'')+'.'+m[4]),t0=performance.now();
    function paso(t){var k=Math.min(1,(t-t0)/500),v=fin*(1-Math.pow(1-k,3));
      var txt=v.toLocaleString('es-CO',{minimumFractionDigits:2,maximumFractionDigits:2});el.textContent=(neg?'-':'')+'$'+txt;
      if(k<1)requestAnimationFrame(paso);else el.textContent=m[0];}
    requestAnimationFrame(paso);
  });
})();
