# Cloudflare integration

Two independent integrations with Cloudflare. Either one is valuable on its own; together they make the operator **fully runnable on Cloudflare's free tier**.

## 1. Cloudflare Workers AI as the LLM backend

The operator's `free_llm_client.py` treats Cloudflare Workers AI as its **top-priority** LLM backend. If both of these env vars are set, Cloudflare wins the provider race:

```bash
export CLOUDFLARE_ACCOUNT_ID=<your Cloudflare account id>
export CLOUDFLARE_API_TOKEN=<token with "Workers AI: Read + Edit" scope>
```

Default model: `@cf/meta/llama-3.3-70b-instruct-fp8-fast` (change via `provider_overrides`). Calls hit Cloudflare's OpenAI-compatible endpoint:

```
https://api.cloudflare.com/client/v4/accounts/{CLOUDFLARE_ACCOUNT_ID}/ai/v1/chat/completions
```

### Free-tier ceiling

Workers AI free tier is **~10,000 neurons / day**. A full-repo review uses a handful of neurons; that's several hundred reviews / day, no credit card required.

### Creating the API token

1. <https://dash.cloudflare.com/profile/api-tokens> → **Create Token** → **Get started** (custom template).
2. Permissions: **Account** → **Workers AI** → **Read** AND **Edit**.
3. Account resources: **Include** → your account.
4. Create → copy the token into `CLOUDFLARE_API_TOKEN`.

### Wire it into GitHub Actions

In both the **gh-ai-operator** and **Portfolio** repos, add the two values as Actions secrets:

```
Settings → Secrets and variables → Actions → New repository secret
  CLOUDFLARE_ACCOUNT_ID = <your account id>
  CLOUDFLARE_API_TOKEN  = <your token>
```

Then add them to the `env:` block of `.github/workflows/ai-review-dispatch.yml` (already pass-through-ready). The heuristic review always runs; if the Cloudflare creds are present, the LLM review is included on top.

## 2. Cloudflare Worker — HTTPS front-end for the operator

`worker.js` is a tiny Worker that gives the operator a permanent HTTPS endpoint at something like:

```
https://arc-ai-operator.<your-subdomain>.workers.dev
```

Anyone (your Portfolio, VALLIS_Liquidity hub, ADMENSION, a CLI, a `curl` from the command line) can POST a review request here and the Worker will fire `repository_dispatch` on `gh-ai-operator`, which runs the review and comments the verdict back on the Portfolio issue.

### Deploy — bootstrap script (recommended)

```bash
cd cloudflare
# one-time: wrangler login opens a browser OAuth flow
npx wrangler login

# then run the bootstrap; it auto-detects the account id, asks for the
# API token + dispatch PAT, sets every secret, deploys the worker, and
# smoke-tests both GET / and POST /review.
bash bootstrap.sh
```

The script is idempotent. Run it again after rotating any secret.

### Deploy — manual

If you'd rather drive it by hand:

```bash
npx wrangler login            # browser flow
cd cloudflare

# required secret — PAT with Actions: read+write on gh-ai-operator
npx wrangler secret put GITHUB_DISPATCH_TOKEN

# optional shared secret callers must present in x-arc-token
npx wrangler secret put WORKER_SHARED_SECRET

npx wrangler deploy
```

### Call it

```bash
curl -sS -X POST https://arc-ai-operator.<sub>.workers.dev/review \
  -H "content-type: application/json" \
  -H "x-arc-token: <WORKER_SHARED_SECRET>" \
  -d '{
        "target_url":      "https://github.com/octocat/Hello-World",
        "portfolio_issue": "GareBear99/Portfolio#123",
        "review_type":     "Full repo review",
        "depth":           "standard",
        "focus":           "Check README, license, tests"
      }'
```

Returns `202` when the dispatch was accepted by GitHub. Follow the run at:

```
https://github.com/GareBear99/gh-ai-operator/actions
```

Then the verdict comment appears on the Portfolio issue.

### Inspect the service card

```bash
curl -sS https://arc-ai-operator.<sub>.workers.dev/
```

Returns a JSON description of the endpoints and accepted body shape.

### Security model

- The Worker never runs the model itself. It only calls GitHub's `repository_dispatch` with a validated JSON body.
- `WORKER_SHARED_SECRET` is strongly recommended when the Worker is publicly routable; otherwise anyone can enqueue reviews.
- Runs entirely on Cloudflare's free tier (~100k requests/day).

## Summary

| Surface | Runs on | Free tier | Role |
|---|---|---|---|
| `free_llm_client.py` → Cloudflare Workers AI | Cloudflare edge | ~10k neurons/day | LLM backend for reviews |
| `worker.js` | Cloudflare Workers | ~100k req/day | HTTPS entry point that dispatches to gh-ai-operator |
| `scout.py` / `review_target.py` | GitHub Actions (or local) | free | The review engine itself |

Combined: a code-review request comes in over HTTPS, is dispatched to GitHub Actions, runs the operator with Cloudflare Workers AI as the LLM, and posts the verdict back to the Portfolio issue — all on free tiers, all owned by you.
