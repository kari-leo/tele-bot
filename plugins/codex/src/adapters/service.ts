import { desktopFull, desktopOwner } from '@telebot/core/contracts'
import { DatabaseSync } from 'node:sqlite'
import { randomUUID, createHash } from 'node:crypto'
import { readFile, realpath, stat } from 'node:fs/promises'
import path from 'node:path'
import type { TrustedActor, ConversationResult } from '@telebot/core/contracts'
import { authorizeHistory, codingDigest, executeCoding, type CodingRequest } from '../domain/coding.js'
import { NativeCodex, type NativeClient, type NativeResult } from './native.js'
import {RunSkills} from './skills.js'
import { archivedThread, historyDetail, historyList, historyResumePrompt, historyScope, historyThreadId, historyTranscript, type HistoryThread, type HistoryTurnPage } from './history-view.js'

interface HistoryPage { id: string; threads: HistoryThread[]; search: string; archived: boolean; nextCursor?: string; expires: number }
interface DetailPage { id: string; thread: HistoryThread; remaining: string; nextCursor?: string; expires: number }
const historyTtl = 30 * 60 * 1000
const historyRejections = new Map([
  ['private_owned_history_required', '请在配置的 Codex 历史所有者本人私聊中使用此功能。'],
  ['codex_history_id_required', '请提供完整的 Codex 会话 ID，或使用当前历史列表的序号。'],
  ['codex_history_number_expired', '序号不在当前列表内，请先发送 /codex history。'],
  ['codex_history_page_expired', '下一页不可用，请重新打开历史列表或会话详情。'],
  ['resume_prompt_required', '请提供 1 至 30,000 字的续聊任务。'],
  ['codex_explicit_resume_required', '请在本人私聊明确发送 /codex resume <会话> <任务>。'],
  ['native_thread_busy', '该 Codex 会话正在执行，请等待本轮结束后再续聊。'],
  ['coding_workspace_not_allowed', '原会话工作目录不存在，或未列入 Codex 设置的允许工作目录。'],
  ['history_resume_requires_fresh_workspace_authorization', '该 Codex 会话已归档，只能查看。'],
])

