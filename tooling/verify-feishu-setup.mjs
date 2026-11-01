import { schedulingRows } from '../tests/support/scheduling-composition.mjs'
import { cleanupVerification } from './cleanup-verification.mjs'
import assert from 'node:assert/strict'
import { mkdtemp, mkdir, readFile, writeFile } from 'node:fs/promises'
import { createServer } from 'node:net'
import { once } from 'node:events'
import { tmpdir } from 'node:os'
import path from 'node:path'
import { root, run } from './common.mjs'
import { officialHost, verificationEnvironment } from './official-host.mjs'
import { checkArtifacts } from './check-artifacts.mjs'
import { startAcp } from '../tests/support/acp-client.mjs'
import { eventually } from '../tests/support/temp-dir.mjs'
import { startMockFeishu } from '../tests/support/mock-feishu.mjs'
import { startMockModel } from '../tests/support/mock-model.mjs'
import { prepareFeatureMocks } from '../tests/support/feature-probes.mjs'

const desktop = process.argv.includes('--desktop')
const host = await officialHost(), work = await mkdtemp(path.join(tmpdir(), 'telebot-setup-'))
const home = path.join(work, 'home'), workspace = path.join(work, 'workspace 中文'), user = path.join(work, 'user')
await mkdir(workspace); await mkdir(user)
const env = { ...verificationEnvironment(home), HOME: user, USERPROFILE: user }
const profile = 'telebot-setup', patch = path.join(work, 'patch.json'), portPatch = path.join(work,'port.patch.yml')
const directory = path.join(home, 'plugins/telebot-feishu/config'), file = path.join(directory, 'config.json')
const report = { date: new Date().toISOString(), runtime: host.version, realHarness: true, realFeishu: false, paidModels: false, checks: [], status: 'running' }
const check = description => { report.checks.push(description); console.log('PASS ' + description) }
const command = args => run(process.execPath, [host.cli, ...args], { cwd: work, env })
let acp, platform, model, port, origin, token, featureMocks
const open = async label => { acp = await startAcp({ cli: host.cli, profile, patches: [patch,portPatch], cwd: work, env, log: path.join(work, label + '.log') }); await eventually(async () => { try { return (await fetch(origin + '/telebot/feishu/setup')).status === 200 } catch { return false } }, 15000); token = (await readFile(path.join(directory, 'setup.token'), 'utf8')).trim() }
const stop = async () => { if (acp) { await acp.stop(); acp = undefined } }
const api = async (method = 'GET', body, headers = {}) => {
  const response = await fetch(origin + '/telebot/feishu/setup/api', { method, headers: { Authorization: 'Bearer ' + token, Origin: origin, ...(body ? { 'Content-Type': 'application/json' } : {}), ...headers }, ...(body ? { body: JSON.stringify(body) } : {}) })
  return { status: response.status, body: await response.json() }
}
const challenge = async () => { const response = await fetch(origin + '/feishu/webhook', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ type: 'url_verification', token: 'setup-loopback-token', challenge: 'configured' }) }); return { status: response.status, text: await response.text() } }
try {
  const manifest = await checkArtifacts(); report.artifacts = manifest.packages
  assert.equal(manifest.packages.filter(row => !row.fixture).length, 7)
  assert(!manifest.packages.some(row => row.name === '@telebot/feishu-tools'))
  await command(['--profile', profile, '--from-default-profile', desktop?'web':'acp', '--dump-config'])
  for (const entry of manifest.packages) await command(['plugin', '--profile', profile, 'add', 'file:' + path.join(root, 'dist', entry.filename).replaceAll('\\', '/')])
  check('seven production bundles install; one unified Feishu bundle owns channel and business')
  const reservation = createServer(); reservation.listen(0, '127.0.0.1'); await once(reservation, 'listening'); port = reservation.address().port; await new Promise(resolve => reservation.close(resolve)); origin = 'http://127.0.0.1:' + port
  env.FEISHU_WEBHOOK_PORT=String(port)
  const portExpression=(await readFile(path.join(root,'examples/feishu.patch.example.yml'),'utf8')).match(/port: !!js >-\r?\n      ([^\r\n]+)/)?.[1]
  assert(portExpression)
  await writeFile(portPatch,'- id: '+(desktop?'webserver':'setup-webserver')+'\n  config:\n    host: 127.0.0.1\n    port: !!js >-\n      '+(desktop?'0':portExpression)+'\n')
  platform = await startMockFeishu({ appId:'cli_setup', appSecret:'setup-loopback-secret' })
  model = await startMockModel()
  featureMocks=await prepareFeatureMocks(work,env)
  await writeFile(patch, JSON.stringify([...(desktop?[{insert:[{id:'setup-acp-startup',name:'@deepseek-ai/dsh-acp-app'},{id:'acp',name:'@deepseek-ai/dsh-acp',inject:['acpAppStartup']}]},{id:'session-title-llm',disabled:true},{id:'web-runtime',config:{printUrl:false,openBrowser:false}}]:[{insert:[{id:'setup-webserver',name:'@deepseek-ai/dsh-host-webserver',config:{host:'127.0.0.1',port}},...schedulingRows]}]), { id: 'llm-pi-ai', config: { providers: { 'setup-loopback': { api:'openai-completions', apiKeyEnv:'TELEBOT_SETUP_UNUSED_KEY',baseURL:model.url,retryPolicy:{mode:'normal',maxRetries:0},models:[{id:'setup-mock',contextWindow:131072,maxTokens:4096,input:['text']}] } } } }, { id: 'telebot-feishu', disabled: false, config: desktop ? {dedicatedIngress:true,deploymentPort:port} : {} },{id:'telebot-codex',disabled:false,config:{}},{id:'telebot-adviser',disabled:false,config:{}},{id:'telebot-publishing',disabled:false,config:{}},{id:'telebot-automation',disabled:false,config:{}}]))
  await open('missing')
  const initial = await api(); assert.equal(initial.status, 200); assert(initial.body.missing.includes('appId')); assert.equal(initial.body.active, false); assert.equal((await challenge()).status, 404)
  check('missing configuration keeps the official host alive, creates config files and shows setup without admitting webhook work')
  const oldImport = await api('POST', { operation: 'import-reference', revision: initial.body.revision, referencePath: path.join(work, 'must-not-read.env') })
  assert.equal(oldImport.status,400); assert.equal(oldImport.body.code,'unsupported_setup_operation')
  assert.equal((await fetch(origin + '/feishu/oauth/callback')).status,404)
  check('removed env import is rejected before file access and no OAuth callback is registered')
  for(const name of ['core','codex','adviser','publishing','automation']) {
    const ownDirectory=path.join(home,'plugins/telebot-'+name+'/config')
    await eventually(async()=>{try{await readFile(path.join(ownDirectory,'setup.token'));return true}catch{return false}},10000)
    const endpoint=JSON.parse(await readFile(path.join(ownDirectory,'endpoint.json'),'utf8'))
    if(desktop)assert.notEqual(endpoint.origin,origin)
    const ownToken=(await readFile(path.join(ownDirectory,'setup.token'),'utf8')).trim(),url=endpoint.origin+endpoint.route+'/api'
    const describe=await(await fetch(url,{headers:{Authorization:'Bearer '+ownToken}})).json()
    assert.equal((await fetch(url,{headers:{Authorization:'Bearer '+token}})).status,403)
    if(name==='core'){assert(describe.active);assert.equal(describe.diagnostics['工具服务'],true);continue}
    assert(describe.missing.length>0)
    const config={...describe.config,adminUserIds:['ou_setup'],...(['codex','adviser'].includes(name)?{cli:name==='codex'?featureMocks.cli:featureMocks.adviser}:{}),...(name!=='adviser'?{allowedRoots:[workspace]}:{}),...(name==='codex'?{historyOwnerUserId:'ou_setup'}:{})}
    const headers={Authorization:'Bearer '+ownToken,Origin:endpoint.origin,'Content-Type':'application/json'}
    const saved=await fetch(url,{method:'POST',headers,body:JSON.stringify({revision:describe.revision,config})})
    assert.equal(saved.status,200);assert((await saved.json()).active)
    const persisted=await readFile(path.join(ownDirectory,'config.json'),'utf8')
    assert.equal((await fetch(url,{method:'POST',headers,body:JSON.stringify({revision:describe.revision,config})})).status,409)
    const current=await(await fetch(url,{headers:{Authorization:'Bearer '+ownToken}})).json()
    const invalid=['codex','adviser'].includes(name)?{cli:'relative'}:{allowedRoots:['relative']}
    assert.equal((await fetch(url,{method:'POST',headers,body:JSON.stringify({revision:current.revision,config:{...config,...invalid}})})).status,400)
    assert.equal(await readFile(path.join(ownDirectory,'config.json'),'utf8'),persisted)
  }
  check('core diagnostics and Codex/adviser setup have separate local capabilities, manual activation, revision checks and validation rollback')
  assert.equal((await fetch(origin + '/telebot/feishu/setup/api')).status, 403)
  assert.equal((await api('GET', undefined, { 'x-forwarded-for': '203.0.113.1' })).status, 403)
  assert.equal((await api('POST', {revision:initial.body.revision}, {Origin:'https://evil.example'})).status, 403)
  check('setup API rejects missing capability, forwarded/public proxy requests and foreign origins')
  const before = await readFile(file, 'utf8')
  const invalid = await api('POST', { revision: initial.body.revision, config: { appId:'cli_setup',adminUserIds:['ou_setup'],workspace:'relative' }, secrets:{appSecret:'never-persist-invalid',verificationToken:'never-persist-invalid'} })
  assert.equal(invalid.status, 400); assert.equal(await readFile(file, 'utf8'), before)
  check('invalid setup does not replace the file or activate the channel')
  const body = { revision: initial.body.revision, config: { appId:'cli_setup',adminUserIds:['ou_setup'],allowedChatIds:['oc_setup'],workspace,dedicatedIngress:desktop,deploymentPort:port,apiBaseUrl:platform.origin,publicWebhookUrl:'https://test.example/feishu/webhook',businessEnabled:true,provider:'setup-loopback',model:'setup-mock' }, secrets:{appSecret:'setup-loopback-secret',verificationToken:'setup-loopback-token'} }
  const saved = await api('POST',body); assert.equal(saved.status,200); assert(saved.body.active)
  assert.deepEqual(JSON.parse((await challenge()).text),{challenge:'configured'})
  let persisted = await readFile(file,'utf8'); assert(!persisted.includes('setup-loopback-secret')); assert(!persisted.includes('setup-loopback-token'))
  const configured = await api(); assert.equal(configured.body.credentials.appSecret,true); assert.equal(configured.body.credentials.verificationToken,true)
  check('saving config uses official credential storage and activates the unified plugin without restarting the host')
  const discovery = await api('POST',{operation:'discover-bitable',revision:configured.body.revision,baseUrl:'https://fixture.feishu.cn/base/test-base'})
  assert.equal(discovery.status,200);assert.equal(discovery.body.tables[0].table_id,'test-table')
  check('authorized setup discovers a specific Base using the stored application identity without copying its token into config')
  assert.equal((await api('POST',body)).status,409); assert.equal(await readFile(file,'utf8'),persisted)
  check('stale revision cannot overwrite a newer configuration')
  await fetch(origin+'/feishu/webhook',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({schema:'2.0',header:{app_id:'cli_setup',event_id:'setup-busy',event_type:'im.message.receive_v1',token:'setup-loopback-token'},event:{sender:{sender_type:'user',sender_id:{open_id:'ou_setup'}},message:{message_id:'om_setup_busy',chat_id:'oc_setup',chat_type:'p2p',message_type:'text',content:JSON.stringify({text:'TELEBOT_TEST:CONFIGURATION_BUSY'})}}})})
  assert.equal((await api('POST',{revision:configured.body.revision,config:configured.body.config})).status,409)
  assert.equal(await readFile(file,'utf8'),persisted)
  await eventually(()=>platform.sent.some(row=>row.replyTo==='om_setup_busy'),15000)
  await eventually(async()=>!(await api()).body.busy,10000)
  check('queued work refuses a config change instead of cancelling, orphaning or rerouting the accepted request')
  const blockedDirectory=path.join(work,'blocked-business-directory');await writeFile(blockedDirectory,'isolated failure fixture')
  const badRuntime = await api('POST',{revision:configured.body.revision,config:{...configured.body.config,business:{...configured.body.config.business,dataDirectory:blockedDirectory}},secrets:{appSecret:'rolled-back-loopback-secret'}})
  assert.equal(badRuntime.status,400);assert.equal((await api()).body.active,true);assert.equal((await challenge()).status,200);assert.equal(await readFile(file,'utf8'),persisted)
  check('failed business configuration restores the previous live channel and leaves its saved configuration intact')
  const previousToken=token;await stop();await open('restarted');assert.notEqual(token,previousToken)
  await eventually(async()=>!(await api()).body.initializing,15000)
  const restarted=await api();assert.equal(restarted.body.config.appId,'cli_setup');assert.equal(restarted.body.active,true);assert.deepEqual(restarted.body.missing,[]);assert.equal((await challenge()).status,200)
  check('process restart restores file configuration and official stored credentials with a rotated setup capability')
  const nextReservation=createServer();nextReservation.listen(0,'127.0.0.1');await once(nextReservation,'listening');const nextPort=nextReservation.address().port;await new Promise(resolve=>nextReservation.close(resolve))
  const next=await api('POST',{revision:restarted.body.revision,config:{...restarted.body.config,deploymentPort:nextPort}});assert.equal(next.status,200);assert.equal((await api()).body.deployment.restartRequired,true)
  persisted=await readFile(file,'utf8');await stop();port=nextPort;origin='http://127.0.0.1:'+port;await open('port-restarted');await eventually(async()=>!(await api()).body.initializing,15000);assert.equal((await api()).body.active,true);assert.equal((await api()).body.deployment.effectivePort,nextPort);assert.equal((await api()).body.deployment.restartRequired,false);assert.equal((await challenge()).status,200)
  check('a saved deployment port changes the actual official listener on restart, including desktop fixed-port isolation')
  if(process.argv.includes('--preview')) {
    await mkdir(path.join(root,'.runtime'),{recursive:true})
    const finishFile = path.join(work, 'finish-preview')
    await writeFile(path.join(root,'.runtime/setup-preview.json'),JSON.stringify({url:origin+'/telebot/feishu/setup#'+token,home,work,finishFile}))
    console.log('PREVIEW_READY '+origin+'/telebot/feishu/setup (isolated fake credentials; press Enter to finish)')
    if (process.stdin.isTTY) { process.stdin.resume();await once(process.stdin,'data');process.stdin.pause() }
    else await eventually(async () => { try { await readFile(finishFile); return true } catch { return false } }, 600000)
  }
  await stop()
  const preservedAfterPreview = await readFile(file,'utf8')
  for(const entry of [...manifest.packages].reverse()) await command(['plugin','--profile',profile,'remove',entry.name])
  assert.equal(await readFile(file,'utf8'),process.argv.includes('--preview') ? preservedAfterPreview : persisted)
  check('official uninstall retains editable config and business data without a second Feishu package')
  report.status='passed'
}catch(error){report.status='failed';report.error=error.message;throw error}
finally{await stop().catch(()=>{});await platform?.close();await model?.close();await featureMocks?.close();await mkdir(path.join(root,'.runtime'),{recursive:true});await writeFile(path.join(root,desktop?'.runtime/verify-desktop-setup-latest.json':'.runtime/verify-feishu-setup-latest.json'),JSON.stringify(report,null,2)+'\n');console.log('Setup evidence: '+path.join(root,desktop?'.runtime/verify-desktop-setup-latest.json':'.runtime/verify-feishu-setup-latest.json'));await cleanupVerification(work,report.status==='passed')}
