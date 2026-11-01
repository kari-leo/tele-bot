/** Official schedule is the only clock. Channel rows hold delivery/audit facts only. */
import type { Context } from '@deepseek-ai/cordis'
import type { Agent, AgentHandle } from '@deepseek-ai/dsh-agent'
import { SessionId } from '@deepseek-ai/dsh-session'
import type {} from '@deepseek-ai/dsh-session-persistence'
import type { ScheduleCreateRequest } from '@deepseek-ai/dsh-schedule'
import { ScheduleId } from '@deepseek-ai/dsh-schedule'
import type {} from '@deepseek-ai/dsh-agent-default-model'
import { defineTool } from '@deepseek-ai/dsh-tools'
import { DatabaseSync } from 'node:sqlite'
import { randomUUID } from 'node:crypto'
import type { ChannelRepository, InboxEntry } from '../domain/types.js'
import {receiptPreset} from '@telebot/core/presets'
import type {} from '@deepseek-ai/dsh-agent-preset-registry'

export class Reminders {
  private readonly db: DatabaseSync
  private readonly handles = new Map<string, AgentHandle>()
  private readonly disposers: (() => void)[] = []
  private reconciling: Promise<void> = Promise.resolve()
  private closed = false
  private readonly presetId='telebot-feishu-receipts'
  private readonly ready:Promise<void>
  private disposePreset:(()=>Promise<void>)|undefined
  constructor(private readonly ctx: Context, filename: string, private readonly channel: ChannelRepository,private readonly authorized:(entry:InboxEntry)=>boolean=()=>false) {
    this.db = new DatabaseSync(filename)
    this.db.exec(`PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL;
      CREATE TABLE IF NOT EXISTS reminders(id TEXT PRIMARY KEY,session TEXT NOT NULL,entry TEXT NOT NULL,text TEXT NOT NULL,state TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS deliveries(id TEXT PRIMARY KEY,state TEXT NOT NULL,outbox_ids TEXT);
      UPDATE deliveries SET state='unknown' WHERE state='sending';`)
    if (!(this.db.prepare('PRAGMA table_info(deliveries)').all() as any[]).some(row => row.name === 'outbox_ids')) this.db.exec('ALTER TABLE deliveries ADD COLUMN outbox_ids TEXT')
    this.disposers.push(ctx.tools.register(defineTool({ name: 'telebot_reminder_receipt', description: 'No-op reminder receipt; no external effects.', parameters: {},
      output: { schema: { type: 'object', properties: { received: { type: 'boolean', required: true } }, additionalProperties: false }, render: () => [{ type: 'text', text: 'Reminder received.' }] }, async execute() { return { received: true } } })))
    this.ready=receiptPreset(ctx,this.presetId,['telebot_reminder_receipt']).then(dispose=>{this.disposePreset=dispose})
    this.disposers.push(ctx.on('schedule/changed', () => { this.reconciling = this.reconciling.catch(() => {}).then(() => this.reconcile()); this.reconciling.catch(() => {}) }))
    this.reconciling = this.ready.then(()=>this.reconcile()); this.reconciling.catch(() => {})
  }
  private schedule() { const service = this.ctx.get('schedule') as Context['schedule'] | undefined; if (!service) throw new Error('official_schedule_required'); return service }
  private async agent(entry: InboxEntry, id: string): Promise<Agent> {
    const agents = this.ctx.get('agents') as Context['agents']
    const live = agents.get(SessionId(id)); if (live) return live
    const persistence = this.ctx.get('sessionPersistence') as Context['sessionPersistence']
    const exists = await persistence.stat(SessionId(id))
    const setup = async (scope: Context) => { await (this.ctx.get('agentPresets') as Context['agentPresets']).mount(scope,this.presetId) }
    const selection = (this.ctx.get('agentDefaultModel') as Context['agentDefaultModel'] | undefined)?.currentSelection()
    const route = { provider: entry.spec.provider ?? selection?.provider, model: entry.spec.model ?? selection?.model }
    const handle = exists ? await agents.resume({ resumeSessionId: SessionId(id), setup })
      : await agents.create({ sessionId: SessionId(id), meta: { cwd: entry.spec.cwd,agentPreset:this.presetId }, agentOptions: route, setup })
    this.handles.set(id, handle)
    return handle.agent
  }
  async command(entry: InboxEntry) {
    await this.ready
    const text = entry.text.slice('/remind'.length).trim(), [action, ...rest] = text.split(/\s+/)
    if (action === 'list') {
      const rows = this.db.prepare("SELECT id,text,state,entry FROM reminders WHERE state!='deleted'").all() as any[]
      return JSON.stringify(rows.filter(row => this.owned(entry, JSON.parse(row.entry))).map(({ id, text, state }) => ({ id, text, scheduleState: state,
        deliveries: (this.db.prepare('SELECT id,state,outbox_ids FROM deliveries').all() as any[]).filter(row => JSON.parse(row.id)[0] === id).map(row => ({ occurrence: JSON.parse(row.id)[1], state: row.state,
          outbox: row.outbox_ids ? this.channel.deliveries?.(JSON.parse(row.outbox_ids)) ?? [] : [] })),
      })))
    }
    if (action === 'delete') {
      const id = rest.join(''), row = this.db.prepare('SELECT entry FROM reminders WHERE id=?').get(id) as any
      if (!row || !this.owned(entry, JSON.parse(row.entry))) throw new Error('reminder_not_owned')
      await this.schedule().delete({ id: ScheduleId(id), sessionId: SessionId(JSON.parse(row.entry).reminderSession) })
      this.db.prepare("UPDATE reminders SET state='deleted' WHERE id=?").run(id)
      return '提醒已删除，业务投递历史保留。'
    }
    if (action !== 'create') return '/remind create {"text":"提醒内容","after_seconds":60}；/remind list；/remind delete <id>'
    const request = JSON.parse(rest.join(' ')), textValue = String(request.text ?? '').trim()
    if (!textValue || textValue.length > 10000) throw new Error('reminder_text_required')
    const session = randomUUID(), stored = { ...entry, reminderSession: session }
    await this.agent(entry, session)
    const { text: _text, ...timing } = request
    const record = await this.schedule().create(SessionId(session), { ...timing, title: textValue.slice(0, 120), prompt: 'Reminder only. Do not execute tools or external operations.' } as ScheduleCreateRequest)
    this.db.prepare('INSERT INTO reminders VALUES(?,?,?,?,?)').run(record.id, session, JSON.stringify(stored), textValue, 'active')
    // A create might already be due after persistence; do not wait for another change event.
    await this.reconcile()
    return JSON.stringify({ id: record.id, text: textValue, scheduledAt: record.scheduledAt, status: 'scheduled' })
  }
  private owned(entry: InboxEntry, original: InboxEntry) { return entry.chatId === original.chatId && entry.threadId === original.threadId && entry.actor.ids.some(id => original.actor.ids.includes(id)) }
  private async reconcile() {
    if (this.closed) return
    const catalog = await this.schedule().catalog()
    for (const row of this.db.prepare("SELECT * FROM reminders WHERE state='active'").all() as any[]) {
      const task = catalog.find(task => task.id === row.id), entry = JSON.parse(row.entry) as InboxEntry
      if(!this.authorized(entry)){if(task)await this.schedule().delete({id:ScheduleId(row.id),sessionId:SessionId(row.session)});this.db.prepare("UPDATE reminders SET state='revoked' WHERE id=?").run(row.id);continue}
      const history = await this.schedule().history({ id: ScheduleId(row.id), sessionId: SessionId(row.session), limit: 100 })
      if (!('records' in history)) continue
      for (const receipt of [...history.records].reverse()) {
        const key = JSON.stringify([row.id, receipt.scheduledAt])
        if (this.db.prepare('SELECT 1 FROM deliveries WHERE id=?').get(key)) continue
        this.db.prepare('INSERT INTO deliveries(id,state) VALUES(?,?)').run(key, 'sending')
        const ids = this.channel.enqueue?.(entry, [{ msgType: 'post', content: JSON.stringify({ zh_cn: { content: [[{ tag: 'text', text: `定时提醒：${row.text}\n计划时间：${receipt.scheduledAt}` }]] } }) }])
        this.db.prepare("UPDATE deliveries SET state='queued',outbox_ids=? WHERE id=?").run(JSON.stringify(ids ?? []), key)
      }
      if (task?.status === 'inactive') this.db.prepare("UPDATE reminders SET state='delivered_to_channel' WHERE id=?").run(row.id)
    }
  }
  async dispose() { if (this.closed) return; this.closed = true; for (const dispose of this.disposers.reverse()) dispose(); await this.reconciling.catch(() => {}); await Promise.allSettled([...this.handles.values()].map(handle => handle.dispose())); await this.disposePreset?.();this.db.close() }
}
