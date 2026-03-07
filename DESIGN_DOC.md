# Design Document — Spotify ETL Pipeline

## 1. Problem Statement

I needed a pipeline that extracts Spotify playlist data daily, transforms it into structured datasets, and stores it in S3 for downstream analytics. The solution had to be fault-tolerant, easily extensible to new playlists, and runnable locally with minimal setup.

---

## 2. Architecture Evolution

### Phase 1: Serverless (AWS Lambda + CloudWatch)

The first version used AWS Lambda for extraction and transformation, with CloudWatch Events for daily scheduling.

**Why it worked initially:**
- Zero infrastructure to manage
- Pay-per-invocation pricing
- Simple for a single playlist

**Why I moved away:**
- No visibility into pipeline state — when a Lambda failed silently, I had no dashboard showing which step broke
- No dependency management between extract and transform — I was using S3 event triggers, which created race conditions when multiple files landed simultaneously
- Debugging was painful — CloudWatch Logs are scattered across invocations with no unified view of a single pipeline run
- Adding a new playlist meant duplicating Lambda functions rather than parameterizing a DAG

### Phase 2: Apache Airflow (Current)

Migrated to Airflow 3.x with CeleryExecutor running in Docker Compose.

**Why Airflow:**
- DAG-based orchestration gives explicit dependency management — `fetch → upload → read → transform → store → archive` runs in guaranteed order
- Built-in retry logic, SLA monitoring, and a web UI for observability
- XCom for lightweight inter-task data passing
- Extensible — adding a new playlist is a config change, not a code rewrite

**Why CeleryExecutor over LocalExecutor:**
- The three transform tasks (album, artist, songs) run in parallel. LocalExecutor handles parallelism via subprocesses, but CeleryExecutor with Redis gives true distributed execution
- In production, CeleryExecutor scales horizontally by adding worker containers
- The trade-off is complexity — CeleryExecutor requires Redis and introduces the JWT secret synchronization issue (see Section 5)
- For a single-playlist pipeline, LocalExecutor would be sufficient. I chose Celery to mirror production patterns and to learn the distributed execution model

---

## 3. Key Design Decisions

### XCom for data passing vs. shared storage

**Decision:** Use XCom for metadata (filenames, S3 keys, file paths) and `/tmp` files for actual data.

**Why not XCom for everything?** Airflow stores XCom values in the metadata database (PostgreSQL). The raw Spotify JSON for a 100-track playlist is ~500KB. While this fits in XCom, it's an anti-pattern — the metadata DB shouldn't be a data lake. For larger playlists or higher frequency, this would degrade Airflow performance.

**The approach:** Every task that handles data writes it to `/tmp` and pushes only the file path via XCom. The fetch task writes raw JSON to `/tmp/{filename}`. The read task consolidates all S3 files into `/tmp/spotify_consolidated.json`. Each transform task writes Parquet to `/tmp/{dataset}_transformed.parquet`. This keeps XCom lightweight (string values only) while avoiding shared filesystem dependencies.

### CSV vs. Parquet

**Decision:** Parquet for transformed output.

Parquet provides columnar storage with built-in compression and schema enforcement. For analytical workloads downstream (Athena, Spark, pandas), Parquet reads are significantly faster than CSV because queries can skip irrelevant columns. It also eliminates CSV parsing issues (quoted commas, encoding, type inference).

### S3 prefix structure: `to_processed/` → `processed/`

**Decision:** Use a two-folder pattern for raw data lifecycle.

```
raw_data/
├── to_processed/   ← new files land here
└── processed/      ← moved here after successful transformation
```

This creates an implicit "inbox/archive" pattern. If the transform step fails, the raw file stays in `to_processed/` and gets picked up on the next run. This is idempotent — rerunning the pipeline won't miss data or duplicate it. The `move_processed_data` task only runs after all three stores succeed (fan-in dependency).

### Hardcoded bucket name vs. Airflow Variables

**Decision:** Pipeline configuration (bucket name, playlist URI, S3 prefixes) is stored in Airflow Variables.

