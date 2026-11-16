#!/usr/bin/env python3
"""Nieuwsradar Helmond – De Peel

Haalt nieuws op uit RSS-feeds, nieuwspagina's en officiële bekendmakingen,
filtert op Helmond, Deurne, Asten en Someren en maakt één vaste pagina:
nieuwsradar.html (in dezelfde map als dit script).

Alleen standaard-Python nodig (3.9 of hoger), geen extra pakketten.
Bronnen, trefwoorden en instellingen staan in bronnen.json.

Gebruik:  python nieuwsradar.py          (één keer ophalen)
          python nieuwsradar.py --open   (ophalen en pagina openen)
"""
import html
import json
import re
import sys
import time
import urllib.parse
import urllib.request
import webbrowser
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path

MAP = Path(__file__).resolve().parent
CONFIG = MAP / "bronnen.json"
STATUS = MAP / "nieuwsradar_data.json"
PAGINA = MAP / "nieuwsradar.html"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")
NU = datetime.now(timezone.utc)


# ---------------------------------------------------------------- hulpjes
def haal(url, timeout=25):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/rss+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "nl-NL,nl;q=0.9,en;q=0.8",
        "Cache-Control": "no-cache"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read()
        charset = r.headers.get_content_charset() or "utf-8"
    return data, charset


def schoon(tekst, maxlen=None):
    if not tekst:
        return ""
    tekst = re.sub(r"<[^>]+>", " ", tekst)
    tekst = html.unescape(tekst)
    tekst = re.sub(r"\s+", " ", tekst).strip()
    if maxlen and len(tekst) > maxlen:
        tekst = tekst[:maxlen].rsplit(" ", 1)[0] + " …"
    return tekst


def datum(s):
    if not s:
        return None
    s = s.strip()
    try:
        d = parsedate_to_datetime(s)
    except Exception:
        d = None
    if d is None:
        try:
            d = datetime.fromisoformat(s.replace("Z", "+00:00")[:32])
        except Exception:
            try:
                d = datetime.strptime(s[:10], "%Y-%m-%d")
            except Exception:
                d = None
    if d is None:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(timezone.utc)


def lokaal(t):
    try:
        return t.astimezone().strftime("%d-%m %H:%M")
    except Exception:
        return ""


# ---------------------------------------------------------------- lezers
def lees_rss(bron):
    data, _ = haal(bron["url"])
    root = ET.fromstring(data)
    items = []
    # RSS 2.0
    for it in root.iter("item"):
        items.append({
            "titel": schoon(it.findtext("title")),
            "link": (it.findtext("link") or "").strip(),
            "tekst": schoon(it.findtext("description"), 280),
            "datum": datum(it.findtext("pubDate") or it.findtext("{http://purl.org/dc/elements/1.1/}date")),
            "betaald": (it.findtext("{https://www.dpgmedia.cloud/paidrss}paid") or "").strip() == "true",
        })
    # Atom
    ns = "{http://www.w3.org/2005/Atom}"
    for it in root.iter(ns + "entry"):
        link = ""
        for l in it.findall(ns + "link"):
            if l.get("rel", "alternate") == "alternate":
                link = l.get("href", "")
        items.append({
            "titel": schoon(it.findtext(ns + "title")),
            "link": link,
            "tekst": schoon(it.findtext(ns + "summary") or it.findtext(ns + "content"), 280),
            "datum": datum(it.findtext(ns + "updated") or it.findtext(ns + "published")),
            "betaald": False,
        })
    return items


class Links(HTMLParser):
    """Verzamelt links met hun tekst, een eventuele kop binnen de link en een datum (<time datetime>) erna."""
    def __init__(self):
        super().__init__()
        self.links, self._href, self._tekst, self._kop, self._inkop = [], None, [], [], False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "a":
            self._href, self._tekst, self._kop = a.get("href"), [], []
        elif tag in ("h1", "h2", "h3", "h4") and self._href is not None:
            self._inkop = True
        elif tag == "time" and a.get("datetime"):
            if self.links and self.links[-1][3] is None:
                self.links[-1][3] = a.get("datetime")

    def handle_data(self, data):
        if self._href is not None:
            self._tekst.append(data)
            if self._inkop:
                self._kop.append(data)

    def handle_endtag(self, tag):
        if tag in ("h1", "h2", "h3", "h4"):
            self._inkop = False
        if tag == "a" and self._href is not None:
            self.links.append([self._href, " ".join(self._tekst), " ".join(self._kop), None])
            self._href = None


