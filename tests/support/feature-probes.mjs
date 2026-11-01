import {DatabaseSync} from 'node:sqlite'
import {startMockGitHttp} from './mock-git-http.mjs'
import assert from 'node:assert/strict'
import { writeFile, mkdir, readFile } from 'node:fs/promises'
import {execFile} from 'node:child_process'
import {promisify} from 'node:util'
import path from 'node:path'
import { eventually } from './temp-dir.mjs'

export async function prepareFeatureMocks(work, env) {
  const cli = path.join(work, 'mock-codex.cjs'), adviser = path.join(work, 'mock-opencli.cjs')
  await writeFile(adviser, "console.log('response: isolated adviser answer')")
  await writeFile(cli, `const r=require('readline').createInterface({input:process.stdin});const send=x=>console.log(JSON.stringify(x));let seq=0;const threadId='mock-native-thread';r.on('line',line=>{const m=JSON.parse(line);if(m.id===undefined)return;let result={};if(m.method==='initialize')result={userAgent:'codex-cli/0.160.0 loopback fixture'};if(m.method==='permissionProfile/list')result={data:[{id:':read-only',allowed:true},{id:':workspace',allowed:true}]};if(m.method==='thread/start'||m.method==='thread/resume')result={thread:{id:threadId,cwd:process.cwd()},sandbox:{type:(m.params.permissions===':workspace'||m.params.sandbox==='workspace-write'||m.params.sandboxPolicy?.type==='workspaceWrite')?'workspaceWrite':'readOnly'},activePermissionProfile:{id:m.params.permissions}};if(m.method==='turn/start'){const id='turn-'+(++seq),artifacts=(m.params.permissions===':workspace'||m.params.sandbox==='workspace-write'||m.params.sandboxPolicy?.type==='workspaceWrite')?['codex-artifact.txt']:[];if(artifacts.length)require('fs').writeFileSync(artifacts[0],'isolated verified artifact');send({id:m.id,result:{turn:{id,status:'inProgress'}}});send({method:'item/completed',params:{threadId,item:{type:'agentMessage',text:JSON.stringify({summary:'isolated native connector result',artifacts})}}});send({method:'turn/completed',params:{threadId,turn:{id,status:'completed'}}});return;}if(m.method==='thread/list')result={data:[]};send({id:m.id,result});});`)
  const repo=path.join(work,'workspace 中文','publish-repo'),posts=path.join(repo,'posts');await mkdir(posts,{recursive:true})
  const execute=promisify(execFile),git=async args=>execute('git',args,{cwd:repo,windowsHide:true})
  await git(['init','--initial-branch=main']);await git(['config','user.name','Telebot fixture']);await git(['config','user.email','test@example.invalid']);await writeFile(path.join(repo,'seed.txt'),'fixture');await git(['add','seed.txt']);await git(['commit','-m','fixture']);await git(['init','--bare',path.join(repo,'.git','remote.git')]);await git(['--git-dir',path.join(repo,'.git','remote.git'),'config','http.receivepack','true']);const gitHttp=await startMockGitHttp(path.join(repo,'.git','remote.git'),env);await git(['remote','add','origin',gitHttp.url])
  return { cli, adviser,repo,posts,close:()=>gitHttp.close() }
}

