"""Local-first Google Tasks integration.

Stdlib-only (urllib + http.server for the OAuth loopback flow) -- this repo
has no pip/venv, only distro packages (see pyproject.toml, CLAUDE.md), so a
packaged Google API client isn't an option here.

Every mutation (add/edit/complete/delete task) applies to the local mirror
(store.json) immediately and is appended to an offline queue (queue.json);
the Todo list is fully usable with no network at all. sync() pushes the
queue and pulls remote changes, but nothing about reading or editing tasks
ever calls it directly or waits on it -- callers (ui/app.py's TodoPage,
automation.py's FocusController) decide when to trigger it (app open, a
debounce after edits, manual refresh, network-up). Pulls use `updatedMin`
per list rather than a full re-list every time, to keep the request count
low.
"""
import contextlib
import fcntl
import json
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer

from .util import CONFIG_DIR, atomic_write_text

TASKS_DIR = CONFIG_DIR / "tasks"
CLIENT_FILE = TASKS_DIR / "client.json"
TOKEN_FILE = TASKS_DIR / "token.json"
STORE_FILE = TASKS_DIR / "store.json"
QUEUE_FILE = TASKS_DIR / "queue.json"
SYNC_FILE = TASKS_DIR / "sync.json"
LOCK_FILE = TASKS_DIR / ".sync.lock"

AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
API_BASE = "https://www.googleapis.com/tasks/v1"
SCOPE = "https://www.googleapis.com/auth/tasks"

DEFAULT_LIST_ID = "local"

# Serializes sync() within this process; the flock in _process_lock covers
# the app and the daemon both calling sync() at once.
_sync_lock = threading.Lock()


class OAuthError(Exception):
    """Auth/network/API failure -- callers treat this as "sync unavailable
    right now", not a fatal error; the local store is unaffected."""


def ensure_defaults():
    TASKS_DIR.mkdir(parents=True, exist_ok=True)
    if not STORE_FILE.exists():
        write_store({"lists": {DEFAULT_LIST_ID: {"id": DEFAULT_LIST_ID, "title": "Tasks"}}, "tasks": {}})
    if not QUEUE_FILE.exists():
        write_queue([])
    if not SYNC_FILE.exists():
        atomic_write_text(SYNC_FILE, json.dumps({}, indent=2))


def _read_json(path, default):
    if path.exists():
        try:
            return json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return default


def _now_iso():
    # Sub-second precision matters: _merge_remote_task does a raw string
    # compare of this against Google's millisecond `updated` timestamps to
    # decide whether a local edit is newer than an incoming pull. A fixed
    # ".000Z" always loses to any remote edit stamped within the same
    # second (".123Z" > ".000Z" lexicographically), so a genuinely newer
    # local edit could get silently clobbered by an in-flight pull.
    now = time.time()
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now)) + f".{int(now % 1 * 1000):03d}Z"


# -- local store --

def read_store():
    return _read_json(STORE_FILE, {"lists": {}, "tasks": {}})


def write_store(store):
    TASKS_DIR.mkdir(parents=True, exist_ok=True)
    atomic_write_text(STORE_FILE, json.dumps(store, indent=2))


def read_queue():
    return _read_json(QUEUE_FILE, [])


def write_queue(queue):
    TASKS_DIR.mkdir(parents=True, exist_ok=True)
    atomic_write_text(QUEUE_FILE, json.dumps(queue, indent=2))


def _enqueue(op):
    q = read_queue()
    q.append(op)
    write_queue(q)


# -- lists --

def lists():
    return list(read_store()["lists"].values())


# -- tasks: local mutation + queue, instant, no network --

def tasks_for_list(list_id):
    store = read_store()
    items = [t for t in store["tasks"].values() if t["list_id"] == list_id and not t.get("deleted")]
    items.sort(key=lambda t: (t.get("status") == "completed", t.get("due") or "9999", t.get("title", "")))
    return items


def add_task(list_id, title, notes="", due=None, parent=None):
    # Locked against sync(): add/update/delete each do a bare
    # read-modify-write of store.json plus an append to queue.json, and
    # atomic_write_text only prevents a *torn* file, not a *lost* update.
    # Without this lock, e.g. the daemon's _push reading the queue for a
    # slow network round trip while this appends a new op, then _push
    # writing back its (now stale) view, silently drops the new op --
    # forever, since nothing re-queues it. Taking the same flock sync()
    # uses serializes every mutation against every sync(), across threads
    # and processes alike (app, tray, CLI, daemon).
    with _process_lock():
        store = read_store()
        task_id = f"local-{uuid.uuid4().hex[:12]}"
        task = {
            "id": task_id, "list_id": list_id, "title": title, "notes": notes,
            "due": due, "parent": parent, "status": "needsAction",
            "updated": _now_iso(), "deleted": False,
        }
        store["tasks"][task_id] = task
        write_store(store)
        _enqueue({"op": "insert", "list_id": list_id, "task_id": task_id})
    return task


