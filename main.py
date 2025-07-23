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

def fetch_comments_and_replies():
    youtube = build("youtube", "v3", developerKey=YOUTUBE_API_KEY)
    comments = []
    cutoff_time = datetime.now(timezone.utc) - timedelta(hours=24)

    request = youtube.commentThreads().list(
        part="snippet,replies",
        allThreadsRelatedToChannelId=CHANNEL_ID,
        maxResults=100,
        order="time"
    )
    response = request.execute()

    for item in response.get("items", []):
        top_comment = item["snippet"]["topLevelComment"]
        top_snippet = top_comment["snippet"]
        published_at = datetime.strptime(top_snippet["publishedAt"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)

        # Stop if comment is older than cutoff (no point in continuing)
        if published_at < cutoff_time:
            print(f"⏹️ Reached cutoff at comment {top_comment['id']} from {published_at}")
            break

        # Skip if from our own channel
        if top_snippet.get("authorChannelId", {}).get("value") == CHANNEL_ID:
            continue

        comments.append({
            "id": top_comment["id"],
            "author": top_snippet.get("authorDisplayName", "Unknown"),
            "text": top_snippet.get("textDisplay", ""),
            "published_at": top_snippet.get("publishedAt", ""),
            "video_id": top_snippet.get("videoId", "")
        })

        # Fetch replies (if any)
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
                    "video_id": r_snip.get("videoId", "")
                })

    return comments

def create_gorgias_ticket(comment):
    base_link = f"https://www.youtube.com/watch?v={comment['video_id']}"
    comment_link = f"{base_link}&lc={comment['id']}"

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
                "body_text": (
                    f"**Comment:** {comment['text']}\n\n"
                    f"**Author:** {comment['author']}\n"
                    f"**Published At:** {comment['published_at']}\n\n"
                    f"[View Comment on YouTube]({comment_link})"
                )
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
    comments = fetch_comments_and_replies()

    for c in comments:
        if is_comment_synced(c["id"]):
            print(f"⏭️ Already synced: {c['id']}")
            continue

        create_gorgias_ticket(c)
        mark_comment_as_synced(c["id"])

if __name__ == "__main__":
    main()
