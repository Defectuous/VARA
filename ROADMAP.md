# V.A.R.A. Project Roadmap

**V.A.R.A.** means **Visual and Audio Reality Assistant**: a local-first personal AI command center. One backend on your PC brings together conversation, multiple AI models, memory, your own files, voice, tools, and integrations. Clients run on Windows, macOS, Linux, and Android.

The roadmap is intentionally staged. Build a trustworthy local foundation first, then add models, memory, knowledge, and an approval layer before any tool can act. Add devices, voice, automation, and integrations after that.

## Target Feature Set

| Area | What V.A.R.A. should do | Phase |
| --- | --- | --- |
| Multiple models | Local models through Ollama, plus optional Anthropic Claude, OpenAI, Google Gemini, Groq, and OpenRouter; manual model choice or automatic routing | 2 |
| Memory | Persistent, user-controlled context across conversations: preferences, projects, commitments | 3 |
| Personal knowledge | Chat with documents, PDFs, notes, code, and past conversations; keyword and semantic search (RAG) | 4 |
| Agent and tools | Multi-step tool use with progress, failure handling, verification, approvals, and audit history | 5 |
| Clients | Chat, history, and a Command Center dashboard on Windows, macOS, Linux, and Android; system tray and global hotkey on desktop | 7 |
| Voice | Speech-to-text, text-to-speech, wake word, hands-free mode, streaming speech, interruption | 8 |
| Computer control | Launch apps, manage files and folders, search the computer, change supported settings, browser-assisted tasks, web search | 9 |
| Integrations | Calendar, email, communication, and developer services such as GitHub | 10 |
| Vision | Image understanding, OCR, translation | 11 |
| Finance | Bills, budgets, reminders | 12 |
| Workflows | Saved custom workflows and narrowly scoped automation | 13 |
| Privacy | Privacy modes, protected credentials, redaction, approvals, audit (built in from Phase 1 onward, not added at the end) | All |

## Starting Decisions

- The first backend runs on your Windows PC. Python is the primary backend language.
- **Local by default, cloud optional.** Ollama is the default model provider. You can add cloud providers with your own API keys. Every model sits behind one provider interface, so you can add, remove, or swap models without rewriting the assistant.
- **Privacy modes decide what can leave the machine.** *Local only* blocks every cloud request. *Ask* requires confirmation before data goes to a cloud provider. *Allowed* sends data to the providers you configured. Memory, knowledge files, and financial data are not sent to a cloud model unless the active mode allows it.
- Build the backend API before the clients. The backend remains the authority for memory, permissions, credentials, and integrations; clients are interfaces to it.
- **One client codebase for four platforms:** Windows, macOS, Linux, and Android. Platform-specific features (tray, hotkeys, background microphone, computer control) live behind small per-platform modules.
- Require your confirmation before sending, changing, purchasing, deleting, or sharing anything. Relax permissions only through explicit, narrow rules later.
- Model output never executes anything directly. Tools run only through a typed action layer that enforces approvals and records an audit trail.
- Aim for durable, searchable memory with backup and restore. No software can guarantee that data will last forever; reliability depends on storage health, backups, encryption-key recovery, and tested restores.
- Start as a single-user system. Do not expose its API directly to the public internet.

## Phases

### 0. Define boundaries and check the machine

**Work:** Record the first use cases, data sensitivity, the default privacy mode, and what must always require approval. This PC is running Windows 11 Pro with a Ryzen 7 5800X and 32 GB RAM. The NVIDIA driver currently exposes an RTX 5060 with 8 GB; Windows lists the Tesla V100-SXM2-16GB as `CM_PROB_PHANTOM`, so it is not currently present on the active PCIe connection. Check the V100's enclosure power, OCuLink seating/link, and device status before treating it as available. Keep a note of the future server's OS and GPU layout when those are known.

**Tools:** VS Code with the Python extension, Git, Python 3.12, `uv`, and `nvidia-smi` for checking the NVIDIA driver and GPU. Use Windows Security/BitLocker where available. Keep model files, API keys, and personal data out of Git.

