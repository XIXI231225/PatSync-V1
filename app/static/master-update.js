/* Source selection is independent of master-table merge rules. */
(()=>{
  let timer=null, current=null, loaded=false;
  const post=(path,body)=>api('/api/master-update/'+path,{method:'POST',headers:{'Content-Type':'application/json','X-DEMO-Source':'1'},body:JSON.stringify(body)});
  function stop(){if(timer)clearTimeout(timer);timer=null}
  async function loadSettings(){
    const d=await api('/api/master-update/settings');
    const labels={patsnap_api:'智慧芽 API',patsnap_mcp:'智慧芽 MCP',generic_mcp:'其他 MCP'};
    $('#source-provider').innerHTML=(d.providers||Object.keys(labels)).map(name=>`<option value="${esc(name)}">${esc(labels[name]||name)}</option>`).join('');
    $('#source-provider').value=d.provider;
    $('#source-patsnap-url').value=d.patsnap_mcp_endpoint||'';
    $('#source-generic-config').value=d.generic_mcp?JSON.stringify(d.generic_mcp,null,2):'';
    const c=d.credentials||{};
    $('#source-credentials').textContent=`已保存：智慧芽 API ${c.patsnap_api?'是':'否'}；智慧芽 MCP ${c.patsnap_mcp?'是':'否'}；其他 MCP ${c.generic_mcp?'是':'否'}。留空保留现有密钥。`;
    loaded=true;
  }
  async function save(){
    const body={provider:$('#source-provider').value,patsnap_mcp_endpoint:$('#source-patsnap-url').value.trim(),patsnap_api_key:$('#source-api-key').value,patsnap_mcp_key:$('#source-mcp-key').value,generic_mcp_key:$('#source-generic-key').value};
    const generic=$('#source-generic-config').value.trim();
    if(generic){try{body.generic_mcp=JSON.parse(generic)}catch(_){throw Error('其他 MCP 配置不是有效 JSON')}}
    if(body.provider==='generic_mcp'&&!body.generic_mcp)throw Error('请填写其他 MCP 配置');
    await post('settings',body);
    ['#source-api-key','#source-mcp-key','#source-generic-key'].forEach(id=>$(id).value='');
    await loadSettings();
  }
  function valueDiff(d){return `<div style="margin-bottom:10px;overflow-wrap:anywhere"><strong>${esc(d.field)}</strong><div style="color:#64716d">${esc(d.before||'（原为空）')}</div><div>→ ${esc(d.after||'（空）')}</div>${d.reason?`<small>${esc(d.reason)}</small>`:''}</div>`}
  function renderRows(){
    if(!current)return;
    const filter=$('#source-filter').value;
    const rows=(current.items||[]).filter(x=>filter==='all'||(filter==='changes'?['新增','更新'].includes(x.action):(x.review||[]).length));
    $('#source-preview-body').innerHTML=rows.map(x=>`<tr class="${x.action==='待确认'?'row-new':''}"><td>${esc(x.application_no)}</td><td><strong>${esc(x.action==='待确认'?'新增候选':x.action)}</strong></td><td>${x.action==='待确认'?'候选信息需先核对，本次不写入':(x.diffs||[]).map(valueDiff).join('')||'原值保留'}</td><td>${(x.review||[]).length?`<details><summary>${x.review.length} 项待核对</summary>${x.review.map(valueDiff).join('')}</details>`:''}${x.message?`<small>${esc(x.message)}</small>`:''}</td></tr>`).join('')||'<tr><td colspan="4">当前筛选没有记录</td></tr>';
  }
  function render(d){
    current=d;
    $('#source-status').textContent=(d.evidence_mode==='local-real-ledger-snapshot'?'原台账快照（本轮未联网） · ':d.evidence_mode==='offline-replay'?'离线回放 · ':'')+(d.message||'尚未获取')+(d.updated_at?' · '+d.updated_at.replace('T',' '):'');
    $('#source-start').disabled=d.status==='fetching';
    $('#source-save').disabled=d.status==='fetching';
    $('#source-resume').classList.toggle('hidden',!['failed','interrupted'].includes(d.status));
    $('#source-preview').classList.toggle('hidden',!d.counts);
    $('#source-apply').disabled=!d.can_apply;
    if(d.counts){
      const c=d.counts;
      $('#source-summary').textContent=`获取 ${d.total} 条｜可执行更新 ${c.更新}｜不变 ${c.不变}｜新增候选 ${c.待确认||0}｜含待核对项 ${d.review_rows||0} 条`;
      renderRows();
      const missing=d.missing_applications||[];
      $('#source-missing-wrap').classList.toggle('hidden',!missing.length);
      $('#source-missing-summary').textContent=`本次未获取 ${missing.length} 条，保留原记录`;
      $('#source-missing').textContent=missing.join('、');
    }
  }
  async function refresh(){
    stop();
    try{
      const d=await api('/api/master-update/status');render(d);
      if(d.status==='fetching'&&$('#view-import').classList.contains('active'))timer=setTimeout(refresh,5000);
    }catch(e){toast(e.message)}
  }
  const originalShowView=showView;
  showView=function(name){
    originalShowView(name);stop();
    if(name==='import'){
      (loaded?Promise.resolve():loadSettings()).then(refresh).catch(e=>toast(e.message));
    }
  };
  $('#source-save').onclick=()=>save().then(()=>toast('连接已保存')).catch(e=>toast(e.message));
  $('#source-refresh').onclick=()=>refresh();
  $('#source-filter').onchange=renderRows;
  $('#source-start').onclick=async()=>{
    $('#source-start').disabled=true;
    try{await save();render(await post('start',{}));await refresh()}catch(e){toast(e.message);await refresh()}
  };
  $('#source-resume').onclick=async()=>{try{render(await post('start',{resume:true}));await refresh()}catch(e){toast(e.message)}};
  $('#source-apply').onclick=async()=>{
    if(!current?.can_apply||!confirm(`仅更新 ${current.counts?.更新||0} 条可执行变化；待核对字段及新增候选不会写入。确认继续？`))return;
    const run=current.run_id;$('#source-apply').disabled=true;
    try{
      const d=await post('apply',{run_id:run});
      toast(d.warning|| (d.already_applied?'此批更新已完成':`总表已更新：新增 ${d.added}，更新 ${d.updated}`));
      await Promise.all([loadStats(),loadPatents(),loadFamilyQuery()]);
    }catch(e){toast(e.message)}finally{await refresh()}
  };
})();
