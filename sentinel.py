"""Sentinel: prüft Seiten, Datenläufe, Domains und Quellen, meldet Änderungen per Telegram.
Normalbetrieb: volle Prüfung alle 3 Stunden. Problemmodus: stündlich, bis alles wieder läuft.
Meldet sofort nur Wichtiges (neu ausgefallen, wieder in Ordnung, eine Erinnerung nach 6 Stunden).
Nebenquellen ("prio": "low") stehen nur im Morgenreport."""
import html, json, os, re, sys, time, datetime as dt, urllib.request, urllib.error, urllib.parse
from concurrent.futures import ThreadPoolExecutor
from zoneinfo import ZoneInfo

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")
GH = os.environ.get("GH_TOKEN", "")
MODE = (sys.argv[1] if len(sys.argv) > 1 else "run").strip() or "run"
# "tick" = Takt von außen (Cloudflare-Worker), verhält sich wie ein geplanter Lauf
MANUAL = os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch" and MODE != "tick"
UA = {"User-Agent": "Mozilla/5.0 (compatible; sentinel3/1.0; +https://github.com/maxim91136/sentinel3)"}
NOW = dt.datetime.now(dt.timezone.utc)
STATE_F = "state.json"
REMIND_H = 6
SEND_FAILED = False

def fill(url):
    """Platzhalter in URLs: {Y} Jahr, {D1}/{D2} Datum gestern/vorgestern (JJJJ-MM-TT), {W1}/{W2} dasselbe als JJJJMMTT."""
    d1, d2 = NOW - dt.timedelta(days=1), NOW - dt.timedelta(days=2)
    for k, v in {"{Y}": str(NOW.year), "{D1}": d1.strftime("%Y-%m-%d"), "{D2}": d2.strftime("%Y-%m-%d"),
                 "{W1}": d1.strftime("%Y%m%d"), "{W2}": d2.strftime("%Y%m%d")}.items():
        url = url.replace(k, v)
    return url

def fetch(url, timeout=20, access=False, limit=400_000, token=None):
    url = fill(url)
    h = dict(UA)
    if access:  # Cloudflare Access: Service-Token (nur für Seiten hinter Access)
        h["CF-Access-Client-Id"] = os.environ.get("CF_ACCESS_CLIENT_ID", "")
        h["CF-Access-Client-Secret"] = os.environ.get("CF_ACCESS_CLIENT_SECRET", "")
    if url.startswith("https://api.github.com/"):
        h["Accept"] = "application/vnd.github+json"
        if token or GH: h["Authorization"] = "Bearer " + (token or GH)
    req = urllib.request.Request(url, headers=h)
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read(limit)
        return r.status, body, round((time.time() - t0) * 1000), r.geturl()

def tg(method, **kw):
    data = urllib.parse.urlencode(kw).encode() if kw else None
    with urllib.request.urlopen(f"https://api.telegram.org/bot{TOKEN}/{method}", data=data, timeout=20) as r:
        return json.load(r)

def send(text):
    global SEND_FAILED
    if not (TOKEN and CHAT):
        print("Telegram nicht eingerichtet, Nachricht wäre:\n" + text); return
    if len(text) > 3900: text = text[:3900].rsplit("\n", 1)[0] + "\n… (gekürzt, Rest im Lauf-Protokoll)"
    for c in [c.strip() for c in CHAT.split(",") if c.strip()]:
        try: tg("sendMessage", chat_id=c, text=text, parse_mode="HTML", disable_web_page_preview="true")
        except Exception as e:
            SEND_FAILED = True
            print("Senden fehlgeschlagen:", type(e).__name__, "- Nachricht wäre:\n" + text)

def when(x):
    """Zeitstempel lesen: ISO-Text oder Unix-Zeit (Sekunden oder Millisekunden)."""
    if isinstance(x, (int, float)) and not isinstance(x, bool):
        return dt.datetime.fromtimestamp(x / 1000 if x > 1e11 else x, dt.timezone.utc)
    t = dt.datetime.fromisoformat(str(x).replace("Z", "+00:00"))
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)

