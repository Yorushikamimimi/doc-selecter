'use strict';
// All data from Python is untrusted text. No markup interpolation or filesystem API.
const $ = id => document.getElementById(id);
const el = (tag, cls='', text='') => {const n=document.createElement(tag); n.className=cls; n.textContent=String(text); return n;};
const button = (text, action, primary=false) => {const b=el('button','btn'+(primary?' p':''),text);b.type='button';b.onclick=action;return b;};
let apiPending=0,pollTimer=null;
window.prepareClose=()=>{window.__closing=true;clearInterval(pollTimer);return apiPending===0;};
let state=null, last='', panel=null, panelKey='', inFlight=false, priorFocus=null;
const colors={'图片截图':'#2fa36b','文档资料':'#3a6fe0','安装包':'#d0562b','压缩包':'#a3712c','音视频':'#8150e6','代码与数据':'#2f86a8','待确认':'#8e8e93'};
async function call(name,...args){
  if(window.__closing)throw new Error('窗口正在关闭。');
  apiPending++;let result;try{result=await window.pywebview.api[name](...args);}finally{apiPending--;}
  if(!result.ok) throw new Error(result.error||'操作未完成，请检查。');
  return result.value;
}
function message(error){const {body,acts}=sheet('请先检查');body.append(el('p','sd',String(error.message||error)));acts.append(button('知道了',closeSheet));}
async function action(name,...args){try{const r=await call(name,...args);await refresh();return r;}catch(e){message(e);return null;}}
function closeSheet(){if(state)$('recursive').checked=state.subfolders.enabled;if(panel==='remaining' && state?.phase!=='archiving') call('remaining_cancel').catch(message);panel=null;panelKey='';$('veil').classList.remove('show');priorFocus?.focus();}
function sheet(title,wide=false,kind=null,sub=''){
  if(!$('veil').classList.contains('show')) priorFocus=document.activeElement;
  const side=wide==='drawer';panel=kind;const s=$('sheet');s.replaceChildren();s.className='sheet'+(side?' drawer':wide?' wide':'');$('veil').classList.toggle('side',side);
  const heading=el('div','st1',title);heading.id='sheet-title';const body=el('div','sheet-body'),acts=el('div','acts');
  if(side){const head=el('div','dh'),x=el('button','x','✕');x.type='button';x.setAttribute('aria-label','关闭');x.onclick=closeSheet;head.append(heading,el('span','dc',sub),x);s.append(head,body,acts);}else s.append(heading,body,acts);
  $('veil').classList.add('show');
  requestAnimationFrame(()=>s.querySelector('button,input,summary')?.focus({preventScroll:true}));
  return {body,acts};
}
function checkbox(text,checked,changed){const label=el('label'),input=el('input');input.type='checkbox';input.checked=checked;input.onchange=()=>changed(input.checked);label.append(input,document.createTextNode(text));return label;}
function folderIcon(){const svg=document.createElementNS('http://www.w3.org/2000/svg','svg');for(const [k,v]of Object.entries({width:56,height:48,viewBox:'0 0 56 48',fill:'none',stroke:'currentColor','stroke-width':2}))svg.setAttribute(k,v);svg.classList.add('big','float');const p=document.createElementNS(svg.namespaceURI,'path');p.setAttribute('d','M4 12h17l6 6h25v24H4z M4 12V6h17l6 6h19v6');svg.append(p);return svg;}
function primary(){if(!state?.selected)return action('pick_folder');if(state.can_organize)return confirmOrganize();return action('preview');}
function confirmOrganize(){
 const hash=state.plan_hash,n=state.movable_count;const {body,acts}=sheet(`整理 ${n} 个文件？`);
 body.append(el('p','sd',`会移到 ${state.folder} 里的「下载分类」对应文件夹。同名文件会跳过、不覆盖。整理后可以撤销。`));
 const mini=el('div','mini');for(const [c,v]of Object.entries(state.categories))if(v.selected&&v.count-v.will_skip>0)mini.append(el('span','',`${c} ${v.count-v.will_skip}`));body.append(mini);
 if(state.settings.archive)body.append(el('p','sd','本次整理后变空的已选文件夹也会归档；撤销时仅在身份与原位置条件允许时还原。'));
 acts.append(button('取消',closeSheet),button(`整理 ${n} 个`,()=>{closeSheet();action('organize',hash,true);},true));
}
function confirmUndo(version=null,token=null){const {body,acts}=sheet(version===null?'撤销上一次整理？':'退回到这次操作后的状态？');body.append(el('p','sd','文件会放回原位置。原位置被占用或文件已变化时会停下，不会覆盖现有文件。'));const credential=token||state.undo_token;acts.append(button('取消',closeSheet),button('确认撤销',()=>{closeSheet();action(version===null?'undo':'return_to',...(version===null?[credential,true]:[version,credential,true]));},true));}
const typeAlias={docx:'DOC',xlsx:'XLS',pptx:'PPT',jpeg:'JPG',markdown:'MD'};
function typeLabel(r){if(r.category==='待确认')return '?';const m=/\.([^.]+)$/.exec(r.name||'');const ext=m?m[1].toLowerCase():'';return (typeAlias[ext]||ext.toUpperCase()||'?').slice(0,4)}
function showTooltip(node,text){node.title=text;const show=()=>{const tip=$('tip');tip.textContent=text;const r=node.getBoundingClientRect();tip.style.left=Math.max(4,Math.min(r.left,innerWidth-430))+'px';tip.style.top=Math.max(4,Math.min(r.bottom+5,innerHeight-65))+'px';tip.style.opacity='1';};const hide=()=>{$('tip').style.opacity='0';};node.onmouseenter=show;node.onmouseleave=hide;node.onfocus=show;node.onblur=hide;}
function classifyName(r,focus=true){
 const wrap=el('div','classify-name'),name=el('div','fn',r.display_name||r.name);
 if(focus)name.tabIndex=0;showTooltip(name,r.display_name&&r.display_name!==r.name?'原名：'+r.name:r.name);
 wrap.append(name,el('div','classify-basis',r.basis));
 for(const hint of r.hints||[]){const chip=el('div','classify-chip'+(hint.kind==='encoded'?' classify-info':''),hint.text);if(focus)chip.tabIndex=0;showTooltip(chip,hint.text);wrap.append(chip);}return wrap;
}

