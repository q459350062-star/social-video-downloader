const $=id=>document.getElementById(id);
let currentUrl="", data=null;
function status(msg,error=false){$("status").textContent=msg;$("status").style.color=error?"#ff9b9b":"#aab4c5";}
function renderSource(s){
 const items=[`源站最高规格：${s.max_resolution||"未知"}`,`最高帧率：${s.max_fps?`${s.max_fps} FPS`:"未提供"}`,`视频编码：${(s.video_codecs||[]).join(" / ")||"未知"}`,`HDR：${(s.hdr||[]).join(" / ")||"未检测到/未提供"}`];
 $("sourceInfo").innerHTML="";
 items.forEach(t=>{const li=document.createElement("li");li.textContent=t;$("sourceInfo").appendChild(li)});
}
$("paste").onclick=async()=>{try{$("url").value=await navigator.clipboard.readText();}catch{status("浏览器未允许读取剪贴板，请长按输入框粘贴。",true)}};
$("resolve").onclick=async()=>{
 const url=$("url").value.trim(); if(!url){status("请先粘贴视频链接。",true);return}
 currentUrl=url; $("result").classList.add("hidden"); $("resolve").disabled=true; status("正在识别平台并读取源站可用规格…");
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
  $("result").classList.remove("hidden"); status("解析成功。已列出最高画质及可用分辨率。 ");
 }catch(e){status(e.message,true)}finally{$("resolve").disabled=false}
};
$("download").onclick=()=>{
 if(!data)return;
 const quality=encodeURIComponent($("formats").value);
 const outputMode=encodeURIComponent($("outputMode").value);
 const url=encodeURIComponent(currentUrl);
 const button=$("download");
 button.disabled=true;
 button.textContent="正在转换，请勿重复点击…";
 status("服务器正在生成 iPhone 兼容 MP4。此过程可能需要 1–3 分钟，请保持页面打开，不要连续点击。完成后浏览器会自动开始下载。");
 // Native attachment response avoids buffering the whole video in a JS Blob.
 window.location.href=`/api/download-file?url=${url}&quality=${quality}&output_mode=${outputMode}`;
};
