(()=>{
  const button=document.querySelector('#bundle-import'),input=document.querySelector('#bundle-file'),message=document.querySelector('#bundle-result');
  button.onclick=async()=>{
    if(!input.files.length){message.textContent='请选择完整数据ZIP包';return}
    const body=new FormData();body.append('file',input.files[0]);button.disabled=true;message.textContent='正在校验并导入，请稍候…';
    try{
      const response=await fetch('/api/bundle/import',{method:'POST',headers:{'X-DEMO-Source':'1'},body});const result=await response.json();
      if(!response.ok||result.error)throw new Error(result.error||'导入失败');
      message.textContent=result.already_imported?'此数据包已导入，未重复添加。':`导入完成：专利 ${result.patents} 件、材料关联 ${result.materials} 条、来源原件 ${result.source_documents} 份。`;
      await loadStats();await loadPatents();loadFamilyQuery();toast(result.already_imported?'未重复导入':'整包导入成功');
    }catch(error){message.textContent=error.message}finally{button.disabled=false}
  };
})();
