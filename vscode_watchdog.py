#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
VS Code (Insiders) Chat-Watchdog
================================
Überwacht in Abwesenheit des Benutzers die letzte aktive Copilot-Chat-Session
in "Visual Studio Code - Insiders" und hält sie am Laufen:

  * Query läuft noch          -> nichts tun, im Intervall erneut prüfen
  * Fehler (Rate-Limit,       -> Nachricht "try again" an den Chat senden
    Connection Error, etc.)
  * Stillstand (Nachfrage,    -> konfigurierbare Antwort ("continue") senden
    hängengebliebener Query)
  * Mehr als N Fehler in Folge-> Prüfintervall auf Backoff-Intervall strecken
  * Läuft wieder              -> Zähler zurücksetzen, Normalintervall

Statusquelle:  %APPDATA%\\<vscode_user_dir>\\User\\workspaceStorage\\*\\chatSessions\\*.jsonl
               (die Datei mit der jüngsten Änderungszeit = letzte aktive Session)
Aktion:        Keybinding-basierte Auslösung von 'workbench.action.chat.open'
               mit {"query": "<text>"} -> sendet den Text an den Chat.
               Das Script pflegt die dafür nötigen Einträge in keybindings.json
               (unbenutzte Sondertasten F13/F14 -> praktisch kollisionsfrei).

Start:
    python vscode_watchdog.py            (GUI, Standard)
    python vscode_watchdog.py --cli      (nur Konsole)
    python vscode_watchdog.py --status   (einmaliger Status, keine Aktion)
    python vscode_watchdog.py --once     (einmal prüfen + ggf. handeln)
    python vscode_watchdog.py --setup-keybindings
    python vscode_watchdog.py --send retry|reply   (manueller Sende-Test)
