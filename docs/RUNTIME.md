# MindFlow Runtime

## 1. Purpose

This document gives a public architectural view of the production runtime.

Operational secrets and environment-specific values are intentionally omitted.

For exact deployment commands, use `mindflow-bot-runtime/README.md`.

## 2. Production services

The Docker Compose runtime contains separate services for:

```text
postgres
migrate
bot
admin
claude-state-init
```

A separate acceptance profile/image is used for isolated integration tests.

## 3. Bot flow

```text
Feishu receiver
   -> durable event
   -> participant binding / consent
   -> participant session queue
   -> constrained Agent
   -> participant-bound MindFlow tools
   -> PostgreSQL / Forecast / Calendar / Care
   -> reliable final delivery
```

## 4. Agent harness

The Agent harness is separated from business authorization.

The backend controls participant identity, consent, tool availability, tool validation, persistence and final delivery.

The model does not receive unrestricted infrastructure access.

## 5. Calendar

Calendar authorization and Calendar event mutation are backend services.

Mutations use durable intent/reconciliation logic because a remote write may succeed even if the local process loses the response.

Forecast and Warning state is invalidated conservatively when local schedule consistency is uncertain.

## 6. Forecast scheduling

The runtime can prepare daily forecasts, refresh after relevant Calendar changes, refresh after new observations, preserve previous-day terminal dependency and avoid recursive unbounded future refresh.

Concurrency is deliberately bounded for the current small research deployment.

## 7. Warning and Care

Warning delivery uses daily send limits, minimum spacing, lead time, late grace, retry/claim leases and final authorization checks.

Care preference controls are re-checked before send.

The system can also create same-day late Care when a missed warning is still contextually relevant.

## 8. Daily Review

Daily Review is a scheduled retrospective feedback path.

Card actions are handled by backend action handlers rather than routed through the conversational model.

The current runtime supports WebSocket CardAction transport by default, with verified HTTP callback as a fallback mode.

## 9. Admin

Admin is served as an independent Starlette process.

It uses role-based access and CSRF-protected mutating actions.

The deployment binds Admin to host loopback by default so it can be accessed through an explicit local/SSH-tunnel path rather than a public unauthenticated listener.

## 10. Database

Production uses PostgreSQL and Alembic migrations.

Research and operational state includes versioned records for participants, forecasts, observations, profiles, warnings, Care, Daily Review, model promotion, parameter learning, dataset snapshots and incidents.

## 11. Acceptance isolation

Destructive PostgreSQL integration tests use a dedicated test database variable.

The test guard refuses arbitrary database names and hosts.

Acceptance also checks Docker health, builder health, disk availability, memory availability, image revision, running service revision and runtime restoration after maintenance.

## 12. Revision parity

Deployment/acceptance embeds the Git revision into images so the operator can verify:

```text
host checkout
==
Bot image
==
Admin image
```

before and after acceptance.

## 13. Runtime philosophy

1. fail closed when authoritative inputs become stale;
2. preserve durable provenance for research interpretation;
3. keep experimental model capability behind explicit evidence gates.
