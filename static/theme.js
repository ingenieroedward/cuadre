(function(){try{var t=localStorage.getItem("tema");if(t)document.documentElement.dataset.theme=t;}catch(e){}})();
function cambiarTema(){var h=document.documentElement,c=h.dataset.theme||"auto",n={auto:"light",light:"dark",dark:"auto"}[c];
 try{if(n==="auto"){localStorage.removeItem("tema");delete h.dataset.theme;}else{localStorage.setItem("tema",n);h.dataset.theme=n;}}catch(e){h.dataset.theme=n;}}