**Done when:** The project has a written scope and a working Python environment. The RTX 5060 is available for the first local model test. Before using the V100, confirm that Windows no longer reports it as phantom and `nvidia-smi` lists it with usable memory, then run a small inference test. If it is still absent, continue on the RTX 5060 while diagnosing the eGPU connection.

### 1. Build a small, secure local backend

**Status:** Done. Settings with a development/production split, JSON logs with secret redaction, the `/v1` API prefix, a localhost-only `uv run python -m vara` command, and tests for each exist. Device authentication moves to Phase 6.

**Work:** Add configuration (with a development/production split), structured logs that redact secrets, and a versioned API (`/v1/...`). Bind to localhost by default. Add authentication before allowing other devices to connect.

**Tools:** FastAPI and Pydantic for the HTTP API; `pydantic-settings` for configuration; `uv` for dependencies; Pytest and Ruff for tests and lint/format checks; Git for version control.

**Done when:** The service starts with one documented command, its health endpoint works, tests pass, logs do not contain secrets, and it is not reachable from other machines unless explicitly configured.

### 2. Add local and cloud model providers

**Status:** Started. The provider interface, the fake provider, the privacy-mode router (tested), the Ollama and Anthropic adapters, and `/v1/chat` and `/v1/providers` exist. Claude and Ollama have each answered a live request; in `local_only` mode the answer came from Ollama with no cloud call. Streaming (`/v1/chat/stream`), context inspection (`/v1/chat/preview`), and in-memory cost tracking (`/v1/usage`) exist. Usage is now stored in the Phase 3 database. Remaining: OpenAI, Gemini, Groq, and OpenRouter adapters; the V100 benchmark.

**RTX 5060 benchmark (2026-11-03):** `llama3.1:8b` Q4_K_M sits fully in VRAM (4.9 GiB), generates about 76 tokens/s, and answers a 300-word prompt in under 5 seconds. With the model loaded, about 1.4 GiB of the card's 8 GiB is still free, which rules out a 12B/14B model on this card. `nvidia-smi` lists only the RTX 5060; the V100 is still not visible.

**Work:** Define a model-provider interface that supports streaming text, tool-call requests, and capability flags (context length, vision, tool use, local or cloud). Implement Ollama first. Then add adapters for Anthropic Claude, OpenAI, Google Gemini, Groq, and OpenRouter using their official SDKs or HTTP APIs. Enforce the privacy mode in the provider layer itself, so a cloud call cannot bypass it. Store API keys in the operating system's credential store, never in config files or the database. Add a simple router: a manual model choice always wins; otherwise pick by rules (task type, privacy mode, required capabilities, and availability) and fall back to the next provider when one fails. Show which model answered each message. Add timeouts, clear errors, token and cost tracking for cloud providers, and a way to inspect exactly what context is being sent.

For local models, benchmark a quantized 7B/8B model on the RTX 5060; its 8 GB VRAM is shared with display workloads, so measure actual free memory and speed. Repeat the benchmark on the V100 once it is active. Try a 12B/14B model only if VRAM, context length, and speed are acceptable.

**Tools:** Ollama as the local runner (compare a CUDA-enabled llama.cpp build if Ollama does not use the V100 reliably); `httpx` and provider SDKs for cloud models; `keyring` for Windows Credential Manager, macOS Keychain, and Linux Secret Service.

**Done when:** A request can be answered with network access disabled; *Local only* mode provably blocks cloud calls (covered by tests); provider failures fall back cleanly; keys never appear in logs or responses; and automated tests run against fake providers without a live model or API key.

### 3. Build durable, user-controlled memory

**Status:** Started. Done so far:

