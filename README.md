# HDD-Schnäppchen

Persönlicher Scanner für neue 3,5"-SATA-NAS- und Enterprise-Festplatten mit 3–8 TB unter einer EUR/TB-Grenze. `schnaeppchen.py` läuft auf dem Windows-11-PC des Besitzers mit Python 3.14 und Playwright Chromium und schickt das Ergebnis per Telegram.

## Quellen

Die fünf Quellen laufen parallel, jede mit eigenem Headless-Chromium:

- eBay.de
- Kleinanzeigen (`kleinanzeigen.de`)
- Amazon.de
- Vinted.de
- Geizhals (`geizhals.de`), Kategorie 3,5" SATA ab 3 TB, sortiert nach EUR/TB

Gesucht wird nach NAS- und Enterprise-Familien: WD Red Plus, WD Red Pro, WD Gold, Ultrastar/HGST, Seagate Exos, IronWolf, IronWolf Pro, Toshiba N300 und Toshiba MG.

## Filter

Die Grenze liegt bei **32 EUR/TB** inklusive Versand und Einfuhr. `max_eur_per_tb` in `config.json` überschreibt sie, gültig sind Werte von 10 bis 100. Die Vorauswahl reicht bis zum 1,15-Fachen. Unter 15 EUR/TB gilt der Preis als unplausibel.

- **Nur SATA.** Reine SAS-Angebote, auch über die Teilenummer erkannt, stehen nur zur Info im Log und im HTML-Bericht. SAS mit gleichzeitig genanntem SATA und Angebote ohne erkennbares Interface bleiben in der Deal-Liste.
- **Nur Neuware.** Der Text muss neu, unbenutzt, versiegelt, OVP oder „Neu (Sonstige)“ ohne Öffnen oder Test sagen. Gebraucht, Refurbished, Pulls, Betriebsstunden, B-Ware, Retouren, RMA und ähnliche Hinweise entfallen. Amazon und Geizhals zählen als neu, solange der Text das nicht widerspricht; die Amazon-Buybox muss Neuware mit lesbarem Neupreis sein.
- **Detailseiten.** Vorschau-Treffer und unklare Kandidaten werden auf der Angebotsseite geprüft: Beschreibung, Zustand, Kapazität, Standort, Versand und ob das Angebot noch online ist. Vinted wird nie ungeprüft gemeldet. Bekannte URLs aus `gesehen.json`, die die Suche diesmal nicht zeigt, werden direkt noch einmal geöffnet.
- **Einfuhrumsatzsteuer.** Liegt der Artikelstandort außerhalb der EU, setzt das Skript 19 % EUSt auf Ware plus Versand an (bei HDDs 0 % Zoll). Zeigt eBay höhere Einfuhrabgaben, gilt der höhere Betrag.
- **Abholpreis und „auf dem Weg“.** Die Pendelroute ist Furtwangen im Schwarzwald–Herrenberg über Vöhrenbach und Villingen-Schwenningen (OSRM, sonst feste Wegpunkte). Ein Standort höchstens 20 km von der Strecke bekommt das Flag **AUF DEM WEG**. Bei Kleinanzeigen zählt dann der Artikelpreis ohne Versand. Liegt nur dieser Abholpreis unter der Grenze, bleibt der Deal nur, wenn Abholung erlaubt ist und der Standort auf der Route liegt.
- **Weiterhin verfügbar.** URLs aus `gesehen.json`, die in diesem Lauf erneut online und qualifiziert sind, stehen getrennt unter „Weiterhin verfügbar“, mit dem Datum des ersten Sehens. Neue Treffer kommen nach dem Lauf in `gesehen.json`.

Zusätzlich: Formfaktor 3,5" (2,5" entfällt), Kapazität 3–8 TB, und ein erkennbares Dauerbetrieb-, NAS- oder Enterprise-Modell. Consumer-Platten, SMR, SSD, Gehäuse, leere Trays und defekte Ware entfallen.

## Aufruf

```text
python schnaeppchen.py [Optionen]
```

| Flag | Wirkung |
| --- | --- |
| `--gruendlich` | Ausführlicher Lauf: Kapazität×Modell-Matrix, Teilenummern, mehr Seiten und Detailseiten (etwa 1–2 Stunden). Schreibt zusätzlich `logs/gruendlich_YYYYMMDD_HHMM.json`. |
| `--test` | Schreibt `gesehen.json` und `referenz.json` nicht und sendet kein Telegram. Logdatei: `logs/YYYY-MM-DD_test.txt`. |
| `--json PFAD` | Schreibt alle qualifizierten Deals zusätzlich als JSON an diesen Pfad. |
| `--ignore-seen` | Ignoriert `gesehen.json` beim Filtern. Die Datei wird am Ende trotzdem aktualisiert. |
| `--route-neu` | Holt die Pendelroute Furtwangen–Herrenberg neu von OSRM und überschreibt `route.json`. |
| `--max-queries N` | Begrenzt die Anzahl der Suchbegriffe. Nur für Tests. |

## Ausgabedateien

Alles liegt neben dem Skript:

| Datei | Inhalt |
| --- | --- |
| `logs/YYYY-MM-DD.txt` | Tageslog, angehängt. Enthält auch SAS-only und auf der Detailseite verworfene Angebote. |
| `logs/YYYY-MM-DD_test.txt` | Log eines Laufs mit `--test`. |
| `berichte/YYYY-MM-DD.html` | Tagesbericht mit Neu, Weiterhin verfügbar, Marktüberblick, SAS-only und Quellenstatistik. |
| `gesehen.json` | Gemeldete URLs plus Preis, Standort, EUR/TB und erstes/letztes Sehen. |
| `referenz.json` | Bisher günstigster detailgeprüfter SATA-Deal. |
| `geo_cache.json` | Geocoding-Cache (Nominatim). |
| `route.json` | Polylinie der Pendelroute. |
| `logs/gruendlich_YYYYMMDD_HHMM.json` | Deal-JSON nach `--gruendlich`, wenn `--json` nicht gesetzt ist. |

Sind `telegram_bot_token` und `telegram_chat_id` gesetzt, geht dieselbe Meldung an Telegram. Ein `--test`-Lauf sendet nichts.

## Einrichtung

Python 3.14, im Projektordner:

```text
pip install -r requirements.txt
playwright install chromium
copy config.example.json config.json
```

`config.json` bleibt lokal. Gelesen werden nur diese Schlüssel:

- `telegram_bot_token` und `telegram_chat_id`: leer lassen, dann kein Versand
- `max_eur_per_tb`: Grenze in EUR/TB, im Beispiel 32

Chromium startet über die von Playwright abgelegte `chrome.exe` unter `%LOCALAPPDATA%\ms-playwright`.

## Täglicher Lauf

`run_once_per_day.ps1` ist der Wrapper für die Windows-Aufgabenplanung. Der Ordner ist `C:\Users\aiche\schnaeppchen`, der Interpreter `C:\Users\aiche\AppData\Local\Programs\Python\Python314\python.exe`.

- `last_run.txt` enthält das Datum `yyyy-MM-dd`. Steht dort schon heute, beendet sich der Wrapper sofort mit Code 0.
- Die Prozessoraffinität wird auf `FFFC` gesetzt. Kern 0 (APIC 0 und 1) bleibt aus, weil dort wiederholt Machine-Check-Fehler auftreten. Python und Chromium erben die Maske.
- Nur bei Exit-Code 0 schreibt der Wrapper das heutige Datum nach `last_run.txt`. Ein fehlgeschlagener Lauf kann am selben Tag noch einmal starten.