def dig(v, keys):
    """Pfad in JSON verfolgen; '*' steht für alle Einträge einer Ebene."""
    if not keys: return [v]
    k, rest = keys[0], keys[1:]
    if k == "*":
        items = list(v.values()) if isinstance(v, dict) else v if isinstance(v, list) else []
        return [x for i in items for x in dig(i, rest)]
    if isinstance(v, dict): return dig(v.get(k), rest)
    if isinstance(v, list) and k.lstrip("-").isdigit() and -len(v) <= int(k) < len(v): return dig(v[int(k)], rest)
    return [None]

def fresh(c, body):
    if c.get("regex"):  # Zeitstempel im sichtbaren Seitentext (ohne HTML-Tags), Gruppen: Tag, Monat, Jahr, Stunde, Minute (UTC)
        text = " ".join(re.sub(r"<[^>]+>", " ", body.decode("utf-8", "replace")).split())
        ms = re.findall(c["regex"], text)
        if not ms: return False, "kein Zeitstempel auf der Seite gefunden"
        times = [dt.datetime(int(y), int(mo), int(d), int(h), int(mi), tzinfo=dt.timezone.utc) for d, mo, y, h, mi in ms]
        h = (NOW - max(times)).total_seconds() / 3600
        return h <= c["max_hours"], f"Stand {h:.0f} h alt"
    vals = dig(json.loads(body), c["field"].split("."))
    if not vals or any(v is None for v in vals): return False, "Zeitfeld fehlt in den Daten"
    h = (NOW - min(when(v) for v in vals)).total_seconds() / 3600  # bei mehreren zählt der älteste
    return h <= c["max_hours"], f"Daten {h:.0f} h alt"

def workflows(c):
    """Datenläufe eines Repos: Alarm, wenn der jüngste abgeschlossene Lauf eines Workflows gescheitert ist.
    Bei häufig laufenden Workflows (Abstand unter 2 Stunden) erst nach zwei Fehlschlägen in Folge."""
    api = f"https://api.github.com/repos/{c['repo']}/actions"
    tok = os.environ.get(c["token_env"], "") if c.get("token_env") else GH
    since = (NOW - dt.timedelta(days=7)).strftime("%Y-%m-%d")
    _, body, _, _ = fetch(f"{api}/runs?status=failure&per_page=50&created=%3E{since}", timeout=30, limit=20_000_000, token=tok)
    seen = {}
    for r in json.loads(body).get("workflow_runs", []):
        if r.get("event") != "pull_request": seen.setdefault(r["workflow_id"], r.get("name") or "?")
    bad_c = ("failure", "timed_out", "startup_failure")
    bad = []
    for wid, name in list(seen.items())[:12]:
        _, b, _, _ = fetch(f"{api}/workflows/{wid}/runs?status=completed&per_page=2", timeout=30, limit=5_000_000, token=tok)
        last = json.loads(b).get("workflow_runs", [])
        if not last or last[0].get("conclusion") not in bad_c: continue
        rare = len(last) < 2 or (when(last[0]["created_at"]) - when(last[1]["created_at"])).total_seconds() > 7200
        if rare or last[1].get("conclusion") in bad_c: bad.append(name)
    if bad: return False, "gescheitert: " + ", ".join(sorted(bad))
    return True, "Läufe in Ordnung"

def domain(c):
    """Ablaufdatum der Domain (RDAP). Ist die Auskunft nicht erreichbar, gilt das nicht als Problem."""
    try:
        _, body, _, _ = fetch("https://rdap.org/domain/" + c["domain"], timeout=25)
        exp = [e["eventDate"] for e in json.loads(body).get("events", []) if e.get("eventAction") == "expiration"]
        days = (when(exp[0]) - NOW).days
    except Exception:
        return True, "übersprungen (Ablaufdatum nicht abrufbar)"
    return days >= c.get("min_days", 14), f"läuft in {days} Tagen ab"

