"""G7b cost overhead: security model spend vs agent model spend, from the router's llm_usage rows.

Tokens are exact. Cost uses list prices from config/models.yaml (free-tier usage converted at list price);
models without a listed price count as 0 and are listed in `unpriced_models` so the figure is never silent.
"""

from __future__ import annotations

from typing import Any

SECURITY_ROLES = {"judge", "tier1_remote"}
AGENT_ROLES = {"agent", "agent_eval", "agent_headline", "proxy_upstream"}


def overhead(rows: list[dict[str, Any]]) -> dict[str, Any]:
    tokens = lambda roles: sum((r.get("input_tokens") or 0) + (r.get("output_tokens") or 0) for r in rows if r.get("role") in roles)
    usd = lambda roles: sum(r.get("est_cost_usd") or 0 for r in rows if r.get("role") in roles)
    unpriced = sorted({f"{r['provider']}/{r['model']}" for r in rows if r.get("role") in SECURITY_ROLES | AGENT_ROLES
                       and not r.get("est_cost_usd") and (r.get("input_tokens") or 0) > 0})
    st, at, su, au = tokens(SECURITY_ROLES), tokens(AGENT_ROLES), usd(SECURITY_ROLES), usd(AGENT_ROLES)
    return {"security_tokens": st, "agent_tokens": at, "token_ratio": round(st / at, 3) if at else None,
            "security_usd": round(su, 6), "agent_usd": round(au, 6), "cost_ratio": round(su / au, 3) if au else None,
            "unpriced_models": unpriced}


def usage_by_role(store: Any, since: float, until: float | None = None) -> list[dict[str, Any]]:
    q = ("SELECT role, provider, model, COUNT(*) AS calls, SUM(cached) AS cached, SUM(input_tokens) AS input_tokens, "
         "SUM(output_tokens) AS output_tokens, SUM(est_cost_usd) AS est_cost_usd, AVG(latency_ms) AS avg_latency_ms, "
         "SUM(CASE WHEN status NOT IN ('ok', 'cached') THEN 1 ELSE 0 END) AS errors "
         "FROM llm_usage WHERE ts >= ?" + (" AND ts < ?" if until else "") + " GROUP BY role, provider, model ORDER BY calls DESC")
    rows = store.query(q, (since, until) if until else (since,))
    for r in rows:   # rounded here, not in SQL: ROUND(x, n) is not portable to PostgreSQL doubles
        r["est_cost_usd"] = round(r["est_cost_usd"] or 0, 6)
        r["avg_latency_ms"] = round(r["avg_latency_ms"]) if r["avg_latency_ms"] is not None else None
    return rows
