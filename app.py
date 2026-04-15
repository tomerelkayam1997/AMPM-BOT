from flask import Flask, request
import anthropic
import base64
import requests
import os
import json
from datetime import datetime, timedelta
from apscheduler.schedulers.background import BackgroundScheduler
from twilio.rest import Client
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from fpdf import FPDF
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from zmanim.zmanim_calendar import ZmanimCalendar
from zmanim.util.geo_location import GeoLocation

app = Flask(__name__)

# Environment variables
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_KEY")
TWILIO_SID = os.environ.get("TWILIO_SID")
TWILIO_TOKEN = os.environ.get("TWILIO_TOKEN")
MY_WHATSAPP = os.environ.get("MY_WHATSAPP")
GMAIL_USER = os.environ.get("GMAIL_USER")
GMAIL_PASS = os.environ.get("GMAIL_PASS")
ACCOUNTANT_EMAIL = os.environ.get("ACCOUNTANT_EMAIL")
GOOGLE_DRIVE_FOLDER = os.environ.get("GOOGLE_DRIVE_FOLDER")

twilio_client = Client(TWILIO_SID, TWILIO_TOKEN)
claude = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

# Data storage
expenses = []
income = []
invoices = []
clients = {}
invoice_counter = 5719
best_day_record = 0
group1_members = []
group2_members = []
group3_members = []
accountant_drive_folder = None

EXPENSE_CATEGORIES = {
    "fuel": ["fuel", "petrol", "gas", "bp", "z energy", "gull"],
    "tools": ["tool", "lock", "key", "hardware", "bunnings", "mitre10"],
    "supplies": ["supply", "supplies", "material"],
    "vehicle": ["warrant", "rego", "car", "van", "truck", "mechanic", "tyre"],
    "phone": ["phone", "internet", "vodafone", "spark", "2degrees"],
    "rent": ["rent", "storage", "lease"],
    "labour": ["labour", "subcontract", "worker"],
    "food": ["food", "cafe", "restaurant", "mcdonald", "subway"],
    "google_ads": ["google", "ads", "advertising"],
}

JEWISH_HOLIDAYS_2025_2026 = [
    "2025-10-02", "2025-10-03", "2025-10-11", "2025-10-12",
    "2025-10-16", "2025-10-17", "2025-10-18", "2025-10-22", "2025-10-23",
    "2026-04-01", "2026-04-02", "2026-04-08", "2026-04-09",
    "2026-05-21", "2026-05-22",
    "2025-12-14", "2025-12-15", "2025-12-16", "2025-12-17",
    "2025-12-18", "2025-12-19", "2025-12-20", "2025-12-21",
]

def is_jewish_rest_day():
    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    if today in JEWISH_HOLIDAYS_2025_2026:
        return True
    # Shabbat - Friday sunset to Saturday night
    if now.weekday() == 4 and now.hour >= 18:
        return True
    if now.weekday() == 5:
        return True
    return False

def send_whatsapp(msg, to=None):
    if is_jewish_rest_day():
        return
    target = to or MY_WHATSAPP
    twilio_client.messages.create(
        from_="whatsapp:+14155238886",
        to=target,
        body=msg
    )

def get_category(text):
    text = text.lower()
    for category, keywords in EXPENSE_CATEGORIES.items():
        for keyword in keywords:
            if keyword in text:
                return category
    return "other"

def scan_receipt(image_url):
    img_data = requests.get(image_url, auth=(TWILIO_SID, TWILIO_TOKEN)).content
    b64 = base64.b64encode(img_data).decode("utf-8")
    response = claude.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=500,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}},
                {"type": "text", "text": "Scan this receipt. Extract: 1) total amount (number only), 2) vendor/store name, 3) date (YYYY-MM-DD), 4) what was purchased. Reply ONLY in JSON: {\"amount\":\"\",\"vendor\":\"\",\"date\":\"\",\"description\":\"\"}"}
            ]
        }]
    )
    text = response.content[0].text.replace("```json", "").replace("```", "").strip()
    return json.loads(text)

