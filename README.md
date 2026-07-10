# worklog

**Your career generates data every day. You're throwing it away.**

`worklog` is a local-first, AI-powered work journal in a single Python file. Type one messy sentence about your day — a local LLM structures it into typed, searchable career events (wins, decisions, collaborations), stored in one SQLite file you own forever. At review season, ask questions in plain English and get answers grounded in your own history.

No cloud. No subscription. No API keys. Zero dependencies. Your entire career — across every company, role, and promotion — in one file that will still open in 2060.

```
$ worklog log "Shipped the caching fix on Atlas, 40% faster. Helped Priya debug the ETL."
$ worklog ask "what were my biggest wins this quarter?"
```

**Why this exists:** performance reviews, promo packets, and resumes all fail for the same reason — human memory. Six months of real impact compresses into three bullet points written the night before. `worklog` fixes the memory, and the AI does the structuring, so capture costs you ten seconds a day.

---

## How it works

```
free text  →  LLM triage (local)  →  SQLite  →  embeddings (local)  →  search / ask
```

1. **Capture** — one command, plain English, no forms
2. **Triage** — a local LLM extracts structured events: type, summary (resume voice), impact, project, people, skills
3. **Store** — one SQLite file; raw text is immutable and never lost
4. **Recall** — semantic search over embeddings, or RAG-based Q&A answered in prose

---

## Commands

| Command | What it does |
|---|---|
| `worklog log "text"` | Log a free-text entry for today (`--date YYYY-MM-DD` for past days) |
| `worklog company add "Name" --start DATE` | Start a new job — auto-closes the previous company and role |
| `worklog role add "Title" --start DATE` | Add a role at your current company |
| `worklog promote "New Title" --date DATE` | Record a promotion — closes current role, opens the new one |
| `worklog career` | Show your full company/role timeline |
| `worklog list` | Browse recent entries (`--since`, `--until`, `--grep`, `--limit`) |
| `worklog triage` | AI-extract structured events from unprocessed entries |
| `worklog embed` | Generate embeddings for new events, making them searchable |
| `worklog search "query"` | Semantic search — finds by meaning, not keywords (`--top N`) |
| `worklog ask "question"` | Ask in plain English, get a prose answer grounded in your entries (`--sources` shows evidence) |
| `worklog export --since DATE --until DATE` | Markdown report grouped by event type — your self-review, pre-written |
| `worklog status` | DB location, current company/role, entry counts |

The weekly ritual: `worklog triage && worklog embed` — everything else is just logging.

---

## Requirements

- **Python 3.9+** (macOS ships with it; no packages to install — the script is pure stdlib)
- **[Ollama](https://ollama.com)** — only needed for the AI commands (`triage`, `embed`, `search`, `ask`). Logging, career tracking, and export work without it.

### Ollama setup

```bash
# 1. Install Ollama (macOS)
brew install ollama          # or download from https://ollama.com/download

# 2. Start the Ollama server (keep it running; the app does this automatically)
ollama serve

# 3. Pull the embedding model (~270MB)
ollama pull nomic-embed-text:v1.5

# 4. Pull the chat model for triage and Q&A (~1.9GB)
ollama pull qwen2.5:3b
```

### Install worklog

```bash
git clone git@github.com:abhaypaii/worklog.git
cd worklog

# Add to ~/.zshrc:
export WORKLOG_DB="$HOME/path/to/worklog.db"          # where your database lives
export WORKLOG_EMBED_MODEL="nomic-embed-text:v1.5"
export WORKLOG_CHAT_MODEL="qwen2.5:3b"
alias worklog='python3 "$HOME/path/to/worklog/worklog.py"'

source ~/.zshrc
worklog status    # creates the database on first run
```

Any Ollama models work — set `WORKLOG_CHAT_MODEL` and `WORKLOG_EMBED_MODEL` to your preference. If you change the embedding model later, re-embed everything (embeddings from different models are incompatible); see `CHEATSHEET.md`.

---

## Design principles

- **Raw text is sacred.** Entries are immutable ground truth. Extraction can always be re-run as models improve — nothing is ever lost to a bad parse.
- **Boring storage wins.** One SQLite file: syncs anywhere (iCloud/Dropbox), edits in any DB browser, opens forever. Brute-force cosine search is sub-second even at a full career's scale — no vector database required.
- **Local by default.** Your career history is yours. Nothing leaves your machine.

## License

MIT
