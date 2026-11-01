import { DatabaseSync } from 'node:sqlite'
import { createHash, randomUUID } from 'node:crypto'
import type { TrustedActor } from '@telebot/core/contracts'
import type { Operation } from '../domain/resources.js'
import { Resources } from '../domain/resources.js'

const digest = (value: unknown) => createHash('sha256').update(JSON.stringify(value)).digest('hex')
const binding = (actor: TrustedActor) => [actor.userId, actor.chatId, actor.sessionId, actor.cwd]
/** This is a business operation ledger, not a host task/timer or a token store. */
export class OperationLedger {
  private readonly db: DatabaseSync
  private readonly owner = randomUUID()
  constructor(filename: string) {
    this.db = new DatabaseSync(filename)
    this.db.exec(`PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL; PRAGMA busy_timeout=5000;
      CREATE TABLE IF NOT EXISTS operations(token TEXT PRIMARY KEY,binding TEXT NOT NULL,operation TEXT NOT NULL,baseline TEXT NOT NULL,digest TEXT NOT NULL,expires INTEGER NOT NULL,state TEXT NOT NULL,result TEXT);
      CREATE TABLE IF NOT EXISTS writer(id INTEGER PRIMARY KEY,pid INTEGER,owner TEXT);`)
    this.db.exec('BEGIN IMMEDIATE')
    const writer = this.db.prepare('SELECT * FROM writer WHERE id=1').get() as any
    let alive = false
    if (writer) { try { process.kill(writer.pid, 0); alive = true } catch (error) { if ((error as NodeJS.ErrnoException).code !== 'ESRCH') alive = true } }
    if (alive) { this.db.exec('ROLLBACK'); this.db.close(); throw new Error('business_database_already_active') }
    this.db.prepare('INSERT OR REPLACE INTO writer VALUES(1,?,?)').run(process.pid, this.owner)
    this.db.exec("UPDATE operations SET state='unknown' WHERE state='running'; COMMIT;")
  }
  async preview(resources: Resources, actor: TrustedActor, operation: Operation, signal: AbortSignal) {
    resources.resource(actor, operation)
    if (['inspect', 'query'].includes(operation.action)) return resources.execute(actor, operation, signal)
    const frozen = JSON.parse(JSON.stringify(operation)) as Operation
    frozen.frozenResource = resources.fingerprint(actor, operation)
    const baseline = await resources.snapshots(actor, frozen, signal)
    const token = randomUUID(), hash = digest([binding(actor), frozen, baseline])
    this.db.prepare('INSERT INTO operations VALUES(?,?,?,?,?,?,?,NULL)').run(token, JSON.stringify(binding(actor)), JSON.stringify(frozen), JSON.stringify(baseline), hash, Date.now() + 900000, 'pending')
    return { status: 'confirmation_required', token, digest: hash, expiresInSeconds: 900, preview: frozen, baseline }
  }
  async confirm(resources: Resources, actor: TrustedActor, token: string, signal: AbortSignal) {
    const row = this.db.prepare('SELECT * FROM operations WHERE token=?').get(token) as Record<string, any> | undefined
    if (!row || row.binding !== JSON.stringify(binding(actor)) || row.expires <= Date.now()) throw new Error('confirmation_binding_or_expiry_mismatch')
    if (row.state !== 'pending') return { status: row.state, result: row.result ? JSON.parse(row.result) : null, replayed: false }
    const operation = JSON.parse(row.operation) as Operation
    resources.resource(actor, operation)
    if (operation.frozenResource !== resources.fingerprint(actor, operation)) throw new Error('frozen_resource_changed_requires_new_confirmation')
    if (digest(await resources.snapshots(actor, operation, signal)) !== digest(JSON.parse(row.baseline))) throw new Error('preview_conflict_requires_new_confirmation')
    signal.throwIfAborted()
    const claimed = this.db.prepare("UPDATE operations SET state='running' WHERE token=? AND state='pending'").run(token)
    if (claimed.changes !== 1) throw new Error('operation_already_claimed')
    try {
      const result = await resources.execute(actor, operation, signal)
      let verified: unknown
      try { verified = await resources.verify(actor, operation, result, signal) }
      catch { this.settle(token, 'unknown', { receipt: result, verification: 'unconfirmed' }); return { status: 'unknown', receipt: result } }
      this.settle(token, 'completed', { receipt: result, verified })
      return { status: 'completed', receipt: result, verified }
    } catch (error) {
      const status = error && typeof error === 'object' && 'outcome' in error && error.outcome === 'failed' ? 'failed' : 'unknown'
      this.settle(token, status, { code: error instanceof Error ? error.message : 'operation_unknown' })
      return { status }
    }
  }
  async supplement(resources: Resources, actor: TrustedActor, token: string, fields: Record<string, unknown>, signal: AbortSignal) {
    const row = this.db.prepare('SELECT * FROM operations WHERE token=?').get(token) as any
    if (!row || row.binding !== JSON.stringify(binding(actor)) || row.state !== 'pending' || row.expires <= Date.now()) throw new Error('supplement_binding_or_expiry_mismatch')
    const operation = JSON.parse(row.operation) as Operation
    if (resources.resource(actor, operation).kind !== 'bitable' || !['create', 'update'].includes(operation.action) || operation.params.records?.length !== 1 || !fields || Array.isArray(fields) || !Object.keys(fields).length || Object.keys(fields).some(key => ['__proto__', 'constructor', 'prototype'].includes(key))) throw new Error('single_record_fields_supplement_required')
    if (operation.frozenResource !== resources.fingerprint(actor, operation)) throw new Error('frozen_resource_changed_requires_new_confirmation')
    if (digest(await resources.snapshots(actor, operation, signal)) !== digest(JSON.parse(row.baseline))) throw new Error('preview_conflict_requires_new_confirmation')
    operation.params.records[0].fields = { ...operation.params.records[0].fields, ...fields }
    const baseline = await resources.snapshots(actor, operation, signal), next = randomUUID(), hash = digest([binding(actor), operation, baseline])
    signal.throwIfAborted()
    this.db.exec('BEGIN IMMEDIATE')
    try {
      if (this.db.prepare("UPDATE operations SET state='superseded' WHERE token=? AND state='pending'").run(token).changes !== 1) throw new Error('operation_already_claimed')
      this.db.prepare('INSERT INTO operations VALUES(?,?,?,?,?,?,?,NULL)').run(next, row.binding, JSON.stringify(operation), JSON.stringify(baseline), hash, row.expires, 'pending')
      this.db.exec('COMMIT')
    } catch (error) { this.db.exec('ROLLBACK'); throw error }
    return { status: 'confirmation_required', token: next, digest: hash, expiresAt: row.expires, preview: operation, baseline }
  }
  async reconcile(resources: Resources, actor: TrustedActor, token: string, signal: AbortSignal) {
    const row = this.db.prepare('SELECT * FROM operations WHERE token=? AND binding=?').get(token, JSON.stringify(binding(actor))) as any
    if (!row) throw new Error('operation_not_owned')
    const operation = JSON.parse(row.operation) as Operation
    if (operation.frozenResource !== resources.fingerprint(actor, operation)) throw new Error('frozen_resource_changed_requires_new_confirmation')
    if (row.state !== 'unknown') return this.status(actor, token)
    const saved = row.result ? JSON.parse(row.result) : {}
    if (!saved.receipt) return { status: 'unknown', instruction: 'Receipt is missing. Inspect/query the remote resource manually before making a new proposal; this command never repeats a write.' }
    try {
      const verified = await resources.verify(actor, operation, saved.receipt, signal)
      this.settle(token, 'completed', { receipt: saved.receipt, verified })
      return this.status(actor, token)
    } catch { return { status: 'unknown', receipt: saved.receipt, verification: 'unconfirmed' } }
  }
  cancel(actor: TrustedActor, token: string) { return this.db.prepare("UPDATE operations SET state='cancelled' WHERE token=? AND binding=? AND state='pending'").run(token, JSON.stringify(binding(actor))).changes === 1 }
  invalidatePending() { return this.db.prepare("UPDATE operations SET state='cancelled',result=? WHERE state='pending'").run(JSON.stringify({ code: 'resource_configuration_changed_new_preview_required' })).changes }
  pending(actor: TrustedActor) { return this.db.prepare("SELECT token,operation,expires FROM operations WHERE binding=? AND state='pending' AND expires>? ORDER BY expires DESC").all(JSON.stringify(binding(actor)), Date.now()) as { token: string; operation: string; expires: number }[] }
  status(actor: TrustedActor, token: string) {
    const row = this.db.prepare('SELECT state,operation,result,expires FROM operations WHERE token=? AND binding=?').get(token, JSON.stringify(binding(actor))) as any
    if (!row) throw new Error('operation_not_owned')
    return { status: row.state, preview: JSON.parse(row.operation), result: row.result ? JSON.parse(row.result) : null, expiresAt: row.expires }
  }
  private settle(token: string, state: string, result: unknown) { this.db.prepare('UPDATE operations SET state=?,result=? WHERE token=?').run(state, JSON.stringify(result), token) }
  close() { this.db.prepare('DELETE FROM writer WHERE owner=?').run(this.owner); this.db.close() }
}
