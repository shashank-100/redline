# Redline

August builds AI for contract review. Legal teams review contracts against a
playbook: an ordered list of rules, where each role contains: the firm's preferred language (starting positions), alternate language (fallback positions), and unacceptable language. Each rule corresponds 1:1 to a numbered section/clause in the corresponding document. 

You have a company's Master Services Agreement (MSA) playbook and a folder of inbound contract drafts and you'll build a service that reviews a draft against the playbook.

## Your task

`./playbook/` holds the playbook and `./contracts/` holds sample drafts. They range from a few pages to a few hundred, sections arrive in any order, terms get restated in order forms, exhibits, and amendments,
some clauses have no rule in the playbook, and some rules have no clause in the draft at all.

Your review gives every playbook rule a status, and it is always exactly one of:

- **accepted** — the draft conforms to the rule's starting position.
- **fallback** — the draft conforms to one of the rule's approved fallback
  positions. Say which.
- **not accepted** — the draft falls outside the rule's approved positions.
  Propose the changes that would make it accepted.
- **not applicable** — the rule maps to no clause in the draft.

Statuses are consistent with the draft as it stands, and a rule's status
updates to accepted if its proposed changes are made. Clauses with no rule in
the playbook take no status; do not force them under the nearest rule.

Two requirements:

- **Review the effective terms.** Exhibits, order forms, and amendments can
  change what a body clause means. Base each status on the terms as they
  stand after all of them are read.
- **Every rule must have a status, reasoning, and citations to the reviewed
  document where applicable.**

## What is provided

    ./playbook/msa_playbook.json  the playbook
    ./contracts/                  five inbound drafts of varying sizes
    ./validate.sh                 checks your deliverable against the contract below

- **AI API credentials:** a Base URL and an API key, shown on your Litmus
  assessment page, that work with any OpenAI-compatible client. Your system
  must read them from the `LITMUS_AI_BASE_URL` and `LITMUS_AI_API_KEY`
  environment variables.

Grading runs your service with a different company's playbook, 
different positions and rule order, and drafts you haven't seen. Don't assume anything specific to this playbook or these drafts.

## What you deliver

A runnable service.

- An executable `./start` at the package root that handles whatever your
  system needs to build and run. It serves HTTP on the port in `$PORT`, and
  `GET /` returns 200 once the service is ready, within **3 minutes** of launch.
- `POST /api/review` with body `{"contract": "<the full text of the draft>"}`
  returns your review within **5 minutes**, even for the largest drafts. The
  format is yours to design — JSON, markdown, anything — as long as every
  rule's status is unmistakable and cited as above.
- The playbook is read at runtime from `./playbook/`. Don't hardcode its
  rules, numbers, or order into your code.
- Reviews are deterministic: the same draft gets the same statuses on
  every run.

## Submitting

When you are finished, run:

```
litmus submit
```

## Notice

Portions of the documents in this workspace are adapted from Common Paper and
Bonterms standard agreements (CC BY 4.0), with modifications.
