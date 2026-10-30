# Features

## Persona Builder

A 10-step onboarding wizard that constructs your Player Profile. It asks about your real-world ambitions and uses your answers to build a personalized task-prioritization model.

## Task Management

- Add quests with deadlines and categories
- Mark tasks complete
- Deadlines and category filters feed into the priority engine

## Priority Engine

Scores every task using a gravity-based model that combines:

- **Cognitive weight** (W)
- **Intrinsic importance** (I)
- **Time remaining / deadline pressure** (D)

Context-aware profiles adapt the scoring to your current situation:

- **PRESSURE** — multiple tasks due soon
- **GROWTH** — normal pacing
- **RECOVERY** — low activity

Each task is placed into an Eisenhower-style quadrant:

- DO NOW (high pull, high importance)
- SCHEDULE (low pull, high importance)
- DEFER (high pull, low importance)
- CONSIDER DROPPING (low pull, low importance)

## NLP Vectorizer

Uses spaCy to convert your onboarding answers and task history into a 300-dimensional user profile vector with temporal decay, so the SYSTEM learns your preferences over time.

## AI Chatbot

Talk to the SYSTEM — an AI mentor powered by Gemini (via LangChain) with task-aware tools, plus a friendly companion persona. The chat can:

- Show your top quests
- Help you prioritize
- Give motivation in either System or Friend mode

## Google Tasks Sync

Import upcoming tasks from Google Tasks. See [Google Tasks Sync](Google-Tasks-Sync).

## Native Notifications

HUD-style desktop alerts styled like the SYSTEM, with:

- Custom notification sounds (`.mp3` / `.wav`) placed in the `sounds/` folder
- Sound preview and validation
- A persistent hourly background notifier (see [Background Service](Background-Service))

## Cross-Platform Autostart

Register the background notifier so it launches on login:

- Linux: `.desktop` autostart entry
- macOS: LaunchAgent
- Windows: Registry `Run` key

Autostart is implemented for all three platforms but is currently tested on Linux.
