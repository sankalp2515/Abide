# Security policy

Abide is a security product, so reports are welcome and taken seriously.

## Reporting a vulnerability

Please do not open a public issue. Use GitHub's private vulnerability reporting on this repository
("Security" → "Report a vulnerability"), with the steps to reproduce and the impact you observed. You will get an
acknowledgement within three working days.

In scope: a way for text an agent reads, or text pasted into a request, to widen what agents may do; a way for anyone
other than the person who delegated (or an admin) to read, confirm, amend or approve a delegation; a forged or altered
receipt chain or handoff seal that verifies; secrets or delegation content exposed through the API, logs or the
Console bundle.

## Supported versions

Only the latest release on `main` receives fixes.

## Operating it safely

- Replace every `local-*` default key and secret in `.env` before exposing the gateway beyond your machine
  (see `.env.example`). Keep `INTENTSEAL_DELEGATE_SECRET` stable and private: it signs agent-to-agent handoffs.
- The Console's public key is built into the web bundle and is read-only; it cannot read delegations.
- Known limits and their planned fixes are listed in [docs/ENGINEERING_REVIEW.md](docs/ENGINEERING_REVIEW.md).
