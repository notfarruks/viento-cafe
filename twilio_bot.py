import logging
import sys
from fastapi import FastAPI, Form, Response, Request, HTTPException
from twilio.twiml.messaging_response import MessagingResponse
from twilio.request_validator import RequestValidator
from datetime import datetime
from collections import Counter
import gspread
import gspread.exceptions
from google.oauth2.service_account import Credentials
from fastapi.middleware.trustedhost import TrustedHostMiddleware
import redis
import json
import os
import uuid
import asyncio
from concurrent.futures import ThreadPoolExecutor
from lexicon import LEXICON

# ─── Logging Setup ────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("viento")

def log(level: str, msg: str, **context):
    ctx = " ".join(f"{k}={v}" for k, v in context.items())
    full_msg = f"{msg} | {ctx}" if ctx else msg
    getattr(logger, level)(full_msg)

# ─── App Setup ────────────────────────────────────────────────────────────────
app = FastAPI(title="Viento Cafe Pro - Premium Automated Waiter")
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["*"])
executor = ThreadPoolExecutor(max_workers=4)

# ─── Redis Session Store ──────────────────────────────────────────────────────
redis_client = redis.from_url(
    os.environ.get("REDIS_URL", "redis://localhost:6379"),
    decode_responses=True
)
SESSION_TTL = 60 * 60 * 6

def get_session(phone: str) -> dict:
    data = redis_client.get(f"session:{phone}")
    if data:
        return json.loads(data)
    log("info", "New session created", phone=phone[-6:])
    return {"lang": None, "state": "IDLE", "basket": [], "table": None}

def save_session(phone: str, session: dict):
    redis_client.setex(f"session:{phone}", SESSION_TTL, json.dumps(session))

# ─── Rate Limiting ────────────────────────────────────────────────────────────
def is_rate_limited(phone: str) -> bool:
    key = f"rate:{phone}"
    count = redis_client.get(key)
    if count and int(count) >= 10:
        log("warning", "Rate limit hit", phone=phone[-6:])
        return True
    pipe = redis_client.pipeline()
    pipe.incr(key)
    pipe.expire(key, 60)
    pipe.execute()
    return False

# ─── Twilio Signature Validation ─────────────────────────────────────────────
async def validate_twilio_request(request: Request) -> dict:
    auth_token = os.environ.get("TWILIO_AUTH_TOKEN", "")
    validator = RequestValidator(auth_token)
    form_data = await request.form()
    params = dict(form_data)
    forwarded_proto = request.headers.get("x-forwarded-proto", "https")
    forwarded_host = request.headers.get("x-forwarded-host", request.headers.get("host", ""))
    url = f"{forwarded_proto}://{forwarded_host}{request.url.path}"
    signature = request.headers.get("X-Twilio-Signature", "")
    if not validator.validate(url, params, signature):
        log("warning", "Invalid Twilio signature rejected", url=url)
        raise HTTPException(status_code=403, detail="Invalid Twilio signature")
    return params

# ─── Google Sheets Auth ───────────────────────────────────────────────────────
_sheets_client = None

def get_google_client(force_refresh: bool = False):
    global _sheets_client
    if _sheets_client is None or force_refresh:
        creds_json = json.loads(os.environ.get("GOOGLE_CREDS"))
        scopes = [
            "https://spreadsheets.google.com/feeds",
            "https://www.googleapis.com/auth/drive"
        ]
        creds = Credentials.from_service_account_info(creds_json, scopes=scopes)
        _sheets_client = gspread.authorize(creds)
        log("info", "Google Sheets client initialized", force_refresh=force_refresh)
    return _sheets_client

# ─── Dynamic Menu ─────────────────────────────────────────────────────────────
PRICES = {}
ITEMS_MAP = {}
DISPLAY_NAMES = {"az": {}, "ru": {}, "en": {}}

