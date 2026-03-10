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
from google.cloud.firestore import Transaction  # REQUIRED FOR STRICT INVENTORY ERP

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
    def __init__(self, id, is_master=False, payment_active=True, permissions=None):
        self.id = id
        self.is_master = is_master
        self.payment_active = payment_active
        # Default to both if not specified
        self.permissions = permissions if permissions else ['sale', 'purchase']

    @property
    def is_active(self):
        return True
    
    def has_permission(self, perm):
        if self.is_master: return True
        return perm in self.permissions

@login_manager.user_loader
def load_user(user_id):
    if user_id == MASTER_USERNAME:
        return User(user_id, is_master=True, payment_active=True, permissions=['sale', 'purchase'])
    
    try:
        user_doc = db.collection('app_users').document(user_id).get()
        if user_doc.exists:
            data = user_doc.to_dict()
            db_active_status = data.get('is_active', False)
            perms = data.get('permissions', ['sale', 'purchase'])
            return User(user_id, is_master=False, payment_active=db_active_status, permissions=perms)
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

def compress_image(file_storage, max_width=400):
    try:
        img = Image.open(file_storage)
        width_percent = (max_width / float(img.size[0]))
        if width_percent < 1:
            new_height = int((float(img.size[1]) * float(width_percent)))
            img = img.resize((max_width, new_height), Image.Resampling.LANCZOS)
        
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

# --- COLLECTION SEPARATION LOGIC ---
def get_collection_name(data):
    cat = data.get('doc_category', 'sale')
    dtype = data.get('doc_type', 'invoice')
    is_cn = data.get('is_credit_note', False)
    is_dn = data.get('is_debit_note', False)
    
    if cat == 'purchase':
        if is_dn: return 'purchase_debit_notes'
        if dtype == 'po': return 'purchase_orders'
        if dtype == 'grn': return 'purchase_grns'
        if dtype == 'bill': return 'purchase_bills'
        return 'purchase_misc'
    else:
        if is_cn: return 'sales_credit_notes'
        if is_dn: return 'sales_debit_notes'
        return 'sales_invoices'

def load_invoices_for_user(target_user_id):
    base = get_db_base(target_user=target_user_id)
    collections = [
        'sales_invoices', 'sales_credit_notes', 'sales_debit_notes', 
        'purchase_orders', 'purchase_grns', 'purchase_bills', 'purchase_debit_notes', 
        'invoices' # Legacy support
    ]
    all_docs = []
    for c_name in collections:
        try:
            docs = base.collection(c_name).stream()
            for d in docs:
                all_docs.append(d.to_dict())
        except: pass
    return all_docs

def load_invoices():
    base = get_db_base()
    collections = [
        'sales_invoices', 'sales_credit_notes', 'sales_debit_notes', 
        'purchase_orders', 'purchase_grns', 'purchase_bills', 'purchase_debit_notes', 
        'invoices'
    ]
    all_docs = []
    for c_name in collections:
        try:
            docs = base.collection(c_name).stream()
            for d in docs:
                all_docs.append(d.to_dict())
        except: pass
    return all_docs

def save_single_invoice(invoice_data):
    base = get_db_base()
    collection_name = get_collection_name(invoice_data)
    doc_id = invoice_data['bill_no'].replace('/', '_')
    base.collection(collection_name).document(doc_id).set(invoice_data)

def load_particulars():
    base = get_db_base()
    docs = base.collection('particulars').stream()
    return {doc.id: doc.to_dict() for doc in docs}

def save_single_particular(name, data):
    base = get_db_base()
    base.collection('particulars').document(name).set(data, merge=True)

def get_next_counter(is_credit_note=False, is_debit_note=False, is_purchase=False, doc_type='invoice'):
    base = get_db_base()
    doc_ref = base.collection('config').document('counters')
    @firestore.transactional
    def update_in_transaction(transaction, doc_ref):
        snapshot = doc_ref.get(transaction=transaction)
        if not snapshot.exists:
            new_data = {"counter": 0, "cn_counter": 0, "dn_counter": 0, "po_counter": 0, "grn_counter": 0, "pb_counter": 0, "pdn_counter": 0}
            transaction.set(doc_ref, new_data)
            current_val = 0
            current_data = new_data
        else:
            current_data = snapshot.to_dict()

        if is_purchase:
            if is_debit_note: field = "pdn_counter"
            elif doc_type == 'po': field = "po_counter"
            elif doc_type == 'grn': field = "grn_counter"
            elif doc_type == 'bill': field = "pb_counter"
            else: field = "po_counter" 
        else:
            if is_credit_note: field = "cn_counter"
            elif is_debit_note: field = "dn_counter"
            else: field = "counter" 
            
        current_val = current_data.get(field, 0)
        new_val = current_val + 1
        
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

