#!/usr/bin/env bash
# Cloudflare bootstrap for the ARC GitHub AI Operator.
#
# This script finishes the Cloudflare side of the live-deployment loop after
# you have completed the two human-only steps:
#
#   1. `npx wrangler login` in a browser (one-time OAuth).
#   2. Created a Cloudflare API token at
#      https://dash.cloudflare.com/profile/api-tokens
#      with scope "Workers AI: Read + Edit" (account-scoped).
#      Copy the token value; we'll prompt for it below.
#
# The script is idempotent. You can run it again whenever you rotate a secret.
#
# What it does:
#   A) Deploys `worker.js` to your Cloudflare Workers account (so the Portfolio,
#      VALLIS_Liquidity, or a `curl` from the command line can POST /review).
#   B) Sets `GITHUB_DISPATCH_TOKEN` as a Worker secret (required so the
#      Worker can fire repository_dispatch at gh-ai-operator).
#   C) Optional: sets `WORKER_SHARED_SECRET` on the Worker for token auth.
#   D) Sets `CLOUDFLARE_ACCOUNT_ID` + `CLOUDFLARE_API_TOKEN` as GitHub Actions
#      secrets on both gh-ai-operator and Portfolio so the dispatch workflow
#      can use Cloudflare Workers AI as the LLM backend.
#
# Prereqs in your shell:
#   - node + npx (>= 18)   (confirmed: npx is on PATH)
#   - gh CLI logged in as GareBear99
#   - the dispatch PAT (Actions: read/write on gh-ai-operator) exported as
#     $DISPATCH_PAT, OR pasted when prompted.

set -uo pipefail

# --- helpers --------------------------------------------------------------
say()  { printf '\n\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\n\033[1;33m!! \033[0m %s\n' "$*"; }
die()  { printf '\n\033[1;31mERROR:\033[0m %s\n' "$*"; exit 1; }

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

# --- 0. Prereqs -----------------------------------------------------------
command -v npx >/dev/null 2>&1 || die "npx not found. Install Node >= 18 first."
command -v gh  >/dev/null 2>&1 || die "gh CLI not found. Install https://cli.github.com."
gh auth status --hostname github.com >/dev/null 2>&1 || die "gh not logged in. Run: gh auth login"

# --- 1. Confirm wrangler login -------------------------------------------
say "Checking Cloudflare wrangler login (runs a free OAuth check)"
if ! npx -y wrangler whoami 2>&1 | tee /tmp/wrangler-whoami.out | grep -q 'account'; then
  warn "Wrangler is not logged in. Run the following in a browser-capable terminal:"
  echo "    npx wrangler login"
  echo "    # complete the OAuth consent screen, then re-run this script."
  exit 2
fi

# Try to parse the Account ID from wrangler whoami output
CF_ACCOUNT_ID="$(grep -oE '[0-9a-f]{32}' /tmp/wrangler-whoami.out | head -1 || true)"
if [ -z "${CF_ACCOUNT_ID}" ]; then
  warn "Could not auto-detect Cloudflare account id from 'wrangler whoami'."
  read -r -p "Paste your Cloudflare Account ID (32-char hex): " CF_ACCOUNT_ID
fi
[ -n "${CF_ACCOUNT_ID}" ] || die "CLOUDFLARE_ACCOUNT_ID is required."
say "Using Cloudflare account id: ${CF_ACCOUNT_ID:0:6}… (${#CF_ACCOUNT_ID} chars)"

# --- 2. Workers AI API token ---------------------------------------------
say "Cloudflare API token for Workers AI"
if [ -z "${CF_API_TOKEN:-}" ]; then
  echo "   Create one at https://dash.cloudflare.com/profile/api-tokens"
  echo "   Template → Custom → Permissions: Account > Workers AI > Read + Edit"
  echo "   Account resources: Include → your account."
  read -r -s -p "Paste the Cloudflare API token (input hidden): " CF_API_TOKEN
  echo
fi
[ -n "${CF_API_TOKEN}" ] || die "CLOUDFLARE_API_TOKEN is required."

