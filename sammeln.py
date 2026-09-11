#!/usr/bin/env python3
"""
Public Health Radar – Sammler
=============================
Dieses Skript läuft automatisch (GitHub Actions) mehrmals am Tag:
  1. Es liest alle Quellen aus quellen.txt
  2. Es ruft RSS-Feeds, Google News, Bing News und PubMed ab
  3. Es bündelt Meldungen zum gleichen Thema (aus verschiedenen Quellen)
  4. Es vergibt Wichtigkeits-Punkte und sortiert in Kategorien
  5. Es schreibt das Ergebnis nach app/data.json (das liest die Handy-App)

Du musst hier nichts ändern. Anpassen kannst du quellen.txt und einstellungen.json.
Benötigt nur Python 3.9+ (keine Zusatzpakete).
"""
import json
import math
import re
import html
import sys
import time
import random
import urllib.request
import urllib.parse
import xml.etree.ElementTree as ET
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path

HIER = Path(__file__).resolve().parent
APP = HIER.parent / "app"
JETZT = datetime.now(timezone.utc)
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0 Safari/537.36 PublicHealthRadar/1.0")

GOOGLE_SPERRE = threading.Semaphore(3)  # höchstens 3 gleichzeitige Anfragen an Google News
EINST = json.loads((HIER / "einstellungen.json").read_text(encoding="utf-8"))
MAX_ALTER = timedelta(days=EINST.get("max_alter_tage", 7))


# --------------------------------------------------------------------------- Quellen
def lade_quellen():
    quellen = []
    for nr, zeile in enumerate((HIER / "quellen.txt").read_text(encoding="utf-8").splitlines(), 1):
        z = zeile.strip()
        if not z or z.startswith("#"):
            continue
        teile = [t.strip() for t in z.split("|")]
        if len(teile) < 5:
            print(f"  ! Zeile {nr} in quellen.txt übersprungen (zu wenige | Trennzeichen)")
            continue
        typ, name, adresse, gewicht, sprache = teile[:5]
        try:
            gewicht = max(1, min(5, int(gewicht)))
        except ValueError:
            gewicht = 2
        quellen.append({"typ": typ.lower(), "name": name, "adresse": adresse,
                        "gewicht": gewicht, "sprache": sprache.lower() or "en"})
    return quellen


def such_urls(q):
    """Aus einer 'suche'-Zeile werden Anfragen an Google News und Bing News."""
    begriffe = q["adresse"]
    urls = []
    if q["sprache"] == "de":
        gn = "hl=de&gl=DE&ceid=DE:de"
        bing = "setlang=de&mkt=de-DE"
    else:
        gn = "hl=en-US&gl=US&ceid=US:en"
        bing = "setlang=en&mkt=en-US"
    urls.append(("Google News", "https://news.google.com/rss/search?q="
                 + urllib.parse.quote(begriffe + " when:7d") + "&" + gn))
    if "site:" not in begriffe:  # Bing kann site:-OR-Ketten schlecht
        urls.append(("Bing News", "https://www.bing.com/news/search?q="
                     + urllib.parse.quote(begriffe) + "&format=rss&" + bing))
    if q["sprache"] == "en" and "site:" not in begriffe:
        # zusätzlich die britisch/europäische Google-News-Ausgabe
        urls.append(("Google News UK", "https://news.google.com/rss/search?q="
                     + urllib.parse.quote(begriffe + " when:7d") + "&hl=en-GB&gl=GB&ceid=GB:en"))
    return urls


# --------------------------------------------------------------------------- Abrufen
def hole(url, versuche=2):
    letzter = None
    for i in range(versuche):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": UA,
                "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*;q=0.8",
                "Accept-Language": "de,en;q=0.8"})
            with urllib.request.urlopen(req, timeout=25) as r:
                return r.read()
        except Exception as e:  # noqa
            letzter = e
            if i < versuche - 1:
                time.sleep(2 + i * 4 + random.random() * 2)
    raise letzter


def domain(url):
    try:
        d = urllib.parse.urlparse(url).netloc.lower()
        return d[4:] if d.startswith("www.") else d
    except Exception:
        return ""


def seiten_gewicht(dom, standard=2):
    gew = EINST.get("seiten_gewichte", {})
    teile = dom.split(".")
    for i in range(len(teile) - 1):
        kandidat = ".".join(teile[i:])
        if kandidat in gew and isinstance(gew[kandidat], (int, float)):
            return gew[kandidat]
    return standard


# --------------------------------------------------------------------------- Parsen
_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


