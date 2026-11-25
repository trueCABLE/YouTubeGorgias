import os
import sys
import json
import base64
from datetime import datetime, timedelta, timezone

import redis
import requests
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# === Environment Variables ===
YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY")
GORGIAS_API_KEY = os.getenv("GORGIAS_API_KEY")
GORGIAS_API_URL = os.getenv("GORGIAS_API_URL", "https://truecable.gorgias.com/api/tickets")
CHANNEL_ID = os.getenv("CHANNEL_ID")
GORGIAS_EMAIL = os.getenv("GORGIAS_EMAIL")
REDIS_URL = os.getenv("REDIS_URL")

# How far back to look for comments. Longer than the run interval on purpose:
# Redis dedupe prevents duplicate tickets, and the overlap covers late or missed runs.
LOOKBACK_HOURS = int(os.getenv("LOOKBACK_HOURS", "48"))

# Optional one-time backfill. Set to an ISO date/time (e.g. 2026-11-09 or 2026-11-09T05:00:00Z)
# to re-send every comment published since then, even if Redis marked it as synced.
# It only takes effect once per value (tracked in Redis), so leaving it set won't cause
# daily duplicates, but remove it after the backfill run anyway.
RESEND_SINCE = os.getenv("RESEND_SINCE", "").strip()

SYNCED_KEY = "synced_youtube_comments"
RESEND_DONE_KEY = "resend_since_done"
LEGACY_KEYS = ("cached_video_ids", "video_cache_last_refresh")  # no longer used

# === Redis ===
redis_client = redis.Redis.from_url(REDIS_URL, decode_responses=True) if REDIS_URL else None


def is_comment_synced(comment_id):
    return redis_client.sismember(SYNCED_KEY, comment_id)


def mark_comment_as_synced(comment_id):
    redis_client.sadd(SYNCED_KEY, comment_id)


def parse_time(ts):
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def parse_resend_since(value):
    if not value:
        return None
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def redact(text):
    """Keep secrets out of the logs (YouTube errors include the full request URL with the key)."""
    text = str(text)
    for secret in (YOUTUBE_API_KEY, GORGIAS_API_KEY):
        if secret:
            text = text.replace(secret, "***REDACTED***")
    return text


def http_reason(err):
    """Pull the YouTube error reason (e.g. commentsDisabled, quotaExceeded) out of an HttpError."""
    try:
        payload = json.loads(err.content.decode("utf-8"))
        return payload["error"]["errors"][0].get("reason", "")
    except Exception:
        return ""


# === Gorgias ===
def gorgias_headers():
    auth = base64.b64encode(f"{GORGIAS_EMAIL}:{GORGIAS_API_KEY}".encode()).decode()
    return {"Authorization": f"Basic {auth}", "Content-Type": "application/json"}


def check_gorgias_auth():
    """Fail fast with a clear message if the Gorgias credentials don't work."""
    try:
        r = requests.get(GORGIAS_API_URL, params={"limit": 1}, headers=gorgias_headers(), timeout=30)
    except Exception as e:
        raise RuntimeError(f"Could not reach Gorgias: {e}")
    if r.status_code in (401, 403):
        raise RuntimeError(
            f"Gorgias auth failed ({r.status_code}). Check GORGIAS_EMAIL and GORGIAS_API_KEY "
            "and make sure that Gorgias user is still active."
        )
    if r.status_code >= 400:
        raise RuntimeError(f"Gorgias check failed ({r.status_code}): {r.text[:300]}")
    print("🔐 Gorgias credentials OK.")