def update_task(task_id, **fields):
    with _process_lock():
        store = read_store()
        task = store["tasks"].get(task_id)
        if task is None:
            return None
        task.update(fields)
        task["updated"] = _now_iso()
        write_store(store)
        _enqueue({"op": "update", "list_id": task["list_id"], "task_id": task_id})
    return task


def set_done(task_id, done):
    return update_task(task_id, status="completed" if done else "needsAction")


def delete_task(task_id):
    with _process_lock():
        store = read_store()
        task = store["tasks"].get(task_id)
        if task is None:
            return
        task["deleted"] = True
        task["updated"] = _now_iso()
        write_store(store)
        _enqueue({"op": "delete", "list_id": task["list_id"], "task_id": task_id})


def due_today_count():
    today = time.strftime("%Y-%m-%d")
    store = read_store()
    return sum(
        1 for t in store["tasks"].values()
        if not t.get("deleted") and t.get("status") != "completed" and (t.get("due") or "").startswith(today)
    )


def status():
    return {"connected": is_connected(), "has_client": has_client(), "pending": len(read_queue())}


# -- OAuth (installed-app loopback flow, stdlib only) --

def has_client():
    return CLIENT_FILE.exists()


def save_client(client_id, client_secret):
    TASKS_DIR.mkdir(parents=True, exist_ok=True)
    atomic_write_text(CLIENT_FILE, json.dumps({"client_id": client_id, "client_secret": client_secret}, indent=2))


def is_connected():
    return TOKEN_FILE.exists()


def disconnect():
    try:
        TOKEN_FILE.unlink()
    except FileNotFoundError:
        pass


def _read_client():
    data = _read_json(CLIENT_FILE, None)
    if not data:
        raise OAuthError("no Google OAuth client configured -- add one on the Todo page first")
    return data


class _LoopbackHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        if not ({"code", "error", "state"} & params.keys()):
            # Browsers commonly prefetch /favicon.ico against whatever origin
            # is currently loaded, including this loopback redirect page --
            # that request has no OAuth params at all. Setting server.result
            # for it would end authorize_url_and_wait()'s wait loop early on
            # a bogus "result", making the real redirect's state check fail
            # a moment later ("Connect Google" failing intermittently).
            self.send_response(204)
            self.end_headers()
            return
        self.server.result = params
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(b"<html><body>hypr-util: you can close this tab.</body></html>")

    def log_message(self, fmt, *args):
        pass  # keep stdlib http.server quiet -- it logs to stderr by default


def authorize_url_and_wait(open_url_callback, timeout=180, cancel_event=None):
    """Run the OAuth loopback flow: start a local HTTP server on an
    ephemeral port, hand the consent URL to `open_url_callback` (kept
    separate from this module so it stays UI-agnostic -- the caller does
    e.g. `webbrowser.open` or `Gio.AppInfo.launch_default_for_uri`), then
    wait for Google's redirect and exchange the code for tokens. Call from
    a background thread; raises OAuthError on failure/timeout/state
    mismatch/cancellation.

    Waits in short (1s) slices rather than one `timeout`-long blocking
    call, so a `cancel_event` (a threading.Event set from another thread,
    e.g. a Cancel button) is noticed within about a second instead of only
    after the full timeout -- otherwise there would be no way to back out
    of a stuck/abandoned consent flow short of force-quitting the app."""
    client = _read_client()
    server = HTTPServer(("127.0.0.1", 0), _LoopbackHandler)
    server.result = None
    port = server.server_address[1]
    redirect_uri = f"http://127.0.0.1:{port}/"
    state = secrets.token_urlsafe(16)

    params = {
        "client_id": client["client_id"], "redirect_uri": redirect_uri,
        "response_type": "code", "scope": SCOPE, "access_type": "offline",
        "prompt": "consent", "state": state,
    }
    open_url_callback(f"{AUTH_ENDPOINT}?{urllib.parse.urlencode(params)}")

    try:
        server.timeout = 1
        deadline = time.time() + timeout
        while server.result is None:
            if cancel_event is not None and cancel_event.is_set():
                raise OAuthError("cancelled")
            if time.time() >= deadline:
                raise OAuthError("timed out waiting for Google's OAuth redirect")
            server.handle_request()
    finally:
        server.server_close()

    result = server.result
    if result.get("state", [None])[0] != state:
        raise OAuthError("OAuth state mismatch")
    if "error" in result:
        raise OAuthError(result["error"][0])

    data = urllib.parse.urlencode({
        "code": result["code"][0], "client_id": client["client_id"],
        "client_secret": client["client_secret"], "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
    }).encode()
    try:
        with urllib.request.urlopen(urllib.request.Request(TOKEN_ENDPOINT, data=data, method="POST"), timeout=15) as resp:
            token = json.loads(resp.read())
    except urllib.error.URLError as e:
        raise OAuthError(f"token exchange failed: {e}") from e
    token["obtained_at"] = time.time()
    TASKS_DIR.mkdir(parents=True, exist_ok=True)
    atomic_write_text(TOKEN_FILE, json.dumps(token, indent=2))
    return token


