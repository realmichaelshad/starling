"""Operations: encrypted archives, verification, restore, retention and the nightly scheduler.

Modules: `archive` (create and verify), `crypto` (container format), `restore`, `retention`,
`scheduler` (`run_backup_loop(settings, stop_event, hour_utc=3)` for the supervisor) and
`commands` (the `backup`, `backup-verify`, `restore` and `backup-prune` CLI commands).
"""
