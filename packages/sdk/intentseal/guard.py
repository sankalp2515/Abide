"""Python SDK (SDK-1…SDK-5): protect an existing agent with a few lines.

    guard = Guard("my-agent")                       # SDK-1 embedded mode (engine in-process)
    guard = Guard("my-agent", remote="https://gw", api_key=...)   # SDK-6 remote mode (Gateway API)
    tools = guard.protect_tools(langchain_tools)          # SDK-7 LangChain / LangGraph tools
    with guard.session(user_id="u1", task="Summarise my inbox", trusted={"customer_id": "C-1042"}):   # SDK-4
        text = guard.inspect(raw_bytes, source="pdf", filename="report.pdf")                          # SDK-2
        send_email(to=..., body=...)                       # decorated with @guard.tool()              # SDK-3

Failure policy (SDK-5): if the engine itself fails, tools marked `failure: closed` in the policy (write/send)
are blocked, and `failure: open` tools (read-only) proceed; both are logged.
"""

from __future__ import annotations

import asyncio
import contextvars
import functools
import inspect
import logging
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, Callable, Iterator

from intentseal_core.policy import AgentPolicy, load_policy, policy_exists, policy_path
from intentseal_core.session import Session
from intentseal_core.types import ActionResult, Decision, InspectResult

if TYPE_CHECKING:
    from intentseal_core.engine import Engine

log = logging.getLogger("intentseal.sdk")
_current: contextvars.ContextVar[Session | None] = contextvars.ContextVar("intentseal_session", default=None)


class ToolBlocked(Exception):
    """Raised by a guarded tool when `on_block="raise"`."""

    def __init__(self, result: ActionResult) -> None:
        super().__init__(result.notice or result.reason)
        self.result = result


