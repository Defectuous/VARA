# V.A.R.A.

Visual and Audio Reality Assistant. The initial backend is a local-only FastAPI service.

## Quick start

Double-click `start-vara.cmd`, or run:

```powershell
.\start-vara.ps1
```

It uses the Ollama at `VARA_OLLAMA_URL` (here, the server defectserver; a local Ollama is started only when
that URL points at this PC), loads the model into GPU memory in the background, starts the
backend hidden (logs in `data\logs`), and opens the chat page at <http://127.0.0.1:8000/>. Anything
already running is left alone, so it is safe to run again.

| Command | What it does |
| --- | --- |
| `.\start-vara.ps1 -Stop` | Stop the backend (Ollama keeps running) |
| `.\start-vara.ps1 -InstallAtLogin` | Start V.A.R.A. automatically when you sign in (a Task Scheduler task) |
| `.\start-vara.ps1 -RemoveAtLogin` | Undo that |
| `.\start-vara.ps1 -NoBrowser` | Start without opening the chat page |

The chat page streams replies, keeps your conversation history in the sidebar, lets you pick a model
(or *Auto*), and shows the privacy mode. Cloud models are greyed out in *Local only* mode. In *Ask* mode
it asks before anything goes to a cloud model. It uses only the public API, like the future apps will.

## Setup

Install Python 3.12 with `uv` and sync the project environment:

```powershell
uv python pin 3.12
uv sync
```

Run the checks:

```powershell
uv run pytest
uv run ruff check .
```

Start the API bound to this computer only:

```powershell
uv run python -m vara
```

The health endpoint is available at `http://127.0.0.1:8000/v1/health` (and unversioned at `/health`).
Interactive API docs are at `/docs` in development.

## Configuration

Settings come from `VARA_*` environment variables or a local `.env` file (never committed):

| Variable | Default | Notes |
| --- | --- | --- |
| `VARA_ENVIRONMENT` | `development` | `production` disables `/docs` and the OpenAPI schema |
| `VARA_HOST` | `127.0.0.1` | Anything other than localhost logs a warning |
| `VARA_PORT` | `8000` | |
| `VARA_LOG_LEVEL` | `INFO` | |
| `VARA_DATA_DIR` | `data` | Holds `vara.db` and `backups/`; ignored by Git |
| `VARA_RETENTION_ON_STARTUP` | `true` | Apply retention rules each time the server starts |
| `VARA_PRIVACY_MODE` | `local_only` | `local_only` blocks all cloud calls; `ask` needs `confirm_cloud: true` per request; `allowed` permits them |
| `VARA_OLLAMA_URL` | `http://127.0.0.1:11434` | Point at another machine to use its Ollama (that server needs `OLLAMA_HOST=0.0.0.0` and port 11434 open); `start-vara.ps1` then won't start a local one |
| `VARA_OLLAMA_MODEL` | `llama3.1:8b` | |
| `VARA_PERSONALITY` | dry, snarky wit when the moment allows | V.A.R.A.'s character, added after the system prompt. The default lets her quip during casual chat and drops it for serious, stressful, or urgent topics. Set your own description, or empty for none |
| `VARA_HOLD_NOTICE_SECONDS` | `6` | When a reply hasn't started after this long (a model loading, long reasoning, a web search), V.A.R.A. says it's going to take a minute (in her own snarky way when a personality is set), in the chat page and out loud. `0` turns it off |
| `VARA_HOLD_REPEAT_SECONDS` | `120` | After that, "still working on it" this often until the reply starts. `0` for just the one notice |
| `VARA_OLLAMA_MODELS` | (none) | More Ollama models for the chat page's Model menu, comma-separated (e.g. `qwen3:14b`). Switching models reloads it into GPU memory, so the first reply after a switch is slower |
| `VARA_OLLAMA_KEEP_ALIVE` | `2h` | How long the model stays in GPU memory after a request; `-1` keeps it loaded. Reloading takes about a minute |
| `VARA_REASONING` | `auto` | For models that can reason (e.g. qwen3). `auto` decides per message: small talk and quick facts answer straight away, while "why"/"explain"/compare/plan/code/maths questions and long messages reason first (spoken replies reason only for clearly hard ones). `on`/`off` force it; a request's `reasoning` overrides this |
| `VARA_SYSTEM_PROMPT` | V.A.R.A. persona | Used when a request has no `system` of its own; `{date}` becomes today's date. A request's `system` replaces it (`""` sends none) |
| `VARA_ANTHROPIC_MODEL` | `claude-opus-5-5` | |
| `VARA_ANTHROPIC_EFFORT` | `medium` | `low`, `medium`, `high`, `xhigh`, or `max` |

