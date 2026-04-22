/**
 * ARC AI Operator — Cloudflare Worker HTTPS front-end.
 *
 * Deploy with `wrangler deploy`. Exposes two routes:
 *
 *   GET  /           — service card (JSON) so you can curl it.
 *   POST /review     — submit a code-review request. Body JSON:
 *                      { target_url, portfolio_issue?, review_type?, depth?, focus? }
 *                      Optionally: {"token": "<SHARED_SECRET>"} (required if the
 *                      worker is configured with one).
 *                      Returns 202 after the dispatch was accepted by GitHub.
 *
 * It does NOT run the model inside the worker. It calls GitHub's
 * repository_dispatch API on GareBear99/gh-ai-operator, which kicks off the
 * existing AI pre-review workflow. That workflow runs `review_target.py`
 * (optionally via Cloudflare Workers AI as the LLM provider) and comments
 * the verdict back on the originating Portfolio issue.
 *
 * Required secrets (set with `wrangler secret put <NAME>`):
 *   GITHUB_DISPATCH_TOKEN   — PAT with Actions:read/write on gh-ai-operator.
 *   WORKER_SHARED_SECRET    — (optional) shared secret callers must include.
 *
 * Free-tier friendly: runs on Cloudflare Workers, ~100k requests/day.
 */

const OPERATOR_OWNER = 'GareBear99';
const OPERATOR_REPO  = 'gh-ai-operator';
const DISPATCH_EVENT = 'code-review-request';

const JSON_HEADERS = { 'content-type': 'application/json; charset=utf-8' };

function jsonResponse(status, body) {
  return new Response(JSON.stringify(body, null, 2), { status, headers: JSON_HEADERS });
}

function isGitHubUrl(u) {
  try {
    const url = new URL(u);
    return url.hostname === 'github.com' || url.hostname === 'gist.github.com';
  } catch (_) {
    return false;
  }
}

function isPortfolioIssueRef(s) {
  // owner/repo#NN
  return typeof s === 'string' && /^[\w.-]+\/[\w.-]+#\d+$/.test(s);
}

async function handleReview(request, env) {
  if (request.method !== 'POST') {
    return jsonResponse(405, { error: 'use POST /review with a JSON body' });
  }
  let payload;
  try {
    payload = await request.json();
  } catch (_) {
    return jsonResponse(400, { error: 'body must be valid JSON' });
  }

  // Optional shared secret
  if (env.WORKER_SHARED_SECRET) {
    const provided = payload.token || request.headers.get('x-arc-token');
    if (provided !== env.WORKER_SHARED_SECRET) {
      return jsonResponse(401, { error: 'invalid or missing x-arc-token / body.token' });
    }
  }

  const target_url = (payload.target_url || '').trim();
  if (!target_url || !isGitHubUrl(target_url)) {
    return jsonResponse(400, {
      error: 'target_url is required and must be a github.com or gist.github.com URL',
    });
  }

  const portfolio_issue = (payload.portfolio_issue || '').trim();
  if (portfolio_issue && !isPortfolioIssueRef(portfolio_issue)) {
    return jsonResponse(400, {
      error: 'portfolio_issue must look like "owner/repo#NN"',
    });
  }

  if (!env.GITHUB_DISPATCH_TOKEN) {
    return jsonResponse(500, {
      error: 'GITHUB_DISPATCH_TOKEN is not configured on the worker',
    });
  }

  const client_payload = {
    target_url,
    portfolio_issue,
    review_type: String(payload.review_type || 'standard'),
    depth:       String(payload.depth       || 'standard'),
    focus:       String(payload.focus       || ''),
  };

  const dispatchUrl =
    `https://api.github.com/repos/${OPERATOR_OWNER}/${OPERATOR_REPO}/dispatches`;
  const gh = await fetch(dispatchUrl, {
    method: 'POST',
    headers: {
      'Authorization': `token ${env.GITHUB_DISPATCH_TOKEN}`,
      'Accept':        'application/vnd.github+json',
      'Content-Type':  'application/json',
      'User-Agent':    'arc-ai-operator-worker',
    },
    body: JSON.stringify({
      event_type:     DISPATCH_EVENT,
      client_payload: client_payload,
    }),
  });

  if (gh.status === 204) {
    return jsonResponse(202, {
      ok:       true,
      accepted: true,
      message:  'review dispatched to gh-ai-operator',
      payload:  client_payload,
      follow: [
        `https://github.com/${OPERATOR_OWNER}/${OPERATOR_REPO}/actions`,
        portfolio_issue
          ? `https://github.com/${portfolio_issue.replace('#', '/issues/')}`
          : null,
      ].filter(Boolean),
    });
  }

  const errText = await gh.text().catch(() => '');
  return jsonResponse(502, {
    error:       'repository_dispatch call failed',
    github_status: gh.status,
    github_body: errText.slice(0, 400),
  });
}

function handleRoot() {
  return jsonResponse(200, {
    service: 'arc-ai-operator-worker',
    version: '0.1.0',
    operator_repo: `https://github.com/${OPERATOR_OWNER}/${OPERATOR_REPO}`,
    endpoints: {
      'GET /':       'this card',
      'POST /review': 'dispatch a code-review request (JSON body)',
    },
    body_shape: {
      target_url:      'https://github.com/<owner>/<repo>  (or a PR URL)',
      portfolio_issue: '<owner>/<repo>#<NN>   (optional)',
      review_type:     'Full repo review | Pull request review | Security | Performance | DSP | Trading | Determinism | General',
      depth:           'quick | standard | deep',
      focus:           'free-text',
    },
    auth: 'x-arc-token or body.token  (only required when WORKER_SHARED_SECRET is set)',
  });
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.pathname === '/' || url.pathname === '') return handleRoot();
    if (url.pathname === '/review') return handleReview(request, env);
    return jsonResponse(404, { error: `no route for ${url.pathname}` });
  },
};
