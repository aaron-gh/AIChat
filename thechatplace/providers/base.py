"""The discovery boundary: plain data, independent of the window."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Protocol, Set

from ..desktop_groups import DesktopGroups
from ..sessions import LiveStatus, SessionInfo


@dataclass
class Snapshot:
    sessions: List[SessionInfo] = field(default_factory=list)
    # Claude compatibility data used by existing ownership guards. Other
    # providers must not put their native IDs in these sets or live mapping.
    desktop_cli_ids: Set[str] = field(default_factory=set)
    cowork_cli_ids: Set[str] = field(default_factory=set)
    live: Dict[str, LiveStatus] = field(default_factory=dict)
    unreadable_files: int = 0
    desktop_groups: DesktopGroups = field(default_factory=DesktopGroups)


class SessionProvider(Protocol):
    provider_id: str

    def collect(self) -> Snapshot:
        """Return discovery results without starting or resuming a turn."""
        ...
