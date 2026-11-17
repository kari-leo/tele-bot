import type { Operation } from './resources.js'

export interface BitableToolArgs {resource:string;action?:string;params?:Record<string,any>;params_json?:string}
const recordsGuide='Use params.records=[{record_id:"exact ID",fields:{"field name":"value"}}] for update; create omits record_id; delete uses record_id only. Inspect fields first.'
export function bitableOperation(action:Operation['action'],args:BitableToolArgs):Operation{
  if(typeof args.resource!=='string'||!args.resource.trim())throw Error('bitable_resource_required: use an exact alias from feishu_resources')
  if(args.action!==undefined&&args.action!==action)throw Error('bitable_action_mismatch: use the tool matching the requested action')
  if(args.params!==undefined&&args.params_json!==undefined)throw Error('one_payload_required: use params OR legacy params_json, never both')
  let params=args.params
  if(args.params_json!==undefined){try{params=JSON.parse(args.params_json)}catch{throw Error('invalid_bitable_json: '+recordsGuide)}}
  params??={}
  if(typeof params!=='object'||Array.isArray(params))throw Error('bitable_params_object_required: '+recordsGuide)
  if(action==='inspect'){
    if(Object.keys(params).length)throw Error('bitable_inspect_has_no_params: pass resource only')
  }else if(action==='query'){
    if(Object.keys(params).some(key=>!['filter','field_names','page_size','page_token'].includes(key)))throw Error('bitable_query_fields: use filter, field_names, page_size (1..100), page_token')
    params={...params,page_size:params.page_size??20}
    if(!Number.isSafeInteger(params.page_size)||params.page_size<1||params.page_size>100)throw Error('invalid_bitable_page_size: use 1..100, default 20')
    if(params.page_token!==undefined&&(typeof params.page_token!=='string'||!params.page_token||params.page_token.length>4096))throw Error('invalid_bitable_page_token: copy next_page_token from the preceding page')
    if(params.field_names!==undefined&&(!Array.isArray(params.field_names)||!params.field_names.length||params.field_names.some((x:unknown)=>typeof x!=='string'||!x)))throw Error('invalid_bitable_field_names: provide an array of exact inspected field names')
    if(params.filter!==undefined&&(!params.filter||typeof params.filter!=='object'||Array.isArray(params.filter)))throw Error('invalid_bitable_filter: use a Feishu filter object')
  }else{
    if(Object.keys(params).some(key=>key!=='records')||!Array.isArray(params.records)||!params.records.length||params.records.length>500)throw Error('bitable_records_required: '+recordsGuide)
    for(const row of params.records){
      if(!row||typeof row!=='object'||Array.isArray(row)||Object.keys(row).some(key=>!['record_id','fields'].includes(key)))throw Error('invalid_bitable_record: '+recordsGuide)
      if(action!=='create'&&(typeof row.record_id!=='string'||!row.record_id))throw Error('bitable_record_id_required: '+recordsGuide)
      if(action==='create'&&row.record_id!==undefined||action==='delete'&&row.fields!==undefined)throw Error('invalid_bitable_record_fields: '+recordsGuide)
      if(action!=='delete'&&(!row.fields||typeof row.fields!=='object'||Array.isArray(row.fields)||!Object.keys(row.fields).length))throw Error('bitable_fields_required: '+recordsGuide)
    }
  }
  return {resource:args.resource,action,params}
}