export class CodingService {
  lastFailure: string | undefined
  private readonly db: DatabaseSync
  private readonly active = new Map<string, AbortController>()
  private readonly resumingThreads = new Set<string>()
  private readonly jobs = new Set<Promise<ConversationResult>>()
  private readonly historyPages = new Map<string, HistoryPage>()
  private readonly detailPages = new Map<string, DetailPage>()
  private readonly writer = randomUUID()
  constructor(private readonly backend: NativeCodex, filename: string, private readonly admins: readonly string[], private readonly roots: readonly string[], private readonly historyOwner: string, private readonly executionTimeoutMs = 1200000, private readonly confirmationTtlMs = 900000,private readonly skills=new RunSkills()) {
    this.db = new DatabaseSync(filename)
    this.db.exec(`PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL;
      CREATE TABLE IF NOT EXISTS coding(token TEXT PRIMARY KEY,binding TEXT NOT NULL,request TEXT NOT NULL,expires INTEGER NOT NULL,state TEXT NOT NULL,result TEXT);
      CREATE TABLE IF NOT EXISTS threads(id TEXT PRIMARY KEY,owner TEXT NOT NULL,cwd TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS writer(id INTEGER PRIMARY KEY,pid INTEGER,owner TEXT);`)
    this.db.exec('BEGIN IMMEDIATE')
    const writer = this.db.prepare('SELECT * FROM writer WHERE id=1').get() as any
    let alive = false
    if (writer) { try { process.kill(writer.pid, 0); alive = true } catch (error) { if ((error as NodeJS.ErrnoException).code !== 'ESRCH') alive = true } }
    if (alive) { this.db.exec('ROLLBACK'); this.db.close(); throw new Error('coding_database_already_active') }
    this.db.prepare('INSERT OR REPLACE INTO writer VALUES(1,?,?)').run(process.pid, this.writer)
    this.db.exec("UPDATE coding SET state='unknown' WHERE state='running'; COMMIT;")
  }
  private binding(actor: TrustedActor) { return JSON.stringify([actor.userId, actor.chatId, actor.sessionId, actor.cwd]) }
  busy() { return this.jobs.size > 0 || this.active.size > 0 }
  pending(actor: TrustedActor) {
    return (this.db.prepare("SELECT token,request FROM coding WHERE binding=? AND state='pending' AND expires>?").all(this.binding(actor), Date.now()) as {token:string;request:string}[]).filter(row => !row.token.startsWith('task:') && !row.token.startsWith('feishu-resume:')).map(row => ({token:row.token,label:JSON.parse(row.request).prompt as string}))
  }
  confirm(actor: TrustedActor, token: string, signal: AbortSignal) {
    const row = this.db.prepare('SELECT request FROM coding WHERE token=? AND binding=?').get(token,this.binding(actor)) as any
    return this.command(actor,(row && JSON.parse(row.request).threadId ? 'resume-confirm ' : 'confirm ')+token,signal)
  }
  /** Called only by the trusted task dispatcher after its separate execution grant. */
  async scheduled(actor: TrustedActor, prompt: string, executionId: string, signal: AbortSignal) {
    if (!desktopOwner(actor) && !this.admins.includes(actor.userId)) throw new Error('coding_actor_not_authorized')
    await this.workspace(actor)
    if (!prompt.trim() || prompt.length > 120000 || !executionId || executionId.length > 300) throw new Error('invalid_task_execution')
    const payload = { userId:actor.userId,chatId:actor.chatId,sessionId:actor.sessionId,cwd:actor.cwd,mode:'apply' as const,prompt,...(desktopFull(actor)?{fullAccess:true}:{}),skills:await this.skills.snapshot() }, request = {...payload,digest:codingDigest(payload)}, token='task:'+executionId
    const existing = this.db.prepare('SELECT binding,request FROM coding WHERE token=?').get(token) as any
    if (existing && (existing.binding !== this.binding(actor) || JSON.parse(existing.request).digest !== request.digest)) throw new Error('task_execution_collision')
    if (!existing) this.db.prepare("INSERT INTO coding VALUES(?,?,?,?,?,NULL)").run(token,this.binding(actor),JSON.stringify(request),Date.now()+this.confirmationTtlMs,'pending')
    return this.command(actor,'confirm '+token,AbortSignal.any([signal,AbortSignal.timeout(this.executionTimeoutMs)]))
  }
  private async artifacts(actor: TrustedActor, result: NativeResult, startedAt: number, requireAll = true) {
    const artifacts = []
    if (result.status === 'completed') for (const target of result.artifacts ?? []) {
      try {
        const file = await realpath(path.resolve(actor.cwd, target)), relative = path.relative(await realpath(actor.cwd), file)
        if (!desktopFull(actor) && (relative.startsWith('..') || path.isAbsolute(relative))) throw new Error('unverified_coding_artifact')
        const info = await stat(file)
        if (!info.isFile() || info.mtimeMs < startedAt - 1000 || info.size > 30 * 1024 * 1024) throw new Error('unverified_coding_artifact')
        artifacts.push({ path: file, size: info.size, sha256: createHash('sha256').update(await readFile(file)).digest('hex') })
      } catch (error) {
        if (requireAll) throw error
      }
    }
    return { ...result, artifacts }
  }
  private async workspace(actor: TrustedActor, cwd = actor.cwd) {
    if (!path.isAbsolute(cwd)) throw new Error('coding_workspace_not_allowed')
    const actual = await realpath(cwd)
    if (!(await stat(actual)).isDirectory()) throw new Error('workspace_not_directory')
    if(desktopFull(actor))return actual
    const roots = await Promise.all((desktopOwner(actor)?actor.allowedRoots??[]:this.roots).map(root => realpath(root)))
    if (!roots.some(root => { const r = path.relative(root, actual); return r !== '..' && !r.startsWith('..' + path.sep) && !path.isAbsolute(r) })) throw new Error('coding_workspace_not_allowed')
    return actual
  }
  command(actor: TrustedActor, text: string, signal: AbortSignal, operationId?:string): Promise<ConversationResult> {
    const job = this.commandImpl(actor, text, signal,operationId)
    this.jobs.add(job); job.finally(() => this.jobs.delete(job)).catch(() => {})
    return job
  }
  private async commandImpl(actor: TrustedActor, text: string, signal: AbortSignal,operationId?:string): Promise<ConversationResult> {
    if (!desktopOwner(actor) && !this.admins.includes(actor.userId)) throw new Error('coding_actor_not_authorized')
    const match = /^(\S+)(?:\s+([\s\S]*))?$/.exec(text.trim())
    const action = match?.[1]?.toLowerCase() ?? '', value = match?.[2] ?? ''
    if (action === 'history' || action === 'read' || action === 'resume' || action === 'resume-confirm' || action === 'resume-select') {
      try { return await this.history(actor, action, value, signal, operationId) }
      catch (error) {
        const text = error instanceof Error ? historyRejections.get(error.message) : undefined
        if (actor.origin === 'feishu' && text) return { status: 'failed', text }
        throw error
      }
    }
    await this.workspace(actor)
    if (action === 'stop') { this.active.get(actor.sessionId)?.abort(); return { status: 'completed', text: '已请求停止 Codex。' } }
    if (action === 'apply' && /^[a-f0-9-]{36}$/.test(value) && this.db.prepare('SELECT 1 FROM coding WHERE token=?').get(value)) throw new Error('apply_token_requires_explicit_confirm')
    if (action === 'cancel') { this.db.prepare("UPDATE coding SET state='cancelled' WHERE token=? AND binding=? AND state='pending'").run(value, this.binding(actor)); return { status: 'completed', text: '已取消待确认请求。' } }
    let request: CodingRequest, token: string | undefined
    if (action === 'confirm') {
      const row = this.db.prepare('SELECT * FROM coding WHERE token=?').get(value) as any
      if (!row || row.binding !== this.binding(actor) || row.expires <= Date.now()) throw new Error('coding_confirmation_mismatch')
      if (row.state !== 'pending') return { status: row.state === 'completed' ? 'completed' : 'unknown', text: row.result ?? '请求已消费，不会重复执行。' }
      request = JSON.parse(row.request); token = value
    } else {
      if (!['inspect', 'plan', 'apply'].includes(action) || !value.trim() || value.length > 120000) return { status: 'failed', text: '/codex inspect|plan|apply <要求>；/codex confirm|cancel <令牌>；history/read/resume/stop' }
      const payload = { userId: actor.userId, chatId: actor.chatId, sessionId: actor.sessionId, cwd: actor.cwd, mode: action as CodingRequest['mode'], prompt: value,...(desktopFull(actor)?{fullAccess:true}:{}),...(action==='apply'?{skills:await this.skills.snapshot()}:{}) }
      request = { ...payload, digest: codingDigest(payload) }
      if (action === 'apply') {
        if(desktopFull(actor)&&!operationId)throw new Error('desktop_operation_id_required')
        token = desktopFull(actor)?'desktop:'+createHash('sha256').update(this.binding(actor)+operationId).digest('hex'):randomUUID()
        const existing=this.db.prepare('SELECT request FROM coding WHERE token=?').get(token) as any
        if(existing){if(JSON.parse(existing.request).digest!==request.digest)throw new Error('desktop_operation_collision');return this.commandImpl(actor,'confirm '+token,signal)}
        this.db.prepare('INSERT INTO coding VALUES(?,?,?,?,?,NULL)').run(token, this.binding(actor), JSON.stringify(request), Date.now() + this.confirmationTtlMs, 'pending')
        if(desktopFull(actor))return this.commandImpl(actor,'confirm '+token,signal)
        return { status: 'completed', text: `写入计划：${request.prompt}\n目录：${actor.cwd}\n摘要：${request.digest}\n技能运行副本：${request.skills?.names.join('、')||'未选择'}\n在确认有效期内使用 /codex confirm ${token} 确认；只授权此用户/会话/目录/内容。` }
      }
    }
    if (this.active.has(actor.sessionId)) throw new Error('coding_session_busy')
    signal.throwIfAborted()
    if (token && this.db.prepare("UPDATE coding SET state='running' WHERE token=? AND state='pending'").run(token).changes !== 1) throw new Error('coding_already_claimed')
    const controller = new AbortController(); this.active.set(actor.sessionId, controller)
    const startedAt = Date.now()
    try {
      if(request.mode==='apply')await this.skills.prepare(actor.cwd,request.skills)
      const grant = request.mode === 'apply' ? { ...actor, digest: request.digest, expiresAt: Date.now() + this.confirmationTtlMs, consumed: false } : undefined
      const result = await executeCoding(this.backend, request, {...actor,fullAccess:desktopFull(actor)}, desktopOwner(actor)?[actor.userId]:this.admins, grant, AbortSignal.any([signal, controller.signal, AbortSignal.timeout(this.executionTimeoutMs)])) as NativeResult
      if (result.threadId) this.db.prepare('INSERT OR REPLACE INTO threads VALUES(?,?,?)').run(result.threadId, actor.userId, actor.cwd)
      const output = await this.artifacts(actor, result, startedAt)
      if (token) this.db.prepare('UPDATE coding SET state=?,result=? WHERE token=?').run(result.status, JSON.stringify(output), token)
      return { status: result.status, text: JSON.stringify(output), artifacts: output.artifacts }
    } catch (error) {
      this.lastFailure = error instanceof Error && /^[a-z_]+$/.test(error.message) ? error.message : (error as NodeJS.ErrnoException)?.code ?? 'coding_verification_failed'
      if (token) this.db.prepare("UPDATE coding SET state='unknown' WHERE token=?").run(token)
      return { status: 'unknown', text: 'Codex 执行或产物回执未确认，没有自动重试。' }
    } finally { this.active.delete(actor.sessionId) }
  }
  private async history(actor: TrustedActor, action: 'history' | 'read' | 'resume' | 'resume-confirm' | 'resume-select', value: string, signal: AbortSignal,operationId?:string): Promise<ConversationResult> {
    if (!actor.privateChat || !desktopOwner(actor) && actor.userId !== this.historyOwner) throw new Error('private_owned_history_required')
    if (actor.origin === 'feishu' && action === 'resume' && !actor.sourceId) throw new Error('codex_explicit_resume_required')
    const client = await this.backend.connect()
    try {
      if (action === 'resume-confirm') {
        const row = this.db.prepare('SELECT * FROM coding WHERE token=?').get(value) as any
        if (!row || row.binding !== this.binding(actor) || row.expires <= Date.now() || row.state !== 'pending') throw new Error('resume_confirmation_mismatch')
        const saved = JSON.parse(row.request)
        if(Boolean(saved.fullAccess)!==desktopFull(actor))throw new Error('resume_permission_changed')
        if (!saved.threadId || typeof saved.cwd !== 'string' || saved.mode !== 'apply' || saved.policyVersion !== 1) throw new Error('resume_scope_mismatch_requires_new_confirmation')
        const thread: HistoryThread = (await client.request('thread/read', { threadId: saved.threadId, includeTurns: false }, signal)).thread
        const cwd = await this.resumeWorkspace(actor, client, thread, signal)
        if (path.relative(saved.cwd, cwd) !== '') throw new Error('resume_scope_mismatch_requires_new_confirmation')
        if (this.active.has(actor.sessionId)) throw new Error('coding_session_busy')
        if (this.resumingThreads.has(saved.threadId)) throw new Error('native_thread_busy')
        signal.throwIfAborted()
        if (this.db.prepare("UPDATE coding SET state='running' WHERE token=? AND state='pending'").run(value).changes !== 1) throw new Error('resume_already_claimed')
        const controller = new AbortController(); this.active.set(actor.sessionId, controller)
        this.resumingThreads.add(saved.threadId)
        const payload = { userId: actor.userId, chatId: actor.chatId, sessionId: actor.sessionId, cwd, mode: 'apply' as const, prompt: saved.prompt,...(desktopFull(actor)?{fullAccess:true}:{}),skills:saved.skills }
        try {
          const startedAt = Date.now()
          await this.skills.prepare(cwd,saved.skills)
          const result = await this.backend.execute({ ...payload, digest: codingDigest(payload) }, { sandbox: desktopFull(actor)?'danger-full-access':'workspace-write', signal: AbortSignal.any([signal, controller.signal, AbortSignal.timeout(this.executionTimeoutMs)]) }, saved.threadId, thread)
          // A Feishu continuation can finish with a text reply and no deliverable files.
          const output = await this.artifacts({ ...actor, cwd }, result, startedAt, actor.origin !== 'feishu')
          this.db.prepare('UPDATE coding SET state=?,result=? WHERE token=?').run(result.status, JSON.stringify(output), value)
          return { status: result.status, text: JSON.stringify(output), artifacts: output.artifacts,
            ...(actor.origin === 'feishu' && result.status === 'completed' ? { card: { title: 'Codex 本轮执行完成', actions: [
              { label: '查看最新对话', command: 'read ' + saved.threadId }, { label: '继续此会话', command: 'resume-select ' + saved.threadId },
            ] } } : {}) }
        } catch { this.db.prepare("UPDATE coding SET state='unknown' WHERE token=?").run(value); return { status: 'unknown', text: '续聊结果未确认，不会自动重复提交。' } }
        finally { this.active.delete(actor.sessionId); this.resumingThreads.delete(saved.threadId) }
      }
      if (action === 'resume-select' || (actor.origin === 'feishu' && action === 'resume' && /^\S+$/.test(value))) {
        if (actor.origin !== 'feishu') throw new Error('codex_explicit_resume_required')
        const threadId = this.feishuThreadId(actor, value)
        this.authorizeThreadRead(actor, threadId)
        const thread: HistoryThread = (await client.request('thread/read', { threadId, includeTurns: false }, signal)).thread
        if (archivedThread(thread) || await this.archived(client, threadId, signal)) throw new Error('history_resume_requires_fresh_workspace_authorization')
        return { status: 'completed', text: historyResumePrompt(thread), card: { title: '继续 Codex 会话', replyCommand: 'resume ' + threadId } }
      }
      if (actor.origin === 'feishu' && (action === 'history' || action === 'read')) return await this.feishuHistory(actor, client, action, value, signal)
      if (action === 'history') {
        const [cursor, archived] = value.split(/\s+/)
        const result = await client.request('thread/list', { limit: 20, ...(cursor ? { cursor } : {}), archived: archived === 'archived', modelProviders: [] }, signal)
        result.data=result.data.filter((thread:any)=>{const owner=this.db.prepare('SELECT owner FROM threads WHERE id=?').get(thread.id) as any;return !owner||owner.owner===actor.userId});return { status: 'completed', text: JSON.stringify(result) }
      }
      const reference = /^\S+/.exec(value)?.[0] ?? ''
      const threadId = actor.origin === 'feishu' ? this.feishuThreadId(actor, reference) : reference
      this.authorizeThreadRead(actor, threadId)
      if (action === 'read') return { status: 'completed', text: JSON.stringify(await client.request('thread/read', { threadId, includeTurns: true }, signal)) }
      const prompt = actor.origin === 'feishu' ? value.slice(reference.length).trim() : value.slice(reference.length).trim().split(/\s+/).join(' ')
      if (!prompt || prompt.length > 30000) throw new Error('resume_prompt_required')
      if (desktopFull(actor) && !operationId) throw new Error('desktop_operation_id_required')
      const immediate = actor.origin === 'feishu' || desktopFull(actor)
      const token = actor.origin === 'feishu' ? 'feishu-resume:' + createHash('sha256').update(JSON.stringify([this.binding(actor), actor.sourceId])).digest('hex')
        : desktopFull(actor) ? 'desktop-resume:' + createHash('sha256').update(this.binding(actor) + operationId).digest('hex') : randomUUID()
      const prior = this.resumeReceipt(actor, token, threadId, prompt)
      if (prior) {
        if (prior.state !== 'pending') return this.priorResumeResult(prior)
        return await this.history(actor, 'resume-confirm', token, signal)
      }
      const thread: HistoryThread = (await client.request('thread/read', { threadId, includeTurns: false }, signal)).thread
      const cwd = await this.resumeWorkspace(actor, client, thread, signal)
      const request = { threadId, prompt, cwd, mode: 'apply', policyVersion: 1, ...(desktopFull(actor) ? { fullAccess: true } : {}), skills: await this.skills.snapshot() }
      if (this.db.prepare('INSERT OR IGNORE INTO coding VALUES(?,?,?,?,?,NULL)').run(token, this.binding(actor), JSON.stringify(request), Date.now() + this.confirmationTtlMs, 'pending').changes !== 1) {
        return this.priorResumeResult(this.resumeReceipt(actor, token, threadId, prompt))
      }
      if (immediate) return await this.history(actor, 'resume-confirm', token, signal)
      return { status: 'completed', text: `续聊一轮：${threadId}\n目录：${cwd}\n要求：${prompt}\n确认后仅允许写当前工作区；关闭 Shell 网络和临时目录默认写权限，拒绝沙箱外审批。\n确认：/codex resume-confirm ${token}` }
    } finally { await this.backend.release(client) }
  }
  private resumeReceipt(actor: TrustedActor, token: string, threadId: string, prompt: string) {
    const prior = this.db.prepare('SELECT binding,request,state,result FROM coding WHERE token=?').get(token) as { binding: string; request: string; state: string; result: string | null } | undefined
    if (!prior) return undefined
    const previous = JSON.parse(prior.request)
    if (prior.binding !== this.binding(actor) || previous.threadId !== threadId || previous.prompt !== prompt) throw new Error('codex_operation_collision')
    return prior
  }
  private priorResumeResult(prior: { state: string; result: string | null } | undefined): ConversationResult {
    const state = prior?.state
    return { status: state === 'completed' || state === 'failed' ? state : 'unknown', text: prior?.result ?? '结果待核实，不重复续聊。' }
  }
  private authorizeThreadRead(actor: TrustedActor, threadId: string) {
    const owner = this.db.prepare('SELECT owner FROM threads WHERE id=?').get(threadId)?.owner
    authorizeHistory(actor, desktopOwner(actor) ? [actor.userId] : this.admins, { owner: typeof owner === 'string' ? owner : actor.userId, cwd: actor.cwd, archived: false }, 'read')
  }
  private async resumeWorkspace(actor: TrustedActor, client: NativeClient, thread: HistoryThread, signal: AbortSignal) {
    this.authorizeThreadRead(actor, thread.id)
    if (thread.status?.type === 'active') throw new Error('native_thread_busy')
    if (archivedThread(thread) || await this.archived(client, thread.id, signal)) throw new Error('history_resume_requires_fresh_workspace_authorization')
    let cwd: string
    try { cwd = await this.workspace(actor, thread.cwd || '') }
    catch { throw new Error('coding_workspace_not_allowed') }
    if (actor.origin === 'web' && path.relative(await this.workspace(actor), cwd) !== '') throw new Error('history_resume_requires_fresh_workspace_authorization')
    return cwd
  }
  private recentPage<T extends { expires: number }>(pages: Map<string, T>, scope: string) {
    const page = pages.get(scope)
    if (page && page.expires > Date.now()) return page
    pages.delete(scope)
    return undefined
  }
  private rememberPage<T extends { expires: number }>(pages: Map<string, T>, scope: string, page: T) {
    pages.delete(scope)
    if (pages.size >= 128) pages.delete(pages.keys().next().value!)
    pages.set(scope, page)
  }
  private feishuThreadId(actor: TrustedActor, reference: string) {
    const page = this.recentPage(this.historyPages, historyScope(actor))
    const id = /^\d+$/.test(reference) ? page?.threads[Number(reference) - 1]?.id : reference
    if (!id || !historyThreadId.test(id)) throw new Error(/^\d+$/.test(reference) ? 'codex_history_number_expired' : 'codex_history_id_required')
    return id
  }
  private async feishuHistory(actor: TrustedActor, client: NativeClient, action: 'history' | 'read', value: string, signal: AbortSignal): Promise<ConversationResult> {
    const scope = historyScope(actor)
    if (action === 'history' && !/^\d+$/.test(value)) {
      const prior = this.recentPage(this.historyPages, scope)
      const next = value === '--next' || value.startsWith('--next ')
      if (next && (!prior?.nextCursor || (value !== '--next' && value !== '--next ' + prior.id))) throw new Error('codex_history_page_expired')
      const archived = next ? prior!.archived : /^--archived(?:\s|$)/.test(value)
      const search = next ? prior!.search : value.replace(/^--archived(?:\s+|$)/, '').slice(0, 200)
      const result = await client.request('thread/list', { limit: 10, archived, ...(next ? { cursor: prior!.nextCursor } : {}), ...(search ? { searchTerm: search } : {}),
        sortKey: 'updated_at', sortDirection: 'desc', sourceKinds: ['cli', 'vscode', 'exec', 'appServer'], modelProviders: [] }, signal)
      const threads = (result.data as HistoryThread[]).filter(thread => {
        const owner = this.db.prepare('SELECT owner FROM threads WHERE id=?').get(thread.id)?.owner
        return historyThreadId.test(thread.id) && (!owner || owner === actor.userId)
      }).slice(0, 10).map(thread => ({ ...thread, archived: archived || archivedThread(thread) }))
      const id = randomUUID()
      this.rememberPage(this.historyPages, scope, { id, threads, search, archived, nextCursor: result.nextCursor, expires: Date.now() + historyTtl })
      this.detailPages.delete(scope)
      return { status: 'completed', text: historyList(threads), card: { title: archived ? 'Codex 已归档会话' : 'Codex 最近会话', actions: [
        ...threads.map((thread, index) => ({ label: `查看 ${index + 1}`, command: 'read ' + thread.id })),
        ...(result.nextCursor ? [{ label: '下一页', command: 'history --next ' + id }] : []),
        { label: '刷新列表', command: ['history', archived ? '--archived' : '', search].filter(Boolean).join(' ') },
      ] } }
    }
    const next = action === 'read' && (value === '--next' || value.startsWith('--next '))
    const previous = next ? this.recentPage(this.detailPages, scope) : undefined
    if (next && (!previous || (value !== '--next' && value !== '--next ' + previous.id))) throw new Error('codex_history_page_expired')
    const threadId = previous?.thread.id ?? this.feishuThreadId(actor, value)
    this.authorizeThreadRead(actor, threadId)
    const thread: HistoryThread = previous?.thread ?? (await client.request('thread/read', { threadId, includeTurns: false }, signal)).thread
    const listed = this.recentPage(this.historyPages, scope)?.threads.find(item => item.id === threadId)
    const archived = archivedThread(thread) || Boolean(listed?.archived)
    const page: HistoryTurnPage | undefined = previous?.remaining ? undefined : await client.request('thread/turns/list', { threadId, limit: 3, sortDirection: 'desc', itemsView: 'full', ...(previous?.nextCursor ? { cursor: previous.nextCursor } : {}) }, signal)
    const text = page ? historyTranscript(page) : previous!.remaining
    const nextCursor = page ? page.nextCursor ?? undefined : previous?.nextCursor
    const visible = Array.from(text).slice(0, 2900).join('')
    const remaining = text.slice(visible.length)
    const id = randomUUID()
    if (remaining || nextCursor) this.rememberPage(this.detailPages, scope, { id, thread: { ...thread, archived }, remaining, nextCursor, expires: Date.now() + historyTtl })
    else this.detailPages.delete(scope)
    return { status: 'completed', text: historyDetail({ ...thread, archived }, visible), card: { title: 'Codex 会话详情', actions: [
      ...(remaining || nextCursor ? [{ label: remaining ? '继续阅读' : '更早对话', command: 'read --next ' + id }] : []),
      ...(!archived ? [{ label: '继续此会话', command: 'resume-select ' + threadId }] : []),
      { label: '返回列表', command: archived ? 'history --archived' : 'history' },
    ] } }
  }
  private async archived(client: NativeClient, id: string, signal: AbortSignal) {
    let cursor: string | undefined
    const seen = new Set<string>()
    for (let page = 0; page < 100; page++) {
      const result = await client.request('thread/list', { archived: true, limit: 100, modelProviders: [], ...(cursor ? { cursor } : {}) }, signal)
      if (result.data.some((entry: any) => entry.id === id)) return true
      if (!result.nextCursor) return false
      if (seen.has(result.nextCursor)) throw new Error('native_history_cursor_cycle')
      cursor = result.nextCursor; seen.add(cursor!)
    }
    throw new Error('native_history_page_budget')
  }
  async dispose() { for (const controller of this.active.values()) controller.abort(); await this.backend.dispose(); await Promise.allSettled([...this.jobs]); this.db.prepare('DELETE FROM writer WHERE owner=?').run(this.writer); this.db.close() }
}