def once(kind, c):
    """Gibt (ok, detail) zurück."""
    if kind == "workflows":
        if not (os.environ.get(c["token_env"]) if c.get("token_env") else GH): return True, "übersprungen (kein GitHub-Token)"
        return workflows(c)
    if kind == "domain": return domain(c)
    if kind == "dep" and c["url"] == "telegram:getMe":
        if not TOKEN: return True, "übersprungen (kein Token)"
        r = tg("getMe"); return bool(r.get("ok")), f"Bot @{r['result'].get('username', '?')} erreichbar" if r.get("ok") else str(r)
    if kind == "dep" and c["url"].startswith("telegram:getChat:"):
        cid = os.environ.get(c["url"].split(":", 2)[2], "")
        if not (TOKEN and cid): return True, "übersprungen (keine Chat-ID hinterlegt)"
        try: ok = bool(tg("getChat", chat_id=cid).get("ok"))
        except urllib.error.HTTPError: ok = False
        return ok, "Kanal erreichbar" if ok else "Bot hat keinen Zugriff auf den Kanal"
    if c.get("access") and not os.environ.get("CF_ACCESS_CLIENT_ID"):
        return True, "übersprungen (kein Access-Token hinterlegt)"
    status, body, ms, final = fetch(c["url"], access=bool(c.get("access")), limit=2_000 if kind == "dep" else 5_000_000)
    if c.get("access") and b"cloudflareaccess.com" in final.encode() + body[:2000]:
        return False, "Access lehnt den Token ab (Login-Seite statt Daten)"
    if kind == "site":
        if c.get("reject") and c["reject"].encode() in body:
            return False, c.get("reject_note", "liefert Ersatzwerte statt echter Daten")
        ok = status == 200 and (c["expect"].encode() in body or (c.get("or_access") and ("cloudflareaccess.com" in final or b"Cloudflare Access" in body)))
        return ok, f"{status}, {ms} ms" + ("" if ok else ", erwarteter Inhalt fehlt")
    if kind == "status":
        ind = json.loads(body)["status"]["indicator"]
        return ind not in ("major", "critical"), f"Status: {ind}"
    if kind == "fresh": return fresh(c, body)
    return 200 <= status < 300, f"{status}, {ms} ms"

def check(item):
    """Gibt (name, ok, detail, betrifft, nebensache) zurück. Bei Fehler ein zweiter Versuch."""
    kind, c = item
    meta = (c.get("used_by", [c["name"]]), c.get("prio") == "low")
    last = None
    for attempt in range(2):
        try:
            ok, det = once(kind, c)
            if ok: return (c["name"], True, html.escape(det, quote=False)) + meta
            last = det
        except urllib.error.HTTPError as e: last = f"HTTP {e.code}"
        except json.JSONDecodeError: last = "Antwort ist kein gültiges JSON"
        except TimeoutError: last = "keine Antwort (Zeitüberschreitung)"
        except urllib.error.URLError as e:
            r = str(e.reason)
            last = "keine Antwort (Zeitüberschreitung)" if "timed out" in r else "Name nicht auflösbar (DNS)" if "Name or service" in r or "getaddrinfo" in r else "keine Verbindung: " + r[:70]
        except Exception as e: last = type(e).__name__ + (": " + str(e)[:80] if str(e) else "")
        if attempt == 0: time.sleep(8)
    return (c["name"], False, html.escape(str(last), quote=False)) + meta

def load_state():
    try: return json.load(open(STATE_F))
    except Exception: return {"mode": "normal", "failing": {}}

def hrs(a, b): return (b - a).total_seconds() / 3600
def ts(): return NOW.strftime("%Y-%m-%dT%H:%MZ")
def berlin(t): return t.astimezone(ZoneInfo("Europe/Berlin")).strftime("%d.%m. %H:%M")
def dur(h): return f"{h*60:.0f} Min." if h < 1 else f"{h:.1f}".replace(".", ",") + " Std."
def since(old, n): return f" (seit {berlin(when(old[n]['since']))} Uhr)" if n in old else ""

