# Deploying on a small shared server

Runs the bot, the Celery worker, Postgres and Redis in Docker with memory/CPU limits, so it can
sit next to another site. Budget: roughly 1.5 GB RAM at peak and 10-15 GB of temporary disk per
long session (raw audio is deleted after the report is posted).

1. Install Docker and the compose plugin on the server.
2. Optional but recommended on machines without swap: `fallocate -l 2G /swapfile && chmod 600
   /swapfile && mkswap /swapfile && swapon /swapfile` and add it to `/etc/fstab`.
3. Copy the repo to the server, then `cp .env.example .env` and fill in:
   `DISCORD_BOT_TOKEN`, `ASSEMBLYAI_API_KEY`, `ANTHROPIC_API_KEY`, and a new `POSTGRES_PASSWORD`.
   `chmod 600 .env`. (`DATABASE_URL`, `REDIS_URL` and `AUDIO_STORAGE_PATH` are set by the compose file.)
4. `docker compose -f docker-compose.prod.yml up -d --build`
5. `docker compose -f docker-compose.prod.yml logs -f bot worker`

Update: `git pull && docker compose -f docker-compose.prod.yml up -d --build`.

The bot connects outbound only; no ports need to be opened. Running a second copy of the bot
(for example on your PC) with the same token while this one runs will make both answer commands.
