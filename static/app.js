if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js").catch(() => {});

function b64(k){const p="=".repeat((4-k.length%4)%4),s=(k+p).replace(/-/g,"+").replace(/_/g,"/"),r=atob(s);return Uint8Array.from([...r].map(c=>c.charCodeAt(0)));}

async function estadoPush(){
  const el=document.getElementById("push-estado"); if(!el) return;
  const btn=document.getElementById("push-btn"), probar=document.getElementById("push-probar");
  if(!("serviceWorker" in navigator)||!("PushManager" in window)){
    el.textContent="Este navegador no admite avisos. En iPhone: Compartir → Añadir a pantalla de inicio, y abre Cuadre desde ahí.";
    btn.hidden=true; return; }
  const reg=await navigator.serviceWorker.ready; const sub=await reg.pushManager.getSubscription();
  if(sub&&Notification.permission==="granted"){el.textContent="✅ Avisos activados en este dispositivo";btn.hidden=true;probar.hidden=false;}
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
