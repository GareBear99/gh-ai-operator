# Quality Proof Plan

This document describes how the ARC GitHub AI Operator proves that it works
end-to-end with the Portfolio (as a real AI helper in production) **and** that
the data it emits actually makes ARC-Neuron-LLMBuilder better. It follows a
professional staged-validation format: explicit phases, measurable acceptance
criteria, an adversarial threat model, and an A/B protocol designed to reject
unsubstantiated "it works" claims.

## 0. System under test

```
Portfolio (intake)
    └── ai-pre-review.yml   ──[repository_dispatch]──▶  gh-ai-operator
                                                           └── ai-review-dispatch.yml
                                                                  ├── review_target.py
                                                                  ├── training_export.py
                                                                  └── artifact: llmbuilder-training-export
                                                                        └──[gh run download]──▶  ARC-Neuron-LLMBuilder
                                                                                                      └── scripts/ingest_operator_reviews.py
                                                                                                            └── data/critique/operator_reviews.jsonl
                                                                                                                  └── Gate v2 training run
```

## 1. Threat model and failure modes

| # | Hop | Failure mode | Detected by |
|---|---|---|---|
| 1 | Portfolio dispatch | `target_url` missing / malformed | Workflow "Extract structured fields" step; issue ack still posts but dispatch step reports skip in logs. |
| 2 | Portfolio dispatch | `AI_OPERATOR_DISPATCH_TOKEN` absent | Dispatch step logs `skipping cross-repo dispatch` and exits 0. Ack comment on issue already posted, so the human is informed. |
| 3 | Operator workflow | `repository_dispatch` received but `gh` missing / token insufficient | Workflow step installs `gh` explicitly; authorization fails loudly; artifact still uploaded for audit. |
| 4 | Review itself | Target repo is private, 404, or huge | `review_target.py` clone path reports `{'error': '...'}`; verdict becomes `🔴 unable to evaluate`; training record tagged with confidence 0.2 and verdict=`unable-to-evaluate`. |
| 5 | LLM backend | Cloudflare creds wrong / missing | `free_llm_client.py` silently skips Cloudflare provider, falls through to next keyed provider, eventually to HuggingFace serverless. Heuristic review always runs regardless. |
| 6 | Verdict post-back | `PORTFOLIO_WRITE_TOKEN` absent | `review_target.py` logs `[post-failed]` and exits non-zero on the post step only; the Markdown artifact + training JSONL still upload. |
| 7 | Training export | JSONL fails validation upstream | LLMBuilder's ingest `validate()` drops invalid records; `--strict` fails CI; dedupe by id prevents poisoning the same slot twice. |
| 8 | LLMBuilder ingest | Cron workflow can't download | Ingest logs `no inputs found` and exits 0 — no commit, no pollution. |
| 9 | Ingested corpus | Record contradicts canonical seed | `data/critique/operator_reviews.jsonl` is a **separate shard** from `seed_examples.jsonl`; no auto-promotion; human curator diffs. |
| 10 | Adversarial record | Actor opens a Portfolio issue with a malicious `target_url` | URL validator restricts to `github.com` / `gist.github.com`; clone runs with `--depth 1` in a tempdir; heuristic only (no code execution inside the target repo). |

## 2. Phased acceptance criteria

Each phase has a pass/fail test. No phase passes because we think it works.

### Phase 0 — Local smoke test `PROVED`
**Claim**: operator produces a valid markdown review + a valid LLMBuilder-schema JSONL line from a real public GitHub target.

**Test**:
```bash
cd github_ai_operator && python review_target.py \
  --target https://github.com/octocat/Hello-World --depth quick
```

**Pass**: exit 0; `output/target_review/*.md` contains "🤖 ARC GitHub AI Operator — pre-review"; `output/training_export/critique/seed_examples.jsonl` has exactly one line and passes `tests/test_basic.py::test_training_record_schema_matches_llmbuilder_seed`.

