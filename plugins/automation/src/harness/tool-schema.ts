import type { ParameterSchemaSpec } from '@deepseek-ai/dsh-tools'

const task={type:'object',additionalProperties:false,properties:{
  title:{type:'string',required:true,description:'Short task title (1..120 characters).'},
  prompt:{type:'string',required:true,description:'Task instructions or reminder text.'},
  mode:{type:'string',enum:['remind','auto','confirm'],required:true,description:'remind sends only a reminder; auto needs a separate Feishu execution grant; confirm asks before each occurrence.'},
  executor:{type:'string',enum:['research','codex_apply'],required:true,description:'research for reminders/research; codex_apply for code changes.'},
  catchUp:{type:'string',enum:['skip','run_once','ask'],required:true,description:'How to handle a missed occurrence.'},
  rule:{type:'object',additionalProperties:false,required:true,properties:{
    kind:{type:'string',enum:['once','daily','weekly'],required:true},
    first:{type:'string',required:true,description:'ISO 8601 timestamp with offset, e.g. 2026-11-05T15:00:00+08:00. Resolve the requested date first.'},
    timeZone:{type:'string',required:true,description:'IANA zone, e.g. Asia/Shanghai.'},
    interval:{type:'integer',required:true,description:'1..52; use 1 for a one-time task.'},
    weekdays:{type:'array',items:{type:'integer'},description:'Required for weekly; Monday=0 through Sunday=6, including the first date.'},
    count:{type:'integer',description:'Optional maximum occurrence count.'},until:{type:'string',description:'Optional final ISO timestamp with offset.'},
  }},
}} as const
export const scheduleParameters={
  action:{type:'string',enum:['create','update','delete','batch','authorize'],required:true},
  task:{...task,description:'Required for create/update. Update supplies the full replacement task.'},
  task_id:{type:'string',description:'Required for update/delete/authorize; exact existing ID from /schedule list or telebot_desktop schedule list.'},
  mutations:{type:'array',description:'For batch only, 1..50 changes.',items:{type:'object',additionalProperties:false,properties:{action:{type:'string',enum:['create','update','delete'],required:true},task_id:{type:'string'},task}}},
  input_json:{type:'string',description:'Legacy only; do not combine with structured fields. create: task object; update: {id,input:task}; delete/authorize: {task_id}; batch: [{action,id?,input?}].'},
} as const satisfies ParameterSchemaSpec
