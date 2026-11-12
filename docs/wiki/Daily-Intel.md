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
| **S** | Source reliability — a per-source quality signal. |
| **N** | Novelty — penalizes stories already shown recently, marked Known, or rejected. |

If you have no persona, the **P** component is removed and the remaining weights are renormalized. The same applies to **T** when you have no active quests. Missing information therefore does not lower every article's score; it simply stops being part of the decision.

The intended split is roughly **45% persona / 35% quests / 20% general** when both signals exist, with flexibility when one section has no strong matches. Sections are never filled with irrelevant articles. If an article is not a strong persona or quest match, it can still appear under **Beyond Your Horizon** if it is recent and useful.

Each card explains its inclusion with short reasons such as:

- `Matches your profile: python, machine learning`
- `Related to your quest: Build an AI chatbot`
- `Published in the last 24 hours`
- `High-reliability source`
- `New to you`

## AI summaries

Each selected article is summarized in four parts:

1. **What happened?**
2. **Why does it matter?**
3. **What can you learn?**
4. **Potential action**

When a chatbot provider is configured, Daily Intel asks the configured model for a concise, grounded summary and caches the result. The model is instructed to treat feed text as untrusted data, never follow instructions found inside it, and avoid inventing facts.

When no provider is available, the request fails, or the response is malformed, Daily Intel falls back to a local summary built only from the feed's title and description. Briefings always render, even offline.

## Feedback

Every card has controls for:

- **Like** — more stories like this
- **Dislike** — fewer stories like this
- **Save** — keep it for later
- **Known** — hide stories you have already seen
- **Mute topic** — show fewer stories in that topic
- **Add quest** — create an active quest from the article title, after confirmation

Feedback influences later briefings. Disliked and Known articles are hidden, Saved articles are preserved in the **Saved** filter, and liked or muted topics adjust the topic preference. **Settings → Daily Intel → Reset News Preferences** clears news feedback, saved articles, and selected topics. It does not touch your Player Profile or quests.

## Sources and topics

Daily Intel includes official company and research feeds plus established publications. Examples include OpenAI, Google DeepMind, Google Research, NVIDIA, GitHub, arXiv, Nature, IEEE Spectrum, Ars Technica, MIT Technology Review, The Verge, TechCrunch, Krebs on Security, and Hacker News.

Open **Sources & Topics** from the Daily Intel window or Settings to:

- Enable or disable Daily Intel
- Choose preferred topics such as AI, Robotics, Cybersecurity, Space, or Health & Biotech
- Enable or disable individual feeds
- Reset news preferences

Feed collection runs in the background. A daily briefing is generated at most once per local date; feeds are refreshed between briefings according to the configured interval.

## Offline and failure behaviour

Daily Intel treats connectivity and model failures as normal states:

| Situation | Behaviour |
|---|---|
| Some feeds fail | The briefing is built from the remaining feeds and marked `partial`. |
| All feeds fail, cache exists | The last successful briefing is shown with an offline warning. |
| All feeds fail, no cache | An empty state explains the problem and offers **Retry**. |
| No internet | Cached articles and briefings are used. |
| LLM unavailable | Local metadata-based summaries are shown. |
| Daily Intel disabled | The window explains how to enable it. |
| No persona or quests | A general briefing is shown with links to build a persona or open quests. |
| Corrupt JSON | The affected file is ignored and defaults are used. |

## Data and privacy

Daily Intel stores its data alongside the rest of your SYSTEM data:

- Linux: `~/.config/system-sl/`
- Windows: `%APPDATA%\system-sl`

| File | Contents |
|---|---|
| `intel_config.json` | Enabled state, preferred topics, disabled sources, refresh settings |
| `intel_articles.json` | Cached article metadata |
| `intel_briefings.json` | Up to 21 recent daily briefings |
| `intel_feedback.json` | Likes, dislikes, saves, Known marks, topic preferences |
| `intel_summaries.json` | Cached article summaries |

Article text stays local unless a chatbot provider is configured and a summary is requested. In that case only the selected article's title, description, and a short profile context are sent to your configured provider. No API keys are stored by Daily Intel; it uses the provider configured under **Settings → LLM**.

## Current limitations

- Relevance matching uses deterministic keywords, topic classification, and title similarity rather than embeddings or semantic search.
- Summaries are only as good as the feed metadata; sources that publish headlines without descriptions produce thinner summaries.
- Feed availability depends on each publisher. A disabled or broken source is skipped without affecting the rest.
- Background collection happens while the desktop app is running. The lightweight `--bg` notifier does not generate Daily Intel briefings.
