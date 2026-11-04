const b2u=s=>{const p="=".repeat((4-s.length%4)%4),r=atob((s+p).replace(/-/g,"+").replace(/_/g,"/"));return Uint8Array.from(r,c=>c.charCodeAt(0)).buffer;};
const u2b=b=>{let s="";new Uint8Array(b).forEach(x=>s+=String.fromCharCode(x));return btoa(s).replace(/\+/g,"-").replace(/\//g,"_").replace(/=+$/,"");};
const post=(u,b)=>fetch(u,{method:"POST",headers:{"Content-Type":"application/json"},body:b?JSON.stringify(b):undefined}).then(async r=>({ok:r.ok,j:await r.json().catch(()=>({}))}));
const pkSoportado=()=>!!(window.PublicKeyCredential&&navigator.credentials);

async function registrarPasskey(){
  const msg=document.getElementById("pk-msg"); msg.textContent="";
  try{
    const nombre=prompt("Ponle un nombre a este dispositivo","Mi celular")||"Mi dispositivo";
    const o=(await post("/passkey/registro/opciones")).j;
    o.challenge=b2u(o.challenge); o.user.id=b2u(o.user.id); (o.excludeCredentials||[]).forEach(c=>c.id=b2u(c.id));
    const c=await navigator.credentials.create({publicKey:o});
    const r=await post("/passkey/registro/verificar",{nombre,credencial:{id:c.id,rawId:u2b(c.rawId),type:c.type,
      authenticatorAttachment:c.authenticatorAttachment,clientExtensionResults:c.getClientExtensionResults(),
      response:{clientDataJSON:u2b(c.response.clientDataJSON),attestationObject:u2b(c.response.attestationObject),
      transports:c.response.getTransports?c.response.getTransports():[]}}});
    if(r.ok) location.href="/cuenta?msg=Passkey%20creada"; else msg.textContent=r.j.error||"No se pudo crear la passkey";
  }catch(e){msg.textContent=e.name==="NotAllowedError"?"Cancelaste o el dispositivo no la permitió":"No se pudo crear: "+e.message;}
}

async function entrarConPasskey(){
  const msg=document.getElementById("pk-msg"); msg.textContent="";
  try{
    const o=(await post("/passkey/login/opciones")).j; o.challenge=b2u(o.challenge);
    (o.allowCredentials||[]).forEach(c=>c.id=b2u(c.id));
    const c=await navigator.credentials.get({publicKey:o});
    const r=await post("/passkey/login/verificar",{credencial:{id:c.id,rawId:u2b(c.rawId),type:c.type,
      authenticatorAttachment:c.authenticatorAttachment,clientExtensionResults:c.getClientExtensionResults(),
      response:{clientDataJSON:u2b(c.response.clientDataJSON),authenticatorData:u2b(c.response.authenticatorData),
      signature:u2b(c.response.signature),userHandle:c.response.userHandle?u2b(c.response.userHandle):null}}});
    if(r.ok) location.href="/"; else msg.textContent=r.j.error||"No se pudo entrar con la passkey";
  }catch(e){msg.textContent=e.name==="NotAllowedError"?"Cancelaste o no hay una passkey en este dispositivo":"No se pudo: "+e.message;}
}
