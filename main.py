import requests
import time
import sqlite3
import os
from bs4 import BeautifulSoup
from telegram import Bot
from telegram.error import TelegramError
from dotenv import load_dotenv
import asyncio
import logging
from playwright.async_api import async_playwright
import re

# Logging konfigurieren
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Umgebungsvariablen laden
load_dotenv()
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
CHAT_ID = os.getenv('CHAT_ID')

if not TELEGRAM_TOKEN or not CHAT_ID:
    logger.error("TELEGRAM_TOKEN oder CHAT_ID nicht in .env gesetzt!")
    exit(1)

# SQLite Datenbank initialisieren
def init_db():
    conn = sqlite3.connect('seen_ads.db')
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS seen_ads (ad_id TEXT PRIMARY KEY, model TEXT)''')
    conn.commit()
    conn.close()

def is_ad_seen(ad_id, model):
    conn = sqlite3.connect('seen_ads.db')
    c = conn.cursor()
    c.execute("SELECT ad_id FROM seen_ads WHERE ad_id = ? AND model = ?", (ad_id, model))
    result = c.fetchone()
    conn.close()
    return result is not None

def mark_ad_seen(ad_id, model):
    conn = sqlite3.connect('seen_ads.db')
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO seen_ads (ad_id, model) VALUES (?, ?)", (ad_id, model))
    conn.commit()
    conn.close()

def escape_markdown(text):
    escape_chars = r'_*[]()~`>#+-=|{}.!'
    return re.sub(f'([{re.escape(escape_chars)}])', r'\\\1', text)

async def search_kleinanzeigen(search_params):
    query = search_params['query'].replace(' ', '+')
    base_url = f"https://www.kleinanzeigen.de/s-{query}/k0?priceFrom={search_params['min_price']}&priceTo={search_params['max_price']}"

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, wie Gecko) Chrome/123.0.0.0 Safari/537.36"
            )
            page = await context.new_page()
            await page.goto(base_url)
            
            # Automatische Scrollen und Rendering der Seite
            for _ in range(5):  # Scrollt 5 Mal nach unten
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await asyncio.sleep(2)
            await page.wait_for_load_state('networkidle', timeout=40000)  # Warte, bis alle Inhalte geladen sind
            content = await page.content()  # Den gesamten HTML-Code der Seite extrahieren
            await browser.close()

            soup = BeautifulSoup(content, 'html.parser')
            ads = soup.find_all('article', class_='aditem')
            logger.info(f"Anzahl gefundener Anzeigen für {search_params['query']}: {len(ads)}")

            filtered_ads = []
            exclude_keywords = ['ankauf', 'reparatur', 'tausch', 'für', 'keyboard', 'tastatur']

            # Filter für Anzeigen, die bestimmte Keywords im Titel enthalten
            for ad in ads:
                title = ad.find('a', class_='ellipsis')
                title_text = title.text.strip().lower() if title else ''
                description = ad.find('p', class_='aditem-main--middle--description')
                desc_text = description.text.strip().lower() if description else ''

                if not title_text:
                    logger.info("Anzeige ohne Titel übersprungen")
                    continue

                if any(keyword in title_text for keyword in exclude_keywords):
                    logger.info(f"Anzeige mit ausgeschlossenem Wort im Titel übersprungen: {title_text}")
                    continue

                logger.info(f"Anzeige Titel: {title_text}")
                if 'ipad' in search_params['query'].lower() or 'iphone' in search_params['query'].lower():
                    if search_params['query'].lower().replace(' ', '') in title_text.replace(' ', '') or search_params['query'].lower().replace(' ', '') in desc_text.replace(' ', ''):
                        price_elem = ad.find('p', class_=re.compile(r'aditem-main--middle--price.*'))
                        price_text = price_elem.text.strip() if price_elem else ''
                        logger.info(f"Preis Text (HTML): {price_text}")
                        if price_text.lower() == 'vb' or not price_text:
                            filtered_ads.append(ad)
                            logger.info(f"Passende Anzeige (VB oder kein Preis): {title_text}, Preis: {price_text}")
                            continue
                        try:
                            price_clean = re.sub(r'[^\d,.]', '', price_text)
                            price_clean = price_clean.replace(',', '.').strip()
                            price = float(price_clean) if price_clean else 0.0
                            logger.info(f"Parsierter Preis: {price} €")
                            if int(search_params['min_price']) <= price <= int(search_params['max_price']):
                                filtered_ads.append(ad)
                                logger.info(f"Passende Anzeige: {title_text}, Preis: {price} €")
                        except ValueError:
                            logger.info(f"Ungültiger Preis für {title_text}: {price_text}")
                            continue

            logger.info(f"Gefilterte Anzeigen für {search_params['query']}: {len(filtered_ads)}")
            return filtered_ads
    except Exception as e:
        logger.error(f"Fehler bei der Anfrage für {search_params['query']}: {e}")
        return []

def extract_ad_data(ad):
    ad_id = ad.get('data-adid', '')
    title = ad.find('a', class_='ellipsis').text.strip() if ad.find('a', class_='ellipsis') else 'Kein Titel'
    price_elem = ad.find('p', class_=re.compile(r'aditem-main--middle--price.*'))
    price = price_elem.text.strip() if price_elem else 'Kein Preis'
    description = ad.find('p', class_='aditem-main--middle--description').text.strip() if ad.find('p', class_='aditem-main--middle--description') else 'Keine Beschreibung'
    link = 'https://www.kleinanzeigen.de' + ad.find('a', class_='ellipsis')['href'] if ad.find('a', class_='ellipsis') else ''
    return {'id': ad_id, 'title': title, 'price': price, 'description': description, 'link': link}

async def send_telegram_message(ad_data, model):
    bot = Bot(token=TELEGRAM_TOKEN)

    title = escape_markdown(ad_data['title'])
    price = escape_markdown(ad_data['price'])
    description = escape_markdown(ad_data['description'])
    link = escape_markdown(ad_data['link'])
    model = escape_markdown(model)

    message = (
        f"*Neue Anzeige gefunden für {model}\\!*\n"
        f"*Titel*: {title}\n"
        f"*Preis*: {price}\n"
        f"*Beschreibung*: {description}\n"
        f"*Link*: {link}"
    )

    try:
        logger.info(f"Telegram-Nachricht wird gesendet an {CHAT_ID}: {ad_data['title']} ({model})")
        await bot.send_message(chat_id=CHAT_ID, text=message, parse_mode='MarkdownV2')
        logger.info(f"Nachricht gesendet für Anzeige {ad_data['id']} ({model})")
    except TelegramError as e:
        logger.error(f"Fehler beim Senden der Nachricht: {e.message}")
        logger.exception("Fehlerdetails:")

async def main():
    init_db()

    search_configs = [
        {'query': 'iPhone 12', 'min_price': '100', 'max_price': '180'},
        {'query': 'iPhone 13', 'min_price': '100', 'max_price': '200'},
        {'query': 'iPhone 13 Pro', 'min_price': '120', 'max_price': '240'},
        {'query': 'iPhone 14', 'min_price': '150', 'max_price': '250'},
        {'query': 'iPhone 14 Pro', 'min_price': '120', 'max_price': '290'},
        {'query': 'iPad 10', 'min_price': '50', 'max_price': '230'},
        {'query': 'iPad Air 5', 'min_price': '50', 'max_price': '270'},
        {'query': 'iPad Air 6', 'min_price': '50', 'max_price': '300'},
        {'query': 'iPad Mini 6', 'min_price': '50', 'max_price': '220'},
        {'query': 'iPad Mini 7', 'min_price': '50', 'max_price': '350'},
        {'query': 'iPad Pro 11', 'min_price': '50', 'max_price': '400'},
        {'query': 'iPad Pro 12.9', 'min_price': '50', 'max_price': '380'},
    ]

    logger.info("Meta-Bot gestartet, suche nach neuen Anzeigen für mehrere Modelle...")

    while True:
        for config in search_configs:
            model = config['query']
            logger.info(f"Suche nach {model}...")
            ads = await search_kleinanzeigen(config)
            for ad in ads:
                ad_data = extract_ad_data(ad)
                logger.info(f"Überprüfe Anzeige: {ad_data['title']} ({model}) - bereits gesehen: {is_ad_seen(ad_data['id'], model)}")
                if not is_ad_seen(ad_data['id'], model):
                    await send_telegram_message(ad_data, model)
                    mark_ad_seen(ad_data['id'], model)
        logger.info("Warte 2 Minuten bis zur nächsten Suche...")
        await asyncio.sleep(120)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot gestoppt.")
    except Exception as e:
        logger.error(f"Schwerwiegender Fehler: {e}")
