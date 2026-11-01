import test from 'node:test'
import assert from 'node:assert/strict'
import vm from 'node:vm'
import {readFile} from 'node:fs/promises'
import React from 'react'
import Renderer from 'react-test-renderer'
import {Config} from '../../plugins/feishu/lib/config.js'
import {feishuFields} from '../../plugins/feishu/lib/setup/fields.js'
const {act}=Renderer
const text=node=>typeof node==='string'?node:Array.isArray(node)?node.map(text).join(''):node?.children?.map(text).join('')??''
async function page(t){
 const calls=[],config=Config({appId:'cli_fixture',adminUserIds:['ou_fixture'],allowedChatIds:['oc_scope'],workspace:'D:/fixture',business:{resourcesJson:JSON.stringify([{alias:'doc',kind:'documents',id:'doc1',identity:'tenant',tokenRef:'FEISHU_APP_TENANT',expiresAt:0,capabilities:['documents.read']}])}})
 let revision='one',fields=feishuFields,saved=config,reject=false,emptyResponse=false,capture,registration
 const snapshot=()=>({name:'telebot-feishu',title:'飞书配置',fields,config:structuredClone(saved),revision,missing:[],active:true,busy:false,directory:'D:/fixture/config',credentials:{appSecret:true,verificationToken:true}})
 const request=async(url,options={})=>{
  assert.equal(url,'/api/telebot/configuration');assert.equal(options.credentials,'same-origin');assert(!options.headers?.Authorization)
  const body=options.body?JSON.parse(options.body):undefined;calls.push(body??{action:'list'})
  if(body?.action==='list')return Response.json({pages:[{name:'telebot-codex',title:'Codex 插件配置',active:true,missing:[]},{name:'telebot-feishu',title:'飞书配置',active:true,missing:[]}]})
  if(body.plugin==='telebot-codex')return Response.json({title:'Codex 插件配置',fields:[{key:'cli',label:'CLI 路径'}],config:{cli:'D:/codex/cli.js'},revision:'codex',missing:[],active:true,directory:'D:/fixture/codex'})
  if(body.action==='get')return Response.json(snapshot())
  if(body.body.operation==='discover-bitable')return Response.json({baseId:'fixture-base',tables:[{table_id:'table1',name:'Fixture table'}]})
  if(emptyResponse)return new Response(null,{status:502})
  if(reject)return Response.json({code:'configuration_revision_conflict'},{status:409})
  saved=structuredClone(body.body.config);revision+='-next';return Response.json({saved:true,active:true,revision})
 }
 const source=await readFile(new URL('../../plugins/core/client/index.js',import.meta.url),'utf8')
 vm.runInNewContext(source,{window:{__ModuleLoader__:{load:value=>{capture=value}}},fetch:request,structuredClone,URL,Response,AbortController,console})
 assert.equal(capture.id,'@telebot/core')
 const module=capture.factory(name=>{assert.equal(name,'react');return React})
 const slots={inject(name,fn){assert.equal(name,'settings.section');fn()},register(options,component){registration={options,component};return()=>{}}}
 module.apply({get:name=>name==='slots'?slots:undefined})
 assert.equal(registration.options.id,'telebot');assert.equal(registration.options.label(),'Telebot 插件')
 let root;await act(async()=>{root=Renderer.create(React.createElement(registration.component));await new Promise(setImmediate)})
 t.after(()=>act(()=>root.unmount()))
 const button=label=>root.root.findAllByType('button').find(item=>text(item.toJSON?.()??item)==label||item.children.filter(x=>typeof x==='string').join('')===label)
 return{root,calls,button,get saved(){return saved},get revision(){return revision},reject(){reject=true},empty(){emptyResponse=true},input:key=>root.root.findByProps({id:'telebot-'+key})}
}

test('native settings mount in the official slot, preserve hidden fields and send secrets separately without a setup token',async t=>{
 const view=await page(t);assert(view.root.toJSON());assert.equal(view.root.root.findAllByType('iframe').length,0)
 assert.equal(view.input('appSecret').props.value,'');assert.equal(view.input('appId').props.value,'cli_fixture')
 await act(async()=>{view.input('appId').props.onChange({target:{value:'cli_updated'}});view.input('appSecret').props.onChange({target:{value:'synthetic-new-secret'}})})
 await act(async()=>{await view.root.root.findByType('form').props.onSubmit({preventDefault(){}})})
 const save=view.calls.find(x=>x.action==='save');assert.equal(save.body.config.appId,'cli_updated');assert.equal(save.body.secrets.appSecret,'synthetic-new-secret');assert(!JSON.stringify(save.body.config).includes('synthetic-new-secret'));assert.deepEqual(save.body.config.allowedChatIds,['oc_scope'])
 assert.equal(view.input('appSecret').props.value,'');assert.match(text(view.root.toJSON()),/已保存/)
})

