let feishuPoll = null;
const feishuLabels = {success:'成功', partial:'部分失败', failed:'失败', running:'执行中', interrupted:'已中断'};
const feishuTriggers = {manual:'手动', scheduled:'每日任务', acceptance:'测试验收'};
const feishuTime = v => v ? String(v).replace('T',' ').replace('+08:00','') : '尚未同步';
window.stopFeishuSyncPoll = () => { clearTimeout(feishuPoll); feishuPoll = null; };
async function feishuPost(path, payload={}) {
  return api('/api/feishu/'+path, {method:'POST', headers:{'Content-Type':'application/json','X-DEMO-Sync':'1'}, body:JSON.stringify(payload)});
}
window.loadFeishuSync = async (fill=true) => {
  window.stopFeishuSyncPoll();
  if(fill) window.scrollTo(0,0);
  try {
    const [data, source] = await Promise.all([api('/api/feishu/status'), api('/api/feishu/source')]);
    const s = data.settings;
    if(fill) {
      $('#feishu-enabled').checked=s.enabled; $('#feishu-mode').value=s.mode;
      $('#feishu-app-id').value=s.app_id; $('#feishu-app-token').value=s.app_token; $('#feishu-table-id').value=s.table_id;
      $('#feishu-app-secret').value='';
    }
    $('#feishu-secret-status').textContent=s.has_secret?'本机已保存；留空保持不变':'当前 Windows 账户未保存密钥';
    $('#feishu-last-success').textContent=feishuTime(data.last_success);
    const last=data.runs[0];
    $('#feishu-last-result').textContent=data.running?'执行中':last?(feishuLabels[last.status]||last.status):'尚未执行';
    $('#feishu-scope').textContent=`${source.included} 条集团专利`;
    $('#feishu-validation').textContent=data.live_test?.passed?'已通过':'尚未完成';
    $('#feishu-source-note').textContent=`源库 ${source.total} 条，排除非集团 ${source.excluded} 条，待核对 ${source.pending.length} 条。`+(source.pending.length?'待核对记录：'+source.pending.map(x=>x.application_no).join('、'):'');
    $('#feishu-schedule').textContent=data.schedule;
    $('#feishu-run').disabled=data.running||!s.enabled;
    $('#feishu-save').disabled=data.running;
    $('#feishu-acceptance').disabled=data.running||s.mode!=='test';
    $('#feishu-prepare').disabled=data.running||s.mode!=='test';
    $('#feishu-runs').innerHTML=data.runs.map(x=>{const c=x.counts;const detail=x.trigger==='acceptance'?`通过 ${c.checks_passed||0} 项`:`新增 ${c.added||0} · 修改 ${c.updated||0} · 移除 ${c.removed||0} · 失败 ${c.failed||0}`;return `<tr><td>${esc(feishuTime(x.started))}</td><td>${esc(feishuTriggers[x.trigger]||x.trigger)}</td><td>${esc(feishuLabels[x.status]||x.status)}</td><td>${esc(detail)}</td><td>${esc(x.message)}</td></tr>`}).join('')||'<tr><td colspan="5" class="muted-cell">尚无同步记录</td></tr>';
    $('#feishu-events').innerHTML=data.events.map(x=>`<tr><td>${esc(feishuTime(x.created))}</td><td>${esc(x.app_key)}</td><td>${esc(x.action)}</td><td>${esc(x.result)}</td><td>${esc(x.message)}</td></tr>`).join('')||'<tr><td colspan="5" class="muted-cell">尚无同步日志</td></tr>';
    if(data.running&&$('#view-feishu').classList.contains('active'))feishuPoll=setTimeout(()=>window.loadFeishuSync(false),10000);
  } catch(e) {toast(e.message)}
};
async function feishuAction(button, action) {
  button.disabled=true;
  try {const result=await action();if(result?.message)toast(result.message);await window.loadFeishuSync(false)}
  catch(e){toast(e.message)} finally {button.disabled=false}
}
$('#feishu-refresh').onclick=()=>window.loadFeishuSync(false);
$('#feishu-save').onclick=e=>feishuAction(e.currentTarget,async()=>{
  await feishuPost('settings',{enabled:$('#feishu-enabled').checked,mode:$('#feishu-mode').value,app_id:$('#feishu-app-id').value.trim(),app_token:$('#feishu-app-token').value.trim(),table_id:$('#feishu-table-id').value.trim(),app_secret:$('#feishu-app-secret').value});
  $('#feishu-app-secret').value='';return {message:'连接设置已保存'};
});
$('#feishu-test').onclick=e=>feishuAction(e.currentTarget,()=>feishuPost('test'));
$('#feishu-prepare').onclick=e=>{if(confirm('仅用于独立空白测试表，将添加同步字段。请先保存测试表设置。'))feishuAction(e.currentTarget,()=>feishuPost('prepare'))};
$('#feishu-acceptance').onclick=e=>{if(confirm('将在已保存的测试表中使用虚构专利验证新增、修改、重复同步和异常恢复，完成后移除测试记录。请确保是专用测试表。'))feishuAction(e.currentTarget,()=>feishuPost('acceptance'))};
$('#feishu-run').onclick=e=>{if(confirm('按已保存的配置同步：新增和更新集团专利，移除退出集团范围的共享明细。人工汇总及 SQLite 原记录保留。'))feishuAction(e.currentTarget,()=>feishuPost('run'))};
$('#feishu-preview').onclick=e=>feishuAction(e.currentTarget,async()=>{
  const d=await api('/api/feishu/preview');const box=$('#feishu-diff');box.classList.remove('hidden');
  box.innerHTML=`<strong>新增 ${d.added.length} · 修改 ${d.updated.length} · 移除 ${d.removed.length} · 不变 ${d.unchanged}</strong>`+['added','updated','removed'].map((k,i)=>`<details><summary>${['新增','修改','移除'][i]}清单</summary><p>${esc(d[k].join('、')||'无')}</p></details>`).join('')+(d.pending.length?'<p>存在待核对记录，本轮不会执行同步。</p>':'');
});
