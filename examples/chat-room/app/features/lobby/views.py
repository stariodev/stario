"""Lobby — pick a room."""

import json

from app.common.identity import VISITOR_SESSION_INIT, VisitorIdentity
from app.common.shell import page
from app.features.room.models import Room
from app.features.room.urls import CREATE, DELETE, ROOM
from stario import h
from stario.datastar import DebugInspector, at, data
from stario.markup import HtmlElement

from .urls import LIVE


def lobby_live_view(
    *,
    rooms: list[Room],
    online_counts: dict[str, int],
) -> HtmlElement:
    """Patch target for SSE — excludes the #lobby shell that owns data.init."""
    return h.Div(
        {"id": "lobby-live"},
        h.Div(
            {"class": "lobby-header"},
            h.H1({"class": "lobby-title"}, "Stario Chat 🐾"),
            h.Button(
                {"type": "button", "class": "lobby-new-btn"},
                data.on(
                    "click",
                    "document.getElementById('create-room-dialog').showModal()",
                ),
                "New room",
            ),
        ),
        h.P(
            {"class": "lobby-lead"},
            "Pick a room — each has its own URL, messages, and presence.",
        ),
        h.Ul(
            {"class": "room-list"},
            [_room_card(room, online=online_counts.get(room.id, 0)) for room in rooms]
            or h.Li(h.P({"class": "lobby-empty"}, "No rooms yet — create one above.")),
        ),
        _create_room_dialog(),
    )


LOBBY_LIVE_SELECTOR = "#lobby-live"


def lobby_view(
    *,
    identity: VisitorIdentity,
    rooms: list[Room],
    online_counts: dict[str, int],
) -> HtmlElement:
    return page(
        [
            DebugInspector(),
            h.Div(
                {"id": "lobby", "class": "lobby-container"},
                data.signals(
                    {
                        "user_id": identity.user_id,
                        "username": identity.username,
                        "color": identity.color,
                        "room_title": "",
                        "room_description": "",
                    },
                    if_missing=True,
                ),
                data.init(
                    f"{VISITOR_SESSION_INIT}\n{at.get(LIVE.href(), retry='always')}"
                ),
                lobby_live_view(rooms=rooms, online_counts=online_counts),
            ),
        ]
    )


def _create_room_dialog() -> HtmlElement:
    return h.Dialog(
        {"id": "create-room-dialog", "class": "room-dialog"},
        h.Form(
            {"class": "room-dialog-form"},
            data.on(
                "submit",
                f"""
                if ($room_title.trim()) {{
                    {at.post(CREATE.href())};
                    $room_title = '';
                    $room_description = '';
                    el.closest('dialog').close();
                }}
                """,
                prevent=True,
            ),
            h.H2({"class": "room-dialog-title"}, "New room"),
            h.Label(
                {"class": "room-dialog-label"},
                "Name",
                h.Input(
                    {
                        "type": "text",
                        "class": "room-dialog-input",
                        "placeholder": "e.g. Design",
                        "autocomplete": "off",
                    },
                    data.bind("room_title"),
                ),
            ),
            h.Label(
                {"class": "room-dialog-label"},
                "Description",
                h.Textarea(
                    {
                        "class": "room-dialog-textarea",
                        "placeholder": "What is this room for?",
                        "rows": "3",
                    },
                    data.bind("room_description"),
                ),
            ),
            h.Div(
                {"class": "room-dialog-actions"},
                h.Button(
                    {"type": "button", "class": "room-dialog-cancel"},
                    data.on("click", "el.closest('dialog').close()"),
                    "Cancel",
                ),
                h.Button(
                    {"type": "submit", "class": "room-dialog-create"},
                    data.attrs({"disabled": "!$room_title.trim()"}),
                    "Create",
                ),
            ),
        ),
    )


def _room_card(room: Room, *, online: int) -> HtmlElement:
    # json.dumps yields a fully escaped JS string literal — quotes, newlines,
    # backslashes, and non-ASCII all handled, unlike hand-rolled replaces.
    confirm_text = json.dumps(f"Delete {room.title}?")
    return h.Li(
        h.Div(
            {"class": "room-card"},
            h.A(
                {"class": "room-card-link", "href": ROOM.href(room_id=room.id)},
                h.Span({"class": "room-card-title"}, room.title),
                h.Span({"class": "room-card-desc"}, room.description),
                h.Span({"class": "room-card-meta"}, f"{online} online"),
            ),
            h.Button(
                {"type": "button", "class": "room-delete-btn", "title": "Delete room"},
                data.on(
                    "click",
                    f"""
                    evt.stopPropagation();
                    if (confirm({confirm_text})) {{
                        {at.delete(DELETE.href(room.id))};
                    }}
                    """,
                ),
                "×",
            ),
        )
    )
