import { validateTask, type TaskInput } from './rules.js'

/** Translate only task identifiers for identity-bound proposal preparation, never grant execution. */
export function scheduleToolInput(action:string,input:string):string{
  if(!['authorize','delete'].includes(action))return input
  const value=JSON.parse(input),id=typeof value==='string'?value:value?.task_id??value?.id
  if(typeof id!=='string'||!/^[A-Za-z0-9._:-]{1,200}$/.test(id)||(typeof value==='object'&&(Object.keys(value).length!==1||Object.keys(value).some(key=>!['id','task_id'].includes(key)))))throw new Error('exact_task_id_required')
  return id
}

export interface ScheduleToolArgs { action:string; task?:unknown; task_id?:string; mutations?:readonly {action:string;task_id?:string;task?:unknown}[]; input_json?:string }
const guide='Use task={title,prompt,mode,executor,catchUp,rule:{kind,first,timeZone,interval}}; first must be an ISO timestamp with offset. Use task_id for update/delete/authorize. Do not use at or cron.'
function validTask(value:unknown){validateTask(value as TaskInput);return value}
function taskId(value:unknown){if(typeof value!=='string'||!/^[A-Za-z0-9._:-]{1,200}$/.test(value))throw Error('exact_task_id_required: use the ID from telebot_desktop schedule list or /schedule list');return value}
/** Both public shapes enter the same business authorization and durable ledger. */
export function scheduleToolCommand(args:ScheduleToolArgs):string{
  const {action}=args
  if(!['create','update','delete','authorize','batch'].includes(action))throw Error('invalid_task_action: '+guide)
  const structured=['task','task_id','mutations'].filter(key=>args[key as keyof ScheduleToolArgs]!==undefined)
  if(Object.keys(args).some(key=>!['action','task','task_id','mutations','input_json'].includes(key)))throw Error('unexpected_task_arguments: '+guide)
  let value:unknown
  if(args.input_json!==undefined){
    if(structured.length)throw Error('one_payload_required: use structured arguments OR legacy input_json, never both')
    try{value=JSON.parse(args.input_json)}catch{throw Error('invalid_task_json: '+guide)}
    if(['delete','authorize'].includes(action))return action+' '+scheduleToolInput(action,args.input_json)
    if(action==='create')validTask(value)
    if(action==='update'){
      const row=value as {id?:unknown;input?:unknown};taskId(row?.id);validTask(row?.input)
      if(Object.keys(row).some(key=>!['id','input'].includes(key)))throw Error('unexpected_task_arguments: update expects {id,input:task}')
    }
    if(action==='batch'){
      if(!Array.isArray(value)||!value.length||value.length>50)throw Error('task_batch_limit: provide 1..50 mutations')
      for(const row of value){
        if(!row||!['create','update','delete'].includes(row.action)||Object.keys(row).some(key=>!['action','id','input'].includes(key)))throw Error('invalid_task_mutation: each mutation needs action, optional id and input:task')
        if(row.action!=='create')taskId(row.id)
        if(row.action!=='delete')validTask(row.input)
      }
    }
  }else{
    const allowed=action==='create'?['task']:action==='update'?['task','task_id']:action==='batch'?['mutations']:['task_id']
    if(structured.some(key=>!allowed.includes(key)))throw Error('unexpected_task_arguments: '+guide)
    if(action==='create')value=validTask(args.task)
    else if(action==='update')value={id:taskId(args.task_id),input:validTask(args.task)}
    else if(action==='batch'){
      if(!Array.isArray(args.mutations)||!args.mutations.length||args.mutations.length>50)throw Error('task_batch_limit: provide 1..50 mutations')
      value=args.mutations.map(row=>{
        if(!row||!['create','update','delete'].includes(row.action)||Object.keys(row).some(key=>!['action','task_id','task'].includes(key))||row.action==='create'&&row.task_id!==undefined||row.action==='delete'&&row.task!==undefined)throw Error('invalid_task_mutation: create needs task; update needs task_id + full task; delete needs task_id only')
        return {action:row.action,...(row.action!=='create'?{id:taskId(row.task_id)}:{}),...(row.action!=='delete'?{input:validTask(row.task)}:{})}
      })
    }else return action+' '+taskId(args.task_id)
  }
  return action+' '+JSON.stringify(value)
}
