from .sample import SampleConnector
from .slack import SlackConnector
from .gmail import GmailConnector
from .gdocs import GDocsConnector
from .calendar import CalendarConnector
from .notion import NotionConnector

REGISTRY = {
    "sample": SampleConnector,
    "slack": SlackConnector,
    "gmail": GmailConnector,
    "gdoc": GDocsConnector,
    "calendar": CalendarConnector,
    "notion": NotionConnector,
}
