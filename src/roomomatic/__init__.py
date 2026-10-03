"""roomomatic: client library for lobbyd (identity + directory), roomsd (rooms) and
agentd (on-demand workers)."""

from roomomatic.agentd import TERMINAL_STATUSES, AgentdClient
from roomomatic.client import (
    AmbiguousSummon,
    Client,
    IncompatibleGateway,
    NoServerAvailable,
    Summoned,
    WorkerFailedToStart,
)
from roomomatic.http import ApiError, RoomomaticError, RoomRef, SessionRef
from roomomatic.lobby import Lobby
from roomomatic.peer import Assignment, PeerAgent
from roomomatic.rooms import NoteConflict, RoomsClient
from roomomatic.watcher import Delivery, Watcher

__all__ = [
    "AmbiguousSummon",
    "TERMINAL_STATUSES",
    "AgentdClient",
    "ApiError",
    "Client",
    "Lobby",
    "NoServerAvailable",
    "NoteConflict",
    "RoomRef",
    "RoomomaticError",
    "RoomsClient",
    "SessionRef",
    "Summoned",
    "IncompatibleGateway",
    "WorkerFailedToStart",
    "Assignment",
    "PeerAgent",
    "Delivery",
    "Watcher",
]
