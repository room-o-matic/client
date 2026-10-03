"""roomomatic: client library for lobbyd (identity + directory), roomsd (rooms) and
agentd (on-demand workers)."""

from roomomatic.agentd import TERMINAL_STATUSES, AgentdClient
from roomomatic.client import Client, NoServerAvailable, Summoned
from roomomatic.http import ApiError, RoomomaticError, RoomRef, SessionRef
from roomomatic.lobby import Lobby
from roomomatic.rooms import RoomsClient

__all__ = [
    "TERMINAL_STATUSES",
    "AgentdClient",
    "ApiError",
    "Client",
    "Lobby",
    "NoServerAvailable",
    "RoomRef",
    "RoomomaticError",
    "RoomsClient",
    "SessionRef",
    "Summoned",
]
