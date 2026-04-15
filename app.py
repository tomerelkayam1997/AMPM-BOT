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

app = Flask(__name__)

ANTHROPIC_KEY = os.environ.get("ANTHROPIC_KEY")
TWILIO_SID = os.environ.get("TWILIO_SID")
TWILIO_TOKEN = os.environ.get("TWILIO_TOKEN")
MY_WHATSAPP = os.environ.get("MY_WHATSAPP", "whatsapp:+61449984648")
GMAIL_USER = os.environ.get("GMAIL_USER")
GMAIL_PASS = os.environ.get("GMAIL_PASS")

twilio_client = Client(TWILIO_SID, TWILIO_TOKEN)
claude = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

expenses = []
income = []
invoices = []
clients = {}
invoice_counter = 5719
best_day_record = 0
pending_invoice = None

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
        print(f"Message sent to {to}")
    except Exception as e:
        print(f"Error sending message: {e}")

def scan_receipt(image_url):
    try:
        img_data = requests.get(image_url, auth=(TWILIO_SID, TWILIO_TOKEN)).content
        b64 = base64.b64encode(img_data).decode("utf-8")
        response = claude.messages.create(
            model="claude-sonnet-4-20250514",
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

def get_motivation(today_total):
    global best_day_record
    if today_total > best_day_record:
        best_day_record = today_total
        return f"🏆 NEW RECORD! NZ$ {today_total:.2f} today — YOU ARE ON FIRE!"
    return f"💪 NZ$ {today_total:.2f} done! Best day: NZ$ {best_day_record:.2f} — let's beat it!"

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
        print(f"Email sent to {to}")
    except Exception as e:
        print(f"Email error: {e}")

@app.route("/webhook", methods=["POST"])
def webhook():
    global invoice_counter, pending_invoice
    msg = request.form.get("Body", "").strip()
    media_url = request.form.get("MediaUrl0")
    num_media = int(request.form.get("NumMedia", 0))
    msg_lower = msg.lower()
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    print(f"Received: {msg}")

    if msg_lower == "help":
        send_whatsapp("""👋 Hey! I'm Melisa 😊 — AMPM Services Bot

📸 EXPENSE: Send receipt photo + type expense
💼 JOB: JOB - PAID 250 - PART 30 - description
📄 INVOICE: Start with INVOICE:
📊 REPORT: Type report
📆 WEEKLY: Type weekly
💰 OUTSTANDING: Type outstanding
✅ CONFIRM invoice: Type confirm""")

    elif num_media > 0 and media_url:
        data = scan_receipt(media_url)
        amount = float(data.get("amount") or 0)
        vendor = data.get("vendor", "Unknown")
        date = data.get("date") or now[:10]
        description = data.get("description") or msg
        category = data.get("category", "other")
        expenses.append({"amount": amount, "vendor": vendor, "date": date, "description": description, "category": category})
        send_whatsapp(f"✅ Expense saved!\n🏪 {vendor}\n💸 NZ$ {amount:.2f}\n🏷️ {category.title()}\n📅 {date}\n📝 {description}")

    elif "job" in msg_lower and "paid" in msg_lower:
        import re
        parts_cost = 0
        part_match = re.search(r'part[:\s\-]+(\d+\.?\d*)', msg_lower)
        if part_match:
            parts_cost = float(part_match.group(1))
        amount_match = re.search(r'paid[:\s\-]+(\d+\.?\d*)', msg_lower)
        amount = float(amount_match.group(1)) if amount_match else 0
        net = amount - parts_cost
        today = datetime.now().strftime("%Y-%m-%d")
        income.append({"amount": amount, "parts": parts_cost, "net": net, "description": msg, "date": now})
        today_total = sum(i["net"] for i in income if i["date"].startswith(today))
        motivation = get_motivation(today_total)
        send_whatsapp(f"✅ Job saved!\n💰 Paid: NZ$ {amount:.2f}\n🔧 Parts: NZ$ {parts_cost:.2f}\n📈 Profit: NZ$ {net:.2f}\n\n{motivation}")

    elif msg_lower.startswith("invoice"):
        lines = msg.strip().split("\n")
        client_name = ""
        client_email = ""
        client_address = ""
        items = []
        import re
        for line in lines[1:]:
            line = line.strip()
            if not line:
                continue
            if "@" in line:
                parts = line.split()
                for p in parts:
                    if "@" in p:
                        client_email = p
                client_name = line.replace(client_email, "").strip()
            elif "address" in line.lower():
                client_address = line.replace("Address:", "").replace("address:", "").strip()
            else:
                match = re.search(r'(.+?)\s+(\d+\.?\d*)$', line)
                if match:
                    items.append({"description": match.group(1).strip(), "amount": float(match.group(2))})
        total = sum(i["amount"] for i in items)
        pending_invoice = {"number": invoice_counter, "client_name": client_name, "client_email": client_email, "client_address": client_address, "items": items, "total": total, "date": datetime.now().strftime("%d %b %Y"), "paid": False}
        preview = f"📄 Invoice Preview #{invoice_counter}\n\n"
        preview += f"👤 {client_name}\n📧 {client_email}\n📍 {client_address}\n\n"
        for item in items:
            preview += f"  • {item['description']}: NZ$ {item['amount']:.2f}\n"
        preview += f"\n💰 Total: NZ$ {total:.2f}\n\n"
        preview += "Reply CONFIRM to send or EDIT to change"
        send_whatsapp(preview)

    elif msg_lower == "confirm" and pending_invoice:
        pdf_path = create_invoice_pdf(pending_invoice)
        email_body = f"""<h2>Invoice #{pending_invoice['number']}</h2>
        <p>Dear {pending_invoice['client_name']},</p>
        <p>Please find your invoice attached.</p>
        <p>Payment details:<br>Name: AMPM Services<br>Account: 03-0255-0294020-000<br>Reference: Invoice #{pending_invoice['number']}</p>
        <p>Reply PAID to confirm payment.</p>
        <p>Thank you!<br>AMPM Services<br>+64226068460</p>"""
        send_email(pending_invoice["client_email"], f"Invoice #{pending_invoice['number']} - AMPM Services", email_body, pdf_path)
        invoices.append(pending_invoice)
        invoice_counter += 1
        send_whatsapp(f"✅ Invoice #{pending_invoice['number']} sent to {pending_invoice['client_email']}!")
        pending_invoice = None

    elif msg_lower == "report":
        today = datetime.now().strftime("%Y-%m-%d")
        t_income = sum(i["net"] for i in income if i["date"].startswith(today))
        t_expense = sum(e["amount"] for e in expenses if e["date"].startswith(today))
        send_whatsapp(f"📊 Daily Report — {today}\n\n💰 Income: NZ$ {t_income:.2f}\n🧾 Expenses: NZ$ {t_expense:.2f}\n📈 Profit: NZ$ {(t_income-t_expense):.2f}")

    elif msg_lower == "weekly":
        week_ago = datetime.now() - timedelta(days=7)
        w_income = sum(i["net"] for i in income if datetime.strptime(i["date"][:10], "%Y-%m-%d") >= week_ago)
        w_expense = sum(e["amount"] for e in expenses if datetime.strptime(e["date"][:10], "%Y-%m-%d") >= week_ago)
        by_cat = {}
        for e in expenses:
            if datetime.strptime(e["date"][:10], "%Y-%m-%d") >= week_ago:
                cat = e.get("category", "other")
                by_cat[cat] = by_cat.get(cat, 0) + e["amount"]
        lines = [f"📆 Weekly Report\n", f"💰 Income: NZ$ {w_income:.2f}", f"🧾 Expenses: NZ$ {w_expense:.2f}"]
        for cat, amt in by_cat.items():
            lines.append(f"  • {cat.title()}: NZ$ {amt:.2f}")
        lines.append(f"📈 Profit: NZ$ {(w_income-w_expense):.2f}")
        send_whatsapp("\n".join(lines))

    elif msg_lower == "outstanding":
        unpaid = [i for i in invoices if not i.get("paid")]
        if not unpaid:
            send_whatsapp("✅ No outstanding invoices!")
        else:
            lines = ["💰 Outstanding Invoices\n"]
            for inv in unpaid:
                lines.append(f"#{inv['number']} — {inv['client_name']} — NZ$ {inv['total']:.2f}")
            send_whatsapp("\n".join(lines))

    else:
        send_whatsapp("👋 Hey! I'm Melisa 😊 Type HELP to see all commands!")

    return "OK", 200

def send_daily_report():
    today = datetime.now().strftime("%Y-%m-%d")
    t_income = sum(i["net"] for i in income if i["date"].startswith(today))
    t_expense = sum(e["amount"] for e in expenses if e["date"].startswith(today))
    send_whatsapp(f"📊 Daily Report — {today}\n\n💰 Income: NZ$ {t_income:.2f}\n🧾 Expenses: NZ$ {t_expense:.2f}\n📈 Profit: NZ$ {(t_income-t_expense):.2f}")

def check_unpaid():
    unpaid = [i for i in invoices if not i.get("paid")]
    for inv in unpaid:
        send_whatsapp(f"⏰ Reminder: Invoice #{inv['number']} for {inv['client_name']} — NZ$ {inv['total']:.2f} still unpaid!")

scheduler = BackgroundScheduler()
scheduler.add_job(send_daily_report, "cron", hour=20, minute=0)
scheduler.add_job(check_unpaid, "interval", hours=48)
scheduler.start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
