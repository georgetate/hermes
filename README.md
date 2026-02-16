# Hermes
Hermes is a modular personal-assistant backend focused on email and calendar workflows.
The project uses a ports-and-adapters (hexagonal) design so provider integrations can be swapped with minimal changes to business logic.

## Architecture
- `src/hermes/ports/email.py` and `src/hermes/ports/calendar.py` define provider-agnostic contracts for messaging and scheduling.
- `src/hermes/ports/storage.py` defines persistence contracts for cached domain objects and sync cursors.
- Adapter modules (Google, SQLite, and future providers) implement those contracts without leaking provider-specific shapes into core types.
- This structure keeps auth, transport, normalization, and storage concerns isolated and testable.

## Current capabilities
- OAuth authentication for Google APIs.
- Gmail read and draft/send workflows.
- Google Calendar read and write workflows.
- Provider payload normalization into internal Python DTOs.
- SQLite persistence for threads, events, and sync cursors.

## In progress
- LLM interaction layer (`src/hermes/ports/llm.py` and `src/hermes/adapters/openai/` placeholders).
- CLI and API entrypoints (`src/hermes/app/` placeholders).

## Tech stack
- Python 3.11+
- Google Gmail API
- Google Calendar API
- OAuth 2.0
- SQLite
- JSON-based payload normalization

## Local setup notes
- Runtime configuration lives in `.env` and `src/hermes/config.py`.
- Place OAuth credentials under `.credentials/` (see `GoogleOAuthPaths` in `src/hermes/config.py`).

### Google OAuth quick setup
1. In Google Cloud Console, enable:
   - Gmail API
   - Google Calendar API
2. Configure OAuth consent screen (External or Internal) and add your test user.
3. Create OAuth client credentials:
   - Application type: Desktop app
   - Download the JSON
4. Save the downloaded file as `.credentials/credentials.json`.
   - A safe template is provided at `.credentials/credentials.example.json`.
5. Confirm `.env` has:
   - `GOOGLE_CLIENT_SECRETS_PATH=.credentials/credentials.json`
   - `GOOGLE_TOKEN_PATH=.credentials/token.json`
6. Run any command/path that initializes a Google client.
   - On first run, browser OAuth opens and writes `.credentials/token.json`.
