import os
import requests
import sqlite3
import base64
from datetime import datetime, timedelta, timezone
from googleapiclient.discovery import build

# === Environment Variables ===
YOUTUBE_API_KEY = os.getenv('YOUTUBE_API_KEY')
GORGIAS_API_KEY = os.getenv('GORGIAS_API_KEY')
GORGIAS_API_URL = os.getenv('GORGIAS_API_URL', 'https://truecable.gorgias.com/api/tickets')
CHANNEL_ID = os.getenv('CHANNEL_ID')
GORGIAS_EMAIL = os.getenv("GORGIAS_EMAIL")

# === Database Setup ===
DB_FILE = "data.db"

def init_db():
    """Initialize the SQLite database to store synced comment IDs."""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS sync (
            id TEXT PRIMARY KEY,
            synced_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()

def is_comment_synced(comment_id):
    """Check if a comment has already been processed."""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM sync WHERE id = ?", (comment_id,))
    exists = cursor.fetchone() is not None
    conn.close()
    return exists

def mark_comment_as_synced(comment_id):
    """Record a comment ID as synced."""
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute("INSERT OR IGNORE INTO sync (id) VALUES (?)", (comment_id,))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"❌ Error saving comment ID {comment_id}: {e}")

def fetch_youtube_comments():
    """Fetch recent top-level comments from your channel."""
    try:
        youtube = build("youtube", "v3", developerKey=YOUTUBE_API_KEY)
        request = youtube.commentThreads().list(
            part="snippet",
            allThreadsRelatedToChannelId=CHANNEL_ID,
            maxResults=100,
            order="time"
        )
        response = request.execute()

        comments = []
        for item in response.get("items", []):
            comment = item["snippet"]["topLevelComment"]
            snippet = comment["snippet"]
            comment_data = {
                "id": comment["id"],  # Unique comment ID
                "author": snippet.get("authorDisplayName", "Unknown"),
                "text": snippet.get("textDisplay", ""),
                "published_at": snippet.get("publishedAt", ""),
                "video_id": snippet.get("videoId", "")
            }
            comments.append(comment_data)

        return comments

    except Exception as e:
        print(f"❌ Error fetching YouTube comments: {e}")
        return []

def create_gorgias_ticket(comment):
    """Send the YouTube comment to Gorgias as a ticket."""
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
    init_db()
    comments = fetch_youtube_comments()
    cutoff_time = datetime.now(timezone.utc) - timedelta(hours=24)

    for comment in comments:
        try:
            published_time = datetime.strptime(comment["published_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        except Exception:
            print(f"⚠️ Skipping comment with invalid timestamp: {comment}")
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
