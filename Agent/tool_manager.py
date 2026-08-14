import re

from langchain_core.outputs import chat_result
import numpy as np
from dataclasses import dataclass, field
from pydantic import BaseModel
from langchain_core.tools import BaseTool
from langchain_openai import OpenAIEmbeddings, ChatOpenAI
from langchain_core.messages import HumanMessage, AIMessage, SystemMessage
from rank_bm25 import BM25Okapi

from shared_utils import content_to_text

import structlog
log = structlog.get_logger()


_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _tokenize(text: str) -> list[str]:
    """
    Minimal, dependency-free tokenizer shared by BM25 indexing and querying.
    Lowercases and splits on non-alphanumeric runs. No stemming/stopwording —
    tool descriptions and user queries are short enough that naive overlap
    is already a strong, cheap signal, and stemming adds a dependency for
    marginal gain at this text length.
    """
    return _TOKEN_RE.findall(text.lower())


def _raw_fallback_query(messages: list) -> str:
    """
    Best-effort embedding query when the router-fused rewrite isn't
    available (e.g. router call itself failed). Not as clean as the
    LLM-rewritten intent, but better than nothing for stage-2 similarity.
    """
    return " ".join(
        m.content
        for m in messages[-4:]
        if isinstance(m, (HumanMessage, AIMessage)) and isinstance(m.content, str)
    )


# Pydantic schema for router output.
# Fused: one LLM call returns both the server routing decision AND a
# clean, self-contained rewrite of the user's current intent. The
# rewrite is what gets embedded for within-server tool filtering —
# raw message concatenation embeds poorly against tool descriptions
# (pronouns, follow-ups, conversational scaffolding), a rewritten
# single-sentence intent embeds much better. Fusing into one call
# avoids paying extra latency for a separate rewrite step.
class RouterOutput(BaseModel):
    server_names: list[str]
    rewritten_query: str  # self-contained restatement of current intent


# ToolEntry
@dataclass
class ToolEntry:
    tool: BaseTool
    server: str
    embedding: np.ndarray = field(default=None, repr=False)
    tokens: list[str] = field(default_factory=list, repr=False)  # for BM25