def lees_html(bron):
    data, cs = haal(bron["url"])
    p = Links()
    p.feed(data.decode(cs, errors="replace"))
    patroon = re.compile(bron["link_patroon"])
    gezien, items = set(), []
    for href, tekst, kop, tijd in p.links:
        if not href or not patroon.search(href):
            continue
        url = urllib.parse.urljoin(bron["url"], href).split("#")[0]
        volledig = schoon(tekst)
        titel = schoon(kop) or volledig
        rest = ""
        if not schoon(kop) and len(volledig) > 110:
            # geen kop in de link: knip na de eerste zin-achtige eenheid
            m = re.match(r"(.{20,140}?[a-z0-9'’”)])\s+(?=[A-Z])", volledig)
            if m:
                titel, rest = m.group(1), volledig[len(m.group(1)):].strip()
        elif schoon(kop):
            rest = volledig.replace(titel, "", 1).strip()
        rest = re.sub(r"\b(\d+ (uur|minuten?|dagen?) geleden|gisteren|vandaag)\b.*$", "", rest, flags=re.I).strip()
        if url in gezien or len(titel) < 12:
            continue
        gezien.add(url)
        items.append({"titel": titel[:200], "link": url, "tekst": schoon(rest, 240), "datum": datum(tijd), "betaald": False})
    return items[:30]


def lees_p2000monitor(bron):
    """P2000-tabel van p2000monitor.eu (één regel per capcode; zelfde melding samenvoegen)."""
    data, cs = haal(bron["url"])
    tekst = data.decode("iso-8859-1" if cs.lower() in ("utf-8", "") and b"iso-8859-1" in data[:600].lower() else cs, errors="replace")
    meldingen = {}
    for rij in re.findall(r"<TR>(.*?)</TR>", tekst, re.S | re.I):
        cellen = dict(re.findall(r'<TD CLASS="(COL\d)">(.*?)</TD>', rij, re.S | re.I))
        if not {"COL2", "COL3", "COL7"} <= cellen.keys():
            continue
        delen = re.split(r"<BR>", cellen["COL7"], maxsplit=1, flags=re.I)
        bericht = schoon(delen[0])
        eenheid = schoon(delen[1]) if len(delen) > 1 else ""
        if not bericht:
            continue
        try:
            d = datetime.strptime(schoon(cellen["COL3"]) + " " + schoon(cellen["COL2"]), "%d-%m-%y %H:%M:%S").astimezone()
        except ValueError:
            d = None
        sleutel = (schoon(cellen["COL3"]), schoon(cellen["COL2"]), bericht)
        m = meldingen.setdefault(sleutel, {"titel": bericht, "eenheden": [], "datum": d})
        if eenheid and eenheid not in m["eenheden"]:
            m["eenheden"].append(eenheid)
    items = []
    for (dd, tt, bericht), m in meldingen.items():
        code = re.sub(r"\W+", "", f"{dd}{tt}{bericht}".lower())[:60]
        items.append({"titel": bericht, "link": bron["url"] + "#" + code,
                      "tekst": schoon(" · ".join(m["eenheden"]), 280), "datum": m["datum"],
                      "betaald": False, "alleen_titel": True})
    return items


