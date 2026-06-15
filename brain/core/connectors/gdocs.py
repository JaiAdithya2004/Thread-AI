import os
from .base import Connector

class GDocsConnector(Connector):
    source = "gdoc"
    def __init__(self): self.creds = os.getenv("GOOGLE_CREDENTIALS_JSON")
    def backfill(self):  # TODO Drive list + Docs export -> Episode
        return []
    def poll(self): return []
    def normalize(self, raw):
        return {"source":"gdoc","external_id":raw.get("id"),
                "title":raw.get("title",""), "body":raw.get("body",""),
                "author":raw.get("owner",""), "url":raw.get("url",""),
                "occurred_at":raw.get("modified",""), "acl":raw.get("acl",["everyone"])}
