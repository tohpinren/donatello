# Reviewer blocker audit

## Decision

Rerun the reported experiments. Do not carry the paper's `64,419` retained,
`50,214` killed, or `8,925` surviving-mutant counts into a resubmission until
the new artifacts reproduce them under the policies below.

There are two independent reasons:

1. The legacy paper pipeline represented equivalence as a boolean and retained
   VeriEQL timeouts and errors as non-equivalent. The current Donatello pipeline
   records explicit outcomes and retains only proven non-equivalence (`NEQ`).
2. The full artifacts behind the paper counts are not present in this
   repository, so their provenance cannot be checked. Existing execution
   artifacts also predate explicit result-semantics provenance.

The blocker rerun must therefore start at equivalence filtering. Normalization
and deterministic mutation generation are included in the supplied audit
config to reconstruct a self-contained artifact chain. Counterexample
generation, augmentation, and model reevaluation must then be regenerated from
the corrected bag-primary survivors; they cannot safely reuse the paper's
survivor selection.

## Verified VeriEQL policy

The checked-out VeriEQL version defines these terminal states:

| VeriEQL state | Donatello outcome | Executed downstream? |
| --- | --- | --- |
| `EQU` | `proven_equivalent` | No |
| `NEQ` | `proven_non_equivalent` | Yes |
| `TMO` | `timeout` | No |
| `NSE`, `NIE` | `unsupported` | No |
| `SYN`, `UNK`, `OOM`, `OTE`, missing/malformed output | `error` | No |

The mapping is implemented in `donatello.equivalence._outcome_for_state`.
SQLite execution and counterexample selection use a positive allow-list for
`proven_non_equivalent`; they do not infer non-equivalence from failure.

A local, intentionally untracked real-tool smoke artifact checked during this
audit provides an integration check: of 500 VeriEQL outcomes, 245 were
`proven_non_equivalent`, 168 `unsupported`, 86 `timeout`, and one `error`; the
execution artifact contains exactly the 245 proven-non-equivalent mutants.

## Result equality policy

The primary mutation analysis uses order-insensitive **bag (multiset)
semantics**. Duplicate multiplicity is significant, so `SELECT DISTINCT x`
and `SELECT x` differ when the latter returns duplicates. This is why
`c_multi_dist` is meaningful in the primary analysis. Row ordering is not part
of equality, including when a query contains `ORDER BY`.

The required sensitivity analysis uses order-insensitive **set semantics**.
Duplicate multiplicity is discarded. Report at least generated, retained,
killed, survived, timeout, and error counts for bag semantics, and killed,
survived, timeout, and error counts plus the delta from bag for set semantics.

## Paper-ready clarification

> We preserve VeriEQL's terminal status for every mutant. Only mutants for
> which VeriEQL returns `NEQ` (proven non-equivalent) enter execution-based
> mutation analysis. We exclude proven-equivalent cases, timeouts, unsupported
> features, and all verification errors; these outcomes reduce coverage but
> cannot be counted as surviving mutants. In the primary analysis, execution
> results are compared as bags: row order is ignored, while duplicate
> multiplicities are preserved. We additionally report a set-semantics
> sensitivity analysis that ignores duplicate multiplicities, matching the
> concern raised about set-style benchmark evaluation.

## Unattended/coauthor run order

1. Run `configs/experiments/bird-full-blocker-audit.yaml` on the experiment
   server. It is resumable and produces the bag-primary counts first.
2. Run `scripts/prepare-set-sensitivity.sh`, then
   `configs/experiments/bird-full-set-sensitivity.yaml` for the inexpensive
   set-sensitivity execution pass.
3. After checking the two reports, rerun counterexample generation,
   augmentation, and model evaluation from the bag-primary survivors.

Suggested assignment: Junwen owns the unattended server runs and posts
`report.json`, the equivalence outcome histogram, and both execution artifacts;
Pin Ren updates the paper tables and methodology after those artifacts arrive.

The two blocker runs can be launched as one resumable unattended job with
`scripts/run-paper-blocker-audit.sh`; its standard output should be redirected
to a persistent log as shown in the README.