def lees_bekendmakingen(bron, dagen):
    vanaf = (NU - timedelta(days=dagen)).strftime("%Y-%m-%d")
    q = (f'c.product-area==officielepublicaties and dt.creator=="{bron["gemeente"]}" '
         f'and dt.available>={vanaf}')
    url = ("https://repository.overheid.nl/sru?" + urllib.parse.urlencode(
        {"query": q, "maximumRecords": "100", "httpAccept": "application/xml"}))
    data, _ = haal(url, timeout=40)
    root = ET.fromstring(data)
    dc = "{http://purl.org/dc/terms/}"
    gzd = "{http://standaarden.overheid.nl/sru}"
    items = []
    for rec in root.iter("{http://docs.oasis-open.org/ns/search-ws/sruResponse}record"):
        titel = rec.findtext(f".//{dc}title") or ""
        items.append({
            "titel": schoon(titel)[:220],
            "link": rec.findtext(f".//{gzd}preferredUrl") or "",
            "tekst": schoon(rec.findtext(f".//{dc}abstract"), 220),
            "datum": datum(rec.findtext(f".//{dc}available")),
            "betaald": False,
        })
    return items


# ---------------------------------------------------------------- 112-meldingen splitsen
def dienst_112(titel, tekst):
    """Bepaal de hulpdienst van een P2000-melding: brandweer, ambulance of politie."""
    t = titel.strip()
    eenheden = tekst.lower()
    alles = (titel + " " + tekst).lower()
    if re.match(r"(A ?[0-2]|B ?[12])\b", t, re.I) or re.match(r"ambulance", t, re.I):
        return "ambulance"
    if re.match(r"(P ?[0-3]|PRIO ?[0-3])\b", t, re.I):
        return "brandweer"
    if re.search(r"politie|\bpol\b", eenheden):
        return "politie"
    if re.search(r"\bbrw\b|brandweer", eenheden):
        return "brandweer"
    if re.search(r"ambulance|\brav\b|\bmka\b|lifeliner|traumaheli|mmt", eenheden):
        return "ambulance"
    if re.search(r"politie|\bpol\b", alles):
        return "politie"
    if re.search(r"brand|bdh|brt|hulpverlening|\boms\b|nacontrole|wateroverlast|stormschade", alles):
        return "brandweer"
    if re.search(r"ambu|mmt|traumaheli|lifeliner", alles):
        return "ambulance"
    return "overig"


# ---------------------------------------------------------------- filter
def bepaal_gemeenten(tekst, cfg, standaard):
    gevonden = []
    for gem, w in cfg["gemeenten"].items():
        for woord in w["sterk"]:
            if re.search(r"(?<![\w-])" + re.escape(woord) + r"(?![\w])", tekst, re.I):
                gevonden.append(gem)
                break
        else:
            # zwakke trefwoorden (bijv. Heusden) tellen alleen bij een lokale bron van die gemeente
            if standaard == gem and any(re.search(r"\b" + re.escape(z) + r"\b", tekst, re.I) for z in w["zwak"]):
                gevonden.append(gem)
    if not gevonden and standaard:
        gevonden = [standaard]
    return gevonden