- SQLite storage with SQLAlchemy and Alembic migrations, applied automatically on startup.
- Stored conversations; stored usage that survives restarts.
- Separate memories with provenance and a confirmed/proposed status.
- FTS5 search over messages and memories.
- Each request retrieves the most relevant confirmed memories and the reply lists which were used.
- Correcting and deleting memories; deleting conversations. Deleted text is overwritten, which a test checks.
- JSON export, and `backup` and `restore` commands. A backup has been restored to a test copy.
- Suggested memories: a model proposes categorized facts from your messages, each linked to its source message. Credentials, invented facts, and duplicates are dropped, and nothing is used until you approve it.
- Retention rules for each kind of data, built from the categories you actually have. Defaults keep everything except unapproved suggestions, which expire after 30 days. Commitments are kept until you delete them, and a pinned memory survives any rule. Rules are applied on startup, and you can preview what they would delete.
- A database upgrade from schema version 0001 has been tested: data and search survive.

Remaining:

- A regular backup schedule, and a recovery path for encryption keys.
- Optionally, suggesting memories automatically after each conversation instead of on request.

**Work:** Store conversations and explicit user facts separately. Add searchable history, timestamps, source/provenance, correction and deletion flows, and clear rules for what is retained. Retrieve relevant memory per request instead of sending everything every time, and show which memories were used. Never treat a model's guess as a confirmed fact: proposed memories need your approval, or are marked unconfirmed until you approve them.

**Tools:** SQLite for the initial single-PC database; SQLAlchemy and Alembic for data access and schema migrations; SQLite FTS5 for full-text search. Protect the PC and backups with encryption.

**Done when:** Memory survives restarts and upgrades, can be searched, exported, edited, and deleted, and a backup has been restored successfully on a test copy. Establish a regular backup schedule and a separate recovery path for any encryption keys.

### 4. Add a personal knowledge base (RAG)

**Status:** Started. File access comes first:

- First-run setup (`python -m vara setup`, or `POST /v1/access/setup` for the future apps) asks to read Documents, Downloads, and Desktop together. If declined, it asks about each, then for other folders, private subfolders, and Google Docs.
- Every read goes through one access check that resolves `..`, symlinks, and junctions. V.A.R.A.'s own data folder (which sits inside Documents on this PC), credential files, and folders such as `.ssh` and `.git` are never read.
- Permissions can be listed, added, and revoked.

- Google Docs: read-only sign-in through the Google Drive API (`python -m vara google connect`), with the client and token in Credential Manager. Docs can be listed and read as text; nothing is fetched unless Google Docs was also allowed at setup. Tested against a fake Google; the live sign-in needs the user's OAuth client from Google Cloud Console (steps in the README).

Remaining: text extraction, chunking with source locations, keyword then semantic search, cited answers, re-indexing on change (for Google Docs, by `modifiedTime`), and a live Google sign-in.

**Google Docs:** Google Drive for Desktop's `.gdoc` files are only links, so Docs are read through the Drive API; Drive for Desktop is still useful for ordinary files in Drive, allowed as a folder. Exported text will be indexed locally like any other file.

**Work:** Let you choose folders and files to index: documents, PDFs, notes, code, and past conversations. Extract text, split it into chunks, and keep each chunk linked to its source file and location. Start with keyword search (FTS5), then add semantic search with a local embedding model and combine the two. Answers that use your files must cite the passages they used. Re-index changed files, remove deleted ones, and keep indexing out of folders you have not chosen.

**Tools:** `pypdf` or `pymupdf` for PDFs; a local embedding model through Ollama; `sqlite-vec` to keep vectors in the same database; `watchdog` for detecting file changes.

**Done when:** Questions about indexed files return correct, cited answers. Excluded folders are never read. Deleting a file removes it from search. Embedding and search run locally even when a cloud model writes the answer, and only the retrieved passages are sent to that model, under the active privacy mode.

### 5. Add the agent loop, approval layer, and audit trail

**Work:** Represent every tool as a typed, narrowly scoped action with a declared risk level: read-only, local change, external effect, or financial. Build the agent loop: the model proposes a plan, the backend validates each step, runs it (or waits for approval), returns the result to the model, and reports progress. Add an approval queue that shows the exact action and target; cancellation; step limits and timeouts; duplicate-action protection; and an audit record for every action. Redact sensitive values before they reach logs or cloud models. Start with safe read-only tools (current time, memory search, knowledge search) so the loop is proven before any tool can change anything.

