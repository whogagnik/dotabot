# -*- coding: utf-8 -*-
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Callable, Any
import logging

from scripts.host.core.steamid import get_steamid3

@dataclass
class Account:
    """
    Host-side account model.

    В новой архитектуре Account:
    - хранит данные аккаунта
    - хранит mafile
    - хранит статус
    - не запускает Steam/Dota локально
    - не ищет hwnd
    - не двигает окна
    - не содержит legacy box/window logic
    """

    username: str
    password: str
    logger: logging.Logger
    placer: Any = None
    status_cb: Optional[Callable[[str, str], None]] = None
    thread_registry: Any = None

    steamid3: Optional[int] = None
    steamid64: Optional[int] = None

    mafile_data: Optional[dict] = None
    mafile_path: Optional[str] = None
    steam_id: Optional[str] = None

    status: str = "idle"
    session_seconds: int = 0

    # ---------------------------------------------------------
    # state
    # ---------------------------------------------------------

    def set_status(self, s: str) -> None:
        self.status = str(s)
        try:
            if self.status_cb:
                self.status_cb(self.username, self.status)
        except Exception:
            pass

    # ---------------------------------------------------------
    # mafile
    # ---------------------------------------------------------

    def attach_mafile(self, path: str, data: dict) -> None:
        self.mafile_path = path
        self.mafile_data = data

        try:
            self.steam_id = (
                str(data.get("Session", {}).get("SteamID"))
                if isinstance(data, dict)
                else None
            )
        except Exception:
            self.steam_id = None

    @property
    def has_mafile(self) -> bool:
        return bool(self.mafile_path or self.mafile_data)

    # ---------------------------------------------------------
    # serialization / transport
    # ---------------------------------------------------------

    def to_assigned_payload(self) -> dict:
        return {
            "login": self.username,
            "password": self.password,
            "has_mafile": self.has_mafile,
            "mafile_path": self.mafile_path,
        }

    def to_state_dict(self) -> dict:
        return {
            "username": self.username,
            "password": self.password,
            "mafile_path": self.mafile_path,
            "mafile_data": self.mafile_data,
            "steam_id": self.steam_id,
            "status": self.status,
            "session_seconds": self.session_seconds,
        }

    @classmethod
    def from_state_dict(
        cls,
        data: dict,
        logger: logging.Logger,
        status_cb: Optional[Callable[[str, str], None]] = None,
    ) -> "Account":
        acc = cls(
            username=str(data["username"]),
            password=str(data["password"]),
            logger=logger,
            placer=None,
            status_cb=status_cb,
            thread_registry=None,
        )
        acc.mafile_path = data.get("mafile_path")
        acc.mafile_data = data.get("mafile_data")
        acc.steam_id = data.get("steam_id")
        acc.status = str(data.get("status", "idle"))
        acc.session_seconds = int(data.get("session_seconds", 0))
        return acc

    def get_steamid3(self) -> int:
        if self.steamid3 is None:
            self.steamid3 = get_steamid3(self.username, self.password)
            return self.steamid3
        return self.steamid3

