# Decisions

Numbers below come from `results/ablation.csv` (3,000 people, seed 7). Re-run `cip ablate`.

## 1. Hybrid matching, not a single model
Splink alone: precision 0.9641, linkable recall 0.5368. Its u-probabilities are estimated by
random sampling, which is contaminated by the very duplicates it is trying to find, so shared
phones look less informative than they are. Instead of tuning thresholds, a deterministic pass
links records sharing a *personal* phone or email (recall 0.9986), then guards remove its
false merges.

## 2. Guards are mandatory, and each one is measured
| Step | Precision | Linkable recall |
|---|---|---|
| Splink only | 0.9641 | 0.5368 |
| + hub filter | 0.9903 | 0.5370 |
| + deterministic pass | 0.8818 | 0.9986 |
| + shared-identifier detection | 0.9801 | 0.9393 |
| + DOB constraint | 0.9827 | 0.9393 |

The deterministic pass on its own has the worst precision in the table (1,117 false-merge
pairs, 976 from households). Shared-identifier detection cuts that to 159.

## 3. Negative result: name constraint stays off
Refusing merges whose names conflict: precision +0.22 points (0.9849), linkable recall -2.15
points (0.9178), F1 0.9502 vs 0.9605. People mistype, abbreviate and reorder names, so the
constraint rejects more true links than false ones. Available as `name_constraint=True`.

## 4. Negative result: model cannot-link stays off
Using low Splink probability (< 0.02) as a cannot-link: precision 0.9870, recall 0.8707, F1
0.9252. Same cause: the model's low scores on true pairs with a changed phone are unreliable.

## 5. Precision-first default
A false merge shows one customer another's data (a privacy incident); a missed link costs a
duplicate marketing email. `MatchConfig.precision_first()` is the default and
`MatchConfig.balanced()` is available.

## 6. Full recompute with stable IDs
Each day re-matches everything (simple, deterministic, no drift) and then reconciles IDs
against the registry: the oldest ID survives a merge and the others keep an alias trail; on a
split the largest part keeps the ID. Record-level ID stability stays above 99.9% day over day,
versus a naive "smallest record key" ID (see `docs/evaluation.md`).

## 7. Privacy
Suppression keys are HMAC-SHA256 tokens (email; phone+name), so the list holds no PII. Erasure
deletes from raw, stg, resolve and gold and `verify_erasure` counts any residue. The default
analytics export excludes direct identifiers.

## 8. Lookups are POST
A phone or email in a query string ends up in access logs.