def save_to_drive(image_url, filename, folder_id):
    try:
        scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
        creds = ServiceAccountCredentials.from_json_keyfile_name("credentials.json", scope)
        import googleapiclient.discovery
        drive_service = googleapiclient.discovery.build("drive", "v3", credentials=creds)
        img_data = requests.get(image_url, auth=(TWILIO_SID, TWILIO_TOKEN)).content
        from googleapiclient.http import MediaInMemoryUpload
        media = MediaInMemoryUpload(img_data, mimetype="image/jpeg")
        file_metadata = {"name": filename, "parents": [folder_id]}
        drive_service.files().create(body=file_metadata, media_body=media).execute()
        return True
    except Exception as e:
        print(f"Drive error: {e}")
        return False

def send_email(to, subject, body, attachment_path=None):
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

def create_invoice_pdf(invoice_data):
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Arial", "B", 20)
    pdf.cell(0, 10, "AMPM Services", ln=True, align="R")
    pdf.set_font("Arial", "", 10)
    pdf.cell(0, 6, "NZBN: 9429053281210", ln=True, align="R")
    pdf.cell(0, 6, "+64226068460", ln=True, align="R")
    pdf.cell(0, 6, "ampmservices2024@gmail.com", ln=True, align="R")
    pdf.ln(10)
    pdf.set_font("Arial", "B", 14)
    pdf.cell(0, 8, f"Invoice #{invoice_data['number']}", ln=True)
    pdf.set_font("Arial", "", 10)
    pdf.cell(0, 6, f"Date: {invoice_data['date']}", ln=True)
    pdf.ln(5)
    pdf.set_fill_color(240, 240, 240)
    pdf.set_font("Arial", "B", 10)
    pdf.cell(0, 8, "BILL TO", ln=True, fill=True)
    pdf.set_font("Arial", "", 10)
    pdf.cell(0, 6, invoice_data["client_name"], ln=True)
    pdf.cell(0, 6, invoice_data.get("client_address", ""), ln=True)
    pdf.cell(0, 6, invoice_data.get("client_email", ""), ln=True)
    pdf.ln(8)
    pdf.set_font("Arial", "B", 10)
    pdf.cell(100, 8, "Item", border=1)
    pdf.cell(45, 8, "Price", border=1, align="R")
    pdf.cell(45, 8, "Amount", border=1, align="R")
    pdf.ln()
    pdf.set_font("Arial", "", 10)
    total = 0
    for item in invoice_data["items"]:
        pdf.cell(100, 8, item["description"], border=1)
        pdf.cell(45, 8, f"NZ$ {item['amount']:.2f}", border=1, align="R")
        pdf.cell(45, 8, f"NZ$ {item['amount']:.2f}", border=1, align="R")
        pdf.ln()
        total += item["amount"]
    pdf.ln(5)
    pdf.set_font("Arial", "B", 12)
    pdf.cell(0, 8, f"Total: NZ$ {total:.2f}", ln=True, align="R")
    if not invoice_data.get("paid"):
        pdf.ln(10)
        pdf.set_font("Arial", "B", 10)
        pdf.cell(0, 8, "Payment Details:", ln=True)
        pdf.set_font("Arial", "", 10)
        pdf.cell(0, 6, "Name: AMPM Services", ln=True)
        pdf.cell(0, 6, "Account: 03-0255-0294020-000", ln=True)
        pdf.cell(0, 6, f"Reference: Invoice #{invoice_data['number']}", ln=True)
    path = f"/tmp/invoice_{invoice_data['number']}.pdf"
    pdf.output(path)
    return path

def get_motivation(today_total):
    global best_day_record
    if today_total > best_day_record:
        best_day_record = today_total
        return f"🏆 NEW RECORD! NZ$ {today_total:.2f} today — YOU'RE ON FIRE!"
    return f"💪 NZ$ {today_total:.2f} done! Best day: NZ$ {best_day_record:.2f} — let's beat it!"

