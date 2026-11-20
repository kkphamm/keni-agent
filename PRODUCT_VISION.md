# Product vision

Local Voice Agent is a personal assistant for carrying out spoken requests on a Mac. Its long-term goal is to understand requests that span several apps, run independent actions concurrently when safe, sequence dependent actions, and tell the user what happened.

V1 is a native desktop app with a global microphone shortcut, live transcripts and responses, per-action status, and local action history across restarts. It supports opening approved Mac apps, opening the configured IRDX repository in VS Code, and Spotify track search and playback through the existing bot's integration pattern. Spotify voice playback is subject to Spotify's developer policy. [Spotify Developer Policy](https://developer.spotify.com/policy#iii-some-prohibited-applications)

Future capabilities include approved local Mac actions, Spotify playlist and library management where permitted, Gmail reading and summarization, draft creation and approved sending, multi-app workflows, recovery of interrupted runs, and personalized conversational context. Reliability grows through safe retries, explicit concurrency controls, and observability. An optional cloud deployment can support remote services while local actions stay on the Mac.
