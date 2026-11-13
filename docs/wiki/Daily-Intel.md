# Daily Intel

Daily Intel is a personalized news briefing built into THE SYSTEM. It collects recent articles from trusted technology, science, security, and industry feeds, then organizes them into up to three sections:

| Section | Who it is for |
|---|---|
| **01. Relevant to Your Journey** | Articles that align with your Player Profile. |
| **02. Relevant to Your Active Quests** | Articles connected to the quests currently on your board. |
| **03. Beyond Your Horizon** | Important developments outside your immediate path. |

The feature is optional and never requires a persona or active quests. With neither, you still receive a general briefing; the app does not invent a profile or show an empty page.

## Opening Daily Intel

Click **Daily Intel** in the sidebar, or use **Settings → Daily Intel → Open Daily Intel**.

The window shows the briefing date, when it was last updated, the current sections, and a **Refresh** button. If today's briefing has not been generated yet, it is prepared in the background and saved locally.

## How articles are chosen

Each candidate article receives a relevance score from 0 to 100:

```text
R = 0.40P + 0.30T + 0.15F + 0.10S + 0.05N
```

| Component | Meaning |
|---|---|
| **P** | Persona relevance — overlap with your Player Profile answers, inferred topics, and selected topics. |
| **T** | Quest relevance — overlap with active quest titles, categories, and deadlines. |
| **F** | Freshness — recency, with a small boost for high-impact topics and reliable sources. |
| **S** | Source credibility — the publisher's configured reliability classification. |
| **N** | Novelty — penalizes stories already shown recently, marked Known, or rejected. |

If you have no persona, the **P** component is removed and the remaining weights are renormalized. The same applies to **T** when you have no active quests. Missing information therefore does not lower every article's score; it simply stops being part of the decision.

The intended split is roughly **45% persona / 35% quests / 20% general** when both signals exist, with flexibility when one section has no strong matches. Sections are never filled with irrelevant articles. If an article is not a strong persona or quest match, it can still appear under **Beyond Your Horizon** if it is recent and useful.

When several publishers are represented, the selector also balances sources so one feed cannot fill the whole briefing. The cap is relaxed when only one or two sources are active, so a small catalog still produces a useful briefing.

Each card explains its inclusion with short reasons such as:

- `Matches your profile: python, machine learning`
- `Related to your quest: Build an AI chatbot`
- `Published in the last 24 hours`
- `High-reliability source`
- `New to you`

## Story clustering and duplicate coverage

Daily Intel groups articles that describe the same underlying news story, even when the headlines are written differently. Three publications reporting the same announcement become one story card instead of three near-identical entries.

Clustering is intentionally conservative and works offline:

- The same canonical URL, or the same article id, is treated as an **exact duplicate**.
- Near-identical headlines (93% or more similar) are treated as a **syndicated copy**.
- Different headlines are grouped as the same **story** when they share at least two significant terms, fall within a 72-hour window, and pass a combined title/description/entity similarity threshold.
- Articles that merely share a common word — for example different stories about the same company — are kept separate.
- Time gaps larger than 72 hours keep stories apart unless the URL itself is identical.

Every cluster preserves all original URLs and source names. The card picks the most useful primary article using, in order: publisher credibility, whether full article text is available, description length, and recency. Other coverage appears under **Also covered by**, with a link to each publisher. The primary article also carries a `story_ids` list so scoring and feedback can recognise coverage of a story you have already seen.

Clustering can be disabled with `story_clustering` in `intel_config.json`.

## AI summaries

Each selected article is summarized in four parts:

1. **What happened?**
2. **Why does it matter?**
3. **What can you learn?**
4. **Potential action**

When a chatbot provider is configured, Daily Intel asks the configured model for a concise, grounded summary and caches the result. The model is instructed to treat article text as untrusted data, never follow instructions found inside it, and avoid inventing facts.

The summary input distinguishes between two content types:

- **Full article text** — extracted from the publisher's page for a selected story.
- **Feed excerpt only** — the RSS/Atom description, which is *not* the complete article.

The prompt explicitly tells the model when it only has an excerpt, and the card shows a badge so the player can see which case applies. A cached summary keyed on the full text is stored separately from the excerpt-based summary.

When no provider is available, the request fails, or the response is malformed, Daily Intel falls back to a local summary built only from the available text. Briefings always render, even offline.

## Selective full-article extraction

Daily Intel does not download every page it collects. Full text is requested only when:

- the article was selected for the briefing;
- the source allows extraction (`extract` is true);
- the feed description is thin (shorter than about 420 characters or ends with “read more”);
- a per-run budget (`max_extractions_per_run`, default 6) has not been used up.

