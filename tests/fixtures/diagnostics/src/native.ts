/** Isolated acceptance fixture only. Never installed by production instructions. */
import type {Context} from '@deepseek-ai/cordis'
import {writeFile,mkdir} from 'node:fs/promises'
import path from 'node:path'
import {randomUUID} from 'node:crypto'
export async function installNativeProbes(ctx:Context,directory:string){
 const host=ctx as any,token=randomUUID(),origin=host.webServer.getOrigin(),actors=new Map<string,any>()
 await mkdir(directory,{recursive:true})
 await writeFile(path.join(directory,'native.json'),JSON.stringify({origin,url:host.connection.authenticatedUrl(origin+'/'),token}))
 ctx.on('tools/post-execute',async(exec,result,next)=>{
  if(exec.agent)try{actors.set(exec.agent.session.id,ctx.telebotCore.actor(exec.agent.session.id))}catch{}
  return next()
 })
 const handler=host.connection.createSharedFetchHandler('/api')
 const foreign=host.connection.createPeer({principal:{kind:'telebot-test-foreign',id:'foreign'},interactions:'none'})
 host.webServer.register({kind:'exact',path:'/telebot-fixture-native',async handler(req:any,res:any){
  if(req.headers['x-fixture-token']!==token){res.writeHead(403).end();return}
  try{
   let raw='';for await(const part of req)raw+=part;const input=JSON.parse(raw);let result:any
   if(input.action==='inspect'){
    const observed=await host.sessionController.inspect(input.sessionId)
    result={events:observed.events??observed.session?.snapshotEvents?.(),header:observed.meta,actor:actors.get(input.sessionId),status:host.agents.get(input.sessionId)?.status,capabilities:{features:ctx.telebotCore.hasActorProvider('web')}}
   }else if(input.action==='delivery'){
    const actor=actors.get(input.sessionId);if(!actor)throw new Error('fixture_actor_missing')
    result=await ctx.telebotCore.deliver({id:input.id,actor,result:input.result})
   }else if(input.action==='foreign'){
    const endpoint=input.endpoint,response=await handler.fetch(new Request(origin+'/api/'+endpoint,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({type:'client-request',rpcId:randomUUID(),method:endpoint,payload:{args:{request:input.payload}}})}),foreign)
    result={status:response.status,body:await response.text()}
   }else throw new Error('fixture_action_invalid')
   res.writeHead(200,{'Content-Type':'application/json'}).end(JSON.stringify(result))
  }catch(error){res.writeHead(500).end(String(error))}
 }})
}
