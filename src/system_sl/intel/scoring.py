"""Explainable relevance scoring for Daily Intel.

The initial formula is::

    R = 0.40P + 0.30T + 0.15F + 0.10S + 0.05N

P, T, F, S and N are all 0–100. When the player has no persona or no active
quests, the unavailable component is dropped and the remaining weights are
renormalized. A missing profile therefore does not lower every article's score;
it simply stops being part of the decision.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

from system_sl.intel.profile import PersonaSignal, TaskSignal, matched_task
from system_sl.intel.sources import parse_published, title_similarity
from system_sl.intel.topics import HIGH_IMPACT_TOPICS, terms, topic_preference

BASE_WEIGHTS = {"P": 0.40, "T": 0.30, "F": 0.15, "S": 0.10, "N": 0.05}
PERSONA_THRESHOLD = 30.0
TASK_THRESHOLD = 30.0
SIMILARITY_THRESHOLD = 0.84


@dataclass
class ArticleScore:
    """One article's component scores and explanation."""

    article: dict
    P: float = 0.0
    T: float = 0.0
    F: float = 0.0
    S: float = 0.0
    N: float = 0.0
    R: float = 0.0
    hard_negative: bool = False
    reasons: list[str] = field(default_factory=list)
    matched_terms: list[str] = field(default_factory=list)
    matched_task: str | None = None

    def as_dict(self) -> dict:
        return {
            "P": round(self.P, 2),
            "T": round(self.T, 2),
            "F": round(self.F, 2),
            "S": round(self.S, 2),
            "N": round(self.N, 2),
            "R": round(self.R, 2),
            "hard_negative": self.hard_negative,
            "reasons": list(self.reasons),
            "matched_terms": list(self.matched_terms),
            "matched_task": self.matched_task,
        }


def normalized_weights(has_persona: bool, has_tasks: bool) -> dict[str, float]:
    """Drop unavailable components and normalize the rest to 1.0."""
    weights = dict(BASE_WEIGHTS)
    if not has_persona:
        weights.pop("P")
    if not has_tasks:
        weights.pop("T")
    total = sum(weights.values())
    if total <= 0:
        return {"F": 0.5, "S": 0.3333, "N": 0.1667}
    return {key: value / total for key, value in weights.items()}


def _article_terms(article: dict) -> set[str]:
    title = article.get("title", "")
    description = article.get("description", "")
    category = article.get("category", "")
    result = terms(f"{title} {title} {description} {category} {category}")
    return result


def _keyword_component(article_terms: set[str], profile: dict[str, float], scale: float = 25.0) -> tuple[float, list[str]]:
    if not profile:
        return 0.0, []
    matched = [(term, weight) for term, weight in profile.items() if term in article_terms]
    if not matched:
        return 0.0, []
    matched.sort(key=lambda item: (item[1], len(item[0])), reverse=True)
    weight = sum(item[1] for item in matched)
    score = min(100.0, weight * scale)
    # Prefer readable unigrams and the strongest matches for the explanation.
    labels = [term for term, _ in matched if " " not in term][:4]
    if not labels:
        labels = [term for term, _ in matched][:4]
    return score, labels


def _category_matches(article: dict, persona: PersonaSignal, tasks: TaskSignal) -> tuple[bool, bool]:
    category = article.get("category")
    persona_match = bool(category and category in persona.topics)
    task_match = any(
        task.get("category") and task.get("category") == category for task in (tasks.tasks if tasks.available else [])
    )
    return persona_match, task_match


def freshness_score(article: dict, now: datetime, reliability: int) -> float:
    """Recency, with a small bonus for important topics and sources."""
    raw = article.get("published_at")
    published = parse_published(raw) if raw else None
    if not published:
        base = 45.0
        age_hours: float | None = None
    else:
        try:
            published_dt = datetime.fromisoformat(published)
        except ValueError:
            published_dt = None
        if published_dt is None:
            base = 45.0
            age_hours = None
        else:
            if published_dt.tzinfo is None:
                published_dt = published_dt.replace(tzinfo=timezone.utc)
            age_hours = max(0.0, (now - published_dt).total_seconds() / 3600.0)
            if age_hours <= 6:
                base = 100.0
            elif age_hours <= 24:
                base = 95.0
            elif age_hours <= 48:
                base = 88.0
            elif age_hours <= 72:
                base = 80.0
            elif age_hours <= 168:
                base = 65.0
            elif age_hours <= 720:
                base = 45.0
            else:
                base = 25.0

    importance = 0.0
    if reliability >= 90:
        importance += 5.0
    if article.get("category") in HIGH_IMPACT_TOPICS:
        importance += 5.0
    if age_hours is not None and age_hours <= 24:
        importance += 2.0
    return min(100.0, base + importance)


