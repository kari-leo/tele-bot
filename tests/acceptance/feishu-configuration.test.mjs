import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile, writeFile, readdir } from 'node:fs/promises'
import path from 'node:path'
import { Config } from '../../plugins/feishu/lib/config.js'
import { Configuration } from '../../plugins/feishu/lib/adapters/configuration.js'
import { readReference } from '../../plugins/feishu/lib/adapters/reference-env.js'
import { discoverBase } from '../../plugins/feishu/lib/business/adapters/discovery.js'
import { temporaryDirectory, cleanup } from '../support/temp-dir.mjs'
const credentials = () => { const values = new Map(); return { values, read: async key => values.get(key) ?? '', write: async (key, value) => values.set(key, value), remove: async key => { values.delete(key) } } }

test('explicit Base discovery rejects untrusted/ambiguous URLs before authentication and follows complete table pagination',async()=>{
  let requests=0
  const api={request:async(_method,route)=>{requests++;return {items:[{table_id:route.includes('page_token')?'second':'first',name:'table'}],has_more:!route.includes('page_token'),page_token:'next'}}}
  for(const value of ['https://evil.example/base/secret','https://feishu.cn.evil.example/base/secret','https://user:password@tenant.feishu.cn/base/secret','https://tenant.feishu.cn/wiki/secret']) await assert.rejects(discoverBase(value,api))
  assert.equal(requests,0)
  const value=await discoverBase('https://tenant.feishu.cn/base/authorized?table=second',api)
  assert.deepEqual(value.tables.map(row=>row.table_id),['first','second']);assert.equal(value.preferredTableId,'second');assert(value.complete);assert.equal(requests,2)
})

test('unconfigured unified Feishu creates editable non-secret files, detects missing fields and rejects a second writer', async t => {
  const directory = await temporaryDirectory(t), store = await Configuration.open(directory, Config({}), credentials())
  cleanup(t, () => store.close())
  assert.deepEqual(await store.missing(), ['appId','adminUserIds','workspace','appSecret','verificationToken_or_encryptKey'])
  for (const name of ['config.json','config.example.json','README.txt']) assert((await readdir(directory)).includes(name))
  assert.equal(JSON.parse(await readFile(path.join(directory,'config.json'),'utf8')).config.business.resources.length,0)
  await assert.rejects(Configuration.open(directory, Config({}), credentials()), /already_active/)
})

test('unknown bootstrap values and literal resource tokens cannot leak through first-run files or status',async t=>{
  const directory=await temporaryDirectory(t),store=await Configuration.open(directory,Config({appSecret:'must-not-write-unknown-secret',business:{resourcesJson:'[{"access_token":"must-not-echo-resource-secret"}]'}}),credentials())
  cleanup(t,()=>store.close())
  assert((await store.missing()).includes('bootstrap_resource_configuration_invalid'))
  assert(!JSON.stringify(await store.describe()).includes('must-not-'))
  for(const name of await readdir(directory)) assert(!(await readFile(path.join(directory,name),'utf8')).includes('must-not-'))
})

test('configuration persists credential refs and resource metadata, survives restart without standalone setup capabilities', async t => {
  const directory = await temporaryDirectory(t), secrets = credentials()
  let store = await Configuration.open(directory, Config({}), secrets)
  const {candidate,pending} = await store.prepare({appId:'cli_test',adminUserIds:['ou_alice'],workspace:directory,publicWebhookUrl:'https://test.example/feishu/webhook'}, {appSecret:'test-secret-never-in-json',verificationToken:'test-verification-never-in-json'})
  for(const row of pending) await secrets.write(row.ref,row.value)
  await store.save(candidate)
  const file = await readFile(path.join(directory,'config.json'),'utf8')
  assert(!file.includes('test-secret-never-in-json')); assert(!file.includes('test-verification-never-in-json'))
  assert((await store.describe()).credentials.appSecret); assert.deepEqual(await store.missing(),[])
  await store.close(); store=await Configuration.open(directory,Config({}),secrets); cleanup(t,()=>store.close())
  assert.equal(store.current.appId,'cli_test'); assert.equal(store.current.workspace,directory)
  assert(!(await readdir(directory)).includes('setup.token')); assert(!(await readdir(directory)).includes('endpoint.json'))
})

