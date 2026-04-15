from flask import Flask, request
from twilio.rest import Client
import anthropic
import base64
import requests
import os
import json
from datetime import datetime, timedelta
from apscheduler.schedulers.background import BackgroundScheduler
from fpdf import FPDF
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
import threading
import psycopg2
from psycopg2.extras import RealDictCursor

app = Flask(__name__)

ANTHROPIC_KEY = os.environ.get("ANTHROPIC_KEY")
TWILIO_SID = os.environ.get("TWILIO_SID")
TWILIO_TOKEN = os.environ.get("TWILIO_TOKEN")
MY_WHATSAPP = os.environ.get("MY_WHATSAPP", "whatsapp:+61449984648")
GMAIL_USER = os.environ.get("GMAIL_USER")
GMAIL_PASS = os.environ.get("GMAIL_PASS")
DATABASE_URL = os.environ.get("DATABASE_URL")

twilio_client = Client(TWILIO_SID, TWILIO_TOKEN)
claude = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

invoice_counter = 5719
best_day_record = 0
pending_invoice = None

def get_db():
    return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)

def init_db():
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id SERIAL PRIMARY KEY,
                role VARCHAR(20),
                content TEXT,
                created_at TIMESTAMP DEFAULT NOW()
            );
            CREATE TABLE IF NOT EXISTS expenses (
                id SERIAL PRIMARY KEY,
                amount FLOAT,
                vendor VARCHAR(200),
                category VARCHAR(100),
                description TEXT,
                date VARCHAR(50),
                created_at TIMESTAMP DEFAULT NOW()
            );
            CREATE TABLE IF NOT EXISTS income (
                id SERIAL PRIMARY KEY,
                amount FLOAT,
                parts FLOAT,
                net FLOAT,
                description TEXT,
                date VARCHAR(50),
                created_at TIMESTAMP DEFAULT NOW()
            );
            CREATE TABLE IF NOT EXISTS invoices (
                id SERIAL PRIMARY KEY,
                number INTEGER,
                client_name VARCHAR(200),
                client_email VARCHAR(200),
                client_address TEXT,
                items JSONB,
                total FLOAT,
                date VARCHAR(50),
                paid BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT NOW()
            );
            CREATE TABLE IF NOT EXISTS events (
                id SERIAL PRIMARY KEY,
                title TEXT,
                event_date VARCHAR(100),
                reminder_sent BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT NOW()
            );
            CREATE TABLE IF NOT EXISTS settings (
                key VARCHAR(100) PRIMARY KEY,
                value TEXT
            );
        """)
        conn.commit()
        cur.close()
        conn.close()
        print("Database ready!")
    except Exception as e:
        print(f"DB init error: {e}")

def save_message(role, content):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("INSERT INTO messages (role, content) VALUES (%s, %s)", (role, content))
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print(f"Save message error: {e}")

def get_history(limit=30):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT role, content FROM messages ORDER BY created_at DESC LIMIT %s", (limit,))
        rows = cur.fetchall()
        cur.close()
        conn.close()
        history = [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]
        return history
    except Exception as e:
        print(f"Get history error: {e}")
        return []

def get_setting(key, default=None):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT value FROM settings WHERE key = %s", (key,))
        row = cur.fetchone()
        cur.close()
        conn.close()
        return row["value"] if row else default
    except:
        return default

def save_setting(key, value):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("INSERT INTO settings (key, value) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET value = %s", (key, str(value), str(value)))
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print(f"Save setting error: {e}")

def send_whatsapp(msg):
    try:
        to = MY_WHATSAPP
        if not to.startswith("whatsapp:"):
            to = "whatsapp:" + to
        twilio_client.messages.create(
            from_="whatsapp:+14155238886",
            to=to,
            body=msg
        )
        print(f"Sent: {msg[:50]}")
    except Exception as e:
        print(f"Send error: {e}")

def scan_receipt(image_url):
    try:
        img_data = requests.get(image_url, auth=(TWILIO_SID, TWILIO_TOKEN)).content
        b64 = base64.b64encode(img_data).decode("utf-8")
        response = claude.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=500,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}},
                    {"type": "text", "text": "Scan this receipt. Extract: 1) total amount (number only), 2) vendor/store name, 3) date (YYYY-MM-DD), 4) what was purchased, 5) category from: fuel/tools/supplies/vehicle/phone/food/google_ads/other. Reply ONLY in JSON: {\"amount\":\"\",\"vendor\":\"\",\"date\":\"\",\"description\":\"\",\"category\":\"\"}"}
                ]
            }]
        )
        text = response.content[0].text.replace("```json", "").replace("```", "").strip()
        return json.loads(text)
    except Exception as e:
        print(f"Scan error: {e}")
        return {"amount": "0", "vendor": "Unknown", "date": "", "description": "", "category": "other"}

def search_web(query):
    try:
        response = claude.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1000,
            tools=[{"type": "web_search_20250305", "name": "web_search"}],
            messages=[{"role": "user", "content": f"Search for: {query}. Give a brief helpful summary."}]
        )
        for block in response.content:
            if hasattr(block, "text"):
                return block.text
        return "Could not find results"
    except Exception as e:
        print(f"Search error: {e}")
        return "Search unavailable right now"

def create_invoice_pdf(inv):
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Arial", "B", 20)
    pdf.cell(0, 10, "AMPM Services", ln=True, align="R")
    pdf.set_font("Arial", "", 10)
    pdf.cell(0, 6, "NZBN: 9429053281210", ln=True, align="R")
    pdf.cell(0, 6, "+64226068460", ln=True, align="R")
    pdf.cell(0, 6, "ampmservices2024@gmail.com", ln=True, align="R")
    pdf.ln(5)
    pdf.set_font("Arial", "B", 14)
    pdf.cell(0, 8, f"Invoice #{inv['number']}", ln=True)
    pdf.set_font("Arial", "", 10)
    pdf.cell(0, 6, f"Date: {inv['date']}", ln=True)
    pdf.ln(5)
    pdf.set_font("Arial", "B", 10)
    pdf.cell(0, 8, "BILL TO:", ln=True)
    pdf.set_font("Arial", "", 10)
    pdf.cell(0, 6, inv.get("client_name", ""), ln=True)
    pdf.cell(0, 6, inv.get("client_address", ""), ln=True)
    pdf.cell(0, 6, inv.get("client_email", ""), ln=True)
    pdf.ln(5)
    pdf.set_font("Arial", "B", 10)
    pdf.cell(130, 8, "Item", border=1)
    pdf.cell(60, 8, "Amount", border=1, align="R")
    pdf.ln()
    pdf.set_font("Arial", "", 10)
    total = 0
    for item in inv.get("items", []):
        pdf.cell(130, 8, item["description"], border=1)
        pdf.cell(60, 8, f"NZ$ {item['amount']:.2f}", border=1, align="R")
        pdf.ln()
        total += item["amount"]
    pdf.ln(3)
    pdf.set_font("Arial", "B", 12)
    pdf.cell(0, 8, f"Total: NZ$ {total:.2f}", ln=True, align="R")
    if not inv.get("paid"):
        pdf.ln(8)
        pdf.set_font("Arial", "B", 10)
        pdf.cell(0, 8, "Payment Details:", ln=True)
        pdf.set_font("Arial", "", 10)
        pdf.cell(0, 6, "Name: AMPM Services", ln=True)
        pdf.cell(0, 6, "Account: 03-0255-0294020-000", ln=True)
        pdf.cell(0, 6, f"Reference: Invoice #{inv['number']}", ln=True)
    path = f"/tmp/invoice_{inv['number']}.pdf"
    pdf.output(path)
    return path

def send_email(to, subject, body, attachment_path=None):
    try:
        msg = MIMEMultipart()
        msg["From"] = f"AMPM Services <{GMAIL_USER}>"
        msg["To"] = to
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "html"))
        if attachment_path:
            with open(attachment_path, "rb") as f:
                part = MIMEBase("application", "octet-stream")
                part.set_payload(f.read())
                encoders.encode_base64(part)
                part.add_header("Content-Disposition", f"attachment; filename={os.path.basename(attachment_path)}")
                msg.attach(part)
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(GMAIL_USER, GMAIL_PASS)
            server.sendmail(GMAIL_USER, to, msg.as_string())
        return True
    except Exception as e:
        print(f"Email error: {e}")
        return False

def set_reminder(minutes, message):
    def remind():
        send_whatsapp(f"⏰ Reminder: {message}")
    timer = threading.Timer(minutes * 60, remind)
    timer.start()

def get_state_summary():
    try:
        conn = get_db()
        cur = conn.cursor()
        today = datetime.now().strftime("%Y-%m-%d")
        cur.execute("SELECT COALESCE(SUM(net), 0) as total FROM income WHERE date LIKE %s", (f"{today}%",))
        today_income = float(cur.fetchone()["total"])
        cur.execute("SELECT COALESCE(SUM(amount), 0) as total FROM expenses WHERE date LIKE %s", (f"{today}%",))
        today_expenses = float(cur.fetchone()["total"])
        cur.execute("SELECT COUNT(*) as count, COALESCE(SUM(total), 0) as total FROM invoices WHERE paid = FALSE")
        inv_row = cur.fetchone()
        cur.execute("SELECT number, client_name, total FROM invoices WHERE paid = FALSE ORDER BY created_at DESC LIMIT 3")
        unpaid = [dict(r) for r in cur.fetchall()]
        cur.close()
        conn.close()
        best = float(get_setting("best_day_record", "0"))
        return {
            "today_income": today_income,
            "today_expenses": today_expenses,
            "today_profit": today_income - today_expenses,
            "best_day_record": best,
            "unpaid_invoices_count": inv_row["count"],
            "unpaid_invoices_total": float(inv_row["total"]),
            "unpaid_invoices": unpaid,
            "pending_invoice": pending_invoice,
            "next_invoice_number": int(get_setting("invoice_counter", "5719")),
            "current_datetime": datetime.now().strftime("%Y-%m-%d %H:%M"),
        }
    except Exception as e:
        print(f"State error: {e}")
        return {"error": str(e)}

def ask_melisa(user_message):
    global pending_invoice
    state = get_state_summary()
    history = get_history(30)
    system_prompt = f"""You are Melisa 😊, a smart personal WhatsApp assistant for Tomer who runs AMPM Services (locksmith business in NZ/Australia).

