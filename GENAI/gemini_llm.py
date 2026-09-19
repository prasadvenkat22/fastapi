"""Gemini for the GENAI agents, over the same REST call the news grader uses.

Switched from Claude on 2026-09-19 (section 200). The model, key and endpoint
are the ones scripts/news_enrich.py and news_hourly.py have been running the
RSS and Polygon grades on since 2026-09-14 -- gemini-3.1-flash-lite through
generativelanguage.googleapis.com with GEMINI_API_KEY -- so the GENAI
endpoints and the trading pipeline share one provider, one key and one bill.

WHY REST AND NOT langchain-google-genai. It is not in the image, adding it is
a rebuild, and the grader already proved the REST path. GeminiChat is a
LangChain BaseChatModel over that path: enough for the pandas agent's
ReAct loop, the PDF chain, and the supervisor's synthesis. It does not bind
tools; nothing here uses tool calling.
"""

from __future__ import annotations

import json
import os
from typing import Any, List, Optional

import httpx
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_core.outputs import ChatGeneration, ChatResult

GEMINI_MODEL = os.getenv("TRADING_GEMINI_MODEL", "gemini-3.1-flash-lite")
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


def _key() -> str:
    key = os.getenv("GEMINI_API_KEY", "")
    if not key:
        raise RuntimeError("GEMINI_API_KEY is not set")
    return key


def _body(contents: list, system: Optional[str], max_tokens: int, temperature: float,
          json_mode: bool) -> dict:
    body: dict = {
        "contents": contents,
        "generationConfig": {"temperature": temperature, "maxOutputTokens": max_tokens},
    }
    if system:
        body["systemInstruction"] = {"parts": [{"text": system}]}
    if json_mode:
        body["generationConfig"]["responseMimeType"] = "application/json"
    return body


def _text(out: dict) -> str:
    try:
        return "".join(p.get("text", "") for p in out["candidates"][0]["content"]["parts"])
    except (KeyError, IndexError, TypeError):
        fb = (out.get("promptFeedback") or {}).get("blockReason")
        return f"[gemini returned no text{': ' + fb if fb else ''}]"


def messages_to_contents(messages: List[BaseMessage]) -> "tuple[list, Optional[str]]":
    """LangChain messages -> Gemini contents; system messages -> one instruction."""
    system_parts, contents = [], []
    for m in messages:
        text = m.content if isinstance(m.content, str) else "".join(
            b.get("text", "") for b in m.content if isinstance(b, dict))
        if isinstance(m, SystemMessage):
            system_parts.append(text)
        else:
            role = "model" if isinstance(m, AIMessage) else "user"
            if contents and contents[-1]["role"] == role:
                contents[-1]["parts"].append({"text": text})     # Gemini wants alternation
            else:
                contents.append({"role": role, "parts": [{"text": text}]})
    if not contents:
        contents = [{"role": "user", "parts": [{"text": ""}]}]
    return contents, ("\n\n".join(system_parts) or None)


def generate(prompt: str, system: Optional[str] = None, max_tokens: int = 1024,
             temperature: float = 0.2, json_mode: bool = False, model: Optional[str] = None) -> str:
    url = GEMINI_URL.format(model=model or GEMINI_MODEL) + "?key=" + _key()
    body = _body([{"role": "user", "parts": [{"text": prompt}]}], system, max_tokens, temperature, json_mode)
    r = httpx.post(url, json=body, timeout=90.0)
    r.raise_for_status()
    return _text(r.json())


async def agenerate(prompt: str, system: Optional[str] = None, max_tokens: int = 1024,
                    temperature: float = 0.2, json_mode: bool = False, model: Optional[str] = None) -> str:
    url = GEMINI_URL.format(model=model or GEMINI_MODEL) + "?key=" + _key()
    body = _body([{"role": "user", "parts": [{"text": prompt}]}], system, max_tokens, temperature, json_mode)
    async with httpx.AsyncClient(timeout=90.0) as client:
        r = await client.post(url, json=body)
        r.raise_for_status()
        return _text(r.json())


class GeminiChat(BaseChatModel):
    """A LangChain chat model over the Gemini REST endpoint. Text in, text out."""

    model: str = GEMINI_MODEL
    max_tokens: int = 1024
    temperature: float = 0.2

    @property
    def _llm_type(self) -> str:
        return "gemini-rest"

    def _generate(self, messages: List[BaseMessage], stop: Optional[List[str]] = None,
                  run_manager: Any = None, **kwargs: Any) -> ChatResult:
        contents, system = messages_to_contents(messages)
        url = GEMINI_URL.format(model=self.model) + "?key=" + _key()
        body = _body(contents, system, self.max_tokens, self.temperature, False)
        if stop:
            body["generationConfig"]["stopSequences"] = list(stop)[:5]
        r = httpx.post(url, json=body, timeout=90.0)
        r.raise_for_status()
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=_text(r.json())))])

    async def _agenerate(self, messages: List[BaseMessage], stop: Optional[List[str]] = None,
                         run_manager: Any = None, **kwargs: Any) -> ChatResult:
        contents, system = messages_to_contents(messages)
        url = GEMINI_URL.format(model=self.model) + "?key=" + _key()
        body = _body(contents, system, self.max_tokens, self.temperature, False)
        if stop:
            body["generationConfig"]["stopSequences"] = list(stop)[:5]
        async with httpx.AsyncClient(timeout=90.0) as client:
            r = await client.post(url, json=body)
            r.raise_for_status()
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content=_text(r.json())))])
