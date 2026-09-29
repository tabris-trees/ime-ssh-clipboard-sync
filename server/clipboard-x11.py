#!/usr/bin/python3
import argparse
import fcntl
import json
import os
from pathlib import Path
import signal
import select
import subprocess
import sys
import time
import hashlib
from typing import Dict, List, Optional, Tuple
from urllib.parse import quote, unquote, urlparse

from PIL import Image
from Xlib import X, Xatom
from Xlib import error as xerror
from Xlib.display import Display
from Xlib.protocol import event
from Xlib.ext import xfixes, randr


for _name in ("BadRROutputError", "BadRRCrtcError", "BadRRModeError"):
    _cls = getattr(randr, _name, None)
    if _cls is not None and not hasattr(_cls, "_fields"):
        setattr(randr, _name, type(_name, (xerror.XError,), {}))


SESSION_KEY = hashlib.sha256(os.environ.get("DISPLAY", "").encode()).hexdigest()[:16]
STATE_DIR = Path.home() / ".local/state/clipboard-x11/sessions" / SESSION_KEY
CACHE_DIR = Path.home() / ".cache/clipboard-x11/sessions" / SESSION_KEY
PAYLOAD_DIR = CACHE_DIR / "payloads"
WATCH_DIR = CACHE_DIR / "watch"
PID_FILE = STATE_DIR / "owner.pid"
MANIFEST_FILE = STATE_DIR / "current.json"
READY_FILE = STATE_DIR / "owner.ready"
LOG_FILE = STATE_DIR / "owner.log"
LOCK_FILE = STATE_DIR / "lock"
GENERATION_FILE = STATE_DIR / "watch.generation"
SUPPRESS_FILE = STATE_DIR / "suppress.json"
CLEANUP_STAMP_FILE = STATE_DIR / "cleanup.stamp"
XCLIP = str(Path.home() / "software/bin/xclip")
MAX_FILE_BYTES = 100 * 1024 * 1024
MAX_TEXT_BYTES = 4 * 1024 * 1024
MAX_IMAGE_BYTES = 32 * 1024 * 1024
INCR_CHUNK_BYTES = 60 * 1024
CACHE_TTL_SECONDS = 24 * 60 * 60
CLEANUP_INTERVAL_SECONDS = 24 * 60 * 60
MAX_OWNER_LOG_BYTES = 4 * 1024 * 1024
INBOX_ROOT = Path.home() / ".local/share/windows-clipboard-sync/inbox"


def ensure_env() -> None:
    if not os.environ.get("DISPLAY"):
        fail("missing_display", "DISPLAY is not set; connect with SSH X11 forwarding or set DISPLAY explicitly")
    if "XAUTHORITY" not in os.environ:
        xauth = Path.home() / ".Xauthority"
        if xauth.exists():
            os.environ["XAUTHORITY"] = str(xauth)


def ensure_dirs() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    CACHE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    PAYLOAD_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    WATCH_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        STATE_DIR.chmod(0o700)
        CACHE_DIR.chmod(0o700)
        PAYLOAD_DIR.chmod(0o700)
        WATCH_DIR.chmod(0o700)
    except OSError:
        pass


def cleanup_old_cache(force: bool = False) -> None:
    ensure_dirs()
    now = time.time()
    if not force:
        try:
            last = float(CLEANUP_STAMP_FILE.read_text().strip())
            if now - last < CLEANUP_INTERVAL_SECONDS:
                return
        except Exception:
            pass

    cutoff = now - CACHE_TTL_SECONDS
    current_payload = ""
    try:
        current_payload = str((load_current_manifest() or {}).get("payload", ""))
    except Exception:
        current_payload = ""

    for path in PAYLOAD_DIR.iterdir():
        try:
            if not path.is_file():
                continue
            if str(path) == current_payload:
                continue
            if path.stat().st_mtime < cutoff:
                path.unlink()
        except OSError as exc:
            log_event(f"cleanup payload_failed path={path} error={exc}")

    for path in WATCH_DIR.iterdir():
        try:
            if not path.is_dir():
                continue
            if path.stat().st_mtime >= cutoff:
                continue
            for child in path.iterdir():
                if child.is_file():
                    child.unlink()
            path.rmdir()
        except OSError as exc:
            log_event(f"cleanup watch_failed path={path} error={exc}")

    try:
        if LOG_FILE.exists() and LOG_FILE.stat().st_size > MAX_OWNER_LOG_BYTES:
            data = LOG_FILE.read_bytes()[-MAX_OWNER_LOG_BYTES:]
            LOG_FILE.write_bytes(data)
            LOG_FILE.chmod(0o600)
    except OSError:
        pass

    CLEANUP_STAMP_FILE.write_text(f"{now}\n")
    CLEANUP_STAMP_FILE.chmod(0o600)


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime())


def log_event(message: str) -> None:
    print(f"{now_iso()} {message}", file=sys.stderr, flush=True)


def log_watch(message: str) -> None:
    print(f"{now_iso()} watch {message}", file=sys.stderr, flush=True)


