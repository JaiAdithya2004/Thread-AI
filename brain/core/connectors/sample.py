import json
from pathlib import Path
from .base import Connector

class SampleConnector(Connector):
    """Offline connector that loads sample_data/*.json so the brain runs with
    no real credentials. Great for the first demo."""
    source = "sample"
    def _load(self):
        root = Path(__file__).resolve().parents[3] / "sample_data"
        items = []
        for fp in root.glob("*.json"):
            items += json.loads(fp.read_text())
        return items
    def backfill(self): return self._load()
    def poll(self): return []
    def normalize(self, raw): return raw
