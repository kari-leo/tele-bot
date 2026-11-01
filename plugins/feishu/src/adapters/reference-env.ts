import { readFile, stat } from 'node:fs/promises'
import path from 'node:path'

const names = new Set(['FEISHU_APP_ID', 'FEISHU_APP_SECRET', 'FEISHU_VERIFICATION_TOKEN', 'FEISHU_ENCRYPT_KEY',
  'FEISHU_ADMIN_USER_IDS', 'FEISHU_API_BASE_URL', 'FEISHU_WEBHOOK_PATH', 'FEISHU_WEBHOOK_HOST', 'FEISHU_WEBHOOK_PORT'])
/** Literal whitelist import. Never evaluate shell expressions or return secrets to a UI/report. */
export async function readReference(filename: string) {
  if (!path.isAbsolute(filename) || !(await stat(filename)).isFile() || (await stat(filename)).size > 65536) throw new Error('invalid_reference_file')
  const result: Record<string, string> = {}
  for (const line of (await readFile(filename, 'utf8')).split(/\r?\n/)) {
    const match = /^\s*(?:export\s+)?([A-Z0-9_]+)\s*=\s*(.*?)\s*$/.exec(line)
    if (!match || !names.has(match[1])) continue
    const value = match[2].replace(/^(?:"([\s\S]*)"|'([\s\S]*)')$/, '$1$2')
    if (value.includes('\0')) throw new Error('invalid_reference_value')
    result[match[1]] = value
  }
  return result
}
