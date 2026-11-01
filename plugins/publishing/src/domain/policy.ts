export interface Publication { source: string; slug: string; title: string; description: string; published: string }
export function publicationContent(input: Publication, source: Uint8Array): Uint8Array {
  if (!/^[a-z0-9][a-z0-9-]{0,79}$/.test(input.slug)) throw new Error('invalid_publication_slug')
  if (!input.title?.trim() || !input.description?.trim() || !/^\d{4}-\d{2}-\d{2}$/.test(input.published) || new Date(input.published).toISOString().slice(0,10)!==input.published) throw new Error('publication_metadata_required')
  const text = new TextDecoder('utf-8',{fatal:true}).decode(source).replace(/^\uFEFF/,'')
  const body = text.startsWith('---\n') || text.startsWith('---\r\n') ? text.replace(/^---\r?\n[\s\S]*?\r?\n---(?:\r?\n|$)/,'') : text
  if (!body.trim()) throw new Error('publication_body_required')
  // Only metadata changes. Body bytes, including line endings and trailing space, stay intact.
  return new TextEncoder().encode(`---\ntitle: ${JSON.stringify(input.title)}\ndescription: ${JSON.stringify(input.description)}\npublished: ${JSON.stringify(input.published)}\n---\n`+body)
}
export function safeRelative(value: string) {
  if (typeof value !== 'string' || !value || value.length>1000 || /^[\\/]|^[a-z]:/i.test(value) || value.split(/[\\/]/).some(part => !part || part==='.' || part==='..' || /^(?:\.git|\.codex|\.env(?:\..*)?|credentials|\.aws)$/i.test(part)) || /[\x00-\x1f]/.test(value)) throw new Error('safe_relative_file_required')
  return value
}
