/** Markdown destinations only: prose paths and examples inside code are never upload instructions. */
export interface FileReference { start: number; end: number; path: string; label: string }

function unescape(value: string) { return value.replace(/\\([!"#$%&'()*+,\-./:;<=>?@[\]^_`{|}~\\])/g, '$1').replace(/&amp;/g, '&') }
export function localDestination(destination: string): string | undefined {
  let value = unescape(destination.trim())
  if (!value || value.startsWith('#') || value.startsWith('//')) return
  if (/^file:/i.test(value)) {
    try { const url = new URL(value); value = url.hostname && url.hostname !== 'localhost' ? '//' + url.hostname + url.pathname : url.pathname; if (/^\/[a-z]:\//i.test(value)) value = value.slice(1) } catch { return }
  } else if (/^[a-z][a-z\d+.-]*:/i.test(value) && !/^[a-z]:[\\/]/i.test(value)) return
  try { value = decodeURIComponent(value) } catch { /* A literal percent is a valid local filename. */ }
  return value.split('#')[0].replace(/:\d+(?::\d+)?$/, '') || undefined
}

function closing(text: string, start: number, open: string, close: string) {
  let depth = 1
  for (let i = start + 1; i < text.length; i++) {
    if (text[i] === '\\' && /[\\[\]()]/.test(text[i + 1] ?? '')) { i++; continue }
    if (text[i] === open) depth++
    if (text[i] === close && --depth === 0) return i
  }
  return -1
}

/** Parse balanced inline links, reference links and file autolinks, retaining source offsets. */
export function fileReferences(text: string): FileReference[] {
  const ignored = new Uint8Array(text.length), definitions = new Map<string, string>()
  let offset = 0, fence = '', fenceLength = 0
  for (const line of text.split(/(?<=\n)/)) {
    const marker = line.match(/^ {0,3}(`{3,}|~{3,})/)
    if (fence || marker || /^(?: {4}|\t)/.test(line)) ignored.fill(1, offset, offset + line.length)
    if (marker) {
      if (!fence) { fence = marker[1][0]; fenceLength = marker[1].length }
      else if (marker[1][0] === fence && marker[1].length >= fenceLength) fence = ''
    }
    offset += line.length
  }
  for (let i = 0; i < text.length; i++) {
    if (ignored[i] || text[i] !== '`') continue
    let count = 1; while (text[i + count] === '`') count++
    const end = text.indexOf('`'.repeat(count), i + count)
    if (end >= 0) { ignored.fill(1, i, end + count); i = end + count - 1 }
  }
  const normalize = (label: string) => unescape(label).trim().replace(/\s+/g, ' ').toLowerCase()
  const found: FileReference[] = []
  for (const match of text.matchAll(/^ {0,3}\[([^\]\n]+)\]:\s*(?:<([^>\n]+)>|([^\s]+))[^\n]*/gm)) {
    if (ignored[match.index!]) continue
    definitions.set(normalize(match[1]), match[2] ?? match[3])
    const file = localDestination(match[2] ?? match[3])
    if (file) found.push({ start: match.index!, end: match.index! + match[0].length, path: file, label: '文件引用' })
    ignored.fill(1, match.index!, match.index! + match[0].length)
  }
  for (let i = 0; i < text.length; i++) {
    if (ignored[i]) continue
    if (text[i] === '\\' && /[![<]/.test(text[i + 1] ?? '')) { i++; continue }
    if (text[i] === '<') {
      const end = text.indexOf('>', i + 1), destination = text.slice(i + 1, end)
      if (end > i && /^file:/i.test(destination)) { const file = localDestination(destination); if (file) found.push({ start: i, end: end + 1, path: file, label: '文件' }); i = end }
      continue
    }
    const start = i, bracket = text[i] === '!' && text[i + 1] === '[' ? i + 1 : i
    if (text[bracket] !== '[') continue
    const labelEnd = closing(text, bracket, '[', ']')
    if (labelEnd < 0) continue
    const label = text.slice(bracket + 1, labelEnd), next = labelEnd + 1
    let destination: string | undefined, end = labelEnd + 1
    if (text[next] === '(') {
      const targetEnd = closing(text, next, '(', ')')
      if (targetEnd < 0) continue
      const target = text.slice(next + 1, targetEnd).trim()
      destination = target.startsWith('<') ? target.slice(1, target.indexOf('>')) : target.replace(/\s+["'][\s\S]*$/, '')
      end = targetEnd + 1
    } else if (text[next] === '[') {
      const referenceEnd = closing(text, next, '[', ']')
      if (referenceEnd < 0) continue
      destination = definitions.get(normalize(text.slice(next + 1, referenceEnd) || label)); end = referenceEnd + 1
    } else destination = definitions.get(normalize(label))
    const file = destination === undefined ? undefined : localDestination(destination)
    if (file) found.push({ start, end, path: file, label: unescape(label).replace(/[\[\]`*]/g, '') || '文件' })
    // A web link can wrap a local image. Keep scanning its label for nested images.
    if (file) i = end - 1
  }
  return found.sort((a, b) => a.start - b.start)
}

export function rewriteFileReferences(text: string, replacement: (reference: FileReference) => string): string {
  const references = fileReferences(text)
  for (const reference of references.reverse()) text = text.slice(0, reference.start) + replacement(reference) + text.slice(reference.end)
  return text
}