def morning(res, fail, old, loc, limits):
    """Morgenreport: einmal täglich ab 7 Uhr, auch wenn alles ruhig ist."""
    main_f = {n: v for n, v in fail.items() if not v[2]}
    side_f = {n: v for n, v in fail.items() if v[2]}
    ok_n = len(res) - len(fail)
    head = "🟢 Alles Wichtige läuft" if not main_f else f"🔴 {len(main_f)} Problem(e)"
    t = f"🛡️ <b>Sentinel3: Morgenreport</b> · {loc.strftime('%d.%m.%Y')}\n\n{head}: {ok_n} von {len(res)} Prüfungen ok"
    if main_f:
        t += "\n\n" + "\n".join(f"🔴 <b>{n}</b>: {d}{since(old, n)}\n   betrifft: {', '.join(ub)}" for n, (d, ub, _) in main_f.items())
    if side_f:
        t += "\n\n<b>Nebenquellen gestört</b>\n" + "\n".join(f"🟡 {n}: {d}{since(old, n)} · {', '.join(ub)}" for n, (d, ub, _) in side_f.items())
    ages = [(n, int(d.split()[1])) for n, ok, d, _, _ in res if ok and d.startswith(("Daten", "Stand"))]
    aging = [f"• {n}: {h} h alt (Grenze {limits[n]} h)" for n, h in ages if n in limits and h > limits[n] / 2]  # nur was sich der Grenze nähert
    if ages:
        rest = len(ages) - len(aging)
        t += "\n\n<b>Frische der Daten</b>\n" + "\n".join(aging + ([f"• {'übrige' if aging else 'alle'} {rest} frisch"] if rest else []))
    doms = sorted((int(d.split()[2]), n) for n, ok, d, _, _ in res if ok and d.startswith("läuft in"))
    if doms: t += f"\n\n<b>Domains</b>\n• nächster Ablauf: {doms[0][1].replace('Domain ', '')} in {doms[0][0]} Tagen"
    skipped = [n for n, ok, d, _, _ in res if ok and d.startswith("übersprungen")]
    if skipped: t += "\n\n⚪ übersprungen: " + ", ".join(skipped)
    return t

def probe():
    """Adressen von GitHub aus abrufen und den Anfang der Antwort zeigen (zum Testen neuer Prüfungen)."""
    for u in os.environ.get("PROBE_URLS", "").split():
        try:
            status, body, ms, final = fetch(u, limit=3_000_000)
            txt = body.decode("utf-8", "replace")
            print(f"\n=== {status} · {ms} ms · {len(body)} Bytes · {u}\n" + txt[:700] + "\n   [...]\n" + txt[-300:])
            pat = os.environ.get("PROBE_FIND", "")
            for m in list(re.finditer(pat, txt))[:12] if pat else []:
                print("   >>>", " ".join(txt[max(0, m.start() - 260):m.end() + 60].split()))
        except urllib.error.HTTPError as e: print(f"\n=== HTTP {e.code} · {u}")
        except Exception as e: print(f"\n=== {type(e).__name__}: {str(e)[:100]} · {u}")

