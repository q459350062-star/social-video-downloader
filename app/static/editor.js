(()=>{
 const file=document.getElementById("video");
 const preview=document.getElementById("preview");
 let objectUrl="";
 file.addEventListener("change",()=>{
   if(objectUrl) URL.revokeObjectURL(objectUrl);
   const f=file.files&&file.files[0];
   if(!f){preview.hidden=true;preview.removeAttribute("src");return;}
   objectUrl=URL.createObjectURL(f);
   preview.src=objectUrl;
   preview.hidden=false;
 });
 window.addEventListener("pagehide",()=>{if(objectUrl)URL.revokeObjectURL(objectUrl);});
})();