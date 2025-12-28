#!/usr/bin/env python3
"""
Gmail Integration Test Script

This script helps test the Gmail integration end-to-end using a real Gmail account.

Setup:
1. Create a test Gmail account
2. Enable Gmail API in Google Cloud Console
3. Create OAuth 2.0 credentials (desktop application)
4. Download credentials.json to this directory
5. Run this script to authenticate and test

Usage:
    python scripts/test_gmail_integration.py setup     # Initial OAuth setup
    python scripts/test_gmail_integration.py send      # Send test email
    python scripts/test_gmail_integration.py watch     # Set up Gmail watch
    python scripts/test_gmail_integration.py check     # Check for new messages
"""

import argparse
import base64
import pickle
import sys
from datetime import datetime
from email.mime.text import MIMEText
from pathlib import Path

# Google API imports
try:
    from google.auth.transport.requests import Request
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError
except ImportError:
    print("Please install Google API libraries:")
    print("uv add google-auth google-auth-oauthlib google-auth-httplib2 google-api-python-client")
    sys.exit(1)

# Gmail API scope
SCOPES = ["https://www.googleapis.com/auth/gmail.readonly", "https://www.googleapis.com/auth/gmail.send"]

# File paths
SCRIPT_DIR = Path(__file__).parent
TOKEN_FILE = SCRIPT_DIR / "token.pickle"
CREDENTIALS_FILE = SCRIPT_DIR / "credentials.json"


def get_gmail_service():
    """Get authenticated Gmail service."""
    creds = None

    # Load existing token
    if TOKEN_FILE.exists():
        with open(TOKEN_FILE, "rb") as token:
            creds = pickle.load(token)

    # If there are no (valid) credentials available, let the user log in
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not CREDENTIALS_FILE.exists():
                print(f"Error: {CREDENTIALS_FILE} not found!")
                print("Please download OAuth 2.0 credentials from Google Cloud Console")
                return None

            flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_FILE), SCOPES)
            creds = flow.run_local_server(port=0)

        # Save the credentials for the next run
        with open(TOKEN_FILE, "wb") as token:
            pickle.dump(creds, token)

    return build("gmail", "v1", credentials=creds)


def setup_oauth():
    """Initial OAuth setup."""
    print("Setting up Gmail OAuth...")
    service = get_gmail_service()
    if service:
        # Test by getting profile
        try:
            profile = service.users().getProfile(userId="me").execute()
            print(f"✓ Successfully authenticated as: {profile['emailAddress']}")
            print(f"✓ Token saved to: {TOKEN_FILE}")

            # Save email address for later use
            with open(SCRIPT_DIR / "test_email.txt", "w") as f:
                f.write(profile["emailAddress"])

            # Print refresh token for app testing
            with open(TOKEN_FILE, "rb") as token:
                creds = pickle.load(token)
                print("\nRefresh token for testing:")
                print(f"  {creds.refresh_token}")

            return profile["emailAddress"]
        except HttpError as error:
            print(f"✗ Error: {error}")
            return None
    else:
        print("✗ Failed to authenticate")
        return None


def send_test_email(subject=None, body=None):
    """Send a test email to self."""
    service = get_gmail_service()
    if not service:
        return None

    try:
        # Get user's email
        profile = service.users().getProfile(userId="me").execute()
        email_address = profile["emailAddress"]

        # Create message
        if not subject:
            subject = f"Test Email - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        if not body:
            body = (
                "This is a test email for Gmail webhook integration.\n\nTrigger phrase: ACTIVATE\n\nTimestamp: "
                + datetime.now().isoformat()
            )

        message = MIMEText(body)
        message["to"] = email_address
        message["from"] = email_address
        message["subject"] = subject

        # Encode message
        raw_message = base64.urlsafe_b64encode(message.as_bytes()).decode("utf-8")

        # Send message
        result = service.users().messages().send(userId="me", body={"raw": raw_message}).execute()

        print("✓ Test email sent successfully!")
        print(f"  To: {email_address}")
        print(f"  Subject: {subject}")
        print(f"  Message ID: {result['id']}")

        return result["id"]

    except HttpError as error:
        print(f"✗ Error sending email: {error}")
        return None


