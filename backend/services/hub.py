"""WebSocket fan-out. Quote updates are coalesced and flushed every 250 ms."""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from fastapi import WebSocket

log = logging.getLogger(__name__)


class _Client:
    def __init__(self, ws: WebSocket):
        self.ws = ws
        self.queue: asyncio.Queue[str] = asyncio.Queue(maxsize=2000)
        self.task: asyncio.Task | None = None


class Hub:
    FLUSH_SEC = 0.25

    def __init__(self) -> None:
        self._clients: set[_Client] = set()
        self._pending_quotes: dict[str, dict] = {}
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._flush_loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
        for c in list(self._clients):
            if c.task:
                c.task.cancel()

    def publish(self, kind: str, data: Any) -> None:
        if not self._clients:
            return
        text = json.dumps({"type": kind, "data": data}, ensure_ascii=False, default=str)
        for c in list(self._clients):
            try:
                c.queue.put_nowait(text)
            except asyncio.QueueFull:
                log.warning("websocket client too slow, dropping message %s", kind)

    def quote(self, code: str, record: dict) -> None:
        self._pending_quotes[code] = record

    async def _flush_loop(self) -> None:
        while True:
            await asyncio.sleep(self.FLUSH_SEC)
            if self._pending_quotes:
                batch, self._pending_quotes = self._pending_quotes, {}
                self.publish("quotes", batch)

    async def serve(self, ws: WebSocket) -> None:
        client = _Client(ws)
        self._clients.add(client)

        async def sender() -> None:
            while True:
                text = await client.queue.get()
                await ws.send_text(text)

        client.task = asyncio.create_task(sender())
        try:
            while True:
                await ws.receive_text()  # client pings; content ignored
        except Exception:
            pass
        finally:
            client.task.cancel()
            self._clients.discard(client)
