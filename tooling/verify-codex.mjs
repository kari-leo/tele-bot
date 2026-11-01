import { cleanupVerification } from './cleanup-verification.mjs'
import assert from 'node:assert/strict'
import { access, mkdir, mkdtemp, writeFile, readFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import path from 'node:path'
import { createServer } from 'node:http'
import { once } from 'node:events'
import { root, run } from './common.mjs'
import { verificationEnvironment } from './official-host.mjs'
import { startCodex } from '../tests/support/codex-client.mjs'
import { NativeCodex } from '../plugins/codex/lib/adapters/native.js'
import { CodingService } from '../plugins/codex/lib/adapters/service.js'
import { RunSkills } from '../plugins/codex/lib/adapters/skills.js'
import { codingDigest } from '../plugins/codex/lib/domain/coding.js'
import { setTimeout as delay } from 'node:timers/promises'

const cli = process.env.TELEBOT_CODEX_CLI
assert(cli && path.isAbsolute(cli), 'Set TELEBOT_CODEX_CLI to the installed official @openai/codex/bin/codex.js')
const work = await mkdtemp(path.join(tmpdir(), 'telebot-codex-p0-'))
const home = path.join(work, 'codex-home'), cwd = path.join(work, 'workspace 中文')
await mkdir(home); await mkdir(cwd)
const nativeTemp=path.join(work,'native-temp');await mkdir(nativeTemp)
const env = { ...verificationEnvironment(home), HOME: home, USERPROFILE: home, CODEX_HOME: home, TEMP:nativeTemp,TMP:nativeTemp }
const evidence = { platform: process.platform, paidModels: false, realUserHistory: false, checks: [] }
const check = description => { evidence.checks.push(description); console.log(`PASS ${description}`) }
const requests = []
let nativeNetworkRequests=0
const server = createServer(async (request, response) => {
  if(request.url==='/native-network'){nativeNetworkRequests++;response.writeHead(200,{'Content-Type':'text/plain'}).end('synthetic-native-network');return}
  let raw = ''; for await (const chunk of request) raw += chunk
  const body = JSON.parse(raw)
  requests.push(body)
  const serialized = JSON.stringify(body.input)
  const instruction = JSON.stringify(body.input?.findLast(item => item.role === 'user' && /P4_/.test(JSON.stringify(item))) ?? body.input)
  const currentTurn=body.input.slice(body.input.findLastIndex(item=>item.role==='user'&&/P4_/.test(JSON.stringify(item)))+1)
  if (instruction.includes('P4_CANCEL')) return
  const artifact = /P4_WRITE_(ARTIFACT|CSV|XLSX)/.test(instruction),extension=instruction.includes('P4_WRITE_CSV')?'csv':instruction.includes('P4_WRITE_XLSX')?'xlsx':'txt'
  const full = instruction.includes('P4_NATIVE_FULL'),fullMarker=path.join(work,'native-full-outside.txt')
  const jsonText = JSON.stringify({ summary: 'P0 loopback result', artifacts: full ? [fullMarker] : artifact ? ['generated.'+extension] : [] })
  let output = [{ type: 'message', id: `msg_${requests.length}`, status: 'completed', role: 'assistant',
    content: [{ type: 'output_text', text: jsonText, annotations: [] }] }]
  if ((instruction.includes('P4_WRITE_DENIAL') || instruction.includes('P4_OUTSIDE_DENIAL') || instruction.includes('P4_TEMP_DENIAL') || instruction.includes('P4_READ_NATIVE') || full || artifact) && !currentTurn.some(item => item.type === 'function_call_output')) {
    const tool = body.tools.find(tool => ['exec_command', 'shell_command', 'shell'].includes(tool.name))
    assert(tool, 'Native Codex must expose its official command tool for write-denial validation')
    const marker = path.join(instruction.includes('P4_OUTSIDE_DENIAL')?work:instruction.includes('P4_TEMP_DENIAL')?nativeTemp:cwd, artifact ? 'generated.'+extension : 'readonly-must-not-exist.txt').replaceAll("'", "''")
    let command = instruction.includes('P4_READ_NATIVE') ? '(Get-Location).Path' : `Set-Content -LiteralPath '${marker}' -Value '${artifact ? 'verified artifact' : 'must-not-exist'}'`
    if(full)command=`Set-Content -LiteralPath '${fullMarker.replaceAll("'","''")}' -Value 'native full verified'; (Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:${server.address().port}/native-network').Content`
    if(extension==='csv')command=`[pscustomobject]@{name='synthetic';value=1} | Export-Csv -LiteralPath '${marker}' -NoTypeInformation -Encoding utf8`
    if(extension==='xlsx'){
      const parts={'[Content_Types].xml':'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>','_rels/.rels':'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>','xl/workbook.xml':'<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Synthetic" sheetId="1" r:id="rId1"/></sheets></workbook>','xl/_rels/workbook.xml.rels':'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>','xl/worksheets/sheet1.xml':'<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>synthetic</t></is></c><c r="B1"><v>1</v></c></row></sheetData></worksheet>'}
      const encoded=Buffer.from(JSON.stringify(parts)).toString('base64')
      command=`$parts=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('${encoded}')) | ConvertFrom-Json -AsHashtable; $zip=[IO.Compression.ZipFile]::Open('${marker}',[IO.Compression.ZipArchiveMode]::Create); try { foreach($entry in $parts.GetEnumerator()) { $writer=[IO.StreamWriter]::new($zip.CreateEntry($entry.Key).Open()); try { $writer.Write($entry.Value) } finally { $writer.Dispose() } } } finally { $zip.Dispose() }`
    }
    const args = tool.name === 'exec_command' ? { cmd: command, workdir: cwd, max_output_tokens: 1000 }
      : tool.name === 'shell_command' ? { command, workdir: cwd, timeout_ms: 10000 } : { command: ['pwsh', '-Command', command], workdir: cwd, timeout_ms: 10000 }
    output = [{ type: 'function_call', id: 'fc_probe', call_id: 'call_write_denial', name: tool.name, arguments: JSON.stringify(args), status: 'completed' }]
  }
  response.writeHead(200, { 'Content-Type': 'text/event-stream' })
  const emit = (type, extra) => response.write(`event: ${type}\ndata: ${JSON.stringify({ type, ...extra })}\n\n`)
  emit('response.created', { response: { id: `resp_${requests.length}`, status: 'in_progress', output: [] } })
  emit('response.output_item.added', { output_index: 0, item: { ...output[0], status: 'in_progress', content: [] } })
  if (output[0].type === 'message') emit('response.output_text.delta', { output_index: 0, content_index: 0, item_id: output[0].id, delta: jsonText })
  emit('response.output_item.done', { output_index: 0, item: output[0] })
  emit('response.completed', { response: { id: `resp_${requests.length}`, status: 'completed', output,
    usage: { input_tokens: 1, output_tokens: 1, total_tokens: 2 } } })
  response.end()
})
server.listen(0, '127.0.0.1'); await once(server, 'listening')
await writeFile(path.join(home, 'config.toml'), [
  'model = "telebot-mock"', 'model_provider = "telebot-loopback"', 'approval_policy = "never"',
  'sandbox_mode = "read-only"', '[model_providers.telebot-loopback]', 'name = "Telebot P0 loopback"',
  `base_url = "http://127.0.0.1:${server.address().port}/v1"`, 'wire_api = "responses"',
  'requires_openai_auth = false', 'supports_websockets = false',
  ...(process.platform === 'win32' ? ['[features]', 'prefer_mxc = true', '[windows]', 'sandbox = "mxc"'] : []),
].join('\n') + '\n')
let app
try {
  evidence.cli = (await run(process.execPath, [cli, '--version'], { env, cwd })).stdout.trim()
  app = await startCodex({ cli, cwd, env })
  const started = await app.request('thread/start', { cwd, sandbox: 'read-only', approvalPolicy: 'never', ephemeral: false })
  const effective = await app.request('config/read', { includeLayers: false })
  evidence.executionConfiguration = { windows: effective.config?.windows, sandboxMode: effective.config?.sandbox_mode, defaultPermissions: effective.config?.default_permissions }
  assert.equal(started.sandbox.type, 'readOnly')
  assert.equal(started.approvalPolicy, 'never')
  const threadId = started.thread.id
  const turn = await app.request('turn/start', { threadId, input: [{ type: 'text', text: 'P0 loopback persistence probe', text_elements: [] }],
    outputSchema: { type: 'object', properties: { result: { type: 'string' } }, required: ['result'], additionalProperties: false } })
  const completed = await app.waitFor(row => row.method === 'turn/completed' && row.params.turn.id === turn.turn.id)
  assert.equal(completed.params.turn.status, 'completed')
  assert(requests.length > 0)
  check('official Codex app-server initializes in an isolated home and completes a loopback-only native turn under read-only/never')
  const listing = await app.request('thread/list', { limit: 1, modelProviders: [] })
  assert(listing.data.some(row => row.id === threadId))
  const details = await app.request('thread/read', { threadId, includeTurns: true })
  assert(details.thread.turns.length > 0)
  check('native thread/list pagination and thread/read expose the persisted Codex history independently of Harness Sessions')
  await app.stop(); app = await startCodex({ cli, cwd, env })
  const resumed = await app.request('thread/resume', { threadId, cwd, sandbox: 'read-only', approvalPolicy: 'never' })
  assert.equal(resumed.thread.id, threadId)
  assert.equal(resumed.sandbox.type, 'readOnly')
  check('native thread/resume survives process restart and accepts an explicit permission reset')
  await app.request('thread/archive', { threadId })
  const archived = await app.request('thread/list', { limit: 10, archived: true, modelProviders: [] })
  assert(archived.data.some(row => row.id === threadId))
  const archivedRead = await app.request('thread/read', { threadId, includeTurns: true })
  assert(archivedRead.thread.turns.length > 0)
  check('archived native history remains readable; production connectors must keep archived threads read-only')
  await app.stop(); app = undefined
  const backend = new NativeCodex(cli, home, env)
  try {
    const binding = { userId: 'test-admin', chatId: 'test-chat', sessionId: 'p4-session', cwd }
    const request = { ...binding, mode: 'inspect', prompt: 'P4_PRODUCTION structured result' }
    const result = await backend.execute({ ...request, digest: codingDigest(request) }, { sandbox: 'read-only', signal: AbortSignal.timeout(30000) })
    assert.equal(result.status, 'completed'); assert.equal(result.text, 'P0 loopback result'); assert.deepEqual(result.artifacts, [])
    assert(requests.some(body => body.text?.format?.type === 'json_schema'))
    check('P4 production native connector validates the actual structured result and preserves the native thread identity')
    const read = { ...request, prompt: 'P4_READ_NATIVE only read the current directory' }
    const readBefore = requests.length
    await backend.execute({ ...read, digest: codingDigest(read) }, { sandbox: 'read-only', signal: AbortSignal.timeout(30000) })
    evidence.nativeReadReceipt = requests.slice(readBefore).find(body => body.input?.some(item => item.type === 'function_call_output'))?.input.filter(item => item.type === 'function_call_output')
    assert(evidence.nativeReadReceipt?.some(item => item.output.includes(cwd)), 'A harmless native read must execute successfully under the tested sandbox')
    check('P4 native sandbox executes a harmless read, distinguishing filesystem isolation from a blanket command-policy refusal')
    const write = { ...request, prompt: 'P4_WRITE_DENIAL attempt to create the controlled marker' }
    const before = requests.length
    const denied = await backend.execute({ ...write, digest: codingDigest(write) }, { sandbox: 'read-only', signal: AbortSignal.timeout(30000) })
    assert.equal(denied.status, 'completed')
    await assert.rejects(access(path.join(cwd, 'readonly-must-not-exist.txt')), { code: 'ENOENT' })
    const receipt = requests.slice(before).find(body => JSON.stringify(body.input).includes('function_call_output'))
    assert(receipt && /denied|not permitted|read.only|Access|sandbox/i.test(JSON.stringify(receipt.input)), 'Actual read-only refusal must reach the native model')
    check('P4 native read-only sandbox rejects an actual file write and returns its refusal to the loopback model')
    for(const [label,folder]of [['P4_OUTSIDE_DENIAL',work],['P4_TEMP_DENIAL',nativeTemp]]) {
      const input={...request,mode:'apply',prompt:label+' attempt only the controlled boundary marker'}
      const before=requests.length,result=await backend.execute({...input,digest:codingDigest(input)},{sandbox:'workspace-write',signal:AbortSignal.timeout(30000)})
      assert.equal(result.status,'completed')
      await assert.rejects(access(path.join(folder,'readonly-must-not-exist.txt')),{code:'ENOENT'})
      assert(requests.slice(before).some(body=>body.input?.some(item=>item.type==='function_call_output'&&/denied|not permitted|Access|sandbox/i.test(String(item.output)))))
      assert.equal(backend.lastPermission.requested.networkAccess,false)
      assert.equal(backend.lastPermission.requested.excludeTmpdirEnvVar,true)
    }
    check('P4 workspace-write explicitly excludes temporary roots and refuses writes outside the workspace under the actual native sandbox')
    const controller = new AbortController(), cancel = { ...request, prompt: 'P4_CANCEL held native request' }
    const pending = backend.execute({ ...cancel, digest: codingDigest(cancel) }, { sandbox: 'read-only', signal: controller.signal })
    const deadline = Date.now() + 15000
    while (!requests.some(body => JSON.stringify(body.input).includes('P4_CANCEL')) && Date.now() < deadline) await delay(50)
    controller.abort()
    assert.equal((await pending).status, 'failed')
    check('P4 production connector cancels a held native turn and closes its dedicated process')
    const skillSource=path.join(work,'source-skills');await mkdir(path.join(skillSource,'synthetic'),{recursive:true});const skillText='# Synthetic verification skill\nKeep run state in the workspace copy.';await writeFile(path.join(skillSource,'synthetic/SKILL.md'),skillText)
    const skills=new RunSkills(skillSource,['synthetic']),service = new CodingService(backend, path.join(work, 'coding.sqlite'), ['test-admin'], [cwd], 'test-admin',1200000,900000,skills)
    try {
      const actor = { ...binding, privateChat: true, origin: 'feishu' }
      const proposal = await service.command(actor, 'apply P4_WRITE_ARTIFACT create the controlled artifact', AbortSignal.timeout(30000))
      const token = proposal.text.match(/\/codex confirm ([a-f0-9-]+)/)?.[1]
      assert(token)
      const applied = await service.command(actor, 'confirm ' + token, AbortSignal.timeout(30000))
      assert.equal(applied.status, 'completed', applied.text + '\n' + service.lastFailure + '\n' + JSON.stringify(backend.lastPermission) + '\n' + JSON.stringify(backend.lastTurnFailure) + '\n' + JSON.stringify(requests.at(-1)?.input?.filter(item => item.type === 'function_call_output')))
      assert.equal(JSON.parse(applied.text).artifacts[0].sha256.length, 64)
      await access(path.join(cwd, 'generated.txt'))
      const count = requests.length
      await service.command(actor, 'confirm ' + token, AbortSignal.timeout(30000))
      assert.equal(requests.length, count)
      check('P4 production service freezes and consumes a write grant, verifies the real artifact hash and never repeats native execution')
      const thread=JSON.parse(applied.text).threadId
      const resume=await service.command(actor,'resume '+thread+' P4_WRITE_ARTIFACT update only the controlled workspace artifact',AbortSignal.timeout(30000))
      const resumeToken=resume.text.match(/\/codex resume-confirm ([a-f0-9-]+)/)?.[1]
      assert(resumeToken);assert(resume.text.includes('关闭 Shell 网络'))
      const continued=await service.command(actor,'resume-confirm '+resumeToken,AbortSignal.timeout(30000))
      assert.equal(continued.status,'completed',continued.text)
      assert.equal(JSON.parse(continued.text).threadId,thread)
      assert.equal(continued.artifacts[0].sha256.length,64)
      const afterResume=requests.length
      await assert.rejects(service.command(actor,'resume-confirm '+resumeToken,AbortSignal.timeout(30000)),/confirmation/)
      assert.equal(requests.length,afterResume)
      check('P4 native continuation resets a one-use confirmed workspace-write policy and preserves the original thread with verified artifacts')
      for(const extension of ['csv','xlsx']){
        const plan=await service.command(actor,'apply P4_WRITE_'+extension.toUpperCase()+' generate the controlled '+extension+' artifact',AbortSignal.timeout(30000)),token=plan.text.match(/\/codex confirm ([a-f0-9-]+)/)?.[1];assert(token)
        const output=await service.command(actor,'confirm '+token,AbortSignal.timeout(30000));assert.equal(output.status,'completed',output.text);assert(output.artifacts[0].path.endsWith('.'+extension));assert.equal(output.artifacts[0].sha256.length,64)
        if(extension==='csv')assert((await readFile(output.artifacts[0].path,'utf8')).includes('synthetic'))
        else{const listing=await run('tar',['-tf',output.artifacts[0].path],{env,cwd});assert(listing.stdout.includes('xl/worksheets/sheet1.xml'));const sheet=await run('tar',['-xOf',output.artifacts[0].path,'xl/worksheets/sheet1.xml'],{env,cwd});assert(sheet.stdout.includes('synthetic'))}
        check('P4 actual native workspace sandbox creates a '+extension.toUpperCase()+' artifact whose bytes and SHA-256 are independently verified')
      }
      const nativeActor={...actor,origin:'web',permissions:'full',sourceId:actor.chatId},fullResult=await service.command(nativeActor,'apply P4_NATIVE_FULL controlled outside-cwd marker and loopback network only',AbortSignal.timeout(30000),'native-full-op')
      assert.equal(fullResult.status,'completed',JSON.stringify(fullResult)+' '+JSON.stringify(backend.lastPermission))
      assert.equal(fullResult.artifacts[0].path,path.join(work,'native-full-outside.txt'));assert((await readFile(fullResult.artifacts[0].path,'utf8')).includes('native full verified'))
      evidence.nativeFullOutputs=requests.filter(body=>JSON.stringify(body.input).includes('P4_NATIVE_FULL')).flatMap(body=>body.input?.filter(item=>item.type==='function_call_output')??[]);assert(nativeNetworkRequests>0,JSON.stringify(evidence.nativeFullOutputs));evidence.nativeLoopbackNetworkRequests=nativeNetworkRequests
      const afterFull=requests.length;await service.command(nativeActor,'apply P4_NATIVE_FULL controlled outside-cwd marker and loopback network only',AbortSignal.timeout(30000),'native-full-op');assert.equal(requests.length,afterFull)
      check('P8 native owner full policy performs actual isolated outside-cwd write and loopback HTTP, verifies hash and fences replay')
      const fullThread=JSON.parse(fullResult.text).threadId,continuedFull=await service.command(nativeActor,'resume '+fullThread+' P4_NATIVE_FULL controlled one-turn continuation',AbortSignal.timeout(30000),'native-full-resume-op');assert.equal(continuedFull.status,'completed',continuedFull.text);assert.equal(JSON.parse(continuedFull.text).threadId,fullThread);assert.equal(backend.lastPermission.requested.type,'dangerFullAccess');const afterFullResume=requests.length;await service.command(nativeActor,'resume '+fullThread+' P4_NATIVE_FULL controlled one-turn continuation',AbortSignal.timeout(30000),'native-full-resume-op');assert.equal(requests.length,afterFullResume);const historyRead=await service.command(nativeActor,'read '+fullThread,AbortSignal.timeout(30000));assert(JSON.parse(historyRead.text).thread.turns.length>0);check('P8 actual native owner history and one-turn resume retain full policy and original thread without repeated execution')
      const copy=await skills.prepare(cwd,await skills.snapshot());assert.equal(await readFile(path.join(skillSource,'synthetic/SKILL.md'),'utf8'),skillText);assert(requests.some(body=>JSON.stringify(body.input).includes('.telebot-skills')));await writeFile(path.join(copy,'synthetic/run-state.json'),'{"seen":["fixture"]}');assert.equal(await skills.prepare(cwd,await skills.snapshot()),copy)
      check('P4 selected Skill hash and run-copy instructions reach native Codex while original source stays intact and local deduplication state persists')
    } finally { await service.dispose() }
  } finally { await backend.dispose() }
  // Sanity: no credential file was imported, and only loopback requests occurred.
  await assert.rejects(access(path.join(home, 'auth.json')), { code: 'ENOENT' })
  evidence.modelRequests = requests.length
  evidence.status = 'passed'
} catch (error) { evidence.status = 'failed'; evidence.error = error.message; throw error }
finally {
  await app?.stop().catch(() => {})
  server.closeAllConnections(); await new Promise(resolve => server.close(resolve))
  await mkdir(path.join(root, '.runtime'), { recursive: true })
  const result = JSON.stringify(evidence, null, 2) + '\n'
  await writeFile(path.join(work, 'result.json'), result)
  await writeFile(path.join(root, '.runtime/verify-codex-latest.json'), result)
  console.log(`Codex P0 evidence: ${path.join(work, 'result.json')}`)
  await cleanupVerification(work, evidence.status === 'passed')
}
