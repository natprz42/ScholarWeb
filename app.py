import streamlit as st
import pandas as pd
import time
import re
import requests
import urllib.parse as urlparse
from urllib.parse import urlencode
import unicodedata
import io
import random

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
st.set_page_config(page_title="ScholarHunt Cloud", page_icon="📚", layout="wide")

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
            time.sleep(1.5) # Cloud safe limit
            try:
                queries = [f'"{fn} {sn}" email'] if strategy == "google" else [f'"{fn} {sn}" {affil_clean if affil_clean and affil_clean != "Unknown" else c} email']
                with DDGS() as ddgs:
                    all_s = []
                    for q in queries:
                        if matched_email: break
                        res = list(ddgs.text(q, max_results=3))
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

def to_excel_buffer(df):
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
st.title("📚 ScholarHunt Cloud")
st.markdown("Scientific Contacts Management System - Wersja Pełna")

with st.sidebar:
    st.header("⚙️ Ustawienia Globalne")
    global_api_key = st.text_input("OpenAlex API Key (Opcjonalnie):", type="password")
    if not HAS_DDGS: st.error("⚠️ Błąd ładowania pakietu ddgs.")

tabs = st.tabs(["🗂️ Merge", "👥 Past Authors", "🔑 Keywords", "📜 Cited", "💬 Citing", "✅ Validation", "🕵️ Hunter"])

# --- 1. MERGE ---
with tabs[0]:
    st.header("🗂️ Merge Files")
    m_files = st.file_uploader("📂 Wgraj pliki WoS/Excel", accept_multiple_files=True, key="m_f")
    m_oa = st.text_input("🌐 Link OpenAlex API", key="m_oa")
    m_fast, m_strat, m_api, m_pdf, m_deep = render_kombajn_ui("m")
    m_opts = render_options_ui("m")
    
    if st.button("🚀 RUN MERGE", type="primary"):
        final_rows = []
        progress_text = st.empty()
        prog_bar = st.progress(0)
        
        if m_files:
            for idx, f in enumerate(m_files):
                progress_text.text(f"Przetwarzanie pliku {idx+1}/{len(m_files)}...")
                df = pd.read_excel(f) if f.name.endswith(('xls', 'xlsx')) else pd.read_csv(f)
                for _, r in df.iterrows():
                    doi, title, af, ems, ad, rp = extract_universal_data(df, r)
                    final_rows.extend(process_single_article(doi, title, af, ems, ad, rp, r, {}, m_opts, m_strat, m_fast, m_deep))
                prog_bar.progress((idx + 1) / len(m_files))
                
        if m_oa:
            progress_text.text("Przetwarzanie OpenAlex API...")
            oa_works = fetch_works_from_openalex_url(m_oa, global_api_key)
            for work in oa_works:
                doi, title, af, ems, ad, rp, orcid_map = extract_openalex_work(work)
                final_rows.extend(process_single_article(doi, title, af, ems, ad, rp, None, orcid_map, m_opts, m_strat, m_fast, m_deep))
        
        if final_rows:
            out_df = pd.DataFrame(final_rows)
            out_df = enforce_column_order(out_df, 'merge')
            if 'Email' in out_df.columns:
                out_df = pd.concat([out_df[out_df['Email'] != ''].drop_duplicates(subset=['Email']), out_df[out_df['Email'] == ''].drop_duplicates(subset=['Name', 'Surname'])])
            
            progress_text.text("✅ Zakończono!")
            st.success(f"Połączono pomyślnie. Znaleziono {len(out_df)} unikalnych autorów.")
            st.download_button("💾 Pobierz Merge.xlsx", data=to_excel_buffer(out_df), file_name="Merge_Result.xlsx")

