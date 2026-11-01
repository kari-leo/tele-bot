/** Functional component driver, not visual/browser automation. Uses the installed client against real owner APIs. */
import React from 'react'
import Renderer from 'react-test-renderer'
import vm from 'node:vm'
import {readFile} from 'node:fs/promises'
import assert from 'node:assert/strict'
import {eventually} from './temp-dir.mjs'

export async function saveThroughNativeSettings({clientFile,origin,cookie,values}) {
  let module,root,saved
  const request=async(route,options)=>{
    assert.equal(route,'/api/telebot/configuration')
    const response=await fetch(origin+route,{...options,headers:{...options.headers,Cookie:cookie,Origin:origin}})
    const payload=JSON.parse(options.body)
    if(payload.action==='save'&&!payload.body?.operation)saved={status:response.status,body:await response.clone().json()}
    return response
  }
  const source=await readFile(clientFile,'utf8')
  vm.runInNewContext(source,{window:{__ModuleLoader__:{load:value=>{module=value}}},fetch:request,structuredClone,URL,Response,console})
  const client=module.factory(name=>{assert.equal(name,'react');return React});let Component
  const slots={inject(_name,fn){fn()},register(options,component){assert.equal(options.name,'settings.section');Component=component;return()=>{}}}
  client.apply({get:name=>name==='slots'?slots:undefined})
  try {
    await Renderer.act(async()=>{root=Renderer.create(React.createElement(Component))})
    await Renderer.act(async()=>{await eventually(()=>root.root.findAllByProps({id:'telebot-appId'}).length===1,10000)})
    await Renderer.act(async()=>{for(const [key,value]of Object.entries(values)){const input=root.root.findByProps({id:'telebot-'+key});input.props.onChange({target:input.props.type==='checkbox'?{checked:value}:{value:String(value)}})}})
    await Renderer.act(async()=>{await root.root.findByType('form').props.onSubmit({preventDefault(){}})})
    assert(saved,'Native form did not send a save request')
    return saved
  } finally {if(root)Renderer.act(()=>root.unmount())}
}
