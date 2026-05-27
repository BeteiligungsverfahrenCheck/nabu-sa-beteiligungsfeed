# NABU-SA Beteiligungs-Mailpush

Schickt einmal werktäglich eine Sammelmail an `mail@nabu.lsa.de`,
wenn im Beteiligungsportal Sachsen-Anhalt neue Verfahren auftauchen.
Keine Verfahren = keine Mail. Bereits gemeldete Verfahren werden in
`state/seen.json` gemerkt und nicht erneut versandt.

> **Einordnung:** Dieser Push ersetzt nicht die formelle
> Verbandsbeteiligung nach § 63 BNatSchG oder die TöB-Beteiligung nach
> § 4 BauGB. Er ist eine Frühwarnung, falls ein Anschreiben nicht bei
> euch ankommt.

---

## 1. Setup im Überblick

1. Repo anlegen (Public, NABU-SA-GitHub-Account).
2. Dateien hochladen (`scraper.py`, `requirements.txt`,
   `.github/workflows/mailpush.yml`, `state/.gitkeep`).
3. In **Settings → Secrets and variables → Actions** die SMTP-Daten
   hinterlegen (siehe Abschnitt 2).
4. **Actions → Beteiligungsportal-Mailpush → Run workflow** für den
   ersten manuellen Lauf.
5. Mail prüfen, Selektoren ggf. anpassen.

---

## 2. Benötigte Repository-Secrets

In *Settings → Secrets and variables → Actions → New repository
secret* folgende Werte anlegen. Die Empfänger-Adresse `mail@nabu.lsa.de`
(steht im Code nirgends fest) wird über `MAIL_TO` gesetzt — wenn das ein
Tippfehler ist und ihr eigentlich `mail@nabu-lsa.de` meint, einfach hier
ändern, das Skript bleibt gleich.

| Secret           | Beispiel / Beschreibung                                   |
|------------------|-----------------------------------------------------------|
| `SMTP_HOST`      | `mail.nabu-lsa.de` oder `smtp.your-provider.de`           |
| `SMTP_PORT`      | `587` (Submission/STARTTLS) oder `465` (SMTPS)            |
| `SMTP_USER`      | Postfach-Login, z. B. `feedbot@nabu-lsa.de`               |
| `SMTP_PASSWORD`  | Passwort des Postfachs                                    |
| `SMTP_USE_TLS`   | `1` bei Port 587, sonst `0`                               |
| `SMTP_USE_SSL`   | `1` bei Port 465, sonst `0`                               |
| `SMTP_FROM`      | Absenderadresse, z. B. `feedbot@nabu-lsa.de`              |
| `MAIL_TO`        | Empfänger, hier: `mail@nabu.lsa.de` (oder Komma-Liste)    |
| `MAIL_REPLY_TO`  | optional, z. B. `info@nabu-lsa.de` für Rückantworten      |

**Wichtig zur Auswahl:** Wenn ihr einen eigenen Mailserver habt
(`mail.nabu-lsa.de`), nehmt den. Sonst geht jeder SMTP-Anbieter — wir
brauchen Authentizität (DKIM/SPF) nur, wenn ihr nach extern senden
würdet. Hier geht die Mail intern an `nabu.lsa.de`, das ist unkritisch
solange der Empfangsserver den Absender annimmt.

Wenn die IT keinen dedizierten Funktionsaccount anlegen möchte,
funktioniert auch ein Relay-Service (Mailjet, Brevo, Postmark — alle
mit kostenfreier Stufe bis einige hundert Mails/Monat) als
SMTP-Backend.

---

## 3. Dateien in der Übersicht

```
nabu-sa-mailpush/
├── scraper.py                       # Hauptskript
├── requirements.txt                 # Playwright
├── state/
│   ├── .gitkeep
│   └── seen.json                    # wird vom Skript geschrieben
└── .github/
    └── workflows/
        └── mailpush.yml             # Cron + manuell
```

