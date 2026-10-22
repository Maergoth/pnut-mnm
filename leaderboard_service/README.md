# Anonymous leaderboard Python adapter

This optional Python adapter accepts `POST /snapshots` and appends validated JSONL to the fixed `Maergoth/pnut-mnm` repository, `leaderboard-data` branch, `leaderboard.jsonl` path. The hosted deployment uses the equivalent [Cloudflare Worker](../leaderboard_worker/README.md); this Python listener is not deployed. The desktop reads the public JSONL directly. The data branch and hosted endpoint have been configured and verified separately from the held desktop release.

The shared `mnmparse.leaderboard.validate_snapshot` function defines the complete allowed payload. Only the exact `window` capture backend is eligible. Names, logs, window titles and extra fields are rejected. The service additionally requires a UTC timestamp on the current UTC day and within five minutes of server time. Request bodies are capped at 4 KiB; public history is capped at the shared byte and record limits. Existing malformed history causes an unavailable response and is never overwritten.

The source accepts unchanged original and version 2 signed rows, and version 3 personal-best rows. The desktop signs protected maximum HP/Mana, attribute, AC and resist records; Stat Total and Resist Total remain separate best complete-reading totals, rather than sums of individual maxima. A total can remain unavailable when no complete group was observed. A fresh Window Capture timestamp is still required for submission eligibility and its UTC daily ID. Copy remains a current-reading operation.

Messages must already be normalized and censored before signing; daily retries preserve the original accepted snapshot/message across all formats. Missing legacy values remain unavailable. The version 3 hosted upgrade is not deployed. Version 1.0.24's strict desktop reader requires an update before refreshing a public file containing version 2 or 3 rows; cached entries remain available. Coordinate the compatible backend and approved desktop rollout before any production version 3 write.

Repeating a daily submission ID returns the original public record (`200`, `created: false`); a new record returns `201`, `created: true`. Both responses have the shape `{"snapshot": {...}, "created": true}`. Public records retain the same anonymous identity across days.

GitHub updates use the file SHA and retry concurrent-write conflicts up to three times. If an update succeeds but its response is lost, retrying the same submission ID safely discovers the existing record. Files larger than 1 MiB are read using the immutable Git blob SHA. The process handles one HTTP request at a time and uses bounded GitHub timeouts; a deployment proxy should bound connections and rate-limit writes.

## Local preparation

Use Python 3.14 and install the service's pinned dependency (`python -m pip install -r leaderboard_service/requirements.txt`). No Qt libraries are required. From the repository root, provide a server-only `GITHUB_TOKEN` with Contents read/write access restricted to this repository, then run:

```text
python -m leaderboard_service
```

The default listener is `127.0.0.1:8765`. Optional server variables are `PNUT_LEADERBOARD_BIND` and `PNUT_LEADERBOARD_PORT`. The repository, branch and filename are constants, not request or environment parameters. The dedicated data branch must already exist before accepting writes. A missing JSONL file can be created on that existing branch. Coordinate protected-branch rules and token access before enabling submissions.

Build the optional container **from the repository root**:

```text
docker build -f leaderboard_service/Dockerfile -t pnut-leaderboard-draft .
```

Run with a runtime secret supplied by the deployment platform and bind the published container port to loopback (`127.0.0.1:8765:8765`). Do not put a token in the Dockerfile, image, desktop package, Git repository, public configuration or clipboard. The image copies only this service and the two small shared Python files.

## Before any public deployment

Keep this service behind a TLS reverse proxy; never publish the plain HTTP listener directly. Set an explicit hostname, trusted TLS certificate, a 4 KiB request limit, read/upstream timeouts, connection limits and a modest rate limit for `/snapshots`. Expose only `POST /snapshots` and, if useful to infrastructure, `GET /health`. `/health` reports that the process is alive, not GitHub availability. Disable proxy request-body logging and avoid retaining client IPs. The service itself does not log payloads, IP addresses or tokens.

Publishing this optional Python listener is a separate deployment from the configured Worker. When the shared file limit is reached, writes stop with `503`; the service does not silently discard old records.

Tests use an in-memory fake GitHub transport and a loopback HTTP server, never a live GitHub write:

```text
python -m unittest tests.test_leaderboard_service -v
```

GitHub API behavior follows the official [Contents API](https://docs.github.com/en/rest/repos/contents#create-or-update-file-contents) and [Git blobs API](https://docs.github.com/en/rest/git/blobs#get-a-blob) documentation.