# --- 2. PAST AUTHORS ---
with tabs[1]:
    st.header("👥 Past Authors")
    p_files = st.file_uploader("📂 Wgraj pliki", accept_multiple_files=True, key="p_f")
    p_oa = st.text_input("🌐 Link OpenAlex API", key="p_oa")
    p_jrnl = st.text_input("DG Journal name:", key="p_jrnl")
    p_fast, p_strat, p_api, p_pdf, p_deep = render_kombajn_ui("p")
    p_opts = render_options_ui("p")
    
    if st.button("🚀 RUN SPLIT", type="primary"):
        final_rows = []
        progress_text = st.empty()
        
        extra = {"DG Journal name": p_jrnl}
        if p_files:
            for f in p_files:
                df = pd.read_excel(f) if f.name.endswith(('xls', 'xlsx')) else pd.read_csv(f)
                for _, r in df.iterrows():
                    doi, title, af, ems, ad, rp = extract_universal_data(df, r)
                    final_rows.extend(process_single_article(doi, title, af, ems, ad, rp, r, {}, p_opts, p_strat, p_fast, p_deep, True, extra))
                    
        if p_oa:
            oa_works = fetch_works_from_openalex_url(p_oa, global_api_key)
            for work in oa_works:
                doi, title, af, ems, ad, rp, orcid_map = extract_openalex_work(work)
                final_rows.extend(process_single_article(doi, title, af, ems, ad, rp, None, orcid_map, p_opts, p_strat, p_fast, p_deep, True, extra))

        if final_rows:
            out_df = pd.DataFrame(final_rows)
            out_df = enforce_column_order(out_df, 'past')
            if 'Email' in out_df.columns:
                out_df = pd.concat([out_df[out_df['Email'] != ''].drop_duplicates(subset=['Email']), out_df[out_df['Email'] == ''].drop_duplicates(subset=['Name', 'Surname'])])

            df_corr = out_df[out_df['is_corr']==True].drop(columns=['is_corr'], errors='ignore')
            df_co = out_df[out_df['is_corr']==False].drop(columns=['is_corr'], errors='ignore')
            
            progress_text.text("✅ Zakończono!")
            col1, col2 = st.columns(2)
            col1.download_button("💾 Pobierz Corresponding", data=to_excel_buffer(df_corr), file_name="Corresponding.xlsx")
            col2.download_button("💾 Pobierz CoAuthors", data=to_excel_buffer(df_co), file_name="CoAuthors.xlsx")

# --- 3. KEYWORDS ---
with tabs[2]:
    st.header("🔑 Keywords")
    k_files = st.file_uploader("📂 Wgraj pliki", accept_multiple_files=True, key="k_f")
    col1, col2 = st.columns(2)
    k_jrnl = col1.text_input("DG Journal name:", key="k_j")
    k_kw = col2.text_input("DG Keyword:", key="k_kw")
    k_title = col1.text_input("DG article title:", key="k_t")
    k_link = col2.text_input("Link:", key="k_l")
    k_doi = col1.text_input("DG article DOI:", key="k_d")
    
    k_fast, k_strat, k_api, k_pdf, k_deep = render_kombajn_ui("k")
    k_opts = render_options_ui("k")
    
    if st.button("🚀 GENERATE KEYWORDS LIST", type="primary"):
        final_rows = []
        extra = {"DG Journal name": k_jrnl, "DG Keyword": k_kw, "DG article title": k_title, "link": k_link, "DG article DOI": k_doi}
        if k_files:
            for f in k_files:
                df = pd.read_excel(f) if f.name.endswith(('xls', 'xlsx')) else pd.read_csv(f)
                for _, r in df.iterrows():
                    doi, title, af, ems, ad, rp = extract_universal_data(df, r)
                    final_rows.extend(process_single_article(doi, title, af, ems, ad, rp, r, {}, k_opts, k_strat, k_fast, k_deep, False, extra))
        if final_rows:
            out_df = pd.DataFrame(final_rows)
            out_df = enforce_column_order(out_df, 'keywords')
            if 'Email' in out_df.columns:
                out_df = pd.concat([out_df[out_df['Email'] != ''].drop_duplicates(subset=['Email']), out_df[out_df['Email'] == ''].drop_duplicates(subset=['Name', 'Surname'])])
            st.success("Wygenerowano listę.")
            st.download_button("💾 Pobierz Keywords List", data=to_excel_buffer(out_df), file_name="Keywords_List.xlsx")