def text_sauber(s, laenge=None):
    if not s:
        return ""
    s = html.unescape(_TAG.sub(" ", html.unescape(s)))
    s = _WS.sub(" ", s).strip()
    if laenge and len(s) > laenge:
        s = s[:laenge].rsplit(" ", 1)[0] + " …"
    return s


def datum(s):
    if not s:
        return None
    s = s.strip()
    try:
        d = parsedate_to_datetime(s)
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d.astimezone(timezone.utc)
    except Exception:
        pass
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d.astimezone(timezone.utc)
    except Exception:
        pass
    for fmt in ("%Y-%m-%d", "%Y/%m/%d %H:%M", "%Y/%m/%d", "%d.%m.%Y", "%a, %d %b %Y"):
        try:
            return datetime.strptime(s[:len(fmt) + 8].strip(), fmt).replace(tzinfo=timezone.utc)
        except Exception:
            continue
    return None


def _lokal(tag):
    return tag.rsplit("}", 1)[-1].lower() if isinstance(tag, str) else ""


def _kind(el, *namen):
    for c in el:
        if _lokal(c.tag) in namen:
            return c
    return None


def _text(el, *namen):
    c = _kind(el, *namen)
    return (c.text or "").strip() if c is not None and c.text else ""


_BAD_AMP = re.compile(r"&(?!(?:amp|lt|gt|quot|apos|#\d+|#x[0-9a-fA-F]+);)")
_BAD_CHR = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def parse_feed(roh):
    """Liest RSS 2.0, RSS 1.0 (RDF) und Atom. Gibt Liste von dicts zurück."""
    if isinstance(roh, bytes):
        t = None
        m = re.search(rb'encoding=["\']([\w-]+)', roh[:200])
        for enc in ([m.group(1).decode()] if m else []) + ["utf-8", "latin-1"]:
            try:
                t = roh.decode(enc)
                break
            except Exception:
                continue
    else:
        t = roh
    t = _BAD_CHR.sub("", t)
    t = re.sub(r"^\s*<\?xml[^>]*\?>", "", t)
    try:
        wurzel = ET.fromstring(t)
    except ET.ParseError:
        try:
            wurzel = ET.fromstring(_BAD_AMP.sub("&amp;", t))
        except ET.ParseError:
            return parse_notfall(t)

    eintraege = [e for e in wurzel.iter() if _lokal(e.tag) in ("item", "entry")]
    out = []
    for e in eintraege:
        titel = _text(e, "title")
        link = ""
        for c in e:
            if _lokal(c.tag) == "link":
                if c.get("href") and c.get("rel", "alternate") in ("alternate", ""):
                    link = c.get("href")
                    break
                if c.text and c.text.strip():
                    link = c.text.strip()
                    break
        if not link:
            g = _text(e, "guid", "id")
            if g.startswith("http"):
                link = g
        d = _text(e, "pubdate", "published", "updated", "date", "issued", "created")
        besch = _text(e, "description", "summary", "content", "encoded", "abstract")
        quelle_el = _kind(e, "source")
        quelle = None
        if quelle_el is not None and (quelle_el.text or "").strip():
            quelle = {"name": quelle_el.text.strip(), "url": quelle_el.get("url", "")}
        if titel and link:
            out.append({"titel": titel, "link": link, "datum": datum(d),
                        "text": besch, "quelle": quelle})
    return out


def parse_notfall(t):
    """Für kaputtes XML: einfache Mustersuche."""
    out = []
    for block in re.findall(r"<(?:item|entry)\b.*?</(?:item|entry)>", t, flags=re.S | re.I):
        def feld(n):
            m = re.search(rf"<{n}\b[^>]*>(.*?)</{n}>", block, flags=re.S | re.I)
            if not m:
                return ""
            v = m.group(1)
            v = re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", v, flags=re.S)
            return v.strip()
        titel = text_sauber(feld("title"))
        link = feld("link")
        if not link:
            m = re.search(r'<link[^>]+href="([^"]+)"', block)
            link = m.group(1) if m else ""
        d = feld("pubDate") or feld("published") or feld("updated") or feld("dc:date")
        if titel and link:
            out.append({"titel": titel, "link": html.unescape(link), "datum": datum(d),
                        "text": feld("description") or feld("summary"), "quelle": None})
    return out


