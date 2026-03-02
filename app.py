import os
import io
import json
import logging
import base64
import zipfile
import tempfile
import smtplib
import random
import string
import qrcode
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from datetime import date, datetime, timedelta, timezone
from urllib.parse import unquote

from openpyxl import Workbook
from flask import Flask, request, send_file, jsonify, render_template, redirect, url_for, flash, session
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from dotenv import load_dotenv
from fpdf import FPDF
from PIL import Image

# --- LOAD ENV ---
load_dotenv()

# --- FIREBASE SETUP ---
import firebase_admin
from firebase_admin import credentials, firestore

if not firebase_admin._apps:
    firebase_creds_env = os.getenv("FIREBASE_CREDENTIALS")
    if firebase_creds_env:
        cred_json = json.loads(base64.b64decode(firebase_creds_env))
        cred = credentials.Certificate(cred_json)
    else:
        # LOCAL FALLBACK
        key_path = "invoice-generator-5c42c-firebase-adminsdk-fbsvc-dd71702a41.json"
        if os.path.exists(key_path):
            cred = credentials.Certificate(key_path)
        else:
            cred = None
            logging.warning("No Firebase Credentials found. DB calls will fail.")
    
    if cred:
        firebase_admin.initialize_app(cred)

db = firestore.client() if firebase_admin._apps else None

# ------------------ CONFIG ------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CALIBRI_FONT_PATH = os.path.join(BASE_DIR, "CALIBRI.TTF")
DEFAULT_LOGO = os.path.join(BASE_DIR, "static", "logo.png") 
DEFAULT_SIGNATURE = os.path.join(BASE_DIR, "static", "Signatory.png")
TEMPLATE_DIR = os.path.join(BASE_DIR, "templates")

app = Flask(__name__, template_folder=TEMPLATE_DIR, static_folder=os.path.join(BASE_DIR, "static"))
app.secret_key = os.getenv("SECRET_KEY", "change_this_secret")

EMAIL_HOST = os.getenv('EMAIL_HOST', 'smtp.gmail.com')
EMAIL_PORT = int(os.getenv('EMAIL_PORT', 587))
EMAIL_USER = os.getenv('EMAIL_USER')
EMAIL_PASSWORD = os.getenv('EMAIL_PASSWORD')

# UPI CONFIG
UPI_ID = "sugandh.mishra1@ybl"
UPI_NAME = "SM Tech"

# CRON TIME (UTC) -> 16:30 UTC = 10:00 PM IST
REPORT_HOUR_UTC = 16 

