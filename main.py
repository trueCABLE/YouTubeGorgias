import os
import requests
from googleapiclient.discovery import build
import sqlite3
from datetime import datetime
import base64

# === Environment Variables ===
YOUTUBE_API_KEY = os.getenv('YOUTUBE_API_KEY')
GORGIAS_API_KEY = os.getenv('GORGIAS_API_KEY')
GORGIAS_API_URL = os.getenv('GORGIAS_API_URL', 'https://truecable.gorgias.com/api/tickets')
CHANNEL_ID = os.getenv('CHANNEL_ID')

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

def get_last_synced_comment():
    """Retrieve the last synced comment ID from the database."""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM sync ORDER BY synced_at DESC LIMIT 1")
    result = cursor.fetchone()
    conn.close()
    return result[0] if result else None

def mark_comment_as_synced(comment_id):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("INSERT OR IGNORE INTO sync (id) VALUES (?)", (comment_id,))
    conn.commit()
    conn.close()

# === YouTube API ===
def fetch_youtube_comments():
    """Fetch the latest comments from your YouTube channel."""
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
            snippet = item["snippet"]["topLevelComment"]["snippet"]
            comment_data = {
                "id": item["snippet"]["topLevelComment"]["id"],  # ✅ real unique comment ID
                "author": snippet["authorDisplayName"],
                "text": snippet["textDisplay"],
                "published_at": snippet["publishedAt"],
                "video_id": snippet["videoId"],
            }
            comments.append(comment_data)

        return comments

    except Exception as e:
        print(f"Error fetching YouTube comments: {e}")
        return []

# === Gorgias API ===
def create_gorgias_ticket(comment):
    """Create a Gorgias ticket for a YouTube comment."""
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
                    f"[View Comment on YouTube](https://www.youtube.com/watch?v={comment['video_id']}&lc={comment['id']})"
                )
            }
        ]
    }

    # Use Basic Auth with email and API key
    GORGIAS_EMAIL = os.getenv("GORGIAS_EMAIL")
    auth_string = f"{GORGIAS_EMAIL}:{GORGIAS_API_KEY}"
    auth_encoded = base64.b64encode(auth_string.encode()).decode()

    headers = {
        "Authorization": f"Basic {auth_encoded}",
        "Content-Type": "application/json"
    }

    try:
        response = requests.post(GORGIAS_API_URL, json=ticket_data, headers=headers)

        if response.status_code == 201:
            print(f"Ticket created for comment: {comment['text']}")
        else:
            print(f"Failed to create ticket: {response.status_code} - {response.text}")
            print("Request Payload:", ticket_data)
            print("Headers:", headers)

    except requests.exceptions.RequestException as e:
        print(f"Error connecting to Gorgias API: {e}")

def is_comment_synced(comment_id):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM sync WHERE id = ?", (comment_id,))
    exists = cursor.fetchone() is not None
    conn.close()
    return exists

def main():
    init_db()

    # Fetch latest comments from YouTube
    comments = fetch_youtube_comments()

    for comment in comments:
        if is_comment_synced(comment["id"]):
            continue  # Skip already-synced comments

        # Only ticket new, unseen comments
        create_gorgias_ticket(comment)
        mark_comment_as_synced(comment["id"])

if __name__ == "__main__":
    main()
