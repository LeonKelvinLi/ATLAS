"""Perplexity-backed web research helper.

Reads PERPLEXITY_API_KEY from the environment or the gitignored .env. The key
is NEVER written to source, printed, or logged - this repository is public, so
a leaked key would be published the moment anything is pushed. See
selfcheck.py's secret scan, which fails the audit if a key ever reaches a
tracked file.

Usage:
  .venv\\Scripts\\python.exe research_web.py --check
  .venv\\Scripts\\python.exe research_web.py "IQ Option binary strike mechanics"
  .venv\\Scripts\\python.exe research_web.py --agent "compare X and Y in depth"
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
SEARCH_URL = "https://api.perplexity.ai/search"
AGENT_URL = "https://api.perplexity.ai/v1/responses"
# Advanced Deep Research runs multi-step sourcing and routinely takes several
# minutes; the old 90 s ceiling killed it mid-flight and surfaced as a bare
# socket timeout. Search stays fast, so the two get different budgets.
SEARCH_TIMEOUT_S = 90
AGENT_TIMEOUT_S = 1800


def load_key() -> str | None:
    """Environment first, then the gitignored .env. Never returns a default,
    never logs the value."""
    key = os.environ.get("PERPLEXITY_API_KEY")
    if key:
        return key.strip()
    from runtime_paths import ENV_FILE

    env = ENV_FILE
    if env.exists():
        for line in env.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if line.startswith("PERPLEXITY_API_KEY") and "=" in line:
                return line.split("=", 1)[1].strip()
    return None


def _post(url: str, payload: dict, key: str,
          timeout: int = SEARCH_TIMEOUT_S) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {key}",
                 "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:400]
        # Deliberately does not echo the request headers - they carry the key.
        raise SystemExit(f"HTTP {exc.code} from {url}: {body}")
    except urllib.error.URLError as exc:
        raise SystemExit(f"network error contacting {url}: {exc.reason}")


def search(query: str, key: str, max_results: int = 5,
           max_tokens_per_page: int = 512) -> dict:
    return _post(SEARCH_URL, {"query": query, "max_results": max_results,
                              "max_tokens_per_page": max_tokens_per_page},
                 key, timeout=SEARCH_TIMEOUT_S)


# Presets accepted by /v1/responses. "fast-search" is cheap and shallow;
# deep research trades minutes and tokens for multi-step sourcing, which is
# what a question like broker strike mechanics actually needs. The exact
# preset names are Perplexity's, so they are passed through rather than
# hardcoded to one guess - --preset takes whatever their docs list.
DEEP_PRESET = "advanced-deep-research"   # the console's "Advanced Deep Research"
FAST_PRESET = "fast-search"


def ask(prompt: str, key: str, preset: str = DEEP_PRESET,
        model: str | None = None, timeout: int = AGENT_TIMEOUT_S) -> dict:
    payload: dict = {"preset": preset, "input": prompt}
    if model:
        payload["model"] = model
    return _post(AGENT_URL, payload, key, timeout=timeout)


def format_agent(payload: dict) -> str:
    """Pull the written answer and its sources out of a /v1/responses reply.

    The raw object interleaves search queries, per-result snippets and the
    final message; dumping it is unreadable, and the snippets are often
    non-English pages that crowd out the actual answer. Pure - unit-tested."""
    if not isinstance(payload, dict):
        return str(payload)[:2000]
    if payload.get("error"):
        return f"API error: {payload['error']}"

    texts, sources = [], []
    for item in payload.get("output") or []:
        if not isinstance(item, dict):
            continue
        for res in item.get("results") or []:
            if isinstance(res, dict) and res.get("url"):
                sources.append((res.get("title") or "", res["url"]))
        content = item.get("content")
        if isinstance(content, str):
            texts.append(content)
        elif isinstance(content, list):
            for c in content:
                if isinstance(c, dict) and isinstance(c.get("text"), str):
                    texts.append(c["text"])
                elif isinstance(c, str):
                    texts.append(c)
        elif isinstance(item.get("text"), str):
            texts.append(item["text"])

    out = []
    if texts:
        out.append("\n\n".join(t.strip() for t in texts if t.strip()))
    else:
        out.append("(no written answer in the response; model="
                   f"{payload.get('model')}, status={payload.get('status')})")
    if sources:
        seen, lines = set(), []
        for title, url in sources:
            if url in seen:
                continue
            seen.add(url)
            lines.append(f"  - {title[:80]} {url}" if title else f"  - {url}")
        out.append(f"\nSOURCES ({len(seen)}):\n" + "\n".join(lines[:25]))
    usage = payload.get("usage") or {}
    if usage:
        out.append(f"\n[model={payload.get('model')} "
                   f"tokens={usage.get('total_tokens', '?')}]")
    return "\n".join(out)


def format_search(payload: dict) -> str:
    results = payload.get("results") or payload.get("data") or []
    if not results:
        return json.dumps(payload, indent=2)[:2000]
    lines = []
    for i, r in enumerate(results, 1):
        title = r.get("title") or r.get("name") or "(untitled)"
        url = r.get("url") or r.get("link") or ""
        snippet = (r.get("snippet") or r.get("text") or "").strip()
        lines.append(f"{i}. {title}\n   {url}\n   {snippet[:400]}")
    return "\n".join(lines)


def main() -> int:
    # Windows consoles default to cp1252, and research answers routinely
    # contain en-dashes, Unicode minus signs and non-Latin source titles. A
    # single unencodable character used to crash the print AFTER a multi-minute
    # (billed) deep-research call had already succeeded, losing the whole
    # result. Never let formatting discard a completed answer.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("query", nargs="*", help="search query")
    ap.add_argument("--agent", action="store_true",
                    help="use the reasoning endpoint instead of plain search")
    ap.add_argument("--preset", default=DEEP_PRESET,
                    help=f"reasoning preset (default {DEEP_PRESET}; "
                         f"{FAST_PRESET} is the cheap shallow one)")
    ap.add_argument("--model", default=None,
                    help="override the model the preset would choose")
    ap.add_argument("--fast", action="store_true",
                    help=f"shorthand for --preset {FAST_PRESET}")
    ap.add_argument("--max-results", type=int, default=5)
    ap.add_argument("--json", action="store_true", help="raw JSON output")
    ap.add_argument("--check", action="store_true",
                    help="report whether a key is configured, without using it")
    ap.add_argument("--out", default=None,
                    help="also write the raw JSON here (utf-8), so a costly "
                         "answer survives any later formatting problem")
    args = ap.parse_args()

    key = load_key()
    if args.check:
        if key:
            # Length and prefix only - never the key itself.
            print(f"PERPLEXITY_API_KEY configured (prefix {key[:4]}..., "
                  f"{len(key)} chars)")
            return 0
        print("PERPLEXITY_API_KEY not set. Add it to the gitignored .env:\n"
              '  echo PERPLEXITY_API_KEY=your-key-here >> .env', file=sys.stderr)
        return 1
    if not key:
        print("PERPLEXITY_API_KEY not set (see --check)", file=sys.stderr)
        return 1
    if not args.query:
        print("no query given", file=sys.stderr)
        return 2

    q = " ".join(args.query)
    preset = FAST_PRESET if args.fast else args.preset
    payload = (ask(q, key, preset=preset, model=args.model) if args.agent
               else search(q, key, args.max_results))
    # Persist BEFORE formatting: the raw answer is the expensive artefact and
    # must not depend on the pretty-printer succeeding.
    if args.out:
        Path(args.out).write_text(json.dumps(payload, indent=2,
                                             ensure_ascii=False),
                                  encoding="utf-8")
    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(format_agent(payload) if args.agent else format_search(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
