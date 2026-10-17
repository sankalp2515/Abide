import { useSyncExternalStore } from "react";
import { api } from "./api";

// Reviewer session: the reviewer's own admin key, validated with /v1/whoami. Kept in this tab's sessionStorage only
// (cleared when the tab closes), never in localStorage and never in the build. The public key stays the default.
export interface Reviewer { key: string; name: string }

const STORE = "intentseal.reviewer";
let current: Reviewer | null = (() => {
  try {
    const raw = sessionStorage.getItem(STORE);
    return raw ? (JSON.parse(raw) as Reviewer) : null;
  } catch {
    return null;
  }
})();
const listeners = new Set<() => void>();

function set(r: Reviewer | null) {
  current = r;
  try {
    if (r) sessionStorage.setItem(STORE, JSON.stringify(r));
    else sessionStorage.removeItem(STORE);
  } catch {
    /* storage unavailable: the session lasts until reload */
  }
  listeners.forEach((l) => l());
}

export async function signIn(key: string): Promise<Reviewer> {
  const who = await api.whoami(key.trim());
  if (who.scope !== "admin") throw new Error("This key is not a reviewer (admin) key.");
  const r = { key: key.trim(), name: who.name };
  set(r);
  return r;
}

export function signOut() {
  set(null);
}

export function useReviewer(): Reviewer | null {
  return useSyncExternalStore((l) => { listeners.add(l); return () => listeners.delete(l); }, () => current);
}