def _refresh_token(token):
    refresh_token = token.get("refresh_token")
    if not refresh_token:
        # A re-consent that omits refresh_token (e.g. Google only issues one
        # on the *first* consent for a given client+account) would otherwise
        # raise a bare KeyError here -- not an OAuthError, so it escapes
        # sync()'s except OAuthError and crashes whatever thread called it
        # (the daemon's _sync_worker). Surface it the same way every other
        # auth failure is: sync fails soft, and the user re-connects.
        raise OAuthError("re-authorization required")
    client = _read_client()
    data = urllib.parse.urlencode({
        "refresh_token": refresh_token, "client_id": client["client_id"],
        "client_secret": client["client_secret"], "grant_type": "refresh_token",
    }).encode()
    try:
        with urllib.request.urlopen(urllib.request.Request(TOKEN_ENDPOINT, data=data, method="POST"), timeout=15) as resp:
            refreshed = json.loads(resp.read())
    except urllib.error.URLError as e:
        raise OAuthError(f"token refresh failed: {e}") from e
    token.update(refreshed)
    token["obtained_at"] = time.time()
    atomic_write_text(TOKEN_FILE, json.dumps(token, indent=2))
    return token


def _access_token():
    token = _read_json(TOKEN_FILE, None)
    if not token:
        raise OAuthError("not connected to Google Tasks")
    if time.time() - token.get("obtained_at", 0) > token.get("expires_in", 3600) - 60:
        token = _refresh_token(token)
    return token["access_token"]


# -- REST --

def _api(method, path, body=None, params=None):
    url = f"{API_BASE}{path}"
    if params:
        url += f"?{urllib.parse.urlencode(params)}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {_access_token()}")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        raise OAuthError(f"Google Tasks API error {e.code}: {e.read().decode(errors='replace')}") from e
    except urllib.error.URLError as e:
        raise OAuthError(f"Google Tasks unreachable: {e}") from e


# -- sync (push local queue, pull remote deltas) --