# ---------------------------------------------------------------- pagina
def maak_pagina(cfg, items, bronstatus, laatst):
    inst = cfg["instellingen"]
    gemeenten = list(cfg["gemeenten"].keys())
    soorten = sorted({i["soort"] for i in items})
    rijen = []
    for i in items:
        nieuw = ' nieuw' if i.get("nieuw") else ''
        chips = "".join(f'<span class="chip g-{g.lower()}">{g}</span>' for g in i["gemeenten"])
        betaald = '<span class="slot" title="Achter betaalmuur">€</span>' if i.get("betaald") else ""
        if i.get("prio"):
            betaald += '<span class="prio">PRIO 1</span>'
        rijen.append(
            f'<article class="item{nieuw}" data-gem="{" ".join(i["gemeenten"])}" data-soort="{html.escape(i["soort"])}">'
            f'<div class="meta"><span class="tijd">{html.escape(i["tijd"])}</span>{chips}'
            f'<span class="bron">{html.escape(i["bron"])}</span>{betaald}</div>'
            f'<a href="{html.escape(i["link"])}" target="_blank" rel="noopener">{html.escape(i["titel"])}</a>'
            + (f'<p>{html.escape(i["tekst"])}</p>' if i["tekst"] else "") + "</article>")
    status = "".join(
        f'<li class="{"ok" if s["ok"] else "fout"}">{html.escape(n)}: '
        f'{s["aantal"] if s["ok"] else html.escape(s["fout"])}</li>' for n, s in bronstatus.items())
    knoppen_g = "".join(f'<button data-f="gem" data-v="{g}">{g}</button>' for g in gemeenten)
    verb = cfg["instellingen"].get("verborgen_onder_alles", [])
    gewoon = [s for s in soorten if not any(s.startswith(x) for x in verb)]
    knoppen_s = "".join(f'<button data-f="soort" data-v="{html.escape(s)}">{html.escape(s)}</button>' for s in gewoon)
    volg = ["brandweer", "ambulance", "politie", "overig"]
    s112 = sorted([s for s in soorten if s not in gewoon],
                  key=lambda s: volg.index(s.split(" ", 1)[-1]) if s.split(" ", 1)[-1] in volg else 9)
    if s112:
        knoppen_s += ('<span class="sep">112:</span><button data-f="soort" data-v="112*">alle 112</button>'
                      + "".join(f'<button data-f="soort" data-v="{html.escape(s)}">{html.escape(s.split(" ", 1)[-1])}</button>' for s in s112))
    n_nieuw = sum(1 for i in items if i.get("nieuw"))
    pagina = f"""<!DOCTYPE html>
<html lang="nl"><head><meta charset="utf-8">
<meta http-equiv="refresh" content="{int(inst.get('pagina_verversen_minuten', 15)) * 60}">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex,nofollow">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="Nieuwsradar">
<meta name="theme-color" content="#14243B">
<title>{html.escape(inst['pagina_titel'])}</title>
<style>
:root{{--ink:#14243B;--body:#3B4859;--paper:#F6F3EC;--card:#FCFAF6;--blue:#1E5AA8;--orange:#D9731A;--line:#DDD6C8}}
*{{box-sizing:border-box}}body{{margin:0;font-family:Segoe UI,Arial,sans-serif;background:var(--paper);color:var(--body)}}
header{{background:var(--ink);color:#F6F3EC;padding:18px 28px;position:sticky;top:0;z-index:2}}
h1{{margin:0;font-size:22px}}.sub{{font-size:13px;opacity:.8;margin-top:4px}}
.balk{{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-top:12px}}
button{{border:1px solid #9FB0C6;background:transparent;color:#F6F3EC;border-radius:16px;padding:4px 12px;font-size:13px;cursor:pointer}}
button.aan{{background:#F6F3EC;color:var(--ink);border-color:#F6F3EC}}
input{{padding:5px 10px;border-radius:16px;border:0;font-size:13px;min-width:220px}}
main{{max-width:1000px;margin:20px auto;padding:0 20px}}
.item{{background:var(--card);border-left:5px solid var(--line);padding:12px 16px;margin-bottom:10px;border-radius:4px}}
.item.nieuw{{border-left-color:var(--orange)}}
.item a{{color:var(--ink);font-weight:600;font-size:16px;text-decoration:none}}.item a:hover{{text-decoration:underline}}
.item p{{margin:6px 0 0;font-size:14px}}
.meta{{font-size:12px;margin-bottom:4px;display:flex;gap:6px;flex-wrap:wrap;align-items:center}}
.tijd{{font-weight:600}}.bron{{color:#6B7585}}.slot{{color:var(--orange);font-weight:700}}
.chip{{padding:1px 8px;border-radius:10px;color:#fff;font-size:11px}}
.g-helmond{{background:#1E5AA8}}.g-deurne{{background:#2E7D5B}}.g-asten{{background:#8A4FA3}}.g-someren{{background:#B85E12}}
.prio{{background:#B3261E;color:#fff;padding:1px 6px;border-radius:8px;font-weight:700}}
.sep{{color:#F6F3EC;font-size:13px;margin-left:10px;opacity:.8}}
.nieuwlabel{{background:var(--orange);color:#fff;padding:1px 6px;border-radius:8px}}
footer{{max-width:1000px;margin:30px auto;padding:0 20px 40px;font-size:12px;color:#6B7585}}
footer li.fout{{color:#B3261E}}
</style></head><body>
<header><h1>{html.escape(inst['pagina_titel'])}</h1>
<div class="sub">Bijgewerkt {html.escape(laatst)} · {len(items)} berichten van de afgelopen {inst['dagen_tonen']} dagen · <span class="nieuwlabel">{n_nieuw} nieuw</span> sinds de vorige ronde</div>
<div class="balk"><button data-f="gem" data-v="" class="aan">Alle gemeenten</button>{knoppen_g}</div>
<div class="balk"><button data-f="soort" data-v="" class="aan">Alle bronnen</button>{knoppen_s}
<button id="alleennieuw">Alleen nieuw</button><input id="zoek" placeholder="Zoeken in koppen…"></div></header>
<main id="lijst">{''.join(rijen) or '<p>Nog geen berichten.</p>'}</main>
<footer><b>Status bronnen</b> (aantal berichten bij de laatste ronde)<ul>{status}</ul>
Koppen en korte samenvattingen komen uit de feeds van de bronnen; lees het volledige bericht bij de bron. Alleen voor intern gebruik.</footer>
<script>
const st={{gem:'',soort:'',nieuw:false,q:''}};
const verborgen={json.dumps(cfg["instellingen"].get("verborgen_onder_alles", []))};
function pas(){{document.querySelectorAll('.item').forEach(e=>{{
 const ok=(!st.gem||e.dataset.gem.split(' ').includes(st.gem))&&(st.soort?(st.soort.endsWith('*')?e.dataset.soort.startsWith(st.soort.slice(0,-1)):e.dataset.soort===st.soort):!verborgen.some(v=>e.dataset.soort.startsWith(v)))
 &&(!st.nieuw||e.classList.contains('nieuw'))&&(!st.q||e.textContent.toLowerCase().includes(st.q));
 e.style.display=ok?'':'none';}});}}
document.querySelectorAll('button[data-f]').forEach(b=>b.onclick=()=>{{st[b.dataset.f]=b.dataset.v;
 document.querySelectorAll('button[data-f="'+b.dataset.f+'"]').forEach(x=>x.classList.toggle('aan',x===b));pas();}});
document.getElementById('alleennieuw').onclick=e=>{{st.nieuw=!st.nieuw;e.target.classList.toggle('aan',st.nieuw);pas();}};
document.getElementById('zoek').oninput=e=>{{st.q=e.target.value.toLowerCase();pas();}};
</script></body></html>"""
    PAGINA.write_text(pagina, encoding="utf-8")
    extra = cfg["instellingen"].get("extra_kopie_naar")
    if extra:
        try:
            doel = Path(extra)
            if not doel.is_absolute():
                doel = MAP / doel
            doel.parent.mkdir(parents=True, exist_ok=True)
            doel.write_text(pagina, encoding="utf-8")
        except Exception as e:
            print(f"Extra kopie naar {extra} mislukt: {e}")