def _combined_state(feedback: dict | None, article: dict) -> dict:
    """Feedback for the article plus any coverage of the same story.

    A player who liked, saved, disliked or marked one publisher's coverage has
    expressed an opinion about the whole story. Combining the states keeps that
    signal when clustering later promotes a different publisher's article.
    """
    if not feedback or not isinstance(feedback.get("articles"), dict):
        return {}
    articles = feedback["articles"]
    identifiers = [article.get("id"), *(article.get("story_ids") or [])]
    merged: dict = {}
    for identifier in identifiers:
        state = articles.get(identifier) if identifier else None
        if not isinstance(state, dict):
            continue
        for key in ("liked", "disliked", "saved", "known"):
            if state.get(key):
                merged[key] = True
    return merged


def novelty_score(
    article: dict,
    history_ids: Iterable[str] = (),
    history_titles: Iterable[str] = (),
    feedback: dict | None = None,
) -> float:
    """Penalize articles the player has already seen or explicitly rejected."""
    article_id = article.get("id")
    story_ids = {
        str(identifier)
        for identifier in (article.get("story_ids") or [])
        if identifier
    }
    seen = set(history_ids)
    score = 100.0
    if article_id and article_id in seen:
        score = 40.0
    elif story_ids & seen:
        # Another publisher's coverage of the same story was already shown.
        score = 45.0
    elif any(title_similarity(article.get("title", ""), title) >= 0.9 for title in history_titles):
        score = 50.0

    state = _combined_state(feedback, article)
    if state:
        if state.get("known"):
            score = min(score, 10.0)
        elif state.get("saved"):
            score = max(score, 80.0)
    preference = topic_preference(feedback, article.get("category", ""))
    if preference <= -3:
        score = min(score, 15.0)
    elif preference > 0:
        score = min(100.0, score + 3.0 * preference)
    return max(0.0, min(100.0, score))


def feedback_adjustment(article: dict, feedback: dict | None) -> tuple[float, bool]:
    """Return a score delta and whether the article should be hidden."""
    state = _combined_state(feedback, article)
    if not feedback:
        return 0.0, False

    delta = 0.0
    hidden = False
    if state.get("liked"):
        delta += 8.0
    if state.get("saved"):
        delta += 4.0
    if state.get("disliked"):
        delta -= 30.0
        hidden = True
    if state.get("known"):
        delta -= 30.0
        hidden = True

    preference = topic_preference(feedback, article.get("category", ""))
    if preference > 0:
        delta += min(9.0, 3.0 * preference)
    elif preference <= -3:
        delta -= 40.0
        hidden = True
    return delta, hidden


