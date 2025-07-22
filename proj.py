from flask import Flask, render_template, request, redirect, url_for, session, send_from_directory, jsonify, flash
import os
import PyPDF2
import docx
import pytesseract
import pdf2image
import openai
from werkzeug.utils import secure_filename
from datetime import datetime
from supabase import create_client, Client
import firebase_admin
from firebase_admin import credentials, auth as firebase_auth

# -- Load environment variables --
from dotenv import load_dotenv
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))

# --- Config ---
cred_path = os.getenv("FIREBASE_CRED_PATH", os.path.join(BASE_DIR, "serviceAccountKey.json"))
cred = credentials.Certificate(cred_path)
firebase_admin.initialize_app(cred)

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "supersecretkey")
UPLOAD_FOLDER = 'uploads'
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

openai.api_key = os.getenv("OPENAI_API_KEY")

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# --- Helpers ---
def extract_pdf_text(path):
    with open(path, 'rb') as f:
        reader = PyPDF2.PdfReader(f)
        return ' '.join(p.extract_text() or '' for p in reader.pages).strip()

def extract_pdf_ocr(path):
    images = pdf2image.convert_from_path(path)
    return ' '.join(pytesseract.image_to_string(img) for img in images).strip()

def extract_docx_text(path):
    doc = docx.Document(path)
    return ' '.join(para.text for para in doc.paragraphs).strip()

def extract_text(path):
    ext = path.lower().rsplit('.', 1)[-1]
    if ext == 'pdf':
        text = extract_pdf_text(path)
        return text if text else extract_pdf_ocr(path)
    elif ext == 'docx':
        return extract_docx_text(path)
    elif ext == 'txt':
        with open(path, 'r', encoding='utf-8') as f:
            return f.read().strip()
    return ''

def extract_field(text, field):
    for line in text.splitlines():
        if line.lower().startswith(field.lower()):
            return line.split(':', 1)[1].strip()
    return "Not found"

# --- Routes ---
@app.route('/')
def home():
    return redirect('/login')

@app.route('/login')
def login():
    return render_template('login.html')

@app.route('/forgot-password')
def forgot_password():
    return render_template('forgot_password.html')

@app.route('/logout')
def logout():
    session.clear()
    return redirect('/login')

@app.route('/dashboard')
def dashboard():
    if not session.get('logged_in'):
        return redirect('/login')
    jobs = supabase.table("jobs").select("*").eq("user_id", session['uid']).order("created_at", desc=True).execute()
    return render_template("dashboard_stitch.html", jobs=jobs.data)

@app.route('/add-job', methods=['POST'])
def add_job():
    if not session.get('logged_in'):
        return redirect('/login')
    title = request.form['title']
    description = request.form['description']
    user_id = session.get('uid')
    supabase.table("jobs").insert({
        "user_id": user_id,
        "title": title,
        "description": description,
        "created_at": datetime.now().isoformat()
    }).execute()
    return redirect('/dashboard')

@app.route('/edit-job/<job_id>', methods=['GET', 'POST'])
def edit_job(job_id):
    if not session.get('logged_in'):
        return redirect('/login')

    job = supabase.table("jobs").select("*").eq("id", job_id).single().execute().data

    if request.method == 'POST':
        title = request.form['title']
        description = request.form['description']
        supabase.table("jobs").update({
            "title": title,
            "description": description
        }).eq("id", job_id).execute()
        return redirect('/dashboard')

    return render_template("edit_job.html", job=job)

@app.route('/delete-job/<job_id>', methods=['POST'])
def delete_job(job_id):
    if not session.get('logged_in'):
        return redirect('/login')
    supabase.table("jobs").delete().eq("id", job_id).execute()
    return redirect('/dashboard')

@app.route('/upload/<job_id>', methods=['GET'])
def upload_page(job_id):
    if not session.get('logged_in'):
        return redirect('/login')
    job = supabase.table("jobs").select("*").eq("id", job_id).single().execute().data
    return render_template('frontend.html', job=job)

