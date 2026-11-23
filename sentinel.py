"""Sentinel: prüft Seiten und Dienste, meldet Änderungen per Telegram.
Normalbetrieb: volle Prüfung alle 3 Stunden. Problemmodus: stündlich, bis alles wieder läuft.
Meldet nur Änderungen: neu ausgefallen, wieder in Ordnung, Erinnerung bei Dauerproblem alle 6 Stunden."""
import json, os, sys, time, datetime as dt, urllib.request, urllib.error
from concurrent.futures import ThreadPoolExecutor

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")
MODE = (sys.argv[1] if len(sys.argv) > 1 else "run").strip() or "run"
# "tick" = Takt von außen (Cloudflare-Worker), verhält sich wie ein geplanter Lauf
MANUAL = os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch" and MODE != "tick"
UA = {"User-Agent": "Mozilla/5.0 (compatible; sentinel3/1.0; +https://github.com/maxim91136/sentinel3)"}
NOW = dt.datetime.now(dt.timezone.utc)
STATE_F = "state.json"
REMIND_H = 6

def fetch(url, timeout=20):
    req = urllib.request.Request(url, headers=UA)
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read(400_000)
        return r.status, body, round((time.time() - t0) * 1000), r.geturl()

def tg(method, **kw):
    data = urllib.parse.urlencode(kw).encode() if kw else None
    with urllib.request.urlopen(f"https://api.telegram.org/bot{TOKEN}/{method}", data=data, timeout=20) as r:
        return json.load(r)

def send(text):
    if not (TOKEN and CHAT):
        print("Telegram nicht eingerichtet, Nachricht wäre:\n" + text); return
    for c in [c.strip() for c in CHAT.split(",") if c.strip()]:
        try: tg("sendMessage", chat_id=c, text=text, parse_mode="HTML", disable_web_page_preview="true")
        except Exception as e: print("Senden fehlgeschlagen:", e)

def once(kind, c):
    """Gibt (ok, detail) zurück."""
    if kind == "dep" and c["url"] == "telegram:getMe":
        if not TOKEN: return True, "übersprungen (kein Token)"
        r = tg("getMe"); return bool(r.get("ok")), "Bot erreichbar" if r.get("ok") else str(r)
    status, body, ms, final = fetch(c["url"])
    if kind == "site":
        ok = status == 200 and (c["expect"].encode() in body or (c.get("or_access") and ("cloudflareaccess.com" in final or b"Cloudflare Access" in body)))
        return ok, f"{status}, {ms} ms" + ("" if ok else ", erwarteter Inhalt fehlt")
    if kind == "status":
        ind = json.loads(body)["status"]["indicator"]
        return ind not in ("major", "critical"), f"Status: {ind}"
    if kind == "fresh":
        ts = json.loads(body).get(c["field"])
        t = dt.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        if t.tzinfo is None: t = t.replace(tzinfo=dt.timezone.utc)
        h = (NOW - t).total_seconds() / 3600
        return h <= c["max_hours"], f"Daten {h:.0f} h alt"
    return 200 <= status < 300, f"{status}, {ms} ms"

def check(item):
    kind, c = item
    last = None
    for attempt in range(2):
        try:
            ok, det = once(kind, c)
            if ok: return c["name"], True, det, c.get("used_by", [c["name"]])
            last = det
        except urllib.error.HTTPError as e: last = f"HTTP {e.code}"
        except Exception as e: last = type(e).__name__ + (": " + str(e)[:80] if str(e) else "")
        if attempt == 0: time.sleep(8)
    return c["name"], False, last, c.get("used_by", [c["name"]])

def load_state():
    try: return json.load(open(STATE_F))
    except Exception: return {"mode": "normal", "failing": {}}

def hrs(a, b): return (b - a).total_seconds() / 3600
def ts(): return NOW.strftime("%Y-%m-%dT%H:%MZ")
from zoneinfo import ZoneInfo
def berlin(t): return t.astimezone(ZoneInfo("Europe/Berlin")).strftime("%d.%m. %H:%M")
def dur(h): return f"{h*60:.0f} Min." if h < 1 else f"{h:.1f}".replace(".", ",") + " Std."

