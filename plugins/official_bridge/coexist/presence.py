"""Which groups the old bot is in, and which of the two bots answers a member there."""

from __future__ import annotations

import time

from nonebot import logger

from ..bridge import Bridge
from ..state import online, config, idmap, legacy_bots

# ---------------------------------------------------------------- which bot answers a group

legacy_group_cache: dict[str, tuple[float, set[int]]] = {}
_final: set[int] = {row["gid"] for row in idmap.groups(active_only=False) if idmap.roster_final(row["gid"])}  # groups whose kept member list is final
LEFT_HOLD = 120.0  # seconds the old client may go on listing a group its account was just taken out of
legacy_left: dict[int, float] = {}


def legacy_seen_in(bot_id: str, gid: int) -> None:
    """The old bot has just heard or joined this group: it is in it, whatever the last list said."""
    legacy_left.pop(gid, None)
    cached = legacy_group_cache.get(bot_id)
    if cached is not None:
        cached[1].add(gid)
    if gid in _final:
        _final.discard(gid)
        idmap.set_roster_final(gid, False)  # its member list of the group is a living one again


def legacy_gone_from(gid: int) -> None:
    legacy_left[gid] = time.monotonic()
    for _, groups in legacy_group_cache.values():
        groups.discard(gid)
    if served(gid) and idmap.roster(gid):
        # From here on the kept member list is all that will ever be known of who was in the
        # group besides the members the official bot sees: it stands as the record.
        _final.add(gid)
        idmap.set_roster_final(gid, True)
        logger.info(f"official bridge: the old bot is out of group {gid}; its last member list there ({len(idmap.roster(gid))} members) is kept as the record")


def served(gid: int) -> bool:
    """A group of the old bot's that the official bot is in and has taken over."""
    row = idmap.group(gid)
    return row is not None and bool(row["linked"] and row["active"])


# Since when the old bot has been in no group at all. From then on nobody is recognised by the two
# bots hearing the same message any more, and a member bound through the old bot has to come back
# on their own (plugins/skland_health keeps their binding for a while, see its dormant.py).
# One empty list is not believed: a client that has just started lists nothing for a moment.
GONE_CONFIRM = 600.0  # seconds between two empty lists before the old bot counts as gone
_empty_since: float | None = None


def note_legacy_groups(groups: set[int]) -> None:
    """The old bot has just said which groups it is in."""
    global _empty_since
    if groups:
        _empty_since = None
        if legacy_left_for_good():
            idmap.set_meta("legacy_groups", "")  # no longer known to be in none; which ones is noted when they are next listed in full
        if idmap.meta("legacy_gone_at"):
            idmap.set_meta("legacy_gone_at", "")
            logger.info("official bridge: the old bot is in a group again; members bound through it are no longer counted as waiting")
        return
    now = time.time()
    if _empty_since is None:
        _empty_since = now
    elif now - _empty_since >= GONE_CONFIRM and not idmap.meta("legacy_gone_at"):
        idmap.set_meta("legacy_gone_at", str(now))
        # Its last known group list is the empty one from here on: should the account never
        # connect again (closed for good), "which groups is the old bot in" stays answerable.
        idmap.set_meta("legacy_groups", "[]")
        logger.info("official bridge: the old bot is in no group any more; from now on members bound through it have to be recognised by their own doing")


def legacy_left_for_good() -> bool:
    """The old bot was in no group at all when its groups were last listed.

    It then has nothing to come back for: its groups can be answered for without it, and nothing
    is to be held back for it after a restart.
    """
    return idmap.meta("legacy_groups") == "[]"


def legacy_gone_at() -> float | None:
    """When the old bot was found to be in no group at all; None while it is in one, or was never seen to leave them all."""
    value = idmap.meta("legacy_gone_at")
    return float(value) if value else None


async def legacy_in_group(gid: int) -> bool:
    if time.monotonic() - legacy_left.get(gid, float("-inf")) < LEFT_HOLD:
        return False
    for bot in legacy_bots():
        cached = legacy_group_cache.get(bot.self_id)
        if cached is None or time.monotonic() - cached[0] > 60:
            try:
                listed = await bot.get_group_list()
                groups = {int(g["group_id"]) for g in listed}
                for g in listed:  # keep the names the old bot has for its groups
                    row = idmap.group(int(g["group_id"]))
                    if row is not None and row["linked"] and g.get("group_name") and row["name"] != g["group_name"]:
                        idmap.note_group(row["openid"], name=str(g["group_name"]))
                note_legacy_groups(groups)
            except Exception:
                groups = cached[1] if cached else set()
            cached = legacy_group_cache[bot.self_id] = (time.monotonic(), groups)
        if gid in cached[1]:
            return True
    return False


async def in_shadow(bridge: Bridge, gid: int, member_openid: str, sent_at: int) -> bool:
    """True while the old bot is the one that answers this member in this group.

    Both bots decide from the message's own time against the moment the member was tied to
    their QQ number, so a message arriving while that happens is still answered exactly once.
    """
    if not legacy_bots():
        return False
    row = bridge.idmap.group(gid)
    if row is None or not row["linked"]:
        return config.official_bridge_shadow_unlinked
    if config.official_bridge_takeover == "all" or not await legacy_in_group(gid):
        return False
    since = bridge.idmap.linked_at(member_openid)
    return since is None or sent_at < since


def official_answers(gid: int, user_id: int, sent_at: int) -> bool:
    """The other half of in_shadow, asked by the old bot: is this member's message the official bot's?"""
    if not online:
        return False
    row = idmap.group(gid)
    if row is None or not (row["linked"] and row["active"] and row["hears_all"]):
        return False
    if config.official_bridge_takeover == "all":
        return True
    member = idmap.user(user_id)
    return bool(member and member["linked"] and sent_at >= member["linked_at"])
