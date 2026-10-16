"""Command line.

  intentseal init [--agent my-agent] [--template research-assistant] [--mode enforce] [--home DIR]
      write a starter policy (and a .env template) for the SDK's embedded mode
  intentseal init --list                       the policy templates
  intentseal providers check [--no-call] [--models]
      providers, key *aliases* and health; one minimal test call per key (key values are never printed)
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

import yaml

from .config import config_path, load_settings, repo_root
from .llm.adapters import ChatRequest, ProviderError
from .llm.router import Router


def _check_model(settings, provider: str) -> str | None:
    p = settings.providers[provider]
    if p.options.get("check_model"):
        return str(p.options["check_model"])
    for role in settings.roles.values():
        for m in role.models:
            if m.provider == provider:
                return m.model
    return None


async def providers_check(call: bool, show_models: bool) -> int:
    settings = load_settings()
    router = Router(settings)
    bad = 0
    try:
        configured = {(m.provider, m.model) for r in settings.roles.values() for m in r.models}
        for name, p in sorted(settings.providers.items(), key=lambda kv: kv[1].priority):
            aliases = ", ".join(k.alias for k in p.keys) or "-"
            print(f"\n[{name}] type={p.type} priority={p.priority} keys={len(p.keys)} ({aliases})")
            print(f"  status: {router.provider_status(name)}")
            if not p.active:
                continue
            if show_models:
                try:
                    ids = await router.list_models(name)
                    print(f"  models available: {len(ids)}")
                    for prov, model in sorted(configured):
                        if prov == name:
                            found = model in ids or f"models/{model}" in ids
                            print(f"    configured {model}: {'FOUND' if found else 'NOT LISTED'}")
                except ProviderError as e:
                    print(f"  models: error {e.kind}")
            if not call:
                continue
            model = _check_model(settings, name)
            if not model:
                print("  test call: skipped (no model configured for this provider)")
                continue
            for key in p.keys:
                req = ChatRequest(messages=[{"role": "user", "content": "Reply with the single word: ok"}],
                                  max_tokens=64, temperature=0)
                try:
                    r = await router.call_direct(name, model, req, key=key)
                    text = (r.content or "").strip().replace("\n", " ")[:20]
                    print(f"  {key.alias}: OK  {r.latency_ms:.0f} ms  model={model}  reply='{text}'")
                except ProviderError as e:
                    bad += 1
                    print(f"  {key.alias}: FAIL {e.kind} (HTTP {e.http_status})  model={model}")
    finally:
        await router.aclose()
    return 1 if bad else 0


ENV_TEMPLATE = """# IntentSeal settings (read from this folder and from your project's .env). Keys are never printed or logged.
# The AI judge (Tier 2) uses free-tier providers by default; set at least one key:
GROQ_API_KEY=
GEMINI_API_KEY=
NVIDIA_API_KEY=
OPENROUTER_API_KEY=
# Values that must never leave through a tool argument (comma-separated), e.g. a test canary:
# INTENTSEAL_CANARIES=
"""


def init(agent: str, template: str, mode: str, home: str | None, list_only: bool) -> int:
    templates = {f.stem: f for f in sorted(config_path("policy-templates").glob("*.yaml"))}
    if list_only:
        for name, f in templates.items():
            data = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
            print(f"{name:20} {data.get('description', '')}")
        return 0
    if template not in templates:
        print(f"unknown template '{template}'; choose one of: {', '.join(templates)}", file=sys.stderr)
        return 2
    from .policy import AgentPolicy, policy_path, save_policy

    if home:
        os.environ["INTENTSEAL_HOME"] = str(Path(home).resolve())
    root = repo_root()
    raw = dict((yaml.safe_load(templates[template].read_text(encoding="utf-8")) or {}).get("policy") or {})
    raw.update({"agent": agent, "mode": mode})
    try:
        policy = AgentPolicy.model_validate(raw)
        target = policy_path(agent)
    except ValueError as e:
        print(f"invalid agent id or policy: {str(e)[:200]}", file=sys.stderr)
        return 2
    if target.exists():
        print(f"{target} already exists; edit it or choose another --agent")
        return 1
    path = save_policy(policy)
    env = root / ".env"
    if not env.exists():
        env.parent.mkdir(parents=True, exist_ok=True)
        env.write_text(ENV_TEMPLATE, encoding="utf-8")
    print(f"policy  {path}  ({template}, {mode} mode, tools: {', '.join(policy.tools) or 'none'})")
    print(f"keys    {env}  (add at least one provider key for the AI judge)")
    print(f"""
Next, in your agent:

    from intentseal import Guard
    guard = Guard("{agent}")                              # embedded: pip install "intentseal[embedded]"
    # guard = Guard("{agent}", remote="https://<gateway>", api_key=...)   # or a shared gateway

    with guard.session(user_id="u1", task=user_message):
        text = guard.inspect(page_html, source="web")     # cleaned content, or a safe notice

    @guard.tool()                                         # checked before it runs
    def {next(iter(policy.tools), 'send_message')}(...): ...

Rename the policy's tools to your agent's tool names: any tool not listed is blocked in enforce mode.""")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="intentseal")
    sub = ap.add_subparsers(dest="cmd", required=True)
    pr = sub.add_parser("providers", help="provider operations")
    prs = pr.add_subparsers(dest="sub", required=True)
    chk = prs.add_parser("check", help="list providers, key aliases, health; test each key")
    chk.add_argument("--no-call", action="store_true", help="do not make test calls")
    chk.add_argument("--models", action="store_true", help="verify configured model IDs against /models")
    ini = sub.add_parser("init", help="write a starter policy and a .env template")
    ini.add_argument("--agent", default="my-agent", help="agent id (letters, digits, '.', '_' or '-')")
    ini.add_argument("--template", default="research-assistant", help="policy template (see --list)")
    ini.add_argument("--mode", default="enforce", choices=["enforce", "monitor"])
    ini.add_argument("--home", help="folder for policies and settings (default: INTENTSEAL_HOME or ~/.intentseal)")
    ini.add_argument("--list", action="store_true", help="list the templates and exit")
    args = ap.parse_args(argv)
    if args.cmd == "init":
        return init(args.agent, args.template, args.mode, args.home, args.list)
    if args.cmd == "providers" and args.sub == "check":
        return asyncio.run(providers_check(call=not args.no_call, show_models=args.models))
    return 2


if __name__ == "__main__":
    sys.exit(main())