---

## 4. Lokal testen (vor dem ersten echten Lauf)

```bash
git clone https://github.com/<euer-user>/nabu-sa-mailpush.git
cd nabu-sa-mailpush
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium

export SMTP_HOST=mail.nabu-lsa.de
export SMTP_PORT=587
export SMTP_USER=feedbot@nabu-lsa.de
export SMTP_PASSWORD='...'
export SMTP_USE_TLS=1
export SMTP_FROM=feedbot@nabu-lsa.de
export MAIL_TO=mail@nabu.lsa.de

# Dry-Run: Mail wird nur ausgegeben, nicht versandt; State bleibt unverändert
python scraper.py --dry-run

# Echtlauf
python scraper.py
```

---

## 5. Selektoren prüfen (nur falls 0 Einträge gefunden werden)

Wie schon beim RSS-Setup: einmal mit `--debug` lokal laufen lassen,
`debug_dump.html` im Browser öffnen, einen Verfahrenseintrag suchen,
dessen umschließenden Container identifizieren und
`PARSE_CONFIG` in `scraper.py` anpassen.

---

## 6. Filter

In `scraper.py` oben:

```python
KEYWORDS_INCLUDE = ["Natur", "Landschaft", "Wald", "FFH"]
LOCATIONS_INCLUDE = ["Burg", "Magdeburg", "Wittenberg"]
```

Leer = alle Verfahren. Sobald mindestens ein Stichwort gesetzt ist,
landen nur Treffer im Mailpush. Beides wird Case-insensitive im Titel
und in der Kurzbeschreibung gesucht.

---

## 7. Cron-Frequenz anpassen

Default in `mailpush.yml`: werktags 06:30 UTC.

* Einmal täglich morgens an Werktagen: `30 6 * * 1-5` *(Default)*
* Alle drei Stunden, ohne Wochenende: `0 */3 * * 1-5`
* Nur Montag, Mittwoch, Freitag morgens: `30 6 * * 1,3,5`

Cron läuft in UTC; im Sommer +2h für Berlin-Zeit, im Winter +1h.

---

## 8. Outlook-Regel zum sauberen Einsortieren

Damit die Mails nicht im allgemeinen Posteingang versacken, empfehle
ich euch eine Outlook-Regel auf dem `mail@nabu.lsa.de`-Postfach:

* *Bedingung:* Betreff enthält „Beteiligungsportal SA"
* *Aktion:* Verschieben in Ordner „Beteiligungsverfahren"
* *Aktion:* Markieren als wichtig
* *Aktion (optional):* Weiterleiten an Naturschutz-Referat

So ist die Mail-Eingangsverteilung sauber, und die Verfahrens-Mails
landen am richtigen Schreibtisch — ohne dass jeder Kollege selbst RSS
einrichten muss.

---

## 9. Was tun bei Fehlern

* **Mail kommt nicht an** → Actions-Log prüfen, dort steht entweder
  „Mailversand fehlgeschlagen: …" mit Begründung, oder „0 neue von X" —
  dann gab's einfach nichts Neues.
* **Mail kommt zu oft** → Cron-Frequenz reduzieren *oder* prüfen, ob
  `state/seen.json` korrekt committet wird (sonst denkt der Bot bei
  jedem Lauf, alles wäre neu).
* **Falsche Verfahren in der Mail** → Filter (`KEYWORDS_INCLUDE`,
  `LOCATIONS_INCLUDE`) schärfen.
* **GitHub schließt das Repo wegen Inaktivität** → erst nach 60 Tagen
  Pause, und nur die Cron-Schedules. Manueller Lauf reicht, um sie
  wieder zu aktivieren.

---

## 10. Lizenz

MIT für das Skript. Die abgerufenen Daten gehören dem Land
Sachsen-Anhalt; das Skript ist eine technische Umverpackung
öffentlicher Inhalte.
