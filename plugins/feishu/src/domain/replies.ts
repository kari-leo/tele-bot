import type { ConversationResult } from '@telebot/core/contracts'
import type { ChannelPorts, InboxEntry, Outbound } from './types.js'
import { fileReferences, rewriteFileReferences } from './file-references.js'

export type Output = Pick<Outbound, 'msgType' | 'content'>
export function posts(text: string): Output[] {
  const points = Array.from(text), outputs: Output[] = []
  for (let offset = 0; offset < points.length; offset += 6000) outputs.push({ msgType: 'post', content: JSON.stringify({ zh_cn: { content: [[{ tag: 'md', text: points.slice(offset, offset + 6000).join('') }]] } }) })
  return outputs
}

/** One composition boundary for chat, recovery, commands, notifications and delayed deliveries. */
export async function composeReply(entry: InboxEntry, result: Pick<ConversationResult, 'text' | 'artifacts' | 'presentedFiles'>, files?: ChannelPorts['files'], cardStatus?: string): Promise<{ text: string; outputs: Output[] }> {
  const references = fileReferences(result.text), prepared = new Map<string, string>(), media: Output[] = [], seen = new Set<string>(), frozen = new Map<string,string>()
  const artifacts = new Map(result.artifacts?.map(artifact => [artifact.path, artifact]))
  let bytes = 0, images = 0
  const prepare = async (filename: string): Promise<string> => {
    const existing = prepared.get(filename); if (existing) return existing
    let label: string
    try {
      if (!files) throw Error('file_transfers_unavailable')
      const content = await files.outbound(entry, filename, { reference: true }), file = JSON.parse(content)
      const artifact = artifacts.get(filename)
      const identity = file.sourcePath ?? file.path
      if (artifact) frozen.set(identity,artifact.sha256)
      if (frozen.has(identity) && file.sha256 !== frozen.get(identity)) throw Error('artifact_changed_after_execution')
      label = '附件：' + (file.name ?? filename.split(/[\\/]/).at(-1))
      if (!seen.has(identity)) {
        const size = file.size ?? artifact?.size ?? 0
        if (bytes + size > 30 * 1024 * 1024) throw Error('attachment_batch_limit')
        const image = file.kind === 'image' && images < 8
        media.push({ msgType: image ? 'image' : 'file', content }); seen.add(identity); bytes += size; if (image) images++
      }
    } catch (error) {
      const code = error instanceof Error ? (error as Error & { code?: string }).code ?? error.message : ''
      label = code === 'ENOENT' ? '文件不存在，未附上' : /size|limit/.test(code) ? '文件超过投递限制，未上传' : /changed/.test(code) ? '文件内容已变化，未上传' : '文件未通过目录或权限校验，未上传'
    }
    prepared.set(filename, label); return label
  }
  // Explicit artifacts carry a frozen hash, so validate them before links to the same file.
  for (const artifact of result.artifacts ?? []) await prepare(artifact.path)
  for (const reference of references) await prepare(reference.path)
  for (const file of result.presentedFiles ?? []) await prepare(file.path)
  let text = rewriteFileReferences(result.text, reference => `${reference.label}（${prepared.get(reference.path)}）`)
  for (const file of [...(result.artifacts ?? []), ...(result.presentedFiles ?? [])]) {
    if (!references.some(reference => reference.path === file.path)) text += '\n\n' + prepared.get(file.path)
  }
  const body = posts(text)
  const outputs = cardStatus ? body.map(post => ({ msgType: 'interactive' as const, content: JSON.stringify({ header: { title: { tag: 'plain_text', content: '操作确认结果' }, template: cardStatus === 'completed' ? 'green' : 'orange' }, elements: [{ tag: 'markdown', content: JSON.parse(post.content).zh_cn.content[0][0].text }] }) })) : body
  return { text, outputs: [...outputs, ...media] }
}

/** Progress is provisional; never upload partial/intermediate deliverables. */
export function progressText(text: string) { return rewriteFileReferences(text, reference => `${reference.label}（文件引用将在最终回复中处理）`) }
