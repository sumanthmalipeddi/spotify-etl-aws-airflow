import logging
import json
import pandas as pd
import spotipy
from spotipy.oauth2 import SpotifyClientCredentials

from airflow import DAG
from airflow.providers.standard.operators.python import PythonOperator
from airflow.models import Variable
from airflow.providers.amazon.aws.hooks.s3 import S3Hook
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

CONN_ID = "aws_s3_spotify"

default_args = {
    "owner": "sumanth",
    "depends_on_past": False,
    "start_date": datetime(2026, 3, 6),
    "retries": 2,
    "retry_delay": timedelta(minutes=5),
}


def _get_config():
    """Load pipeline config from Airflow Variables."""
    return {
        "bucket_name": Variable.get("s3_bucket_name", default_var="spotify-etl-pipeline-sumanth-dec25"),
        "playlist_url": Variable.get("spotify_playlist_url", default_var="https://open.spotify.com/playlist/6VOedaf3eNWDOVpa9Qdlvg"),
    }


def _fetch_spotify_data(**kwargs):
    client_id = Variable.get("spotify_client_id")
    client_secret = Variable.get("spotify_client_secret")
    config = _get_config()

    try:
        client_credentials_manager = SpotifyClientCredentials(
            client_id=client_id, client_secret=client_secret
        )
        sp = spotipy.Spotify(client_credentials_manager=client_credentials_manager)
        playlist_URI = config["playlist_url"].split("/")[-1]
        spotify_data = sp.playlist_tracks(playlist_URI)
    except spotipy.exceptions.SpotifyException as e:
        logger.error("Spotify API error: %s", e)
        raise
    except Exception as e:
        logger.error("Failed to fetch Spotify data: %s", e)
        raise

    filename = "spotify_raw_" + datetime.now().strftime("%Y%m%d%H%M%S") + ".json"
    tmp_path = f"/tmp/{filename}"

    with open(tmp_path, "w") as f:
        json.dump(spotify_data, f)

    logger.info("Fetched %d tracks, saved to %s", len(spotify_data.get("items", [])), tmp_path)
    kwargs["ti"].xcom_push(key="spotify_filename", value=filename)
    kwargs["ti"].xcom_push(key="tmp_path", value=tmp_path)


def _upload_raw_to_s3(**kwargs):
    ti = kwargs["ti"]
    config = _get_config()
    filename = ti.xcom_pull(task_ids="fetch_spotify_data", key="spotify_filename")
    tmp_path = ti.xcom_pull(task_ids="fetch_spotify_data", key="tmp_path")
    s3_hook = S3Hook(aws_conn_id=CONN_ID)
    s3_hook.load_file(
        filename=tmp_path,
        key=f"raw_data/to_processed/{filename}",
        bucket_name=config["bucket_name"],
        replace=True,
    )
    logger.info("Uploaded %s to s3://%s/raw_data/to_processed/", filename, config["bucket_name"])


def _read_data_from_s3(**kwargs):
    """Read all raw JSON files from S3 and consolidate to a /tmp file.

    Pushes only the file path via XCom (not the data itself) to keep the
    Airflow metadata DB lightweight. See DESIGN_DOC.md Section 3.
    """
    config = _get_config()
    s3_hook = S3Hook(aws_conn_id=CONN_ID)
    prefix = "raw_data/to_processed"
    keys = s3_hook.list_keys(bucket_name=config["bucket_name"], prefix=prefix)
    keys = [k for k in keys if not k.endswith("/")]

    if not keys:
        raise ValueError(f"No files found in s3://{config['bucket_name']}/{prefix}")

    spotify_data = []
    for key in keys:
        data = s3_hook.read_key(key, config["bucket_name"])
        spotify_data.append(json.loads(data))

    # Write consolidated data to /tmp instead of pushing through XCom.
    # XCom stores values in the metadata DB (PostgreSQL) — pushing ~500KB+
    # of raw JSON per run degrades DB performance over time.
    tmp_path = "/tmp/spotify_consolidated.json"
    with open(tmp_path, "w") as f:
        json.dump(spotify_data, f)

    logger.info("Read %d raw files from S3, consolidated to %s", len(spotify_data), tmp_path)
    kwargs["ti"].xcom_push(key="spotify_data_path", value=tmp_path)