test('native Base links have an independent add/remove flow and retain other resource families',async t=>{
 const view=await page(t),base=view.root.root.findByProps({placeholder:'https://…feishu.cn/base/…'})
 await act(async()=>base.props.onChange({target:{value:'https://fixture.feishu.cn/base/fixture-base?table=table1'}}))
 await act(async()=>{await view.button('添加链接并读取数据表').props.onClick()})
 await act(async()=>{await view.root.root.findByType('form').props.onSubmit({preventDefault(){}})})
 assert.equal(JSON.parse(view.saved.business.baseLinksJson)[0].baseId,'fixture-base');assert.equal(JSON.parse(view.saved.business.resourcesJson).length,2)
 assert.equal(JSON.parse(view.saved.business.resourcesJson).find(row=>row.kind==='bitable').name,'Fixture table')
 await act(async()=>view.input('business.resourcesJson').props.onChange({target:{value:''}}))
 await act(async()=>{await view.root.root.findByType('form').props.onSubmit({preventDefault(){}})})
 assert.equal(JSON.parse(view.saved.business.resourcesJson).length,1)
 assert.equal(JSON.parse(view.saved.business.resourcesJson)[0].name,'Fixture table')
 await act(async()=>view.button('移除此链接').props.onClick())
 await act(async()=>{await view.root.root.findByType('form').props.onSubmit({preventDefault(){}})})
 assert.deepEqual(JSON.parse(view.saved.business.baseLinksJson),[]);assert.deepEqual(JSON.parse(view.saved.business.resourcesJson),[])
})

test('native settings protect dirty edits when switching plugins and preserve them after a revision conflict',async t=>{
 const view=await page(t);await act(async()=>view.input('appId').props.onChange({target:{value:'unsaved'}}))
 await act(async()=>view.button('Codex 插件配置').props.onClick());assert.equal(view.input('appId').props.value,'unsaved');assert.match(text(view.root.toJSON()),/未保存/)
 view.reject();await act(async()=>{await view.root.root.findByType('form').props.onSubmit({preventDefault(){}})})
 assert.equal(view.input('appId').props.value,'unsaved');assert.match(text(view.root.toJSON()),/配置已被更新/)
 await act(async()=>{await view.button('重新读取').props.onClick()});await act(async()=>{await view.button('Codex 插件配置').props.onClick()})
 assert.equal(view.input('cli').props.value,'D:/codex/cli.js')
})

test('blank optional Feishu resource JSON saves an empty whitelist instead of a JSON parsing error',async t=>{
 const view=await page(t)
 await act(async()=>view.input('business.resourcesJson').props.onChange({target:{value:'  \n  '}}))
 await act(async()=>{await view.root.root.findByType('form').props.onSubmit({preventDefault(){}})})
 assert(view.calls.some(x=>x.action==='save'),'No save was sent; page notice: '+text(view.root.toJSON()))
 assert.deepEqual(JSON.parse(view.saved.business.resourcesJson),[])
})

test('invalid nonempty resource JSON identifies the field and retains unsaved edits without sending a save',async t=>{
 const view=await page(t)
 await act(async()=>view.input('business.resourcesJson').props.onChange({target:{value:'{'}}))
 await act(async()=>{await view.root.root.findByType('form').props.onSubmit({preventDefault(){}})})
 assert.equal(view.calls.filter(x=>x.action==='save').length,0)
 assert.equal(view.input('business.resourcesJson').props.value,'{')
 assert.match(text(view.root.toJSON()),/文档、日历和任务白名单.*JSON/)
 assert(!text(view.root.toJSON()).includes('Unexpected end'))
})

test('empty server responses report HTTP status and uncertain save state without retrying or losing form values',async t=>{
 const view=await page(t);view.empty()
 await act(async()=>view.input('appId').props.onChange({target:{value:'still-unsaved'}}))
 await act(async()=>{await view.root.root.findByType('form').props.onSubmit({preventDefault(){}})})
 assert.equal(view.calls.filter(x=>x.action==='save').length,1)
 assert.equal(view.input('appId').props.value,'still-unsaved')
 assert.match(text(view.root.toJSON()),/502/)
 assert.match(text(view.root.toJSON()),/重新读取/)
 assert(!text(view.root.toJSON()).includes('Unexpected end'))
})
