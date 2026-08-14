"""
Find_More_Like_This.py
-----------------------
Backend logic for the "Find More Like This" quick action.

Pipeline:

  1. FINGERPRINT (nano, via navigator_basic_llm(schema=_Fingerprint)):
     turn the page's extracted text into a compact fingerprint — topic,
     subtopics, content type, style — plus 2-3 good search queries.
     navigator_basic_llm's schema param wraps the model in
     with_structured_output, so this returns an already-validated
     _Fingerprint instance directly, no manual JSON parsing needed.

  2. SEARCH (no LLM — Tavily API): run those queries against Tavily's
     search API, which is purpose-built for feeding LLM pipelines. Unlike
     the earlier Bing/DuckDuckGo scraping attempts, this is a real,
     sanctioned API call (not scraping), so there's no bot-detection wall
     to hit. Tavily's free tier is 1,000 credits/month, no card required.
     Returns title + url + a relevance-ranked content snippet per result —
     already close to publish-ready, no extraction step needed.

  3. RANK (via navigator_smart_llm(schema=_RankResult)): hand
     Tavily's candidates (title + url + snippet) back to the model
     alongside the fingerprint, ask for exactly 5 best matches with a
     one-line reason each — same structured-output pattern as step 1.
     Tavily's own results are already relevance-ranked, but this step
     re-ranks specifically against the fingerprint's content_type/
     writing_style (which Tavily doesn't know about) and filters out
     anything that's a poor fit despite matching keywords.

History:
  - This originally scraped Bing directly with plain httpx, then switched
    to DuckDuckGo's no-JS HTML endpoint when Bing served a TLS/HTTP
    fingerprint-level bot-challenge page. DuckDuckGo held up briefly, then
    started serving an actual CAPTCHA ("select all squares containing a
    duck") after a burst of testing traffic — neither is fixable with
    header spoofing or request pacing, both are real anti-bot systems
    working as intended. It was then swapped to OpenAI's `web_search` tool
    ($10/1k calls) as a reliable-but-paid fallback. Tavily replaces that:
    it's free (real free tier, not a scraping workaround), reliable (a
    real API, not fighting bot detection), and was purpose-built for
    exactly this "feed search results to an LLM" pattern.
  - TAVILY_API_KEY is expected in the environment (same .env as
    OPENAI_API_KEY and whatever else configuration.py already loads).
  - We never open a tab, hidden or otherwise, in the user's browser for
    this — everything happens inside navigator_bridge.py.
  - Same request/response shape as Summarise_Page.py, so it slots into
    navigator_bridge.py the same way.
"""

import uuid
import asyncio
from typing import List, Optional

import structlog
from tavily import AsyncTavilyClient
from pydantic import BaseModel
from langchain_core.messages import SystemMessage, HumanMessage

import configuration

log = structlog.get_logger()

# ── FIND MORE LIKE THIS DATA MODELS ─────────────────────────────────────

class FindMoreLikeThisRequest(BaseModel):
    url: str
    title: str
    content: str
    # URLs already shown to the user in this find-more session (accumulated
    # client-side across "Find More" clicks). Excluded from both the search
    # candidate pool and the ranking step so repeat clicks never resurface
    # a link that's already on screen.
    exclude_urls: List[str] = []


class RelatedLink(BaseModel):
    title: str
    url: str
    reason: str  # one-line "why this is similar" the UI shows under the link


class FindMoreLikeThisResponse(BaseModel):
    results: List[RelatedLink]
    # Surfaced to the UI so it can show a friendly message instead of an
    # empty list if the scrape failed or search found nothing usable.
    error: Optional[str] = None


class _Fingerprint(BaseModel):
    """Internal only — not returned to the frontend. Passed as the schema
    to navigator_basic_llm(schema=...) for step 1, so the model's output
    is constrained/parsed directly into this shape rather than us hand-
    parsing a JSON string out of response.content."""
    topic: str
    subtopics: List[str]
    content_type: str
    writing_style: str
    search_queries: List[str]