@contextlib.contextmanager
def _process_lock():
    TASKS_DIR.mkdir(parents=True, exist_ok=True)
    with open(LOCK_FILE, "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def sync():
    """Push then pull. Safe to call from any thread/process -- the app and
    the daemon may both call this; the in-process lock plus a flock (so it
    also holds across processes) keep two syncs from interleaving their
    reads/writes of store.json/queue.json. No-ops quietly (returns
    ok: False) if not connected -- callers never need to check connectivity
    first, e.g. the daemon's periodic/network-up trigger."""
    if not is_connected():
        return {"ok": False, "reason": "not connected"}
    with _sync_lock, _process_lock():
        try:
            _sync_lists()
            _push()
            _pull_all()
        except OAuthError as e:
            return {"ok": False, "reason": str(e)}
    return {"ok": True}


def _remote_list_id(store, local_list_id):
    return store["lists"].get(local_list_id, {}).get("remote_id") or "@default"


def _sync_lists():
    result = _api("GET", "/users/@me/lists")
    remote_lists = result.get("items", [])
    if not remote_lists:
        return
    store = read_store()
    mapped = {l.get("remote_id") for l in store["lists"].values()}
    # First-time connect: point the pre-existing default local list at
    # Google's actual default list instead of creating a duplicate.
    default_local = store["lists"].get(DEFAULT_LIST_ID)
    if default_local and not default_local.get("remote_id"):
        default_local["remote_id"] = remote_lists[0]["id"]
        default_local["title"] = remote_lists[0]["title"]
        mapped.add(remote_lists[0]["id"])
    for rl in remote_lists:
        if rl["id"] in mapped:
            continue
        local_id = f"local-{uuid.uuid4().hex[:12]}"
        store["lists"][local_id] = {"id": local_id, "title": rl["title"], "remote_id": rl["id"]}
    write_store(store)


def _task_to_remote(task):
    body = {"title": task["title"], "notes": task.get("notes") or "", "status": task.get("status", "needsAction")}
    if task.get("due"):
        body["due"] = task["due"]
    return body


def _push():
    queue = read_queue()
    if not queue:
        return
    store = read_store()
    remaining = []
    for op in queue:
        task = store["tasks"].get(op["task_id"])
        remote_list_id = _remote_list_id(store, op["list_id"])
        try:
            if op["op"] == "insert":
                if task is None or task.get("remote_id"):
                    continue
                result = _api("POST", f"/lists/{remote_list_id}/tasks", body=_task_to_remote(task))
                task["remote_id"] = result["id"]
                if result.get("updated"):
                    # Adopt the server's own timestamp for this edit, not just
                    # our local one -- otherwise the next _pull_all sees its
                    # own just-pushed change come back with a remote
                    # `updated` that can be <= our local one only by luck of
                    # clock skew; storing it back keeps local/remote in
                    # lockstep so a later pull doesn't second-guess this push.
                    task["updated"] = result["updated"]
            elif op["op"] == "update":
                if task is None:
                    continue
                remote_id = task.get("remote_id")
                if not remote_id:
                    # This task's own insert op is still ahead of it in the
                    # queue (or failed) -- retry this update next sync().
                    remaining.append(op)
                    continue
                result = _api("PATCH", f"/lists/{remote_list_id}/tasks/{remote_id}", body=_task_to_remote(task))
                if result.get("updated"):
                    task["updated"] = result["updated"]
            elif op["op"] == "delete":
                remote_id = task.get("remote_id") if task else None
                if remote_id:
                    _api("DELETE", f"/lists/{remote_list_id}/tasks/{remote_id}")
                if task is not None:
                    del store["tasks"][op["task_id"]]
        except OAuthError:
            remaining.append(op)  # keep it queued, retry on the next sync()
    write_store(store)
    write_queue(remaining)


def _merge_remote_task(store, list_id, remote_task, by_remote_id):
    local = by_remote_id.get(remote_task["id"])
    if remote_task.get("deleted"):
        if local:
            local["deleted"] = True
        return
    if local is None:
        local_id = f"local-{uuid.uuid4().hex[:12]}"
        local = {"id": local_id, "list_id": list_id, "remote_id": remote_task["id"], "updated": ""}
        store["tasks"][local_id] = local
        by_remote_id[remote_task["id"]] = local
    if local.get("updated", "") > remote_task.get("updated", ""):
        return  # a local edit is newer than this pull -- next push wins, don't clobber it
    local.update({
        "title": remote_task.get("title", ""),
        "notes": remote_task.get("notes", ""),
        "status": remote_task.get("status", "needsAction"),
        "due": remote_task.get("due"),
        "updated": remote_task.get("updated", local["updated"]),
        "deleted": False,
    })


def _pull_all():
    store = read_store()
    sync_state = _read_json(SYNC_FILE, {})
    # Built once per pull rather than re-scanning every local task per remote
    # task inside _merge_remote_task (that was O(local count x remote count)).
    by_remote_id = {t["remote_id"]: t for t in store["tasks"].values() if t.get("remote_id")}
    for list_id, l in store["lists"].items():
        remote_list_id = l.get("remote_id") or "@default"
        params = {"showDeleted": "true", "showHidden": "true", "maxResults": "100"}
        updated_min = sync_state.get(list_id)
        if updated_min:
            params["updatedMin"] = updated_min
        result = _api("GET", f"/lists/{remote_list_id}/tasks", params=params)
        newest = updated_min
        for remote_task in result.get("items", []):
            _merge_remote_task(store, list_id, remote_task, by_remote_id)
            remote_updated = remote_task.get("updated", "")
            if remote_updated and (not newest or remote_updated > newest):
                newest = remote_updated
        if newest:
            sync_state[list_id] = newest
    write_store(store)
    atomic_write_text(SYNC_FILE, json.dumps(sync_state, indent=2))
