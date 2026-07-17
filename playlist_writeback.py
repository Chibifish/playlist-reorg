#!/usr/bin/env python3
"""Playlist Reorg — write-back script.

Reads a CSV of classified videos and rebuilds them as topic-based playlists
on the authenticated user's own YouTube channel.

What it does:
  1. OAuth sign-in (installed-app flow, token cached locally in token.json)
  2. Creates each target playlist via playlists.insert (50 quota units each)
  3. Adds each video via playlistItems.insert (50 quota units each)

Designed for a one-time, single-user personal reorganization (~4,400 inserts
total, ~219K quota units, run over several days at 50K units/day).

Resumable: every successful insert is logged to progress.csv, so re-running
after hitting the daily quota limit skips everything already done.

Expected input CSV columns (adjust COL_* below if yours differ):
  videoId   — the 11-character YouTube video ID
  playlist  — the name of the target playlist

Usage:
  pip install google-api-python-client google-auth-oauthlib
  python playlist_writeback.py Stash-classified.csv

Requires client_secret.json (OAuth desktop client credentials) in the same
directory. Neither client_secret.json, token.json, nor the CSVs should be
committed to the repo — see .gitignore.
"""

import csv
import os
import sys
import time

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# ---- Config -----------------------------------------------------------------

SCOPES = ["https://www.googleapis.com/auth/youtube"]
CLIENT_SECRET_FILE = "client_secret.json"
TOKEN_FILE = "token.json"
PROGRESS_FILE = "progress.csv"      # log of completed inserts (for resume)
PLAYLIST_MAP_FILE = "playlists.csv" # log of created playlists name -> ID

COL_VIDEO_ID = "videoId"
COL_PLAYLIST = "playlist"

PLAYLIST_PRIVACY = "unlisted"       # "private" | "unlisted" | "public"
SLEEP_BETWEEN_INSERTS = 0.3         # seconds; gentle pacing


# ---- Auth -------------------------------------------------------------------

def get_service():
    creds = None
    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                CLIENT_SECRET_FILE, SCOPES
            )
            creds = flow.run_local_server(port=0)
        with open(TOKEN_FILE, "w") as f:
            f.write(creds.to_json())
    return build("youtube", "v3", credentials=creds)


# ---- Progress tracking ------------------------------------------------------

def load_done():
    """Set of (videoId, playlistName) pairs already inserted."""
    done = set()
    if os.path.exists(PROGRESS_FILE):
        with open(PROGRESS_FILE, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                done.add((row["videoId"], row["playlist"]))
    return done


def log_done(video_id, playlist_name):
    new_file = not os.path.exists(PROGRESS_FILE)
    with open(PROGRESS_FILE, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(["videoId", "playlist"])
        w.writerow([video_id, playlist_name])


def load_playlist_map():
    """Map of playlist name -> playlist ID for playlists already created."""
    mapping = {}
    if os.path.exists(PLAYLIST_MAP_FILE):
        with open(PLAYLIST_MAP_FILE, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                mapping[row["name"]] = row["playlistId"]
    return mapping


def log_playlist(name, playlist_id):
    new_file = not os.path.exists(PLAYLIST_MAP_FILE)
    with open(PLAYLIST_MAP_FILE, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(["name", "playlistId"])
        w.writerow([name, playlist_id])


# ---- YouTube operations -----------------------------------------------------

def create_playlist(youtube, name):
    resp = youtube.playlists().insert(
        part="snippet,status",
        body={
            "snippet": {
                "title": name,
                "description": "Created by Playlist Reorg (one-time "
                               "reorganization of a saved playlist).",
            },
            "status": {"privacyStatus": PLAYLIST_PRIVACY},
        },
    ).execute()
    return resp["id"]


def insert_video(youtube, playlist_id, video_id):
    youtube.playlistItems().insert(
        part="snippet",
        body={
            "snippet": {
                "playlistId": playlist_id,
                "resourceId": {"kind": "youtube#video", "videoId": video_id},
            }
        },
    ).execute()


def is_quota_error(err: HttpError) -> bool:
    try:
        return any(
            e.get("reason") in ("quotaExceeded", "dailyLimitExceeded")
            for e in err.error_details
        )
    except Exception:
        return err.resp.status == 403 and b"quota" in err.content.lower()


# ---- Main -------------------------------------------------------------------

def main():
    if len(sys.argv) != 2:
        sys.exit(f"Usage: python {sys.argv[0]} <classified.csv>")

    # Read and group the input.
    groups = {}  # playlist name -> [video IDs]
    with open(sys.argv[1], newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            vid = row[COL_VIDEO_ID].strip()
            pl = row[COL_PLAYLIST].strip()
            if vid and pl:
                groups.setdefault(pl, []).append(vid)

    done = load_done()
    playlist_ids = load_playlist_map()
    total = sum(len(v) for v in groups.values())
    remaining = total - len(done)
    print(f"{len(groups)} playlists, {total} videos total, "
          f"{remaining} remaining.")

    youtube = get_service()
    inserted = skipped = failed = 0

    for name, video_ids in groups.items():
        # Create the playlist if we haven't yet.
        if name not in playlist_ids:
            try:
                playlist_ids[name] = create_playlist(youtube, name)
                log_playlist(name, playlist_ids[name])
                print(f"Created playlist: {name}")
            except HttpError as err:
                if is_quota_error(err):
                    print("Daily quota reached — run again tomorrow to resume.")
                    return
                print(f"FAILED to create playlist {name!r}: {err}")
                continue

        pl_id = playlist_ids[name]
        for vid in video_ids:
            if (vid, name) in done:
                skipped += 1
                continue
            try:
                insert_video(youtube, pl_id, vid)
                log_done(vid, name)
                inserted += 1
                if inserted % 50 == 0:
                    print(f"  ...{inserted} inserted this run")
            except HttpError as err:
                if is_quota_error(err):
                    print(f"Daily quota reached after {inserted} inserts — "
                          f"run again tomorrow to resume.")
                    return
                # Video may have gone private/deleted since enrichment.
                print(f"  FAILED {vid} -> {name}: {err.resp.status}")
                failed += 1
            time.sleep(SLEEP_BETWEEN_INSERTS)

    print(f"Done. Inserted {inserted}, skipped {skipped} (already done), "
          f"failed {failed}.")


if __name__ == "__main__":
    main()
