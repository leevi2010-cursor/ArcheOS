"""Isolated read-only bridge to the pinned local ``wechat-cli`` runtime.

This module is executed by the Python interpreter that owns ``wechat-cli``.
It deliberately imports provider-private query helpers only inside that
isolated process.  ArcheOS receives structured JSON and never imports provider
types into its Core or persistence layers.
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import sqlite3
import sys
from contextlib import closing, redirect_stdout
from datetime import datetime
from io import StringIO
from pathlib import Path


def _digest(prefix: str, *parts: object) -> str:
    encoded = "\0".join(str(part) for part in parts).encode("utf-8")
    return f"{prefix}_{hashlib.sha256(encoded).hexdigest()[:32]}"


def _cursor(value: object, field: str) -> tuple[int, str, str]:
    if not isinstance(value, dict) or set(value) != {
        "timestamp",
        "conversation_key",
        "message_key",
    }:
        raise ValueError(f"{field} is invalid")
    timestamp = value["timestamp"]
    conversation_key = value["conversation_key"]
    message_key = value["message_key"]
    if (
        isinstance(timestamp, bool)
        or not isinstance(timestamp, int)
        or timestamp < 0
        or not isinstance(conversation_key, str)
        or not isinstance(message_key, str)
    ):
        raise ValueError(f"{field} is invalid")
    return timestamp, conversation_key, message_key


def _cursor_dict(value: tuple[int, str, str]) -> dict[str, object]:
    return {
        "timestamp": value[0],
        "conversation_key": value[1],
        "message_key": value[2],
    }


def _window_upper(
    remaining: list[tuple[int, str, str]],
    *,
    window_days: int,
    message_limit: int,
) -> tuple[int, str, str]:
    first_timestamp = min(cursor[0] for cursor in remaining)
    cutoff = first_timestamp + window_days * 24 * 60 * 60
    time_bounded = sorted(
        cursor for cursor in remaining if cursor[0] < cutoff
    )
    return time_bounded[min(len(time_bounded), message_limit) - 1]


def _message_type(local_type: object, app_type: int | None) -> str:
    from wechat_cli.core.messages import _split_msg_type

    base_type, _ = _split_msg_type(local_type)
    if base_type == 1:
        return "text"
    if base_type == 3:
        return "image"
    if base_type == 34:
        return "voice"
    if base_type == 43:
        return "video"
    if base_type == 47:
        return "sticker"
    if base_type == 48:
        return "location"
    if base_type == 49 and app_type == 6:
        return "file"
    if base_type == 49 and app_type == 5:
        return "link"
    if base_type == 49:
        return "application"
    if base_type == 50:
        return "call"
    if base_type in {10000, 10002}:
        return "system"
    return "unsupported"


def _app_message(content: str) -> tuple[int | None, str | None]:
    from wechat_cli.core.messages import _parse_int, _parse_xml_root

    root = _parse_xml_root(content)
    if root is None:
        return None, None
    appmsg = root.find(".//appmsg")
    if appmsg is None:
        return None, None
    app_type = _parse_int((appmsg.findtext("type") or "").strip())
    title = (appmsg.findtext("title") or "").strip() or None
    return app_type, title


def _attachment(
    *,
    app: object,
    message_key: str,
    local_type: object,
    content: str,
    create_time: int,
) -> list[dict[str, object]]:
    from wechat_cli.core.messages import _split_msg_type

    base_type, _ = _split_msg_type(local_type)
    app_type, title = _app_message(content) if base_type == 49 else (None, None)
    attachment_key = _digest("wechat_attachment", message_key, 1)
    if base_type == 49 and app_type == 6:
        if title is None or Path(title).name != title or title in {".", ".."}:
            return [{
                "attachment_key": attachment_key,
                "status": "ambiguous",
                "filename_hint": title or "attachment",
                "media_type": "application/octet-stream",
                "path": None,
            }]
        month = datetime.fromtimestamp(create_time).astimezone().strftime("%Y-%m")
        candidate = Path(app.db_dir).parent / "msg" / "file" / month / title
        try:
            exact = candidate.is_file() and not candidate.is_symlink()
        except OSError:
            exact = False
        return [{
            "attachment_key": attachment_key,
            "status": "available" if exact else "missing",
            "filename_hint": title,
            "media_type": mimetypes.guess_type(title)[0]
            or "application/octet-stream",
            "path": str(candidate) if exact else None,
        }]
    if base_type in {3, 34, 43, 47}:
        labels = {3: "image", 34: "voice", 43: "video", 47: "sticker"}
        return [{
            "attachment_key": attachment_key,
            "status": "ambiguous",
            "filename_hint": labels[base_type],
            "media_type": {
                3: "image/unknown",
                34: "audio/unknown",
                43: "video/unknown",
                47: "image/unknown",
            }[base_type],
            "path": None,
        }]
    return []


def _sessions(
    app: object, names: dict[str, str] | None = None
) -> tuple[tuple[str, str, bool], ...]:
    from wechat_cli.core.contacts import get_contact_names

    session_path = app.cache.get(os.path.join("session", "session.db"))
    if not session_path:
        raise RuntimeError("session database unavailable")
    names = names or get_contact_names(app.cache, app.decrypted_dir)
    with closing(sqlite3.connect(session_path)) as connection:
        rows = connection.execute(
            "SELECT username FROM SessionTable WHERE last_timestamp > 0 "
            "ORDER BY username"
        ).fetchall()
    return tuple(
        (username, names.get(username, username), "@chatroom" in username)
        for (username,) in rows
        if isinstance(username, str) and username
    )


def _message_table_locations(
    app: object,
) -> dict[str, tuple[tuple[str, str], ...]]:
    locations: dict[str, list[tuple[str, str]]] = {}
    for relative_key in app.msg_db_keys:
        database = app.cache.get(relative_key)
        if not database:
            continue
        try:
            with closing(sqlite3.connect(database)) as connection:
                names = connection.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='table' AND name LIKE 'Msg_%'"
                ).fetchall()
        except sqlite3.Error:
            raise RuntimeError("message table discovery failed") from None
        for (table_name,) in names:
            if isinstance(table_name, str):
                locations.setdefault(table_name, []).append(
                    (relative_key, str(database))
                )
    return {
        table_name: tuple(values) for table_name, values in locations.items()
    }


def _located_session_tables(
    sessions: tuple[tuple[str, str, bool], ...],
    table_locations: dict[str, tuple[tuple[str, str], ...]],
) -> dict[str, tuple[tuple[str, str, str, bool, str], ...]]:
    by_database: dict[str, list[tuple[str, str, str, bool, str]]] = {}
    for username, display_name, is_group in sessions:
        table_name = "Msg_" + hashlib.md5(username.encode()).hexdigest()
        for relative_key, database in table_locations.get(table_name, ()):
            by_database.setdefault(database, []).append(
                (relative_key, username, display_name, is_group, table_name)
            )
    return {
        database: tuple(values) for database, values in by_database.items()
    }


def _all_cursor_rows(
    located_tables: dict[str, tuple[tuple[str, str, str, bool, str], ...]],
    *,
    start_timestamp: int,
) -> list[tuple[str, str, object, object]]:
    rows: list[tuple[str, str, object, object]] = []
    for database, tables in located_tables.items():
        try:
            with closing(sqlite3.connect(database)) as connection:
                for relative_key, username, _label, _group, table_name in tables:
                    values = connection.execute(
                        f"SELECT local_id, create_time FROM [{table_name}] "
                        "WHERE create_time >= ? ORDER BY create_time ASC",
                        (start_timestamp,),
                    ).fetchall()
                    rows.extend(
                        (username, relative_key, *value) for value in values
                    )
        except sqlite3.Error:
            raise RuntimeError("message cursor query failed") from None
    return rows


def _all_message_rows(
    located_tables: dict[str, tuple[tuple[str, str, str, bool, str], ...]],
    *,
    start_timestamp: int,
    end_timestamp: int,
) -> list[tuple[object, ...]]:
    from wechat_cli.core.messages import _query_messages

    rows: list[tuple[object, ...]] = []
    for database, tables in located_tables.items():
        try:
            with closing(sqlite3.connect(database)) as connection:
                for relative_key, username, display_name, is_group, table_name in tables:
                    rows.extend(
                        (username, display_name, is_group, relative_key, *row)
                        for row in _query_messages(
                            connection,
                            table_name,
                            start_ts=start_timestamp,
                            end_ts=end_timestamp,
                            limit=None,
                        )
                    )
        except sqlite3.Error:
            raise RuntimeError("message database query failed") from None
    return rows


def _capture(request: dict[str, object]) -> dict[str, object]:
    from wechat_cli.core.contacts import get_contact_names
    from wechat_cli.core.context import AppContext
    from wechat_cli.core.messages import (
        _format_message_text,
        _load_name2id_maps,
        _resolve_sender_label,
        decompress_content,
    )

    if set(request) != {
        "config_path",
        "after_cursor",
        "upper_bound",
        "all_history_upper_bound",
        "observe_only",
        "window_days",
        "window_message_limit",
    }:
        raise ValueError("capture request is invalid")
    config_path = request["config_path"]
    if config_path is not None and not isinstance(config_path, str):
        raise ValueError("config_path is invalid")
    if not isinstance(request["observe_only"], bool):
        raise TypeError("observe_only is invalid")
    window_days = request["window_days"]
    if (
        isinstance(window_days, bool)
        or not isinstance(window_days, int)
        or not 1 <= window_days <= 366
    ):
        raise ValueError("window_days is invalid")
    window_message_limit = request["window_message_limit"]
    if (
        isinstance(window_message_limit, bool)
        or not isinstance(window_message_limit, int)
        or window_message_limit < 1
    ):
        raise ValueError("window_message_limit is invalid")
    after = _cursor(request["after_cursor"], "after_cursor")
    fixed_upper = (
        None
        if request["upper_bound"] is None
        else _cursor(request["upper_bound"], "upper_bound")
    )
    all_history_upper = (
        None
        if request["all_history_upper_bound"] is None
        else _cursor(
            request["all_history_upper_bound"], "all_history_upper_bound"
        )
    )
    if fixed_upper is not None and all_history_upper is not None:
        raise ValueError("capture boundaries conflict")
    app = AppContext(config_path)
    names = get_contact_names(app.cache, app.decrypted_dir)
    sessions = _sessions(app, names)
    table_locations = _message_table_locations(app)
    located_tables = _located_session_tables(sessions, table_locations)
    all_cursor_rows = _all_cursor_rows(
        located_tables, start_timestamp=after[0]
    )
    if request["observe_only"]:
        cursors = [
            (create_time, _digest("wechat_conversation", username), _digest(
                "wechat_message", username, database_key, local_id, create_time
            ))
            for username, database_key, local_id, create_time in all_cursor_rows
            if isinstance(create_time, int) and not isinstance(create_time, bool)
        ]
        bounded = [
            cursor
            for cursor in cursors
            if cursor > after
            and (fixed_upper is None or cursor <= fixed_upper)
            and (all_history_upper is None or cursor <= all_history_upper)
        ]
        observed_upper = (
            fixed_upper
            or all_history_upper
            or (max(bounded) if bounded else after)
        )
        return {
            "schema_version": "wechat-cli-capture/1.0",
            "observed_upper": _cursor_dict(observed_upper),
            "messages": [],
        }
    cursor_rows = [
        (
            create_time,
            _digest("wechat_conversation", username),
            _digest(
                "wechat_message", username, database_key, local_id, create_time
            ),
        )
        for username, database_key, local_id, create_time in all_cursor_rows
        if isinstance(create_time, int) and not isinstance(create_time, bool)
    ]
    remaining = [
        cursor
        for cursor in cursor_rows
        if cursor > after
        and (fixed_upper is None or cursor <= fixed_upper)
        and (all_history_upper is None or cursor <= all_history_upper)
    ]
    if fixed_upper is None and remaining:
        effective_upper = _window_upper(
            remaining,
            window_days=window_days,
            message_limit=window_message_limit,
        )
    else:
        effective_upper = fixed_upper or after
    id_maps: dict[str, dict[object, str]] = {}
    for relative_key in app.msg_db_keys:
        database = app.cache.get(relative_key)
        if not database:
            continue
        try:
            with closing(sqlite3.connect(database)) as connection:
                id_maps[relative_key] = _load_name2id_maps(connection)
        except sqlite3.Error:
            id_maps[relative_key] = {}
    messages: dict[str, dict[str, object]] = {}
    rows = _all_message_rows(
        located_tables,
        start_timestamp=after[0],
        end_timestamp=effective_upper[0],
    )
    for row in rows:
        (
            username,
            display_name,
            is_group,
            database_key,
            local_id,
            local_type,
            create_time,
            real_sender_id,
            raw_content,
            compression,
        ) = row
        conversation_key = _digest("wechat_conversation", username)
        if isinstance(create_time, bool) or not isinstance(create_time, int):
            continue
        message_key = _digest(
            "wechat_message",
            username,
            database_key,
            local_id,
            create_time,
        )
        cursor = (create_time, conversation_key, message_key)
        if cursor <= after or cursor > effective_upper:
            continue
        content = decompress_content(raw_content, compression)
        if content is None:
            content = "(unavailable)"
        content = str(content)
        app_type, _title = _app_message(content)
        sender_from_content, visible_content = _format_message_text(
            local_id,
            local_type,
            content,
            is_group,
            username,
            display_name,
            names,
            app.display_name_fn,
            db_dir=app.db_dir,
            create_time_ts=create_time,
            resolve_media=False,
        )
        sender_label = _resolve_sender_label(
            real_sender_id,
            sender_from_content,
            is_group,
            username,
            display_name,
            names,
            id_maps.get(str(database_key), {}),
            app.display_name_fn,
        )
        captured = {
            "conversation_key": conversation_key,
            "provider_conversation_id": username,
            "conversation_label": display_name,
            "is_group": is_group,
            "message_key": message_key,
            "cursor": _cursor_dict(cursor),
            "sender_label": sender_label or "unavailable",
            "message_type": _message_type(local_type, app_type),
            "timestamp": create_time,
            "sent_at": datetime.fromtimestamp(create_time).astimezone().isoformat(),
            "visible_content": str(visible_content),
            "structured_payload": content,
            "attachments": _attachment(
                app=app,
                message_key=message_key,
                local_type=local_type,
                content=content,
                create_time=create_time,
            ),
        }
        existing = messages.get(message_key)
        if existing is not None and existing != captured:
            raise RuntimeError("message identity collision")
        messages[message_key] = captured
    ordered = sorted(
        messages.values(),
        key=lambda item: _cursor(item["cursor"], "message.cursor"),
    )
    observed_upper = effective_upper
    return {
        "schema_version": "wechat-cli-capture/1.0",
        "observed_upper": _cursor_dict(observed_upper),
        "messages": ordered,
    }


def main() -> int:
    try:
        request = json.loads(sys.stdin.read())
        if not isinstance(request, dict):
            raise TypeError("capture request is invalid")
        with redirect_stdout(StringIO()):
            result = _capture(request)
    except Exception as exc:  # noqa: BLE001 - provider boundary must fail closed.
        print(f"capture_failed:{exc.__class__.__name__}", file=sys.stderr)
        return 1
    sys.stdout.write(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
