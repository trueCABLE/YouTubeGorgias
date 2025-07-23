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
REDIS_URL = os.getenv("REDIS_URL")  # Set this in Render

# === Redis Client ===
redis_client = redis.Redis.from_url(REDIS_URL, decode_responses=True)

def is_comment_synced(comment_id):
    return redis_client.sismember("synced_youtube_comments", comment_id)

def mark_comment_as_synced(comment_id):
    redis_client.sadd("synced_youtube_comments", comment_id)

def fetch_youtube_comments_and_replies():
    """Fetch top-level comments and their replies from the channel."""
    try:
        youtube = build("youtube", "v3", developerKey=YOUTUBE_API_KEY)

        request = youtube.commentThreads().list(
            part="snippet,replies",
            allThreadsRelatedToChannelId=CHANNEL_ID,
            maxResults=100,
            order="time"
        )
        response = request.execute()

        all_comments = []

        for item in response.get("items", []):
            # === Top-level comment ===
            top_comment = item["snippet"]["topLevelComment"]
            top_snippet = top_comment["snippet"]
            all_comments.append({
                "id": top_comment["id"],
                "author": top_snippet.get("authorDisplayName", "Unknown"),
                "text": top_snippet.get("textDisplay", ""),
                "published_at": top_snippet.get("publishedAt", ""),
                "video_id": top_snippet.get("videoId", "")
            })

            # === Replies (if any) ===
            reply_count = item["snippet"].get("totalReplyCount", 0)
            if reply_count > 0:
                parent_id = top_comment["id"]
                reply_request = youtube.comments().list(
                    part="snippet",
                    parentId=parent_id,
                    maxResults=100
                )
                reply_response = reply_request.execute()
                for reply in reply_response.get("items", []):
                    reply_snippet = reply["snippet"]
                    all_comments.append({
                        "id": reply["id"],
                        "author": reply_snippet.get("authorDisplayName", "Unknown"),
                        "text": reply_snippet.get("textDisplay", ""),
                        "published_at": reply_snippet.get("publishedAt", ""),
                        "video_id": reply_snippet.get("videoId", "")
                    })

        return all_comments

    except Exception as e:
        print(f"❌ Error fetching comments: {e}")
        return []

def create_gorgias_ticket(comment):
    comment_link = f"https://www.youtube.com/watch?v={comment['video_id']}&lc={comment['id']}"
    ticket_data = {
        "subject": f"New Comment from {comment['author']}",
        "channel": "api",
        "via": "api",
        "tags": ["YouTube"],
        "messages": [
            {
                "channel": "api",
                "via": "api",
                "from_agent": False,
                "sender": {
                    "name": comment['author']
                },
                "body_text": (
                    f"**Comment:** {comment['text']}\n\n"
                    f"**Author:** {comment['author']}\n"
                    f"**Published At:** {comment['published_at']}\n\n"
                    f"[View Comment on YouTube]({comment_link})"
                )
            }
        ]
    }

    auth_string = f"{GORGIAS_EMAIL}:{GORGIAS_API_KEY}"
    auth_encoded = base64.b64encode(auth_string.encode()).decode()
    headers = {
        "Authorization": f"Basic {auth_encoded}",
        "Content-Type": "application/json"
    }

    try:
        response = requests.post(GORGIAS_API_URL, json=ticket_data, headers=headers)
        if response.status_code == 201:
            print(f"✅ Ticket created for comment: {comment['id']}")
        else:
            print(f"❌ Gorgias error {response.status_code}: {response.text}")
    except Exception as e:
        print(f"❌ Failed to send ticket: {e}")

def main():
    comments = fetch_youtube_comments_and_replies()
    cutoff_time = datetime.now(timezone.utc) - timedelta(hours=24)

    for comment in comments:
        try:
            published_time = datetime.strptime(comment["published_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        except Exception:
            print(f"⚠️ Skipping invalid timestamp: {comment}")
            continue

        if published_time < cutoff_time:
            print(f"⏩ Skipping old comment: {comment['id']} from {published_time}")
            continue

        if is_comment_synced(comment["id"]):
            print(f"⏭️ Already synced: {comment['id']}")
            continue

        create_gorgias_ticket(comment)
        mark_comment_as_synced(comment["id"])

if __name__ == "__main__":
    main()