Current date/time: {datetime.now().strftime("%Y-%m-%d %H:%M")}
Business state: {json.dumps(state, indent=2)}

You are Tomer's FULL personal assistant:
- Business: invoices, expenses, jobs, reports, reminders about unpaid invoices
- Personal: reminders, to-do lists, events, scheduling
- Travel: find flights, compare prices (use web search)
- General: answer questions, draft messages, help with decisions
- Memory: you remember everything from past conversations

BUSINESS DETAILS:
- Business: AMPM Services (locksmith)
- NZBN: 9429053281210
- Phone: +64226068460
- Email: ampmservices2024@gmail.com
- Bank: AMPM Services, 03-0255-0294020-000
- No GST yet (will add 15% NZ GST later)
- Invoice counter: {state.get('next_invoice_number', 5719)}

Pending invoice: {json.dumps(pending_invoice) if pending_invoice else 'None'}

RESPOND with JSON:
{{
  "message": "your friendly reply to Tomer",
  "action": "none|save_expense|save_job|create_invoice|confirm_invoice|update_invoice|set_reminder|show_report|show_weekly|show_outstanding|mark_paid|search_web|save_event",
  "data": {{}},
  "search_query": "only if action is search_web"
}}

ACTION DATA FORMATS:
- set_reminder: {{"minutes": 20, "reminder_message": "text"}}
- create_invoice: {{"client_name": "", "client_email": "", "client_address": "", "items": [{{"description": "", "amount": 0}}]}}
- update_invoice: {{"field": "value"}} or {{"items": [...]}}
- save_job: {{"amount": 0, "parts": 0, "description": ""}}
- save_expense: {{"amount": 0, "vendor": "", "category": "", "description": ""}}
- mark_paid: {{"invoice_number": 5719}}
- save_event: {{"title": "", "event_date": "", "remind_before_minutes": 60}}
- search_web: search_query field with what to search

