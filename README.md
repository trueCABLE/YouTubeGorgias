# YouTube to Gorgias Bot

This bot fetches new YouTube comments from a channel and syncs them as tickets in Gorgias. It skips duplicate comments by tracking the last synced comment.

## Features
- Fetches new comments from your YouTube channel.
- Creates Gorgias tickets for each comment.
- Prevents duplicate tickets using a database.

## Setup

### Prerequisites
- Python 3.8+
- Google Cloud project with YouTube Data API enabled.
- Gorgias account with an API key.

### Installation
1. Clone this repository:
   ```bash
   git clone https://github.com/your-repo/youtube-to-gorgias
   cd youtube-to-gorgias
