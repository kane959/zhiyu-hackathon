(function App(){
"use strict";
// crypto.randomUUID only exists in a secure context (HTTPS or localhost).
// On a plain http://IP page it is undefined and the match/reader flows throw.
// getRandomValues works in every context, so fill in a v4-shaped UUID when needed.
if(globalThis.crypto && !globalThis.crypto.randomUUID){
  globalThis.crypto.randomUUID=()=>{
    const b=globalThis.crypto.getRandomValues(new Uint8Array(16));
    b[6]=(b[6]&0x0f)|0x40; b[8]=(b[8]&0x3f)|0x80;
    const h=[...b].map(x=>x.toString(16).padStart(2,"0"));
    return `${h.slice(0,4).join("")}-${h.slice(4,6).join("")}-${h.slice(6,8).join("")}-${h.slice(8,10).join("")}-${h.slice(10).join("")}`;
  };
}
const root=document.querySelector("#root"),toast=document.querySelector("#toast");
const esc=x=>String(x??"").replace(/[&<>"']/g,v=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[v]));
const repo={get(k){try{return sessionStorage.getItem("zy:"+k)}catch{return null}},set(k,v){try{sessionStorage.setItem("zy:"+k,v)}catch{}},del(k){try{sessionStorage.removeItem("zy:"+k)}catch{}}};
let ws,pid,detail,epoch=0,reader,receipt,editing;
const icon={home:"M3 10l9-7 9 7v10H3z M9 20v-7h6v7",edit:"M4 20l4-1 12-12-3-3L5 16z",match:"M3 6h5l8 12h5 M3 18h5l3-4 M13 10l3-4h5",result:"M4 4v16h17 M8 16v-4 M13 16V7 M18 16v-6"};
const svg=k=>`<svg class="ico" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round" viewBox="0 0 24 24" aria-hidden="true"><path d="${icon[k]}"/></svg>`;
const brand=`<a href="/" class="brand"><img alt="" src="/mark.svg"><span>知遇<small>场景试用工作台</small></span></a>`;
const tag=(t,k="")=>`<span class="tag ${k}">${esc(t)}</span>`;
const btn=(text,id,variant="")=>`<button type="button" data-do="${id}" class="${variant}">${text}</button>`;
const link=(text,href,variant="secondary")=>`<a class="btn ${variant}" href="${esc(href)}">${text}</a>`;
const head=(title,sub,act="")=>`<header class="head"><div><h1>${esc(title)}</h1><p class="sub">${esc(sub)}</p></div><div class="tools">${act}</div></header>`;
const field=(k,label,val="",area=false,wide=false,required=true,max=2000)=>`<div class="field ${wide?"wide":""}"><label for="${k}">${esc(label)}</label>${area?`<textarea id="${k}" name="${k}" maxlength="${max}" ${required?"required":""}>${esc(val)}</textarea>`:`<input id="${k}" name="${k}" maxlength="${max}" value="${esc(val)}" ${required?"required":""}>`}</div>`;
const summary=rows=>`<dl class="details">${rows.map(([k,v])=>`<dt>${esc(k)}</dt><dd>${esc(v)||"未填写"}</dd>`).join("")}</dl>`;
const publicLayout=(caption,body)=>`<div class="public-wrap"><header>${brand}<span class="small">${esc(caption)}</span></header><main id="main" class="public-sheet">${body}</main></div>`;
const href=(v,id=pid)=>"#"+v+(id?"/"+id:"");
const here=(v,id=pid)=>{const h=href(v,id); if(location.hash===h) render();else location.hash=h};
const navs=[["home","工作台"],["edit","项目登记"],["match","场景匹配"],["result","读者反馈"]];
function shell(v,body){
 const active=v==="card"||v==="published"?"match":v;
 const name=navs.find(x=>x[0]===active)?.[1]||"工作台";
 return `<div class="shell"><aside class="rail">${brand}<nav aria-label="开发者导航">${navs.map(([k,l])=>`<a href="${href(k,k==="home"?null:pid)}" ${k===active?'aria-current="page"':""}>${svg(k)}${l}</a>`).join("")}</nav><div class="rail-foot"><b>无敌大战队</b>开发者本地工作区</div></aside><div><div class="top"><span>知遇　/　${esc(name)}</span><span class="mode"><i class="dot"></i> 本地演示</span></div><div class="sheet"><main id="main" class="content">${body}</main></div></div></div>`;
}
async function api(path,payload){
 const r=await fetch("/api/"+path,payload===undefined?{cache:"no-store"}:{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(payload)});
 const x=await r.json();if(!r.ok)throw Error(x.error||"服务暂不可用");return x
}
const projectApi=(suffix,data)=>api("projects/"+encodeURIComponent(pid)+(suffix?"/"+suffix:""),data);
const readerApi=(suffix,data)=>api("cards/"+encodeURIComponent(reader.slug)+"/"+suffix,{version:reader.version,...data});
const formData=f=>Object.fromEntries(new FormData(f));
function say(t){toast.textContent=t;clearTimeout(say.t);say.t=setTimeout(()=>toast.textContent="",3300)}
function alertMsg(msg){
 let a=document.querySelector("#alert"); if(!a){a=document.createElement("div");a.id="alert";a.setAttribute("role","alert");(document.querySelector("main")||root).prepend(a)}
 a.className="error";a.textContent=msg;a.scrollIntoView({block:"nearest"});
}
const current=()=>ws?.projects.find(p=>p.projectId===pid);
const selectedProject=()=>ws.projects.length?`<label class="row small">当前工具 <select id="switch-project">${ws.projects.map(p=>`<option value="${esc(p.projectId)}" ${pid===p.projectId?"selected":""}>${esc(p.name)}</option>`).join("")}</select></label>`:"";
const prep=()=>`<p class="empty">先登记工具，告诉知遇它接收什么材料、交付什么结果。</p><div class="tools form-end">${link("登记工具",href("edit","new"),"")}${btn("试跑访谈稿示例","example","secondary")}</div>`;
function home(){
 return head("开发者工作台","为你的工具找到一个合适的试用场景。",link("＋ 登记自己的工具",href("edit","new"),""))+
 `<section class="board"><div class="board-copy"><h2>让工具遇见<br>需要它的人。</h2><p class="sub">拿访谈摘录工具「拾句」试一次：找到需要核对原话的读者，给他一张能照着做的试用卡。</p>${btn("试跑访谈稿示例　→","example")}</div><figure class="folio"><figcaption class="row spread"><b>场景摘录</b><span class="sample-label">合成样例</span></figcaption><blockquote><span class="qmark">“</span>我已经有了逐字稿和初步结论，想回到受访者的原话逐句核对，而不是再要一份摘要。</blockquote><p class="small">材料：访谈文字稿　 /　 任务：核对引文</p></figure></section>
 <div class="band grid"><section class="box"><div class="bandhead"><h2>我的工具</h2><span class="small">${ws.projects.length} 个项目</span></div>${ws.projects.length?ws.projects.map(p=>`<div class="project-row"><a href="${href("match",p.projectId)}">${esc(p.name)} →</a><p class="small">${esc(p.task)}</p></div>`).join(""):`<p class="empty">还没有登记工具。可以从上面的示例走一遍，再换成自己的项目。</p>`}</section><aside class="box quietbox"><h2>一张试用卡怎么用</h2><ol class="process"><li>开发者核对场景与原文证据。</li><li>读者带上材料，到原工具试用。</li><li>返回写下做完了，还是卡在哪一步。</li></ol><p class="small">当前场景为合成文本。知乎官方检索及模型尚未配置。</p></aside></div>`;
}
function edit(id){
 const p=id==="new"?{}:(current()||{});
 return head(p.projectId?"编辑工具登记":"登记自己的工具","按读者实际能做的一件事来写。")+`<form id="project" class="box"><div class="formgrid">${field("name","工具名称",p.name)}${field("url","工具入口（网址）",p.url)}${field("audience","给谁使用",p.audience,false,true)}${field("material","读者要带什么材料",p.material,true)}${field("task","能完成什么任务",p.task,true)}${field("output","能拿到什么结果",p.output,true)}${field("limits","哪些情况不适用（选填）",p.limits,true,false,false)}</div><div class="tools form-end"><button>保存并查看场景</button>${link("返回工作台","#home")}</div><div class="formalert" id="alert" role="alert"></div></form>`;
}
function match(){
 if(!detail)return head("场景匹配","从工具能力出发寻找读者场景。")+prep();
 const p=current(),r=detail.run,stale=r&&Object.keys(r.projectSnapshot).some(k=>r.projectSnapshot[k]!==p[k]);
 let result;
 if(!r||stale)result=`<div class="empty">${stale?"登记内容已变更，请重新匹配。":"还没有匹配记录。运行本地样例即可核对能力和证据。"}</div>`;
 else if(r.status==="failed")result=`<div class="error"><b>真实检索未配置</b><p>知乎官方接口和模型尚未接通。已保留工具登记，可切回本地样例。错误码：not_configured</p></div>`;
 else if(!r.candidates.length)result=`<div class="empty"><b>没有找到相符场景</b><p>合成样例库只覆盖访谈引文、录音转写和课程截止时间。可以调整工具描述；这里不会补造结果。</p></div>`;
 else result=r.candidates.map(c=>`<article class="box"><div class="row spread">${tag(c.judgment,c.judgment==="排除"?"no":c.judgment==="待核实"?"unknown":"")}<span class="small">合成问答 · 非知乎原文</span></div><h3 class="band">${esc(c.title)}</h3><blockquote class="source">${esc(c.evidence)}</blockquote><p>${esc(c.reason)}</p><p class="small">尚需核对：${esc(c.unknown)}</p><div class="tools form-end"><a href="${esc(c.url)}" target="_blank" rel="noopener">查看样例全文 ↗</a>${c.judgment==="适配"?link("选这个场景做卡片",href("card",pid)+"/"+c.id,""):""}</div></article>`).join("");
 return head("匹配使用场景",p.name,selectedProject())+`<div class="grid"><section><div class="bandhead"><h2>候选场景</h2><span class="small">${r?.mode==="synthetic"&&!stale?`${r.candidates.length} 条 · 本地规则`:"最多展示 3 条"}</span></div>${result}</section><aside><div class="box quietbox"><h2>登记的工具能力</h2>${summary([["目标用户",p.audience],["输入",p.material],["任务",p.task],["输出",p.output],["边界",p.limits]])}${link("修改登记",href("edit"))}</div><div class="box"><h3>匹配来源</h3><p class="small">本地样例可以立即试用。真实模式需要官方检索及模型。</p><div class="tools form-end">${btn("运行本地匹配","run")}${btn("检查真实模式","live","secondary")}</div></div>${detail.cards.length?`<div class="box"><h3>已发布版本</h3>${detail.cards.map(c=>`<div class="version-row"><a href="${href("published",pid)}/${c.slug}">v${c.version} · ${esc(c.title)}</a> ${c.active?"":"（已撤回）"}</div>`).join("")}</div>`:""}</aside></div>`;
}
function defaultDraft(p,c){
 const interview=c.id==="interview";
 return {title:interview?"从访谈稿里找出支持结论的原话":p.task,material:p.material,steps:interview?"准备一段脱敏文字稿，写下一条想核对的结论。\n在原工具中查找候选引文，逐句回到上下文核对。":"准备好材料，再打开原工具。\n"+p.task+"，核对输出是否符合预期。",criterion:interview?"得到三句能定位到原文的候选引文，并指出哪一句最能支持结论。":p.output,question:interview?"哪句原话最能支持结论？如果不能，具体差在哪里？":"结果哪一处有用，或哪一步让你卡住了？"};
}
const draftKey=(c)=>"draft/"+pid+"/"+detail.run.runId+"/"+c.id;
function cardEditor(cid){
 const c=detail?.run?.candidates.find(x=>x.id===cid&&x.judgment==="适配");
 if(!c)return head("先选择场景","请先在匹配结果中选择一条有原文依据的适配场景。",link("查看场景",href("match")));
 let saved;try{saved=JSON.parse(repo.get(draftKey(c)))}catch{}
 const d=saved||defaultDraft(current(),c);
 return head("编辑试用卡","写清楚读者带什么、做什么，以及怎样判断有用。",link("返回匹配",href("match")))+`<div class="grid"><form id="card-edit" class="box" data-cid="${esc(cid)}"><div class="formgrid">${field("title","卡片标题",d.title,false,true)}${field("material","所需材料",d.material,true,true)}${field("steps","试用步骤（每行一步）",d.steps,true,true)}${field("criterion","完成标准",d.criterion,true,true)}${field("question","想听读者回答的问题",d.question,true,true)}</div><div class="tools form-end"><button>预览试用卡</button></div></form><aside class="box quietbox"><h2>选中的场景</h2><p>${esc(c.title)}</p><blockquote class="source">${esc(c.evidence)}</blockquote><p class="small">合成样例 · 试用材料直接在原工具处理。</p></aside></div>`;
}
function cardBody(c){
 return `<div class="row spread"><b>${esc(c.tool)}</b><span class="sample-label">${c.version?"试用卡 v"+c.version:"发布前预览"} · 合成场景</span></div><h1>${esc(c.title)}</h1><p class="small">适用场景：${esc(c.scene)}</p><hr class="sep"><h2>带什么</h2><p class="copy">${esc(c.material)}</p><div class="band"><h2>怎么试</h2><ol class="process">${c.steps.split(/\r?\n/).filter(x=>x.trim()).map(x=>`<li>${esc(x)}</li>`).join("")}</ol></div><div class="box quietbox"><h3>做到什么算完成</h3><p class="copy">${esc(c.criterion)}</p></div>`;
}
function published(slug){
 const c=detail?.cards.find(x=>x.slug===slug);if(!c)return head("找不到这个版本","回到场景页查看已发布版本。",link("查看场景",href("match")));
 const url=new URL("/t/"+c.slug,location.origin).href;
 return head(c.active?"试用卡已发布":"试用卡已撤回",`版本 v${c.version} · ${c.title}`)+`<div class="grid"><section class="box"><h2>读者链接</h2><p class="small">本机地址可在这台电脑的另一浏览器打开。外部读者需要公网部署后的链接。</p><input class="urlbox" aria-label="读者链接" readonly value="${esc(url)}"><div class="tools">${c.active?`<a class="btn" href="${esc(url)}" target="_blank" rel="noopener">打开读者页 ↗</a>${btn("复制链接","copy","secondary")}`:""}${link("查看读者反馈",href("result"))}</div></section><aside class="box quietbox"><h2>版本记录</h2><p>这张卡使用发布时的快照。修改内容后再发布会生成新版本。</p>${c.active?`<div class="tools form-end">${btn("撤回此版本","withdraw-check","secondary")}</div>`:""}</aside></div>`;
}
function dashboard(data,filter){
 if(!detail)return head("读者反馈","分别查看访问、进入工具和自报完成。")+prep();
 const vs=data.versions,blocks=filter?vs.filter(c=>c.slug===filter):vs;
 const stats=[["卡片访问",blocks.reduce((s,x)=>s+x.visits,0)],["进入工具",blocks.reduce((s,x)=>s+x.opened,0)],["自报完成",blocks.reduce((s,x)=>s+x.completed,0)],["具体反馈",blocks.reduce((s,x)=>s+x.feedback.length,0)]];
 const rows=blocks.flatMap(c=>c.feedback.map(x=>({...x,version:c.version}))).sort((a,b)=>b.at-a.at);
 return head("读者反馈",current().name,selectedProject())+`<section class="box"><div class="row spread"><h2>试用记录</h2><label class="small">卡片版本 <select id="version"><option value="">全部版本</option>${vs.map(v=>`<option value="${v.slug}" ${filter===v.slug?"selected":""}>v${v.version}</option>`).join("")}</select></label></div><dl class="stats">${stats.map(([l,n])=>`<div><dt>${l}</dt><dd>${n}</dd></div>`).join("")}</dl><p class="small">${ws.demoWorkspace?"这是演示工作区，初始有 2 次合成访问及反馈。 ":""}每个读者浏览器会话在同一版本只计一次。进入工具只计点击，完成须由读者自己提交。</p></section><section class="box"><h2>反馈原文</h2>${rows.length?rows.map(x=>`<article class="entry">${tag(x.status==="done"?"自报做完":"卡住了",x.status==="done"?"":"no")} <span class="small">v${x.version} · ${esc(new Date(x.at*1000).toLocaleString("zh-CN"))}</span>${x.step?`<p>卡住的位置：${esc(x.step)}</p>`:""}<p class="verbatim">${esc(x.note)}</p></article>`).join(""):`<p class="empty">这个范围内还没有反馈。发布卡片后，让读者从读者页填写。</p>`}</section>`;
}
function showPreview(){
 root.innerHTML=shell("card",head("预览试用卡","确认以下内容可以交给读者，再发布这个版本。")+`<section class="public-sheet">${cardBody(editing)}<hr class="sep"><h3>反馈问题</h3><p>${esc(editing.question)}</p><div class="tools form-end">${btn("确认发布","publish")}${btn("继续编辑","back-edit","secondary")}</div></section>`);window.scrollTo(0,0)
}
function feedbackForm(){
 if(receipt)return `<div class="notice" role="status"><strong>反馈已收到 · ${receipt.status==="done"?"做完了":"卡住了"}</strong><p>已归入 v${reader.version}。刷新页面也不会重复提交。</p></div>`;
 let draft={};try{draft=JSON.parse(repo.get("feedback/"+reader.slug))||{}}catch{}
 return `<form id="feedback"><h2>试过之后</h2><fieldset><legend>这次任务怎么样？</legend><label class="choice"><input type="radio" name="status" value="done" required ${draft.status==="done"?"checked":""}> 做完了</label><label class="choice"><input type="radio" name="status" value="blocked" required ${draft.status==="blocked"?"checked":""}> 卡住了</label></fieldset><div id="step-wrap" ${draft.status==="blocked"?"":"hidden"}>${field("step","卡在哪一步？",draft.step||"",false,false,draft.status==="blocked",300)}</div>${field("note",reader.question,draft.note||"",true,false,true,1000)}<p class="hint">只写操作体验，请勿粘贴原始材料或个人信息。</p><div class="tools form-end"><button>提交反馈</button></div></form>`;
}
function drawReader(){
 root.innerHTML=publicLayout("读者试用页",cardBody(reader)+`<div class="tools form-end"><a href="${esc(reader.url)}" id="open-tool" class="btn" target="_blank" rel="noopener noreferrer">打开原工具试用 ↗</a><span class="small">试完回到此页填写反馈。</span></div><div class="band" id="reader-feedback">${feedbackForm()}</div>`);
}
const sampleText="甲：我每次读完访谈，只留下摘要，汇报时很难找回原话。\n乙：开会有人问结论从哪里来，我又从头翻了一遍逐字稿。\n丙：如果每句引用都有原文位置，我就能当场核对。\n丁：我习惯把会议安排在下午。";
function sample(){return publicLayout("示例原工具",`<span class="sample-label">本地规则示例</span><h1>拾句 · 访谈证据摘录</h1><p>输入脱敏文字稿与结论，按文字重合度列出最多三句候选，供你回原文核对。材料仅在本页处理。</p><form id="extract">${field("transcript","访谈文字稿（每行一句）",sampleText,true,true,true,16000)}${field("claim","想核对的结论","研究者需要找回访谈原话，核对结论的来源。",true,true)}<div class="tools form-end"><button>查找候选原话</button></div></form><div id="quotes" aria-live="polite"></div>`)}
async function render(){
 const seq=++epoch,path=location.pathname,parts=location.hash.slice(1).split("/"),[v="home",id,arg]=parts;
 try{
  if(path==="/sample"){root.innerHTML=sample();return}
  if(path.startsWith("/source/")){const s=await api("sources/"+path.split("/").pop());if(seq===epoch)root.innerHTML=publicLayout("证据样例",`<span class="sample-label">合成全文 · 非知乎原文</span><h1>${esc(s.title)}</h1><p class="copy">${esc(s.body)}</p><hr class="sep"><h2>匹配用的证据</h2><blockquote class="source">${esc(s.evidence)}</blockquote>`);return}
  if(path.startsWith("/t/")){
    const card=await api("cards/"+path.split("/").pop());if(seq!==epoch)return;reader=card;
    receipt=(await readerApi("visit",{})).receipt;if(seq===epoch)drawReader();return
  }
  const workspace=await api("workspace");if(seq!==epoch)return;ws=workspace;
  pid=id&&id!=="new"?id:ws.projects.some(x=>x.projectId===repo.get("project"))?repo.get("project"):ws.projects[0]?.projectId;
  if(pid)repo.set("project",pid);
  const projectDetail=pid&&v!=="home"&&v!==""&&!(v==="edit"&&id==="new")?await projectApi(""):null;if(seq!==epoch)return;detail=projectDetail;
  let body;
  if(v==="edit")body=edit(id);else if(v==="match")body=match();else if(v==="card")body=cardEditor(arg);else if(v==="published")body=published(arg);else if(v==="result"){const data=detail?await projectApi("results"):{versions:[]};if(seq!==epoch)return;body=dashboard(data,arg)}else body=home();
  root.innerHTML=shell(v||"home",body);
 }catch(e){if(seq===epoch)root.innerHTML=publicLayout("访问提示",`<h1>暂时无法打开</h1><p class="error">${esc(e.message)}</p><div class="tools">${btn("重新读取","retry","secondary")}${link("回到工作台","/")}</div>`)}
}
async function run(mode){await projectApi("runs",{requestId:crypto.randomUUID(),mode});here("match")}
root.addEventListener("click",async e=>{
 const a=e.target.closest("[data-do],#open-tool");if(!a)return;
 if(a.id==="open-tool"){readerApi("open",{}).catch(x=>alertMsg("已打开工具，但点击记录未保存："+x.message));return}
 const job=a.dataset.do;a.disabled=true;
 try{
  if(job==="retry")await render();
  if(job==="example"){let p=ws.projects.find(x=>Object.keys(ws.example).every(k=>x[k]===ws.example[k]));if(!p)p=await api("projects",ws.example);pid=p.projectId;await run("synthetic")}
  if(job==="run"||job==="live")await run(job==="live"?"live":"synthetic");
  if(job==="back-edit")await render();
  if(job==="publish"){const c=await projectApi("publish",editing);say("试用卡已发布");here("published",pid+"/"+c.slug)}
  if(job==="copy"){const input=root.querySelector(".urlbox");try{await navigator.clipboard.writeText(input.value);say("已复制读者链接")}catch{input.focus();input.select();say("已选中链接，可以复制")}}
  if(job==="withdraw-check"){a.parentElement.innerHTML=`<span>撤回后读者将无法打开，历史反馈保留。</span>${btn("确认撤回","withdraw","secondary")}${btn("取消","retry","quiet")}`}
  if(job==="withdraw"){await projectApi("withdraw/"+location.hash.split("/")[2],{});await render()}
 }catch(x){alertMsg(x.message)}finally{a.disabled=false}
});
root.addEventListener("change",e=>{
 if(e.target.id==="switch-project"){here(location.hash.slice(1).split("/")[0],e.target.value);return}
 if(e.target.id==="version"){here("result",pid+(e.target.value?"/"+e.target.value:""));return}
 if(e.target.name==="status"){const blocked=e.target.value==="blocked";document.querySelector("#step-wrap").hidden=!blocked;document.querySelector("#step").required=blocked}
});
root.addEventListener("input",e=>{
 const f=e.target.form;if(f?.id==="feedback")repo.set("feedback/"+reader.slug,JSON.stringify(formData(f)));
 if(f?.id==="card-edit")repo.set(draftKey({id:f.dataset.cid}),JSON.stringify(formData(f)))
});
root.addEventListener("submit",async e=>{
 e.preventDefault();const f=e.target,b=f.querySelector("button");if(!b||b.disabled)return;b.disabled=true;
 try{
  const d=formData(f);
  if(f.id==="project"){const existing=location.hash.split("/")[1]!=="new"?current():null;const p=await api("projects",{...d,...(existing?{projectId:existing.projectId}:{})});pid=p.projectId;await run("synthetic")}
  if(f.id==="card-edit"){
   const cid=f.dataset.cid,c=detail.run.candidates.find(x=>x.id===cid);repo.set(draftKey(c),JSON.stringify(d));editing={...d,tool:current().name,scene:c.title,runId:detail.run.runId,candidateId:cid};showPreview()
  }
  if(f.id==="feedback"){
   let sid=repo.get("sid/"+reader.slug)||crypto.randomUUID();repo.set("sid/"+reader.slug,sid);
   receipt=(await readerApi("feedback",{...d,step:d.status==="blocked"?d.step:"",submissionId:sid})).receipt;repo.del("feedback/"+reader.slug);document.querySelector("#reader-feedback").innerHTML=feedbackForm()
  }
  if(f.id==="extract"){
   const key=new Set([...d.claim].filter(ch=>/[\p{L}\p{N}]/u.test(ch)));
   const rows=d.transcript.split(/\r?\n/).map((line,i)=>({line:line.trim(),i:i+1,score:[...new Set(line)].filter(ch=>key.has(ch)).length})).filter(x=>x.line&&x.score).sort((a,b)=>b.score-a.score||a.i-b.i).slice(0,3);
   document.querySelector("#quotes").innerHTML=`<h2 class="band">候选原话</h2>${rows.length?`<ol class="demo-output">${rows.map(x=>`<li>第 ${x.i} 行：${esc(x.line)}</li>`).join("")}</ol>`:`<p class="empty">没有文字重合的引文，请调整结论后重试。</p>`}<p class="small">文字重合不等于支持结论，请人工核对上下文。</p>`
  }
 }catch(x){alertMsg(x.message)}finally{b.disabled=false}
});
window.addEventListener("hashchange",()=>{render();window.scrollTo(0,0)});render()
})();
