"""AgentScope runtime: build a ReAct-style Agent from a persona + its skills and run it.

Targets AgentScope 2.x (``agentscope==2.0.11``): models are built from a
credential + model id, tools are registered as ``FunctionTool``, and the agent
is driven through ``Agent.reply``.
"""

import functools
from typing import AsyncIterator, Callable

from app.agents.plugins import PersonaSpec, SkillSpec, current_user_id
from app.config import get_settings


class LLMNotConfigured(Exception):
    pass


def llm_configured() -> bool:
    return bool(get_settings().llm_api_key)


def _build_model(stream: bool = False):
    """Map our provider settings onto an AgentScope chat model."""
    from agentscope import credential, model

    s = get_settings()
    if not s.llm_api_key:
        raise LLMNotConfigured("未配置 LLM_API_KEY")
    provider = s.llm_provider.lower()
    base_url = s.llm_base_url or None
    if provider == "anthropic":
        return model.AnthropicChatModel(
            credential=credential.AnthropicCredential(api_key=s.llm_api_key, base_url=base_url),
            model=s.llm_model,
            stream=stream,
        )
    if provider == "dashscope":
        return model.DashScopeChatModel(
            credential=credential.DashScopeCredential(api_key=s.llm_api_key, base_url=base_url),
            model=s.llm_model,
            stream=stream,
        )
    if provider == "openai":
        # OpenAI-compatible gateways (火山方舟 / vLLM / one-api ...) go through here.
        return model.OpenAIChatModel(
            credential=credential.OpenAICredential(api_key=s.llm_api_key, base_url=base_url),
            model=s.llm_model,
            parameters=model.OpenAIChatModel.Parameters(max_tokens=8000),
            stream=stream,
        )
    raise LLMNotConfigured(f"不支持的 LLM_PROVIDER: {s.llm_provider}")


def _as_tool(fn: Callable):
    """Skill tools return plain str; report failures as text instead of raising."""
    from agentscope.tool import FunctionTool

    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        try:
            return await fn(*args, **kwargs)
        except Exception as exc:
            return f"工具执行出错: {exc}"

    # wraps() keeps the signature/docstring, which FunctionTool turns into the tool schema.
    return FunctionTool(wrapper, permission=_pre_approved())


def _pre_approved():
    """These agents run server-side, so there is no human to answer a confirm prompt."""
    from agentscope.permission import PermissionBehavior, PermissionDecision

    return PermissionDecision(behavior=PermissionBehavior.ALLOW, message="平台内置技能工具，服务端已预授权")


async def _register_skill_tools(toolkit, skills: list[SkillSpec]) -> None:
    for sk in skills:
        for fn in sk.load_tools():
            await toolkit.add_tool(_as_tool(fn))


def build_system_prompt(persona: PersonaSpec, skills: list[SkillSpec]) -> str:
    prompt = persona.system_prompt.strip()
    if skills:
        prompt += "\n\n# 你掌握的技能\n"
        for sk in skills:
            prompt += f"\n## {sk.name}\n{sk.description}\n\n{sk.instructions}\n"
    return prompt


async def run_agent(persona: PersonaSpec, skills: list[SkillSpec], prompt: str, user_id: int | None = None) -> str:
    from agentscope.agent import Agent, ReActConfig
    from agentscope.message import UserMsg
    from agentscope.tool import Toolkit

    toolkit = Toolkit()
    await _register_skill_tools(toolkit, skills)

    agent = Agent(
        name=persona.name,
        system_prompt=build_system_prompt(persona, skills),
        model=_build_model(),
        toolkit=toolkit,
        react_config=ReActConfig(max_iters=8),
    )

    token = current_user_id.set(user_id)
    try:
        reply = await agent.reply(UserMsg(name="user", content=prompt))
    finally:
        current_user_id.reset(token)
    return reply.get_text_content() or ""


async def run_agent_stream(
    persona: PersonaSpec, skills: list[SkillSpec], prompt: str, user_id: int | None = None
) -> AsyncIterator[str]:
    """Same agent as ``run_agent``, but yields text deltas as the model streams them."""
    from agentscope.agent import Agent, ReActConfig
    from agentscope.event import TextBlockDeltaEvent
    from agentscope.message import UserMsg
    from agentscope.tool import Toolkit

    toolkit = Toolkit()
    await _register_skill_tools(toolkit, skills)

    agent = Agent(
        name=persona.name,
        system_prompt=build_system_prompt(persona, skills),
        model=_build_model(stream=True),
        toolkit=toolkit,
        react_config=ReActConfig(max_iters=8),
    )

    token = current_user_id.set(user_id)
    try:
        async for evt in agent.reply_stream(UserMsg(name="user", content=prompt)):
            if isinstance(evt, TextBlockDeltaEvent):
                yield evt.delta
    finally:
        current_user_id.reset(token)
