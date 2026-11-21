Relevance study: 24 pairs, embeddings real.

| Kind | Pairs | Lexical keeps | Dense cosine range |
| --- | ---: | ---: | --- |
| paraphrase | 8 | 0 | 0.5298 to 0.6585 |
| trap | 8 | 8 | 0.4151 to 0.7246 |
| match | 4 | 4 | 0.7371 to 0.8012 |
| unrelated | 4 | 1 | 0.2424 to 0.3665 |

Lexical gate: drops 8 relevant (para-vacation-cairo, para-missing-computer, para-server-bill, para-late-bills, para-login-secret, para-save-holiday, para-scam-drills, para-income-quarter), keeps 9 irrelevant (trap-leave-holidays, trap-travel-budget, trap-family-days, trap-laptop-purchase, trap-board-2026, trap-mailbox-password, trap-alexandria-count, trap-contractor-names, unrelated-sales-phishing).
Best dense threshold 0.5396: 3 errors (drops ['para-save-holiday'], keeps ['trap-alexandria-count', 'trap-contractor-names']).
Highest threshold dropping no relevant pair: 0.5298 (keeps 3 irrelevant).

relevance-study: wrote /content/m3-run/out/relevance-study.json (inference_mode=gateway)
