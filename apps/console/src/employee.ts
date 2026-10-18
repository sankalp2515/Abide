import { useSyncExternalStore } from "react";
import { api } from "./api";

// Employee session for the Delegate page: a project (or admin) key, validated with /v1/whoami. Kept in this tab's
// sessionStorage only, like the reviewer key. The public read-only key stays the default everywhere else.
export interface Employee { key: string; name: string }

const STORE = "abide.employee";
// "Remember me on this device": the key is kept in localStorage instead of this tab's sessionStorage, so the
// tasks can be reopened after the tab closes. Off for shared computers.
let current: Employee | null = (() => {
  try {
    const raw = sessionStorage.getItem(STORE) ?? localStorage.getItem(STORE);
    return raw ? (JSON.parse(raw) as Employee) : null;
  } catch {
    return null;
  }
})();
let remember = (() => { try { return localStorage.getItem(STORE) !== null || localStorage.getItem("abide.remember") === "1"; } catch { return false; } })();
export const remembering = () => remember;
export function setRemember(on: boolean) {
  remember = on;
  try {
    localStorage.setItem("abide.remember", on ? "1" : "0");
    if (on && current) localStorage.setItem(STORE, JSON.stringify(current));
    if (!on) localStorage.removeItem(STORE);
  } catch { /* storage unavailable */ }
}
const listeners = new Set<() => void>();

function set(e: Employee | null) {
  current = e;
  try {
    if (e) sessionStorage.setItem(STORE, JSON.stringify(e));
    else sessionStorage.removeItem(STORE);
    if (e && remember) localStorage.setItem(STORE, JSON.stringify(e));
    if (!e) localStorage.removeItem(STORE);
  } catch {
    /* storage unavailable: the session lasts until reload */
  }
  listeners.forEach((l) => l());
}

export async function signInEmployee(key: string): Promise<Employee> {
  const who = await api.whoami(key.trim());
  if (who.agent_key) throw new Error("That is an agent key. Sign in with your own key; agent keys are for your agents.");
  if (who.scope !== "project" && who.scope !== "admin") throw new Error("This key cannot act for an employee.");
  const e = { key: key.trim(), name: who.name };
  set(e);
  return e;
}

/** No key at hand: ask the service for a private 7-day trial key. Throws if the deployment does not offer them. */
export async function startTrial(): Promise<Employee> {
  const t = await api.trialKey();
  const e = { key: t.key, name: t.name };
  set(e);
  return e;
}

export function signOutEmployee() {
  set(null);
}

export function useEmployee(): Employee | null {
  return useSyncExternalStore((l) => { listeners.add(l); return () => listeners.delete(l); }, () => current);
}