class _RankResult(BaseModel):
    """Internal only — the schema passed to navigator_smart_llm(schema=...)
    for step 3. Wraps RelatedLink in the { "results": [...] } shape the
    ranking prompt asks for."""
    results: List[RelatedLink]


# ── STEP 1: FINGERPRINT ─────────────────────────────────────────────────

_FINGERPRINT_SYSTEM = SystemMessage(content=(
    "You analyze a web page and describe it so it can be used to find similar "
    "pages elsewhere on the web. You handle every kind of content: technical "
    "docs, casual articles, recipes, stories, news, tutorials, opinion pieces, "
    "reference material — anything.\n\n"
    "topic: a short phrase for the core subject.\n"
    "subtopics: 2-4 more specific themes or angles covered.\n"
    "content_type: one of technical_article, tutorial, recipe, story, news, "
    "opinion, reference, blog_post, other.\n"
    "writing_style: a short phrase, e.g. 'casual conversational', 'formal "
    "academic', 'step-by-step instructional'.\n"
    "search_queries: 2-3 short web search queries (3-6 words each) that would "
    "surface OTHER pages with similar topic, subject matter, and style — not "
    "this exact page. Queries should be general enough to find other sites, "
    "not so narrow they only match this one page or this one publisher."
))


async def _build_fingerprint(
    url: str, title: str, content: str, already_shown_count: int = 0
) -> _Fingerprint:
    # Passing the schema makes navigator_basic_llm return a
    # with_structured_output-wrapped model (see configuration.py) — the
    # call below returns an already-validated _Fingerprint instance
    # directly, not a chat message we'd need to parse JSON out of.
    llm = configuration.navigator_basic_llm(schema=_Fingerprint)

    extra_instruction = ""
    if already_shown_count > 0:
        extra_instruction = (
            f"\n\nNote: {already_shown_count} related pages have already been "
            "shown to the user from earlier queries on this same page. Generate "
            "search_queries that explore different angles, subtopics, or "
            "phrasings than an obvious first pass would — the goal is fresh "
            "candidates, not the same top hits again."
        )

    human_msg = HumanMessage(
        content=f"URL: {url}\nTitle: {title}\n\nContent:\n{content}{extra_instruction}"
    )

    fingerprint = None
    session_id = f"fingerprint_{uuid.uuid4().hex[:8]}"
    
    try:
        from usage_tracker import record_usage
        async for event in llm.astream_events([_FINGERPRINT_SYSTEM, human_msg], version="v2"):
            
            if event["event"] == "on_chat_model_end":
                output = event.get("data", {}).get("output")
                if output and hasattr(output, "usage_metadata") and output.usage_metadata:
                    usage = output.usage_metadata
                    model_name = output.response_metadata.get("model_name", "unknown")
                    
                    try:
                        record_usage(
                            dimension="navigator",
                            session_id=session_id,
                            model_name=model_name,
                            input_tokens=usage.get("input_tokens", 0),
                            output_tokens=usage.get("output_tokens", 0),
                            cached_input_tokens=usage.get("input_token_details", {}).get("cache_read_tokens", 0)
                        )
                    except Exception as rec_err:
                        log.warning("record_usage failed for fingerprint", error=str(rec_err))
                        
            elif event["event"] == "on_chain_end":
                data_out = event.get("data", {}).get("output")
                if isinstance(data_out, _Fingerprint):
                    fingerprint = data_out
                    
    except Exception as e:
        log.warning("Failed during fingerprint event stream tracking", error=str(e))
    
    if not fingerprint:
        fingerprint = await llm.ainvoke([_FINGERPRINT_SYSTEM, human_msg])

    return fingerprint


# ── STEP 2: SEARCH (Tavily API — real API, no scraping, no LLM) ─────────

_tavily_client = AsyncTavilyClient()  # reads TAVILY_API_KEY from env


