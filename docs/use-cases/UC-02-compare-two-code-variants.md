# Benchmark Use Case Template

## UC-02: Check that a candidate is never much worse than a baseline, across environments

| | |
|---|---|
| **Status** | draft |
| **Owner** | MarcoGorelli |
| **Last updated** | 2026-09-02 |
| **Related** | UC-01. Distinct from "compare two revisions" (rok/benchx#17) — see §2. |

### 1. Summary

A contributor wants to check that a candidate implementation of an operation is never
much worse (slower) than a baseline implementation. The candidate may be faster than the
baseline by any amount — that's fine, even desirable — but it must not be slower than the
baseline by more than a small, stated margin (e.g. no more than 5% slower). This is a
one-sided check, not a symmetric "are they close" comparison. It should hold irrespective
of how everything else (deps, arch, machine, build) happens to vary — not just in one
environment picked at random.

### 2. Motivation

Reviewers routinely want to know that a candidate implementation (a proposed change, a
rewrite, a new algorithm) doesn't regress performance relative to a baseline — it's fine,
even desirable, for the candidate to be much faster; what matters is ruling out the
candidate being meaningfully slower. Doing this by hand today means running each variant
separately, at different times, possibly on different machines, which introduces
confounds unrelated to the code itself (machine load, thermal state, dependency drift
between runs). A discussion on an earlier PR
(https://github.com/rok/benchx/pull/9#discussion_r3822100028) raised exactly this: if the
two things being compared aren't run together, on the same machine, a single comparison
isn't apples-to-apples.

This is not the same as "compare two revisions" (the regression-tracking use case from
issue rok/benchx#17): there, the same implementation is compared against itself at a
different point in history — code identity is controlled, code version varies. Here,
code version is controlled (both variants are evaluated together, at the same point in
time) and code identity varies (it's two different implementations, not one
implementation over time).

The added requirement here is that the "candidate is never much worse than baseline"
claim should be checked across a swept set of environments (different BLAS vendors,
different architectures, different machines), not asserted from a single one. A
single-environment comparison can't distinguish "this holds generally" from "this
happens to hold on my laptop" — the whole point is to rule out the latter.

### 3. Actors and trigger

- **Actor:** contributor or reviewer checking a candidate implementation against a
  baseline
- **Trigger:** a manually typed command, during development or PR review; possibly a
  CI matrix run across several supported environments
- **Frequency:** on demand, run once per environment in the swept set

### 4. Workflow sketch

```
$ benchx run --variant candidate=new_impl.py --variant baseline=old_impl.py bench_sort
<runs both variants, interleaved, on the current machine>

variant     benchmark   time      vs. baseline
baseline    bench_sort  1.20s     -
candidate   bench_sort  1.24s     +3.3%   (within a 5% margin — OK)
```

Had the candidate come in at 1.30s (+8.3%), that would violate a 5% margin — the finding
would be a failure for this environment, not a value to average away.

The same command is re-run once per environment in the swept set (different machine,
BLAS vendor, architecture, …). Within each run, environment is fixed and the two
variants are compared paired — that's what makes any one run's number trustworthy. The
loop closes by checking the invariant across the whole set: "candidate stayed within
margin of baseline in every environment tested," or a list of which environments it
didn't.

### 5. Study design

| Coordinate | Role | Notes |
|---|---|---|
| Code identity | **varying** | the two variants under test — a baseline and a candidate — everything else about the code (input data, parametrization) held equal |
| Code version | controlled | both variants evaluated together, e.g. from the same working tree/commit, so neither has drifted independently |
| Environment | controlled *within* a run, **varying** across the study | fixed for any single paired comparison (needed for that comparison to be valid at all); deliberately swept across runs (machine, dependencies, architecture, build) so the "candidate never much worse than baseline" claim is tested against, not merely inside, environment variation |
| Execution context | controlled | interleaved within one session, same machine — this is the "run together, not separately" requirement from the PR-9 discussion |

This is a nested design: code identity varies *within* each run (that's the comparison),
and environment varies *across* runs (that's the robustness check). Both are load-bearing
— dropping the inner pairing makes any single run invalid; dropping the outer sweep
makes the "never much worse" claim untested, just asserted from one data point.

### 6. Measurements

| Measure | Instrument | Unit | Direction |
|---|---|---|---|
| wall time | `timeit`/`pyperf`-style | s | lower is better |
| peak memory (optional) | e.g. `memray` | bytes | lower is better |

- **Raw samples retained?** yes — enough to compute a ratio with a stated noise floor,
  not just a single-shot measurement per variant
- **Is the instrument available in every environment this use case spans?** needs to
  hold within any one comparison; across the swept set, an environment where it isn't
  available should show up as a gap in the sweep (§10), not be silently skipped

### 7. Comparison semantics

- **What comparison is valid?** paired within each environment — each variant runs the
  same benchmark, in the same session, so the inner pairing is (benchmark, session).
  Across environments, the comparison is unpaired/independent trials of the same claim.
- **Estimator:** median (or trimmed mean) of interleaved samples per environment
- **What counts as within margin, per environment?** a one-sided threshold on
  `candidate / baseline - 1`: any negative or small-positive value is fine (candidate
  equal to or faster than baseline is never a problem), and it must not exceed a stated
  margin (e.g. 5%). There is no corresponding check in the other direction — baseline
  being much slower than candidate is the desired outcome, not a finding.
- **What counts as the invariant holding, across environments?** candidate stays within
  margin of baseline (or beats it) in every environment tested (or some stated minimum
  fraction, if a small number of outliers is tolerated). A single environment where it
  doesn't hold is itself the finding, not noise to be averaged away.
- **Expected noise floor:** run-to-run noise is expected within an environment; the
  interleaved same-machine design in §5 exists to keep that noise from being confused
  with a real per-environment margin violation. Because the margin can be small (e.g.
  5%), it must be checked against the measured noise floor — a margin smaller than the
  noise floor makes the check meaningless, since no single run can distinguish a real
  3% regression from 3% run-to-run noise.

### 8. Schema implications

- **Profile:** new, `candidate-baseline-comparison`
- **Fields required beyond the core:**
  - a `role` label (`baseline` / `candidate`) distinguishing the two sides of the
    comparison, as part of code identity — this replaces a generic, unordered `variant`
    label, since which side is which is load-bearing here (§7)
  - a `comparison_id` grouping every run of this same candidate-vs-baseline claim across
    environments, independent of which environment each individual run used
- **Fields required to be absent or explicitly null:** n/a
- **New fields not currently in the schema:**
  - `role: enum{baseline, candidate}` — which side of the comparison a record
    represents; populated by the runner from the CLI invocation; required, no sensible
    default; exactly one `baseline` expected per inner comparison (see open question 3
    on whether multiple candidates against one baseline should be supported)
  - `comparison_id: str` — identifies the candidate-vs-baseline claim being tested,
    shared across all environments it's checked in; populated by the caller (e.g. from
    the PR or the pair of code refs being compared), stable across separate `benchx run`
    invocations
  - the margin itself (e.g. "5%") is **not** a record field — it's a parameter to the
    analysis step that evaluates the invariant, not a property of a single measurement
- **Comparability key (inner, per environment):** same `session_id` + same benchmark id
  + same environment; records being compared must share everything except `role`
- **Comparability key (outer, across the study):** same `comparison_id`; records being
  aggregated must share everything except environment (and the `session_id`/`env_hash`
  that identifies it)
- **Validation invariants:** all records in an inner comparison share `session_id` and
  `env_hash`; exactly one record has `role = baseline`; all records aggregated under one
  `comparison_id` share the same benchmark id
- **Conflicts with existing use cases:** UC-01 treats environment as merely "controlled"
  with no cross-record comparability requirement; this use case requires environment to
  be *identical* within an inner comparison (stricter than UC-01) but *deliberately
  distinct* across records sharing a `comparison_id` (the outer aggregation) — both
  requirements apply at once, at different grouping levels

### 9. Storage and lifecycle

- **Volume:** one record per role per benchmark per session; sessions per
  `comparison_id` equal to the number of environments swept — small per environment,
  but the study as a whole spans as many sessions as environments tested
- **Retention:** needs to outlive a single session — the invariant check requires
  collecting results from every environment in the sweep before it can be evaluated, so
  at minimum retention must span the whole sweep (which may run on different machines,
  possibly at different times)
- **Location:** shared store — results from different machines/environments need to be
  collected in one place to check the invariant across them, unlike a single-environment
  comparison which could stay purely local
- **Does this data ever need to join against data from another use case?** yes, within
  this one: every session sharing a `comparison_id` must be joined to evaluate whether
  the invariant held. This is a join across sessions of the *same* use case (not a
  different one), but it's a real cross-session requirement this use case did not have
  when scoped to a single environment.

### 10. Degenerate and failure modes

- If baseline and candidate are *not* actually run together (someone runs them
  separately, at different times or on different machines), the comparison silently
  becomes invalid — exactly what the PR-9 discussion flagged. The schema should make
  `session_id` mandatory and equal across compared records so this is detectable, not
  just conventionally avoided.
- If the baseline fails to run at all, no comparison is possible for that environment —
  the schema should record that explicitly rather than have a missing baseline read as
  "no regression." If the candidate fails to run (crashes, errors) while the baseline
  succeeds, that arguably *is* "much worse than baseline" and should be treated as a
  margin violation, not silently dropped as missing data (see open question 4).
- If the sweep is incomplete (one environment's session never ran, or failed), the
  invariant check should report "not evaluated in N of M environments," not silently
  treat the missing environments as passing.
- Worst wrong conclusion, inner: because the metric being thresholded is a ratio of two
  noisy quantities and the margin can be small, noise cuts both ways — an unusually slow
  baseline run can make a fine candidate look like it violated the margin (false
  failure), and an unusually fast baseline run can mask a real regression in the
  candidate (false pass). Both require the noise floor to be characterized and compared
  against the margin, not assumed away.
- Worst wrong conclusion, outer: declaring "candidate never much worse than baseline"
  from a sweep that only covered similar environments (e.g. three machines with the same
  BLAS vendor) — the invariant claim is only as strong as the diversity of the swept
  set, and that diversity isn't visible from the schema alone unless environment fields
  are recorded in enough detail to check it.

### 11. Non-goals

- Comparing the same implementation against itself at different points in history —
  that's "compare two revisions" from issue rok/benchx#17 (code identity controlled,
  code version varying); see §2 for the distinction.
- A symmetric two-sided check (also flagging when baseline is much worse than
  candidate) — this use case is one-directional by design; a much-faster candidate is
  never a finding.
- Studying *how* the environment affects performance, or which environment factor is
  responsible for a margin violation — this use case only checks whether the invariant
  holds, not why it fails when it does.
- Choosing which environments belong in the swept set, or how many are "enough" — left
  to the caller; see open question 1.
- Choosing the margin value itself, or what fraction of environments must pass — this
  use case only covers checking against a caller-supplied margin (5% is an illustrative
  example, not a default this use case mandates); see open question 2.

### 12. Open questions

1. How is the swept set of environments chosen, and does the schema need to record
   *intended* coverage (e.g. "these 5 environments are required") separately from which
   environments actually reported a result?
2. Should the margin be a fixed percentage supplied per comparison, per benchmark, or
   derived automatically from the measured noise floor (e.g. reject a margin smaller
   than the noise, per §7) rather than trusting the caller to pick a sensible one?
3. Is one candidate against one baseline the common case, or does the schema need to
   support multiple candidates checked against a single baseline in one comparison?
4. Does a candidate that fails outright (crash, error, timeout) count automatically as a
   margin violation, or as its own separate failure category outside the margin check?
