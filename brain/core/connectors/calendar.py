import os
from .base import Connector

class CalendarConnector(Connector):
    source = "calendar"
    def __init__(self): self.creds = os.getenv("GOOGLE_CREDENTIALS_JSON")
    def backfill(self):  # TODO events().list -> Episode (title+notes+attendees)
        return []
    def poll(self): return []
    def normalize(self, raw):
        return {"source":"calendar","external_id":raw.get("id"),
                "title":raw.get("summary",""), "body":raw.get("description",""),
                "author":raw.get("organizer",""), "url":raw.get("htmlLink",""),
                "occurred_at":raw.get("start",""),
                "acl":raw.get("attendees",["everyone"])}
