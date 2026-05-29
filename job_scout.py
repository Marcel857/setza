#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Setza – KI-Job-Scout – 24/7-Version (server / GitHub Actions).

Sucht über mehrere freie Job-APIs nach passenden Remote-/Junior-Designstellen,
bewertet sie (0-100), entfernt Duplikate und versendet Treffer >= MATCH_THRESHOLD
per E-Mail (Gmail SMTP).

Konfiguration über Umgebungsvariablen (Secrets):
  SMTP_USER   -> Absender-Gmail-Adresse (z. B. marcel.brack8@gmail.com)
  SMTP_PASS   -> Gmail-App-Passwort (16 Zeichen, KEIN normales Passwort)
  ALERT_TO    -> Empfänger (Standard: adamikisabel@gmail.com)
Optional:
  MATCH_THRESHOLD     -> Mindest-Score für Alarm (Standard 60)
  MAX_ALERTS_PER_RUN  -> Obergrenze Mails pro Lauf (Standard 25)
  POSTED_WITHIN_DAYS  -> nur Stellen der letzten N Tage (Standard 30)
  DRY_RUN             -> "1" = nicht senden, nur ausgeben (zum Testen)
"""

import os
import re
import json
import smtplib
import datetime as dt
import xml.etree.ElementTree as ET
from email.mime.text import MIMEText
from email.utils import formataddr, parsedate_to_datetime

import requests

# --------------------------------------------------------------------------- #
# Konfiguration
# --------------------------------------------------------------------------- #
HERE = os.path.dirname(os.path.abspath(__file__))
SEEN_FILE = os.path.join(HERE, "seen_jobs.json")
LOG_FILE = os.path.join(HERE, "scout_log.md")
JOBS_FILE = os.path.join(HERE, "jobs.json")  # vollständige Liste für die Setza-App

SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")
ALERT_TO = os.environ.get("ALERT_TO", "adamikisabel@gmail.com")
MATCH_THRESHOLD = int(os.environ.get("MATCH_THRESHOLD", "60"))
MAX_ALERTS_PER_RUN = int(os.environ.get("MAX_ALERTS_PER_RUN", "25"))
POSTED_WITHIN_DAYS = int(os.environ.get("POSTED_WITHIN_DAYS", "30"))
DRY_RUN = os.environ.get("DRY_RUN", "0") == "1"

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; JobScout/1.0; +https://example.com)"}
TIMEOUT = 25

# --------------------------------------------------------------------------- #
# Such- und Filterlogik
# --------------------------------------------------------------------------- #

# Passende Jobtitel / Schlüsselwörter (Titel muss mindestens eines enthalten)
INCLUDE_KEYWORDS = [
    "polygraf", "polygrafin",
    "grafiker", "grafikerin", "grafik", "graphic", "graphik",
    "grafikdesign", "graphic design",
    "mediengestalter", "mediengestalterin", "media designer",
    "packaging", "verpackung",
    "brand design", "brand designer",
    "visual design", "visual designer",
    "creative design", "creative designer",
    "marketing design", "marketing designer",
    "social media design", "social media designer",
    "print design", "printdesign", "print designer",
    "layout", "layouter",
    "dtp", "reinzeichn", "reinzeichner",
    "kommunikationsdesign", "communication design",
    "werbegestalt", "werbegrafik",
    "editorial design",
    "screen design", "screendesign",
    "digital design", "digital designer",
    "produktdesign", "product design",
    "junior designer", "junior design",
    "designer",  # breit; wird durch Exclude/Score gefiltert
    "gestalter",
]

# Sofort ausschließen, wenn der Titel eines dieser Wörter enthält
EXCLUDE_KEYWORDS = [
    "senior", "lead", "head of", "head ", "principal", "staff",
    "art director", "director", "manager", "chief", "vp ", "vp,",
    "praktik", "intern", "internship", "trainee", "apprentice", "apprenticeship",
    "founding", "founder", "working student",
    "werkstudent", "freelanc", "selbständ", "selbstständ", "self-employed",
    "ausbildung", "auszubild", "studium", "duales", "dual ",
    "developer", "entwickler", "engineer", "architekt",
]

# Begriffe, die auf 100% Remote / ortsunabhängig hindeuten
REMOTE_HINTS = [
    "remote", "ortsunabhängig", "anywhere", "worldwide", "home office",
    "homeoffice", "100% remote", "fully remote", "work from home",
    "telearbeit", "europe", "emea", "germany", "deutschland", "dach",
]

# Harte Englisch-Pflicht -> Stelle komplett ausschließen (laut Profil)
ENGLISH_HARD_EXCLUDE = [
    "english c1", "english c2", "englisch c1", "englisch c2",
    "verhandlungssicheres englisch", "business fluent english", "c1 english",
]
# Weiche Hinweise -> nur Score "Deutsch ausreichend" entfällt
ENGLISH_REQUIRED_HINTS = ENGLISH_HARD_EXCLUDE + ["fluent english", "native english"]

DESIGN_HINTS = ["design", "grafik", "graphic", "gestalt", "creative", "kreativ", "layout"]
JUNIOR_HINTS = ["junior", " jr", "jr.", "entry", "einsteiger", "berufseinsteiger",
                "graduate", "absolvent", "0-2", "0–2", "0 - 2"]
PACK_PRINT_HINTS = ["packaging", "verpackung", "print", "druck", "polygraf"]
SALARY_HINTS = ["€", "eur", "gehalt", "salary", "chf", "lohn", "k/jahr", "p.a.", "brutto"]


def now_iso():
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M")


def days_since(epoch_or_iso):
    """Versucht, das Alter einer Stelle in Tagen zu bestimmen. None = unbekannt."""
    if epoch_or_iso is None:
        return None
    try:
        if isinstance(epoch_or_iso, (int, float)):
            d = dt.datetime.utcfromtimestamp(float(epoch_or_iso))
        else:
            s = str(epoch_or_iso)
            # epoch in ms?
            if s.isdigit() and len(s) >= 12:
                d = dt.datetime.utcfromtimestamp(int(s) / 1000.0)
            elif s.isdigit():
                d = dt.datetime.utcfromtimestamp(int(s))
            elif "," in s and any(m in s for m in ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
                                                    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")):
                d = parsedate_to_datetime(s).replace(tzinfo=None)  # RFC822 (RSS)
            else:
                s = s.replace("Z", "").split(".")[0]
                d = dt.datetime.fromisoformat(s[:19])
        return (dt.datetime.utcnow() - d).days
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# Quellen (freie APIs, kein Login nötig)
# --------------------------------------------------------------------------- #

def fetch_arbeitnow():
    out = []
    try:
        r = requests.get("https://www.arbeitnow.com/api/job-board-api",
                         headers=HEADERS, timeout=TIMEOUT)
        for j in r.json().get("data", []):
            out.append({
                "source": "Arbeitnow",
                "title": j.get("title", ""),
                "company": j.get("company_name", ""),
                "url": j.get("url", ""),
                "location": j.get("location", ""),
                "remote": bool(j.get("remote")),
                "description": j.get("description", "") or "",
                "age": days_since(j.get("created_at")),
            })
    except Exception as e:
        print("  ! Arbeitnow Fehler:", e)
    return out


def fetch_remotive():
    out = []
    try:
        r = requests.get("https://remotive.com/api/remote-jobs?category=design&limit=80",
                         headers=HEADERS, timeout=TIMEOUT)
        for j in r.json().get("jobs", []):
            out.append({
                "source": "Remotive",
                "title": j.get("title", ""),
                "company": j.get("company_name", ""),
                "url": j.get("url", ""),
                "location": j.get("candidate_required_location", ""),
                "remote": True,
                "description": (j.get("description", "") or "") + " " + (j.get("salary", "") or ""),
                "age": days_since(j.get("publication_date")),
            })
    except Exception as e:
        print("  ! Remotive Fehler:", e)
    return out


def fetch_remoteok():
    out = []
    try:
        r = requests.get("https://remoteok.com/api", headers=HEADERS, timeout=TIMEOUT)
        data = r.json()
        for j in data:
            if not isinstance(j, dict) or "position" not in j:
                continue
            out.append({
                "source": "RemoteOK",
                "title": j.get("position", "") or j.get("title", ""),
                "company": j.get("company", ""),
                "url": j.get("url", ""),
                "location": j.get("location", "") or "Remote",
                "remote": True,
                "description": j.get("description", "") or "",
                "age": days_since(j.get("epoch") or j.get("date")),
            })
    except Exception as e:
        print("  ! RemoteOK Fehler:", e)
    return out


def fetch_himalayas():
    out = []
    try:
        r = requests.get("https://himalayas.app/jobs/api?limit=80",
                         headers=HEADERS, timeout=TIMEOUT)
        for j in r.json().get("jobs", []):
            locs = j.get("locationRestrictions") or []
            out.append({
                "source": "Himalayas",
                "title": j.get("title", ""),
                "company": j.get("companyName", ""),
                "url": j.get("applicationLink") or j.get("guid", ""),
                "location": ", ".join(locs) if isinstance(locs, list) else str(locs),
                "remote": True,
                "description": j.get("description", "") or j.get("excerpt", "") or "",
                "age": days_since(j.get("pubDate")),
            })
    except Exception as e:
        print("  ! Himalayas Fehler:", e)
    return out


def fetch_jobicy():
    out = []
    try:
        r = requests.get("https://jobicy.com/api/v2/remote-jobs?count=50&tag=design",
                         headers=HEADERS, timeout=TIMEOUT)
        for j in r.json().get("jobs", []):
            lvl = j.get("jobLevel", "") or ""
            out.append({
                "source": "Jobicy",
                "title": j.get("jobTitle", ""),
                "company": j.get("companyName", ""),
                "url": j.get("url", ""),
                "location": j.get("jobGeo", ""),
                "remote": True,
                "description": (j.get("jobExcerpt", "") or "") + " level:" + str(lvl),
                "age": days_since(j.get("pubDate")),
            })
    except Exception as e:
        print("  ! Jobicy Fehler:", e)
    return out


def _strip_html(text):
    return re.sub(r"<[^>]+>", " ", text or "").replace("&nbsp;", " ").strip()


def _rss_items_regex(xml_text):
    """Toleranter Fallback für nicht wohlgeformtes XML."""
    items = re.findall(r"<item[ >].*?</item>", xml_text, re.S | re.I)

    def field(block, tag):
        m = re.search(r"<%s[^>]*>(.*?)</%s>" % (tag, tag), block, re.S | re.I)
        if not m:
            return ""
        val = m.group(1)
        cd = re.search(r"<!\[CDATA\[(.*?)\]\]>", val, re.S)
        return (cd.group(1) if cd else val).strip()

    return items, field


def fetch_rss(url, source, split_company=False):
    """Generischer RSS-Leser (stdlib). Fällt bei kaputtem XML auf Regex zurück."""
    out = []
    try:
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    except Exception as e:
        print(f"  ! {source} Fehler:", e)
        return out

    parsed = []
    try:
        root = ET.fromstring(r.content)
        for item in root.iter("item"):
            def t(tag, _i=item):
                el = _i.find(tag)
                return el.text if el is not None and el.text else ""
            parsed.append({
                "title": (t("title") or "").strip(),
                "link": (t("link") or "").strip(),
                "region": (t("region") or "").strip(),
                "creator": (t("{http://purl.org/dc/elements/1.1/}creator") or "").strip(),
                "description": t("description"),
                "pubDate": t("pubDate"),
            })
    except Exception:
        try:  # Fallback für fehlerhafte Feeds (z. B. SkipTheDrive)
            xml_text = r.content.decode("utf-8", "ignore")
            items, field = _rss_items_regex(xml_text)
            for block in items:
                parsed.append({
                    "title": field(block, "title"),
                    "link": field(block, "link"),
                    "region": field(block, "region"),
                    "creator": field(block, "dc:creator"),
                    "description": field(block, "description"),
                    "pubDate": field(block, "pubDate"),
                })
        except Exception as e:
            print(f"  ! {source} Fehler:", e)
            return out

    for p in parsed:
        title = p["title"]
        company = ""
        if split_company and ":" in title:
            company, title = [x.strip() for x in title.split(":", 1)]
        if not company:
            company = p["creator"]
        out.append({
            "source": source,
            "title": title,
            "company": company,
            "url": p["link"],
            "location": p["region"] or "Remote",
            "remote": True,
            "description": _strip_html(p["description"]),
            "age": days_since(p["pubDate"]),
        })
    return out


def fetch_weworkremotely():
    return fetch_rss("https://weworkremotely.com/categories/remote-design-jobs.rss",
                     "WeWorkRemotely", split_company=True)


def fetch_jobspresso():
    return fetch_rss("https://jobspresso.co/remote-work/feed/", "Jobspresso")


def fetch_skipthedrive():
    return fetch_rss("https://www.skipthedrive.com/job-category/design/feed/", "SkipTheDrive")


def fetch_workingnomads():
    out = []
    for endpoint in ("https://www.workingnomads.com/api/exposed_jobs/",
                     "https://www.workingnomads.com/jobsapi/"):
        try:
            r = requests.get(endpoint, headers=HEADERS, timeout=TIMEOUT)
            data = r.json()
            rows = data if isinstance(data, list) else data.get("jobs", data.get("data", []))
            for j in rows:
                if not isinstance(j, dict):
                    continue
                out.append({
                    "source": "WorkingNomads",
                    "title": j.get("title", "") or j.get("position", ""),
                    "company": j.get("company_name", "") or j.get("company", ""),
                    "url": j.get("url", "") or j.get("link", ""),
                    "location": j.get("location", "") or "Remote",
                    "remote": True,
                    "description": _strip_html(j.get("description", "")
                                              + " " + str(j.get("tags", ""))),
                    "age": days_since(j.get("pub_date") or j.get("created_at")),
                })
            if out:
                break  # erster funktionierender Endpunkt reicht
        except Exception as e:
            print("  ! WorkingNomads Fehler:", e)
    return out


# --------------------------------------------------------------------------- #
# Daten-Burggraben: ATS-Quellen (Firmen-Karriereseiten, legal & strukturiert)
# Kuratierte Firmen-Tokens hier pflegen — das ist der Wettbewerbsvorteil.
# Personio = DACH-stark; Greenhouse/Lever = Startups/Agenturen.
# --------------------------------------------------------------------------- #
ATS_PERSONIO = [
    # "firmatoken",   # -> https://firmatoken.jobs.personio.de
]
ATS_GREENHOUSE = [
    # "firmatoken",   # -> https://boards.greenhouse.io/firmatoken
]
ATS_LEVER = [
    # "firmatoken",   # -> https://jobs.lever.co/firmatoken
]


def fetch_personio():
    out = []
    for cmp in ATS_PERSONIO:
        try:
            r = requests.get(f"https://{cmp}.jobs.personio.de/xml?language=de",
                             headers=HEADERS, timeout=TIMEOUT)
            root = ET.fromstring(r.content)
            for pos in root.iter("position"):
                def t(tag, _p=pos):
                    el = _p.find(tag)
                    return el.text if el is not None and el.text else ""
                jid = t("id")
                office = t("office")
                desc = _strip_html(" ".join((e.text or "") for e in pos.iter("value")))
                desc += " " + t("seniority") + " " + t("employmentType") + " " + t("schedule")
                out.append({
                    "source": f"Personio/{cmp}", "title": t("name"), "company": cmp,
                    "url": f"https://{cmp}.jobs.personio.de/job/{jid}",
                    "location": office or "DACH",
                    "remote": "remote" in (office + " " + desc).lower(),
                    "description": desc, "age": days_since(t("createdAt")),
                })
        except Exception as e:
            print(f"  ! Personio/{cmp} Fehler:", e)
    return out


def fetch_greenhouse():
    out = []
    for tok in ATS_GREENHOUSE:
        try:
            r = requests.get(f"https://boards-api.greenhouse.io/v1/boards/{tok}/jobs?content=true",
                             headers=HEADERS, timeout=TIMEOUT)
            for j in r.json().get("jobs", []):
                loc = (j.get("location") or {}).get("name", "") or ""
                out.append({
                    "source": f"Greenhouse/{tok}", "title": j.get("title", ""), "company": tok,
                    "url": j.get("absolute_url", ""), "location": loc,
                    "remote": "remote" in loc.lower(),
                    "description": _strip_html(j.get("content", "")),
                    "age": days_since(j.get("updated_at")),
                })
        except Exception as e:
            print(f"  ! Greenhouse/{tok} Fehler:", e)
    return out


def fetch_lever():
    out = []
    for tok in ATS_LEVER:
        try:
            r = requests.get(f"https://api.lever.co/v0/postings/{tok}?mode=json",
                             headers=HEADERS, timeout=TIMEOUT)
            for j in r.json():
                cat = j.get("categories", {}) or {}
                loc = cat.get("location", "") or ""
                out.append({
                    "source": f"Lever/{tok}", "title": j.get("text", ""), "company": tok,
                    "url": j.get("hostedUrl", ""), "location": loc,
                    "remote": "remote" in loc.lower(),
                    "description": (j.get("descriptionPlain", "") or "") + " " + (cat.get("commitment", "") or ""),
                    "age": days_since(j.get("createdAt")),
                })
        except Exception as e:
            print(f"  ! Lever/{tok} Fehler:", e)
    return out


SOURCES = [
    fetch_arbeitnow, fetch_remotive, fetch_remoteok, fetch_himalayas, fetch_jobicy,
    fetch_weworkremotely, fetch_jobspresso, fetch_skipthedrive, fetch_workingnomads,
    fetch_personio, fetch_greenhouse, fetch_lever,
]


# --------------------------------------------------------------------------- #
# Filter + Scoring
# --------------------------------------------------------------------------- #

def passes_filter(job):
    title = job["title"].lower()
    if not title:
        return False
    if not any(k in title for k in INCLUDE_KEYWORDS):
        return False
    if any(k in title for k in EXCLUDE_KEYWORDS):
        return False
    text = (title + " " + job["description"].lower())
    if any(k in text for k in ENGLISH_HARD_EXCLUDE):
        return False
    if job["age"] is not None and job["age"] > POSTED_WITHIN_DAYS:
        return False
    return True


def score(job):
    title = job["title"].lower()
    desc = job["description"].lower()
    text = title + " " + desc
    pts = 0
    reasons = []

    # Remote (30)
    if job["remote"] or any(h in text for h in REMOTE_HINTS):
        pts += 30
        reasons.append("100% Remote")

    # Junior (25 / 12)
    if any(h in text for h in JUNIOR_HINTS):
        pts += 25
        reasons.append("Junior/Einsteiger")
    else:
        pts += 12  # kein Senior (sonst ausgefiltert) -> Einstieg plausibel

    # Design-Fokus (20 / 12)
    if any(h in title for h in DESIGN_HINTS):
        pts += 20
        reasons.append("Design-Fokus")
    elif any(h in desc for h in DESIGN_HINTS):
        pts += 12

    # Packaging / Print (10)
    if any(h in text for h in PACK_PRINT_HINTS):
        pts += 10
        reasons.append("Packaging/Print")

    # Deutsch ausreichend (10) – minus bei hoher Englisch-Pflicht
    if not any(h in text for h in ENGLISH_REQUIRED_HINTS):
        pts += 10
        reasons.append("Deutsch ausreichend")

    # Gehalt angegeben (5)
    if any(h in desc for h in SALARY_HINTS):
        pts += 5
        reasons.append("Gehalt angegeben")

    return min(pts, 100), ", ".join(reasons)


# --------------------------------------------------------------------------- #
# E-Mail
# --------------------------------------------------------------------------- #

def send_mail(job, sc, reasons):
    subject = f"🚨 Neuer Remote-Job gefunden (Match: {sc}%)"
    body = (
        f"Firma: {job['company'] or 'k.A.'}\n"
        f"Position: {job['title']}\n"
        f"Match: {sc}%\n"
        f"Remote: {'Ja' if job['remote'] else 'Unklar'}\n"
        f"Gehalt: k.A.\n"
        f"Warum passend: {reasons}\n"
        f"Quelle: {job['source']}\n"
        f"Bewerbungslink: {job['url']}\n"
        f"Empfehlung: Sofort bewerben."
    )
    if DRY_RUN:
        print(f"   [DRY_RUN] würde senden ({sc}%): {job['title']} – {job['company']}")
        return True
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = formataddr(("Setza", SMTP_USER))
    msg["To"] = ALERT_TO
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(SMTP_USER, SMTP_PASS)
        s.sendmail(SMTP_USER, [ALERT_TO], msg.as_string())
    print(f"   ✓ gesendet ({sc}%): {job['title']} – {job['company']}")
    return True


# --------------------------------------------------------------------------- #
# Hauptlauf
# --------------------------------------------------------------------------- #

def load_seen():
    if os.path.exists(SEEN_FILE):
        try:
            with open(SEEN_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"reported": []}


def save_seen(seen):
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(seen, f, ensure_ascii=False, indent=2)


def _tags(job, reasons):
    text = (job["title"] + " " + job["description"]).lower()
    tags = []
    if any(h in text for h in PACK_PRINT_HINTS):
        tags.append("Packaging/Print")
    if any(h in text for h in ["brand", "branding"]):
        tags.append("Branding")
    if any(h in text for h in ["social media", "social-media"]):
        tags.append("Social Media")
    if any(h in text for h in ["foto", "photo", "produktfoto"]):
        tags.append("Fotografie")
    if any(h in text for h in JUNIOR_HINTS):
        tags.append("Junior")
    return tags


def write_jobs_json(all_scored, threshold, seen_urls):
    """Schreibt die komplette, bewertete Stellenliste für die Setza-App."""
    jobs = []
    for sc, reasons, j in sorted(all_scored, key=lambda x: x[0], reverse=True):
        jobs.append({
            "title": j["title"],
            "company": j["company"] or "",
            "url": j["url"],
            "source": j["source"],
            "location": j["location"],
            "remote": bool(j["remote"]),
            "score": sc,
            "reasons": reasons,
            "tags": _tags(j, reasons),
            "age_days": j["age"],
            "is_match": sc >= threshold,
            "is_new": sc >= threshold and j["url"] not in seen_urls,
        })
    payload = {
        "app": "Setza",
        "updated": dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "threshold": threshold,
        "count": len(jobs),
        "jobs": jobs,
    }
    with open(JOBS_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def append_log(platforms, found, hits, sent, note=""):
    line = f"| {now_iso()} | {platforms} | {found} | {hits} | {sent} | {note} |\n"
    if not os.path.exists(LOG_FILE):
        with open(LOG_FILE, "w", encoding="utf-8") as f:
            f.write("# Job-Scout Protokoll (24/7)\n\n")
            f.write("| Datum/Zeit | Plattformen | Gefunden | Treffer ≥Schwelle | Versendet | Hinweis |\n")
            f.write("|---|---|---|---|---|---|\n")
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line)


def main():
    print(f"== Job-Scout {now_iso()} | Schwelle {MATCH_THRESHOLD} | DRY_RUN={DRY_RUN} ==")
    seen = load_seen()
    seen_urls = {e.get("url") for e in seen.get("reported", [])}
    first_run = len(seen_urls) == 0

    all_jobs, used = [], []
    for fn in SOURCES:
        jobs = fn()
        if jobs:
            used.append(jobs[0]["source"])
        all_jobs.extend(jobs)
    print(f"Roh geladen: {len(all_jobs)} Stellen von {len(used)} Quellen")

    # dedupe innerhalb des Laufs nach URL
    uniq = {}
    for j in all_jobs:
        if j["url"]:
            uniq.setdefault(j["url"], j)
    candidates = [j for j in uniq.values() if passes_filter(j)]
    print(f"Nach Filter: {len(candidates)} Kandidaten")

    all_scored, scored = [], []
    for j in candidates:
        sc, reasons = score(j)
        all_scored.append((sc, reasons, j))
        if sc >= MATCH_THRESHOLD and j["url"] not in seen_urls:
            scored.append((sc, reasons, j))
    scored.sort(key=lambda x: x[0], reverse=True)
    print(f"Treffer >= {MATCH_THRESHOLD}: {len(scored)}")

    # Vollständige, bewertete Liste für die Setza-App schreiben (jeder Lauf)
    write_jobs_json(all_scored, MATCH_THRESHOLD, seen_urls)

    sent = 0
    if first_run and not DRY_RUN:
        # Erstlauf: alles als gesehen markieren, NICHT spammen
        for sc, reasons, j in scored:
            seen["reported"].append({"url": j["url"], "title": j["title"],
                                     "company": j["company"], "score": sc, "date": now_iso()})
        save_seen(seen)
        append_log("+".join(used), len(uniq), len(scored), 0,
                   "Erstlauf: Seeding, keine Mails")
        print("Erstlauf – Backlog als gesehen markiert, keine Mails versendet.")
        return

    for sc, reasons, j in scored:
        if sent >= MAX_ALERTS_PER_RUN:
            break
        try:
            if send_mail(j, sc, reasons):
                sent += 1
                seen["reported"].append({"url": j["url"], "title": j["title"],
                                         "company": j["company"], "score": sc, "date": now_iso()})
        except Exception as e:
            print("   ! Sendefehler:", e)

    if not DRY_RUN:
        save_seen(seen)
    append_log("+".join(used), len(uniq), len(scored), sent)
    print(f"Fertig. Versendet: {sent}")


if __name__ == "__main__":
    main()
