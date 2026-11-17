import test from 'node:test'
import assert from 'node:assert/strict'
import path from 'node:path'
import {mkdir,writeFile,readFile,symlink,utimes} from 'node:fs/promises'
import {RunSkills} from '../../plugins/codex/lib/adapters/skills.js'
import {CodingService} from '../../plugins/codex/lib/adapters/service.js'
import {temporaryDirectory,cleanup} from '../support/temp-dir.mjs'
test('selected Skills freeze source hashes, run copies retain state, changed source/program is refused and original config stays intact',async t=>{
  const root=await temporaryDirectory(t),source=path.join(root,'original-skills'),cwd=path.join(root,'workspace')
  await mkdir(path.join(source,'report'),{recursive:true});await mkdir(cwd);await writeFile(path.join(source,'report/SKILL.md'),'# synthetic skill\nUse only this local run copy.')
  const skills=new RunSkills(source,['report']),snapshot=await skills.snapshot(),copy=await skills.prepare(cwd,snapshot);assert(copy.startsWith(cwd));assert.equal(await readFile(path.join(copy,'report/SKILL.md'),'utf8'),await readFile(path.join(source,'report/SKILL.md'),'utf8'))
  await writeFile(path.join(copy,'report/dedup-state.json'),'{"seen":["synthetic"]}');assert.equal(await skills.prepare(cwd,snapshot),copy);assert((await readFile(path.join(copy,'report/dedup-state.json'),'utf8')).includes('synthetic'))
  await writeFile(path.join(source,'report/SKILL.md'),'changed source');await assert.rejects(skills.prepare(cwd,snapshot),/changed_new_confirmation/)
  assert.equal(await readFile(path.join(copy,'report/SKILL.md'),'utf8'),'# synthetic skill\nUse only this local run copy.')
  await assert.rejects(new RunSkills(source,['../outside']).snapshot(),/invalid_selected/)
})
test('Codex UI execution limit reaches the actual executor and scheduled dispatch keys cannot execute twice',async t=>{
  const cwd=await temporaryDirectory(t),actor={userId:'alice',chatId:'chat',sessionId:'s',cwd,privateChat:true,origin:'feishu'};let calls=0
  const backend={enforcesReadOnly:true,async execute(_request,options){calls++;await new Promise(resolve=>options.signal.addEventListener('abort',resolve,{once:true}));return{status:'failed',text:'stopped by configured deadline',artifacts:[]}},async dispose(){}}
  const service=new CodingService(backend,path.join(cwd,'coding.sqlite'),['alice'],[cwd],'alice',30,900000);cleanup(t,()=>service.dispose())
  const started=Date.now(),timer=setTimeout(()=>{},2000);try{assert.equal((await service.command(actor,'inspect synthetic slow task',AbortSignal.timeout(5000))).status,'failed');assert(Date.now()-started<1500)}finally{clearTimeout(timer)}
  const keepAlive=setTimeout(()=>{},2000);try{await service.scheduled(actor,'synthetic bounded task','same-execution',AbortSignal.timeout(5000));const count=calls;await service.scheduled(actor,'synthetic bounded task','same-execution',AbortSignal.timeout(5000));assert.equal(calls,count)}finally{clearTimeout(keepAlive)}
})

test('Feishu private history works without workspace grants and keeps owner and execution checks', async t => {
  const root = await temporaryDirectory(t)
  const actor = { userId: 'alice', chatId: 'chat', sessionId: 's', cwd: root, privateChat: true, origin: 'feishu' }
  const thread = { id: '12345678-1234-1234-1234-123456789abc', name: '个人会话', cwd: root }
  const requests = []
  const client = { async request(method, params) {
    requests.push(method)
    if (method === 'thread/list') return { data: params.archived ? [] : [thread] }
    if (method === 'thread/read') return { thread }
    if (method === 'thread/turns/list') return { data: [] }
    assert.fail('Unexpected history RPC: ' + method)
  } }
  const backend = {
    async connect(cwd) { assert.equal(cwd, undefined); return client },
    async release() {}, async dispose() {},
  }
  const service = new CodingService(backend, path.join(root, 'coding.sqlite'), ['alice', 'bob'], [], 'alice')
  cleanup(t, () => service.dispose())
  const signal = AbortSignal.timeout(5000)

  const history = await service.command(actor, 'history', signal)
  assert.equal(history.status, 'completed')
  assert.match(history.text, /个人会话/)
  assert.equal(history.card.actions[0].command, 'read ' + thread.id)
  const read = await service.command({ ...actor, cwd: path.join(root, 'unavailable') }, 'read ' + thread.id, signal)
  assert.equal(read.status, 'completed')
  assert(read.text.includes(thread.id))

  for (const result of [
    await service.command({ ...actor, privateChat: false }, 'history', signal),
    await service.command({ ...actor, userId: 'bob' }, 'read ' + thread.id, signal),
  ]) {
    assert.equal(result.status, 'failed')
    assert.match(result.text, /本人私聊/)
  }
  assert.equal((await service.command(actor, 'resume ' + thread.id + ' continue', signal)).status, 'failed')
  const rejected = await service.command({ ...actor, sourceId: 'explicit' }, 'resume ' + thread.id + ' continue', signal)
  assert.equal(rejected.status, 'failed')
  assert.match(rejected.text, /允许工作目录/)
  await assert.rejects(service.command(actor, 'apply change files', signal), /coding_workspace_not_allowed/)
  assert.deepEqual(requests, ['thread/list', 'thread/read', 'thread/turns/list', 'thread/read', 'thread/list'])
})

