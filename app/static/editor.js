(()=>{
 const file=document.getElementById("video");
 const preview=document.getElementById("preview");
 const form=document.querySelector('form[action="/api/editor/export"]');
 const metadataForm=document.getElementById("metadata_form");
 const metadataButton=document.getElementById("metadata_button");
 const rights=document.getElementById("rights_confirm");
 const exportButton=document.getElementById("export_button");
 let objectUrl="";
 file.addEventListener("change",()=>{
   if(objectUrl) URL.revokeObjectURL(objectUrl);
   const f=file.files&&file.files[0];
   if(!f){preview.hidden=true;preview.removeAttribute("src");return;}
   objectUrl=URL.createObjectURL(f);
   preview.src=objectUrl;
   preview.hidden=false;
 });
 form.addEventListener("submit",(event)=>{
   if(!rights.checked){
     event.preventDefault();
     alert("目前没有原作者授权。请先使用上方评论稿工作区；只有在素材由你创作、已获明确授权或确有其他合法使用依据时，才能确认并导出。");
     document.getElementById("commentary-planner").scrollIntoView({behavior:"smooth",block:"start"});
     return;
   }
   exportButton.disabled=true;
   exportButton.textContent="正在处理，请保持页面打开…";
 });
 document.getElementById("save_script").addEventListener("click",()=>{
   const script=document.getElementById("commentary_script").value.trim();
   if(!script){alert("先写入你的原创解说与评论稿。");return;}
   const source=document.getElementById("source_url").value.trim();
   const creator=document.getElementById("source_creator").value.trim();
   const content=[
     "VideoDrop 原创评论稿",
     "生成时间："+new Date().toLocaleString(),
     "原视频链接："+(source||"未填写"),
     "原作者账号："+(creator||"未填写"),
     "",
     "原创解说与评论稿",
     "------------------------------",
     script,
     "",
     "素材使用提醒：当前未记录原作者授权。来源署名不等于授权；发布前请确认素材使用依据，必要时取得作者明确许可。",
     "本文件仅保存评论文稿和来源记录，不包含原视频，也不代表取得原视频版权。"
   ].join("\n");
   const blob=new Blob([content],{type:"text/plain;charset=utf-8"});
   const url=URL.createObjectURL(blob);
   const a=document.createElement("a");
   a.href=url;
   a.download="VideoDrop-原创评论稿.txt";
   document.body.appendChild(a);
   a.click();
   a.remove();
   setTimeout(()=>URL.revokeObjectURL(url),1000);
 });
 metadataForm.addEventListener("submit",()=>{metadataButton.disabled=true;metadataButton.textContent="正在写入元数据，请保持页面打开…";});
 window.addEventListener("pagehide",()=>{if(objectUrl)URL.revokeObjectURL(objectUrl);});
})();