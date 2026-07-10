"""Minimal GramAddict globals for dashboard debug runs."""

from __future__ import annotations

from types import SimpleNamespace

DEFAULT_APP_ID = "com.instagram.android"
_bootstrapped = False


def ensure_gramaddict_globals() -> None:
    global _bootstrapped
    if _bootstrapped:
        return

    from GramAddict.core import utils as ga_utils
    from GramAddict.core import views as ga_views
    from GramAddict.core import interaction as ga_interaction
    from GramAddict.core.resources import ResourceID as resources

    if getattr(ga_utils, "args", None) is not None:
        configs = getattr(ga_utils, "configs", None) or SimpleNamespace(args=ga_utils.args)
        resource_ids = resources(getattr(ga_utils.args, "app_id", DEFAULT_APP_ID))
        if getattr(ga_views, "ResourceID", None) is None:
            ga_views.ResourceID = resource_ids
        if getattr(ga_utils, "ResourceID", None) is None:
            ga_utils.ResourceID = resource_ids
        if getattr(ga_interaction, "ResourceID", None) is None:
            ga_interaction.load_config(configs)
        _bootstrapped = True
        return

    minimal = SimpleNamespace(
        app_id=DEFAULT_APP_ID,
        speed_multiplier=1,
        screen_record=False,
        dont_type=False,
        close_apps=False,
        kill_atx_agent=False,
        disable_block_detection=True,
        watch_video_time="0",
        watch_photo_time="0",
    )
    ga_utils.args = minimal
    ga_utils.configs = SimpleNamespace(args=minimal)
    ga_utils.app_id = DEFAULT_APP_ID
    ga_views.args = minimal
    ga_views.configs = ga_utils.configs
    resource_ids = resources(DEFAULT_APP_ID)
    ga_views.ResourceID = resource_ids
    ga_utils.ResourceID = resource_ids
    ga_interaction.load_config(ga_utils.configs)
    _bootstrapped = True
