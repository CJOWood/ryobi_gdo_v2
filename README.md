# Robust WebSocket Handling and (Silent) Error Recovery for Ryobi GDO Integration
Lots of big changes to the codebase were made in this PR.

## Overview
This change improves the reliability of the Ryobi GDO integration’s websocket handling. It addresses error handling, reconnection logic, logging, and callback recursion, ensuring the integration can, hopefully, detect and robustly recover from abnormal websocket closures (such as code 1006) and other transient failures.

---
## 1. Abnormal Closure (1006) Handling
The integration now explicitly detects websocket close code 1006 (abnormal closure, e.g., network drop or server crash) and attempts to reconnect, rather than stopping permanently.
```
is_abnormal_close = close_code == 1006 or close_code is None
if is_abnormal_close:
    LOGGER.warning(
        "Websocket closed abnormally (code 1006), will attempt reconnect"
    )
    self._state = STATE_DISCONNECTED
    await self.set_state(STATE_DISCONNECTED)
```

## 2. Improved Logging
All logging now uses lazy formatting and is more consistent throughout the integration, with clear separation between debug, warning, and error levels. It has also been update to meet Home Assistant's linting requirements.

## 3. Centralized Post-WebSocket Error and Retry Logic
Post-websocket error handling and retry logic is now unified after the main connection loop, reducing duplication and making the code easier to maintain.
```
except Exception as err:
    error = err
    close_code = getattr(self._ws_client, "close_code", None)

# Unified post-connection and error handling
is_auth_error = (
    isinstance(error, aiohttp.ClientResponseError)
    and getattr(error, "status", None) == 401
)
is_abnormal_close = close_code == 1006 or close_code is None

if is_auth_error:
    ...
if error is not None and isinstance(error, aiohttp.ClientResponseError):
    ...
if error is not None or (close_code is not None and self._state != STATE_STOPPED):
    ...
```

## 4. Callback and Recursion Fixes
Callback logic is refactored to avoid recursive loops that previously caused stack overflows and maximum recursion depth errors. State changes only trigger reconnection or closure logic when appropriate.

## 5. Modern Python Features and Home Assistant Standards
- Pattern matching (match/case) is used for websocket message types.
- Type hints and f-strings are used throughout.
- All async I/O is non-blocking and follows Home Assistant’s async patterns.
- Logging follows Home Assistant’s lazy logging requirements.

## 6. Retry Logic: Immediate First Retry
The first retry after a failure now happens immediately, with no wait, and subsequent retries use exponential backoff.
```
retry_delay = (
    0
    if self.failed_attempts == 1
    else min(2 ** (self.failed_attempts - 2) * 30, 300)
)
LOGGER.error(
    "Websocket connection failed, retrying in %ds: %s",
    retry_delay,
    error,
)
await asyncio.sleep(retry_delay)
```