**Status**: proven 2026-04-22 against `octocat/Hello-World`. Verdict was `🟡 low-signal repo (too few files to evaluate)` (expected for Hello-World).

### Phase 1 — Round-trip CI `PROVED AUTOMATICALLY`
**Claim**: the JSONL the operator produces ingests cleanly into LLMBuilder's corpus without schema changes.

**Test**: `.github/workflows/loop-integration.yml` in this repo runs on every push: reviews `octocat/Hello-World`, clones LLMBuilder, runs its ingest script against the emitted artifact, asserts `data/critique/operator_reviews.jsonl` contains ≥ 1 record with `capability=critique`, `tags` includes `arc-operator`, `confidence ∈ [0, 1]`, and `provenance.source` endswith `gh-ai-operator`.

**Pass**: CI green on every push. A breaking change on either side turns the check red.

**Status**: committed together with this doc. See the workflow file.

### Phase 2 — Portfolio dispatch live
**Claim**: a new Code-review issue on the Portfolio triggers a review in this repo within 60 s.

**Test**: manually open a Code-review issue on the Portfolio with `target_url = https://github.com/octocat/Hello-World`, then observe:
  1. The issue receives the ack comment + `ai-review-queued` label within 30 s (proves the Portfolio workflow ran).
  2. The operator's Actions tab shows an `ai-review-dispatch` run starting within 60 s (proves `repository_dispatch` fired).
  3. That run emits the `llmbuilder-training-export` artifact (proves step 4 of the DAG).

**Pass**: all three observable within the time budget.

**Blocker**: `AI_OPERATOR_DISPATCH_TOKEN` must be set on the Portfolio. See §5.

### Phase 3 — Verdict posted back on the originating issue
**Claim**: the operator's verdict comment lands on the originating Portfolio issue.

**Test**: after Phase 2, wait for the `ai-review-dispatch` run to complete; refresh the Portfolio issue.

**Pass**: the issue contains a bot comment starting with `## 🤖 ARC GitHub AI Operator — pre-review`, with a verdict line, a structural snapshot, and findings.

**Blocker**: `PORTFOLIO_WRITE_TOKEN` must be set on this repo. See §5.

### Phase 4 — LLMBuilder ingests the live run
**Claim**: the nightly ingest on LLMBuilder picks up the artifact and commits the record.

**Test**: trigger `ingest-operator-reviews.yml` on LLMBuilder manually (workflow_dispatch). Wait for the run to complete. `git pull` locally.

**Pass**: `data/critique/operator_reviews.jsonl` has ≥ 1 record; the commit message reads `Ingest gh-ai-operator reviews: N critique records`; `git log data/critique/operator_reviews.jsonl` shows a bot-authored commit.

**Blocker**: `OPERATOR_READ_TOKEN` must be set on LLMBuilder. See §5.

### Phase 5 — Proof of learning (A/B)
**Claim**: the operator-sourced corpus actually improves a candidate brain versus seed-only.

**Protocol**:
  1. Freeze a snapshot of `data/critique/seed_examples.jsonl` — call it the **baseline corpus**.
  2. Append `data/critique/operator_reviews.jsonl` (≥ 50 records ingested over time) — call it the **enriched corpus**.
  3. Train two identical candidates with identical seeds: **B** on baseline, **E** on enriched. Identical compute budget, identical arch, identical RNG seed.
  4. Score both against LLMBuilder's **165-task benchmark**, specifically the `critique` slice.
  5. Compute Δ = E_score − B_score.
  6. Pass if Δ > 0 on the critique slice AND no regression > 0.5 pp on any other slice (Gate v2 discipline).

**Pass**: the operator-enriched candidate scores strictly higher on critique and does not regress elsewhere. This is the single highest-confidence statement we can make about "the AI helper teaches the brain".

**Not yet proven**. Requires ≥ 50 ingested operator reviews first. Tracks Phases 2–4 as prerequisites.

## 3. Quality gates (Gate v2 alignment)