Extraction uses a small, dependency-free readable-text parser. It removes navigation, headers, footers, sidebars, cookie/newsletter prompts, scripts, and similar boilerplate. Requests use sensible timeouts (8 seconds) and a response-size cap (about 1.5 MB).

Each article's content status is stored and shown on the card:

| Status | Badge | Meaning |
|---|---|---|
| `full` | **FULL ARTICLE** | Readable article text was extracted and used for the summary. |
| `excerpt` | **RSS EXCERPT** | Only the feed description was available. |
| `failed` | **EXTRACTION FAILED** | The page could not be fetched or no readable text was found. The RSS excerpt is used instead. |
| `blocked` | **ACCESS RESTRICTED** | The publisher disallows access (robots.txt, 401/402/403/451, or a detected paywall). The RSS excerpt is used instead. |

Daily Intel respects `robots.txt` on a best-effort basis, caches the rules per host, and never attempts to bypass paywalls or access controls. A `robots.txt` that cannot be read is treated permissively, but a page that answers with an access restriction is recorded as blocked rather than retried aggressively.

Because the parser is lightweight, JavaScript-rendered pages may yield no readable text. That is treated as `failed`, not as a reason to fabricate content. Extracted text is cached per article: 14 days for successful extraction, 24 hours for failures, and 7 days for access restrictions. Repeated briefings reuse the cache instead of downloading the same article again.

## Source registry

Daily Intel ships with a default list of trusted sources and keeps their definitions separate from the Python code. Open **Sources & Topics** to:

- Enable or disable individual feeds;
- Add a custom RSS or Atom feed;
- Remove a source (including a built-in one);
- See each source's configured interval and current health.

Source definitions are validated before they are saved: the URL must be HTTP(S), the type must be `rss`, `atom`, or the reserved `api`, and reliability must be between 0 and 100. Invalid entries are rejected with a message; a corrupt hand-edited entry is ignored instead of breaking the catalog.

Built-in sources remain the defaults, but the persisted `intel_sources.json` stores only:

- overrides for built-in sources;
- custom sources;
- the ids of built-in sources you removed (`removed_defaults`).

This means a future release can add a new built-in feed without resurrecting one you deleted, and without overwriting your customizations. Enable/disable state stays in `intel_config.json` (`disabled_sources`) for backwards compatibility. **Reset News Preferences** clears feedback and topics but does not re-add removed sources; the registry can be reset separately through the service API.

`api` is reserved for future providers. It validates and is stored, but is not fetched yet; unsupported sources fail gracefully and do not affect other feeds.

## Per-source scheduling and conditional requests

Feed collection is independent of daily briefing generation. Each source has its own polling interval:

- a source-specific `interval_minutes`, when configured;
- otherwise the global `fetch_interval_hours` (default 6 hours).

Only sources that are *due* are contacted. The background scheduler checks periodically; forcing a refresh rebuilds the briefing but still respects each source's due time and retry backoff, so pressing **Refresh** repeatedly cannot hammer a broken feed.

Requests are conditional:

- `ETag` values are stored and sent as `If-None-Match`;
- `Last-Modified` values are stored and sent as `If-Modified-Since`;
- an `HTTP 304 Not Modified` response counts as a successful pass and keeps the cached articles;
- `Cache-Control: max-age` can extend the next allowed request;
- `Retry-After` is honoured for rate limits and temporary failures.

A temporary failure uses exponential backoff: 10, 20, 40 minutes, and so on, capped at six hours. After three consecutive failures a source is marked **unavailable**, but it is still retried and becomes **ok** again after the next successful fetch. Sources are never permanently removed because of temporary failures. One failed feed never stops the others; the briefing is built from whatever could be collected and is marked `partial`.

## Source credibility versus source health

Daily Intel keeps two measurements separate:

| Measurement | Where it lives | What it means |
|---|---|---|
| **Credibility** | `reliability` on the source definition | How trustworthy the publisher is. It is a stable classification and never changes because a server was slow. |
| **Health / availability** | `intel_source_state.json` | Technical behaviour: last success, last failure, consecutive failures, success rate, average response time, last HTTP status, availability, and next allowed attempt. |

A frequently available tabloid is not promoted above a primary research publication, and a reliable publisher having a bad day is not treated as untrustworthy. The health record is used only for retries, backoff, and the availability display.

## Feedback

Every card has controls for:

- **Like** — more stories like this
- **Dislike** — fewer stories like this
- **Save** — keep it for later
- **Known** — hide stories you have already seen
- **Mute topic** — show fewer stories in that topic
- **Add quest** — create an active quest from the article title, after confirmation

