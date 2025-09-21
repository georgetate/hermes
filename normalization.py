from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials
from datetime import datetime

creds = Credentials.from_authorized_user_file('/home/george/trillion-agentos/.credentials/token.json') # use same scope as token to avoid mismatch

gmail = build('gmail', 'v1', credentials=creds)
cal = build('calendar', 'v3', credentials=creds)

# email drills
# drill 1
def gmail_list_unread_ids(service, max_results=5, label_ids=('INBOX', 'UNREAD')):
    resp = service.users().messages().list(
        userId='me', labelIds=list(label_ids), maxResults=max_results
    ).execute()

    ids = [msg['id'] for msg in resp.get('messages', [])]
    
    return ids, resp.get('nextPageToken')

# drill 2
def gmail_get_envelope(service, msg_id, headers=('FROM', 'SUBJECT', 'DATE')):
    msg = service.users().messages().get(
        userId='me', id=msg_id, format='metadata', metadataHeaders=list(headers)
    ).execute()

    h = {k['name'].lower(): k['value'] for k in msg['payload']['headers']}
    
    return {
        'id': msg['id'],
        'from': h.get('from'),
        'subject': h.get('subject'),
        'date_raw': h.get('date')
    }


# drill 3
from datetime import timezone
from email.utils import parseaddr, parsedate_to_datetime

def normalize_envelope(env: dict, tz_hint="America/Denver"):
    name, email = parseaddr(env['from'] or '')
    dt_utc_iso = dt_local_iso = None

    try:
        dt = parsedate_to_datetime(env['date_raw'])
        dt_uct_iso = dt.astimezone(timezone.utc).isoformat()
        dt_local_iso = dt.astimezone().isoformat() # system local timezone
    except:
        pass

    return {
        'id': env.get('id'),
        'from_name': name or None,
        'from_email': email or None,
        'subject': env.get('subject'),
        'date_utc_iso': dt_utc_iso,
        'date_local_iso': dt_local_iso
    }

# drill 4
def gmail_get_meta(service, msg_id):
    msg = service.users().messages().get(
        userId ='me', id=msg_id, format='metadata'
    ).execute()

    return {'snippet': msg.get('snippet', []), 'labelIds': msg.get('labelIds', [])}

# drill 5
def gmail_normalize_unread(service, max_results=5):
    ids, _ = gmail_list_unread_ids(service, max_results=max_results, label_ids=('INBOX', 'UNREAD'))
    out = []
    for mid in ids:
        env = gmail_get_envelope(service, mid, headers=('FROM', 'SUBJECT', 'DATE'))
        norm = normalize_envelope(env)
        meta = gmail_get_meta(service, mid)
        norm.update(meta)
        out.append(norm)
    return out

# calendar drills
# drill 1
def cal_list_upcoming(service, calendar_id='primary', n=5):
    now = datetime.now(timezone.utc).isoformat()
    resp = service.events().list(
        calendarId=calendar_id, timeMin=now,
        singleEvents=True, orderBy='startTime', maxResults=n
    ).execute()
    return resp.get('items', [])

def normalize_event(e: dict):
    start = e.get('start', [])
    end = e.get('end', [])
    is_all_day = 'date' in start and 'dateTime' not in start
    start_iso = start.get('dateTime') or start.get('date')
    end_iso   = end.get('dateTime')   or end.get('date')
    
    return {
        'summary': e.get('summary'),
        'location': e.get('location'),
        'start_iso': start_iso,
        'end_iso': end_iso,
        'is_all_day': bool(is_all_day)
    }

def calendar_normalize_upcoming(service, n=5):
    return [normalize_event(e) for e in cal_list_upcoming(service, n=n)]


def print_gmail(items):
    print(f"--- Unread ({len(items)}) ---")
    for it in items:
        ts = it.get("date_utc_iso") or it.get("date_local_iso") or ""
        who = f'{it.get("from_name") or ""} <{it.get("from_email") or ""}>'.strip()
        print(f"[{ts}] {it.get('subject') or '(no subject)'} — {who}")

def print_calendar(items):
    print(f"\n--- Upcoming ({len(items)}) ---")
    for e in items:
        when = e["start_iso"]
        title = e.get("summary") or "(no title)"
        loc = f" ({e['location']})" if e.get("location") else ""
        print(f"{when}  {title}{loc}")


gmail_items = gmail_normalize_unread(gmail, max_results=5)
cal_items   = calendar_normalize_upcoming(cal, n=5)
print_gmail(gmail_items)
print_calendar(cal_items)
