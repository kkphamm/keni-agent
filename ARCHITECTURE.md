# Architecture

## Processes and data flow

1. The SwiftUI app handles the microphone shortcut, macOS permissions, and display. The local Python supervisor starts the worker, LiveKit server, and loopback control API when the app launches.
2. The desktop client uses a private bearer token to request a short-lived LiveKit session token from the loopback control API on port 8765. The local supervisor supplies ephemeral LiveKit server credentials. Provider secrets stay in the Python environment.
3. The app connects to the LiveKit room and voice agent at launch and prepares a microphone track with hardware input off. Pressing the shortcut or button mutes Keni’s playback, interrupts its current reply, and enables local capture. Release disables input immediately and sends a numbered message after the final audio frames have time to arrive. A quick tap discards the uncommitted input. Both waveforms use the same smoothed local mic level while held. The Python worker uses LiveKit Agents and the OpenAI Realtime API for English transcription, spoken replies, and structured tool calls. OpenAI processes the conversation remotely.
4. Typed requests use the same LiveKit room and action tools. Python validates and runs the allowed actions. Mac app launch resolves an app name to a locally approved bundle ID and uses a fixed macOS command. The IRDX action opens the configured repository in VS Code. The Spotify action builds an ordered song queue or a shuffled artist, playlist, or mood selection and starts the Spotify desktop app in the background. Independent actions can overlap. Each reports its own result.
5. SwiftUI renders transcript updates and action status from the LiveKit room. Quiet Mode uses a LiveKit RPC to disable the worker’s audio output while keeping the transcript and accepting requests. The Mac also mutes the reply immediately when the switch is turned on. Keni checks Spotify's playback state before reporting a successful start on this Mac. The worker records action state transitions in local PostgreSQL. SwiftUI loads the most recent 50 records from FastAPI using the desktop bearer token. [LiveKit text and transcriptions](https://docs.livekit.io/agents/multimodality/text/)

## Deployment boundary

The self-hosted LiveKit development server binds locally on port 7880 and keeps the worker and room on one Mac. LiveKit Cloud would manage the room and its network edge. A worker handling local desktop actions would still run on the Mac. PostgreSQL runs on loopback for local history. Hosted infrastructure and distributed tracing become useful when a workflow extends beyond the Mac. [Local LiveKit](https://docs.livekit.io/transport/self-hosting/local/) · [OpenAI Realtime plugin](https://docs.livekit.io/agents/models/realtime/openai/)

Keep the control token, Spotify OAuth cache, and service log in the user's private Application Support directory. The Spotify callback listens on its configured loopback address during authorization. The `.env` stays outside the app bundle and source control.

The Spotify panel uses the authenticated FastAPI service for top tracks, linked playlists, device status, and sign-in controls. Its Play buttons use the same Spotify playback code as voice requests. Linked playlist names are saved per Spotify account in the private Application Support directory.

Gmail uses a Desktop app OAuth client and a loopback callback. The local FastAPI service starts authorization and serves read-only inbox previews through the authenticated desktop API. Preview requests and results stay outside the voice session.
