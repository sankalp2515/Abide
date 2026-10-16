import { useRef, useState } from "react";
import { signIn, signOut, useReviewer } from "../reviewer";
import { ErrorNote } from "./ui";

/** Header control: sign in with a reviewer (admin) key to approve, label decisions and edit policy. */
export function ReviewerSignIn({ label = "Reviewer sign-in" }: { label?: string }) {
  const reviewer = useReviewer();
  const dialog = useRef<HTMLDialogElement>(null);
  const [key, setKey] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  if (reviewer) {
    return (
      <span className="flex items-center gap-2 text-xs">
        <span className="tag" style={{ color: "var(--accent-text)" }} title="Reviewer actions are enabled in this tab">Reviewer · {reviewer.name}</span>
        <button className="pill pill-sm" onClick={signOut}>Sign out</button>
      </span>
    );
  }

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await signIn(key);
      setKey("");
      dialog.current?.close();
    } catch (err) {
      setError((err as Error).message === "missing or invalid API key" ? "That key was not accepted." : (err as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <button className="pill pill-sm" onClick={() => { setError(null); dialog.current?.showModal(); }}>{label}</button>
      <dialog ref={dialog} aria-label="Reviewer sign-in" className="card m-auto w-[min(92vw,440px)] p-7 backdrop:backdrop-blur-sm"
        style={{ color: "var(--text)" }}>
        <form onSubmit={submit} className="flex flex-col gap-4">
          <h2 className="h3">Reviewer sign-in</h2>
          <p className="subtle m-0 text-xs leading-6">
            Paste your reviewer (admin) key to approve escalated actions, confirm attacks or false alarms, register agents and edit policy.
            It stays in this browser tab only and is cleared when the tab closes.
          </p>
          <p className="subtle m-0 text-xs leading-6">
            Running the local Docker setup? The default reviewer key is <span className="mono">local-admin-key</span> (it works only on this machine).
          </p>
          <label className="flex flex-col gap-2 text-xs">
            <span className="section-label">Reviewer key</span>
            <input className="input mono" type="password" autoComplete="off" spellCheck={false} required value={key}
              onChange={(e) => setKey(e.target.value)} autoFocus />
          </label>
          {error && <ErrorNote>{error}</ErrorNote>}
          <div className="flex justify-end gap-3">
            <button type="button" className="pill pill-sm" onClick={() => dialog.current?.close()}>Cancel</button>
            <button className="pill pill-primary pill-sm" disabled={busy || !key.trim()}>{busy ? "Checking…" : "Sign in"}</button>
          </div>
        </form>
      </dialog>
    </>
  );
}