@app.route("/webhook", methods=["POST"])
def webhook():
    global invoice_counter
    msg = request.form.get("Body", "").strip()
    media_url = request.form.get("MediaUrl0")
    num_media = int(request.form.get("NumMedia", 0))
    from_number = request.form.get("From", "")
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    msg_lower = msg.lower()

    # GROUP 1 - Expense receipt
    if num_media > 0 and media_url and from_number in group1_members or from_number == MY_WHATSAPP:
        if any(word in msg_lower for word in ["expense", "receipt", "paid", "bought", "fuel", "tool", "supply"]):
            try:
                data = scan_receipt(media_url)
                amount = float(data.get("amount", 0) or 0)
                vendor = data.get("vendor", "Unknown")
                date = data.get("date", now[:10])
                description = data.get("description", msg)
                category = get_category(vendor + " " + description)
                expense = {"amount": amount, "vendor": vendor, "date": date, "description": description, "category": category, "image_url": media_url}
                expenses.append(expense)
                if accountant_drive_folder:
                    save_to_drive(media_url, f"receipt_{date}_{vendor}.jpg", accountant_drive_folder)
                send_whatsapp(f"✅ Expense saved!\n🏪 {vendor}\n💸 NZ$ {amount:.2f}\n🏷️ {category.title()}\n📅 {date}\n📝 {description}")
            except Exception as e:
                send_whatsapp(f"❌ Could not scan receipt. Please try again.\nError: {str(e)}")

    # GROUP 2 - Job income
    elif "job" in msg_lower and "paid" in msg_lower:
        try:
            parts_cost = 0
            if "part" in msg_lower:
                import re
                part_match = re.search(r'part[:\s]+(\d+\.?\d*)', msg_lower)
                if part_match:
                    parts_cost = float(part_match.group(1))
            amount_match = re.search(r'paid[:\s]+(\d+\.?\d*)', msg_lower)
            amount = float(amount_match.group(1)) if amount_match else 0
            net = amount - parts_cost
            today = datetime.now().strftime("%Y-%m-%d")
            today_total = sum(i["net"] for i in income if i["date"].startswith(today)) + net
            income.append({"amount": amount, "parts": parts_cost, "net": net, "description": msg, "date": now})
            motivation = get_motivation(today_total)
            send_whatsapp(f"✅ Job saved!\n💰 Paid: NZ$ {amount:.2f}\n🔧 Parts: NZ$ {parts_cost:.2f}\n📈 Profit: NZ$ {net:.2f}\n\n{motivation}")
        except Exception as e:
            send_whatsapp(f"❌ Could not save job. Try: JOB - PAID 250 card - PART 30 - description")

    # INVOICE creation
    elif msg_lower.startswith("invoice:") or msg_lower.startswith("invoice :"):
        lines = msg.strip().split("\n")
        client_name = ""
        client_email = ""
        client_address = ""
        items = []
        for line in lines[1:]:
            line = line.strip()
            if "@" in line:
                parts = line.split(" ")
                for p in parts:
                    if "@" in p:
                        client_email = p
                client_name = line.replace(client_email, "").strip()
            elif "address:" in line.lower():
                client_address = line.replace("Address:", "").replace("address:", "").strip()
            elif line and not line.lower().startswith("total") and not line.lower().startswith("gst"):
                import re
                match = re.search(r'(.+?)\s+(\d+\.?\d*)$', line)
                if match:
                    items.append({"description": match.group(1).strip(), "amount": float(match.group(2))})
        total = sum(i["amount"] for i in items)
        preview = f"📄 *Invoice Preview #{invoice_counter}*\n\n"
        preview += f"👤 {client_name}\n📧 {client_email}\n📍 {client_address}\n\n"
        for item in items:
            preview += f"  • {item['description']}: NZ$ {item['amount']:.2f}\n"
        preview += f"\n💰 *Total: NZ$ {total:.2f}*\n\n"
        preview += "Reply *CONFIRM* to send or *EDIT* to change"
        pending_invoice = {"number": invoice_counter, "client_name": client_name, "client_email": client_email, "client_address": client_address, "items": items, "total": total, "date": datetime.now().strftime("%d %b %Y"), "paid": False}
        invoices.append({"status": "pending", "data": pending_invoice})
        if client_email and client_email in clients:
            pass
        else:
            clients[client_email] = {"name": client_name, "email": client_email, "address": client_address}
        send_whatsapp(preview)

    elif msg_lower == "confirm":
        pending = next((i for i in invoices if i["status"] == "pending"), None)
        if pending:
            pending["status"] = "sent"
            invoice_counter += 1
            pdf_path = create_invoice_pdf(pending["data"])
            email_body = f"""
            <h2>Invoice #{pending['data']['number']}</h2>
            <p>Dear {pending['data']['client_name']},</p>
            <p>Please find your invoice attached.</p>
            <p>To pay, use these bank details:<br>
            Name: AMPM Services<br>
            Account: 03-0255-0294020-000<br>
            Reference: Invoice #{pending['data']['number']}</p>
            <p>Once paid, simply reply PAID to this email.</p>
            <br><p>Thank you!<br>AMPM Services<br>+64226068460</p>
            """
            send_email(pending["data"]["client_email"], f"Invoice #{pending['data']['number']} - AMPM Services", email_body, pdf_path)
            send_whatsapp(f"✅ Invoice #{pending['data']['number']} sent to {pending['data']['client_email']}!")

    elif msg_lower == "report" or msg_lower == "daily":
        send_daily_report()
    elif msg_lower == "weekly":
        send_weekly_report()
    elif msg_lower == "outstanding":
        send_outstanding_invoices()
    elif msg_lower == "help":
        send_whatsapp("""👋 *AMPM Services Bot*

📸 *Expenses:* Send receipt photo + word 'expense'
💼 *Jobs:* JOB - PAID 250 card - PART 30 - description
📄 *Invoice:* Start message with INVOICE:
📊 *Reports:* Type 'report' or 'weekly'
💰 *Outstanding:* Type 'outstanding'
✅ *Confirm invoice:* Type 'CONFIRM'""")

    return "OK", 200