# ToolManager
class ToolManager:

    def __init__(self):
        # Indexed by server name so per-server lookups (registration,
        # unregistration, within-server filtering) are O(1) into the
        # relevant bucket instead of a linear scan over every tool from
        # every connected server. Matters once you're at hundreds of
        # tools across many MCP servers.
        self._registry: dict[str, list[ToolEntry]] = {}
        self._server_descriptions: dict[str, str] = {}
        self._bm25_index: dict[str, BM25Okapi] = {}  # server -> lexical index, built alongside embeddings
        # text-embedding-3-large: meaningfully better retrieval accuracy
        # than -3-small, at extra cost/latency that's negligible compared
        # to the LLM calls already happening per turn.
        self._embedder = OpenAIEmbeddings(model="text-embedding-3-large")
        self._router   = ChatOpenAI(
            model="gpt-5.6-luna",
            use_responses_api=True,
            reasoning_effort="medium",
        ).with_structured_output(RouterOutput, include_raw=True)
        self._describer = ChatOpenAI(
            model="gpt-5.6-luna",
            use_responses_api=True,
            reasoning_effort="medium",
        )

    # ── Registration ─────────────────────────────────────────
    async def register(self, tools: list[BaseTool], server: str, server_description: str | None = None):
        if server in self._registry:
            log.warning("server_already_registered", server=server)
            return

        # Auto-generate description from tool descriptions if not provided
        if not server_description:
            server_description = await self._generate_server_description(server, tools)

        self._server_descriptions[server] = server_description

        # Embed all tool descriptions for within-server filtering later.
        #
        # Server name is prepended because bare tool descriptions are often
        # terse ("Search the catalog") and don't restate the domain — the
        # embedding for "search_products: Search the catalog" sits closer to
        # unrelated short phrases than to a query like "search for chocolate"
        # unless the domain word ("Instamart", "grocery") is present in the
        # embedded text itself. This is a one-time cost at registration, not
        # per-turn, so it's effectively free.
        tool_descriptions = [
            f"{server} {t.name}: {t.description}" for t in tools
        ]
        embeddings = await self._embedder.aembed_documents(tool_descriptions)

        # Build a per-server BM25 lexical index alongside the embeddings.
        #
        # Dense embeddings are tuned for semantic/topical similarity between
        # natural-language sentences, and can systematically under-rank a
        # perfectly on-topic tool description if its surface grammar looks
        # like a generic terse API blurb ("Search for grocery products...")
        # rather than "sounding like" the user's phrasing — observed in
        # practice: a description containing "search", "grocery", "products",
        # "pricing" scored BELOW topically unrelated tools for a query about
        # searching for a priced grocery item. BM25 doesn't have this failure
        # mode: it directly rewards literal token overlap, so it acts as an
        # independent signal that catches exactly the cases where embedding
        # geometry misleads. Combined in get_tools_for_servers() as a hybrid
        # score — this is generic (no tool/server-specific tuning) and applies
        # identically to every server or tool registered, present or future.
        token_lists = [_tokenize(desc) for desc in tool_descriptions]
        self._bm25_index[server] = BM25Okapi(token_lists) if token_lists else None

        self._registry[server] = [
            ToolEntry(tool=tool, server=server, embedding=np.array(emb), tokens=toks)
            for tool, emb, toks in zip(tools, embeddings, token_lists)
        ]

        log.info("server_registered", server=server, tool_count=len(tools), tools=[t.name for t in tools])


    def unregister(self, server: str):
        removed = len(self._registry.pop(server, []))
        self._server_descriptions.pop(server, None)
        self._bm25_index.pop(server, None)
        log.info("server_unregistered", server=server, removed=removed)


    @property
    def loaded_servers(self) -> list[str]:
        return list(self._server_descriptions.keys())

    @property
    def all_tools(self) -> list[BaseTool]:
        return [entry.tool for entries in self._registry.values() for entry in entries]

    @property
    def tool_map(self) -> dict[str, BaseTool]:
        """name -> tool, across all registered servers. O(1) lookup for
        callers (agent.py's main_node / tool_executor_node) instead of
        rebuilding this dict from a flat scan on every single call."""
        return {
            entry.tool.name: entry.tool
            for entries in self._registry.values()
            for entry in entries
        }

    def server_for_tool(self, tool_name: str) -> str | None:
        """Which server a given tool name belongs to, or None if unknown.
        Used by the router-failure fallback in agent.py to recover
        'which servers were used last turn' from tool-call history."""
        for server, entries in self._registry.items():
            if any(e.tool.name == tool_name for e in entries):
                return server
        return None

    
    async def _generate_server_description(self, server: str, tools: list[BaseTool]) -> str:
        """
        Auto-generates a one-line server description from tool descriptions.
        Called at registration time — runs once, zero ongoing maintenance.
        """
        tool_summary = "\n".join(
            f"- {t.name}: {t.description}"
            for t in tools
        )
        try:
            result = await self._describer.ainvoke([
                SystemMessage(content=(
                    "You are writing a description of a service for an AI router.\n"
                    "The router reads this description to decide whether this service "
                    "is needed for a given user request.\n\n"
                    "Write a clear, comprehensive description covering:\n"
                    "- What this service is for (the main purpose)\n"
                    "- What kinds of user requests it handles\n"
                    "- What actions it can perform\n"
                    "- What it explicitly does NOT handle (if relevant)\n\n"
                    "Be thorough enough that the router can confidently include or "
                    "exclude this service. Use plain English. No bullet points — "
                    "write in flowing prose. 3-5 sentences is ideal."
                )),
                HumanMessage(content=(
                    f"Service name: {server}\n\n"
                    f"Tools available:\n{tool_summary}\n\n"
                    "Describe this service."
                ))
            ])
            if hasattr(result, "usage_metadata") and result.usage_metadata:
                try:
                    from usage_tracker import record_usage
                    usage_meta = result.usage_metadata
                    model_name = getattr(result, "response_metadata", {}).get("model_name", "gpt-5.6-luna")
                    msg_id = getattr(result, "id", None)
                    record_usage(
                        dimension="agent",
                        session_id=f"server_desc_{server}",
                        model_name=model_name,
                        input_tokens=usage_meta.get("input_tokens", 0),
                        output_tokens=usage_meta.get("output_tokens", 0),
                        cached_input_tokens=usage_meta.get("input_token_details", {}).get("cache_read_tokens", 0),
                        message_id=msg_id
                    )
                except Exception as rec_err:
                    log.warning("record_usage failed for server description generation", error=str(rec_err))

            # Router returns ServerSelection, but we need raw text here
            # Use a separate simple LLM call for this
            return content_to_text(result.content) if hasattr(result, 'content') else str(result)
        except Exception:
            # Fallback: join tool names
            return f"Service with tools: {', '.join(t.name for t in tools)}"


    # ── Stage 1: Server routing + query rewrite (fused) ──────
    async def route(self, messages: list, user_preferences: str | None = None) -> tuple[list[str] | None, str]:
        """
        Single cheap LLM call that reads the conversation and returns:
          - which servers are needed right now (routing decision)
          - a clean, self-contained rewrite of the user's current intent
            (used later as the embedding query for within-server filtering)

        Fusing these into one call means the query-rewrite step costs no
        extra latency over the routing call that already existed.

        Return contract — IMPORTANT, callers must distinguish these:
          - server_names == []   -> genuinely no tools needed (pure
                                     conversation: greeting, thanks, etc).
                                     Trust this. Zero tools is correct.
          - server_names is None -> the router call itself failed
                                     (exception, bad output). This is NOT
                                     the same as "no tools needed" and must
                                     NOT be treated as such by the caller.
                                     The caller decides the fallback
                                     (bounded, never "all tools").

        The rewritten_query is always returned best-effort: on router
        failure we fall back to a raw last-message string so downstream
        embedding still has *something* to work with, even though the
        server list is None.
        """
        if not self._server_descriptions:
            return [], _raw_fallback_query(messages)

        server_list = "\n".join(
            f"- {server}: {desc}"
            for server, desc in self._server_descriptions.items()
        )

        recent = "\n".join(
            f"{type(m).__name__}: {m.content}"
            for m in messages[-8:]
            if isinstance(m, (HumanMessage, AIMessage))
            and isinstance(m.content, str)
        )

        preferences_block = (
            f"\n\nRelevant user preferences (use these to make the rewritten query more specific):\n{user_preferences}"
            if user_preferences
            else ""
        )

        try:
            res = await self._router.ainvoke([
                SystemMessage(content=(
                    "You are a service router and query-rewriter for an AI assistant.\n\n"
                    "TASK 1 — server_names:\n"
                    "Return the names of services needed to respond to the user's "
                    "CURRENT request.\n"
                    "- Only include services where an action or data lookup is "
                    "genuinely needed RIGHT NOW\n"
                    "- Ignore services mentioned only as past context or in passing\n"
                    "- Return an empty list for pure conversation: greetings, "
                    "thank-yous, general questions that need no external data\n"
                    "- Return only names exactly as they appear in the list\n"
                    "- When unsure between one or two services, include both\n\n"
                    "TASK 2 — rewritten_query:\n"
                    "Rewrite the user's current request as ONE self-contained "
                    "sentence describing what they want done right now. Resolve "
                    "pronouns and references to earlier turns (e.g. 'the other one', "
                    "'reply to her', 'do it again') into concrete terms using the "
                    "conversation context. Do not include past-tense completed "
                    "actions or unrelated history. If the message is pure "
                    "conversation with no action needed, just restate it plainly.\n"
                    "IMPORTANT: If user preferences are provided, incorporate the "
                    "relevant ones into the rewritten query to make it more specific "
                    "(e.g. preferred brand, price range, dietary restrictions)."
                )),
                HumanMessage(content=(
                    f"Available services:\n{server_list}\n\n"
                    f"Conversation (most recent last):\n{recent}"
                    f"{preferences_block}\n\n"
                    "Which services are needed right now, and what is the "
                    "user's current intent as one self-contained sentence?"
                ))
            ])
            result = res["parsed"]
            raw_msg = res.get("raw")
            if raw_msg and hasattr(raw_msg, "usage_metadata") and raw_msg.usage_metadata:
                try:
                    from usage_tracker import record_usage
                    usage_meta = raw_msg.usage_metadata
                    model_name = getattr(raw_msg, "response_metadata", {}).get("model_name", "gpt-5.6-luna")
                    msg_id = getattr(raw_msg, "id", None)
                    record_usage(
                        dimension="agent",
                        session_id="tool_router",
                        model_name=model_name,
                        input_tokens=usage_meta.get("input_tokens", 0),
                        output_tokens=usage_meta.get("output_tokens", 0),
                        cached_input_tokens=usage_meta.get("input_token_details", {}).get("cache_read_tokens", 0),
                        message_id=msg_id
                    )
                except Exception as rec_err:
                    log.warning("record_usage failed for tool router", error=str(rec_err))

            valid = set(self._server_descriptions.keys())
            selected = [s for s in result.server_names if s in valid]
            query = result.rewritten_query or _raw_fallback_query(messages)

            log.info("relevant_servers", selected=selected or "none_conversational", rewritten_query=query)
            return selected, query

        except Exception as e:
            # Router genuinely failed. Signal this distinctly from "no
            # tools needed" — caller must NOT fall back to all_tools here.
            log.warning("server_router_failed", error=str(e))
            return None, _raw_fallback_query(messages)


    # Absolute floor on the HYBRID score — this is now the PRIMARY gate.
    #
    # Earlier version used a tight relative-to-top-score margin (0.08) as the
    # primary gate. That failed in practice: when a query needs MULTIPLE
    # tools (e.g. "select home address AND search chocolate"), one tool
    # (search_products) can score very high because it shares many literal
    # query tokens, which drags the "acceptable zone" up with it and cuts
    # other genuinely-relevant tools (get_addresses) that scored fine in
    # absolute terms (~0.55) but weren't within 0.08 of the top (~0.76).
    # Verified against real logged score distributions: junk tools
    # (report_error, confirm_order, check_payment_status, get_delivery_status,
    # get_payment_options) consistently land in the 0.15-0.34 hybrid range,
    # while genuinely relevant tools land 0.32-0.76+, across different query
    # shapes. 0.30 sits in that empirical gap. Revisit if you register a
    # server with much denser/sparser tool overlap and see it mis-cut again.
    MIN_HYBRID_SCORE = 0.30

    # Relative margin — now a SECONDARY, loose safety net, not the primary
    # gate. Only matters when the top score itself is unusually low (e.g. no
    # tool in the server is a great match) — this stays wide enough that it
    # essentially never fires when top scores are healthy (0.6-0.8, as seen
    # in practice), so it stops fighting the absolute floor above.
    RELATIVE_MARGIN = 0.35

    # Weight given to the embedding (semantic) score vs. the BM25 (lexical)
    # score in the combined ranking. Embeddings generally win on paraphrase/
    # synonym matches ("cheap" ~ "budget"); BM25 generally wins when the
    # query and description share literal, distinctive tokens but embedding
    # geometry doesn't reflect it (e.g. two short, structurally-similar API
    # blurbs land close together in embedding space regardless of topic).
    # 0.6/0.4 favors embeddings as the primary signal while letting lexical
    # overlap veto/rescue cases the embedding gets wrong. Tune empirically.
    EMBEDDING_WEIGHT = 0.6

    # ── Stage 2: Within-server tool filtering ────────────────
    async def get_tools_for_servers(
        self,
        servers: list[str],
        query: str | None = None,
        top_k_per_server: int = 6,
        min_similarity: float = MIN_HYBRID_SCORE,
        relative_margin: float = RELATIVE_MARGIN,
        embedding_weight: float = EMBEDDING_WEIGHT,
    ) -> list[BaseTool]:
        """
        Returns tools from the selected servers.

        If query is provided, ranks tools within each server using a HYBRID
        of two independent signals and returns up to top_k_per_server tools,
        but only those clearing a floor relative to that server's own top
        score:
          - cosine similarity between query and tool-description embeddings
            (catches semantic/paraphrase matches)
          - BM25 lexical overlap between query and tool-description tokens
            (catches literal keyword matches embeddings sometimes miss,
            e.g. when a terse tool description and the query share distinctive
            words but land far apart in embedding space anyway)
        Both signals are normalized to [0, 1] per-server before blending, so
        neither dominates purely because of its own scale.

        This is generic by construction: no server- or tool-specific tuning,
        just two signals computed identically for every registered tool from
        its name/description text, so it applies unchanged to any server
        added in the future.

        A server can legitimately contribute 0 tools here if the router
        flagged it but none of its tools actually fit the query — that's a
        signal worth logging, not a bug to paper over by always filling up
        to K.

        If no query, returns all tools from the selected servers
        (no ranking signal available to filter on).
        """
        if not servers:
            return []

        result: list[BaseTool] = []

        for server in servers:
            server_entries = self._registry.get(server, [])

            if not server_entries:
                continue

            if not query:
                # No ranking signal — take everything from this server.
                result.extend(e.tool for e in server_entries)
                continue

            query_emb = np.array(await self._embedder.aembed_query(query))
            query_tokens = _tokenize(query)

            bm25 = self._bm25_index.get(server)
            raw_bm25_scores = (
                bm25.get_scores(query_tokens) if bm25 is not None
                else np.zeros(len(server_entries))
            )
            # BM25 scores are unbounded (roughly 0-10+ depending on corpus
            # size/term rarity), unlike cosine's natural [-1, 1]. Min-max
            # normalize within this server+query so it's comparable to cosine
            # before blending. A single-tool server or an all-zero-overlap
            # query collapses the range to 0 — handled via the +1e-9 guard.
            bm25_min, bm25_max = float(raw_bm25_scores.min()), float(raw_bm25_scores.max())
            bm25_range = bm25_max - bm25_min + 1e-9

            scores = []
            for entry, raw_bm25 in zip(server_entries, raw_bm25_scores):
                cosine = float(
                    np.dot(query_emb, entry.embedding)
                    / (np.linalg.norm(query_emb) * np.linalg.norm(entry.embedding) + 1e-9)
                )
                bm25_norm = (float(raw_bm25) - bm25_min) / bm25_range
                hybrid = embedding_weight * cosine + (1 - embedding_weight) * bm25_norm
                scores.append((hybrid, cosine, bm25_norm, entry))

            scores.sort(reverse=True, key=lambda x: x[0])

            top_score = scores[0][0] if scores else 0.0
            dynamic_floor = max(min_similarity, top_score - relative_margin)

            selected = [
                (h, c, b, e) for h, c, b, e in scores[:top_k_per_server]
                if h >= dynamic_floor
            ]

            missed_tools = [
                f"{e.tool.name}: hybrid={round(h, 4)} cos={round(c, 4)} bm25={round(b, 4)}"
                for h, c, b, e in scores
                if (h, c, b, e) not in selected
            ]

            if not selected:
                # Only reachable if this server had zero tools at all.
                log.info("no_tools_above_threshold", server=server, top_score=None)
            else:
                log.info(
                    "server_tools_filtered",
                    server=server,
                    query=query, # Added query here for quick cross-referencing
                    selected=len(selected),
                    of_candidates=len(scores), # Changed to show ALL candidates in the server
                    score_range=(round(selected[-1][0], 4), round(selected[0][0], 4)),
                    dynamic_floor=round(dynamic_floor, 4), # visibility into why the cut landed where it did
                    missed_tools=missed_tools # Added missed tools
                )

            result.extend(e.tool for _, _, _, e in selected)

        log.info("tools_selected", count=len(result), tools=[t.name for t in result])
        return result