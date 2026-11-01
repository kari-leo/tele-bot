/** rc.5 embeddings own a public model-selection ref; newer Agent.model is unavailable. */
import type { Context } from '@deepseek-ai/cordis'
import type { Agent, ModelSelectionRef, ModelSelection } from '@deepseek-ai/dsh-agent'
export class HarnessModels {
  private readonly selections=new WeakMap<Agent,ModelSelectionRef>()
  async mount(ctx:Context,agent:Agent,initial:ModelSelection){
    const {installModelSelection}=await import('@deepseek-ai/dsh-agent')
    const selection:ModelSelectionRef={current:{...initial},assembled:undefined}
    this.selections.set(agent,selection)
    const dispose=installModelSelection(ctx,selection);ctx.effect(()=>dispose)
  }
  current(agent:Agent):ModelSelection|undefined{
    return this.selections.get(agent)?.current??(agent.options?.provider&&agent.options?.model?{provider:agent.options.provider,model:agent.options.model}:undefined)
  }
  select(agent:Agent,value:ModelSelection){
    const selection=this.selections.get(agent)
    if(!selection)throw new Error('conversation_model_owned_elsewhere')
    selection.current={...value}
  }
}
