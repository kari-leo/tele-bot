/** Single verification entry: scoped modules by default, full composition only on explicit request. */
import {parseArgs} from 'node:util'
import path from 'node:path'
import {root,run} from './common.mjs'
import {modules,gates,selectModules} from './verification-catalog.mjs'
const {values}=parseArgs({options:{module:{type:'string',multiple:true},gate:{type:'string',multiple:true},local:{type:'boolean'},installed:{type:'boolean'},release:{type:'boolean'},list:{type:'boolean'},live:{type:'string'},reference:{type:'string'},workspace:{type:'string'},base:{type:'string'},table:{type:'string'},chat:{type:'string'},callback:{type:'string'},write:{type:'boolean'},send:{type:'boolean'}}})
if(values.list){for(const [name,entry]of Object.entries(modules))console.log(`${name}: ${entry.description}; installed=${entry.gates.join(',')||'按宿主接线变更显式选 gate'}`);console.log('Extra gates: '+Object.keys(gates).join(', '));process.exit(0)}
if(values.live){
  if(values.module||values.gate||values.release||values.local||values.installed)throw Error('Live checks cannot be mixed with mock verification')
  const {verifyLive}=await import('../tests/integration/feishu-live.mjs');await verifyLive(values)
}else{
  if(values.send||values.write||values.reference||values.base||values.chat||values.callback||values.table||values.workspace)throw Error('Real-service options require --live preflight|bitable|chat')
  if(values.local&&values.installed)throw Error('Choose --local or --installed; omit both to run both layers')
  if(!values.module&&!values.release&&!values.gate)throw Error('Use --module <name> (repeatable), --gate <name>, --release or --list')
  const selected=selectModules(values.release?undefined:values.module??[])
  const checks=[...new Set([...(values.release?['harness','p0','composition','composition-web','resources-acp','resources-web','progress-acp','progress-web','questions-acp','questions-web','settings','settings-desktop','desktop','knowledge']:selected.flatMap(name=>modules[name].gates)),...(values.gate??[])])]
  for(const name of checks)if(!Object.hasOwn(gates,name))throw Error('Unknown gate: '+name)
  const exec=(file,args=[])=>run(process.execPath,[path.join(root,file),...args],{stdio:'inherit',timeout:1200000})
  const withInstalled=!values.local&&checks.length>0
  if(withInstalled)await exec('tooling/build.mjs')
  if(selected.length&&!values.installed)await exec('tooling/test.mjs',[...selected.flatMap(name=>['--module',name]),...(withInstalled?['--no-build']:[])])
  if(!values.local&&checks.length){
    // Packing checks every archive once; integration processes never rebuild or repack.
    await exec('tooling/pack.mjs',['--fixtures'])
    for(const name of checks){const gate=gates[name];await exec('tests/integration/'+gate.file+'.mjs',gate.args??[])}
  }
}
