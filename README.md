# Assistant

Personal Telegram assistant powered by OpenAI. The application source and Docker Compose deployment configuration are included.

## Prepare deployment

The Compose file builds the application from `source/`. Configure these values through Dokploy or another secret manager; do not commit credentials:

- `OPENAI_API_KEY`
- `OWNER_TELEGRAM_ID`
- `TG_API_ID`

Create the secret files referenced by Compose in the ignored `secrets/` directory:

- `bot_token`
- `data_key`
- `internal_token`
- `tg_api_hash`

The optional integration tokens are described by the environment variable names in `compose.yml`. Add only the integrations you use.

## Runtime data

The `assistant_data` and `profile_state` Docker volumes store the assistant database and encrypted Telegram profile session. They are runtime data and are not included in this repository. Keep encrypted backups separately.

Never commit API keys, Telegram tokens or sessions, database files, or backups. The repository `.gitignore` excludes these paths and common local model artifacts.
