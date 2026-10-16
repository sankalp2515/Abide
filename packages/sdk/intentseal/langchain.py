"""LangChain / LangGraph helper (SDK-7): protect an existing tool list in one line.

    from intentseal import Guard
    guard = Guard("my-agent")
    tools = guard.protect_tools(tools)          # or: protect_tools(guard, tools)
    with guard.session(user_id="u1", task=task):
        agent.invoke(...)

Every call is checked by the action guard before the tool runs (pinned fields rewritten, blocked calls return the
guard's notice to the model instead of running), and every tool result is inspected as untrusted content before
the model sees it. The wrapped tools keep their name, description and argument schema, so prompts and
tool-calling behaviour do not change.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Iterable

from intentseal_core.types import Decision

if TYPE_CHECKING:
    from langchain_core.tools import BaseTool

    from .guard import Guard


def protect_tools(guard: "Guard", tools: Iterable["BaseTool"], inspect_outputs: bool = True) -> list["BaseTool"]:
    from langchain_core.tools import StructuredTool

    def wrap(t: "BaseTool") -> "BaseTool":
        async def acall(**kwargs: Any) -> Any:
            r = await guard.acheck(t.name, kwargs)
            if r.enforced != Decision.ALLOW:
                return r.notice or f"Action '{t.name}' was blocked by the security policy."
            out = await t.ainvoke(r.final_args if r.final_args is not None else kwargs)
            if inspect_outputs and isinstance(out, str):
                out = await guard.ainspect(out, source=f"tool:{t.name}")
            return out

        def call(**kwargs: Any) -> Any:
            r = guard.check(t.name, kwargs)
            if r.enforced != Decision.ALLOW:
                return r.notice or f"Action '{t.name}' was blocked by the security policy."
            out = t.invoke(r.final_args if r.final_args is not None else kwargs)
            if inspect_outputs and isinstance(out, str):
                out = guard.inspect(out, source=f"tool:{t.name}")
            return out

        return StructuredTool(name=t.name, description=t.description, args_schema=t.args_schema,
                              func=call, coroutine=acall, return_direct=getattr(t, "return_direct", False))

    return [wrap(t) for t in tools]
