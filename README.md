# VS Code Chat-Watchdog

Überwacht automatisch die **letzte aktive Copilot-Chat-Session** in
*Visual Studio Code - Insiders* und hält sie in Abwesenheit des Benutzers am Laufen.

## Was macht das Programm?

| Beobachteter Zustand | Aktion |
|---|---|
| Query läuft noch | nichts tun, im Intervall erneut prüfen |
| Fehler (Connection Error, Rate-/Quota-Limit, Timeout, …) | sendet `try again` an den Chat |
| Stillstand / Nachfrage des Chats (kein Fortschritt seit X Minuten) | sendet `continue` |
| Mehr als 3 Eingriffe in Folge (pro Session) | Prüfintervall von 3 min auf 60 min strecken (Backoff, pro Session) |
| Danach läuft es wieder | zurück zum 3-Minuten-Takt |

### Multi-Session-Betrieb

Der Watchdog überwacht **alle aktiven Chat-Sessions gleichzeitig** (Standard:
alle Sessions, deren Datei in den letzten 12 h geschrieben wurde):

* Pro `workspaceStorage`-Ordner zählt die jüngste Session (die im Chat-View
  des zugehörigen Fensters offene).
* Jede Session wird über die `workspace.json` ihres Ordners dem passenden
  VS-Code-Fenster zugeordnet (Ordnername bzw. „Workspace (Multi-Root)“) –
  „try again“/„continue“ geht also an das **richtige** Fenster.
* Fehlerzähler und Backoff gelten **pro Session**: eine hängende Session
  blockiert die Betreuung der anderen nicht.
* Die GUI zeigt eine Session-Tabelle mit Status, Fehlerzähler, Modus und
  letzter Aktivität; die Status-Karte aggregiert („2 Sessions: 1 query läuft,
  1 fehler").
* Sendungen an verschiedene Fenster erfolgen zeitversetzt
  (`inter_send_delay_sec`, Standard 2 s).

Mit `multi_session: false` wird nur die jeweils zuletzt aktive Session
betreut (Altsverhalten).

## Wie funktioniert es?

1. **Status-Erkennung** – VS Code schreibt jede Chat-Session fortlaufend als
   JSONL-Datei nach
   `%APPDATA%\Code - Insiders\User\workspaceStorage\<ws>\chatSessions\<session>.jsonl`.
   Das Programm nimmt die zuletzt geänderte Datei (= letzte aktive Session) und
   rekonstruiert daraus den Zustand des letzten Requests:
   * kein `result` → Query **läuft**
   * `result.errorDetails` → **Fehler** (inkl. Meldung, z. B. Rate-Limit-Reset-Zeit)
   * `result.timings` → **abgeschlossen**
   * kein `result` und Datei seit > `stall_threshold_min` Minuten unverändert → **Stillstand**
2. **Aktion** – Das Programm fokussiert das VS-Code-Fenster und drückt eine
   Sondertasten-Kombination (F13/F14), die in `keybindings.json` auf
   `workbench.action.chat.open` mit `{"query": "try again"}` bzw.
   `{"query": "continue"}` gebunden ist. Das ist der offizielle Befehl, der eine
   Nachricht an den Chat sendet – keine fragilen Bildschirm-Koordinaten.
   Die Keybindings werden vom Programm automatisch (idempotent, mit Backup)
   angelegt/aktualisiert.

## Start

```bat
start_watchdog.bat                  :: GUI (Standard, ohne Konsolenfenster, via pythonw.exe)
start_watchdog.bat --cli            :: reine Konsole (Endlosschleife)
start_watchdog.bat --status         :: nur Status anzeigen
start_watchdog.bat --once           :: einmal prüfen + ggf. handeln
start_watchdog.bat --setup-keybindings
start_watchdog.bat --send retry     :: manuell "try again" senden (Test)
start_watchdog.bat --send reply     :: manuell "continue" senden (Test)
```

## Konfiguration (`config.json`)

| Schlüssel | Default | Bedeutung |
|---|---|---|
| `check_interval_sec` | `180` | Normaler Prüftakt (3 Minuten) |
| `backoff_interval_sec` | `3600` | Prüftakt nach wiederholten Fehlern (60 Minuten) |
| `max_consecutive_failures` | `3` | ab dem 4. Fehler in Folge → Backoff |
| `stall_threshold_min` | `10` | ab wann "kein Fortschritt" als Stillstand gilt |
| `retry_message` | `try again` | Nachricht bei Fehler |
| `question_reply_message` | `continue` | Nachricht bei Stillstand/Nachfrage |
| `auto_reply_on_stall` | `true` | bei Stillstand automatisch antworten? |
| `hotkey_retry` / `hotkey_reply` | `ctrl+alt+shift+f13` / `f14` | Tasten für die Aktionen |
| `send_delay_after_focus_ms` | `600` | Wartezeit nach Fokussieren des Fensters |
| `multi_session` | `true` | alle aktiven Sessions überwachen (sonst nur die letzte) |
| `session_active_window_hours` | `12` | Sessions jünger als X Stunden gelten als aktiv |
| `inter_send_delay_sec` | `2.0` | Pause zwischen Sendungen an verschiedene Fenster |
| `dark_mode` | `true` | GUI in Dunkel (`true`) oder Hell (`false`) |
| `accent_color` | `#0078D7` | Akzentfarbe der GUI (Blau) |
| `gui_geometry` | `""` | gemerkte Fensterposition/-größe |

Alle Werte sind auch direkt in der GUI änderbar („Speichern & Anwenden").

## GUI

* Look: dunkles/helles Theme (Umschalter ☀️/🌙 oben rechts, wird gespeichert),
  Akzentfarbe `#0078D7`, Segoe-UI-Schrift, abgerundete Emoji-Icons (PIL).
* Status-Karte mit farbigem Puls-Indikator (läuft = blau pulsierend,
  Fehler = rot, Stillstand = gelb, fertig = grün), Chips für Countdown,
  Fehlerzähler und Modus (Normal/Backoff).
* Emoji-Buttons: 💾 Speichern & Anwenden, 🔄 Jetzt prüfen, 📊 Status anzeigen,
  🔁 Test: try again, ⏩ Test: continue, ⏸/▶ Pause/Start.
* Farblich hinterlegtes Protokoll (Warnungen gelb, Fehler rot, Sendungen blau).
* Programm-Icon: 🤖 auf Blau, wird beim Start als `vscode_watchdog.ico`
  generiert und für das Fenster gesetzt (funktioniert auch als Taskleisten-Icon).

## Dateien

* `vscode_watchdog.py` – das Programm
* `vscode_watchdog.config.json` – Konfiguration
* `vscode_watchdog.log` – Protokoll (rotiert bei 2 MB)

## Voraussetzungen

* Windows, Python 3.8+ mit `psutil` (zum Identifizieren der VS-Code-Fenster)
* VS Code - Insiders muss laufen und der Ziel-Chat darf nicht in einem
  privaten/inkognito-ähnlichen Zustand sein. Der Chat-View muss existieren
  (muss nicht sichtbar im Vordergrund sein – das Fenster wird automatisch
  geholt).

 
 
Author
Sebastian Fischer post@scriptometer.de;
