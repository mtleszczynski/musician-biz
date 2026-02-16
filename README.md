# Musician Expense Tracker

A Discord bot that helps musicians and music teachers track income and expenses for taxes.
Send a photo of a receipt, a text description, or a voice message — the bot uses AI to
extract the financial data and logs it to a Google Sheet.

## Features

- **Photo recognition** — snap a receipt or invoice, the bot reads it automatically
- **Voice messages** — record a quick audio note, the bot transcribes and extracts data
- **Text input** — type a quick note like "Sarah paid $50 for piano lesson"
- **Smart categorization** — auto-categorizes income and expenses
- **Clarifying questions** — if something is unclear, the bot asks in a Discord thread
- **Google Sheets** — all data goes to a spreadsheet, with links back to the Discord conversation
- **Monthly summaries** — `!summary` shows income/expense breakdown by category

## Setup Guide

### 1. Create a Discord Bot

1. Go to [discord.com/developers/applications](https://discord.com/developers/applications)
2. Click **New Application**, give it a name (e.g., "Expense Tracker")
3. Go to the **Bot** tab on the left sidebar
4. Click **Reset Token** and copy the token — you'll need this as `DISCORD_TOKEN`
5. Scroll down and enable **Message Content Intent** (this is required!)
6. Go to **OAuth2 → URL Generator** on the left sidebar
7. Under **Scopes**, check `bot`
8. Under **Bot Permissions**, check:
   - Send Messages
   - Create Public Threads
   - Send Messages in Threads
   - Manage Messages (for reactions)
   - Read Message History
   - Add Reactions
9. Copy the generated URL at the bottom and open it in your browser to invite the bot to your server

### 2. Get Your Discord Channel ID

1. In Discord, go to **User Settings → Advanced** and enable **Developer Mode**
2. Right-click the channel where you want the bot to listen
3. Click **Copy Channel ID** — you'll need this as `CHANNEL_ID`

### 3. Set Up Google Gemini API

1. Go to [aistudio.google.com/apikey](https://aistudio.google.com/apikey)
2. Click **Create API Key**
3. Copy the key — you'll need this as `GEMINI_API_KEY`

### 4. Set Up Google Sheets

#### Create the Spreadsheet

1. Go to [sheets.google.com](https://sheets.google.com) and create a new spreadsheet
2. Name it something like "Music Business Expenses 2026"
3. Copy the spreadsheet ID from the URL:
   `https://docs.google.com/spreadsheets/d/`**THIS_PART**`/edit`
4. You'll need this as `SPREADSHEET_ID`

#### Create a Google Service Account

1. Go to [console.cloud.google.com](https://console.cloud.google.com)
2. Create a new project (or use an existing one)
3. Enable the **Google Sheets API**:
   - Go to **APIs & Services → Library**
   - Search for "Google Sheets API" and click **Enable**
4. Create a service account:
   - Go to **APIs & Services → Credentials**
   - Click **Create Credentials → Service Account**
   - Give it a name (e.g., "expense-tracker")
   - Click **Done** (no need to grant additional roles)
5. Create a key for the service account:
   - Click on the service account you just created
   - Go to the **Keys** tab
   - Click **Add Key → Create new key → JSON**
   - A JSON file will download — this contains your credentials
6. Share your Google Sheet with the service account:
   - Open the JSON file and find the `client_email` field (looks like `expense-tracker@your-project.iam.gserviceaccount.com`)
   - In your Google Sheet, click **Share** and add this email with **Editor** access

### 5. Deploy

#### Option A: Run Locally

```bash
# Clone the repo
git clone https://github.com/mtleszczynski/musician-biz.git
cd musician-biz

# Install dependencies
pip install -r requirements.txt

# Create your .env file
cp .env.example .env
# Edit .env with your actual values

# For local dev, set GOOGLE_CREDENTIALS_JSON to the file path:
# GOOGLE_CREDENTIALS_JSON=credentials.json
# (and put the downloaded JSON file in the project directory)

# Run the bot
python main.py
```

#### Option B: Deploy on Railway (recommended)

1. Go to [railway.app](https://railway.app) and sign in with GitHub
2. Click **New Project → Deploy from GitHub Repo**
3. Select the `mtleszczynski/musician-biz` repository
4. Go to the **Variables** tab and add:
   - `DISCORD_TOKEN` — your bot token
   - `CHANNEL_ID` — your channel ID
   - `GEMINI_API_KEY` — your Gemini API key
   - `SPREADSHEET_ID` — your spreadsheet ID
   - `GOOGLE_CREDENTIALS_JSON` — paste the **entire contents** of your service account JSON file
5. Railway will auto-deploy. Check the **Logs** tab to verify the bot starts successfully.

Future pushes to `main` will auto-deploy.

## Usage

Once the bot is running:

1. **Send a message** in your designated Discord channel with:
   - A photo of a receipt or invoice
   - A text description like "Bought new guitar strings for $12"
   - A voice message describing a payment
   - Any combination of the above!

2. **Review** the bot's extraction in the thread it creates

3. **Confirm** by replying "yes" in the thread, or correct any mistakes

4. The entry appears in your Google Sheet with a link back to the Discord thread

### Commands

| Command | Description |
|---------|-------------|
| `!summary` | Show this month's income & expense summary |
| `!summary 1 2026` | Show summary for January 2026 |
| `!undo` | Remove the last entry from the spreadsheet |
| `!categories` | List available categories |
| `!help` | Show help message |

## Cost

- **Discord**: Free
- **Gemini 3 Flash**: Has a free tier; paid tier is ~$0.50/1M input tokens — pennies/month for personal use
- **Google Sheets API**: Free
- **Railway**: ~$5/month (hobby plan)
