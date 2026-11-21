Embedding space: `BAAI/bge-m3@5617a9f61b028005a4858fdac845db406aefb181` (real); 22 cases, 94 chunks, top-5.

| Mode | recall@1 | recall@5 | MRR@5 | document recall@5 | median query (ms) |
| --- | ---: | ---: | ---: | ---: | ---: |
| dense | 0.864 | 0.909 | 0.886 | 1.000 | 3.9 |
| hybrid | 0.773 | 0.909 | 0.841 | 0.955 | 4.3 |

Per-case rank differences (dense → hybrid): 2
- fin-travel: 1 → 2
- it-password-length: 1 → 2
Lexical gate on dense top-k: drops 0/20 gold hits, keeps 28/51 other-document hits.
Lexical gate on hybrid top-k: drops 0/20 gold hits, keeps 36/54 other-document hits.

retrieval-eval: wrote /content/m3-run/out/retrieval-eval.json (inference_mode=gateway)
