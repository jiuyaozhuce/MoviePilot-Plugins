#!/bin/bash
echo 'import sys; sys.path.insert(0, "/app")
from app.plugins.embyunwatchedwash import EmbyUnwatchedWash
plugin = EmbyUnwatchedWash()
plugin.init_plugin({
    "enabled": True, "cron": "", "notify": False,
    "only_once": False, "include_series": True,
    "selected_items": [], "series_episode_level": True,
    "limit": 3, "exclude_libraries": ["Kids"],
    "exclude_keywords": ["children"], "dry_run": True
})
print("dry_run:", plugin._dry_run)
print("exclude_libraries:", plugin._exclude_libraries)
plugin.sync()
print("sync done")
' | docker exec -i -u 1000:10 moviepilot-v3 python3 2>&1 | tail -30
