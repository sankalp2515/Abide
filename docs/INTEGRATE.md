# Bring your own agent and your own business

IntentSeal Delegate governs **any** agent and **any** business you describe, not just the two demo sandboxes.
Nothing to sign up for and nobody to contact: a deployment with self-serve keys gives you a private key in one call.

**Fastest path:** open [`examples/colab/intentseal_delegate_quickstart.ipynb`](../examples/colab/intentseal_delegate_quickstart.ipynb)
in Google Colab, set `GATEWAY` to the service URL, and run all cells (about two minutes). It registers a travel
agency, turns an employee's words into a permission card, runs a real Gemini agent (Google ADK) under that card, and
prints what ran, what was stopped and the receipt.

## 1. A key

```bash
curl -X POST https://<gateway>/v1/trial-keys          # a private 7-day project key (when the deployment allows it)
```

Your delegations and your businesses are visible only to your key (and the operator's admins). Keys are limited to
three per address per hour. An operator issues permanent keys through `INTENTSEAL_API_KEYS`.

## 2. The client

One file, only `httpx` needed, served by the gateway itself:

```bash
curl -O https://<gateway>/v1/sdk/intentseal_delegate.py
```

(or `from intentseal.delegate import DelegateClient, RemoteAdkGuard` with the `intentseal` package installed).

## 3. Describe your business, once

```python
from intentseal_delegate import DelegateClient, RemoteAdkGuard

client = DelegateClient("https://<gateway>", key)          # or: await DelegateClient.with_trial_key(url)
await client.register_workspace("northwind-travel", {
    "org_domains": ["northwind-travel.example"],           # addresses inside the company
    "currency": "$",
    "tools": {                                             # every tool your agents may call, and what it does
        "read_email":   {"action": "read", "args": ["email_id"]},
        "read_booking": {"action": "read", "resource_args": ["booking_id"]},
        "issue_refund": {"action": "purchase", "resource_args": ["booking_id"], "amount_args": ["amount"]},
        "send_email":   {"action": "send", "recipient_args": ["to"], "content_args": ["subject", "body"]},
    },
    "contacts":  [{"id": "priya", "name": "Priya Shah", "address": "priya.shah@mail.example", "aliases": ["priya"]}],
    "resources": [{"id": "price-list", "name": "Price list", "aliases": ["price list"], "data_class": "internal"}],
    "records":   [{"type": "booking", "words": ["booking"], "pattern": "digits", "data_class": "confidential"}],
    "verbs":     {"purchase": ["refund"]},                 # your business's words for an action
    "ask_before": [{"action": "send", "when": "external_recipient"}],   # optional company rule
})
```

| Field | Meaning |
|---|---|
| `tools.<name>.action` | `read`, `draft`, `write`, `send`, `share`, `submit`, `purchase`, `delete` or `execute`. Anything other than read and draft is an action that leaves or changes something, and needs the employee's words. |
| `recipient_args` | Fields that name who receives something; checked against the people the employee named. |
| `resource_args` | Fields that name a file or record; checked against what the employee named. |
| `amount_args` | Money or quantities; checked against the limit in the employee's words, across the whole task. |
| `content_args` | Free text (bodies); traced back to untrusted content, and confidential text cannot leave in them. |
| `args` | Other harmless fields. A call carrying a field the tool does not declare is refused on actions that leave. |
| `contacts`, `resources` | The trusted directory: the only place names are resolved from. What an agent reads never adds to it. |
| `records` | Things too many to list (orders, bookings, tickets), named by a word and a number: `pattern` is `digits` or `code`. Mark personal records `confidential`: then even reading one requires the employee to name it. |

Limits: 50 tools, 200 contacts, 200 resources, 10 record types; 20 businesses per key.

## 4. Delegate, and put your agent under the card

```python
card = await client.start("Read Priya's email about booking #4410, refund it up to $300 and reply to her.",
                          workspace="northwind-travel")
for line in card["clauses"]:
    print(line["plain"], "←", line["quote"])               # every line quotes the employee's words
await client.confirm(card)                                 # confirm exactly this card, once

guard = RemoteAdkGuard(client, card["workflow_id"], "travel-agent", on_ask=my_approval_handler)
agent = LlmAgent(name="travel_agent", model="gemini-3.5-flash", tools=[...],
                 before_tool_callback=guard.before_tool,   # every call checked before it runs
                 after_tool_callback=guard.after_tool)     # runs recorded; what reading tools return is inspected
...
report = await client.report(card["workflow_id"])          # sentences that cite hash-chained receipts
```

A stopped call never runs; the agent is told why and carries on with the rest. When a company rule needs the
employee (`ask_before`), `on_ask(check)` is awaited (a person, a chat message, a ticket); without it the call waits and
`client.answer(workflow_id, approval_id, True)` approves that one exact action later.

## Not using ADK

The same checks are plain REST (all documented at `https://<gateway>/docs`):

| Step | Call |
|---|---|
| Employee's words → card | `POST /v1/delegations` `{"request", "workspace"}` |
| Confirm the card | `POST /v1/delegations/{id}/confirm` `{"hash"}` |
| Before each tool call | `POST /v1/delegations/{id}/check` `{"agent", "tool", "args"}` → `allow`, `ask` or `block` (+ `notice` for the agent) |
| After it ran | `POST /v1/delegations/{id}/executed` `{"receipt", "executed"}` |
| What a reading tool returned | `POST /v1/delegations/{id}/read` `{"agent", "content" or "content_b64", "filename"}` → cleaned content |
| The receipt | `GET /v1/delegations/{id}/report` |