# --- 3. Quick sanity check: hit Workers AI with the token ----------------
say "Verifying the token hits Workers AI OK"
verify_body='{"messages":[{"role":"user","content":"reply ok"}],"max_tokens":8}'
verify=$(curl -sS -o /tmp/cf-verify.json -w '%{http_code}' \
  -H "Authorization: Bearer ${CF_API_TOKEN}" \
  -H "Content-Type: application/json" \
  "https://api.cloudflare.com/client/v4/accounts/${CF_ACCOUNT_ID}/ai/v1/chat/completions" \
  -d "$(cat <<EOF
{"model":"@cf/meta/llama-3.3-70b-instruct-fp8-fast",${verify_body:1}
EOF
)")
if [ "$verify" != "200" ]; then
  warn "Workers AI probe returned HTTP ${verify}. Body:"
  head -c 400 /tmp/cf-verify.json || true; echo
  die "Token or account id is wrong, or the model isn't enabled on your plan."
fi
say "Workers AI responded 200 OK"

# --- 4. Set GitHub Actions secrets on both repos -------------------------
say "Setting Actions secrets on GareBear99/gh-ai-operator"
echo -n "${CF_ACCOUNT_ID}" | gh secret set CLOUDFLARE_ACCOUNT_ID --repo GareBear99/gh-ai-operator
echo -n "${CF_API_TOKEN}"  | gh secret set CLOUDFLARE_API_TOKEN  --repo GareBear99/gh-ai-operator

say "Setting Actions secrets on GareBear99/Portfolio (for loop visibility)"
echo -n "${CF_ACCOUNT_ID}" | gh secret set CLOUDFLARE_ACCOUNT_ID --repo GareBear99/Portfolio || true
echo -n "${CF_API_TOKEN}"  | gh secret set CLOUDFLARE_API_TOKEN  --repo GareBear99/Portfolio || true

# --- 5. Worker dispatch PAT ---------------------------------------------
say "Worker → gh-ai-operator dispatch PAT (scope: Actions read/write on gh-ai-operator)"
if [ -z "${DISPATCH_PAT:-}" ]; then
  echo "   Create one at https://github.com/settings/personal-access-tokens/new"
  echo "   Target repo: GareBear99/gh-ai-operator"
  echo "   Permissions: Actions (read and write) + Metadata (read)"
  read -r -s -p "Paste the dispatch PAT (input hidden): " DISPATCH_PAT
  echo
fi
[ -n "${DISPATCH_PAT}" ] || die "DISPATCH_PAT is required to deploy the Worker."

# --- 6. Optional shared secret ------------------------------------------
say "Optional: WORKER_SHARED_SECRET (callers must send x-arc-token)"
read -r -p "Generate a random one? [Y/n]: " GEN
GEN=${GEN:-Y}
case "$GEN" in
  [Yy]*) WORKER_SHARED_SECRET=$(openssl rand -hex 24) ;;
  *)     read -r -s -p "Paste your shared secret (blank = skip): " WORKER_SHARED_SECRET; echo ;;
esac

# --- 7. Deploy the Worker -----------------------------------------------
say "Putting Worker secrets + deploying worker.js"
echo -n "${DISPATCH_PAT}"       | npx -y wrangler secret put GITHUB_DISPATCH_TOKEN
if [ -n "${WORKER_SHARED_SECRET:-}" ]; then
  echo -n "${WORKER_SHARED_SECRET}" | npx -y wrangler secret put WORKER_SHARED_SECRET
fi
npx -y wrangler deploy | tee /tmp/wrangler-deploy.out
WORKER_URL=$(grep -Eo 'https://[^ ]+\.workers\.dev' /tmp/wrangler-deploy.out | tail -1 || true)
if [ -z "${WORKER_URL}" ]; then
  warn "Could not parse the deployed URL; check /tmp/wrangler-deploy.out"
else
  say "Worker deployed: ${WORKER_URL}"
fi

# --- 8. Smoke-test the deployed Worker ----------------------------------
if [ -n "${WORKER_URL}" ]; then
  say "Smoke-testing GET ${WORKER_URL}/"
  curl -sS "${WORKER_URL}/" | head -20 || true
  echo

  if [ -n "${WORKER_SHARED_SECRET:-}" ]; then
    say "Smoke-testing POST ${WORKER_URL}/review with octocat/Hello-World"
    curl -sS -X POST "${WORKER_URL}/review" \
      -H "content-type: application/json" \
      -H "x-arc-token: ${WORKER_SHARED_SECRET}" \
      -d '{"target_url":"https://github.com/octocat/Hello-World","review_type":"quick","depth":"quick","focus":"cloudflare bootstrap smoke test"}' \
      | head -40
    echo
  fi
fi

say "Done."
echo
echo "Next steps:"
echo "  1. Set PORTFOLIO_WRITE_TOKEN on gh-ai-operator (Issues: read+write on Portfolio)."
echo "  2. Set AI_OPERATOR_DISPATCH_TOKEN on Portfolio (Actions: read+write on gh-ai-operator)."
echo "  3. Set OPERATOR_READ_TOKEN on ARC-Neuron-LLMBuilder (Actions: read on gh-ai-operator)."
echo "  4. Close + reopen Portfolio issue #1 to replay the FreeEQ8 review under the new creds."
echo
if [ -n "${WORKER_URL}" ]; then
  echo "Worker URL: ${WORKER_URL}"
fi
[ -n "${WORKER_SHARED_SECRET:-}" ] && echo "Worker shared secret: ${WORKER_SHARED_SECRET}  (save this)"
