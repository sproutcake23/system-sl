# Features

## Persona Builder

A 10-step onboarding wizard that constructs your Player Profile. It asks about your real-world ambitions and uses your answers to build a personalized task-prioritization model.

The wizard is optional. If you skip it, tasks are ranked by urgency alone, and you can create or replace your profile later with **Settings → Edit Player Profile**.

## Task Management

- Add quests with optional deadlines
- Edit, complete, or remove them
- Drag a quest to reorder the list; the order is saved and applied to the ranking
- Deadlines feed into the priority engine

Quests added in the Tasks window go into a default category. The chatbot and document import can set a category when they add quests.

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

Talk to the SYSTEM, an AI mentor with task-aware tools (via LangChain). It can:

- Show your top quests
- Help you prioritize
- Add, complete, or remove quests
- Read your profile and completed tasks

Replies stream in with live markdown, and the **Stop** button interrupts one. Models that expose their reasoning show it as well. If the SYSTEM tries to add a quest you didn't explicitly ask for, a Yes/No bar asks you first.

Every turn carries a short context block with today's date, a summary of your profile and your top quests, so the SYSTEM can answer without reading anything first. It only opens the profile or task list when you ask it to, when the block is not enough, or right after it changed something — a casual question does not cost you extra round trips. Conversations are remembered across turns, model switches and restarts; when a conversation outgrows the model's context window, the oldest turns are dropped and summarized.

### Providers and models

Choose a provider under **Settings → LLM**:

- Google Gemini
- Ollama (local)
- LM Studio (local)
- OpenRouter
- Groq
- Together AI
- Any other OpenAI-compatible endpoint

**Fetch Models** lists what your provider offers, and **Test Connection** checks the settings before you save them. The dropdown at the top of the chat panel switches models, including any it finds on local servers.

API keys are stored in your system keyring, or in an encrypted file if no keyring is available.

### Document attachments

Click the paperclip or drag a file onto the chat panel, then ask the SYSTEM to pull out what needs doing. You review the tasks it finds in an editable table and choose which to add.

| Type | Extensions |
|------|------------|
| PDF | `.pdf` |
| Word | `.docx` |
| HTML | `.html`, `.htm` |
| Text | `.txt`, `.md`, `.csv`, `.log`, `.json`, `.xml`, `.rst`, `.tex` |

Scanned PDFs are read with OCR, in English. Release builds include everything it needs; from source, install `tesseract` and `poppler` on your system.

All files of one message share a single budget, derived from the current model's context window. When a document is too long for it, the panel says how many characters were cut. The document text is wrapped in explicit `<document>` tags, and the SYSTEM is told to treat it as untrusted data rather than instructions.

## Google Tasks Sync

Import upcoming tasks from Google Tasks. See [Google Tasks Sync](Google-Tasks-Sync).

## Desktop Reminders

Small toast popups drawn by the app in the SYSTEM's style. They are not OS-native notifications: they appear in a corner, never take focus, and close on their own after a few seconds. They come with:

- Custom notification sounds (`.mp3` / `.wav`) placed in the `sounds/` folder
- Sound preview and validation
- An hourly reminder with its own on/off switch (see [Background Service](Background-Service))

## Cross-Platform Autostart

Register the background notifier so it launches on login:

- Linux: `.desktop` autostart entry
- macOS: LaunchAgent
- Windows: Registry `Run` key

Autostart is implemented for all three platforms but is currently tested on Linux.

## Settings and Sidebar

The sidebar holds **Tasks**, **Google** (once sync is enabled), and **Settings**. Press `Ctrl+Shift+S` or click its toggle to collapse it to a rail of icons; the choice is remembered.

The **Settings** window has two tabs:

- **General**: notifications, autostart, notification sound, theme, player profile, close to tray, auto update, Google Tasks Sync, and **Install App** when running as an AppImage
- **LLM**: chatbot provider, API key, and model

## Themes

Choose a theme from the picker in Settings:

- **System** (default) follows your operating system's light/dark appearance and switches when the OS does. In dark mode it uses THE SYSTEM's Solo Leveling navy palette.
- **Light** uses the built-in light palette.
- **Dark** uses a darker, OLED-friendly near-black palette with the same cyan accent, so it is visibly distinct from System.
- **Dynamic** extracts colors from your wallpaper with [wallust](https://codeberg.org/explosion-mental/wallust) and applies them across the whole app, including icons, chat, notifications, dialogs, and the tray. It is always a dark variant, follows the primary monitor, and updates when the wallpaper changes.

Released builds bundle wallust. Running from source requires wallust on your `PATH`, or set `SYSTEM_SL_WALLUST` to its executable. If wallust is unavailable in Dynamic mode, the app falls back to the Dark palette and shows a note in Settings.

## System Tray

Turn on **Close to System Tray** to keep the app running when you close the window. Use the tray menu to reopen it or exit. The option is disabled on desktops without a system tray.

## Auto Update

Turn on **Auto Update** to check GitHub for a new release at launch. Downloads are verified against the release's SHA-256 checksums before they're installed. An AppImage updates to the new AppImage and relaunches into it.
