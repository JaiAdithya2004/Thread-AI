"""Every connector implements this so adding sources is uniform."""
from abc import ABC, abstractmethod

class Connector(ABC):
    source = "base"
    @abstractmethod
    def backfill(self): ...          # -> iterable[Episode dict]   (full history once)
    @abstractmethod
    def poll(self): ...              # -> iterable[Episode dict]   (new items)
    @abstractmethod
    def normalize(self, raw): ...    # raw event -> Episode dict   (webhook path)

# Episode dict shape:
# {source, external_id, title, body, author, url, occurred_at, acl:[principals]}
