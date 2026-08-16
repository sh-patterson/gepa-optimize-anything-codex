# External canary fixtures

`scripts/canary_contract.py` lets a custody-bound benchmark exercise every
optimizer through the same case runner and deterministic evaluator. Loading a
manifest verifies all artifact hashes and rejects any family that crosses the
development/validation boundary before a case can run.

The adapter surface is intentionally small:

```python
class CaseRunner(Protocol):
    def run_case(self, candidate: str, case: CaseInput) -> CaseObservation: ...

class CaseEvaluator(Protocol):
    def evaluate(
        self, candidate: str, case: CanaryCase, observation: CaseObservation
    ) -> CaseScore: ...
```

The runner owns inference or replay. The evaluator owns deterministic scoring.
Neither is built into the plugin, so a fake or retained-transcript runner can
exercise the complete contract without a provider call.

`CaseInput` excludes `expected_label`, `family_id`, and `split`. Only the
evaluator receives the full sealed case. Do not copy sealed truth into
`payload` or any runner-visible artifact.

## Manifest version 1

```json
{
  "schema_version": 1,
  "suite_id": "frozen-suite-v1",
  "positive_label": "candidate",
  "cases": [
    {
      "case_id": "case-001",
      "family_id": "creative-family-001",
      "split": "development",
      "expected_label": "candidate",
      "artifacts": [
        {
          "role": "admission_input",
          "path": "cases/case-001/admission.json",
          "sha256": "<64 lowercase hex characters>"
        },
        {
          "role": "retained_transcript",
          "path": "cases/case-001/transcript.json",
          "sha256": "<64 lowercase hex characters>"
        }
      ],
      "payload": {"source": "external-suite-owned metadata"}
    }
  ]
}
```

Paths must be relative to the manifest directory and cannot escape it. Include
custody-bound sparse-frame derivatives as additional artifacts, one path and
hash per derivative. A valid suite contains at least one development case and
one validation case, and a `family_id` may occur in only one split.

`run_suite(...)` reports the mean deterministic score, false positives, false
negatives, known provider cost, the number of cases whose provider cost is
unknown, and human-review units. Unknown cost stays unknown; it is never
silently converted to zero.

This fixture contract is compatible with `gepa`, `best_of_n`, `autoresearch`,
and `meta_harness` because candidates and evaluator scores remain engine
neutral. Engine readiness is a separate runtime gate: do not dispatch a live
runner merely because a fixture validates.
