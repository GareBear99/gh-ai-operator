#!/usr/bin/env python3
"""Convert a live review into ARC-Neuron-LLMBuilder-compatible training JSONL.

LLMBuilder expects seed examples under `data/<capability>/seed_examples.jsonl`
with a fixed schema (see `data/critique/seed_examples.jsonl`):

    {"id": str, "capability": str, "domain": str, "difficulty": str,
     "input": {"task": str}, "target": {"analysis": str, "confidence": float},
     "tags": [str, ...]}

Every production call the operator makes is a natural fit for the `critique`
capability — input is (repo URL + focus + snapshot), target is the review
text + confidence. This module writes one JSONL line per review into
`output/training_export/critique/arc-operator-<slug>.jsonl`, which the
LLMBuilder ingest script can later merge into its canonical corpus.

A "correction" lane is also supported — when a Portfolio Follow-up issue
is opened that contradicts or amends an earlier verdict, the correction is
emitted as a *second* JSONL entry with `tags: [..., "correction"]` so
LLMBuilder can weight corrections higher.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Dict, List, Optional


CAPABILITY = 'critique'
DOMAIN = 'code'

_DEPTH_DIFFICULTY = {
    'quick': 'easy',
    'standard': 'medium',
    'deep': 'hard',
}


def _slug(s: str, cap: int = 60) -> str:
    s = re.sub(r'[^a-zA-Z0-9]+', '-', s.lower()).strip('-')
    return s[:cap] or 'target'


def _id_for(target_url: str, suffix: str = '') -> str:
    h = hashlib.sha256(target_url.encode('utf-8')).hexdigest()[:10]
    return f'arc-operator-{h}{("-" + suffix) if suffix else ""}'


def build_record(
    target_url: str,
    focus: str,
    depth: str,
    review_markdown: str,
    confidence: float,
    findings: List[str],
    verdict: str,
    suffix: str = '',
    tags: Optional[List[str]] = None,
) -> Dict:
    """Build a single LLMBuilder-compatible seed-examples record."""
    difficulty = _DEPTH_DIFFICULTY.get((depth or '').lower(), 'medium')
    base_tags = ['critique', 'arc-operator', 'live-deployment']
    if tags:
        base_tags.extend(tags)
    focus_text = focus.strip() if focus else '(no specific focus provided)'

    task = (
        f"Review the public GitHub target <{target_url}>. "
        f"Requester focus: {focus_text}. "
        "Produce a concise, evidence-first critique with a coarse verdict "
        "(ship / address feedback / redesign), then highlights and concrete "
        "findings. Never invent concerns; every point must reference a file "
        "pattern or observable fact."
    )

    # Keep analysis under ~4KB so the SFT corpus stays tight.
    analysis = review_markdown.strip()
    if len(analysis) > 4000:
        analysis = analysis[:3990] + '\n... [truncated]'

    rec: Dict = {
        'id':         _id_for(target_url, suffix),
        'capability': CAPABILITY,
        'domain':     DOMAIN,
        'difficulty': difficulty,
        'input':      {'task': task},
        'target':     {
            'analysis':   analysis,
            'confidence': max(0.0, min(1.0, float(confidence))),
            'verdict':    verdict,
            'findings':   list(findings[:20]),
        },
        'tags':       base_tags,
        'provenance': {
            'source':     'github.com/GareBear99/gh-ai-operator',
            'emitted_at': _dt.datetime.now(_dt.timezone.utc).isoformat(timespec='seconds'),
            'target_url': target_url,
        },
    }
    return rec


def append_jsonl(record: Dict, out_dir: Path) -> Path:
    """Append a single record to the capability shard and return the path."""
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / 'seed_examples.jsonl'
    with path.open('a', encoding='utf-8') as fh:
        fh.write(json.dumps(record, ensure_ascii=False))
        fh.write('\n')
    return path


def export_from_review(
    target_url: str,
    focus: str,
    depth: str,
    review_markdown: str,
    confidence: float,
    findings: List[str],
    verdict: str,
    base_dir: str = 'output/training_export',
) -> Path:
    """Public entrypoint. Emit a single JSONL line under
    <base_dir>/critique/seed_examples.jsonl and return the file path."""
    rec = build_record(
        target_url=target_url,
        focus=focus,
        depth=depth,
        review_markdown=review_markdown,
        confidence=confidence,
        findings=findings,
        verdict=verdict,
    )
    return append_jsonl(rec, Path(base_dir) / CAPABILITY)


def export_correction(
    target_url: str,
    focus: str,
    depth: str,
    correction_markdown: str,
    confidence: float = 0.95,
    base_dir: str = 'output/training_export',
) -> Path:
    """Emit a Portfolio-follow-up correction as a higher-priority training
    record (tagged `correction`) that LLMBuilder's ingest can weight up."""
    rec = build_record(
        target_url=target_url,
        focus=focus,
        depth=depth,
        review_markdown=correction_markdown,
        confidence=confidence,
        findings=[],
        verdict='correction',
        suffix='correction',
        tags=['correction', 'human-follow-up'],
    )
    return append_jsonl(rec, Path(base_dir) / CAPABILITY)


# ---------------------------------------------------------------
# CLI
# ---------------------------------------------------------------
def _cli() -> int:
    import argparse
    p = argparse.ArgumentParser(description='Emit a training JSONL line from a stdin-review.')
    p.add_argument('--target', required=True)
    p.add_argument('--focus', default='')
    p.add_argument('--depth', default='standard')
    p.add_argument('--confidence', type=float, default=0.75)
    p.add_argument('--verdict', default='unknown')
    p.add_argument('--base-dir', default='output/training_export')
    p.add_argument('--review-file', required=True,
                   help='Path to the markdown review to embed as target.analysis.')
    args = p.parse_args()

    md = Path(args.review_file).read_text(encoding='utf-8')
    out = export_from_review(
        target_url=args.target,
        focus=args.focus,
        depth=args.depth,
        review_markdown=md,
        confidence=args.confidence,
        findings=[],
        verdict=args.verdict,
        base_dir=args.base_dir,
    )
    print(f'[training] appended to {out}')
    return 0


if __name__ == '__main__':
    raise SystemExit(_cli())
