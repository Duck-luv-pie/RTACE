"""Loads the shared Lua scripts from ../lua and exposes them as redis-py Script objects.

The scripts are the single source of truth for the per-event checks; the Go
decision service loads the same files. Call a script with ``client=pipe`` to
queue it in a pipeline (redis-py loads missing scripts before execute).
"""

from pathlib import Path

LUA_DIR = Path(__file__).resolve().parent.parent / "lua"

TX_CHECK = (LUA_DIR / "tx_check.lua").read_text()
AUTH_CHECK = (LUA_DIR / "auth_check.lua").read_text()

# Result field positions of tx_check.lua
TX_REPLAY_STATUS, TX_STORED_REF, TX_BURST_COUNT, TX_GEO_STATUS = 0, 1, 2, 3
TX_DISTANCE_KM, TX_ELAPSED_S, TX_VELOCITY_KMH, TX_PREV_LAT, TX_PREV_LON = 4, 5, 6, 7, 8


class Scripts:
    def __init__(self, redis_client) -> None:
        self.tx_check = redis_client.register_script(TX_CHECK)
        self.auth_check = redis_client.register_script(AUTH_CHECK)
