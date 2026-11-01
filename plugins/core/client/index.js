/* Official Harness client module. React and slots are supplied by the host. */
window.__ModuleLoader__.load({id:'@telebot/core',factory:require=>{
  const React=require('react'),h=React.createElement,{useState,useEffect,useRef}=React;
  const endpoint='/api/telebot/configuration';
  const messages={configuration_busy:'当前有任务执行，请等待完成或停止任务后保存。',configuration_revision_conflict:'配置已被更新，请重新读取后再保存。',configuration_plugin_not_enabled:'插件未启用，请先在 Harness 插件页启用。',existing_absolute_workspace_required:'工作目录必须是本机已存在的绝对目录。',valid_app_and_administrators_required:'请填写有效的 App ID 和管理员 ID。',application_secret_and_source_authentication_required:'请填写 App Secret，以及 Verification Token 或 Encrypt Key。',matching_https_public_callback_required:'公网地址需使用 HTTPS，路径需与 webhook 路径一致。',runtime_configuration_rejected:'启用失败，已保留原配置。请检查目录、资源和依赖。',configuration_request_rejected:'配置请求失败，请检查填写内容。'};
  const label={appId:'App ID',adminUserIds:'管理员 ID',workspace:'工作目录',appSecret:'App Secret',verificationToken_or_encryptKey:'Verification Token 或 Encrypt Key',config_file_invalid:'配置文件格式'};
  const order=['telebot-feishu','telebot-desktop','telebot-codex','telebot-publishing','telebot-automation','telebot-adviser','telebot-core'];
  async function api(plugin,action,body,signal){
    const response=await fetch(endpoint,{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json'},body:JSON.stringify(plugin?{plugin,action,body}:{action:'list'}),signal});
    const data=await response.json();if(!response.ok)throw new Error(messages[data.code]||data.code||'读取失败');return data;
  }
  const styles={root:{display:'grid',gap:20,maxWidth:1000,width:'100%',padding:'8px 0',color:'inherit'},tabs:{display:'flex',flexWrap:'wrap',gap:8},button:{font:'inherit',padding:'8px 14px',border:'1px solid color-mix(in srgb, currentColor 22%, transparent)',borderRadius:8,background:'transparent',color:'inherit',cursor:'pointer'},section:{border:'1px solid color-mix(in srgb, currentColor 16%, transparent)',borderRadius:12,padding:20,display:'grid',gap:16},field:{display:'grid',gap:7,fontSize:13},input:{boxSizing:'border-box',width:'100%',font:'inherit',border:'1px solid color-mix(in srgb, currentColor 25%, transparent)',borderRadius:7,padding:'9px 10px',background:'transparent',color:'inherit'},hint:{opacity:.7,fontSize:12,lineHeight:1.6},notice:{padding:12,borderRadius:8,background:'color-mix(in srgb, currentColor 6%, transparent)',whiteSpace:'pre-wrap'},row:{display:'flex',gap:10,alignItems:'center',flexWrap:'wrap'}};
  const read=(object,key)=>key.split('.').reduce((value,part)=>value?.[part],object);
  function write(object,key,value){const parts=key.split('.'),last=parts.pop();let target=object;for(const part of parts)target=target[part]??=( {} );target[last]=value;}
  function format(field,value){return field.secret?'':field.type==='list'?(value||[]).join('\n'):field.type==='json'?JSON.stringify(typeof value==='string'?JSON.parse(value):value??[],null,2):value??'';}
  function parse(field,value){return field.type==='list'?value.split(/[,;\n]+/).map(x=>x.trim()).filter(Boolean):field.type==='json'?JSON.stringify(JSON.parse(value)):field.type==='number'?Number(value):field.type==='boolean'?Boolean(value):value.trim();}
  function ConfigurationSection(){
    const [pages,setPages]=useState([]),[selected,setSelected]=useState(''),[snapshot,setSnapshot]=useState(null),[values,setValues]=useState({}),[notice,setNotice]=useState('正在读取插件配置…'),[error,setError]=useState(false),[busy,setBusy]=useState(false),[dirty,setDirty]=useState(false),[baseLinks,setBaseLinks]=useState([]),[baseResources,setBaseResources]=useState([]),[baseUrl,setBaseUrl]=useState('');
    const generation=useRef(0),mounted=useRef(true);
    useEffect(()=>{mounted.current=true;return()=>{mounted.current=false;generation.current++}},[]);
    async function list(){const result=await api();if(!mounted.current)return [];const sorted=result.pages.sort((a,b)=>order.indexOf(a.name)-order.indexOf(b.name));setPages(sorted);return sorted;}
    async function load(name){const current=++generation.current;setBusy(true);setSnapshot(null);setError(false);setNotice('正在读取…');
      try{const result=await api(name,'get');if(!mounted.current||current!==generation.current)return;
        const links=name==='telebot-feishu'?JSON.parse(result.config.business.baseLinksJson||'[]'):[],resources=name==='telebot-feishu'?JSON.parse(result.config.business.resourcesJson||'[]'):[];
        const displayed=structuredClone(result.config);if(name==='telebot-feishu')displayed.business.resourcesJson=JSON.stringify(resources.filter(x=>x.kind!=='bitable'));
        setSnapshot(result);setValues(Object.fromEntries(result.fields.map(field=>[field.key,format(field,read(displayed,field.key))])));setBaseLinks(links);setBaseResources(resources.filter(x=>x.kind==='bitable'));setBaseUrl('');setDirty(false);
        setNotice(result.missing.length?'请补充：'+result.missing.map(x=>label[x]||x).join('、'):result.active?'配置已生效。':'请检查配置后保存。');
      }catch(e){if(mounted.current&&current===generation.current){setNotice(e.message);setError(true)}}finally{if(mounted.current&&current===generation.current)setBusy(false)}
    }
    useEffect(()=>{let active=true;list().then(items=>{if(active&&items.length){setSelected(items[0].name);load(items[0].name)}else if(active)setNotice('没有已启用的插件，请先在 Harness 插件页启用。')}).catch(e=>{if(active){setNotice(e.message);setError(true)}});return()=>{active=false}},[]);
    async function select(name){if(busy||name===selected)return;if(dirty){setNotice('有未保存的修改，请先保存或重新读取。');return}setSelected(name);await load(name)}
    function edit(key,value){setValues(previous=>({...previous,[key]:value}));setDirty(true)}
    async function save(event){event.preventDefault();if(!snapshot||busy)return;setBusy(true);setError(false);
      try{const config=structuredClone(snapshot.config),secrets={};for(const field of snapshot.fields){if(field.secret)secrets[field.key]=values[field.key];else write(config,field.key,parse(field,values[field.key]))}
        if(selected==='telebot-feishu'){config.business.resourcesJson=JSON.stringify([...baseResources,...JSON.parse(config.business.resourcesJson)]);config.business.baseLinksJson=JSON.stringify(baseLinks);config.business.confirmAll=true}
        await api(selected,'save',{revision:snapshot.revision,config,secrets});await load(selected);await list();if(mounted.current){setNotice('已保存。'+(selected==='telebot-feishu'?'部署模式或端口变更需重启 Harness 生效。':''));setError(false)}
      }catch(e){if(mounted.current){setNotice(e.message);setError(true)}}finally{if(mounted.current)setBusy(false)}
    }
    async function discover(){if(!snapshot||busy)return;setBusy(true);setError(false);try{
      const inventory=await api(selected,'save',{operation:'discover-bitable',revision:snapshot.revision,baseUrl:baseUrl.trim()});
      if(!mounted.current)return;const url=new URL(baseUrl.trim());url.search='';url.hash='';setBaseLinks(previous=>[...previous.filter(x=>x.baseId!==inventory.baseId),{baseId:inventory.baseId,url:url.href}]);
      setBaseResources(previous=>{const next=[...previous];for(const table of inventory.tables)if(!next.some(x=>x.id===inventory.baseId&&x.tableId===table.table_id))next.push({alias:'base-'+table.table_id,kind:'bitable',id:inventory.baseId,tableId:table.table_id,identity:'tenant',tokenRef:'FEISHU_APP_TENANT',expiresAt:0,capabilities:['bitable.read','bitable.create','bitable.update','bitable.delete']});return next});setBaseUrl('');setDirty(true);setNotice('链接及数据表已加入表单，点击保存后生效。');
    }catch(e){if(mounted.current){setNotice(e.message);setError(true)}}finally{if(mounted.current)setBusy(false)}}
    const groups=snapshot?[...new Set(snapshot.fields.map(field=>field.section||'正式配置'))]:[];
    function fieldControl(field){const id='telebot-'+field.key,checked=field.type==='boolean';return h('label',{key:field.key,htmlFor:id,style:styles.field},field.label,
      field.type==='list'||field.type==='json'?h('textarea',{id,rows:field.type==='json'?7:3,value:values[field.key],disabled:busy,onChange:e=>edit(field.key,e.target.value),style:styles.input,spellCheck:false}):h('input',{id,type:checked?'checkbox':field.type==='password'?'password':field.type==='number'?'number':'text',...(checked?{checked:values[field.key]}:{value:values[field.key]}),min:field.min,max:field.max,disabled:busy,onChange:e=>edit(field.key,checked?e.target.checked:e.target.value),style:checked?{justifySelf:'start'}:styles.input,autoComplete:field.secret?'new-password':'off'}),
      field.secret?h('small',{style:styles.hint},snapshot.credentials?.[field.key]?'已配置，留空保留。':'尚未配置。'):null,field.help?h('small',{style:styles.hint},field.help):null)}
    const ids=[...new Set([...baseLinks.map(x=>x.baseId),...baseResources.map(x=>x.id)])];
    return h('section',{'aria-label':'Telebot 插件配置',style:styles.root},
      h('div',null,h('h2',{style:{margin:'0 0 8px'}},'Telebot 插件'),h('p',{style:styles.hint},'配置保存在各插件独立目录。停用的插件请先在“插件”页启用；知识库使用其自身页面。')),
      h('nav',{'aria-label':'选择配置插件',style:styles.tabs},...pages.map(page=>h('button',{key:page.name,type:'button',disabled:busy,onClick:()=>select(page.name),'aria-pressed':selected===page.name,style:{...styles.button,fontWeight:selected===page.name?700:400,background:selected===page.name?'color-mix(in srgb, currentColor 10%, transparent)':'transparent'}},page.title,(page.missing?.length?' · 待配置':'')))),
      h('div',{role:error?'alert':'status','aria-live':'polite',style:styles.notice},notice),
      snapshot?h('form',{onSubmit:save,'aria-label':snapshot.title},
        h('h3',null,snapshot.title),snapshot.deployment?h('p',{style:styles.hint},'监听端口：'+snapshot.deployment.effectivePort+'；配置端口：'+snapshot.config.deploymentPort+(snapshot.deployment.restartRequired?'（需重启）':'')):null,
        ...groups.map(group=>h('section',{key:group,style:{...styles.section,marginBottom:16}},h('h4',{style:{margin:0}},group),...snapshot.fields.filter(field=>(field.section||'正式配置')===group).map(fieldControl))),
        selected==='telebot-feishu'?h('section',{'aria-label':'多维表格链接',style:{...styles.section,marginBottom:16}},h('h4',{style:{margin:0}},'多维表格链接'),h('p',{style:styles.hint},'独立管理接入链接。移除仅解除接入，不删除飞书数据；修改后保存生效。'),
          ...ids.map(id=>h('div',{key:id,style:styles.section},h('span',{style:{overflowWrap:'anywhere'}},baseLinks.find(x=>x.baseId===id)?.url||id),h('small',{style:styles.hint},'已选择 '+baseResources.filter(x=>x.id===id).length+' 个数据表'),h('button',{type:'button',disabled:busy,style:styles.button,onClick:()=>{setBaseLinks(previous=>previous.filter(x=>x.baseId!==id));setBaseResources(previous=>previous.filter(x=>x.id!==id));setDirty(true);setNotice('已从表单移除，保存后生效。')}},'移除此链接'))),
          !ids.length?h('p',{style:styles.hint},'尚未接入多维表格。'):null,h('label',{style:styles.field},'添加多维表格链接',h('input',{type:'url',value:baseUrl,disabled:busy,onChange:e=>setBaseUrl(e.target.value),style:styles.input,placeholder:'https://…feishu.cn/base/…'})),h('button',{type:'button',disabled:busy||!baseUrl.trim(),onClick:discover,style:styles.button},'添加链接并读取数据表'),h('small',{style:styles.hint},'先保存应用凭据；仅查询此链接的数据表。')):null,
        Object.keys(snapshot.diagnostics||{}).length?h('section',{style:styles.section},...Object.entries(snapshot.diagnostics).map(([key,value])=>h('p',{key},key+'：'+(value===true?'可用':value===false?'未提供':value)))):null,
        h('div',{style:styles.row},snapshot.fields.length?h('button',{type:'submit',disabled:busy,style:{...styles.button,fontWeight:700}},busy?'处理中…':'保存并应用'):null,h('button',{type:'button',disabled:busy,onClick:()=>load(selected),style:styles.button},'重新读取'),dirty?h('span',{style:styles.hint},'有未保存的修改'):null),h('p',{style:{...styles.hint,overflowWrap:'anywhere'}},snapshot.directory+'/config.json')):null);
  }
  return {inject:['slots'],apply(ctx){const slots=ctx.get('slots');if(!slots)return;slots.inject('settings.section',()=>slots.register({name:'settings.section',id:'telebot',order:65,label:()=> 'Telebot 插件'},ConfigurationSection));}};
}});
