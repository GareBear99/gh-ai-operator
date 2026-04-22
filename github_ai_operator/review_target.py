#!/usr/bin/env python3
"""Review a single GitHub target (repo, pull-request, or gist) and produce a
Markdown pre-review.

Designed to be the entrypoint the Portfolio's code-review dispatch workflow
calls. Reads:

    TARGET_URL        — the URL to review (required)
    PORTFOLIO_ISSUE   — optional `owner/repo#NN` to post the review back to
    REVIEW_TYPE       — free-text tag ("full", "pr", "security", …)
    REVIEW_DEPTH      — "quick" | "standard" | "deep"
    FOCUS             — free-text: what the requester cares about
    GITHUB_TOKEN      — token used for gh-api + posting

Writes `output/target_review/<slug>.md` locally.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from github_ai_operator.config import Limits  # noqa: E402
from github_ai_operator.review import (
    clone_repo, collect_snapshot, heuristic_findings, safe_delete,
)
from github_ai_operator import review_rules  # noqa: E402
import training_export  # noqa: E402


REPO_URL_RE = re.compile(
    r'^https?://github\.com/(?P<owner>[^/]+)/(?P<repo>[^/#?]+)'
    r'(?:/pull/(?P<pr>\d+))?'
    r'(?:/tree/(?P<ref>[^/?#]+))?'
    r'/?$'
)


def parse_target(url: str) -> dict:
    """Parse a github.com URL into owner/repo/pr/ref. Gists fall back to raw."""
    if url.startswith('https://gist.github.com/'):
        return {'kind': 'gist', 'url': url}
    m = REPO_URL_RE.match(url.strip())
    if not m:
        raise ValueError(f'Not a supported github.com URL: {url!r}')
    d = m.groupdict()
    kind = 'pr' if d.get('pr') else 'repo'
    return {
        'kind': kind,
        'owner': d['owner'],
        'repo': (d['repo'][:-4] if d['repo'].endswith('.git') else d['repo']),
        'pr': int(d['pr']) if d.get('pr') else None,
        'ref': d.get('ref'),
        'url': url,
    }


def review_repo_snapshot(owner: str, repo: str, ref: str | None) -> dict:
    limits = Limits()
    with tempfile.TemporaryDirectory() as tmpdir:
        dest = Path(tmpdir) / 'repo'
        clone_url = f'https://github.com/{owner}/{repo}.git'
        ok = clone_repo(clone_url, dest, depth=limits.max_clone_depth)
        if not ok:
            return {'error': f'clone failed for {clone_url}'}
        if ref:
            subprocess.run(['git', '-C', str(dest), 'checkout', '--quiet', ref],
                           capture_output=True, text=True, check=False)
        snapshot = collect_snapshot(dest, limits)
        base = heuristic_findings(dest, snapshot)
        extra, langs = review_rules.run_all(dest, snapshot)
        return {
            'snapshot':  snapshot,
            'findings':  base + extra,
            'languages': langs,
            'rule_packs_fired': {
                'python':  any(f.startswith('[python]')  for f in extra),
                'juce':    any(f.startswith('[juce]')    for f in extra),
                'node':    any(f.startswith('[node]')    for f in extra),
                'finance': any(f.startswith('[finance]') for f in extra),
            },
        }


def render_markdown(target: dict, review: dict, context: dict) -> str:
    lines: list[str] = []
    now = datetime.now(timezone.utc).isoformat(timespec='seconds')
    lines.append('## 🤖 ARC GitHub AI Operator — pre-review')
    lines.append('')
    lines.append(f'**Target**: <{target["url"]}>  ')
    lines.append(f'**Kind**: `{target["kind"]}`  ')
    if context.get('review_type'):
        lines.append(f'**Type**: {context["review_type"]}  ')
    if context.get('depth'):
        lines.append(f'**Depth**: {context["depth"]}  ')
    lines.append(f'**Generated**: {now}')
    lines.append('')
    if context.get('focus'):
        lines.append('### Requester focus')
        lines.append(context['focus'])
        lines.append('')

    if review.get('error'):
        lines.append('### ⚠️ Could not fetch the target')
        lines.append(f'`{review["error"]}`')
        lines.append('')
        lines.append('If the target is private or non-GitHub, email `gdoman99@gmail.com`.')
        verdict = '🔴 unable to evaluate'
    else:
        snap = review['snapshot']
        findings = review['findings']
        verdict = _derive_verdict(findings, snap)
        lines.append(f'### Verdict: {verdict}')
        lines.append('')
        lines.append('### Structural snapshot')
        lines.append(f'- File count (filtered): **{snap.get("file_count", 0)}**')
        lines.append(f'- Total size (bytes): **{snap.get("total_size_bytes", 0):,}**')
        top_ext = snap.get('top_extensions', {})
        if top_ext:
            line = ', '.join(f'`{k}`×{v}' for k, v in list(top_ext.items())[:8])
            lines.append(f'- Top extensions: {line}')
        root = snap.get('root_files', [])
        if root:
            lines.append(f'- Root files: {", ".join(f"`{x}`" for x in root[:12])}')
        root_dirs = snap.get('root_dirs', [])
        if root_dirs:
            lines.append(f'- Root dirs: {", ".join(f"`{x}`" for x in root_dirs[:12])}')
        lines.append('')

        lines.append('### Heuristic findings')
        if findings:
            for f in findings:
                lines.append(f'- {f}')
        else:
            lines.append('- No surface-level issues detected by the heuristic pass.')
        lines.append('')

        # Rule-pack activation (transparency about which detectors ran)
        packs = review.get('rule_packs_fired', {}) or {}
        langs = review.get('languages', []) or []
        activated = [k for k, v in packs.items() if v]
        lines.append('### Rule packs run')
        lines.append(f'- Languages detected: {", ".join(f"`{l}`" for l in langs) if langs else "(none)"}')
        lines.append(f'- Packs that produced findings: {", ".join(f"`{p}`" for p in activated) if activated else "(none fired)"}')
        lines.append('')

        # Honest 'Thoughts' section — explicit about what the heuristic alone can't do.
        lines.append('### Thoughts (honest confidence)')
        thoughts = _build_thoughts(snap, findings, packs, context.get("depth", ""))
        for t in thoughts:
            lines.append(f'- {t}')
        lines.append('')

    lines.append('---')
    lines.append('')
    lines.append(
        '_This is an **automated pre-review** by the [ARC GitHub AI Operator]'
        '(https://github.com/GareBear99/gh-ai-operator). Gary may reply with '
        'additional notes depending on the verdict, or you can open a '
        '[Follow-up issue](https://github.com/GareBear99/Portfolio/issues/new?template=follow-up.yml) '
        'if you want a deeper human round._'
    )
    return '\n'.join(lines) + '\n'


def _build_thoughts(snap: dict, findings: list, packs: dict, depth: str) -> list:
    """Explicit honesty layer. The operator states what this pass can and
    cannot tell you, so readers do not over-weight a heuristic review."""
    out: list = []
    n = len(findings)
    fc = snap.get('file_count', 0)
    # Ceiling honesty
    any_ai = False  # heuristic-only for now; AI-backed review will flip this
    if any_ai:
        out.append('LLM-layered review ran on top of the heuristic pass.')
    else:
        out.append('This was a **heuristic-only** pass. No LLM ran over the source; '
                   'semantic correctness, algorithmic safety, and idiomatic style were not evaluated.')
    # What we actually looked at
    out.append(f'Snapshot covered **{fc}** files (depth `{depth or "standard"}`, `--depth 1` git clone). Vendored '
               'trees and build artefacts were skipped via `IGNORED_DIRS`; your repo may have more surface area than this number.')
    # What each pack contributes, if it did
    if packs.get('python'):
        out.append('Python rule pack fired. Bare-except, mutable-default-args, `sys.path` hacks, and timeout-less `requests` calls are surface anti-patterns, not proofs — confirm each one in context before acting.')
    if packs.get('juce'):
        out.append('JUCE rule pack fired. Flagged patterns inside `processBlock` are **real-time-safety smells** — the regex cannot prove they are on the audio thread at runtime, but a human audio engineer should review each one.')
    if packs.get('node'):
        out.append('Node rule pack fired. Lockfile / engines / test-script checks are hygiene-level; none of them are correctness claims.')
    if packs.get('finance'):
        out.append('Finance rule pack fired. Missing kill-switch / non-paper-default signals are production-readiness concerns for live trading bots, not code defects.')
    if n == 0:
        out.append('Zero findings on a well-formed repo is a genuine signal of hygiene; on a sparse repo it can also mean "not enough sampled source". Both are possible.')
    # What a follow-up round would add
    out.append('A follow-up round with LLM backing (Cloudflare Workers AI) will layer: idiomatic-style feedback, algorithmic soundness checks, test-coverage inference, and named-risk suggestions. That round is available when the CF credentials are set on the operator workflow.')
    return out


def _derive_verdict(findings: list, snapshot: dict) -> str:
    """Map heuristic findings to a coarse ship/feedback/redesign verdict."""
    n = len(findings)
    file_count = snapshot.get('file_count', 0)
    # Empty or near-empty repos are high-uncertainty rather than bad.
    if file_count < 3:
        return '🟡 low-signal repo (too few files to evaluate)'
    if n == 0:
        return '🟢 ship — no surface-level issues on first pass'
    if n <= 2:
        return '🟡 address feedback — small number of specific fixes suggested'
    return '🔴 redesign pass recommended — several structural gaps'


def post_to_portfolio_issue(target: str, body: str) -> bool:
    """target is owner/repo#NN. Uses `gh` so GITHUB_TOKEN is honoured."""
    m = re.match(r'^([^/]+)/([^#]+)#(\d+)$', target)
    if not m:
        print(f'[warn] PORTFOLIO_ISSUE must be owner/repo#NN, got {target!r}')
        return False
    owner, repo, num = m.group(1), m.group(2), m.group(3)
    url = f'https://github.com/{owner}/{repo}/issues/{num}'
    r = subprocess.run(
        ['gh', 'issue', 'comment', num,
         '--repo', f'{owner}/{repo}', '--body', body],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        print(f'[post-failed] {r.stderr.strip()[:400]}')
        return False
    print(f'[posted] {url}')
    return True


def main() -> int:
    p = argparse.ArgumentParser(description='Single-target AI pre-review.')
    p.add_argument('--target', default=os.environ.get('TARGET_URL', ''),
                   help='GitHub repo / PR / gist URL to review.')
    p.add_argument('--portfolio-issue',
                   default=os.environ.get('PORTFOLIO_ISSUE', ''),
                   help='owner/repo#NN to post the review back to.')
    p.add_argument('--review-type',
                   default=os.environ.get('REVIEW_TYPE', 'standard'))
    p.add_argument('--depth',
                   default=os.environ.get('REVIEW_DEPTH', 'standard'))
    p.add_argument('--focus', default=os.environ.get('FOCUS', ''))
    p.add_argument('--output-dir', default='output/target_review')
    args = p.parse_args()

    if not args.target:
        print('error: --target (or TARGET_URL) is required')
        return 2
    try:
        target = parse_target(args.target)
    except ValueError as exc:
        print(f'error: {exc}')
        return 2

    context = {
        'review_type': args.review_type,
        'depth': args.depth,
        'focus': args.focus,
    }

    if target['kind'] in ('repo', 'pr'):
        review = review_repo_snapshot(target['owner'], target['repo'], target.get('ref'))
    else:
        review = {'error': 'gist reviews are not yet implemented'}

    body = render_markdown(target, review, context)

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r'[^a-z0-9\-_]+', '-', args.target.lower())[:80] or 'target'
    out_path = out_dir / f'{slug}.md'
    out_path.write_text(body, encoding='utf-8')
    print(f'[wrote] {out_path}')

    # ----- LLMBuilder training export -----
    # Every live review becomes a supervised training example for
    # ARC-Neuron-LLMBuilder. Always on; opt out with EMIT_TRAINING_DATA=0.
    if os.environ.get('EMIT_TRAINING_DATA', '1') != '0':
        snap = (review or {}).get('snapshot', {}) or {}
        findings = (review or {}).get('findings', []) or []
        # Pull a coarse verdict string by running the same helper.
        verdict = _derive_verdict(findings, snap) if 'snapshot' in (review or {}) else 'unable-to-evaluate'
        # Confidence heuristic: fewer findings = higher confidence.
        n = len(findings)
        if 'error' in (review or {}):
            confidence = 0.2
        elif n == 0:
            confidence = 0.85
        elif n <= 2:
            confidence = 0.7
        else:
            confidence = 0.55
        try:
            jsonl = training_export.export_from_review(
                target_url=target.get('url', ''),
                focus=context.get('focus', ''),
                depth=context.get('depth', ''),
                review_markdown=body,
                confidence=confidence,
                findings=findings,
                verdict=verdict,
            )
            print(f'[training] appended training example -> {jsonl}')
        except Exception as exc:
            # Never fail a review because training export hiccuped.
            print(f'[training-warn] could not emit training data: {exc}')

    if args.portfolio_issue:
        ok = post_to_portfolio_issue(args.portfolio_issue, body)
        return 0 if ok else 1

    return 0


if __name__ == '__main__':
    raise SystemExit(main())