**Tools:** Pydantic models for action schemas; a durable local job table in SQLite; the provider interface's tool-call support from Phase 2.

**Done when:** Every action can be traced to a request, the plan step it came from, the permission used, and its result. Risky actions cannot run without approval, which tests verify. A runaway loop is stopped by its limits, and you can pause all agent activity at once.

### 6. Make the backend reachable from your devices

**Work:** Add authenticated device accounts, pairing and revocation, encrypted transport, and narrow per-device permissions. Start with access on the home network. For remote access, use a private network rather than opening a router port. Add a streaming channel (WebSocket or server-sent events) for responses, progress, and approval requests.

**Tools:** HTTPS for device traffic; Tailscale for private remote access without public port forwarding. Store device tokens in each platform's secure storage.

**Done when:** A paired device can connect, an unpaired device cannot, and a lost device can be revoked without losing your data.

### 7. Build the clients: Windows, macOS, Linux, Android

**Status:** Started, with a change of plan for Windows. The backend serves a chat page at `/` (streaming, history, model choice, privacy mode, web sources). The first Windows app is a native .NET 10 tray app (`clients/windows`) rather than Flutter: it listens for "VARA" and answers out loud (see Phase 8), with the black-circle tray icon, pause/resume, and an "Open chat" link. Android comes next (pairing and Tailscale from Phase 6 first); Linux and iOS later; iOS cannot listen for a wake word in the background.

**Work:** Build one client against the stable API with chat, history, model and privacy-mode selection, approval prompts, and a **Command Center** dashboard. The dashboard shows connection state, active model, running tasks, pending approvals, recent actions, and microphone and camera state. On desktop, add a system tray icon, a global hotkey, and launch at login. On Android, use notifications for approvals and a foreground service for anything that runs in the background. Ship Windows first, then add macOS and Linux, then Android.

**Tools:** Flutter (one codebase for all four platforms); `tray_manager` and `hotkey_manager` for desktop tray icons and hotkeys; `flutter_secure_storage` for tokens; Android permission and foreground-service APIs.

**Done when:** All four clients can pair, chat with streamed responses, approve or reject actions, display connection state, and recover from disconnects. Device permissions are requested only when a feature needs them.

### 8. Add voice

**Status:** Started, ahead of the clients phase. Done:

- Speech-to-text on the backend (`/v1/voice/transcribe`, faster-whisper `small` on the CPU, about 1.6 s for a short question), which also checks that a transcript starts with "VARA" so false wake-ups are dropped.
- Text-to-speech on the backend (`/v1/voice/speak`), Kokoro-82M with the `bf_lily` voice (British female), about 0.8 s per sentence on the CPU.
- A custom "VARA" wake word trained with openWakeWord on synthetic voices (WSL, RTX 5060), run in the Windows listener through a C# port of openWakeWord's pipeline that matches the Python reference to within 0.001.
- The listener: wake word, then it records until you stop talking, transcribes, asks chat (`spoken: true` for short plain answers), and speaks the reply sentence by sentence as it streams. Saying "VARA" interrupts it, and saying only "VARA" gets a "Yes?". Replies continue one conversation for 10 minutes.
- Each request carries the device's own clock and time zone, so the time V.A.R.A. gives is the time where you are.

Remaining: wake-word accuracy (model v5: about 63% of plain "VARA" from unfamiliar voices, "hey VARA" about 99%, about 1.5 false wake-ups per hour on noisy background audio; v3 is kept as `vara-v3.onnx` for comparison), measuring false wake-ups at home, echo cancellation so its own voice can't trigger it, a hands-free mode, and a local voice for when the backend is unreachable.

**Work:** Add speech-to-text and spoken responses, then streaming speech (start speaking before the full answer is ready), interruption (stop speaking when you talk), a wake word, and a hands-free conversation mode. The client always shows when the microphone is listening. Process voice locally by default; cloud voices are optional and follow the privacy mode. Keep text-only use available if a voice model is unavailable.

