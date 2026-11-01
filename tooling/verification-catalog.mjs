/** One catalog for local and installed checks. Shared files use name filters, not duplicate runs. */
const file=name=>'tests/acceptance/'+name+'.test.mjs'
const entry=(name,pattern)=>({file:file(name),...(pattern?{pattern}:{})})
export const modules={
  core:{description:'共享宿主适配、会话流和权限边界',packages:['core'],tests:['artifacts','verification-isolation','conversations'].map(x=>entry(x)),gates:['harness']},
  settings:{description:'原生配置页、凭据、持久化和 Base 链接',packages:['core','feishu'],tests:['feishu-configuration','configuration-and-bases','native-configuration-ui'].map(x=>entry(x)),gates:['settings','settings-desktop']},
  feishu:{description:'webhook、会话、思考进度、队列、收发和附件',packages:['core','feishu','adviser'],tests:[entry('feishu'),entry('commands'),entry('production-business','^P2 ')],gates:['feishu-acp','feishu-web','progress-acp','progress-web','questions-acp','questions-web']},
  'feishu-business':{description:'应用资源、表名发现、CRUD 和冻结确认',packages:['core','feishu','codex','adviser'],tests:[entry('feishu-resources'),entry('local-business','^P3 '),entry('production-business','^P3 ')],gates:['resources-acp','resources-web']},
  codex:{description:'本地 Codex 会话、权限、历史、Skill 和产物',packages:['core','feishu','codex','adviser'],tests:[entry('local-business','^P4 (?:inspect|apply|cancelled)'),entry('skills-and-limits')],gates:[]},
  adviser:{description:'OpenCLI 适配、脱敏、缓存、超时和取消',packages:['core','feishu','codex','adviser'],tests:[entry('local-business','^P4 adviser'),entry('production-business','^P4 OpenCLI')],gates:[]},
  publishing:{description:'报告、原文发布和 Git 交付规则',packages:['core','publishing'],tests:[entry('publishing')],gates:[]},
  automation:{description:'调度、授权、账本、恢复和投递',packages:['core','automation'],tests:[entry('automation'),entry('schedule-prototype')],gates:['questions-acp','questions-web']},
  desktop:{description:'官方原生入口、身份和完整权限隔离',packages:['core','desktop','feishu','codex','publishing','automation'],tests:[entry('desktop')],gates:['desktop']},
  knowledge:{description:'上游 KB 安装和关键词检索',packages:[],tests:[],gates:['knowledge']},
}
export const aliases={business:['feishu-business','codex','adviser','publishing','automation']}
export function selectModules(names=Object.keys(modules)){
  const selected=[...new Set(names.flatMap(name=>aliases[name]??[name]))]
  for(const name of selected)if(!Object.hasOwn(modules,name))throw Error('Unknown module: '+name)
  return selected
}
export function selectTests(names){
  const selected=new Map()
  for(const name of selectModules(names))for(const spec of modules[name].tests){
    const previous=selected.get(spec.file)
    selected.set(spec.file,previous?{file:spec.file,...(previous.pattern&&spec.pattern?{pattern:previous.pattern+'|'+spec.pattern}:{})}:spec)
  }
  return [...selected.values()]
}
export const gates={
  harness:{file:'harness'},p0:{file:'harness',args:['--p0']},
  'feishu-acp':{file:'feishu'},'feishu-web':{file:'feishu',args:['--web']},
  'resources-acp':{file:'feishu',args:['--resources']},'resources-web':{file:'feishu',args:['--resources','--web']},
  'progress-acp':{file:'feishu',args:['--progress']},'progress-web':{file:'feishu',args:['--progress','--web']},
  'questions-acp':{file:'feishu',args:['--questions']},'questions-web':{file:'feishu',args:['--questions','--web']},
  settings:{file:'settings'},'settings-desktop':{file:'settings',args:['--desktop']},
  composition:{file:'feishu',args:['--features']},'composition-web':{file:'feishu',args:['--features','--web']},
  desktop:{file:'desktop'},knowledge:{file:'knowledge'},'codex-native':{file:'codex'},
}
