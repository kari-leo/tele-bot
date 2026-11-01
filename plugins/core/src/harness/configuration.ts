import type { Context } from '@deepseek-ai/cordis'
import type {} from '@deepseek-ai/dsh-host-webserver'
import { resolveDshHome } from '@deepseek-ai/dsh-home-paths'
import { mkdir, open, readFile, rename, unlink, stat } from 'node:fs/promises'
import { randomBytes, randomUUID, timingSafeEqual } from 'node:crypto'
import path from 'node:path'

declare module '@deepseek-ai/cordis' { interface Events { 'telebot/entry-policy-changed'(): void } }

export interface SettingField { key: string; label: string; type?: 'text' | 'list' | 'number' | 'boolean' | 'json'; help?: string; min?: number; max?: number }
export interface PluginConfiguration<T extends Record<string, any>> {
  name: string; title: string; bootstrap: T; fields: SettingField[]
  missing(value: T): Promise<string[]>; validate(value: T): Promise<void>
  activate(value: T): Promise<() => Promise<void>>; busy(): boolean
  diagnostics?(): Record<string, string | boolean | number>
}
const json = (value: unknown) => JSON.stringify(value, null, 2) + '\n'
const escape = (value: string) => value.replace(/[&<>"']/g, letter => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[letter]!))
function page(title: string, fields: SettingField[], nonce: string) {
  const rows = fields.map(field => `<label>${escape(field.label)}${field.type === 'list' || field.type === 'json' ? `<textarea data-key="${escape(field.key)}" rows="4"></textarea>` : `<input data-key="${escape(field.key)}" type="${field.type === 'boolean' ? 'checkbox' : field.type === 'number' ? 'number' : 'text'}"${field.min === undefined ? '' : ` min="${field.min}"`}${field.max === undefined ? '' : ` max="${field.max}"`}>`}<small>${escape(field.help ?? '')}</small></label>`).join('')
  return `<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>${escape(title)}</title><style nonce="${nonce}">body{font:15px 'Segoe UI','Microsoft YaHei',sans-serif;color:#17304f;background:#f3f5fa;margin:0}main{max-width:880px;margin:32px auto;padding:24px;background:white;border-radius:16px}label{display:flex;flex-direction:column;gap:8px;margin:18px 0}input,textarea,button{font:inherit;padding:10px;border:1px solid #cbd5e1;border-radius:7px}input[type=checkbox]{align-self:start}small{color:#596b80}button{background:#315ee7;color:white;cursor:pointer}#notice{white-space:pre-wrap;padding:14px;background:#eef3ff}#location{overflow-wrap:anywhere}form[hidden]{display:none}</style><main><h1>${escape(title)}</h1><p>${fields.length ? '配置在本机独立目录持久保存。首次使用或缺项时补齐后保存；卸载保留数据。' : '核心服务无需填写独立业务参数；模型和通用权限通过官方宿主界面管理。'}</p><div id="access"><label>本机配置访问令牌<input id="token" type="password"></label><button id="connect">连接</button></div><p id="notice">正在连接…</p><div id="diagnostics"></div><form id="form" hidden>${rows}<button>保存并应用</button></form><p id="location"></p></main><script nonce="${nonce}">
const fields=${JSON.stringify(fields).replace(/</g,'\\u003c')},$=id=>document.getElementById(id);let snapshot,token=decodeURIComponent(location.hash.slice(1));$('token').value=token;
async function api(method,body){const r=await fetch(location.pathname+'/api',{method,headers:{Authorization:'Bearer '+token,...(body?{'Content-Type':'application/json'}:{})},...(body?{body:JSON.stringify(body)}:{})});const data=await r.json();if(!r.ok)throw new Error(data.code||'保存失败');return data}
async function load(){try{token=$('token').value.trim();snapshot=await api('GET');for(const field of fields){const el=document.querySelector('[data-key="'+field.key+'"]'),v=snapshot.config[field.key];if(field.type==='boolean')el.checked=v;else el.value=field.type==='list'?v.join('\\n'):field.type==='json'?JSON.stringify(v,null,2):v??''}$('form').hidden=!fields.length;$('diagnostics').replaceChildren();for(const [name,value]of Object.entries(snapshot.diagnostics||{})){const row=document.createElement('p');row.textContent=name+'：'+(value===true?'可用':value===false?'未提供':value);$('diagnostics').append(row)};$('access').hidden=true;$('notice').textContent=snapshot.missing.length?'请补充：'+snapshot.missing.join('、'):'配置已生效。';$('location').textContent=snapshot.directory+'/config.json'}catch(e){$('notice').textContent=e.message}}
$('connect').onclick=load;$('form').onsubmit=async event=>{event.preventDefault();try{const config=structuredClone(snapshot.config);for(const field of fields){const el=document.querySelector('[data-key="'+field.key+'"]');config[field.key]=field.type==='boolean'?el.checked:field.type==='number'?Number(el.value):field.type==='list'?el.value.split(/[,;\\n]+/).map(v=>v.trim()).filter(Boolean):field.type==='json'?JSON.parse(el.value):el.value.trim()}await api('POST',{revision:snapshot.revision,config});await load();$('notice').textContent=snapshot.missing.length?'已保存，请补齐：'+snapshot.missing.join('、'):'已保存并应用。'}catch(e){$('notice').textContent=e.message}};if(token)load();
</script></html>`
}
/** Reusable local setup over the official webServer, not another HTTP host or loader. */
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
  const token=randomBytes(32).toString('base64url'),route='/telebot/'+options.name.replace(/^telebot-/,'')+'/setup'
  await atomic('setup.token',token+'\n')
  const saveFile=async(value:T)=>{const next=randomUUID();await atomic('config.json',json({version:1,revision:next,config:value}));current=value;revision=next;loadError=''}
  if(!revision&&!loadError)await saveFile(current)
  try { const file=await open(path.join(directory,'config.example.json'),'wx',0o600);await file.writeFile(json({version:1,revision:'example',config:options.bootstrap}));await file.close() } catch(error){if((error as NodeJS.ErrnoException).code!=='EEXIST')throw error}
  const missing=async()=>loadError?[loadError]:options.missing(current)
  if(!(await missing()).length)try{dispose=await options.activate(current)}catch{loadError='配置未能启用，请检查路径及依赖。'}
  let queue:Promise<any>=Promise.resolve()
  ctx.inject(['webServer'],async scope=>{
    const origin=scope.webServer.getOrigin(),authority=new URL(origin).host
    await atomic('endpoint.json',json({origin,route}))
    const remove=scope.webServer.register({kind:'prefix',path:route,handler:async(req,res)=>{
      res.setHeader('Cache-Control','no-store');res.setHeader('X-Content-Type-Options','nosniff');res.setHeader('Referrer-Policy','no-referrer')
      const send=(status:number,value:unknown)=>{res.writeHead(status,{'Content-Type':'application/json; charset=utf-8'}).end(json(value))}
      if(!['127.0.0.1','::1','::ffff:127.0.0.1'].includes(req.socket.remoteAddress??'')||req.headers.host!==authority||['forwarded','x-forwarded-for','x-forwarded-host','x-real-ip','cf-connecting-ip'].some(key=>req.headers[key])||(req.headers.origin&&req.headers.origin!==origin))return send(403,{code:'local_setup_only'})
      const pathname=new URL(req.url??'/',origin).pathname
      if(pathname===route&&req.method==='GET'){const nonce=randomBytes(18).toString('base64');res.setHeader('Content-Security-Policy',`default-src 'none'; script-src 'nonce-${nonce}'; style-src 'nonce-${nonce}'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'`);res.writeHead(200,{'Content-Type':'text/html; charset=utf-8'}).end(page(options.title,options.fields,nonce));return}
      if(pathname!==route+'/api')return send(404,{code:'not_found'})
      const supplied=Buffer.from((req.headers.authorization??'').replace(/^Bearer /,'')),expected=Buffer.from(token)
      if(supplied.length!==expected.length||!timingSafeEqual(supplied,expected))return send(403,{code:'setup_access_denied'})
      if(req.method==='GET')return send(200,{directory,config:current,revision,missing:await missing(),active:Boolean(dispose),busy:options.busy(),diagnostics:options.diagnostics?.()??{}})
      if(req.method!=='POST'||req.headers.origin!==origin||req.headers['content-type']?.split(';')[0]!=='application/json')return send(403,{code:'same_origin_json_required'})
      try {
        const chunks:Buffer[]=[];let size=0;for await(const part of req){size+=part.length;if(size>262144)return send(413,{code:'configuration_too_large'});chunks.push(part)}
        const body=JSON.parse(Buffer.concat(chunks).toString('utf8'))
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
        });queue=job.catch(()=>{});send(200,await job)
      }catch(error){const code=error instanceof Error&&/^[a-z_]{1,100}$/.test(error.message)?error.message:'configuration_rejected';send(code==='configuration_busy'||code==='configuration_revision_conflict'?409:400,{code})}
    }})
    scope.logger.info('%s configuration UI (local only): %s%s#%s',options.name,origin,route,token)
    scope.effect(()=>remove)
  })
  ctx.on('telebot/entry-policy-changed',()=>{const job=queue.then(async()=>{if(!stopping&&!dispose&&!(await missing()).length){try{dispose=await options.activate(current);loadError=''}catch{loadError='配置未能启用，请检查路径及依赖。'}}});queue=job.catch(()=>{})})
  ctx.effect(()=>async()=>{stopping=true;await queue;await dispose?.();try{const saved=JSON.parse(await readFile(lock,'utf8'));if(saved.owner===lockOwner)await unlink(lock)}catch{}})
  return {current:()=>current,missing,directory}
}