def send_daily_report():
    if is_jewish_rest_day():
        return
    today = datetime.now().strftime("%Y-%m-%d")
    todays_income = [i for i in income if i["date"].startswith(today)]
    todays_expenses = [e for e in expenses if e["date"].startswith(today)]
    total_in = sum(i["net"] for i in todays_income)
    total_ex = sum(e["amount"] for e in todays_expenses)
    lines = [f"📊 *AMPM Services*", f"📅 Daily Report — {today}", ""]
    lines.append(f"💰 Income: NZ$ {total_in:.2f}")
    lines.append(f"🧾 Expenses: NZ$ {total_ex:.2f}")
    lines.append(f"📈 Net Profit: NZ$ {(total_in - total_ex):.2f}")
    send_whatsapp("\n".join(lines))

def send_weekly_report():
    if is_jewish_rest_day():
        return
    week_ago = datetime.now() - timedelta(days=7)
    weekly_income = [i for i in income if datetime.strptime(i["date"][:10], "%Y-%m-%d") >= week_ago]
    weekly_expenses = [e for e in expenses if datetime.strptime(e["date"][:10], "%Y-%m-%d") >= week_ago]
    total_in = sum(i["net"] for i in weekly_income)
    total_ex = sum(e["amount"] for e in weekly_expenses)
    by_category = {}
    for e in weekly_expenses:
        cat = e.get("category", "other")
        by_category[cat] = by_category.get(cat, 0) + e["amount"]
    lines = [f"📊 *AMPM Services*", f"📆 Weekly Report", ""]
    lines.append(f"💰 Total Income: NZ$ {total_in:.2f}")
    lines.append(f"\n🧾 Expenses by Category:")
    for cat, amount in by_category.items():
        lines.append(f"  • {cat.title()}: NZ$ {amount:.2f}")
    lines.append(f"\n💸 Total Expenses: NZ$ {total_ex:.2f}")
    lines.append(f"📈 Net Profit: NZ$ {(total_in - total_ex):.2f}")
    send_whatsapp("\n".join(lines))

def send_outstanding_invoices():
    unpaid = [i for i in invoices if i["status"] == "sent" and not i["data"].get("paid")]
    if not unpaid:
        send_whatsapp("✅ No outstanding invoices!")
        return
    lines = ["💰 *Outstanding Invoices*\n"]
    total = 0
    for inv in unpaid:
        d = inv["data"]
        lines.append(f"#{d['number']} — {d['client_name']} — NZ$ {d['total']:.2f}")
        total += d["total"]
    lines.append(f"\n💸 Total owing: NZ$ {total:.2f}")
    send_whatsapp("\n".join(lines))

def check_unpaid_reminders():
    if is_jewish_rest_day():
        return
    unpaid = [i for i in invoices if i["status"] == "sent" and not i["data"].get("paid")]
    for inv in unpaid:
        d = inv["data"]
        send_whatsapp(f"⏰ Reminder: Invoice #{d['number']} for {d['client_name']} — NZ$ {d['total']:.2f} still unpaid!")

scheduler = BackgroundScheduler()
scheduler.add_job(send_daily_report, "cron", hour=20, minute=0)
scheduler.add_job(send_weekly_report, "cron", day_of_week="mon", hour=8, minute=0)
scheduler.add_job(check_unpaid_reminders, "interval", hours=48)
scheduler.start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
