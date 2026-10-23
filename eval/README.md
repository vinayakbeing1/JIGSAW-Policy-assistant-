# Evaluation

`golden_set.jsonl` holds 71 questions about the sample Jigsaw documents, written by hand with the expected
outcome for each. `run_eval.py` sends them through the same pipeline the chat uses (`answer_question`),
against the documents indexed in the database, and scores the results.

```bash
docker compose up -d && docker compose run --rm api python -m app.cli seed   # documents must be indexed first
docker compose run --rm eval                                                 # all items, 4 in parallel
docker compose run --rm eval python -m eval.run_eval --category versions --workers 1
```

Each run prints a Markdown summary and writes `reports/<timestamp>.md` and `.json` (per-item results). Runs use
the models configured in `.env`; a full run with `gpt-6-luna` is estimated at well under US$1 (the report shows the actual estimate).

## Golden set format

```json
{"id": "home-19", "category": "versions", "role": "claims_handler",
 "selection": {"product_code": "HOME", "region": "UK"},
 "question": "A pipe burst on 10 June 2025. What escape of water excess applied?",
 "expect": "answered", "facts": [["£300"]], "evidence": "Escape of water | £300", "version": "2025.1"}
```

- `role` - the user role the question is asked as (document access differs by role).
- `expect` - `answered`, `abstained` (the documents do not answer it, or the role cannot see the answer),
  `clarification` (the assistant should ask which product, region or version), or `any`.
- `facts` - every inner list must be matched by at least one of its alternatives in the answer text
  (thousand separators are ignored, so `£1,000` matches `£1000`).
- `evidence` - a phrase from the source document that answers the question.
- `version` / `region` - the wording that must be used (region defaults to `UK`).
- `no_decision` - the answer must not contain a coverage or claim decision.

Questions are answered "as of" 12 October 2026 (`--today` to change it), which fixes which versions and
endorsements are in force.

## Metrics

| Metric | Definition |
|---|---|
| Items passed | every applicable check for the item passed |
| Answerable answered | items expected `answered` that were answered |
| Fact accuracy | answered items whose answer contains all expected facts |
| Retrieval hit | the evidence phrase is in one of the top-k retrieved chunks |
| Citation hit | the evidence phrase is in a source the answer cites |
| Correct version | the answer used the expected wording version and region |
| Unanswerable abstained | items expected `abstained` where the assistant declined to answer |
| Clarification | items expected `clarification` where the assistant asked |
| No coverage decisions | answers free of "your claim will be paid" style statements |

Categories: limits and excesses, conditions, cancellation, claims procedure, coverage, exclusions,
endorsements (including the OCR-scanned M07), versions (incident dates), regions (UK vs Ireland), internal
guides and role-based access, unanswerable questions, clarification and adversarial prompts.

When the documents change, update the affected items. When an item fails, check the per-item JSON: it shows
the outcome, which checks failed and the answer text; `GET /api/admin/messages/{id}/trace` shows the same
retrieval details for questions asked in the app.
