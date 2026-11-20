"""名单: the old bot's full member list of every tied group, kept for the day it is gone from the group."""

from __future__ import annotations

import asyncio
import time

from nonebot import logger

from ..idmap import GROUP_BASE
from ..state import background, driver, idmap, legacy_bots
from .presence import legacy_in_group

# ---------------------------------------------------------------- 名单: the old bot's member lists, kept

ROSTER_EVERY = 24 * 3600.0  # a kept list is renewed when it is this old
ROSTER_CHECK = 600.0  # how often to look for lists that are due (a newly tied group has none)
ROSTER_PAUSE = 5.0  # seconds between two groups


async def take_rosters(force: bool = False) -> int:
    """Keep the old bot's full member list of each tied group it is in; the number of lists taken."""
    taken = 0
    for row in idmap.groups():
        gid = row["gid"]
        if not row["linked"] or gid >= GROUP_BASE or (not force and time.time() - idmap.roster_taken(gid) < ROSTER_EVERY):
            continue
        old = legacy_bots()
        if not old or not await legacy_in_group(gid):
            continue
        try:
            listed = await old[0].get_group_member_list(group_id=gid, no_cache=True)
        except Exception as e:
            logger.warning(f"official bridge: member list of group {gid} not read ({type(e).__name__}); the kept one stays")
            continue
        people = [m for m in listed or [] if not m.get("is_robot") and str(m.get("user_id")) != old[0].self_id]
        kept = len(idmap.roster(gid))
        if not people or len(people) * 2 < kept:
            logger.warning(f"official bridge: member list of group {gid} came back short ({len(people)} against {kept} kept); the kept one stays")
            continue
        idmap.save_roster(gid, people)
        taken += 1
        logger.info(f"official bridge: kept the member list of group {gid} ({len(people)} members)")
        await asyncio.sleep(ROSTER_PAUSE)
    return taken


async def _keep_rosters() -> None:
    await asyncio.sleep(120)
    while True:
        try:
            await take_rosters()
        except Exception:
            logger.opt(exception=True).warning("official bridge: member lists not kept this time")
        await asyncio.sleep(ROSTER_CHECK)


@driver.on_startup
async def _start_rosters() -> None:
    background.add(task := asyncio.get_running_loop().create_task(_keep_rosters()))
    task.add_done_callback(background.discard)
