"""
Beteiligungsportal Sachsen-Anhalt -> E-Mail-Push

Scraped die Verfahrensliste, vergleicht mit dem letzten Stand
(state/seen.json) und schickt eine Sammelmail an MAIL_TO, sobald
neue Verfahren auftauchen.

Bei keinen Neuigkeiten: keine Mail, kein Spam.

Konfiguration ueber Environment Variables - im GitHub-Actions-Workflow
werden diese aus den Repository-Secrets gespeist (siehe README).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import html
import json
import os
import smtplib
import ssl
import sys
from dataclasses import dataclass, asdict
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import urljoin

from playwright.sync_api import sync_playwright


# ---------------------------------------------------------------------------
# PORTAL-KONFIGURATION
# ---------------------------------------------------------------------------

PORTAL_URLS = [
    "https://beteiligung.sachsen-anhalt.de/portal/hauptportal/beteiligung/themen",
    "https://beteiligung.sachsen-anhalt.de/portal/rbplan/beteiligung/themen",
]

# Optionaler Themenfilter. Leer = alles durchlassen.
KEYWORDS_INCLUDE: list[str] = [
    # "Natur", "Landschaft", "Wald", "FFH", "Schutzgebiet",
    # "Solar", "Wind", "Bebauungsplan", "Flaechennutzungsplan",
]

# Optionaler Ortsfilter. Leer = ueberall in Sachsen-Anhalt.
LOCATIONS_INCLUDE: list[str] = []

# Wie weit darf der Speicher der bereits gemeldeten Verfahren zurueckreichen?
# Aeltere Eintraege werden aus state/seen.json entfernt, damit die Datei
# nicht ewig waechst. 365 Tage sind ein guter Default.
SEEN_RETENTION_DAYS = 365

MAX_PAGINATION_CLICKS = 10

STATE_PATH = Path(__file__).parent / "state" / "seen.json"


# ---------------------------------------------------------------------------
# SELEKTOREN (siehe README, Abschnitt "Selektoren pruefen")
# ---------------------------------------------------------------------------

PARSE_CONFIG = {
    "item_selector": "article, .teaser, li.result-item, .beteiligung-card",
    "link_selector": "a[href]",
    "title_selector": "h2, h3, .title, .headline",
    "description_selector": ".teaser-text, .description, p, .summary",
}


# ---------------------------------------------------------------------------
# MAIL-KONFIGURATION (aus ENV)
# ---------------------------------------------------------------------------

def env(name: str, default: str | None = None, required: bool = False) -> str:
    val = os.environ.get(name, default)
    if required and (val is None or val == ""):
        print(f"[fehler] Pflichtvariable {name} ist nicht gesetzt.", file=sys.stderr)
        sys.exit(2)
    return val or ""


def load_mail_config() -> dict:
    return {
        "host": env("SMTP_HOST", required=True),
        "port": int(env("SMTP_PORT", "587")),
        "user": env("SMTP_USER"),
        "password": env("SMTP_PASSWORD"),
        "use_tls": env("SMTP_USE_TLS", "1") == "1",
        "use_ssl": env("SMTP_USE_SSL", "0") == "1",
        "from_addr": env("SMTP_FROM", required=True),
        "to_addrs": [a.strip() for a in env("MAIL_TO", required=True).split(",") if a.strip()],
        "reply_to": env("MAIL_REPLY_TO"),
    }


# ---------------------------------------------------------------------------
# DATENMODELL
# ---------------------------------------------------------------------------

@dataclass
class FeedItem:
    title: str
    link: str
    description: str
    source_portal: str
    guid: str
    first_seen: str  # ISO-Datum

    @classmethod
    def make(cls, title: str, link: str, description: str, source_portal: str) -> "FeedItem":
        guid_source = (link or title).encode("utf-8")
        guid = hashlib.sha1(guid_source).hexdigest()
        return cls(
            title=title.strip(),
            link=link.strip(),
            description=description.strip(),
            source_portal=source_portal,
            guid=guid,
            first_seen=dt.datetime.now(dt.timezone.utc).date().isoformat(),
        )


# ---------------------------------------------------------------------------
# STATE-HANDLING
# ---------------------------------------------------------------------------

def load_seen() -> dict[str, str]:
    """Gibt {guid: first_seen_iso} zurueck."""
    if not STATE_PATH.exists():
        return {}
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
        # Migration: frueher war es vielleicht eine Liste
        if isinstance(data, list):
            today = dt.date.today().isoformat()
            return {g: today for g in data}
        return {}
    except Exception as exc:
        print(f"[warnung] seen.json konnte nicht gelesen werden: {exc}", file=sys.stderr)
        return {}


def save_seen(seen: dict[str, str]) -> None:
    # Retention: alte Eintraege loeschen
    cutoff = dt.date.today() - dt.timedelta(days=SEEN_RETENTION_DAYS)
    cleaned = {
        guid: first
        for guid, first in seen.items()
        if dt.date.fromisoformat(first) >= cutoff
    }
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(
        json.dumps(cleaned, indent=2, sort_keys=True, ensure_ascii=False),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# SCRAPING
# ---------------------------------------------------------------------------

def scrape_portal(url: str, debug: bool = False) -> list[FeedItem]:
    items: list[FeedItem] = []
    portal_label = url.split("/portal/")[-1].split("/")[0] if "/portal/" in url else url

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            # Echter Chrome-User-Agent. Manche Behoerdenportale filtern
            # alles, was nach Bot aussieht.
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            locale="de-DE",
            timezone_id="Europe/Berlin",
            extra_http_headers={
                "Accept-Language": "de-DE,de;q=0.9,en;q=0.6",
            },
        )
        page = context.new_page()
        # "commit" wartet nur, bis der Server irgendetwas zurueckschickt.
        # Wir sind dann fuer die DOM-/Renderwartung selbst zustaendig.
        try:
            page.goto(url, wait_until="commit", timeout=45_000)
        except Exception as exc:
            print(f"[fehler] goto: {exc}", file=sys.stderr)
            browser.close()
            return items
        # Jetzt aufs DOM warten...
        try:
            page.wait_for_load_state("domcontentloaded", timeout=30_000)
        except Exception:
            pass
        # ...und dann gezielt auf einen Verfahrenseintrag.
        try:
            page.wait_for_selector(PARSE_CONFIG["item_selector"], timeout=20_000)
        except Exception:
            # Falls Selektor nicht passt: trotzdem etwas Zeit fuer JS-Rendering
            page.wait_for_timeout(3000)

        for sel in [
            "button:has-text('Akzeptieren')",
            "button:has-text('Alle akzeptieren')",
            "button:has-text('Zustimmen')",
            "button:has-text('OK')",
        ]:
            try:
                if page.locator(sel).first.is_visible(timeout=1500):
                    page.locator(sel).first.click()
                    break
            except Exception:
                pass

        for _ in range(MAX_PAGINATION_CLICKS):
            try:
                more = page.locator(
                    "button:has-text('Mehr'), button:has-text('Weitere'), "
                    "a:has-text('Naechste')"
                ).first
                if more.is_visible(timeout=1500):
                    more.click()
                    page.wait_for_load_state("networkidle", timeout=10_000)
                else:
                    break
            except Exception:
                break

        if debug:
            html_dump = Path(__file__).parent / "debug_dump.html"
            html_dump.write_text(page.content(), encoding="utf-8")
            print(f"[debug] HTML in {html_dump} geschrieben.", file=sys.stderr)

        cards = page.locator(PARSE_CONFIG["item_selector"]).all()
        for card in cards:
            try:
                title_el = card.locator(PARSE_CONFIG["title_selector"]).first
                link_el = card.locator(PARSE_CONFIG["link_selector"]).first
                desc_el = card.locator(PARSE_CONFIG["description_selector"]).first

                title = title_el.inner_text(timeout=2000) if title_el.count() else ""
                href = link_el.get_attribute("href") if link_el.count() else ""
                desc = desc_el.inner_text(timeout=2000) if desc_el.count() else ""

                if not title or not href:
                    continue

                full_link = urljoin(url, href)
                items.append(FeedItem.make(title, full_link, desc, portal_label))
            except Exception as exc:
                if debug:
                    print(f"[debug] Eintrag uebersprungen: {exc}", file=sys.stderr)
                continue

        browser.close()

    return items


def apply_filters(items: list[FeedItem]) -> list[FeedItem]:
    if not KEYWORDS_INCLUDE and not LOCATIONS_INCLUDE:
        return items
    needles = [n.lower() for n in (KEYWORDS_INCLUDE + LOCATIONS_INCLUDE)]
    return [
        it
        for it in items
        if any(n in f"{it.title} {it.description}".lower() for n in needles)
    ]


def dedupe(items: list[FeedItem]) -> list[FeedItem]:
    seen: set[str] = set()
    out: list[FeedItem] = []
    for it in items:
        if it.guid in seen:
            continue
        seen.add(it.guid)
        out.append(it)
    return out


# ---------------------------------------------------------------------------
# MAIL-AUSGABE
# ---------------------------------------------------------------------------

def build_mail(new_items: list[FeedItem], mail_cfg: dict) -> EmailMessage:
    today = dt.date.today().strftime("%d.%m.%Y")
    n = len(new_items)
    subject = f"[Beteiligungsportal SA] {n} neue{'s' if n == 1 else ''} Verfahren am {today}"

    # Plaintext
    text_lines = [
        f"Heute, {today}, sind {n} neue Verfahren im Beteiligungsportal "
        "Sachsen-Anhalt aufgetaucht:",
        "",
    ]
    for it in new_items:
        text_lines.extend([
            f"* {it.title}",
            f"  Portal:       {it.source_portal}",
            f"  Beschreibung: {it.description or '(keine Kurzbeschreibung)'}",
            f"  Link:         {it.link}",
            "",
        ])
    text_lines.extend([
        "--",
        "Automatischer Hinweis. Kein Ersatz fuer die Verbandsbeteiligung",
        "nach Paragraph 63 BNatSchG oder die TOeB-Beteiligung nach Paragraph 4 BauGB.",
        "Quelle: https://beteiligung.sachsen-anhalt.de/",
    ])
    text_body = "\n".join(text_lines)

    # HTML
    html_items_parts = []
    for it in new_items:
        html_items_parts.append(
            "<li style='margin-bottom:14px'>"
            f"<a href='{html.escape(it.link)}'>"
            f"<strong>{html.escape(it.title)}</strong></a><br/>"
            f"<span style='color:#666;font-size:90%'>Portal: "
            f"{html.escape(it.source_portal)}</span><br/>"
            f"{html.escape(it.description) if it.description else '<em>(keine Kurzbeschreibung)</em>'}"
            "</li>"
        )
    html_items = "\n".join(html_items_parts)
    html_body = (
        "<html><body style='font-family:Arial,Helvetica,sans-serif;color:#222'>"
        f"<p>Heute, {today}, sind <strong>{n}</strong> neue Verfahren im "
        "Beteiligungsportal Sachsen-Anhalt aufgetaucht:</p>"
        f"<ul>{html_items}</ul>"
        "<hr/>"
        "<p style='color:#888;font-size:85%'>Automatischer Hinweis. Kein "
        "Ersatz fuer die Verbandsbeteiligung nach &sect;&nbsp;63 BNatSchG "
        "oder die T&Ouml;B-Beteiligung nach &sect;&nbsp;4 BauGB.<br/>"
        "Quelle: <a href='https://beteiligung.sachsen-anhalt.de/'>"
        "beteiligung.sachsen-anhalt.de</a></p>"
        "</body></html>"
    )

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = mail_cfg["from_addr"]
    msg["To"] = ", ".join(mail_cfg["to_addrs"])
    if mail_cfg["reply_to"]:
        msg["Reply-To"] = mail_cfg["reply_to"]
    msg["Date"] = dt.datetime.now(dt.timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")
    return msg


def send_mail(msg: EmailMessage, cfg: dict) -> None:
    if cfg["use_ssl"]:
        ctx = ssl.create_default_context()
        with smtplib.SMTP_SSL(cfg["host"], cfg["port"], context=ctx, timeout=30) as smtp:
            if cfg["user"]:
                smtp.login(cfg["user"], cfg["password"])
            smtp.send_message(msg)
    else:
        with smtplib.SMTP(cfg["host"], cfg["port"], timeout=30) as smtp:
            smtp.ehlo()
            if cfg["use_tls"]:
                ctx = ssl.create_default_context()
                smtp.starttls(context=ctx)
                smtp.ehlo()
            if cfg["user"]:
                smtp.login(cfg["user"], cfg["password"])
            smtp.send_message(msg)


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def run(debug: bool = False, dry_run: bool = False) -> int:
    # 1. Scrapen
    all_items: list[FeedItem] = []
    for url in PORTAL_URLS:
        try:
            items = scrape_portal(url, debug=debug)
            print(f"[ok] {url}: {len(items)} Eintraege gefunden", file=sys.stderr)
            all_items.extend(items)
        except Exception as exc:
            print(f"[fehler] {url}: {exc}", file=sys.stderr)

    all_items = dedupe(all_items)
    all_items = apply_filters(all_items)

    # 2. Diff gegen Vorlauf
    seen = load_seen()
    new_items = [it for it in all_items if it.guid not in seen]
    print(f"[ok] {len(new_items)} neue von {len(all_items)} aktuellen Verfahren", file=sys.stderr)

    # 3. Wenn neu vorhanden, Mail bauen + versenden
    if new_items:
        mail_cfg = load_mail_config()
        msg = build_mail(new_items, mail_cfg)
        if dry_run:
            print("[dry-run] Wuerde folgende Mail versenden:", file=sys.stderr)
            print("-" * 60)
            print(f"To:      {msg['To']}")
            print(f"From:    {msg['From']}")
            print(f"Subject: {msg['Subject']}")
            print()
            # Plaintext-Teil ausgeben
            for part in msg.iter_parts():
                if part.get_content_type() == "text/plain":
                    print(part.get_content())
                    break
            print("-" * 60)
        else:
            try:
                send_mail(msg, mail_cfg)
                print(f"[ok] Mail an {msg['To']} verschickt.", file=sys.stderr)
            except Exception as exc:
                print(f"[fehler] Mailversand fehlgeschlagen: {exc}", file=sys.stderr)
                return 1

    # 4. State aktualisieren
    today_iso = dt.date.today().isoformat()
    for it in all_items:
        seen.setdefault(it.guid, today_iso)
    if not dry_run:
        save_seen(seen)
        print(f"[ok] state/seen.json aktualisiert ({len(seen)} Eintraege).", file=sys.stderr)

    return 0


if __name__ == "__main__":
    debug_flag = "--debug" in sys.argv
    dry_flag = "--dry-run" in sys.argv
    sys.exit(run(debug=debug_flag, dry_run=dry_flag))