def json_line(payload: Dict[str, object]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def fail(error: str, message: str = "", code: int = 1, **extra) -> None:
    payload: Dict[str, object] = {"ok": False, "error": error}
    if message:
        payload["message"] = message
    payload.update(extra)
    json_line(payload)
    raise SystemExit(code)


def fail_stderr(error: str, message: str, code: int = 1) -> None:
    print(json.dumps({"ok": False, "error": error, "message": message}, ensure_ascii=False, separators=(",", ":")), file=sys.stderr)
    raise SystemExit(code)


def read_pid() -> Optional[int]:
    try:
        return int(PID_FILE.read_text().strip())
    except Exception:
        return None


def process_alive(pid: Optional[int]) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def kill_old_owner() -> None:
    pid = read_pid()
    if not process_alive(pid):
        return
    assert pid is not None
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return
    for _ in range(30):
        if not process_alive(pid):
            return
        time.sleep(0.05)
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass


def write_payload_bytes(kind: str, data: bytes, extra: Optional[dict] = None) -> Path:
    ensure_dirs()
    stamp = f"{int(time.time() * 1000)}-{os.getpid()}"
    payload = PAYLOAD_DIR / f"{stamp}.{kind}"
    payload.write_bytes(data)
    payload.chmod(0o600)
    manifest = {
        "kind": kind,
        "payload": str(payload),
        "display": os.environ.get("DISPLAY", ""),
        "xauthority": os.environ.get("XAUTHORITY", ""),
        "created": time.time(),
        "updated_at": now_iso(),
    }
    if extra:
        manifest.update(extra)
    manifest_path = PAYLOAD_DIR / f"{stamp}.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    manifest_path.chmod(0o600)
    return manifest_path


def next_generation() -> int:
    ensure_dirs()
    try:
        current = int(GENERATION_FILE.read_text().strip())
    except Exception:
        current = 0
    generation = current + 1
    GENERATION_FILE.write_text(f"{generation}\n")
    GENERATION_FILE.chmod(0o600)
    return generation


def generation_dir(generation: int) -> Path:
    return WATCH_DIR / str(generation)


def read_suppressions() -> List[dict]:
    try:
        data = json.loads(SUPPRESS_FILE.read_text())
        if isinstance(data, list):
            return data
    except Exception:
        pass
    return []


def write_suppressions(items: List[dict]) -> None:
    ensure_dirs()
    SUPPRESS_FILE.write_text(json.dumps(items[-200:], ensure_ascii=False, indent=2))
    SUPPRESS_FILE.chmod(0o600)


def add_suppression(kind: str, digest: str) -> None:
    items = read_suppressions()
    items.append({"type": kind, "sha256": digest, "created": time.time(), "origin": "windows"})
    write_suppressions(items)


def is_suppressed(kind: str, digest: str) -> bool:
    now = time.time()
    active = []
    matched = False
    for item in read_suppressions():
        if now - float(item.get("created", 0)) <= 300:
            if item.get("type") == kind and item.get("sha256") == digest:
                matched = True
                continue
            active.append(item)
    if matched:
        write_suppressions(active)
    return matched


def event_digest(kind: str, payload: bytes) -> str:
    return hashlib.sha256(kind.encode("utf-8") + b"\0" + payload).hexdigest()


def files_watch_payload(paths: List[Path]) -> bytes:
    files = []
    for real in paths:
        stat = real.stat()
        files.append({"path": str(real), "size": stat.st_size, "mtime": stat.st_mtime})
    return json.dumps({"files": files}, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def path_to_file_uri(path: Path) -> str:
    return "file://" + quote(str(path), safe="/:@")


def validate_existing_files(paths: List[str]) -> Tuple[List[Path], List[str]]:
    if not paths:
        raise SystemExit("clipboard-x11-files requires at least one file")
    real_paths: List[Path] = []
    uris: List[str] = []
    for item in paths:
        if not item.startswith("/"):
            raise SystemExit(f"not an absolute path: {item}")
        path = Path(item).resolve(strict=True)
        if not path.is_file():
            raise SystemExit(f"not a regular file: {path}")
        size = path.stat().st_size
        if size > MAX_FILE_BYTES:
            raise SystemExit(f"file exceeds 100 MiB limit: {path} ({size} bytes)")
        real_paths.append(path)
        uris.append(path_to_file_uri(path))
    return real_paths, uris


def validate_inbox_manifest(data: bytes) -> Tuple[str, List[Path], List[str]]:
    try:
        manifest = json.loads(data.decode("utf-8"))
    except UnicodeDecodeError as exc:
        fail("invalid_utf8", "manifest is not valid UTF-8", detail=str(exc))
    except json.JSONDecodeError as exc:
        fail("invalid_manifest", "stdin is not valid JSON", detail=str(exc))
    if not isinstance(manifest, dict):
        fail("invalid_manifest", "manifest must be a JSON object")
    operation = manifest.get("operation")
    if operation != "copy":
        fail("invalid_manifest", "only operation='copy' is supported")
    paths = manifest.get("paths")
    if not isinstance(paths, list):
        fail("invalid_manifest", "paths must be an array")
    if not paths:
        fail("invalid_manifest", "paths must not be empty")
    if len(paths) > 1000:
        fail("invalid_manifest", "too many paths", limit=1000)

    inbox_root = INBOX_ROOT.resolve(strict=False)
    real_paths: List[Path] = []
    uris: List[str] = []
    for item in paths:
        if not isinstance(item, str):
            fail("invalid_path", "each path must be a string")
        if not item.startswith("/"):
            fail("invalid_path", "path is not absolute")
        try:
            real = Path(item).resolve(strict=True)
        except FileNotFoundError:
            fail("file_not_found", "file does not exist")
        except OSError as exc:
            fail("invalid_path", "cannot canonicalize path", detail=str(exc))
        try:
            real.relative_to(inbox_root)
        except ValueError:
            fail("path_outside_inbox", "canonical path is outside the allowed inbox")
        if not real.is_file():
            fail("invalid_path", "path is not a regular file")
        stat = real.stat()
        if stat.st_uid != os.getuid():
            fail("invalid_path", "file is not owned by current user")
        if stat.st_size > MAX_FILE_BYTES:
            fail("payload_too_large", "file exceeds 100 MiB limit", limit=MAX_FILE_BYTES)
        real_paths.append(real)
        uris.append(path_to_file_uri(real))
    return operation, real_paths, uris


def get_current_owner_xid() -> int:
    ensure_env()
    try:
        display = Display(os.environ["DISPLAY"])
        clipboard = display.intern_atom("CLIPBOARD", only_if_exists=False)
        owner = display.get_selection_owner(clipboard)
        return int(owner.id) if owner else 0
    except Exception:
        return 0


def load_current_manifest() -> Optional[dict]:
    try:
        return json.loads(MANIFEST_FILE.read_text())
    except Exception:
        return None


def manifest_owned(manifest: Optional[dict] = None) -> bool:
    manifest = manifest or load_current_manifest()
    if not manifest:
        return False
    pid = manifest.get("owner_pid")
    xid = manifest.get("owner_xid")
    if not isinstance(pid, int) or not isinstance(xid, int):
        return False
    if not process_alive(pid):
        return False
    return get_current_owner_xid() == xid


def is_deduplicated(kind: str, digest: str) -> bool:
    manifest = load_current_manifest()
    return bool(
        manifest
        and manifest.get("kind") == kind
        and manifest.get("sha256") == digest
        and manifest_owned(manifest)
    )


def replace_owner(manifest_path: Path) -> dict:
    ensure_env()
    ensure_dirs()
    try:
        READY_FILE.unlink()
    except FileNotFoundError:
        pass
    kill_old_owner()
    with LOG_FILE.open("ab", buffering=0) as log:
        proc = subprocess.Popen(
            ["/usr/bin/python3", __file__, "_owner", str(manifest_path), str(READY_FILE)],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            close_fds=True,
            start_new_session=True,
            env=os.environ.copy(),
        )
    PID_FILE.write_text(f"{proc.pid}\n")
    PID_FILE.chmod(0o600)
    for _ in range(100):
        if READY_FILE.exists() and process_alive(proc.pid):
            manifest = json.loads(manifest_path.read_text())
            try:
                ready = json.loads(READY_FILE.read_text())
            except Exception:
                ready = {"owner_pid": proc.pid}
            manifest["owner_pid"] = int(ready.get("owner_pid", proc.pid))
            manifest["owner_xid"] = int(ready.get("owner_xid", 0))
            manifest["owned"] = True
            MANIFEST_FILE.write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
            MANIFEST_FILE.chmod(0o600)
            return manifest
        if proc.poll() is not None:
            fail("daemon_start_failed", f"clipboard owner exited early, see {LOG_FILE}")
        time.sleep(0.05)
    fail("timeout", f"clipboard owner did not become ready, see {LOG_FILE}")


class ClipboardOwner:
    def __init__(self, manifest_path: Path, ready_path: Path):
        self.manifest = json.loads(manifest_path.read_text())
        self.ready_path = ready_path
        self.kind = self.manifest["kind"]
        self.targets, self.data = self._load_targets()
        self.display = Display(os.environ["DISPLAY"])
        self.screen = self.display.screen()
        self.window = self.screen.root.create_window(0, 0, 1, 1, 0, X.CopyFromParent)
        self.atoms: Dict[str, int] = {}
        for name in [
            "CLIPBOARD",
            "TARGETS",
            "TIMESTAMP",
            "ATOM",
            "INCR",
            "UTF8_STRING",
            "TEXT",
            "STRING",
            "text/plain;charset=utf-8",
            "text/plain",
            "image/png",
            "text/uri-list",
            "x-special/gnome-copied-files",
        ] + self.targets:
            self.atom(name)
        self.incr_transfers: Dict[Tuple[int, int], Dict[str, object]] = {}

    def mark_lost_ownership(self) -> None:
        try:
            manifest = json.loads(MANIFEST_FILE.read_text())
            if manifest.get("owner_pid") == os.getpid():
                manifest["owned"] = False
                manifest["lost_ownership_at"] = now_iso()
                MANIFEST_FILE.write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
                MANIFEST_FILE.chmod(0o600)
        except Exception as exc:
            log_event(f"ownership_lost_status_update_failed error={exc}")

    def _load_targets(self) -> Tuple[List[str], Dict[str, bytes]]:
        if self.kind == "text":
            text = Path(self.manifest["payload"]).read_bytes()
            return (
                ["UTF8_STRING", "text/plain;charset=utf-8", "text/plain", "TEXT", "STRING"],
                {
                    "UTF8_STRING": text,
                    "text/plain;charset=utf-8": text,
                    "text/plain": text,
                    "TEXT": text,
                    "STRING": text,
                },
            )
        if self.kind == "png":
            data = Path(self.manifest["payload"]).read_bytes()
            return ["image/png"], {"image/png": data}
        if self.kind == "files":
            uri_list = self.manifest["uri_list"].encode("utf-8")
            gnome = self.manifest["gnome_copied_files"].encode("utf-8")
            return (
                ["text/uri-list", "x-special/gnome-copied-files", "UTF8_STRING", "text/plain"],
                {
                    "text/uri-list": uri_list,
                    "x-special/gnome-copied-files": gnome,
                    "UTF8_STRING": uri_list,
                    "text/plain": uri_list,
                },
            )
        raise SystemExit(f"unknown clipboard kind: {self.kind}")

    def atom(self, name: str) -> int:
        if name not in self.atoms:
            self.atoms[name] = self.display.intern_atom(name, only_if_exists=False)
        return self.atoms[name]

    def atom_name(self, atom_id: int) -> str:
        try:
            return self.display.get_atom_name(atom_id)
        except Exception:
            return str(atom_id)

    def send_selection_notify(self, req, property_atom: int) -> None:
        notify = event.SelectionNotify(
            time=req.time,
            requestor=req.requestor,
            selection=req.selection,
            target=req.target,
            property=property_atom,
        )
        req.requestor.send_event(notify, propagate=False)
        self.display.flush()

    def serve_targets(self, req, prop: int) -> int:
        target_atoms = [self.atom("TARGETS"), self.atom("TIMESTAMP")]
        target_atoms.extend(self.atom(name) for name in self.targets)
        req.requestor.change_property(prop, self.atom("ATOM"), 32, target_atoms)
        return prop

    def serve_data(self, req, target_name: str, prop: int) -> int:
        data = self.data[target_name]
        if len(data) > INCR_CHUNK_BYTES:
            return self.start_incr_transfer(req, target_name, prop, data)
        req.requestor.change_property(prop, self.atom(target_name), 8, data)
        return prop

    def start_incr_transfer(self, req, target_name: str, prop: int, data: bytes) -> int:
        try:
            req.requestor.change_attributes(event_mask=X.PropertyChangeMask)
        except Exception as exc:
            print(f"failed to select property events for INCR target {target_name}: {exc}", file=sys.stderr, flush=True)
        req.requestor.change_property(prop, self.atom("INCR"), 32, [len(data)])
        self.incr_transfers[(int(req.requestor.id), int(prop))] = {
            "requestor": req.requestor,
            "property": int(prop),
            "target_atom": self.atom(target_name),
            "target_name": target_name,
            "data": data,
            "offset": 0,
        }
        return prop

    def continue_incr_transfer(self, ev) -> None:
        if getattr(ev, "state", None) != X.PropertyDelete:
            return
        transfer = self.incr_transfers.get((int(ev.window.id), int(ev.atom)))
        if not transfer:
            return
        requestor = transfer["requestor"]
        prop = int(transfer["property"])
        target_atom = int(transfer["target_atom"])
        data = transfer["data"]
        offset = int(transfer["offset"])
        assert isinstance(data, bytes)
        chunk = data[offset:offset + INCR_CHUNK_BYTES]
        requestor.change_property(prop, target_atom, 8, chunk)
        transfer["offset"] = offset + len(chunk)
        if not chunk:
            self.incr_transfers.pop((int(ev.window.id), int(ev.atom)), None)
        self.display.flush()

    def handle_selection_request(self, req) -> None:
        prop = req.property
        if prop == X.NONE:
            prop = req.target
        target_name = self.atom_name(req.target)
        property_to_report = X.NONE
        try:
            if target_name == "TARGETS":
                property_to_report = self.serve_targets(req, prop)
            elif target_name == "TIMESTAMP":
                req.requestor.change_property(prop, Xatom.INTEGER, 32, [int(self.manifest["created"])])
                property_to_report = prop
            elif target_name in self.data:
                property_to_report = self.serve_data(req, target_name, prop)
        except Exception as exc:
            print(f"failed to serve target {target_name}: {exc}", file=sys.stderr, flush=True)
            property_to_report = X.NONE
        self.send_selection_notify(req, property_to_report)

    def run(self):
        clipboard = self.atom("CLIPBOARD")
        self.window.set_selection_owner(clipboard, X.CurrentTime)
        self.display.sync()
        if self.display.get_selection_owner(clipboard) != self.window:
            raise SystemExit("failed to own X11 CLIPBOARD")
        self.ready_path.write_text(json.dumps({"owner_pid": os.getpid(), "owner_xid": int(self.window.id)}))
        self.ready_path.chmod(0o600)
        signal.signal(signal.SIGTERM, lambda *_args: sys.exit(0))
        while True:
            ev = self.display.next_event()
            if ev.type == X.SelectionRequest:
                self.handle_selection_request(ev)
            elif ev.type == X.PropertyNotify:
                self.continue_incr_transfer(ev)
            elif ev.type == X.SelectionClear:
                self.mark_lost_ownership()


def cmd_text(_args) -> None:
    ensure_env()
    data = sys.stdin.buffer.read()
    if len(data) > MAX_TEXT_BYTES:
        fail("payload_too_large", "text payload exceeds 4 MiB limit", limit=MAX_TEXT_BYTES)
    try:
        data.decode("utf-8")
    except UnicodeDecodeError as exc:
        fail("invalid_utf8", "stdin is not valid UTF-8", detail=str(exc))
    digest = hashlib.sha256(data).hexdigest()
    if is_deduplicated("text", digest):
        log_event(f"type=text bytes={len(data)} sha256={digest} deduplicated=true")
        json_line({"ok": True, "type": "text", "bytes": len(data), "sha256": digest, "owned": True, "deduplicated": True})
        return
    manifest = write_payload_bytes("text", data, {"sha256": digest, "bytes": len(data), "type": "text"})
    owned_manifest = replace_owner(manifest)
    add_suppression("text", event_digest("text", data))
    log_event(f"type=text bytes={len(data)} sha256={digest} success=true")
    json_line({"ok": True, "type": "text", "bytes": len(data), "sha256": digest, "owned": manifest_owned(owned_manifest)})


def decode_png_dimensions(data: bytes) -> Tuple[int, int]:
    from io import BytesIO

    try:
        with Image.open(BytesIO(data)) as img:
            if img.format != "PNG":
                fail("invalid_png", "stdin is not a PNG image")
            img.verify()
        with Image.open(BytesIO(data)) as img:
            return int(img.width), int(img.height)
    except SystemExit:
        raise
    except Exception as exc:
        fail("invalid_png", "PNG decode failed", detail=str(exc))


def cmd_png(_args) -> None:
    ensure_env()
    data = sys.stdin.buffer.read()
    if len(data) > MAX_IMAGE_BYTES:
        fail("payload_too_large", "image payload exceeds 32 MiB limit", limit=MAX_IMAGE_BYTES)
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        fail("invalid_png", "stdin is not a PNG stream")
    width, height = decode_png_dimensions(data)
    digest = hashlib.sha256(data).hexdigest()
    if is_deduplicated("png", digest):
        log_event(f"type=image bytes={len(data)} sha256={digest} deduplicated=true")
        json_line({
            "ok": True,
            "type": "image",
            "bytes": len(data),
            "sha256": digest,
            "width": width,
            "height": height,
            "mime": "image/png",
            "owned": True,
            "deduplicated": True,
        })
        return
    manifest = write_payload_bytes(
        "png",
        data,
        {"sha256": digest, "bytes": len(data), "type": "image", "width": width, "height": height, "mime": "image/png"},
    )
    owned_manifest = replace_owner(manifest)
    add_suppression("png", event_digest("png", data))
    log_event(f"type=image bytes={len(data)} sha256={digest} success=true")
    json_line({
        "ok": True,
        "type": "image",
        "bytes": len(data),
        "sha256": digest,
        "width": width,
        "height": height,
        "mime": "image/png",
        "owned": manifest_owned(owned_manifest),
    })


def cmd_files(args) -> None:
    ensure_env()
    if args.files:
        real_paths, uris = validate_existing_files(args.files)
        operation = "copy"
    else:
        operation, real_paths, uris = validate_inbox_manifest(sys.stdin.buffer.read())
    uri_list = "\r\n".join(uris) + "\r\n"
    gnome = "copy\n" + "\n".join(uris) + "\n"
    digest_payload = json.dumps({"operation": operation, "uris": uris}, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    digest = hashlib.sha256(digest_payload).hexdigest()
    if is_deduplicated("files", digest):
        log_event(f"type=files count={len(real_paths)} sha256={digest} deduplicated=true")
        json_line({
            "ok": True,
            "type": "files",
            "count": len(real_paths),
            "sha256": digest,
            "operation": operation,
            "owned": True,
            "deduplicated": True,
        })
        return
    manifest = write_payload_bytes(
        "files",
        "\n".join(str(p) for p in real_paths).encode("utf-8"),
        {
            "sha256": digest,
            "bytes": len(digest_payload),
            "type": "files",
            "files": [str(p) for p in real_paths],
            "uris": uris,
            "uri_list": uri_list,
            "gnome_copied_files": gnome,
            "operation": operation,
            "count": len(real_paths),
        },
    )
    owned_manifest = replace_owner(manifest)
    add_suppression("files", event_digest("files", files_watch_payload(real_paths)))
    log_event(f"type=files count={len(real_paths)} sha256={digest} success=true")
    json_line({
        "ok": True,
        "type": "files",
        "count": len(real_paths),
        "sha256": digest,
        "operation": operation,
        "owned": manifest_owned(owned_manifest),
    })


def run_xclip(target: str, timeout: float = 2.0) -> bytes:
    ensure_env()
    proc = subprocess.run(
        [XCLIP, "-selection", "clipboard", "-o", "-target", target],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        env=os.environ.copy(),
    )
    if proc.returncode != 0:
        return b""
    return proc.stdout


def read_targets() -> List[str]:
    targets_blob = run_xclip("TARGETS")
    return [line for line in targets_blob.decode("utf-8", "replace").splitlines() if line]


def parse_file_uris(data: bytes, target: str) -> List[str]:
    text = data.decode("utf-8", "replace").replace("\r\n", "\n")
    lines = [line.strip() for line in text.split("\n") if line.strip() and not line.startswith("#")]
    if target == "x-special/gnome-copied-files" and lines and lines[0] in {"copy", "cut"}:
        lines = lines[1:]
    paths: List[str] = []
    for line in lines:
        parsed = urlparse(line)
        if parsed.scheme != "file":
            continue
        if parsed.netloc not in {"", "localhost"}:
            continue
        path = Path(unquote(parsed.path)).resolve(strict=False)
        paths.append(str(path))
    return paths


def validate_stream_files(paths: List[str]) -> List[dict]:
    result = []
    for item in paths:
        path = Path(item)
        try:
            real = path.resolve(strict=True)
        except Exception as exc:
            raise ValueError(f"missing file: {item}: {exc}")
        if not real.is_file():
            raise ValueError(f"not a regular file: {real}")
        size = real.stat().st_size
        if size > MAX_FILE_BYTES:
            raise ValueError(f"file exceeds 100 MiB limit: {real} ({size} bytes)")
        result.append({"path": str(real), "size": size, "mtime": real.stat().st_mtime})
    return result


def detect_clipboard_payload() -> Optional[Tuple[str, bytes, Dict[str, object]]]:
    targets = read_targets()
    target_set = set(targets)
    metadata: Dict[str, object] = {"targets": targets}

    if "x-special/gnome-copied-files" in target_set:
        data = run_xclip("x-special/gnome-copied-files")
        if data:
            paths = parse_file_uris(data, "x-special/gnome-copied-files")
            files = validate_stream_files(paths)
            payload = json.dumps({"files": files}, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            metadata["format"] = "x-special/gnome-copied-files"
            metadata["file_count"] = len(files)
            return "files", payload, metadata

    if "text/uri-list" in target_set:
        data = run_xclip("text/uri-list")
        if data:
            paths = parse_file_uris(data, "text/uri-list")
            files = validate_stream_files(paths)
            payload = json.dumps({"files": files}, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            metadata["format"] = "text/uri-list"
            metadata["file_count"] = len(files)
            return "files", payload, metadata

    if "image/png" in target_set:
        data = run_xclip("image/png", timeout=5.0)
        if data:
            metadata["format"] = "image/png"
            return "png", data, metadata

    for target in ["UTF8_STRING", "text/plain;charset=utf-8", "text/plain"]:
        if target in target_set:
            data = run_xclip(target)
            if data:
                metadata["format"] = target
                return "text", data, metadata

    return None


def cache_detected_clipboard() -> Optional[dict]:
    detected = detect_clipboard_payload()
    if not detected:
        return None
    kind, payload, metadata = detected
    digest = event_digest(kind, payload)
    if is_suppressed(kind, digest):
        log_watch(f"suppressed type={kind} sha256={digest}")
        return {"suppressed": True, "type": kind, "sha256": digest}
    generation = next_generation()
    gdir = generation_dir(generation)
    gdir.mkdir(parents=True, exist_ok=False, mode=0o700)
    gdir.chmod(0o700)
    if kind == "text":
        payload_path = gdir / "text.utf8"
        payload_path.write_bytes(payload)
        payload_path.chmod(0o600)
        event_type = "text"
        event_size = len(payload)
    elif kind == "png":
        payload_path = gdir / "image.png"
        payload_path.write_bytes(payload)
        payload_path.chmod(0o600)
        event_type = "image"
        event_size = len(payload)
    elif kind == "files":
        payload_path = gdir / "files.json"
        payload_path.write_bytes(payload)
        payload_path.chmod(0o600)
        event_type = "files"
        event_size = len(payload)
    else:
        return None
    event_payload = {
        "ok": True,
        "event": "clipboard",
        "generation": generation,
        "type": event_type,
        "size": event_size,
        "sha256": digest,
        "token": str(generation),
        "origin": "linux",
        "source": "linux-x11",
        "created_at": now_iso(),
        "targets": metadata.get("targets", []),
        "format": metadata.get("format"),
    }
    if event_type == "files":
        try:
            files_doc = json.loads(payload.decode("utf-8"))
            files = files_doc.get("files", [])
            event_payload["count"] = len(files)
            event_payload["total_size"] = sum(int(f.get("size", 0)) for f in files)
        except Exception:
            pass
    manifest = dict(event_payload)
    manifest["payload"] = str(payload_path)
    manifest_path = gdir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    manifest_path.chmod(0o600)
    log_watch(f"cached generation={generation} type={event_type} size={event_size} sha256={digest}")
    return event_payload


def load_generation_manifest(generation_text: str) -> dict:
    try:
        generation = int(generation_text)
    except ValueError:
        fail_stderr("invalid_generation", "generation must be an integer")
    manifest_path = generation_dir(generation) / "manifest.json"
    if not manifest_path.exists():
        fail_stderr("generation_not_found", "cached clipboard generation was not found")
    try:
        return json.loads(manifest_path.read_text())
    except Exception as exc:
        fail_stderr("internal_error", f"cannot read generation manifest: {exc}")


def read_generation_payload(generation_text: str, expected_type: str) -> bytes:
    manifest = load_generation_manifest(generation_text)
    if manifest.get("type") != expected_type:
        fail_stderr("wrong_type", f"generation type is {manifest.get('type')}, not {expected_type}")
    payload_path = Path(str(manifest.get("payload", "")))
    try:
        return payload_path.read_bytes()
    except Exception as exc:
        fail_stderr("generation_not_found", f"cached payload is unavailable: {exc}")


def write_frame(kind: str, payload: bytes, metadata: Dict[str, object], last_hash: Optional[str]) -> Optional[str]:
    digest = event_digest(kind, payload)
    if digest == last_hash:
        return last_hash
    header = {
        "protocol": "clipboard-x11-stream-v1",
        "type": kind,
        "length": len(payload),
        "sha256": digest,
        "timestamp": time.time(),
        "source": "linux-x11",
        "origin": os.environ.get("CLIPBOARD_X11_STREAM_ORIGIN", "linux-x11"),
    }
    header.update(metadata)
    header_bytes = json.dumps(header, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
    sys.stdout.buffer.write(header_bytes)
    sys.stdout.buffer.write(payload)
    sys.stdout.buffer.flush()
    print(f"emitted {kind} length={len(payload)} sha256={digest}", file=sys.stderr, flush=True)
    return digest


def current_clipboard_hash() -> Optional[str]:
    try:
        detected = detect_clipboard_payload()
        if not detected:
            return None
        kind, payload, _metadata = detected
        return event_digest(kind, payload)
    except Exception as exc:
        print(f"failed to fingerprint initial clipboard: {exc}", file=sys.stderr, flush=True)
        return None


def emit_current_clipboard(last_hash: Optional[str]) -> Optional[str]:
    try:
        detected = detect_clipboard_payload()
        if not detected:
            print("clipboard has no supported payload", file=sys.stderr, flush=True)
            return last_hash
        kind, payload, metadata = detected
        return write_frame(kind, payload, metadata, last_hash)
    except Exception as exc:
        print(f"failed to read clipboard: {exc}", file=sys.stderr, flush=True)
        return last_hash


def cmd_status(_args) -> None:
    ensure_env()
    pid = read_pid()
    running = process_alive(pid)
    if not running:
        json_line({"ok": False, "running": False, "owned": False, "error": "daemon_not_running"})
        raise SystemExit(1)
    manifest = load_current_manifest() or {}
    owned = manifest_owned(manifest)
    targets = read_targets()
    checks = {}
    for target in [
        "UTF8_STRING",
        "text/plain",
        "image/png",
        "text/uri-list",
        "x-special/gnome-copied-files",
    ]:
        present = target in targets
        data = run_xclip(target) if present else b""
        checks[target] = {"present": present, "bytes": len(data)}
    kind = manifest.get("kind")
    api_type = manifest.get("type")
    if not api_type and kind == "png":
        api_type = "image"
    payload = {
        "ok": True,
        "running": True,
        "owned": owned,
        "type": api_type or kind,
        "bytes": manifest.get("bytes"),
        "sha256": manifest.get("sha256"),
        "updated_at": manifest.get("updated_at"),
        "owner_pid": pid,
        "display": os.environ.get("DISPLAY", ""),
        "xauthority": os.environ.get("XAUTHORITY", ""),
        "targets": targets,
        "checks": checks,
    }
    json_line(payload)


def cmd_stream(args) -> None:
    ensure_env()
    display = Display(os.environ["DISPLAY"])
    screen = display.screen()
    window = screen.root.create_window(0, 0, 1, 1, 0, X.CopyFromParent)
    clipboard = display.intern_atom("CLIPBOARD", only_if_exists=False)
    try:
        version = display.xfixes_query_version()
        print(f"XFixes version={version.major_version}.{version.minor_version}", file=sys.stderr, flush=True)
    except Exception as exc:
        print(f"XFixes query failed, fallback owner checks remain active: {exc}", file=sys.stderr, flush=True)
    mask = (
        xfixes.XFixesSetSelectionOwnerNotifyMask
        | xfixes.XFixesSelectionWindowDestroyNotifyMask
        | xfixes.XFixesSelectionClientCloseNotifyMask
    )
    try:
        display.xfixes_select_selection_input(window, clipboard, mask)
        display.flush()
        print("XFixes selection notifications enabled", file=sys.stderr, flush=True)
    except Exception as exc:
        print(f"XFixes selection notifications failed, using fallback owner checks: {exc}", file=sys.stderr, flush=True)
    print(f"listening DISPLAY={os.environ['DISPLAY']} selection=CLIPBOARD", file=sys.stderr, flush=True)

    last_hash: Optional[str] = None
    last_owner = display.get_selection_owner(clipboard)
    last_owner_id = int(last_owner.id) if last_owner else 0
    if args.initial:
        last_hash = emit_current_clipboard(last_hash)
    else:
        last_hash = current_clipboard_hash()
        if last_hash:
            print(f"initial clipboard fingerprint sha256={last_hash}", file=sys.stderr, flush=True)

    running = True

    def stop(_signum, _frame):
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    while running:
        ready, _, _ = select.select([display.fileno()], [], [], 0.5)
        if ready:
            ev = display.next_event()
            print(f"xfixes event={ev.__class__.__name__} data={getattr(ev, '_data', {})}", file=sys.stderr, flush=True)
            time.sleep(0.05)
            last_hash = emit_current_clipboard(last_hash)
            owner = display.get_selection_owner(clipboard)
            last_owner_id = int(owner.id) if owner else 0
            continue
        owner = display.get_selection_owner(clipboard)
        owner_id = int(owner.id) if owner else 0
        if owner_id != last_owner_id:
            print(f"selection owner changed {last_owner_id}->{owner_id}", file=sys.stderr, flush=True)
            last_owner_id = owner_id
        # Some X servers expose XFixes but do not deliver selection notifications
        # reliably over forwarded displays. This low-frequency content check is
        # deduplicated by SHA-256 and keeps the stream correct without busy polling.
        last_hash = emit_current_clipboard(last_hash)


def cmd_watch(args) -> None:
    ensure_env()
    display = Display(os.environ["DISPLAY"])
    screen = display.screen()
    window = screen.root.create_window(0, 0, 1, 1, 0, X.CopyFromParent)
    clipboard = display.intern_atom("CLIPBOARD", only_if_exists=False)
    try:
        display.xfixes_query_version()
        mask = (
            xfixes.XFixesSetSelectionOwnerNotifyMask
            | xfixes.XFixesSelectionWindowDestroyNotifyMask
            | xfixes.XFixesSelectionClientCloseNotifyMask
        )
        display.xfixes_select_selection_input(window, clipboard, mask)
        display.flush()
        log_watch(f"xfixes=enabled display={os.environ['DISPLAY']}")
    except Exception as exc:
        log_watch(f"xfixes=unavailable fallback=content-poll error={exc}")

    last_hash = current_clipboard_hash()
    if last_hash:
        log_watch(f"initial_fingerprint sha256={last_hash}")
    running = True

    def stop(_signum, _frame):
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    while running:
        ready, _, _ = select.select([display.fileno()], [], [], 0.5)
        if ready:
            try:
                ev = display.next_event()
                log_watch(f"xfixes_event={ev.__class__.__name__}")
            except Exception as exc:
                log_watch(f"xfixes_event_error={exc}")
            time.sleep(0.05)
        try:
            detected = detect_clipboard_payload()
            if not detected:
                continue
            kind, payload, _metadata = detected
            digest = event_digest(kind, payload)
            if digest == last_hash:
                continue
            last_hash = digest
            event_payload = cache_detected_clipboard()
            if event_payload and not event_payload.get("suppressed"):
                json_line(event_payload)
        except Exception as exc:
            log_watch(f"read_error={exc}")


def cmd_read_text(args) -> None:
    data = read_generation_payload(args.generation, "text")
    sys.stdout.buffer.write(data)
    sys.stdout.buffer.flush()


def cmd_read_image(args) -> None:
    data = read_generation_payload(args.generation, "image")
    sys.stdout.buffer.write(data)
    sys.stdout.buffer.flush()


def cmd_read_files(args) -> None:
    data = read_generation_payload(args.generation, "files")
    # Payload is already compact UTF-8 JSON.
    sys.stdout.buffer.write(data)
    if not data.endswith(b"\n"):
        sys.stdout.buffer.write(b"\n")
    sys.stdout.buffer.flush()


def cmd_ack(args) -> None:
    manifest = load_generation_manifest(args.generation)
    generation = int(args.generation)
    gdir = generation_dir(generation)
    for child in sorted(gdir.iterdir()):
        if child.is_file():
            child.unlink()
    gdir.rmdir()
    json_line({
        "ok": True,
        "generation": generation,
        "type": manifest.get("type"),
        "sha256": manifest.get("sha256"),
        "acknowledged": True,
    })


def cmd_owner(args) -> None:
    ensure_env()
    ClipboardOwner(Path(args.manifest), Path(args.ready)).run()


def main() -> None:
    parser = argparse.ArgumentParser(prog="clipboard-x11")
    sub = parser.add_subparsers(required=True)
    sub.add_parser("text").set_defaults(func=cmd_text)
    sub.add_parser("png").set_defaults(func=cmd_png)
    p_files = sub.add_parser("files")
    p_files.add_argument("files", nargs="*")
    p_files.set_defaults(func=cmd_files)
    sub.add_parser("status").set_defaults(func=cmd_status)
    p_stream = sub.add_parser("stream")
    p_stream.add_argument("--initial", action="store_true", help="emit current clipboard immediately before listening")
    p_stream.set_defaults(func=cmd_stream)
    sub.add_parser("watch").set_defaults(func=cmd_watch)
    p_read_text = sub.add_parser("read-text")
    p_read_text.add_argument("generation")
    p_read_text.set_defaults(func=cmd_read_text)
    p_read_image = sub.add_parser("read-image")
    p_read_image.add_argument("generation")
    p_read_image.set_defaults(func=cmd_read_image)
    p_read_files = sub.add_parser("read-files")
    p_read_files.add_argument("generation")
    p_read_files.set_defaults(func=cmd_read_files)
    p_ack = sub.add_parser("ack")
    p_ack.add_argument("generation")
    p_ack.set_defaults(func=cmd_ack)
    p_owner = sub.add_parser("_owner")
    p_owner.add_argument("manifest")
    p_owner.add_argument("ready")
    p_owner.set_defaults(func=cmd_owner)
    args = parser.parse_args()
    ensure_env()
    ensure_dirs()
    cleanup_old_cache()
    try:
        if args.func in (cmd_text, cmd_png, cmd_files):
            with LOCK_FILE.open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                args.func(args)
        else:
            args.func(args)
    except SystemExit:
        raise
    except subprocess.TimeoutExpired as exc:
        fail("timeout", "operation timed out", detail=str(exc))
    except Exception as exc:
        log_event(f"internal_error {exc.__class__.__name__}: {exc}")
        fail("internal_error", "internal error", detail=str(exc))


if __name__ == "__main__":
    main()
