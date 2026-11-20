from pathlib import Path

import nonebot
from nonebot.adapters.onebot.v11 import Adapter as OneBotV11Adapter


nonebot.init(
    localstore_use_cwd=True,
    alembic_startup_check=True,
    htmlrender_browser="chromium",
)

driver = nonebot.get_driver()
driver.register_adapter(OneBotV11Adapter)

# 设置了 QQ_BOTS 才注册官方适配器、加载桥接层，这个仓库只维护这种用法。
# 没设置时就是一个普通的 OneBot v11 机器人，我不测试，见 docs/development.md。
OFFICIAL_BOT = bool(getattr(driver.config, "qq_bots", None))
if OFFICIAL_BOT:
    from nonebot.adapters.qq import Adapter as QQAdapter

    driver.register_adapter(QQAdapter)


def load_required_plugin(module_name: str) -> None:
    if nonebot.load_plugin(module_name) is None:
        raise RuntimeError(f"Required plugin failed to load: {module_name}")


load_required_plugin("plugins.log_privacy")
load_required_plugin("plugins.sqlite_foreign_keys")
load_required_plugin("plugins.command_tolerance")
if OFFICIAL_BOT:
    load_required_plugin("plugins.official_bridge")
load_required_plugin("plugins.temp_session_forward")
load_required_plugin("plugins.forward_reply_owner")
load_required_plugin("nonebot_plugin_skland")
load_required_plugin("plugins.skland_resource_resilience")
load_required_plugin("plugins.skland_compact_images")
load_required_plugin("plugins.render_image_cache")
load_required_plugin("plugins.skland_ark_assets")
load_required_plugin("plugins.skland_ef_theme")
load_required_plugin("plugins.skland_auto_gacha")
load_required_plugin("plugins.skland_ark_gacha")
load_required_plugin("plugins.skland_bind_sign")
load_required_plugin("plugins.skland_index_tolerance")
load_required_plugin("plugins.skland_shortcuts")
load_required_plugin("plugins.skland_reaction_order")
load_required_plugin("plugins.skland_buttons")
load_required_plugin("plugins.skl_help")
load_required_plugin("plugins.friend_welcome")
load_required_plugin("plugins.skland_gacha_rank")
load_required_plugin("plugins.endfield_roster")
load_required_plugin("plugins.arknights_roster")
load_required_plugin("plugins.arknights_garrison")
load_required_plugin("plugins.sanity_reminder")
load_required_plugin("plugins.endfield_wiki")
load_required_plugin("plugins.skland_ef_pools")
load_required_plugin("plugins.endfield_guide")
load_required_plugin("plugins.weekly_report")
load_required_plugin("plugins.game_calendar")
load_required_plugin("plugins.skland_health")
load_required_plugin("plugins.heybox_import")
load_required_plugin("plugins.group_onboarding")

# 只保存在本机的插件：local_plugins/ 下的每个包（这个文件夹不在仓库里）。
for _local in sorted(Path(__file__).with_name("local_plugins").glob("*/__init__.py")):
    load_required_plugin(f"local_plugins.{_local.parent.name}")


if __name__ == "__main__":
    nonebot.run()