def main():
    if MODE == "setup":
        r = tg("getUpdates")
        ids = {f'{m["chat"]["id"]} ({m["chat"].get("type")}{": " + (m["chat"].get("title") or m["chat"].get("username") or "") })'
               for u in r.get("result", []) for m in [u.get("message") or u.get("channel_post") or {}] if m.get("chat")}
        print("Chat-IDs (vorher dem Bot privat eine Nachricht schreiben):"); [print(" ", i) for i in sorted(ids)] or print("  keine gefunden"); return
    if MODE == "test":
        send("🛡️ <b>Sentinel3</b>: Testnachricht. Wenn du das liest, kommen Meldungen an."); return

    st = load_state()
    lf = st.get("last_full")
    due = (not lf) or hrs(dt.datetime.fromisoformat(lf.replace("Z", "+00:00")), NOW) >= 2.8
    if st.get("mode") == "normal" and not MANUAL and not due:
        print("Normalbetrieb, letzte volle Prüfung", lf, "- nächste nach 3 Stunden."); return
    if not MANUAL and lf and hrs(dt.datetime.fromisoformat(lf.replace("Z", "+00:00")), NOW) < 0.33:
        print("Letzte Prüfung vor weniger als 20 Minuten, übersprungen."); return

    cfg = json.load(open("checks.json"))
    items = [("site", c) for c in cfg["sites"]] + [("fresh", c) for c in cfg["fresh"]] + \
            [("status", c) for c in cfg["status_pages"]] + [("dep", c) for c in cfg["deps"]]
    with ThreadPoolExecutor(8) as ex:
        res = list(ex.map(check, items))
    for n, ok, det, _ in res: print(("OK  " if ok else "FEHL"), n, "·", det)

    fail = {n: (det, ub) for n, ok, det, ub in res if not ok}
    old = st.get("failing", {})
    msgs = []
    new = [n for n in fail if n not in old]
    # Entwarnung erst nach zwei erfolgreichen Prüfungen in Folge (gegen Flackern)
    recovering = {n: old[n].get("ok_streak", 0) + 1 for n in old if n not in fail}
    gone = [n for n, k in recovering.items() if k >= 2]
    hold = {n: k for n, k in recovering.items() if k < 2}
    if new:
        lines = [f"🔴 <b>{n}</b>: {fail[n][0]}\n   betrifft: {', '.join(fail[n][1])}" for n in new]
        msgs.append("🛡️ <b>Sentinel3: Problem</b>\n\n" + "\n".join(lines) + "\n\nIch prüfe ab jetzt stündlich.")
    if gone:
        lines = [f"🟢 <b>{n}</b> wieder in Ordnung (Ausfall ca. {dur(hrs(dt.datetime.fromisoformat(old[n]['since'].replace('Z','+00:00')), NOW))})" for n in gone]
        msgs.append("🛡️ <b>Sentinel3: Entwarnung</b>\n\n" + "\n".join(lines) + ("\n\nAlles läuft, zurück auf alle 3 Stunden." if not fail and not hold else ""))
    loc_h = NOW.astimezone(ZoneInfo("Europe/Berlin")).hour
    quiet = not (7 <= loc_h < 22)
    remind = [] if quiet else [n for n in fail if n in old and hrs(dt.datetime.fromisoformat(old[n]["last_alert"].replace("Z", "+00:00")), NOW) >= REMIND_H]
    if remind:
        lines = [f"🟠 <b>{n}</b> weiterhin gestört seit {berlin(dt.datetime.fromisoformat(old[n]['since'].replace('Z','+00:00')))} Uhr: {fail[n][0]}" for n in remind]
        msgs.append("🛡️ <b>Sentinel3: Erinnerung</b>\n\n" + "\n".join(lines))

    nf = {}
    for n, (det, ub) in fail.items():
        o = old.get(n, {})
        nf[n] = {"since": o.get("since", ts()), "last_alert": ts() if (n in new or n in remind) else o.get("last_alert", ts()), "detail": det, "ok_streak": 0}
    for n, k in hold.items():
        nf[n] = dict(old[n], ok_streak=k)
    new_state = {"mode": "problem" if nf else "normal", "failing": nf, "last_full": ts(), "last_weekly": st.get("last_weekly")}
    loc = NOW.astimezone(ZoneInfo("Europe/Berlin")); wk = f"{loc.isocalendar()[0]}-W{loc.isocalendar()[1]:02d}"
    if loc.weekday() == 0 and loc.hour >= 7 and st.get("last_weekly") != wk and not MANUAL:
        ok_n = len(res) - len(fail)
        w = f"🛡️ <b>Sentinel3: Wochencheck</b>\n\nIch laufe. ✅ {ok_n} von {len(res)} in Ordnung"
        if fail: w += "\n" + "\n".join(f"🔴 <b>{n}</b>: {d}" for n, (d, _) in fail.items())
        msgs.append(w); new_state["last_weekly"] = wk
    if MANUAL and not msgs:
        ok_n = len(res) - len(fail)
        txt = f"🛡️ <b>Sentinel3: Manuelle Prüfung</b>\n\n✅ {ok_n} von {len(res)} in Ordnung"
        if fail:
            txt += "\n" + "\n".join(f"🔴 <b>{n}</b>: {d} (bekannt seit {berlin(dt.datetime.fromisoformat(old[n]['since'].replace('Z','+00:00')))} Uhr)" if n in old else f"🔴 <b>{n}</b>: {d}" for n, (d, _) in fail.items())
        msgs.append(txt)
    for m in msgs: send(m)
    if new_state != st:
        json.dump(new_state, open(STATE_F, "w"), ensure_ascii=False, indent=1)

if __name__ == "__main__":
    import urllib.parse
    main()
