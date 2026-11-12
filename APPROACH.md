# Redline: approach

Redline is a service that reviews an inbound MSA draft against the playbook in `./playbook/`. It gives every rule exactly one status:

- `accepted`
- `fallback` (it says which one)
- `not accepted` (it proposes changes)
- `not applicable`

Every status comes with reasoning and citations to the draft.

## Running it

```
PORT=8641 ./start
curl localhost:8641/                         # 200 when ready
curl -X POST localhost:8641/api/review \
  -H 'Content-Type: application/json' \
  -d '{"contract": "<full draft text>"}'     # JSON review
# add ?format=md for a Markdown rendering of the same review
```

The service reads `LITMUS_AI_BASE_URL` and `LITMUS_AI_API_KEY` from the environment, and `./start` also loads them from `./.env` if that file exists. The code is Python 3 standard library only, so there's nothing to install, no dependencies to go missing in a clean sandbox, and no limit on request body size.

## Pipeline

```
draft ─► segment ─► Stage A: locate (parallel windows) ─► Stage B: decide (one call per rule, parallel) ─► validate ─► review
```

### 1. Load the playbook (on every request)
The service takes the first JSON file in `./playbook/`. It finds the list of rules whatever it's called, and handles rules given as a list or as a dict. Field names are matched loosely: `starting_position` or `preferred_position`, `fallbacks` or `fallback_positions`, `heading` or `title`. Every field of every rule goes to the model as written. Rule order and numbering come from the file. Nothing about the playbook is hardcoded.

### 2. Segment the draft
The draft is split into sections at lines that look like headings: numbered clauses (`14.`), lettered sub-clauses (`OF-6.`, `E-5.`), and document markers (`EXHIBIT`, `ORDER FORM`, `AMENDMENT`, `SCHEDULE`, `SIDE LETTER`, `CHANGE ORDER`, …). Each section keeps an id, its line range, its heading, and the document it sits in, for example `EXHIBIT A > ORDER FORM No. 1 > OF-6. Renewal`. A document's subtitle becomes part of that label, so a header like `PRIOR AGREEMENT (SUPERSEDED — ATTACHED FOR REFERENCE)` travels with every clause under it. Very long sections are split further.

### 3. Stage A: find the relevant sections
The draft is packed into windows of about 60 KB, and the windows are sent to the model in parallel, 8 at a time. For each rule, the model lists every section that states, restates, modifies or overrides that rule's subject, wherever it appears: body, order form, exhibit or amendment. A whole-word match on rule headings against section headings runs alongside as a backup, in case a window fails.

### 4. Stage B: decide each rule on its effective terms
All rules run in parallel, one call each. Each call gets:
- the rule, plus the playbook's preamble and scope
- the sections found for it, in document order
- the opening section of each document those sections sit in, so markers like "superseded" or "for reference only" are always visible
- the full text of any amendments, side letters or change orders
- the draft's precedence and override clauses ("notwithstanding", "supersedes", "in the event of conflict", "amended", …), strongest first

The model then works out the effective term and checks it, in order:
1. against the starting position, which gives **accepted**
2. against each fallback, in order, which gives **fallback**, naming the one that matched
3. against the not-acceptable language and anything else, which gives **not accepted**, with replacement language that would make the rule accepted. The replacement includes deleting or amending any order-form or amendment term that overrides the clause.

If no operative clause addresses the rule at all, it is **not applicable**.

The decision rules come from the README and the playbook:
- An override wins. An amendment or a "notwithstanding" order-form term replaces the body clause.
- Material the draft marks as superseded, prior, unexecuted or reference-only is not effective. The draft under review is itself operative.
- A clause with no matching rule takes no status, and a rule with no matching clause is not applicable.

### 5. Validate the output
- Statuses are mapped to the four exact values.
- A fallback must refer to an approved fallback that actually exists.
- Every citation is checked against the section it names, ignoring differences in quote marks, dashes and whitespace. Each citation shows the section label, line range, quote and `quote_verified`.
- A `not accepted` result always comes with proposed changes.

## Output shape

```json
{
  "playbook": "Corvessa — MSA Standards Playbook",
  "model": "anthropic/claude-sonnet-5.5",
  "summary": {"accepted": 6, "fallback": 2, "not accepted": 2, "not applicable": 1},
  "results": [
    {
      "rule": 3, "heading": "Fees and Payment", "status": "not accepted",
      "fallback_used": null, "fallback_index": null,
      "effective_term": "Amendment No. 1 replaces §14: payment 90 days after acceptance, no late interest.",
      "reasoning": "…",
      "citations": [{"section": "AMENDMENT No. 1 TO MASTER SERVICES AGREEMENT > 1. …",
                     "lines": "3071-3074", "quote": "…within 90 days of Customer's acceptance…",
                     "quote_verified": true}],
      "proposed_changes": "Delete Amendment No. 1 §1 and restore §14: …"
    }
  ]
}
```

## Determinism
- Every model call uses `temperature=0`, `top_p=1` and a fixed `seed`, asks for JSON output, and sends its inputs in a fixed order.
- Each finished review is cached in memory and on disk (`./cache/`). The cache key is a hash of the code version, the model, the playbook and the contract, so the same draft always gets the identical review.
- A review is only cached once every call in it has succeeded, so a temporary error is never frozen into the cache.

## Time and failure handling
- The whole request has a 270 s budget, against the 300 s limit.
- Each call has a timeout, and rate-limit and server errors (429 and 5xx) are retried with backoff.
- If a rule's call still fails, the rule is still reported, with a conservative status based on the sections found for it.
- Every rule always appears in the output.

## Model choice
The service lists the gateway's models when it first needs one and picks the first available from `anthropic/claude-sonnet-5.5`, `anthropic/claude-sonnet-5`, `openai/gpt-4.1`, `openai/gpt-4o`. Model names are matched loosely, and embedding, image and tiny models are never chosen. `LITMUS_AI_MODEL` overrides the choice.

## Validation on the sample drafts

| Draft | Size | Time | Notable effective-term findings |
|---|---|---|---|
| Aldermere | 8 KB | ~9 s | English governing law, so not accepted. Audit and liability are fallback 1. |
| Pinnfold | 13 KB | ~9 s | Renewal only by mutual writing, so fallback 2. No audit or liability clause, so not applicable. |
| Vantorre | 15 KB | ~8 s | A pricing-commitment clause exists, so not accepted. |
| Ellsworth | 85 KB | ~16 s | Order form clause OF-6 ("Notwithstanding…") gives the customer a unilateral renewal at locked pricing, so renewal is not accepted. |
| Drummond | 333 KB | ~25 s | Amendment No. 1 replaces fees with 90 days after acceptance, so not accepted. The superseded Exhibit BH is ignored: audit is accepted, termination is fallback 1, and non-solicit is not applicable. |

`./validate.sh` passes on a clean copy of the repo.

## Known limits
- AI providers do not guarantee bit-identical output even at temperature 0. The cache makes repeat reviews identical, and a review run cold from scratch is very likely, but not guaranteed, to match.
- Segmentation relies on heading patterns. A draft with no recognisable headings still gets reviewed, but its citations are coarser.
- A trap unlike the ones covered here (overrides, precedence clauses, superseded or reference material) could be missed.
