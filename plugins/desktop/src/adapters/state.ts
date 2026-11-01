import { DatabaseSync } from 'node:sqlite'
import { randomUUID, createHash } from 'node:crypto'
import type { TrustedActor, DeliveryRequest } from '@telebot/core/contracts'
export class DesktopState {
  private readonly db: DatabaseSync
  private readonly writer = randomUUID()
  readonly userId: string
  constructor(filename: string) {
    this.db = new DatabaseSync(filename)
    const version = Number((this.db.prepare('PRAGMA user_version').get() as any).user_version)
    if (version > 1) { this.db.close(); throw new Error('desktop_data_version_unsupported') }
    this.db.exec(`PRAGMA journal_mode=WAL;PRAGMA synchronous=FULL;
      CREATE TABLE IF NOT EXISTS identity(id INTEGER PRIMARY KEY,value TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS bindings(id TEXT PRIMARY KEY,cwd TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS deliveries(id TEXT PRIMARY KEY,digest TEXT NOT NULL,request TEXT NOT NULL,state TEXT NOT NULL,message TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS writer(id INTEGER PRIMARY KEY,pid INTEGER,owner TEXT);BEGIN IMMEDIATE;`)
    const owner = this.db.prepare('SELECT pid FROM writer WHERE id=1').get() as any
    if (owner) { let alive = true; try { process.kill(owner.pid,0) } catch (error) { alive = (error as NodeJS.ErrnoException).code !== 'ESRCH' }
      if (alive) { this.db.exec('ROLLBACK'); this.db.close(); throw new Error('desktop_database_already_active') }
    }
    this.db.prepare('INSERT OR REPLACE INTO writer VALUES(1,?,?)').run(process.pid,this.writer)
    this.db.prepare('INSERT OR IGNORE INTO identity VALUES(1,?)').run('desktop:owner:'+randomUUID())
    this.db.exec("UPDATE deliveries SET state='unknown' WHERE state='sending';PRAGMA user_version=1;COMMIT;")
    this.userId = (this.db.prepare('SELECT value FROM identity WHERE id=1').get() as any).value
  }
  bind(id: string,cwd: string) { this.db.prepare('INSERT OR REPLACE INTO bindings VALUES(?,?)').run(id,cwd) }
  owns(actor: TrustedActor) {
    const row = this.db.prepare('SELECT cwd FROM bindings WHERE id=?').get(actor.chatId) as any
    return actor.origin === 'web' && actor.userId === this.userId && actor.privateChat && actor.sourceId === actor.chatId && row?.cwd === actor.cwd
  }
  prepare(request: DeliveryRequest) {
    const digest=createHash('sha256').update(JSON.stringify(request)).digest('hex'),message='telebot-desktop-'+createHash('sha256').update(request.id).digest('hex')
    this.db.prepare("INSERT OR IGNORE INTO deliveries VALUES(?,?,?,'pending',?)").run(request.id,digest,JSON.stringify(request),message)
    const row=this.db.prepare('SELECT * FROM deliveries WHERE id=?').get(request.id) as any
    if(row.digest!==digest)throw new Error('desktop_delivery_collision')
    return row as {id:string;digest:string;request:string;state:string;message:string}
  }
  set(id:string,state:string){this.db.prepare('UPDATE deliveries SET state=? WHERE id=?').run(state,id)}
  get(actor:TrustedActor,id:string){const row=this.db.prepare('SELECT * FROM deliveries WHERE id=?').get(id) as any;if(!row||!this.owns(actor))throw new Error('desktop_delivery_not_owned');const original=JSON.parse(row.request) as DeliveryRequest;if(original.actor.userId!==actor.userId||original.actor.chatId!==actor.chatId)throw new Error('desktop_delivery_not_owned');return row}
  close(){this.db.prepare('DELETE FROM writer WHERE owner=?').run(this.writer);this.db.close()}
}