def _process_album(**kwargs):
    data_path = kwargs["ti"].xcom_pull(task_ids="read_data_from_s3", key="spotify_data_path")
    with open(data_path, "r") as f:
        spotify_data = json.load(f)

    album_list = []
    for data in spotify_data:
        for row in data["items"]:
            album = row["track"]["album"]
            album_list.append({
                "album_id": album["id"],
                "name": album["name"],
                "release_date": album["release_date"],
                "total_tracks": album["total_tracks"],
                "url": album["external_urls"]["spotify"],
            })

    album_df = pd.DataFrame(album_list)
    album_df = album_df.drop_duplicates(subset=["album_id"])
    album_df["release_date"] = pd.to_datetime(album_df["release_date"], format="mixed")

    _validate(album_df, "album_id", "album", date_column="release_date")

    tmp_path = "/tmp/album_transformed.parquet"
    album_df.to_parquet(tmp_path, index=False, engine="pyarrow")
    kwargs["ti"].xcom_push(key="album_path", value=tmp_path)


def _process_artist(**kwargs):
    data_path = kwargs["ti"].xcom_pull(task_ids="read_data_from_s3", key="spotify_data_path")
    with open(data_path, "r") as f:
        spotify_data = json.load(f)

    artist_list = []
    for data in spotify_data:
        for row in data["items"]:
            for artist in row["track"]["artists"]:
                artist_list.append({
                    "artist_id": artist["id"],
                    "artist_name": artist["name"],
                    "external_url": artist["external_urls"]["spotify"],
                })

    artist_df = pd.DataFrame(artist_list)
    artist_df = artist_df.drop_duplicates(subset=["artist_id"])

    _validate(artist_df, "artist_id", "artist")

    tmp_path = "/tmp/artist_transformed.parquet"
    artist_df.to_parquet(tmp_path, index=False, engine="pyarrow")
    kwargs["ti"].xcom_push(key="artist_path", value=tmp_path)


def _process_songs(**kwargs):
    data_path = kwargs["ti"].xcom_pull(task_ids="read_data_from_s3", key="spotify_data_path")
    with open(data_path, "r") as f:
        spotify_data = json.load(f)

    song_list = []
    for data in spotify_data:
        for row in data["items"]:
            track = row["track"]
            song_list.append({
                "song_id": track["id"],
                "song_name": track["name"],
                "duration_ms": track["duration_ms"],
                "url": track["external_urls"]["spotify"],
                "popularity": track["popularity"],
                "song_added": row["added_at"],
                "album_id": track["album"]["id"],
                "artist_id": track["album"]["artists"][0]["id"],
            })

    song_df = pd.DataFrame(song_list)
    song_df = song_df.drop_duplicates(subset=["song_id"])

    _validate(song_df, "song_id", "songs")

    tmp_path = "/tmp/songs_transformed.parquet"
    song_df.to_parquet(tmp_path, index=False, engine="pyarrow")
    kwargs["ti"].xcom_push(key="song_path", value=tmp_path)


def _validate(df, pk_column, dataset_name, date_column=None):
    """Data quality checks: row count, null PKs, duplicates, date validity."""
    if len(df) == 0:
        raise ValueError(f"{dataset_name}: empty dataframe - 0 rows")
    null_count = df[pk_column].isnull().sum()
    if null_count > 0:
        raise ValueError(f"{dataset_name}: {null_count} null values in {pk_column}")
    dup_count = df[pk_column].duplicated().sum()
    if dup_count > 0:
        raise ValueError(f"{dataset_name}: {dup_count} duplicate {pk_column} values after dedup")
    if date_column and date_column in df.columns:
        nat_count = df[date_column].isna().sum()
        if nat_count > 0:
            raise ValueError(f"{dataset_name}: {nat_count} unparseable dates in {date_column}")
    logger.info("%s: validated %d rows, 0 nulls, 0 duplicates", dataset_name, len(df))


