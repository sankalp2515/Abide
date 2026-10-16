# IntentSeal

**A prompt-injection firewall for AI agents.** IntentSeal inspects everything your agent reads (user messages, web
pages, PDFs, Word files, emails, Markdown, HTML, API responses, OCR text, source code, images, retrieved RAG passages)
before the model sees it, and checks every tool call before it runs. It keeps the agent inside the user's intent.

- **Detects and neutralises** instruction override, role change, secret extraction, tool abuse, credential theft,
  context poisoning, multi-step jailbreaks, encoded instructions and indirect prompt injection. Every decision is
  labelled with the attack type.
- **Sees what the model sees**: hidden text in PDFs and Word files, CSS-hidden HTML, HTML-only email parts, look-alike
  letters and encoded payloads (base64, hex, %-encoding, Unicode tags) are decoded and checked.
- **Rules decide, AI advises**: fast structural rules and a local classifier first, an AI judge only when needed, and
  deterministic action rules (allow-lists, pinned fields, outbound secret scan, "did the user ask for this?") that a
  model can never override.
- **Any model provider**: the SDK checks content and tools, not the model call, so it works with OpenAI, Anthropic,
  Gemini, Groq, Mistral, local models or anything else.

> Status: alpha (0.1). APIs may change between minor versions.

## Install

```bash
pip install intentseal                 # talks to an IntentSeal gateway (remote mode): small install
pip install "intentseal[embedded]"     # runs the engine in your process (PDF/Word/HTML extraction, classifier, OCR)
pip install "intentseal[langchain]"    # LangChain / LangGraph tool helper
```

Python 3.11+.

## Quickstart (embedded)

```bash
intentseal init --agent my-agent --template research-assistant    # writes ~/.intentseal/policies/my-agent.yaml
intentseal init --list                                             # other templates (RAG, support, coding agent)
```

Add one provider key for the AI judge to `~/.intentseal/.env` or your project's `.env` (`GROQ_API_KEY`,
`GEMINI_API_KEY`, `NVIDIA_API_KEY` or `OPENROUTER_API_KEY`). Then:

```python
from intentseal import Guard

guard = Guard("my-agent")

with guard.session(user_id="u-42", task=user_message):
    page = guard.inspect(html, source="web")                       # cleaned content, or a short safe notice
    doc = guard.inspect(pdf_bytes, source="pdf", filename="q3.pdf")  # raw bytes: hidden text is seen too

    @guard.tool()                                                  # arguments checked before it runs
    def send_message(to: str, subject: str, body: str): ...

    send_message(to="someone@elsewhere.example", subject="...", body="...")   # blocked: returns a notice
```

The policy (`~/.intentseal/policies/my-agent.yaml`) lists your agent's tools with their risk, allow-lists (recipients,
domains, paths), pinned fields and the words that count as the user asking for an action. Any tool not listed is
blocked in enforce mode; `mode: monitor` records everything and blocks nothing.

## Remote mode (a shared gateway, Console, audit trail)

```python
guard = Guard("my-agent", remote="https://intentseal.example.com", api_key=os.environ["INTENTSEAL_KEY"])
```

Same API; decisions come from the gateway, which also offers an OpenAI- and Anthropic-compatible **LLM proxy** (protect
an agent by changing its base URL only), a Security Console (decisions, approvals, session replay, policy) and SIEM
export.

## RAG

Check files when you index them and passages when you retrieve them:

```python
r = guard.inspect_result(open(path, "rb").read(), source="rag", filename=path)
if r.enforced.value in ("BLOCK", "ESCALATE"):
    quarantine(path)                                               # never enters the index
else:
    index(r.cleaned_content)

with guard.session(user_id=user.id, task=question):
    passages = [guard.inspect(c.text, source="rag") for c in retriever.search(question)]
```

## LangChain / LangGraph

```python
tools = guard.protect_tools([search_tool, email_tool])             # inputs checked, outputs inspected
```

## Links

Source, documentation, evaluation results and the gateway: https://github.com/sankalp2515/intentseal

Licensed under the Apache License 2.0.
