const Fmt = {
  usd(n) {
    if (n==null) return '—';
    const a=Math.abs(n),s=n<0?'-':'';
    if(a===0) return '$0.00';
    if(a<0.0001) return s+'$'+a.toFixed(6);
    if(a<0.01)   return s+'$'+a.toFixed(4);
    if(a<1)      return s+'$'+a.toFixed(3);
    if(a<1000)   return s+'$'+a.toFixed(2);
    if(a<1e6)    return s+'$'+(a/1000).toFixed(1)+'K';
    return s+'$'+(a/1e6).toFixed(2)+'M';
  },
  pnl(n) {
    if(n==null) return {text:'—',cls:''};
    const a=Math.abs(n);
    return {text:(n>=0?'+':'-')+'$'+a.toFixed(2), cls:n>=0?'positive':'negative'};
  },
  pct(n,d=1){ return n==null?'—':n.toFixed(d)+'%' },
  price(n) {
    if(n==null) return '—';
    if(n<0.000001) return '$'+n.toExponential(3);
    if(n<0.01)  return '$'+n.toFixed(6);
    if(n<1)     return '$'+n.toFixed(4);
    return '$'+n.toFixed(2);
  },
  ago(s) {
    if(!s) return '—';
    const d=new Date(s.endsWith('Z')?s:s+'Z'),sec=Math.floor((Date.now()-d)/1000);
    if(sec<60)    return sec+'s ago';
    if(sec<3600)  return Math.floor(sec/60)+'m ago';
    if(sec<86400) return Math.floor(sec/3600)+'h ago';
    return d.toLocaleDateString('en-US',{month:'short',day:'numeric'});
  },
  datetime(s) {
    if(!s) return '—';
    const d=new Date(s.endsWith('Z')?s:s+'Z');
    return d.toLocaleDateString('en-US',{month:'short',day:'numeric'})+' '+
      d.toLocaleTimeString('en-US',{hour:'2-digit',minute:'2-digit',hour12:false});
  },
  scoreBadge(v) {
    if(v==null) return '<span class="score-badge medium">—</span>';
    const c=v>=80?'high':v>=60?'medium':'low';
    return `<span class="score-badge ${c}">${v}</span>`;
  },
  decisionBadge(d) {
    const k=(d||'pending').toLowerCase();
    const L={buy:'BUY',skip:'SKIP',pending:'PENDING',rug:'RUG'};
    return `<span class="decision-badge ${k}">${L[k]||k.toUpperCase()}</span>`;
  },
  chainBadge(c){ return `<span class="chain-badge">${(c||'?').toUpperCase()}</span>` },
};