def PDF_Generator(invoice_data, is_credit_note=False, is_debit_note=False):
    pdf = FPDF()
    pdf.add_page()
    pdf.add_font("Calibri", "", CALIBRI_FONT_PATH, uni=True)
    pdf.add_font("Calibri", "B", CALIBRI_FONT_PATH, uni=True)

    profile = get_seller_profile_data()
    
    margin = 15
    page_width = pdf.w - 2 * margin 
    col_width = (page_width / 2) - 5 
    line_height = 5 
    
    # --- LOGO HANDLING ---
    logo_data = profile.get('logo_base64')
    if logo_data:
        try:
            if "," in logo_data:
                logo_data = logo_data.split(",")[1]
            
            img_bytes = base64.b64decode(logo_data)
            with tempfile.NamedTemporaryFile(delete=False, suffix=".png") as tmp:
                with Image.open(io.BytesIO(img_bytes)) as pil_img:
                    pil_img.save(tmp, format="PNG")
                tmp_path = tmp.name
            
            pdf.image(tmp_path, x=15, y=8, w=30)
            os.unlink(tmp_path)
        except Exception as e:
            logging.error(f"Logo Error: {e}")
            if os.path.exists(DEFAULT_LOGO):
                pdf.image(DEFAULT_LOGO, x=15, y=8, w=30)
    elif os.path.exists(DEFAULT_LOGO):
        pdf.image(DEFAULT_LOGO, x=15, y=8, w=30)
    
    pdf.set_font("Calibri", "B", 22)
    is_non_gst = invoice_data.get('is_non_gst', False)
    
    doc_category = invoice_data.get('doc_category', 'sale')
    doc_type = invoice_data.get('doc_type', 'invoice')

    # --- DYNAMIC HEADERS ---
    if doc_category == 'purchase':
        if is_debit_note:
            pdf.set_text_color(0, 51, 102) 
            header_title = "DEBIT NOTE (PURCHASE)"
        elif doc_type == 'po':
            pdf.set_text_color(0, 51, 102) 
            header_title = "PURCHASE ORDER"
        elif doc_type == 'grn':
            pdf.set_text_color(0, 100, 0) 
            header_title = "GOODS RECEIPT NOTE"
        elif doc_type == 'bill':
            pdf.set_text_color(50, 50, 50) 
            header_title = "PURCHASE BILL"
        else:
            header_title = "PURCHASE DOC"
    else:
        if is_credit_note:
            pdf.set_text_color(220, 38, 38) 
            header_title = "CREDIT NOTE"
        elif is_debit_note:
            pdf.set_text_color(0, 51, 102)
            header_title = "DEBIT NOTE"
        elif is_non_gst:
            pdf.set_text_color(0, 128, 0) 
            header_title = "BILL OF SUPPLY"
        else:
            pdf.set_text_color(255, 165, 0) 
            header_title = "TAX INVOICE"

    pdf.cell(page_width, 10, profile.get('company_name', 'SM Tech'), ln=True, align='C')
    pdf.set_font("Calibri", "B", 14)
    pdf.set_text_color(0, 0, 0)
    pdf.cell(page_width, 8, header_title, ln=True, align='C')
    pdf.set_font("Calibri", "", 10)
    
    my_gstin = profile.get('gstin', '')
    address_str = f"{profile.get('address_1','')}\n{profile.get('address_2','')}\nPhone: {profile.get('phone','')} | E-mail: {profile.get('email','')}\nGSTIN: {my_gstin}"
    pdf.multi_cell(page_width, line_height, address_str, align='C')
    pdf.ln(5)
    pdf.line(margin, pdf.get_y(), pdf.w - margin, pdf.get_y())

    if is_credit_note or is_debit_note:
        pdf.ln(3)
        pdf.set_font("Calibri", "B", 11)
        if is_credit_note: pdf.set_text_color(220, 38, 38)
        else: pdf.set_text_color(0, 51, 102)
            
        ref_bill = invoice_data.get('original_invoice_no', '')
        if not ref_bill:
             ref_bill = invoice_data.get('bill_no', '').replace('CN-', '').replace('DN-', '').replace('TE-CN', 'TE').replace('TE-DN', 'TE')
        pdf.cell(0, 7, f"Ref Invoice No: {ref_bill}", ln=True, align='C')
        pdf.set_text_color(0, 0, 0)
    
    pdf.ln(5)

    def format_address(prefix):
        addr_lines = [
            invoice_data.get(f'{prefix}_name',''),
            invoice_data.get(f'{prefix}_address1',''),
            invoice_data.get(f'{prefix}_address2',''),
            f"{invoice_data.get(f'{prefix}_district','')} - {invoice_data.get(f'{prefix}_pincode','')}" if invoice_data.get(f'{prefix}_pincode','') else invoice_data.get(f'{prefix}_district',''),
            f"{invoice_data.get(f'{prefix}_state','')}",
            f"GSTIN: {invoice_data.get(f'{prefix}_gstin','')}" if invoice_data.get(f'{prefix}_gstin','') else "",
            f"Mobile: {invoice_data.get(f'{prefix}_mobile','')}" if invoice_data.get(f'{prefix}_mobile','') else ""
        ]
        return "\n".join([line for line in addr_lines if line and line.strip() != '-' and line.strip() != ''])

    bill_to_text = format_address('client')
    ship_to_text = format_address('shipto')
    
    label_bill_to = "Bill To:"
    label_ship_to = "Ship To:"
    label_doc_no = f"{header_title} No:"
    
    if doc_category == 'purchase':
        if is_debit_note:
            label_bill_to = "To (Vendor):"
            label_ship_to = "Reference Details:"
        else:
            label_bill_to = "Vendor Details:"
            label_ship_to = "Ship To (Our Warehouse):"
    
    invoice_no_text = f"{label_doc_no} {invoice_data.get('bill_no','')}"
    invoice_date_text = f"Date: {invoice_data.get('invoice_date','')}"
    po_number_text = f"PO Number: {invoice_data.get('po_number','')}"

    y_start = pdf.get_y()
    pdf.set_font("Calibri", "B", 12)
    pdf.cell(col_width, line_height, label_bill_to, ln=True)
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
    pdf.cell(col_width, line_height, label_ship_to, ln=True)
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
    
    particulars_w, hsn_w, qty_w, rate_w, tax_percent_w, taxable_amt_w, tax_amt_w, total_w = 50, 15, 12, 20, 13, 25, 20, 25
    pdf.set_fill_color(255, 204, 153)
    if doc_category == 'purchase': pdf.set_fill_color(200, 220, 255)

    pdf.set_font("Calibri", "B", 9)
    pdf.cell(particulars_w, 8, "Particulars", 1, 0, 'L', True)
    pdf.cell(hsn_w, 8, "HSN", 1, 0, 'C', True)
    pdf.cell(qty_w, 8, "Qty", 1, 0, 'C', True)
    pdf.cell(rate_w, 8, "Rate", 1, 0, 'R', True)
    pdf.cell(tax_percent_w, 8, "Tax %", 1, 0, 'R', True)
    pdf.cell(taxable_amt_w, 8, "Taxable", 1, 0, 'R', True)
    pdf.cell(tax_amt_w, 8, "Tax Amt", 1, 0, 'R', True)
    pdf.cell(total_w, 8, "Total", 1, 1, 'R', True)

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
        pdf.multi_cell(particulars_w, 7, str(particulars[i]), 0, 'L')
        y_after = pdf.get_y()
        row_h = y_after - start_y
        pdf.set_xy(start_x + particulars_w, start_y)
        
        qty_val = float(qtys[i]) if i < len(qtys) else 0
        total_qty_calc += abs(qty_val)
        qty_str = str(int(abs(qty_val))) if qty_val.is_integer() else str(abs(qty_val))

        try:
            tax_p = float(taxrates[i])
            tax_str = f"{tax_p:.0f}%" if tax_p.is_integer() else f"{tax_p}%"
        except: tax_str = "0%"

        display_hsn = "" if is_non_gst else (str(hsns[i]) if i < len(hsns) else '')
        
        pdf.cell(hsn_w, row_h, display_hsn, 1, 0, 'C')
        pdf.cell(qty_w, row_h, qty_str, 1, 0, 'C') 
        pdf.cell(rate_w, row_h, f"{abs(float(rates[i])):.2f}", 1, 0, 'R')
        pdf.cell(tax_percent_w, row_h, tax_str, 1, 0, 'R')
        pdf.cell(taxable_amt_w, row_h, f"{abs(float(amounts[i])):.2f}", 1, 0, 'R')
        pdf.cell(tax_amt_w, row_h, f"{abs(float(line_tax_amounts[i])):.2f}", 1, 0, 'R')
        pdf.cell(total_w, row_h, f"{abs(float(line_total_amounts[i])):.2f}", 1, 0, 'R')
        
        pdf.rect(start_x, start_y, particulars_w, row_h)
        pdf.set_y(y_after)

    pdf.set_font("Calibri", "B", 9)
    pdf.set_fill_color(240, 240, 240)
    pdf.cell(particulars_w + hsn_w, 7, "Total Quantity:", 1, 0, 'R', True)
    pdf.cell(qty_w, 7, f"{total_qty_calc:g}", 1, 0, 'C', True)
    remaining_w = page_width - (particulars_w + hsn_w + qty_w)
    pdf.cell(remaining_w, 7, "", 1, 1, 'R', True)
    
    pdf.set_fill_color(230, 230, 230)
    def add_total(label, val):
        pdf.cell(135, 7, label, 1, 0, 'R', True) 
        pdf.cell(45, 7, f"{abs(val):.2f}", 1, 1, 'R', True) 
    
    add_total("Sub Total", invoice_data.get('sub_total',0))
    add_total("IGST", invoice_data.get('igst',0))
    add_total("CGST", invoice_data.get('cgst',0))
    add_total("SGST", invoice_data.get('sgst',0))
    add_total("Grand Total", invoice_data.get('grand_total',0))
    pdf.ln(10)

    pdf.set_font("Calibri", "", 10)
    bank_text = f"Rupees: {convert_to_words(invoice_data.get('grand_total',0))}\nBank Name: {profile.get('bank_name','')}\nAccount Holder: {profile.get('account_holder','')}\nAccount No: {profile.get('account_no','')}\nIFSC: {profile.get('ifsc','')}"
    pdf.multi_cell(page_width, line_height, bank_text)
    pdf.ln(5)
    
    pdf.set_font("Calibri", "B", 10)
    pdf.cell(0, 5, f"For {profile.get('company_name', 'SM Tech')}", ln=True, align='R')

    sig_data = profile.get('signature_base64')
    if sig_data:
        try:
            if "," in sig_data:
                sig_data = sig_data.split(",")[1]
            
            sig_bytes = base64.b64decode(sig_data)
            with tempfile.NamedTemporaryFile(delete=False, suffix=".png") as tmp:
                with Image.open(io.BytesIO(sig_bytes)) as pil_img:
                    pil_img.save(tmp, format="PNG")
                tmp_path = tmp.name
            
            pdf.image(tmp_path, x=pdf.w - margin - 40, y=pdf.get_y(), w=40)
            os.unlink(tmp_path)
        except Exception as e:
            logging.error(f"Signature Error: {e}")
            if os.path.exists(DEFAULT_SIGNATURE):
                pdf.image(DEFAULT_SIGNATURE, x=pdf.w - margin - 40, y=pdf.get_y(), w=40)
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
        "Invoice Date", "Bill No", "Client/Vendor Name", "GSTIN", 
        "Item Name", "HSN", "Qty", "Rate (Incl Tax)", 
        "GST %", "Taxable Value", "Tax Amount", "Line Total", "Doc Type", "Category"
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
        
        doc_cat = inv.get('doc_category', 'sale')
        d_type = inv.get('doc_type', 'invoice')
        display_type = "Tax Invoice"
        
        if doc_cat == 'purchase':
            if d_type == 'po': display_type = "Purchase Order"
            elif d_type == 'grn': display_type = "GRN"
            elif d_type == 'bill': display_type = "Purchase Bill"
            elif inv.get('is_debit_note') or d_type == 'dn': display_type = "Debit Note (Purchase)"
        else:
            if inv.get('is_credit_note') or d_type == 'cn': display_type = "Credit Note"
            elif inv.get('is_debit_note') or d_type == 'dn': display_type = "Debit Note"
            elif inv.get('is_non_gst'): display_type = "Bill of Supply"

        for i in range(len(part_list)):
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
                display_type,
                doc_cat.upper()
            ])

    output = io.BytesIO()
    wb.save(output)
    output.seek(0)
    return output

