import streamlit as st
import pandas as pd
import time
import re
import requests
import urllib.parse as urlparse
from urllib.parse import urlencode, urljoin
import unicodedata
import io
import json

# --- EXTERNAL LIBRARIES ---
try:
    from ddgs import DDGS
    HAS_DDGS = True
except ImportError:
    HAS_DDGS = False

try:
    import cloudscraper
    from bs4 import BeautifulSoup
    HAS_DEEP_SCRAPE = True
except ImportError:
    HAS_DEEP_SCRAPE = False

from streamlit_lottie import st_lottie

# --- CONFIG & BRANDING ---
# 1. Renamed to simply ScholarHunt
st.set_page_config(page_title="ScholarHunt", page_icon="📚", layout="wide")

# UI Aesthetic Gradient Tło (Point 4)
st.markdown("""
<style>
/* Gradient background for the main content area */
.stApp {
    background-image: linear-gradient(135deg, #fdfbfb 0%, #ebedee 100%);
}
/* Professional tweaks to headers and borders */
h1, h2, h3 {
    color: #2c3e50;
    font-weight: 700;
}
.stTabs [data-baseweb="tab-list"] {
    background-color: #ffffff;
    border-radius: 8px;
    padding: 10px;
    box-shadow: 0 4px 6px rgba(0,0,0,0.1);
}
.stProgress > div > div > div > div {
    background-color: #50c878; /* Green progress bar */
}
</style>
""", unsafe_allow_html=True)

TLD_MAP = {
    'sweden': '.se', 'poland': '.pl', 'germany': '.de', 'france': '.fr', 'italy': '.it', 
    'spain': '.es', 'china': '.cn', 'japan': '.jp', 'uk': '.uk', 'united kingdom': '.uk', 
    'england': '.uk', 'australia': '.au', 'canada': '.ca', 'brazil': '.br', 'india': '.in', 
    'russia': '.ru', 'netherlands': '.nl', 'switzerland': '.ch', 'belgium': '.be',
    'austria': '.at', 'portugal': '.pt', 'denmark': '.dk', 'norway': '.no', 'finland': '.fi', 
    'czech republic': '.cz', 'south korea': '.kr'
}

ADV_FIELDS = {
    "Article Title": ["TI", "Article Title"], 
    "DOI": ["DI", "DOI"], 
    "Publication Year": ["PY", "Publication Year"], 
    "Times Cited": ["TC", "Times Cited, WoS Core"], 
    "Author Keywords": ["DE", "Author Keywords"]
}

# --- SESJA STATE INITIALIZATION ---
if 'cited_db' not in st.session_state: st.session_state.cited_db = []
if 'cited_batches' not in st.session_state: st.session_state.cited_batches = []
if 'citing_db' not in st.session_state: st.session_state.citing_db = []
if 'citing_batches' not in st.session_state: st.session_state.citing_batches = []

# Emergency accumulation lists for Point 3
if 'merge_accumulation' not in st.session_state: st.session_state.merge_accumulation = []
if 'past_accumulation' not in st.session_state: st.session_state.past_accumulation = []
if 'keywords_accumulation' not in st.session_state: st.session_state.keywords_accumulation = []

# --- CORE LOGIC FUNCTIONS ---
def normalize_international(text):
    if not isinstance(text, str) or pd.isna(text): return ""
    nfkd_form = unicodedata.normalize('NFKD', text)
    result = "".join([c for c in nfkd_form if not unicodedata.combining(c)])
    for k, v in {'ł': 'l', 'Ł': 'l', 'ø': 'o', 'Ø': 'o', 'æ': 'ae', 'ß': 'ss', 'đ': 'd', '[at]': '@', '(at)': '@', ' [dot] ': '.'}.items(): result = result.replace(k, v)
    return result.lower().strip()

def clean_author_name(n):
    n_clean = re.sub(r'\(\d+\)', '', str(n)).strip()
    n_clean = re.sub(r'\(.*?\)', '', n_clean).strip()
    if ',' in n_clean:
        parts = n_clean.split(',', 1)
        return parts[0].strip(), parts[1].strip()
    parts = n_clean.split()
    return (parts[-1], " ".join(parts[:-1])) if len(parts) > 1 else (n_clean, "")

