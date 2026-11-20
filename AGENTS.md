# Project instructions

Use `./scripts/start` to set up and launch the app. Run `uv run python -m unittest discover -s tests` for automated checks. See [README.md](README.md) for prerequisites, Spotify sign-in, and live acceptance checks.

- Apply the Ponytail decision ladder before writing code. Check whether the change is needed, then look for existing code, the standard library, native framework features, and installed dependencies. Write the smallest working solution that preserves validation, error handling, security, and accessibility.
- Keep tool calls typed and allowlisted. The model can request named actions. Python controls their inputs, fixed command arguments, and permitted paths. The IRDX action uses the single locally configured repository.
- Keep `.env`, bearer tokens, and OAuth caches private. Keep provider credentials server-side and use loopback bindings for local services.
- Report each action from its actual result. Keep mock tests labeled and verify real integrations separately before reporting acceptance.
- When a plan is approved, give each subagent its own files and exact scope. Sequence dependent work and inspect the resulting diff or output before reporting it.
- When `graphify-out/graph.json` exists, start codebase questions with `graphify query`. Use `graphify path` or `graphify explain` for focused relationships and read the wiki index for broad navigation when present. Run `graphify update .` after code changes.

Read [PRODUCT_VISION.md](PRODUCT_VISION.md) for scope, [ARCHITECTURE.md](ARCHITECTURE.md) for data flow and boundaries, [DECISIONS.md](DECISIONS.md) for tradeoffs, and [ROADMAP.md](ROADMAP.md) for later stages. Update the relevant docs when an acceptance gate changes the implementation or reveals a blocker.