# --------------------------------------------------------------------------- PubMed
def hole_pubmed(q):
    basis = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/"
    such = basis + "esearch.fcgi?" + urllib.parse.urlencode({
        "db": "pubmed", "term": q["adresse"], "reldate": 7, "datetype": "edat",
        "retmax": 40, "retmode": "json", "sort": "relevance", "tool": "publichealthradar"})
    ids = json.loads(hole(such))["esearchresult"].get("idlist", [])
    if not ids:
        return []
    time.sleep(0.5)
    summ = basis + "esummary.fcgi?" + urllib.parse.urlencode({
        "db": "pubmed", "id": ",".join(ids), "retmode": "json", "tool": "publichealthradar"})
    res = json.loads(hole(summ)).get("result", {})
    out = []
    for i in ids:
        r = res.get(i)
        if not r:
            continue
        d = datum(r.get("sortpubdate", "")) or JETZT
        if d > JETZT or JETZT - d > MAX_ALTER:
            d = JETZT - timedelta(days=2)  # neu in PubMed aufgenommen
        out.append({"titel": r.get("title", ""), "link": f"https://pubmed.ncbi.nlm.nih.gov/{i}/",
                    "datum": d, "text": "", "quelle": {"name": r.get("fulljournalname") or r.get("source") or "PubMed", "url": ""}})
    return out


# --------------------------------------------------------------------------- Stichwörter
def kompiliere(woerter):
    muster = []
    for w in woerter:
        w = w.strip().lower()
        if not w:
            continue
        if len(w) <= 3:
            muster.append(r"(?<![\w-])" + re.escape(w) + r"(?![\w])")
        else:
            muster.append(r"(?<![\w])" + re.escape(w))
    return re.compile("|".join(muster), re.I) if muster else None


KATEGORIEN = [{**k, "re": kompiliere(k["woerter"])} for k in EINST["kategorien"]]
DRINGEND = kompiliere(EINST.get("dringend_woerter", []))
AUSSCHLUSS = kompiliere(EINST.get("ausschluss_woerter", []))

STOP = set("""der die das und oder mit von für fur auf aus bei nach über uber unter vor im in am an zu zum zur ein eine einer
eines einem den dem des ist sind war wird werden hat haben nicht auch noch wie was wer wo als bis durch gegen ohne um
sich sie er es wir ihr man mehr neue neuer neues jetzt schon sehr so nur alle viele immer heute
the a an and or of for on in at to by with from into over under as is are was were be been has have had not no
new more most this that these those it its their his her they we you our after before about than how what why when
who will would can could may might says said say amid
""".split())


def tokens(titel):
    t = titel.lower()
    t = re.sub(r"[^\w\s-]", " ", t)
    worte = [w.strip("-") for w in t.split()]
    return {w[:5] for w in worte if len(w) >= 3 and w not in STOP and not w.isdigit()}


_DE = set("der die das und mit für bei nicht ist ein eine auf zu von im den dem des sich wird werden nach über auch mehr".split())
_EN = set("the and of in to for with is on are from by at as be new after over says more".split())


def sprache_erkennen(text, standard):
    w = re.findall(r"[a-zäöüß]+", text.lower())
    de = sum(1 for x in w if x in _DE) + 1.5 * len(re.findall(r"[äöüß]", text.lower()))
    en = sum(1 for x in w if x in _EN)
    if de > en:
        return "de"
    if en > de:
        return "en"
    return standard