def extract_emails_from_text(text, author_name=""):
    text = str(text).lower()
    text = text.replace(' [at] ', '@').replace('(at)', '@').replace(' [dot] ', '.')
    junk_prefixes = ['works.email', 'email:', 'e-mail:', 'mailto:', 'contact:', 'mail:', 'email']
    for junk in junk_prefixes: text = text.replace(junk, ' ')
    
    found = re.findall(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.(?:com|org|edu|net|gov|mil|int|info|biz|eu|[a-zA-Z]{2})', text)
    cleaned = []
    bad_prefixes = ['societies.contacts', 'contact.', 'info.', 'web.', 'staff.', 'people.', 'admin.']

    for e in found:
        e = re.sub(r'^[^a-zA-Z0-9]+', '', e)
        if author_name:
            normalized_name = normalize_international(author_name).replace(" ", "")
            if e.startswith(normalized_name) and len(e.split('@')[0]) > len(normalized_name) + 2:
                e = e[len(normalized_name):]
                e = re.sub(r'^[^a-zA-Z0-9]+', '', e)
        if '@' in e:
            local_part, domain = e.split('@', 1)
            for prefix in bad_prefixes:
                if local_part.startswith(prefix):
                    local_part = local_part[len(prefix):]
            e = f"{local_part}@{domain}"
        if not e.endswith(('.png', '.jpg', '.jpeg', '.gif', '.css', '.js')):
            cleaned.append(e)
            
    return list(set(cleaned))

def get_matched_email(surname, email_list):
    if not email_list: return ""
    sn_raw = normalize_international(surname)
    sn_clean = re.sub(r'[^a-z]', '', sn_raw)
    for em in email_list:
        em_str = str(em).lower().strip()
        if sn_raw in em_str: return em_str
        if "@" in em_str:
            em_pref = re.sub(r'[^a-z]', '', em_str.split('@')[0])
            if sn_clean and sn_clean in em_pref: return em_str
    return ""

def validate_email_intelligence(email, surname, country_raw):
    em_clean = str(email).lower().strip()
    sn_clean = str(surname).lower().strip()
    c_clean = str(country_raw).lower().strip()
    if not re.match(r"[^@]+@[^@]+\.[a-z]{2,}", em_clean): return "Invalid Format"
    
    email_prefix = em_clean.split('@')[0]
    email_clean_sn = re.sub(r'[^a-z]', '', normalize_international(email_prefix))
    surname_clean = re.sub(r'[^a-z]', '', normalize_international(sn_clean))
    
    is_match = False
    if len(surname_clean) <= 2: is_match = (surname_clean == email_clean_sn)
    else: is_match = (surname_clean in email_clean_sn)
    
    if is_match: return "Certain (Surname in email)"
    tld = TLD_MAP.get(c_clean, "")
    if tld and em_clean.endswith(tld): return f"Probable (Country Match {tld})"
    if em_clean.endswith(('.edu', '.ac.uk', '.edu.cn', '.edu.au', '.edu.pl', '.ac.jp', '.edu.kr', '.edu.br')): return "Probable (Academic Domain)"
    return "Manual Verification Required"

def extract_universal_data(df, r):
    doi = str(r.get('DOI', r.get('DI', ''))).strip()
    if doi.lower() == 'nan': doi = ""
    row_title = str(r.get('Title', r.get('Article Title', r.get('TI', '')))).strip()
    if row_title.lower() == 'nan': row_title = ""
    
    raw_authors = str(r.get('Author Full Names', r.get('AF', '')))
    if raw_authors and raw_authors.lower() != 'nan':
        af = [x.strip() for x in raw_authors.split(';') if x.strip()]
    else:
        raw_authors = str(r.get('Authors', r.get('Name', '')))
        if ';' in raw_authors: af = [x.strip() for x in raw_authors.split(';') if x.strip()]
        elif ',' in raw_authors: af = [x.strip() for x in raw_authors.split(',') if x.strip()]
        else: af = [raw_authors] if raw_authors else []
        
    raw_ems = str(r.get('Email Addresses', r.get('EM', '')))
    if raw_ems and raw_ems.lower() != 'nan':
        em_all = [x.strip().lower() for x in raw_ems.split(';') if "@" in x]
    else:
        em_all = []
        for col in ['Correspondence Address', 'Email', 'e-mail', 'Corresponding Authors']:
            if col in df.columns and pd.notna(r.get(col)):
                val = str(r.get(col))
                em_all.extend(re.findall(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.(?:com|org|edu|net|gov|mil|int|info|biz|eu|[a-zA-Z]{2})', val))
        em_all = list(set([e.lower() for e in em_all]))
                
    ad, rp = "", ""
    for c in ['Addresses', 'C1', 'Authors with affiliations', 'Latest Affilation']:
        if c in df.columns and pd.notna(r.get(c)): ad += " " + str(r.get(c))
    for c in ['Reprint Addresses', 'RP', 'Correspondence Address', 'Corresponding Authors']:
        if c in df.columns and pd.notna(r.get(c)): rp += " " + str(r.get(c))
        
    return doi, row_title, af, em_all, ad, rp

def get_emails_from_orcid(orcid_id):
    emails, urls = [], []
    if not orcid_id or len(orcid_id) < 15: return emails, urls
    try:
        res = requests.get(f"https://pub.orcid.org/v3.0/{orcid_id}/person", headers={"Accept": "application/json"}, timeout=10)
        if res.status_code == 200:
            data = res.json()
            if 'emails' in data and data['emails'] and 'email' in data['emails']:
                for e in data['emails']['email']: emails.append(e.get('email', '').strip())
            if 'researcher-urls' in data and data['researcher-urls'] and 'researcher-url' in data['researcher-urls']:
                for u in data['researcher-urls']['researcher-url']:
                    if 'url' in u and 'value' in u['url']: urls.append(u['url']['value'])
    except: pass
    return list(set(emails)), list(set(urls))

def scrape_deep(base_url, author_name=""):
    all_emails = []
    if not HAS_DEEP_SCRAPE: return []
    scraper = cloudscraper.create_scraper(browser={'browser': 'chrome', 'platform': 'windows', 'mobile': False})
    try:
        res = scraper.get(base_url, timeout=10)
        soup = BeautifulSoup(res.text, 'html.parser')
        all_emails.extend(extract_emails_from_text(soup.get_text(), author_name))
        links_to_visit = []
        for a in soup.find_all('a', href=True):
            href = a['href'].lower()
            if any(x in href for x in ['contact', 'staff', 'people', 'directory', 'profile']):
                full_link = urljoin(base_url, a['href'])
                if full_link not in links_to_visit: links_to_visit.append(full_link)
        for link in list(set(links_to_visit))[:2]:
            try:
                sub_res = scraper.get(link, timeout=10)
                sub_soup = BeautifulSoup(sub_res.text, 'html.parser')
                all_emails.extend(extract_emails_from_text(sub_soup.get_text(), author_name))
            except: continue
    except: pass
    return list(set(all_emails))

def fetch_works_from_openalex_url(url, api_key=""):
    url_parts = list(urlparse.urlparse(url))
    query = dict(urlparse.parse_qsl(url_parts[4]))
    query['mailto'] = "natprz42@gmail.com"
    if api_key: query['api_key'] = api_key
    if "filter=" in url or "search=" in url:
        query['per-page'] = '100'
        if 'cursor' not in query: query['cursor'] = '*'
    all_works = []
    while True:
        url_parts[4] = urlencode(query)
        next_url = urlparse.urlunparse(url_parts)
        try:
            res = requests.get(next_url, timeout=15)
            if res.status_code != 200: break
            data = res.json()
            if 'results' in data:
                results = data.get('results', [])
                if not results: break
                all_works.extend(results)
                meta = data.get('meta', {})
                next_cursor = meta.get('next_cursor')
                if not next_cursor or query.get('cursor') == next_cursor: break
                query['cursor'] = next_cursor
                time.sleep(0.5)
            else:
                all_works.append(data)
                break
        except: break
    return all_works

def extract_openalex_work(work):
    doi = work.get('doi', '')
    if doi: doi = doi.replace('https://doi.org/', '')
    row_title = work.get('title', '')
    if not row_title: row_title = ""
    af, em_all, ad, rp, orcid_map = [], [], "", "", {}
    for authorship in work.get('authorships', []):
        author = authorship.get('author', {})
        name = author.get('display_name', '')
        if name: 
            af.append(name)
            if author.get('orcid'): orcid_map[name] = author.get('orcid').replace('https://orcid.org/', '')
        is_corr = authorship.get('is_corresponding', False)
        affils = authorship.get('raw_affiliation_strings', [])
        affil_str = " ".join(affils) if affils else authorship.get('raw_affiliation_string', '')
        if affil_str:
            ad += f"[{name}] {affil_str}; "
            if is_corr: rp += f"[{name}] {affil_str}; "
    return doi, row_title, af, em_all, ad, rp, orcid_map

def enforce_column_order(df, mode=""):
    base_order = ['Name', 'Surname', 'Email', 'ORCID', 'Country', 'Affiliation', 'DG Journal name', 'DG article title']
    if mode in ['merge', 'past']:
        for col in ['link', 'DG article DOI', 'DG article title']:
            if col in df.columns: df = df.drop(columns=[col])
            if col in base_order: base_order.remove(col)
        if mode == 'merge':
            if 'DG Journal name' in df.columns: df = df.drop(columns=['DG Journal name'])
            if 'DG Journal name' in base_order: base_order.remove('DG Journal name')
    else:
        base_order.extend(['link', 'DG article DOI'])
        
    base_order.append('Article Title')
    for col in base_order:
        if col not in df.columns: df[col] = "" 
            
    existing_desired = [col for col in base_order if col in df.columns]
    ignored_cols = ['batch_id', 'link', 'DG article DOI', 'DG article title']
    if mode == 'merge': ignored_cols.append('DG Journal name')
    other_cols = [col for col in df.columns if col not in base_order and col not in ignored_cols]
    return df[existing_desired + other_cols]

# Modified core loop for accumulation Point 3
def process_single_article(doi, row_title, af, em_all, ad, rp, df_row, orcid_map, opts, strategy, fast_mode, use_deep, accumulation_list, is_past=False, extra_data=None):
    for n in af:
        sn, fn = clean_author_name(n)
        if not sn: continue
        
        matched_email = get_matched_email(sn, em_all)
        if not matched_email and len(em_all) == 1 and sn.lower() in (rp or "").lower(): matched_email = em_all[0].lower()
        
        c = "Unknown"; affil_full = ""
        if ad:
            try:
                c = ad.split(f"[{n}]")[1].split(';')[0].split(',')[-1].strip() if f"[{n}]" in ad else ad.split(',')[-1].strip()
                affil_full = ad.split(f"[{n}]")[1].split(';')[0].strip() if f"[{n}]" in ad else ad.strip()
            except: pass

        affil_clean = re.sub(r'^\[.*?\]\s*', '', affil_full).strip() if affil_full else ""
        orcid_val = orcid_map.get(n, "") if orcid_map else (str(df_row.get('ORCID', '')) if df_row is not None else "")

        if not matched_email and use_deep and HAS_DDGS and not fast_mode:
            time.sleep(1.5)
            try:
                queries = [f'"{fn} {sn}" email'] if strategy == "google" else [f'"{fn} {sn}" {affil_clean if affil_clean and affil_clean != "Unknown" else c} email']
                with DDGS() as ddgs:
                    all_s = []
                    for q in queries:
                        if matched_email: break
                        res = list(ddgs.text(q, max_results=3, backend="html"))
                        for r in res:
                            all_s.extend(extract_emails_from_text(r.get('body', '') + " " + r.get('title', ''), n))
                        matched_email = get_matched_email(sn, list(set(all_s)))
            except: pass

        rec = {"Name": fn.strip(), "Surname": sn.strip(), "Email": matched_email, "ORCID": orcid_val, "Country": c, "Affiliation": affil_clean, "Article Title": row_title}
        if extra_data: rec.update(extra_data)
        if is_past: rec["is_corr"] = sn.lower() in (rp or "").lower()
        
        if df_row is not None:
            for opt_key, cols in ADV_FIELDS.items():
                if opts.get(opt_key, False):
                    rec[opt_key] = next((str(df_row.get(col, "")) for col in cols if col in df_row.index), "")

        accumulation_list.append(rec)

# POINT 1: Function to update progress with remaining time (ETA)
def update_progress_with_eta(placeholder, bar, current, total, start_time, base_text):
    if total <= 0: return
    bar.progress(min(current/total, 1.0))
    elapsed = time.time() - start_time
    if current > 0:
        avg_time_per_record = elapsed / current
        remaining_records = total - current
        eta_seconds = int(remaining_records * avg_time_per_record)
        
        mins, secs = divmod(eta_seconds, 60)
        if current < total:
            # English labels for Point 3
            eta_text = f"⏳ {base_text}: {current}/{total} | Time Left: ~{mins}m {secs}s"
        else:
            eta_text = f"✅ {base_text}: Completed!"
        placeholder.text(eta_text)
    else:
        placeholder.text(f"⏳ {base_text}: {current}/{total} | Calculating time...")

def to_excel_buffer(list_data):
    df = pd.DataFrame(list_data)
    if df.empty: return io.BytesIO()
    
    # Sanitize and dedup
    for col in df.select_dtypes(include=['object']):
        df[col] = df[col].apply(lambda x: re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', str(x)) if pd.notna(x) else x)
        
    if 'Email' in df.columns:
        df = pd.concat([df[df['Email'] != ''].drop_duplicates(subset=['Email']), df[df['Email'] == ''].drop_duplicates(subset=['Name', 'Surname'])])
            
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df.to_excel(writer, index=False)
    return output.getvalue()

def to_excel_multisheet_buffer(df_dict):
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        for sheet_name, df in df_dict.items():
            df.to_excel(writer, sheet_name=sheet_name, index=False)
    return output.getvalue()

def load_lottieurl(url: str):
    r = requests.get(url)
    if r.status_code != 200: return None
    return r.json()

# Minimalist, professional Lottie animated owl for academics
# Source: Public available minimalist lottie JSON
lottie_owl = load_lottieurl("https://assets5.lottiefiles.com/packages/lf20_m9ubp8eg.json")

# --- UI COMPONENTS ---
def render_kombajn_ui(prefix):
    st.markdown("#### ⚙️ Operation Mode")
    fast = st.checkbox("⚡ FAST MODE (No web search)", key=f"{prefix}_fast")
    strat = st.radio("Search strategy:", ["Google-Style (Broad)", "Affiliation (Specific)"], key=f"{prefix}_strat")
    strat_val = "google" if "Google" in strat else "affil"
    col1, col2, col3 = st.columns(3)
    api = col1.checkbox("Search in API (ORCID)", value=True, key=f"{prefix}_api")
    pdf = col2.checkbox("Read PDF (Disabled in Cloud)", value=False, disabled=True, key=f"{prefix}_pdf") # PDFs need local processing
    deep = col3.checkbox("Deep Web Hunt (Hunter)", value=False, disabled=fast, key=f"{prefix}_deep")
    return fast, strat_val, api, pdf, deep

def render_options_ui(prefix):
    st.markdown("#### 📊 Additional Columns to Copy")
    opts = {}
    cols = st.columns(3)
    for i, key in enumerate(ADV_FIELDS.keys()):
        opts[key] = cols[i % 3].checkbox(key, value=True, key=f"{prefix}_opt_{key}")
    return opts

# --- MAIN APP ---
st.title("📚 ScholarHunt") # Renamed Point 2
st.markdown("Professional Scientific Contacts Management")

with st.sidebar:
    # Point 4: Animated Owl Branding
    if lottie_owl:
        st_lottie(lottie_owl, height=180, key="owl")
    else:
        st.write("🦉")
    st.markdown("---")
    st.header("⚙️ Global Settings")
    global_api_key = st.text_input("OpenAlex API Key (Optional):", type="password")
    if not HAS_DDGS: st.error("⚠️ Error loading 'ddgs' package.")

tabs = st.tabs(["📖 Guide", "🗂️ Merge", "👥 Past Authors", "🔑 Keywords", "📜 Cited Outreach", "💬 Citing Outreach", "✅ Validation", "🕵️ Hunter"])

# --- 0. GUIDE ---
with tabs[0]:
    st.markdown("""
    ## WELCOME TO SCHOLARHUNT!
    The following guide will help you understand what each tab is for.
    """)
    
    col1, col2 = st.columns([1,2])
    
    with col1:
        st.markdown("#### ⚡ FAST MODE")
        st.markdown("* If your database has thousands of records, check the 'FAST MODE' option. The program will skip time-consuming web searches.")

    with col2:
        # POINT 2: REMOVED GLOSSARY SECTION
        st.markdown("#### 📖 Tab Guide")
        st.markdown("""
        * **🗂️ 1. Merge:** Combining Excel/WoS file batches into a single list.
        * **👥 2. Past Authors:** Splits files into Corresponding and Co-Authors tabs.
        * **🔑 3. Keywords:** Generates a ready-to-use keywords list.
        * **📜 4. Cited Outreach (Bibliography):** Management for authors cited by Base Papers.
        * **💬 5. Citing Outreach (Citations):** Management for authors citing Base Papers.
        * **✅ 6. Validation:** Intelligence-based mailing list cleanup.
        * **🕵️ 7. Hunter:** Cascading ORCID and Web search for missing emails.
        """)

# --- 1. MERGE ---
with tabs[1]:
    st.header("🗂️ Merge Files")
    m_files = st.file_uploader("📂 Upload WoS/Excel files", accept_multiple_files=True, key="m_f")
    m_oa = st.text_input("🌐 OpenAlex Mass Link (filter=... or search=...)", key="m_oa")
    
    col_k1, col_k2 = st.columns([2, 1])
    with col_k1:
        m_fast, m_strat, m_api, m_pdf, m_deep = render_kombajn_ui("m")
    with col_k2:
        m_opts = render_options_ui("m")
    
    # Point 3: Autosave download button
    btn_col1, btn_col2 = st.columns([1,1])
    
    if btn_col1.button("🚀 RUN MERGE", type="primary"):
        st.session_state.merge_accumulation = [] # Point 3: Reset accumulation
        prog_bar_p = st.empty()
        prog_bar = prog_bar_p.progress(0)
        eta_placeholder = st.empty()
        
        # Determine total work for ETA
        total_work = 0
        works_list_api = []
        df_list_local = []
        
        if m_oa and "api.openalex.org" in m_oa:
            with st.spinner("Fetching Mass Link..."):
                works_list_api = fetch_works_from_openalex_url(m_oa, global_api_key)
                total_work += len(works_list_api) * 2 # Assumed factor

        if m_files:
            for f in m_files:
                df = pd.read_excel(f) if f.name.endswith(('xls', 'xlsx')) else pd.read_csv(f)
                df_list_local.append(df)
                total_work += len(df)
        
        if total_work == 0:
            st.warning("No data found.")
            st.stop()

        current_record = 0
        start_time = time.time()
        
        # Point 1: Added ETA tracking
        for work in works_list_api:
            doi, title, af, ems, ad, rp, pdf_url, orcid_map = extract_openalex_work(work)
            process_single_article(doi, title, af, ems, ad, rp, None, orcid_map, m_opts, m_strat, m_fast, m_deep, st.session_state.merge_accumulation)
            current_record += 1
            update_progress_with_eta(eta_placeholder, prog_bar, current_record, total_work, start_time, "Merging API Data")

        for df in df_list_local:
            for _, r in df.iterrows():
                doi, title, af, ems, ad, rp = extract_universal_data(df, r)
                process_single_article(doi, title, af, ems, ad, rp, r, {}, m_opts, m_strat, m_fast, m_deep, st.session_state.merge_accumulation)
                current_record += 1
                update_progress_with_eta(eta_placeholder, prog_bar, current_record, total_work, start_time, "Merging Files")
                
        if st.session_state.merge_accumulation:
            out_df = pd.DataFrame(st.session_state.merge_accumulation)
            out_df = enforce_column_order(out_df, 'merge')
            if 'Email' in out_df.columns:
                out_df = pd.concat([out_df[out_df['Email'] != ''].drop_duplicates(subset=['Email']), out_df[out_df['Email'] == ''].drop_duplicates(subset=['Name', 'Surname'])])
            
            eta_placeholder.text("✅ Completed!")
            # Point 4: Bonus Balloons
            st.balloons()
            st.success(f"Successfully merged. Found {len(out_df)} unique authors.")
            st.download_button("💾 Download Final Merge.xlsx", data=to_excel_buffer(st.session_state.merge_accumulation), file_name="Merge_Result.xlsx")

    # Point 3: Autosave download button always present next to progress
    if st.session_state.merge_accumulation:
        btn_col2.download_button("💾 Download Emergency Autosave (Current Progress)", data=to_excel_buffer(st.session_state.merge_accumulation), file_name="Merge_Autosave.xlsx", key="m_auto_btn")

# --- 2. PAST AUTHORS ---
with tabs[2]:
    st.header("👥 Past Authors Split")
    p_files = st.file_uploader("📂 Upload files", accept_multiple_files=True, key="p_f")
    p_oa = st.text_input("🌐 OpenAlex Mass Link", key="p_oa")
    p_jrnl = st.text_input("DG Journal name (for new records):", key="p_jrnl")
    
    col_kp1, col_kp2 = st.columns([2, 1])
    with col_kp1:
        p_fast, p_strat, p_api, p_pdf, p_deep = render_kombajn_ui("p")
    with col_kp2:
        p_opts = render_options_ui("p")
    
    # Point 3: Autosave download button
    btn_p_col1, btn_p_col2 = st.columns([1,1])
    
    if btn_p_col1.button("🚀 RUN SPLIT", type="primary"):
        st.session_state.past_accumulation = [] # Reset Point 3
        prog_bar = st.progress(0)
        eta_placeholder = st.empty()
        
        extra = {"DG Journal name": p_jrnl}
        works_list_api = []
        df_list_local = []
        total_work = 0
        
        if p_oa and "api.openalex.org" in p_oa:
            with st.spinner("Fetching Mass Link..."):
                works_list_api = fetch_works_from_openalex_url(p_oa, global_api_key)
                total_work += len(works_list_api) * 2

        if p_files:
            for f in p_files:
                df = pd.read_excel(f) if f.name.endswith(('xls', 'xlsx')) else pd.read_csv(f)
                df_list_local.append(df)
                total_work += len(df)
                
        if total_work == 0:
            st.stop()

        current_record = 0
        start_time = time.time()

        for work in works_list_api:
            doi, title, af, ems, ad, rp, pdf_url, orcid_map = extract_openalex_work(work)
            # accumulating results Point 3
            process_single_article(doi, title, af, ems, ad, rp, None, orcid_map, p_opts, p_strat, p_fast, p_deep, st.session_state.past_accumulation, True, extra)
            current_record += 1
            # ETA Point 1
            update_progress_with_eta(eta_placeholder, prog_bar, current_record, total_work, start_time, "Analyzing API Authors")

        for df in df_list_local:
            for _, r in df.iterrows():
                doi, title, af, ems, ad, rp = extract_universal_data(df, r)
                # accumulating results Point 3
                process_single_article(doi, title, af, ems, ad, rp, r, {}, p_opts, p_strat, p_fast, p_deep, st.session_state.past_accumulation, True, extra)
                current_record += 1
                # ETA Point 1
                update_progress_with_eta(eta_placeholder, prog_bar, current_record, total_work, start_time, "Analyzing File Authors")

        if st.session_state.past_accumulation:
            out_df = pd.DataFrame(st.session_state.past_accumulation)
            out_df = enforce_column_order(out_df, 'past')
            if 'Email' in out_df.columns:
                out_df = pd.concat([out_df[out_df['Email'] != ''].drop_duplicates(subset=['Email']), out_df[out_df['Email'] == ''].drop_duplicates(subset=['Name', 'Surname'])])

            df_corr = out_df[out_df['is_corr']==True].drop(columns=['is_corr'], errors='ignore')
            df_co = out_df[out_df['is_corr']==False].drop(columns=['is_corr'], errors='ignore')
            
            eta_placeholder.text("✅ Completed!")
            col1, col2 = st.columns(2)
            col1.download_button("💾 Download Corresponding.xlsx", data=to_excel_buffer(df_corr.to_dict('records')), file_name="Corresponding.xlsx")
            col2.download_button("💾 Download CoAuthors.xlsx", data=to_excel_buffer(df_co.to_dict('records')), file_name="CoAuthors.xlsx")

    # Point 3: Autosave download button
    if st.session_state.past_accumulation:
        btn_p_col2.download_button("💾 Download Emergency Autosave (Current Progress)", data=to_excel_buffer(st.session_state.past_accumulation), file_name="Past_Authors_Autosave.xlsx", key="p_auto_btn")

# --- 3. KEYWORDS ---
with tabs[3]:
    st.header("🔑 Keywords List Generation")
    k_files = st.file_uploader("📂 Upload files", accept_multiple_files=True, key="k_f")
    col1, col2 = st.columns(2)
    k_jrnl = col1.text_input("DG Journal name:", key="k_j")
    k_kw = col2.text_input("DG Keyword:", key="k_kw")
    k_title = col1.text_input("DG article title:", key="k_t")
    k_link = col2.text_input("Link:", key="k_l")
    k_doi = col1.text_input("DG article DOI:", key="k_d")
    
    col_kk1, col_kk2 = st.columns([2, 1])
    with col_kk1:
        k_fast, k_strat, k_api, k_pdf, k_deep = render_kombajn_ui("k")
    with col_kk2:
        k_opts = render_options_ui("k")
    
    # Point 3: Autosave download button
    btn_k_col1, btn_k_col2 = st.columns([1,1])
    
    if btn_k_col1.button("🚀 GENERATE KEYWORDS LIST", type="primary"):
        st.session_state.keywords_accumulation = [] # Reset Point 3
        prog_bar = st.progress(0)
        eta_placeholder = st.empty()
        
        extra = {"DG Journal name": k_jrnl, "DG Keyword": k_kw, "DG article title": k_title, "link": k_link, "DG article DOI": k_doi}
        total_work = 0
        dfs_list = []
        if k_files:
            for f in k_files:
                df = pd.read_excel(f) if f.name.endswith(('xls', 'xlsx')) else pd.read_csv(f)
                dfs_list.append(df)
                total_work += len(df)
        
        if total_work == 0:
            st.stop()
            
        current_record = 0
        start_time = time.time()
        
        for df in dfs_list:
            for _, r in df.iterrows():
                doi, title, af, ems, ad, rp = extract_universal_data(df, r)
                # Accumulation Point 3
                process_single_article(doi, title, af, ems, ad, rp, r, {}, k_opts, k_strat, k_fast, k_deep, st.session_state.keywords_accumulation, False, extra)
                current_record += 1
                # ETA Point 1
                update_progress_with_eta(eta_placeholder, prog_bar, current_record, total_work, start_time, "Generating List")
                
        if st.session_state.keywords_accumulation:
            eta_placeholder.text("✅ Completed!")
            st.success("Keywords list generated.")
            st.download_button("💾 Download Final Keywords List.xlsx", data=to_excel_buffer(st.session_state.keywords_accumulation), file_name="Keywords_List.xlsx")

    # Point 3: Autosave download button
    if st.session_state.keywords_accumulation:
        btn_k_col2.download_button("💾 Download Emergency Autosave (Current Progress)", data=to_excel_buffer(st.session_state.keywords_accumulation), file_name="Keywords_Autosave.xlsx", key="k_auto_btn")

# --- 4. CITED ---
with tabs[4]:
    st.header("📜 Cited Outreach (Backwards/References)")
    
    # Emergency Download for Auto-Pilot Point 3
    if 'cited_auto_accumulation' not in st.session_state: st.session_state.cited_auto_accumulation = []
    if 'cited_auto_batches' not in st.session_state: st.session_state.cited_auto_batches = []
    
    col_c1, col_c2 = st.columns([2, 1])
    with col_c1:
        c_fast, c_strat, c_api, c_pdf, c_deep = render_kombajn_ui("c")
    with col_c2:
        c_opts = render_options_ui("c")
    
    st.markdown("---")
    st.subheader("Mode 1: Manual Batch Load (from files)")
    c_man_files = st.file_uploader("📂 Upload references file (Excel/WoS)", accept_multiple_files=True, key="c_man_f")
    c1, c2 = st.columns(2)
    c_man_jrnl = c1.text_input("DG Journal name:", key="c_man_j")
    c_man_title = c2.text_input("DG article title:", key="c_man_t")
    c_man_link = c1.text_input("Link:", key="c_man_l")
    c_man_doi = c2.text_input("DG article DOI:", key="c_man_d")
    
    if st.button("➕ LOAD AS BATCH (Manual)"):
        if c_man_files and c_man_title:
            batch_id = str(time.time())
            extra = {"DG Journal name": c_man_jrnl, "DG article title": c_man_title, "link": c_man_link, "DG article DOI": c_man_doi}
            added_count = 0
            
            prog_p = st.empty()
            prog_bar = prog_p.progress(0)
            works_dfs = []
            
            total_records = 0
            with st.spinner("Loading files..."):
                for f in c_man_files:
                    df = pd.read_excel(f) if f.name.endswith(('xls', 'xlsx')) else pd.read_csv(f)
                    works_dfs.append(df)
                    total_records += len(df)
            
            records_accumulation = []
            
            for df in works_dfs:
                for _, r in df.iterrows():
                    doi, title, af, ems, ad, rp = extract_universal_data(df, r)
                    # Accumulation for specific batch Point 3
                    process_single_article(doi, title, af, ems, ad, rp, r, {}, c_opts, c_strat, c_fast, c_deep, records_accumulation, False, extra)
                    added_count += len(af) if af else 1
                    if total_records > 0: prog_bar.progress(min(added_count/total_records, 1.0))

            for res in records_accumulation: res['batch_id'] = batch_id
            st.session_state.cited_db.extend(records_accumulation)
            st.session_state.cited_batches.append({'batch_id': batch_id, 'Journal': c_man_jrnl, 'Title': c_man_title, 'Count': added_count})
            prog_p.empty()
            st.success("Files added as a batch to the management table below.")
        else:
            st.warning("Please upload files and provide an article title.")

    st.markdown("---")
    st.subheader("Mode 2: Auto-Pilot (Fetch references from OpenAlex)")
    c_jrnl_auto = st.text_input("DG Journal name (for new records):", key="c_auto_j")
    c_doi_auto = st.text_input("Base Paper DOI or Mass OpenAlex Link:", key="c_auto_d")
    
    # Point 3 Autosave Download Buttons
    btn_ca_col1, btn_ca_col2 = st.columns([1,1])
    
    if btn_ca_col1.button("🚀 RUN CITED AUTO-PILOT", type="primary"):
        st.session_state.cited_auto_accumulation = [] # Reset accumulation Point 3
        
        if not c_doi_auto:
            st.stop()
            
        base_works = []
        headers = {'api_key': global_api_key} if global_api_key else {}
        
        with st.spinner("Connecting to OpenAlex..."):
            if "api.openalex.org" in c_doi_auto and ("filter=" in c_doi_auto or "search=" in c_doi_auto):
                # Mass Link Handling
                base_works = fetch_works_from_openalex_url(c_doi_auto, global_api_key)
            else:
                clean_doi = re.search(r'10\.\d{4,9}/[-._;()/:a-zA-Z0-9]+', c_doi_auto)
                clean_doi = clean_doi.group(0) if clean_doi else c_doi_auto.replace('https://doi.org/', '').strip()
                res = requests.get(f"https://api.openalex.org/works/https://doi.org/{clean_doi}", headers=headers).json()
                if 'id' in res: base_works = [res]
        
        if not base_works:
            st.error("No works found.")
            st.stop()
            
        added_count = 0
        total_base = len(base_works)
        prog_bar = st.progress(0)
        eta_placeholder = st.empty()
        
        # We don't add directly to cited_db, but to accumulation Point 3
        # If user stops, they use emergency download. If finished, we append to DB.
        
        for idx, b_work in enumerate(base_works):
            b_title = b_work.get('title', 'Unknown')
            b_doi = b_work.get('doi', '').replace('https://doi.org/', '')
            update_progress_with_eta(eta_placeholder, prog_bar, idx+1, total_base, time.time()-(idx*2), f"Fetch references for paper {idx+1}/{total_base}") # Fake start time for ETA simulation on base papers fetch
            
            ref_urls = b_work.get('referenced_works', [])
            extra = {"DG Journal name": c_jrnl_auto, "DG article title": b_title, "link": f"https://doi.org/{b_doi}" if b_doi else "", "DG article DOI": b_doi}
            
            if ref_urls:
                ref_ids = [ref.split('/')[-1] for ref in ref_urls]
                for k in range(0, len(ref_ids), 50):
                    works = fetch_works_from_openalex_url("https://api.openalex.org/works?filter=openalex:" + "|".join(ref_ids[k:k+50]), global_api_key)
                    for work in works:
                        w_doi, w_title, af, ems, ad, rp, orcid_map = extract_openalex_work(work)
                        # Accumulate Point 3
                        # Need to adjust core loop to not sleep if ETA function already sleeps/waits. Currently process_single handles its own timing.
                        process_single_article(w_doi, w_title, af, ems, ad, rp, None, orcid_map, c_opts, c_strat, c_fast, c_deep, st.session_state.cited_auto_accumulation, False, extra)
                        added_count += len(af) if af else 1
            prog_bar.progress(min((idx+1)/total_base, 1.0))

        if st.session_state.cited_auto_accumulation:
            batch_id = str(time.time())
            # Attach batch_id to the successfully accumulated records
            for res in st.session_state.cited_auto_accumulation: res['batch_id'] = batch_id
            
            st.session_state.cited_db.extend(st.session_state.cited_auto_accumulation)
            
            title_b = f"Mass Link ({total_base} papers)" if total_base > 1 else f"Auto-Pilot ({base_works[0].get('title', 'Unknown')[:25]}...)"
            st.session_state.cited_batches.append({'batch_id': batch_id, 'Journal': c_jrnl_auto, 'Title': title_b, 'Count': len(st.session_state.cited_auto_accumulation)})
            
            eta_placeholder.text("✅ Completed!")
            st.success("Auto-Pilot added references to management table.")
    
    # Point 3: Autosave download button
    if st.session_state.cited_auto_accumulation:
        btn_ca_col2.download_button("💾 Download Emergency Autosave (Current Progress)", data=to_excel_buffer(st.session_state.cited_auto_accumulation), file_name="Cited_Autopilot_Autosave.xlsx", key="ca_auto_btn")

    # MANAGEMENT TABLE
    if st.session_state.cited_batches:
        st.markdown("---")
        st.write(f"📊 **Total Unqiue Database Entries:** {len(pd.DataFrame(st.session_state.cited_db).drop_duplicates(subset=['Name', 'Surname']) if st.session_state.cited_db else [])}")
        st.markdown("#### Management Table")
        
        df_batches = pd.DataFrame(st.session_state.cited_batches)
        st.dataframe(df_batches[['Journal', 'Title', 'Count']], use_container_width=True)
        
        col_del1, col_del2 = st.columns([3, 1])
        batch_to_delete = col_del1.selectbox("Select row to delete:", options=st.session_state.cited_batches, format_func=lambda x: f"{x['Title']} ({x['Count']} items)", key="c_del_sel")
        
        if col_del2.button("🗑️ DELETE SELECTED ROW"):
            st.session_state.cited_db = [r for r in st.session_state.cited_db if r.get('batch_id') != batch_to_delete['batch_id']]
            st.session_state.cited_batches = [b for b in st.session_state.cited_batches if b['batch_id'] != batch_to_delete['batch_id']]
            st.rerun()
            
        out_df = enforce_column_order(pd.DataFrame(st.session_state.cited_db).drop_duplicates(subset=['Name', 'Surname']), 'cited')
        st.download_button("💾 EXPORT CITED RESULTS", data=to_excel_buffer(st.session_state.cited_db), file_name="Cited_Outreach_Base.xlsx")

# --- 5. CITING ---
with tabs[5]:
    st.header("💬 Citing Outreach (Forwards/Citations)")
    
    # Emergency Download for Auto-Pilot Point 3
    if 'citing_auto_accumulation' not in st.session_state: st.session_state.citing_auto_accumulation = []
    
    col_ci1, col_ci2 = st.columns([2, 1])
    with col_ci1:
        ci_fast, ci_strat, ci_api, ci_pdf, ci_deep = render_kombajn_ui("ci")
    with col_ci2:
        ci_opts = render_options_ui("ci")
    
    st.markdown("---")
    st.subheader("Mode 1: Manual Batch Load (from files)")
    ci_man_files = st.file_uploader("📂 Upload Citations file (Excel/WoS)", accept_multiple_files=True, key="ci_man_f")
    ci1, ci2 = st.columns(2)
    ci_man_jrnl = ci1.text_input("DG Journal name:", key="ci_man_j")
    ci_man_title = ci2.text_input("DG article title:", key="ci_man_t")
    ci_man_link = ci1.text_input("Link:", key="ci_man_l")
    ci_man_doi = ci2.text_input("DG article DOI:", key="ci_man_d")
    
    if st.button("➕ LOAD AS BATCH (Manual)", key="ci_man_btn"):
        if ci_man_files and ci_man_title:
            batch_id = str(time.time())
            extra = {"DG Journal name": ci_man_jrnl, "DG article title": ci_man_title, "link": ci_man_link, "DG article DOI": ci_man_doi}
            added_count = 0
            
            works_dfs = []
            total_records = 0
            with st.spinner("Loading files..."):
                for f in ci_man_files:
                    df = pd.read_excel(f) if f.name.endswith(('xls', 'xlsx')) else pd.read_csv(f)
                    works_dfs.append(df)
                    total_records += len(df)
            
            prog_bar = st.progress(0)
            records_accumulation = []
            
            for df in works_dfs:
                for _, r in df.iterrows():
                    doi, title, af, ems, ad, rp = extract_universal_data(df, r)
                    process_single_article(doi, title, af, ems, ad, rp, r, {}, ci_opts, ci_strat, ci_fast, ci_deep, records_accumulation, False, extra)
                    added_count += len(af) if af else 1
                    if total_records > 0: prog_bar.progress(min(added_count/total_records, 1.0))

            for res in records_accumulation: res['batch_id'] = batch_id
            st.session_state.citing_db.extend(records_accumulation)
            st.session_state.citing_batches.append({'batch_id': batch_id, 'Journal': ci_man_jrnl, 'Title': ci_man_title, 'Count': added_count})
            prog_bar.empty()
            st.success("Files added as a batch to the management table below.")
        else:
            st.warning("Please upload files and provide an article title.")

    st.markdown("---")
    st.subheader("Mode 2: Auto-Pilot (Fetch citations from OpenAlex)")
    ci_jrnl_auto = st.text_input("DG Journal name (for new records):", key="ci_auto_j")
    ci_doi_auto = st.text_input("Base Paper DOI or Mass OpenAlex Link:", key="ci_auto_d")
    
    # Point 3 Autosave Download Buttons
    btn_cia_col1, btn_cia_col2 = st.columns([1,1])
    
    if btn_cia_col1.button("🚀 RUN CITING AUTO-PILOT", type="primary"):
        st.session_state.citing_auto_accumulation = [] # Reset Point 3
        
        if not ci_doi_auto:
            st.stop()
            
        base_works = []
        headers = {'api_key': global_api_key} if global_api_key else {}
        
        with st.spinner("Connecting to OpenAlex..."):
            if "api.openalex.org" in ci_doi_auto and ("filter=" in ci_doi_auto or "search=" in ci_doi_auto):
                # Mass Link Handling
                base_works = fetch_works_from_openalex_url(ci_doi_auto, global_api_key)
            else:
                clean_doi = re.search(r'10\.\d{4,9}/[-._;()/:a-zA-Z0-9]+', ci_doi_auto)
                clean_doi = clean_doi.group(0) if clean_doi else ci_doi_auto.replace('https://doi.org/', '').strip()
                res = requests.get(f"https://api.openalex.org/works/https://doi.org/{clean_doi}", headers=headers).json()
                if 'id' in res: base_works = [res]
        
        if not base_works:
            st.error("No works found.")
            st.stop()
            
        added_count = 0
        total_base = len(base_works)
        prog_bar = st.progress(0)
        eta_placeholder = st.empty()
        
        for idx, b_work in enumerate(base_works):
            b_title = b_work.get('title', 'Unknown')
            b_doi = b_work.get('doi', '').replace('https://doi.org/', '')
            b_id = b_work.get('id', '').split('/')[-1]
            update_progress_with_eta(eta_placeholder, prog_bar, idx+1, total_base, time.time()-(idx*2), f"Fetch citations for paper {idx+1}/{total_base}") 
            
            extra = {"DG Journal name": ci_jrnl_auto, "DG article title": b_title, "link": f"https://doi.org/{b_doi}" if b_doi else "", "DG article DOI": b_doi}
            
            works = fetch_works_from_openalex_url(f"https://api.openalex.org/works?filter=cites:{b_id}", global_api_key)
            for work in works:
                w_doi, w_title, af, ems, ad, rp, orcid_map = extract_openalex_work(work)
                # Accumulate Point 3
                process_single_article(w_doi, w_title, af, ems, ad, rp, None, orcid_map, ci_opts, ci_strat, ci_fast, ci_deep, st.session_state.citing_auto_accumulation, False, extra)
                added_count += len(af) if af else 1
            prog_bar.progress(min((idx+1)/total_base, 1.0))

        if st.session_state.citing_auto_accumulation:
            batch_id = str(time.time())
            # Attach batch_id to the successfully accumulated records Point 3
            for res in st.session_state.citing_auto_accumulation: res['batch_id'] = batch_id
            
            st.session_state.citing_db.extend(st.session_state.citing_auto_accumulation)
            
            title_b = f"Mass Link ({total_base} papers)" if total_base > 1 else f"Auto-Pilot ({base_works[0].get('title', 'Unknown')[:25]}...)"
            st.session_state.citing_batches.append({'batch_id': batch_id, 'Journal': ci_jrnl_auto, 'Title': title_b, 'Count': len(st.session_state.citing_auto_accumulation)})
            
            eta_placeholder.text("✅ Completed!")
            st.success("Auto-Pilot added citing papers to management table.")

    # Point 3: Autosave download button
    if st.session_state.citing_auto_accumulation:
        btn_cia_col2.download_button("💾 Download Emergency Autosave (Current Progress)", data=to_excel_buffer(st.session_state.citing_auto_accumulation), file_name="Citing_Autopilot_Autosave.xlsx", key="cia_auto_btn")

    # MANAGEMENT TABLE
    if st.session_state.citing_batches:
        st.markdown("---")
        st.write(f"📊 **Total Database Entries:** {len(st.session_state.citing_db)}")
        
        df_batches = pd.DataFrame(st.session_state.citing_batches)
        st.dataframe(df_batches[['Journal', 'Title', 'Count']], use_container_width=True)
        
        col_del1, col_del2 = st.columns([3, 1])
        batch_to_delete = col_del1.selectbox("Select row to delete:", options=st.session_state.citing_batches, format_func=lambda x: f"{x['Title']} ({x['Count']} items)", key="ci_del_sel")
        
        if col_del2.button("🗑️ DELETE SELECTED ROW", key="clear_ci"):
            st.session_state.citing_db = [r for r in st.session_state.citing_db if r.get('batch_id') != batch_to_delete['batch_id']]
            st.session_state.citing_batches = [b for b in st.session_state.citing_batches if b['batch_id'] != batch_to_delete['batch_id']]
            st.rerun()
            
        out_df = enforce_column_order(pd.DataFrame(st.session_state.citing_db).drop_duplicates(subset=['Name', 'Surname']), 'citing')
        st.download_button("💾 EXPORT CITING RESULTS", data=to_excel_buffer(st.session_state.citing_db), file_name="Citing_Outreach_Base.xlsx")

# --- 6. VALIDATION ---
with tabs[6]:
    st.header("✅ Intelligent Validation")
    v_file = st.file_uploader("📂 Select Excel File", type=["xlsx", "xls"], key="v_f")
    if v_file and st.button("🔍 START VALIDATION", type="primary"):
        df = pd.read_excel(v_file)
        em_col = next((c for c in df.columns if 'mail' in str(c).lower()), None)
        sn_col = next((c for c in df.columns if 'surname' in str(c).lower() or 'nazwisko' in str(c).lower()), None)
        c_col = next((c for c in df.columns if 'country' in str(c).lower() or 'kraj' in str(c).lower()), None)
        
        if em_col and sn_col:
            prog_bar = st.progress(0)
            eta_placeholder = st.empty()
            start_time = time.time()
            status_list = []
            for i, row in df.iterrows():
                em, sn = str(row[em_col]), str(row[sn_col])
                ct = str(row[c_col]) if c_col else ""
                status_list.append(validate_email_intelligence(em, sn, ct) if em and em != 'nan' else "Missing Email")
                # ETA Point 1
                update_progress_with_eta(eta_placeholder, prog_bar, i+1, len(df), start_time, "Validating Emails")
                
            df['Smart Status'] = status_list
            sheets = {
                'Certain': df[df['Smart Status'].str.startswith('Certain')],
                'Probable': df[df['Smart Status'].str.startswith('Probable')],
                'To_Verify': df[df['Smart Status'] == 'Manual Verification Required'],
                'Invalid': df[df['Smart Status'].str.contains('Invalid|Missing')]
            }
            eta_placeholder.text("✅ Validation completed.")
            st.download_button("💾 Download Validation Report.xlsx", data=to_excel_multisheet_buffer(sheets), file_name="Validated_Report.xlsx")
        else:
            st.error("Required columns missing: Email, Surname.")

# --- 7. HUNTER ---
with tabs[7]:
    st.header("🕵️ Cascading Email Hunter")
    
    # Emergency Download Point 3
    if 'hunter_accumulation' not in st.session_state: st.session_state.hunter_accumulation = []
    
    h_file = st.file_uploader("📂 Upload database with missing emails", type=["xlsx", "xls"], key="h_f")
    h_strat = st.radio("Search Strategy:", ["Google-Style (Surname + Email)", "Affiliation (Surname + Institution)"], key="h_s")
    is_deep = st.checkbox("🕸️ DEEP SCAN (Hunter - Takes more time)", value=False)
    
    # Point 3 Autosave Download Buttons
    btn_h_col1, btn_h_col2 = st.columns([1,1])
    
    if btn_h_col1.button("🚀 RUN HUNTER", type="primary"):
        st.session_state.hunter_accumulation = [] # Reset Point 3 accumulation
        
        df = pd.read_excel(h_file)
        
        email_col = next((c for c in df.columns if 'mail' in c.lower()), 'Email')
        if email_col not in df.columns: df[email_col] = ""
        df[email_col] = df[email_col].astype(object)
        
        surname_col = next((c for c in df.columns if 'surname' in c.lower() or 'nazwisko' in c.lower()), 'Surname')
        name_col = next((c for c in df.columns if 'name' in c.lower() and 'surname' not in c.lower()), 'Name')
        aff_col = next((c for c in df.columns if 'country' in c.lower() or 'affil' in c.lower()), None)
        orcid_col = next((c for c in df.columns if 'orcid' in c.lower()), None)
        
        prog_bar = st.progress(0)
        eta_placeholder = st.empty()
        start_time = time.time()
        found_count = 0
        total = len(df)
        h_strategy = "google" if "Google" in h_strat else "affil"
        
        for idx, row in df.iterrows():
            sn = str(row.get(surname_col, '')).strip()
            nm = str(row.get(name_col, '')).strip()
            aff = str(row.get(aff_col, '')).strip()
            orc = str(row.get(orcid_col, '')).strip()
            if 'orcid.org/' in orc: orc = orc.split('/')[-1]
            
            # Record Point 3: We don't accumulate yet, we accumulate the whole df row later if email found
            # Current progress ETA Point 1
            update_progress_with_eta(eta_placeholder, prog_bar, idx+1, total, start_time, f"Hunting: {nm} {sn}")

            if pd.isna(sn) or sn == "" or '@' in str(row.get(email_col, '')): 
                continue
                
            email_found = ""; all_s = []

            if orc and len(orc) >= 15:
                o_emails, o_urls = get_emails_from_orcid(orc)
                all_s.extend(o_emails)
                if is_deep:
                    for u in o_urls[:3]: all_s.extend(scrape_deep(u, nm + " " + sn))
                email_found = get_matched_email(sn, list(set(all_s)))

            if not email_found and HAS_DDGS:
                time.sleep(1.8) # Anti-bot delay
                queries = [f'"{nm} {sn}" email', f'"{sn}" email contact'] if h_strategy == "google" else [f'"{nm} {sn}" {aff if aff and aff.lower()!="nan" else "university"} email']
                try:
                    with DDGS() as ddgs:
                        for q in queries:
                            if email_found: break
                            res = list(ddgs.text(q, max_results=3, backend="html"))
                            for r in res:
                                snippet = r.get('body', '') + " " + r.get('title', '')
                                all_s.extend(extract_emails_from_text(snippet, nm + " " + sn))
                                if is_deep and 'href' in r:
                                    url = r.get('href', '')
                                    if url and not any(x in url.lower() for x in ['facebook', 'twitter', 'linkedin', 'researchgate', 'youtube']):
                                        all_s.extend(scrape_deep(url, nm + " " + sn))
                            email_found = get_matched_email(sn, list(set(all_s)))
                exceptException as e: pass

            if email_found:
                df.at[idx, email_col] = email_found
                found_count += 1
            
            # Point 3: Convert current row state to dict and accumulate
            row_dict = row.to_dict()
            if email_found: row_dict[email_col] = email_found # Ensure found email is in state
            st.session_state.hunter_accumulation.append(row_dict)
            
        eta_placeholder.text(f"✅ Completed! Found {found_count} records.")
        # Point 4: Bonus Balloons
        st.balloons()
        
        # Must sanitize before excel download
        for col in df.select_dtypes(include=['object']):
            df[col] = df[col].apply(lambda x: re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', str(x)) if pd.notna(x) else x)
        st.download_button("💾 Download Updated Database.xlsx", data=to_excel_buffer(df.to_dict('records')), file_name="Hunter_Results.xlsx")

    # Point 3: Autosave download button always present next to progress
    if st.session_state.hunter_accumulation:
        btn_h_col2.download_button("💾 Download Emergency Autosave (Current Progress)", data=to_excel_buffer(st.session_state.hunter_accumulation), file_name="Hunter_Autosave.xlsx", key="h_auto_btn")