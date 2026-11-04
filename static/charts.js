const COP=v=>"$"+Math.round(v).toLocaleString("es-CO");
const COPK=v=>v>=1e6?(v/1e6).toFixed(v%1e6?1:0)+" M":v>=1e3?Math.round(v/1e3)+" mil":String(Math.round(v));
function donut(el,data,centro,sub){
  const R=62,C=2*Math.PI*R,tot=data.reduce((a,x)=>a+x.v,0)||1;let off=0,segs="";
  data.forEach(x=>{const l=x.v/tot*C;segs+=`<circle cx="80" cy="80" r="${R}" fill="none" stroke="${x.c}" stroke-width="18" stroke-dasharray="${Math.max(l-2,0.5)} ${C-Math.max(l-2,0.5)}" stroke-dashoffset="${-off}" transform="rotate(-90 80 80)"/>`;off+=l;});
  el.innerHTML=`<svg viewBox="0 0 160 160" width="170" height="170" role="img"><circle cx="80" cy="80" r="${R}" fill="none" stroke="var(--soft)" stroke-width="18"/>${segs}
  <text x="80" y="78" text-anchor="middle" font-size="15" font-weight="800" fill="var(--tx)">${centro}</text><text x="80" y="96" text-anchor="middle" font-size="10.5" fill="var(--mu)">${sub}</text></svg>`;
}
function barras(el,h){
  const W=Math.max(300,el.clientWidth||620),H=W<520?200:230,L=W<520?40:46,B=30,T=10,max=Math.max(1,...h.flatMap(x=>[x.ing,x.gas])),paso=(W-L)/Math.max(h.length,1);
  let g="";for(let i=0;i<=4;i++){const y=T+(H-B-T)*(1-i/4);g+=`<line x1="${L}" x2="${W}" y1="${y}" y2="${y}" stroke="var(--ln)"/><text x="${L-8}" y="${y+4}" text-anchor="end" font-size="10.5" fill="var(--mu)">${COPK(max*i/4)}</text>`;}
  const bw=Math.min(26,Math.max(8,paso/3.4));h.forEach((x,i)=>{const cx=L+paso*i+paso/2,m=["ene","feb","mar","abr","may","jun","jul","ago","sep","oct","nov","dic"][+x.mes.slice(5,7)-1];
    [[x.ing,"var(--ok)",-bw-1],[x.gas,"var(--bad)",1]].forEach(([v,c,dx])=>{const hh=(H-B-T)*v/max;g+=`<rect x="${cx+dx}" y="${H-B-hh}" width="${bw}" height="${Math.max(hh,v?2:0)}" rx="5" fill="${c}"><title>${COP(v)}</title></rect>`;});
    g+=`<text x="${cx}" y="${H-9}" text-anchor="middle" font-size="11.5" fill="var(--mu)">${m}</text>`;});
  el.innerHTML=`<svg viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" role="img" style="display:block">${g}</svg><div class="leg"><span><i style="background:var(--ok)"></i>Ingresos</span><span><i style="background:var(--bad)"></i>Gastos</span></div>`;
}