def main():
    if MODE == "setup":
        r = tg("getUpdates")
        ids = {f'{m["chat"]["id"]} ({m["chat"].get("type")}{": " + (m["chat"].get("title") or m["chat"].get("username") or "") })'
               for u in r.get("result", []) for m in [u.get("message") or u.get("channel_post") or {}] if m.get("chat")}
        print("Chat-IDs (vorher dem Bot privat eine Nachricht schreiben):"); [print(" ", i) for i in sorted(ids)] or print("  keine gefunden"); return
    if MODE == "test":
        send("🛡️ <b>Sentinel3</b>: Testnachricht. Wenn du das liest, kommen Meldungen an."); return
    if MODE == "probe":
        probe(); return

    st = load_state()
    lf = st.get("last_full")
    loc = NOW.astimezone(ZoneInfo("Europe/Berlin")); day = loc.strftime("%Y-%m-%d")
    report_due = loc.hour >= 7 and st.get("last_daily") != day and not MANUAL
    due = (not lf) or hrs(when(lf), NOW) >= 2.8 or report_due
    if st.get("mode") == "normal" and not MANUAL and not due:
        print("Normalbetrieb, letzte volle Prüfung", lf, "- nächste nach 3 Stunden."); return
    if not MANUAL and lf and hrs(when(lf), NOW) < 0.33:
        print("Letzte Prüfung vor weniger als 20 Minuten, übersprungen."); return

    cfg = json.load(open("checks.json"))
    kinds = [("site", "sites"), ("fresh", "fresh"), ("workflows", "workflows"), ("domain", "domains"), ("status", "status_pages"), ("dep", "deps")]
    items = [(kind, c) for kind, key in kinds for c in cfg.get(key, [])]
    with ThreadPoolExecutor(16) as ex:
        res = list(ex.map(check, items))
    for n, ok, det, _, low in res: print(("OK  " if ok else "FEHL"), n, "·", det, "(Nebenquelle)" if low and not ok else "")

    fail = {n: (det, ub, low) for n, ok, det, ub, low in res if not ok}
    names = {r[0] for r in res}
    old = {n: v for n, v in st.get("failing", {}).items() if n in names}  # entfernte Prüfungen vergessen
    msgs = []
    new = [n for n in fail if n not in old and not fail[n][2]]
    # Entwarnung erst nach zwei erfolgreichen Prüfungen in Folge (gegen Flackern)
    recovering = {n: old[n].get("ok_streak", 0) + 1 for n in old if n not in fail}
    gone = [n for n, k in recovering.items() if k >= 2 and not old[n].get("low")]
    hold = {n: k for n, k in recovering.items() if k < 2}
    if new:
        lines = [f"🔴 <b>{n}</b>: {fail[n][0]}\n   betrifft: {', '.join(fail[n][1])}" for n in new]
        msgs.append("🛡️ <b>Sentinel3: Problem</b>\n\n" + "\n".join(lines) + "\n\nIch prüfe ab jetzt stündlich.")
    nf = {}
    for n, (det, ub, low) in fail.items():
        o = old.get(n, {})
        nf[n] = {"since": o.get("since", ts()), "last_alert": o.get("last_alert", ts()), "detail": det, "ok_streak": 0, "low": low, "reminded": bool(o.get("reminded"))}
    for n, k in hold.items():
        nf[n] = dict(old[n], ok_streak=k)
    serious = any(not v.get("low") for v in nf.values())
    if gone:
        lines = [f"🟢 <b>{n}</b> wieder in Ordnung (Ausfall ca. {dur(hrs(when(old[n]['since']), NOW))})" for n in gone]
        msgs.append("🛡️ <b>Sentinel3: Entwarnung</b>\n\n" + "\n".join(lines) + ("" if serious else "\n\nAlles Wichtige läuft, zurück auf alle 3 Stunden."))
    quiet = not (7 <= loc.hour < 22)
    remind = [] if quiet else [n for n in fail if n in old and not fail[n][2] and not old[n].get("reminded") and hrs(when(old[n]["last_alert"]), NOW) >= REMIND_H]
    for n in remind: nf[n]["reminded"] = True
    if remind and not report_due:  # steht ohnehin im Morgenreport, wenn der im selben Lauf kommt
        lines = [f"🟠 <b>{n}</b> weiterhin gestört seit {berlin(when(old[n]['since']))} Uhr: {fail[n][0]}" for n in remind]
        msgs.append("🛡️ <b>Sentinel3: Erinnerung</b>\n\n" + "\n".join(lines) + "\n\nAb jetzt steht das nur noch im Morgenreport, bis es wieder läuft.")
    for n in new + remind: nf[n]["last_alert"] = ts()

    new_state = {"mode": "problem" if serious else "normal", "failing": nf, "last_full": ts(), "last_daily": st.get("last_daily")}
    if report_due:
        msgs.append(morning(res, fail, old, loc, {c["name"]: c["max_hours"] for c in cfg.get("fresh", [])})); new_state["last_daily"] = day
    if MANUAL and not msgs:
        txt = f"🛡️ <b>Sentinel3: Manuelle Prüfung</b>\n\n✅ {len(res) - len(fail)} von {len(res)} in Ordnung"
        if fail: txt += "\n" + "\n".join(("🟡" if low else "🔴") + f" <b>{n}</b>: {d}{since(old, n)}" for n, (d, _, low) in fail.items())
        msgs.append(txt)
    for m in msgs: send(m)
    if SEND_FAILED:
        # Zustand nicht speichern: der nächste Lauf versucht dieselbe Meldung erneut.
        # Der Lauf endet als Fehler, damit GitHub per E-Mail Bescheid gibt.
        sys.exit("Telegram nicht erreichbar, Meldung nicht zugestellt.")
    if new_state != st:
        json.dump(new_state, open(STATE_F, "w"), ensure_ascii=False, indent=1)

if __name__ == "__main__":
    main()
