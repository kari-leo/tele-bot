import type { ParameterSchemaSpec } from '@deepseek-ai/dsh-tools'
import type { Operation } from '../domain/resources.js'

export function bitableParameters(action:Operation['action']):ParameterSchemaSpec{
  const record={type:'object',additionalProperties:false,properties:{
    ...(action!=='create'?{record_id:{type:'string',required:true,description:'Exact record ID returned by query.'} as const}:{}),
    ...(action!=='delete'?{fields:{type:'object',additionalProperties:true,required:true,description:'Exact inspected field names mapped to Feishu field values.'} as const}:{}),
  }} as const
  let properties:ParameterSchemaSpec={}
  if(action==='query')properties={
    filter:{type:'object',additionalProperties:true,description:'Filter on exact field names; prefer this over scanning the whole table. Example: {conjunction:"and",conditions:[{field_name:"任务名称",operator:"contains",value:["面试"]}]}'},
    field_names:{type:'array',items:{type:'string'},description:'Return only the fields needed for this task.'},
    page_size:{type:'integer',description:'1..100 records; default 20. Use a filter for a specific record.'},
    page_token:{type:'string',description:'For continuation only; copy next_page_token and keep the same filter/field_names.'},
  }
  else if(action!=='inspect')properties={records:{type:'array',items:record,required:true,description:'1..500 records.'}}
  return {
    resource:{type:'string',required:true,description:'Exact alias from feishu_resources.'},
    ...(action!=='inspect'?{params:{type:'object',additionalProperties:false,properties}} as const:{}),
    params_json:{type:'string',description:'Legacy JSON encoding of params; do not combine with params. '+(action==='inspect'?'Use {}.':action==='query'?'Use filter/field_names/page_size/page_token.':'Use {records:[{'+(action==='create'?'fields:{...}':action==='delete'?'record_id:"..."':'record_id:"...",fields:{...}')+'}]}.')},
  }
}
