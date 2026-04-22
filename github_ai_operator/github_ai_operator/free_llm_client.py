from __future__ import annotations

"""
free_llm_client.py — Free/open LLM backends for code review intelligence.

Provider priority (tried in order based on which keys are present):

  KEYED (faster, smarter):
  1. Cloudflare Workers AI — CLOUDFLARE_API_TOKEN + CLOUDFLARE_ACCOUNT_ID
                           — Llama-3.3 70B (fp8-fast) on Cloudflare's edge.
                             Free tier: ~10,000 neurons/day, no credit card.
                             OpenAI-compatible endpoint.
  2. Groq         — GROQ_API_KEY        — Llama 3.1 70B, free tier, groq.com
  3. Together AI  — TOGETHER_API_KEY    — Qwen 2.5 Coder 72B, free credit, together.ai
  4. Mistral      — MISTRAL_API_KEY     — Codestral, free tier, mistral.ai
  5. Cerebras     — CEREBRAS_API_KEY    — Llama 3.1 70B, free tier, inference.cerebras.ai

  LOCAL (no key, needs hardware):
  6. Ollama       — no key, needs `ollama serve` running locally
  7. LM Studio    — no key, needs LM Studio running locally

  ZERO-KEY PUBLIC FALLBACK (no key, no hardware, always available):
  8. Hugging Face Serverless Inference — no key needed for public models,
     rate limited but always reachable. Uses Mistral-7B-Instruct by default.
     Good enough to write a real review. Not as smart as 70B but not a template.

Setup (any one is enough):
  # Cloudflare Workers AI (recommended — no credit card, generous free tier):
  export CLOUDFLARE_ACCOUNT_ID=<your-account-id>
  export CLOUDFLARE_API_TOKEN=<token-with-Workers-AI:Read-and-Edit>
  # or any of:
  export GROQ_API_KEY=gsk_...
  export TOGETHER_API_KEY=...
  export MISTRAL_API_KEY=...
  export CEREBRAS_API_KEY=...
  # or just run ollama serve with a model pulled
  # or set nothing — HuggingFace will be used automatically
"""

import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import requests

# ---------------------------------------------------------------------------
# System prompt — shared across all providers
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are a senior open-source developer doing a genuine, helpful code review.

Your goal: write a GitHub issue that the maintainer reads and thinks
"this person actually looked at my code and has something useful to say."

RULES:
1. Evidence only. Every concern must reference a specific file, function name,
   line pattern, or observable fact from the data you were given. No invented concerns.
2. If the repo is genuinely clean — say so. Ask a real technical question instead.
3. One specific real concern beats five vague ones. No padding.
4. Tone: peer reviewer, not auditor. "I noticed X, have you considered Y?"
5. Issue body must be readable in under 2 minutes. Be tight.
6. End with one genuine question or conversation hook.
7. Title must sound human. Not "Automated review findings."
8. confidence: be honest. Low confidence = don't post it.

