# Roadmap

1. **V1 acceptance.** Verify Finder launch, push-to-talk, an audible reply and transcript, opening Chrome and ChatGPT, and the real IRDX action. Save local action history in PostgreSQL and verify it after an app restart. Verify that actions still report their outcomes during a database outage or an independent action failure. Validate Spotify playback and the combined request if the integration has permission for voice control.
2. **More approved actions.** Explore Spotify playlist creation and editing, catalog search, and library management within the service's rules. Add Gmail reading, summarization, and draft creation. Require explicit approval before sending email.
3. **Durable workflows.** Extend local action history to track dependencies and retry state when interrupted runs need to resume. Add idempotency and coordination before running multiple workers. Revisit personalization when retained context has a defined use and retention policy.
4. **Operational readiness.** Use Docker for repeatable service builds, structured logs with correlation IDs, and OpenTelemetry for traces and metrics when debugging spans processes or machines.
5. **Optional cloud deployment.** Evaluate ECS/Fargate for hosted workers, CloudWatch for AWS logs and metrics, and API Gateway if an external API is needed. Keep Mac actions on the device and preserve the local desktop boundary.
