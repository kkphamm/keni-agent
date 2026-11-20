# Local Voice Agent

Local Voice Agent is a native macOS app that takes spoken requests, streams a live transcript, and shows an outcome for each action. V1 opens Mac apps and the configured IRDX repository in VS Code, integrates Spotify track search and playback, and keeps local action history across app restarts.

## Set up PostgreSQL once

Install PostgreSQL 18 before running `./scripts/start`. [Homebrew PostgreSQL 18](https://formulae.brew.sh/formula/postgresql@18)

```sh
brew install postgresql@18
```

Keep PostgreSQL listening on loopback. For a fresh Homebrew cluster, replace `trust` with `peer` for Unix sockets and with `scram-sha-256` for loopback TCP in `$(brew --prefix)/var/postgresql@18/pg_hba.conf` before starting the service. [PostgreSQL authentication](https://www.postgresql.org/docs/18/auth-pg-hba-conf.html)

```sh
brew services start postgresql@18
pg_bin="$(brew --prefix postgresql@18)/bin"
"$pg_bin/psql" -X -d postgres
```

As the local PostgreSQL administrator, create the role and database. `\password` prompts for a password without putting it in shell history.

```sql
CREATE ROLE keni_app LOGIN NOINHERIT;
\password keni_app
CREATE DATABASE keni;
REVOKE ALL ON DATABASE keni FROM PUBLIC;
GRANT CONNECT ON DATABASE keni TO keni_app;
\q
```

From the project directory, install the schema and grant the role access to the history table.

```sh
"$pg_bin/psql" -X -v ON_ERROR_STOP=1 -d keni -f migrations/001_action_runs.sql -f migrations/002_mac_app_actions.sql
"$pg_bin/psql" -X -v ON_ERROR_STOP=1 -d keni <<'SQL'
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO keni_app;
GRANT SELECT, INSERT, UPDATE ON public.action_runs TO keni_app;
SQL
```

Set `KENI_DATABASE_URL=postgresql://keni_app:URL_ENCODED_PASSWORD@127.0.0.1:5432/keni` in the private `.env`, replacing `URL_ENCODED_PASSWORD` with the role password encoded for a URL. Keep `.env` readable only by your account (`chmod 600 .env`).

For an existing History database, apply `migrations/002_mac_app_actions.sql` as the database owner to enable app launch records.

## Run on macOS

1. Use macOS 14 or later and the Apple Command Line Tools with Swift 6.1 or later. Install `uv` and the LiveKit development server (`brew install uv livekit`). Install VS Code for the IRDX action.
2. Keep the project's `.env` private. It contains `OPENAI_API_KEY`, `SPOTIFY_CLIENT_ID`, `SPOTIFY_CLIENT_SECRET`, and `SPOTIFY_REDIRECT_URI`. The OpenAI project needs access to the Realtime API. Set `KENI_IRDX_PATH` to override the default `~/Projects/irdx-R-D`.
3. Run `./scripts/start` from the project directory. This installs the pinned Python dependencies, builds the app, and opens it. For later launches, open `.build/Local Voice Agent.app` from Finder. Keep the bundle in the checkout so it can find the local services.
4. Keni connects to the voice agent at launch with the microphone off. Once it says **Ready to listen**, hold **⌘⇧Space** or **Hold to talk** while you speak. The waveform appears on press and disappears on release. Releasing stops recording and sends the turn. Allow microphone access when macOS prompts. To change access later, go to **System Settings → Privacy & Security → Microphone**.

**⌘⇧Space** works while another app is in front. Minimize or hide Keni to keep the shortcut available. Closing Keni’s last window quits it.

Open **History** to see the most recent 50 actions across restarts. The view refreshes on live action updates and checks for missed updates every five seconds while open. **No final outcome recorded** means a saved action started and its outcome was never saved. The action may have run. If PostgreSQL is unavailable, voice actions can still run and show their live results.

The voice agent defaults to `gpt-realtime-2.1-mini`, caps each model response at 512 output tokens, and limits Realtime conversation history to 4,000 tokens after instructions. To choose another Realtime model, set `KENI_REALTIME_MODEL` in `.env` and restart the app. [OpenAI Realtime pricing](https://developers.openai.com/api/docs/pricing#realtime-audio)

## Open Mac apps by voice

Say “Open Claude,” “Open Chrome,” or “Open ChatGPT.” Keni matches the exact name of a Mac app in the standard Applications folders, including your own `~/Applications`. Name one app per request. “Open IRDX in VS Code” opens your configured repository.

For a custom nickname, an app installed elsewhere, or duplicate app names, add an entry to `KENI_MAC_APPS` in `.env` and restart Keni. Keep any existing entries. Map the name you want to say to the app’s `CFBundleIdentifier` in its `Contents/Info.plist`.

```dotenv
KENI_MAC_APPS='{"Obsidian":"md.obsidian"}'
```

## Connect Spotify

**Connect Spotify** opens the OAuth consent screen in your browser. A playback request opens Spotify on this Mac if needed and waits for the Mac to appear in Spotify Connect. Playback requires Spotify Premium and the Spotify desktop app signed in to the same account. Spotify's developer policy prohibits third-party voice control of its service. [Start Playback](https://developer.spotify.com/documentation/web-api/reference/start-a-users-playback) · [Spotify Developer Policy](https://developer.spotify.com/policy#iii-some-prohibited-applications)

Add `http://127.0.0.1:8767/callback` to **Redirect URIs** in the Spotify developer app settings, and set `KENI_SPOTIFY_REDIRECT_URI` to the same URL in `.env`. Local Voice Agent uses its own callback and token cache, so Spotify Bot can keep running on port 8000. Restart Local Voice Agent after editing `.env`. [Spotify redirect URI requirements](https://developer.spotify.com/documentation/web-api/concepts/redirect_uri)

Say “Play a song by Perfume” to let Keni choose one Spotify track credited to that artist. For a specific track title shared by multiple artists, include the artist too. Spotify Bot also listens for **⌘⇧Space**. Use Keni’s **Hold to talk** button when both apps are open, or quit Spotify Bot to give Keni the shortcut for that session.

## Connect Gmail

Enable the Gmail API in your Google Cloud project and create a Desktop app OAuth client. If the app is in External testing, add your account as a test user. Save the downloaded Desktop client JSON as `~/Library/Application Support/Local Voice Agent/gmail-client.json` with permissions `0600`. [Gmail desktop setup](https://developers.google.com/workspace/gmail/api/quickstart/python)

Run `./scripts/start`, click **Connect Gmail** in Keni, and approve read-only access in your browser. Once connected, open **Inbox** to view or search the five most recent matching inbox messages. Previews stay in the Mac app. Keni saves the OAuth token in its private Application Support directory. If an External testing refresh token expires after seven days, reconnect in Keni. [Google OAuth token expiration](https://developers.google.com/identity/protocols/oauth2#expiration)

## Test the backend in Docker

The Docker image runs the Python worker, FastAPI, and LiveKit on container loopback. Use `./scripts/start` on macOS for the SwiftUI app, desktop actions, and local PostgreSQL history.

```sh
docker build -t keni-backend .
docker run --rm keni-backend python -m unittest discover -s tests
```

To check that all three services start, use a placeholder key and check the container's health status after startup. This check makes no voice requests and needs no credentials from `.env`.

```sh
docker run --rm --detach --init --name keni-backend -e OPENAI_API_KEY=smoke-test keni-backend
docker inspect --format '{{.State.Health.Status}}' keni-backend
docker stop keni-backend
```

## Verify

Run `uv run python -m unittest discover -s tests` for automated checks. The live acceptance checks start with an audible reply and transcript, followed by “Open IRDX in VS Code.” When authorized for Spotify voice playback, test “Play Dynamite by BTS on Spotify” separately, then say “Play Dynamite by BTS on Spotify and open IRDX in VS Code.” Confirm the track is playing, the correct repository opens, and each action has a result. Repeat with an action that fails, such as an unavailable Spotify device, and verify the other action can succeed.

To test persistence, run “Open IRDX in VS Code,” quit Local Voice Agent, reopen its `.app` from Finder, and check **History**. For database integration tests, apply both migrations to a disposable PostgreSQL database and set `KENI_TEST_DATABASE_URL` when running the test suite. To test an outage, point a test instance of Keni at that database, stop the test database, run an action, and confirm its live outcome still appears with a history warning while **History** is unavailable.

For startup errors, check `~/Library/Application Support/Local Voice Agent/service.log`.