This separates configuration from code. A new user clones the repo, sets their own bucket name in the Airflow UI, and the pipeline works without touching Python code. It also means the same DAG code can run in dev/staging/prod with different Variable values.

The code uses `Variable.get("s3_bucket_name", default_var="spotify-etl-pipeline-sumanth-dec25")` — the default fallback means the pipeline works out of the box for my setup, but any user can override it via the Airflow Variables UI without modifying code.

---

## 4. Data Quality

Each transform branch includes a validation step that checks:

- **Row count > 0** — ensures the Spotify API returned data
- **No null primary keys** — `album_id`, `artist_id`, `song_id` must be present
- **No duplicates on primary key** — deduplication is applied, then verified
- **Date validation** — for datasets with date columns (albums), `release_date` is parsed with `pd.to_datetime()` before validation. The `_validate()` function then checks for any `NaT` values that indicate unparseable dates. This two-step approach gives a clean error message ("3 unparseable dates in release_date") rather than a cryptic pandas traceback

If any check fails, the task raises an exception, which prevents the corresponding `store_*_to_s3` task from running. This fail-fast approach ensures only validated data reaches S3.

---

## 5. The JWT Secret Bug — Root Cause Analysis

### Symptom
After migrating from the course directory to a standalone repo, every task failed with:
```
Invalid auth token: Signature verification failed
```

### Investigation
1. The error came from the Celery worker trying to communicate with the Airflow API server
2. Airflow 3.x uses JWT tokens for worker ↔ apiserver authentication
3. The JWT is signed with a secret key from `[api_auth] jwt_secret` in `airflow.cfg`

### Root cause
Each Docker container generates its own `airflow.cfg` on startup. When `AIRFLOW_CONFIG` is not set, Airflow writes the config to an in-memory default path inside each container. Since each container is isolated, each gets a **different random `jwt_secret`**.

The worker signs its JWT with secret `A`, but the apiserver validates against secret `B` → signature mismatch → every task fails.

### The fix
```yaml
# docker-compose.yaml
AIRFLOW_CONFIG: '/opt/airflow/config/airflow.cfg'
```

Combined with the volume mount:
```yaml
- ${AIRFLOW_PROJ_DIR:-.}/config:/opt/airflow/config
```

The `airflow-init` container generates `airflow.cfg` once on first startup, writing it to the shared `config/` volume. All other containers read from this same file, ensuring they all use the same `jwt_secret`.

### Why the course directory worked
The original course `docker-compose.yaml` already had `AIRFLOW_CONFIG` set. When I cleaned up the file for the standalone repo, I removed it thinking it was unnecessary boilerplate. It wasn't.

### Lesson
In distributed systems, any shared secret must come from a single source of truth. When multiple processes need to agree on a value, you can't let each generate its own. This applies to JWT secrets, encryption keys, and any shared configuration in a multi-container setup.

---

## 6. Error Handling Strategy

- **Retries:** All PythonOperator tasks retry twice with a 5-minute delay. This handles transient Spotify API rate limits and temporary S3 connectivity issues
- **Spotify API errors:** The fetch task wraps the API call in try/except, logging the specific error (rate limit, invalid credentials, network timeout) before re-raising
- **S3 errors:** The AWS provider handles retries internally via botocore's retry configuration
- **Idempotency:** All S3 uploads use `replace=True`, so retried tasks overwrite rather than duplicate. The `move_processed_data` task checks for file existence before attempting the move

---

## 7. What I'd Do Differently in Production

1. **Secrets management:** Use AWS Secrets Manager or HashiCorp Vault instead of Airflow Variables for API credentials
2. **Monitoring:** Add Airflow email/Slack alerts on task failure via `on_failure_callback`
3. **Data catalog:** Register the Parquet files in AWS Glue Catalog for Athena querying
4. **Partitioning:** Partition S3 data by date (`year=2026/month=03/day=06/`) for efficient time-range queries
5. **Testing:** Add unit tests for transform functions using sample fixture data
6. **CI/CD:** GitHub Actions to lint the DAG, run tests, and validate the Docker build on every push