# ------------------ ROUTES ------------------

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
        perms = ['sale', 'purchase']
        if not is_master:
            try:
                u_doc = db.collection('app_users').document(user_id).get()
                payment_active = u_doc.to_dict().get('is_active', False)
                perms = u_doc.to_dict().get('permissions', ['sale', 'purchase'])
            except: payment_active = False
        
        user_obj = User(user_id, is_master=is_master, payment_active=payment_active, permissions=perms)

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

@app.route("/", methods=["GET"])
def root_redirect():
    if current_user.is_authenticated: return redirect(url_for("dashboard"))
    return redirect(url_for("login"))

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
        target_perms = ['sale', 'purchase']
        if target_user != MASTER_USERNAME:
             try:
                 u = db.collection('app_users').document(target_user).get()
                 d = u.to_dict()
                 target_is_active = d.get('is_active', False)
                 target_perms = d.get('permissions', ['sale', 'purchase'])
             except: pass
        
        all_requests = []
        if current_user.is_master:
            all_requests = get_all_activation_requests()

        return render_template('user_profile.html', profile=profile_data, target_user=target_user, target_is_active=target_is_active, target_perms=target_perms, pending_requests=all_requests)

    if request.method == 'POST':
        if 'verify_request' in request.form:
            if not current_user.is_master: return "Unauthorized", 403
            req_id = request.form.get('request_id')
            user_to_activate = request.form.get('user_to_activate')
            
            db.collection('app_users').document(user_to_activate).set({"is_active": True}, merge=True)
            db.collection('activation_requests').document(req_id).update({"status": "Approved"})
            
            flash(f"Payment Verified! User {user_to_activate} is now Active.", "success")
            return redirect(url_for('user_profile'))

        if 'update_perms' in request.form:
             if not current_user.is_master: return "Unauthorized", 403
             target = request.form.get('target_user_id')
             perm_sale = request.form.get('perm_sale')
             perm_purchase = request.form.get('perm_purchase')
             
             new_perms = []
             if perm_sale: new_perms.append('sale')
             if perm_purchase: new_perms.append('purchase')
             
             db.collection('app_users').document(target).set({"permissions": new_perms}, merge=True)
             flash(f"Permissions for {target} updated!", "success")
             return redirect(url_for('user_profile', edit_user=target))

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
            
            perm_sale = request.form.get('new_perm_sale')
            perm_purchase = request.form.get('new_perm_purchase')
            new_perms = []
            if perm_sale: new_perms.append('sale')
            if perm_purchase: new_perms.append('purchase')
            
            if new_u and new_p:
                db.collection('app_users').document(new_u).set({
                    "password": new_p, 
                    "is_active": False,
                    "permissions": new_perms
                })
                flash(f"User {new_u} created! (Inactive by default)", "success")
            return redirect(url_for('user_profile'))

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


