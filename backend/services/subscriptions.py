"""Subscription bookkeeping.

* Before any new subscription the remaining quota is read from query_subscription;
  if it is not enough the request is rejected and nothing is sent.
  Stocks: 1 quota per code per subtype. Options: 1 option quota per chain
  (same underlying + expiration) per subtype, from a separate pool.
* A subscription must stay active for min_subscription_hold_sec (60 s). Cancelling
  earlier schedules the unsubscribe automatically and reports the remaining wait.
* After a reconnect every subscription in the registry is verified and restored.
"""
from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import dataclass, field

from ..config import ConfigStore
from ..errors import UserError
from ..gateway.base import SUBTYPE_ZH, OpenDError
from ..opend_client import OpenDClient
from ..ratelimit import Priority
from .hub import Hub

log = logging.getLogger(__name__)

__all__ = ["SubscriptionManager", "SubEntry", "UserError"]


@dataclass
class SubEntry:
    key: str                 # stock code, or CHAIN|<underlying>|<expiry>
    subtype: str
    codes: list[str]
    is_option: bool
    subscribed_at: float
    unsub_at: float | None = None
    task: asyncio.Task | None = field(default=None, repr=False)


class SubscriptionManager:
    def __init__(self, client: OpenDClient, cfg: ConfigStore, hub: Hub):
        self.client = client
        self.cfg = cfg
        self.hub = hub
        self.entries: dict[tuple[str, str], SubEntry] = {}
        self.quota: dict = {}
        self._lock = asyncio.Lock()

    @property
    def hold_sec(self) -> float:
        return float(self.cfg.get("quota", "min_subscription_hold_sec", default=60))

    def _session(self, is_option: bool) -> str | None:
        return None if is_option else (self.cfg.get("stocks", "subscribe_session", default="ALL") or None)

    async def refresh_quota(self) -> dict:
        try:
            self.quota = await self.client.query_subscription()
        except OpenDError as e:
            log.warning("query_subscription failed: %s", e)
            raise
        self.hub.publish("quota", self.quota_view())
        return self.quota

    def quota_view(self) -> dict:
        q = self.quota or {}
        used, remain = q.get("total_used"), q.get("remain")
        oused, oremain = q.get("option_used_quota"), q.get("option_remain_quota")
        return {"stock_used": used, "stock_remain": remain,
                "stock_total": (used + remain) if used is not None and remain is not None else None,
                "option_used": oused, "option_remain": oremain,
                "option_total": (oused + oremain) if oused is not None and oremain is not None else None}

    # ------------------------------------------------------------ subscribe
    async def subscribe(self, key: str, codes: list[str], subtypes: list[str], is_option: bool) -> list[str]:
        msgs: list[str] = []
        async with self._lock:
            for st in subtypes:
                e = self.entries.get((key, st))
                if e and e.unsub_at:
                    if e.task:
                        e.task.cancel()
                    e.unsub_at, e.task = None, None
                    msgs.append(f"{SUBTYPE_ZH.get(st, st)}：已取消预定的退订")
            need = [st for st in subtypes if (key, st) not in self.entries]
            if not need:
                self._publish()
                return msgs
            try:
                q = await self.refresh_quota()
            except OpenDError as e:
                raise UserError(f"无法查询订阅额度，未发送订阅请求：{e.msg}") from None
            if is_option:
                remain, required, pool = q.get("option_remain_quota"), len(need), "期权"
            else:
                remain, required, pool = q.get("remain"), len(need) * len(codes), "股票"
            if remain is None:
                raise UserError(f"OpenD 未返回{pool}剩余订阅额度，未发送订阅请求")
            if remain < required:
                names = "、".join(SUBTYPE_ZH.get(s, s) for s in need)
                raise UserError(f"{pool}订阅额度不足：订阅 {names} 需要 {required} 个额度，当前剩余 {remain} 个。"
                                f"请先取消其他订阅后再试。")
            try:
                await self.client.subscribe(codes, need, self._session(is_option))
            except OpenDError as e:
                raise UserError(f"订阅失败：{e.msg}") from None
            now = time.time()
            for st in need:
                self.entries[(key, st)] = SubEntry(key, st, list(codes), is_option, now)
            log.info("subscribed %s %s (%d codes)", key, need, len(codes))
        try:
            await self.refresh_quota()
        except OpenDError:
            pass
        self._publish()
        return msgs

    async def add_codes(self, key: str, new_codes: list[str]) -> None:
        """Extend an existing chain subscription with newly listed contracts (no extra quota)."""
        async with self._lock:
            for (k, st), e in self.entries.items():
                if k == key:
                    extra = [c for c in new_codes if c not in e.codes]
                    if extra:
                        await self.client.subscribe(extra, [st], self._session(e.is_option))
                        e.codes.extend(extra)

    # ---------------------------------------------------------- unsubscribe
    async def unsubscribe(self, key: str, subtypes: list[str]) -> list[str]:
        msgs: list[str] = []
        async with self._lock:
            now = time.time()
            for st in subtypes:
                e = self.entries.get((key, st))
                if e is None or e.unsub_at:
                    continue
                age = now - e.subscribed_at
                if age >= self.hold_sec:
                    try:
                        await self.client.unsubscribe(e.codes, [st])
                        del self.entries[(key, st)]
                        msgs.append(f"{SUBTYPE_ZH.get(st, st)}：已退订")
                    except OpenDError as err:
                        msgs.append(f"{SUBTYPE_ZH.get(st, st)}：退订失败（{err.msg}），5 秒后重试")
                        self._schedule(e, 5)
                else:
                    wait = self.hold_sec - age + 1
                    self._schedule(e, wait)
                    msgs.append(f"{SUBTYPE_ZH.get(st, st)}：订阅需保持至少 1 分钟，还需等待 {math.ceil(wait)} 秒，"
                                f"到期后将自动退订")
        try:
            await self.refresh_quota()
        except OpenDError:
            pass
        self._publish()
        return msgs

    def _schedule(self, e: SubEntry, wait: float) -> None:
        e.unsub_at = time.time() + wait
        if e.task:
            e.task.cancel()
        e.task = asyncio.create_task(self._delayed_unsub(e, wait))

    async def _delayed_unsub(self, e: SubEntry, wait: float) -> None:
        await asyncio.sleep(wait)
        async with self._lock:
            if self.entries.get((e.key, e.subtype)) is not e or not e.unsub_at:
                return
            try:
                await self.client.unsubscribe(e.codes, [e.subtype])
                del self.entries[(e.key, e.subtype)]
                log.info("scheduled unsubscribe done: %s %s", e.key, e.subtype)
                self.hub.publish("toast", {"level": "info",
                                           "text": f"{e.key.replace('CHAIN|', '')} {SUBTYPE_ZH.get(e.subtype, e.subtype)} 已自动退订"})
            except OpenDError as err:
                log.warning("scheduled unsubscribe failed, retrying in 5 s: %s", err)
                e.task = None
                self._schedule(e, 5)
                return
        try:
            await self.refresh_quota()
        except OpenDError:
            pass
        self._publish()

    # -------------------------------------------------------------- restore
    async def restore(self) -> None:
        """After (re)connect: make sure every registered subscription exists in OpenD."""
        async with self._lock:
            try:
                q = await self.client.query_subscription(priority=Priority.NORMAL)
                self.quota = q
            except OpenDError as e:
                log.warning("restore: query_subscription failed: %s", e)
                return
            sub_list = q.get("sub_list") or {}
            restored = 0
            for (key, st), e in list(self.entries.items()):
                if e.unsub_at:  # it was going away anyway
                    if e.task:
                        e.task.cancel()
                    del self.entries[(key, st)]
                    continue
                have = set(sub_list.get(st, []) or [])
                missing = [c for c in e.codes if c not in have]
                if not missing:
                    continue
                try:
                    await self.client.subscribe(missing, [st], self._session(e.is_option))
                    e.subscribed_at = time.time()
                    restored += 1
                except OpenDError as err:
                    log.error("restore subscribe failed %s %s: %s", key, st, err)
                    self.hub.publish("toast", {"level": "error",
                                               "text": f"恢复订阅失败：{key.replace('CHAIN|', '')} "
                                                       f"{SUBTYPE_ZH.get(st, st)}（{err.msg}）"})
            if restored:
                log.info("restored %d subscriptions after reconnect", restored)
                self.hub.publish("toast", {"level": "info", "text": f"OpenD 已重连，已恢复 {restored} 项订阅"})
        try:
            await self.refresh_quota()
        except OpenDError:
            pass
        self._publish()

    async def shutdown(self) -> None:
        """Release quota: unsubscribe everything that is past the 1 minute hold; younger
        subscriptions are released by OpenD automatically after the connection closes."""
        for e in self.entries.values():
            if e.task:
                e.task.cancel()
        now = time.time()
        by_type: dict[str, list[str]] = {}
        young = 0
        for e in self.entries.values():
            if now - e.subscribed_at >= self.hold_sec:
                by_type.setdefault(e.subtype, []).extend(e.codes)
            else:
                young += 1
        for st, codes in by_type.items():
            try:
                await asyncio.wait_for(self.client.unsubscribe(codes, [st]), timeout=5)
            except Exception as err:  # noqa: BLE001 - best effort on shutdown
                log.warning("shutdown unsubscribe %s failed: %s", st, err)
        log.info("shutdown: released %d subtype groups; %d young subscriptions auto-release after close",
                 len(by_type), young)
        self.entries.clear()

    # ---------------------------------------------------------------- views
    def state(self) -> dict[str, dict[str, dict]]:
        now = time.time()
        out: dict[str, dict[str, dict]] = {}
        for (key, st), e in self.entries.items():
            out.setdefault(key, {})[st] = {
                "since": e.subscribed_at,
                "unsub_in": max(0, math.ceil(e.unsub_at - now)) if e.unsub_at else None,
                "hold_left": max(0, math.ceil(self.hold_sec - (now - e.subscribed_at))),
            }
        return out

    def active(self, key: str, subtype: str) -> bool:
        e = self.entries.get((key, subtype))
        return e is not None and e.unsub_at is None

    def _publish(self) -> None:
        self.hub.publish("subs", self.state())