# --------------------------------------------------------------------------- Hauptablauf
def sammle():
    quellen = lade_quellen()
    auftraege = []  # (quelle, anbieter, url)
    for q in quellen:
        if q["typ"] == "feed":
            auftraege.append((q, q["name"], q["adresse"]))
        elif q["typ"] == "suche":
            for anbieter, url in such_urls(q):
                auftraege.append((q, anbieter, url))
        elif q["typ"] == "pubmed":
            auftraege.append((q, "PubMed", None))

    print(f"Rufe {len(auftraege)} Adressen aus {len(quellen)} Quellen ab …")
    status, roh_meldungen = [], []

    def arbeite(a):
        q, anbieter, url = a
        start = time.time()
        if q["typ"] == "pubmed":
            items = hole_pubmed(q)
        else:
            if "news.google.com" in url:
                with GOOGLE_SPERRE:  # Google nicht überlasten
                    time.sleep(0.5 + random.random())
                    roh = hole(url, versuche=3)
            else:
                roh = hole(url)
            items = parse_feed(roh)
        return a, items, time.time() - start

    pubmed = [a for a in auftraege if a[0]["typ"] == "pubmed"]
    rest = [a for a in auftraege if a[0]["typ"] != "pubmed"]
    ergebnisse = []
    with ThreadPoolExecutor(max_workers=12) as ex:
        futs = {ex.submit(arbeite, a): a for a in rest}
        for f in as_completed(futs):
            a = futs[f]
            try:
                ergebnisse.append(f.result())
            except Exception as e:
                status.append({"quelle": a[0]["name"], "anbieter": a[1], "ok": False,
                               "fehler": str(e)[:120], "anzahl": 0})
    for a in pubmed:  # PubMed nacheinander (max. 3 Anfragen/Sekunde erlaubt)
        try:
            ergebnisse.append(arbeite(a))
        except Exception as e:
            status.append({"quelle": a[0]["name"], "anbieter": a[1], "ok": False,
                           "fehler": str(e)[:120], "anzahl": 0})
        time.sleep(0.5)

    for (q, anbieter, url), items, dauer in ergebnisse:
        frisch = 0
        for it in items:
            d = it["datum"]
            if d is None:
                d = JETZT - timedelta(hours=12)
            if d > JETZT + timedelta(hours=2):
                d = JETZT
            if JETZT - d > MAX_ALTER:
                continue
            titel = text_sauber(it["titel"], 300)
            if not titel:
                continue
            link = it["link"]
            if "bing.com" in link:
                m = re.search(r"[?&]url=([^&]+)", link)  # Bing: echte Ziel-URL steckt in url=
                if m:
                    link = urllib.parse.unquote(m.group(1))
            quelle = it.get("quelle") or {}
            if q["typ"] == "feed":
                qname = quelle.get("name") or q["name"].split(" – ")[0]
                qdom = domain(link) or domain(url)
                gewicht = q["gewicht"]
            elif q["typ"] == "pubmed":
                qname = quelle.get("name") or "PubMed"
                qdom = "pubmed/" + qname.lower()
                gewicht = q["gewicht"]
            else:
                qname = quelle.get("name") or domain(link)
                qdom = domain(quelle.get("url", "")) or (domain(link) if "news.google." not in link else qname.lower())
                gewicht = seiten_gewicht(qdom, 2)
                if "site:" in q["adresse"]:
                    gewicht = max(gewicht, q["gewicht"])
            for trenner in (" - ", " | ", " – "):
                if qname and titel.endswith(trenner + qname):
                    titel = titel[: -len(qname) - len(trenner)].strip()
            it["link"] = link
            text = "" if q["typ"] == "suche" else text_sauber(it["text"], 260)
            roh_meldungen.append({
                "titel": titel, "link": it["link"], "datum": d, "text": text,
                "quelle": qname, "dom": qdom, "gewicht": gewicht,
                "sprache": sprache_erkennen(titel + " " + text, q["sprache"]),
                "quellenname": q["name"]})
            frisch += 1
        status.append({"quelle": q["name"], "anbieter": anbieter, "ok": True,
                       "anzahl": frisch, "gesamt": len(items), "sek": round(dauer, 1)})

    return quellen, auftraege, status, roh_meldungen


def buendeln(meldungen):
    """Gleiche Nachricht aus verschiedenen Quellen -> ein Eintrag (Cluster)."""
    meldungen.sort(key=lambda m: (-m["gewicht"], -m["datum"].timestamp()))
    cluster, index = [], {}
    gesehen_links = set()
    for m in meldungen:
        if m["link"] in gesehen_links:
            continue
        gesehen_links.add(m["link"])
        tok = tokens(m["titel"])
        if not tok:
            continue
        kandidaten = {}
        for t in tok:
            for cid in index.get(t, ()):
                kandidaten[cid] = kandidaten.get(cid, 0) + 1
        bester, best_j = None, 0.0
        for cid, gemeinsam in kandidaten.items():
            if gemeinsam < 3 and not (gemeinsam >= 2 and len(tok) <= 4):
                continue
            ct = cluster[cid]["tok"]
            j = gemeinsam / len(tok | ct)
            if j > best_j:
                bester, best_j = cid, j
        if bester is not None and best_j >= 0.42:
            c = cluster[bester]
            c["mitglieder"].append(m)
        else:
            cid = len(cluster)
            cluster.append({"tok": tok, "mitglieder": [m]})
            for t in tok:
                index.setdefault(t, []).append(cid)
    return cluster