**Tools:** `faster-whisper` for local speech recognition; Piper for local speech generation; openWakeWord for the wake word; voice activity detection (for example Silero VAD) for interruption. On Android, wake-word listening must run in a visible foreground service; on macOS it needs microphone permission.

**Done when:** A full voice conversation works on the PC with networking disabled, interruption stops playback reliably, false wake-ups are measured and acceptably rare, and uncertain transcriptions can be reviewed before an action runs.

### 9. Add computer control and browser tasks

**Status:** Web search is done ahead of the rest: V.A.R.A. runs its own SearXNG (a checkout of its GitHub repository with its own environment, no Docker, updated daily with automatic rollback) and searches Google, Bing, DuckDuckGo, Brave, Startpage, Mojeek, Wikipedia, and news engines when a question needs current information. Answers name their sources. Searching looks only at the latest message so far; follow-ups such as "and the vice president?" may not search.

**Work:** Add tools for launching and closing apps, working with files and folders, searching the computer, changing supported settings (volume, display, power, and similar), web search, and browser-assisted tasks. These tools act on the machine they run on, so add a small, signed **local agent host** for each desktop OS. It is paired like a device, receives approved actions from the backend, runs them, and reports results. Implement Windows first, then macOS and Linux. Android support is limited to what the platform permits, such as opening apps and sharing content. Every tool that changes something uses the Phase 5 approval layer.

**Tools:** Python with `pywinauto` and Windows APIs on Windows, AppleScript/Shortcuts and Accessibility APIs on macOS, and D-Bus/`xdg` tools on Linux. Playwright for browser tasks in a separate browser profile; a web-search API, or a self-hosted SearXNG for privacy.

**Done when:** Each tool has tests or reproducible checks on each OS it supports. Deleting or moving files always requires approval and can be undone where possible. Browser tasks never use your main browser profile or saved passwords without explicit permission.

### 10. Add personal and developer integrations one at a time

**Work:** Add calendar read/create flows first, then email search and drafts, then communication services and phone notifications as Android and each service's policies allow, then developer services such as GitHub (issues, pull requests, notifications). Use official OAuth/API flows with minimal scopes; never ask for or store account passwords. Initially require confirmation for every write or send.

**Tools:** Google Calendar/Gmail APIs or Microsoft Graph, depending on your accounts; the GitHub API; OAuth with minimal scopes; the Phase 5 action layer.

**Done when:** Each integration has a clear permissions screen, a working revoke path, audit history, duplicate/retry handling, and tests for time zones and recurring events where applicable. Drafting is separate from sending.

### 11. Add images, OCR, and translation

**Work:** Add image understanding, screenshot and photo questions, OCR, and translation. Benchmark multimodal models separately, because their memory needs differ from text-only models. Make the camera and screen-capture state obvious.

**Tools:** A local vision model through the provider interface (cloud vision models only if the privacy mode allows them); local OCR (for example Tesseract or PaddleOCR) where it improves the task.

**Done when:** Each path has reproducible sample checks, processing stays local as configured, and uncertain readings or translations can be reviewed before you act on them.

### 12. Add bill and budget support

**Work:** Begin with manual entry and CSV import, categories, recurring bills, due-date reminders, and budget summaries. Reconcile imported transactions before considering automated bank connections. Never initiate transfers or payments.

**Tools:** The existing database and scheduler. Bank-data providers vary by country and institution; evaluate one only after location, coverage, privacy, and terms are known.

**Done when:** Totals reconcile to the imported source, uncertain categories are reviewable, reminders are reliable, and financial data is never sent to a cloud model unless you explicitly allow it.

### 13. Add custom workflows and staged automation

**Work:** Let you save multi-step workflows (for example, a morning summary of calendar, email, and bills) built from existing tools, and run them on request or on a schedule. At first, every write step still asks for approval. Later, allow specific low-risk actions to run automatically under limits you configure.

**Tools:** The Phase 5 job and approval layer, a durable local scheduler, rate limits, and per-integration permission scopes.

**Done when:** Every automated action can be traced to a workflow and permission, risky actions still require confirmation, and automation can be paused globally or revoked per integration. Expand autonomy only after a period of successful supervised use.

