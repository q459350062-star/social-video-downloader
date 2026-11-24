const $=id=>document.getElementById(id);
let currentUrl="", data=null;
function status(msg,error=false){$("status").textContent=msg;$("status").style.color=error?"#ff9b9b":"#aab4c5";}
function updateDownloadHref(){
 if(!data || !currentUrl) return;
 const params=new URLSearchParams({
  url:currentUrl,
  quality:$("formats").value||"best",
  output_mode:$("outputMode").value||"ios"
 });
 $("download").href="/api/download-file?"+params.toString();
}
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
 currentUrl=url; data=null; $("download").removeAttribute("href"); $("result").classList.add("hidden"); $("resolve").disabled=true; status("正在识别平台并读取源站可用规格…");
 try{
  const r=await fetch("/api/resolve",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({url})});
  const j=await r.json(); if(!r.ok)throw new Error(j.detail||"解析失败");
  data=j; $("platform").textContent=j.platform; $("title").textContent=j.title;
  $("duration").textContent=j.duration?`时长：${Math.floor(j.duration/60)}分${j.duration%60}秒`:"";
  $("thumb").src=j.thumbnail||""; $("thumb").style.visibility=j.thumbnail?"visible":"hidden";
  $("formats").innerHTML="";
  j.choices.forEach(f=>{const o=document.createElement("option");o.value=f.quality;o.textContent=f.label;$("formats").appendChild(o)});
  renderSource(j.source||{});
  const provenance=[
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
  updateDownloadHref();
  $("result").classList.remove("hidden"); status("解析成功。已列出最高画质及可用分辨率。请直接点击绿色下载按钮。");
 }catch(e){status(e.message||"解析失败，请重试。",true)}finally{$("resolve").disabled=false}
};
$("formats").addEventListener("change",updateDownloadHref);
$("outputMode").addEventListener("change",updateDownloadHref);