Be friendly, natural, encouraging. Use emojis. Keep replies SHORT.
Remember Tomer's preferences and past conversations.
For business motivation mention best day record NZ$ {state.get('best_day_record', 0):.2f}.
If unclear, ask ONE simple question."""

    save_message("user", user_message)
    history.append({"role": "user", "content": user_message})
    response = claude.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1000,
        system=system_prompt,
        messages=history
    )
    reply_text = response.content[0].text.replace("```json", "").replace("```", "").strip()
    try:
        reply = json.loads(reply_text)
    except:
        reply = {"message": reply_text, "action": "none", "data": {}}
    save_message("assistant", reply_text)
    return reply

def handle_action(action, data, search_query=None):
    global pending_invoice
    try:
        conn = get_db()
        cur = conn.cursor()
        if action == "save_expense":
            cur.execute(
                "INSERT INTO expenses (amount, vendor, category, description, date) VALUES (%s, %s, %s, %s, %s)",
                (float(data.get("amount", 0)), data.get("vendor", ""), data.get("category", "other"), data.get("description", ""), datetime.now().strftime("%Y-%m-%d"))
            )
            conn.commit()
        elif action == "save_job":
            amount = float(data.get("amount", 0))
            parts = float(data.get("parts", 0))
            net = amount - parts
            today = datetime.now().strftime("%Y-%m-%d")
            cur.execute(
                "INSERT INTO income (amount, parts, net, description, date) VALUES (%s, %s, %s, %s, %s)",
                (amount, parts, net, data.get("description", ""), today)
            )
            cur.execute("SELECT COALESCE(SUM(net), 0) as total FROM income WHERE date = %s", (today,))
            today_total = float(cur.fetchone()["total"])
            best = float(get_setting("best_day_record", "0"))
            if today_total > best:
                save_setting("best_day_record", today_total)
            conn.commit()
        elif action == "create_invoice":
            inv_num = int(get_setting("invoice_counter", "5719"))
            pending_invoice = {
                "number": inv_num,
                "client_name": data.get("client_name", ""),
                "client_email": data.get("client_email", ""),
                "client_address": data.get("client_address", ""),
                "items": data.get("items", []),
                "total": sum(i["amount"] for i in data.get("items", [])),
                "date": datetime.now().strftime("%d %b %Y"),
                "paid": False
            }
        elif action == "update_invoice" and pending_invoice:
            for key, value in data.items():
                if key == "items":
                    pending_invoice["items"] = value
                    pending_invoice["total"] = sum(i["amount"] for i in value)
                else:
                    pending_invoice[key] = value
        elif action == "confirm_invoice" and pending_invoice:
            pdf_path = create_invoice_pdf(pending_invoice)
            email_body = f"""<h2>Invoice #{pending_invoice['number']}</h2>
            <p>Dear {pending_invoice['client_name']},</p>
            <p>Please find your invoice attached.</p>
            <p>Payment details:<br>Name: AMPM Services<br>Account: 03-0255-0294020-000<br>Reference: Invoice #{pending_invoice['number']}</p>
            <p>Reply PAID to confirm payment.</p>
            <p>Thank you!<br>AMPM Services<br>+64226068460</p>"""
            send_email(pending_invoice["client_email"], f"Invoice #{pending_invoice['number']} - AMPM Services", email_body, pdf_path)
            cur.execute(
                "INSERT INTO invoices (number, client_name, client_email, client_address, items, total, date, paid) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                (pending_invoice["number"], pending_invoice["client_name"], pending_invoice["client_email"],
                 pending_invoice["client_address"], json.dumps(pending_invoice["items"]),
                 pending_invoice["total"], pending_invoice["date"], False)
            )
            new_num = pending_invoice["number"] + 1
            save_setting("invoice_counter", new_num)
            conn.commit()
            pending_invoice = None
        elif action == "mark_paid":
            inv_num = data.get("invoice_number")
            cur.execute("UPDATE invoices SET paid = TRUE WHERE number = %s", (inv_num,))
            conn.commit()
        elif action == "set_reminder":
            minutes = data.get("minutes", 20)
            reminder_msg = data.get("reminder_message", "Check in!")
            set_reminder(minutes, reminder_msg)
        elif action == "save_event":
            cur.execute(
                "INSERT INTO events (title, event_date) VALUES (%s, %s)",
                (data.get("title", ""), data.get("event_date", ""))
            )
            conn.commit()
        elif action == "show_report":
            today = datetime.now().strftime("%Y-%m-%d")
            cur.execute("SELECT COALESCE(SUM(net), 0) as total FROM income WHERE date LIKE %s", (f"{today}%",))
            t_income = float(cur.fetchone()["total"])
            cur.execute("SELECT COALESCE(SUM(amount), 0) as total FROM expenses WHERE date LIKE %s", (f"{today}%",))
            t_expense = float(cur.fetchone()["total"])
            cur.close()
            conn.close()
            return f"📊 Daily Report — {today}\n\n💰 Income: NZ$ {t_income:.2f}\n🧾 Expenses: NZ$ {t_expense:.2f}\n📈 Profit: NZ$ {(t_income-t_expense):.2f}"
        elif action == "show_weekly":
            week_ago = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
            cur.execute("SELECT COALESCE(SUM(net), 0) as total FROM income WHERE date >= %s", (week_ago,))
            w_income = float(cur.fetchone()["total"])
            cur.execute("SELECT category, SUM(amount) as total FROM expenses WHERE date >= %s GROUP BY category", (week_ago,))
            cats = cur.fetchall()
            cur.execute("SELECT COALESCE(SUM(amount), 0) as total FROM expenses WHERE date >= %s", (week_ago,))
            w_expense = float(cur.fetchone()["total"])
            lines = ["📆 Weekly Report\n", f"💰 Income: NZ$ {w_income:.2f}\n🧾 Expenses by category:"]
            for cat in cats:
                lines.append(f"  • {cat['category'].title()}: NZ$ {cat['total']:.2f}")
            lines.append(f"💸 Total: NZ$ {w_expense:.2f}")
            lines.append(f"📈 Profit: NZ$ {(w_income-w_expense):.2f}")
            cur.close()
            conn.close()
            return "\n".join(lines)
        elif action == "show_outstanding":
            cur.execute("SELECT number, client_name, total FROM invoices WHERE paid = FALSE")
            unpaid = cur.fetchall()
            cur.close()
            conn.close()
            if not unpaid:
                return "✅ No outstanding invoices!"
            lines = ["💰 Outstanding Invoices\n"]
            for inv in unpaid:
                lines.append(f"#{inv['number']} — {inv['client_name']} — NZ$ {inv['total']:.2f}")
            return "\n".join(lines)
        elif action == "search_web" and search_query:
            cur.close()
            conn.close()
            return search_web(search_query)
        cur.close()
        conn.close()
    except Exception as e:
        print(f"Action error: {e}")
    return None

@app.route("/webhook", methods=["POST"])
def webhook():
    global pending_invoice
    msg = request.form.get("Body", "").strip()
    media_url = request.form.get("MediaUrl0")
    num_media = int(request.form.get("NumMedia", 0))
    print(f"Received: {msg}")
    if num_media > 0 and media_url:
        send_whatsapp("📸 Scanning your receipt... one moment!")
        data = scan_receipt(media_url)
        amount = float(data.get("amount") or 0)
        vendor = data.get("vendor", "Unknown")
        date = data.get("date") or datetime.now().strftime("%Y-%m-%d")
        description = data.get("description") or msg
        category = data.get("category", "other")
        try:
            conn = get_db()
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO expenses (amount, vendor, category, description, date) VALUES (%s, %s, %s, %s, %s)",
                (amount, vendor, category, description, date)
            )
            conn.commit()
            cur.close()
            conn.close()
        except Exception as e:
            print(f"DB error: {e}")
        send_whatsapp(f"✅ Expense saved!\n🏪 {vendor}\n💸 NZ$ {amount:.2f}\n🏷️ {category.title()}\n📅 {date}\n📝 {description}\n\nIs this correct? Reply YES or correct me!")
        return "OK", 200
    try:
        reply = ask_melisa(msg)
        action = reply.get("action", "none")
        data = reply.get("data", {})
        message = reply.get("message", "")
        search_query = reply.get("search_query", "")
        extra = handle_action(action, data, search_query)
        if action == "create_invoice" and pending_invoice:
            preview = f"📄 Invoice Preview #{pending_invoice['number']}\n\n"
            preview += f"👤 {pending_invoice['client_name']}\n"
            preview += f"📧 {pending_invoice['client_email']}\n"
            preview += f"📍 {pending_invoice['client_address']}\n\n"
            for item in pending_invoice["items"]:
                preview += f"  • {item['description']}: NZ$ {item['amount']:.2f}\n"
            preview += f"\n💰 Total: NZ$ {pending_invoice['total']:.2f}\n\n"
            preview += "Say CONFIRM to send or tell me what to change!"
            send_whatsapp(message)
            send_whatsapp(preview)
        elif extra and isinstance(extra, str):
            send_whatsapp(message)
            send_whatsapp(extra)
        else:
            send_whatsapp(message)
    except Exception as e:
        print(f"Webhook error: {e}")
        send_whatsapp("Sorry, I had a small issue! Try again 😊")
    return "OK", 200

def send_daily_report():
    try:
        conn = get_db()
        cur = conn.cursor()
        today = datetime.now().strftime("%Y-%m-%d")
        cur.execute("SELECT COALESCE(SUM(net), 0) as total FROM income WHERE date LIKE %s", (f"{today}%",))
        t_income = float(cur.fetchone()["total"])
        cur.execute("SELECT COALESCE(SUM(amount), 0) as total FROM expenses WHERE date LIKE %s", (f"{today}%",))
        t_expense = float(cur.fetchone()["total"])
        cur.close()
        conn.close()
        send_whatsapp(f"📊 Good evening Tomer! Daily Report — {today}\n\n💰 Income: NZ$ {t_income:.2f}\n🧾 Expenses: NZ$ {t_expense:.2f}\n📈 Profit: NZ$ {(t_income-t_expense):.2f}")
    except Exception as e:
        print(f"Daily report error: {e}")

def check_unpaid():
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT number, client_name, total FROM invoices WHERE paid = FALSE")
        unpaid = cur.fetchall()
        cur.close()
        conn.close()
        for inv in unpaid:
            send_whatsapp(f"⏰ Reminder: Invoice #{inv['number']} for {inv['client_name']} — NZ$ {inv['total']:.2f} still unpaid!")
    except Exception as e:
        print(f"Unpaid check error: {e}")

scheduler = BackgroundScheduler()
scheduler.add_job(send_daily_report, "cron", hour=20, minute=0)
scheduler.add_job(check_unpaid, "interval", hours=48)
scheduler.start()

init_db()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
