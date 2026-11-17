"""Room chat UI — pure functions of room state."""

import time

from app.common.identity import VISITOR_SESSION_INIT
from app.common.shell import page
from app.features.lobby.urls import LOBBY
from stario import h
from stario.datastar import DebugInspector, at, data
from stario.markup import HtmlElement, SafeString, classes, styles

from .models import Message, Room, User
from .urls import LIVE, SEND, TYPING


def message_view(msg: Message, current_user_id: str) -> HtmlElement:
    msg_time = time.strftime("%H:%M", time.localtime(msg.timestamp))
    return h.Div(
        {
            "data-msg-id": msg.id,
        },
        classes("message", "own" if msg.user_id == current_user_id else None),
        h.Div(
            {"class": "message-header"},
            h.Span(
                {"class": "username"},
                styles({"color": msg.color}),
                msg.username,
            ),
            h.Span({"class": "timestamp"}, msg_time),
        ),
        h.Div({"class": "message-text"}, msg.text),
    )


def messages_view(current_user_id: str, messages: list[Message]) -> HtmlElement:
    return h.Div(
        {"id": "messages"},
        classes("messages", "empty" if not messages else None),
        h.Div({"class": "empty-state"}, "No messages yet. Say hello!")
        if not messages
        else None,
        data.on("load", "setTimeout(() => el.scrollTop = el.scrollHeight, 10)")
        if messages
        else None,
        [message_view(msg, current_user_id) for msg in messages] or None,
    )


def typing_text(typing_users: list[User]) -> str | None:
    if not typing_users:
        return None
    if len(typing_users) == 1:
        return f"{typing_users[0].username} is typing"
    if len(typing_users) == 2:
        return f"{typing_users[0].username} and {typing_users[1].username} are typing"
    return f"{typing_users[0].username} and {len(typing_users) - 1} others are typing"


def typing_indicator_view(current_user_id: str, users: list[User]) -> HtmlElement:
    typing_users = [
        user for user in users if user.typing and user.id != current_user_id
    ]
    text = typing_text(typing_users)
    return h.Div(
        {"id": "typing"},
        classes("typing-indicator", "hidden" if not text else None),
        h.Span({"class": "typing-text"}, text) if text else None,
        h.Span(
            {"class": "typing-dots"},
            h.Span({"class": "dot"}, "."),
            h.Span({"class": "dot"}, "."),
            h.Span({"class": "dot"}, "."),
        )
        if text
        else None,
    )


def online_users_view(users: list[User]) -> HtmlElement:
    if not users:
        return h.Div({"id": "online", "class": "online-users"})

    return h.Div(
        {"id": "online", "class": "online-users"},
        h.Span({"class": "online-label"}, f"{len(users)} online"),
        h.Div(
            {"class": "avatars"},
            [
                h.Span(
                    {
                        "class": "avatar",
                        "title": user.username,
                    },
                    styles({"background-color": user.color}),
                    user.username[0].upper(),
                )
                for user in users[:8]
            ],
            h.Span({"class": "avatar more"}, f"+{len(users) - 8}")
            if len(users) > 8
            else None,
        ),
    )


def input_form_view(room_id: str) -> HtmlElement:
    send = at.post(SEND.href(room_id))
    return h.Form(
        {"id": "input-form", "class": "input-form"},
        data.on(
            "submit",
            f"if ($message.trim()) {{ {send}; $message = ''; }}",
            prevent=True,
        ),
        h.Input(
            {
                "id": "message-input",
                "type": "text",
                "class": "message-input",
                "placeholder": "Type a message...",
                "autocomplete": "off",
                "autofocus": True,
            },
            data.bind("message"),
            data.on("input", at.post(TYPING.href(room_id))),
        ),
        h.Button(
            {"type": "button", "class": "send-button"},
            data.attrs({"disabled": "!$message"}),
            data.on(
                "click",
                f"""
                if ($message.trim()) {{
                    {send};
                    $message = '';
                    document.getElementById('message-input').focus();
                }}
                """,
            ),
            h.Span(
                {"class": "send-icon"},
                SafeString(
                    """<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14.536 21.686a.5.5 0 0 0 .937-.024l6.5-19a.496.496 0 0 0-.635-.635l-19 6.5a.5.5 0 0 0-.024.937l7.93 3.18a2 2 0 0 1 1.112 1.11z"/><path d="m21.854 2.147-10.94 10.939"/></svg>"""
                ),
            ),
        ),
    )


def room_live_view(
    room: Room,
    user_id: str,
    *,
    messages: list[Message],
    users: list[User],
) -> HtmlElement:
    """Patch target for SSE — excludes the #room shell that owns data.init."""
    return h.Div(
        {"id": "room-live"},
        h.Div(
            {"class": "chat-header"},
            h.A({"class": "back-link", "href": LOBBY.href()}, "← Rooms"),
            h.Div(
                {"class": "chat-title-wrap"},
                h.Div({"class": "chat-title"}, room.title),
                h.Div({"class": "chat-subtitle"}, room.description),
            ),
            online_users_view(users),
        ),
        h.Div(
            {"class": "chat-body"},
            messages_view(user_id, messages),
            typing_indicator_view(user_id, users),
        ),
        h.Div({"class": "chat-footer"}, input_form_view(room.id)),
    )


ROOM_LIVE_SELECTOR = "#room-live"


def room_view(
    room: Room,
    user_id: str,
    username: str,
    color: str,
    *,
    messages: list[Message],
    users: list[User],
) -> HtmlElement:
    return page(
        [
            DebugInspector(),
            h.Div(
                {"id": "room", "class": "chat-container"},
                data.signals(
                    {
                        "user_id": user_id,
                        "username": username,
                        "color": color,
                        "message": "",
                    },
                    if_missing=True,
                ),
                data.init(f"{VISITOR_SESSION_INIT}\n{at.get(LIVE.href(room.id))}"),
                room_live_view(
                    room,
                    user_id,
                    messages=messages,
                    users=users,
                ),
            ),
        ]
    )