# --- 4. CITED ---
with tabs[3]:
    st.header("📜 Cited (Backwards/References)")
    st.subheader("Auto-Pilot (OpenAlex)")
    c_jrnl = st.text_input("DG Journal name:", key="c_j")
    c_doi = st.text_input("Base Paper DOI (np. 10.1515/...):", key="c_d")
    c_fast, c_strat, c_api, c_pdf, c_deep = render_kombajn_ui("c")
    c_opts = render_options_ui("c")
    
    if st.button("🚀 RUN AUTO-PILOT (CITED)", type="primary"):
        if c_doi:
            clean_doi = re.search(r'10\.\d{4,9}/[-._;()/:a-zA-Z0-9]+', c_doi)
            clean_doi = clean_doi.group(0) if clean_doi else c_doi.replace('https://doi.org/', '').strip()
            
            with st.spinner("Pobieranie referencji z OpenAlex..."):
                headers = {'api_key': global_api_key} if global_api_key else {}
                res = requests.get(f"https://api.openalex.org/works/https://doi.org/{clean_doi}", headers=headers).json()
                b_title = res.get('title', 'Unknown')
                ref_urls = res.get('referenced_works', [])
                
                final_rows = []
                extra = {"DG Journal name": c_jrnl, "DG article title": b_title, "link": f"https://doi.org/{clean_doi}", "DG article DOI": clean_doi}
                
                if ref_urls:
                    ref_ids = [ref.split('/')[-1] for ref in ref_urls]
                    prog_bar = st.progress(0)
                    for k in range(0, len(ref_ids), 50):
                        works = fetch_works_from_openalex_url("https://api.openalex.org/works?filter=openalex:" + "|".join(ref_ids[k:k+50]), global_api_key)
                        for work in works:
                            w_doi, w_title, af, ems, ad, rp, orcid_map = extract_openalex_work(work)
                            final_rows.extend(process_single_article(w_doi, w_title, af, ems, ad, rp, None, orcid_map, c_opts, c_strat, c_fast, c_deep, False, extra))
                        prog_bar.progress(min((k+50)/len(ref_ids), 1.0))
                
                if final_rows:
                    out_df = enforce_column_order(pd.DataFrame(final_rows).drop_duplicates(subset=['Name', 'Surname']), 'cited')
                    st.success("Zakończono pobieranie cytowań!")
                    st.download_button("💾 Eksportuj Cited Base", data=to_excel_buffer(out_df), file_name="Cited_Outreach_Base.xlsx")

# --- 5. CITING ---
with tabs[4]:
    st.header("💬 Citing (Forwards/Citations)")
    st.subheader("Auto-Pilot (OpenAlex)")
    ci_jrnl = st.text_input("DG Journal name:", key="ci_j")
    ci_doi = st.text_input("Base Paper DOI:", key="ci_d")
    ci_fast, ci_strat, ci_api, ci_pdf, ci_deep = render_kombajn_ui("ci")
    ci_opts = render_options_ui("ci")
    
    if st.button("🚀 RUN AUTO-PILOT (CITING)", type="primary"):
        if ci_doi:
            clean_doi = re.search(r'10\.\d{4,9}/[-._;()/:a-zA-Z0-9]+', ci_doi)
            clean_doi = clean_doi.group(0) if clean_doi else ci_doi.replace('https://doi.org/', '').strip()
            
            with st.spinner("Pobieranie dokumentów cytujących..."):
                headers = {'api_key': global_api_key} if global_api_key else {}
                res = requests.get(f"https://api.openalex.org/works/https://doi.org/{clean_doi}", headers=headers).json()
                b_title = res.get('title', 'Unknown')
                b_id = res.get('id', '').split('/')[-1]
                
                extra = {"DG Journal name": ci_jrnl, "DG article title": b_title, "link": f"https://doi.org/{clean_doi}", "DG article DOI": clean_doi}
                works = fetch_works_from_openalex_url(f"https://api.openalex.org/works?filter=cites:{b_id}", global_api_key)
                
                final_rows = []
                for work in works:
                    w_doi, w_title, af, ems, ad, rp, orcid_map = extract_openalex_work(work)
                    final_rows.extend(process_single_article(w_doi, w_title, af, ems, ad, rp, None, orcid_map, ci_opts, ci_strat, ci_fast, ci_deep, False, extra))
                
                if final_rows:
                    out_df = enforce_column_order(pd.DataFrame(final_rows).drop_duplicates(subset=['Name', 'Surname']), 'citing')
                    st.success("Zakończono pobieranie!")
                    st.download_button("💾 Eksportuj Citing Base", data=to_excel_buffer(out_df), file_name="Citing_Outreach_Base.xlsx")