Logs are JSON lines, with API keys, bearer tokens, and password/secret fields redacted.

API keys are not configuration. Store them in Windows Credential Manager under the service `vara`:

```powershell
uv run --with keyring python -c "import keyring, getpass; keyring.set_password('vara', 'anthropic', getpass.getpass('Key: '))"
```

A cloud provider is enabled only when its key is present. Check with `GET /v1/providers`.

## Chat

```powershell
$body = @{ messages = @(@{ role = "user"; content = "Hello" }) } | ConvertTo-Json -Depth 3
Invoke-RestMethod http://127.0.0.1:8000/v1/chat -Method Post -ContentType application/json -Body $body
```

Every exchange is stored. The response includes `conversation_id`; to continue that conversation, send it
back with only the new message, and the stored history is included automatically.

Optional fields: `conversation_id`, `provider` (for example `"anthropic"`; a manual choice never falls back),
`model`, `system`, `confirm_cloud` (required in `ask` mode), and `use_memory` (default `true`). Without a
provider, local models are tried before cloud ones. The response names the provider and model that answered,
with token usage, estimated cost (`0.0` for local models, `null` for a cloud model without a known price), and
`memories_used`, which lists the memories that were added to the request.

Other chat endpoints take the same body:

| Endpoint | What it does |
| --- | --- |
| `POST /v1/chat/stream` | Server-sent events: `delta` per text chunk, then `done` (usage and cost) or `error`. Privacy and provider failures before the first chunk are normal HTTP errors. |
| `GET /v1/events` | Live server-sent events for every chat exchange from any client: `exchange_started` (the question; `source` is `voice` or `chat`), `exchange_delta`, then `exchange_done` or `exchange_failed`. Each carries the sender's `client` id (sent in the chat request), so a page can skip its own. The chat page uses this to show voice conversations as they happen. |
| `POST /v1/chat/preview` | Calls no model and stores nothing. Lists the memories that would be used and the providers that would be tried, in order, with the exact request each would receive. |

A failed or blocked request stores nothing.

## Voice

Speech is transcribed on this PC with faster-whisper; audio never leaves it and nothing is stored.

```powershell
Invoke-RestMethod http://127.0.0.1:8000/v1/voice/transcribe -Method Post -ContentType audio/wav -InFile question.wav
```

The body is a 16-bit PCM WAV file (any sample rate, mono or stereo, up to 10 MB). The response has the
`text`, `wake_word` (whether it starts with "VARA", allowing "hey"/"ok" before it and spellings such as Vara,
Vera, or V.A.R.A.), and `request`, the part after the wake word, to send to `/v1/chat`. Clients keep a moment
of audio from before the wake-word detector fired, so a transcript that doesn't start with VARA is a false
wake-up and is ignored.

The model loads on the first request (a few seconds; downloaded once). On the Ryzen 7 5800X, `small` on the
CPU transcribes a 3.5 s question in about 1.6 s. The CPU is the default because the chat model fills most of
the RTX 5060's memory.

