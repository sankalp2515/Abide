# Abide design: The Ledger

Abide keeps AI agents to a person's word. The interface borrows from the objects people already use to hold each
other to their word: the **work order**, the **rubber stamp**, the **receipt** and the **margin note**. There are no
illustrations; the product's own objects are the visuals.

## Colour: each colour has one job

| Token | Light (ledger paper) | Night shift | Job |
|---|---|---|---|
| `--bg` | `#F3F4F1` | `#0F141C` | the page |
| `--surface` | `#FFFFFF` | `#18202B` | cards, the work order |
| `--text` | `#101826` (16.9:1) | `#E6E8E3` | ink |
| `--subtle` | `#4A5466` (7.3:1) | `#A6AEB9` | second text, labels |
| `--accent` / `--allow` | `#1D5E46` | `#6CC59E` | brand, primary action, *Ran* |
| `--block` | `#B4321E` | `#F07A63` | *Stopped* only |
| `--neutralise` | `#8A5D00` | `#E2B652` | *Waits for you*, cleaned content |

Never a gradient, glow or glass panel. Light is the default; Night shift is remembered when chosen.

## Type: three voices

- **Schibsted Grotesk**: the system (headings, interface, body).
- **Newsreader italic** (`.words`): only ever the person's own words. If it is serif, a human said it.
- **Fragment Mono** (`.mono`, `.section-label`): the record (receipt ids, hashes, amounts, labels).

## Stamps: word, border and colour

`<Stamp kind="ran|stopped|wait|quiet">`. *Ran*: solid border. *Stopped*: double border, turned 2.5°. *Waits for you*:
dashed border. Each carries its word and an icon, so colour is never the only signal.

## Layout

- **Landing**: 1280 px content, an 80 px outer margin, and a 180 px margin rail for section numbers (§ 01). Text is
  left-aligned like a contract, never centred.
- **App**: four places, one idea each.
  - **Tasks**: one list of sandbox runs and your own agents' tasks, plus a task view (work order, steps, questions,
    receipt). Replay opens from a task.
  - **Needs you**: every waiting question across tasks, with a badge, plus the reviewers' queue.
  - **Activity**: the decision feed, BigQuery insights and what was learned from review.
  - **Connect**: your key, how to connect an agent, the agents that acted under your work orders (live), your
    businesses, and advanced filter settings.

  Old addresses (`/delegate`, `/agents`, `/decisions`, `/review`, `/setup`, `/policy`) still open. Below 1024 px the
  sidebar becomes a top bar, and the open task shows before the list.
- **Shape**: 6 px corners, 1 px rules, 2 px dashed perforation under a work order's stub.

## Motion: only when something is decided

| Name | What happens | When |
|---|---|---|
| Press | A stamp lands, scale 1.12 → 1 in 160 ms | a decision |
| Ink | A quoted phrase underlines left to right in 500 ms | it becomes a line of the order |
| Settle | Cards slide up 8 px and stop | new content |
| Scan | A thin line runs under the timeline | agents are working |
| Tilt | The hero work order follows the pointer, at most 5° | fine pointers only |

`prefers-reduced-motion` turns all of it off.

## Accessibility

- Text contrast is 4.5:1 or more, and 3:1 or more for large text.
- Focus is a 3 px accent ring.
- Touch targets are 44 px or larger.
- Every control is a real `button`, `a`, `input` or `label`.
- Live regions announce progress.
