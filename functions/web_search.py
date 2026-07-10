import re
from datetime import datetime
from typing import Iterable, List
from urllib.parse import urlparse

import requests
import trafilatura
from bs4 import BeautifulSoup

try:
    from ddgs import DDGS
except ImportError:  # pragma: no cover - compatibility fallback
    from duckduckgo_search import DDGS  # type: ignore


SEARCH_TRIGGER_PHRASES = {
    "today",
    "tonight",
    "right now",
    "currently",
    "current",
    "latest",
    "recent",
    "recently",
    "new",
    "newest",
    "updated",
    "update",
    "as of",
    "this year",
    "this month",
    "this week",
    "yesterday",
    "news",
    "headline",
    "announced",
    "announcement",
    "released",
    "release",
    "launch",
    "launched",
    "happened",
    "happening",
    "going on",
    "ongoing",
    "started",
    "starting",
    "start date",
    "live",
    "still",
    "won",
    "lost",
    "score",
    "result",
    "match",
    "game",
    "fixture",
    "world cup",
    "ipl",
    "premier league",
    "ucl",
    "nba",
    "nfl",
    "f1",
    "table",
    "standings",
    "bracket",
    "knockout",
    "round of",
    "semi final",
    "final",
    "price",
    "cost",
    "stock",
    "market cap",
    "rate",
    "discount",
    "version",
    "patch",
    "changelog",
    "is it true",
    "did",
    "has",
    "have they",
    "who won",
    "what happened",
    "can you check",
    "look it up",
    "search that",
}

FOLLOW_UP_SEARCH_PHRASES = {
    "again",
    "now again",
    "is it going on",
    "is it live",
    "is it still on",
    "is it happening",
    "what about now",
    "check again",
    "still",
}

NEWS_INTENT_PHRASES = {
    "news",
    "headline",
    "latest",
    "recent",
    "update",
    "updated",
    "announced",
    "announcement",
    "what happened",
    "again",
    "now",
    "today",
    "currently",
    "going on",
    "ongoing",
    "started",
    "live",
    "still",
    "won",
    "lost",
    "score",
    "result",
    "match",
    "game",
    "fixture",
}

LOW_VALUE_DOMAINS_FOR_NEWS = {
    "wikipedia.org",
}

LOW_VALUE_PATH_TERMS = {
    "tickets",
    "hospitality",
    "shop",
    "store",
    "signup",
    "login",
    "register",
}

PREFERRED_TEXT_HINTS = {
    "news",
    "article",
    "report",
    "live",
    "updates",
    "analysis",
    "recap",
    "fixtures",
    "results",
    "schedule",
}


def should_search(query: str) -> bool:
    text = query.casefold().strip()
    if not text:
        return False

    if any(phrase in text for phrase in SEARCH_TRIGGER_PHRASES):
        return True

    if re.search(r"\b20\d{2}\b", text):
        return True

    if re.search(r"\b\d{1,2}[-:]\d{1,2}\b", text):
        return True

    if re.search(
        r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
        text,
    ):
        return True

    return False


def _normalize_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _strip_speaker_prefix(text: str) -> str:
    cleaned = _normalize_whitespace(text)
    return re.sub(r"^\[[^\]]+\]:\s*", "", cleaned)


def _strip_search_command_prefix(text: str) -> str:
    cleaned = _strip_speaker_prefix(text)
    return re.sub(
        r"^(?:(?:can you|could you|please)\s+)*(?:check(?: out)?)\s+",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )


def _split_sentences(text: str) -> List[str]:
    return [
        sentence.strip()
        for sentence in re.split(r"(?<=[.!?])\s+", text)
        if sentence and sentence.strip()
    ]


def _query_terms(query: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z0-9]{3,}", query.casefold())
        if token not in {"what", "when", "where", "which", "that", "this", "with", "from", "check"}
    }


def _message_text(message: dict) -> str:
    return _strip_search_command_prefix(str(message.get("content") or ""))


def build_search_query(query: str, recent_messages: List[dict], max_messages: int = 4) -> str:
    base_query = _strip_search_command_prefix(query)
    lowered = base_query.casefold()
    follow_up = any(phrase in lowered for phrase in FOLLOW_UP_SEARCH_PHRASES)
    sparse_query = len(_query_terms(base_query)) <= 4

    if not recent_messages or not (follow_up or sparse_query):
        return base_query

    context_parts: List[str] = []
    for message in recent_messages[-max_messages:]:
        content = _message_text(message)
        if not content:
            continue
        if len(content) > 180:
            content = content[:180]
        context_parts.append(content)

    if not context_parts:
        return base_query

    return _normalize_whitespace(" ".join(context_parts + [base_query]))


def _is_news_intent(query: str) -> bool:
    text = query.casefold()
    return any(phrase in text for phrase in NEWS_INTENT_PHRASES)


def _domain_from_url(url: str) -> str:
    netloc = urlparse(url).netloc.lower()
    return netloc[4:] if netloc.startswith("www.") else netloc


def _looks_low_value_for_news(url: str, title: str) -> bool:
    domain = _domain_from_url(url)
    path = urlparse(url).path.casefold()
    title_text = title.casefold()
    if any(domain.endswith(bad) for bad in LOW_VALUE_DOMAINS_FOR_NEWS):
        return True
    if any(term in path for term in LOW_VALUE_PATH_TERMS):
        return True
    if any(term in title_text for term in LOW_VALUE_PATH_TERMS):
        return True
    return False