| Variable | Default | Notes |
| --- | --- | --- |
| `VARA_WHISPER_MODEL` | `small` | `base` is faster, `large-v3-turbo` more accurate |
| `VARA_WHISPER_DEVICE` | `cpu` | `cuda` also needs the cuBLAS and cuDNN libraries |
| `VARA_WHISPER_COMPUTE_TYPE` | `int8` | `int8_float16` on `cuda` |

### Speaking

V.A.R.A.'s voice is Kokoro-82M, run locally: the same voices as SiteRecorder and SiteReader.

```powershell
$body = @{ text = "Good morning." } | ConvertTo-Json
Invoke-WebRequest http://127.0.0.1:8000/v1/voice/speak -Method Post -ContentType application/json -Body $body -OutFile hello.wav
```

`POST /v1/voice/speak` takes `text` (up to 1,000 characters), and optionally `voice` and `speed` (0.5-2.0), and
returns a 24 kHz WAV. `GET /v1/voice/voices` lists the voices. "V.A.R.A." is read as "Vara", not spelled out.
About 0.8 s for a sentence on the CPU.

| Variable | Default | Notes |
| --- | --- | --- |
| `VARA_TTS_VOICE` | `bf_lily` | British female (read with British pronunciation). Others: `bf_alice`, `bf_emma`, `bf_isabella`; US female `af_heart`, `af_bella`, `af_nicole`, `af_sarah`, `af_nova`, ... |
| `VARA_TTS_SPEED` | `1.0` | |

The model files (about 120 MB) go in `data\models\kokoro`:

```powershell
New-Item -ItemType Directory -Force data\models\kokoro | Out-Null
foreach ($f in "kokoro-v1.0.int8.onnx", "voices-v1.0.bin") {
  curl.exe -L -o "data\models\kokoro\$f" "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/$f"
}
```

## Voice listener (Windows)

`clients/windows/` is a .NET 10 tray app that listens for "VARA" and answers out loud. It sits in the
system tray (the black-circle icon) and does no thinking itself: the backend transcribes, answers, and keeps
the conversation.

1. You say "VARA" (or "hey VARA"). The wake-word model runs on this PC's CPU, on 80 ms chunks of audio;
   nothing leaves the microphone until it fires.
2. A chime, then it records until you stop talking, keeping the moment before so the clip starts with "VARA".
3. The backend transcribes it. If the transcript doesn't start with VARA it was a false wake-up and is dropped.
4. The request goes to chat (`spoken: true`, which asks for a short, plain answer), and the reply is spoken
   sentence by sentence as it streams. Say "VARA" to interrupt.

Say only "VARA" (or pause after it) and it answers "Yes?" and listens once more without needing the name.
After the wake word it waits up to 2.5 s for you to start talking (4 s after "Yes?"), then stops at 0.9 s of
silence. Requests within
10 minutes of each other continue the same conversation, so they show up together in the chat page.

Build it (needs the .NET 10 SDK and the trained `models/vara.onnx`):

```powershell
dotnet publish clients\windows\Vara.Listener -c Release -o clients\windows\publish
```

`start-vara.ps1` then starts it with everything else (`-NoListener` to skip it). Windows must allow desktop
apps to use the microphone (Settings > Privacy & security > Microphone). Settings are in
`clients\windows\publish\appsettings.json`:

| Setting | Default | Notes |
| --- | --- | --- |
| `Threshold` | `0.5` | Wake-word score (0-1) that counts as "VARA"; raise it if it wakes by mistake |
| `ThresholdWhileSpeaking` | `0.8` | Higher while it talks, so its own voice is less likely to trigger it |
| `MicrophoneDevice` | `-1` | Windows recording device number; `-1` is the default microphone |
| `Voice` | `bf_lily` | A Kokoro voice from the backend (see Speaking) |
| `FallbackVoice` | `Microsoft Zira Desktop` | Windows voice used if the backend can't speak |
| `EndSilenceSeconds` | `0.9` | Silence that ends a request |
| `ConversationIdleMinutes` | `10` | After this long, the next request starts a new conversation |

