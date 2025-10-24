from __future__ import print_function
import os.path
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

# If modifying scopes, delete the token.json file
SCOPES = [
    'https://www.googleapis.com/auth/gmail.readonly',
    'https://www.googleapis.com/auth/calendar.readonly'
]

def main():
    creds = None
    token_path = os.path.join(".credentials", "token.json")
    creds_path = os.path.join(".credentials", "credentials.json")

    # Load saved credentials if they exist
    if os.path.exists(token_path):
        creds = Credentials.from_authorized_user_file(token_path, SCOPES)

    # If no valid creds, go through OAuth flow
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(creds_path, SCOPES)
            creds = flow.run_local_server(port=0)  # Opens browser for login
        # Save credentials for next run
        with open(token_path, 'w') as token:
            token.write(creds.to_json())

    print("Authentication successful!")

if __name__ == '__main__':
    main()
