import fs from 'node:fs/promises'
import path from 'node:path'
import { tmpdir } from 'node:os'

/** Dispose only this successful, isolated verifier's direct temporary directory. */
export async function cleanupVerification(directory, passed) {
  if (!passed || process.env.TELEBOT_KEEP_VERIFICATION === '1') return
  const parent=await fs.realpath(tmpdir()),target=path.resolve(directory)
  if(path.dirname(target).toLowerCase()!==parent.toLowerCase()||!/^telebot-(?:acp|feishu|setup|codex-p0|knowledge|desktop)-[A-Za-z0-9]+$/.test(path.basename(target)))throw new Error('verification_cleanup_outside_temp_scope')
  const actual=await fs.realpath(target)
  if(actual.toLowerCase()!==target.toLowerCase()||(await fs.lstat(target)).isSymbolicLink())throw new Error('verification_cleanup_reparse_refused')
  await fs.rm(target,{recursive:true,force:true,maxRetries:2,retryDelay:100})
}
