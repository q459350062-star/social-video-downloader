const $=id=>document.getElementById(id);
let currentUrl="", data=null;
function status(msg,error=false){$("status").textContent=msg;$("status").style.color=error?"#ff9b9b":"#aab4c5";}
function renderSource(s){
 const items=[
  `源站最高规格：${s.max_resolution||"未知"}`,
  `最高帧率：${s.max_fps?`${s.max_fps} FPS`:"未提供"}`,
  `视频编码：${(s.video_codecs||[]).join(" / ")||"未知"}`,
  `HDR状态：${s.hdr_status||"源站未提供明确标记"}`,
  `优先规格编码：${s.best_video_codec||"未知"}`,
  `优先规格动态范围：${s.best_dynamic_range||"未提供"}`
 ];
 $("sourceInfo").innerHTML="";
 items.forEach(t=>{const li=document.createElement("li");li.textContent=t;$("sourceInfo").appendChild(li)});
}
$("paste").onclick=async()=>{try{$("url").value=await navigator.clipboard.readText();}catch{status("浏览器未允许读取剪贴板，请长按输入框粘贴。",true)}};
$("resolve").onclick=async()=>{
 const url=$("url").value.trim(); if(!url){status("请先粘贴视频链接。",true);return}
 currentUrl=url; data=null; $("result").classList.add("hidden"); $("resolve").disabled=true; status("正在识别平台并读取源站可用规格…");
 try{
  const r=await fetch("/api/resolve",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({url})});
  const j=await r.json(); if(!r.ok)throw new Error(j.detail||"解析失败");
  data=j; $("platform").textContent=j.platform; $("title").textContent=j.title;
  $("duration").textContent=j.duration?`时长：${Math.floor(j.duration/60)}分${j.duration%60}秒`:"";
  $("thumb").src=j.thumbnail||""; $("thumb").style.visibility=j.thumbnail?"visible":"hidden";
  $("formats").innerHTML="";
  j.choices.forEach(f=>{const o=document.createElement("option");o.value=f.quality;o.textContent=f.label;$("formats").appendChild(o)});
  renderSource(j.source||{});
  const provenance = [
   `创作者：${j.creator||"源站未提供"}`,
   `原始发布日期：${j.source_date||"源站未提供"}`,
   `内容编号：${j.source_post_id||"源站未提供"}`
  ];
  $("provenance").innerHTML="";
  provenance.forEach(t=>{const li=document.createElement("li");li.textContent=t;$("provenance").appendChild(li)});
  $("sourceLink").href=j.source_url||currentUrl;
  $("note").textContent=j.note;
  if(j.platform==="Instagram" && !(j.source||{}).hdr_available){
   $("note").textContent += " 请注意：当前认证状态或源站返回结果不包含 HDR；不要把本次文件当作 HDR。";
  }
  $("result").classList.remove("hidden"); status("解析成功。已列出最高画质及可用分辨率。");
 }catch(e){status(e.message||"解析失败，请重试。",true)}finally{$("resolve").disabled=false}
};
$("download").onclick=()=>{
 if(!data || !currentUrl){status("请先成功解析 Instagram 视频，再点击下载。",true);return}
 const params=new URLSearchParams({
  url:currentUrl,
  quality:$("formats").value||"best",
  output_mode:$("outputMode").value||"ios"
 });
 const downloadUrl=`/api/download-file?${params.toString()}`;
 const button=$("download");
 button.disabled=true;
 button.textContent="下载请求已发送…";
 const mode=$("outputMode").value==="source"?"正在准备源流文件。":"服务器正在生成 iPhone 兼容 MP4（必要时重新编码），大文件可能需要几分钟。";
 status(mode+" 如果没有自动开始，请点击下面的备用下载链接。");
 // Open the attachment in a separate browsing context so the result page remains visible on iPhone.
 let fallback=$("manualDownload");
 if(!fallback){
  fallback=document.createElement("a");
  fallback.id="manualDownload";
  fallback.className="download-fallback";
  fallback.target="_blank";
  fallback.rel="noopener";
  fallback.style.cssText="display:inline-block;margin-top:12px;padding:10px 14px;border:1px solid #b7f36b;border-radius:10px;color:#b7f36b;text-decoration:none;font-weight:600";
  $("status").appendChild(document.createElement("br"));
  $("status").appendChild(fallback);
 }
 fallback.href=downloadUrl;
 fallback.textContent="手动开始下载";
 fallback.hidden=false;
 const launcher=document.createElement("a");
 launcher.href=downloadUrl;
 launcher.target="_blank";
 launcher.rel="noopener";
 launcher.style.display="none";
 document.body.appendChild(launcher);
 launcher.click();
 launcher.remove();
};
