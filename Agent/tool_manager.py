from langchain_core.outputs import chat_result
import numpy as np
from dataclasses import dataclass, field
from pydantic import BaseModel
from langchain_core.tools import BaseTool
from langchain_openai import OpenAIEmbeddings, ChatOpenAI
from langchain_core.messages import HumanMessage, AIMessage, SystemMessage

import structlog
log = structlog.get_logger()


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
        # text-embedding-3-large: meaningfully better retrieval accuracy
        # than -3-small, at extra cost/latency that's negligible compared
        # to the LLM calls already happening per turn.
        self._embedder = OpenAIEmbeddings(model="text-embedding-3-large")
        self._router   = ChatOpenAI(model="gpt-5.4-nano", temperature=0).with_structured_output(RouterOutput, include_raw=True)
        self._describer  = ChatOpenAI(model="gpt-5.4-nano", temperature=0)

    # ── Registration ─────────────────────────────────────────
    async def register(self, tools: list[BaseTool], server: str, server_description: str | None = None):
        if server in self._registry:
            log.warning("server_already_registered", server=server)
            return

        # Auto-generate description from tool descriptions if not provided
        if not server_description:
            server_description = await self._generate_server_description(server, tools)

        self._server_descriptions[server] = server_description

        # Embed all tool descriptions for within-server filtering later
        tool_descriptions = [f"{t.name}: {t.description}" for t in tools]
        embeddings = await self._embedder.aembed_documents(tool_descriptions)

        self._registry[server] = [
            ToolEntry(tool=tool, server=server, embedding=np.array(emb))
            for tool, emb in zip(tools, embeddings)
        ]

        log.info("server_registered", server=server, tool_count=len(tools), tools=[t.name for t in tools])


    def unregister(self, server: str):
        removed = len(self._registry.pop(server, []))
        self._server_descriptions.pop(server, None)
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
                    model_name = getattr(result, "response_metadata", {}).get("model_name", "gpt-5.4-nano")
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
            return result.content if hasattr(result, 'content') else str(result)
        except Exception:
            # Fallback: join tool names
            return f"Service with tools: {', '.join(t.name for t in tools)}"


    # ── Stage 1: Server routing + query rewrite (fused) ──────
    async def route(self, messages: list) -> tuple[list[str] | None, str]:
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
                    "conversation with no action needed, just restate it plainly."
                )),
                HumanMessage(content=(
                    f"Available services:\n{server_list}\n\n"
                    f"Conversation (most recent last):\n{recent}\n\n"
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
                    model_name = getattr(raw_msg, "response_metadata", {}).get("model_name", "gpt-5.4-nano")
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


    # Similarity floor for within-server tool filtering. A tool below this
    # score is treated as "not actually relevant to this query" even if its
    # server was selected by the router — the router picks servers at a
    # coarser grain than individual tools, so the two stages can legitimately
    # disagree. This threshold lets stage 2 veto stage 1 rather than always
    # padding out to top_k regardless of fit.
    MIN_TOOL_SIMILARITY = 0.35

    # ── Stage 2: Within-server tool filtering ────────────────
    async def get_tools_for_servers(
        self,
        servers: list[str],
        query: str | None = None,
        top_k_per_server: int = 6,
        min_similarity: float = MIN_TOOL_SIMILARITY,
    ) -> list[BaseTool]:
        """
        Returns tools from the selected servers.

        If query is provided, uses embedding similarity within each server
        and returns up to top_k_per_server tools, but ONLY those clearing
        min_similarity. A server can legitimately contribute 0 tools here
        if the router flagged it but none of its tools actually fit the
        query — that's a signal worth logging, not a bug to paper over
        by always filling up to K.

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

            scores = []
            for entry in server_entries:
                cosine = float(
                    np.dot(query_emb, entry.embedding)
                    / (np.linalg.norm(query_emb) * np.linalg.norm(entry.embedding) + 1e-9)
                )
                scores.append((cosine, entry))

            scores.sort(reverse=True, key=lambda x: x[0])

            selected = [(s, e) for s, e in scores[:top_k_per_server] if s >= min_similarity]

            missed_tools = [
                f"{e.tool.name}: {round(s, 4)}" 
                for s, e in scores 
                if (s, e) not in selected
            ]

            if not selected:        
                top_score = scores[0][0] if scores else None
                log.info("no_tools_above_threshold", server=server, top_score=top_score)
            else:
                log.info(
                    "server_tools_filtered",
                    server=server,
                    query=query, # Added query here for quick cross-referencing
                    selected=len(selected),
                    of_candidates=len(scores), # Changed to show ALL candidates in the server
                    score_range=(round(selected[-1][0], 4), round(selected[0][0], 4)),
                    missed_tools=missed_tools # Added missed tools
                )

            result.extend(e.tool for _, e in selected)

        log.info("tools_selected", count=len(result), tools=[t.name for t in result])
        return result