def load_menu():
    global PRICES, ITEMS_MAP, DISPLAY_NAMES
    try:
        client = get_google_client()
        spreadsheet = client.open("Cafe_Orders_DB")
        menu_sheet = spreadsheet.worksheet("Menu")
        menu_records = menu_sheet.get_all_records()

        new_prices = {}
        new_items_map = {}
        for row in menu_records:
            item_id = str(row["id"])
            name = row["name"]
            price = float(row["price"])
            new_prices[name] = price
            new_items_map[item_id] = name

        new_display = {"az": {}, "ru": {}, "en": {}}
        for lang, sheet_name in [("az", "Menu_AZ"), ("ru", "Menu_RU")]:
            try:
                lang_sheet = spreadsheet.worksheet(sheet_name)
                lang_records = lang_sheet.get_all_records()
                for row in lang_records:
                    new_display[lang][str(row["id"])] = row["display_name"]
            except Exception as e:
                log("warning", f"Could not load {sheet_name}", error=str(e))

        new_display["en"] = {k: v for k, v in new_items_map.items()}
        PRICES = new_prices
        ITEMS_MAP = new_items_map
        DISPLAY_NAMES = new_display
        log("info", "Menu loaded successfully", items=len(PRICES))
    except Exception as e:
        log("error", "Failed to load menu", error=str(e))

def build_menu_text(lang: str, table: str) -> str:
    headers = {
        "az": f"☕ *Masa {table} üçün Menyu* ☕\n\n",
        "ru": f"☕ *Меню для Столика {table}* ☕\n\n",
        "en": f"☕ *Menu for Table {table}* ☕\n\n"
    }
    footers = {
        "az": "\n\n👉 Sifariş etmək istədiyiniz məhsulun *nömrəsini* yazın!",
        "ru": "\n\n👉 Введите *номер* товара для добавления в корзину!",
        "en": "\n\n👉 Reply with the item *number* to add it to your cart!"
    }
    number_emojis = {"1": "1️⃣", "2": "2️⃣", "3": "3️⃣", "4": "4️⃣",
                     "5": "5️⃣", "6": "6️⃣", "7": "7️⃣", "8": "8️⃣", "9": "9️⃣"}
    lines = []
    for item_id, base_name in ITEMS_MAP.items():
        display = DISPLAY_NAMES[lang].get(item_id, base_name)
        price = PRICES.get(base_name, 0.0)
        emoji = number_emojis.get(item_id, f"{item_id}.")
        lines.append(f"{emoji} {display} - {price:.2f} AZN")
    return headers[lang] + "\n".join(lines) + footers[lang]

# ─── Language Picker Message ──────────────────────────────────────────────────
def build_language_picker(table: str) -> str:
    return (
        f"👋 *Viento Cafe*-a xoş gəlmisiniz! / Welcome! / Добро пожаловать!\n"
        f"📍 *Masa / Table / Стол: {table}*\n\n"
        f"Zəhmət olmasa dil seçin / Please select your language / Выберите язык:\n\n"
        f"🇦🇿 *AZ* — Azərbaycan dili\n"
        f"🇬🇧 *EN* — English\n"
        f"🇷🇺 *RU* — Русский"
    )

@app.on_event("startup")
async def startup_event():
    log("info", "Viento Cafe bot starting up...")
    load_menu()

# ─── Google Sheets: Write Order ───────────────────────────────────────────────
def write_to_google_sheets(phone, name, item_or_request, table_num, lang, order_id):
    def _attempt(client):
        sheet = client.open("Cafe_Orders_DB").sheet1
        current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        sheet.append_row([phone, name, item_or_request, table_num, lang, current_time, order_id, "Preparing"])

    try:
        _attempt(get_google_client())
        log("info", "Order written to Sheets", order=order_id, table=table_num, phone=phone[-6:])
    except gspread.exceptions.APIError as e:
        if e.response.status_code == 401:
            log("warning", "Token expired, refreshing", order=order_id)
            try:
                _attempt(get_google_client(force_refresh=True))
                log("info", "Order written after token refresh", order=order_id)
            except Exception as retry_error:
                log("error", "Sheets write failed after retry", order=order_id, error=str(retry_error))
        else:
            log("error", "Sheets API error", order=order_id, status=e.response.status_code)
    except Exception as e:
        log("error", "Sheets write failed", order=order_id, error=str(e))

