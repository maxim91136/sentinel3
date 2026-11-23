# Sentinel3

Wacht über die Projekte von [Dreier Labs](https://dreierlabs.com) und die Dienste, an denen sie hängen.

- **Prüft:** die eigenen Seiten (erreichbar, richtiger Inhalt), die Aktualität der 3Kings-Daten, die Statusseiten von GitHub und Cloudflare sowie externe Datenquellen (Yahoo Finance, Coinbase, Binance, FRED, CoinGecko, mempool.space, Tor Onionoo, Bitnodes, Telegram). Liste: `checks.json`.
- **Rhythmus:** volle Prüfung alle 3 Stunden (gemessen ab der letzten Prüfung, verspätete Läufe gehen nicht verloren); bei einem Problem stündlich, bis alles wieder läuft. Jede Prüfung wird bei Fehlern einmal wiederholt, um Wackler auszufiltern.
- **Meldungen** per Telegram, nur bei Änderungen: 🔴 Problem, 🟢 Entwarnung mit Ausfalldauer, 🟠 Erinnerung alle 6 Stunden bei Dauerstörung (nur 7 bis 22 Uhr). Montags ein kurzer Wochencheck als Lebenszeichen. Sonst bleibt es still.
- **Zustand:** `state.json` (wird nur bei Änderungen geschrieben).

Einrichtung: Secrets `TELEGRAM_BOT_TOKEN` und `TELEGRAM_CHAT_ID` (private Chat-ID). Actions → sentinel → Run workflow mit `setup` zeigt Chat-IDs, mit `test` kommt eine Testnachricht, mit `run` wird sofort geprüft.

© 2026 Steve Dreier · MIT-Lizenz

## Takt von außen (Cloudflare-Worker)

GitHubs eigener Zeitplan läuft oft stundenlang zu spät. Deshalb stößt ein kleiner Cloudflare-Worker (Ordner `worker/`) den Workflow jede Stunde per `workflow_dispatch` mit `mode=tick` an. Der Zeitplan im Workflow bleibt als Reserve. Läuft beides kurz nacheinander, überspringt Sentinel die zweite Prüfung (weniger als 20 Minuten Abstand).

Einrichtung: Worker `sentinel3-ticker` aus `worker/` anlegen, Cron-Trigger `7 * * * *`, Secret `GH_TOKEN` (fine-grained Token, nur dieses Repo, „Actions: Read and write“).
