# Architecture

## Processes and data flow

1. The SwiftUI app handles the microphone shortcut, macOS permissions, and display. The local Python supervisor starts the worker, LiveKit server, and loopback control API when the app launches.
2. The desktop client uses a private bearer token to request a short-lived LiveKit session token from the loopback control API on port 8765. The local supervisor supplies ephemeral LiveKit server credentials. Provider secrets stay in the Python environment.
3. The app connects to the LiveKit room and voice agent at launch. The LiveKit Swift SDK publishes microphone audio to the room on the same Mac during a hold, stops input on release, and signals the worker to commit the turn. Both waveforms use the same smoothed local mic level while held. Audio output stays available for the reply. The Python worker uses LiveKit Agents and the OpenAI Realtime API for speech, model responses, and structured tool calls. OpenAI processes the conversation remotely.
4. Python validates and runs the allowed actions. Mac app launch resolves a spoken name to a locally approved bundle ID and uses a fixed macOS command. The IRDX action opens the configured repository in VS Code. The Spotify action uses Spotipy for OAuth, search, device selection, playback, and playback verification. Independent actions can overlap. Each reports its own result.
5. SwiftUI renders transcript updates and action status from the LiveKit room. A successful Spotify result requires Spotify to report the requested track playing. The worker also records action state transitions in local PostgreSQL. SwiftUI loads the most recent 50 records from FastAPI using the desktop bearer token.

## Deployment boundary

The self-hosted LiveKit development server binds locally on port 7880 and keeps the worker and room on one Mac. LiveKit Cloud would manage the room and its network edge. A worker handling local desktop actions would still run on the Mac. PostgreSQL runs on loopback for local history. Hosted infrastructure and distributed tracing become useful when a workflow extends beyond the Mac. [Local LiveKit](https://docs.livekit.io/transport/self-hosting/local/) · [OpenAI Realtime plugin](https://docs.livekit.io/agents/models/realtime/openai/)

Keep the control token, Spotify OAuth cache, and service log in the user's private Application Support directory. The Spotify callback listens on its configured loopback address during authorization. The `.env` stays outside the app bundle and source control.

Gmail uses a Desktop app OAuth client and a loopback callback. The local FastAPI service starts authorization and serves read-only inbox previews through the authenticated desktop API. Preview requests and results stay outside the voice session.