Return ONLY valid JSON. No markdown fences. No preamble. No postamble.
Required keys:
{
  "summary": "one sentence describing what you found",
  "praise": ["specific good things with evidence"],
  "concerns": ["specific evidence-backed concerns only"],
  "improvements": ["concrete actionable suggestions"],
  "issue_title": "human-sounding title",
  "issue_body": "full markdown issue body ready to post",
  "confidence": 0.0,
  "conversation_hook": "the closing question"
}"""


# ---------------------------------------------------------------------------
# Provider definitions
# ---------------------------------------------------------------------------

@dataclass
class LLMProvider:
    name: str
    api_url: str                    # may contain {ENV_VAR} placeholders
    default_model: str
    api_key_env: Optional[str]
    needs_key: bool = True
    max_context_chars: int = 60000
    extra_headers: Dict[str, str] = field(default_factory=dict)
    request_style: str = "openai"   # "openai" | "hf_serverless"
    # Extra env vars (besides the API key) that MUST all be set for this
    # provider to be used. Values are substituted into api_url.
    required_env: List[str] = field(default_factory=list)


PROVIDERS: List[LLMProvider] = [
    LLMProvider(
        name="Cloudflare Workers AI",
        # Cloudflare's OpenAI-compatible endpoint. Account ID is baked into
        # the path, so we template it in from CLOUDFLARE_ACCOUNT_ID.
        api_url="https://api.cloudflare.com/client/v4/accounts/{CLOUDFLARE_ACCOUNT_ID}/ai/v1/chat/completions",
        default_model="@cf/meta/llama-3.3-70b-instruct-fp8-fast",
        api_key_env="CLOUDFLARE_API_TOKEN",
        needs_key=True,
        max_context_chars=60000,
        required_env=["CLOUDFLARE_ACCOUNT_ID"],
    ),
    LLMProvider(
        name="Groq",
        api_url="https://api.groq.com/openai/v1/chat/completions",
        default_model="llama-3.1-70b-versatile",
        api_key_env="GROQ_API_KEY",
        needs_key=True,
        max_context_chars=80000,
    ),
    LLMProvider(
        name="Together AI",
        api_url="https://api.together.xyz/v1/chat/completions",
        default_model="Qwen/Qwen2.5-Coder-72B-Instruct",
        api_key_env="TOGETHER_API_KEY",
        needs_key=True,
        max_context_chars=60000,
    ),
    LLMProvider(
        name="Mistral",
        api_url="https://api.mistral.ai/v1/chat/completions",
        default_model="codestral-latest",
        api_key_env="MISTRAL_API_KEY",
        needs_key=True,
        max_context_chars=50000,
    ),
    LLMProvider(
        name="Cerebras",
        api_url="https://api.cerebras.ai/v1/chat/completions",
        default_model="llama3.1-70b",
        api_key_env="CEREBRAS_API_KEY",
        needs_key=True,
        max_context_chars=60000,
    ),
    LLMProvider(
        name="Ollama (local)",
        api_url="http://localhost:11434/v1/chat/completions",
        default_model="qwen2.5-coder:14b",
        api_key_env=None,
        needs_key=False,
        max_context_chars=60000,
        request_style="openai",
    ),
    LLMProvider(
        name="LM Studio (local)",
        api_url="http://localhost:1234/v1/chat/completions",
        default_model="local-model",
        api_key_env=None,
        needs_key=False,
        max_context_chars=60000,
        request_style="openai",
    ),
    LLMProvider(
        name="HuggingFace Serverless (no key)",
        api_url="https://api-inference.huggingface.co/models/mistralai/Mistral-7B-Instruct-v0.3",
        default_model="mistralai/Mistral-7B-Instruct-v0.3",
        api_key_env=None,
        needs_key=False,
        max_context_chars=12000,   # smaller model, smaller context
        request_style="hf_serverless",
    ),
]


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class FreeLLMReviewer:
    """
    Tries available LLM providers in priority order.
    Falls all the way through to HuggingFace serverless if nothing else is configured.
    Returns the first successful review or None if everything fails.
    """

    def __init__(
        self,
        provider_overrides: Optional[Dict[str, str]] = None,
        max_retries: int = 2,
        timeout: int = 120,
    ) -> None:
        self.provider_overrides = provider_overrides or {}
        self.max_retries = max_retries
        self.timeout = timeout

    def review(self, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        for provider in PROVIDERS:
            # Skip keyed providers when no key is set
            if provider.needs_key:
                key = os.getenv(provider.api_key_env or "")
                if not key:
                    continue
            else:
                key = os.getenv(provider.api_key_env or "", "") or None

            # Providers that need extra env vars (e.g. Cloudflare account id)
            env_values: Dict[str, str] = {}
            missing_env = False
            for var in provider.required_env:
                val = os.getenv(var, "")
                if not val:
                    missing_env = True
                    break
                env_values[var] = val
            if missing_env:
                continue

            api_url = provider.api_url.format(**env_values) if env_values else provider.api_url

            model = self.provider_overrides.get(provider.name, provider.default_model)
            user_content = self._build_prompt(payload, provider.max_context_chars)

            try:
                if provider.request_style == "hf_serverless":
                    result = self._call_hf_serverless(provider, user_content, key, api_url)
                else:
                    result = self._call_openai_compat(provider, model, user_content, key, api_url)

                if result:
                    print(f"[llm:{provider.name}] confidence={result.get('confidence', 0):.2f}")
                    return self._validate(result)

            except requests.exceptions.ConnectionError:
                # Local server not running — skip silently
                if "localhost" in provider.api_url:
                    continue
                print(f"[llm:{provider.name}] connection error")
                continue
            except Exception as exc:
                print(f"[llm:{provider.name}] failed: {exc}")
                continue

        print("[llm] all providers exhausted")
        return None

    def _call_openai_compat(
        self,
        provider: LLMProvider,
        model: str,
        user_content: str,
        api_key: Optional[str],
        api_url: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        url = api_url or provider.api_url
        headers: Dict[str, str] = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        headers.update(provider.extra_headers)

        body = {
            "model": model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
            "temperature": 0.2,
            "max_tokens": 2000,
        }

        for attempt in range(self.max_retries + 1):
            resp = requests.post(
                url, headers=headers, json=body, timeout=self.timeout
            )
            if resp.status_code == 429:
                wait = int(resp.headers.get("retry-after", 15))
                print(f"[llm:{provider.name}] rate limited — waiting {min(wait, 60)}s")
                time.sleep(min(wait, 60))
                continue
            if resp.status_code == 503 and attempt < self.max_retries:
                # Model loading on HF — wait and retry
                time.sleep(20)
                continue
            resp.raise_for_status()
            choices = resp.json().get("choices", [])
            if not choices:
                raise ValueError("Empty choices")
            raw = choices[0].get("message", {}).get("content", "")
            return self._extract_json(raw)

        return None

    def _call_hf_serverless(
        self,
        provider: LLMProvider,
        user_content: str,
        api_key: Optional[str],
        api_url: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        HuggingFace serverless inference uses a different request format.
        We format the prompt as a single string since it doesn't use chat turns.
        We ask explicitly for JSON output in the prompt itself.
        """
        headers: Dict[str, str] = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        # HF serverless uses [INST] prompt format for Mistral
        full_prompt = (
            f"[INST] {SYSTEM_PROMPT}\n\n"
            f"Here is the repository data to review:\n\n"
            f"{user_content}\n\n"
            "Return ONLY the JSON object. No other text. [/INST]"
        )

        body = {
            "inputs": full_prompt,
            "parameters": {
                "max_new_tokens": 1500,
                "temperature": 0.2,
                "return_full_text": False,
            },
        }

        url = api_url or provider.api_url
        for attempt in range(self.max_retries + 1):
            resp = requests.post(
                url, headers=headers, json=body, timeout=self.timeout
            )
            if resp.status_code == 503:
                # Model is loading — HF returns estimated_time
                try:
                    wait = resp.json().get("estimated_time", 20)
                except Exception:
                    wait = 20
                print(f"[llm:HuggingFace] model loading — waiting {int(wait)}s")
                time.sleep(min(int(wait) + 2, 60))
                continue
            if resp.status_code == 429 and attempt < self.max_retries:
                time.sleep(30)
                continue
            resp.raise_for_status()

            data = resp.json()
            # HF returns a list: [{"generated_text": "..."}]
            if isinstance(data, list) and data:
                raw = data[0].get("generated_text", "")
            elif isinstance(data, dict):
                raw = data.get("generated_text", "")
            else:
                raw = str(data)

            result = self._extract_json(raw)
            if result:
                return result
            # HF model didn't return JSON — not unusual, just log and return None
            print(f"[llm:HuggingFace] no JSON in response: {raw[:200]!r}")
            return None

        return None

    @staticmethod
    def _build_prompt(payload: Dict[str, Any], max_chars: int) -> str:
        repo = payload.get("repo", {})
        snapshot = payload.get("snapshot", {})
        heuristics = payload.get("heuristics", [])
        contribution_rules = payload.get("contribution_rules", {})
        source_samples = payload.get("source_samples", {})
        similarity_score = payload.get("similarity_score", 0.0)

        parts: List[str] = []

        parts.append(f"# Repo: {repo.get('full_name', 'unknown')}")
        parts.append(f"Description: {repo.get('description') or 'none'}")
        parts.append(f"Language: {repo.get('language') or 'unknown'}")
        parts.append(f"Stars: {repo.get('stars', 0)}")
        parts.append(f"Topics: {', '.join(repo.get('topics', [])) or 'none'}")
        parts.append(f"License: {repo.get('license_name') or 'none'}")
        parts.append(f"Open issues: {repo.get('open_issues_count', 0)}")
        parts.append(f"Relevance: {similarity_score:.3f}")
        parts.append("")

        parts.append("# Structure")
        parts.append(f"Files: {snapshot.get('file_count', 0)}")
        parts.append(f"Root: {', '.join((snapshot.get('root_files') or [])[:20])}")
        parts.append(f"Dirs: {', '.join(snapshot.get('root_dirs') or [])}")
        ext_pairs = list((snapshot.get("top_extensions") or {}).items())[:8]
        parts.append(f"Extensions: {', '.join(f'{k}({v})' for k,v in ext_pairs)}")
        parts.append("")

        if heuristics:
            parts.append("# Static findings")
            for h in heuristics:
                parts.append(f"- {h}")
            parts.append("")

        contrib = (contribution_rules or {}).get("files", [])
        if contrib:
            parts.append(f"# Contribution files: {', '.join(contrib)}")
            parts.append("")

        base = "\n".join(parts)
        remaining = max_chars - len(base) - 500

        if source_samples and remaining > 1500:
            parts.append("# Source code samples")
            parts.append("Use these to make specific, evidence-backed observations.")
            parts.append("")
            chars_used = 0
            files_shown = 0
            for rel, content in source_samples.items():
                if chars_used >= remaining:
                    leftover = len(source_samples) - files_shown
                    if leftover:
                        parts.append(f"... {leftover} more files omitted")
                    break
                budget = min(len(content), remaining - chars_used, 4000)
                parts.append(f"## {rel}")
                parts.append(content[:budget])
                parts.append("")
                chars_used += budget + len(rel) + 10
                files_shown += 1

        parts.append("---")
        parts.append("Write the review now. Be specific, brief, and genuine.")
        return "\n".join(parts)

    @staticmethod
    def _extract_json(text: str) -> Optional[Dict[str, Any]]:
        if not text:
            return None
        text = text.strip()
        candidates = [text]
        fence = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, flags=re.DOTALL)
        if fence:
            candidates.append(fence.group(1))
        brace = re.search(r"(\{.*\})", text, flags=re.DOTALL)
        if brace:
            candidates.append(brace.group(1))
        for c in candidates:
            try:
                obj = json.loads(c)
                if isinstance(obj, dict):
                    return obj
            except json.JSONDecodeError:
                continue
        return None

    @staticmethod
    def _validate(obj: Dict[str, Any]) -> Dict[str, Any]:
        obj.setdefault("summary", "")
        obj.setdefault("praise", [])
        obj.setdefault("concerns", [])
        obj.setdefault("improvements", [])
        obj.setdefault("issue_title", "")
        obj.setdefault("issue_body", "")
        obj.setdefault("conversation_hook", "")
        try:
            obj["confidence"] = max(0.0, min(1.0, float(obj.get("confidence", 0.7))))
        except (TypeError, ValueError):
            obj["confidence"] = 0.7
        for key in ("praise", "concerns", "improvements"):
            val = obj[key]
            if not isinstance(val, list):
                obj[key] = [str(val)] if val else []
        return obj