def setup_watch(webhook_url):
    """Set up Gmail watch for push notifications."""
    service = get_gmail_service()
    if not service:
        return None

    try:
        # Set up watch request
        request_body = {
            "labelIds": ["INBOX"],
            "topicName": "projects/your-project/topics/gmail-push",  # Update with your topic
            "labelFilterAction": "include",
        }

        # Start watch
        result = service.users().watch(userId="me", body=request_body).execute()

        print("✓ Watch set up successfully!")
        print(f"  History ID: {result.get('historyId')}")
        print(f"  Expiration: {result.get('expiration')}")

        return result

    except HttpError as error:
        print(f"✗ Error setting up watch: {error}")
        print("\nNote: For direct HTTPS webhooks (without Pub/Sub), you'll need to:")
        print("1. Use ngrok or a public URL")
        print("2. Configure the webhook URL in your app")
        return None


def check_messages(history_id=None):
    """Check for new messages since history_id."""
    service = get_gmail_service()
    if not service:
        return None

    try:
        # Get latest messages
        if history_id:
            # Get history since ID
            history = service.users().history().list(userId="me", startHistoryId=history_id).execute()

            print(f"History changes since {history_id}:")

            changes = history.get("history", [])
            if not changes:
                print("  No new messages")
            else:
                for record in changes:
                    for added in record.get("messagesAdded", []):
                        msg_id = added["message"]["id"]
                        msg = (
                            service.users()
                            .messages()
                            .get(userId="me", id=msg_id, format="metadata", metadataHeaders=["From", "Subject"])
                            .execute()
                        )

                        headers = {h["name"]: h["value"] for h in msg["payload"].get("headers", [])}
                        print(f"  - {msg_id}: {headers.get('Subject', 'No subject')}")
                        print(f"    From: {headers.get('From', 'Unknown')}")
        else:
            # Get recent messages
            results = service.users().messages().list(userId="me", maxResults=5, labelIds=["INBOX"]).execute()

            messages = results.get("messages", [])

            print("Recent messages:")
            for msg_data in messages:
                msg = (
                    service.users()
                    .messages()
                    .get(userId="me", id=msg_data["id"], format="metadata", metadataHeaders=["From", "Subject", "Date"])
                    .execute()
                )

                headers = {h["name"]: h["value"] for h in msg["payload"].get("headers", [])}
                print(f"  - {msg_data['id']}: {headers.get('Subject', 'No subject')}")
                print(f"    From: {headers.get('From', 'Unknown')}")
                print(f"    Date: {headers.get('Date', 'Unknown')}")
                print()

    except HttpError as error:
        print(f"✗ Error checking messages: {error}")
        return None


def main():
    parser = argparse.ArgumentParser(description="Gmail Integration Test Script")
    parser.add_argument("command", choices=["setup", "send", "watch", "check"], help="Command to run")
    parser.add_argument("--webhook-url", help="Webhook URL for watch setup")
    parser.add_argument("--history-id", help="History ID for checking changes")
    parser.add_argument("--subject", help="Email subject for send command")
    parser.add_argument("--body", help="Email body for send command")

    args = parser.parse_args()

    if args.command == "setup":
        setup_oauth()
    elif args.command == "send":
        send_test_email(args.subject, args.body)
    elif args.command == "watch":
        if not args.webhook_url:
            print("Error: --webhook-url required for watch command")
            print(
                "Example: python test_gmail_integration.py watch --webhook-url https://xyz.ngrok.io/api/email/webhook/google"
            )
        else:
            setup_watch(args.webhook_url)
    elif args.command == "check":
        check_messages(args.history_id)


if __name__ == "__main__":
    main()