def _normalize_url(url: str) -> str:
    """
    Normalize a URL for comparison so near-identical variants (http vs
    https, www. vs no-www, trailing slash, trailing query/fragment) are
    treated as the same page. Used both to de-dupe Tavily's results across
    multiple queries and as a hard backstop filter before results reach
    the user.
    """
    from urllib.parse import urlparse

    parsed = urlparse(url.strip())
    host = parsed.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    path = parsed.path.rstrip("/")
    return f"{host}{path}"


async def _search_one_query(query: str, max_results: int = 6) -> List[dict]:
    try:
        response = await _tavily_client.search(
            query=query,
            max_results=max_results,
            search_depth="basic",  # "advanced" costs more credits; basic is
                                    # plenty for a title+snippet candidate pool
        )
    except Exception as e:
        log.warning("Tavily search request failed", query=query, error=str(e))
        return []

    results = response.get("results", []) if isinstance(response, dict) else []
    return [
        {
            "title": r.get("title", ""),
            "url": r.get("url", ""),
            # Tavily's "content" field is the relevance-ranked snippet —
            # this is the text that renders below the title/link in the UI.
            "snippet": r.get("content", ""),
        }
        for r in results
        if r.get("url")
    ]


async def _search_all_queries(
    queries: List[str], source_url: str, exclude_urls: Optional[List[str]] = None
) -> List[dict]:
    """
    Run each fingerprint query against Tavily, merge and de-dupe candidates
    across queries. Queries run concurrently to keep latency down.

    `exclude_urls` — links already shown to the user in this session (from
    earlier "Find More" batches) — are seeded into the same dedup set as
    the source page itself, so a repeat search can't resurface something
    already on screen.
    """
    per_query_results = await asyncio.gather(
        *[_search_one_query(q) for q in queries],
        return_exceptions=True,
    )

    seen_urls = {_normalize_url(source_url)}
    seen_urls.update(_normalize_url(u) for u in (exclude_urls or []))

    candidates = []
    for result_set in per_query_results:
        if isinstance(result_set, Exception):
            continue
        for r in result_set:
            normalized = _normalize_url(r["url"])
            if normalized in seen_urls:
                continue
            seen_urls.add(normalized)
            candidates.append(r)

    return candidates


# ── STEP 3: RANK (via navigator_smart_llm) ──────────────────────────────

_RANK_SYSTEM = SystemMessage(content=(
    "You are given a description of a web page (the 'source') and a list of "
    "candidate pages found via web search. Pick the 5 candidates that best "
    "match the source's topic, subtopics, content type, and writing style. "
    "Prefer genuine topical/stylistic matches over pages that merely share a "
    "keyword. Exclude anything that looks like a login page, paywall splash, "
    "search results page, category/tag listing, homepage, or spam.\n\n"
    "For each of the 5 results, give a one-sentence reason for why it matches. "
    "Return fewer than 5 only if fewer than 5 good candidates exist. Use each "
    "candidate's given title and url exactly as provided — do not invent or "
    "modify them."
))


