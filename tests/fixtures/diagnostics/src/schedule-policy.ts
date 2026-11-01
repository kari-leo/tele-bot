/** P0 business-policy prototype only. Official schedule remains the sole clock. */
import { DatabaseSync } from 'node:sqlite'
import { randomUUID } from 'node:crypto'
export interface OldRule { kind: 'once' | 'daily' | 'weekly'; interval: number; weekdays?: number[]; count?: number; until?: string }
export const isoWeekday = (old: number) => { if (!Number.isInteger(old) || old < 0 || old > 6) throw new Error('invalid_old_weekday'); return old + 1 }
export function nextWake(planned: string, rule: OldRule, consumed: number): { at: string } | undefined {
  if (!Number.isInteger(rule.interval) || rule.interval < 1 || rule.interval > 52) throw new Error('invalid_interval')
  if (rule.kind === 'once' || (rule.count !== undefined && consumed >= rule.count)) return undefined
  const start = Date.parse(planned), offset = 8 * 3600000, day = 86400000
  if (!Number.isFinite(start)) throw new Error('invalid_planned_time')
  const local = new Date(start + offset), weekday = (local.getUTCDay() + 6) % 7
  let distance = rule.interval
  if (rule.kind === 'weekly') {
    if (!rule.weekdays?.length) throw new Error('weekdays_required')
    rule.weekdays.forEach(isoWeekday)
    distance = 1
    while (distance <= 7 * rule.interval + 7) {
      const candidateWeekday = (weekday + distance) % 7, weekDistance = Math.floor((weekday + distance) / 7)
      if (rule.weekdays.includes(candidateWeekday) && weekDistance % rule.interval === 0) break
      distance++
    }
  }
  const at = new Date(start + distance * day).toISOString()
  if (rule.until && Date.parse(at) > Date.parse(rule.until)) return undefined
  return { at } // Caller hands this one-shot target back to the official create API.
}
export function missedDecision(mode: 'remind' | 'auto' | 'confirm', policy: 'skip' | 'run_once' | 'ask', planned: number, now: number, overlap = false) {
  if (overlap || (now - planned > 300000 && policy === 'skip')) return 'skipped'
  if (mode === 'remind') return 'reminded'
  if (mode === 'confirm' || (now - planned > 300000 && policy === 'ask')) return 'awaiting_auth'
  return 'queued'
}
export class PrototypeOccurrences {
  private readonly db: DatabaseSync
  private readonly owner = randomUUID()
  constructor(filename: string) {
    this.db = new DatabaseSync(filename)
    this.db.exec(`PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL;
      CREATE TABLE IF NOT EXISTS dispatcher(id INTEGER PRIMARY KEY,owner TEXT,expires INTEGER);
      CREATE TABLE IF NOT EXISTS occurrences(id TEXT PRIMARY KEY,task TEXT,planned TEXT,version INTEGER,state TEXT,receipt TEXT);`)
    this.db.exec('BEGIN IMMEDIATE')
    const row = this.db.prepare('SELECT * FROM dispatcher WHERE id=1').get() as any
    if (row && row.expires > Date.now()) { this.db.exec('ROLLBACK'); this.db.close(); throw new Error('single_dispatcher_required') }
    this.db.prepare('INSERT OR REPLACE INTO dispatcher VALUES(1,?,?)').run(this.owner, Date.now() + 60000)
    this.db.exec("UPDATE occurrences SET state='unknown' WHERE state='running'; COMMIT;")
  }
  claim(task: string, version: number, expectedAt: string, receipt: { scheduledAt: string; messageId: string } | undefined, authorized: boolean) {
    if (!receipt || receipt.scheduledAt !== expectedAt || !receipt.messageId || !authorized) throw new Error('verified_occurrence_authorization_required')
    const key = JSON.stringify([task, expectedAt, version])
    return this.db.prepare('INSERT OR IGNORE INTO occurrences VALUES(?,?,?,?,?,?)').run(key, task, expectedAt, version, 'running', receipt.messageId).changes === 1
  }
  finish(task: string, version: number, planned: string) { this.db.prepare("UPDATE occurrences SET state='completed' WHERE id=? AND state='running'").run(JSON.stringify([task, planned, version])) }
  history(task: string) { return this.db.prepare('SELECT * FROM occurrences WHERE task=?').all(task) }
  deleteTask(_task: string) { /* Historical occurrence rows deliberately survive task deletion. */ }
  close() { this.db.prepare('DELETE FROM dispatcher WHERE owner=?').run(this.owner); this.db.close() }
}