test('configuration rejects invalid workspace/callback/secret and unknown literal credential fields without replacing saved data', async t => {
  const directory=await temporaryDirectory(t),store=await Configuration.open(directory,Config({}),credentials());cleanup(t,()=>store.close())
  const original=await readFile(path.join(directory,'config.json'),'utf8')
  const base={appId:'cli_test',adminUserIds:['ou_alice'],workspace:directory}
  for(const [patch,secrets] of [[{workspace:'relative'},{}],[{publicWebhookUrl:'http://public.example/feishu/webhook'},{}],[{webhookPath:'/telebot/feishu/setup/api'},{}],[{business:{oauthRedirectUri:'https://obsolete.example/callback'}},{}],[{appSecret:'literal-leak'},{}],[{}, {appSecret:123}],[{},{}]]) await assert.rejects(store.prepare({...base,...patch},secrets))
  await assert.rejects(store.prepare({...base,business:{resourcesJson:JSON.stringify([{alias:'bad',access_token:'literal-token-must-not-save'}])}}),/metadata_only/)
  assert.equal(await readFile(path.join(directory,'config.json'),'utf8'),original)
})

test('malformed saved config is retained for correction and enters setup instead of silently activating another profile',async t=>{
  const directory=await temporaryDirectory(t);await writeFile(path.join(directory,'config.json'),'not json')
  const store=await Configuration.open(directory,Config({appId:'cli_bootstrap'}),credentials());cleanup(t,()=>store.close())
  assert((await store.missing()).includes('config_file_invalid'));assert.equal(await readFile(path.join(directory,'config.json'),'utf8'),'not json')
})

test('reference env import is literal, bounded, whitelisted and does not import old model/runtime settings',async t=>{
  const directory=await temporaryDirectory(t),filename=path.join(directory,'local.env')
  await writeFile(filename,"FEISHU_APP_ID=cli_test\nFEISHU_APP_SECRET='$(not-executed)'\nFEISHU_VERIFICATION_TOKEN=test-token\nOPENAI_API_KEY=must-not-import\nFEISHU_BITABLE_SQLITE_PATH=old-data\n")
  assert.deepEqual(await readReference(filename),{FEISHU_APP_ID:'cli_test',FEISHU_APP_SECRET:'$(not-executed)',FEISHU_VERIFICATION_TOKEN:'test-token'})
  await assert.rejects(readReference('relative.env'),/invalid_reference/)
})

test('native settings registry scopes pages to enabled plugins, bounds writes and does not leak arbitrary errors',async t=>{
  const {createRequire}=await import('node:module');const require=createRequire(new URL('../../plugins/core/package.json',import.meta.url));
  const {pathToFileURL}=await import('node:url');const {Context}=await import(pathToFileURL(require.resolve('@deepseek-ai/cordis')));
  const {ConfigurationPages}=await import('../../plugins/core/lib/harness/configuration-ui.js');
  const ctx=new Context(),registry=new ConfigurationPages(ctx);cleanup(t,()=>ctx.fiber.dispose());let writes=0;
  const remove=registry.register({name:'telebot-fixture',title:'Fixture',fields:[],describe:async()=>({config:{},revision:'one',missing:[],active:true}),save:async body=>{writes++;if(body.fail)throw new Error('/private/secret');return{saved:true}}});
  const request=(body,headers={})=>new Request('http://127.0.0.1/api/telebot/configuration',{method:'POST',headers:{'Content-Type':'application/json',...headers},body:JSON.stringify(body)});
  const listing=await registry.handle(request({action:'list'}));assert.equal((await listing.json()).pages[0].name,'telebot-fixture');
  assert.equal((await registry.handle(request({plugin:'telebot-fixture',action:'save',body:{}}))).status,200);
  assert.equal((await registry.handle(request({plugin:'telebot-fixture',action:'save',body:{}},{'x-forwarded-for':'203.0.113.1'}))).status,403);
  assert.equal((await registry.handle(request({plugin:'telebot-fixture',action:'save',body:{value:'x'.repeat(262145)}}))).status,413);
  assert.equal(writes,1);
  const failure=await registry.handle(request({plugin:'telebot-fixture',action:'save',body:{fail:true}}));assert.equal((await failure.json()).code,'configuration_request_rejected');
  remove();assert.equal((await registry.handle(request({plugin:'telebot-fixture',action:'get'}))).status,404);
})
