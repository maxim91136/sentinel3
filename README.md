# Sentinel3

Wacht über die Projekte von [Dreier Labs](https://dreierlabs.com) und die Dienste, an denen sie hängen.

- **Prüft** (Liste: `checks.json`):
  - `sites`: die eigenen Seiten und Schnittstellen (erreichbar, richtiger Inhalt, keine Ersatzwerte)
  - `fresh`: wie alt die Daten der Projekte sind (Zeitfeld in JSON, `*` für alle Einträge, oder Zeitstempel im Seitentext)
  - `workflows`: ob Datenläufe in den Projekt-Repos gescheitert sind
  - `domains`: wann die Domains ablaufen (Warnung 14 Tage vorher)
  - `status_pages`: Statusseiten von GitHub und Cloudflare
  - `deps`: externe Datenquellen aller Projekte. Mit `"prio": "low"` sind es Nebenquellen: kein Sofort-Alarm, nur im Morgenreport.
  - In Adressen sind die Platzhalter `{Y}`, `{D1}`, `{D2}` (Jahr, gestern, vorgestern) und `{W1}`, `{W2}` (Datum ohne Striche) erlaubt.
- **Rhythmus:** volle Prüfung alle 3 Stunden (gemessen ab der letzten Prüfung, verspätete Läufe gehen nicht verloren); bei einem wichtigen Problem stündlich, bis alles wieder läuft. Jede Prüfung wird bei Fehlern einmal wiederholt, um Wackler auszufiltern.
- **Meldungen** per Telegram: 🔴 Problem, 🟢 Entwarnung mit Ausfalldauer, 🟠 Erinnerung alle 6 Stunden bei Dauerstörung (nur 7 bis 22 Uhr). Dazu jeden Morgen beim ersten Takt ab 7 Uhr ein Morgenreport mit Gesamtstand, Problemen, gestörten Nebenquellen, Alter der Daten und nächstem Domain-Ablauf. Kommt eine Meldung bei Telegram nicht an, endet der Lauf als Fehler und der nächste Lauf versucht es erneut.
- **Zustand:** `state.json` (wird nur bei Änderungen geschrieben).

Einrichtung: Secrets `TELEGRAM_BOT_TOKEN` und `TELEGRAM_CHAT_ID` (private Chat-ID). Actions → sentinel → Run workflow mit `setup` zeigt Chat-IDs, mit `test` kommt eine Testnachricht, mit `run` wird sofort geprüft, mit `probe` und Adressen im Feld `urls` zeigt der Lauf, was GitHub von dort zurückbekommt (zum Testen neuer Prüfungen).

© 2026 Steve Dreier · MIT-Lizenz

## Takt von außen (Cloudflare-Worker)

GitHubs eigener Zeitplan läuft oft stundenlang zu spät. Deshalb stößt ein kleiner Cloudflare-Worker (Ordner `worker/`) den Workflow jede Stunde per `workflow_dispatch` mit `mode=tick` an. Der Zeitplan im Workflow bleibt als Reserve. Läuft beides kurz nacheinander, überspringt Sentinel die zweite Prüfung (weniger als 20 Minuten Abstand).

Derselbe Worker stößt auch den Daten-Workflow von Cyberlage an (alle 2 Stunden und zwei Morgenläufe), weil dessen GitHub-Zeitplan ebenfalls unzuverlässig läuft. Der Zeitplan dort bleibt als Reserve; doppelte Läufe schaden nicht.

Einrichtung: Worker `sentinel3-ticker` aus `worker/` anlegen, Cron-Trigger wie in `wrangler.jsonc`, Secret `GH_TOKEN` (fine-grained Token, nur die Repos `sentinel3` und `cyberlage.org`, „Actions: Read and write“).

## Seiten hinter Cloudflare Access (3Kings)

Für Prüfungen mit `"access": true` in `checks.json` sendet der Sentinel einen Cloudflare-Access-Service-Token mit (Secrets `CF_ACCESS_CLIENT_ID` und `CF_ACCESS_CLIENT_SECRET`). Fehlen sie, wird die Prüfung übersprungen und als solche vermerkt. Der Token gilt nur für die Access-Anwendung von 3Kings und darf dort nur lesen.
