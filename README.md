# Spotify ETL Pipeline — Apache Airflow + AWS S3

An end-to-end data engineering pipeline that extracts track, album, and artist data from a Spotify playlist daily, transforms it into structured CSVs, and loads them into AWS S3 — orchestrated entirely with Apache Airflow running in Docker.

---

## Architecture

![Architecture](images/excalidraw_architecture.png)

---

## Airflow DAG Graph

![DAG Graph](images/dag_graph.png)

---

## How It Works

The pipeline runs on a daily schedule via an Airflow DAG with 10 tasks across 4 stages:

**1. Extract**
- Connects to the Spotify API using `spotipy` and pulls all tracks from a configured playlist
- Saves raw JSON locally in `/tmp` and uploads it to `s3://spotify-etl-pipeline-sumanth-dec25/raw_data/to_processed/`

**2. Read**
- Reads all raw JSON files from S3 and pushes the data downstream via Airflow XCom

**3. Transform**
- Three parallel tasks process albums, artists, and songs independently
- Deduplicates records, parses dates, and serialises each dataset as a CSV
- Uploads transformed CSVs to `s3://.../transformed_data/{album_data | artist_data | songs_data}/`

**4. Archive**
- Moves processed raw files from `raw_data/to_processed/` to `raw_data/processed/` and deletes the originals

---

## DAG

![DAG Run](images/dag_run.png)

| Task | Type | Description |
|---|---|---|
| `fetch_spotify_data` | PythonOperator | Calls Spotify API, writes raw JSON to /tmp |
| `upload_raw_to_s3` | PythonOperator | Uploads raw JSON file to S3 |
| `read_data_from_s3` | PythonOperator | Reads all files from S3 to_processed prefix |
| `process_album` | PythonOperator | Extracts and deduplicates album records |
| `process_artist` | PythonOperator | Extracts and deduplicates artist records |
| `process_songs` | PythonOperator | Extracts song records with album/artist IDs |
| `store_album_to_s3` | S3CreateObjectOperator | Uploads album CSV to S3 |
| `store_artist_to_s3` | S3CreateObjectOperator | Uploads artist CSV to S3 |
| `store_songs_to_s3` | S3CreateObjectOperator | Uploads songs CSV to S3 |
| `move_processed_data` | PythonOperator | Archives raw JSON from to_processed → processed |

---

## S3 Bucket Structure

```
spotify-etl-pipeline-sumanth-dec25/
├── raw_data/
│   ├── to_processed/        ← raw JSON lands here
│   └── processed/           ← moved here after transformation
└── transformed_data/
    ├── album_data/          ← album CSVs
    ├── artist_data/         ← artist CSVs
    └── songs_data/          ← song CSVs
```

![S3 Structure](images/s3_structure.png)

---

## Tech Stack

- **Orchestration:** Apache Airflow 3.x
- **Containerisation:** Docker + Docker Compose
- **Cloud Storage:** AWS S3
- **Language:** Python 3.12
- **Key Libraries:** `spotipy`, `pandas`, `apache-airflow-providers-amazon`
- **Data Source:** Spotify Web API

---

## Setup

### Prerequisites
- Docker and Docker Compose
- Spotify Developer account — [Create an app](https://developer.spotify.com/dashboard)
- AWS account with an S3 bucket and IAM credentials

### 1. Clone the repo

```bash
git clone https://github.com/sumanthmalipeddi/spotify-etl-aws-airflow.git
cd spotify-etl-aws-airflow
```

### 2. Start Airflow

```bash
docker-compose up -d
```

Airflow UI will be available at `http://localhost:8080`
Default credentials: `airflow / airflow`

### 3. Configure Airflow Variables

Go to **Admin → Variables** and add:

| Key | Value |
|---|---|
| `spotify_client_id` | Your Spotify app client ID |
| `spotify_client_secret` | Your Spotify app client secret |

### 4. Configure Airflow Connection

Go to **Admin → Connections → Add**:

| Field | Value |
|---|---|
| Conn ID | `aws_s3_airbnb` |
| Conn Type | `Amazon Web Services` |
| Login | AWS Access Key ID |
| Password | AWS Secret Access Key |

### 5. Trigger the DAG

Enable `spotify_etl_dag_airflow` in the Airflow UI and trigger a run manually or let the daily schedule handle it.

---

## Output

![S3 Transformed Output](images/s3_transformed.png)

Three CSVs are produced per run, timestamped and stored in separate S3 prefixes:

- `album_transformed_<timestamp>.csv` — album ID, name, release date, total tracks, URL
- `artist_transformed_<timestamp>.csv` — artist ID, name, external URL
- `songs_transformed_<timestamp>.csv` — song ID, name, duration, popularity, added date, album ID, artist ID

---

## Project History

This project was originally built using AWS Lambda + CloudWatch for a fully serverless approach. The Lambda version and original exploration notebook are preserved in the [`archive/`](archive/) folder for reference.
