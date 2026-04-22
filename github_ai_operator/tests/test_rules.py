"""Rule-pack unit tests. Pure-Python, no network."""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).parent.parent
sys.path.insert(0, str(HERE))

from github_ai_operator import review_rules  # noqa: E402


# ----------------------------- Language detection --------------------------
def test_detect_languages_orders_by_count() -> None:
    snap = {'top_extensions': {'.py': 10, '.h': 3, '.cpp': 2, '.md': 40}}
    langs = review_rules.detect_languages(snap)
    assert langs[0] == 'python'          # 10 py hits
    assert 'cpp' in langs                 # 3 + 2 = 5 cpp hits
    # .md isn't mapped; must not appear
    assert all(l in {'python', 'cpp', 'c', 'node', 'rust', 'go', 'solidity'} for l in langs)


def test_is_juce_detects_vendored_juce_dir() -> None:
    assert review_rules.is_juce({'root_dirs': ['JUCE', 'Source']}, Path('.')) is True


def test_is_juce_detects_processBlock_in_source() -> None:
    snap = {'root_dirs': [], 'root_files': [],
            'source_samples': {'Source/Plugin.cpp': 'void processBlock(...) {}'}}
    assert review_rules.is_juce(snap, Path('.')) is True


def test_detect_trading_patterns() -> None:
    snap = {'symbol_samples': {}, 'source_samples': {
        'bot.py': 'FUNDING_RATE = 0.08\norder_book = fetch()\nleverage = 3'}}
    assert review_rules.detect_trading_patterns(snap) is True


# ----------------------------- Python rules --------------------------------
def test_python_bare_except_flagged_when_multiple() -> None:
    snap = {'source_samples': {
        'a.py': 'try:\n    x()\nexcept:\n    pass\n',
        'b.py': 'try:\n    y()\nexcept:\n    pass\n',
    }}
    out = review_rules.rules_python(Path('.'), snap)
    assert any('Bare `except:`' in f for f in out)


def test_python_bare_except_ignored_when_single() -> None:
    snap = {'source_samples': {'a.py': 'try:\n    x()\nexcept:\n    pass\n'}}
    out = review_rules.rules_python(Path('.'), snap)
    assert not any('Bare `except:`' in f for f in out)


def test_python_mutable_default_args() -> None:
    snap = {'source_samples': {'a.py': 'def f(x=[]): return x'}}
    out = review_rules.rules_python(Path('.'), snap)
    assert any('Mutable default arguments' in f for f in out)


def test_python_syspath_hack() -> None:
    snap = {'source_samples': {'scripts/run.py': 'import sys\nsys.path.insert(0, "..")'}}
    out = review_rules.rules_python(Path('.'), snap)
    assert any('sys.path' in f.lower() for f in out)


def test_python_requests_without_timeout() -> None:
    snap = {'source_samples': {'net.py': 'import requests\nr = requests.get("https://x")'}}
    out = review_rules.rules_python(Path('.'), snap)
    assert any('timeout' in f.lower() for f in out)


def test_python_requests_with_timeout_is_clean() -> None:
    snap = {'source_samples': {'net.py': 'import requests\nr = requests.get("https://x", timeout=30)'}}
    out = review_rules.rules_python(Path('.'), snap)
    assert not any('timeout' in f.lower() for f in out)


# ----------------------------- C++/JUCE rules ------------------------------
def test_juce_flags_allocation_in_processBlock() -> None:
    snap = {
        'root_dirs': ['JUCE', 'Source'],
        'source_samples': {'Source/Plugin.cpp': '''
            void MyAudioProcessor::processBlock(AudioBuffer<float>& buffer) {
                std::vector<float> tmp(buffer.getNumSamples());
                auto sp = std::make_shared<Filter>();
            }
        '''},
    }
    out = review_rules.rules_cpp_juce(Path('.'), snap)
    assert any('allocation inside `processBlock`' in f for f in out)


def test_juce_flags_lock_in_processBlock() -> None:
    snap = {
        'root_dirs': ['JUCE', 'Source'],
        'source_samples': {'Source/Plugin.cpp': '''
            void Plugin::processBlock(AudioBuffer<float>& buf) {
                std::lock_guard<std::mutex> g(paramMutex);
            }
        '''},
    }
    out = review_rules.rules_cpp_juce(Path('.'), snap)
    assert any('Lock primitive' in f for f in out)


def test_juce_skips_when_not_juce() -> None:
    snap = {
        'root_dirs': ['src'],
        'root_files': [],
        'source_samples': {'src/main.cpp': 'int main() {}'},
    }
    out = review_rules.rules_cpp_juce(Path('.'), snap)
    assert out == []


# ----------------------------- Node rules ----------------------------------
def test_node_lockfile_missing(tmp_path: Path) -> None:
    (tmp_path / 'package.json').write_text(
        '{"name":"x","version":"0.1.0","scripts":{"test":"jest"},"engines":{"node":">=18"}}',
        encoding='utf-8',
    )
    snap = {'root_files': ['package.json']}
    out = review_rules.rules_node(tmp_path, snap)
    assert any('lockfile' in f.lower() for f in out)


def test_node_missing_engines_and_test(tmp_path: Path) -> None:
    (tmp_path / 'package.json').write_text('{"name":"x"}', encoding='utf-8')
    snap = {'root_files': ['package.json', 'package-lock.json']}
    out = review_rules.rules_node(tmp_path, snap)
    assert any('engines' in f.lower() for f in out)
    assert any('"test"' in f for f in out)


# ----------------------------- Finance rules -------------------------------
def test_finance_flags_missing_kill_switch() -> None:
    snap = {'source_samples': {
        'bot.py': 'FUNDING_RATE=0.08\nleverage=3\norder = place_order()',
    }}
    out = review_rules.rules_finance(Path('.'), snap)
    assert any('kill-switch' in f.lower() for f in out)


def test_finance_flags_api_key_literal() -> None:
    snap = {'source_samples': {
        'bad.py': "api_key = 'sk-live-abcdefghijklmnopqrstuvwxyz01234567'\nfunding_rate=0.08\nleverage=3",
    }}
    out = review_rules.rules_finance(Path('.'), snap)
    assert any('api key' in f.lower() for f in out)


def test_finance_skips_nontrading_repo() -> None:
    snap = {'source_samples': {
        'app.py': 'from flask import Flask\napp = Flask(__name__)',
    }}
    out = review_rules.rules_finance(Path('.'), snap)
    assert out == []


# ----------------------------- Orchestrator --------------------------------
def test_run_all_returns_findings_and_langs() -> None:
    snap = {
        'top_extensions': {'.py': 5},
        'source_samples': {
            'a.py': 'try:\n    x()\nexcept:\n    pass\n',
            'b.py': 'try:\n    y()\nexcept:\n    pass\n',
        },
    }
    findings, langs = review_rules.run_all(Path('.'), snap)
    assert 'python' in langs
    assert any('Bare `except:`' in f for f in findings)
