"""PUBLIC: the site chat widget's news questions.

The rest of /api/genai is admin-only (it spends model credit and can read the
trading tables). This is the one exception, and it is narrow on purpose:

  * one POST, a question of at most 300 characters;
  * answered only by the news agent, which reads headlines and the graded news
    verdict -- never positions, trades or the SQL agent;
  * a question that is not about a company's news gets `is_news: false` and
    costs nothing: no model call, no Polygon call, no database read;
  * answers are cached per symbol (GENAI_NEWS_CACHE_SECONDS), and nginx holds
    /api/news/ to the login zone, five a minute per IP.
"""

from typing import List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from .agents.news_agent import answer_news, detect_symbol, is_news_question

router = APIRouter(prefix="/api/news", tags=["News (public)"])


class NewsAsk(BaseModel):
    query: str = Field(..., min_length=1, max_length=300)


class NewsHeadline(BaseModel):
    title: str
    source: Optional[str] = None
    published: Optional[str] = None
    url: Optional[str] = None
    sentiment: Optional[str] = None


class NewsAnswer(BaseModel):
    is_news: bool
    symbol: Optional[str] = None
    answer: Optional[str] = None
    headlines: List[NewsHeadline] = []


@router.post("/ask", response_model=NewsAnswer)
async def news_ask(body: NewsAsk):
    """Latest headlines on the ticker the question names, summarised."""
    if not is_news_question(body.query):
        return NewsAnswer(is_news=False)
    try:
        res = await answer_news(body.query, detect_symbol(body.query))
    except Exception:
        # Never echo internals to an anonymous caller.
        raise HTTPException(status_code=502, detail="News is unavailable right now.")
    return NewsAnswer(is_news=True, symbol=res["symbol"], answer=res["answer"],
                      headlines=[NewsHeadline(**h) for h in res["headlines"]])