def create_gorgias_ticket(comment):
    """Returns True only if Gorgias confirmed the ticket was created."""
    base_link = f"https://www.youtube.com/watch?v={comment['video_id']}"
    comment_link = f"{base_link}&lc={comment['id']}"

    body_lines = [
        f"**Comment:** {comment['text']}",
        f"**Author:** {comment['author']}",
        f"**Published At:** {comment['published_at']}",
        f"**Video Title:** {comment.get('video_title', 'Unknown')}",
        f"[View Comment on YouTube]({comment_link})",
    ]
    if comment.get("is_reply") and comment.get("parent_text"):
        body_lines.insert(1, f"**In reply to:** {comment['parent_text']}")
    if comment.get("video_thumbnail"):
        body_lines.append(f"\n![Video Thumbnail]({comment['video_thumbnail']})")

    ticket_data = {
        "subject": f"New YouTube Comment from {comment['author']}",
        "channel": "api",
        "via": "api",
        "tags": ["YouTube"],
        "messages": [
            {
                "channel": "api",
                "via": "api",
                "from_agent": False,
                "sender": {"name": comment["author"]},
                "body_text": "\n\n".join(body_lines),
            }
        ],
    }

    try:
        r = requests.post(GORGIAS_API_URL, json=ticket_data, headers=gorgias_headers(), timeout=30)
    except Exception as e:
        print(f"❌ Ticket send failed for {comment['id']}: {e}")
        return False

    if r.status_code in (200, 201):
        print(f"✅ Ticket created for: {comment['id']}")
        return True
    print(f"❌ Gorgias error {r.status_code} for {comment['id']}: {r.text[:500]}")
    return False


# === Videos ===
def fetch_channel_videos(youtube):
    """
    List every upload via the channel's uploads playlist.
    playlistItems costs 1 quota unit per page (search.list costs 100) and
    returns title/thumbnail too, so no extra videos.list calls are needed.
    """
    resp = youtube.channels().list(part="contentDetails", id=CHANNEL_ID).execute()
    items = resp.get("items", [])
    if not items:
        raise RuntimeError(f"Channel {CHANNEL_ID} not found. Check the CHANNEL_ID env var.")
    uploads_id = items[0]["contentDetails"]["relatedPlaylists"]["uploads"]

    videos = []
    page = None
    while True:
        resp = youtube.playlistItems().list(
            part="snippet,contentDetails",
            playlistId=uploads_id,
            maxResults=50,
            pageToken=page,
        ).execute()
        for item in resp.get("items", []):
            snip = item["snippet"]
            videos.append({
                "id": item["contentDetails"]["videoId"],
                "title": snip.get("title", "Unknown Video"),
                "thumbnail": snip.get("thumbnails", {}).get("default", {}).get("url", ""),
            })
        page = resp.get("nextPageToken")
        if not page:
            break

    print(f"🎬 Found {len(videos)} videos on the channel.")
    return videos


# === Comments ===
def build_comment(c, video, is_reply, parent_text=""):
    snip = c["snippet"]
    return {
        "id": c["id"],
        "author": snip.get("authorDisplayName", "Unknown"),
        "text": snip.get("textDisplay", ""),
        "published_at": snip.get("publishedAt", ""),
        "published_dt": parse_time(snip["publishedAt"]),
        "video_id": video["id"],
        "video_title": video["title"],
        "video_thumbnail": video["thumbnail"],
        "is_reply": is_reply,
        "parent_text": parent_text,
    }


def is_own_comment(snip):
    return snip.get("authorChannelId", {}).get("value") == CHANNEL_ID


def fetch_all_replies(youtube, parent_id):
    replies = []
    page = None
    while True:
        resp = youtube.comments().list(
            part="snippet",
            parentId=parent_id,
            maxResults=100,
            textFormat="plainText",
            pageToken=page,
        ).execute()
        replies.extend(resp.get("items", []))
        page = resp.get("nextPageToken")
        if not page:
            break
    return replies


