from fastapi import FastAPI, Form, Response
from twilio.twiml.messaging_response import MessagingResponse
from datetime import datetime
from collections import Counter
import gspread
from google.oauth2.service_account import Credentials
import uuid
import redis
import json
import os

app = FastAPI(title="Viento Cafe Pro - Premium Automated Waiter")

# Redis session store
redis_client = redis.from_url(
    os.environ.get("REDIS_URL", "redis://localhost:6379"),
    decode_responses=True
)
SESSION_TTL = 60 * 60 * 6  # 6 hours

def get_session(phone: str) -> dict:
    data = redis_client.get(f"session:{phone}")
    if data:
        return json.loads(data)
    return {"lang": "az", "state": "IDLE", "basket": [], "table": "0 (Takeaway)"}

def save_session(phone: str, session: dict):
    redis_client.setex(f"session:{phone}", SESSION_TTL, json.dumps(session))

# Cached at module level — created once on startup
_sheets_client = None

def get_google_client():
    global _sheets_client
    if _sheets_client is not None:
        return _sheets_client
    creds_json = json.loads(os.environ.get("GOOGLE_CREDS"))
    scopes = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds = Credentials.from_service_account_info(creds_json, scopes=scopes)
    _sheets_client = gspread.authorize(creds)
    return _sheets_client

# 💵 Centralized Price Database (AZN)
PRICES = {
    "Flat White": 6.00,
    "Iced Spanish Latte": 7.50,
    "San Sebastian Paxlava": 9.00,
    "Croissant": 5.00
}

# 📊 Google Sheets Connection
def write_to_google_sheets(phone, name, item_or_request, table_num, lang, order_id):
    try:
        client = get_google_client()
        sheet = client.open("Cafe_Orders_DB").sheet1
        
        current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        sheet.append_row([phone, name, item_or_request, table_num, lang, current_time, order_id, "Preparing"])
        print(f"📊 [GSHEET SUCCESS] Order #{order_id} logged cleanly.")
    except Exception as e:
        print(f"⚠️ [GSHEET ERROR] Cloud sync skipped: {e}")

# 🔍 Read Order Status live from the spreadsheet
def fetch_order_status_from_sheets(phone):
    try:
        client = get_google_client()
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
    except Exception as e:
        print(f"⚠️ [STATUS FETCH ERROR]: {e}")
        return {"found": False}


