# Production logging

Longhouse writes operational logs to stdout and stderr. A production container
runtime should retain those streams outside the container so logs survive a
restart or replacement. Longhouse does not write or rotate application log
files and does not require a logging database or collector.

## Linux Docker recommendation

For a durable Linux Docker host, use Docker's `journald` logging driver:

```yaml
services:
  longhouse:
    logging:
      driver: journald
      options:
        tag: "{{.Name}}"
```

The host must use persistent journald storage and a bounded retention policy.
Those limits are host-wide, so choose them from measured disk capacity and the
other services sharing the journal. Applying a different Docker logging driver
recreates the container. `docker logs` continues to work for the current
container.

Longhouse development Compose remains optimized for local development; do not
treat its defaults as a production retention policy.

## Querying retained logs

Use UTC and bounded time windows:

```bash
journalctl CONTAINER_NAME=<longhouse-container> --since -2h --utc -o short-iso-precise
journalctl CONTAINER_NAME=<longhouse-container> --since "2026-07-23 12:45:00 UTC" --until "2026-07-23 13:15:00 UTC" --utc -o short-iso-precise
journalctl CONTAINER_ID=<old-container-id> --utc -o short-iso-precise
journalctl CONTAINER_NAME=<longhouse-container> -f --utc -o short-iso-precise
```

Container name, ID, image, and stream metadata come from Docker and journald.
Longhouse records use stable text fields such as component, `event`, status,
duration, and outcome. Filter those fields as message text; journald priority is
the stdout/stderr stream priority and does not represent Python's log level.

After a replacement, a container-name query can include multiple generations.
Use an explicit time window and the previous `CONTAINER_ID` when isolating the
old container.

## Expected coverage

The Runtime Host API, background work, supervisors, `catalogd`, and `searchd`
share the container stream. Supervisors retain meaningful lifecycle transitions
such as child startup, readiness, exit, and restart; daemon exception paths
retain their failures. Current status JSON files also track transient component
health, but they are not retained history.

Operational logs must not include authorization headers, cookies, tokens,
secrets, prompts, transcripts, email bodies, or arbitrary request/response
bodies.