def _store_parquet_to_s3(task_ids, xcom_key, s3_prefix, **kwargs):
    """Upload Parquet file from /tmp to S3."""
    config = _get_config()
    file_path = kwargs["ti"].xcom_pull(task_ids=task_ids, key=xcom_key)
    s3_hook = S3Hook(aws_conn_id=CONN_ID)
    ts = kwargs["ts_nodash"]
    s3_key = f"transformed_data/{s3_prefix}/{s3_prefix.replace('_data', '')}_transformed_{ts}.parquet"
    s3_hook.load_file(
        filename=file_path,
        key=s3_key,
        bucket_name=config["bucket_name"],
        replace=True,
    )
    logger.info("Stored %s to s3://%s/%s", s3_prefix, config["bucket_name"], s3_key)


def _move_processed_data(**kwargs):
    config = _get_config()
    s3_hook = S3Hook(aws_conn_id=CONN_ID)
    prefix = "raw_data/to_processed/"
    target_prefix = "raw_data/processed/"
    keys = s3_hook.list_keys(bucket_name=config["bucket_name"], prefix=prefix)
    keys = [k for k in keys if not k.endswith("/")]

    moved = 0
    for key in keys:
        if key.endswith(".json"):
            new_key = key.replace(prefix, target_prefix)
            s3_hook.copy_object(
                source_bucket_key=key,
                dest_bucket_key=new_key,
                source_bucket_name=config["bucket_name"],
                dest_bucket_name=config["bucket_name"],
            )
            s3_hook.delete_objects(bucket=config["bucket_name"], keys=[key])
            moved += 1
    logger.info("Archived %d raw files from to_processed -> processed", moved)


dag = DAG(
    dag_id="spotify_etl_dag_airflow",
    default_args=default_args,
    description="Production ETL pipeline: Spotify API -> S3 (Parquet)",
    schedule=timedelta(days=1),
    catchup=False,
)

fetch_data = PythonOperator(
    task_id="fetch_spotify_data",
    python_callable=_fetch_spotify_data,
    dag=dag,
)

store_raw_to_s3 = PythonOperator(
    task_id="upload_raw_to_s3",
    python_callable=_upload_raw_to_s3,
    dag=dag,
)

read_data_from_s3 = PythonOperator(
    task_id="read_data_from_s3",
    python_callable=_read_data_from_s3,
    dag=dag,
)

process_album = PythonOperator(
    task_id="process_album",
    python_callable=_process_album,
    dag=dag,
)

store_album_to_s3 = PythonOperator(
    task_id="store_album_to_s3",
    python_callable=_store_parquet_to_s3,
    op_kwargs={"task_ids": "process_album", "xcom_key": "album_path", "s3_prefix": "album_data"},
    dag=dag,
)

process_artist = PythonOperator(
    task_id="process_artist",
    python_callable=_process_artist,
    dag=dag,
)

store_artist_to_s3 = PythonOperator(
    task_id="store_artist_to_s3",
    python_callable=_store_parquet_to_s3,
    op_kwargs={"task_ids": "process_artist", "xcom_key": "artist_path", "s3_prefix": "artist_data"},
    dag=dag,
)

process_songs = PythonOperator(
    task_id="process_songs",
    python_callable=_process_songs,
    dag=dag,
)

store_songs_to_s3 = PythonOperator(
    task_id="store_songs_to_s3",
    python_callable=_store_parquet_to_s3,
    op_kwargs={"task_ids": "process_songs", "xcom_key": "song_path", "s3_prefix": "songs_data"},
    dag=dag,
)

move_processed_data_task = PythonOperator(
    task_id="move_processed_data",
    python_callable=_move_processed_data,
    dag=dag,
)

fetch_data >> store_raw_to_s3 >> read_data_from_s3
read_data_from_s3 >> process_album >> store_album_to_s3
read_data_from_s3 >> process_artist >> store_artist_to_s3
read_data_from_s3 >> process_songs >> store_songs_to_s3
[store_album_to_s3, store_artist_to_s3, store_songs_to_s3] >> move_processed_data_task
