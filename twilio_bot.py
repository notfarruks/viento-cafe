from fastapi import FastAPI, Form, Response, Request, HTTPException
from twilio.twiml.messaging_response import MessagingResponse
from twilio.request_validator import RequestValidator
from datetime import datetime
from collections import Counter
import gspread
import gspread.exceptions
from google.oauth2.service_account import Credentials
import redis
import json
import os
import uuid
import asyncio
from concurrent.futures import ThreadPoolExecutor
from lexicon import LEXICON

app = FastAPI(title="Viento Cafe Pro - Premium Automated Waiter")
executor = ThreadPoolExecutor(max_workers=4)

# ─── Redis Session Store ───────────────────────────────────────────────────────
redis_client = redis.from_url(
    os.environ.get("REDIS_URL", "redis://localhost:6379"),
    decode_responses=True
)
SESSION_TTL = 60 * 60 * 6

def get_session(phone: str) -> dict:
    data = redis_client.get(f"session:{phone}")
    if data:
        return json.loads(data)
    return {"lang": "az", "state": "IDLE", "basket": [], "table": "0 (Takeaway)"}

def save_session(phone: str, session: dict):
    redis_client.setex(f"session:{phone}", SESSION_TTL, json.dumps(session))

# ─── Twilio Signature Validation ──────────────────────────────────────────────
async def validate_twilio_request(request: Request) -> dict:
    auth_token = os.environ.get("TWILIO_AUTH_TOKEN", "")
    validator = RequestValidator(auth_token)
    form_data = await request.form()
    return dict(form_data)
    params = dict(form_data)
    url = str(request.url)
    signature = request.headers.get("X-Twilio-Signature", "")
    if not validator.validate(url, params, signature):
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
    return _sheets_client

# ─── Dynamic Menu (loaded from Sheets on startup) ────────────────────────────
# These are populated by load_menu() at startup and can be reloaded anytime
PRICES = {}        # {"Flat White": 6.00, ...}
ITEMS_MAP = {}     # {"1": "Flat White", ...}
DISPLAY_NAMES = {  # {"az": {"1": "Flat White"}, "ru": {"1": "Флэт Уайт"}, ...}
    "az": {},
    "ru": {},
    "en": {}
}

def load_menu():
    """Load menu from Google Sheets and populate global dicts."""
    global PRICES, ITEMS_MAP, DISPLAY_NAMES
    try:
        client = get_google_client()
        spreadsheet = client.open("Cafe_Orders_DB")

        # Load base menu (id, name, price)
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

        # Load localized display names
        new_display = {"az": {}, "ru": {}, "en": {}}

        for lang, sheet_name in [("az", "Menu_AZ"), ("ru", "Menu_RU")]:
            try:
                lang_sheet = spreadsheet.worksheet(sheet_name)
                lang_records = lang_sheet.get_all_records()
                for row in lang_records:
                    new_display[lang][str(row["id"])] = row["display_name"]
            except Exception as e:
                print(f"⚠️ [MENU] Could not load {sheet_name}: {e}")

        # English uses base name as display name
        new_display["en"] = {k: v for k, v in new_items_map.items()}

        PRICES = new_prices
        ITEMS_MAP = new_items_map
        DISPLAY_NAMES = new_display

        print(f"✅ [MENU] Loaded {len(PRICES)} items from Sheets.")
    except Exception as e:
        print(f"❌ [MENU] Failed to load menu: {e}")

def build_menu_text(lang: str, table: str) -> str:
    """Build the menu message string dynamically from loaded data."""
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

# ─── Load menu on startup ─────────────────────────────────────────────────────
@app.on_event("startup")
async def startup_event():
    load_menu()

# ─── Google Sheets: Write Order ───────────────────────────────────────────────
def write_to_google_sheets(phone, name, item_or_request, table_num, lang, order_id):
    def _attempt(client):
        sheet = client.open("Cafe_Orders_DB").sheet1
        current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        sheet.append_row([phone, name, item_or_request, table_num, lang, current_time, order_id, "Preparing"])
        print(f"📊 [GSHEET SUCCESS] Order #{order_id} logged cleanly.")

    try:
        _attempt(get_google_client())
    except gspread.exceptions.APIError as e:
        if e.response.status_code == 401:
            print("🔄 [AUTH] Token expired, refreshing client...")
            try:
                _attempt(get_google_client(force_refresh=True))
            except Exception as retry_error:
                print(f"❌ [GSHEET FATAL] Retry failed: {retry_error}")
        else:
            print(f"⚠️ [GSHEET ERROR] Cloud sync skipped: {e}")
    except Exception as e:
        print(f"⚠️ [GSHEET ERROR] Cloud sync skipped: {e}")

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
        return _attempt(get_google_client())
    except gspread.exceptions.APIError as e:
        if e.response.status_code == 401:
            print("🔄 [AUTH] Token expired, refreshing client...")
            try:
                return _attempt(get_google_client(force_refresh=True))
            except Exception as retry_error:
                print(f"❌ [STATUS FATAL] Retry failed: {retry_error}")
                return {"found": False}
        else:
            print(f"⚠️ [STATUS FETCH ERROR]: {e}")
            return {"found": False}
    except Exception as e:
        print(f"⚠️ [STATUS FETCH ERROR]: {e}")
        return {"found": False}


# ─── Reload menu endpoint (call this after updating the sheet) ────────────────
@app.post("/reload-menu")
async def reload_menu(request: Request):
    api_key = request.headers.get("X-Admin-Key", "")
    if api_key != os.environ.get("ADMIN_KEY", ""):
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

    # 📍 TABLE ASSIGNMENT
    if "table" in user_text or "masa" in user_text or "стол" in user_text:
        table_digits = "".join([char for char in user_text if char.isdigit()])
        if table_digits:
            session["table"] = table_digits
            msg = LEXICON[session["lang"]]["welcome"].format(table=session["table"])
            response.message(msg)
            save_session(From, session)
            return Response(content=str(response), media_type="application/xml")

    # 🌍 LANGUAGE SWITCH
    if user_text in ["az", "ru", "en"]:
        session["lang"] = user_text
        msg = LEXICON[user_text]["welcome"].format(table=session["table"])
        response.message(msg)
        save_session(From, session)
        return Response(content=str(response), media_type="application/xml")

    lang = session["lang"]
    table = session["table"]

    # 🚨 CALL WAITER
    if user_text in ["waiter", "ofisiant", "официант"]:
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
                    (DISPLAY_NAMES[lang].get(k) for k, v in ITEMS_MAP.items() if v == item),
                    item
                )
                summary_lines.append(f"• {qty}x {display} ({item_cost:.2f} AZN)")
            basket_summary = "\n".join(summary_lines)
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
                (DISPLAY_NAMES[lang].get(k) for k, v in ITEMS_MAP.items() if v == item),
                item
            )
            summary_lines.append(f"• {qty}x {display} ({item_cost:.2f} AZN)")
            db_items.append(f"{qty}x {item}")
        final_order_string = ", ".join(db_items)
        basket_summary = "\n".join(summary_lines)
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