import os
import requests
from googleapiclient.discovery import build
import sqlite3
from datetime import datetime

# === Environment Variables ===
YOUTUBE_API_KEY = os.getenv('YOUTUBE_API_KEY')
GORGIAS_API_KEY = os.getenv('GORGIAS_API_KEY')
GORGIAS_API_URL = os.getenv('GORGIAS_API_URL', 'https://truecable.gorgias.com/api/tickets')
CHANNEL_ID = os.getenv('CHANNEL_ID')

# === Database Setup ===
DB_FILE = "data.db"

def init_db():
    """Initialize the SQLite database to store the last synced comment."""
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

def save_last_synced_comment(comment_id):
    """Save the last synced comment ID to the database."""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("INSERT OR REPLACE INTO sync (id) VALUES (?)", (comment_id,))
    conn.commit()
    conn.close()

# === YouTube API ===
def fetch_youtube_comments():
    """Fetch the latest comments from your YouTube channel."""
    try:
        youtube = build("youtube", "v3", developerKey=YOUTUBE_API_KEY)

        # Request to get comments for the channel
        request = youtube.commentThreads().list(
            part="snippet",
            allThreadsRelatedToChannelId=CHANNEL_ID,
            maxResults=20,  # Adjust as needed
            order="time"  # Fetch the latest comments first
        )
        response = request.execute()

        # Extract comment details
        comments = []
        for item in response.get("items", []):
            snippet = item["snippet"]["topLevelComment"]["snippet"]
            comment_data = {
                "id": item["id"],
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
    # Construct the link to the highlighted comment
    comment_link = f"https://www.youtube.com/watch?v={comment['video_id']}&lc={comment['id']}"

    # Create the ticket payload
    ticket_data = {
        "subject": f"New Comment from {comment['author']}",
        "description": (
            f"**Comment:** {comment['text']}\n\n"
            f"**Author:** {comment['author']}\n"
            f"**Published At:** {comment['published_at']}\n\n"
            f"[View Comment on YouTube]({comment_link})"
        ),
        "tags": ["YouTube", "Comment"],
        "assigned_user_id": "1591495"  # Replace with your actual Gorgias User ID (string or integer)
    }

    headers = {
        "Authorization": f"Bearer {GORGIAS_API_KEY}",
        "Content-Type": "application/json"
    }

    try:
        response = requests.post(GORGIAS_API_URL, json=ticket_data, headers=headers)

        if response.status_code == 201:
            print(f"Ticket created for comment: {comment['text']}")
        else:
            print(f"Failed to create ticket: {response.status_code}, {response.text}")

    except requests.exceptions.RequestException as e:
        print(f"Error connecting to Gorgias API: {e}")

# === Main Process ===
def main():
    """Main function to fetch comments and sync with Gorgias."""
    init_db()

    # Get the last synced comment ID
    last_synced = get_last_synced_comment()

    # Fetch comments from YouTube
    comments = fetch_youtube_comments()

    for comment in comments:
        # Skip already synced comments
        if comment["id"] == last_synced:
            break

        # Create a Gorgias ticket
        create_gorgias_ticket(comment)

        # Save the last synced comment ID
        save_last_synced_comment(comment["id"])

if __name__ == "__main__":
    main()
