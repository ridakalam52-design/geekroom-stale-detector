# Freshness Guard on a stock Bifrost v2.4.0 gateway: checks

Run on Wed 2026-11-04, about 8:50 PM IST (UTC+05:30), against `npx @maximhq/bifrost` (v2.4.0) started with this repo's `bifrost/config.json`. Freshness Guard is registered as a stdio MCP client (`mcp.client_configs`, name `freshness_guard`). No Bifrost code was changed. Responses are copied as returned.

## 1. Tool discovery through Bifrost's MCP endpoint (POST /mcp, with the virtual key)
```
["freshness_guard-check_freshness", "freshness_guard-triage_freshness"]
```
Bifrost prefixes each tool with the client name. The virtual key `stale-detector` is allowed these tools by its `mcp_configs` entry.

## 2. Same call without a virtual key -> refused
```
{"is_bifrost_error":false,"status_code":401,"error":{"message":"virtual key required to access mcp server; set one of x-bf-vk, Authorization: Bearer \u003cvk\u003e, x-api-key, or x-goog-api-key in your MCP client config"},"extra_fields":{"routing_info":{}}}
```

## 3. Server instructions forwarded by Bifrost on initialize
```
<mcp_server name="freshness_guard">
Freshness Guard checks LLM answers for facts that may be out of date (prices, API/software versions, laws, who holds a role, company facts). Call triage_freshness first: it is free and offline. Call check_freshness only when triage finds time-sensitive sentences and the answer matters; it re-checks claims live with Tavily and costs model calls and search credits. Treat 'changed' claims as wrong until corrected and 'unverifiable' as unknown, never as confirmed.
</mcp_server>
```

## 4. Free triage through Bifrost's tool-execution API (POST /v1/mcp/tool/execute?format=chat)
```
{"role":"tool","content":"1 of 2 sentences look time-sensitive; run check_freshness (suggested max_claims=1).\n- 0.90 price: GitHub Copilot costs $10 per month.","tool_call_id":"call_1"}
```

## 5. Bad arguments -> a tool error result, not a crash
```
{"jsonrpc":"2.0","id":6,"result":{"content":[{"type":"text","text":"Error: max_claims must be between 1 and 8"}],"isError":true}}
```

## 6. Bifrost's MCP tool logs (GET /api/mcp-logs), oldest first

| tool | server | virtual key | status | latency (ms) | budget / rate limit attached |
|---|---|---|---|---|---|
| triage_freshness | freshness_guard | stale-detector | success | 0 | budget-stale-detector / rl-stale-detector |
| check_freshness | freshness_guard | stale-detector | success | 25697 | budget-stale-detector / rl-stale-detector |
| triage_freshness | freshness_guard | stale-detector | success | 0 | budget-stale-detector / rl-stale-detector |
| check_freshness | freshness_guard | stale-detector | success | 0 | budget-stale-detector / rl-stale-detector |

The first two rows are the `staledetect guard` run in [`run-guard/`](run-guard/guard.md) (triage, then the live check). The rest are sections 4 and 5 above. Bifrost logs the bad-argument call as a success because the tool returned an error result rather than failing. Every call carries the virtual key's budget and rate limit.

The live check's own model requests went back through the same gateway under the same virtual key, so they count against its $0.30 budget and 300 requests/day limit (numbers in [`run-guard/guard.md`](run-guard/guard.md)).
## 7. HTTP transport instead of stdio

Same Bifrost v2.4.0, with the client registered as `connection_type: "http"` pointing at `staledetect-mcp --http 3001` (`http://127.0.0.1:3001/mcp`) instead of stdio. Bifrost's startup log:
```
[Bifrost MCP] Per-user headers verification succeeded for 'freshness_guard_http': discovered 2 tools
```
A triage call through Bifrost's `/mcp` to `freshness_guard_http-triage_freshness` with "Node.js 20 is the current Active LTS release." returned:
```
1 of 1 sentences look time-sensitive; run check_freshness (suggested max_claims=1).
- 0.95 version,recency: Node.js 20 is the current Active LTS release.
```
