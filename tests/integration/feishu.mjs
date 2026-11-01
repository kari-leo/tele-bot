import { schedulingRows } from '../support/scheduling-composition.mjs'
import { cleanupVerification } from '../../tooling/cleanup-verification.mjs'
import assert from 'node:assert/strict'
import { randomUUID, createHash, createCipheriv } from 'node:crypto'
import { createServer } from 'node:net'
import { once } from 'node:events'
import { cp, mkdir, mkdtemp, readFile, rename, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import path from 'node:path'
import { DatabaseSync } from 'node:sqlite'
import { methods } from '@agentclientprotocol/sdk'
import { root, run } from '../../tooling/common.mjs'
import { checkArtifacts } from '../../tooling/check-artifacts.mjs'
import { officialHost, verificationEnvironment } from '../../tooling/official-host.mjs'
import { startAcp, deadline } from '../support/acp-client.mjs'
import { startMockModel } from '../support/mock-model.mjs'
import { startMockFeishu } from '../support/mock-feishu.mjs'
import { eventually } from '../support/temp-dir.mjs'
import { prepareFeatureMocks, verifyFeatures } from '../support/feature-probes.mjs'

const host = await officialHost()
const webProfile = process.argv.includes('--web')
const features = process.argv.includes('--features')
const resourcesOnly = process.argv.includes('--resources')
const progressOnly=process.argv.includes('--progress'),questionsOnly=process.argv.includes('--questions'),focused=resourcesOnly||progressOnly||questionsOnly
if(resourcesOnly&&features)throw Error('Choose resource discovery or full composition')
const work = await mkdtemp(path.join(tmpdir(), 'telebot-feishu-'))
const home = path.join(work, 'home'), profile = 'telebot-feishu-verify'
const workspace = path.join(work, 'workspace 中文')
await mkdir(workspace)
const userHome = path.join(work, 'user-home'); await mkdir(userHome)
const env = { ...verificationEnvironment(home), HOME: userHome, USERPROFILE: userHome,
  FEISHU_APP_SECRET: 'loopback-app-secret', FEISHU_VERIFICATION_TOKEN: 'loopback-source-token', FEISHU_ENCRYPT_KEY: '',
  FEISHU_APP_ID: 'cli_test', FEISHU_ADMIN_USER_IDS: 'ou_alice,ou_bob', TELEBOT_WORKSPACE: workspace,
  TELEBOT_PROVIDER: 'telebot-verification', TELEBOT_MODEL: 'telebot-mock-a', TELEBOT_MAX_CONCURRENT_SESSIONS: '2' }
const evidence = { runtime: host.version, platform: process.platform, baseProfile: webProfile ? 'web' : 'acp', scope:questionsOnly?'natural-questions-and-tasks':progressOnly?'editable-progress':resourcesOnly?'registered-resource-discovery':'channel', realHarness: true, realFeishu: false, paidModels: false, checks: [] }
const check = name => { evidence.checks.push(name); console.log('PASS ' + name) }
const filename = path.join(home, 'plugins/telebot-feishu/cli_test/channel.sqlite')
function rows(sql, ...parameters) {
  const db = new DatabaseSync(filename, { readOnly: true })
  try { return db.prepare(sql).all(...parameters) } finally { db.close() }
}
async function command(args, commandEnvironment = env) {
  return run(process.execPath, [host.cli, ...args], { cwd: work, env: commandEnvironment, timeout: 180000 })
}
let acp, model, platform, featureMocks
const patch = path.join(work, 'verify.patch.json'), disabled = path.join(work, 'disabled.patch.json')
const examplePatch = path.join(root, 'examples/feishu.patch.example.yml')
let encrypted = false, port, featureDisabled = false
async function post(payload, { wrongSignature = false, encrypt = encrypted } = {}) {
  let raw = JSON.stringify(payload)
  const headers = { 'Content-Type': 'application/json' }
  if (encrypt) {
    const key = createHash('sha256').update(env.FEISHU_ENCRYPT_KEY).digest()
    const cipher = createCipheriv('aes-256-cbc', key, key.subarray(0, 16))
    raw = JSON.stringify({ encrypt: Buffer.concat([cipher.update(raw), cipher.final()]).toString('base64') })
    const timestamp = String(Math.floor(Date.now() / 1000)), nonce = randomUUID()
    headers['x-lark-request-timestamp'] = timestamp; headers['x-lark-request-nonce'] = nonce
    headers['x-lark-signature'] = wrongSignature ? '0'.repeat(64) : createHash('sha256').update(timestamp + nonce + env.FEISHU_ENCRYPT_KEY + raw).digest('hex')
  }
  const response = await fetch('http://127.0.0.1:' + port + '/feishu/webhook', { method: 'POST', headers, body: raw, signal: AbortSignal.timeout(8000) })
  const body = await response.text()
  return { status: response.status, body: body.startsWith('{') ? JSON.parse(body) : {} }
}
function message(id, text, { chat = 'oc_main', actor = 'ou_alice', thread = '' } = {}) {
  return { schema: '2.0', header: { app_id: 'cli_test', event_id: 'event_' + id, event_type: 'im.message.receive_v1', token: env.FEISHU_VERIFICATION_TOKEN },
    event: { sender: { sender_type: 'user', sender_id: { open_id: actor } }, message: { message_id: 'om_' + id, chat_id: chat, thread_id: thread, message_type: 'text', content: JSON.stringify({ text }) } } }
}
const received = id => {
  const receipt=rows("SELECT remote_id FROM outbox WHERE reply_to=? AND state='sent' AND purpose IN ('message','reply') AND NOT(msg_type='interactive' AND COALESCE(json_extract(content,'$.header.title.content'),'') IN ('执行进度','需要你的回答','请求操作授权')) ORDER BY seq LIMIT 1",'om_'+id)[0]
  return receipt?platform.sent.find(entry=>entry.messageId===receipt.remote_id):undefined
}
async function settled(id) { await eventually(() => received(id), 20000); return received(id) }
async function open(label, isDisabled = false) {
  acp = await startAcp({ cli: host.cli, profile, patches: [...(webProfile ? [examplePatch] : []), patch, ...(isDisabled ? [disabled] : [])], cwd: work, env, log: path.join(work, label + '.log') })
  await eventually(async () => {
    try { return (await post({ type: 'url_verification', challenge: 'ready', token: env.FEISHU_VERIFICATION_TOKEN })).status === (isDisabled ? 404 : 200) }
    catch { return false }
  }, 15000)
}
async function stop() { await acp.stop(); acp = undefined }
async function configuration() {
  env.FEISHU_API_BASE_URL = platform.origin
  env.FEISHU_ENCRYPT_KEY = encrypted ? 'loopback-encrypt-key' : ''
  const feishuConfig = {
    appId: 'cli_test', adminUserIds: ['ou_alice', 'ou_bob'], workspace, provider: 'telebot-verification', model: 'telebot-mock-a',
    apiBaseUrl: platform.origin, encryptKeyRef: encrypted ? 'FEISHU_ENCRYPT_KEY' : '',
    maxConcurrentSessions: 2, pollMs: 50, leaseMs: 10000, retryMs: 100, actionTtlMs: 60000,
    streamProgress:progressOnly,
    businessEnabled: !featureDisabled,
    business: { resourcesJson: JSON.stringify(features ? [
      { alias: 'test-table', kind: 'bitable', id: 'test-base', tableId: 'test-table', identity: 'tenant', tokenRef: 'FEISHU_APP_TENANT', expiresAt: 0, capabilities: ['bitable.read','bitable.create','bitable.update','bitable.delete'] },
      { alias: 'test-calendar', kind: 'calendar', id: 'fixture@group.calendar.feishu.cn', identity: 'tenant', tokenRef: 'FEISHU_APP_TENANT', expiresAt: 0, capabilities: ['calendar.read','calendar.create','calendar.update','calendar.delete'] },
    ] : [{alias:'test-table',kind:'bitable',id:'test-base',tableId:'test-table',identity:'tenant',tokenRef:'FEISHU_APP_TENANT',expiresAt:0,capabilities:['bitable.read']}]) },
  }
  await writeFile(patch, JSON.stringify([
    ...(webProfile ? [
      { id: 'webserver', config: { host: '127.0.0.1', port } },
      { id: 'web-runtime', config: { printUrl: false, openBrowser: false } },
      { id: 'session-title-llm', disabled: true },
      { insert: [{ id: 'acp-app-startup', name: '@deepseek-ai/dsh-acp-app' }, { id: 'acp', name: '@deepseek-ai/dsh-acp', inject: ['acpAppStartup'] }] },
    ] : [{ insert: [{ id: 'verify-webserver', name: '@deepseek-ai/dsh-host-webserver', config: { host: '127.0.0.1', port } }, ...schedulingRows] }]),
    { id: 'llm-pi-ai', config: { providers: { 'telebot-verification': { api: 'openai-completions', apiKeyEnv: 'TELEBOT_VERIFY_KEY', baseURL: model.url,
      retryPolicy: { mode: 'normal', maxRetries: 0 }, models: ['telebot-mock-a', 'telebot-mock-b'].map(id => ({ id, contextWindow: 131072, maxTokens: 4096, input: ['text'] })) } } } },
    { id: 'acp', config: { provider: 'telebot-verification', model: 'telebot-mock-a' } },
    { id: 'telebot-feishu', disabled: false, config: feishuConfig },
    ...(questionsOnly?[{insert:[{id:'verify-ask-user',name:'@deepseek-ai/dsh-tool-ask-user',config:{mode:'legacy'}}]},{id:'telebot-automation',disabled:false,config:{adminUserIds:['ou_alice'],allowedRoots:[],provider:'telebot-verification',model:'telebot-mock-a'}}]:[]),
    ...(features ? [
      {id:'telebot-diagnostics',config:{webProbes:true}},
      {id:'web',config:{fetchProvider:'telebot-fixture'}},
      { insert: [{ id: 'verify-ask-user', name: '@deepseek-ai/dsh-tool-ask-user', config: { mode: 'legacy' } }] },
      { id: 'telebot-codex', disabled: featureDisabled, config: { cli: featureMocks.cli, allowedRoots: [workspace], adminUserIds: ['ou_alice'], historyOwnerUserId: 'ou_alice' } },
      { id: 'telebot-adviser', disabled: featureDisabled, config: { cli: featureMocks.adviser, adminUserIds: ['ou_alice'] } },
      { id: 'telebot-publishing', disabled: featureDisabled, config: { allowedRoots:[workspace],adminUserIds:['ou_alice'],postsDirectory:featureMocks.posts,repoRoot:featureMocks.repo,branch:'main' } },
      { id: 'telebot-automation', disabled: featureDisabled, config: { allowedRoots:[workspace],adminUserIds:['ou_alice'],provider:'telebot-verification',model:'telebot-mock-a' } },
    ] : []),
  ]))
  // Tests edit only their own non-secret configuration while the profile is stopped.
  const configFile = path.join(home, 'plugins/telebot-feishu/config/config.json')
  try {
    const saved = JSON.parse(await readFile(configFile, 'utf8'))
    saved.config = { ...saved.config, ...feishuConfig, business: { ...saved.config.business, ...feishuConfig.business } }
    delete saved.config.business.resources
    saved.revision = randomUUID()
    await writeFile(configFile, JSON.stringify(saved))
  } catch (error) { if (error.code !== 'ENOENT') throw error }
}
try {
  const manifest = await checkArtifacts()
  const installed=manifest.packages.filter(entry=>features||['@telebot/core','@telebot/feishu','@telebot/diagnostics',...(questionsOnly?['@telebot/automation']:[])].includes(entry.name));evidence.artifacts=installed
  assert(!manifest.packages.some(entry => entry.name === '@telebot/feishu-tools'), 'Feishu business must ship in the single Feishu bundle')
  for (const name of ['@telebot/core', '@telebot/feishu', '@telebot/diagnostics']) assert(manifest.packages.some(entry => entry.name === name), 'Pack with --fixtures')
  await command(['--profile', profile, '--from-default-profile', webProfile ? 'web' : 'acp', '--dump-config'])
  const artifacts = path.join(work, 'artifacts'); await mkdir(artifacts)
  for (const entry of installed) {
    await cp(path.join(root, 'dist', entry.filename), path.join(artifacts, entry.filename))
    await command(['plugin', '--profile', profile, 'add', 'file:' + path.join(artifacts, entry.filename).replaceAll('\\', '/')])
  }
  await rename(artifacts, artifacts + '.unavailable')
  model = await startMockModel(); platform = await startMockFeishu(features?{}:{tables:[{table_id:'test-table',name:'Todo'},{table_id:'not-registered',name:'Unregistered table'}]})
  if (features) featureMocks = await prepareFeatureMocks(work, env)
  const reservation = createServer(); reservation.listen(0, '127.0.0.1'); await once(reservation, 'listening')
  port = reservation.address().port; await new Promise(resolve => reservation.close(resolve))
  await configuration()
  await writeFile(disabled, JSON.stringify([{ id: 'telebot-feishu', disabled: true }]))
  if (webProfile) {
    const example = await command(['--profile', profile, '--patch', examplePatch, '--dump-config'])
    assert(example.stdout.includes('FEISHU_APP_ID') && example.stdout.includes('telebot-feishu'), 'Official CLI must compose the shipped example patch before runtime evaluation')
  }
  await open('enabled')
  check('packaged Feishu/core/diagnostics load on an external official ACP profile with native webServer')

  assert.deepEqual((await post({ type: 'url_verification', challenge: 'test', token: env.FEISHU_VERIFICATION_TOKEN })).body, { challenge: 'test' })
  assert.equal((await post({ type: 'url_verification', challenge: 'test', token: 'invalid' })).status, 403)
  assert.equal((await post(message('denied', 'hello', { actor: 'ou_stranger' }))).status, 403)
  const wrongApp = message('wrong_app', 'hello'); wrongApp.header.app_id = 'cli_other'
  assert.equal((await post(wrongApp)).status, 403)
  const bot = message('bot', 'hello'); bot.event.sender.sender_type = 'app'
  assert.equal((await post(bot)).body.ignored, true)
  assert.equal(model.requests.length, 0)
  check('real webhook verifies challenge/source/app/admin and ignores bot messages before Agent admission')

  if(progressOnly){
  await post(message('thinking_progress','TELEBOT_TEST:THINKING_ONLY',{chat:'oc_progress'}));await settled('thinking_progress')
  const publications=platform.attempts.filter(row=>row.replyTo==='om_thinking_progress'),creates=publications.filter(row=>row.method==='POST'),edits=publications.filter(row=>row.method==='PUT')
  assert.equal(creates.length,1);assert(edits.length>=1&&edits.length<=6)
  assert(publications.every(row=>row.body.msg_type==='post'&&JSON.parse(row.body.content).zh_cn.content[0][0].tag==='md'))
  assert(edits.every(row=>row.messageId===creates[0].messageId))
  assert(received('thinking_progress').body.content.includes('Keyless response complete.'))
  assert(!JSON.stringify(platform.attempts).includes('PRIVATE_REASONING_FIXTURE'))
  check('one Markdown placeholder is edited in place and becomes the final reply without duplicate cards or private reasoning')
  }

  if(!features&&!progressOnly&&!questionsOnly){
    model.toolCases.set('TABLE_DISCOVERY',{name:'feishu_resources',arguments:{kind:'bitable'},then:{name:'feishu_bitable_inspect',arguments:{resource:'Todo',params_json:'{}'}}})
    await post(message('table_discovery','TELEBOT_TEST:TABLE_DISCOVERY',{chat:'oc_tables'}));await settled('table_discovery')
    const calls=model.requests.filter(row=>row.instruction==='TABLE_DISCOVERY')
    assert(calls.some(row=>row.receipt?.includes('Todo')))
    assert(calls.some(row=>row.receipt?.includes('baseName')))
    assert(!calls.some(row=>row.receipt?.includes('Unregistered table')))
    assert(calls.some(row=>row.receipt?.includes('field_name')))
    assert(!received('table_discovery').body.content.includes('unknown_resource'))
    check('model resolves legacy registered IDs to Todo through the installed inventory tool, excludes unregistered tables and inspects by name without filesystem scans')
  }

  if(questionsOnly){
    const options={chat:'oc_natural_questions'},send=async(id,text)=>{const payload=message(id,text,options);payload.event.message.chat_type='p2p';assert.equal((await post(payload)).status,200);return payload},body=id=>JSON.parse(received(id).body.content).zh_cn.content[0][0].text
    model.toolCases.set('NATURAL_QUESTION',{name:'ask_user_question',arguments:{questions:[{id:'city',question:'城市？',options:[{label:'济南',description:'测试城市'},{label:'北京',description:'另一个测试城市'}],multi_select:false}]}})
    await send('natural_question','TELEBOT_TEST:NATURAL_QUESTION')
    await eventually(()=>platform.sent.some(row=>row.replyTo==='om_natural_question'&&row.body.content.includes('需要你的回答')),15000)
    await send('natural_city','济南');await settled('natural_city');await settled('natural_question')
    assert(body('natural_city').includes('回答已提交'));assert(model.requests.some(row=>row.instruction==='NATURAL_QUESTION'&&row.receipt?.includes('济南')))
    check('plain text answers its identity-bound pending question through the control lane without another model turn')
    await send('blocked_question','TELEBOT_TEST:NATURAL_QUESTION')
    await eventually(()=>platform.sent.some(row=>row.replyTo==='om_blocked_question'&&row.body.content.includes('需要你的回答')),15000)
    await send('new_topic','TELEBOT_TEST:UNPREFIXED_NEW_TOPIC');await settled('new_topic')
    assert.equal(rows("SELECT state FROM inbox WHERE message_id='om_blocked_question'")[0].state,'waiting')
    const original=JSON.parse(rows("SELECT pinned_spec FROM inbox WHERE message_id='om_blocked_question'")[0].pinned_spec)
    const continued=JSON.parse(rows("SELECT pinned_spec FROM inbox WHERE message_id='om_new_topic'")[0].pinned_spec)
    assert.notEqual(original.id,continued.id);assert.equal(continued.parentSessionId,original.id)
    const next=model.requests.find(row=>row.instruction==='UNPREFIXED_NEW_TOPIC');assert(next);assert(JSON.stringify(next.body.messages).includes('城市'))
    await send('plain_greeting','你好');await settled('plain_greeting')
    assert.equal(rows("SELECT state FROM inbox WHERE message_id='om_blocked_question'")[0].state,'waiting')
    await send('late_city','北京');await settled('late_city');await settled('blocked_question')
    assert(body('late_city').includes('回答已提交'));assert.equal(JSON.parse(rows("SELECT pinned_spec FROM inbox WHERE message_id='om_late_city'")[0].pinned_spec).id,original.id)
    check('unprefixed requests and plain greetings continue in an official history-preserving fork while the old card remains answerable in its original session')
    if(webProfile){
      model.toolCases.set('RAW_SCHEDULE_DENY',{name:'schedule_create',arguments:{prompt:'must not arm the foreground',title:'must-not-create',after_seconds:3600}})
      await send('raw_schedule_deny','TELEBOT_TEST:RAW_SCHEDULE_DENY');await settled('raw_schedule_deny')
      assert(model.requests.some(row=>row.instruction==='RAW_SCHEDULE_DENY'&&row.receipt?.includes('feishu_schedule_requires_isolated_task')))
    }
    const task={title:'isolated natural task',prompt:'synthetic future work',mode:'auto',executor:'research',catchUp:'skip',rule:{kind:'once',first:new Date(Date.now()+3600000).toISOString(),timeZone:'Asia/Shanghai',interval:1}}
    model.toolCases.set('NATURAL_TASK',{name:'telebot_schedule',arguments:{action:'create',input_json:JSON.stringify(task)}})
    await send('natural_task','TELEBOT_TEST:NATURAL_TASK');await settled('natural_task');await send('task_confirm','同意');await settled('task_confirm')
    await send('task_list','/schedule list');await settled('task_list');const created=JSON.parse(body('task_list'));assert.equal(created.length,1);assert.equal(created[0].authorized,false)
    model.toolCases.set('NATURAL_AUTHORIZE',{name:'telebot_schedule',arguments:{action:'authorize',input_json:JSON.stringify({task_id:created[0].id})}})
    await send('task_authorize','TELEBOT_TEST:NATURAL_AUTHORIZE');await settled('task_authorize');await send('grant_confirm','同意授权');await settled('grant_confirm')
    await send('task_after','/schedule list');await settled('task_after');assert.equal(JSON.parse(body('task_after'))[0].authorized,true)
    const taskCalls=model.requests.filter(row=>row.instruction==='NATURAL_TASK');assert(taskCalls.length)
    await send('chat_with_future_task','新问题：现在请打个招呼');await settled('chat_with_future_task')
    check('task creation and separate automatic authorization accept trusted natural confirmation; active future tasks leave chat responsive and raw same-session scheduling is denied before execution')
  }

  if(!focused){
  const token = randomUUID(), echo = message('echo', 'TELEBOT_TEST:ECHO:' + token)
  assert.equal((await post(echo)).status, 200)
  const reply = await settled('echo')
  assert(reply.body.content.includes(token))
  assert(model.requests.some(entry => entry.receipt?.includes(token) && JSON.parse(entry.receipt).coreIdentity))
  const firstMapping = rows('SELECT id FROM sessions WHERE key=(SELECT session_key FROM inbox WHERE message_id=?)', 'om_echo')[0].id
  const count = model.requests.length
  assert.equal((await post(echo)).body.duplicate, true)
  echo.header.event_id = 'different_event_same_message'
  assert.equal((await post(echo)).body.duplicate, true)
  assert.equal(model.requests.length, count)
  assert.equal(platform.sent.filter(entry => entry.replyTo === 'om_echo' && !entry.body.content?.includes('"content":"执行进度"')).length, 1)
  check('text executes an actual native tool and replies with its receipt; event/message redelivery cannot execute twice')

  const holdText = 'TELEBOT_TEST:WAIT_FOR_CANCEL:' + randomUUID()
  assert.equal((await post(message('held', holdText))).status, 200)
  await deadline(model.waitFor(entry => entry.instruction === holdText.slice(13)), 'held Feishu request')
  assert.equal((await post(message('queued', 'TELEBOT_TEST:AFTER_CANCEL'))).status, 200)
  assert.equal((await post(message('parallel', 'TELEBOT_TEST:OTHER_CHAT', { chat: 'oc_other', thread: 'omt_thread' }))).status, 200)
  await settled('parallel')
  assert.equal(received('parallel').body.reply_in_thread, true)
  assert(!model.requests.some(entry => entry.instruction === 'AFTER_CANCEL'))
  assert.equal((await post(message('status', '/status'))).status, 200)
  const cardReply = await settled('status')
  assert.equal(cardReply.body.msg_type, 'interactive')
  const card = JSON.parse(cardReply.body.content)
  assert(card.elements[0].text.content.includes('正在处理'))
  check('webhook acknowledges while Agent is held; same-chat queue is serial and another thread runs independently')

  const callback = { schema: '2.0', header: { app_id: 'cli_test', event_type: 'card.action.trigger', event_id: 'card_event', token: env.FEISHU_VERIFICATION_TOKEN },
    event: { operator: { open_id: 'ou_bob' }, context: { open_chat_id: 'oc_main', open_message_id: cardReply.messageId },
      action: { value: { ...card.elements[1].actions[0].value, user_id: 'ou_alice', text: 'TELEBOT_TEST:FORGED', sessionId: firstMapping } } } }
  assert.equal((await post(callback)).status, 403)
  callback.event.operator.open_id = 'ou_alice'; callback.event.context.open_chat_id = 'oc_other'
  assert.equal((await post(callback)).status, 403)
  callback.event.context.open_chat_id = 'oc_main'; callback.event.context.open_message_id = 'om_forged'
  assert.equal((await post(callback)).status, 403)
  callback.event.context.open_message_id = cardReply.messageId
  assert.equal((await post(callback)).status, 200)
  assert((await post(callback)).body.toast.content.includes('已处理'))
  await eventually(() => platform.sent.some(entry => entry.replyTo === cardReply.messageId), 10000)
  assert(!model.requests.some(entry => entry.instruction === 'FORGED'))
  assert.equal((await post(message('stop', '/stop'))).status, 200)
  await settled('stop'); await settled('held'); await settled('queued')
  assert(received('held').body.content.includes('停止'))
  check('status and owned one-use callbacks bypass held work; forged actor/chat/message is refused and /stop cancels the native Agent')

  platform.behaviors.set('om_retry', 'rate-limit-once')
  await post(message('retry', 'TELEBOT_TEST:DELIVERY_RETRY')); await settled('retry')
  const retries = platform.attempts.filter(entry => entry.replyTo === 'om_retry'&&entry.body.msg_type==='post')
  assert.equal(retries.length, 2); assert.equal(retries[0].body.uuid, retries[1].body.uuid)
  assert.equal(model.requests.filter(entry => entry.instruction === 'DELIVERY_RETRY').length, 1)
  platform.behaviors.set('om_unknown', 'disconnect')
  await post(message('unknown', 'TELEBOT_TEST:DELIVERY_UNKNOWN'))
  await eventually(() => rows("SELECT * FROM outbox WHERE reply_to='om_unknown' AND msg_type='post' AND state='unknown'").length === 1, 15000)
  assert.equal(platform.attempts.filter(entry => entry.replyTo === 'om_unknown'&&entry.body.msg_type==='post').length, 1)
  check('confirmed rate limit retries only delivery with the same UUID; lost send receipt becomes unknown without Agent rerun')

  await stop(); const beforeRestart = model.requests.length, sendsBeforeRestart = platform.attempts.length
  encrypted = true; await configuration(); await open('restarted-encrypted')
  assert.equal(model.requests.length, beforeRestart)
  assert.equal(platform.attempts.length, sendsBeforeRestart)
  assert.equal((await post(message('signature', 'hello'), { wrongSignature: true })).status, 403)
  assert.equal((await post(echo)).body.duplicate, true)
  await post(message('resumed', 'TELEBOT_TEST:RESUMED_FEISHU')); await settled('resumed')
  assert.equal(rows('SELECT id FROM sessions WHERE key=(SELECT session_key FROM inbox WHERE message_id=?)', 'om_resumed')[0].id, firstMapping)
  const resumed = model.requests.find(entry => entry.instruction === 'RESUMED_FEISHU')
  assert(JSON.stringify(resumed.body.messages).includes(token))
  assert.equal(platform.attempts.filter(entry => entry.replyTo === 'om_unknown'&&entry.body.msg_type==='post').length, 1)
  check('restart preserves mapping/history and deduplication, never resends unknown delivery, and accepts only valid signed/encrypted input')

  const controlsChat = { chat: 'oc_p2_controls' }
  const beforeControls = model.requests.length
  await post(message('model_list', '/model', controlsChat)); await settled('model_list')
  assert(received('model_list').body.content.includes('telebot-mock-b'))
  await post(message('model_change', '/model telebot-verification/telebot-mock-b', controlsChat)); await settled('model_change')
  assert(received('model_change').body.content.includes('已切换'))
  assert.equal(model.requests.length, beforeControls, 'Model control must not invoke a model')
  await post(message('model_actual', 'TELEBOT_TEST:P2_MODEL_SELECTED', controlsChat)); await settled('model_actual')
  assert.equal(model.requests.find(entry => entry.instruction === 'P2_MODEL_SELECTED').model, 'telebot-mock-b')
  check('P2 /model lists the native catalog and changes the next actual model request without model-assisted command parsing')

  const childWorkspace = path.join(workspace, 'P2 child 中文'); await mkdir(childWorkspace)
  const previousId = rows('SELECT id FROM sessions WHERE key=(SELECT session_key FROM inbox WHERE message_id=?)', 'om_model_change')[0].id
  await post(message('cd_change', '/cd ' + childWorkspace, controlsChat)); await settled('cd_change')
  assert(received('cd_change').body.content.includes('已切换'))
  const changed = rows('SELECT id,spec FROM sessions WHERE key=(SELECT session_key FROM inbox WHERE message_id=?)', 'om_cd_change')[0]
  assert.notEqual(changed.id, previousId); assert.equal(JSON.parse(changed.spec).cwd, childWorkspace)
  model.toolCases.set('P2_WORKSPACE', { name: process.platform === 'win32' ? 'pwsh' : 'bash',
    arguments: { description: 'Verify the native Shell after channel workspace switching',
      command: process.platform === 'win32' ? '(Get-Location).Path' : 'pwd', timeoutMs: 10000 } })
  await post(message('cwd_actual', 'TELEBOT_TEST:P2_WORKSPACE', controlsChat)); await settled('cwd_actual')
  const shellReceipt = model.requests.find(entry => entry.instruction === 'P2_WORKSPACE' && entry.receipt)
  assert(shellReceipt, 'The actual native Shell must return a tool receipt')
  assert(shellReceipt.receipt.replaceAll('\\\\', '/').replaceAll('\\', '/').includes(childWorkspace.replaceAll('\\', '/')))
  await post(message('cd_denied', '/cd ' + work, controlsChat)); await settled('cd_denied')
  assert(received('cd_denied').body.content.includes('被拒绝'))
  assert.equal(rows('SELECT id FROM sessions WHERE key=(SELECT session_key FROM inbox WHERE message_id=?)', 'om_cd_denied')[0].id, changed.id)
  await stop(); await open('p2-restarted')
  await post(message('model_persisted', 'TELEBOT_TEST:P2_MODEL_PERSISTED', controlsChat)); await settled('model_persisted')
  assert.equal(model.requests.find(entry => entry.instruction === 'P2_MODEL_PERSISTED').model, 'telebot-mock-b')
  await post(message('cwd_persisted', '/cd', controlsChat)); await settled('cwd_persisted')
  assert(received('cwd_persisted').body.content.includes('P2 child'))
  check('P2 /cd creates a native workspace session, changes actual Shell cwd, rejects root escape and persists cwd/model through restart')

  if (features) {
    await verifyFeatures({ post, message, settled, received, platform, model, workspace, rows, check })
    await stop(); featureDisabled = true; await configuration(); await open('business-disabled')
    const session = await acp.request(methods.agent.session.new, { cwd: workspace, mcpServers: [] })
    const before = model.requests.length
    await acp.prompt(session.sessionId, 'TELEBOT_TEST:BUSINESS_DISABLED')
    const disabledNames = model.requests.slice(before).flatMap(row => row.names)
    assert(!disabledNames.some(name => ['ask_adviser','telebot_codex','write_report','publish_source','telebot_schedule','telebot_task_read','telebot_task_list'].includes(name) || name.startsWith('feishu_')))
    await post(message('main_after_feature_disable', 'TELEBOT_TEST:MAIN_AFTER_FEATURE_DISABLE', { chat: 'oc_independent' })); await settled('main_after_feature_disable')
    await acp.request(methods.agent.session.close, { sessionId: session.sessionId })
    check('optional Codex/adviser and integrated Feishu business tools disable while the unified Feishu channel keeps running')
  }

  for (const id of ['crash_a', 'crash_b']) {
    const instruction = 'WAIT_FOR_CANCEL:' + id
    await post(message(id, 'TELEBOT_TEST:' + instruction, { chat: 'oc_' + id }))
    await deadline(model.waitFor(entry => entry.instruction === instruction), 'active work before crash')
  }
  await post(message('durable_pending', 'TELEBOT_TEST:DURABLE_PENDING', { chat: 'oc_pending' }))
  assert.equal(rows("SELECT state FROM inbox WHERE message_id='om_durable_pending'")[0].state, 'pending')
  await acp.crash(); acp = undefined
  const crashRequests = model.requests.length
  // The native process was killed, so nobody can release its renewable database lease.
  const leaseExpiry = rows('SELECT expires FROM lease')[0].expires
  await new Promise(resolve => setTimeout(resolve, Math.max(0, leaseExpiry - Date.now() + 100)))
  await open('after-forced-crash')
  await settled('durable_pending')
  await eventually(() => rows("SELECT * FROM inbox WHERE message_id IN ('om_crash_a','om_crash_b') AND state!='running'").length === 2, 15000)
  for (const id of ['crash_a', 'crash_b']) {
    assert.equal(model.requests.filter(entry => entry.instruction === 'WAIT_FOR_CANCEL:' + id).length, 1, 'recovery must never resubmit interrupted work')
    assert(['unknown', 'cancelled', 'failed'].includes(rows('SELECT state FROM inbox WHERE message_id=?', 'om_' + id)[0].state))
  }
  assert.equal(model.requests.length, crashRequests + 1)
  assert.equal((await post(message('durable_pending', 'TELEBOT_TEST:DURABLE_PENDING', { chat: 'oc_pending' }))).body.duplicate, true)
  check('forced process termination recovers durable pending work and interrupted native receipts without resubmitting running requests')

  const heldReply = 'crash_send'
  platform.behaviors.set('om_' + heldReply, 'hold')
  await post(message(heldReply, 'TELEBOT_TEST:CRASH_DURING_SEND'))
  await eventually(() => platform.attempts.some(entry => entry.replyTo === 'om_' + heldReply&&entry.body.msg_type==='post'), 15000)
  assert.equal(rows("SELECT state FROM outbox WHERE reply_to=? AND msg_type='post'", 'om_' + heldReply)[0].state, 'sending')
  await acp.crash(); acp = undefined
  const sendLeaseExpiry = rows('SELECT expires FROM lease')[0].expires
  await new Promise(resolve => setTimeout(resolve, Math.max(0, sendLeaseExpiry - Date.now() + 100)))
  await open('after-send-crash')
  assert.equal(rows("SELECT state FROM outbox WHERE reply_to=? AND msg_type='post'", 'om_' + heldReply)[0].state, 'unknown')
  assert.equal(platform.attempts.filter(entry => entry.replyTo === 'om_' + heldReply&&entry.body.msg_type==='post').length, 1)
  check('forced termination during remote send preserves an unknown outcome instead of duplicating the message on restart')
  }

  await stop()
  await open('disabled', true)
  const response = await fetch('http://127.0.0.1:' + port + '/feishu/webhook', { method: 'POST', body: '{}' })
  assert.equal(response.status, 404)
  await stop()
  check('disabled bundle unregisters webhook and worker while the official carrier remains alive')
  for (const entry of [...installed].reverse().filter(entry => entry.name !== '@telebot/core')) await command(['plugin', '--profile', profile, 'remove', entry.name])
  await command(['plugin', '--profile', profile, 'remove', '@telebot/core'])
  const pkg = JSON.parse(await readFile(path.join(home, 'profiles', profile, 'package.json'), 'utf8'))
  for (const name of ['@telebot/feishu', '@telebot/core', '@telebot/diagnostics']) assert(!pkg.dependencies?.[name])
  assert(rows('SELECT * FROM sessions').length >= (focused?1:2))
  check('official uninstall removes all bundles and preserves channel business data')
  evidence.status = 'passed'
} catch (error) { evidence.status = 'failed'; evidence.error = error.message; throw error }
finally {
  if (acp) await acp.stop().catch(() => {})
  await featureMocks?.close()
  if (model) await model.close()
  if (platform) await platform.close()
  evidence.modelRequests = model?.requests.length ?? 0
  evidence.deliveryAttempts = platform?.attempts.length ?? 0
  const report = path.join(work, 'result.json')
  await writeFile(report, JSON.stringify(evidence, null, 2) + '\n')
  await mkdir(path.join(root, '.runtime'), { recursive: true })
  await cp(report, path.join(root, questionsOnly ? (webProfile?'.runtime/verify-feishu-questions-web-latest.json':'.runtime/verify-feishu-questions-latest.json') : progressOnly ? (webProfile?'.runtime/verify-feishu-progress-web-latest.json':'.runtime/verify-feishu-progress-latest.json') : resourcesOnly ? (webProfile?'.runtime/verify-feishu-resources-web-latest.json':'.runtime/verify-feishu-resources-latest.json') : features ? (webProfile ? '.runtime/verify-features-web-latest.json' : '.runtime/verify-features-latest.json') : (webProfile ? '.runtime/verify-feishu-web-latest.json' : '.runtime/verify-feishu-latest.json')))
  console.log('Evidence: ' + report)
  await cleanupVerification(work, evidence.status === 'passed')
}
