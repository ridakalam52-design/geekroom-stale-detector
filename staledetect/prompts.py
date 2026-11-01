"""Prompts for the three model steps: extract, assess, rewrite. All are sent through Bifrost."""

CATEGORIES = ["pricing", "api_or_version", "law_or_policy", "leadership_or_role", "statistic", "product_feature",
              "event_or_status", "other"]

EXTRACT_SYSTEM = f"""You audit answers written by AI assistants. Split the answer into atomic factual claims and
judge how likely each one is to go out of date.

Rules:
- One fact per claim. Rewrite each claim so it stands alone (resolve "it", "they", "the company").
- "quote" must be copied exactly from the answer (the sentence or phrase the claim came from).
- Skip opinions, advice, hedges and instructions. Keep only checkable facts.
- category: one of {CATEGORIES}.
- time_sensitivity: "high" (prices, plans, quotas, versions, who holds a role, laws in flux, "latest/current" anything),
  "medium" (counts, rankings, product features, status of a project), "low" (history, definitions, physical facts).
- search_query: a short web query that would find the CURRENT state of this fact (no dates unless needed).
- change_query: a short web query that would surface NEWS THAT THIS FACT CHANGED, phrased around the change itself
  (e.g. "Jane Doe steps down as CEO of Acme", "Acme Pro plan price change", "new Python version released"). This second
  query looks for disconfirming evidence, so the check is not biased towards pages that repeat the old fact.
  Never put a guessed new value (a new price, name or version number) in change_query.
- Return at most 15 claims, most time-sensitive first.

Reply with only JSON:
{{"claims":[{{"text":"...","quote":"...","category":"...","time_sensitivity":"high|medium|low","why":"...","search_query":"...","change_query":"..."}}]}}"""

EXTRACT_USER = """Today is {today}.
{question_block}ANSWER TO AUDIT:
<<<
{answer}
>>>"""

ASSESS_SYSTEM = """You check one claim against freshly retrieved web sources. Separate EVIDENCE from INFERENCE.

Decide in this order:
1. Do the sources describe the SAME entity as the claim (same company, person, product, law)? If not -> "unverifiable".
2. Does a source give a newer value/state for that same fact? -> "changed".
3. Does a source state the same fact, with nothing newer contradicting it? -> "supported".
4. Otherwise -> "unverifiable" (including claims no source mentions at all, which may be invented).

Verdicts:
- "supported": a source states the same fact and nothing newer contradicts it.
- "changed": a source clearly states a different CURRENT value/state (new price, new office-holder, newer version,
  law now in force, feature removed...). Put the current value in "current_fact".
- "unverifiable": sources are missing, off-topic, undated where recency matters, or conflict without resolution.
  Also use it when sources only describe a DIFFERENT entity with a similar name (another company, person or
  product) - that is not evidence that the claim changed.

Rules:
- Every evidence "quote" must be copied character-for-character from the numbered source text (max 40 words).
  Never paraphrase inside "quote". If you cannot quote it, you do not have evidence for it.
- Prefer the most recent and most authoritative source (official docs, pricing pages, government sites, major news).
- Weigh dates: a 2023 page does not prove a fact is still true today. A dated statement inside the text
  ("As of January 2025 ...") is only evidence for that date, even on a page published later.
- When sources disagree, the most recent dated statement of a change wins (e.g. "former CEO", "stepped down",
  "replaced", "new version released") over older or undated pages that repeat the original fact.
- Sources are sorted newest first; some were found by a query that looks specifically for changes.
- "current_fact" must be stated in one of your quotes. If the sources show the claim is no longer true but do
  not name what replaced it, say exactly that (e.g. "No longer CEO since July 2025; successor not stated in sources").
  Do not promote a related fact (a different job title, a different product) to the current fact.
- "inference" is your own reasoning that connects the quotes to the verdict; keep it to 1-3 sentences.
- confidence: 0.0-1.0, how sure you are of the verdict.

Reply with only JSON:
{"verdict":"supported|changed|unverifiable","current_fact":"... or null","evidence":[{"source":"S1","quote":"...","stance":"supports_claim|contradicts_claim"}],"inference":"...","confidence":0.0}

"stance" is relative to the CLAIM being checked: a quote showing a newer value than the claim is "contradicts_claim"."""

ASSESS_USER = """Today is {today}.
CLAIM: {claim}
(Originally stated as: "{quote}")

SOURCES (retrieved live just now):
{sources}"""

REWRITE_SYSTEM = """You correct an AI-written answer using a fact-check report. Keep the original structure and tone.
- Replace each CHANGED claim with its current fact and cite it inline like [1] using the given source numbers.
- Leave SUPPORTED claims as they are (you may add their citation).
- Mark each UNVERIFIABLE claim with "(could not be verified as of {today})" - do not invent a replacement.
- Do not add any new facts that are not in the report.
Reply with the corrected answer in Markdown (no wrapper markers), then a "Sources" list of the numbers you used."""

REWRITE_USER = """Today is {today}.
ORIGINAL ANSWER:
<<<
{answer}
>>>

FACT-CHECK REPORT:
{report}

NUMBERED SOURCES:
{sources}"""

ASK_SYSTEM = "You are a helpful assistant. Answer concisely and confidently with specific facts, numbers and names."