# 🌍 Multilingual Lexicon Upgraded with Receipt & Pricing Layouts
LEXICON = {
    "az": {
        "welcome": "👋 *Viento Cafe*-a xoş gəlmisiniz!\n📍 Masanız: *Masa {table}*\n\n👉 Menyuya baxmaq və sifariş üçün: *MENYU*\n👉 Ofisiantı çağırmaq: *OFİSİANT*\n👉 Sifariş statusu: *STATUS*",
        "menu": "☕ *Masa {table} üçün Menyu* ☕\n\n1️⃣ Flat White - 6.00 AZN\n2️⃣ Iced Spanish Latte - 7.50 AZN\n3️⃣ San Sebastian Paxlava - 9.00 AZN\n4️⃣ Kruassan - 5.00 AZN\n\n👉 Sifariş etmək istədiyiniz məhsulun *nömrəsini* yazın!",
        "added_item": "➕ *{item}* səbətinizə əlavə olundu!\n\n👉 Başqa bir şey istəyirsinizsə, yeni məhsul *nömrəsini* yazın.\n👉 Sifarişi tamamlamaq üçün: *TƏSDİQ*\n👉 Səbəti təmizləmək üçün: *SİL*",
        "empty_basket": "Səbətiniz boşdur. Sifariş üçün əvvəlcə *MENYU* yazın.",
        "ask_name": "🛍️ *Səbətiniz:*\n{basket_details}\n\n💵 *Cəm məbləğ:* {total_price:.2f} AZN\n\nSifarişi mətbəxə göndərmək üçün **adınızı** qeyd edin:",
        "confirmed": "✅ Çox sağ olun, {name}!\n\nSifarişiniz mətbəxə ötürüldü!\n🆔 Sifariş nömrəniz: *#{order_id}*\n\n{basket_details}\n💰 *Ödəniləcək məbləğ:* {total_price:.2f} AZN\n\n⏱️ Sifarişinizin vəziyyətini öyrənmək üçün istənilən vaxt *STATUS* yaza bilərsiniz.",
        "status_success": "📋 *Sifarişinizin Vəziyyəti:*\n🆔 Sifariş: *#{order_id}*\n🚦 Status: *{status}*",
        "status_not_found": "🔍 Aktiv sifariş tapılmadı. Sifariş vermək üçün *MENYU* yazın.",
        "waiter_alerted": "🔔 *Ofisiant çağırıldı!* Masanıza yaxınlaşırlar.",
        "fallback": "Zəhmət olmasa menyu üçün *MENYU*, status üçün *STATUS* yazın."
    },
    "ru": {
        "welcome": "👋 Добро пожаловать в *Viento Cafe*!\n📍 Ваш столик: *Стол {table}*\n\n👉 Меню и заказ: *МЕНЮ*\n👉 Вызвать официанта: *ОФИЦИАНТ*\n👉 Статус заказа: *СТАТУС*",
        "menu": "☕ *Меню для Столика {table}* ☕\n\n1️⃣ Флэт Уайт - 6.00 AZN\n2️⃣ Айс Испанский Латте - 7.50 AZN\n3️⃣ Сан-Себастьян Пахлава - 9.00 AZN\n4️⃣ Круассан - 5.00 AZN\n\n👉 Введите *номер* товара для добавления в корзину!",
        "added_item": "➕ *{item}* добавлен в вашу корзину!\n\n👉 Чтобы добавить ещё что-то, введите другой *номер*.\n👉 Для оформления заказа напишите: *ПОДТВЕРДИТЬ*\n👉 Чтобы очистить корзину: *ОЧИСТИТЬ*",
        "empty_basket": "Ваша корзина пуста. Напишите *МЕНЮ*, чтобы выбрать товары.",
        "ask_name": "🛍️ *Ваша корзина:*\n{basket_details}\n\n💵 *Итого к оплате:* {total_price:.2f} AZN\n\nНа какое **имя** записать заказ для отправки на кухню?",
        "confirmed": "✅ Спасибо, {name}!\n\nВаш заказ передан на кухню!\n🆔 Номер вашего заказа: *#{order_id}*\n\n{basket_details}\n💰 *Сумма к оплате:* {total_price:.2f} AZN\n\n⏱️ Вы можете проверить его готовность в любой момент, написав слово *СТАТУС*.",
        "status_success": "📋 *Статус вашего заказа:*\n🆔 Номер: *#{order_id}*\n🚦 Состояние: *{status}*",
        "status_not_found": "🔍 Активных заказов не найдено. Напишите *МЕНЮ*, чтобы сделать заказ.",
        "waiter_alerted": "🔔 *Официант вызван!* Он скоро подойдет к вашему столику.",
        "fallback": "Пожалуйста, напишите *МЕНЮ* для заказа или *СТАТУС* для проверки готовности."
    },
    "en": {
        "welcome": "👋 Welcome to *Viento Cafe*!\n📍 Your location: *Table {table}*\n\n👉 View menu & order: *MENU*\n👉 Call a waiter: *WAITER*\n👉 Track order status: *STATUS*",
        "menu": "☕ *Menu for Table {table}* ☕\n\n1️⃣ Flat White - 6.00 AZN\n2️⃣ Iced Spanish Latte - 7.50 AZN\n3️⃣ San Sebastian Baklava - 9.00 AZN\n4️⃣ Croissant - 5.00 AZN\n\n👉 Reply with the item *number* to add it to your cart!",
        "added_item": "➕ *{item}* added to your basket!\n\n👉 To add more, reply with another item *number*.\n👉 To complete your order, reply: *CHECKOUT*\n👉 To clear your basket, reply: *CLEAR*",
        "empty_basket": "Your basket is empty. Type *MENU* to browse items first.",
        "ask_name": "🛍️ *Your Basket:*\n{basket_details}\n\n💵 *Total Price:* {total_price:.2f} AZN\n\nPlease type your **name** to send this order to the kitchen:",
        "confirmed": "✅ Thank you, {name}!\n\nYour order has been sent to the kitchen!\n🆔 Your Order ID: *#{order_id}*\n\n{basket_details}\n💰 *Total Amount due:* {total_price:.2f} AZN\n\n⏱️ You can track your order at any time by replying with the word *STATUS*.",
        "status_success": "📋 *Your Order Tracking:*\n🆔 Order ID: *#{order_id}*\n🚦 Current Status: *{status}*",
        "status_not_found": "🔍 No active orders found for your number. Type *MENU* to start an order.",
        "waiter_alerted": "🔔 *Waiter alerted!* Someone from our staff is coming to your table shortly.",
        "fallback": "Please type *MENU* to see items or *STATUS* to check your order details."
    }
}