# ─── Google Sheets: Fetch Order Status ───────────────────────────────────────
def fetch_order_status_from_sheets(phone):
    def _attempt(client):
        sheet = client.open("Cafe_Orders_DB").sheet1
        all_records = sheet.get_all_records()
        for record in reversed(all_records):
            db_phone = str(record.get("Phone Number", "")).replace("whatsapp:", "").strip()
            user_phone = str(phone).replace("whatsapp:", "").strip()
            if db_phone == user_phone and db_phone != "":
                return {
                    "found": True,
                    "order_id": record.get("Order ID"),
                    "status": record.get("Status")
                }
        return {"found": False}

    try:
        result = _attempt(get_google_client())
        log("info", "Status fetched", phone=phone[-6:], found=result["found"])
        return result
    except gspread.exceptions.APIError as e:
        if e.response.status_code == 401:
            log("warning", "Token expired on status fetch, refreshing", phone=phone[-6:])
            try:
                return _attempt(get_google_client(force_refresh=True))
            except Exception as retry_error:
                log("error", "Status fetch failed after retry", phone=phone[-6:], error=str(retry_error))
                return {"found": False}
        else:
            log("error", "Sheets API error on status fetch", status=e.response.status_code)
            return {"found": False}
    except Exception as e:
        log("error", "Status fetch failed", phone=phone[-6:], error=str(e))
        return {"found": False}

# ─── Reload Menu Endpoint ─────────────────────────────────────────────────────
@app.post("/reload-menu")
async def reload_menu(request: Request):
    api_key = request.headers.get("X-Admin-Key", "")
    if api_key != os.environ.get("ADMIN_KEY", ""):
        log("warning", "Unauthorized reload-menu attempt")
        raise HTTPException(status_code=403, detail="Unauthorized")
    load_menu()
    return {"status": "ok", "items": len(PRICES)}

