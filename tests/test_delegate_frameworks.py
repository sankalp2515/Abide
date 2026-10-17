"""The one-file client's framework adapters: a plain function, LangChain, the OpenAI Agents SDK and MCP, each held to
the same card through `ToolGate` against a running gateway."""

from __future__ import annotations

import asyncio
import dataclasses
import json
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from intentseal.delegate import DelegateClient, ToolGate, langchain_tools, mcp_call, openai_agents_tools
from test_delegate_remote_guard import KEY, app  # noqa: F401 - the gateway fixture
from test_delegate_workspaces import CLINIC

REQUEST = "Read patient #10442's record and message Dr Rao about it."
RAN: list[str] = []


def read_record(patient_id: str) -> str:
    """Read a patient's record."""
    RAN.append(f"read {patient_id}")
    return f"Patient {patient_id}: follow-up in 2 weeks."


async def send_message(to: str, body: str) -> str:
    """Send a message."""
    RAN.append(f"send {to}")
    return "sent"


async def session(a) -> tuple[httpx.AsyncClient, ToolGate]:
    http = httpx.AsyncClient(transport=httpx.ASGITransport(app=a), base_url="http://gw")
    client = DelegateClient("http://gw", KEY, client=http)
    await client.register_workspace("sunrise-clinic", CLINIC)
    card = await client.start(REQUEST, workspace="sunrise-clinic")
    await client.confirm(card)
    return http, ToolGate(client, card["workflow_id"], "nurse-bot")


@pytest.fixture(autouse=True)
def _clear():
    RAN.clear()


def test_a_wrapped_function_keeps_its_signature_and_is_held_to_the_card(app):  # noqa: F811
    a, _ = app

    async def go():
        http, gate = await session(a)
        read, send = gate.wrap(read_record), gate.wrap(send_message)
        text = await read("10442")
        stopped = await send(to="leak@elsewhere.example", body="records")
        await send("rao@sunrise-clinic.example", "Follow-up")
        await http.aclose()
        return text, stopped, gate.events, read

    text, stopped, events, read = asyncio.run(go())
    assert "10442" in text and RAN == ["read 10442", "send rao@sunrise-clinic.example"]   # the leak never ran
    assert isinstance(stopped, str) and [e["outcome"] for e in events][1] in ("blocked", "waiting")
    assert read.__name__ == "read_record" and read.__doc__ == "Read a patient's record."


def test_langchain_tools_are_guarded_and_sync_calls_are_refused(app):  # noqa: F811
    tools_mod = pytest.importorskip("langchain_core.tools")
    a, _ = app

    async def go():
        http, gate = await session(a)
        read, send = langchain_tools(gate, [tools_mod.tool(read_record), tools_mod.tool(send_message)])
        text = await read.ainvoke({"patient_id": "10442"})
        stopped = await send.ainvoke({"to": "leak@elsewhere.example", "body": "records"})
        with pytest.raises(RuntimeError):
            read.invoke({"patient_id": "10442"})
        await http.aclose()
        return text, stopped, read

    text, stopped, read = asyncio.run(go())
    assert "10442" in text and RAN == ["read 10442"] and isinstance(stopped, str)
    assert read.name == "read_record" and "patient_id" in read.args


@dataclasses.dataclass
class FunctionTool:                     # the OpenAI Agents SDK's shape: name + on_invoke_tool(ctx, json_args)
    name: str
    on_invoke_tool: Any


def test_openai_agents_function_tools_are_guarded(app):  # noqa: F811
    a, _ = app

    async def invoke(ctx, raw):
        return await send_message(**json.loads(raw))

    async def go():
        http, gate = await session(a)
        [send] = openai_agents_tools(gate, [FunctionTool("send_message", invoke)])
        stopped = await send.on_invoke_tool(None, json.dumps({"to": "leak@elsewhere.example", "body": "x"}))
        ok = await send.on_invoke_tool(None, json.dumps({"to": "rao@sunrise-clinic.example", "body": "Follow-up"}))
        await http.aclose()
        return stopped, ok

    stopped, ok = asyncio.run(go())
    assert ok == "sent" and RAN == ["send rao@sunrise-clinic.example"] and isinstance(stopped, str)


class McpSession:                        # an MCP ClientSession's shape: call_tool -> result.content[*].text
    async def call_tool(self, name, args):
        RAN.append(f"mcp {name}")
        text = read_record(**args) if name == "read_record" else "sent"
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)])


def test_mcp_tool_calls_are_guarded_and_reads_are_inspected(app):  # noqa: F811
    a, _ = app

    async def go():
        http, gate = await session(a)
        text = await mcp_call(gate, McpSession(), "read_record", {"patient_id": "10442"})
        stopped = await mcp_call(gate, McpSession(), "send_message", {"to": "leak@elsewhere.example", "body": "x"})
        await http.aclose()
        return text, stopped, gate.events

    text, stopped, events = asyncio.run(go())
    assert "10442" in text and "mcp send_message" not in RAN
    assert [e["outcome"] for e in events][0] == "ran" and events[1]["outcome"] in ("blocked", "waiting")