export async function verifyFeatures({ post, message, settled, received, platform, model, workspace, home, rows, check }) {
  const options = { chat: 'oc_features' }
  const readRows=(sql,...args)=>{const db=new DatabaseSync(path.join(home,'plugins/telebot-automation/automation.sqlite'),{readOnly:true});try{return db.prepare(sql).all(...args)}finally{db.close()}}
  const tokenOf=text=>{assert(!text.trim().startsWith('{'),text);const token=text.match(/\/(?:publish|schedule|bitable) confirm ([a-f0-9-]{36})/)?.[1];assert(token,text);return token}
  const latestTask=()=>readRows('SELECT id FROM tasks ORDER BY rowid DESC LIMIT 1')[0].id
  const historyOf=id=>readRows('SELECT * FROM runs WHERE task=? ORDER BY planned',id)

  const send = async (id, text, extra = options) => { const payload = message(id, text, extra); payload.event.message.chat_type = 'p2p'; assert.equal((await post(payload)).status, 200); await settled(id); return received(id) }
  await send('adviser_direct', '/adviser isolated question')
  assert(received('adviser_direct').body.content.includes('isolated adviser answer'))
  await send('codex_direct', '/codex inspect isolated code request')
  assert(received('codex_direct').body.content.includes('isolated native connector result'))
  const proposal = await send('codex_propose', '/codex apply isolated change')
  const token = JSON.parse(proposal.body.content).zh_cn.content[0][0].text.match(/\/codex confirm ([a-f0-9-]+)/)?.[1]
  assert(token)
  const uploadsBeforeAlias=platform.uploads.length
  model.toolCases.set('CODEX_ALIAS_ATTACK',{name:'telebot_codex',arguments:{mode:'apply',prompt:token}})
  await send('codex_alias_denied','TELEBOT_TEST:CODEX_ALIAS_ATTACK')
  assert(model.requests.some(row=>row.instruction==='CODEX_ALIAS_ATTACK'&&row.receipt?.includes('apply_token_requires_explicit_confirm')))
  assert.equal(platform.uploads.length,uploadsBeforeAlias)
  check('P4 actual model apply-token call cannot consume a trusted user grant; explicit confirmation remains required')
  await send('codex_confirm', '/codex confirm ' + token)
  assert(received('codex_confirm').body.content.includes('isolated native connector result'))
  await eventually(() => platform.sent.some(row => row.replyTo === 'om_codex_confirm' && row.body.msg_type === 'file'), 15000)
  const uploadsAfterCoding = platform.uploads.length
  await send('codex_repeat', '/codex confirm ' + token)
  assert.equal(platform.uploads.length, uploadsAfterCoding)
  check('P4 installed Codex/adviser bundles use trusted identity, one-use apply and verified artifact delivery without paid APIs')

  const bodyOf=id=>JSON.parse(received(id).body.content).zh_cn.content[0][0].text
  await send('report_new','/report '+JSON.stringify({filename:'new-report.md',content:'synthetic source-preserving report'}))
  assert(bodyOf('report_new').includes('报告已新建'))
  await eventually(()=>platform.sent.some(row=>row.replyTo==='om_report_new'&&row.body.msg_type==='file'),15000)
  const publicationBody='Synthetic original body\r\n  preserved whitespace  \r\n';await writeFile(path.join(workspace,'original.md'),publicationBody)
  await send('publication_plan','/publish publish '+JSON.stringify({items:['one','two'].map(slug=>({source:'original.md',slug,title:slug,description:'synthetic',published:'2026-10-31'}))}))
  const publication={token:tokenOf(bodyOf('publication_plan'))};assert(bodyOf('publication_plan').includes('原文发布'));await send('publication_confirm','/publish confirm '+publication.token)
  for(const slug of ['one','two'])assert((await readFile(path.join(workspace,'publish-repo/posts',slug+'.md'),'utf8')).endsWith(publicationBody))
  await send('git_plan','/publish git '+JSON.stringify({files:['posts/one.md','posts/two.md'],message:'synthetic batch publication'}))
  const gitPlan={token:tokenOf(bodyOf('git_plan'))};assert(bodyOf('git_plan').includes('提交并推送'));await send('git_confirm','/publish confirm '+gitPlan.token);assert(bodyOf('git_confirm').includes('已完成'),bodyOf('git_confirm'))
  const execute=promisify(execFile),repo=path.join(workspace,'publish-repo'),remote=path.join(repo,'.git/remote.git'),head=(await execute('git',['rev-parse','HEAD'],{cwd:repo,windowsHide:true})).stdout.trim(),remoteHead=(await execute('git',['--git-dir',remote,'rev-parse','refs/heads/main'],{cwd:repo,windowsHide:true})).stdout.trim();assert.equal(head,remoteHead)
  check('P5 source-preserving batch publication and separately confirmed specific-file Git push execute through the installed official Shell against a loopback HTTP bare fixture')
  const task={title:'synthetic native-clock coding',prompt:'isolated native scheduled change',mode:'auto',executor:'codex_apply',catchUp:'run_once',rule:{kind:'once',first:new Date(Date.now()+3000).toISOString(),timeZone:'Asia/Shanghai',interval:1}}
  await send('task_propose','/schedule create '+JSON.stringify(task))
  const taskProposal={token:tokenOf(bodyOf('task_propose'))};assert(bodyOf('task_propose').includes('尚未执行'))
  await send('task_create','/schedule confirm '+taskProposal.token)
  const taskId=latestTask();assert(bodyOf('task_create').includes(taskId))
  await send('task_list_before_auth','/schedule list')
  assert(bodyOf('task_list_before_auth').includes('等待自动执行授权'));assert.equal(readRows('SELECT state FROM tasks WHERE id=?',taskId)[0].state,'awaiting_authorization')
  await send('task_auth','/schedule authorize '+taskId)
  const grant={token:tokenOf(bodyOf('task_auth'))}
  await send('task_auth_confirm','/schedule confirm '+grant.token)
  let history
  await eventually(async()=>{await new Promise(resolve=>setTimeout(resolve,250));await send('task_history_'+Date.now(),'/schedule history '+taskId);const posts=platform.sent.filter(row=>row.replyTo.startsWith('om_task_history_'));try{assert(!JSON.parse(posts.at(-1).body.content).zh_cn.content[0][0].text.trim().startsWith('['));history=historyOf(taskId);return history.some(row=>row.state==='completed')}catch{return false}},20000)
  assert.equal(history.length,1)
  await eventually(()=>platform.sent.some(row=>row.replyTo==='om_task_create'&&row.body.msg_type==='file'),15000)
  await send('task_repeat_grant','/schedule confirm '+grant.token)
  await send('task_history_final','/schedule history '+taskId)
  assert.equal(historyOf(taskId).length,1)
  check('P5/P6 installed reports and separately authorized automation use official one-shot timing, a structured receipt, one coding execution and durable artifact delivery')
  model.toolCases.set('TASK_REPORT',{name:'web_fetch',arguments:{url:'https://fixture.invalid/research'},then:{name:'write_report',arguments:{filename:'task-research.md',content:'synthetic-web-evidence report'}}})
  const researchTask={...task,title:'restricted research',prompt:'TELEBOT_TEST:TASK_REPORT',mode:'confirm',executor:'research',rule:{...task.rule,first:new Date(Date.now()+1500).toISOString()}}
  await send('research_propose','/schedule create '+JSON.stringify(researchTask));const researchProposal={token:tokenOf(bodyOf('research_propose'))}
  await send('research_create','/schedule confirm '+researchProposal.token);const researchId=latestTask()
  let researchRun
  await eventually(async()=>{await new Promise(resolve=>setTimeout(resolve,250));const id='research_history_'+Date.now();await send(id,'/schedule history '+researchId);researchRun=historyOf(researchId)[0];return researchRun?.state==='awaiting_auth'},15000)
  await send('research_execute','/schedule execute '+researchRun.id)
  await eventually(()=>platform.sent.some(row=>row.replyTo==='om_research_create'&&row.body.msg_type==='file'),20000)
  const researchRequests=model.requests.filter(row=>row.instruction==='TASK_REPORT');assert(researchRequests.length>=3);assert(researchRequests.some(row=>row.receipt?.includes('synthetic-web-evidence')));assert(researchRequests.every(row=>row.names.includes('web_fetch')&&row.names.includes('web_search')))
  assert(researchRequests.every(row=>!row.names.some(name=>['bash','pwsh','shell','telebot_codex','publish_source','telebot_schedule','skill'].includes(name)||name.startsWith('feishu_'))))
  check('P6 per-occurrence confirmed research runs the official Agent with restricted tools and delivers a hash-verified new report without Shell, MCP or publication')

  await send('bitable_preview', '/feishu ' + JSON.stringify({ resource: 'test-table', action: 'create', params: { records: [{ fields: { name: 'isolated row' } }] } }))
  const plan = {token:tokenOf(bodyOf('bitable_preview'))}
  assert(bodyOf('bitable_preview').includes('尚未执行')); assert.equal(platform.records.size, 0)
  await send('bitable_confirm', '/feishu confirm ' + plan.token)
  assert(received('bitable_confirm').body.content.includes('已完成')); assert.equal(platform.records.size, 1)
  await send('bitable_repeat', '/feishu confirm ' + plan.token); assert.equal(platform.records.size, 1)
  check('P3 installed Bitable bundle freezes a preview, writes only on the trusted confirmation and verifies remote readback without duplicate records')

  const event = { summary: 'isolated calendar event', start_time: { timestamp: '1800000000', timezone: 'Asia/Shanghai' }, end_time: { timestamp: '1800003600', timezone: 'Asia/Shanghai' } }
  const calendar = async (id, action, params) => {
    await send(id, '/feishu ' + JSON.stringify({ resource: 'test-calendar', action, params }))
    return action==='query'?bodyOf(id):{token:tokenOf(bodyOf(id))}
  }
  const create = await calendar('calendar_create', 'create', { event, notify: false })
  assert(bodyOf('calendar_create').includes('尚未执行')); assert.equal(platform.calendarEvents.size, 0)
  await send('calendar_confirm', '/feishu confirm ' + create.token)
  assert.equal(platform.calendarEvents.size, 1)
  const eventId = [...platform.calendarEvents.keys()][0]
  const listing = await calendar('calendar_read', 'query', { start_time: '1799999000', end_time: '1800004000' })
  assert(JSON.stringify(listing).includes(eventId))
  const update = await calendar('calendar_update', 'update', { event_id: eventId, event: { ...event, summary: 'updated' }, notify: false, recurrence_scope: 'all' })
  await send('calendar_update_confirm', '/feishu confirm ' + update.token)
  assert.equal(platform.calendarEvents.get(eventId).summary, 'updated')
  const deletion = await calendar('calendar_delete', 'delete', { event_id: eventId, notify: false, recurrence_scope: 'all' })
  await send('calendar_delete_confirm', '/feishu confirm ' + deletion.token)
  assert.equal(platform.calendarEvents.size, 0)
  check('P3 calendar CRUD uses the installed application token and owned write confirmation without an OAuth callback or user token')

  const pending=await send('base_pending','/feishu '+JSON.stringify({resource:'test-table',action:'create',params:{records:[{fields:{name:'must-not-write-after-unlink'}}]}}))
  const pendingToken=tokenOf(bodyOf('base_pending'))
  const beforeManagement=model.requests.length
  await send('base_remove','移除多维表格 https://fixture.feishu.cn/base/test-base')
  assert(received('base_remove').body.content.includes('未删除'));assert.equal(platform.records.size,1)
  await send('base_add','添加多维表格链接 https://fixture.feishu.cn/base/test-base')
  assert(received('base_add').body.content.includes('已接入'))
  await send('base_stale_confirm','/feishu confirm '+pendingToken)
  assert(received('base_stale_confirm').body.content.includes('已取消'));assert.equal(platform.records.size,1)
  await send('base_list','查看已接入的多维表格链接')
  assert(received('base_list').body.content.includes('test-base'))
  assert.equal(model.requests.length,beforeManagement)
  check('Base links add/remove through trusted natural language without a model; unlink preserves remote records and invalidates pending writes')

  // The actual native tool invokes the official question service; the channel
  // answer must bypass its still-running Agent without releasing a foreign Actor.
  model.toolCases.set('P2_QUESTION', { name: 'ask_user_question', arguments: { questions: [{ id: 'choice', question: 'Choose the isolated option', options: [{ label: 'one', description: 'First' }, { label: 'two', description: 'Second' }], multi_select: false }] } })
  const questionInput = message('native_question', 'TELEBOT_TEST:P2_QUESTION', { chat: 'oc_questions' }); questionInput.event.message.chat_type = 'p2p'
  await post(questionInput)
  let card
  await eventually(() => { card = platform.sent.find(row => row.replyTo === 'om_native_question' && row.body.msg_type === 'interactive' && row.body.content.includes('需要你的回答')); return card }, 20000)
  const content = JSON.parse(card.body.content), questionToken = content.elements[1].actions[0].value.token
  await send('question_forged', '/answer ' + questionToken + ' one', { chat: 'oc_questions', actor: 'ou_bob' })
  assert(received('question_forged').body.content.includes('失效'))
  await send('question_answer', '/answer ' + questionToken + ' one', { chat: 'oc_questions' })
  await eventually(() => platform.sent.some(row => row.replyTo === 'om_native_question' && row.body.msg_type === 'post'), 15000)
  check('P2 native questions render through the channel and accept only the original user answer while the model turn is waiting')

  platform.resources.set('test-file', { data: Buffer.from('attachment text'), mediaType: 'text/plain' })
  const upload = message('file_in', '', { chat: 'oc_files' }); upload.event.message.message_type = 'file'; upload.event.message.content = JSON.stringify({ file_key: 'test-file', file_name: 'input.txt' })
  await post(upload); await settled('file_in'); assert(received('file_in').body.content.includes('24'))
  const artifact = path.join(workspace, 'outgoing.txt'); await writeFile(artifact, 'verified outbound')
  const uploadsBeforeFile = platform.uploads.length
  await send('file_out', '/send ' + artifact, { chat: 'oc_files' })
  assert.equal(received('file_out').body.msg_type, 'file'); assert.equal(platform.uploads.length, uploadsBeforeFile + 1)
  check('P2 real webhook stages a bound file and the installed channel uploads an explicitly selected workspace artifact with a frozen hash')

  const before = platform.sent.length
  await send('remind_create', '/remind create {"text":"isolated reminder","after_seconds":1}', { chat: 'oc_reminders' })
  assert(received('remind_create').body.content.includes('计划时间'))
  await eventually(() => platform.sent.slice(before).some(row => row.body.content.includes('定时提醒：isolated reminder')), 15000)
  check('P2 plain reminders use official timing with a tool-restricted native session and durable channel delivery')
}
