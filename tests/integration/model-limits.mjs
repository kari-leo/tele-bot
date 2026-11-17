import assert from 'node:assert/strict'
import { createRequire } from 'node:module'
import { pathToFileURL } from 'node:url'
import { createServer } from 'node:http'
import { once } from 'node:events'
import { writeFile } from 'node:fs/promises'
import path from 'node:path'
import { officialHost } from '../../tooling/official-host.mjs'
import { root } from '../../tooling/common.mjs'

// Exercise the official Messages adapter, not a plugin-owned model watchdog.
const host=await officialHost(),require=createRequire(path.join(host.installRoot,'package.json'))
const {DeepSeekAdapter,resolveAdapterOptions}=await import(pathToFileURL(require.resolve('@deepseek-ai/dsh-llm-deepseek')).href)
const {Config}=await import(pathToFileURL(require.resolve('@deepseek-ai/dsh-llm-deepseek-account')).href)
Config({streamIdleTimeoutMs:60000,reasoningEffort:'low',retryPolicy:{mode:'normal',maxRetries:2},modelOverrides:{'deepseek-flash':{maxTokens:16384}}})
let received
const server=createServer(async(request,response)=>{
  let raw='';for await(const chunk of request)raw+=chunk
  received=JSON.parse(raw)
  response.writeHead(200,{'Content-Type':'text/event-stream'})
  response.write('event: message_start\ndata: '+JSON.stringify({type:'message_start',message:{id:'isolated',type:'message',role:'assistant',model:'fixture',content:[],usage:{input_tokens:1,output_tokens:0}}})+'\n\n')
  // Leave the next provider read silent; the official idle deadline must end it.
})
server.listen(0,'127.0.0.1');await once(server,'listening')
try{
  const options=resolveAdapterOptions({baseURL:`http://127.0.0.1:${server.address().port}/anthropic`,streamIdleTimeoutMs:100,reasoningEffort:'low',maxTokens:16384,models:[{id:'fixture',contextWindow:131072,maxTokens:16384}],retryPolicy:{mode:'normal',maxRetries:2}})
  const adapter=new DeepSeekAdapter({options:()=>options,resolveAuth:async()=>({headers:{}}),resolveUserId:()=> 'isolated-model-probe',prepareExtensions:async()=>({fields:{},accept:async()=>{}})})
  const started=Date.now()
  await assert.rejects(async()=>{for await(const chunk of adapter.stream({provider:'fixture',model:'fixture',messages:[{role:'user',content:[{type:'text',text:'synthetic idle probe'}]}]})){}},error=>error.code==='TIMEOUT')
  assert(received,'Actual loopback Messages request received')
  assert.equal(received.max_tokens,16384);assert.equal(received.output_config.effort,'low')
  assert(Date.now()-started<5000)
  const evidence={runtime:host.version,paidModels:false,loopbackOnly:true,checks:['official account config accepts idle deadline, low effort, model output limit and bounded retries','official Messages stream times out after a silent read and sends low effort/output cap'],productionPolicy:{streamIdleTimeoutMs:60000,reasoningEffort:'low',maxTokens:16384,maxRetries:2}}
  await writeFile(path.join(root,'.runtime/verify-model-limits-latest.json'),JSON.stringify(evidence,null,2)+'\n')
  for(const check of evidence.checks)console.log('PASS '+check)
}finally{server.closeAllConnections();await new Promise(done=>server.close(done))}