"""

import argparse
import ctypes
import ctypes.wintypes as wt
import glob
import json
import logging
import os
import queue
import re
import subprocess
import sys
import threading
import time
from datetime import datetime

try:
    import psutil
except ImportError:
    psutil = None

APP_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(APP_DIR, "vscode_watchdog.config.json")
LOG_PATH = os.path.join(APP_DIR, "vscode_watchdog.log")

# Unter pythonw.exe (GUI-Start ohne Konsole) sind sys.stdout/sys.stderr None.
# print()/StreamHandler wuerden dann mit AttributeError crashen -> Dummy umleiten.
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w")

APPDATA = os.environ.get("APPDATA", os.path.expandvars(r"%APPDATA%"))

# ----------------------------------------------------------------------------
# Konfiguration
# ----------------------------------------------------------------------------

DEFAULT_CONFIG = {
    # Prüfintervall im Normalbetrieb (Sekunden)  -- 3 Minuten
    "check_interval_sec": 180,
    # Prüfintervall nach wiederholten Fehlern (Sekunden)  -- 60 Minuten
    "backoff_interval_sec": 3600,
    # Ab wie vielen Fehlern IN FOLGE wird auf Backoff umgeschaltet?
    # (> max, d.h. bei 3 kommt der 4. Fehler in den Backoff)
    "max_consecutive_failures": 3,
    # Ein Query ohne Ergebnis gilt als "hängengeblieben", wenn die Session-Datei
    # länger als diese Anzahl Minuten nicht mehr geschrieben wurde
    "stall_threshold_min": 10,

    # Nachricht bei Fehler
    "retry_message": "try again",
    # Nachricht bei Stillstand / Nachfrage
    "question_reply_message": "continue",
    # Bei Stillstand automatisch antworten? (False = nur protokollieren)
    "auto_reply_on_stall": True,

    # Fenster-/Prozess-Erkennung
    "vscode_process_name": "Code - Insiders",
    "vscode_window_title_filter": "Visual Studio Code",
    "vscode_user_dir": "Code - Insiders",

    # Hotkeys (müssen zur keybindings.json passen, siehe --setup-keybindings)
    "hotkey_retry": "ctrl+alt+shift+f13",
    "hotkey_reply": "ctrl+alt+shift+f14",

    # Wartezeit nach Fokussieren des VS-Code-Fensters vor dem Tastendruck (ms)
    "send_delay_after_focus_ms": 600,

    # Multi-Session: alle aktiven Chats überwachen?
    "multi_session": True,
    # Sessions gelten als "aktiv", wenn ihre Datei nicht älter ist (Stunden)
    "session_active_window_hours": 12,
    # Pause zwischen zwei Sendungen an verschiedene Fenster (Sekunden)
    "inter_send_delay_sec": 2.0,

    # GUI
    "dark_mode": True,
    "accent_color": "#0078D7",
    "gui_geometry": "",

    # Log-Level: DEBUG / INFO / WARNING
    "log_level": "INFO",
}


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(CONFIG_PATH):
        try:
            # utf-8-sig: verzeiht BOM, den z.B. PowerShell Set-Content -Encoding UTF8 schreibt
            with open(CONFIG_PATH, "r", encoding="utf-8-sig") as f:
                user = json.load(f)
            if isinstance(user, dict):
                cfg.update({k: v for k, v in user.items() if k in DEFAULT_CONFIG})
        except Exception as e:
            print("WARNUNG: config.json nicht lesbar (%s) - Defaults verwendet." % e)
    return cfg


def save_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=4, ensure_ascii=False)
        f.write("\n")


# ----------------------------------------------------------------------------
# Logging
# ----------------------------------------------------------------------------

log = logging.getLogger("watchdog")


def setup_logging(cfg, console=False):
    level = getattr(logging, str(cfg.get("log_level", "INFO")).upper(), logging.INFO)
    log.setLevel(level)
    fmt = logging.Formatter("%(asctime)s  %(levelname)-7s %(message)s", "%d.%m.%Y %H:%M:%S")
    # Datei (einfache Rotation)
    try:
        if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > 2 * 1024 * 1024:
            os.replace(LOG_PATH, LOG_PATH + ".old")
        fh = logging.FileHandler(LOG_PATH, encoding="utf-8")
        fh.setFormatter(fmt)
        log.addHandler(fh)
    except Exception:
        pass
    if console:
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        log.addHandler(sh)


# ----------------------------------------------------------------------------
# Chat-Session-Status auslesen
# ----------------------------------------------------------------------------

def _ws_identifier(ws_dir, cfg):
    """Bestimmt aus workspaceStorage/<dir>/workspace.json einen Match-String für
    den Fenstertitel sowie ein Anzeigelabel.
    Liefert (match_string, label) - match_string None, wenn unbekannt."""
    wj = os.path.join(ws_dir, "workspace.json")
    try:
        with open(wj, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
        uri = data.get("workspace") or data.get("folder") or ""
        path = ""
        if uri.startswith("file:///"):
            from urllib.parse import unquote
            path = unquote(uri[len("file:///"):]).replace("/", "\\")
        elif uri:
            path = uri
        if not path:
            return None, os.path.basename(ws_dir)[:8]
        folder = os.path.dirname(path)
        # unbenannter/gespeicherter Multi-Root-Workspace liegt unter ...\Workspaces\<id>\
        ws_root = os.path.join(APPDATA, cfg.get("vscode_user_dir", "Code - Insiders"), "Workspaces")
        if os.path.normcase(folder).startswith(os.path.normcase(ws_root) + os.sep):
            return "(workspace)", "Workspace (Multi-Root)"
        name = os.path.basename(folder.rstrip("\\/")) or folder
        return name.lower(), name
    except Exception:
        return None, os.path.basename(ws_dir)[:8]


def find_active_sessions(cfg):
    """Sammelt alle aktiven Chat-Sessions.

    Pro workspaceStorage-Ordner zählt nur die JÜNGSTE Session (die im Chat-View
    des zugehörigen Fensters offene). Liefert eine Liste von dicts, sortiert
    nach Änderungszeit (neueste zuerst):
    {path, mtime, ws_dir, session_id, ws_match, ws_label}
    """
    base = os.path.join(APPDATA, cfg.get("vscode_user_dir", "Code - Insiders"),
                        "User", "workspaceStorage")
    cutoff = time.time() - float(cfg.get("session_active_window_hours", 12)) * 3600.0
    newest_per_ws = {}
    for ws_dir in glob.glob(os.path.join(base, "*")):
        if not os.path.isdir(ws_dir):
            continue
        best = None
        for path in glob.glob(os.path.join(ws_dir, "chatSessions", "*.jsonl")):
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                continue
            if mtime < cutoff:
                continue
            if best is None or mtime > best[1]:
                best = (path, mtime)
        if best:
            newest_per_ws[ws_dir] = best
    sessions = []
    for ws_dir, (path, mtime) in newest_per_ws.items():
        ws_match, ws_label = _ws_identifier(ws_dir, cfg)
        sessions.append({
            "path": path, "mtime": mtime, "ws_dir": ws_dir,
            "session_id": os.path.splitext(os.path.basename(path))[0],
            "ws_match": ws_match, "ws_label": ws_label,
        })
    sessions.sort(key=lambda s: s["mtime"], reverse=True)
    return sessions


def find_latest_session(cfg):
    """Liefert (pfad, mtime) der zuletzt geschriebenen chatSessions/*.jsonl."""
    sessions = find_active_sessions(cfg)
    if not sessions:
        return None
    s = sessions[0]
    return s["path"], s["mtime"]


def _set_path(store, keys, value):
    """Wendet einen inkrementellen Pfad wie ["requests",3,"result"] auf ein dict an.
    Int-Schlüssel indizieren Listen, String-Schlüssel Dicts."""
    node = store
    for i in range(len(keys) - 1):
        key = keys[i]
        nxt = keys[i + 1]
        if isinstance(key, int) and isinstance(node, list):
            while len(node) <= key:
                node.append(None)
            child = node[key]
            if not isinstance(child, (dict, list)):
                child = [] if isinstance(nxt, int) else {}
                node[key] = child
        elif isinstance(node, dict):
            child = node.get(key)
            if not isinstance(child, (dict, list)):
                child = [] if isinstance(nxt, int) else {}
                node[key] = child
        else:
            return  # Struktur passt nicht -> Datensatz überspringen
        node = child
    last = keys[-1]
    if isinstance(last, int) and isinstance(node, list):
        while len(node) <= last:
            node.append(None)
    try:
        node[last] = value
    except (IndexError, TypeError):
        pass


def parse_session_file(path):
    """Liest die JSONL-Sitzungsdatei in ein verschachteltes dict ein."""
    store = {}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue  # unvollständige letzte Zeile o.ä.
                if not isinstance(rec, dict):
                    continue
                keys = rec.get("k")
                if isinstance(keys, list) and keys:
                    _set_path(store, keys, rec.get("v"))
    except OSError as e:
        log.warning("Session-Datei nicht lesbar: %s (%s)", path, e)
    return store


# Status-Konstanten
ST_RUNNING = "RUNNING"      # Query läuft
ST_COMPLETE = "COMPLETE"    # letzter Request erfolgreich abgeschlossen (Chat ruhig)
ST_ERROR = "ERROR"          # letzter Request mit Fehler abgebrochen
ST_STALLED = "STALLED"      # kein Ergebnis, aber auch kein Fortschritt (Nachfrage/Hänger)
ST_EMPTY = "EMPTY"          # keine Requests in der Session

STATUS_TEXT_DE = {
    ST_RUNNING: "Query läuft",
    ST_COMPLETE: "abgeschlossen / wartet auf Eingabe",
    ST_ERROR: "FEHLER",
    ST_STALLED: "Stillstand (Nachfrage/Hänger)",
    ST_EMPTY: "Session ohne Requests",
    "NO_SESSION": "keine Chat-Session gefunden",
}

RATE_LIMIT_RE = re.compile(r"limit will reset at\s+(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2})", re.I)


def evaluate_status(store, session_mtime, cfg):
    """Bestimmt den Chat-Status aus dem geparsten Session-Store."""
    reqs = store.get("requests") if isinstance(store, dict) else None
    if not isinstance(reqs, list) or not reqs:
        return ST_EMPTY, "Session enthält keine Requests"
    last = reqs[-1]
    if not isinstance(last, dict):
        return ST_EMPTY, "letzter Request unlesbar"
    res = last.get("result")
    if res is None:
        age_min = (time.time() - session_mtime) / 60.0
        if age_min > float(cfg.get("stall_threshold_min", 10)):
            return ST_STALLED, "kein Fortschritt seit %.0f min" % age_min
        return ST_RUNNING, "läuft (Session-Datei %.1f min alt)" % age_min
    if isinstance(res, dict) and res.get("errorDetails"):
        err = res.get("errorDetails") or {}
        msg = str(err.get("message") or err.get("code") or "unbekannter Fehler")
        detail = " ".join(msg.split())[:300]
        return ST_ERROR, detail
    return ST_COMPLETE, "letzte Anfrage erfolgreich abgeschlossen"


def get_chat_status(cfg):
    """Kombiniert Suche+Auswertung. Liefert (status, detail, session_path)."""
    found = find_latest_session(cfg)
    if not found:
        return "NO_SESSION", "keine chatSessions/*.jsonl gefunden", None
    path, mtime = found
    store = parse_session_file(path)
    status, detail = evaluate_status(store, mtime, cfg)
    return status, detail, path


# ----------------------------------------------------------------------------
# Windows-Fensterfindung + Tastatur-Simulation
# ----------------------------------------------------------------------------

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

VK_MAP = {
    "ctrl": 0x11, "control": 0x11,
    "alt": 0x12, "menu": 0x12, "altgr": 0x12,
    "shift": 0x10,
    "win": 0x5B, "meta": 0x5B, "cmd": 0x5B,
    "pause": 0x13, "break": 0x13,
    "scroll": 0x91, "scrolllock": 0x91,
    "enter": 0x0D, "return": 0x0D, "tab": 0x09, "space": 0x20,
    "escape": 0x1B, "esc": 0x1B, "backspace": 0x08, "insert": 0x2D,
    "delete": 0x2E, "end": 0x23, "home": 0x24, "pageup": 0x21, "pagedown": 0x22,
    "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
}
for _i in range(1, 25):                       # f1..f24
    VK_MAP["f%d" % _i] = 0x70 + (_i - 1)
for _i, _c in enumerate("abcdefghijklmnopqrstuvwxyz"):
    VK_MAP[_c] = 0x41 + _i
for _i in range(10):                          # 0..9
    VK_MAP[str(_i)] = 0x30 + _i


def combo_to_vks(combo):
    """'ctrl+alt+shift+f13' -> ([0x11,0x12,0x10], 0x7C)"""
    mods, main = [], None
    for part in str(combo).lower().split("+"):
        part = part.strip()
        if not part:
            continue
        vk = VK_MAP.get(part)
        if vk is None:
            raise ValueError("unbekannte Taste: %r" % part)
        if part in ("ctrl", "control", "alt", "menu", "altgr", "shift", "win", "meta", "cmd"):
            mods.append(vk)
        else:
            main = vk
    if main is None:
        raise ValueError("keine Haupttaste in %r" % combo)
    return mods, main


def _keybd(vk, up=False):
    user32.keybd_event(vk, 0, 2 if up else 0, 0)


def send_hotkey(combo, hold=0.03):
    """Sendet eine Tastenkombination an das VORDERGRUNDFenster."""
    mods, main = combo_to_vks(combo)
    for vk in mods:
        _keybd(vk)
        time.sleep(hold)
    _keybd(main)
    time.sleep(hold)
    _keybd(main, up=True)
    time.sleep(hold)
    for vk in reversed(mods):
        _keybd(vk, up=True)
        time.sleep(hold / 2)
    return True


WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)


def _window_text(hwnd):
    n = user32.GetWindowTextLengthW(hwnd)
    if n <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def find_vscode_windows(cfg):
    """Alle Hauptfenster des konfigurierten VS-Code-Prozesses."""
    proc_name = (cfg.get("vscode_process_name") or "Code - Insiders").lower()
    title_filter = cfg.get("vscode_window_title_filter") or ""
    result = []

    def on_enum(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        title = _window_text(hwnd)
        if not title or (title_filter and title_filter.lower() not in title.lower()):
            return True
        pid = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        name = ""
        if psutil is not None:
            try:
                # psutil liefert "Code - Insiders.exe", Get-Process "Code - Insiders"
                name = psutil.Process(pid.value).name()
                if name.lower().endswith(".exe"):
                    name = name[:-4]
            except Exception:
                name = ""
        if name.lower() != proc_name:
            return True
        rect = wt.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        area = max(0, rect.right - rect.left) * max(0, rect.bottom - rect.top)
        result.append({
            "hwnd": hwnd,
            "title": title,
            "iconic": bool(user32.IsIconic(hwnd)),
            "area": area,
        })
        return True

    user32.EnumWindows(WNDENUMPROC(on_enum), 0)
    # Präferenz: nicht minimiert, dann größte Fläche
    result.sort(key=lambda w: (not w["iconic"], w["area"]), reverse=True)
    return result


def force_foreground(hwnd):
    """Holt ein Fenster zuverlässig in den Vordergrund (best effort)."""
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        time.sleep(0.25)
    fg = user32.GetForegroundWindow()
    if fg == hwnd:
        return True
    # Trick: kurzer ALT-Druck erlaubt SetForegroundWindow
    _keybd(0x12); _keybd(0x12, up=True)
    user32.SetForegroundWindow(hwnd)
    user32.BringWindowToTop(hwnd)
    if user32.GetForegroundWindow() == hwnd:
        return True
    # Hartnäckiger Fall: Input-Threads verbinden
    try:
        fg_thread = user32.GetWindowThreadProcessId(user32.GetForegroundWindow(), None)
        this_thread = kernel32.GetCurrentThreadId()
        target_thread = user32.GetWindowThreadProcessId(hwnd, None)
        user32.AttachThreadInput(this_thread, fg_thread, True)
        user32.AttachThreadInput(this_thread, target_thread, True)
        user32.SetForegroundWindow(hwnd)
        user32.AttachThreadInput(this_thread, fg_thread, False)
        user32.AttachThreadInput(this_thread, target_thread, False)
    except Exception:
        pass
    time.sleep(0.1)
    return user32.GetForegroundWindow() == hwnd


def send_message_to_chat(cfg, which="retry", prefer_ws=None):
    """Fokussiert das passende VS-Code-Fenster und löst das Hotkey aus.

    prefer_ws: Match-String (kleingeschrieben) oder '(workspace)'-Marker des
    Workspace-Ordners, zu dem die Session gehört. None = bestes Fenster.
    """
    combo = cfg.get("hotkey_retry") if which == "retry" else cfg.get("hotkey_reply")
    wins = find_vscode_windows(cfg)
    if not wins:
        log.warning("Kein VS-Code-Fenster gefunden (Prozess %r) - Nachricht nicht gesendet.",
                    cfg.get("vscode_process_name"))
        return False
    win = None
    title_of = lambda w: (w["title"] or "").lower()
    if prefer_ws:
        # 1) exakter Ordnername im Titel, 2) Multi-Root-Marker, 3) nichts
        cands = [w for w in wins if prefer_ws in title_of(w)]
        if not cands and prefer_ws == "(workspace)":
            cands = [w for w in wins if "(workspace)" in title_of(w)]
        if cands:
            win = cands[0]
    if win is None:
        fg = user32.GetForegroundWindow()
        for w in wins:
            if w["hwnd"] == fg:
                win = w  # bereits fokussiertes VS-Code-Fenster beibehalten (kein Fokus-Raub)
                break
    if win is None:
        win = wins[0]
    elif prefer_ws and prefer_ws not in title_of(win) and not (
            prefer_ws == "(workspace)" and "(workspace)" in title_of(win)):
        log.warning("Fenster-Zuordnung unsicher (Session ≙ %r) - benutze: %s",
                    prefer_ws, win["title"][:60])
    log.info("Sende Hotkey %s an Fenster: %s", combo, win["title"][:80])
    if not force_foreground(win["hwnd"]):
        log.warning("Konnte VS-Code-Fenster nicht in den Vordergrund holen - versende trotzdem.")
    time.sleep(max(0.05, float(cfg.get("send_delay_after_focus_ms", 600)) / 1000.0))
    try:
        send_hotkey(combo)
    except ValueError as e:
        log.error("Hotkey ungültig: %s", e)
        return False
    log.info("Hotkey gesendet (%s).", which)
    return True


# ----------------------------------------------------------------------------
# keybindings.json pflegen
# ----------------------------------------------------------------------------

KEYBINDINGS_MARKER = "__vscode_watchdog"


def keybindings_path(cfg):
    return os.path.join(APPDATA, cfg.get("vscode_user_dir", "Code - Insiders"),
                        "User", "keybindings.json")


def _strip_jsonc(text):
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    lines = []
    for line in text.splitlines():
        # // nur außerhalb von Strings entfernen (grob, aber ausreichend)
        out, in_str = [], False
        i = 0
        while i < len(line):
            c = line[i]
            if c == '"':
                in_str = not in_str
                out.append(c)
            elif not in_str and c == "/" and i + 1 < len(line) and line[i + 1] == "/":
                break
            else:
                out.append(c)
            i += 1
        lines.append("".join(out))
    return "\n".join(lines)


def ensure_keybindings(cfg):
    """Legt die beiden Managed-Keybindings an (idempotent, mit Backup)."""
    path = keybindings_path(cfg)
    entries = []
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = f.read()
            data = json.loads(_strip_jsonc(raw) or "[]")
            if isinstance(data, list):
                entries = data
        except Exception as e:
            log.error("keybindings.json nicht parsbar (%s) - KEINE Änderung vorgenommen!", e)
            return False

    def is_ours(e):
        args = e.get("args") if isinstance(e, dict) else None
        return isinstance(args, dict) and args.get(KEYBINDINGS_MARKER) is True

    kept = [e for e in entries if not is_ours(e)]
    removed = len(entries) - len(kept)

    ours = [
        {
            "key": cfg.get("hotkey_retry", DEFAULT_CONFIG["hotkey_retry"]),
            "command": "workbench.action.chat.open",
            "args": {
                "query": cfg.get("retry_message", DEFAULT_CONFIG["retry_message"]),
                KEYBINDINGS_MARKER: True,
            },
        },
        {
            "key": cfg.get("hotkey_reply", DEFAULT_CONFIG["hotkey_reply"]),
            "command": "workbench.action.chat.open",
            "args": {
                "query": cfg.get("question_reply_message", DEFAULT_CONFIG["question_reply_message"]),
                KEYBINDINGS_MARKER: True,
            },
        },
    ]
    # Konsistenzcheck: Hotkeys dürfen nicht von anderen Einträgen belegt sein
    our_keys = {o["key"].lower() for o in ours}
    kept = [e for e in kept if not (
        isinstance(e, dict) and str(e.get("key", "")).lower() in our_keys
        and e.get("command") == "workbench.action.chat.open")]

    new_entries = kept + ours
    if new_entries == entries:
        log.info("keybindings.json bereits aktuell.")
        return True

    backup = path + ".watchdog.bak"
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                old = f.read()
            if not os.path.exists(backup) or open(backup, "r", encoding="utf-8").read() != old:
                with open(backup, "w", encoding="utf-8") as f:
                    f.write(old)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(new_entries, f, indent=4, ensure_ascii=False)
            f.write("\n")
    except Exception as e:
        log.error("Konnte keybindings.json nicht schreiben: %s", e)
        return False
    log.info("keybindings.json aktualisiert (%d alte entfernt, 2 eigene angelegt). Backup: %s",
             removed, backup)
    return True


# ----------------------------------------------------------------------------
# Watchdog-Logik (Thread)
# ----------------------------------------------------------------------------

class Watchdog(threading.Thread):
    def __init__(self, cfg, ui_queue=None):
        super().__init__(daemon=True)
        self.cfg = dict(cfg)
        self.ui_queue = ui_queue if ui_queue is not None else queue.Queue()
        self.wake = threading.Event()
        self.stop_event = threading.Event()
        self.paused = threading.Event()
        self.next_check_at = 0.0
        # Per-Session-Zustand: session_id -> {"failures": int, "backoff": bool,
        #                                    "status": str, "detail": str, "label": str}
        self.sessions = {}

    # ---- Hilfen ------------------------------------------------------------
    def ui(self, kind, **kw):
        try:
            self.ui_queue.put_nowait(dict(kind=kind, **kw))
        except Exception:
            pass

    def apply_config(self, cfg):
        self.cfg = dict(cfg)
        self.wake.set()

    def check_now(self):
        self.wake.set()

    def _interval_for(self, sid_state):
        if sid_state.get("backoff"):
            return max(30, int(self.cfg.get("backoff_interval_sec", 3600)))
        return max(15, int(self.cfg.get("check_interval_sec", 180)))

    @property
    def backoff_mode(self):
        return any(s.get("backoff") for s in self.sessions.values())

    @property
    def consecutive_failures(self):
        return sum(s.get("failures", 0) for s in self.sessions.values())

    # ---- Kernschleife ------------------------------------------------------
    def run(self):
        log.info("Watchdog gestartet (Intervall %ds / Backoff %ds, Multi-Session %s).",
                 self.cfg.get("check_interval_sec"), self.cfg.get("backoff_interval_sec"),
                 "an" if self.cfg.get("multi_session", True) else "aus")
        ensure_keybindings(self.cfg)
        while not self.stop_event.is_set():
            if self.paused.is_set():
                self.next_check_at = 0.0
                self.wake.wait(timeout=1.0)
                self.wake.clear()
                continue
            try:
                self.do_check()
            except Exception:
                log.exception("Unerwarteter Fehler im Prüflauf")
            interval = self._current_interval()
            self.next_check_at = time.time() + interval
            self.ui("planned", at=self.next_check_at, interval=interval,
                    backoff=self.backoff_mode, failures=self.consecutive_failures)
            # wartbar aufweckbar
            waited = 0.0
            while not self.stop_event.is_set() and not self.wake.is_set() and waited < interval:
                if self.paused.is_set():
                    break
                step = 0.5
                time.sleep(step)
                waited += step
            self.wake.clear()
        log.info("Watchdog beendet.")

    def _current_interval(self):
        if self.backoff_mode:
            return max(30, int(self.cfg.get("backoff_interval_sec", 3600)))
        return max(15, int(self.cfg.get("check_interval_sec", 180)))

    # ---- Einzelprüfung -----------------------------------------------------
    def do_check(self):
        found = find_active_sessions(self.cfg)
        if not found:
            log.info("Keine aktiven Chat-Sessions im Zeitfenster (%d h).",
                     self.cfg.get("session_active_window_hours", 12))
            self.sessions.clear()
            self.ui("sessions", sessions=[])
            self.ui("status", status="NO_SESSION", detail="keine aktiven Sessions",
                    session="-", failures=0, backoff=False)
            return

        # Zustand für verschwundene Sessions aufräumen
        live_ids = {s["session_id"] for s in found}
        for sid in list(self.sessions):
            if sid not in live_ids:
                del self.sessions[sid]

        any_sent = False
        for s in found:
            sid = s["session_id"]
            st_state = self.sessions.setdefault(
                sid, {"failures": 0, "backoff": False, "status": None,
                      "detail": "", "label": s.get("ws_label") or sid[:8]})
            store = parse_session_file(s["path"])
            status, detail = evaluate_status(store, s["mtime"], self.cfg)
            st_state["status"], st_state["detail"] = status, detail
            st_state["label"] = s.get("ws_label") or st_state["label"]
            label = st_state["label"]
            log.info("Check [%s]: %s | %s", label,
                     STATUS_TEXT_DE.get(status, status), detail[:140])
            self.ui("status", status=status, detail=detail,
                    session=os.path.basename(s["path"]),
                    failures=st_state["failures"], backoff=st_state["backoff"],
                    label=label)

            if status in (ST_RUNNING, ST_COMPLETE, ST_EMPTY, "NO_SESSION"):
                if status == ST_RUNNING and st_state["failures"]:
                    log.info("[%s] Chat läuft wieder - Fehlerzähler zurückgesetzt.", label)
                st_state["failures"] = 0
                if st_state["backoff"]:
                    log.info("[%s] Zurück zum Normalintervall.", label)
                    st_state["backoff"] = False
                continue

            # --- Fehler oder Stillstand: eingreifen -------------------------
            which = "retry" if status == ST_ERROR else "reply"
            if status == ST_ERROR:
                rl = RATE_LIMIT_RE.search(detail or "")
                if rl:
                    log.warning("[%s] Rate-Limit erkannt, Reset angeblich um %s.",
                                label, rl.group(1))
            elif status == ST_STALLED and not self.cfg.get("auto_reply_on_stall", True):
                log.info("[%s] Stillstand erkannt, aber auto_reply_on_stall=False - kein Eingriff.",
                         label)
                continue

            if any_sent:
                time.sleep(max(0.0, float(self.cfg.get("inter_send_delay_sec", 2.0))))
            ok = send_message_to_chat(self.cfg, which, prefer_ws=s.get("ws_match"))
            if ok:
                any_sent = True
                st_state["failures"] += 1
                maxf = int(self.cfg.get("max_consecutive_failures", 3))
                if st_state["failures"] > maxf and not st_state["backoff"]:
                    st_state["backoff"] = True
                    log.warning("[%s] %d Fehler/Stillstände in Folge -> Backoff für diese Session.",
                                label, st_state["failures"])
                else:
                    log.info("[%s] Eingriff #%d (%s).", label, st_state["failures"],
                             "Backoff" if st_state["backoff"] else "Normal")
            else:
                log.warning("[%s] Konnte Nachricht nicht senden (Fenster nicht gefunden?).", label)
            self.ui("status", status=status, detail=detail,
                    session=os.path.basename(s["path"]),
                    failures=st_state["failures"], backoff=st_state["backoff"],
                    sent=ok, which=which, label=label)

        # Zusammenfassung für die Session-Liste der GUI
        rows = []
        for s in found:
            sid = s["session_id"]
            st = self.sessions.get(sid, {})
            rows.append({
                "label": s.get("ws_label") or sid[:8],
                "session": sid,
                "status": st.get("status"),
                "detail": (st.get("detail") or "")[:120],
                "failures": st.get("failures", 0),
                "backoff": bool(st.get("backoff")),
                "mtime": s["mtime"],
                "ws_match": s.get("ws_match"),
            })
        self.ui("sessions", sessions=rows)


# ----------------------------------------------------------------------------
# GUI
# ----------------------------------------------------------------------------

def run_gui(cfg):
    import io
    import tkinter as tk
    from tkinter import ttk, messagebox

    ui_q = queue.Queue()
    dog = Watchdog(cfg, ui_q)

    root = tk.Tk()
    root.title("VS Code Chat-Watchdog")
    root.minsize(840, 600)
    try:
        root.geometry(cfg.get("gui_geometry") or "940x700")
    except Exception:
        root.geometry("940x700")

    # ------------------------------------------------------------------
    # Farben / Themes
    # ------------------------------------------------------------------
    THEMES = {
        "dark": {
            "bg": "#2b2b2b", "card": "#3c3c3c", "card2": "#444444",
            "text": "#ffffff", "muted": "#9a9a9a",
            "entry": "#4d4d4d", "button": "#565656", "border": "#4f4f4f",
            "log_bg": "#1e1e1e", "log_fg": "#d4d4d4",
            "accent": "#0078D7", "accent_hover": "#1a86df", "accent_text": "#ffffff",
            "ok": "#4ec94e", "warn": "#e0a030", "err": "#ff5555",
        },
        "light": {
            "bg": "#f0f0f0", "card": "#ffffff", "card2": "#ececec",
            "text": "#1a1a1a", "muted": "#666666",
            "entry": "#ffffff", "button": "#dedede", "border": "#c8c8c8",
            "log_bg": "#fbfbfb", "log_fg": "#333333",
            "accent": "#005a9e", "accent_hover": "#1e6fb0", "accent_text": "#ffffff",
            "ok": "#1a8f1a", "warn": "#b07800", "err": "#c42b1c",
        },
    }
    STATUS_COLOR_KEY = {
        ST_RUNNING: "accent", ST_COMPLETE: "ok", ST_ERROR: "err",
        ST_STALLED: "warn", ST_EMPTY: "muted", "NO_SESSION": "muted",
    }
    STATUS_EMOJI = {
        ST_RUNNING: "⏳", ST_COMPLETE: "✅", ST_ERROR: "❌",
        ST_STALLED: "💤", ST_EMPTY: "🫥", "NO_SESSION": "🫥",
    }

    def _hex_rgb(h):
        h = str(h).lstrip("#")
        if len(h) == 3:
            h = "".join(c * 2 for c in h)
        try:
            return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))
        except Exception:
            return (0, 120, 215)

    PIL_OK = True
    try:
        from PIL import Image, ImageDraw, ImageFont
    except Exception:
        PIL_OK = False

    def _emoji_font(size):
        for name in ("seguiemj.ttf", "seguisym.ttf", "arialuni.ttf"):
            try:
                return ImageFont.truetype(name, size)
            except Exception:
                continue
        return ImageFont.load_default()

    def _render_emoji(emoji, px, fg, bg=None, radius=0, pad=0):
        """RGBA-Bild eines Emojis, optional auf abgerundetem farbigem Grund."""
        size = px + 2 * pad
        img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        if bg is not None:
            d.rounded_rectangle([0, 0, size - 1, size - 1],
                                radius=radius or max(4, size // 5), fill=tuple(bg) + (255,))
        f = _emoji_font(px)
        try:
            bbox = d.textbbox((0, 0), emoji, font=f)
            w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
            pos = ((size - w) // 2 - bbox[0], (size - h) // 2 - bbox[1])
        except Exception:
            pos = (pad, pad)
        try:
            d.text(pos, emoji, font=f, embedded_color=True)
        except Exception:
            d.text(pos, emoji, font=f, fill=tuple(fg) + (255,))
        return img

    icon_cache = {}

    def icon_photo(emoji, px, fg, bg=None, radius=0, pad=0):
        """Emoji als tk.PhotoImage (mit Cache); None wenn kein PIL."""
        if not PIL_OK:
            return None
        key = (emoji, px, fg, bg, radius, pad)
        if key not in icon_cache:
            buf = io.BytesIO()
            _render_emoji(emoji, px, fg, bg=bg, radius=radius, pad=pad).save(buf, "PNG")
            icon_cache[key] = tk.PhotoImage(data=buf.getvalue())
        return icon_cache[key]

    def set_window_icon():
        if not PIL_OK:
            return
        try:
            accent = _hex_rgb(cfg.get("accent_color", "#0078D7"))
            img = _render_emoji("🤖", 150, (255, 255, 255), bg=accent, radius=44, pad=26)
            ico = os.path.join(APP_DIR, "vscode_watchdog.ico")
            img.save(ico, format="ICO",
                     sizes=[(16, 16), (24, 24), (32, 32), (48, 48),
                            (64, 64), (128, 128), (256, 256)])
            root.iconbitmap(ico)
        except Exception as e:
            log.debug("Fenster-Icon fehlgeschlagen: %s", e)

    set_window_icon()

    # ------------------------------------------------------------------
    # Zustand & Variablen (überleben Theme-Wechsel)
    # ------------------------------------------------------------------
    state = {"theme": "dark" if cfg.get("dark_mode", True) else "light",
             "log_lines": [], "pulse": 0, "last_status": None}
    W = {}  # Widget-Referenzen (werden bei rebuild neu gefüllt)

    v_check = tk.StringVar(value=str(cfg["check_interval_sec"]))
    v_backoff = tk.StringVar(value=str(cfg["backoff_interval_sec"]))
    v_maxfail = tk.StringVar(value=str(cfg["max_consecutive_failures"]))
    v_stall = tk.StringVar(value=str(cfg["stall_threshold_min"]))
    v_retry = tk.StringVar(value=cfg["retry_message"])
    v_reply = tk.StringVar(value=cfg["question_reply_message"])
    v_auto = tk.BooleanVar(value=bool(cfg["auto_reply_on_stall"]))
    v_hk_retry = tk.StringVar(value=cfg["hotkey_retry"])
    v_hk_reply = tk.StringVar(value=cfg["hotkey_reply"])
    v_proc = tk.StringVar(value=cfg["vscode_process_name"])
    v_title = tk.StringVar(value=cfg["vscode_window_title_filter"])
    v_active_h = tk.StringVar(value=str(cfg.get("session_active_window_hours", 12)))

    status_var = tk.StringVar(value="warte auf ersten Check…")
    detail_var = tk.StringVar(value="")
    session_var = tk.StringVar(value="-")
    countdown_var = tk.StringVar(value="-")
    failures_var = tk.StringVar(value="0")
    mode_var = tk.StringVar(value="Normal (3 min Takt)")
    running_var = tk.BooleanVar(value=True)
    v_multi = tk.BooleanVar(value=bool(cfg.get("multi_session", True)))

    # Session-Liste (von Watchdog-Thread gefüllt)
    session_rows = {"rows": []}

    def collect_cfg():
        c = dict(cfg)
        c["check_interval_sec"] = max(15, int(float(v_check.get())))
        c["backoff_interval_sec"] = max(60, int(float(v_backoff.get())))
        c["max_consecutive_failures"] = max(1, int(float(v_maxfail.get())))
        c["stall_threshold_min"] = max(1, int(float(v_stall.get())))
        c["retry_message"] = v_retry.get()
        c["question_reply_message"] = v_reply.get()
        c["auto_reply_on_stall"] = bool(v_auto.get())
        c["hotkey_retry"] = v_hk_retry.get().strip()
        c["hotkey_reply"] = v_hk_reply.get().strip()
        c["vscode_process_name"] = v_proc.get().strip()
        c["vscode_window_title_filter"] = v_title.get().strip()
        c["session_active_window_hours"] = max(1, int(float(v_active_h.get())))
        c["multi_session"] = bool(v_multi.get())
        c["dark_mode"] = state["theme"] == "dark"
        return c

    # ------------------------------------------------------------------
    # Aktionen
    # ------------------------------------------------------------------
    def on_save():
        try:
            c = collect_cfg()
        except ValueError:
            messagebox.showerror("Fehler", "Bitte nur Zahlen in die Intervall-Felder eingeben.")
            return
        try:
            combo_to_vks(c["hotkey_retry"]); combo_to_vks(c["hotkey_reply"])
        except ValueError as e:
            messagebox.showerror("Fehler", "Hotkey ungültig: %s" % e)
            return
        c["gui_geometry"] = root.geometry()
        save_config(c)
        ensure_keybindings(c)
        dog.apply_config(c)
        log.info("Konfiguration gespeichert und angewendet.")

    def on_toggle():
        if running_var.get():
            dog.paused.set()
            running_var.set(False)
            status_var.set("pausiert")
            countdown_var.set("-")
        else:
            dog.paused.clear()
            running_var.set(True)
            state["status_core"] = "warte auf Check…"
            dog.check_now()
        build_ui()

    def on_check_now():
        if not running_var.get():
            messagebox.showinfo("Hinweis", "Überwachung ist pausiert.")
            return
        dog.check_now()

    def on_test(which):
        msg = v_retry.get() if which == "retry" else v_reply.get()
        rows = session_rows["rows"]
        ws = None
        if len(rows) >= 1:
            names = [r["label"] for r in rows] or ["bestes Fenster"]
            sel = messagebox.askyesnocancel(
                "Test",
                "Nachricht „%s“ senden.\n\nJa  = an Session/Fenster: %s\nNein = an bestes verfügbares Fenster" %
                (msg, names[0]))
            if sel is None:
                return
            if sel:
                ws = rows[0].get("ws_match")
        else:
            if not messagebox.askyesno("Test", "Jetzt wirklich „%s“ an den Chat senden?" % msg):
                return
        send_message_to_chat(collect_cfg(), which, prefer_ws=ws)

    def on_status_dialog():
        st, det, sess = get_chat_status(collect_cfg())
        messagebox.showinfo("Chat-Status", "%s\n\n%s\n\nSession: %s" %
                            (STATUS_TEXT_DE.get(st, st), det,
                             os.path.basename(sess) if sess else "-"))

    def on_toggle_theme():
        state["theme"] = "light" if state["theme"] == "dark" else "dark"
        try:
            c = collect_cfg()
            c["gui_geometry"] = root.geometry()
            save_config(c)
        except ValueError:
            pass  # Theme-Wechsel soll an ungültigen Eingaben nicht scheitern
        build_ui()

    # ------------------------------------------------------------------
    # UI-Aufbau (komplett theme-abhängig, bei Wechsel neu gebaut)
    # ------------------------------------------------------------------
    def _fmt_age(mtime):
        sec = max(0, int(time.time() - mtime))
        if sec < 90:
            return "vor %d s" % sec
        m, s = divmod(sec, 60)
        if m < 90:
            return "vor %d min" % m
        h, m = divmod(m, 60)
        return "vor %d h %02d min" % (h, m)

    def _render_sessions(tree, T):
        tree.delete("placeholder") if tree.exists("placeholder") else None
        for i in tree.get_children(""):
            tree.delete(i)
        for r in session_rows["rows"]:
            st = r.get("status") or ST_EMPTY
            status_txt = STATUS_TEXT_DE.get(st, st)
            mode_txt = "Backoff" if r.get("backoff") else "Normal"
            tree.insert("", "end",
                        text="%s  ·  %s" % (r.get("label", "?"), r.get("session", "?")[:13]),
                        values=(status_txt, r.get("failures", 0), mode_txt,
                                _fmt_age(r.get("mtime", time.time()))),
                        tags=(st,))

    def build_ui():
        for child in root.winfo_children():
            child.destroy()
        W.clear()
        T = THEMES[state["theme"]]
        accent = cfg.get("accent_color", "#0078D7")
        T = dict(T)
        T["accent"] = accent if state["theme"] == "dark" else T["accent"]
        if state["theme"] == "dark" and accent:
            h = accent.lstrip("#")
            T["accent_hover"] = "#%02x%02x%02x" % tuple(min(255, v + 24) for v in _hex_rgb(accent))

        F_TEXT = ("Segoe UI", 10)
        F_BOLD = ("Segoe UI", 10, "bold")
        F_TITLE = ("Segoe UI", 13, "bold")
        F_BIG = ("Segoe UI", 15, "bold")
        F_SMALL = ("Segoe UI", 9)
        F_LOG = ("Consolas", 9)

        style = ttk.Style(root)
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure(".", background=T["bg"], foreground=T["text"],
                        bordercolor=T["border"], lightcolor=T["card"], darkcolor=T["card"])
        style.configure("TFrame", background=T["bg"])
        style.configure("Card.TFrame", background=T["card"])
        style.configure("TLabel", background=T["bg"], foreground=T["text"], font=F_TEXT)
        style.configure("Card.TLabel", background=T["card"], foreground=T["text"], font=F_TEXT)
        style.configure("Muted.TLabel", background=T["bg"], foreground=T["muted"], font=F_SMALL)
        style.configure("MutedCard.TLabel", background=T["card"], foreground=T["muted"], font=F_SMALL)
        style.configure("TLabelframe", background=T["bg"], bordercolor=T["border"],
                        relief="solid", borderwidth=1)
        style.configure("TLabelframe.label", background=T["bg"],
                        foreground=T["muted"], font=("Segoe UI", 9, "bold"))
        style.configure("TButton", background=T["button"], foreground=T["text"],
                        borderwidth=0, focusthickness=1, focuscolor=T["accent"],
                        padding=(10, 6), font=F_TEXT)
        style.map("TButton",
                  background=[("pressed", T["accent"]), ("active", T["card2"])],
                  foreground=[("pressed", T["accent_text"])])
        style.configure("Accent.TButton", background=T["accent"], foreground=T["accent_text"],
                        padding=(12, 6), font=F_BOLD)
        style.map("Accent.TButton",
                  background=[("pressed", T["accent"]), ("active", T.get("accent_hover", T["accent"]))],
                  foreground=[("pressed", T["accent_text"]), ("active", T["accent_text"])])
        style.configure("TEntry", fieldbackground=T["entry"], foreground=T["text"],
                        bordercolor=T["border"], insertcolor=T["text"], padding=3)
        style.map("TEntry", bordercolor=[("focus", T["accent"])])
        style.configure("TCheckbutton", background=T["card"], foreground=T["text"], font=F_TEXT)
        style.map("TCheckbutton",
                  background=[("active", T["card"])],
                  indicatorcolor=[("selected", T["accent"]), ("!selected", T["entry"])])
        style.configure("TScrollbar", background=T["button"], troughcolor=T["card"],
                        bordercolor=T["card"], arrowcolor=T["muted"])
        style.configure("Session.Treeview", background=T["card"], foreground=T["text"],
                        fieldbackground=T["card"], borderwidth=0, font=("Segoe UI", 9))
        style.map("Session.Treeview",
                  background=[("selected", T["accent"])],
                  foreground=[("selected", T["accent_text"])])
        style.configure("Session.Treeview.Heading",
                        background=T["card2"], foreground=T["muted"],
                        font=("Segoe UI", 9, "bold"), borderwidth=0)
        style.map("Session.Treeview.Heading", background=[("active", T["card2"])])
        root.configure(bg=T["bg"])

        def emoji_btn(parent, text, emoji, cmd, accent_btn=False, width=None):
            ph = icon_photo(emoji, 18, _hex_rgb(T["text"]))
            b = ttk.Button(parent, text=" " + text if ph else text, command=cmd,
                           style="Accent.TButton" if accent_btn else "TButton",
                           image=ph, compound="left")
            if width:
                b.configure(width=width)
            return b

        wrap = ttk.Frame(root, padding=(14, 10))
        wrap.pack(fill="both", expand=True)

        # --- Kopfzeile ----------------------------------------------------
        head = ttk.Frame(wrap, style="Card.TFrame", padding=(12, 8))
        head.pack(fill="x")
        logo = icon_photo("🤖", 34, (255, 255, 255), bg=_hex_rgb(accent), radius=9, pad=4)
        if logo:
            ttk.Label(head, image=logo, style="Card.TLabel").pack(side="left", padx=(0, 10))
        box = ttk.Frame(head, style="Card.TFrame")
        box.pack(side="left", fill="x", expand=True)
        ttk.Label(box, text="VS Code Chat-Watchdog", style="Card.TLabel",
                  font=F_TITLE).pack(anchor="w")
        ttk.Label(box, text="hält den Copilot-Chat automatisch in Schwung",
                  style="MutedCard.TLabel").pack(anchor="w")
        right = ttk.Frame(head, style="Card.TFrame")
        right.pack(side="right")
        theme_emoji = "☀️" if state["theme"] == "dark" else "🌙"
        theme_btn = emoji_btn(right, "Theme", theme_emoji, on_toggle_theme)
        theme_btn.pack(side="right", padx=(8, 0))
        toggle_btn = emoji_btn(right, "Pause" if running_var.get() else "Start",
                               "⏸" if running_var.get() else "▶", on_toggle)
        toggle_btn.pack(side="right")
        W["toggle_btn"] = toggle_btn

        # --- Status-Karte ---------------------------------------------------
        sf = ttk.LabelFrame(wrap, text="  STATUS  ", padding=12)
        sf.pack(fill="x", pady=(10, 0))

        row1 = ttk.Frame(sf)
        row1.pack(fill="x")
        dot = tk.Label(row1, text="●", font=("Segoe UI", 18),
                       fg=T["muted"], bg=T["bg"])
        dot.pack(side="left", padx=(0, 8))
        W["dot"] = dot
        ttl = ttk.Label(row1, textvariable=status_var, font=F_BIG)
        ttl.pack(side="left")
        chips = ttk.Frame(row1)
        chips.pack(side="right")
        W["chip_countdown"] = ttk.Label(chips, text="⏱ nächster Check: -", style="Muted.TLabel",
                                        font=F_SMALL)
        W["chip_countdown"].pack(side="right", padx=(10, 0))
        W["chip_failures"] = ttk.Label(chips, text="⚠ Fehler: 0", style="Muted.TLabel",
                                       font=F_SMALL)
        W["chip_failures"].pack(side="right", padx=(10, 0))
        W["chip_mode"] = ttk.Label(chips, text="⚙ Normal", style="Muted.TLabel", font=F_SMALL)
        W["chip_mode"].pack(side="right")

        row2 = ttk.Frame(sf)
        row2.pack(fill="x", pady=(6, 0))
        ttk.Label(row2, text="Session:", style="Muted.TLabel").pack(side="left")
        ttk.Label(row2, textvariable=session_var, font=("Consolas", 9)).pack(side="left", padx=(6, 0))
        d = tk.Label(row2, textvariable=detail_var, fg=T["muted"], bg=T["bg"],
                     font=F_SMALL, anchor="w", justify="left", wraplength=760)
        d.pack(fill="x", pady=(4, 0))
        W["detail"] = d

        # --- Session-Liste (Multi-Session) ----------------------------------
        sessf = ttk.LabelFrame(wrap, text="  ÜBERWACHTE SESSIONS  ", padding=6)
        sessf.pack(fill="x", pady=(10, 0))
        cols = ("status", "failures", "backoff", "age")
        tree = ttk.Treeview(sessf, columns=cols, show="tree headings", height=3,
                            style="Session.Treeview", selectmode="none")
        tree.heading("#0", text="Workspace / Session")
        tree.heading("status", text="Status")
        tree.heading("failures", text="Fehler")
        tree.heading("backoff", text="Modus")
        tree.heading("age", text="letzte Aktivität")
        tree.column("#0", width=280, minwidth=180, stretch=True)
        tree.column("status", width=190, anchor="w", stretch=False)
        tree.column("failures", width=55, anchor="center", stretch=False)
        tree.column("backoff", width=80, anchor="w", stretch=False)
        tree.column("age", width=130, anchor="w", stretch=False)
        tree.pack(fill="x")
        W["sessions_tree"] = tree
        # Tag-Farben je Status
        for st_key, col_key in [(ST_RUNNING, "accent"), (ST_COMPLETE, "ok"),
                                (ST_ERROR, "err"), (ST_STALLED, "warn"),
                                (ST_EMPTY, "muted"), ("NO_SESSION", "muted")]:
            tree.tag_configure(st_key, foreground=T[col_key])
        if not session_rows["rows"]:
            tree.insert("", "end", iid="placeholder", text="(noch keine Session erkannt)",
                        values=("", "", "", ""), tags=(ST_EMPTY,))
        else:
            _render_sessions(tree, T)

        # --- Einstellungen --------------------------------------------------
        ef = ttk.LabelFrame(wrap, text="  EINSTELLUNGEN  (wirken nach „Speichern & Anwenden“)  ",
                            padding=12)
        ef.pack(fill="x", pady=(10, 0))

        def add_row(r, label, var, width, tip):
            ttk.Label(ef, text=label).grid(row=r, column=0, sticky="w", pady=2)
            e = ttk.Entry(ef, textvariable=var, width=width)
            e.grid(row=r, column=1, sticky="w", padx=(8, 16), pady=2)
            if tip:
                ttk.Label(ef, text=tip, style="Muted.TLabel").grid(
                    row=r, column=2, columnspan=3, sticky="w")

        add_row(0, "Check-Intervall [s]:", v_check, 8, "Normalbetrieb, z.B. 180 = 3 Minuten")
        add_row(1, "Backoff-Intervall [s]:", v_backoff, 8, "nach wiederholten Fehlern, z.B. 3600 = 60 Minuten")
        add_row(2, "Max. Fehler in Folge:", v_maxfail, 8, "danach wird auf Backoff-Intervall umgeschaltet")
        add_row(3, "Stillstand nach [min]:", v_stall, 8, "länger keine Session-Aktivität = Nachfrage/Hänger")
        add_row(4, "Nachricht bei Fehler:", v_retry, 24, "wird als „try again“ an den Chat gesendet")
        add_row(5, "Nachricht bei Stillstand:", v_reply, 24, "Antwort auf Nachfragen, z.B. „continue“")
        add_row(6, "Hotkey Fehler:", v_hk_retry, 22, "Sondertaste F13 – muss in VS Code frei sein")
        add_row(7, "Hotkey Stillstand:", v_hk_reply, 22, "Sondertaste F14")
        add_row(8, "VS-Code-Prozess:", v_proc, 20, "Prozessname der Hauptfenster")
        add_row(9, "Fenstertitel enthält:", v_title, 24, "Filter zur Fenstersuche")
        add_row(10, "Aktiv-Fenster [h]:", v_active_h, 6, "Sessions jünger als X Stunden gelten als aktiv")
        ttk.Checkbutton(ef, text="Bei Stillstand/Nachfrage automatisch antworten",
                        variable=v_auto).grid(row=11, column=0, columnspan=4,
                                              sticky="w", pady=(6, 0))
        ttk.Checkbutton(ef, text="Alle aktiven Chat-Sessions überwachen (Multi-Session)",
                        variable=v_multi).grid(row=12, column=0, columnspan=4,
                                               sticky="w", pady=(2, 0))

        # --- Aktionsleiste ---------------------------------------------------
        bf = ttk.Frame(wrap)
        bf.pack(fill="x", pady=10)
        emoji_btn(bf, "Speichern & Anwenden", "💾", on_save, accent_btn=True).pack(side="left")
        emoji_btn(bf, "Jetzt prüfen", "🔄", on_check_now).pack(side="left", padx=(8, 0))
        emoji_btn(bf, "Status anzeigen", "📊", on_status_dialog).pack(side="left", padx=(8, 0))
        emoji_btn(bf, "Test: try again", "🔁", lambda: on_test("retry")).pack(side="left", padx=(8, 0))
        emoji_btn(bf, "Test: continue", "⏩", lambda: on_test("reply")).pack(side="left", padx=(8, 0))

        # --- Protokoll --------------------------------------------------------
        lf = ttk.LabelFrame(wrap, text="  PROTOKOLL  ", padding=6)
        lf.pack(fill="both", expand=True)
        logbox = tk.Text(lf, height=10, state="disabled", font=F_LOG, wrap="word",
                         bg=T["log_bg"], fg=T["log_fg"], relief="flat",
                         insertbackground=T["text"], padx=8, pady=6,
                         selectbackground=T["accent"], selectforeground=T["accent_text"])
        sb = ttk.Scrollbar(lf, orient="vertical", command=logbox.yview)
        logbox.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        logbox.pack(fill="both", expand=True)
        logbox.tag_configure("WARNING", foreground=T["warn"])
        logbox.tag_configure("ERROR", foreground=T["err"])
        logbox.tag_configure("sent", foreground=T["accent"])
        W["log"] = logbox
        # vorhandene Zeilen (nach Theme-Wechsel) wieder einfügen
        if state["log_lines"]:
            logbox.configure(state="normal")
            for line, tag in state["log_lines"]:
                logbox.insert("end", line + "\n", tag or ())
            logbox.see("end")
            logbox.configure(state="disabled")

        W["theme_T"] = T

    def append_log(msg):
        msg = msg.rstrip("\n")
        tag = None
        if " WARNING " in msg or msg.startswith("WARN"):
            tag = "WARNING"
        elif " ERROR " in msg or msg.startswith("FEHLER"):
            tag = "ERROR"
        elif "Hotkey gesendet" in msg or "gesendet (" in msg:
            tag = "sent"
        state["log_lines"].append((msg, tag))
        if len(state["log_lines"]) > 2000:
            del state["log_lines"][:500]
        txt = W.get("log")
        if txt is None or not txt.winfo_exists():
            return
        txt.configure(state="normal")
        txt.insert("end", msg + "\n", tag or ())
        txt.see("end")
        txt.configure(state="disabled")

    class Qtlog(logging.Handler):
        def emit(self, record):
            try:
                root.after(0, lambda: append_log(self.format(record)))
            except Exception:
                pass

    qh = Qtlog()
    qh.setFormatter(logging.Formatter("%(asctime)s  %(levelname)-7s %(message)s", "%H:%M:%S"))
    log.addHandler(qh)

    # ------------------------------------------------------------------
    # Queue-Polling: Status-UI aktualisieren
    # ------------------------------------------------------------------
    next_at = {"at": 0.0}

    def set_status_text(txt):
        state["status_core"] = txt
        refresh_status_look()

    def refresh_status_look():
        T = W.get("theme_T")
        dot = W.get("dot")
        if T is None or dot is None or not dot.winfo_exists():
            return
        st = state.get("last_status")
        key = STATUS_COLOR_KEY.get(st, "muted")
        base = T.get(key, T["muted"])
        if st == ST_RUNNING and running_var.get():
            state["pulse"] ^= 1
            dot.configure(fg=base if state["pulse"] else T["border"])
        else:
            dot.configure(fg=base)
        core = state.get("status_core", "warte auf ersten Check…")
        if not running_var.get():
            status_var.set("⏸  pausiert" if PIL_OK else "pausiert")
        else:
            pre = STATUS_EMOJI.get(st, "•") + "  " if PIL_OK else ""
            status_var.set(pre + core)

    def poll_queue():
        try:
            while True:
                msg = ui_q.get_nowait()
                if msg["kind"] == "status":
                    # Einzelnachricht: nur nachrichten-relevante Felder übernehmen;
                    # die aggregierte Anzeige macht die "sessions"-Nachricht.
                    if msg.get("sent"):
                        which = "try again" if msg.get("which") == "retry" else "continue"
                        state["status_core"] = "[%s]  →  „%s“ gesendet" % (
                            msg.get("label", "?"), which)
                        state["last_status"] = msg.get("status")
                        refresh_status_look()
                elif msg["kind"] == "planned":
                    next_at["at"] = msg.get("at", 0.0)
                elif msg["kind"] == "sessions":
                    session_rows["rows"] = msg.get("sessions", [])
                    rows = session_rows["rows"]
                    if rows:
                        # Aggregat für die Status-Karte
                        counts = {}
                        for r in rows:
                            st = r.get("status") or ST_EMPTY
                            counts[st] = counts.get(st, 0) + 1
                        prio = [ST_ERROR, ST_STALLED, ST_RUNNING, ST_COMPLETE, ST_EMPTY]
                        worst = next((p for p in prio if p in counts), ST_EMPTY)
                        state["last_status"] = worst
                        parts = []
                        for st in prio:
                            if st in counts:
                                parts.append("%d %s" % (counts[st],
                                         STATUS_TEXT_DE.get(st, st).split(" (")[0].lower()))
                        state["status_core"] = "%d Session%s: %s" % (
                            len(rows), "en" if len(rows) != 1 else "", ", ".join(parts))
                        refresh_status_look()
                        if worst == ST_ERROR:
                            err_rows = [r for r in rows if r.get("status") == ST_ERROR]
                            detail_var.set(err_rows[0].get("detail", "")[:220])
                        elif rows:
                            newest = max(rows, key=lambda r: r.get("mtime", 0))
                            detail_var.set("zuletzt aktiv: %s" % newest.get("label", "-"))
                        session_var.set("%d aktiv" % len(rows))
                        fails = sum(r.get("failures", 0) for r in rows)
                        failures_var.set(str(fails))
                        mode_var.set("Backoff" if any(r.get("backoff") for r in rows) else "Normal")
                    else:
                        state["last_status"] = "NO_SESSION"
                        state["status_core"] = "keine aktiven Sessions"
                        refresh_status_look()
                    tree = W.get("sessions_tree")
                    T = W.get("theme_T")
                    if tree is not None and tree.winfo_exists() and T is not None:
                        _render_sessions(tree, T)
        except queue.Empty:
            pass
        # Countdown + Chips
        if dog.paused.is_set() or not dog.is_alive():
            countdown_var.set("pausiert")
        else:
            rem = next_at["at"] - time.time()
            if rem > 0:
                m, s = divmod(int(rem), 60)
                countdown_var.set("in %02d:%02d" % (m, s))
            else:
                countdown_var.set("jetzt…")
        chip_c, chip_m, chip_f = W.get("chip_countdown"), W.get("chip_mode"), W.get("chip_failures")
        if chip_c is not None and chip_c.winfo_exists():
            chip_c.configure(text="⏱ nächster Check: %s" % countdown_var.get())
            chip_m.configure(text="⚙ %s (%d min Takt)" % (
                mode_var.get(),
                (dog.cfg.get("backoff_interval_sec", 3600) // 60
                 if mode_var.get() == "Backoff" else dog.cfg.get("check_interval_sec", 180) // 60)))
            chip_f.configure(text="⚠ Fehler: %s" % failures_var.get())
        refresh_status_look()
        root.after(500, poll_queue)

    def on_close():
        dog.stop_event.set()
        dog.wake.set()
        try:
            c = collect_cfg()
            c["gui_geometry"] = root.geometry()
            save_config(c)
        except Exception:
            pass
        root.after(150, root.destroy)

    root.protocol("WM_DELETE_WINDOW", on_close)

    build_ui()
    root.after(500, poll_queue)
    dog.start()
    root.mainloop()


# ----------------------------------------------------------------------------
# CLI-Modi
# ----------------------------------------------------------------------------

def cmd_status(cfg):
    sessions = find_active_sessions(cfg)
    if not sessions:
        print("Keine aktiven Chat-Sessions im Zeitfenster (%d h)."
              % cfg.get("session_active_window_hours", 12))
    else:
        print("Aktive Chat-Sessions: %d" % len(sessions))
        for i, s in enumerate(sessions, 1):
            store = parse_session_file(s["path"])
            st, det = evaluate_status(store, s["mtime"], cfg)
            print("  %d. [%s] %s | %s" % (i, s["ws_label"], STATUS_TEXT_DE.get(st, st), det[:80]))
            print("     Session: %s" % os.path.basename(s["path"]))
    wins = find_vscode_windows(cfg)
    for w in wins:
        print("Fenster : [%s] %s" % ("minimiert" if w["iconic"] else "aktiv", w["title"][:90]))
    if not wins:
        print("Fenster : keines gefunden (Prozess %r)" % cfg["vscode_process_name"])
    return 0


def cmd_once(cfg):
    dog = Watchdog(cfg)
    dog.do_check()
    return 0


def main():
    ap = argparse.ArgumentParser(description="VS Code (Insiders) Chat-Watchdog")
    ap.add_argument("--gui", action="store_true", help="GUI starten (Standard)")
    ap.add_argument("--cli", action="store_true", help="ohne GUI laufen lassen (Endlosschleife)")
    ap.add_argument("--status", action="store_true", help="einmal Status anzeigen und beenden")
    ap.add_argument("--once", action="store_true", help="einmal prüfen (und ggf. handeln)")
    ap.add_argument("--setup-keybindings", action="store_true", help="nur Keybindings anlegen/aktualisieren")
    ap.add_argument("--send", choices=["retry", "reply"], help="Nachricht manuell senden (Test)")
    ap.add_argument("--no-console-log", action="store_true")
    args = ap.parse_args()

    cfg = load_config()
    setup_logging(cfg, console=not args.no_console_log)

    if args.status:
        sys.exit(cmd_status(cfg))
    if args.setup_keybindings:
        sys.exit(0 if ensure_keybindings(cfg) else 1)
    if args.send:
        sys.exit(0 if send_message_to_chat(cfg, args.send) else 1)
    if args.once:
        sys.exit(cmd_once(cfg))
    if args.cli:
        dog = Watchdog(cfg)
        dog.start()
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            dog.stop_event.set()
            dog.wake.set()
            time.sleep(1)
        sys.exit(0)

    # Standard: GUI
    run_gui(cfg)


if __name__ == "__main__":
    main()
