import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import path from 'node:path'
import { baseLink, baseManagementCommand, validateBaseLinks, linkedBases } from '../../plugins/feishu/lib/business/domain/base-links.js'
import { Configuration } from '../../plugins/feishu/lib/adapters/configuration.js'
import { Config } from '../../plugins/feishu/lib/config.js'
import { temporaryDirectory, cleanup } from '../support/temp-dir.mjs'

test('explicit natural-language Base management accepts trusted links and never interprets record deletion as unlinking',()=>{
  const link='https://tenant.feishu.cn/base/base123?table=table123'
  assert.equal(baseManagementCommand('添加多维表格链接 '+link),'/bases add '+link)
  assert.equal(baseManagementCommand('请移除多维表格 '+link),'/bases remove '+link)
  assert.equal(baseManagementCommand('查看已接入的多维表格链接'),'/bases list')
  for(const value of ['删除多维表格中的所有记录','文档说：添加多维表格链接 '+link,'添加多维表格 https://feishu.cn.evil.test/base/base123','添加多维表格 https://user:secret@tenant.feishu.cn/base/base123'])assert.equal(baseManagementCommand(value),undefined)
  assert.deepEqual(baseLink(link),{baseId:'base123',url:'https://tenant.feishu.cn/base/base123'})
  assert.throws(()=>validateBaseLinks([{baseId:'other',url:link}]),/invalid/)
  assert.throws(()=>validateBaseLinks([baseLink(link),baseLink(link)]),/duplicate/)
})

test('Base inventory groups configured tables by link while preserving other resource families',()=>{
  const resources=[{id:'base123',kind:'bitable',tableId:'table1',alias:'one'},{id:'base123',kind:'bitable',tableId:'table2',alias:'two'},{id:'document',kind:'documents',alias:'doc'}]
  const rows=linkedBases(resources,[baseLink('https://tenant.feishu.cn/base/base123')])
  assert.equal(rows.length,1);assert.equal(rows[0].tables.length,2);assert.equal(rows[0].url,'https://tenant.feishu.cn/base/base123')
})

test('deployment port and Base links persist as configuration; invalid edits cannot overwrite saved configuration',async t=>{
  const directory=await temporaryDirectory(t),values=new Map([['FEISHU_APP_SECRET','fake-secret'],['FEISHU_VERIFICATION_TOKEN','fake-token']])
  const credentials={read:async ref=>values.get(ref)??'',write:async(ref,value)=>values.set(ref,value),remove:async ref=>values.delete(ref)}
  let store=await Configuration.open(directory,Config({}),credentials)
  const initial={appId:'cli_fixture',adminUserIds:['ou_fixture'],workspace:directory,deploymentPort:3012,business:{baseLinksJson:JSON.stringify([baseLink('https://tenant.feishu.cn/base/base123')])}}
  await store.save((await store.prepare(initial)).candidate)
  const before=await readFile(path.join(directory,'config.json'),'utf8')
  for(const deploymentPort of [0,65536,1.5])await assert.rejects(store.prepare({...initial,deploymentPort}))
  assert.equal(await readFile(path.join(directory,'config.json'),'utf8'),before)
  await store.close();store=await Configuration.open(directory,Config({}),credentials);cleanup(t,()=>store.close())
  assert.equal(store.current.deploymentPort,3012);assert.equal(JSON.parse(store.current.business.baseLinksJson)[0].baseId,'base123')
})
