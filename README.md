# Hermes
Hermes is a personal assistant that is focused on email and calendar services. The project uses an interface based design, so new providers can be added with minimal changes to other layers of the repository.

## Architecture
- `src/hermes/ports/email.py` and `src/hermes/ports/calendar.py` define contracts for messaging and scheduling that a specific provider must fulfill.
- `src/hermes/ports/storage.py` defines the interface for the data persistence service.
- Adapter modules (Google, SQLite, and future providers) implement those contracts and connect to the email and calendar interfaces.
- This structure keeps auth, transport, normalization, and storage services independent and testable.

## Current capabilities
- OAuth authentication for Google APIs.
- Gmail read, drafting, and sending capabilities.
- Google Calendar read and write capabilities.
- Provider payload normalization into internal Python DTOs.
- SQLite persistence for threads, events, and sync cursors.
- LLM interaction layer.
- CLI entrypoint.

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
5. Confirm that `.env` has:
   - `GOOGLE_CLIENT_SECRETS_PATH=.credentials/credentials.json`
   - `GOOGLE_TOKEN_PATH=.credentials/token.json`
6. Run any command that initializes a Google client.
   - On first run, the browser OAuth opens and writes a token to `.credentials/token.json`.