# --- 6. VALIDATION ---
with tabs[5]:
    st.header("✅ Smart Validation")
    v_file = st.file_uploader("📂 Select Excel File", type=["xlsx", "xls"], key="v_f")
    if v_file and st.button("🔍 START VALIDATION", type="primary"):
        df = pd.read_excel(v_file)
        em_col = next((c for c in df.columns if 'mail' in str(c).lower()), None)
        sn_col = next((c for c in df.columns if 'surname' in str(c).lower() or 'nazwisko' in str(c).lower()), None)
        c_col = next((c for c in df.columns if 'country' in str(c).lower() or 'kraj' in str(c).lower()), None)
        
        if em_col and sn_col:
            prog = st.progress(0)
            status_list = []
            for i, row in df.iterrows():
                em, sn = str(row[em_col]), str(row[sn_col])
                ct = str(row[c_col]) if c_col else ""
                status_list.append(validate_email_intelligence(em, sn, ct) if em and em != 'nan' else "Missing Email")
                prog.progress((i+1)/len(df))
                
            df['Status Walidacji'] = status_list
            sheets = {
                'Certain': df[df['Status Walidacji'].str.startswith('Certain')],
                'Probable': df[df['Status Walidacji'].str.startswith('Probable')],
                'To_Verify': df[df['Status Walidacji'] == 'Manual Verification Required'],
                'Invalid': df[df['Status Walidacji'].str.contains('Invalid|Missing')]
            }
            st.success("Zakończono segregację maili.")
            st.download_button("💾 Pobierz Raport Walidacji", data=to_excel_multisheet_buffer(sheets), file_name="Validated_Report.xlsx")
        else:
            st.error("Wymagane kolumny: Email, Surname.")

# --- 7. HUNTER ---
with tabs[6]:
    st.header("🕵️ Cascading Email Hunter")
    h_file = st.file_uploader("📂 Wgraj bazę z brakującymi", type=["xlsx", "xls"], key="h_f")
    h_strat = st.radio("Strategia:", ["Google-Style", "Affiliation"], key="h_s")
    is_deep = st.checkbox("🕸️ DEEP SCAN (Wymaga czasu)", value=False)
    
    if h_file and st.button("🚀 URUCHOM HUNTERA", type="primary"):
        df = pd.read_excel(h_file)
        
        # FIX: PANDAS FLOAT64 ISSUE
        email_col = next((c for c in df.columns if 'mail' in c.lower()), 'Email')
        if email_col not in df.columns: df[email_col] = ""
        df[email_col] = df[email_col].astype(object)
        
        surname_col = next((c for c in df.columns if 'surname' in c.lower() or 'nazwisko' in c.lower()), 'Surname')
        name_col = next((c for c in df.columns if 'name' in c.lower() and 'surname' not in c.lower()), 'Name')
        aff_col = next((c for c in df.columns if 'country' in c.lower() or 'affil' in c.lower()), None)
        orcid_col = next((c for c in df.columns if 'orcid' in c.lower()), None)
        
        prog_bar = st.progress(0)
        status_text = st.empty()
        znalezione = 0
        total = len(df)
        
        for idx, row in df.iterrows():
            sn = str(row.get(surname_col, '')).strip()
            nm = str(row.get(name_col, '')).strip()
            aff = str(row.get(aff_col, '')).strip()
            orc = str(row.get(orcid_col, '')).strip()
            if 'orcid.org/' in orc: orc = orc.split('/')[-1]
            
            if pd.isna(sn) or sn == "" or '@' in str(row.get(email_col, '')): 
                prog_bar.progress((idx + 1) / total)
                continue
                
            status_text.text(f"🔍 Szukam: {nm} {sn}...")
            email_found = ""; all_s = []

            if orc and len(orc) >= 15:
                o_emails, o_urls = get_emails_from_orcid(orc)
                all_s.extend(o_emails)
                if is_deep:
                    for u in o_urls[:3]: all_s.extend(scrape_deep(u, nm + " " + sn))
                email_found = get_matched_email(sn, list(set(all_s)))

            if not email_found and HAS_DDGS:
                time.sleep(2.0) # Zabezpieczenie przed limitem w Streamlit Cloud
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
                    st.warning(f"Błąd silnika dla {sn}: {e}")

            if email_found:
                df.at[idx, email_col] = email_found
                znalezione += 1
                
            prog_bar.progress((idx + 1) / total)
            
        status_text.text(f"✅ Zakończono! Uzupełniono {znalezione} rekordów.")
        st.download_button("💾 Pobierz Uzupełnioną Bazę", data=to_excel_buffer(df), file_name="Hunter_Results.xlsx")