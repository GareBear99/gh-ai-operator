"""Minimal tests for the GitHub AI Operator.

Runs without a network. Covers the bits the Portfolio dispatch flow depends on.
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).parent.parent
sys.path.insert(0, str(HERE))

from github_ai_operator.config import AppConfig, Limits  # noqa: E402
import review_target  # noqa: E402


def test_config_from_example() -> None:
    example = HERE / 'config.example.json'
    cfg = AppConfig.from_json(example)
    assert cfg.seed_repos, 'example config must declare seed repos'
    assert 0 < cfg.limits.min_similarity_score <= 1
    assert cfg.posting.draft_only is True
    cfg.validate()  # should not raise


def test_limits_defaults() -> None:
    limits = Limits()
    assert limits.max_repos_per_run >= 1
    assert 0 < limits.min_similarity_score < 1
    assert 0 < limits.min_issue_confidence < 1


def test_parse_target_repo() -> None:
    t = review_target.parse_target('https://github.com/octocat/Hello-World')
    assert t['kind'] == 'repo'
    assert t['owner'] == 'octocat'
    assert t['repo'] == 'Hello-World'
    assert t['pr'] is None


def test_parse_target_pr() -> None:
    t = review_target.parse_target('https://github.com/octocat/Hello-World/pull/42')
    assert t['kind'] == 'pr'
    assert t['pr'] == 42


def test_parse_target_gist() -> None:
    t = review_target.parse_target('https://gist.github.com/octocat/abc123')
    assert t['kind'] == 'gist'


def test_parse_target_rejects_non_github() -> None:
    try:
        review_target.parse_target('https://gitlab.com/octocat/Hello-World')
    except ValueError:
        return
    raise AssertionError('non-github URL should have raised ValueError')


def test_verdict_ship_with_no_findings() -> None:
    v = review_target._derive_verdict([], {'file_count': 50})
    assert v.startswith('🟢')


def test_verdict_feedback_with_few_findings() -> None:
    v = review_target._derive_verdict(['one', 'two'], {'file_count': 50})
    assert v.startswith('🟡')


def test_verdict_redesign_with_many_findings() -> None:
    v = review_target._derive_verdict(['a', 'b', 'c', 'd'], {'file_count': 50})
    assert v.startswith('🔴')


def test_verdict_low_signal_for_empty_repo() -> None:
    v = review_target._derive_verdict([], {'file_count': 1})
    assert 'low-signal' in v


def test_render_markdown_contains_verdict_and_footer() -> None:
    target = {'kind': 'repo', 'owner': 'octocat', 'repo': 'Hello-World',
              'url': 'https://github.com/octocat/Hello-World', 'pr': None}
    review = {'snapshot': {'file_count': 10, 'total_size_bytes': 1234,
                           'top_extensions': {'.py': 5}, 'root_files': ['README.md']},
              'findings': []}
    context = {'review_type': 'standard', 'depth': 'quick',
               'focus': 'Check startup sequencing.'}
    body = review_target.render_markdown(target, review, context)
    assert 'pre-review' in body
    assert 'Verdict' in body
    assert 'ARC GitHub AI Operator' in body
    assert 'Follow-up' in body


def test_cloudflare_provider_registered_and_top_priority() -> None:
    from github_ai_operator import free_llm_client
    names = [p.name for p in free_llm_client.PROVIDERS]
    assert 'Cloudflare Workers AI' in names
    assert names[0] == 'Cloudflare Workers AI', 'CF should be top priority'
    cf = next(p for p in free_llm_client.PROVIDERS if p.name == 'Cloudflare Workers AI')
    assert cf.api_key_env == 'CLOUDFLARE_API_TOKEN'
    assert cf.required_env == ['CLOUDFLARE_ACCOUNT_ID']
    assert '{CLOUDFLARE_ACCOUNT_ID}' in cf.api_url
    resolved = cf.api_url.format(CLOUDFLARE_ACCOUNT_ID='acc123')
    assert 'api.cloudflare.com' in resolved
    assert '/accounts/acc123/ai/v1/chat/completions' in resolved


def test_cloudflare_skipped_when_account_id_missing(monkeypatch) -> None:
    import os
    from github_ai_operator import free_llm_client
    monkeypatch.setenv('CLOUDFLARE_API_TOKEN', 'fake')
    monkeypatch.delenv('CLOUDFLARE_ACCOUNT_ID', raising=False)
    cf = next(p for p in free_llm_client.PROVIDERS if p.name == 'Cloudflare Workers AI')
    missing = any(not os.getenv(v) for v in cf.required_env)
    assert missing, 'without CLOUDFLARE_ACCOUNT_ID the CF provider must be skipped'


def test_render_markdown_handles_error() -> None:
    body = review_target.render_markdown(
        {'kind': 'repo', 'url': 'https://github.com/x/y', 'owner': 'x', 'repo': 'y', 'pr': None},
        {'error': 'clone failed'},
        {'review_type': 'standard', 'depth': 'standard', 'focus': ''},
    )
    assert 'Could not fetch the target' in body
    assert 'clone failed' in body
