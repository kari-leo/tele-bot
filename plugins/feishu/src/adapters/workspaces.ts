import path from 'node:path'
import { realpath, stat } from 'node:fs/promises'
import { randomUUID } from 'node:crypto'
import type { WorkspacePort } from '../domain/types.js'

/** Canonicalize both roots and targets so junctions cannot widen directory permission. */
export class Workspaces implements WorkspacePort {
  constructor(private readonly roots: readonly string[]) {}
  sessionId(): string { return randomUUID() }
  async resolve(target: string): Promise<string> {
    if (!path.isAbsolute(target)) throw new Error('absolute_workspace_required')
    const actual = await realpath(target)
    if (!(await stat(actual)).isDirectory()) throw new Error('workspace_not_directory')
    const roots = await Promise.all(this.roots.map(root => realpath(root)))
    if (!roots.some(root => {
      const relative = path.relative(root, actual)
      return relative === '' || (relative !== '..' && !relative.startsWith(`..${path.sep}`) && !path.isAbsolute(relative))
    })) throw new Error('workspace_outside_allowed_roots')
    return actual
  }
}