@app.route('/upload/<job_id>', methods=['POST'])
def upload(job_id):
    if not session.get('logged_in'):
        return redirect('/login')

    job = supabase.table("jobs").select("*").eq("id", job_id).single().execute().data

    files = request.files.getlist('resumes')
    for file in files:
        filename = secure_filename(file.filename)
        path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        file.save(path)
        resume_text = extract_text(path)

        prompt = f"""
You are a resume screening assistant.

Given this resume and job description:

Resume:
{resume_text}

Job Description:
{job['description']}

Extract:
- Candidate Name
- Match Percentage (0–100%)
- Matching Skills
- Missing Skills
- Feedback (1-2 lines)

Respond ONLY in this format:
Candidate Name: ...
Match Percentage: ...%
Matching Skills: ...
Missing Skills: ...
Feedback: ...
"""
        try:
            response = openai.ChatCompletion.create(
                model="gpt-3.5-turbo",
                messages=[
                    {"role": "system", "content": "You are a helpful resume-screening assistant."},
                    {"role": "user", "content": prompt}
                ]
            )
            content = response.choices[0].message['content']
            candidate_name = extract_field(content, "Candidate Name").lower()
            match_percent = extract_field(content, "Match Percentage").replace('%', '')
            matched_skills = [s.strip() for s in extract_field(content, "Matching Skills").split(',') if s.strip()]
            missing_skills = [s.strip() for s in extract_field(content, "Missing Skills").split(',') if s.strip()]
            feedback = extract_field(content, "Feedback")

            existing = supabase.table("resumes").select("id").eq("job_id", job_id).or_(
                f"file_name.eq.{filename},candidate_name.ilike.{candidate_name}").execute()
            if existing.data:
                flash(f"Resume '{filename}' already uploaded.", "warning")
                continue

            supabase.table("resumes").insert({
                "job_id": job_id,
                "candidate_name": candidate_name.title(),
                "match_percentage": float(match_percent),
                "matched_skills": matched_skills,
                "missing_skills": missing_skills,
                "feedback": feedback,
                "file_name": filename,
                "created_at": datetime.now().isoformat()
            }).execute()
        except Exception:
            continue

    return redirect(f'/results/{job_id}')

@app.route('/results/<job_id>')
def results(job_id):
    if not session.get('logged_in'):
        return redirect('/login')
    page = int(request.args.get('page', 1))
    page_size = 100
    sort_by = request.args.get('sort_by', 'latest')
    order_field = 'match_percentage' if sort_by == 'match' else 'created_at'

    job = supabase.table("jobs").select("*").eq("id", job_id).single().execute().data
    resumes = supabase.table("resumes").select("*").eq("job_id", job_id).order(order_field, desc=True).range(
        (page - 1) * page_size, page * page_size - 1).execute()

    for r in resumes.data:
        try:
            dt = datetime.fromisoformat(r['created_at'].replace('Z', ''))
            r['formatted_date'] = dt.strftime('%m-%d-%Y')
        except:
            r['formatted_date'] = r['created_at']

    total = supabase.table("resumes").select("id", count='exact').eq("job_id", job_id).execute().count or 0
    total_pages = (total + page_size - 1) // page_size

    return render_template('results.html', results=resumes.data, job=job, page=page, total_pages=total_pages, sort_by=sort_by)

@app.route('/delete-resume/<resume_id>/<job_id>', methods=['POST'])
def delete_resume(resume_id, job_id):
    supabase.table("resumes").delete().eq("id", resume_id).execute()
    return redirect(f'/results/{job_id}')

@app.route('/delete-selected-resumes/<job_id>', methods=['POST'])
def delete_selected_resumes(job_id):
    ids = request.form.getlist('resume_ids')
    for rid in ids:
        supabase.table("resumes").delete().eq("id", rid).execute()
    return redirect(f'/results/{job_id}')

@app.route('/uploads/<path:filename>')
def download_file(filename):
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename, as_attachment=True)

@app.route('/sessionLogin', methods=['POST'])
def session_login():
    id_token = request.json.get('idToken')
    try:
        decoded = firebase_auth.verify_id_token(id_token)
        session['uid'] = decoded['uid']
        session['logged_in'] = True
        return jsonify({'status': 'success'})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 401

if __name__ == '__main__':
    app.run(debug=True)