async def _rank_candidates(
    fingerprint: "_Fingerprint", candidates: List[dict], source_url: str,
    exclude_urls: Optional[List[str]] = None,
) -> List[RelatedLink]:
    if not candidates:
        return []

    candidates_block = "\n".join(
        f"- title: {c['title']}\n  url: {c['url']}\n  snippet: {c['snippet']}"
        for c in candidates
    )

    human_msg = HumanMessage(content=(
        f"SOURCE PAGE\n"
        f"topic: {fingerprint.topic}\n"
        f"subtopics: {', '.join(fingerprint.subtopics)}\n"
        f"content_type: {fingerprint.content_type}\n"
        f"writing_style: {fingerprint.writing_style}\n\n"
        f"CANDIDATES\n{candidates_block}"
    ))

    # Same pattern as _build_fingerprint — schema=_RankResult gets back an
    # already-validated instance, no manual JSON parsing needed.
    llm = configuration.navigator_smart_llm(schema=_RankResult)
    
    rank_result = None
    session_id = f"rank_{uuid.uuid4().hex[:8]}"
    
    try:
        from usage_tracker import record_usage
        async for event in llm.astream_events([_RANK_SYSTEM, human_msg], version="v2"):
            
            if event["event"] == "on_chat_model_end":
                output = event.get("data", {}).get("output")
                if output and hasattr(output, "usage_metadata") and output.usage_metadata:
                    usage = output.usage_metadata
                    model_name = output.response_metadata.get("model_name", "unknown")
                    
                    try:
                        record_usage(
                            dimension="navigator",
                            session_id=session_id,
                            model_name=model_name,
                            input_tokens=usage.get("input_tokens", 0),
                            output_tokens=usage.get("output_tokens", 0),
                            cached_input_tokens=usage.get("input_token_details", {}).get("cache_read_tokens", 0)
                        )
                    except Exception as rec_err:
                        log.warning("record_usage failed for rank", error=str(rec_err))
                        
            elif event["event"] == "on_chain_end":
                data_out = event.get("data", {}).get("output")
                if isinstance(data_out, _RankResult):
                    rank_result = data_out
                    
    except Exception as e:
        log.warning("Failed during rank event stream tracking", error=str(e))
        
    if not rank_result:
        rank_result = await llm.ainvoke([_RANK_SYSTEM, human_msg])

    results = rank_result.results

    # Hard backstop: the source page and anything already shown can never
    # reach the user even if the ranking model slips, since candidates were
    # already deduped before this step, but this guarantees it regardless.
    banned = {_normalize_url(source_url)}
    banned.update(_normalize_url(u) for u in (exclude_urls or []))
    filtered = [r for r in results if _normalize_url(r.url) not in banned]

    return filtered[:5]


# ── ORCHESTRATION ─────────────────────────────────────────────────────────

async def process_find_more_like_this(req: FindMoreLikeThisRequest) -> FindMoreLikeThisResponse:
    """
    Full pipeline for the "Find More Like This" quick action. Mirrors
    process_summarise_page's shape: one public async function, LLM pulled
    fresh from configuration, errors caught and turned into a response the
    frontend can render rather than a raw 500.
    """
    log.info(
        "Find more like this: starting",
        url=req.url,
        content_length=len(req.content),
        already_shown=len(req.exclude_urls),
    )

    # Step 1 — fingerprint
    try:
        fingerprint = await _build_fingerprint(
            req.url, req.title, req.content, already_shown_count=len(req.exclude_urls)
        )
    except Exception as e:
        log.error("Find more like this: fingerprint step failed", error=str(e))
        return FindMoreLikeThisResponse(
            results=[],
            error="Couldn't analyze this page's content. Try again?",
        )

    log.info(
        "Find more like this: fingerprint built",
        topic=fingerprint.topic,
        content_type=fingerprint.content_type,
        queries=fingerprint.search_queries,
    )

    # Step 2 — search (Tavily API, no LLM)
    candidates = await _search_all_queries(
        fingerprint.search_queries, req.url, exclude_urls=req.exclude_urls
    )

    if not candidates:
        log.warning(
            "Find more like this: no search candidates found",
            url=req.url,
            already_shown=len(req.exclude_urls),
        )
        error_msg = (
            "No new related pages found — you may have seen them all already."
            if req.exclude_urls
            else "Couldn't find related pages right now. Search may be temporarily unavailable."
        )
        return FindMoreLikeThisResponse(results=[], error=error_msg)

    log.info("Find more like this: candidates found", count=len(candidates))

    # Step 3 — rank (nano)
    try:
        ranked = await _rank_candidates(
            fingerprint, candidates, source_url=req.url, exclude_urls=req.exclude_urls
        )
    except Exception as e:
        log.error("Find more like this: ranking step failed", error=str(e))
        return FindMoreLikeThisResponse(
            results=[],
            error="Found some pages but couldn't rank them. Try again?",
        )

    if not ranked:
        return FindMoreLikeThisResponse(
            results=[],
            error="No strong matches found for this page.",
        )

    log.info("Find more like this: done", result_count=len(ranked))
    return FindMoreLikeThisResponse(results=ranked)