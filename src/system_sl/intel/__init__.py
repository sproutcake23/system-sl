"""Daily Intel: personalized, explainable news for THE SYSTEM.

The public surface is intentionally small. GUI code imports the service and
the topic/source catalog; tests inject fakes into :class:`BriefingService`.
"""

from system_sl.intel.briefing import BriefingService, get_intel_service
from system_sl.intel.clustering import cluster_articles, single_cluster, story_similarity
from system_sl.intel.extraction import ContentExtractor, extract_readable_text
from system_sl.intel.health import SourceHealthTracker, success_rate
from system_sl.intel.profile import PersonaSignal, TaskSignal, load_persona_signal, load_task_signal
from system_sl.intel.registry import SourceRegistry, SourceValidationError
from system_sl.intel.scoring import (
    ArticleScore,
    normalized_weights,
    score_article,
    select_briefing,
)
from system_sl.intel.sources import (
    DEFAULT_SOURCES,
    FetchResult,
    NewsSource,
    SourceFetchOutcome,
    fetch_all,
    parse_feed,
)
from system_sl.intel.storage import IntelStore
from system_sl.intel.summaries import SummaryEngine, fallback_summary
from system_sl.intel.topics import GENERAL_TOPIC, TOPICS, classify, strip_html, tokenize

__all__ = [
    "ArticleScore",
    "BriefingService",
    "ContentExtractor",
    "DEFAULT_SOURCES",
    "FetchResult",
    "GENERAL_TOPIC",
    "IntelStore",
    "NewsSource",
    "PersonaSignal",
    "SourceFetchOutcome",
    "SourceHealthTracker",
    "SourceRegistry",
    "SourceValidationError",
    "SummaryEngine",
    "TOPICS",
    "TaskSignal",
    "classify",
    "cluster_articles",
    "extract_readable_text",
    "fallback_summary",
    "fetch_all",
    "get_intel_service",
    "load_persona_signal",
    "load_task_signal",
    "normalized_weights",
    "parse_feed",
    "score_article",
    "select_briefing",
    "single_cluster",
    "story_similarity",
    "strip_html",
    "success_rate",
    "tokenize",
]
