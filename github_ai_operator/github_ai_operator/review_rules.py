"""Language-aware review rules.

The base `heuristic_findings()` in review.py is kept deliberately shallow so it
works on any codebase. This module layers language-specific rules on top. It
activates per-language rule packs based on the snapshot's file extensions and
then returns a list of additional findings that get appended to the review.

Every finding cites a file pattern or observable fact; no finding is
speculative. Empty return means "nothing to add" — not an error.

Rule packs available:
  - Python (bare-except, mutable defaults, sys.path hacks, timeout-less http)
  - C++/JUCE (processBlock allocations, locking, logging — all RT-unsafe)
  - Node (lockfile consistency, engines.node pin)
  - Finance/trading patterns (kill-switch, paper-trading default, hardcoded keys)
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Callable, Dict, List, Tuple


# ---------------------------------------------------------------------------
# Language detection
# ---------------------------------------------------------------------------
_LANG_BY_EXT: Dict[str, str] = {
    '.py':    'python',
    '.pyi':   'python',
    '.cpp':   'cpp',
    '.cc':    'cpp',
    '.cxx':   'cpp',
    '.c':     'c',
    '.h':     'cpp',     # most headers in this ecosystem are JUCE/C++
    '.hpp':   'cpp',
    '.hh':    'cpp',
    '.js':    'node',
    '.mjs':   'node',
    '.cjs':   'node',
    '.ts':    'node',
    '.tsx':   'node',
    '.jsx':   'node',
    '.rs':    'rust',
    '.go':    'go',
    '.sol':   'solidity',
}


def detect_languages(snapshot: dict) -> List[str]:
    """Return languages present in the snapshot, ordered by file count."""
    ext_counts = snapshot.get('top_extensions', {}) or {}
    tally: Dict[str, int] = {}
    for ext, n in ext_counts.items():
        lang = _LANG_BY_EXT.get(ext.lower())
        if lang:
            tally[lang] = tally.get(lang, 0) + int(n)
    return [lang for lang, _ in sorted(tally.items(), key=lambda kv: kv[1], reverse=True)]


def is_juce(snapshot: dict, repo_dir: Path) -> bool:
    """JUCE presence: vendored `JUCE/`, a `.jucer` file, or processBlock text."""
    root_dirs  = {d.lower() for d in snapshot.get('root_dirs',  [])}
    root_files = {f.lower() for f in snapshot.get('root_files', [])}
    if 'juce' in root_dirs or any(f.endswith('.jucer') for f in root_files):
        return True
    for rel, text in (snapshot.get('source_samples') or {}).items():
        if 'processBlock' in text or 'juce::AudioProcessor' in text:
            return True
    return False


def detect_trading_patterns(snapshot: dict) -> bool:
    """Returns True if this repo looks like a trading/finance codebase.

    Uses symbol samples + source samples for signal, not just filename.
    """
    corpus = ' '.join(
        [' '.join(symbols) for symbols in (snapshot.get('symbol_samples') or {}).values()]
        + list((snapshot.get('source_samples') or {}).values())
    ).lower()
    hits = sum(1 for kw in (
        'funding_rate', 'arbitrage', 'leverage', 'position_size',
        'stop_loss', 'kill_switch', 'paper_trading', 'dry_run',
        'order_book', 'orderbook', 'binance', 'ccxt', 'kraken',
    ) if kw in corpus)
    return hits >= 2


# ---------------------------------------------------------------------------
# Rule primitives
# ---------------------------------------------------------------------------
def _iter_files_of(snapshot: dict, exts: Tuple[str, ...]) -> List[Tuple[str, str]]:
    samples = snapshot.get('source_samples') or {}
    return [(rel, text) for rel, text in samples.items()
            if any(rel.lower().endswith(e) for e in exts)]


def _first_n(seq: List[str], n: int = 3) -> str:
    return ', '.join(f'`{x}`' for x in seq[:n])


# ---------------------------------------------------------------------------
# Python rules
# ---------------------------------------------------------------------------
_BARE_EXCEPT   = re.compile(r'^\s*except\s*:', re.MULTILINE)
_MUT_DEFAULT   = re.compile(r'def\s+\w+\([^)]*=\s*(\[\]|\{\})[^)]*\)')
_SYS_PATH      = re.compile(r'\bsys\.path\.(insert|append)\s*\(')
_REQUESTS_NOT  = re.compile(r'\brequests\.(get|post|put|delete|patch|head)\s*\(')
_TIMEOUT_HINT  = re.compile(r'\btimeout\s*=')


def rules_python(repo_dir: Path, snapshot: dict) -> List[str]:
    out: List[str] = []
    py = _iter_files_of(snapshot, ('.py',))
    if not py:
        return out

    bare_except_files = [rel for rel, t in py if _BARE_EXCEPT.search(t)]
    if len(bare_except_files) >= 2:
        out.append(f'[python] Bare `except:` in {len(bare_except_files)} files (e.g. {_first_n(bare_except_files)}): catches SystemExit/KeyboardInterrupt and hides bugs — use `except Exception:` or specific types.')

    mut_default_files = [rel for rel, t in py if _MUT_DEFAULT.search(t)]
    if mut_default_files:
        out.append(f'[python] Mutable default arguments found in {_first_n(mut_default_files)}: `def f(x=[])` / `{{}}` shares state across calls. Use `None` + `if x is None: x = []`.')

    sys_path_files = [rel for rel, t in py if _SYS_PATH.search(t)]
    if sys_path_files:
        out.append(f'[python] `sys.path` manipulation in {_first_n(sys_path_files)}: a packaging smell — prefer `pyproject.toml` + editable install.')

    req_no_timeout = []
    for rel, t in py:
        calls = _REQUESTS_NOT.findall(t)
        if not calls:
            continue
        # Crude: check every call site has a timeout= nearby
        segments = _REQUESTS_NOT.split(t)
        # This is imprecise but a useful signal without parsing AST
        matches = [m for m in _REQUESTS_NOT.finditer(t)]
        for m in matches:
            window = t[m.start():m.start() + 400]
            if not _TIMEOUT_HINT.search(window):
                req_no_timeout.append(rel)
                break
    req_no_timeout = sorted(set(req_no_timeout))
    if req_no_timeout:
        out.append(f'[python] `requests.*` call without apparent `timeout=` in {_first_n(req_no_timeout)}: unbounded HTTP can hang the process indefinitely.')

    return out


# ---------------------------------------------------------------------------
# C++ / JUCE rules
# ---------------------------------------------------------------------------
_CPP_EXTS = ('.cpp', '.cc', '.cxx', '.h', '.hpp', '.hh')
# Heuristic body grab: match from processBlock's opening `{` to the first
# line that contains a lone closing `}` at any indent. Not a real parser,
# but reliable for flagging obvious RT-safety anti-patterns.
_PROCESS_BLOCK = re.compile(
    r'\b(?:void\s+)?\w*::?processBlock\b[^{]*\{([\s\S]*?)\n\s*\}',
)
_ALLOC_PATTERNS = re.compile(
    r'\b(?:new\s+\w|std::make_(?:shared|unique)\s*<|std::vector\s*<|std::string\s*\(|std::map\s*<|malloc\s*\()',
)
_LOCK_PATTERNS = re.compile(
    r'\b(?:std::mutex|std::lock_guard|std::unique_lock|std::scoped_lock)\b',
)
_LOG_PATTERNS = re.compile(
    r'\b(?:std::cout|std::cerr|DBG\s*\(|printf\s*\()',
)


def rules_cpp_juce(repo_dir: Path, snapshot: dict) -> List[str]:
    out: List[str] = []
    if not is_juce(snapshot, repo_dir):
        return out

    cpp = _iter_files_of(snapshot, _CPP_EXTS)
    alloc_hits: List[str] = []
    lock_hits:  List[str] = []
    log_hits:   List[str] = []

    for rel, text in cpp:
        for m in _PROCESS_BLOCK.finditer(text):
            body = m.group(1)
            if _ALLOC_PATTERNS.search(body):
                alloc_hits.append(rel)
            if _LOCK_PATTERNS.search(body):
                lock_hits.append(rel)
            if _LOG_PATTERNS.search(body):
                log_hits.append(rel)

    if alloc_hits:
        out.append(f'[juce] Possible allocation inside `processBlock` in {_first_n(sorted(set(alloc_hits)))}: `new`/`std::make_shared`/`std::vector<>(...)`/`malloc()` on the audio thread can cause priority inversion and dropouts.')
    if lock_hits:
        out.append(f'[juce] Lock primitive inside `processBlock` in {_first_n(sorted(set(lock_hits)))}: `std::mutex`/`lock_guard`/`unique_lock` can block the audio thread. Prefer lock-free FIFOs for parameter updates.')
    if log_hits:
        out.append(f'[juce] Logging call inside `processBlock` in {_first_n(sorted(set(log_hits)))}: `std::cout`/`DBG(`/`printf` is not real-time-safe. Gate behind `#ifndef NDEBUG` or use a lock-free logger.')
    return out


# ---------------------------------------------------------------------------
# Node/JS rules
# ---------------------------------------------------------------------------
def rules_node(repo_dir: Path, snapshot: dict) -> List[str]:
    out: List[str] = []
    root = {f.lower() for f in snapshot.get('root_files', [])}
    if 'package.json' not in root:
        return out

    pkg_path = repo_dir / 'package.json'
    try:
        pkg = json.loads(pkg_path.read_text(encoding='utf-8', errors='ignore'))
    except Exception:
        return out

    lock_files = {'package-lock.json', 'yarn.lock', 'pnpm-lock.yaml'}
    if not (root & lock_files):
        out.append('[node] `package.json` is present but no lockfile (`package-lock.json` / `yarn.lock` / `pnpm-lock.yaml`) was found at repo root: installs are non-reproducible across machines.')

    engines = (pkg.get('engines') or {}).get('node')
    if not engines:
        out.append('[node] `package.json` is missing `"engines": {"node": ">=X"}`: new contributors will guess the Node version. Pin it.')

    if not (pkg.get('scripts') or {}).get('test'):
        out.append('[node] `package.json` defines no `"test"` script: `npm test` will silently succeed in CI.')

    return out


# ---------------------------------------------------------------------------
# Finance/trading rules
# ---------------------------------------------------------------------------
_API_KEY_LITERAL = re.compile(
    r"""['"](?:sk[_-]live[_-]|pk[_-]live[_-]|AKIA|xoxb-|gsk_|ghp_)[A-Za-z0-9_-]{16,}['"]""",
)
_DRY_RUN_DEFAULT = re.compile(
    r'\b(?:DRY_RUN|PAPER_TRADING|TESTNET)\b[^=\n]*=\s*(True|true|"?true"?|1)',
)
_KILL_SWITCH = re.compile(r'(kill_switch|panic_close|halt_all|emergency_exit)', re.IGNORECASE)