Feedback influences later briefings. Disliked and Known articles are hidden, Saved articles are preserved in the **Saved** filter, and liked or muted topics adjust the topic preference. When a story has multiple publishers, marking the primary article Known or Disliked also reduces the novelty of the other coverage through the stored `story_ids`.

**Settings → Daily Intel → Reset News Preferences** clears news feedback, saved articles, and selected topics. It does not touch your Player Profile, quests, or source registry.

## Offline and failure behaviour

Daily Intel treats connectivity and model failures as normal states:

| Situation | Behaviour |
|---|---|
| Some feeds fail | The briefing is built from the remaining feeds and marked `partial`. |
| All feeds fail, cache exists | The last successful briefing is shown with an offline warning. |
| All feeds fail, no cache | An empty state explains the problem and offers **Retry**. |
| A feed is unchanged (304) | Cached articles are kept and the pass counts as successful. |
| A feed is rate-limited or failing | Backoff and `Retry-After` delay the next request; other feeds continue. |
| No internet | Cached articles and briefings are used. |
| LLM unavailable | Local metadata-based summaries are shown. |
| Extraction fails or is blocked | The RSS excerpt is shown with the matching badge. |
| Daily Intel disabled | The window explains how to enable it. |
| No persona or quests | A general briefing is shown with links to build a persona or open quests. |
| Corrupt JSON | The affected file is ignored and defaults are used. |

## Data and privacy

Daily Intel stores its data alongside the rest of your SYSTEM data:

- Linux: `~/.config/system-sl/`
- Windows: `%APPDATA%\system-sl`

| File | Contents |
|---|---|
| `intel_config.json` | Enabled state, preferred topics, disabled sources, fetch/extraction/clustering settings |
| `intel_sources.json` | Built-in source overrides, custom sources, and removed built-in ids |
| `intel_source_state.json` | Per-source health, ETag/Last-Modified validators, and retry schedule |
| `intel_articles.json` | Cached article metadata |
| `intel_content.json` | Cached extracted article text and content status (up to 300 recent entries) |
| `intel_briefings.json` | Up to 21 recent daily briefings |
| `intel_feedback.json` | Likes, dislikes, saves, Known marks, topic preferences |
| `intel_summaries.json` | Cached article summaries (excerpt and full-text variants) |

Article text stays local unless a chatbot provider is configured and a summary is requested. In that case the selected article's title, available text, and a short profile context are sent to your configured provider. No API keys are stored by Daily Intel; it uses the provider configured under **Settings → LLM**.

## Configuration examples

Most players should use the **Sources & Topics** dialog. The JSON files are also safe to edit while the app is closed.

Add a custom source by adding it through the dialog, or write an entry to `intel_sources.json`:

```json
{
  "version": 2,
  "removed_defaults": [],
  "sources": [
    {
      "id": "example-lab",
      "name": "Example Lab",
      "url": "https://lab.example.com/feed.xml",
      "homepage": "https://lab.example.com",
      "category": "AI & Machine Learning",
      "reliability": 88,
      "enabled": true,
      "official": false,
      "type": "atom",
      "interval_minutes": 30,
      "extract": true
    }
  ]
}
```

Relevant `intel_config.json` keys:

| Key | Default | Purpose |
|---|---|---|
| `fetch_interval_hours` | `6` | Fallback polling interval for sources without their own interval. |
| `extract_full_articles` | `true` | Enable selective full-text extraction. |
| `max_extractions_per_run` | `6` | Maximum page downloads per briefing. Set `0` to rely on excerpts only. |
| `story_clustering` | `true` | Group cross-source coverage of the same story. |
| `max_articles` | `9` | Maximum cards in a briefing. |

## Current limitations

- Relevance matching and story clustering use deterministic keywords and text similarity rather than embeddings or semantic search. Clustering is deliberately conservative and may occasionally miss two very differently worded reports of one event.
- Summaries are only as good as the available text. Sources that publish headlines without descriptions produce thinner summaries, and extraction failures fall back to the excerpt.
- JavaScript-heavy pages may yield no readable text; Daily Intel reports an extraction failure rather than guessing at the article body.
- The `api` source type is reserved for future providers and is not fetched yet.
- Paywalls and access controls are never bypassed. When a publisher restricts access, the card shows **ACCESS RESTRICTED** and uses the RSS excerpt.
- Feed availability depends on each publisher. Disabled, broken, or temporarily unavailable sources do not affect the rest.
- Background collection happens while the desktop app is running. The lightweight `--bg` notifier does not generate Daily Intel briefings.
