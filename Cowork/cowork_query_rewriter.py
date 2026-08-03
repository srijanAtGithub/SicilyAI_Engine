"""
cowork_query_rewriter.py
------------------------
Single-pass pre-RAG Query Rewriter for Sicily Cowork.
Transforms a conversational user prompt into a single, contextually rich search query.
"""

from pydantic import BaseModel, Field
from langchain_openai import ChatOpenAI
from langchain_core.prompts import ChatPromptTemplate
import structlog

log = structlog.get_logger()


class SingleRewrittenQuery(BaseModel):
    reasoning: str = Field(
        description="Brief explanation of what conversational noise was stripped and what explicit terms were added/clarified."
    )
    rewritten_query: str = Field(
        description="A single, context-dense search query containing all key terms, explicit intent, and zero conversational fluff."
    )


# System prompt enforcing full context retention in a single query
REWRITE_PROMPT = ChatPromptTemplate.from_messages([
    ("system", 
     "You are an expert search-query optimizer for a local document and filesystem search engine.\n"
     "Your sole job is to rewrite a conversational or vague user prompt into a SINGLE, highly effective search query.\n\n"
     "Rules:\n"
     "1. RETAIN FULL CONTEXT: Keep every critical detail, entity, timeframe, or specific concept mentioned by the user.\n"
     "2. STRIP CONVERSATIONAL NOISE: Remove filler words like 'can you tell me', 'where did I put', 'I want to see', 'please find'.\n"
     "3. MAKE IMPLICIT INTENT EXPLICIT: If the user hints at a keyword or concept, spell it out explicitly so both keyword (TF-IDF) and vector search engines hit it.\n"
     "4. DO NOT generate multiple options. Return exactly ONE consolidated search string."),
    ("human", "{query}")
])


def rewrite_query(query: str, llm_model: str = "gpt-4o-mini") -> str:
    """
    Transforms a single user query into a single context-rich search query string.
    Falls back to returning the original query if the call fails.
    """
    try:
        llm = ChatOpenAI(model=llm_model, temperature=0.0)
        structured_llm = llm.with_structured_output(SingleRewrittenQuery)
        
        chain = REWRITE_PROMPT | structured_llm
        result: SingleRewrittenQuery = chain.invoke({"query": query})
        
        log.debug(
            "rag.query_rewritten", 
            original=query, 
            rewritten=result.rewritten_query, 
            reasoning=result.reasoning
        )
        
        return result.rewritten_query

    except Exception as e:
        log.warning("rag.query_rewrite_failed", error=str(e))
        return query