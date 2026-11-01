import { DatabaseSync } from 'node:sqlite'
import { createHash, randomUUID } from 'node:crypto'
import type { AcceptResult, ChannelRepository, ChannelSettings, DeliveryResult, InboxEntry, InputEvent, Outbound } from '../domain/types.js'
import type { ChannelQuestion, ConversationSpec, TrustedActor } from '@telebot/core/contracts'

type Row = Record<string, string | number | null>
const hash = (value: unknown) => createHash('sha256').update(JSON.stringify(value)).digest('hex')

/** Own channel facts only. The lease excludes a second channel worker for the same database. */
export class SqliteChannelRepository implements ChannelRepository {
  private readonly db: DatabaseSync
  private readonly owner = randomUUID()
  private closed = false
  constructor(filename: string, appId: string, leaseMs: number) {
    this.db = new DatabaseSync(filename)
    try {
      this.db.exec('PRAGMA busy_timeout=5000; PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL; PRAGMA foreign_keys=ON;')
      const version = (this.db.prepare('PRAGMA user_version').get() as Row).user_version
      if (![0,1,2,3].includes(Number(version))) throw new Error('unsupported_channel_schema')
      this.db.exec(`
        CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS lease (id INTEGER PRIMARY KEY CHECK(id=1), owner TEXT NOT NULL, expires INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS sessions (key TEXT PRIMARY KEY, id TEXT NOT NULL UNIQUE, spec TEXT NOT NULL, initialized INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS inbox (
          seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE, input_id TEXT NOT NULL,
          event_id TEXT NOT NULL, message_id TEXT NOT NULL, chat_id TEXT NOT NULL, thread_id TEXT NOT NULL,
          actor TEXT NOT NULL, kind TEXT NOT NULL, text TEXT NOT NULL, session_key TEXT NOT NULL REFERENCES sessions(key),
          state TEXT NOT NULL DEFAULT 'pending', created INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS received (key TEXT PRIMARY KEY, inbox_id TEXT NOT NULL REFERENCES inbox(id));
        CREATE TABLE IF NOT EXISTS outbox (
          seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE, inbox_id TEXT NOT NULL REFERENCES inbox(id),
          chat_id TEXT NOT NULL, reply_to TEXT NOT NULL, thread_id TEXT NOT NULL,
          msg_type TEXT NOT NULL, content TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
          next_at INTEGER NOT NULL DEFAULT 0, remote_id TEXT, error_code TEXT);
        CREATE TABLE IF NOT EXISTS actions (
          token TEXT PRIMARY KEY, inbox_id TEXT NOT NULL, session_key TEXT NOT NULL, actor TEXT NOT NULL,
          chat_id TEXT NOT NULL, thread_id TEXT NOT NULL, message_id TEXT, expires INTEGER NOT NULL, consumed INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS external_delivery(id TEXT PRIMARY KEY,digest TEXT NOT NULL,outbox_ids TEXT NOT NULL);
      `)
      if(!(this.db.prepare('PRAGMA table_info(outbox)').all() as Row[]).some(row=>row.name==='uploaded_key'))this.db.exec('ALTER TABLE outbox ADD COLUMN uploaded_key TEXT')
      this.transaction(()=>{
        const columns=(this.db.prepare('PRAGMA table_info(outbox)').all() as Row[]).map(row=>row.name)
        if(!columns.includes('purpose'))this.db.exec("ALTER TABLE outbox ADD COLUMN purpose TEXT NOT NULL DEFAULT 'message'")
        if(!columns.includes('edit_id'))this.db.exec('ALTER TABLE outbox ADD COLUMN edit_id TEXT')
        if(!(this.db.prepare('PRAGMA table_info(inbox)').all() as Row[]).some(row=>row.name==='pinned_spec'))this.db.exec('ALTER TABLE inbox ADD COLUMN pinned_spec TEXT')
        this.db.exec(`UPDATE inbox SET pinned_spec=(SELECT spec FROM sessions WHERE key=inbox.session_key) WHERE pinned_spec IS NULL AND state!='pending'; PRAGMA user_version=3`)
      })
      this.transaction(() => {
        const existing = this.db.prepare("SELECT value FROM metadata WHERE key='app_id'").get() as Row | undefined
        if (existing && existing.value !== appId) throw new Error('channel_database_app_mismatch')
        this.db.prepare("INSERT OR IGNORE INTO metadata VALUES ('app_id', ?)").run(appId)
        const lease = this.db.prepare('SELECT * FROM lease WHERE id=1').get() as Row | undefined
        if (lease && Number(lease.expires) > Date.now()) throw new Error('channel_database_already_active')
        this.db.prepare('INSERT OR REPLACE INTO lease VALUES (1, ?, ?)').run(this.owner, Date.now() + leaseMs)
      })
    } catch (error) { this.db.close(); throw error }
  }
  private transaction<T>(operation: () => T): T {
    this.db.exec('BEGIN IMMEDIATE')
    try { const result = operation(); this.db.exec('COMMIT'); return result }
    catch (error) { this.db.exec('ROLLBACK'); throw error }
  }
  heartbeat(leaseMs: number): boolean {
    return this.db.prepare('UPDATE lease SET expires=? WHERE id=1 AND owner=?').run(Date.now() + leaseMs, this.owner).changes === 1
  }
  private owned(): void {
    const lease = this.db.prepare('SELECT owner,expires FROM lease WHERE id=1').get() as Row | undefined
    if (!lease || lease.owner !== this.owner || Number(lease.expires) <= Date.now()) throw new Error('channel_lease_lost')
  }
  accept(original: InputEvent, settings: ChannelSettings): AcceptResult {
    if (!original.actor.ids.some(id => settings.adminUserIds.includes(id))) return { accepted: false, rejected: true }
    if (settings.allowedChatIds?.length && !settings.allowedChatIds.includes(original.chatId)) return { accepted: false, rejected: true }
    return this.transaction(() => {
      this.owned()
      let event = { ...original, actor: { ...original.actor, authorizedUserId: original.actor.ids.find(id => settings.adminUserIds.includes(id)) } }
      this.db.prepare("INSERT OR REPLACE INTO metadata VALUES('merge_ms',?)").run(String(settings.mergeMs ?? 0))
      this.db.prepare("INSERT OR REPLACE INTO metadata VALUES('max_merge_wait_ms',?)").run(String(settings.maxMergeWaitMs ?? 10000))
      let action: Row | undefined
      if (event.actionToken) {
        action = this.db.prepare('SELECT * FROM actions WHERE token=?').get(event.actionToken) as Row | undefined
        if (!action || action.chat_id !== event.chatId || action.message_id !== event.messageId || Number(action.expires) <= Date.now() ||
          !(JSON.parse(String(action.actor)) as string[]).some(id => event.actor.ids.includes(id))) return { accepted: false, rejected: true }
        event = { ...event, threadId: String(action.thread_id) }
      }
      const keys = [`event:${settings.appId}:${event.eventId}`, event.actionToken ? `action:${event.actionToken}` : `message:${settings.appId}:${event.messageId}`]
      if (keys.some(key => this.db.prepare('SELECT 1 FROM received WHERE key=?').get(key))) return { accepted: true, duplicate: true }
      if (action?.consumed) return { accepted: false, rejected: true }
      const count = this.db.prepare("SELECT count(*) AS n FROM inbox WHERE state IN ('pending','running','waiting')").get() as Row
      const control = event.kind !== 'text' || event.text.startsWith('/')
      if (Number(count.n) >= settings.queueLimit + (control ? 32 : 0)) return { accepted: false, full: true }
      const sessionKey = hash([settings.appId, event.chatId, event.threadId])
      if (!this.db.prepare('SELECT 1 FROM sessions WHERE key=?').get(sessionKey)) {
        const id = randomUUID()
        const spec: ConversationSpec = { id, cwd: settings.workspace, allowCreate: true, provider: settings.provider, model: settings.model, preset: settings.preset }
        this.db.prepare('INSERT INTO sessions (key,id,spec) VALUES (?,?,?)').run(sessionKey, id, JSON.stringify(spec))
      }
      const id = randomUUID()
      let pinned: string|null=null
      if(action){const source=this.db.prepare('SELECT * FROM inbox WHERE id=?').get(action.inbox_id!) as Row|undefined;if(!source)return{accepted:false,rejected:true};pinned=JSON.stringify(this.entry(source).spec)}
      const answer=/^\/answer\s+(\S+)\s/.exec(event.text)
      if(!pinned&&answer){const source=this.db.prepare('SELECT i.* FROM actions a JOIN inbox i ON i.id=a.inbox_id WHERE a.token=? AND a.session_key=?').get(answer[1],sessionKey) as Row|undefined;if(source&&(JSON.parse(String(source.actor)) as InputEvent['actor']).authorizedUserId===event.actor.authorizedUserId)pinned=JSON.stringify(this.entry(source).spec)}
      if(event.targetSessionId&&!pinned){
        const current=this.currentSpec(sessionKey)
        const source=this.db.prepare("SELECT * FROM inbox WHERE session_key=? AND json_extract(pinned_spec,'$.id')=? AND json_extract(actor,'$.authorizedUserId')=? ORDER BY seq DESC LIMIT 1").get(sessionKey,event.targetSessionId,event.actor.authorizedUserId!) as Row|undefined
        if(event.targetSessionId===current.id)pinned=JSON.stringify(current)
        else if(source&&this.entry(source).spec.cwd===current.cwd)pinned=JSON.stringify(this.entry(source).spec)
        else return{accepted:false,rejected:true}
      }
      this.db.prepare(`INSERT INTO inbox (id,input_id,event_id,message_id,chat_id,thread_id,actor,kind,text,session_key,created)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)`).run(id, randomUUID(), event.eventId, event.messageId, event.chatId, event.threadId, JSON.stringify(event.actor), event.kind, event.text, sessionKey, Date.now())
      if(pinned)this.db.prepare('UPDATE inbox SET pinned_spec=? WHERE id=?').run(pinned,id)
      for (const key of keys) this.db.prepare('INSERT INTO received VALUES (?,?)').run(key, id)
      if (event.actionToken) this.db.prepare('UPDATE actions SET consumed=1 WHERE token=?').run(event.actionToken)
      return { accepted: true }
    })
  }
  private entry(row: Row): InboxEntry {
    const mapping = this.db.prepare('SELECT spec,initialized FROM sessions WHERE key=?').get(row.session_key!) as Row
    const spec = JSON.parse(String(row.pinned_spec??mapping.spec)) as ConversationSpec
    return {
      id: String(row.id), inputId: String(row.input_id), eventId: String(row.event_id), messageId: String(row.message_id),
      chatId: String(row.chat_id), threadId: String(row.thread_id), actor: JSON.parse(String(row.actor)),
      kind: row.kind as InboxEntry['kind'], text: String(row.text), sessionKey: String(row.session_key), state: String(row.state),
      spec: { ...spec, allowCreate: row.pinned_spec?spec.allowCreate:!mapping.initialized },
    }
  }
  running(): InboxEntry[] {
    return (this.db.prepare("SELECT * FROM inbox WHERE state IN ('running','waiting') ORDER BY seq").all() as Row[]).map(row => this.entry(row))
  }
  actorFor(event:InputEvent,settings:ChannelSettings):TrustedActor|undefined{
    const userId=event.actor.ids.find(id=>settings.adminUserIds.includes(id))
    if(!userId||(settings.allowedChatIds?.length&&!settings.allowedChatIds.includes(event.chatId)))return
    const row=this.db.prepare('SELECT spec FROM sessions WHERE key=?').get(hash([settings.appId,event.chatId,event.threadId])) as Row|undefined
    if(!row)return
    const spec=JSON.parse(String(row.spec)) as ConversationSpec
    const related=this.db.prepare("SELECT DISTINCT json_extract(pinned_spec,'$.id') AS id FROM inbox WHERE session_key=? AND json_extract(pinned_spec,'$.cwd')=? AND json_extract(actor,'$.authorizedUserId')=? AND pinned_spec IS NOT NULL ORDER BY seq DESC LIMIT 100").all(hash([settings.appId,event.chatId,event.threadId]),spec.cwd,userId) as Row[]
    return{userId,chatId:event.chatId,threadId:event.threadId,sessionId:spec.id,cwd:spec.cwd,privateChat:Boolean(event.actor.privateChat),origin:'feishu',relatedSessionIds:related.map(row=>String(row.id))}
  }
  busy(): boolean {
    return Boolean(this.db.prepare("SELECT 1 FROM inbox WHERE state IN ('pending','running','waiting') LIMIT 1").get() || this.db.prepare("SELECT 1 FROM outbox WHERE state IN ('pending','sending') LIMIT 1").get())
  }
  claim(excluded: readonly string[], controlsOnly = false, waiting:readonly string[]=[]): InboxEntry | undefined {
    return this.transaction(() => {
      this.owned()
      const rows = this.db.prepare(`SELECT i.*,s.id AS session_id FROM inbox i JOIN sessions s ON s.key=i.session_key
        WHERE i.state='pending' ORDER BY i.seq`).all() as Row[]
      const mergeMs = Number(this.db.prepare("SELECT value FROM metadata WHERE key='merge_ms'").get()?.value ?? 0)
      const maxWait = Number(this.db.prepare("SELECT value FROM metadata WHERE key='max_merge_wait_ms'").get()?.value ?? 10000)
      const candidates = (first: Row) => {
        const batch: Row[] = []; let length = 0
        for (const candidate of rows.filter(item => item.session_key === first.session_key && Number(item.seq) >= Number(first.seq))) {
          if (candidate.kind !== 'text' || String(candidate.text).startsWith('/') || candidate.actor !== first.actor || length + String(candidate.text).length > 40000) break
          batch.push(candidate); length += String(candidate.text).length
        }
        return batch
      }
      const row = rows.find(item => {
        const control = item.kind !== 'text' || String(item.text).startsWith('/')
        if (control) return true
        if (controlsOnly || excluded.includes(String(item.session_id))) return false
        if (mergeMs && Date.now() - Number(item.created) < maxWait) {
          const batch = candidates(item), last = batch.at(-1) ?? item
          if (Date.now() - Number(last.created) < mergeMs) return false
        }
        const prior=this.db.prepare("SELECT * FROM inbox WHERE session_key=? AND seq<? AND state IN ('pending','running','waiting','unknown')").all(item.session_key!,item.seq!) as Row[]
        return !prior.some(row=>{const spec=this.entry(row).spec;return spec.id===item.session_id&&!waiting.includes(spec.id)})
      })
      if (!row) return undefined
      if (mergeMs && row.kind === 'text' && !String(row.text).startsWith('/')) {
        const batch = candidates(row)
        if (batch.length > 1) {
          row.text = batch.map(item => item.text).join('\n')
          this.db.prepare('UPDATE inbox SET text=? WHERE id=?').run(row.text, row.id!)
          for (const merged of batch.slice(1)) this.db.prepare("UPDATE inbox SET state='merged' WHERE id=?").run(merged.id!)
        }
      }
      this.db.prepare("UPDATE inbox SET state='running' WHERE id=?").run(row.id!)
      row.state='running';const result=this.entry(row)
      this.db.prepare('UPDATE inbox SET pinned_spec=? WHERE id=?').run(JSON.stringify(result.spec),row.id!)
      return result
    })
  }
  markMapped(id: string): void { this.owned(); this.db.prepare('UPDATE sessions SET initialized=1 WHERE id=?').run(id) }
  currentSpec(sessionKey:string):ConversationSpec{const row=this.db.prepare('SELECT spec,initialized FROM sessions WHERE key=?').get(sessionKey) as Row;return{...JSON.parse(String(row.spec)),allowCreate:!row.initialized}}
  waiting(entry:InboxEntry){this.owned();this.db.prepare("UPDATE inbox SET state='waiting' WHERE id=? AND state='running'").run(entry.id)}
  resumeQuestion(entry:InboxEntry){if(this.closed)return;this.owned();this.db.prepare("UPDATE inbox SET state='running' WHERE id=? AND state='waiting'").run(entry.id)}
  continuation(entry:InboxEntry):ConversationSpec{return{...entry.spec,id:randomUUID(),parentSessionId:entry.spec.id,allowCreate:true}}
  commitContinuation(entry:InboxEntry,spec:ConversationSpec){this.transaction(()=>{this.owned();if(spec.parentSessionId!==entry.spec.id||spec.cwd!==entry.spec.cwd)throw new Error('invalid_continuation');const changed=this.db.prepare('UPDATE sessions SET id=?,spec=?,initialized=1 WHERE key=? AND id=?').run(spec.id,JSON.stringify(spec),entry.sessionKey,entry.spec.id);if(changed.changes!==1)throw new Error('channel_mapping_changed');this.db.prepare('UPDATE inbox SET pinned_spec=? WHERE id=?').run(JSON.stringify({...spec,allowCreate:false}),entry.id)})}
  updateMapping(entry: InboxEntry, spec: ConversationSpec): void {
    this.transaction(() => {
      this.owned()
      if (spec.id !== entry.spec.id && this.status(entry.sessionKey).pending) throw new Error('workspace_has_pending_requests')
      const result = this.db.prepare('UPDATE sessions SET id=?,spec=?,initialized=1 WHERE key=? AND id=?')
        .run(spec.id, JSON.stringify(spec), entry.sessionKey, entry.spec.id)
      if (result.changes !== 1) throw new Error('channel_mapping_changed')
    })
  }
  finish(entry: InboxEntry, state: string, outputs: readonly Pick<Outbound, 'msgType' | 'content'>[]): void {
    this.transaction(() => {
      this.owned()
      const current = this.db.prepare('SELECT state FROM inbox WHERE id=?').get(entry.id) as Row
      if (!['running','waiting'].includes(String(current.state))) return
      const progress=this.db.prepare("SELECT * FROM outbox WHERE inbox_id=? AND purpose='progress' ORDER BY seq LIMIT 1").get(entry.id) as Row|undefined
      this.db.prepare("UPDATE outbox SET state='superseded' WHERE inbox_id=? AND purpose='progress-update' AND state='pending'").run(entry.id)
      let promoted=false
      if(progress?.state==='pending'){
        if(progress.attempts===0&&outputs[0]?.msgType==='post'){
          this.db.prepare("UPDATE outbox SET content=?,purpose='message' WHERE id=?").run(outputs[0].content,progress.id!);promoted=true
        }else this.db.prepare("UPDATE outbox SET state='superseded' WHERE id=?").run(progress.id!)
      }
      for (const [index,output] of outputs.entries()){
        if(index===0&&promoted)continue
        this.insert(entry,output,index===0&&progress&&output.msgType==='post'?'reply':'message')
      }
      this.db.prepare('UPDATE inbox SET state=? WHERE id=?').run(state, entry.id)
    })
  }
  private insert(entry:InboxEntry,output:Pick<Outbound,'msgType'|'content'>,purpose:string){
    const id=randomUUID()
    this.db.prepare('INSERT INTO outbox(id,inbox_id,chat_id,reply_to,thread_id,msg_type,content,purpose) VALUES(?,?,?,?,?,?,?,?)')
      .run(id,entry.id,entry.chatId,entry.actionToken?'':entry.messageId,entry.threadId,output.msgType,output.content,purpose)
    return id
  }
  progress(entry:InboxEntry,content:string):void{
    this.transaction(()=>{
      this.owned()
      if(!['running','waiting'].includes(String((this.db.prepare('SELECT state FROM inbox WHERE id=?').get(entry.id) as Row)?.state)))return
      const first=this.db.prepare("SELECT * FROM outbox WHERE inbox_id=? AND purpose='progress' ORDER BY seq LIMIT 1").get(entry.id) as Row|undefined
      if(!first){this.insert(entry,{msgType:'post',content},'progress');return}
      if(['failed','unknown','superseded'].includes(String(first.state)))return
      if(this.db.prepare("SELECT 1 FROM outbox WHERE inbox_id=? AND purpose='progress-update' AND state='unknown' LIMIT 1").get(entry.id))return
      const latest=this.db.prepare("SELECT * FROM outbox WHERE inbox_id=? AND purpose IN ('progress','progress-update') ORDER BY seq DESC LIMIT 1").get(entry.id) as Row
      if(latest.content===content)return
      if(latest.state==='pending'&&latest.attempts===0)this.db.prepare('UPDATE outbox SET content=? WHERE id=?').run(content,latest.id!)
      else this.insert(entry,{msgType:'post',content},'progress-update')
    })
  }
  enqueue(entry: InboxEntry, outputs: readonly Pick<Outbound, 'msgType' | 'content'>[]): string[] {
    return this.transaction(() => {
      this.owned()
      const ids: string[] = []
      for (const output of outputs) {
        const id = randomUUID(); ids.push(id)
        this.db.prepare(`INSERT INTO outbox (id,inbox_id,chat_id,reply_to,thread_id,msg_type,content) VALUES(?,?,?,?,?,?,?)`)
          .run(id, entry.id, entry.chatId, entry.messageId, entry.threadId, output.msgType, output.content)
      }
      return ids
    })
  }
  source(actor: TrustedActor): InboxEntry {
    const row = actor.sourceId ? this.db.prepare('SELECT * FROM inbox WHERE id=?').get(actor.sourceId) as Row | undefined : undefined
    if (!row) throw new Error('durable_delivery_source_required')
    const entry = this.entry(row)
    if (entry.chatId !== actor.chatId || entry.threadId !== (actor.threadId ?? '') || entry.actor.authorizedUserId !== actor.userId) throw new Error('delivery_source_mismatch')
    // Delayed work retains its trusted creation-time workspace after chat /cd.
    return { ...entry, spec: { ...entry.spec, id: actor.sessionId, cwd: actor.cwd } }
  }
  enqueueOnce(key: string, actor: TrustedActor, outputs: readonly Pick<Outbound, 'msgType' | 'content'>[]) {
    return this.transaction(() => {
      this.owned()
      const entry = this.source(actor), digest = hash([actor, outputs]), previous = this.db.prepare('SELECT * FROM external_delivery WHERE id=?').get(key) as Row | undefined
      if (previous) { if (previous.digest !== digest) throw new Error('delivery_key_collision'); return JSON.parse(String(previous.outbox_ids)) as string[] }
      const ids = outputs.map(output => { const id = randomUUID(); this.db.prepare('INSERT INTO outbox(id,inbox_id,chat_id,reply_to,thread_id,msg_type,content) VALUES(?,?,?,?,?,?,?)').run(id,entry.id,entry.chatId,entry.messageId,entry.threadId,output.msgType,output.content);return id })
      this.db.prepare('INSERT INTO external_delivery VALUES(?,?,?)').run(key,digest,JSON.stringify(ids))
      return ids
    })
  }
  deliveries(ids: string[]) { return ids.map(id => this.db.prepare('SELECT id,state FROM outbox WHERE id=?').get(id) as { id: string; state: string }).filter(Boolean) }
  externalStatus(actor:TrustedActor,ids:unknown){
    const source=this.source(actor)
    if(!Array.isArray(ids)||ids.length>1000||ids.some(id=>typeof id!=='string'))throw new Error('delivery_ids_required')
    return ids.map(id=>{const row=this.db.prepare('SELECT id,state,attempts,remote_id,error_code,msg_type,uploaded_key FROM outbox WHERE id=? AND inbox_id=?').get(id,source.id) as Row|undefined;if(!row)throw new Error('delivery_not_owned');return{id:row.id,state:row.state,attempts:row.attempts,messageId:row.remote_id,error:row.error_code,type:row.msg_type,...(row.msg_type==='file'?{upload:row.uploaded_key?'uploaded':'not_confirmed'}:{})}})
  }
  retryExternal(actor:TrustedActor,ids:unknown){return this.transaction(()=>{this.owned();const rows=this.externalStatus(actor,ids);for(const row of rows)if(row.state==='failed')this.db.prepare("UPDATE outbox SET state='pending',attempts=0,next_at=0 WHERE id=? AND state='failed'").run(row.id);return this.externalStatus(actor,ids)})}
  uploaded(message:Outbound,key:string){this.owned();if(!key||key.length>1024)throw new Error('invalid_upload_receipt');if(this.db.prepare("UPDATE outbox SET uploaded_key=? WHERE id=? AND state='sending' AND msg_type='file' AND (uploaded_key IS NULL OR uploaded_key=?)").run(key,message.id,key).changes!==1)throw new Error('upload_receipt_mismatch')}
  question(entry: InboxEntry, question: ChannelQuestion): string {
    this.owned()
    this.db.prepare('INSERT INTO actions (token,inbox_id,session_key,actor,chat_id,thread_id,expires) VALUES(?,?,?,?,?,?,?)')
      .run(question.token, entry.id, entry.sessionKey, JSON.stringify(entry.actor.ids), entry.chatId, entry.threadId, Date.now() + 300000)
    return JSON.stringify({ header: { title: { tag: 'plain_text', content: question.approval ? '请求操作授权' : '需要你的回答' } }, elements: [
      { tag: 'div', text: { tag: 'lark_md', content: question.question + `\n${question.approval?'可直接回复“允许一次”或“拒绝”':'可直接回复选项或编号；自由回答请说“回答：内容”'}，也可使用 /answer ${question.token} <回答>。可直接发送新问题，此卡片仍保留，5 分钟未回答会失效。` } },
      ...(question.choices.length ? [{ tag: 'action', actions: question.choices.map(choice => ({ tag: 'button', text: { tag: 'plain_text', content: choice }, value: { action: 'telebot.answer', token: question.token, answer: choice } })) }] : []),
    ] })
  }
  claimDelivery(now: number): Outbound | undefined {
    return this.transaction(() => {
      this.owned()
      let row:Row|undefined
      while(true){
      row = this.db.prepare(`SELECT * FROM outbox o WHERE state='pending' AND next_at<=?
        AND NOT(o.purpose='progress-update' AND EXISTS(SELECT 1 FROM outbox s WHERE s.inbox_id=o.inbox_id AND s.purpose='progress' AND s.state IN ('pending','sending')))
        AND NOT(o.purpose='reply' AND EXISTS(SELECT 1 FROM outbox s WHERE s.inbox_id=o.inbox_id AND s.purpose='progress' AND s.state='sending')) AND NOT EXISTS
        (SELECT 1 FROM outbox p WHERE p.inbox_id=o.inbox_id AND p.seq<o.seq AND p.state!='sent'
          AND p.state!='superseded' AND p.purpose NOT IN ('progress','progress-update')
          AND NOT(p.msg_type='interactive' AND json_extract(p.content,'$.header.title.content')='执行进度')) ORDER BY seq LIMIT 1`).get(now) as Row | undefined
      if (!row) return undefined
      if(row.purpose==='progress-update'||row.purpose==='reply'){
        const first=this.db.prepare("SELECT * FROM outbox WHERE inbox_id=? AND purpose='progress' ORDER BY seq LIMIT 1").get(row.inbox_id!) as Row|undefined
        const uncertainEdit=this.db.prepare("SELECT 1 FROM outbox WHERE inbox_id=? AND purpose='progress-update' AND state='unknown' LIMIT 1").get(row.inbox_id!)
        if(first?.state==='sent'&&first.remote_id&&!uncertainEdit){row.edit_id=first.remote_id;this.db.prepare('UPDATE outbox SET edit_id=? WHERE id=?').run(first.remote_id,row.id!)}
        else if(row.purpose==='progress-update'){
          if(first&&['pending','sending'].includes(String(first.state)))return undefined
          this.db.prepare("UPDATE outbox SET state='superseded' WHERE id=?").run(row.id!);continue
        }
      }
      break
      }
      this.db.prepare("UPDATE outbox SET state='sending',attempts=attempts+1 WHERE id=?").run(row.id!)
      return { id: String(row.id), chatId: String(row.chat_id), replyTo: String(row.reply_to), threadId: String(row.thread_id),
        msgType: row.msg_type as Outbound['msgType'], content: String(row.content), attempts: Number(row.attempts) + 1,...(row.uploaded_key?{uploadedFileKey:String(row.uploaded_key)}:{}),...(row.edit_id?{editMessageId:String(row.edit_id)}:{}) }
    })
  }
  settleDelivery(message: Outbound, result: DeliveryResult, retryAt: number, maxAttempts: number): void {
    this.transaction(() => {
      this.owned()
      const state = result.status === 'retry' ? (message.attempts < maxAttempts ? 'pending' : 'failed') : result.status
      this.db.prepare('UPDATE outbox SET state=?,next_at=?,remote_id=?,error_code=? WHERE id=?').run(
        state, retryAt, result.status === 'sent' ? result.messageId : null, result.status === 'sent' ? null : result.code, message.id)
      if (result.status === 'sent'&&message.msgType==='interactive') this.db.prepare(`UPDATE actions SET message_id=? WHERE inbox_id=(SELECT inbox_id FROM outbox WHERE id=?) AND message_id IS NULL`).run(result.messageId, message.id)
    })
  }
  recoverDeliveries(): void { this.db.prepare("UPDATE outbox SET state='unknown',error_code='process_stopped_during_send' WHERE state='sending'").run() }
  status(sessionKey: string) {
    const pending = this.db.prepare("SELECT count(*) AS n FROM inbox WHERE session_key=? AND state='pending'").get(sessionKey) as Row
    const unknown = this.db.prepare("SELECT count(*) AS n FROM inbox WHERE session_key=? AND state='unknown'").get(sessionKey) as Row
    const uncertainDelivery = this.db.prepare("SELECT count(*) AS n FROM outbox o JOIN inbox i ON o.inbox_id=i.id WHERE i.session_key=? AND o.state='unknown' AND o.purpose NOT IN ('progress','progress-update')").get(sessionKey) as Row
    return { pending: Number(pending.n), unknown: Number(unknown.n) + Number(uncertainDelivery.n) }
  }
  statusCard(entry: InboxEntry, text: string, ttlMs: number): string {
    const token = randomUUID()
    this.db.prepare('INSERT INTO actions (token,inbox_id,session_key,actor,chat_id,thread_id,expires) VALUES (?,?,?,?,?,?,?)')
      .run(token, entry.id, entry.sessionKey, JSON.stringify(entry.actor.ids), entry.chatId, entry.threadId, Date.now() + ttlMs)
    return JSON.stringify({ header: { title: { tag: 'plain_text', content: 'Harness 会话状态' } }, elements: [
      { tag: 'div', text: { tag: 'lark_md', content: text } },
      { tag: 'action', actions: [{ tag: 'button', text: { tag: 'plain_text', content: '刷新状态' }, value: { action: 'telebot.status', token } }] },
    ] })
  }
  release(): void {
    if (this.closed) return
    this.db.prepare('DELETE FROM lease WHERE owner=?').run(this.owner)
    this.db.close(); this.closed = true
  }
}
