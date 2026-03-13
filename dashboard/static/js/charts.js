Chart.defaults.color='#525252';
Chart.defaults.borderColor='#262626';
Chart.defaults.font.family="'IBM Plex Mono',monospace";
Chart.defaults.font.size=10;

const Charts={
  _i:{},
  destroy(id){if(this._i[id]){this._i[id].destroy();delete this._i[id]}},
  pnlLine(id,data){
    this.destroy(id);
    const el=document.getElementById(id);if(!el)return;
    const vals=data.map(d=>d.pnl),last=vals[vals.length-1]||0;
    const col=last>=0?'#22c55e':'#ef4444';
    this._i[id]=new Chart(el,{type:'line',data:{
      labels:data.map(d=>d.date),
      datasets:[{data:vals,borderColor:col,borderWidth:1.5,pointRadius:0,pointHoverRadius:4,
        fill:true,backgroundColor:(ctx)=>{
          const g=ctx.chart.ctx.createLinearGradient(0,0,0,ctx.chart.height);
          g.addColorStop(0,col+'22');g.addColorStop(1,col+'00');return g;
        },tension:0.3}]
    },options:{
      responsive:true,maintainAspectRatio:true,
      animation:{duration:600},interaction:{intersect:false,mode:'index'},
      plugins:{legend:{display:false},tooltip:{backgroundColor:'#111',borderColor:'#262626',borderWidth:1,padding:10,
        callbacks:{label:c=>' '+(c.raw>=0?'+':'')+' $'+c.raw.toFixed(2)}}},
      scales:{
        x:{grid:{color:'#1a1a1a'},ticks:{maxTicksLimit:8}},
        y:{grid:{color:'#1a1a1a'},ticks:{callback:v=>(v>=0?'+':'')+' $'+v.toFixed(0)}}
      }
    }});
  },
  sparkline(id,history,color='#f59e0b'){
    this.destroy(id);
    const el=document.getElementById(id);if(!el)return;
    this._i[id]=new Chart(el,{type:'line',data:{
      labels:history.map(d=>d.date),
      datasets:[{data:history.map(d=>d.apy),borderColor:color,borderWidth:1.5,pointRadius:0,fill:false,tension:0.4}]
    },options:{responsive:false,animation:false,
      plugins:{legend:{display:false},tooltip:{enabled:false}},
      scales:{x:{display:false},y:{display:false}}
    }});
  },
  flashLoanBar(id, labels, successData, revertedData, profitData) {
    this.destroy(id);
    const el = document.getElementById(id); if (!el) return;
    const profitColors = (profitData||[]).map(v => v > 0 ? 'rgba(34,197,94,.6)' : 'rgba(239,68,68,.4)');
    const datasets = [
      {label:'Success',  data:successData,  backgroundColor:'rgba(34,197,94,.75)',  hoverBackgroundColor:'#22c55e', borderWidth:0, borderRadius:3, yAxisID:'y'},
      {label:'Reverted', data:revertedData, backgroundColor:'rgba(239,68,68,.55)',  hoverBackgroundColor:'#ef4444', borderWidth:0, borderRadius:3, yAxisID:'y'},
    ];
    if (profitData && profitData.some(v => v !== 0)) {
      datasets.push({label:'Profit (USDC)', data:profitData, backgroundColor:profitColors, borderWidth:0, borderRadius:3, yAxisID:'y1'});
    }
    this._i[id] = new Chart(el, {type:'bar', data:{labels, datasets}, options:{
      responsive:true, maintainAspectRatio:true,
      animation:{duration:700},
      plugins:{
        legend:{display:true, labels:{boxWidth:10, padding:20, color:'#a3a3a3'}},
        tooltip:{backgroundColor:'#111', borderColor:'#262626', borderWidth:1, padding:10,
          callbacks:{label: c => c.dataset.label === 'Profit (USDC)' ? ` Profit: $${(c.raw||0).toFixed(2)}` : ` ${c.dataset.label}: ${c.raw}`}}
      },
      scales:{
        x:{grid:{color:'#1a1a1a'}, ticks:{color:'#525252', maxRotation:45, font:{size:9}}},
        y:{grid:{color:'#1a1a1a'}, ticks:{color:'#525252', stepSize:1}, stacked:false, position:'left'},
        y1:{grid:{drawOnChartArea:false}, ticks:{color:'#525252', callback: v => '$'+v.toFixed(0)}, position:'right', display: !!(profitData && profitData.some(v => v !== 0))}
      }
    }});
  },
  monthlyPnlBar(id, labels, data) {
    this.destroy(id);
    const el = document.getElementById(id); if (!el) return;
    const colors = data.map(v => v >= 0 ? 'rgba(34,197,94,.75)' : 'rgba(239,68,68,.65)');
    const hover  = data.map(v => v >= 0 ? '#22c55e' : '#ef4444');
    this._i[id] = new Chart(el, {type:'bar', data:{
      labels,
      datasets:[{data, backgroundColor:colors, hoverBackgroundColor:hover, borderWidth:0, borderRadius:3}]
    }, options:{
      responsive:true, maintainAspectRatio:true,
      animation:{duration:700},
      plugins:{
        legend:{display:false},
        tooltip:{backgroundColor:'#111', borderColor:'#262626', borderWidth:1, padding:10,
          callbacks:{label: c => ' P&L: ' + (c.raw >= 0 ? '+' : '') + ' $' + c.raw.toFixed(2)}}
      },
      scales:{
        x:{grid:{color:'#1a1a1a'}, ticks:{color:'#525252', maxTicksLimit:8, maxRotation:45}},
        y:{grid:{color:'#1a1a1a'}, ticks:{color:'#525252', callback: v => (v >= 0 ? '+' : '') + ' $' + v.toFixed(0)}}
      }
    }});
  }
};
