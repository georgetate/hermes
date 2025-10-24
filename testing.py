from agentos.adapters.google.gmail.writer import GmailWriter
from agentos.adapters.google.gmail.reader import GmailReader
from agentos.adapters.google.gmail.client import GmailClient, GmailClientConfig

gr = GmailReader(GmailClient(GmailClientConfig().from_settings_or_env()))
gw = GmailWriter(GmailClient(GmailClientConfig().from_settings_or_env()))

# print(type(gr.list_threads()))

print(gw.create_draft_new(gw._build_draft_new(to=["trilliont@gmail.com"], subject="Test", body_text="This is a test.")))