function render(v){
 const previous=state;state=v;$('fname').textContent=v.folder||'还没选文件夹';$('folder').classList.toggle('has',v.selected);$('fsub').textContent=v.selected?(v.subfolders.enabled?'扫描范围：含子文件夹':'扫描范围：仅顶层'):'选一个开始';
 $('chg').disabled=v.busy;$('recursive').checked=v.subfolders.enabled;$('recursive').disabled=!v.selected||v.busy;$('folders').disabled=!v.selected||v.busy;$('folders').classList.toggle('hidden',!v.subfolders.enabled);
 const labels={empty:'未预览',selected:'未预览',preview:'预览 · 只读',scanning:'正在扫描…',organizing:'整理中',archiving:'归档中',undoing:'还原中',done:'整理结果',undone:'未预览',needs_review:'需要检查',error:'需要检查',checking:'检查中',discovering:'读取中',preparing:'生成预览中',recovering:'检查中'};
 $('badge').textContent=labels[v.phase]||'未预览';$('badge').classList.toggle('live',v.busy);$('prog').classList.toggle('shimmer',v.busy);$('sectitle').textContent=v.phase==='done'?'这次整理的结果':'类别';$('title').textContent=v.phase==='empty'?'整理文件':v.phase==='preview'?`${v.filter==='hints'?'有提示的文件':v.filter||'全部文件'} · ${v.rows.length} 个`:v.phase==='done'?(v.filter==='hints'?'有提示的文件':v.filter||'全部文件'):v.busy?labels[v.phase]:v.folder;
 const cats=$('cats');cats.querySelectorAll('.cat').forEach(n=>n.remove());const pill=$('pill')||el('div','pill');pill.id='pill';if(!pill.parentNode)cats.append(pill);pill.style.transform=`translateY(${v.filter===null?0:Object.keys(colors).indexOf(v.filter)*30+30}px)`;pill.style.opacity=v.filter!=='hints'&&['preview','done'].includes(v.phase)?'1':'0';
 const entries=[['全部',null,{count:v.phase==='done'?v.moved_count:v.selected_count,skipped:v.phase==='done'?v.skipped_count:0,selected:true}],...Object.keys(colors).map(c=>[c,c,v.categories[c]]),...(v.hint_count?[['有提示','hints',{count:v.hint_count,selected:true}]]:[])];
 for(const [title,key,info]of entries){const row=el('div','cat'+(!info.selected?' off':'')+(key==='hints'?' classify-filter'+(v.filter==='hints'?' classify-on':''):''));row.tabIndex=0;row.setAttribute('role','button');row.setAttribute('aria-label',`筛选${title}`);row.onclick=()=>{if(!v.busy)action('set_filter',key);};row.onkeydown=e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();row.click();}};
 if(key&&key!=='hints'&&v.phase!=='done'){const box=el('span','cb'),input=el('input');input.type='checkbox';input.checked=info.selected;input.disabled=v.busy||!v.selected;input.setAttribute('aria-label',`包含${title}`);input.onclick=e=>e.stopPropagation();input.onchange=()=>action('set_categories',Object.keys(v.categories).filter(c=>c===key?input.checked:v.categories[c].selected));box.append(input);row.append(box);}else{const spacer=el('span');spacer.style.width='14px';row.append(spacer);}const dot=el('span','dot');dot.style.background=key==='hints'?'var(--warn)':colors[key]||'var(--ink)';row.append(dot);
 row.append(el('span','nm',title),el('span','ct',(['empty','selected','undone'].includes(v.phase)?'':String(info.count||''))+(info.skipped?` +${info.skipped}`:'')));if(key&&key!=='hints'){const help=el('div','classify-help');help.id='classify-help-'+Object.keys(colors).indexOf(key);help.setAttribute('role','tooltip');help.append(el('b','',title),el('div','classify-help-note',(v.category_subfolders?.[key]||[]).length?'只细分顶层文件；子文件夹里的文件保留相对层级。':'不细分，保持原样。'));const list=el('ul');for(const sub of v.category_subfolders?.[key]||[])list.append(el('li','',sub));help.append(list);row.append(help);row.setAttribute('aria-describedby',help.id);}cats.append(row);}
 $('skipline').textContent=v.skipped_count?`${v.skipped_count} 个${v.phase==='done'?'已':'会'}跳过 · 看原因 ›`:v.excluded_count?`${v.excluded_count} 个未列入 · 看原因 ›`:''; $('skipline').classList.toggle('show',!!v.selected&&!!(v.skipped_count||v.excluded_count));$('skipline').disabled=v.busy;
 const bs=$('bannerSlot');bs.replaceChildren();if(v.banner){const b=el('div','banner '+v.banner.kind),text=el('div');text.append(el('div','bt',v.banner.title),el('div','bd',v.banner.description));if(v.banner.kind==='ok'){const svg=document.createElementNS('http://www.w3.org/2000/svg','svg');svg.setAttribute('viewBox','0 0 22 22');svg.classList.add('check');const path=document.createElementNS(svg.namespaceURI,'path');for(const [k,val]of Object.entries({d:'M6.5 11.3 9.6 14.2 15.5 8',stroke:'currentColor','stroke-width':2,fill:'none','stroke-linecap':'round','stroke-linejoin':'round'}))path.setAttribute(k,val);svg.append(path);b.append(svg);}b.append(text);if(v.can_open){const f=button('在访达中打开',()=>action('open_target'));f.classList.add('act');b.append(f);}bs.append(b);}
 const content=$('content');const updateOnly=previous&&previous.phase===v.phase&&v.busy&&previous.rows.map(r=>r.id).join()===v.rows.map(r=>r.id).join()&&content.querySelector('.rows');if(!updateOnly)content.replaceChildren();
 if(updateOnly){for(const r of v.rows){const row=content.querySelector(`[data-row="${CSS.escape(r.id)}"]`);if(row){const cell=row.querySelector('.st');if(cell.textContent!==r.label){cell.textContent=r.label;cell.className='st '+r.state;row.classList.toggle('skip',['sk','warn'].includes(r.state));if(['ok','back'].includes(r.state)){row.classList.add('fly');row.addEventListener('animationend',()=>row.classList.remove('fly'),{once:true});}}}}}
 else if(v.phase==='scanning'){const scan=el('div','scan');scan.append(el('div','ring'),el('b','','正在扫描文件夹'),el('div','now','只读取文件信息，不会移动文件'));content.append(scan);}
 else if(['empty','selected','undone'].includes(v.phase)){const empty=el('div','empty');empty.append(folderIcon(),el('div','et',v.phase==='empty'?'先选择要整理的文件夹':v.phase==='undone'?'需要的话，重新生成预览':'先预览，再决定'),el('div','ed',v.phase==='empty'?'选好后会马上生成只读预览，告诉你每个文件会被放到哪里。确认之前不会移动任何文件。':`可以先调整左边的类别，再为 ${v.folder} 生成一次预览。`),button(v.phase==='empty'?'选择文件夹…':'生成预览',primary,true));content.append(empty);}
 else {content.append(el('div','summary',v.busy?'正在处理 · 逐项结果以实际记录为准':v.phase==='done'?`${v.rows.filter(r=>r.state==='ok').length} 个已整理 · ${v.skipped_count} 个跳过`:`发现 ${v.total} 个文件，${v.movable_count} 个可以整理${v.skipped_count?'，'+v.skipped_count+' 个同名会跳过':''}${v.total>v.selected_count?'，'+(v.total-v.selected_count)+' 个类别没勾选':''}。`));const rows=el('div','rows'+(v.phase==='preview'?' classify-preview':'')),docs=v.filter==='文档资料';const header=el('div','hdr'+(docs?' docs':''));for(const title of ['', '文件',...(docs?['格式']:[]),'大小','整理到',v.phase==='preview'?'':'结果'])header.append(el('span','',title));rows.append(header);
 for(const [index,r]of v.rows.entries()){const row=el('div','row in'+(docs?' docs':'')+(['sk','warn'].includes(r.state)?' skip':''));row.dataset.row=r.id;row.style.animationDelay=Math.min(index,20)*35+'ms';const icon=el('span','ftype',typeLabel(r));icon.style.background=colors[r.category]||'#999';const name=classifyName(r);row.classList.add('classify-row');row.append(icon,name);if(docs)row.append(el('span','sz',r.format));const dest=el('span','to');if(r.subfolder){dest.append(document.createTextNode(r.category+'/'),el('b','',r.subfolder));}else{dest.textContent=r.to.replace(/^下载分类\//,'');}dest.tabIndex=0;showTooltip(dest,r.destination);row.append(el('span','sz',r.size),dest,el('span','st '+r.state,r.label));row.ondblclick=()=>details();rows.append(row);}content.append(rows);}
 $('undo').disabled=!v.can_undo;$('arch').disabled=!v.selected||v.busy;$('details').disabled=!v.selected;$('settings').disabled=!v.selected||v.busy;
 $('primary').classList.toggle('hidden',['empty','selected','undone'].includes(v.phase));$('primary').disabled=v.busy;$('primary').textContent=v.can_organize?`确认整理 ${v.movable_count} 个文件`:'重新生成预览';
}
async function refresh(){if(inFlight||window.__closing)return;inFlight=true;try{const v=await call('get_state'),key=JSON.stringify(v);if(key!==last){last=key;render(v);}if(panel==='remaining'){const k=JSON.stringify([v.remaining,v.busy]);if(k!==panelKey){panelKey=k;renderRemaining();}}if(panel==='folders'&&!v.busy){const k=JSON.stringify(v.subfolders);if(k!==panelKey){panelKey=k;renderFolders();}}}finally{inFlight=false;}}
const fieldNames={entries:'文件记录',directory_entries:'目录记录',result:'结果',attempt:'开始处理',outcome:'处理结果',reason:'原因',reason_code:'原因标记',source:'原位置',dest:'目标位置',destination:'目标位置',rel_path:'相对路径',name:'文件',category:'类别',size:'大小',format:'格式',basis:'分类依据',classification_basis:'分类依据',to:'整理到',state:'当前状态',label:'说明',undo_state:'还原结果',undo_status:'还原情况',execution:'执行记录',undo:'撤销记录',snapshot:'逐项记录',rows:'文件明细',new_files:'发现新文件',settling:'仍在变化',temp_skipped:'临时文件',scan_issues:'读取问题',partial_coverage:'未完整读取',error:'错误',count:'数量',names:'名称',items:'项目',types:'类型',detail:'说明',path:'位置',identity:'文件身份',size_bytes:'字节数',mtime_ns:'修改时间记录',dev:'所在磁盘',ino:'文件标记',mode:'类型记录',checked_at:'检查时间',total_seen:'看到的项目',total_new:'新增数量',total_settling:'仍在变化的数量',total_temp:'临时项目数量',version:'版本',directories:'目录归档',excluded:'未列入项目',issues:'读取问题'};
const values={moved:'已移动',skipped:'已跳过',failed:'未完成',restored:'已放回',pending:'待还原',attempting:'正在还原',skipped_not_moved:'未移动，无需还原',completed:'已完成',active:'可撤销',undone:'已撤销',needs_review:'需要检查',unknown_needs_review:'需要检查',archived:'已归档',ok:'已移动',sk:'已跳过',back:'已放回',blocked_source_exists:'原位置被占用'};
function describe(data,depth=0){
 if(data===null||data===undefined)return '无';
 if(typeof data==='boolean')return data?'是':'否';
 if(typeof data!=='object')return values[data]||String(data).replace(/plan-\d{8}-\d{6}-[a-zA-Z0-9]+/g,'本次记录');
 if(Array.isArray(data))return data.length?data.map((x,i)=>`${i+1}. ${describe(x,depth+1)}`).join('\n'):'无';
 return Object.entries(data).filter(([k])=>!['plan_id','plan_hash','binding','schema_version','op_id','id'].includes(k)).map(([k,v])=>`${'  '.repeat(depth)}${fieldNames[k]||(/^op-/.test(k)?'项目 '+k.slice(3):k)}：${describe(v,depth+1)}`).join('\n');
}
function record(body,title,data){const d=el('details','sheet-row'),s=el('summary','',title);d.append(s,el('pre','',describe(data)));body.append(d);}
// Information panels slide in from the right; confirmations keep the top sheet.
function drawer(title,sub=''){return sheet(title,'drawer',null,sub);}
const basename=p=>String(p||'').split('/').filter(Boolean).pop()||String(p||'');
const dirname=p=>{const parts=String(p||'').split('/');parts.pop();return parts.join('/');};
let viewFolder='';
const shortDir=rel=>`${viewFolder||state.folder}/${rel?rel+'/':''}`;
function chevron(){const svg=document.createElementNS('http://www.w3.org/2000/svg','svg');for(const [k,v]of Object.entries({viewBox:'0 0 14 14',fill:'none',stroke:'currentColor','stroke-width':1.6}))svg.setAttribute(k,v);svg.classList.add('chev');const p=document.createElementNS(svg.namespaceURI,'path');p.setAttribute('d','M5 3l4 4-4 4');svg.append(p);return svg;}
function badge(r){const t=el('span','ftype',r.category==='原本空目录'?'DIR':/^\.[^.]*$/.test(basename(r.name))?'·':typeLabel(r));t.style.background=colors[r.category]||'#8e8e93';return t;}
function shortPath(rel){const p=el('span','path');const i=rel.indexOf('下载分类');if(i<0){p.textContent=shortDir(rel);return p;}p.append(document.createTextNode(shortDir(rel.slice(0,i).replace(/\/$/,''))),el('b','',rel.slice(i)+'/'));return p;}
function group(body,kind,title,count,note){const g=el('div','grp '+kind),h=el('div','gh');h.append(el('span','gdot'),el('span','',title),el('span','gc',String(count)));g.append(h);if(note)g.append(el('div','gd',note));body.append(g);return g;}
function item(g,icon,name,sub,status,cls,full){const it=el('div','gi');it.title=full;const text=el('div');text.append(el('div','gn',name),el('div','gs',sub));it.append(icon,text,el('span','st '+cls,status));g.append(it);}
function listed(g,names,limit=200){for(const n of names.slice(0,limit))item(g,badge({name:n}),basename(n),shortDir(dirname(n)),'不处理','n',`位置：${shortDir(dirname(n))}${basename(n)}`);if(names.length>limit)g.append(el('div','gd more',`还有 ${names.length-limit} 个没列出`));}
const excludedNotes={'隐藏项':'以「.」开头的隐藏文件不参与整理。','未完成下载或临时项':'还在下载或临时的文件不参与整理，下载完再整理就行。','疑似敏感项':'名字像密钥、证书或凭证的文件只计数、不显示名字，也不会移动。','疑似敏感目录（整体跳过）':'这类文件夹整个跳过，不读取里面的内容。','符号链接':'替身和链接不移动，免得动到别处的文件。','文件夹':'文件夹本身不参与整理。','空文件夹（无文件，整体跳过）':'里面没有文件，整个跳过。','包目录（整体跳过）':'App 包这类目录整体跳过，不拆开。','代码项目目录（整体跳过）':'看起来是代码项目，整个跳过，保持原样。','其他特殊类型':'不是普通文件，不参与整理。','元数据读取失败':'读不到这些项目的信息，没有动它们。','递归读取失败/扫描变化':'扫描时读取失败或内容在变化，没有动它们。'};
function excludedGroups(body,ex){let n=0;for(const [label,v]of Object.entries(ex||{})){if(label==='跳过原因')continue;const note=excludedNotes[label]||'这些项目没有参与整理。';
 if(Array.isArray(v)){if(!v.length)continue;n+=v.length;listed(group(body,'neutral',label,v.length,note),v);}
 else if(v&&v.count){n+=v.count;const g=group(body,'neutral',label,v.count,note);if(Array.isArray(v.items))listed(g,v.items.map(x=>x.path));}}return n;}
async function details(skips=false){try{const d=await call('get_details');viewFolder=state.folder;if(skips)return skipPanel(d);
 const {body,acts}=drawer('本次整理详情',`${state.folder} · ${d.rows.length} 个项目`);const seg=el('div','seg'),views={};seg.setAttribute('role','tablist');body.before(seg);
 const tabs=[['files','文件明细'],['runs','执行与撤销'],['newf','新文件'],['dirs','目录归档']];
 for(const [k,t]of tabs){const b=el('button','',t);b.type='button';b.setAttribute('role','tab');b.onclick=()=>{for(const [kk,v]of Object.entries(views))v.classList.toggle('hidden',kk!==k);seg.querySelectorAll('button').forEach(x=>{x.classList.toggle('on',x===b);x.setAttribute('aria-selected',x===b);});};seg.append(b);views[k]=el('div','view');body.append(views[k]);}
 fileRows(views.files,d.rows);runsView(views.runs,d);newView(views.newf,d.discovery);dirsView(views.dirs,d.directories);seg.firstChild.click();
 const hint=el('span','dhint','点一行看完整位置和分类依据');acts.append(hint,button('关闭',closeSheet));}catch(e){message(e);}}
function fileRows(box,rows){if(!rows.length){box.append(el('p','sd','还没有预览结果。'));return;}
 for(const r of rows){const x=el('div','xrow'),h=el('button','xh'),moved=['ok'].includes(r.state),skipped=['sk','hint'].includes(r.state);h.type='button';h.setAttribute('aria-expanded','false');
  h.append(badge(r),classifyName(r,false),el('span','st '+(r.state||'n'),r.label||'会整理'),chevron());showTooltip(h,r.display_name&&r.display_name!==r.name?'原名：'+r.name:r.name);h.onclick=()=>{x.classList.toggle('ex');h.setAttribute('aria-expanded',x.classList.contains('ex'));};
  const kv=el('dl','kv'),add=(k,v,full)=>{const dd=el('dd');dd.append(typeof v==='string'?document.createTextNode(v):v);if(full)dd.title=full;kv.append(el('dt','',k),dd);};
  add('原位置',shortDir(dirname(r.name))+(moved?'':skipped||!r.state?'（没动）':''),r.source);
  add(moved?'现在位置':r.state==='back'?'放回到':skipped?'本来要去':'会放到',r.state==='back'?shortDir(dirname(r.name)):shortPath(r.to),r.state==='back'?r.source:r.destination);
  if(r.display_name&&r.display_name!==r.name)add('原名',r.name);for(const hint of r.hints||[])add('提示',hint.text);
  if(r.reason)add('原因',r.reason);const why=el('span','why',r.basis);add('为什么',why);if(r.category!=='原本空目录')add('大小',r.size);
  const b=el('div','xb'),inner=el('div');inner.append(kv);b.append(inner);x.append(h,b);box.append(x);}}
function runsView(box,d){const rows=d.rows,count=s=>rows.filter(r=>r.state===s).length,ok=count('ok'),sk=count('sk'),back=count('back'),warn=count('warn');const tl=el('div','tl');
 if(!ok&&!sk&&!back&&!warn)box.append(el('p','sd','还没有整理，现在只是预览，不会移动任何文件。'));
 else{const run=el('div','node'+(warn?' bad':'')),t=el('div','nt');t.append(document.createTextNode('整理 '),el('span','tag '+(warn?'warn':'ok'),warn?'需要检查':'完成'));run.append(t,el('div','nm2',`移动 ${ok+back} 个 · 跳过 ${sk} 个${warn?` · ${warn} 个需要检查`:''}`));tl.append(run);
  if(back){const u=el('div','node'),t2=el('div','nt');t2.append(document.createTextNode('撤销 '),el('span','tag n','已放回'));u.append(t2,el('div','nm2',`${back} 个文件放回原位置${sk?`，跳过的 ${sk} 个本来就没动`:''}`));tl.append(u);}box.append(tl);}
 const raw=el('div','raw');record(raw,'原始执行记录',d.execution);record(raw,'原始撤销记录',d.undo);record(raw,'逐项记录',d.snapshot);box.append(raw);}
function newView(box,r){if(!r){group(box,'neutral','新文件检查','未检查','在「设置」里可以立即检查。自动检查只提示，不会自己整理。');return;}
 const when=r.checked_at?new Date(r.checked_at*1000).toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit'}):'';
 const g=group(box,r.error||r.partial_coverage?'warn':'neutral','最近一次检查',when,r.error?`检查没完成：${r.error}`:r.partial_coverage?'有读取失败或扫描变化，结果不完整，建议重新检查。':r.total_new?`发现 ${r.total_new} 个新文件。自动检查只提示，不会自己整理。`:'没有发现新文件。自动检查只提示，不会自己整理。');
 listed(g,r.new_files||[]);if(r.total_settling)g.append(el('div','gd more',`还有 ${r.total_settling} 个仍在变化，稍后再看`));}
function dirsView(box,dirs){if(!state.settings.archive){group(box,'neutral','归档变空的文件夹','未开启','在「设置」里打开后，整理后变空的文件夹会一起归档，撤销时一并放回。');return;}
 const g=group(box,'neutral','归档变空的文件夹',dirs.length,dirs.length?'这些文件夹整理后变空，会一起归档；撤销时在条件允许时放回。':'这次没有要归档的空文件夹。');for(const o of dirs)item(g,badge({category:'原本空目录'}),basename(o.rel_path||o.source),shortDir(dirname(o.rel_path||'')),'归档','n',o.source);}
function skipPanel(d){const rows=d.rows.filter(r=>['hint','sk','warn'].includes(r.state)),done=state.phase==='done';
 const same=rows.filter(r=>r.state!=='warn'&&/同名/.test(r.label)),other=rows.filter(r=>r.state==='sk'&&!/同名/.test(r.label)),bad=rows.filter(r=>r.state==='warn');
 const {body,acts}=drawer(done?'跳过的项目':'会跳过的项目','');
 if(same.length){const g=group(body,'warn','同名文件已存在',same.length,`目标文件夹里已经有同名文件。为了不覆盖，这些文件${done?'没有移动':'不会移动'}。`);for(const r of same)item(g,badge(r),basename(r.name),`本来要放到 ${state.folder}/${r.to}/`,'没有覆盖','sk',`原位置：${r.source}`);}
 if(other.length){const g=group(body,'warn','其他原因',other.length,'这些文件没有移动，原因写在右边。');for(const r of other)item(g,badge(r),basename(r.name),shortDir(dirname(r.name)),r.reason||'已跳过','sk',`原位置：${r.source}`);}
 if(bad.length){const g=group(body,'warn','需要检查',bad.length,'这些项目没有按预期完成，请在「操作记录」里检查。');for(const r of bad)item(g,badge(r),basename(r.name),shortDir(dirname(r.name)),r.label,'warn',`原位置：${r.source}`);}
 const n=same.length+other.length+bad.length+excludedGroups(body,d.excluded);
 body.parentNode.querySelector('.dc').textContent=n?`${n} 个 · 都留在原处，没有改动`:'没有跳过的项目';if(!n)body.append(el('p','sd','这次没有跳过任何项目。'));
 acts.append(el('span','dhint',same.length?'想让同名文件也整理？先在访达里改名或删掉旧的，再重新生成预览。':'这些项目留在原处，不会被移动。'),button('关闭',closeSheet));}
function stepNeedsCheck(s){return s.status==='needs_review'||s.uncertain||(s.failed>0&&!s.checked);}
async function history(){try{const h=await call('get_history');viewFolder=state.folder;const {body,acts}=drawer('操作记录','');const seg=el('div','seg');seg.setAttribute('role','tablist');body.before(seg);
 body.append(el('div','hsec','这次打开'));const tl=el('div','tl');body.append(tl);
 const small=(text,fn,disabled=false)=>{const b=button(text,fn);b.classList.add('sm');b.disabled=disabled||state.busy;return b;};
 for(const s of [...h.steps].reverse()){const cur=s.version===h.head,check=stepNeedsCheck(s),node=el('div','node'+(cur?' cur':'')+(check?' bad':'')),t=el('div','nt');
  t.append(document.createTextNode(`版本 ${s.version} · 整理 ${basename(s.source)}`));const tag=check?['warn','需要检查']:cur?['cur','当前']:s.status==='undone'?['n','已撤销']:['ok',values[s.status]||s.status];t.append(el('span','tag '+tag[0],tag[1]));
  const line=[`移动 ${s.moved} 个`,`跳过 ${s.skipped} 个`];if(s.archived_dirs)line.push(`归档 ${s.archived_dirs} 个文件夹`);if(s.failed)line.push(`${s.failed} 个未完成`);if(s.at)line.unshift(when(s.at));node.append(t,el('div','nm2',line.join(' · ')));if(s.detail)node.append(el('div','nm2',s.detail));
  const a=el('div','nacts');if(s.active&&!cur)a.append(small('退回到这里',()=>confirmUndo(s.version,s.token)));a.append(small('查看明细',()=>stepDetail(s.version,basename(s.source))));if(check)a.append(small('恢复和检查',async()=>{closeSheet();await action('recover_check',s.token,s.version);}));node.append(a);tl.append(node);}
 if(h.steps.length){const origin=el('div','node origin');origin.append(el('div','nt','本次打开前'),el('div','nm2','这次打开时的样子'));const a=el('div','nacts');a.append(small('退回到这里',()=>confirmUndo(0,h.root_token),!h.head));origin.append(a);tl.append(origin);}else tl.replaceWith(el('p','hempty','这次打开还没有整理。整理后会出现在这里，并且可以退回。'));
 pastList(body,seg,h.past);
 acts.append(el('span','dhint',h.past.length?'以前的记录只能查看。最近一次整理可以用「撤销上一次」。':'退回前会再确认一次。'));if(state.selected){const l=el('button','lnk','记录看起来不对？检查一下');l.type='button';l.onclick=()=>{closeSheet();action('recover_check',h.current_token);};acts.append(l);}}catch(e){message(e);}}
// Earlier launches are read from the saved records; they are view-only.
const DAY=864e5,pastTags={kept:['ok','保留中'],undone:['n','已撤销'],partial:['warn','有未完成项'],check:['warn','需要检查']};
const dayStart=t=>{const d=new Date(t);d.setHours(0,0,0,0);return d.getTime();};
function when(at){const t=at*1000,d=new Date(t),hm=d.toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit',hour12:false}),diff=Math.round((dayStart(Date.now())-dayStart(t))/DAY);return diff===0?`今天 ${hm}`:diff===1?`昨天 ${hm}`:`${d.getMonth()+1}月${d.getDate()}日 ${hm}`;}
function dayLabel(t){const d=new Date(t),diff=Math.round((dayStart(Date.now())-dayStart(t))/DAY),md=`${d.getMonth()+1}月${d.getDate()}日`;return diff===0?'今天':diff===1?`昨天 · ${md}`:`${md} ${'日一二三四五六'.replace(/./g,c=>'周'+c).match(/../g)[d.getDay()]}`;}
let pastScope='here',pastAll=false;
function pastList(body,seg,past){const box=el('div');body.append(el('div','hsec','更早 · 只能查看'),box);const sub=body.parentNode.querySelector('.dc'),here=past.some(p=>p.here);if(!here)pastScope='all';
 const draw=()=>{box.replaceChildren();const cut=Date.now()-30*DAY,pick=past.filter(p=>pastScope==='all'||p.here),shown=pickShown(pick,cut);
  let n=0,group=null,key='';for(const p of shown){const t=(p.at||0)*1000,old=t<cut,k=old?`${new Date(t).getFullYear()}-${new Date(t).getMonth()}`:String(dayStart(t));
   if(k!==key){key=k;group=el('div','day');group.append(el('div','dl',p.at?old?`${new Date(t).getFullYear()}年${new Date(t).getMonth()+1}月`:dayLabel(t):'时间未知'));box.append(group);}
   const b=el('button','rec'+(old?' wide':''));b.type='button';const w=el('div','w'),tag=pastTags[p.status]||pastTags.check,line=[p.kind==='archive'?`归档 ${p.moved+p.archived} 项`:`移动 ${p.moved} 个`];
   if(p.status==='undone')line.push('已全部放回');else{line.push(`跳过 ${p.skipped} 个`);if(p.failed)line.push(`${p.failed} 个未完成`);}
   w.append(el('div','w1',`${p.kind==='archive'?'归档剩余项目':'整理'} ${p.source}`),el('div','w2',line.join(' · ')));
   const d=p.at?new Date(t):null;b.append(el('span','tm',d?old?`${d.getMonth()+1}月${d.getDate()}日 ${d.toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit',hour12:false})}`:d.toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit',hour12:false}):'—'),w,el('span','tag '+tag[0],tag[1]),chevron());b.onclick=()=>pastDetail(p.ref);group.append(b);n++;}
  if(!pick.length)box.append(el('p','hempty',pastScope==='here'?'这个文件夹以前没有整理记录。':'以前没有整理记录。'));
  else if(!n)box.append(el('p','hempty','最近 30 天没有整理记录。'));
  const older=pick.length-n;if(older>0){const m=el('button','hmore',`显示更早的记录（30 天以前，${older} 次）`);m.type='button';m.onclick=()=>{pastAll=true;draw();};box.append(m);}
  sub.textContent=`${pastScope==='here'?state.folder:'所有文件夹'} · ${pastAll?'全部记录':'最近 30 天'} · ${n} 次`;};
 const pickShown=(pick,cut)=>pastAll?pick:pick.filter(p=>(p.at||0)*1000>=cut);
 for(const [k,t]of [['here','这个文件夹'],['all','所有文件夹']]){const b=el('button','',t);b.type='button';b.setAttribute('role','tab');const on=()=>{seg.querySelectorAll('button').forEach(x=>{x.classList.toggle('on',x===b);x.setAttribute('aria-selected',x===b);});};b.onclick=()=>{pastScope=k;on();draw();};seg.append(b);if(k===pastScope)on();if(k==='here'&&!here)b.disabled=true;}
 draw();}
async function pastDetail(ref){try{const d=await call('get_history',ref);viewFolder=d.source;const {body,acts}=drawer(`${d.at?when(d.at)+' ':'以前'}的整理`,`${d.source} · ${d.rows.length} 个项目`);
 const note=el('div','hnote');note.append(el('b','','这是以前的记录，只能查看。'),document.createTextNode('「现在位置」是当时整理到的地方，之后你可能已经移动、改名或删除了这些文件。'));body.append(note);fileRows(body,d.rows);
 const raw=el('div','raw');record(raw,'原始执行记录',d.execution);record(raw,'原始撤销记录',d.undo);body.append(raw);acts.append(el('span','dhint','点一行看完整位置和分类依据'),button('返回',history));}catch(e){message(e);}}
async function stepDetail(version,folder){try{const d=await call('get_history',version);viewFolder=folder;const {body,acts}=drawer(`版本 ${version} 的明细`,`${d.rows.length} 个项目`);fileRows(body,d.rows);const raw=el('div','raw');record(raw,'原始执行记录',d.execution);record(raw,'原始撤销记录',d.undo);body.append(raw);acts.append(el('span','dhint','点一行看完整位置和分类依据'),button('返回',history));}catch(e){message(e);}}
function settings(){const {body,acts}=sheet('设置');let archive=state.settings.archive,auto=state.settings.auto_check;const a=el('div','sheet-row'),b=el('div','sheet-row');a.append(checkbox('归档本次整理后变空的文件夹',archive,x=>archive=x),el('p','sd','更改后需重新预览；只处理本次明确选择范围内的文件夹。'));b.append(checkbox('自动检查新文件（仅提示）',auto,x=>auto=x),el('p','sd','发现新文件只提示，不会自动整理。'));body.append(a,b,button('立即检查',()=>{closeSheet();action('check_new_files');}));acts.append(button('取消',closeSheet),button('保存',()=>{closeSheet();action('settings_set',archive,auto);},true));}
async function folders(){panelKey='';sheet('读取子文件夹…',true,'folders');await action('pick_subfolders');}
function renderFolders(){const {body,acts}=sheet('选择子文件夹',true,'folders');const selected=new Set(state.subfolders.selected);body.append(el('p','sd','顶层文件始终包含；只进入明确勾选的子文件夹。'));for(const f of state.subfolders.candidates){const row=el('div','sheet-row');row.append(checkbox(f.rel_path,selected.has(f.rel_path),on=>on?selected.add(f.rel_path):selected.delete(f.rel_path)));body.append(row);}if(!state.subfolders.candidates.length)body.append(el('p','sd','没有可选的子文件夹。'));acts.append(button('取消',closeSheet),button('应用并预览',()=>{closeSheet();action('set_subfolders',true,[...selected]);},true));}
async function remaining(){panelKey='';sheet('正在发现剩余项目…',true,'remaining');await action('remaining_discover');}
function renderRemaining(){const d=state.remaining,{body,acts}=sheet('归档剩余项目',true,'remaining');if(!d){body.append(el('p','sd',state.busy?'正在读取，只做预览…':'没有读取到候选，请重新发现。'));acts.append(button('关闭',closeSheet));return;}
 body.append(el('p','sd',d.status));for(const c of d.candidates){const row=el('div','sheet-row'),label=checkbox(c.rel_path,d.selected.includes(c.rel_path),on=>action('remaining_select',on?[...d.selected,c.rel_path]:d.selected.filter(x=>x!==c.rel_path)));label.querySelector('input').disabled=state.busy;row.append(label);body.append(row);}record(body,'未列入的项目与读取问题',{excluded:d.excluded,issues:d.issues});for(const r of d.rows)body.append(el('p','sd',`${r.name} → ${r.destination}`));
 const next=button(d.plan_hash?'确认归档…':'生成归档预览',()=>{if(!d.plan_hash){action('remaining_preview');return;}const hash=d.plan_hash;const p=sheet('确认归档所选项目？');p.body.append(el('p','sd','只归档已勾选且身份没有变化的项目。同名不覆盖，之后可按记录撤销。'));p.acts.append(button('返回',()=>{panelKey='';renderRemaining();}),button('确认归档',()=>{panel=null;closeSheet();action('remaining_organize',hash,true);},true));},true);next.disabled=state.busy||!d.selected.length||!!d.issues.length;acts.append(button('取消',closeSheet),next);
}
$('chg').onclick=()=>action('pick_folder');$('primary').onclick=primary;$('undo').onclick=()=>confirmUndo();$('arch').onclick=remaining;$('details').onclick=()=>details();$('skipline').onclick=()=>details(true);$('history').onclick=()=>{pastScope='here';pastAll=false;history();};$('settings').onclick=settings;$('folders').onclick=folders;$('recursive').onchange=e=>e.target.checked?folders():action('set_subfolders',false,[]);
// No links, navigation, file drops, forms or popups are part of this application.
document.addEventListener('click',e=>{if(e.target.closest('a'))e.preventDefault();});document.addEventListener('drop',e=>e.preventDefault());document.addEventListener('dragover',e=>e.preventDefault());
document.addEventListener('keydown',e=>{if(e.key==='Escape'&&panel!=='remaining')closeSheet();if(e.key==='Tab'&&$('veil').classList.contains('show')){const a=[...$('sheet').querySelectorAll('button:not(:disabled),input:not(:disabled),summary')];if(a.length&&(e.shiftKey&&document.activeElement===a[0]||!e.shiftKey&&document.activeElement===a[a.length-1])){e.preventDefault();(e.shiftKey?a[a.length-1]:a[0]).focus();}}});
window.addEventListener('pywebviewready',()=>{refresh().catch(message);pollTimer=setInterval(()=>refresh().catch(message),160);});