The log (`%LOCALAPPDATA%\VARA\logs\listener.log`, or **Open log** in the tray menu) records events only:
never audio or what was said.

The wake-word pipeline is a C# port of openWakeWord's. `dotnet test clients\windows` checks it against
features and scores produced by openWakeWord's own Python code (`tools/wakeword/make_parity_fixtures.py`).

## Web search

V.A.R.A. searches the web when a question needs current information (who holds an office, news, prices,
weather, scores, recent releases). It uses its own **SearXNG**, a metasearch engine that sends one search to
Google, Bing, DuckDuckGo, Brave, Startpage, Mojeek, Wikipedia/Wikidata, and their news engines at once and
merges the results. The top results go to the model with the engines that agreed on each, and the reply says
where the answer came from; the chat page lists the sources under it. Only the search words leave this PC:
never memories, files, or conversation history.

Deciding to search costs nothing for most messages: a quick word check skips anything without a sign of needing
current information, and only then is the model asked whether to search and for what. "Search for ..." or
"look up ..." searches directly. A failed search never fails the chat; V.A.R.A. just answers without it. Some
engines (Google and DuckDuckGo especially) block automated searches at times; the others still answer.

SearXNG is a **module**: a checkout of its GitHub repository in `data\modules\searxng` with its own Python
environment, started and watched by V.A.R.A. on `127.0.0.1:8890`. No Docker. Once a day V.A.R.A. checks GitHub
for new SearXNG commits; if there are any it updates, restarts SearXNG, and runs a test search, rolling back to
the previous version if that fails. Its settings are in `vara\search\searxng_settings.yml`.

### Deals and price watches

Ask in chat or by voice: "find me the best deal on a Samsung 990 Pro 2TB", "where can I buy AirPods Pro 3
cheapest?", "tell me when the Dyson V15 drops below $450", "what prices are you watching?", "stop watching the
Dyson". V.A.R.A. never buys anything; it only reads prices.

- **Sources:** Slickdeals' public deal feeds (front-page and community posts from the last 45 days), store and
  manufacturer pages found by web search (only where robots.txt allows it, read for their published price data),
  and Best Buy's official API if you add a free key from developer.bestbuy.com:
  `uv run python -m vara deals bestbuy-key`. Amazon, Walmart and Target pages are never read (their terms
  forbid it); their deals still appear through Slickdeals.
