import os
from .base import Connector

class NotionConnector(Connector):
    source = "notion"
    def __init__(self): self.token = os.getenv("NOTION_TOKEN")
    def backfill(self):  # TODO notion-client search + block children -> Episode
        return []
    def poll(self): return []
    def normalize(self, raw):
        return {"source":"notion","external_id":raw.get("id"),
                "title":raw.get("title",""), "body":raw.get("body",""),
                "author":raw.get("author",""), "url":raw.get("url",""),
                "occurred_at":raw.get("edited",""), "acl":raw.get("acl",["everyone"])}