class Guard:
    def __init__(self, agent_id: str, engine: Engine | None = None, mode: str | None = None,
                 policy: AgentPolicy | None = None, secrets: list[str] | None = None,
                 engine_options: dict[str, Any] | None = None, warm_up: bool = True,
                 remote: str | None = None, api_key: str | None = None, http_client: Any = None) -> None:
        self.agent_id = agent_id
        if remote or http_client is not None:   # SDK-6: decisions come from the Gateway API
            from .remote import RemoteEngine
            engine = RemoteEngine(remote or "http://gateway", api_key or "", client=http_client)
            policy = policy or engine.policy(agent_id)   # failure policy (SDK-5) uses the gateway's tool settings
            warm_up = False
        if engine is None:
            # Embedded mode runs the engine in-process; remote mode (above) needs only the light install.
            try:
                from intentseal_core.engine import Engine
                from intentseal_core.llm.router import Router
            except ImportError as e:
                raise ImportError(
                    f"Embedded mode needs the engine: pip install 'intentseal[embedded]' (missing: {e.name}). "
                    "Or use a gateway: Guard(agent_id, remote='https://<gateway>', api_key=...).") from e
        if policy is None and remote is None and http_client is None and not policy_exists(agent_id):
            log.warning("No policy file for agent '%s' (%s): the default policy runs in monitor mode, so nothing is "
                        "enforced. Create one with `intentseal init --agent %s`, or pass mode='enforce'.",
                        agent_id, policy_path(agent_id), agent_id)
        self._policy = policy or load_policy(agent_id)
        if mode:
            self._policy.mode = mode
        if engine is None:
            router = Router()   # shares the persistent store (settings.db_path) for usage, cache and decisions
            opts = {"tier0_timeout_s": 10.0, **(engine_options or {})}   # extraction incl. OCR on CPU
            engine = Engine(policy_loader=self._load, router=router, store=router.store, **opts)
        self.engine = engine
        if secrets:
            self.engine.register_secrets(secrets)
        if warm_up:
            _warm_up()

    def _load(self, agent_id: str) -> AgentPolicy:
        return self._policy if agent_id == self.agent_id else load_policy(agent_id)

    @property
    def policy(self) -> AgentPolicy:
        return self._policy

    # ---- sessions (SDK-4) --------------------------------------------------------------------
    @contextmanager
    def session(self, user_id: str | None = None, task: str = "", trusted: dict[str, Any] | None = None) -> Iterator[Session]:
        s = self.engine.start_session(self.agent_id, user_ref=user_id, task=task, trusted=trusted)
        token = _current.set(s)
        try:
            yield s
        finally:
            _current.reset(token)

    def start_session(self, user_id: str | None = None, task: str = "", trusted: dict[str, Any] | None = None) -> Session:
        s = self.engine.start_session(self.agent_id, user_ref=user_id, task=task, trusted=trusted)
        _current.set(s)
        return s

    @property
    def current_session(self) -> Session | None:
        return _current.get()

    def _sid(self, session_id: str | None) -> str | None:
        if session_id:
            return session_id
        s = _current.get()
        return s.session_id if s else None

    def register_secrets(self, values: list[str]) -> None:
        self.engine.register_secrets(values)

    # ---- inbound content (SDK-2) -------------------------------------------------------------
    async def ainspect_result(self, content: bytes | str, source: str, filename: str | None = None,
                              session_id: str | None = None) -> InspectResult:
        return await self.engine.inspect(content, source, self.agent_id, session_id=self._sid(session_id),
                                         filename=filename)

    async def ainspect(self, content: bytes | str, source: str, filename: str | None = None,
                       session_id: str | None = None) -> str:
        """Return what the agent should see: cleaned content, or a safe notice when blocked."""
        try:
            r = await self.ainspect_result(content, source, filename, session_id)
        except Exception as e:  # inspection unavailable (e.g. remote gateway down): never pass content unseen
            log.warning("inspection failed for %s (%s); content withheld", source, type(e).__name__)
            return f"[Content from {source} was withheld: the security check is unavailable]"
        if r.enforced == Decision.ALLOW and isinstance(content, str):
            return content   # allowed text passes unchanged (G3b); files return their extracted view
        if r.enforced == Decision.BLOCK:
            return f"[Content from {source} was withheld by the security policy: {r.reason}]"
        if r.enforced == Decision.ESCALATE:
            return f"[Content from {source} is held for human review: {r.reason}]"
        return r.cleaned_content

    def inspect(self, content: bytes | str, source: str, filename: str | None = None, session_id: str | None = None) -> str:
        return _run(self.ainspect(content, source, filename, session_id))

    def inspect_result(self, content: bytes | str, source: str, filename: str | None = None,
                       session_id: str | None = None) -> InspectResult:
        return _run(self.ainspect_result(content, source, filename, session_id))

    # ---- the model's answer -------------------------------------------------------------------
    async def acheck_output(self, text: str, session_id: str | None = None) -> str:
        """What the user may see of the model's final answer: protected values (canaries, registered secrets, known
        secret formats, also encoded) are redacted, or the answer is withheld when they cannot be."""
        try:
            r = await self.engine.check_output(text, self.agent_id, session_id=self._sid(session_id))
        except Exception as e:   # the check is unavailable: the answer is not known to be safe
            log.warning("answer check failed (%s); answer withheld", type(e).__name__)
            return "[The answer was withheld: the security check is unavailable]"
        return r.cleaned_content

    def check_output(self, text: str, session_id: str | None = None) -> str:
        return _run(self.acheck_output(text, session_id))

    # ---- actions (SDK-3, SDK-5) --------------------------------------------------------------
    async def acheck(self, tool: str, args: dict[str, Any], session_id: str | None = None) -> ActionResult:
        try:
            return await self.engine.check_action(tool, args, self.agent_id, session_id=self._sid(session_id))
        except Exception as e:  # engine failure: apply the tool's failure policy
            tp = self._policy.tools.get(tool)
            closed = (tp.failure if tp else "closed") == "closed"
            log.warning("guard check failed for %s (%s); failing %s", tool, type(e).__name__, "closed" if closed else "open")
            d = Decision.BLOCK if closed else Decision.ALLOW
            return ActionResult(decision=d, enforced=d, final_args=None if closed else dict(args), degraded=True,
                                policy_rule=f"guard-failure-fail-{'closed' if closed else 'open'}",
                                reason=f"guard unavailable ({type(e).__name__})",
                                notice=f"Action '{tool}' was not performed: the security check is unavailable." if closed else None)

    def check(self, tool: str, args: dict[str, Any], session_id: str | None = None) -> ActionResult:
        return _run(self.acheck(tool, args, session_id))

    def record_execution(self, result: ActionResult | str | None, executed: bool, detail: str = "") -> None:
        """Execution receipt: tell Abide whether an allowed call actually ran (the Console's provenance view shows
        it as evidence). Best effort: a receipt never breaks the tool call."""
        event_id = result.event_id if isinstance(result, ActionResult) else result
        if not event_id:
            return
        try:
            self.engine.record_execution(event_id, executed, detail)
        except Exception as e:
            log.debug("execution receipt not recorded (%s)", type(e).__name__)

    def tool(self, name: str | None = None, pinned: dict[str, Any] | None = None,
             on_block: str = "notice") -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """Decorate a tool function. Arguments are checked (and pinned fields rewritten) before it runs.

        pinned: extra trusted values for this tool's arguments, merged into the session's trusted context.
        on_block: "notice" returns the guard's notice string; "raise" raises ToolBlocked.
        """

        def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
            tool_name = name or fn.__name__
            sig = inspect.signature(fn)

            def bind(args: tuple, kwargs: dict) -> dict[str, Any]:
                b = sig.bind_partial(*args, **kwargs)
                return dict(b.arguments)

            def apply_pins() -> None:
                s = _current.get()
                if s is not None and pinned:
                    s.trusted.update(pinned)
                    s.note_trusted(" ".join(str(v) for v in pinned.values()))

            def blocked(r: ActionResult) -> Any:
                if on_block == "raise":
                    raise ToolBlocked(r)
                return r.notice or f"Action '{tool_name}' was blocked by the security policy."

            if inspect.iscoroutinefunction(fn):
                @functools.wraps(fn)
                async def awrapper(*args: Any, **kwargs: Any) -> Any:
                    apply_pins()
                    r = await self.acheck(tool_name, bind(args, kwargs))
                    if r.enforced != Decision.ALLOW:
                        return blocked(r)
                    try:
                        out = await fn(**(r.final_args or {}))
                    except Exception as e:
                        self.record_execution(r, False, f"raised {type(e).__name__}")
                        raise
                    self.record_execution(r, True)
                    return out
                return awrapper

            @functools.wraps(fn)
            def wrapper(*args: Any, **kwargs: Any) -> Any:
                apply_pins()
                r = self.check(tool_name, bind(args, kwargs))
                if r.enforced != Decision.ALLOW:
                    return blocked(r)
                try:
                    out = fn(**(r.final_args or {}))
                except Exception as e:
                    self.record_execution(r, False, f"raised {type(e).__name__}")
                    raise
                self.record_execution(r, True)
                return out
            return wrapper

        return deco


    def protect_tools(self, tools: Any, inspect_outputs: bool = True) -> list[Any]:
        """SDK-7: wrap LangChain / LangGraph tools (see intentseal.langchain)."""
        from .langchain import protect_tools
        return protect_tools(self, tools, inspect_outputs)


def _warm_up() -> None:
    """Load the local classifier and OCR once so the first real inspection is not slowed down."""
    try:
        import io

        from PIL import Image, ImageDraw

        from intentseal_core import extract as _extract
        from intentseal_core.classifier import local_classifier

        local_classifier()
        img = Image.new("RGB", (160, 40), "white")
        ImageDraw.Draw(img).text((5, 10), "warm up", fill="black")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        _extract.extract(buf.getvalue(), fmt="image")
    except Exception as e:  # warm-up is best effort
        log.info("warm-up skipped (%s)", type(e).__name__)


def _run(coro: Any) -> Any:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    raise RuntimeError("Guard sync methods cannot run inside an event loop; use the async variants (ainspect/acheck).")