The operator's contribution to LLMBuilder is disciplined by the same gate doctrine that promotes other candidates:
- **Hard-reject floor** on critique-slice score (must not drop below v5 floor).
- **Per-capability regression ceiling** (max +/-0.5 pp on non-critique slices).
- **Provenance filter**: Gate v2 can slice the corpus by `provenance.source == github.com/GareBear99/gh-ai-operator` to train or ablate operator-sourced records specifically.

## 4. Red-team checks

| Vector | Mitigation |
|---|---|
| Malicious `target_url` pointing at a private repo or a non-github host | URL validator restricts to `github.com` / `gist.github.com`. Clone happens inside a `tempfile.TemporaryDirectory()`. |
| Poisoning the training corpus via many adversarial issues | Dedupe by `id`; `id` is `sha256(target_url)[:10]` so the same URL collapses. Corrections lane is confidence-capped at 1.0. Separate shard (`operator_reviews.jsonl`) is never auto-promoted into the curated seed. |
| Exfiltration of the `PORTFOLIO_WRITE_TOKEN` | Token only used by `gh issue comment`; never echoed; never written to artifact. |
| Denial of service via large targets | `max_files_scanned=250`, `max_source_chars_per_file=12000`, `max_clone_depth=1`. Hard ceilings on snapshot size. |
| Forged training records from a third party | Artifact upload is gated on the `ai-review-dispatch` workflow, which only runs on `repository_dispatch` from a trusted source (`AI_OPERATOR_DISPATCH_TOKEN` is scoped to the Portfolio). Nightly ingest downloads only from `GareBear99/gh-ai-operator`. |

## 5. Activation checklist

Phase 2–4 only run live when these secrets are set. Until then, each hop logs a skip and the CI integration test continues to cover the contract.

| Where | Secret | Scope | Purpose |
|---|---|---|---|
| Portfolio | `AI_OPERATOR_DISPATCH_TOKEN` | Fine-grained PAT, `Actions: read and write` on `GareBear99/gh-ai-operator` | Lets `ai-pre-review.yml` fire `repository_dispatch` at the operator. |
| gh-ai-operator | `PORTFOLIO_WRITE_TOKEN` | Fine-grained PAT, `Issues: read and write` on `GareBear99/Portfolio` | Lets `review_target.py` post the verdict comment back to the Portfolio issue. |
| gh-ai-operator | `CLOUDFLARE_ACCOUNT_ID` | Plain text | URL path segment for Workers AI. |
| gh-ai-operator | `CLOUDFLARE_API_TOKEN` | Token with `Workers AI: Read + Edit` scope | Lights up Workers AI as LLM backend. |
| ARC-Neuron-LLMBuilder | `OPERATOR_READ_TOKEN` | Fine-grained PAT, `Actions: read` on `GareBear99/gh-ai-operator` | Lets the nightly ingest `gh run download` the training-export artifact. |

## 6. Proof surface (what is always true on `main`)

The following facts hold on every commit to `main` on this repo:
- **19 unit tests** pass: config load, URL parser, verdict derivation, Markdown renderer, error path, Cloudflare provider registration / env gating, training-record schema match, depth→difficulty mapping, confidence clamping, correction lane, long-review truncation.
- **CI integration loop** runs the full operator → JSONL → LLMBuilder-ingest round-trip against `octocat/Hello-World` and asserts the produced record satisfies LLMBuilder's seed-examples schema. Red CI = contract broken.
- **All artifacts carry provenance** (`provenance.source`, `provenance.emitted_at`, `provenance.target_url`) for downstream filtering and audit.

## 7. What this plan does not yet prove

- **Live Portfolio → operator → verdict-on-issue** (Phase 2 + 3). Requires PATs set.
- **Live LLMBuilder nightly ingest from a real production run** (Phase 4). Requires Phase 2 + 3 first.
- **A/B proof of learning** (Phase 5). Requires ≥ 50 ingested operator records.

Until those are proven live, the system has **contract correctness** (Phases 0–1) but not yet **field evidence** (Phases 2–5). The activation checklist in §5 removes every remaining blocker.