def score_article(
    article: dict,
    persona: PersonaSignal,
    tasks: TaskSignal,
    *,
    preferred_topics: Iterable[str] = (),
    feedback: dict | None = None,
    now: Optional[datetime] = None,
    history_ids: Iterable[str] = (),
    history_titles: Iterable[str] = (),
) -> ArticleScore:
    """Score one article against the available player signals."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    article_terms = _article_terms(article)
    persona_score, persona_terms = _keyword_component(article_terms, persona.keywords if persona.available else {})
    task_score, task_terms = _keyword_component(article_terms, tasks.keywords if tasks.available else {}, scale=30.0)
    persona_category, task_category = _category_matches(article, persona, tasks)

    reasons: list[str] = []
    if persona.available and persona_category:
        persona_score = min(100.0, persona_score + 20.0)
        reasons.append(f"Aligns with your profile topic: {article.get('category')}")
    elif persona_terms:
        reasons.append("Matches your profile: " + ", ".join(persona_terms))

    matched_quest = matched_task(tasks, article_terms) if tasks.available else None
    if tasks.available and task_category:
        task_score = min(100.0, task_score + 20.0)
        reasons.append(f"Related to your active quests in {article.get('category')}")
    elif matched_quest:
        reasons.append(f"Related to your quest: {matched_quest}")

    reliability = int(article.get("reliability") or 0)
    freshness = freshness_score(article, now, reliability)
    novelty = novelty_score(article, history_ids, history_titles, feedback)

    topics = {topic for topic in preferred_topics if topic}
    interest_bonus = 0.0
    if article.get("category") in topics:
        interest_bonus += 8.0
        reasons.append(f"One of your selected topics: {article.get('category')}")

    # Freshness explanations are intentionally coarse and factual.
    published = parse_published(article.get("published_at"))
    if published:
        try:
            age = now - datetime.fromisoformat(published)
            hours = max(0.0, age.total_seconds() / 3600.0)
            if hours <= 24:
                reasons.append("Published in the last 24 hours")
            elif hours <= 72:
                reasons.append("Published in the last 3 days")
            elif hours <= 168:
                reasons.append("Published this week")
        except (ValueError, TypeError):
            pass
    else:
        reasons.append("Publication date unavailable; treated conservatively")

    if reliability >= 90:
        reasons.append("High-reliability source")
    if novelty >= 90:
        reasons.append("New to you")
    elif novelty <= 50:
        reasons.append("Similar story seen recently")

    weights = normalized_weights(persona.available, tasks.available)
    final = (
        weights.get("P", 0.0) * persona_score
        + weights.get("T", 0.0) * task_score
        + weights.get("F", 0.0) * freshness
        + weights.get("S", 0.0) * reliability
        + weights.get("N", 0.0) * novelty
    )

    delta, hidden = feedback_adjustment(article, feedback)
    final = max(0.0, min(100.0, final + delta + interest_bonus))
    if state := (feedback or {}).get("articles", {}).get(article.get("id"), {}):
        if isinstance(state, dict):
            if state.get("liked"):
                reasons.append("You liked this topic before")
            elif state.get("saved"):
                reasons.append("Saved for later")

    score = ArticleScore(
        article=article,
        P=persona_score,
        T=task_score,
        F=freshness,
        S=float(reliability),
        N=novelty,
        R=final,
        hard_negative=hidden,
        reasons=reasons[:5],
        matched_terms=persona_terms[:4],
        matched_task=matched_quest,
    )
    return score


def _take(
    candidates: list[ArticleScore],
    count: int,
    used_ids: set[str],
    used_titles: list[str],
    *,
    source_counts: dict[str, int] | None = None,
    max_per_source: int | None = None,
) -> list[dict]:
    selected: list[dict] = []
    for score in candidates:
        if len(selected) >= count:
            break
        article = score.article
        identifier = article.get("id")
        if not identifier or identifier in used_ids:
            continue
        if any(title_similarity(article.get("title", ""), title) >= SIMILARITY_THRESHOLD for title in used_titles):
            continue
        source_id = str(article.get("source_id") or "")
        if (
            source_counts is not None
            and max_per_source is not None
            and source_id
            and source_counts.get(source_id, 0) >= max_per_source
        ):
            continue
        used_ids.add(identifier)
        used_titles.append(article.get("title", ""))
        if source_counts is not None and source_id:
            source_counts[source_id] = source_counts.get(source_id, 0) + 1
        selected.append(
            {
                "article": article,
                "score": score.as_dict(),
                "explanation": " · ".join(score.reasons[:3]),
            }
        )
    return selected


def select_briefing(
    scores: Iterable[ArticleScore],
    *,
    has_persona: bool,
    has_tasks: bool,
    max_articles: int = 9,
) -> dict[str, list[dict]]:
    """Pick a balanced briefing without filling sections with weak matches."""
    candidates = [score for score in scores if not score.hard_negative]
    candidates.sort(key=lambda item: (item.R, item.F), reverse=True)

    if has_persona and has_tasks:
        persona_target = max(1, round(max_articles * 0.45))
        task_target = max(1, round(max_articles * 0.35))
    elif has_persona:
        persona_target = max(1, round(max_articles * 0.65))
        task_target = 0
    elif has_tasks:
        persona_target = 0
        task_target = max(1, round(max_articles * 0.65))
    else:
        persona_target = 0
        task_target = 0
    general_target = max(0, max_articles - persona_target - task_target)

    used_ids: set[str] = set()
    used_titles: list[str] = []
    source_counts: dict[str, int] = {}
    distinct_sources = {
        str(score.article.get("source_id"))
        for score in candidates
        if score.article.get("source_id")
    }
    # Source diversity matters when several publishers are present. With one
    # or two sources a strict cap would starve the briefing, so it is relaxed.
    if len(distinct_sources) >= 4:
        max_per_source = 2
    elif len(distinct_sources) >= 2:
        max_per_source = 3
    else:
        max_per_source = None

    persona_candidates = [
        score for score in candidates if has_persona and score.P >= PERSONA_THRESHOLD
    ]
    task_candidates = [
        score for score in candidates if has_tasks and score.T >= TASK_THRESHOLD
    ]

    persona = _take(
        persona_candidates,
        persona_target,
        used_ids,
        used_titles,
        source_counts=source_counts,
        max_per_source=max_per_source,
    )
    tasks = _take(
        task_candidates,
        task_target,
        used_ids,
        used_titles,
        source_counts=source_counts,
        max_per_source=max_per_source,
    )

    # General discovery prefers stories that are not already personal. It has
    # no relevance threshold: general news should exist even for a brand-new
    # player with no profile.
    general_pool = sorted(
        candidates,
        key=lambda item: (item.P >= PERSONA_THRESHOLD or item.T >= TASK_THRESHOLD, -item.R, -item.F),
    )
    general = _take(
        general_pool,
        general_target,
        used_ids,
        used_titles,
        source_counts=source_counts,
        max_per_source=max_per_source,
    )

    # Shortfalls can be filled with additional strong matches in their own
    # sections, never by lowering the general section's quality or duplicating.
    remaining = max_articles - len(persona) - len(tasks) - len(general)
    if remaining > 0 and has_persona:
        extra = _take(
            persona_candidates,
            remaining,
            used_ids,
            used_titles,
            source_counts=source_counts,
            max_per_source=max_per_source,
        )
        persona.extend(extra)
        remaining -= len(extra)
    if remaining > 0 and has_tasks:
        extra = _take(
            task_candidates,
            remaining,
            used_ids,
            used_titles,
            source_counts=source_counts,
            max_per_source=max_per_source,
        )
        tasks.extend(extra)
        remaining -= len(extra)
    if remaining > 0:
        general.extend(
            _take(
                general_pool,
                remaining,
                used_ids,
                used_titles,
                source_counts=source_counts,
                max_per_source=max_per_source,
            )
        )
    remaining = max_articles - len(persona) - len(tasks) - len(general)
    if remaining > 0:
        # The cap is a preference, not a hard limit: never return a short
        # briefing when good candidates are still available.
        general.extend(_take(general_pool, remaining, used_ids, used_titles))

    return {"persona": persona, "tasks": tasks, "general": general}


def collect_history(briefings: dict, feedback: dict, *, today: datetime, days: int = 7) -> tuple[set[str], list[str]]:
    """IDs and titles from recent briefings plus saved/known articles."""
    ids: set[str] = set()
    titles: list[str] = []
    cutoff = (today - timedelta(days=days)).date().isoformat()

    for date_key, briefing in (briefings or {}).items():
        if not isinstance(date_key, str) or date_key < cutoff or not isinstance(briefing, dict):
            continue
        sections = briefing.get("sections", {})
        if not isinstance(sections, dict):
            continue
        for entries in sections.values():
            if not isinstance(entries, list):
                continue
            for entry in entries:
                article = entry.get("article") if isinstance(entry, dict) else None
                if isinstance(article, dict):
                    if article.get("id"):
                        ids.add(article["id"])
                    if article.get("title"):
                        titles.append(article["title"])

    articles = (feedback or {}).get("articles", {})
    if isinstance(articles, dict):
        for identifier, state in articles.items():
            if isinstance(state, dict) and (state.get("saved") or state.get("known")):
                ids.add(identifier)
    return ids, titles
