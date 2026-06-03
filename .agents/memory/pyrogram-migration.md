---
name: Pyrogram MTProto migration
description: How the bot was migrated from HTTP Bot API polling to Pyrogram MTProto, key patterns and gotchas.
---

## Architecture pattern

Pyrogram async handlers **convert** incoming objects to Bot-API-compatible dicts, then dispatch to a `ThreadPoolExecutor` (worker_pool). All business logic in `handle_message()` / `handle_callback_query()` runs unchanged in threads and calls sync wrappers around Pyrogram.

### Key components

- `_pyro_sync(coro, timeout, reraise)` — runs any Pyrogram coroutine from a thread using `asyncio.run_coroutine_threadsafe(_event_loop)`. `_event_loop` is set inside `async def _run()` before `await idle()`.
- `_convert_keyboard(markup)` — converts Bot-API keyboard dicts (`inline_keyboard`, `keyboard`, `remove_keyboard`) to Pyrogram objects (`InlineKeyboardMarkup`, `ReplyKeyboardMarkup`, `ReplyKeyboardRemove`). All existing keyboard dicts are kept as-is and converted at send-time.
- `_pyro_msg_to_update`, `_pyro_callback_to_update`, `_pyro_channel_to_update` — thin converters from Pyrogram objects to Bot-API update dicts so existing handlers need zero changes.
- Handler decorators dispatch to worker_pool (16 workers); background_pool (8 workers) for DB saves, notifications.
- `idle` must be imported as `from pyrogram import idle`, NOT `from pyrogram.idle import idle`.

### send_* functions return format
All send wrappers return `{'ok': True, 'result': {'message_id': result.id}}` or `None` — same shape as old HTTP responses so business logic checks remain valid.

### Broadcast
`_run_broadcast` uses `_pyro_sync(..., reraise=True)` then catches Pyrogram exceptions by checking error string for 'blocked'/'deactivated'/'invalid'/'forbidden'/'peer'.

### TgCrypto warning
"TgCrypto is missing" is harmless — bot works, just slower crypto. Can install `tgcrypto` for speedup.

**Why:** Pyrogram MTProto gives lower latency and doesn't rely on HTTP polling which has getUpdates timeouts and conflicts.

**How to apply:** When adding new Telegram API calls, use `_pyro_sync(app.<method>(...))` inside sync functions, or `await app.<method>(...)` inside async Pyrogram handlers.
