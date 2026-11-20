# Decisions

## How these were measured

Ablations run on 3,000 synthetic people, three seeds per group, reported as mean ± standard
deviation (`results/ablation*.csv`, per-seed rows in `*_by_seed.csv`):

* **Dev seeds 11, 12, 13** (`cip ablate --seeds 11 12 13 --out ablation_dev.csv`): used to check
  that a conclusion is not an accident of one world.
* **Test seeds 7, 21, 22** (`cip ablate`): the numbers quoted below.

Honest caveat: the defaults were first chosen while looking at seed 7, which is also in the test
group. The dev seeds were added afterwards as a check. Every conclusion below holds on both
groups, with the same sign and a similar size, but this is a confirmation, not a blind test.

## 1. Hybrid matching, not a single model
Splink alone: precision 0.9585 ± 0.0167, linkable recall 0.5374 ± 0.0056. Its u-probabilities
are estimated by random sampling, which is contaminated by the very duplicates it is trying to
find, so shared phones look less informative than they are. Instead of tuning thresholds, a
deterministic pass links records sharing a *personal* phone or email (recall 0.9983), then
guards remove its false merges.

## 2. Guards are mandatory, and each one is measured
| Step | Precision | Linkable recall |
|---|---|---|
| Splink only | 0.9585 ± 0.0167 | 0.5374 ± 0.0056 |
| + hub filter | 0.9877 ± 0.0080 | 0.5374 ± 0.0049 |
| + deterministic pass | 0.8215 ± 0.0567 | 0.9983 ± 0.0005 |
| + shared-identifier detection | 0.9814 ± 0.0021 | 0.9373 ± 0.0029 |
| + DOB constraint | 0.9840 ± 0.0012 | 0.9372 ± 0.0029 |

The deterministic pass on its own has the worst precision and the most variance across worlds;
shared-identifier detection is what makes it usable (+16 points of precision for -6 points of
recall). Removing it from the final configuration drops F1 from 0.9600 to 0.9188.

## 3. Negative result: name constraint stays off
Refusing merges whose names conflict: precision 0.9875 (+0.35 pts), linkable recall 0.9144
(-2.28 pts), F1 0.9495 vs 0.9600. People mistype, abbreviate and reorder names, so the
constraint rejects more true links than false ones. The dev seeds agree (F1 0.9539 vs 0.9635).
Available as `name_constraint=True`.

## 4. Negative result: model cannot-link stays off
Using low Splink probability (< 0.02) as a cannot-link: precision 0.9866, recall 0.8727,
F1 0.9261 vs 0.9600. Same cause: the model's low scores on true pairs with a changed phone are
unreliable. Dev seeds agree (0.9295 vs 0.9635).

## 5. Precision-first default
A false merge shows one customer another's data (a privacy incident); a missed link costs a
duplicate marketing email. `MatchConfig.precision_first()` is the default and
`MatchConfig.balanced()` is available. Teams with the opposite cost structure should flip it.

## 6. Full recompute with stable IDs
Each day re-matches everything (simple, deterministic, no drift) and then reconciles IDs
against the registry: the oldest ID survives a merge and the others keep an alias trail; on a
split the largest part keeps the ID. Record-level ID stability stays at or above 99.90% day over
day, versus 99.39-99.67% for a naive "smallest record key" ID (see `docs/evaluation.md`). The
gain is real but modest on this data.

## 7. Privacy
Suppression keys are HMAC-SHA256 tokens (email; phone+name), so the list holds no PII. Erasure
deletes from raw, stg, resolve and gold and `verify_erasure` counts any residue. The default
analytics export excludes direct identifiers.

## 8. Lookups are POST
A phone or email in a query string ends up in access logs.

## 9. The API is public by default and fails closed
Lookups need `X-API-Key`; with `CIP_API_KEY` unset they return 503 rather than serve anyone.
The default person view is `dim_customer_public`, the same analytics-safe model the Delta export
uses. Names, phones, emails and DOB need a second key (`X-PII-Key`), and so do the details of
why `/v1/resolve` would not pick an owner: "this number is shared by a household of three" is
itself membership information. IDs stay sequential (see the architecture note).