def fetch_comments_for_video(youtube, video, cutoff):
    comments = []
    page = None
    while True:
        resp = youtube.commentThreads().list(
            part="snippet,replies",
            videoId=video["id"],
            maxResults=100,
            order="time",
            textFormat="plainText",  # avoids &#39; and <br> showing up in tickets
            pageToken=page,
        ).execute()

        for item in resp.get("items", []):
            top = item["snippet"]["topLevelComment"]
            top_snip = top["snippet"]

            if parse_time(top_snip["publishedAt"]) >= cutoff and not is_own_comment(top_snip):
                comments.append(build_comment(top, video, is_reply=False))

            # Replies are checked even when the top-level comment is old:
            # a new reply on an old thread still needs a ticket.
            total_replies = item["snippet"].get("totalReplyCount", 0)
            if total_replies == 0:
                continue
            replies = item.get("replies", {}).get("comments", [])
            if total_replies > len(replies):
                # commentThreads only embeds a handful of replies; fetch the rest.
                replies = fetch_all_replies(youtube, top["id"])
            for reply in replies:
                r_snip = reply["snippet"]
                if parse_time(r_snip["publishedAt"]) < cutoff or is_own_comment(r_snip):
                    continue
                comments.append(build_comment(
                    reply, video, is_reply=True, parent_text=top_snip.get("textDisplay", "")
                ))

        page = resp.get("nextPageToken")
        if not page:
            break
    return comments


def fetch_recent_comments(cutoff):
    youtube = build("youtube", "v3", developerKey=YOUTUBE_API_KEY, cache_discovery=False)
    comments = []

    for video in fetch_channel_videos(youtube):
        try:
            comments.extend(fetch_comments_for_video(youtube, video, cutoff))
        except HttpError as e:
            reason = http_reason(e)
            # One problem video (comments off, removed, or a YouTube processing error)
            # must not stop the whole run.
            if reason in ("commentsDisabled", "videoNotFound", "forbidden", "processingFailure") \
                    or e.resp.status == 404:
                print(f"⏭️ Skipping {video['id']} ({video['title']}): {reason or e.resp.status}")
                continue
            # Quota/auth problems should fail the run loudly so Render marks it failed.
            raise

    print(f"💬 Found {len(comments)} comments/replies since {cutoff.isoformat()}.")
    return comments


# === Main ===
def check_config():
    missing = [name for name in
               ("YOUTUBE_API_KEY", "GORGIAS_API_KEY", "GORGIAS_EMAIL", "CHANNEL_ID", "REDIS_URL")
               if not os.getenv(name)]
    if missing:
        raise RuntimeError(f"Missing env vars: {', '.join(missing)}")


def main():
    check_config()
    check_gorgias_auth()

    # Clean up keys from the old video-ID cache.
    redis_client.delete(*LEGACY_KEYS)

    cutoff = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)

    resend_since = parse_resend_since(RESEND_SINCE)
    if resend_since and redis_client.sismember(RESEND_DONE_KEY, RESEND_SINCE):
        print(f"ℹ️ Backfill for RESEND_SINCE={RESEND_SINCE} already done. Remove the env var.")
        resend_since = None
    if resend_since:
        print(f"🔁 Backfill: re-sending all comments since {resend_since.isoformat()}.")
        cutoff = min(cutoff, resend_since)

    comments = fetch_recent_comments(cutoff)

    created = skipped = failed = 0
    for c in comments:
        force = resend_since is not None and c["published_dt"] >= resend_since
        if not force and is_comment_synced(c["id"]):
            skipped += 1
            continue
        if create_gorgias_ticket(c):
            # Only mark as synced after Gorgias accepts it, so failures retry next run.
            mark_comment_as_synced(c["id"])
            created += 1
        else:
            failed += 1

    if resend_since and failed == 0:
        redis_client.sadd(RESEND_DONE_KEY, RESEND_SINCE)

    print(f"📊 Done. Created: {created} | Already synced: {skipped} | Failed: {failed}")
    if failed:
        raise RuntimeError(f"{failed} ticket(s) failed to send. See errors above.")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        # Print a redacted message instead of a raw traceback, which would include the API key.
        print(f"❌ Run failed: {type(e).__name__}: {redact(e)}")
        sys.exit(1)
