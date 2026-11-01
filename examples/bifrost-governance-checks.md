# Bifrost governance checks against the local gateway (bifrost/config.json), 2026-11-01T22:57Z

## 1. Inference without the virtual key -> rejected
{"status_code":401,"type":"virtual_key_required","message":"virtual key is required. Provide a virtual key via the x-bf-vk header."}

## 2. A model outside the key's allow-list -> blocked before any provider is called
{"status_code":403,"type":"model_blocked","message":"Model 'openai/gpt-oss-120b' is not allowed for virtual key 'stale-detector'"}

## 3. Primary provider down -> Bifrost falls back (routing_info)
{"provider":"otari","model":"mistralai/mistral-small-3","key":"otari-key","is_fallback":true,"primary_provider":"otari-down","primary_model":"alibaba/qwen3-30b-a3b-instruct-2507","requested_provider":"otari-down","requested_model":"alibaba/qwen3-30b-a3b-instruct-2507"}

## 4. Virtual key budget and rate-limit counters (GET /api/governance/virtual-keys)
{"name":"stale-detector","budget_used":0.0017884100000000003,"budget_limit":0.3,"reset":"1M","requests_used":5,"requests_limit":300,"tokens_used":14117,"tokens_limit":400000}

## 5. Logs (GET /api/logs) - every request above is recorded with provider, model, status, tokens and cost
(see the per-run numbers in each examples/run-*/report.md, section "How this run used Bifrost and Tavily")
