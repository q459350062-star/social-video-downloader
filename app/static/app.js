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
  $("note").textContent=j.note; $("result").classList.remove("hidden"); status("解析成功。已列出最高画质及可用分辨率。 ");
 }catch(e){status(e.message,true)}finally{$("resolve").disabled=false}
};
$("download").onclick=async()=>{
 if(!data)return; $("download").disabled=true; status("正在下载并按需合并音视频流，请保持页面打开…");
 try{
  const r=await fetch("/api/download",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({url:currentUrl,quality:$("formats").value})});
  if(!r.ok){let j={};try{j=await r.json()}catch{}throw new Error(j.detail||"下载失败")}
  const blob=await r.blob(); const disposition=r.headers.get("Content-Disposition")||"";const match=disposition.match(/filename\*=UTF-8''([^;]+)|filename="?([^";]+)"?/i);const filename=match?decodeURIComponent(match[1]||match[2]):"video";
  const a=document.createElement("a");const obj=URL.createObjectURL(blob);a.href=obj;a.download=filename;document.body.appendChild(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(obj),30000);status("文件已准备，浏览器已开始下载。");
 }catch(e){status(e.message,true)}finally{$("download").disabled=false}
};