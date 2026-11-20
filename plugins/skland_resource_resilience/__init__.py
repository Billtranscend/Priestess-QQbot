"""Skland 资源数据的两处补丁：GitHub 文件列表请求的重试和缓存；启动时不等网络检查。

文件列表：上游下载图片资源时，每个资源路径都要拉一次完整的递归 GitHub 目录树（好几 MB），
用的是 httpx 默认的 5 秒超时、没有重试，所以偶尔会报 ``获取文件列表失败: ReadTimeout``。
这条路径只有 ``/skland sync`` 和启动时会走；而启动时的资源下载只在 check_res_update 打开时运行
（默认 False），所以这个补丁主要对 ``/skland sync`` 起作用。
这里给 ``GameResourceDownloader.fetch_file_list`` 套上更长的超时、最多三次尝试，
以及按 URL 的短时目录树缓存，免得每个路径都把同一份列表重新下载一遍。

启动：上游的启动钩子依次检查方舟数据、终末地卡池表和终末地图标（要抓官方页面），全部做完前机器人
连不上 QQ，而结果几乎总是“已是最新”。这里只换掉启动钩子里的那次调用：先只读这三项在磁盘上的缓存，
等启动完成（ready）后再在后台跑同一个检查，三项仍按原来的顺序（图标检查要用卡池表）。
缓存缺文件或读不出来（全新部署）时，这次启动照旧先等网络检查。
每日更新和 /资源更新 用的是上游各自的引用，不受影响。

上游挪了模块、版本或源码哈希对不上时，对应的补丁跳过并给出警告，不会卡住启动。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from importlib.metadata import version
from pathlib import Path
from types import ModuleType
from typing import Any

from httpx import AsyncClient, HTTPError, Timeout
from nonebot import get_driver, logger, require
from nonebot.plugin import PluginMetadata

require("nonebot_plugin_skland")

try:
    from nonebot_plugin_skland import data_source as skland_data
    from nonebot_plugin_skland import download as skland_download
    from nonebot_plugin_skland import hook as skland_hook
    from nonebot_plugin_skland.config import GACHA_DATA_PATH
    from nonebot_plugin_skland.download import File, GameResourceDownloader
    from nonebot_plugin_skland.exception import RequestException, ResourceUpdateInProgress
    from nonebot_plugin_skland.services import resources as skland_resources
except ImportError as e:  # 上游挪了模块：两处补丁都跳过，不让启动失败
    _import_error: ImportError | None = e
else:
    _import_error = None

__plugin_meta__ = PluginMetadata(
    name="Skland resource resilience",
    description="Skland GitHub 文件列表加长超时、重试和缓存；启动时先用本地数据，网络检查在启动完成后进行。",
    usage="无指令，自动生效。",
    type="application",
)

_EXPECTED_VERSION = "0.7.4"
_EXPECTED_SHA256 = {
    "nonebot_plugin_skland.download": "d80bdb4a8fe9b26259b93817296d7be7508207b2d13aa8058e7f776590e0be74",
    "nonebot_plugin_skland.hook": "1e1bcd23252c90f8b215d088c4fde0b44bde7fe6f5daca06f00be3e51b5cb703",
    "nonebot_plugin_skland.services.resources": "fee7449a65c426d63b91506964293102d9d68649dffefc256603977486a0dca7",
    "nonebot_plugin_skland.data_source": "9f276d0df70e504d7422959e54297fb2772a991b568628315420672cd01ab637",
}
_TIMEOUT = Timeout(60.0, connect=15.0)
_ATTEMPTS = 3
_BACKOFF_SECONDS = (3, 10)
_CACHE_TTL_SECONDS = 600

_tree_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_tree_locks: dict[str, asyncio.Lock] = {}

_CACHE_ERRORS = (OSError, ValueError, TypeError, KeyError, AttributeError)  # 和上游读缓存时接的一样
_on_ready = getattr(getattr(get_driver(), "_lifespan", None), "on_ready", None)  # driver 没有公开的 on_ready
_pending: dict[str, bool] | None = None  # 推迟到启动完成后的那次检查的参数
_tasks: set[asyncio.Task] = set()


def _upstream_matches(patch: str, *modules: ModuleType) -> bool:
    installed = version("nonebot-plugin-skland")
    if installed != _EXPECTED_VERSION:
        logger.warning(f"Skland {installed} is not {_EXPECTED_VERSION}; {patch} patch skipped")
        return False
    for module in modules:
        path = Path(module.__file__)
        if hashlib.sha256(path.read_bytes()).hexdigest() != _EXPECTED_SHA256[module.__name__]:
            logger.warning(f"Skland {path.name} hash changed; {patch} patch skipped")
            return False
    return True


async def _fetch_tree(url: str) -> dict[str, Any]:
    lock = _tree_locks.setdefault(url, asyncio.Lock())
    async with lock:
        cached = _tree_cache.get(url)
        if cached and time.monotonic() - cached[0] < _CACHE_TTL_SECONDS:
            return cached[1]

        from nonebot_plugin_skland.config import config

        headers = {"Authorization": f"{config.github_token}"} if config.github_token else {}
        last_error: HTTPError | None = None
        for attempt in range(_ATTEMPTS):
            try:
                async with AsyncClient(timeout=_TIMEOUT) as client:
                    response = await client.get(url, headers=headers)
                    response.raise_for_status()
                    data = response.json()
                _tree_cache[url] = (time.monotonic(), data)
                return data
            except HTTPError as e:
                last_error = e
                if attempt < _ATTEMPTS - 1:
                    delay = _BACKOFF_SECONDS[attempt]
                    logger.warning(
                        f"Skland file list attempt {attempt + 1}/{_ATTEMPTS} failed "
                        f"({type(e).__name__}); retrying in {delay}s"
                    )
                    await asyncio.sleep(delay)
        raise RequestException(f"获取文件列表失败: {type(last_error).__name__}: {last_error}")


async def fetch_file_list(cls, url: str, dl_url: str, route: str) -> list[File]:
    data = await _fetch_tree(url)
    if "." in route.split("/")[-1]:

        def path_filter(path: str) -> bool:
            return path == route

    else:
        dir_route = route.rstrip("/") + "/"

        def path_filter(path: str) -> bool:
            return path.startswith(dir_route)

    return [
        File(name=item["path"].split("/")[-1], download_url=f"{dl_url}{item['path']}")
        for item in data.get("tree", [])
        if item["type"] == "blob" and path_filter(item["path"])
    ]


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _load_local_data() -> None:
    """把三项检查在磁盘上的缓存读进内存；缺文件或读不出来时抛异常，内存里什么都不改。"""
    ark, pools, icons = skland_data.gacha_table_data, skland_data.ef_gacha_pool_data, skland_data.ef_item_icon_data
    pool_table = pools._parse(_read_json(pools._file_path))
    icon_table = icons._parse(_read_json(icons._file_path))
    data_version = ark.version_file.read_text(encoding="utf-8").strip()
    if not data_version:
        raise ValueError("empty Arknights data version")
    metadata = ark._load_operator_metadata()
    if metadata is None:
        raise FileNotFoundError("operator metadata")
    tables = ark._load_operator_tables()
    tables["gacha_table.json"] = _read_json(GACHA_DATA_PATH / "gacha_table.json")
    state = ark._parse_tables(tables, metadata)

    ark.character_table, ark.gacha_table, ark.operator_catalog = state
    ark.version, ark._metadata_snapshot = data_version, metadata
    ark._load_gacha_details()  # 卡池详情上游本来就可有可无：有缓存就用，没有等后台检查去取
    pools.pool_table = pool_table
    icons.icons, icons._cache_loaded = icon_table, True


async def _startup_update(*, force: bool = False, refresh_metadata: bool = True) -> skland_resources.ResourceUpdateResult:
    """启动钩子里的 update_data_resources：缓存齐全就只读缓存，网络检查推迟到启动完成后。"""
    global _pending
    try:
        _load_local_data()
    except _CACHE_ERRORS as e:
        logger.info(f"Skland local data incomplete ({type(e).__name__}); checking resources before startup")
        return await skland_resources.update_data_resources(force=force, refresh_metadata=refresh_metadata)
    _pending = {"force": force, "refresh_metadata": refresh_metadata}
    logger.info("Skland data loaded from local cache; resource check runs after startup")
    return skland_resources.ResourceUpdateResult(messages=(), failed=False)


async def _check(**options: bool) -> None:
    try:
        await skland_resources.update_data_resources(**options)
    except ResourceUpdateInProgress:
        logger.info("Skland startup resource check skipped: an update is already running")
    except Exception as e:  # 后台任务的异常没人接：记下来
        logger.warning(f"Skland startup resource check failed: {type(e).__name__}: {e}")


async def _check_after_startup() -> None:
    """启动完成后把推迟的检查放到后台跑一次；三项的顺序由上游函数保证。"""
    global _pending
    if _pending is None:
        return
    options, _pending = _pending, None
    task = asyncio.get_running_loop().create_task(_check(**options))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


def _install() -> None:
    """上游模块、版本和源码哈希都对得上的补丁才装上，其余跳过并警告。"""
    if _import_error is not None:
        logger.warning(f"Skland modules changed ({_import_error}); resource patches skipped")
        return
    if _upstream_matches("resource list", skland_download):
        GameResourceDownloader.fetch_file_list = classmethod(fetch_file_list)
        logger.info("Skland resource list resilience patch active")
    if _on_ready is None:
        logger.warning("NoneBot driver has no ready hook; Skland startup resource patch skipped")
    elif _upstream_matches("startup resource", skland_hook, skland_resources, skland_data):
        skland_hook.update_data_resources = _startup_update
        _on_ready(_check_after_startup)
        logger.info("Skland startup resource patch active")


_install()