### 14. Harden, package, and maintain

**Work:** Add migration and recovery drills, dependency and security updates, diagnostics, redacted logs, resource limits, and a documented upgrade/rollback process. Decide whether the backend starts at login or runs as a managed service. Package each client: a signed Windows installer (MSIX), a notarized macOS app, Linux AppImage or Flatpak, and an Android APK or Play Store build.

**Tools:** Pytest and Flutter tests in continuous integration on all four platforms, Ruff, dependency scanning, encrypted backups, and Windows Task Scheduler (later a service) for starting the backend.

**Done when:** A clean machine can be set up from the documentation, backups restore, upgrades are repeatable, every client installs on its platform, and no secrets or personal data are committed to the repository.

## Next Implementation Session

Test the voice listener with real use (wake-word hit rate and false wake-ups at home) and keep improving the wake word. Then Phase 5's agent loop and approval layer, which the custom-agents and self-upgrade goals in **To Do** build on; web search becomes its first tool. Still open from earlier: Phase 4 text extraction and keyword search for allowed folders; Phase 3's scheduled off-PC backup; the OpenAI adapter once its key is in Credential Manager.

## To Do

Goals that span several phases. Each one builds on the Phase 5 action layer, so everything it does stays
typed, approved, audited, and reversible.

- [ ] **V.A.R.A. builds custom agents when a request needs one.** When a request is bigger than a single
  answer or tool call (for example "watch these three sites and tell me when prices drop", or "research X
  and write me a summary every Monday"), V.A.R.A. designs a purpose-built agent for it: its goal, the tools
  it may use, its permissions, limits, schedule, and how it reports back.
  - Agents are definitions V.A.R.A. writes (Pydantic schemas plus instructions), run by the Phase 5 agent
    loop, not free-form code. An agent may only use tools that already exist, within the permissions it
    was granted.
  - The owner approves each new agent before it first runs, seeing exactly what it will do, which tools and
    data it can touch, and when it runs. Later runs need no approval while they stay inside that scope;
    anything outside it asks again.
  - Agents can be listed, paused, edited, and deleted, and each run has an audit history.
  - If a request needs a tool that doesn't exist yet, V.A.R.A. proposes building it through the
    self-upgrade pipeline below instead of working around the gap.
  - The owner has said they will fully support this, so it is a priority once Phase 5 is in place.
- [ ] **V.A.R.A. upgrades itself.** V.A.R.A. notices changes: new hardware such as the V100, new or updated
  models, new API keys, dependency and security updates, and its own recurring failures. It works out what
  should change and prepares the upgrade.
  - Code changes are written by Claude Code (Agent SDK) in a separate git worktree.
  - Every upgrade is tested before it is offered: the full test suite, lint, and smoke checks against a
    trial server.
  - The owner approves each upgrade in an Upgrades panel that shows the change, the reason, the diff, and
    the test results.
  - Before applying, it backs up the database and tags the commit; if health checks fail afterwards, it
    rolls back automatically.
  - It never changes privacy enforcement, file-access checks, credential handling, or the upgrade pipeline
    itself without the owner's review.
  - Order: detection, testing, and rollback for configuration and dependency upgrades first; code changes
    and the approval screen after that.

## Decisions to Confirm Before Model and Device Work

- Available disk space, and whether Windows and `nvidia-smi` now detect the V100. At the last check, `nvidia-smi` listed only the RTX 5060 and Windows reported the V100 as a phantom device. Check the OCuLink/PCIe link width and speed if performance seems unexpectedly low.
- Future server OS, system RAM, and GPU configuration when known. Keep the model-host boundary replaceable; benchmark multi-GPU serving on that server rather than assuming multiple V100s will make one model proportionally faster.
- The default privacy mode, and which cloud providers you actually want keys for.
- Which calendar, email, and communication providers you use, and your Android phone model/version.
- Whether you have Mac and Linux machines available for testing clients and the local agent host.
- Your country, before evaluating financial-data connections.
