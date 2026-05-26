import streamlit as st
import pandas as pd
import time
import re
import requests
import urllib.parse as urlparse
from urllib.parse import urlencode
import unicodedata
import io

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

# --- CONFIG & CONSTANTS ---
st.set_page_config(page_title="ScholarHunt", page_icon="📚", layout="wide")

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

# --- STATE INITIALIZATION ---
if 'cited_db' not in st.session_state: st.session_state.cited_db = []
if 'cited_batches' not in st.session_state: st.session_state.cited_batches = []
if 'citing_db' not in st.session_state: st.session_state.citing_db = []
if 'citing_batches' not in st.session_state: st.session_state.citing_batches = []

# Autosave accumulation states for ALL tabs
if 'merge_acc' not in st.session_state: st.session_state.merge_acc = []
if 'past_acc' not in st.session_state: st.session_state.past_acc = []
if 'keywords_acc' not in st.session_state: st.session_state.keywords_acc = []
if 'cited_acc' not in st.session_state: st.session_state.cited_acc = []
if 'citing_acc' not in st.session_state: st.session_state.citing_acc = []
if 'val_acc_df' not in st.session_state: st.session_state.val_acc_df = pd.DataFrame()
if 'hunter_acc_df' not in st.session_state: st.session_state.hunter_acc_df = pd.DataFrame()

# --- CORE LOGIC FUNCTIONS ---
def update_progress_with_eta(placeholder, bar, current, total, start_time, base_text):
    if total <= 0: return
    bar.progress(min(current/total, 1.0))
    elapsed = time.time() - start_time
    if current > 0:
        avg_time = elapsed / current
        rem_sec = int((total - current) * avg_time)
        mins, secs = divmod(rem_sec, 60)
        if current < total:
            placeholder.text(f"⏳ {base_text}: {current}/{total} | Time Left: ~{mins}m {secs}s")
        else:
            placeholder.text(f"✅ {base_text}: Completed!")
    else:
        placeholder.text(f"⏳ {base_text}: {current}/{total} | Calculating...")

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
                full_link = urlparse.urljoin(base_url, a['href'])
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

def process_single_article(doi, row_title, af, em_all, ad, rp, df_row, orcid_map, opts, strategy, fast_mode, use_deep, is_past=False, extra_data=None):
    results = []
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

        results.append(rec)
    return results

