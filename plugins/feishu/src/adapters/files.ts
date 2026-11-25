import { DatabaseSync } from 'node:sqlite'
import { randomUUID, createHash } from 'node:crypto'
import { mkdir, writeFile, readFile, unlink, realpath, open, rename } from 'node:fs/promises'
import path from 'node:path'
import type { IncomingFile } from '@telebot/core/contracts'
import type { InboxEntry } from '../domain/types.js'

export class FileTransfers {
  private readonly db: DatabaseSync
  constructor(private readonly directory: string, private readonly download: (messageId: string, key: string, type: string) => Promise<{ bytes: Uint8Array; mediaType: string }>) {
    this.db = new DatabaseSync(path.join(directory, 'attachments.sqlite'))
    this.db.exec(`PRAGMA journal_mode=WAL; CREATE TABLE IF NOT EXISTS files(message TEXT PRIMARY KEY,chat TEXT NOT NULL,thread TEXT NOT NULL,actor TEXT NOT NULL,path TEXT NOT NULL,name TEXT NOT NULL,kind TEXT NOT NULL,mime TEXT NOT NULL,expires INTEGER NOT NULL);`)
  }
  private actor(entry: InboxEntry) { return entry.actor.authorizedUserId ?? entry.actor.ids[0] }
  async receive(entry: InboxEntry) {
    const previous = this.db.prepare('SELECT * FROM files WHERE message=?').get(entry.messageId)
    if (previous) return
    const value = JSON.parse(entry.text)
    const result = await this.download(entry.messageId, value.key, value.kind)
    const cap = (value.kind === 'image' ? 20 : 30) * 1024 * 1024
    if (result.bytes.byteLength > cap) throw new Error('attachment_size_limit')
    const name = path.basename(String(value.name)).replace(/[\u0000-\u001f]/g, '').slice(0, 200) || 'attachment'
    if (/\.(?:txt|md|csv)$/i.test(name) && new TextDecoder().decode(result.bytes).length > 120000) throw new Error('attachment_text_limit')
    await mkdir(path.join(this.directory, 'files'), { recursive: true, mode: 0o700 })
    const filename = path.join(this.directory, 'files', randomUUID())
    await writeFile(filename, result.bytes, { mode: 0o600 })
    this.db.prepare('INSERT INTO files VALUES(?,?,?,?,?,?,?,?,?)').run(entry.messageId, entry.chatId, entry.threadId, this.actor(entry), filename, name, value.kind, result.mediaType, Date.now() + 24 * 3600000)
  }
  async forMessage(entry: InboxEntry): Promise<IncomingFile[]> {
    const expired = this.db.prepare('SELECT message,path FROM files WHERE expires<=?').all(Date.now()) as { message: string; path: string }[]
    for (const row of expired) {
      const absolute = path.resolve(row.path), intended = path.resolve(this.directory, 'files')
      if (path.dirname(absolute) !== intended) throw new Error('attachment_path_mismatch')
      await unlink(absolute).catch(() => {})
      this.db.prepare('DELETE FROM files WHERE message=?').run(row.message)
    }
    const selected = (entry.actor.referenceMessageId
      ? this.db.prepare('SELECT * FROM files WHERE chat=? AND thread=? AND actor=? AND expires>? AND message=?').all(entry.chatId, entry.threadId, this.actor(entry), Date.now(), entry.actor.referenceMessageId)
      : this.db.prepare('SELECT * FROM files WHERE chat=? AND thread=? AND actor=? AND expires>? ORDER BY expires DESC LIMIT 8').all(entry.chatId, entry.threadId, this.actor(entry), Date.now())) as Record<string, any>[]
    if (entry.actor.referenceMessageId && !selected.length) throw new Error('referenced_attachment_unavailable_or_not_owned')
    const result: IncomingFile[] = []
    let total = 0
    for (const row of selected) {
      const data = await readFile(row.path); total += data.byteLength
      if (total > 30 * 1024 * 1024) throw new Error('attachment_batch_limit')
      result.push({ name: row.name, kind: row.kind, mediaType: row.mime, data })
    }
    return result
  }
  close() { this.db.close() }
  async outbound(entry: InboxEntry, filename: string, options?: { reference: true }) {
    if (!options?.reference && !path.isAbsolute(filename)) throw new Error('absolute_artifact_path_required')
    // UNC/device paths can initiate network access; file references never grant that authority.
    if (/^(?:\\\\|\/\/)/.test(filename)) throw new Error('artifact_outside_allowed_scope')
    const cwd = await realpath(entry.spec.cwd), target = path.resolve(cwd, filename), file = await realpath(target), relative = path.relative(cwd, file)
    const sensitive = /(?:^|[\\/])(?:\.env(?:\..*)?|\.git|credentials(?:\..*)?|\.credentials(?:\..*)?|\.aws|\.ssh|\.codex|\.dsh)(?:[\\/]|$)/i
    if (relative === '..' || relative.startsWith('..' + path.sep) || path.isAbsolute(relative) || sensitive.test(relative) || sensitive.test(path.relative(cwd,target))) throw new Error('artifact_outside_allowed_scope')
    const handle = await open(file, 'r')
    let bytes: Buffer
    try {
      const info = await handle.stat()
      if (!info.isFile() || info.size > 30 * 1024 * 1024) throw new Error('artifact_size_limit')
      bytes = await handle.readFile()
      if (bytes.length > 30 * 1024 * 1024 || await realpath(target) !== file) throw new Error('artifact_changed_after_execution')
    } finally { await handle.close() }
    const sha256 = createHash('sha256').update(bytes).digest('hex'), name = path.basename(file)
    const image = bytes.subarray(0,8).equals(Buffer.from([137,80,78,71,13,10,26,10])) || bytes[0]===255&&bytes[1]===216&&bytes[2]===255 || /^GIF8[79]a/.test(bytes.toString('ascii',0,6)) || bytes.toString('ascii',0,4)==='RIFF'&&bytes.toString('ascii',8,12)==='WEBP' || bytes.toString('ascii',0,2)==='BM'
    const kind = image && bytes.length > 0 && bytes.length <= 10 * 1024 * 1024 ? 'image' : 'file'
    if (!options?.reference) return JSON.stringify({ path:file,sha256 })
    // Content-addressed spool freezes the reply even if its source is removed before upload/restart.
    const directory = path.join(this.directory,'outgoing'); await mkdir(directory,{recursive:true,mode:0o700})
    const snapshot = path.join(directory,sha256), staging = path.join(directory,randomUUID()+'.tmp')
    // Publish complete bytes atomically: concurrent replies must never observe a partial spool file.
    try {
      await writeFile(staging,bytes,{flag:'wx',mode:0o600})
      try { await rename(staging,snapshot) }
      catch(error) { if(createHash('sha256').update(await readFile(snapshot)).digest('hex')!==sha256)throw error }
    } finally { await unlink(staging).catch(error=>{if((error as NodeJS.ErrnoException).code!=='ENOENT')throw error}) }
    return JSON.stringify({ path:snapshot,sourcePath:file,name,size:bytes.length,sha256,kind })
  }
}
