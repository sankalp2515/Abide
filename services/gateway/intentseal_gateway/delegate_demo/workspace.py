"""Sandboxed office tools. The execution log here is the ground truth: an action happened only if it is in `log`."""

from __future__ import annotations

from typing import Any

from . import fixtures


class Workspace:
    def __init__(self, attack: str | None = fixtures.DEFAULT_ATTACK, carrier: str = "pdf") -> None:
        """carrier: "pdf" (hidden text in the quote), "email" (visible text in the supplier's email), or "split"
        (a multi-step attack: `attack` is "part one || part two"; the first part in the PDF, the second in the email)."""
        self.inbox = fixtures.inbox()
        pdf_part = email_part = None
        if attack and carrier == "split":
            pdf_part, email_part = (p.strip() for p in attack.split("||", 1))
        elif carrier == "pdf":
            pdf_part = attack
        elif carrier == "email":
            email_part = attack
        self.drive = fixtures.drive(pdf_part)
        if email_part:
            self.inbox[0]["body"] += "\n\n" + email_part
        self.drafts: list[dict[str, Any]] = []
        self.sent: list[dict[str, Any]] = []
        self.log: list[dict[str, Any]] = []

    def source_of(self, tool: str, filename: str | None) -> str:
        """What the content firewall is told the content is."""
        if tool == "list_inbox":
            return "tool:list_inbox"
        if tool == "read_email":
            return "email"
        return "pdf" if (filename or "").endswith(".pdf") else "file"

    def label_of(self, tool: str, args: dict[str, Any], filename: str | None) -> str | None:
        """How the employee knows this content (receipts and "traced to" use it)."""
        if tool == "read_email":
            m = next((m for m in self.inbox if m["id"] == args.get("id")), None)
            return f"the email from {m['from']}" if m else None
        if tool == "read_file" and filename:
            return filename
        return None

    def run(self, tool: str, args: dict[str, Any]) -> tuple[Any, str | None]:
        """Execute a tool. Returns (result, filename) — filename when the result is file content."""
        self.log.append({"tool": tool, "args": args})
        if tool == "list_inbox":
            return "\n".join(f"{m['id']} | from {m['from']} | {m['subject']}" for m in self.inbox), None
        if tool == "read_email":
            m = next((m for m in self.inbox if m["id"] == args.get("id")), None)
            if m is None:
                return "no such email", None
            return (f"From: {m['from']}\nSubject: {m['subject']}\n\n{m['body']}\nAttachments: "
                    f"{', '.join(m['attachments']) or 'none'}"), None
        if tool == "read_file":
            name = str(args.get("name", ""))
            f = self._file(name)
            if f is None:
                return "no such file", None
            fname, item = f
            return (item["bytes"], fname) if "bytes" in item else (item["text"], None)
        if tool == "draft_email":
            self.drafts.append(dict(args))
            return f"draft d-{len(self.drafts)} saved", None
        if tool == "send_email":
            self.sent.append(dict(args))
            return "sent", None
        if tool == "share_file":
            return "shared", None
        if tool == "delete_file":
            f = self._file(str(args.get("name", "")))
            if f:
                self.drive.pop(f[0])
            return "deleted", None
        return f"unknown tool {tool}", None

    def _file(self, name: str) -> tuple[str, dict[str, Any]] | None:
        from intentseal_core.delegate.model import norm
        from intentseal_core.delegate import load_delegate_policy
        if name in self.drive:
            return name, self.drive[name]
        r = load_delegate_policy("workspace").resource(name)
        for fname, item in self.drive.items():
            if r and item["resource"] == r.id or norm(fname) == norm(name):
                return fname, item
        return None