- **New items only.** Refurbished, renewed, open-box and used offers, forum questions, and down-voted posts are
  dropped. Model numbers and sizes must match exactly ("990 Pro 2TB" doesn't match a 9100 Pro or a 4TB).
- **Watches** are re-checked every 6 hours. An alert fires when a fresh offer (a store price, or a deal posted
  in the last 7 days) is at or below your target, or, with no target, 5% below the lowest price seen. It shows
  as a banner on the chat page and V.A.R.A. mentions it the next time you talk to it.

| Endpoint | What it does |
| --- | --- |
| `GET /v1/deals?q=` | Prices for `q` now, cheapest first |
| `GET/POST /v1/deals/watches`, `DELETE /v1/deals/watches/{id}` | List, add (`query`, optional `target_price`), or stop watches |
| `GET /v1/deals/alerts?unseen=true`, `POST /v1/deals/alerts/{id}/seen` | Price alerts |

### Automatic updates

Starting V.A.R.A. (`start-vara.cmd`) keeps everything that fetches information current:

| What | When | Safety check | Result recorded in |
| --- | --- | --- | --- |
| SearXNG | At backend startup, then daily while running | Test search; rolls back on failure | `data\modules\searxng\status.json` |
| Ollama models (every installed model) | At backend startup, then daily while running | An unchanged model is only compared, not downloaded | `data\modules\ollama-updates.json` |
| Python packages (`uv.lock`) | In `start-vara.ps1` before the backend starts, at most once a day | Tests and lint must pass, or the previous `uv.lock` is restored | `data\modules\packages-update.json` |

`VARA_MODEL_UPDATES=off` stops the model pulls; `VARA_WEB_SEARCH=off` stops SearXNG altogether. Package upgrades
change `uv.lock` in the working tree, so they appear in `git status` to commit.

```powershell
uv run python -m vara search install   # once: clone SearXNG and set up its environment
uv run python -m vara search status    # installed/running, version, last update check
uv run python -m vara search update    # check for an update now
```

| Endpoint | What it does |
| --- | --- |
| `GET /v1/web/search?q=` | Search directly (`category=news` for news) |
| `GET /v1/web/status` | Installed, running, version, and the last update check |
| `POST /v1/web/update` | Check for a SearXNG update now |

`VARA_WEB_SEARCH=off` turns searching off; a chat request can also send `"web_search": false`.

On Windows (SearXNG is built for Linux, V.A.R.A.'s eventual host) the module skips a folder of Linux server
templates whose file names Windows can't store, and adds a stand-in for the Unix-only `pwd` module to SearXNG's
own environment. Neither is needed on Linux.

## Memory and history

Memories are facts you choose to keep, stored separately from conversation history. For each request, the
confirmed memories that best match the latest message (keyword search, up to five) are added to the system
prompt. Memories marked `proposed` are never used until you confirm them.

| Endpoint | What it does |
| --- | --- |
| `GET /v1/memories?status=` | List memories, optionally only `confirmed` or `proposed` |
| `POST /v1/memories` | Add one: `content`, optional `category`, `status`, `pinned`, and `source_message_id` (provenance) |
| `PATCH /v1/memories/{id}` | Correct `content` or `category`, approve with `status: "confirmed"`, or set `pinned` |
| `POST /v1/conversations/{id}/suggest-memories` | A model proposes facts from your messages in that conversation (see below) |
| `DELETE /v1/memories/{id}` | Delete it, including from the search index |
| `GET /v1/conversations`, `GET /v1/conversations/{id}` | List conversations, or one with its messages |
| `PATCH` / `DELETE /v1/conversations/{id}` | Rename, or delete with all its messages |
| `GET /v1/search?q=` | Full-text search over messages (with snippets) and memories |
| `GET /v1/usage` | Requests, tokens, and cost per provider and model, all time |
| `GET /v1/export` | Everything as one JSON document |

Deleted text is overwritten in the database file, not just hidden.

### Suggested memories

`suggest-memories` sends only your own messages from one conversation to a model, routed under the privacy
mode like chat (optional `provider`, `model`, `confirm_cloud`). Each suggested fact gets a category and a link
to the message it came from, and is saved as `proposed`. Suggestions are dropped if they look like a
credential, share no meaningful word with what you wrote (which catches invented facts), or duplicate an
existing memory. Approve a suggestion with `PATCH /v1/memories/{id}` and `status: "confirmed"`.

Categories: `identity`, `preference`, `relationship`, `location`, `work`, `health`, `finance`, `commitment`,
and `other`.

### Retention

There is one rule for each kind of data. `keep_days: null` means keep until you delete it.

| Rule | Default |
| --- | --- |
| `conversations` | Keep (counted from the last message) |
| `proposed` (unapproved suggestions) | 30 days |
| Every memory category, `commitment` included | Keep |

`GET /v1/retention` lists every rule with how much you have saved of each kind, the categories you use most
first, and flags `health` and `finance` as sensitive. Change a rule with `PUT /v1/retention/{key}` and
`{"keep_days": 90}` or `{"keep_days": null}`. A **pinned** memory is never deleted by any rule, so you can keep
particular items when a category has a time limit. `GET /v1/retention/due` shows what the current rules would
delete, so you can pin items first. Rules are applied when the server starts (`VARA_RETENTION_ON_STARTUP=false`
turns that off) and by `POST /v1/retention/apply`.

## File access

V.A.R.A. reads none of your files until you allow it. Run the first-run setup in a terminal:

```powershell
uv run python -m vara setup
```

It offers Documents, Downloads, and Desktop together, plus `My Drive` if Google Drive for Desktop is
installed. If you decline, it asks about each one, then for any
other folders, then for folders inside those to keep private, and whether it may read your Google Docs (see
[Google Docs](#google-docs) for connecting the account). Running it again replaces your choices. The
server logs a warning on startup until setup is done.

Some things are never read, whatever you allow: V.A.R.A.'s own data folder; files such as `.env`, private
keys (`id_rsa`, `*.pem`, `*.key`, `*.pfx`), and password databases (`*.kdbx`); and folders named `.ssh`,
`.git`, `AppData`, or `node_modules` inside an allowed folder. Drive placeholders (`.gdoc`, `.gsheet`) are
skipped; Google Docs are read through Google instead. Paths are resolved before checking, so `..`,
shortcuts, symlinks, and junctions cannot reach outside an allowed folder.

| Endpoint | What it does |
| --- | --- |
| `GET /v1/access` | Whether setup is done, the suggested folders, your current permissions, and Google Docs status |
| `POST /v1/access/setup` | The same choices as the terminal setup: `folders` (`documents`, `downloads`, `desktop`, `google_drive`, or full paths), `exclude`, `google_docs` |
| `POST /v1/access` | Add one permission: `kind` (`folder`, `exclude`, `google_docs`) and `path` |
| `DELETE /v1/access/{id}` | Revoke one |
| `GET /v1/access/check?path=` | Whether a path may be read, and why |

### Google Docs

V.A.R.A. reads Google Docs through the Google Drive API with read-only access: it can list and read your
documents but never change, share, or delete anything. It needs both the Google Docs permission from setup and
a connected account. Google Drive for Desktop does not help here, because its `.gdoc` files are only links;
it is still useful for ordinary files in Drive (PDFs, Word files), which you can allow as a folder, such as
`G:\My Drive`.

One-time setup in [Google Cloud Console](https://console.cloud.google.com/), signed in with the Google account
whose documents you want:

1. Create a project (for example `VARA`).
2. **APIs & Services → Library**: enable the **Google Drive API**.
3. **Google Auth Platform → Branding**: fill in the app name and your email. **Audience**: choose
   **External**, and add your own address as a test user.
4. **Clients → Create client**: application type **Desktop app**. Download the JSON file.
5. **Audience → Publish app**. In *Testing*, Google expires the sign-in every 7 days; once published, it
   lasts until you revoke it. Google will not review an app only you use, so the sign-in page warns that it
   is unverified: choose **Advanced**, then go to the app.

Then connect:

```powershell
uv run python -m vara google connect C:\path\to\client_secret.json   # opens the browser to sign in
uv run python -m vara google status
uv run python -m vara google disconnect                               # revokes access at Google too
```

The client and the sign-in token are stored in Windows Credential Manager (`vara/google-client` and
`vara/google`), so you can delete the downloaded JSON file afterwards. You can also remove access at any time
at [myaccount.google.com/permissions](https://myaccount.google.com/permissions).

| Endpoint | What it does |
| --- | --- |
| `GET /v1/google/docs?limit=` | Your Google Docs, most recently modified first (not including the bin) |
| `GET /v1/google/docs/{id}/text` | One document as plain text |

Both return 403 until Google Docs is allowed in setup, and 409 with instructions if the account is not
connected or the sign-in has expired.

## Backups

```powershell
uv run python -m vara backup                       # writes data/backups/vara-<timestamp>.db, verified
uv run python -m vara restore <backup.db> --yes    # stop the server first; replaces the database
```

A backup can be taken while the server is running. Store copies off this PC, and keep the drive encrypted
(BitLocker), because the database holds your conversations and memories in readable form.

Keep the service bound to localhost until device authentication is implemented.

## Credits

Special thanks to Duckie (he knows who he is) for his insightful ideas and testing.
