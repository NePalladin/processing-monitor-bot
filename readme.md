# Processing Monitoring Bot

An automated, containerized IMAP parser and Telegram alerting microservice built with Python. 

This service autonomously monitors specific email inboxes for critical system reports, parses raw textual data to extract operational metrics, and routes real-time alerts to Telegram. It was designed to ensure zero-downtime monitoring for financial processing and ETL batch jobs.

## Features
* **Automated IMAP Parsing:** Connects via SSL, searches for specific subjects, and extracts plain-text bodies.
* **Regex Data Extraction:** Identifies and extracts specific transactional metrics from raw text reports.
* **Multi-threaded Architecture:** Runs a continuous schedule loop (`schedule`) on a daemon thread while keeping the Telegram bot (`telebot`) responsive via long-polling.
* **State Management:** Tracks daily processed batches and expected deadlines in memory, resetting automatically at shift changes.
* **Container Ready:** Fully Dockerized for seamless deployment.

## Tech Stack
* **Language:** Python 3
* **Libraries:** `pyTelegramBotAPI`, `imaplib`, `schedule`, `python-dotenv`
* **Infrastructure:** Docker, Docker Compose

## Quick Start

1. **Clone the repository:**
   ```bash
   git clone [https://github.com/YOUR_USERNAME/processing-monitor-bot.git](https://github.com/YOUR_USERNAME/processing-monitor-bot.git)
   cd processing-monitor-bot
2. Configure environment variables:
   Copy the example environment file and fill in your credentials.
   cp .env.example .env
   Required variables: BOT_TOKEN, CHAT_ID, EMAIL_ACCOUNT, APP_PASSWORD, IMAP_SERVER
   
3.Configure the business logic:
   Adjust config.json with your specific parsing rules, target institutes, and schedules.
   
4. Deploy with Docker:
```bash
   docker build -t imap_bot .
   docker run -d --env-file .env --name processing_bot imap_bot