test('Feishu numbered history resumes the selected native thread once in its own allowed workspace', async t => {
  const root = await temporaryDirectory(t)
  const cwd = path.join(root, 'original-project')
  await mkdir(cwd)
  const launcher = path.join(root, 'start-desktop.ps1')
  const oldFile = path.join(cwd, 'old.txt')
  await writeFile(launcher, 'existing launcher')
  await writeFile(oldFile, 'existing workspace file')
  await utimes(oldFile, new Date(0), new Date(0))
  const actor = { userId: 'alice', chatId: 'chat', sessionId: 's', cwd: path.join(root, 'other-session'), privateChat: true, origin: 'feishu' }
  const thread = { id: '12345678-1234-1234-1234-123456789abc', name: '排障任务', cwd }
  const lists = [], executions = []
  let archived = false, busy = false
  const client = { async request(method, params) {
    if (method === 'thread/list') {
      lists.push(params)
      return { data: Boolean(params.archived) === archived ? [thread] : [], nextCursor: params.cursor ? null : 'opaque-cursor' }
    }
    if (method === 'thread/read') return { thread: { ...thread, archived } }
    if (method === 'thread/turns/list') return { data: [{ items: [{ type: 'userMessage', content: [{ type: 'text', text: '用户任务' }] }, { type: 'agentMessage', text: '助手答复' }, { type: 'commandExecution', command: 'private tool output' }] }] }
    assert.fail('Unexpected history RPC: ' + method)
  } }
  const backend = {
    async connect() { return client }, async release() {}, async dispose() {},
    async execute(request, options, threadId) {
      executions.push({ request, sandbox: options.sandbox, threadId })
      if (busy) return { status: 'failed', text: '原会话被占用', diagnostic: 'codex_thread_locked' }
      await writeFile(path.join(cwd, 'result.txt'), 'generated this turn')
      return { status: 'completed', text: '重启未完成，系统拒绝访问。', threadId, artifacts: [launcher, 'old.txt', 'missing.txt', 'result.txt'] }
    },
  }
  const service = new CodingService(backend, path.join(root, 'coding.sqlite'), ['alice'], [cwd], 'alice')
  cleanup(t, () => service.dispose())
  const signal = AbortSignal.timeout(5000)

  assert.match((await service.command(actor, 'history 排障', signal)).text, /1\. .*排障任务/)
  assert.deepEqual(lists[0], { limit: 10, archived: false, searchTerm: '排障', sortKey: 'updated_at', sortDirection: 'desc', sourceKinds: ['cli', 'vscode', 'exec', 'appServer'], modelProviders: [] })
  await service.command(actor, 'history --next', signal)
  assert.equal(lists[1].cursor, 'opaque-cursor')
  assert.equal(lists[1].searchTerm, '排障')
  const detail = await service.command(actor, 'history 1', signal)
  assert.match(detail.text, /用户任务[\s\S]*助手答复/)
  assert.doesNotMatch(detail.text, /private tool output/)
  const selection = await service.command(actor, detail.card.actions.find(action => action.label === '继续此会话').command, signal)
  assert.equal(selection.card.replyCommand, 'resume ' + thread.id)
  assert.match(selection.text, /直接发送一条文字任务/)
  assert.equal(executions.length, 0)
  for (let i = 0; i < 2; i++) {
    const result = await service.command({ ...actor, sourceId: 'resume-request' }, 'resume 1 修复问题', signal)
    assert.equal(result.status, 'completed', result.text)
    const output = JSON.parse(result.text)
    assert.equal(output.text, '重启未完成，系统拒绝访问。')
    assert.deepEqual(output.artifacts.map(artifact => path.basename(artifact.path)), ['result.txt'])
    assert.equal(output.artifacts[0].sha256.length, 64)
  }
  assert.equal(executions.length, 1)
  assert.equal(executions[0].request.cwd, cwd)
  assert.equal(executions[0].sandbox, 'workspace-write')
  assert.equal(executions[0].threadId, thread.id)
  archived = true
  assert.match((await service.command(actor, 'history --archived 排障', signal)).text, /排障任务/)
  const rejected = await service.command({ ...actor, sourceId: 'archived-request' }, 'resume 1 修复问题', signal)
  assert.equal(rejected.status, 'failed')
  assert.match(rejected.text, /已归档/)
  assert.equal(executions.length, 1)

  archived = false
  busy = true
  const locked = { ...actor, sourceId: 'locked-request' }
  const resume = 'resume ' + thread.id + ' 修复问题'
  assert.equal((await service.command(locked, resume, signal)).status, 'failed')
  assert.equal((await service.command(locked, resume, signal)).status, 'failed')
  assert.equal(executions.length, 2)
})
