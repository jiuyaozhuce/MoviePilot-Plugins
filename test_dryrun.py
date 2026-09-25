#!/usr/bin/env python3
"""测试 Dry-run 模式"""
import sys
sys.path.insert(0, "/app")

from app.plugins.embyunwatchedwash import EmbyUnwatchedWash

# Create instance
plugin = EmbyUnwatchedWash()
plugin.init_plugin({
    "enabled": True,
    "cron": "",
    "notify": False,
    "only_once": False,
    "include_series": True,
    "selected_items": [],
    "series_episode_level": True,
    "limit": 3,
    "exclude_libraries": ["Kids"],
    "exclude_keywords": ["children"],
    "dry_run": True
})

print("dry_run:", plugin._dry_run)
print("exclude_libraries:", plugin._exclude_libraries)
print("limit:", plugin._limit)
print("calling sync()...")
plugin.sync()
print("sync done")
