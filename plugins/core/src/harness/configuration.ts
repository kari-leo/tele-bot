import type { Context } from '@deepseek-ai/cordis'
import { resolveDshHome } from '@deepseek-ai/dsh-home-paths'
import { mkdir, open, readFile, rename, unlink, stat } from 'node:fs/promises'
import { randomUUID } from 'node:crypto'
import path from 'node:path'
import { registerConfigurationPage, type SettingField } from './configuration-ui.js'

declare module '@deepseek-ai/cordis' { interface Events { 'telebot/entry-policy-changed'(): void } }
export type { SettingField } from './configuration-ui.js'
export { registerConfigurationPage } from './configuration-ui.js'
export interface PluginConfiguration<T extends Record<string, any>> {
  name: string; title: string; bootstrap: T; fields: SettingField[]
  missing(value: T): Promise<string[]>; validate(value: T): Promise<void>
  activate(value: T): Promise<() => Promise<void>>; busy(): boolean
  diagnostics?(): Record<string, string | boolean | number>
}
const json = (value: unknown) => JSON.stringify(value, null, 2) + '\n'

/** Versioned configuration and lifecycle for the official native settings page. */
export async function configuredPlugin<T extends Record<string, any>>(ctx: Context, options: PluginConfiguration<T>): Promise<{ current(): T; missing(): Promise<string[]>; directory: string }> {
  const directory = options.bootstrap.configDirectory || path.join(resolveDshHome(), 'plugins', options.name, 'config')
  if (!path.isAbsolute(directory)) throw new Error('absolute_configuration_directory_required')
  await mkdir(directory, { recursive: true, mode: 0o700 })
  const lock = path.join(directory, 'writer.lock'), lockOwner = randomUUID()
  try { const handle=await open(lock,'wx',0o600);await handle.writeFile(json({pid:process.pid,owner:lockOwner}));await handle.close() }
  catch(error) {
    if ((error as NodeJS.ErrnoException).code!=='EEXIST') throw error
    const previous=JSON.parse(await readFile(lock,'utf8'));let alive=true
    try { process.kill(previous.pid,0) } catch(error) { if((error as NodeJS.ErrnoException).code==='ESRCH')alive=false }
    if(alive)throw new Error('configuration_directory_already_active')
    await unlink(lock);return configuredPlugin(ctx,options)
  }
  const atomic=async(name:string,value:string)=>{const file=path.join(directory,name),temporary=file+'.'+randomUUID()+'.tmp';const handle=await open(temporary,'wx',0o600);try{await handle.writeFile(value);await handle.sync()}finally{await handle.close()}try{await rename(temporary,file)}finally{await unlink(temporary).catch(()=>{})}}
  let current={...options.bootstrap},revision='',dispose:(()=>Promise<void>)|undefined,loadError='',stopping=false
  const known=new Set(Object.keys(options.bootstrap))
  try { const file=path.join(directory,'config.json');if((await stat(file)).size>262144)throw new Error('configuration_too_large');const value=JSON.parse(await readFile(file,'utf8'));if(value.version!==1||typeof value.revision!=='string'||Object.keys(value.config).some(key=>!known.has(key)))throw new Error('invalid_configuration');const candidate={...current,...value.config};await options.validate(candidate);current=candidate;revision=value.revision }
  catch(error) { if((error as NodeJS.ErrnoException).code!=='ENOENT')loadError='配置文件无效，请修正后保存。' }
  try { await options.validate(current) } catch { loadError ||= '配置参数无效，请修正后保存。' }
  for(const obsolete of ['setup.token','endpoint.json'])await unlink(path.join(directory,obsolete)).catch(error=>{if(error.code!=='ENOENT')throw error})
  const saveFile=async(value:T)=>{const next=randomUUID();await atomic('config.json',json({version:1,revision:next,config:value}));current=value;revision=next;loadError=''}
  if(!revision&&!loadError)await saveFile(current)
  try { const file=await open(path.join(directory,'config.example.json'),'wx',0o600);await file.writeFile(json({version:1,revision:'example',config:options.bootstrap}));await file.close() } catch(error){if((error as NodeJS.ErrnoException).code!=='EEXIST')throw error}
  const missing=async()=>loadError?[loadError]:options.missing(current)
  if(!(await missing()).length)try{dispose=await options.activate(current)}catch{loadError='配置未能启用，请检查路径及依赖。'}
  let queue:Promise<any>=Promise.resolve()
  registerConfigurationPage(ctx,{
    name:options.name,title:options.title,fields:options.fields,
    describe:async()=>({directory,config:current,revision,missing:await missing(),active:Boolean(dispose),busy:options.busy(),diagnostics:options.diagnostics?.()??{}}),
    save:body=>{
        const job=queue.then(async()=>{
          if(stopping)throw new Error('configuration_stopping')
          if(body.revision!==revision)throw new Error('configuration_revision_conflict')
          if(options.busy())throw new Error('configuration_busy')
          if(!body.config||typeof body.config!=='object'||Array.isArray(body.config)||Object.keys(body.config).some(key=>!known.has(key))||body.config.configDirectory!==current.configDirectory)throw new Error('invalid_configuration')
          const candidate={...current,...body.config};await options.validate(candidate)
          const previous=current;await dispose?.();dispose=undefined
          try{if(!(await options.missing(candidate)).length)dispose=await options.activate(candidate);await saveFile(candidate)}
          catch(error){await dispose?.();dispose=undefined;if(!(await options.missing(previous)).length)dispose=await options.activate(previous);throw error}
          return {saved:true,active:Boolean(dispose),revision}
        });queue=job.catch(()=>{});return job
    },
  })
  if((await missing()).length)ctx.logger.warn('%s needs configuration: Settings → Telebot 插件',options.name)
  ctx.on('telebot/entry-policy-changed',()=>{const job=queue.then(async()=>{if(!stopping&&!dispose&&!(await missing()).length){try{dispose=await options.activate(current);loadError=''}catch{loadError='配置未能启用，请检查路径及依赖。'}}});queue=job.catch(()=>{})})
  ctx.effect(()=>async()=>{stopping=true;await queue;await dispose?.();try{const saved=JSON.parse(await readFile(lock,'utf8'));if(saved.owner===lockOwner)await unlink(lock)}catch{}})
  return {current:()=>current,missing,directory}
}