def rules_finance(repo_dir: Path, snapshot: dict) -> List[str]:
    out: List[str] = []
    if not detect_trading_patterns(snapshot):
        return out

    samples = snapshot.get('source_samples') or {}
    leak_files: List[str] = []
    kill_any = False
    paper_default_any = False

    for rel, text in samples.items():
        if _API_KEY_LITERAL.search(text):
            leak_files.append(rel)
        if _KILL_SWITCH.search(text):
            kill_any = True
        if _DRY_RUN_DEFAULT.search(text):
            paper_default_any = True

    if leak_files:
        out.append(f'[finance] Possible hard-coded API key literal in {_first_n(leak_files)}: rotate immediately and move to environment variable.')
    if not kill_any:
        out.append('[finance] No kill-switch / emergency-close pattern detected in sampled source (`kill_switch` / `panic_close` / `halt_all` / `emergency_exit`). A production trading bot needs one.')
    if not paper_default_any:
        out.append('[finance] Could not confirm `DRY_RUN=True` / `PAPER_TRADING=True` default in sampled config: a production trading bot should paper-trade by default and require explicit opt-in for live.')
    return out


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------
RuleFn = Callable[[Path, dict], List[str]]


def run_all(repo_dir: Path, snapshot: dict) -> Tuple[List[str], List[str]]:
    """Run every applicable rule pack. Return (findings, langs_activated)."""
    langs = detect_languages(snapshot)
    findings: List[str] = []

    if 'python' in langs:
        findings.extend(rules_python(repo_dir, snapshot))
    if 'cpp' in langs or 'c' in langs:
        findings.extend(rules_cpp_juce(repo_dir, snapshot))
    if 'node' in langs:
        findings.extend(rules_node(repo_dir, snapshot))

    # Finance runs across any language — signal comes from content.
    findings.extend(rules_finance(repo_dir, snapshot))

    return findings, langs