def _extract_date_candidates(text: str) -> list[str]:
    patterns = [
        r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\s+\d{1,2},\s+20\d{2}\b",
        r"\b\d{1,2}\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\s+20\d{2}\b",
        r"\b20\d{2}-\d{2}-\d{2}\b",
    ]
    found: list[str] = []
    lowered = text.casefold()
    for pattern in patterns:
        for match in re.findall(pattern, lowered):
            found.append(match)
            if len(found) >= 2:
                return found
    return found


def _pick_relevant_sentences(text: str, query: str, max_chars: int = 360) -> str:
    cleaned = _normalize_whitespace(text)
    if not cleaned:
        return ""

    sentences = _split_sentences(cleaned)
    if not sentences:
        return cleaned[:max_chars].strip()

    query_terms = _query_terms(query)
    ranked = sorted(
        sentences,
        key=lambda sentence: (
            sum(term in sentence.casefold() for term in query_terms),
            any(hint in sentence.casefold() for hint in PREFERRED_TEXT_HINTS),
            -abs(len(sentence) - 150),
        ),
        reverse=True,
    )

    chosen: List[str] = []
    total = 0
    for sentence in ranked:
        sentence = sentence.strip(" -")
        if not sentence:
            continue
        score = sum(term in sentence.casefold() for term in query_terms)
        if score == 0 and chosen:
            continue
        candidate_len = len(sentence) + (1 if chosen else 0)
        if total + candidate_len > max_chars and chosen:
            continue
        chosen.append(sentence)
        total += candidate_len
        if total >= max_chars or len(chosen) >= 2:
            break

    summary = " ".join(chosen) if chosen else cleaned[:max_chars]
    return summary[:max_chars].strip()


def _fetch_page_text(url: str) -> str:
    downloaded = trafilatura.fetch_url(url)
    if downloaded:
        extracted = trafilatura.extract(
            downloaded,
            include_comments=False,
            include_tables=False,
            favor_precision=True,
            with_metadata=False,
        )
        if extracted:
            return _normalize_whitespace(extracted)

    response = requests.get(
        url,
        timeout=8,
        headers={"User-Agent": "RainyAI/1.0 (+web context retrieval)"},
    )
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    return _normalize_whitespace(soup.get_text(" ", strip=True))


def _safe_results(query: str, max_results: int) -> Iterable[dict]:
    with DDGS() as ddgs:
        return list(ddgs.text(query, max_results=max_results))


def _score_result(query: str, title: str, url: str, snippet: str, page_text: str, news_intent: bool) -> int:
    query_terms = _query_terms(query)
    text_blob = " ".join([title, snippet, page_text[:4000]]).casefold()
    score = 0
    score += sum(3 for term in query_terms if term in title.casefold())
    score += sum(2 for term in query_terms if term in snippet.casefold())
    score += sum(1 for term in query_terms if term in text_blob)
    score += min(len(_extract_date_candidates(text_blob)) * 2, 4)
    score += sum(2 for hint in PREFERRED_TEXT_HINTS if hint in text_blob)

    domain = _domain_from_url(url)
    if news_intent:
        if _looks_low_value_for_news(url, title):
            score -= 8
        if any(hint in url.casefold() for hint in ("news", "article", "live", "report", "updates")):
            score += 4
        if domain.endswith("reuters.com") or domain.endswith("apnews.com") or domain.endswith("espn.com"):
            score += 3
    else:
        if domain.endswith("wikipedia.org"):
            score -= 2

    page_len = len(page_text)
    if 400 <= page_len <= 12000:
        score += 3
    elif page_len < 200:
        score -= 3

    return score


def _build_line(title: str, url: str, summary: str, page_text: str) -> str:
    date_bits = _extract_date_candidates(page_text)
    date_text = f" | {date_bits[0].title()}" if date_bits else ""
    return f"- {title} | {_domain_from_url(url)}{date_text}: {summary}"


def search_web_context(
    query: str,
    *,
    max_results: int = 8,
    max_sources: int = 2,
    max_total_chars: int = 1100,
) -> str:
    try:
        results = _safe_results(query, max_results=max_results)
    except Exception:
        return ""

    news_intent = _is_news_intent(query)
    candidates: list[tuple[int, str]] = []
    used_urls: set[str] = set()

    for result in results:
        url = str(result.get("href") or result.get("url") or "").strip()
        title = _normalize_whitespace(str(result.get("title") or "Untitled source"))
        snippet = _normalize_whitespace(
            str(result.get("body") or result.get("snippet") or result.get("description") or "")
        )
        if not url or url in used_urls:
            continue
        used_urls.add(url)

        if news_intent and _looks_low_value_for_news(url, title):
            continue

        try:
            page_text = _fetch_page_text(url)
        except Exception:
            page_text = ""

        if not page_text and not snippet:
            continue

        summary = _pick_relevant_sentences(page_text or snippet, query)
        if not summary:
            continue

        score = _score_result(query, title, url, snippet, page_text, news_intent)
        line = _build_line(title, url, summary, page_text or snippet)
        candidates.append((score, line[:460]))

    if not candidates:
        return ""

    candidates.sort(key=lambda item: item[0], reverse=True)

    lines: list[str] = []
    total_chars = 0
    for score, line in candidates:
        if score <= 0 and lines:
            continue
        if total_chars + len(line) > max_total_chars and lines:
            break
        lines.append(line)
        total_chars += len(line)
        if len(lines) >= max_sources:
            break

    if not lines:
        return ""

    evidence_rule = (
        "Use these site-derived facts first for current real-world questions. "
        "If they are weak or conflicting, say that briefly instead of guessing."
    )
    return "Recent web context:\n" + "\n".join(lines) + f"\n{evidence_rule}"
