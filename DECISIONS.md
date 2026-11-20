# Decisions

## Ship a Finder-launchable app

Use SwiftUI and the native LiveKit Swift SDK. A native bundle gives macOS ownership of microphone permissions and supports ⌘⇧Space as a global shortcut. SwiftPM and the command-line build script cover the local development deliverable. The alternative browser client would require a different launch and shortcut experience.

## Keep orchestration and desktop actions local

Use the LiveKit development server and a Python worker on the same Mac. Managed LiveKit is an option when operating the room server becomes a burden. OpenAI Realtime handles speech and model reasoning. Python is responsible for deciding whether a requested operation is allowed, running it, and reporting the outcome.

## Reuse the existing Spotify flow

Adapt the Spotify Bot's Spotipy Authorization Code flow and track search. Start the Spotify desktop app and identify this Mac by its computer name in Spotify Connect before requesting and verifying playback. Use a separately registered loopback callback and store the OAuth cache in this app's support directory so authorization works independently of Spotify Bot. Spotify's developer policy prohibits third-party voice control of its service. [Spotify Developer Policy](https://developer.spotify.com/policy#iii-some-prohibited-applications)

## Persist local action history in V1

Use Homebrew PostgreSQL 18 on the Mac and one `action_runs` table. The worker saves states by action UUID and preserves the first final outcome. The authenticated FastAPI History endpoint returns the most recent 50 records. LiveKit delivers actual outcomes during database outages, and the UI labels any missing final outcome. Recovery and automatic retries remain future work. [PostgreSQL 18 upserts](https://www.postgresql.org/docs/18/sql-insert.html)
