(()=>{
 const file=document.getElementById("video");
 const preview=document.getElementById("preview");
 const form=document.querySelector('form[action="/api/editor/export"]');
 const checksumForm=document.getElementById("checksum_form");
 const checksumButton=document.getElementById("checksum_button");
 const checksumResult=document.getElementById("checksum_result");
 checksumForm.addEventListener("submit",async(event)=>{
   event.preventDefault();
   const selected=document.getElementById("checksum_video").files[0];
   if(!selected){alert("请先选择视频文件。");return;}
   if(selected.size>300*1024*1024){alert("单个文件最大支持 300 MB。");return;}
   const data=new FormData();
   data.append("video",selected);
   checksumButton.disabled=true;
   checksumButton.textContent="正在计算，请保持页面打开…";
   checksumResult.hidden=false;
   checksumResult.textContent="正在计算文件校验值…";
   try{
     const response=await fetch("/api/editor/checksum",{method:"POST",body:data});
     const result=await response.json();
     if(!response.ok) throw new Error(result.detail||"计算失败，请稍后重试。");
     checksumResult.textContent=[
       "文件："+result.filename,
       "大小："+result.size_bytes.toLocaleString()+" bytes",
       "MD5："+result.md5,
       "SHA-256："+result.sha256,
       result.note
     ].join("\n");
     checksumResult.style.whiteSpace="pre-wrap";
   }catch(error){
     checksumResult.textContent=error.message||"计算失败，请稍后重试。";
   }finally{
     checksumButton.disabled=false;
     checksumButton.textContent="计算文件校验值 ↓";
   }
 });
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
   const sourceVideo=file.files&&file.files[0];
   const narrationFile=document.getElementById("narration").files[0];
   const subtitleFile=document.getElementById("subtitles").files[0];
   if(!sourceVideo){event.preventDefault();alert("请先选择视频文件。");return;}
   if(sourceVideo.size>300*1024*1024){event.preventDefault();alert("视频文件超过 300 MB，服务器会返回 413。请先压缩或缩短素材后再上传。");return;}
   if(narrationFile&&narrationFile.size>100*1024*1024){event.preventDefault();alert("配音文件最大支持 100 MB。");return;}
   if(subtitleFile&&subtitleFile.size>5*1024*1024){event.preventDefault();alert("字幕文件最大支持 5 MB。");return;}
    const totalUploadBytes=sourceVideo.size+(narrationFile?narrationFile.size:0)+(subtitleFile?subtitleFile.size:0);
    if(totalUploadBytes>290*1024*1024){event.preventDefault();alert("视频、配音和字幕合计不能超过 290 MB，以避免上传请求返回 413。");return;}
    if(Number.isFinite(previewDuration)&&previewDuration>600){event.preventDefault();alert("视频时长超过 10 分钟，请先缩短素材。");return;}
    if(Number.isFinite(previewDuration)&&document.getElementById("upscale_to").value==="2160"&&previewDuration>180){event.preventDefault();alert("免费实例的 4K 放大最长支持 3 分钟，请缩短素材或选择 1440p 以下。");return;}
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
 metadataForm.addEventListener("submit",(event)=>{
   const selected=document.getElementById("metadata_video").files[0];
   if(!selected){event.preventDefault();alert("请先选择视频文件。");return;}
   if(selected.size>300*1024*1024){event.preventDefault();alert("视频文件超过 300 MB，服务器会返回 413。请先压缩或缩短素材后再上传。");return;}
   metadataButton.disabled=true;metadataButton.textContent="正在写入元数据，请保持页面打开…";
 });
 window.addEventListener("pagehide",()=>{if(objectUrl)URL.revokeObjectURL(objectUrl);});
})();