@app.post("/whatsapp")
async def incoming_whatsapp(Body: str = Form(...), From: str = Form(...)):
    user_text = Body.lower().strip()
    response = MessagingResponse()
    session = get_session(From)

    # 📍 PARSE TABLE ASSIGNMENT
    if "table" in user_text or "masa" in user_text or "стол" in user_text:
        table_digits = "".join([char for char in user_text if char.isdigit()])
        if table_digits:
            session["table"] = table_digits
            msg = LEXICON[session["lang"]]["welcome"].format(table=session["table"])
            response.message(msg)
            save_session(From, session)
            return Response(content=str(response), media_type="application/xml")

    # 🌍 GLOBAL LANGUAGE SWITCHES
    if user_text in ["az", "ru", "en"]:
        session["lang"] = user_text
        msg = LEXICON[user_text]["welcome"].format(table=session["table"])
        response.message(msg)
        save_session(From, session)
        return Response(content=str(response), media_type="application/xml")

    lang = session["lang"]
    table = session["table"]

    # 🚨 IMMEDIATE OVERRIDE: CALL THE WAITER
    if user_text in ["waiter", "ofisiant", "официант"]:
        write_to_google_sheets(From, "Valued Customer", "🚨 NEEDS PHYSICAL WAITER", table, lang.upper(), "N/A")
        response.message(LEXICON[lang]["waiter_alerted"])
        save_session(From, session)
        return Response(content=str(response), media_type="application/xml")

    # 🔍 GLOBAL OVERRIDE: TRACK ORDER STATUS
    if user_text in ["status", "состояние", "vəziyyət"]:
        result = fetch_order_status_from_sheets(From)
        if result["found"]:
            response.message(LEXICON[lang]["status_success"].format(order_id=result["order_id"], status=result["status"]))
        else:
            response.message(LEXICON[lang]["status_not_found"])
        save_session(From, session)
        return Response(content=str(response), media_type="application/xml")

    # 🗑️ BASKET MANAGEMENT: CLEAR CART
    if user_text in ["sil", "clear", "очистить"]:
        session["basket"] = []
        session["state"] = "IDLE"
        response.message(LEXICON[lang]["empty_basket"])
        save_session(From, session)
        return Response(content=str(response), media_type="application/xml")

    # 🛒 BASKET MANAGEMENT: CHECKOUT
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
                summary_lines.append(f"• {qty}x {item} ({item_cost:.2f} AZN)")
            basket_summary = "\n".join(summary_lines)
            response.message(LEXICON[lang]["ask_name"].format(basket_details=basket_summary, total_price=grand_total))
        save_session(From, session)
        return Response(content=str(response), media_type="application/xml")

    # 🔄 STATE CHECK: WAITING FOR BASKET ADDITIONS
    if session["state"] == "WAITING_FOR_ITEM":
        items_map = {"1": "Flat White", "2": "Iced Spanish Latte", "3": "San Sebastian Paxlava", "4": "Croissant"}
        if user_text in items_map:
            chosen_item = items_map[user_text]
            session["basket"].append(chosen_item)
            response.message(LEXICON[lang]["added_item"].format(item=chosen_item))
        else:
            if user_text not in ["menu", "menyu", "меню"]:
                response.message(LEXICON[lang]["fallback"])
        save_session(From, session)
        return Response(content=str(response), media_type="application/xml")

    # 📝 STATE CHECK: WAITING FOR CUSTOMER NAME
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
            summary_lines.append(f"• {qty}x {item} ({item_cost:.2f} AZN)")
            db_items.append(f"{qty}x {item}")
        final_order_string = ", ".join(db_items)
        basket_summary = "\n".join(summary_lines)
        write_to_google_sheets(From, customer_name, final_order_string, table, lang.upper(), generated_id)
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

    # 🏁 CORE IDLE ROUTING
    if user_text in ["menu", "menyu", "меню"]:
        response.message(LEXICON[lang]["menu"].format(table=table))
        session["state"] = "WAITING_FOR_ITEM"
    else:
        response.message(LEXICON[lang]["welcome"].format(table=table))

    save_session(From, session)
    return Response(content=str(response), media_type="application/xml")