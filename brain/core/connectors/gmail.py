import os
from .base import Connector

class GmailConnector(Connector):
    source = "gmail"
    def __init__(self): self.creds = os.getenv("GOOGLE_CREDENTIALS_JSON")
    def backfill(self):
        # TODO: google-api-python-client, users().messages().list, filter promo/social,
        # body -> Episode, acl=[owner email]
        return []
    def poll(self): return []
    def normalize(self, raw):
        return {"source":"gmail","external_id":raw.get("id"),
                "title":raw.get("subject",""), "body":raw.get("body",""),
                "author":raw.get("from",""), "url":"",
                "occurred_at":raw.get("date",""), "acl":[raw.get("owner","everyone")]}