# ---------------------------------------------------------------- hoofd
def main():
    cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    inst = cfg["instellingen"]
    dagen = int(inst.get("dagen_tonen", 7))
    uitsluiten = [u.lower() for u in inst.get("uitsluiten_titels", [])]
    oud = json.loads(STATUS.read_text(encoding="utf-8")) if STATUS.exists() else {"items": {}}
    eerdere = oud.get("items", {})
    alle, bronstatus = {}, {}

    for bron in cfg["bronnen"]:
        naam = bron["naam"]
        try:
            if bron["type"] == "rss":
                ruw = lees_rss(bron)
            elif bron["type"] == "p2000monitor":
                ruw = lees_p2000monitor(bron)
            elif bron["type"] == "html":
                ruw = lees_html(bron)
            elif bron["type"] == "bekendmakingen":
                ruw = lees_bekendmakingen(bron, dagen)
            else:
                ruw = []
            n = 0
            bron_dagen = int(bron.get("dagen_tonen", dagen))
            niet_als = [re.compile(r, re.I) for r in bron.get("niet_als", [])]
            prio_als = [re.compile(r, re.I) for r in bron.get("prio_als", [])]
            for it in ruw:
                if not it["titel"] or not it["link"]:
                    continue
                if any(u in it["titel"].lower() for u in uitsluiten):
                    continue
                if any(r.search(it["titel"]) for r in niet_als):
                    continue
                zoektekst = it["titel"] if it.get("alleen_titel") else it["titel"] + " " + it["tekst"]
                gems = bepaal_gemeenten(zoektekst, cfg, bron.get("standaard_gemeente"))
                if not gems:
                    continue  # regionale bron zonder treffer
                sleutel = it["link"]
                eerst = eerdere.get(sleutel, {}).get("eerst_gezien") or NU.isoformat()
                d = it["datum"] or datum(eerst)
                if d and d < NU - timedelta(days=bron_dagen):
                    continue
                if sleutel in alle:  # zelfde link uit twee bronnen: gemeenten samenvoegen
                    alle[sleutel]["gemeenten"] = sorted(set(alle[sleutel]["gemeenten"]) | set(gems))
                    continue
                alle[sleutel] = {
                    "titel": it["titel"], "link": it["link"], "tekst": it["tekst"],
                    "datum": (d or NU).isoformat(), "eerst_gezien": eerst,
                    "bron": naam, "gemeenten": gems,
                    "soort": (f"112 {dienst_112(it['titel'], it['tekst'])}" if bron.get("splits_112")
                              else bron.get("soort", "")),
                    "betaald": it.get("betaald", False), "nieuw": sleutel not in eerdere,
                    "dagen": bron_dagen, "prio": any(r.search(it["titel"]) for r in prio_als),
                }
                n += 1
            bronstatus[naam] = {"ok": True, "aantal": n, "fout": ""}
        except Exception as e:  # één kapotte bron mag de rest niet stoppen
            bronstatus[naam] = {"ok": False, "aantal": 0, "fout": f"{type(e).__name__}: {str(e)[:120]}"}
        time.sleep(0.5)

    # berichten die eerder gezien zijn maar nu niet meer in de feed staan, blijven binnen de termijn zichtbaar
    for k, v in eerdere.items():
        if k not in alle and datum(v.get("datum")) and datum(v["datum"]) >= NU - timedelta(days=int(v.get("dagen", dagen))):
            v = dict(v); v["nieuw"] = False
            alle[k] = v

    # dubbele koppen (bijv. Drimble verwijst naar hetzelfde bericht) samenvoegen
    verborgen = set(inst.get("verborgen_onder_alles", []))
    per_kop = {}
    for k, v in sorted(alle.items(), key=lambda kv: kv[1]["soort"] == "verzamelsite"):
        kop = re.sub(r"\W+", "", v["titel"].lower())[:80]
        if any(v.get("soort", "").startswith(x) for x in verborgen):
            kop += v["datum"][:16]
        if kop in per_kop:
            per_kop[kop]["gemeenten"] = sorted(set(per_kop[kop]["gemeenten"]) | set(v["gemeenten"]))
            continue
        per_kop[kop] = v
    items = sorted(per_kop.values(), key=lambda v: v["datum"], reverse=True)
    for v in items:
        v["tijd"] = lokaal(datum(v["datum"]))

    laatst = datetime.now().strftime("%d-%m-%Y %H:%M")
    STATUS.write_text(json.dumps({"bijgewerkt": laatst, "items": alle, "bronnen": bronstatus},
                                 ensure_ascii=False, indent=1), encoding="utf-8")
    maak_pagina(cfg, items, bronstatus, laatst)
    fout = [n for n, s in bronstatus.items() if not s["ok"]]
    print(f"{laatst}: {len(items)} berichten, {sum(1 for i in items if i.get('nieuw'))} nieuw."
          + (f" Fout bij: {', '.join(fout)}" if fout else ""))
    if "--open" in sys.argv:
        webbrowser.open(PAGINA.as_uri())


if __name__ == "__main__":
    main()