# GST STATE CODES FOR GSTR-1
STATE_CODES = {
    "Jammu and Kashmir": "01", "Himachal Pradesh": "02", "Punjab": "03", "Chandigarh": "04",
    "Uttarakhand": "05", "Haryana": "06", "Delhi": "07", "Rajasthan": "08", "Uttar Pradesh": "09",
    "Bihar": "10", "Sikkim": "11", "Arunachal Pradesh": "12", "Nagaland": "13", "Manipur": "14",
    "Mizoram": "15", "Tripura": "16", "Meghalaya": "17", "Assam": "18", "West Bengal": "19",
    "Jharkhand": "20", "Odisha": "21", "Chhattisgarh": "22", "Madhya Pradesh": "23",
    "Gujarat": "24", "Dadra and Nagar Haveli": "26", "Maharashtra": "27", "Karnataka": "29",
    "Goa": "30", "Lakshadweep": "31", "Kerala": "32", "Tamil Nadu": "33", "Puducherry": "34",
    "Andaman and Nicobar Islands": "35", "Telangana": "36", "Andhra Pradesh": "37", "Ladakh": "38"
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# ------------------ AUTH ------------------
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "login"

MASTER_USERNAME = os.getenv("LOGIN_USER", "admin")
MASTER_PASSWORD = os.getenv("LOGIN_PASS", "password")

class User(UserMixin):
    def __init__(self, id, is_master=False, payment_active=True):
        self.id = id
        self.is_master = is_master
        # payment_active determines if they can access the dashboard.
        self.payment_active = payment_active

    @property
    def is_active(self):
        return True

@login_manager.user_loader
def load_user(user_id):
    if user_id == MASTER_USERNAME:
        return User(user_id, is_master=True, payment_active=True)
    
    try:
        user_doc = db.collection('app_users').document(user_id).get()
        if user_doc.exists:
            data = user_doc.to_dict()
            db_active_status = data.get('is_active', False)
            return User(user_id, is_master=False, payment_active=db_active_status)
    except: pass
    return None

# ------------------ ACTIVATION MIDDLEWARE ------------------
@app.before_request
def check_activation():
    if current_user.is_authenticated:
        if not current_user.is_master and not current_user.payment_active:
            if request.endpoint not in ['activation_page', 'logout', 'static', 'check_status_api']:
                return redirect(url_for('activation_page'))

# ------------------ HELPERS ------------------
def generate_otp():
    return ''.join(random.choices(string.digits, k=6))

def send_email_raw(to_email, subject, body):
    try:
        msg = MIMEText(body)
        msg['Subject'] = subject
        msg['From'] = EMAIL_USER
        msg['To'] = to_email
        with smtplib.SMTP(EMAIL_HOST, EMAIL_PORT) as server:
            server.starttls()
            server.login(EMAIL_USER, EMAIL_PASSWORD)
            server.send_message(msg)
        return True
    except Exception as e:
        logging.error(f"Email Error: {e}")
        return False

# --- IMPROVED IMAGE COMPRESSION ---
def compress_image(file_storage, max_width=400):
    try:
        img = Image.open(file_storage)
        
        # Resize logic
        width_percent = (max_width / float(img.size[0]))
        if width_percent < 1:
            new_height = int((float(img.size[1]) * float(width_percent)))
            img = img.resize((max_width, new_height), Image.Resampling.LANCZOS)
        
        # Convert all images to RGBA/RGB and save as PNG to ensure FPDF compatibility
        # This fixes the issue where JPEGs saved as .png caused errors
        if img.mode not in ('RGBA', 'RGB', 'L'):
            img = img.convert('RGBA')
            
        output = io.BytesIO()
        img.save(output, format='PNG', optimize=True)
        return base64.b64encode(output.getvalue()).decode('utf-8')
    except Exception as e:
        logging.error(f"Error processing image: {e}")
        return None

# ------------------ DATABASE CONTEXT HELPER ------------------
def get_db_base(target_user=None):
    if target_user:
        if target_user == MASTER_USERNAME: return db
        return db.collection('users').document(target_user)

    if current_user.is_authenticated and not current_user.is_master:
        return db.collection('users').document(current_user.id)

    view_mode = session.get('view_mode')
    if current_user.is_authenticated and current_user.is_master and view_mode and view_mode != MASTER_USERNAME:
        return db.collection('users').document(view_mode)

    return db

def get_all_users():
    users = [MASTER_USERNAME]
    try:
        docs = db.collection('app_users').stream()
        for doc in docs:
            users.append(doc.id)
    except: pass
    return sorted(users)

@app.context_processor
def inject_global_data():
    if current_user.is_authenticated:
        profile = get_seller_profile_data()
        all_users = get_all_users() if current_user.is_master else []
        viewing_user = session.get('view_mode', current_user.id)
        return dict(profile=profile, current_user=current_user, all_users=all_users, viewing_user=viewing_user)
    return dict(profile={}, current_user=None, all_users=[], viewing_user=None)

# ------------------ FIRESTORE HELPERS ------------------
def get_seller_profile_data(target_user_id=None):
    try:
        base = get_db_base(target_user=target_user_id)
        is_root = (base == db)
        if is_root:
            doc = base.collection('config').document('seller_profile').get()
        else:
            doc = base.collection('config').document('profile').get()

        if doc.exists:
            return doc.to_dict()
    except Exception as e:
        logging.error(f"Error fetching profile: {e}")
    
    return {
        "company_name": "SM Tech",
        "invoice_prefix": "SMT",
    }

def save_seller_profile_data(data, target_user_id=None):
    base = get_db_base(target_user=target_user_id)
    is_root = (base == db)
    if is_root:
        base.collection('config').document('seller_profile').set(data, merge=True)
    else:
        base.collection('config').document('profile').set(data, merge=True)

def load_clients():
    base = get_db_base()
    docs = base.collection('clients').stream()
    return {doc.id: doc.to_dict() for doc in docs}

def save_single_client(name, data):
    base = get_db_base()
    base.collection('clients').document(name).set(data, merge=True)

def load_invoices_for_user(target_user_id):
    base = get_db_base(target_user=target_user_id)
    docs = base.collection('invoices').stream()
    return [doc.to_dict() for doc in docs]

def load_invoices():
    base = get_db_base()
    docs = base.collection('invoices').stream()
    return [doc.to_dict() for doc in docs]

def save_single_invoice(invoice_data):
    base = get_db_base()
    doc_id = invoice_data['bill_no'].replace('/', '_')
    base.collection('invoices').document(doc_id).set(invoice_data)

def load_particulars():
    base = get_db_base()
    docs = base.collection('particulars').stream()
    return {doc.id: doc.to_dict() for doc in docs}

def save_single_particular(name, data):
    base = get_db_base()
    base.collection('particulars').document(name).set(data, merge=True)

def get_next_counter(is_credit_note=False):
    base = get_db_base()
    doc_ref = base.collection('config').document('counters')
    @firestore.transactional
    def update_in_transaction(transaction, doc_ref):
        snapshot = doc_ref.get(transaction=transaction)
        if not snapshot.exists:
            new_data = {"counter": 0, "cn_counter": 0}
            transaction.set(doc_ref, new_data)
            current_val = 0
        else:
            current_data = snapshot.to_dict()
            field = "cn_counter" if is_credit_note else "counter"
            current_val = current_data.get(field, 0)
        new_val = current_val + 1
        field = "cn_counter" if is_credit_note else "counter"
        transaction.update(doc_ref, {field: new_val})
        return new_val
    return update_in_transaction(db.transaction(), doc_ref)

def get_all_activation_requests():
    try:
        docs = db.collection('activation_requests').order_by('timestamp', direction=firestore.Query.DESCENDING).stream()
        return [doc.to_dict() for doc in docs]
    except: return []

# ------------------ UTILS & PDF ------------------
def convert_to_words(number):
    units = ["","One","Two","Three","Four","Five","Six","Seven","Eight","Nine","Ten",
             "Eleven","Twelve","Thirteen","Fourteen","Fifteen","Sixteen","Seventeen","Eighteen","Nineteen"]
    tens = ["","","Twenty","Thirty","Forty","Fifty","Sixty","Seventy","Eighty","Ninety"]
    def two_digit(n):
        return units[n] if n < 20 else tens[n//10] + (" " + units[n%10] if n%10 else "")
    def three_digit(n):
        s = ""
        if n >= 100: s += units[n//100] + " Hundred" + (" " if n % 100 else "")
        if n % 100: s += two_digit(n%100)
        return s
    n = int(abs(number))
    paise = round((abs(number) - n) * 100)
    crore = n // 10000000; n %= 10000000
    lakh = n // 100000; n %= 100000
    thousand = n // 1000; n %= 1000
    hundred = n
    parts = []
    if crore: parts.append(three_digit(crore) + " Crore")
    if lakh: parts.append(three_digit(lakh) + " Lakh")
    if thousand: parts.append(three_digit(thousand) + " Thousand")
    if hundred: parts.append(three_digit(hundred))
    words = " ".join(parts) if parts else "Zero"
    if paise: words += f" and {two_digit(paise)} Paise"
    return words + " Only"

def send_email_with_attachment(to_email, subject, body, attachment_bytes, filename):
    msg = MIMEMultipart()
    msg['From'] = EMAIL_USER
    msg['To'] = to_email
    msg['Subject'] = subject
    msg.attach(MIMEText(body, 'plain'))
    part = MIMEBase('application', 'octet-stream')
    part.set_payload(attachment_bytes.getvalue())
    encoders.encode_base64(part)
    part.add_header('Content-Disposition', f'attachment; filename={filename}')
    msg.attach(part)
    with smtplib.SMTP(EMAIL_HOST, EMAIL_PORT) as server:
        server.starttls()
        server.login(EMAIL_USER, EMAIL_PASSWORD)
        server.send_message(msg)

def PDF_Generator(invoice_data, is_credit_note=False):
    pdf = FPDF()
    pdf.add_page()
    pdf.add_font("Calibri", "", CALIBRI_FONT_PATH, uni=True)
    pdf.add_font("Calibri", "B", CALIBRI_FONT_PATH, uni=True)

    profile = get_seller_profile_data()
    
    margin = 15
    page_width = pdf.w - 2 * margin 
    col_width = (page_width / 2) - 5 
    line_height = 5 
    
    # --- LOGO RENDER ---
    logo_data = profile.get('logo_base64')
    if logo_data:
        try:
            with tempfile.NamedTemporaryFile(delete=False, suffix=".png") as tmp:
                tmp.write(base64.b64decode(logo_data))
                tmp_path = tmp.name
            pdf.image(tmp_path, x=15, y=8, w=30)
            os.unlink(tmp_path)
        except Exception as e:
            logging.error(f"Logo Error: {e}")
    elif os.path.exists(DEFAULT_LOGO):
        pdf.image(DEFAULT_LOGO, x=15, y=8, w=30)
    
    # --- HEADER ---
    pdf.set_font("Calibri", "B", 22)
    is_non_gst = invoice_data.get('is_non_gst', False)
    
    if is_credit_note:
        pdf.set_text_color(220, 38, 38) # Red
        header_title = "CREDIT NOTE"
    elif is_non_gst:
        pdf.set_text_color(0, 128, 0) # Green
        header_title = "BILL OF SUPPLY"
    else:
        pdf.set_text_color(255, 165, 0) # Orange
        header_title = "TAX INVOICE"

    pdf.cell(page_width, 10, profile.get('company_name', 'SM Tech'), ln=True, align='C')
    pdf.set_font("Calibri", "B", 14)
    pdf.set_text_color(0, 0, 0)
    pdf.cell(page_width, 8, header_title, ln=True, align='C')
    
    # --- SELLER DETAILS ---
    pdf.set_font("Calibri", "", 10)
    my_gstin = profile.get('gstin', '')
    address_str = f"{profile.get('address_1','')}\n{profile.get('address_2','')}\nPhone: {profile.get('phone','')} | E-mail: {profile.get('email','')}\nGSTIN: {my_gstin}"
    pdf.multi_cell(page_width, line_height, address_str, align='C')
    pdf.ln(5)
    pdf.line(margin, pdf.get_y(), pdf.w - margin, pdf.get_y())

    # --- CREDIT NOTE REF ---
    if is_credit_note:
        pdf.ln(3)
        pdf.set_font("Calibri", "B", 11)
        pdf.set_text_color(220, 38, 38)
        ref_bill = invoice_data.get('original_invoice_no', '')
        if not ref_bill:
             ref_bill = invoice_data.get('bill_no', '').replace('CN-', '').replace('TE-CN', 'TE')
        pdf.cell(0, 7, f"This is a credit note against Invoice No: {ref_bill}", ln=True, align='C')
        pdf.set_text_color(0, 0, 0)
    pdf.ln(5)

    # --- ADDRESS FORMATTING HELPERS ---
    def format_address(prefix):
        addr_lines = [
            invoice_data.get(f'{prefix}_name',''),
            invoice_data.get(f'{prefix}_address1',''),
            invoice_data.get(f'{prefix}_address2',''),
            f"{invoice_data.get(f'{prefix}_district','')} - {invoice_data.get(f'{prefix}_pincode','')}",
            f"{invoice_data.get(f'{prefix}_state','')}",
            f"GSTIN: {invoice_data.get(f'{prefix}_gstin','')}",
            f"Mobile: {invoice_data.get(f'{prefix}_mobile','')}"
        ]
        # Filter empty lines
        return "\n".join([line for line in addr_lines if line and line.strip() != '-' and line.strip() != ''])

    bill_to_text = format_address('client')
    ship_to_text = format_address('shipto')
    
    invoice_no_text = f"{header_title} No: {invoice_data.get('bill_no','')}"
    invoice_date_text = f"Date: {invoice_data.get('invoice_date','')}"
    po_number_text = f"PO Number: {invoice_data.get('po_number','')}"

    y_start = pdf.get_y()
    pdf.set_font("Calibri", "B", 12)
    pdf.cell(col_width, line_height, "Bill To:", ln=True)
    pdf.set_font("Calibri", "", 10)
    pdf.multi_cell(col_width, line_height, bill_to_text)
    
    y_left = pdf.get_y()
    pdf.set_y(y_start)
    pdf.set_x(margin + col_width + 10)
    
    pdf.set_font("Calibri", "B", 10)
    pdf.multi_cell(col_width, line_height, f"{invoice_no_text}\n{invoice_date_text}")
    
    y_right = pdf.get_y()
    pdf.set_y(max(y_left, y_right))
    pdf.ln(5) 
    
    y_start = pdf.get_y()
    pdf.set_font("Calibri", "B", 12)
    pdf.cell(col_width, line_height, "Ship To:", ln=True)
    pdf.set_font("Calibri", "", 10)
    pdf.multi_cell(col_width, line_height, ship_to_text)
    
    y_left = pdf.get_y()
    pdf.set_y(y_start)
    pdf.set_x(margin + col_width + 10)
    
    pdf.set_font("Calibri", "B", 10)
    pdf.multi_cell(col_width, line_height, po_number_text)
    
    y_right = pdf.get_y()
    pdf.set_y(max(y_left, y_right))
    pdf.ln(10) 
    
    # --- TABLE CONFIGURATION (Refined Widths) ---
    # Total Width available: 180mm
    # Particulars: 50 | HSN: 15 | Qty: 12 | Rate: 20 | Tax%: 13 | Taxable: 25 | TaxAmt: 20 | Total: 25
    # Sum: 50+15+12+20+13+25+20+25 = 180
    
    particulars_w = 50
    hsn_w = 15
    qty_w = 12
    rate_w = 20
    tax_percent_w = 13
    taxable_amt_w = 25
    tax_amt_w = 20
    total_w = 25

    # --- TABLE HEADER ---
    pdf.set_fill_color(255, 204, 153) # Light Orange
    pdf.set_font("Calibri", "B", 9) # Slightly smaller font for better fit
    pdf.cell(particulars_w, 8, "Particulars", 1, 0, 'L', True)
    pdf.cell(hsn_w, 8, "HSN", 1, 0, 'C', True)
    pdf.cell(qty_w, 8, "Qty", 1, 0, 'C', True)
    pdf.cell(rate_w, 8, "Rate", 1, 0, 'R', True)
    pdf.cell(tax_percent_w, 8, "Tax %", 1, 0, 'R', True)
    pdf.cell(taxable_amt_w, 8, "Taxable", 1, 0, 'R', True)
    pdf.cell(tax_amt_w, 8, "Tax Amt", 1, 0, 'R', True)
    pdf.cell(total_w, 8, "Total", 1, 1, 'R', True)

    # --- TABLE ROWS ---
    pdf.set_font("Calibri", "", 9)
    particulars = invoice_data.get('particulars', [])
    hsns = invoice_data.get('hsns', [])
    qtys = invoice_data.get('qtys', [])
    rates = invoice_data.get('rates', [])
    taxrates = invoice_data.get('taxrates', [])
    amounts = invoice_data.get('amounts', []) 
    line_tax_amounts = invoice_data.get('line_tax_amounts', [])
    line_total_amounts = invoice_data.get('line_total_amounts', [])
    
    total_qty_calc = 0

    for i in range(len(particulars)):
        start_y, start_x = pdf.get_y(), pdf.get_x()
        
        # Multi-cell for Particulars (Auto Wrap)
        pdf.multi_cell(particulars_w, 7, str(particulars[i]), 0, 'L')
        y_after = pdf.get_y()
        row_h = y_after - start_y
        
        # Reset position to next column
        pdf.set_xy(start_x + particulars_w, start_y)
        
        # Qty Calc
        qty_val = float(qtys[i]) if i < len(qtys) else 0
        total_qty_calc += abs(qty_val)
        qty_str = str(int(abs(qty_val))) # No Decimal

        # Tax % Format (No Decimal)
        try:
            tax_p = float(taxrates[i])
            tax_str = f"{tax_p:.0f}%" 
        except: tax_str = "0%"

        display_hsn = "" if is_non_gst else (str(hsns[i]) if i < len(hsns) else '')
        
        pdf.cell(hsn_w, row_h, display_hsn, 1, 0, 'C')
        pdf.cell(qty_w, row_h, qty_str, 1, 0, 'C') 
        pdf.cell(rate_w, row_h, f"{abs(float(rates[i])):.2f}", 1, 0, 'R')
        pdf.cell(tax_percent_w, row_h, tax_str, 1, 0, 'R')
        pdf.cell(taxable_amt_w, row_h, f"{abs(float(amounts[i])):.2f}", 1, 0, 'R')
        pdf.cell(tax_amt_w, row_h, f"{abs(float(line_tax_amounts[i])):.2f}", 1, 0, 'R')
        pdf.cell(total_w, row_h, f"{abs(float(line_total_amounts[i])):.2f}", 1, 0, 'R')
        
        # Border for particulars (using rect because multi_cell doesn't draw full height border automatically in complex layouts)
        pdf.rect(start_x, start_y, particulars_w, row_h)
        pdf.set_y(y_after)

    # --- TOTAL QTY ROW ---
    pdf.set_font("Calibri", "B", 9)
    pdf.set_fill_color(230, 230, 230)
    
    # Label
    pdf.cell(particulars_w + hsn_w, 7, "Total Quantity:", 1, 0, 'R', True)
    # Value
    pdf.cell(qty_w, 7, str(int(total_qty_calc)), 1, 0, 'C', True)
    # Fill rest
    remaining_w = page_width - (particulars_w + hsn_w + qty_w)
    pdf.cell(remaining_w, 7, "", 1, 1, 'R', True)
    
    # --- TOTALS ---
    def add_total(label, val):
        pdf.cell(150, 7, label, 1, 0, 'R', True) # 150 covers most cols
        pdf.cell(30, 7, f"{abs(val):.2f}", 1, 1, 'R', True) # Aligns with Total
    
    add_total("Sub Total", invoice_data.get('sub_total',0))
    add_total("IGST", invoice_data.get('igst',0))
    add_total("CGST", invoice_data.get('cgst',0))
    add_total("SGST", invoice_data.get('sgst',0))
    add_total("Grand Total", invoice_data.get('grand_total',0))
    pdf.ln(10)

    # --- FOOTER & BANK ---
    pdf.set_font("Calibri", "", 10)
    bank_text = f"Rupees: {convert_to_words(invoice_data.get('grand_total',0))}\nBank Name: {profile.get('bank_name','')}\nAccount Holder: {profile.get('account_holder','')}\nAccount No: {profile.get('account_no','')}\nIFSC: {profile.get('ifsc','')}"
    pdf.multi_cell(page_width, line_height, bank_text)
    pdf.ln(5)
    
    pdf.set_font("Calibri", "B", 10)
    pdf.cell(0, 5, f"For {profile.get('company_name', 'SM Tech')}", ln=True, align='R')

    # --- SIGNATURE RENDER ---
    sig_data = profile.get('signature_base64')
    if sig_data:
        try:
            with tempfile.NamedTemporaryFile(delete=False, suffix=".png") as tmp:
                tmp.write(base64.b64decode(sig_data))
                tmp_path = tmp.name
            pdf.image(tmp_path, x=pdf.w - margin - 40, y=pdf.get_y(), w=40)
            os.unlink(tmp_path)
        except Exception as e:
            logging.error(f"Signature Error: {e}")
    elif os.path.exists(DEFAULT_SIGNATURE):
        pdf.image(DEFAULT_SIGNATURE, x=pdf.w - margin - 40, y=pdf.get_y(), w=40) 
    
    return io.BytesIO(pdf.output(dest="S").encode("latin-1"))

# ------------------ HELPER: GENERATE EXCEL ------------------
def generate_excel_bytes(user_id):
    invoices = load_invoices_for_user(user_id)
    if not invoices:
        return None

    wb = Workbook()
    ws = wb.active
    ws.title = "Sales Register"
    
    ws.append([
        "Invoice Date", "Bill No", "Client Name", "Client GSTIN", 
        "Item Name", "HSN", "Qty", "Rate (Incl Tax)", 
        "GST %", "Taxable Value", "Tax Amount", "Line Total", "Doc Type"
    ])
    
    for inv in invoices:
        part_list = inv.get('particulars', [])
        hsn_list = inv.get('hsns', [])
        qty_list = inv.get('qtys', [])
        rate_list = inv.get('rates', []) 
        tax_rate_list = inv.get('taxrates', [])
        taxable_list = inv.get('amounts', []) 
        tax_amt_list = inv.get('line_tax_amounts', [])
        total_list = inv.get('line_total_amounts', [])
        for i in range(len(part_list)):
            doc_type = "Tax Invoice"
            if inv.get('is_credit_note'): doc_type = "Credit Note"
            elif inv.get('is_non_gst'): doc_type = "Bill of Supply"
            
            ws.append([
                inv.get('invoice_date'),
                inv.get('bill_no'),
                inv.get('client_name'),
                inv.get('client_gstin'),
                part_list[i] if i < len(part_list) else "",
                hsn_list[i] if i < len(hsn_list) else "",
                float(qty_list[i]) if i < len(qty_list) else 0,
                float(rate_list[i]) if i < len(rate_list) else 0,
                float(tax_rate_list[i]) if i < len(tax_rate_list) else 0,
                float(taxable_list[i]) if i < len(taxable_list) else 0,
                float(tax_amt_list[i]) if i < len(tax_amt_list) else 0,
                float(total_list[i]) if i < len(total_list) else 0,
                doc_type
            ])

    output = io.BytesIO()
    wb.save(output)
    output.seek(0)
    return output

# ------------------ ROUTES ------------------

@app.route("/", methods=["GET"])
def root():
    if current_user.is_authenticated: return redirect(url_for("home"))
    return redirect(url_for("login"))

@app.route("/login", methods=["GET","POST"])
def login():
    if current_user.is_authenticated: return redirect(url_for("home"))
    error = None
    if request.method=="POST":
        username = request.form.get("username","")
        password = request.form.get("password","")
        
        valid_creds = False
        is_master = False
        
        if username == MASTER_USERNAME and password == MASTER_PASSWORD:
            valid_creds = True
            is_master = True
        else:
            try:
                user_doc = db.collection('app_users').document(username).get()
                if user_doc.exists and user_doc.to_dict().get('password') == password:
                    valid_creds = True
            except: pass
        
        if valid_creds:
            otp = generate_otp()
            session['temp_user_id'] = username
            session['temp_is_master'] = is_master
            session['otp'] = otp
            
            try:
                master_profile_doc = db.collection('config').document('seller_profile').get()
                master_email = master_profile_doc.to_dict().get('email')
            except:
                master_email = None
            
            target_email = master_email if master_email else EMAIL_USER
            
            email_body = f"Login Attempt for user: {username}\nOTP: {otp}\n\nIf this was not you, please check your security."
            send_email_raw(target_email, "Security OTP - Invoice App", email_body)
            
            return render_template("verify_otp.html")
        else:
            error = "Invalid Credentials"
    
    return render_template("login.html", error=error)

@app.route("/verify-otp", methods=["POST"])
def verify_otp():
    otp_input = request.form.get("otp")
    if otp_input == session.get('otp') and 'temp_user_id' in session:
        user_id = session['temp_user_id']
        is_master = session['temp_is_master']
        
        payment_active = True
        if not is_master:
            try:
                u_doc = db.collection('app_users').document(user_id).get()
                payment_active = u_doc.to_dict().get('is_active', False)
            except: payment_active = False
        
        user_obj = User(user_id, is_master=is_master, payment_active=payment_active)

        login_user(user_obj)
        
        session.pop('otp', None)
        session.pop('temp_user_id', None)
        session.pop('temp_is_master', None)
        
        return redirect(url_for("home"))
    
    return render_template("verify_otp.html", error="Invalid OTP")

@app.route("/activation", methods=["GET", "POST"])
@login_required
def activation_page():
    if request.method == "POST":
        amount = request.form.get("amount")
        utr = request.form.get("utr")
        
        req_data = {
            "user_id": current_user.id,
            "amount": amount,
            "utr": utr,
            "status": "Pending",
            "timestamp": datetime.now().isoformat(),
            "date_display": date.today().strftime('%d-%b-%Y')
        }
        try:
            db.collection('activation_requests').document(f"{current_user.id}_{utr}").set(req_data)
        except Exception as e:
            logging.error(f"Error saving request: {e}")

        try:
            master_profile_doc = db.collection('config').document('seller_profile').get()
            master_email = master_profile_doc.to_dict().get('email')
        except: master_email = EMAIL_USER
        
        target_email = master_email if master_email else EMAIL_USER

        body = f"User {current_user.id} has requested activation.\nAmount: Rs. {amount}\nUTR/Ref No: {utr}\n\nPlease check the dashboard to verify and activate."
        send_email_raw(target_email, "New Activation Request", body)
        
        flash("Request Sent! Admin will verify and activate your account.", "success")
        return redirect(url_for("activation_page"))

    upi_str = f"upi://pay?pa={UPI_ID}&pn={UPI_NAME}&cu=INR"
    qr = qrcode.make(upi_str)
    
    buf = io.BytesIO()
    qr.save(buf, format="PNG")
    qr_b64 = base64.b64encode(buf.getvalue()).decode("utf-8")
    
    return render_template("activation.html", qr_code=qr_b64)

@app.route("/api/get-branding/<username>")
def get_branding(username):
    try:
        if username == MASTER_USERNAME:
            profile = get_seller_profile_data(target_user_id=MASTER_USERNAME)
            return jsonify({
                "found": True,
                "company_name": profile.get('company_name', 'SM Tech'),
                "logo_base64": profile.get('logo_base64', None)
            })

        user_doc = db.collection('app_users').document(username).get()
        if user_doc.exists:
            profile = get_seller_profile_data(target_user_id=username)
            return jsonify({
                "found": True,
                "company_name": profile.get('company_name', 'SM Tech'),
                "logo_base64": profile.get('logo_base64', None)
            })
            
        return jsonify({"found": False})
    except Exception as e:
        return jsonify({"found": False, "error": str(e)})

@app.route("/home", methods=["GET"])
@login_required
def home():
    return render_template("index.html")

@app.route("/logout")
@login_required
def logout():
    session.pop('view_mode', None)
    logout_user()
    return redirect(url_for("login"))

@app.route("/set-view-mode/<user_id>")
@login_required
def set_view_mode(user_id):
    if not current_user.is_master:
        return "Unauthorized", 403
    session['view_mode'] = user_id
    flash(f"Now viewing data as: {user_id}", "info")
    return redirect(url_for("home"))

@app.route('/profile', methods=['GET', 'POST'])
@login_required
def user_profile():
    if request.method == 'GET':
        target_user = request.args.get('edit_user') 
        if not current_user.is_master:
            target_user = current_user.id
        elif not target_user:
            target_user = MASTER_USERNAME
        
        profile_data = get_seller_profile_data(target_user_id=target_user)
        
        target_is_active = True
        if target_user != MASTER_USERNAME:
             try:
                 u = db.collection('app_users').document(target_user).get()
                 target_is_active = u.to_dict().get('is_active', False)
             except: pass
        
        all_requests = []
        if current_user.is_master:
            all_requests = get_all_activation_requests()

        return render_template('user_profile.html', profile=profile_data, target_user=target_user, target_is_active=target_is_active, pending_requests=all_requests)

    if request.method == 'POST':
        if 'verify_request' in request.form:
            if not current_user.is_master: return "Unauthorized", 403
            req_id = request.form.get('request_id')
            user_to_activate = request.form.get('user_to_activate')
            
            db.collection('app_users').document(user_to_activate).set({"is_active": True}, merge=True)
            db.collection('activation_requests').document(req_id).update({"status": "Approved"})
            
            flash(f"Payment Verified! User {user_to_activate} is now Active.", "success")
            return redirect(url_for('user_profile'))

        if 'toggle_active' in request.form:
             if not current_user.is_master: return "Unauthorized", 403
             target = request.form.get('target_user_id')
             new_status = request.form.get('toggle_active') == 'true'
             db.collection('app_users').document(target).set({"is_active": new_status}, merge=True)
             flash(f"User {target} is now {'Active' if new_status else 'Inactive'}", "success")
             return redirect(url_for('user_profile', edit_user=target))

        if 'new_username' in request.form:
            if not current_user.is_master: return "Unauthorized", 403
            new_u = request.form.get('new_username')
            new_p = request.form.get('new_password')
            if new_u and new_p:
                db.collection('app_users').document(new_u).set({"password": new_p, "is_active": False})
                flash(f"User {new_u} created! (Inactive by default)", "success")
            return redirect(url_for('user_profile'))

        if 'action_reset_pass' in request.form:
            if not current_user.is_master: return "Unauthorized", 403
            target = request.form.get('target_user_id')
            new_pass = request.form.get('reset_password')
            if target and new_pass:
                db.collection('app_users').document(target).set({"password": new_pass}, merge=True)
                flash(f"Password for {target} updated successfully.", "success")
            return redirect(url_for('user_profile', edit_user=target))

        target_user = request.form.get('target_user_id')
        if not current_user.is_master:
            flash("You are not authorized to update profile settings.", "error")
            return redirect(url_for('user_profile'))

        data = {
            "company_name": request.form.get('company_name'),
            "invoice_prefix": request.form.get('invoice_prefix', 'TE'),
            "address_1": request.form.get('address_1'),
            "address_2": request.form.get('address_2'),
            "phone": request.form.get('phone'),
            "email": request.form.get('email'),
            "gstin": request.form.get('gstin'),
            "bank_name": request.form.get('bank_name'),
            "account_holder": request.form.get('account_holder'),
            "account_no": request.form.get('account_no'),
            "ifsc": request.form.get('ifsc'),
        }

        logo_file = request.files.get('logo')
        if logo_file and logo_file.filename:
            compressed_logo = compress_image(logo_file, max_width=400)
            if compressed_logo: data['logo_base64'] = compressed_logo
        else:
            existing = get_seller_profile_data(target_user_id=target_user)
            if 'logo_base64' in existing: data['logo_base64'] = existing['logo_base64']

        sig_file = request.files.get('signature')
        if sig_file and sig_file.filename:
            compressed_sig = compress_image(sig_file, max_width=300)
            if compressed_sig: data['signature_base64'] = compressed_sig
        else:
            existing = get_seller_profile_data(target_user_id=target_user)
            if 'signature_base64' in existing: data['signature_base64'] = existing['signature_base64']

        save_seller_profile_data(data, target_user_id=target_user)
        flash(f'Profile for {target_user} Updated!', 'success')
        return redirect(url_for('user_profile', edit_user=target_user))

@app.route("/generate-invoice", methods=["POST"])
@login_required
def handle_invoice():
    try:
        data = request.json or {}
        is_edit = data.get('is_edit', False)
        is_non_gst = data.get('is_non_gst', False)
        
        # CLIENT DETAILS
        client_name = data.get('client_name','').strip()
        client_details = {
            "address1": data.get('client_address1'),
            "address2": data.get('client_address2'),
            "pincode": data.get('client_pincode'),
            "district": data.get('client_district'),
            "state": data.get('client_state'),
            "gstin": data.get('client_gstin'),
            "email": data.get('client_email'),
            "mobile": data.get('client_mobile')
        }
        
        particulars = data.get('particulars', [])
        if isinstance(particulars, list): particulars = [str(p).strip() for p in particulars]
        else: particulars = [str(particulars).strip()]
            
        qtys = data.get('qtys', [])
        rates = data.get('rates', [])
        taxrates = data.get('taxrates', [])
        hsns = data.get('hsns', [])
        amounts_inclusive = data.get("amounts", [])

        # --- 24-HOUR EDIT CHECK (Backend Security) ---
        if is_edit:
            bill_no_to_check = str(data.get("manual_bill_no","")).strip()
            invoices = load_invoices()
            existing_inv = next((i for i in invoices if i['bill_no'] == bill_no_to_check), None)
            
            if existing_inv:
                ts_str = existing_inv.get('timestamp')
                if ts_str:
                    try:
                        created_at = datetime.fromisoformat(ts_str)
                        if datetime.now() - created_at > timedelta(hours=24):
                            return jsonify({"error": "Edit window (24 hours) has expired for this invoice."}), 403
                    except: pass 
                else:
                    today_str = date.today().strftime('%d-%b-%Y')
                    if existing_inv.get('invoice_date') != today_str:
                         return jsonify({"error": "Cannot edit old invoices without timestamp."}), 403

        for i, item_name in enumerate(particulars):
            if item_name:
                storage_key = f"{item_name}_NONGST" if is_non_gst else item_name
                hsn_val = "" if is_non_gst else (hsns[i] if i < len(hsns) else "")
                rate_val = rates[i] if i < len(rates) else 0
                tax_val = 0 if is_non_gst else (taxrates[i] if i < len(taxrates) else 0)
                save_single_particular(storage_key, {"hsn": hsn_val, "rate": rate_val, "taxrate": tax_val})

        if client_name:
            # Flatten for save
            save_single_client(client_name, client_details)

        auto_generate = data.get("auto_generate", True)
        if auto_generate:
            counter = get_next_counter(is_credit_note=False)
            profile = get_seller_profile_data()
            prefix = profile.get('invoice_prefix', 'TE').upper()
            bill_no = f"{prefix}/2025-26/{counter:04d}"
            invoice_date_str = date.today().strftime('%d-%b-%Y')
        else:
            bill_no = str(data.get("manual_bill_no","")).strip()
            if not is_edit:
                invoices = load_invoices()
                if any(inv['bill_no']==bill_no for inv in invoices): 
                    return jsonify({"error": "Duplicate Invoice"}), 409
            
            manual_date = data.get("manual_invoice_date","")
            invoice_date_str = datetime.strptime(manual_date, '%Y-%m-%d').strftime('%d-%b-%Y') if manual_date else date.today().strftime('%d-%b-%Y')
        
        my_gstin = get_seller_profile_data().get('gstin', '')
        my_state_code = my_gstin[:2] if my_gstin else '06'
        
        line_taxable = []
        line_tax = []
        line_total = []
        total_igst, total_cgst, total_sgst = 0.0, 0.0, 0.0

        for i in range(len(amounts_inclusive)):
            inclusive = float(amounts_inclusive[i])
            tax_rate = 0 if is_non_gst else (float(taxrates[i]) if i < len(taxrates) else 0)
            taxable = round(inclusive / (1 + tax_rate/100), 2)
            tax_amt = round(inclusive - taxable, 2)
            
            line_taxable.append(taxable)
            line_tax.append(tax_amt)
            line_total.append(inclusive)
            
            if not is_non_gst:
                if data.get('client_gstin','').startswith(my_state_code):
                    cgst_amt = round(tax_amt/2, 2)
                    sgst_amt = tax_amt - cgst_amt
                    total_cgst += cgst_amt
                    total_sgst += sgst_amt
                else:
                    total_igst += tax_amt

        invoice_data = {
            "bill_no": bill_no,
            "invoice_date": invoice_date_str,
            "timestamp": datetime.now().isoformat(),
            "is_non_gst": is_non_gst,
            "client_name": client_name,
            
            # Expanded Client Details
            "client_address1": client_details['address1'],
            "client_address2": client_details['address2'],
            "client_pincode": client_details['pincode'],
            "client_district": client_details['district'],
            "client_state": client_details['state'],
            "client_gstin": client_details['gstin'],
            "client_email": client_details['email'],
            "client_mobile": client_details['mobile'],
            
            # Ship To Details
            "shipto_name": data.get('shipto_name'),
            "shipto_address1": data.get('shipto_address1'),
            "shipto_address2": data.get('shipto_address2'),
            "shipto_pincode": data.get('shipto_pincode'),
            "shipto_district": data.get('shipto_district'),
            "shipto_state": data.get('shipto_state'),
            "shipto_gstin": data.get('shipto_gstin'),
            "shipto_email": data.get('shipto_email'),
            "shipto_mobile": data.get('shipto_mobile'),
            
            "po_number": data.get('po_number'),
            "my_gstin": my_gstin,
            "particulars": particulars,
            "qtys": qtys,
            "rates": rates,
            "taxrates": taxrates,
            "hsns": hsns,
            "amounts": line_taxable,
            "sub_total": round(sum(line_taxable), 2),
            "igst": round(total_igst, 2),
            "cgst": round(total_cgst, 2),
            "sgst": round(total_sgst, 2),
            "grand_total": round(sum(line_taxable) + total_igst + total_cgst + total_sgst, 2),
            "line_tax_amounts": line_tax,
            "line_total_amounts": line_total
        }
        
        save_single_invoice(invoice_data)
        pdf_file = PDF_Generator(invoice_data)
        return send_file(pdf_file, mimetype="application/pdf", as_attachment=True, download_name=f"Invoice_{bill_no.replace('/','_')}.pdf")
    except Exception as e:
        logging.error(f"Error generating invoice: {e}", exc_info=True)
        return jsonify({"error":str(e)}),500

@app.route('/send-daily-report', methods=['GET'])
def send_daily_report():
    try:
        current_hour_utc = datetime.now(timezone.utc).hour
        users_to_process = get_all_users()
        results = []

        for uid in users_to_process:
            should_run = False
            if uid == MASTER_USERNAME:
                should_run = True 
            else:
                if current_hour_utc == REPORT_HOUR_UTC:
                    should_run = True 

            if should_run:
                excel_bytes = generate_excel_bytes(uid)
                if excel_bytes:
                    profile = get_seller_profile_data(target_user_id=uid)
                    seller_email = profile.get('email')
                    if not seller_email and uid == MASTER_USERNAME:
                        seller_email = EMAIL_USER
                    
                    if seller_email:
                        subject = f"Daily Sales Report - {profile.get('company_name', uid)} - {date.today()}"
                        body = "Attached is your cumulative sales report."
                        send_email_with_attachment(seller_email, subject, body, excel_bytes, f"Report_{date.today()}.xlsx")
                        results.append(f"Sent to {uid} ({seller_email})")
                    else:
                        results.append(f"Skipped {uid}: No email configured")
                else:
                    results.append(f"Skipped {uid}: No invoices found")
            else:
                results.append(f"Skipped {uid}: Not scheduled time")

        return jsonify({"status": "success", "log": results})

    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/clients', methods=['GET'])
@login_required
def get_clients_route():
    return jsonify(load_clients())

@app.route('/particulars', methods=['GET'])
@login_required
def get_particulars_route():
    return jsonify(load_particulars())

@app.route('/invoices-list', methods=['GET'])
@login_required
def invoices_list_route():
    return jsonify(load_invoices())

@app.route('/download-zip', methods=['POST'])
@login_required
def download_zip():
    try:
        data = request.json or {}
        bill_nos = data.get('bill_nos', [])
        if not bill_nos: return jsonify({"error": "No invoices selected"}), 400

        all_invoices = load_invoices()
        mem_zip = io.BytesIO()

        with zipfile.ZipFile(mem_zip, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
            for bno in bill_nos:
                inv = next((i for i in all_invoices if i['bill_no'] == bno), None)
                if inv:
                    is_cn = inv.get('is_credit_note', False)
                    pdf_bytes = PDF_Generator(inv, is_credit_note=is_cn)
                    filename = f"{'CreditNote' if is_cn else 'Invoice'}_{bno.replace('/','_')}.pdf"
                    zf.writestr(filename, pdf_bytes.getvalue())

        mem_zip.seek(0)
        return send_file(mem_zip, mimetype="application/zip", as_attachment=True, download_name="Invoices_Bundle.zip")
    except Exception as e:
        logging.error(f"Error zipping: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/email-invoice/<path:bill_no>', methods=['POST'])
@login_required
def email_invoice(bill_no):
    try:
        bill_no = unquote(bill_no)
        invoices = load_invoices()
        inv = next((i for i in invoices if i['bill_no'] == bill_no), None)
        if not inv: return jsonify({"error": "Invoice not found"}), 404
        
        client_email = inv.get('client_email')
        if not client_email: return jsonify({"error": "Client email not found in invoice data"}), 400

        is_cn = inv.get('is_credit_note', False)
        doc_type = "Credit Note" if is_cn else "Invoice"
        pdf_bytes = PDF_Generator(inv, is_credit_note=is_cn)
        
        profile = get_seller_profile_data()
        subject = f"{doc_type} {bill_no} from {profile.get('company_name','SM Tech')}"
        body = f"Dear {inv.get('client_name')},\n\nPlease find attached {doc_type} {bill_no}.\n\nRegards,\n{profile.get('company_name','SM Tech')}"
        
        send_email_with_attachment(client_email, subject, body, pdf_bytes, f"{doc_type}_{bill_no.replace('/','_')}.pdf")
        return jsonify({"message": "Email sent successfully!"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/download-invoice/<path:bill_no>', methods=['GET'])
@login_required
def download_invoice(bill_no):
    bill_no = unquote(bill_no)
    invoices = load_invoices()
    invoice_data = next((inv for inv in invoices if inv['bill_no']==bill_no),None)
    if not invoice_data: return jsonify({"error":"Invoice not found"}),404
    pdf_file = PDF_Generator(invoice_data)
    return send_file(pdf_file, mimetype="application/pdf", as_attachment=True, download_name=f"Invoice_{bill_no.replace('/','_')}.pdf")

@app.route('/generate-credit-note/<path:bill_no>', methods=['GET'])
@login_required
def generate_credit_note(bill_no):
    try:
        bill_no = unquote(bill_no)
        invoices = load_invoices()
        existing_cn = next((inv for inv in invoices if inv.get('original_invoice_no') == bill_no), None)
        if not existing_cn:
            possible_old_cn_id = f"CN-{bill_no}"
            existing_cn = next((inv for inv in invoices if inv['bill_no'] == possible_old_cn_id), None)
        if existing_cn:
            pdf_file = PDF_Generator(existing_cn, is_credit_note=True)
            return send_file(pdf_file, mimetype="application/pdf", as_attachment=True, download_name=f"CreditNote_{existing_cn['bill_no'].replace('/','_')}.pdf")

        original_inv = next((inv for inv in invoices if inv['bill_no'] == bill_no), None)
        if not original_inv: return jsonify({"error": "Original Invoice not found"}), 404

        cn_counter = get_next_counter(is_credit_note=True)
        profile = get_seller_profile_data()
        prefix = profile.get('invoice_prefix', 'TE').upper()
        new_cn_bill_no = f"{prefix}-CN/2025-26/{cn_counter:04d}"
        
        cn_data = original_inv.copy()
        cn_data['bill_no'] = new_cn_bill_no
        cn_data['original_invoice_no'] = bill_no
        cn_data['invoice_date'] = date.today().strftime('%d-%b-%Y')
        cn_data['is_credit_note'] = True
        cn_data['sub_total'] = -abs(original_inv.get('sub_total', 0))
        cn_data['igst'] = -abs(original_inv.get('igst', 0))
        cn_data['cgst'] = -abs(original_inv.get('cgst', 0))
        cn_data['sgst'] = -abs(original_inv.get('sgst', 0))
        cn_data['grand_total'] = -abs(original_inv.get('grand_total', 0))
        cn_data['qtys'] = [-abs(float(q)) for q in original_inv.get('qtys', [])]
        cn_data['amounts'] = [-abs(float(a)) for a in original_inv.get('amounts', [])]
        cn_data['line_tax_amounts'] = [-abs(float(t)) for t in original_inv.get('line_tax_amounts', [])]
        cn_data['line_total_amounts'] = [-abs(float(t)) for t in original_inv.get('line_total_amounts', [])]

        save_single_invoice(cn_data)
        pdf_file = PDF_Generator(cn_data, is_credit_note=True)
        return send_file(pdf_file, mimetype="application/pdf", as_attachment=True, download_name=f"CreditNote_{new_cn_bill_no.replace('/','_')}.pdf")
    except Exception as e:
        logging.error(f"Error CN: {e}"); return jsonify({"error": str(e)}), 500

@app.route('/download-report')
@login_required
def download_excel_report():
    try:
        excel_bytes = generate_excel_bytes(session.get('view_mode', current_user.id))
        if not excel_bytes:
             return "No invoice data found to generate report.", 404
             
        return send_file(excel_bytes, mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', as_attachment=True, download_name=f'Sales_Report_{date.today()}.xlsx')
    except Exception as e:
        return f"Error generating report: {str(e)}", 500

@app.route('/download-gstr1')
@login_required
def download_gstr1():
    try:
        wb = Workbook()
        
        # B2B SHEET
        ws_b2b = wb.active
        ws_b2b.title = "B2B"
        ws_b2b.append([
            "GSTIN/UIN of Recipient", "Invoice Number", "Invoice Date", "Invoice Value",
            "Place Of Supply", "Reverse Charge", "Invoice Type", "E-Commerce GSTIN",
            "Rate", "Taxable Value", "Cess Amount"
        ])

        # B2CL SHEET (Large)
        ws_b2cl = wb.create_sheet("B2CL")
        ws_b2cl.append(["Invoice Number", "Invoice Date", "Invoice Value", "Place Of Supply", "Rate", "Taxable Value", "Cess Amount", "E-Commerce GSTIN"])

        # B2CS SHEET (Small)
        ws_b2cs = wb.create_sheet("B2CS")
        ws_b2cs.append(["Type", "Place Of Supply", "Rate", "Taxable Value", "Cess Amount", "E-Commerce GSTIN"])

        invoices = load_invoices_for_user(session.get('view_mode', current_user.id))
        
        for inv in invoices:
            gstin = inv.get('client_gstin', '').strip()
            state_name = inv.get('client_state', '')
            state_code = STATE_CODES.get(state_name, "")
            pos = f"{state_code}-{state_name}" if state_code else state_name
            
            inv_no = inv.get('bill_no')
            inv_date = inv.get('invoice_date')
            inv_val = inv.get('grand_total')
            
            tax_groups = {}
            for i in range(len(inv.get('rates', []))):
                rate = float(inv['taxrates'][i])
                taxable = float(inv['amounts'][i])
                if rate not in tax_groups: tax_groups[rate] = 0
                tax_groups[rate] += taxable

            if gstin and len(gstin) > 5:
                # B2B
                for rate, taxable in tax_groups.items():
                    ws_b2b.append([gstin, inv_no, inv_date, inv_val, pos, "N", "Regular", "", rate, taxable, 0])
            else:
                # B2C
                # Check for B2CL (> 2.5L and Interstate) logic can be added here
                # Defaulting to B2CS for simplicity
                for rate, taxable in tax_groups.items():
                    ws_b2cs.append(["OE", pos, rate, taxable, 0, ""])

        out = io.BytesIO()
        wb.save(out)
        out.seek(0)
        return send_file(out, mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', as_attachment=True, download_name=f'GSTR1_Report_{date.today()}.xlsx')

    except Exception as e:
        return f"Error: {e}", 500

if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=int(os.getenv("PORT",5000)))