# ------------------ GENERATE INVOICE (STRICT ERP LOGIC) ------------------
@app.route("/generate-invoice", methods=["POST"])
@login_required
def handle_invoice():
    try:
        data = request.json or {}
        
        # --- 1. STRICT TYPE EXTRACTION ---
        doc_category = data.get('doc_category', 'sale') 
        doc_type = data.get('doc_type', 'invoice') 
        
        # --- 2. PERMISSION & RULE CHECK ---
        if not current_user.has_permission(doc_category):
             return jsonify({"error": f"You do not have permission for {doc_category} operations."}), 403

        # STRICT ERP RULES
        if doc_category == 'sale':
            if doc_type == 'dn':
                 return jsonify({"error": "RULE: Sales Debit Notes are not allowed in this system."}), 400
            if doc_type == 'cn' and not data.get('original_invoice_no') and not data.get('manual_bill_no'):
                 pass 

        if doc_category == 'purchase':
            if doc_type == 'cn':
                 return jsonify({"error": "RULE: Purchase Credit Notes are not allowed. Use Debit Note for Vendor Returns."}), 400

        # --- 3. DATA PREPARATION ---
        is_edit = data.get('is_edit', False)
        is_non_gst = data.get('is_non_gst', False)
        
        is_debit_note = (doc_type == 'dn')
        is_credit_note = (doc_type == 'cn')
        
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

        # --- 4. 24-HOUR EDIT CHECK ---
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

        # --- 5. SAVE MASTERS (Clients/Particulars) ---
        for i, item_name in enumerate(particulars):
            if item_name:
                storage_key = f"{item_name}_NONGST" if is_non_gst else item_name
                hsn_val = "" if is_non_gst else (hsns[i] if i < len(hsns) else "")
                rate_val = rates[i] if i < len(rates) else 0
                tax_val = 0 if is_non_gst else (taxrates[i] if i < len(taxrates) else 0)
                save_single_particular(storage_key, {"hsn": hsn_val, "rate": rate_val, "taxrate": tax_val})

        if client_name:
            save_single_client(client_name, client_details)

        # --- 6. GENERATE BILL NUMBER ---
        auto_generate = data.get("auto_generate", True)
        if auto_generate:
            profile = get_seller_profile_data()
            prefix = profile.get('invoice_prefix', 'TE').upper()
            
            if doc_category == 'purchase':
                if doc_type == 'po':
                    ctr = get_next_counter(is_purchase=True, doc_type='po')
                    bill_no = f"{prefix}-PO/25-26/{ctr:04d}"
                elif doc_type == 'grn':
                    ctr = get_next_counter(is_purchase=True, doc_type='grn')
                    bill_no = f"{prefix}-GRN/25-26/{ctr:04d}"
                elif doc_type == 'bill':
                    ctr = get_next_counter(is_purchase=True, doc_type='bill')
                    bill_no = f"{prefix}-PB/25-26/{ctr:04d}"
                elif doc_type == 'dn':
                    ctr = get_next_counter(is_purchase=True, is_debit_note=True)
                    bill_no = f"{prefix}-PDN/25-26/{ctr:04d}"
                else:
                    bill_no = f"TEMP-{random.randint(1000,9999)}"
            else:
                # Sales
                if doc_type == 'cn':
                    ctr = get_next_counter(is_credit_note=True)
                    bill_no = f"{prefix}-CN/25-26/{ctr:04d}"
                else:
                    # Standard Invoice
                    ctr = get_next_counter(is_credit_note=False)
                    bill_no = f"{prefix}/25-26/{ctr:04d}"
            
            invoice_date_str = date.today().strftime('%d-%b-%Y')
        else:
            bill_no = str(data.get("manual_bill_no","")).strip()
            if not is_edit:
                base = get_db_base()
                coll_name = 'sales_invoices'
                if doc_category == 'purchase': coll_name = 'purchase_bills'
                chk = base.collection(coll_name).document(bill_no.replace('/', '_')).get()
                if chk.exists:
                    return jsonify({"error": "Duplicate Invoice/Doc Number"}), 409

            manual_date = data.get("manual_invoice_date","")
            invoice_date_str = datetime.strptime(manual_date, '%Y-%m-%d').strftime('%d-%b-%Y') if manual_date else date.today().strftime('%d-%b-%Y')
        
        # --- 7. CALCULATIONS ---
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
                # State logic: If state codes match, calculate CGST/SGST. Otherwise IGST.
                # Assuming simple check based on GSTIN start, or if missing use fallback
                if data.get('client_gstin','') and data.get('client_gstin','').startswith(my_state_code):
                    cgst_amt = round(tax_amt/2, 2)
                    sgst_amt = tax_amt - cgst_amt
                    total_cgst += cgst_amt
                    total_sgst += sgst_amt
                elif not data.get('client_gstin','') and data.get('client_state','').lower() == 'delhi': 
                    # If you need specific text matching based on your frontend
                    total_cgst += round(tax_amt/2, 2)
                    total_sgst += tax_amt - round(tax_amt/2, 2)
                else:
                    total_igst += tax_amt

        # --- 8. PREPARE DOCUMENT OBJECT ---
        invoice_data = {
            "bill_no": bill_no,
            "invoice_date": invoice_date_str,
            "timestamp": datetime.now().isoformat(),
            "doc_category": doc_category,
            "doc_type": doc_type,
            "is_non_gst": is_non_gst,
            "is_debit_note": is_debit_note,
            "is_credit_note": is_credit_note,
            "original_invoice_no": data.get('original_invoice_no', ''),
            "client_name": client_name,
            
            "client_address1": client_details['address1'],
            "client_address2": client_details['address2'],
            "client_pincode": client_details['pincode'],
            "client_district": client_details['district'],
            "client_state": client_details['state'],
            "client_gstin": client_details['gstin'],
            "client_email": client_details['email'],
            "client_mobile": client_details['mobile'],
            
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
        
        # --- 9. ATOMIC TRANSACTION (INVENTORY + SAVE) ---
        @firestore.transactional
        def commit_transaction(transaction, inv_data, is_edit_mode):
            # A. Inventory Logic
            if not is_edit_mode:
                direction = 0
                d_cat = inv_data.get('doc_category')
                d_type = inv_data.get('doc_type')
                
                if d_cat == 'purchase':
                    if d_type == 'grn': direction = 1
                    elif d_type == 'dn': direction = -1
                elif d_cat == 'sale':
                    if d_type == 'invoice': direction = -1
                    elif d_type == 'cn': direction = 1
                
                if direction != 0:
                    parts = inv_data.get('particulars', [])
                    q_list = inv_data.get('qtys', [])
                    ts = datetime.now().isoformat()
                    
                    for k in range(len(parts)):
                        iname = parts[k].strip()
                        iqty = float(q_list[k]) if k < len(q_list) else 0
                        
                        if not iname or iqty <= 0: continue
                        
                        safe_id = "".join(x for x in iname if x.isalnum()).upper()
                        if not safe_id: continue
                        
                        p_ref = db.collection('inventory_products').document(safe_id)
                        snap = p_ref.get(transaction=transaction)
                        
                        cur_stock = float(snap.get('current_stock') or 0) if snap.exists else 0.0
                        
                        if direction == -1 and not snap.exists:
                            raise ValueError(f"Stock Error: Item '{iname}' not found.")
                            
                        if direction == 1 and not snap.exists:
                             transaction.set(p_ref, {"item_name": iname, "current_stock": 0.0})

                        new_stock = cur_stock + (iqty * direction)
                        
                        if new_stock < 0:
                            raise ValueError(f"Stock Error: Insufficient stock for '{iname}'. Available: {cur_stock}")
                            
                        transaction.update(p_ref, {"current_stock": new_stock, "last_updated": ts})
                        
                        l_ref = db.collection('inventory_ledger').document()
                        transaction.set(l_ref, {
                            "ref_doc_no": inv_data.get('bill_no'),
                            "date": inv_data.get('invoice_date'),
                            "doc_type": f"{d_cat}_{d_type}",
                            "item_name": iname,
                            "qty_change": (iqty * direction),
                            "running_balance": new_stock,
                            "timestamp": ts
                        })

            # B. Save Invoice Document
            base_db = get_db_base()
            c_cat = inv_data.get('doc_category', 'sale')
            c_type = inv_data.get('doc_type', 'invoice')
            c_name = 'sales_invoices' 
            
            if c_cat == 'purchase':
                if c_type == 'dn': c_name = 'purchase_debit_notes'
                elif c_type == 'po': c_name = 'purchase_orders'
                elif c_type == 'grn': c_name = 'purchase_grns'
                elif c_type == 'bill': c_name = 'purchase_bills'
                else: c_name = 'purchase_misc'
            else:
                if c_type == 'cn': c_name = 'sales_credit_notes'
                elif c_type == 'dn': c_name = 'sales_debit_notes' 
                else: c_name = 'sales_invoices'
                
            doc_id = inv_data['bill_no'].replace('/', '_')
            doc_ref = base_db.collection(c_name).document(doc_id)
            transaction.set(doc_ref, inv_data)

        # EXECUTE TRANSACTION
        commit_transaction(db.transaction(), invoice_data, is_edit)

        # --- 10. GENERATE PDF ---
        pdf_file = PDF_Generator(invoice_data, is_debit_note=is_debit_note, is_credit_note=is_credit_note)
        
        prefix = "Document"
        if doc_category == 'purchase': prefix = doc_type.upper()
        elif is_debit_note: prefix = "DebitNote"
        elif is_credit_note: prefix = "CreditNote"
        else: prefix = "Invoice"
            
        return send_file(pdf_file, mimetype="application/pdf", as_attachment=True, download_name=f"{prefix}_{bill_no.replace('/','_')}.pdf")

    except ValueError as ve:
        return jsonify({"error": str(ve)}), 400
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
                    is_dn = inv.get('is_debit_note', False)
                    pdf_bytes = PDF_Generator(inv, is_credit_note=is_cn, is_debit_note=is_dn)
                    
                    prefix = "Invoice"
                    if is_cn: prefix = "CreditNote"
                    elif is_dn: prefix = "DebitNote"
                    elif inv.get('doc_category') == 'purchase': prefix = inv.get('doc_type','doc').upper()
                    
                    filename = f"{prefix}_{bno.replace('/','_')}.pdf"
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
        is_dn = inv.get('is_debit_note', False)
        
        doc_type = "Invoice"
        if is_cn: doc_type = "Credit Note"
        elif is_dn: doc_type = "Debit Note"
        elif inv.get('doc_category') == 'purchase': doc_type = inv.get('doc_type').upper()
        
        pdf_bytes = PDF_Generator(inv, is_credit_note=is_cn, is_debit_note=is_dn)
        
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
    
    is_cn = invoice_data.get('is_credit_note', False)
    is_dn = invoice_data.get('is_debit_note', False)
    pdf_file = PDF_Generator(invoice_data, is_credit_note=is_cn, is_debit_note=is_dn)
    
    prefix = "Invoice"
    if is_cn: prefix = "CreditNote"
    elif is_dn: prefix = "DebitNote"
    elif invoice_data.get('doc_category') == 'purchase': prefix = invoice_data.get('doc_type','doc').upper()
    
    return send_file(pdf_file, mimetype="application/pdf", as_attachment=True, download_name=f"{prefix}_{bill_no.replace('/','_')}.pdf")

@app.route('/generate-credit-note/<path:bill_no>', methods=['GET'])
@login_required
def generate_credit_note(bill_no):
    try:
        bill_no = unquote(bill_no)
        invoices = load_invoices()
        existing_cn = next((inv for inv in invoices if inv.get('original_invoice_no') == bill_no and inv.get('is_credit_note')), None)
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

# --- INVENTORY API ---
@app.route("/api/check-stock/<path:item_name>")
@login_required
def check_stock(item_name):
    try:
        safe_id = "".join(x for x in item_name if x.isalnum()).upper()
        if not safe_id: return jsonify({"exists": False, "stock": 0})
        
        doc = db.collection('inventory_products').document(safe_id).get()
        if doc.exists:
            return jsonify({"exists": True, "stock": doc.to_dict().get('current_stock', 0)})
        return jsonify({"exists": False, "stock": 0})
    except Exception as e:
        logging.error(f"Stock Check Error: {e}")
        return jsonify({"exists": False, "stock": 0})

# --- GSTR-1 ROUTE ---
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

        # CRITICAL: Only grab SALES documents for GSTR-1
        all_docs = load_invoices_for_user(session.get('view_mode', current_user.id))
        sales_docs = [d for d in all_docs if d.get('doc_category', 'sale') == 'sale']
        
        for inv in sales_docs:
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
                for rate, taxable in tax_groups.items():
                    ws_b2cs.append(["OE", pos, rate, taxable, 0, ""])

        out = io.BytesIO()
        wb.save(out)
        out.seek(0)
        return send_file(out, mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', as_attachment=True, download_name=f'GSTR1_Report_{date.today()}.xlsx')

    except Exception as e:
        return f"Error: {e}", 500

# ------------------ INVOICE STATUS ------------------
@app.route('/update-status/<path:bill_no>', methods=['POST'])
@login_required
def update_invoice_status(bill_no):
    try:
        bill_no = unquote(bill_no)
        new_status = request.json.get('status')
        VALID = ['Draft', 'Confirmed', 'Paid', 'Cancelled']
        if new_status not in VALID:
            return jsonify({"error": "Invalid status"}), 400

        base = get_db_base()
        collections = ['sales_invoices', 'sales_credit_notes', 'sales_debit_notes',
                       'purchase_orders', 'purchase_grns', 'purchase_bills', 'purchase_debit_notes', 'invoices']
        doc_id = bill_no.replace('/', '_')
        for c in collections:
            ref = base.collection(c).document(doc_id)
            snap = ref.get()
            if snap.exists:
                ref.update({"status": new_status, "status_updated_at": datetime.now().isoformat()})
                return jsonify({"success": True})
        return jsonify({"error": "Invoice not found"}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ------------------ PAYMENT RECEIPTS ------------------
@app.route('/payments', methods=['GET'])
@login_required
def get_payments():
    try:
        base = get_db_base()
        docs = base.collection('payments').order_by('timestamp', direction=firestore.Query.DESCENDING).stream()
        return jsonify([d.to_dict() for d in docs])
    except Exception as e:
        return jsonify([])

@app.route('/payments', methods=['POST'])
@login_required
def add_payment():
    try:
        data = request.json or {}
        party_name = data.get('party_name', '').strip()
        amount = float(data.get('amount', 0))
        payment_type = data.get('payment_type', 'receipt')  # receipt or payment
        mode = data.get('mode', 'Cash')
        ref_invoice = data.get('ref_invoice', '')
        notes = data.get('notes', '')
        payment_date = data.get('payment_date', date.today().strftime('%d-%b-%Y'))

        if not party_name or amount <= 0:
            return jsonify({"error": "Party name and amount are required"}), 400

        payment_id = f"{party_name}_{datetime.now().strftime('%Y%m%d%H%M%S%f')}"
        entry = {
            "payment_id": payment_id,
            "party_name": party_name,
            "amount": amount,
            "payment_type": payment_type,
            "mode": mode,
            "ref_invoice": ref_invoice,
            "notes": notes,
            "payment_date": payment_date,
            "timestamp": datetime.now().isoformat(),
            "created_by": current_user.id
        }

        base = get_db_base()
        base.collection('payments').document(payment_id).set(entry)

        # Auto-mark invoice as Paid if ref_invoice provided and full payment
        if ref_invoice:
            invoices = load_invoices()
            inv = next((i for i in invoices if i['bill_no'] == ref_invoice), None)
            if inv and payment_type == 'receipt':
                total_paid = _get_total_paid(party_name, ref_invoice)
                if total_paid >= float(inv.get('grand_total', 0)):
                    doc_id = ref_invoice.replace('/', '_')
                    for c in ['sales_invoices', 'invoices']:
                        ref = base.collection(c).document(doc_id)
                        if ref.get().exists:
                            ref.update({"status": "Paid"})
                            break

        return jsonify({"success": True, "payment_id": payment_id})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

def _get_total_paid(party_name, ref_invoice):
    try:
        base = get_db_base()
        docs = base.collection('payments').where('party_name', '==', party_name).where('ref_invoice', '==', ref_invoice).stream()
        return sum(float(d.to_dict().get('amount', 0)) for d in docs)
    except:
        return 0.0


# ------------------ PARTY LEDGER ------------------
@app.route('/ledger/<path:party_name>', methods=['GET'])
@login_required
def party_ledger(party_name):
    try:
        party_name = unquote(party_name)
        invoices = load_invoices()
        base = get_db_base()

        party_invoices = [i for i in invoices if i.get('client_name', '').strip().lower() == party_name.strip().lower()]

        try:
            pay_docs = base.collection('payments').where('party_name', '==', party_name).stream()
            payments = [d.to_dict() for d in pay_docs]
        except:
            payments = []

        entries = []
        for inv in party_invoices:
            cat = inv.get('doc_category', 'sale')
            dtype = inv.get('doc_type', 'invoice')
            is_cn = inv.get('is_credit_note', False)
            amount = float(inv.get('grand_total', 0))

            # For sales: invoice = debit (they owe us), CN = credit (we owe them)
            # For purchase: bill = credit (we owe vendor), DN = debit (vendor owes us)
            if cat == 'sale':
                dr = amount if not is_cn else 0
                cr = amount if is_cn else 0
            else:
                dr = 0
                cr = amount

            entries.append({
                "date": inv.get('invoice_date', ''),
                "doc_no": inv.get('bill_no', ''),
                "doc_type": dtype.upper(),
                "narration": f"{'Credit Note' if is_cn else 'Invoice'} - {inv.get('client_name','')}",
                "debit": dr,
                "credit": cr,
                "timestamp": inv.get('timestamp', '')
            })

        for pay in payments:
            ptype = pay.get('payment_type', 'receipt')
            amount = float(pay.get('amount', 0))
            entries.append({
                "date": pay.get('payment_date', ''),
                "doc_no": pay.get('payment_id', ''),
                "doc_type": "RECEIPT" if ptype == 'receipt' else "PAYMENT",
                "narration": f"Payment {pay.get('mode','')}" + (f" - Ref: {pay.get('ref_invoice','')}" if pay.get('ref_invoice') else ''),
                "debit": 0,
                "credit": amount if ptype == 'receipt' else 0,
                "timestamp": pay.get('timestamp', '')
            })

        entries.sort(key=lambda x: x.get('timestamp', ''))

        running = 0.0
        for e in entries:
            running += e['debit'] - e['credit']
            e['balance'] = round(running, 2)

        return jsonify({
            "party_name": party_name,
            "entries": entries,
            "closing_balance": round(running, 2)
        })
    except Exception as e:
        logging.error(f"Ledger error: {e}")
        return jsonify({"error": str(e)}), 500


# ------------------ OUTSTANDING RECEIVABLES ------------------
@app.route('/outstanding', methods=['GET'])
@login_required
def outstanding_report():
    try:
        invoices = load_invoices()
        base = get_db_base()

        # Only unpaid sales invoices
        sales_inv = [i for i in invoices
                     if i.get('doc_category', 'sale') == 'sale'
                     and i.get('doc_type', 'invoice') == 'invoice'
                     and not i.get('is_credit_note', False)
                     and i.get('status', 'Confirmed') not in ['Paid', 'Cancelled']]

        try:
            pay_docs = base.collection('payments').where('payment_type', '==', 'receipt').stream()
            all_payments = [d.to_dict() for d in pay_docs]
        except:
            all_payments = []

        today = date.today()
        result = []

        for inv in sales_inv:
            bill_no = inv.get('bill_no', '')
            grand_total = float(inv.get('grand_total', 0))

            paid = sum(float(p.get('amount', 0)) for p in all_payments if p.get('ref_invoice') == bill_no)
            balance = round(grand_total - paid, 2)
            if balance <= 0:
                continue

            inv_date_str = inv.get('invoice_date', '')
            try:
                months = {'Jan':1,'Feb':2,'Mar':3,'Apr':4,'May':5,'Jun':6,'Jul':7,'Aug':8,'Sep':9,'Oct':10,'Nov':11,'Dec':12}
                parts = inv_date_str.split('-')
                inv_date = date(int(parts[2]), months.get(parts[1], 1), int(parts[0]))
                days_overdue = (today - inv_date).days
            except:
                days_overdue = 0

            if days_overdue <= 30: age_bucket = "0-30 days"
            elif days_overdue <= 60: age_bucket = "31-60 days"
            elif days_overdue <= 90: age_bucket = "61-90 days"
            else: age_bucket = "90+ days"

            result.append({
                "bill_no": bill_no,
                "invoice_date": inv_date_str,
                "client_name": inv.get('client_name', ''),
                "client_mobile": inv.get('client_mobile', ''),
                "grand_total": grand_total,
                "paid": round(paid, 2),
                "balance": balance,
                "days_overdue": days_overdue,
                "age_bucket": age_bucket,
                "status": inv.get('status', 'Confirmed')
            })

        result.sort(key=lambda x: x['days_overdue'], reverse=True)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ------------------ DASHBOARD DATA ------------------
@app.route('/dashboard-data', methods=['GET'])
@login_required
def dashboard_data():
    try:
        invoices = load_invoices()
        today = date.today()
        this_month = today.strftime('%b-%Y')
        months_map = {'Jan':1,'Feb':2,'Mar':3,'Apr':4,'May':5,'Jun':6,'Jul':7,'Aug':8,'Sep':9,'Oct':10,'Nov':11,'Dec':12}

        today_sales = 0.0
        month_sales = 0.0
        today_purchase = 0.0
        month_purchase = 0.0
        monthly_trend = {}  # last 6 months
        top_clients = {}
        doc_counts = {"invoice": 0, "cn": 0, "po": 0, "grn": 0, "bill": 0, "dn": 0}

        for inv in invoices:
            cat = inv.get('doc_category', 'sale')
            dtype = inv.get('doc_type', 'invoice')
            is_cn = inv.get('is_credit_note', False)
            amount = float(inv.get('grand_total', 0))
            inv_date_str = inv.get('invoice_date', '')
            status = inv.get('status', 'Confirmed')

            if status == 'Cancelled':
                continue

            try:
                parts = inv_date_str.split('-')
                inv_date = date(int(parts[2]), months_map.get(parts[1], 1), int(parts[0]))
                inv_month = inv_date.strftime('%b-%Y')
                inv_today = (inv_date == today)
            except:
                inv_date = None
                inv_month = ''
                inv_today = False

            # Count doc types
            if is_cn: doc_counts['cn'] = doc_counts.get('cn', 0) + 1
            elif dtype in doc_counts: doc_counts[dtype] = doc_counts.get(dtype, 0) + 1

            if cat == 'sale' and not is_cn and dtype == 'invoice':
                if inv_today: today_sales += amount
                if inv_month == this_month: month_sales += amount

                # Monthly trend
                if inv_date:
                    mk = inv_date.strftime('%b %y')
                    monthly_trend[mk] = monthly_trend.get(mk, 0) + amount

                # Top clients
                cname = inv.get('client_name', 'Unknown')
                top_clients[cname] = top_clients.get(cname, 0) + amount

            elif cat == 'purchase' and dtype == 'bill':
                if inv_today: today_purchase += amount
                if inv_month == this_month: month_purchase += amount

        # Get outstanding count
        try:
            outstanding_res = outstanding_report()
            outstanding_data = outstanding_res.get_json()
            outstanding_count = len(outstanding_data) if isinstance(outstanding_data, list) else 0
            outstanding_total = sum(o['balance'] for o in outstanding_data) if isinstance(outstanding_data, list) else 0
        except:
            outstanding_count = 0
            outstanding_total = 0

        # Get inventory low stock
        try:
            inv_docs = db.collection('inventory_products').stream()
            low_stock = []
            for d in inv_docs:
                item = d.to_dict()
                stock = float(item.get('current_stock', 0))
                reorder = float(item.get('reorder_level', 0))
                if stock <= reorder:
                    low_stock.append({"item": item.get('item_name', d.id), "stock": stock, "reorder": reorder})
        except:
            low_stock = []

        # Sort monthly trend - last 6 months only
        sorted_months = sorted(monthly_trend.items(), key=lambda x: datetime.strptime(x[0], '%b %y'))[-6:]
        top_clients_sorted = sorted(top_clients.items(), key=lambda x: x[1], reverse=True)[:5]

        return jsonify({
            "today_sales": round(today_sales, 2),
            "month_sales": round(month_sales, 2),
            "today_purchase": round(today_purchase, 2),
            "month_purchase": round(month_purchase, 2),
            "outstanding_count": outstanding_count,
            "outstanding_total": round(outstanding_total, 2),
            "low_stock_count": len(low_stock),
            "low_stock_items": low_stock[:5],
            "monthly_trend": sorted_months,
            "top_clients": top_clients_sorted,
            "doc_counts": doc_counts,
            "total_invoices": len(invoices)
        })
    except Exception as e:
        logging.error(f"Dashboard error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/dashboard', methods=['GET'])
@login_required
def dashboard():
    return render_template('dashboard.html')


# ------------------ RESET PASSWORD (FIX) ------------------
@app.route('/reset-password', methods=['POST'])
@login_required
def reset_password():
    if not current_user.is_master:
        return "Unauthorized", 403
    target = request.form.get('target_user_id')
    new_pass = request.form.get('reset_password')
    if target and new_pass:
        db.collection('app_users').document(target).set({"password": new_pass}, merge=True)
        flash(f"Password for {target} updated successfully!", "success")
    return redirect(url_for('user_profile', edit_user=target))


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=int(os.getenv("PORT",5000)))