# Roadmap

1. **V1 acceptance.** Verify Finder launch, push-to-talk, an audible reply and transcript, opening Chrome and ChatGPT, and the real IRDX action. Save local action history in PostgreSQL and verify it after an app restart. Verify that actions still report their outcomes during a database outage or an independent action failure. Validate Spotify playback and the combined request if the integration has permission for voice control.
2. **More approved actions.** Explore Spotify playlist creation and editing, catalog search, and library management within the service's rules. Add Gmail reading, summarization, and draft creation. Require explicit approval before sending email.
3. **Production reliability.** Add action claims, worker recovery, latency measurements, and an ECS staging deployment. [Implementation plan](#production-reliability)
4. **Durable workflows.** Extend local action history to track dependencies and retry state when interrupted runs need to resume. Revisit personalization when retained context has a defined use and retention policy.

## Production reliability

Build the action claim first. Use it in a local crash test, add timing to that test, and run the same failure scenario on ECS. The staging voice client uses generated speech and a single fixture action that writes a synthetic record to RDS. Mac app launches and Spotify and Gmail OAuth stay on the device.

### 1. Claim each action once

History already keeps the first final result for a given action UUID. A disposable PostgreSQL probe with 32 concurrent updates in two batches preserved one row and its final result. The runner currently creates a fresh UUID on each invocation, so the history row only deduplicates status writes. [Action runner](agent/actions.py) · [History upsert](agent/history.py)

1. Derive an action UUID at the validated tool boundary from the original room, LiveKit tool call ID, and action type. Keep that UUID and the original request scope on redelivery. Store a fingerprint of the validated arguments so the same ID with a different payload is rejected. A new spoken request receives a new tool call ID. Keep release-packet deduplication separate from action execution. [Voice tools](agent/voice.py)
2. Extend `action_runs` with an additive numbered migration. Claim with `INSERT … ON CONFLICT DO NOTHING RETURNING id` and commit before dispatch. Only the caller that gets the inserted row starts the side effect. Another delivery returns the stored result or reports that the action already has a claim. Finalize with a conditional update from `running`. If saving the result fails, report the actual live outcome with a history warning and retain the original claim. [PostgreSQL upserts](https://www.postgresql.org/docs/18/sql-insert.html)
3. Use durable claims in the reliability demo and require them in staging. If PostgreSQL cannot confirm a claim, report that the new action could not start and keep voice available. The standard local setup keeps V1’s best-effort history behavior during an outage.

**Acceptance.** Run against real PostgreSQL in CI. Race two worker processes with the same action ID and assert one execution and one history row. Deliver duplicate and late status updates and assert that the first final result stays intact. Verify that a conflicting payload cannot run and that two distinct requests can each run once. [Current PostgreSQL integration test](tests/test_history_postgres.py)

### 2. Recover after a worker crash

The local launcher exits and stops its other child services when the voice worker exits. The desktop can reconnect to a new room, and History already has a label for an action with no saved final outcome. [Launcher](agent/launch.py) · [Voice session](macOS/Sources/VoiceSession.swift) · [History view](macOS/Sources/HistoryView.swift)

1. Restart a failed voice worker with bounded backoff while keeping the local API and LiveKit running. Detect a missing room agent even when the API is healthy. Show **Reconnecting**, close microphone input, and open a new room once the worker is ready. Require a fresh press for the next command.
2. Preserve the claim when a worker dies after accepting an action. Use **No final outcome recorded** if the action may have run and its result is unknown. Leave the original action claimed and surface its status on redelivery. Use LiveKit’s bounded drain for planned shutdowns. [LiveKit worker lifecycle](https://docs.livekit.io/agents/server/startup-modes/)
3. Run fault scenarios through a test harness that owns its worker processes. Stop the worker before a claim, after the claim, and after a separately committed fixture side effect but before the final status write. Test repeated startup failures and normal shutdown so restart attempts end cleanly.

**Acceptance.** History remains readable during the crash. A crash before the claim leaves zero effects. A crash after the claim leaves an unknown outcome if there is no final result. When the fixture effect already committed, a replay leaves its execution count at one. The next fresh voice request succeeds without quitting Keni. Record the recovery time and verify that the microphone stays off until the next press.

### 3. Measure latency across the voice turn

1. Reserve the next release sequence at key-down and pair it with the existing room ID to identify the turn. Map the accepted release to the LiveKit speech handle and attach the action UUID to tool and database timing. Record monotonic durations within each process. Use the IDs to join logs across processes.
2. In Swift, measure key-down to the first captured mic frame and key-up to the first reply audio rendered by the client. In Python, measure the model’s time to first audio, tool execution, and claim and result writes. Record recovery from worker failure to the first successful follow-up turn. Test that an interrupted reply’s audio cannot count as the next reply. [LiveKit latency metrics](https://docs.livekit.io/reference/agents/session-traces/)
3. Emit structured logs with timings and outcomes. Keep room, turn, and action IDs as correlation fields. Exclude transcripts, audio, credentials, and tool arguments. Use synthetic fixtures in CI and a fixed generated-speech set for the live benchmark.

**Acceptance.** Produce a report with the build revision, sample count, p50, p95, and failure rate for warm and cold sessions. Include one correlated trace for a command with an action and one for a recovered session. Set latency targets from the first measured baseline.

### 4. Deploy one staging workflow on ECS and RDS

1. Reuse the Python image with separate commands for FastAPI and the voice worker. Keep the all-in-one Docker entry point for local development. Run the cloud services as separate ECS Fargate services. Use LiveKit Cloud for media rooms. Give an authenticated in-VPC smoke client access to the private staging API for short-lived room tokens and its own history. Limit the hosted worker to the synthetic fixture action. [Current Dockerfile](Dockerfile) · [LiveKit worker networking](https://docs.livekit.io/deploy/custom/deployments/)
2. Define the staging environment in version control. Give the API and worker outbound connectivity to their providers, use private RDS PostgreSQL with TLS verification, and limit database access by security group and role. Use Secrets Manager for the staging credentials and CloudWatch for structured logs. Run numbered migrations once in a separate task with a migration role. Keep migrations compatible with the previous image during rollback. [ECS secrets](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/secrets-envvar-secrets-manager.html) · [RDS TLS](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/PostgreSQL.Concepts.General.SSL.html)
3. Build and test a Linux image in CI and publish it to ECR by digest. Use a manually triggered staging deployment after migrations pass. Check API readiness and worker registration separately. Set the ECS stop timeout above the worker’s drain deadline and enable the ECS rolling deployment circuit breaker with rollback. [LiveKit health checks](https://docs.livekit.io/agents/server/options/) · [ECS rollback](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/deployment-circuit-breaker.html)

**Acceptance.** Run the voice fixture on ECS and verify its history in RDS. Repeat the crash and replay scenario on an isolated staging worker task. Verify ECS replaces the exited task, preserves the existing rows and claims in RDS, and accepts a new turn. Deploy a staging revision that fails its health check and verify ECS restores the last healthy revision. Save the task and revision IDs, test output, and timing report with the demo.

## Reliability demo

Add one command to run the local concurrency and crash checks and publish their results from CI. Reuse its scenario on staging. The recorded walkthrough should show a successful voice action, a lost worker, a duplicate delivery, and a successful new turn. Include the execution count, history state, recovery time, ECS rollback result, and staging teardown steps in the runbook.
