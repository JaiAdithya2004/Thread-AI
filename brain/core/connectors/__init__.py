from .slack import SlackConnector
from .composio import ComposioConnector

REGISTRY = {
    "slack":    SlackConnector,
    "composio": ComposioConnector,
}
