import type { ConversationResult } from './contracts.js'

const labels: Record<string,string> = {
  status:'状态',state:'状态',action:'操作',resource:'资源',alias:'资源别名',name:'名称',title:'标题',baseName:'多维表格',kind:'类型',
  id:'编号',record_id:'记录',tableId:'数据表编号',fields:'字段',items:'条目',records:'记录',tables:'数据表',field_name:'字段名称',field_id:'字段编号',type:'类型',
  text:'内容',summary:'摘要',message:'说明',prompt:'要求',cwd:'工作目录',path:'路径',filename:'文件名',files:'文件',source:'来源',size:'字节数',sha256:'文件校验值',
  receipt:'操作回执',result:'结果',verified:'核验结果',preview:'操作预览',baseline:'原值',params:'变更内容',payload:'变更范围',mutations:'任务变更',input:'任务设置',
  token:'确认令牌',confirm:'确认命令',cancel:'取消命令',expiresAt:'有效期至',expires:'有效期至',expiresInSeconds:'有效秒数',
  completed:'已完成项目',cancelled:'已取消',deleted:'已删除',replayed:'重复执行',complete:'结果完整',warnings:'提示',instruction:'说明',
  mode:'执行模式',executor:'执行器',rule:'时间规则',first:'首次时间',next:'下次时间',scheduledAt:'计划时间',planned:'计划时间',timeZone:'时区',
  interval:'间隔',weekdays:'星期（周一为 0）',catchUp:'补跑策略',grant:'授权任务',scope:'授权范围',version:'版本',authorized:'已授权自动执行',autoExecution:'自动执行授权',
  remote:'远端',branch:'分支',mutation:'文件变更',operation:'操作',content:'内容',artifacts:'产物',threadId:'Codex 会话',thread:'Codex 会话',data:'条目',turns:'会话记录',
  deliveries:'投递',outbox:'出站消息',scheduleState:'提醒状态',occurrence:'本次时间',outcomes:'执行结果',deliveryId:'投递编号',
  known_resources_only:'仅已登记资源',capabilities:'允许操作',page:'页码',total:'总数',nextCursor:'后续页游标',nextPage:'后续页',
  permissions:'权限',provider:'模型服务',model:'模型',active:'正在执行',globalSettingsMutableFromChannel:'允许从聊天修改全局设置',
}
const values: Record<string,string> = {
  confirmation_required:'等待本人确认，尚未执行',completed:'已完成',failed:'执行失败',cancelled:'已取消',unknown:'结果待核实，未自动重试',
  pending:'待处理',running:'执行中',scheduled:'已安排',active:'已启用',queued:'已入队',awaiting_authorization:'等待自动执行授权',awaiting_auth:'等待本次执行确认',
  expired:'已过期',superseded:'已被新预览替代',paused:'已暂停',deleted:'已删除',sent:'已发送',unavailable:'暂不可用',
  create:'新增',update:'修改',delete:'删除',inspect:'查看结构',query:'查询',publish:'原文发布',git:'提交并推送指定文件',files:'文件变更',
  remind:'仅提醒',auto:'自动执行',confirm:'逐次确认',once:'单次',daily:'每天',weekly:'每周',research:'研究任务',codex_apply:'Codex 写入任务',
  bitable:'多维表格',documents:'文档',calendar:'日历',tasks:'任务',
}
const privateKeys = new Set(['binding','actor','digest','frozenResource','nativeRequestDigest','tokenRef','policyVersion'])
const literalKeys = new Set(['id','record_id','token','alias','path','source','filename','tableId','threadId','sha256','field_id','deliveryId'])
function decode(value:unknown):unknown {
  if(typeof value!=='string'||!/^\s*[\[{]/.test(value))return value
  try{return JSON.parse(value)}catch{return value}
}
function scalar(value:unknown,key=''):string {
  if(value==null)return '（空）'
  if(typeof value==='boolean')return value?'是':'否'
  if(typeof value==='number'&&value>1e12&&/(日期|时间|date|At$|expires|planned)/i.test(key)){
    return new Intl.DateTimeFormat('sv-SE',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false}).format(value)+'（Asia/Shanghai）'
  }
  if(Array.isArray(value))return value.map(item=>scalar(item,key)).join('、')||'（空）'
  if(typeof value==='object')return Object.entries(value).filter(([k])=>!privateKeys.has(k)).map(([k,v])=>`${labels[k]??k}：${scalar(v,k)}`).join('；')||'（空）'
  const text=String(value)
  return literalKeys.has(key)?text:values[text]??text
}
function details(value:unknown,depth=0):string {
  if(depth>12)return '内容层级较深，请缩小查询范围后查看。'
  value=decode(value)
  if(Array.isArray(value))return value.length?value.map((row,i)=>`${i+1}. ${details(row,depth+1).replaceAll('\n','\n   ')}`).join('\n'):'暂无条目。'
  if(!value||typeof value!=='object')return scalar(value)
  const row=value as Record<string,unknown>
  if(typeof row.json==='string')return renderPluginResult(row.json,depth+1)
  // Services persist structured receipts for replay. Unwrap them at the display boundary only.
  if(typeof row.text==='string'&&row.status)return [row.status==='completed'?'':values[String(row.status)]??scalar(row.status),renderPluginResult(row.text,depth+1),row.artifacts?details({artifacts:row.artifacts},depth+1):''].filter(Boolean).join('\n\n')
  return Object.entries(row).filter(([key,value])=>!privateKeys.has(key)&&value!==undefined).map(([key,value])=>{
    const decoded=['result','payload','input','operation','plan','request','receipt'].includes(key)?decode(value):value
    const title=labels[key]??key
    return decoded&&typeof decoded==='object'?`**${title}**\n${details(decoded,depth+1)}`:`${title}：${scalar(decoded,key)}`
  }).join('\n')||'暂无详情。'
}
function preview(row:Record<string,any>):string {
  const operation=row.preview,lines=['**请确认以下操作**','尚未执行，确认后才会写入。']
  if(operation){
    lines.push(`操作：${scalar(operation.action)}\n资源：${operation.resource}`)
    const records=operation.params?.records
    if(Array.isArray(records)){
      lines.push(`涉及 ${records.length} 条记录：`)
      for(const record of records){
        const old=row.baseline?.find((item:any)=>item.id===record.record_id)?.fields??{}
        const fields=operation.action==='delete'?old:record.fields??{}
        lines.push(`- ${record.record_id??'新记录'}${operation.action==='delete'?'（将删除）':''}`)
        for(const [key,value]of Object.entries(fields))lines.push(`  - ${key}：${operation.action==='create'?scalar(value,key):operation.action==='delete'?scalar(value,key):`${scalar(old[key],key)} → ${scalar(value,key)}`}`)
      }
    }else lines.push(details(operation.params))
  }else lines.push(details(row.payload??Object.fromEntries(Object.entries(row).filter(([key])=>!['status','token','confirm','expiresAt'].includes(key)))))
  const seconds=row.expiresAt?Math.max(0,Math.floor((Number(row.expiresAt)-Date.now())/1000)):Number(row.expiresInSeconds??900)
  lines.push(`有效期：${Math.ceil(seconds/60)} 分钟，过期后需重新生成预览。`)
  lines.push('可自然语言确认或取消，例如「好的，请修改吧」「可以，按预览执行」或「先不改了」。也可回复「确认」或「取消」。有多项待确认时，请明确指出哪一项。')
  if(row.token){
    const command=row.confirm??(operation?'/bitable confirm '+row.token:undefined)
    lines.push(command?`也可使用：\`${command}\`；取消：\`${command.replace(/ confirm /,' cancel ')}\``:`确认令牌：${row.token}`)
  }
  return lines.join('\n\n')
}

/** User-facing plugin output only. Machine receipts, authorization and storage stay structured. */
export function renderPluginResult(input:unknown,depth=0):string {
  if(depth>12)return '内容层级较深，请缩小查询范围后查看。'
  const value=decode(input)
  if(value&&typeof value==='object'&&!Array.isArray(value)){
    const row=value as Record<string,any>
    if(row.status==='confirmation_required')return preview(row)
    if(row.status==='completed'&&row.receipt&&row.verified){
      const records=row.receipt.records
      const summary=Array.isArray(records)?`已完成操作，涉及 ${records.length} 条记录，并已核验。`:'已完成操作，并已核验。'
      return `**${summary}**\n\n${details(row.receipt,depth+1)}`
    }
    if(row.status==='unknown')return '**结果待核实，没有自动重试。**\n请先检查操作状态，避免重复写入。'+(row.receipt?'\n\n已收到回执，但尚未完成核验。':'')
  }
  return details(value,depth)
}
export function presentResult<T extends ConversationResult>(result:T):T {
  return {...result,text:renderPluginResult(result.text)}
}
