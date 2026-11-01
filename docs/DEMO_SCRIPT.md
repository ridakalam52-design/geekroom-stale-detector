# Demo video script (about 2-3 minutes)

**Before recording:** Bifrost running (`./scripts/run_bifrost.sh`), `.env` loaded, a terminal and a browser tab
on `http://localhost:8080` (Bifrost dashboard / logs). Keep API keys off screen.

1. **Problem (15 s).** "LLMs repeat prices, versions, laws and job titles long after they change. This is a
   stale-answer detector for Geek Room challenge 05, built on Bifrost and Tavily."
2. **Generate a stale answer (30 s).** Run
   `staledetect ask "Who is the CEO of X (Twitter), what is the latest stable Python version, and which Node.js release is the current Active LTS?"`
   Point at the Llama 3.1 8B answer: Parag Agrawal, Python 3.9.9, Node 14.
3. **The check (30 s).** As it runs: "It extracts claims through Bifrost, runs two Tavily searches per claim
   (one for the current state, one that looks for news of a change), then a stronger model judges each claim."
   Read the three CHANGED lines and the new facts.
4. **The report (40 s).** Open `examples/run-sample/report.html`. Show one card: verdict, confidence bar,
   newest evidence date, quotes with links, and the yellow *Inference* box. "Quotes are checked against the page
   text by code; if the model invents a quote it is thrown out." Show the unverifiable Brightwave claim and
   the corrected answer at the bottom.
5. **Bifrost (30 s).** Run `staledetect check examples/sample_answer.md --max-claims 3 --simulate-outage` and
   point at "WARNING: primary model unavailable ... Bifrost fell back". Show the virtual key budget/rate limit
   in `bifrost/config.json` and the cost line in the summary.
6. **Close (10 s).** "Code, sample runs and limitations are in the GitHub repo."
