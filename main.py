import os
import requests
import redis
import base64
from datetime import datetime, timedelta, timezone
from googleapiclient.discovery import build

# === Environment Variables ===
YOUTUBE_API_KEY = os.getenv('YOUTUBE_API_KEY')
GORGIAS_API_KEY = os.getenv('GORGIAS_API_KEY')
GORGIAS_API_URL = os.getenv('GORGIAS_API_URL', 'https://truecable.gorgias.com/api/tickets')
CHANNEL_ID = os.getenv('CHANNEL_ID')
GORGIAS_EMAIL = os.getenv("GORGIAS_EMAIL")
REDIS_URL = os.getenv("REDIS_URL")

# === Redis ===
redis_client = redis.Redis.from_url(REDIS_URL, decode_responses=True)

def is_comment_synced(comment_id):
    return redis_client.sismember("synced_youtube_comments", comment_id)

def mark_comment_as_synced(comment_id):
    redis_client.sadd("synced_youtube_comments", comment_id)

video_metadata_cache = {}

def get_video_metadata(youtube, video_id):
    """Fetch and cache title and thumbnail for a video."""
    if video_id in video_metadata_cache:
        return video_metadata_cache[video_id]

    try:
        resp = youtube.videos().list(part="snippet", id=video_id).execute()
        items = resp.get("items", [])
        if items:
            snippet = items[0]["snippet"]
            title = snippet["title"]
            thumbnail_url = snippet["thumbnails"]["default"]["url"]
            video_metadata_cache[video_id] = {"title": title, "thumbnail": thumbnail_url}
            return video_metadata_cache[video_id]
    except Exception as e:
        print(f"❌ Error fetching metadata for {video_id}: {e}")
    
    return {"title": "Unknown Video", "thumbnail": ""}

def get_cached_video_ids():
    return redis_client.lrange("cached_video_ids", 0, -1)

def cache_video_ids(video_ids):
    redis_client.delete("cached_video_ids")  # clear existing first
    if video_ids:
        redis_client.rpush("cached_video_ids", *video_ids)

def fetch_all_comments_from_all_videos():
    youtube = build("youtube", "v3", developerKey=YOUTUBE_API_KEY)
    cutoff_time = datetime.now(timezone.utc) - timedelta(hours=24)
    comments = []

    # === Check cache expiration (every 2 days) ===
    last_refresh_str = redis_client.get("video_cache_last_refresh")
    now = datetime.now(timezone.utc)

    if last_refresh_str:
        last_refresh = datetime.fromisoformat(last_refresh_str)
        if now - last_refresh > timedelta(days=2):
            print("♻️ Cache is older than 2 days. Clearing cached_video_ids.")
            redis_client.delete("cached_video_ids")
            redis_client.set("video_cache_last_refresh", now.isoformat())
    else:
        print("📅 No cache refresh timestamp found. Setting now.")
        redis_client.set("video_cache_last_refresh", now.isoformat())

    # === Load from cache or fetch video IDs ===
    cached_video_ids = redis_client.lrange("cached_video_ids", 0, -1)
    if cached_video_ids:
        print(f"✅ Loaded {len(cached_video_ids)} video IDs from cache.")
        video_ids = cached_video_ids
    else:
        print("🔍 Fetching video IDs from YouTube API...")
        video_ids = []
        next_page_token = None
        while True:
            resp = youtube.search().list(
                part="id",
                channelId=CHANNEL_ID,
                maxResults=50,
                type="video",
                order="date",
                pageToken=next_page_token
            ).execute()

            for item in resp.get("items", []):
                video_ids.append(item["id"]["videoId"])

            next_page_token = resp.get("nextPageToken")
            if not next_page_token:
                break

        # Cache results
        if video_ids:
            redis_client.delete("cached_video_ids")
            redis_client.rpush("cached_video_ids", *video_ids)
            redis_client.set("video_cache_last_refresh", now.isoformat())
            print(f"📦 Cached {len(video_ids)} video IDs.")

    # Fetch comments and replies
    for vid in video_ids:
        metadata = get_video_metadata(youtube, vid)
        video_title = metadata["title"]
        video_thumb = metadata["thumbnail"]
        next_comment_page = None

        while True:
            resp = youtube.commentThreads().list(
                part="snippet,replies",
                videoId=vid,
                maxResults=100,
                order="time",
                pageToken=next_comment_page
            ).execute()

            for item in resp.get("items", []):
                top_comment = item["snippet"]["topLevelComment"]
                top_snip = top_comment["snippet"]
                published_at = datetime.strptime(top_snip["publishedAt"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)

                if published_at < cutoff_time:
                    continue
                if top_snip.get("authorChannelId", {}).get("value") == CHANNEL_ID:
                    continue

                comments.append({
                    "id": top_comment["id"],
                    "author": top_snip.get("authorDisplayName", "Unknown"),
                    "text": top_snip.get("textDisplay", ""),
                    "published_at": top_snip.get("publishedAt", ""),
                    "video_id": vid,
                    "video_title": video_title,
                    "video_thumbnail": video_thumb,
                    "is_reply": False,
                    "parent_text": ""
                })

                # Replies
                if item["snippet"].get("totalReplyCount", 0) > 0:
                    replies = item.get("replies", {}).get("comments", [])
                    for reply in replies:
                        r_snip = reply["snippet"]
                        r_time = datetime.strptime(r_snip["publishedAt"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                        if r_time < cutoff_time:
                            continue
                        if r_snip.get("authorChannelId", {}).get("value") == CHANNEL_ID:
                            continue

                        comments.append({
                            "id": reply["id"],
                            "author": r_snip.get("authorDisplayName", "Unknown"),
                            "text": r_snip.get("textDisplay", ""),
                            "published_at": r_snip.get("publishedAt", ""),
                            "video_id": vid,
                            "video_title": video_title,
                            "video_thumbnail": video_thumb,
                            "is_reply": True,
                            "parent_text": top_snip.get("textDisplay", "")
                        })

            next_comment_page = resp.get("nextPageToken")
            if not next_comment_page:
                break

    return comments

def create_gorgias_ticket(comment):
    base_link = f"https://www.youtube.com/watch?v={comment['video_id']}"
    comment_link = f"{base_link}&lc={comment['id']}"

    body_lines = [
        f"**Comment:** {comment['text']}",
        f"**Author:** {comment['author']}",
        f"**Published At:** {comment['published_at']}",
        f"**Video Title:** {comment.get('video_title', 'Unknown')}",
        f"[View Comment on YouTube]({comment_link})"
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
                "sender": {"name": comment['author']},
                "body_text": "\n\n".join(body_lines)
            }
        ]
    }

    auth = base64.b64encode(f"{GORGIAS_EMAIL}:{GORGIAS_API_KEY}".encode()).decode()
    headers = {
        "Authorization": f"Basic {auth}",
        "Content-Type": "application/json"
    }

    try:
        r = requests.post(GORGIAS_API_URL, json=ticket_data, headers=headers)
        if r.status_code == 201:
            print(f"✅ Ticket created for: {comment['id']}")
        else:
            print(f"❌ Gorgias error {r.status_code}: {r.text}")
    except Exception as e:
        print(f"❌ Ticket send failed: {e}")

def main():
    comments = fetch_all_comments_from_all_videos()

    for c in comments:
        if is_comment_synced(c["id"]):
            print(f"⏭️ Already synced: {c['id']}")
            continue

        create_gorgias_ticket(c)
        mark_comment_as_synced(c["id"])

if __name__ == "__main__":
    main()