# ─── Main Webhook Endpoint ────────────────────────────────────────────────────
@app.post("/whatsapp")
async def incoming_whatsapp(request: Request):
    params = await validate_twilio_request(request)
    Body = params.get("Body", "")
    From = params.get("From", "")
    user_text = Body.lower().strip()
    response = MessagingResponse()
    session = get_session(From)

    if is_rate_limited(From):
        response.message("⚠️ Too many messages. Please wait a moment.")
        return Response(content=str(response), media_type="application/xml")

    log("info", "Incoming message", phone=From[-6:], state=session["state"], text=user_text[:20])

    # ─── STEP 1: TABLE ASSIGNMENT ─────────────────────────────────────────────
    # Triggered by QR code scan which pre-fills "table5" etc.
    if "table" in user_text or "masa" in user_text or "стол" in user_text:
        table_digits = "".join([char for char in user_text if char.isdigit()])
        if table_digits:
            session["table"] = table_digits
            session["state"] = "WAITING_FOR_LANG"
            session["lang"] = None
            log("info", "Table assigned, waiting for language", phone=From[-6:], table=table_digits)
            response.message(build_language_picker(table_digits))
            save_session(From, session)
            return Response(content=str(response), media_type="application/xml")

    # ─── STEP 2: LANGUAGE SELECTION ───────────────────────────────────────────
    if session["state"] == "WAITING_FOR_LANG" or session["lang"] is None:
        if user_text in ["az", "en", "ru"]:
            session["lang"] = user_text
            session["state"] = "IDLE"
            table = session["table"] or "?"
            log("info", "Language selected", phone=From[-6:], lang=user_text, table=table)
            response.message(LEXICON[user_text]["welcome"].format(table=table))
            save_session(From, session)
            return Response(content=str(response), media_type="application/xml")
        else:
            # Customer sent something else before picking a language
            table = session["table"] or "?"
            response.message(build_language_picker(table))
            save_session(From, session)
            return Response(content=str(response), media_type="application/xml")

    # ─── FROM HERE: LANGUAGE AND TABLE ARE SET ────────────────────────────────
    lang = session["lang"]
    table = session["table"] or "Takeaway"

    # 🌍 MID-SESSION LANGUAGE SWITCH
    if user_text in ["az", "ru", "en"]:
        session["lang"] = user_text
        lang = user_text
        log("info", "Language switched mid-session", phone=From[-6:], lang=user_text)
        response.message(LEXICON[lang]["welcome"].format(table=table))
        save_session(From, session)
        return Response(content=str(response), media_type="application/xml")

    # 🚨 CALL WAITER
    if user_text in ["waiter", "ofisiant", "официант"]:
        log("info", "Waiter requested", phone=From[-6:], table=table)
        asyncio.get_event_loop().run_in_executor(
            executor, write_to_google_sheets,
            From, "Valued Customer", "🚨 NEEDS PHYSICAL WAITER", table, lang.upper(), "N/A"
        )
        response.message(LEXICON[lang]["waiter_alerted"])
        save_session(From, session)
        return Response(content=str(response), media_type="application/xml")

    # 🔍 ORDER STATUS
    if user_text in ["status", "состояние", "vəziyyət"]:
        result = await asyncio.get_event_loop().run_in_executor(
            executor, fetch_order_status_from_sheets, From
        )
        if result["found"]:
            response.message(LEXICON[lang]["status_success"].format(
                order_id=result["order_id"], status=result["status"]
            ))
        else:
            response.message(LEXICON[lang]["status_not_found"])
        save_session(From, session)
        return Response(content=str(response), media_type="application/xml")

    # 🗑️ CLEAR BASKET
    if user_text in ["sil", "clear", "очистить"]:
        log("info", "Basket cleared", phone=From[-6:])
        session["basket"] = []
        session["state"] = "IDLE"
        response.message(LEXICON[lang]["empty_basket"])
        save_session(From, session)
        return Response(content=str(response), media_type="application/xml")

    # 🛒 CHECKOUT
    if user_text in ["tesdiq", "təsdiq", "checkout", "подтвердить"]:
        if not session["basket"]:
            response.message(LEXICON[lang]["empty_basket"])
        else:
            session["state"] = "WAITING_FOR_NAME"
            item_counts = Counter(session["basket"])
            summary_lines = []
            grand_total = 0.0
            for item, qty in item_counts.items():
                item_cost = PRICES.get(item, 0.0) * qty
                grand_total += item_cost
                display = next(
                    (DISPLAY_NAMES[lang].get(k) for k, v in ITEMS_MAP.items() if v == item), item
                )
                summary_lines.append(f"• {qty}x {display} ({item_cost:.2f} AZN)")
            basket_summary = "\n".join(summary_lines)
            log("info", "Checkout initiated", phone=From[-6:], total=grand_total, items=len(item_counts))
            response.message(LEXICON[lang]["ask_name"].format(
                basket_details=basket_summary, total_price=grand_total
            ))
        save_session(From, session)
        return Response(content=str(response), media_type="application/xml")

    # 🔄 WAITING FOR ITEM SELECTION
    if session["state"] == "WAITING_FOR_ITEM":
        if user_text in ITEMS_MAP:
            chosen_item = ITEMS_MAP[user_text]
            session["basket"].append(chosen_item)
            display = DISPLAY_NAMES[lang].get(user_text, chosen_item)
            log("info", "Item added to basket", phone=From[-6:], item=chosen_item)
            response.message(LEXICON[lang]["added_item"].format(item=display))
        else:
            if user_text not in ["menu", "menyu", "меню"]:
                response.message(LEXICON[lang]["fallback"])
        save_session(From, session)
        return Response(content=str(response), media_type="application/xml")

    # 📝 WAITING FOR CUSTOMER NAME
    elif session["state"] == "WAITING_FOR_NAME":
        customer_name = Body.strip()
        generated_id = str(uuid.uuid4())[:8].upper()
        item_counts = Counter(session["basket"])
        summary_lines = []
        grand_total = 0.0
        db_items = []
        for item, qty in item_counts.items():
            item_cost = PRICES.get(item, 0.0) * qty
            grand_total += item_cost
            display = next(
                (DISPLAY_NAMES[lang].get(k) for k, v in ITEMS_MAP.items() if v == item), item
            )
            summary_lines.append(f"• {qty}x {display} ({item_cost:.2f} AZN)")
            db_items.append(f"{qty}x {item}")
        final_order_string = ", ".join(db_items)
        basket_summary = "\n".join(summary_lines)
        log("info", "Order confirmed", phone=From[-6:], order=generated_id, name=customer_name, total=grand_total, table=table)
        asyncio.get_event_loop().run_in_executor(
            executor, write_to_google_sheets,
            From, customer_name, final_order_string, table, lang.upper(), generated_id
        )
        session["basket"] = []
        session["state"] = "IDLE"
        response.message(LEXICON[lang]["confirmed"].format(
            name=customer_name,
            basket_details=basket_summary,
            total_price=grand_total,
            order_id=generated_id,
            table=table
        ))
        save_session(From, session)
        return Response(content=str(response), media_type="application/xml")

    # 🏁 IDLE ROUTING
    if user_text in ["menu", "menyu", "меню"]:
        response.message(build_menu_text(lang, table))
        session["state"] = "WAITING_FOR_ITEM"
    else:
        response.message(LEXICON[lang]["welcome"].format(table=table))

    save_session(From, session)
    return Response(content=str(response), media_type="application/xml")