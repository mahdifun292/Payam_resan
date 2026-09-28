import time
from flask import Flask, render_template, request
from flask_socketio import SocketIO, join_room, leave_room, emit

app = Flask(__name__)
app.config["SECRET_KEY"] = "change-me-in-production"
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

# ---------------- State (in-memory) ----------------
public_rooms = {
    "general": {"name": "general", "description": "اتاق عمومی اصلی", "type": "public"},
    "tech":    {"name": "tech",    "description": "بحث‌های فنی",     "type": "public"},
    "random":  {"name": "random",  "description": "گفتگوی آزاد",     "type": "public"},
}

private_rooms = {}   # name -> {"name","description","password","owner","type"}
users = {}           # sid  -> {"username": str, "room": str|None}
messages = {}        # room_key -> [ {username, text, time, system} ]
MAX_HISTORY = 200


# ---------------- Helpers ----------------
def add_message(room, msg):
    lst = messages.setdefault(room, [])
    lst.append(msg)
    if len(lst) > MAX_HISTORY:
        del lst[: len(lst) - MAX_HISTORY]


def sys_msg(room, text):
    m = {"username": "", "text": text, "time": time.strftime("%H:%M"), "system": True}
    add_message(room, m)
    socketio.emit("message", {"room": room, **m}, to=room)


def room_users(room):
    return sorted(u["username"] for u in users.values() if u.get("room") == room)


def all_users():
    return sorted(u["username"] for u in users.values())


def sid_of(username):
    for sid, u in users.items():
        if u["username"] == username:
            return sid
    return None


def dm_key(a, b):
    return "dm:" + "|".join(sorted([a, b]))


def rooms_payload():
    return {
        "public": list(public_rooms.values()),
        "private": [
            {"name": r["name"], "description": r["description"], "owner": r["owner"], "type": "private"}
            for r in private_rooms.values()
        ],
    }


# ---------------- Routes ----------------
@app.route("/")
def index():
    return render_template("index.html")


# ---------------- Socket events ----------------
@socketio.on("connect")
def on_connect():
    pass


@socketio.on("disconnect")
def on_disconnect():
    sid = request.sid
    user = users.pop(sid, None)
    if not user:
        return
    room = user.get("room")
    if room:
        leave_room(room)
        if room.startswith("dm:"):
            socketio.emit("user_list", {"users": all_users()})
        else:
            sys_msg(room, f"{user['username']} از اتاق خارج شد")
            socketio.emit("room_users", {"room": room, "users": room_users(room)}, to=room)
    socketio.emit("user_list", {"users": all_users()})


@socketio.on("set_username")
def on_set_username(data):
    username = (data.get("username") or "").strip()
    if not username or len(username) > 20:
        emit("login_error", {"message": "نام کاربری نامعتبر است (۱ تا ۲۰ کاراکتر)."})
        return
    if any(u["username"] == username for u in users.values()):
        emit("login_error", {"message": "این نام کاربری همین حالا استفاده می‌شود."})
        return

    users[request.sid] = {"username": username, "room": None}
    emit("username_ok", {"username": username})
    emit("rooms_list", rooms_payload())
    socketio.emit("user_list", {"users": all_users()})


@socketio.on("join")
def on_join(data):
    sid = request.sid
    me = users.get(sid)
    if not me:
        return

    room = (data.get("room") or "").strip()
    password = data.get("password") or ""

    if room in public_rooms:
        info = public_rooms[room]
        title = "#" + room
    elif room in private_rooms:
        info = private_rooms[room]
        if info["password"] != password:
            emit("error_msg", {"message": "رمز اتاق اشتباه است."})
            return
        title = "🔒 " + room
    else:
        emit("error_msg", {"message": "اتاق پیدا نشد."})
        return

    old = me.get("room")
    if old and old != room:
        leave_room(old)
        if not old.startswith("dm:"):
            sys_msg(old, f"{me['username']} اتاق را ترک کرد")
            socketio.emit("room_users", {"room": old, "users": room_users(old)}, to=old)

    join_room(room)
    me["room"] = room

    emit("joined", {
        "room": room,
        "title": title,
        "description": info.get("description", ""),
        "history": messages.get(room, []),
        "dm_target": None,
    })

    sys_msg(room, f"{me['username']} وارد اتاق شد")
    socketio.emit("room_users", {"room": room, "users": room_users(room)}, to=room)


@socketio.on("send_message")
def on_send_message(data):
    me = users.get(request.sid)
    if not me:
        return
    room = me.get("room")
    if not room or room.startswith("dm:"):
        return

    text = (data.get("text") or "").strip()[:1000]
    if not text:
        return

    msg = {"username": me["username"], "text": text,
           "time": time.strftime("%H:%M"), "system": False}
    add_message(room, msg)
    socketio.emit("message", {"room": room, **msg}, to=room)


@socketio.on("create_room")
def on_create_room(data):
    me = users.get(request.sid)
    if not me:
        return

    name = (data.get("name") or "").strip().lower().replace(" ", "-")
    password = data.get("password") or ""
    description = (data.get("description") or "").strip() or "اتاق خصوصی"

    if not name or len(name) > 24:
        emit("error_msg", {"message": "نام اتاق نامعتبر است."})
        return
    if name in public_rooms or name in private_rooms:
        emit("error_msg", {"message": "این نام قبلاً استفاده شده است."})
        return
    if len(password) < 3:
        emit("error_msg", {"message": "رمز باید حداقل ۳ کاراکتر باشد."})
        return

    private_rooms[name] = {
        "name": name, "description": description,
        "password": password, "owner": me["username"], "type": "private",
    }

    socketio.emit("rooms_list", rooms_payload())
    emit("room_created", {"room": name})


@socketio.on("open_dm")
def on_open_dm(data):
    me = users.get(request.sid)
    if not me:
        return

    target = (data.get("target") or "").strip()
    if not target or target == me["username"]:
        return

    key = dm_key(me["username"], target)
    emit("joined", {
        "room": key,
        "title": "@" + target,
        "description": "گفتگوی خصوصی",
        "history": messages.get(key, []),
        "dm_target": target,
    })


@socketio.on("send_dm")
def on_send_dm(data):
    me = users.get(request.sid)
    if not me:
        return

    target = (data.get("target") or "").strip()
    text = (data.get("text") or "").strip()[:1000]
    if not target or not text or target == me["username"]:
        return

    key = dm_key(me["username"], target)
    msg = {"username": me["username"], "text": text,
           "time": time.strftime("%H:%M"), "system": False}
    add_message(key, msg)

    socketio.emit("message", {"room": key, **msg}, to=request.sid)
    tsid = sid_of(target)
    if tsid:
        socketio.emit("message", {"room": key, **msg}, to=tsid)


if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=5000, debug=True)