def to_excel_buffer(df_or_list):
    df = pd.DataFrame(df_or_list) if isinstance(df_or_list, list) else df_or_list
    output = io.BytesIO()
    if not df.empty:
        for col in df.select_dtypes(include=['object']):
            df[col] = df[col].apply(lambda x: re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', str(x)) if pd.notna(x) else x)
        with pd.ExcelWriter(output, engine='openpyxl') as writer:
            df.to_excel(writer, index=False)
    return output.getvalue()

def to_excel_multisheet_buffer(df_dict):
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        for sheet_name, df in df_dict.items():
            df.to_excel(writer, sheet_name=sheet_name, index=False)
    return output.getvalue()

# --- UI COMPONENTS ---
def render_kombajn_ui(prefix):
    st.markdown("### ⚙️ Operation Mode")
    fast = st.checkbox("⚡ FAST MODE", key=f"{prefix}_fast")
    strat = st.radio("Search strategy:", ["Google-Style", "Affiliation"], key=f"{prefix}_strat")
    strat_val = "google" if "Google" in strat else "affil"
    col1, col2, col3 = st.columns(3)
    api = col1.checkbox("Search in API (ORCID/OpenAlex)", value=True, key=f"{prefix}_api")
    pdf = col2.checkbox("Read PDF (Open Access)", value=False, disabled=fast, key=f"{prefix}_pdf")
    deep = col3.checkbox("Deep Web Search (Hunter)", value=False, disabled=fast, key=f"{prefix}_deep")
    return fast, strat_val, api, pdf, deep

def render_options_ui(prefix):
    st.markdown("### 📊 Additional Columns")
    opts = {}
    cols = st.columns(3)
    for i, key in enumerate(ADV_FIELDS.keys()):
        opts[key] = cols[i % 3].checkbox(key, value=True, key=f"{prefix}_opt_{key}")
    return opts

# --- MAIN APP ---
st.title("📚 ScholarHunt")
st.markdown("Scientific Contacts Management System")

with st.sidebar:
    st.header("⚙️ Global Settings")
    global_api_key = st.text_input("OpenAlex API Key (Optional):", type="password")
    if not HAS_DDGS: st.error("⚠️ Error loading 'ddgs' package. Check requirements.txt")

tabs = st.tabs(["📖 Guide", "🗂️ Merge", "👥 Past Authors", "🔑 Keywords", "📜 Cited Outreach", "💬 Citing Outreach", "✅ Validation", "🕵️ Hunter"])

# --- 0. GUIDE ---
with tabs[0]:
    st.markdown("""
    ## WELCOME TO SCHOLARHUNT!
    The following guide will help you understand what each tab is for.

    ### ⚡ FAST MODE
    * **Fast Mode:** If your database has thousands of records, check the 'FAST MODE' option. The program will skip time-consuming web searches.

    ### 📖 Tab Guide
    * **🗂️ 1. Merge:** Combining Excel/WoS file batches into a single list.
    * **👥 2. Past Authors:** Splits files into Corresponding and Co-Authors tabs.
    * **🔑 3. Keywords:** Generates a ready-to-use keywords list.
    * **📜 4. Cited Outreach:** Management for authors cited by Base Papers.
    * **💬 5. Citing Outreach:** Management for authors citing Base Papers.
    * **✅ 6. Validation:** Cleaning the mailing list.
    * **🕵️ 7. Hunter:** Fills in missing emails from the Internet.
    """)

# --- 1. MERGE ---
with tabs[1]:
    st.header("🗂️ Merge Files")
    m_files = st.file_uploader("📂 Upload WoS/Excel files", accept_multiple_files=True, key="m_f")
    m_oa = st.text_input("🌐 OpenAlex API Link", key="m_oa")
    m_fast, m_strat, m_api, m_pdf, m_deep = render_kombajn_ui("m")
    m_opts = render_options_ui("m")
    
    st.markdown("---")
    ctrl1, ctrl2, ctrl3 = st.columns([1, 1, 2])
    run_merge = ctrl1.button("🚀 RUN MERGE", type="primary")
    ctrl2.button("🛑 STOP", key="stop_m")
    if st.session_state.merge_acc:
        ctrl3.download_button("💾 Download Emergency Autosave", data=to_excel_buffer(st.session_state.merge_acc), file_name="AUTOSAVE_Merge.xlsx")
    
    if run_merge:
        st.session_state.merge_acc = []
        progress_text = st.empty()
        prog_bar = st.progress(0)
        
        total_items = (len(m_files) if m_files else 0) + (1 if m_oa else 0)
        current_item = 0
        start_time = time.time()
        
        if m_files:
            for f in m_files:
                current_item += 1
                update_progress_with_eta(progress_text, prog_bar, current_item, total_items, start_time, "Merging Files")
                df = pd.read_excel(f) if f.name.endswith(('xls', 'xlsx')) else pd.read_csv(f)
                for _, r in df.iterrows():
                    doi, title, af, ems, ad, rp = extract_universal_data(df, r)
                    st.session_state.merge_acc.extend(process_single_article(doi, title, af, ems, ad, rp, r, {}, m_opts, m_strat, m_fast, m_deep))
                
        if m_oa:
            current_item += 1
            update_progress_with_eta(progress_text, prog_bar, current_item, total_items, start_time, "Merging API")
            oa_works = fetch_works_from_openalex_url(m_oa, global_api_key)
            for work in oa_works:
                doi, title, af, ems, ad, rp, orcid_map = extract_openalex_work(work)
                st.session_state.merge_acc.extend(process_single_article(doi, title, af, ems, ad, rp, None, orcid_map, m_opts, m_strat, m_fast, m_deep))
        
        if st.session_state.merge_acc:
            out_df = pd.DataFrame(st.session_state.merge_acc)
            out_df = enforce_column_order(out_df, 'merge')
            if 'Email' in out_df.columns:
                out_df = pd.concat([out_df[out_df['Email'] != ''].drop_duplicates(subset=['Email']), out_df[out_df['Email'] == ''].drop_duplicates(subset=['Name', 'Surname'])])
            
            progress_text.text("✅ Completed!")
            st.success(f"Successfully merged. Found {len(out_df)} unique authors.")
            st.download_button("💾 Download Final Merge.xlsx", data=to_excel_buffer(out_df), file_name="Merge_Result.xlsx", key="final_m")

# --- 2. PAST AUTHORS ---
with tabs[2]:
    st.header("👥 Past Authors")
    p_files = st.file_uploader("📂 Upload files", accept_multiple_files=True, key="p_f")
    p_oa = st.text_input("🌐 OpenAlex API Link", key="p_oa")
    p_jrnl = st.text_input("DG Journal name:", key="p_jrnl")
    p_fast, p_strat, p_api, p_pdf, p_deep = render_kombajn_ui("p")
    p_opts = render_options_ui("p")
    
    st.markdown("---")
    ctrl1, ctrl2, ctrl3 = st.columns([1, 1, 2])
    run_past = ctrl1.button("🚀 RUN SPLIT", type="primary")
    ctrl2.button("🛑 STOP", key="stop_p")
    if st.session_state.past_acc:
        ctrl3.download_button("💾 Download Emergency Autosave", data=to_excel_buffer(st.session_state.past_acc), file_name="AUTOSAVE_Past.xlsx")

    if run_past:
        st.session_state.past_acc = []
        progress_text = st.empty()
        prog_bar = st.progress(0)
        
        extra = {"DG Journal name": p_jrnl}
        total_items = (len(p_files) if p_files else 0) + (1 if p_oa else 0)
        current_item = 0
        start_time = time.time()
        
        if p_files:
            for f in p_files:
                current_item += 1
                update_progress_with_eta(progress_text, prog_bar, current_item, total_items, start_time, "Splitting Files")
                df = pd.read_excel(f) if f.name.endswith(('xls', 'xlsx')) else pd.read_csv(f)
                for _, r in df.iterrows():
                    doi, title, af, ems, ad, rp = extract_universal_data(df, r)
                    st.session_state.past_acc.extend(process_single_article(doi, title, af, ems, ad, rp, r, {}, p_opts, p_strat, p_fast, p_deep, True, extra))
                    
        if p_oa:
            current_item += 1
            update_progress_with_eta(progress_text, prog_bar, current_item, total_items, start_time, "Splitting API")
            oa_works = fetch_works_from_openalex_url(p_oa, global_api_key)
            for work in oa_works:
                doi, title, af, ems, ad, rp, orcid_map = extract_openalex_work(work)
                st.session_state.past_acc.extend(process_single_article(doi, title, af, ems, ad, rp, None, orcid_map, p_opts, p_strat, p_fast, p_deep, True, extra))

        if st.session_state.past_acc:
            out_df = pd.DataFrame(st.session_state.past_acc)
            out_df = enforce_column_order(out_df, 'past')
            if 'Email' in out_df.columns:
                out_df = pd.concat([out_df[out_df['Email'] != ''].drop_duplicates(subset=['Email']), out_df[out_df['Email'] == ''].drop_duplicates(subset=['Name', 'Surname'])])

            df_corr = out_df[out_df['is_corr']==True].drop(columns=['is_corr'], errors='ignore')
            df_co = out_df[out_df['is_corr']==False].drop(columns=['is_corr'], errors='ignore')
            
            progress_text.text("✅ Completed!")
            col1, col2 = st.columns(2)
            col1.download_button("💾 Download Corresponding", data=to_excel_buffer(df_corr), file_name="Corresponding.xlsx")
            col2.download_button("💾 Download CoAuthors", data=to_excel_buffer(df_co), file_name="CoAuthors.xlsx")

# --- 3. KEYWORDS ---
with tabs[3]:
    st.header("🔑 Keywords")
    k_files = st.file_uploader("📂 Upload files", accept_multiple_files=True, key="k_f")
    col1, col2 = st.columns(2)
    k_jrnl = col1.text_input("DG Journal name:", key="k_j")
    k_kw = col2.text_input("DG Keyword:", key="k_kw")
    k_title = col1.text_input("DG article title:", key="k_t")
    k_link = col2.text_input("Link:", key="k_l")
    k_doi = col1.text_input("DG article DOI:", key="k_d")
    
    k_fast, k_strat, k_api, k_pdf, k_deep = render_kombajn_ui("k")
    k_opts = render_options_ui("k")
    
    st.markdown("---")
    ctrl1, ctrl2, ctrl3 = st.columns([1, 1, 2])
    run_key = ctrl1.button("🚀 GENERATE KEYWORDS", type="primary")
    ctrl2.button("🛑 STOP", key="stop_k")
    if st.session_state.keywords_acc:
        ctrl3.download_button("💾 Download Emergency Autosave", data=to_excel_buffer(st.session_state.keywords_acc), file_name="AUTOSAVE_Keywords.xlsx")

    if run_key:
        st.session_state.keywords_acc = []
        progress_text = st.empty()
        prog_bar = st.progress(0)
        
        extra = {"DG Journal name": k_jrnl, "DG Keyword": k_kw, "DG article title": k_title, "link": k_link, "DG article DOI": k_doi}
        total_items = len(k_files) if k_files else 0
        current_item = 0
        start_time = time.time()
        
        if k_files:
            for f in k_files:
                current_item += 1
                update_progress_with_eta(progress_text, prog_bar, current_item, total_items, start_time, "Generating Keywords")
                df = pd.read_excel(f) if f.name.endswith(('xls', 'xlsx')) else pd.read_csv(f)
                for _, r in df.iterrows():
                    doi, title, af, ems, ad, rp = extract_universal_data(df, r)
                    st.session_state.keywords_acc.extend(process_single_article(doi, title, af, ems, ad, rp, r, {}, k_opts, k_strat, k_fast, k_deep, False, extra))
                    
        if st.session_state.keywords_acc:
            out_df = pd.DataFrame(st.session_state.keywords_acc)
            out_df = enforce_column_order(out_df, 'keywords')
            if 'Email' in out_df.columns:
                out_df = pd.concat([out_df[out_df['Email'] != ''].drop_duplicates(subset=['Email']), out_df[out_df['Email'] == ''].drop_duplicates(subset=['Name', 'Surname'])])
            
            progress_text.text("✅ Completed!")
            st.success("List generated.")
            st.download_button("💾 Download Final Keywords List", data=to_excel_buffer(out_df), file_name="Keywords_List.xlsx", key="final_k")

# --- 4. CITED ---
with tabs[4]:
    st.header("📜 Cited Outreach (Backwards/References)")
    c_fast, c_strat, c_api, c_pdf, c_deep = render_kombajn_ui("c")
    c_opts = render_options_ui("c")
    
    st.subheader("Manual Mode")
    c_man_files = st.file_uploader("📂 Upload Base File", accept_multiple_files=True, key="c_man_f")
    c1, c2 = st.columns(2)
    c_man_jrnl = c1.text_input("DG Journal name:", key="c_man_j")
    c_man_title = c2.text_input("DG article title:", key="c_man_t")
    c_man_link = c1.text_input("Link:", key="c_man_l")
    c_man_doi = c2.text_input("DG article DOI:", key="c_man_d")
    
    if st.button("➕ LOAD BASE FILE (Manual)"):
        if c_man_files and c_man_title:
            batch_id = str(time.time())
            extra = {"DG Journal name": c_man_jrnl, "DG article title": c_man_title, "link": c_man_link, "DG article DOI": c_man_doi}
            added_count = 0
            for f in c_man_files:
                df = pd.read_excel(f) if f.name.endswith(('xls', 'xlsx')) else pd.read_csv(f)
                for _, r in df.iterrows():
                    doi, title, af, ems, ad, rp = extract_universal_data(df, r)
                    results = process_single_article(doi, title, af, ems, ad, rp, r, {}, c_opts, c_strat, c_fast, c_deep, False, extra)
                    for res in results: res['batch_id'] = batch_id
                    st.session_state.cited_db.extend(results)
                    added_count += len(results)
            st.session_state.cited_batches.append({'batch_id': batch_id, 'Journal': c_man_jrnl, 'Title': c_man_title, 'Count': added_count})
            st.success("Files added to database!")
        else:
            st.warning("Please upload files and provide an article title.")

    st.subheader("Auto-Pilot (OpenAlex)")
    c_jrnl = st.text_input("DG Journal name (Auto):", key="c_auto_j")
    c_doi = st.text_input("DOI or Mass OpenAlex Link:", key="c_auto_d")
    
    st.markdown("---")
    ctrl1, ctrl2, ctrl3 = st.columns([1, 1, 2])
    run_cited = ctrl1.button("🚀 RUN AUTO-PILOT", type="primary", key="run_cited")
    ctrl2.button("🛑 STOP", key="stop_c")
    if st.session_state.cited_acc:
        ctrl3.download_button("💾 Download Emergency Autosave", data=to_excel_buffer(st.session_state.cited_acc), file_name="AUTOSAVE_Cited.xlsx")

    if run_cited:
        if c_doi:
            st.session_state.cited_acc = []
            with st.spinner("Fetching base works from OpenAlex..."):
                base_works = []
                headers = {'api_key': global_api_key} if global_api_key else {}
                
                if "api.openalex.org" in c_doi and ("filter=" in c_doi or "search=" in c_doi):
                    base_works = fetch_works_from_openalex_url(c_doi, global_api_key)
                else:
                    clean_doi = re.search(r'10\.\d{4,9}/[-._;()/:a-zA-Z0-9]+', c_doi)
                    clean_doi = clean_doi.group(0) if clean_doi else c_doi.replace('https://doi.org/', '').strip()
                    res = requests.get(f"https://api.openalex.org/works/https://doi.org/{clean_doi}", headers=headers)
                    if res.status_code == 200: base_works = [res.json()]
                
                if base_works:
                    batch_id = str(time.time())
                    added_count = 0
                    total_base = len(base_works)
                    prog_bar = st.progress(0)
                    status_text = st.empty()
                    start_time = time.time()
                    
                    for idx, b_work in enumerate(base_works):
                        b_title = b_work.get('title', 'Unknown')
                        b_doi = b_work.get('doi', '').replace('https://doi.org/', '')
                        
                        update_progress_with_eta(status_text, prog_bar, idx + 1, total_base, start_time, f"Paper: {b_title[:40]}...")
                        
                        ref_urls = b_work.get('referenced_works', [])
                        extra = {"DG Journal name": c_jrnl, "DG article title": b_title, "link": f"https://doi.org/{b_doi}" if b_doi else "", "DG article DOI": b_doi}
                        
                        if ref_urls:
                            ref_ids = [ref.split('/')[-1] for ref in ref_urls]
                            for k in range(0, len(ref_ids), 50):
                                works = fetch_works_from_openalex_url("https://api.openalex.org/works?filter=openalex:" + "|".join(ref_ids[k:k+50]), global_api_key)
                                for work in works:
                                    w_doi, w_title, af, ems, ad, rp, orcid_map = extract_openalex_work(work)
                                    results = process_single_article(w_doi, w_title, af, ems, ad, rp, None, orcid_map, c_opts, c_strat, c_fast, c_deep, False, extra)
                                    for res in results: res['batch_id'] = batch_id
                                    st.session_state.cited_acc.extend(results)
                                    added_count += len(results)
                    
                    st.session_state.cited_db.extend(st.session_state.cited_acc)
                    batch_title = f"Mass Link ({total_base} papers)" if total_base > 1 else f"Auto-Pilot ({base_works[0].get('title', 'Unknown')[:25]}...)"
                    st.session_state.cited_batches.append({'batch_id': batch_id, 'Journal': c_jrnl, 'Title': batch_title, 'Count': added_count})
                    status_text.text("✅ All papers processed!")
                    st.success("Auto-Pilot added references to database!")

    if st.session_state.cited_batches:
        st.markdown("---")
        st.write(f"📊 **Total Database Entries:** {len(st.session_state.cited_db)}")
        df_batches = pd.DataFrame(st.session_state.cited_batches)
        st.dataframe(df_batches[['Journal', 'Title', 'Count']], width=800)
        
        col_del1, col_del2 = st.columns([3, 1])
        batch_to_delete = col_del1.selectbox("Select row to delete:", options=st.session_state.cited_batches, format_func=lambda x: f"{x['Title']} ({x['Count']} items)", key="c_del_sel")
        if col_del2.button("🗑️ DELETE SELECTED ROW", key="c_del_btn"):
            st.session_state.cited_db = [r for r in st.session_state.cited_db if r.get('batch_id') != batch_to_delete['batch_id']]
            st.session_state.cited_batches = [b for b in st.session_state.cited_batches if b['batch_id'] != batch_to_delete['batch_id']]
            st.rerun()
            
        out_df = enforce_column_order(pd.DataFrame(st.session_state.cited_db).drop_duplicates(subset=['Name', 'Surname']), 'cited')
        st.download_button("💾 EXPORT FINAL CITED RESULTS", data=to_excel_buffer(out_df), file_name="Cited_Outreach_Base.xlsx")

# --- 5. CITING ---
with tabs[5]:
    st.header("💬 Citing Outreach (Forwards/Citations)")
    ci_fast, ci_strat, ci_api, ci_pdf, ci_deep = render_kombajn_ui("ci")
    ci_opts = render_options_ui("ci")
    
    st.subheader("Manual Mode")
    ci_man_files = st.file_uploader("📂 Upload Base File", accept_multiple_files=True, key="ci_man_f")
    ci1, ci2 = st.columns(2)
    ci_man_jrnl = ci1.text_input("DG Journal name:", key="ci_man_j")
    ci_man_title = ci2.text_input("DG article title:", key="ci_man_t")
    ci_man_link = ci1.text_input("Link:", key="ci_man_l")
    ci_man_doi = ci2.text_input("DG article DOI:", key="ci_man_d")
    
    if st.button("➕ LOAD BASE FILE (Manual)", key="ci_man_btn"):
        if ci_man_files and ci_man_title:
            batch_id = str(time.time())
            extra = {"DG Journal name": ci_man_jrnl, "DG article title": ci_man_title, "link": ci_man_link, "DG article DOI": ci_man_doi}
            added_count = 0
            for f in ci_man_files:
                df = pd.read_excel(f) if f.name.endswith(('xls', 'xlsx')) else pd.read_csv(f)
                for _, r in df.iterrows():
                    doi, title, af, ems, ad, rp = extract_universal_data(df, r)
                    results = process_single_article(doi, title, af, ems, ad, rp, r, {}, ci_opts, ci_strat, ci_fast, ci_deep, False, extra)
                    for res in results: res['batch_id'] = batch_id
                    st.session_state.citing_db.extend(results)
                    added_count += len(results)
            st.session_state.citing_batches.append({'batch_id': batch_id, 'Journal': ci_man_jrnl, 'Title': ci_man_title, 'Count': added_count})
            st.success("Files added to database!")
        else:
            st.warning("Please upload files and provide an article title.")

    st.subheader("Auto-Pilot (OpenAlex)")
    ci_jrnl = st.text_input("DG Journal name (Auto):", key="ci_auto_j")
    ci_doi = st.text_input("DOI or Mass OpenAlex Link:", key="ci_auto_d")
    
    st.markdown("---")
    ctrl1, ctrl2, ctrl3 = st.columns([1, 1, 2])
    run_citing = ctrl1.button("🚀 RUN AUTO-PILOT", type="primary", key="run_citing")
    ctrl2.button("🛑 STOP", key="stop_ci")
    if st.session_state.citing_acc:
        ctrl3.download_button("💾 Download Emergency Autosave", data=to_excel_buffer(st.session_state.citing_acc), file_name="AUTOSAVE_Citing.xlsx")

    if run_citing:
        if ci_doi:
            st.session_state.citing_acc = []
            with st.spinner("Fetching base works from OpenAlex..."):
                base_works = []
                headers = {'api_key': global_api_key} if global_api_key else {}
                
                if "api.openalex.org" in ci_doi and ("filter=" in ci_doi or "search=" in ci_doi):
                    base_works = fetch_works_from_openalex_url(ci_doi, global_api_key)
                else:
                    clean_doi = re.search(r'10\.\d{4,9}/[-._;()/:a-zA-Z0-9]+', ci_doi)
                    clean_doi = clean_doi.group(0) if clean_doi else ci_doi.replace('https://doi.org/', '').strip()
                    res = requests.get(f"https://api.openalex.org/works/https://doi.org/{clean_doi}", headers=headers)
                    if res.status_code == 200: base_works = [res.json()]
                        
                if base_works:
                    batch_id = str(time.time())
                    added_count = 0
                    total_base = len(base_works)
                    prog_bar = st.progress(0)
                    status_text = st.empty()
                    start_time = time.time()
                    
                    for idx, b_work in enumerate(base_works):
                        b_title = b_work.get('title', 'Unknown')
                        b_id = b_work.get('id', '').split('/')[-1]
                        b_doi = b_work.get('doi', '').replace('https://doi.org/', '')
                        
                        update_progress_with_eta(status_text, prog_bar, idx + 1, total_base, start_time, f"Paper: {b_title[:40]}...")
                        
                        extra = {"DG Journal name": ci_jrnl, "DG article title": b_title, "link": f"https://doi.org/{b_doi}" if b_doi else "", "DG article DOI": b_doi}
                        works = fetch_works_from_openalex_url(f"https://api.openalex.org/works?filter=cites:{b_id}", global_api_key)
                        
                        for work in works:
                            w_doi, w_title, af, ems, ad, rp, orcid_map = extract_openalex_work(work)
                            results = process_single_article(w_doi, w_title, af, ems, ad, rp, None, orcid_map, ci_opts, ci_strat, ci_fast, ci_deep, False, extra)
                            for res in results: res['batch_id'] = batch_id
                            st.session_state.citing_acc.extend(results)
                            added_count += len(results)
                            
                    st.session_state.citing_db.extend(st.session_state.citing_acc)
                    batch_title = f"Mass Link ({total_base} papers)" if total_base > 1 else f"Auto-Pilot ({base_works[0].get('title', 'Unknown')[:25]}...)"
                    st.session_state.citing_batches.append({'batch_id': batch_id, 'Journal': ci_jrnl, 'Title': batch_title, 'Count': added_count})
                    status_text.text("✅ All papers processed!")
                    st.success("Auto-Pilot added citing papers to database!")

    if st.session_state.citing_batches:
        st.markdown("---")
        st.write(f"📊 **Total Database Entries:** {len(st.session_state.citing_db)}")
        df_batches = pd.DataFrame(st.session_state.citing_batches)
        st.dataframe(df_batches[['Journal', 'Title', 'Count']], width=800)
        
        col_del1, col_del2 = st.columns([3, 1])
        batch_to_delete = col_del1.selectbox("Select row to delete:", options=st.session_state.citing_batches, format_func=lambda x: f"{x['Title']} ({x['Count']} items)", key="ci_del_sel")
        if col_del2.button("🗑️ DELETE SELECTED ROW", key="ci_del_btn_real"):
            st.session_state.citing_db = [r for r in st.session_state.citing_db if r.get('batch_id') != batch_to_delete['batch_id']]
            st.session_state.citing_batches = [b for b in st.session_state.citing_batches if b['batch_id'] != batch_to_delete['batch_id']]
            st.rerun()
            
        out_df = enforce_column_order(pd.DataFrame(st.session_state.citing_db).drop_duplicates(subset=['Name', 'Surname']), 'citing')
        st.download_button("💾 EXPORT FINAL CITING RESULTS", data=to_excel_buffer(out_df), file_name="Citing_Outreach_Base.xlsx")

# --- 6. VALIDATION ---
with tabs[6]:
    st.header("✅ Smart Validation")
    v_file = st.file_uploader("📂 Select Excel File", type=["xlsx", "xls"], key="v_f")
    
    st.markdown("---")
    ctrl1, ctrl2, ctrl3 = st.columns([1, 1, 2])
    run_val = ctrl1.button("🔍 START VALIDATION", type="primary")
    ctrl2.button("🛑 STOP", key="stop_v")
    if not st.session_state.val_acc_df.empty:
        ctrl3.download_button("💾 Download Emergency Autosave", data=to_excel_buffer(st.session_state.val_acc_df), file_name="AUTOSAVE_Validation.xlsx")

    if run_val and v_file:
        df = pd.read_excel(v_file)
        em_col = next((c for c in df.columns if 'mail' in str(c).lower()), None)
        sn_col = next((c for c in df.columns if 'surname' in str(c).lower() or 'nazwisko' in str(c).lower()), None)
        c_col = next((c for c in df.columns if 'country' in str(c).lower() or 'kraj' in str(c).lower()), None)
        
        if em_col and sn_col:
            st.session_state.val_acc_df = df.copy()
            st.session_state.val_acc_df['Smart Status'] = "Pending"
            
            prog_bar = st.progress(0)
            status_text = st.empty()
            start_time = time.time()
            total = len(df)
            
            for i, row in df.iterrows():
                em, sn = str(row[em_col]), str(row[sn_col])
                ct = str(row[c_col]) if c_col else ""
                status = validate_email_intelligence(em, sn, ct) if em and em != 'nan' else "Missing Email"
                st.session_state.val_acc_df.at[i, 'Smart Status'] = status
                update_progress_with_eta(status_text, prog_bar, i + 1, total, start_time, "Validating")
                
            sheets = {
                'Certain': st.session_state.val_acc_df[st.session_state.val_acc_df['Smart Status'].str.startswith('Certain')],
                'Probable': st.session_state.val_acc_df[st.session_state.val_acc_df['Smart Status'].str.startswith('Probable')],
                'To_Verify': st.session_state.val_acc_df[st.session_state.val_acc_df['Smart Status'] == 'Manual Verification Required'],
                'Invalid': st.session_state.val_acc_df[st.session_state.val_acc_df['Smart Status'].str.contains('Invalid|Missing')]
            }
            status_text.text("✅ Completed!")
            st.success("Email sorting completed.")
            st.download_button("💾 Download Final Validation Report", data=to_excel_multisheet_buffer(sheets), file_name="Validated_Report.xlsx", key="final_v")
        else:
            st.error("Required columns missing: Email, Surname.")

# --- 7. HUNTER ---
with tabs[7]:
    st.header("🕵️ Cascading Email Hunter")
    h_file = st.file_uploader("📂 Upload database with missing emails", type=["xlsx", "xls"], key="h_f")
    h_strat = st.radio("Strategy:", ["Google-Style", "Affiliation"], key="h_s")
    is_deep = st.checkbox("🕸️ DEEP SCAN (Takes more time)", value=False)
    
    st.markdown("---")
    ctrl1, ctrl2, ctrl3 = st.columns([1, 1, 2])
    run_hunt = ctrl1.button("🚀 RUN HUNTER", type="primary")
    ctrl2.button("🛑 STOP", key="stop_h")
    if not st.session_state.hunter_acc_df.empty:
        ctrl3.download_button("💾 Download Emergency Autosave", data=to_excel_buffer(st.session_state.hunter_acc_df), file_name="AUTOSAVE_Hunter.xlsx")

    if run_hunt and h_file:
        df = pd.read_excel(h_file)
        
        email_col = next((c for c in df.columns if 'mail' in c.lower()), 'Email')
        if email_col not in df.columns: df[email_col] = ""
        df[email_col] = df[email_col].astype(object)
        
        surname_col = next((c for c in df.columns if 'surname' in c.lower() or 'nazwisko' in c.lower()), 'Surname')
        name_col = next((c for c in df.columns if 'name' in c.lower() and 'surname' not in c.lower()), 'Name')
        aff_col = next((c for c in df.columns if 'country' in c.lower() or 'affil' in c.lower()), None)
        orcid_col = next((c for c in df.columns if 'orcid' in c.lower()), None)
        
        st.session_state.hunter_acc_df = df.copy() 
        
        prog_bar = st.progress(0)
        status_text = st.empty()
        start_time = time.time()
        znalezione = 0
        total = len(df)
        
        for idx, row in df.iterrows():
            sn = str(row.get(surname_col, '')).strip()
            nm = str(row.get(name_col, '')).strip()
            aff = str(row.get(aff_col, '')).strip()
            orc = str(row.get(orcid_col, '')).strip()
            if 'orcid.org/' in orc: orc = orc.split('/')[-1]
            
            update_progress_with_eta(status_text, prog_bar, idx + 1, total, start_time, f"Searching: {nm} {sn}")

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
                time.sleep(2.0)
                queries = [f'"{nm} {sn}" email', f'"{sn}" email contact'] if "Google" in h_strat else [f'"{nm} {sn}" {aff if aff and aff.lower()!="nan" else "university"} email']
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
                except Exception as e:
                    pass

            if email_found:
                st.session_state.hunter_acc_df.at[idx, email_col] = email_found 
                znalezione += 1
                
        status_text.text(f"✅ Completed! Filled in {znalezione} records.")
        st.download_button("💾 Download Final Updated Database", data=to_excel_buffer(st.session_state.hunter_acc_df), file_name="Hunter_Results.xlsx", key="final_h")