def bewerte(cluster):
    ergebnis = []
    for c in cluster:
        ms = c["mitglieder"]
        haupt = max(ms, key=lambda m: (m["gewicht"], m["datum"]))
        quellen = {}
        for m in ms:
            key = (m["dom"] or m["quelle"]).lower()
            if key not in quellen or m["gewicht"] > quellen[key]["gewicht"]:
                quellen[key] = m
        n = len(quellen)
        gesamt_text = " ".join({m["titel"] for m in ms}) + " " + haupt["text"]
        if AUSSCHLUSS and AUSSCHLUSS.search(haupt["titel"]):
            continue

        juengste = max(m["datum"] for m in ms)
        erste = min(m["datum"] for m in ms)
        alter_h = max(0.0, (JETZT - juengste).total_seconds() / 3600)

        punkte = max(m["gewicht"] for m in ms) * 1.2
        punkte += 3.2 * math.log2(n) if n > 1 else 0
        punkte += min(2, sum(1 for m in quellen.values() if m["gewicht"] >= 4) - 1) * 0.8 if n > 1 else 0
        dring = len(set(x.group(0).lower() for x in DRINGEND.finditer(gesamt_text))) if DRINGEND else 0
        punkte += min(dring, 3) * 1.6
        # Kategorien
        treffer = []
        for k in KATEGORIEN:
            if not k["re"]:
                continue
            anz = len(set(x.group(0).lower() for x in k["re"].finditer(gesamt_text)))
            if anz:
                treffer.append((anz, -KATEGORIEN.index(k), k["id"]))
        treffer.sort(reverse=True)
        kats = [t[2] for t in treffer[:3]]
        if not kats:
            kats = ["forschung"] if any("pubmed" in m["link"] or "rxiv" in m["link"] for m in ms) else ["sonstiges"]
        if "praevention" in kats and "public health" in gesamt_text.lower():
            punkte += 0.8
        # Aktualität: nach 2 Tagen halbe Punkte
        punkte = punkte / (1 + alter_h / 48)

        andere = sorted((m for k, m in quellen.items() if m is not haupt),
                        key=lambda m: -m["gewicht"])[:12]
        ergebnis.append({
            "t": haupt["titel"], "u": haupt["link"], "q": haupt["quelle"],
            "d": juengste.strftime("%Y-%m-%dT%H:%MZ"), "e": erste.strftime("%Y-%m-%dT%H:%MZ"),
            "l": haupt["sprache"], "k": kats, "p": round(punkte, 2), "n": n,
            "x": haupt["text"] if haupt["text"] and haupt["text"][:40] not in haupt["titel"] else "",
            "o": [[m["quelle"], m["link"], m["titel"] if m["titel"] != haupt["titel"] else ""] for m in andere],
        })
    ergebnis.sort(key=lambda e: -e["p"])
    # Stufen 1–5 (Sterne) nach Rang
    total = len(ergebnis)
    for i, e in enumerate(ergebnis):
        r = i / max(1, total)
        e["s"] = 5 if r < 0.03 else 4 if r < 0.10 else 3 if r < 0.25 else 2 if r < 0.55 else 1
    return ergebnis


def main():
    t0 = time.time()
    quellen, auftraege, status, meldungen = sammle()
    print(f"{len(meldungen)} Meldungen aus den letzten {MAX_ALTER.days} Tagen gefunden.")
    cluster = buendeln(meldungen)
    eintraege = bewerte(cluster)[: EINST.get("max_meldungen_in_app", 1500)]
    seiten = {m["dom"] or m["quelle"].lower() for m in meldungen}
    ok = sum(1 for s in status if s["ok"])
    kats = [{"id": k["id"], "name": k["name"], "icon": k.get("icon", "")} for k in EINST["kategorien"]]
    kats.append({"id": "sonstiges", "name": "Sonstiges", "icon": "📰"})
    daten = {
        "stand": JETZT.strftime("%Y-%m-%dT%H:%MZ"),
        "statistik": {
            "quellen_liste": len(quellen), "abrufe": len(auftraege), "abrufe_ok": ok,
            "webseiten": len(seiten), "meldungen": len(meldungen), "themen": len(eintraege),
            "dauer_sek": round(time.time() - t0)},
        "kategorien": kats,
        "eintraege": eintraege,
    }
    APP.mkdir(exist_ok=True)
    (APP / "data.json").write_text(json.dumps(daten, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    status.sort(key=lambda s: (s["ok"], s["quelle"]))
    (APP / "status.json").write_text(json.dumps({"stand": daten["stand"], "abrufe": status},
                                                ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"Fertig: {len(eintraege)} Themen aus {len(seiten)} verschiedenen Webseiten "
          f"({ok}/{len(auftraege)} Abrufe erfolgreich) in {daten['statistik']['dauer_sek']} s.")
    if ok == 0:
        print("FEHLER: Keine einzige Quelle erreichbar.")
        sys.exit(1)


if __name__ == "__main__":
    main()
