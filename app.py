import streamlit as st
import pandas as pd
import time
import os
import re
import requests
import urllib.parse as urlparse
from urllib.parse import urlencode
import unicodedata
import io

try:
    from duckduckgo_search import DDGS
    HAS_DDGS = True
except ImportError:
    HAS_DDGS = False

# --- KONFIGURACJA STRONY ---
st.set_page_config(page_title="ScholarHunt Cloud", page_icon="📚", layout="wide")

TLD_MAP = {
    'sweden': '.se', 'poland': '.pl', 'germany': '.de', 'france': '.fr',
    'italy': '.it', 'spain': '.es', 'china': '.cn', 'japan': '.jp',
    'uk': '.uk', 'united kingdom': '.uk', 'england': '.uk', 'australia': '.au',
    'canada': '.ca', 'brazil': '.br', 'india': '.in', 'russia': '.ru',
    'netherlands': '.nl', 'switzerland': '.ch', 'belgium': '.be'
}

# --- FUNKCJE POMOCNICZE (Z CoreLogicMixin) ---
def normalize_international(text):
    if not isinstance(text, str) or pd.isna(text): return ""
    nfkd_form = unicodedata.normalize('NFKD', text)
    result = "".join([c for c in nfkd_form if not unicodedata.combining(c)])
    for k, v in {'ł': 'l', 'Ł': 'l', 'ø': 'o', 'Ø': 'o', 'æ': 'ae', 'ß': 'ss', 'đ': 'd', '[at]': '@', '(at)': '@', ' [dot] ': '.'}.items(): 
        result = result.replace(k, v)
    return result.lower().strip()

def clean_author_name(n):
    n_clean = re.sub(r'\(\d+\)', '', n).strip()
    n_clean = re.sub(r'\(.*?\)', '', n_clean).strip()
    if ',' in n_clean:
        parts = n_clean.split(',', 1)
        return parts[0].strip(), parts[1].strip()
    parts = n_clean.split()
    return (parts[-1], " ".join(parts[:-1])) if len(parts) > 1 else (n_clean, "")

def extract_emails_from_text(text):
    text = str(text).lower().replace(' [at] ', '@').replace('(at)', '@').replace(' [dot] ', '.')
    for junk in ['works.email', 'email:', 'e-mail:', 'mailto:', 'contact:', 'mail:', 'email']:
        text = text.replace(junk, ' ')
    found = re.findall(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.(?:com|org|edu|net|gov|mil|int|info|biz|eu|[a-zA-Z]{2})', text)
    cleaned = []
    for e in found:
        e = re.sub(r'^[^a-zA-Z0-9]+', '', e)
        if not e.endswith(('.png', '.jpg', '.jpeg', '.gif', '.css', '.js')): cleaned.append(e)
    return list(set(cleaned))

def check_match_in_flight(surname, email):
    if not email or "@" not in email or not surname: return False
    email_prefix = email.split('@')[0]
    email_clean = re.sub(r'[^a-z]', '', normalize_international(email_prefix))
    surname_clean = re.sub(r'[^a-z]', '', normalize_international(surname))
    if len(surname_clean) <= 2: return surname_clean == email_clean
    return surname_clean in email_clean

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
    if check_match_in_flight(sn_clean, em_clean): return "Certain (Surname in email)"
    tld = TLD_MAP.get(c_clean, "")
    if tld and em_clean.endswith(tld): return f"Probable (Country Match {tld})"
    if em_clean.endswith(('.edu', '.ac.uk', '.edu.cn', '.edu.au', '.edu.pl')): return "Probable (Academic Domain)"
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
    for c in ['Addresses', 'C1']:
        if c in df.columns and pd.notna(r.get(c)): ad += " " + str(r.get(c))
    for c in ['Reprint Addresses', 'RP', 'Correspondence Address', 'Corresponding Authors']:
        if c in df.columns and pd.notna(r.get(c)): rp += " " + str(r.get(c))
        
    return doi, row_title, af, em_all, ad, rp

def get_emails_from_orcid(orcid_id):
    emails = []
    if not orcid_id or len(orcid_id) < 15: return emails
    try:
        res = requests.get(f"https://pub.orcid.org/v3.0/{orcid_id}/person", headers={"Accept": "application/json"}, timeout=5)
        if res.status_code == 200:
            data = res.json()
            if 'emails' in data and data['emails'] and 'email' in data['emails']:
                for e in data['emails']['email']: emails.append(e.get('email', '').strip())
    except: pass
    return list(set(emails))

def fetch_works_from_openalex_url(url, api_key=""):
    url_parts = list(urlparse.urlparse(url))
    query = dict(urlparse.parse_qsl(url_parts[4]))
    query['mailto'] = "natprz42@gmail.com"
    if api_key: query['api_key'] = api_key
    if "filter=" in url or "search=" in url: query['per-page'] = '100'
        
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
                time.sleep(0.3)
            else:
                all_works.append(data)
                break
        except: break
    return all_works

def extract_openalex_work(work):
    doi = work.get('doi', '').replace('https://doi.org/', '')
    row_title = work.get('title', 'No title')
    af, em_all, ad, rp = [], [], "", ""
    orcid_map = {}
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
        # Wymagane usunięcia dla Merge i Past Authors
        if 'link' in df.columns: df = df.drop(columns=['link'])
        if 'DG article DOI' in df.columns: df = df.drop(columns=['DG article DOI'])
        if 'DG article title' in df.columns: df = df.drop(columns=['DG article title'])
        if 'DG article title' in base_order: base_order.remove('DG article title')
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

def get_base_dataframe(uploaded_files, openalex_link, api_key):
    """Pomocnicza funkcja łącząca wczytywanie plików i API"""
    dfs = []
    if uploaded_files:
        for file in uploaded_files:
            try:
                df = pd.read_excel(file) if file.name.endswith(('xls', 'xlsx')) else pd.read_csv(file)
                dfs.append(df)
            except Exception as e: st.error(f"Błąd czytania pliku {file.name}: {e}")
    
    oa_works = []
    if openalex_link:
        with st.spinner("Pobieranie danych z OpenAlex API..."):
            oa_works = fetch_works_from_openalex_url(openalex_link, api_key)
            
    return dfs, oa_works

# --- INICJALIZACJA STANU (Dla zakładek Cited/Citing) ---
if 'cited_db' not in st.session_state: st.session_state.cited_db = []
if 'citing_db' not in st.session_state: st.session_state.citing_db = []

# --- INTERFEJS STRONY ---
st.title("📚 ScholarHunt Cloud")
st.markdown("Scientific Contacts Management System")

# Pasek boczny na API Key
with st.sidebar:
    st.header("⚙️ Ustawienia")
    global_api_key = st.text_input("OpenAlex API Key (Opcjonalnie):", type="password")

# --- ZAKŁADKI ---
tab_merge, tab_past, tab_kw, tab_cited, tab_citing, tab_val, tab_hunter = st.tabs([
    "🗂️ Merge", "👥 Past Authors", "🔑 Keywords", "📜 Cited", "💬 Citing", "✅ Validation", "🕵️ Hunter"
])

# ---------------------------------------------------------
# 1. ZAKŁADKA MERGE
# ---------------------------------------------------------
with tab_merge:
    st.header("🗂️ Merge Files")
    st.write("Łączy pliki, usuwa linki, DOI i tytuły DG. Nie wymaga nazwy czasopisma.")
    
    m_files = st.file_uploader("Wgraj pliki WoS/Excel", accept_multiple_files=True, key="m_files")
    m_oa_link = st.text_input("OpenAlex API Link (Opcjonalnie)", key="m_oa")
    
    if st.button("🚀 Uruchom Merge", type="primary"):
        dfs, oa_works = get_base_dataframe(m_files, m_oa_link, global_api_key)
        final_rows = []
        
        with st.spinner("Przetwarzanie danych..."):
            for df in dfs:
                for _, r in df.iterrows():
                    doi, title, af, ems, ad, rp = extract_universal_data(df, r)
                    for n in af:
                        sn, fn = clean_author_name(n)
                        if sn: final_rows.append({"Name": fn, "Surname": sn, "Email": get_matched_email(sn, ems), "Article Title": title})
            
            for work in oa_works:
                doi, title, af, ems, ad, rp, orcid_map = extract_openalex_work(work)
                for n in af:
                    sn, fn = clean_author_name(n)
                    if sn: final_rows.append({"Name": fn, "Surname": sn, "Email": "", "Article Title": title, "ORCID": orcid_map.get(n, "")})

        if final_rows:
            out_df = pd.DataFrame(final_rows)
            out_df = enforce_column_order(out_df, 'merge')
            if 'Email' in out_df.columns:
                out_df = pd.concat([out_df[out_df['Email'] != ''].drop_duplicates(subset=['Email']), out_df[out_df['Email'] == ''].drop_duplicates(subset=['Name', 'Surname'])])
            
            st.success(f"Połączono pomyślnie. {len(out_df)} unikalnych wierszy.")
            st.dataframe(out_df.head())
            
            output = io.BytesIO()
            with pd.ExcelWriter(output, engine='openpyxl') as writer: out_df.to_excel(writer, index=False)
            st.download_button("💾 Pobierz Wynik (Merge.xlsx)", data=output.getvalue(), file_name="Merge_Result.xlsx")

# ---------------------------------------------------------
# 2. ZAKŁADKA PAST AUTHORS
# ---------------------------------------------------------
with tab_past:
    st.header("👥 Past Authors (Splitting)")
    st.write("Wymaga nazwy czasopisma. Usuwa linki i DOI. Dzieli na Corresponding i Co-Authors.")
    
    p_files = st.file_uploader("Wgraj pliki", accept_multiple_files=True, key="p_files")
    p_oa_link = st.text_input("OpenAlex API Link", key="p_oa")
    p_jrnl = st.text_input("DG Journal name:", key="p_jrnl")
    
    if st.button("🚀 Uruchom Split", type="primary"):
        dfs, oa_works = get_base_dataframe(p_files, p_oa_link, global_api_key)
        final_rows = []
        
        with st.spinner("Analiza autorów korespondencyjnych..."):
            for df in dfs:
                for _, r in df.iterrows():
                    doi, title, af, ems, ad, rp = extract_universal_data(df, r)
                    for n in af:
                        sn, fn = clean_author_name(n)
                        if sn: final_rows.append({"Name": fn, "Surname": sn, "Email": get_matched_email(sn, ems), "Article Title": title, "is_corr": sn.lower() in rp.lower(), "DG Journal name": p_jrnl})
            
            for work in oa_works:
                doi, title, af, ems, ad, rp, orcid_map = extract_openalex_work(work)
                for n in af:
                    sn, fn = clean_author_name(n)
                    if sn: final_rows.append({"Name": fn, "Surname": sn, "Email": "", "Article Title": title, "ORCID": orcid_map.get(n, ""), "is_corr": sn.lower() in rp.lower(), "DG Journal name": p_jrnl})

        if final_rows:
            out_df = pd.DataFrame(final_rows)
            out_df = enforce_column_order(out_df, 'past')
            
            if 'Email' in out_df.columns:
                out_df = pd.concat([out_df[out_df['Email'] != ''].drop_duplicates(subset=['Email']), out_df[out_df['Email'] == ''].drop_duplicates(subset=['Name', 'Surname'])])

            df_corr = out_df[out_df['is_corr']==True].drop(columns=['is_corr'], errors='ignore')
            df_co = out_df[out_df['is_corr']==False].drop(columns=['is_corr'], errors='ignore')
            
            st.success("Podział zakończony!")
            col1, col2 = st.columns(2)
            
            out_corr = io.BytesIO()
            with pd.ExcelWriter(out_corr, engine='openpyxl') as w: df_corr.to_excel(w, index=False)
            col1.download_button("💾 Pobierz Corresponding", data=out_corr.getvalue(), file_name="Corresponding.xlsx")
            
            out_co = io.BytesIO()
            with pd.ExcelWriter(out_co, engine='openpyxl') as w: df_co.to_excel(w, index=False)
            col2.download_button("💾 Pobierz CoAuthors", data=out_co.getvalue(), file_name="CoAuthors.xlsx")

# ---------------------------------------------------------
# 3. ZAKŁADKA KEYWORDS
# ---------------------------------------------------------
with tab_kw:
    st.header("🔑 Keywords Mailing List")
    k_files = st.file_uploader("Wgraj pliki", accept_multiple_files=True, key="k_files")
    k_jrnl = st.text_input("DG Journal name:", key="k_jrnl")
    k_kw = st.text_input("DG Keyword:", key="k_kw")
    k_title = st.text_input("DG article title:", key="k_title")
    k_link = st.text_input("Link:", key="k_link")
    k_doi = st.text_input("DG article DOI:", key="k_doi")
    
    if st.button("🚀 Generuj Keywords", type="primary"):
        dfs, _ = get_base_dataframe(k_files, "", global_api_key)
        final_rows = []
        with st.spinner("Generowanie listy..."):
            for df in dfs:
                for _, r in df.iterrows():
                    doi, title, af, ems, ad, rp = extract_universal_data(df, r)
                    for n in af:
                        sn, fn = clean_author_name(n)
                        if sn: final_rows.append({"Name": fn, "Surname": sn, "Email": get_matched_email(sn, ems), "Article Title": title, "DG Journal name": k_jrnl, "DG Keyword": k_kw, "DG article title": k_title, "link": k_link, "DG article DOI": k_doi})
        if final_rows:
            out_df = pd.DataFrame(final_rows)
            out_df = enforce_column_order(out_df, 'keywords')
            if 'Email' in out_df.columns:
                out_df = pd.concat([out_df[out_df['Email'] != ''].drop_duplicates(subset=['Email']), out_df[out_df['Email'] == ''].drop_duplicates(subset=['Name', 'Surname'])])
            st.success("Lista wygenerowana!")
            output = io.BytesIO()
            with pd.ExcelWriter(output, engine='openpyxl') as writer: out_df.to_excel(writer, index=False)
            st.download_button("💾 Pobierz Keywords List", data=output.getvalue(), file_name="Keywords_List.xlsx")

# ---------------------------------------------------------
# 4. ZAKŁADKA CITED
# ---------------------------------------------------------
with tab_cited:
    st.header("📜 Cited (Backwards/References)")
    
    st.subheader("Manual Mode (Własne pliki)")
    cm_jrnl = st.text_input("DG Journal name:", key="cm_j")
    cm_title = st.text_input("DG article title:", key="cm_t")
    cm_link = st.text_input("Link:", key="cm_l")
    cm_doi = st.text_input("DG article DOI:", key="cm_d")
    cm_files = st.file_uploader("Wgraj plik", accept_multiple_files=True, key="cm_f")
    
    if st.button("➕ Dodaj Pliki (Manual)"):
        if cm_title and cm_files:
            dfs, _ = get_base_dataframe(cm_files, "", global_api_key)
            for df in dfs:
                for _, r in df.iterrows():
                    doi, title, af, ems, ad, rp = extract_universal_data(df, r)
                    for n in af:
                        sn, fn = clean_author_name(n)
                        if sn: st.session_state.cited_db.append({"Name": fn, "Surname": sn, "Email": get_matched_email(sn, ems), "DG Journal name": cm_jrnl, "DG article title": cm_title, "link": cm_link, "DG article DOI": cm_doi})
            st.success("Dodano do bazy podręcznej!")

    st.subheader("Auto-Pilot (Z OpenAlex)")
    ca_jrnl = st.text_input("DG Journal name:", key="ca_j")
    ca_doi = st.text_input("Base Paper DOI (np. 10.1515/...):", key="ca_doi")
    
    if st.button("🚀 Pobierz referencje Auto-Pilotem"):
        if ca_doi:
            clean_doi = ca_doi.replace('https://doi.org/', '').strip()
            with st.spinner("Pobieranie referencji..."):
                headers = {'api_key': global_api_key} if global_api_key else {}
                res = requests.get(f"https://api.openalex.org/works/https://doi.org/{clean_doi}", headers=headers).json()
                b_title = res.get('title', 'Unknown')
                ref_urls = res.get('referenced_works', [])
                if ref_urls:
                    ref_ids = [ref.split('/')[-1] for ref in ref_urls]
                    for k in range(0, len(ref_ids), 50):
                        works = fetch_works_from_openalex_url("https://api.openalex.org/works?filter=openalex:" + "|".join(ref_ids[k:k+50]), global_api_key)
                        for work in works:
                            w_doi, w_title, af, _, _, _, orcid_map = extract_openalex_work(work)
                            for n in af:
                                sn, fn = clean_author_name(n)
                                if sn: st.session_state.cited_db.append({"Name": fn, "Surname": sn, "Email": "", "Article Title": w_title, "ORCID": orcid_map.get(n, ""), "DG Journal name": ca_jrnl, "DG article title": b_title, "link": f"https://doi.org/{clean_doi}", "DG article DOI": clean_doi})
                    st.success(f"Pobrano {len(ref_urls)} artykułów do bazy podręcznej!")

    st.write(f"📊 Baza podręczna Cited: {len(st.session_state.cited_db)} rekordów")
    if st.session_state.cited_db:
        if st.button("🗑️ Wyczyść bazę Cited"): st.session_state.cited_db = []
        out_df = enforce_column_order(pd.DataFrame(st.session_state.cited_db), 'cited')
        out_cited = io.BytesIO()
        with pd.ExcelWriter(out_cited, engine='openpyxl') as w: out_df.to_excel(w, index=False)
        st.download_button("💾 Eksportuj Bazę Cited", data=out_cited.getvalue(), file_name="Cited_Outreach_Base.xlsx")

# ---------------------------------------------------------
# 5. ZAKŁADKA CITING (Analogicznie do Cited)
# ---------------------------------------------------------
with tab_citing:
    st.header("💬 Citing (Forwards/Citations)")
    st.subheader("Auto-Pilot (Kto cytuje ten artykuł?)")
    cia_jrnl = st.text_input("DG Journal name:", key="cia_j")
    cia_doi = st.text_input("Base Paper DOI:", key="cia_doi")
    
    if st.button("🚀 Pobierz cytowania Auto-Pilotem"):
        if cia_doi:
            clean_doi = cia_doi.replace('https://doi.org/', '').strip()
            with st.spinner("Pobieranie cytowań..."):
                headers = {'api_key': global_api_key} if global_api_key else {}
                res = requests.get(f"https://api.openalex.org/works/https://doi.org/{clean_doi}", headers=headers).json()
                b_title = res.get('title', 'Unknown')
                b_id = res.get('id', '').split('/')[-1]
                works = fetch_works_from_openalex_url(f"https://api.openalex.org/works?filter=cites:{b_id}", global_api_key)
                for work in works:
                    w_doi, w_title, af, _, _, _, orcid_map = extract_openalex_work(work)
                    for n in af:
                        sn, fn = clean_author_name(n)
                        if sn: st.session_state.citing_db.append({"Name": fn, "Surname": sn, "Email": "", "Article Title": w_title, "ORCID": orcid_map.get(n, ""), "DG Journal name": cia_jrnl, "DG article title": b_title, "link": f"https://doi.org/{clean_doi}", "DG article DOI": clean_doi})
                st.success("Pobrano cytowania do bazy podręcznej!")

    st.write(f"📊 Baza podręczna Citing: {len(st.session_state.citing_db)} rekordów")
    if st.session_state.citing_db:
        if st.button("🗑️ Wyczyść bazę Citing"): st.session_state.citing_db = []
        out_df = enforce_column_order(pd.DataFrame(st.session_state.citing_db), 'citing')
        out_citing = io.BytesIO()
        with pd.ExcelWriter(out_citing, engine='openpyxl') as w: out_df.to_excel(w, index=False)
        st.download_button("💾 Eksportuj Bazę Citing", data=out_citing.getvalue(), file_name="Citing_Outreach_Base.xlsx")

# ---------------------------------------------------------
# 6. ZAKŁADKA VALIDATION
# ---------------------------------------------------------
with tab_val:
    st.header("✅ Email Validation")
    val_file = st.file_uploader("Wgraj plik z mailami", type=["xlsx", "xls"], key="v_file")
    
    if val_file and st.button("🔍 Waliduj", type="primary"):
        with st.spinner("Sprawdzanie adresów..."):
            df = pd.read_excel(val_file)
            em_col = next((c for c in df.columns if 'mail' in str(c).lower()), None)
            sn_col = next((c for c in df.columns if 'surname' in str(c).lower() or 'nazwisko' in str(c).lower()), None)
            c_col = next((c for c in df.columns if 'country' in str(c).lower() or 'kraj' in str(c).lower()), None)
            
            if em_col and sn_col:
                status_list = []
                for _, row in df.iterrows():
                    em = str(row[em_col]); sn = str(row[sn_col]); ct = str(row[c_col]) if c_col else ""
                    status_list.append(validate_email_intelligence(em, sn, ct) if em and em != 'nan' else "Missing Email")
                
                df['Status Walidacji'] = status_list
                output = io.BytesIO()
                with pd.ExcelWriter(output, engine='openpyxl') as writer:
                    df[df['Status Walidacji'].str.startswith('Certain')].to_excel(writer, sheet_name='Certain', index=False)
                    df[df['Status Walidacji'].str.startswith('Probable')].to_excel(writer, sheet_name='Probable', index=False)
                    df[df['Status Walidacji'] == 'Manual Verification Required'].to_excel(writer, sheet_name='To_Verify', index=False)
                    df[df['Status Walidacji'].str.contains('Invalid|Missing')].to_excel(writer, sheet_name='Invalid', index=False)
                st.success("Walidacja zakończona!")
                st.download_button("💾 Pobierz Raport", data=output.getvalue(), file_name="Validated_Report.xlsx")
            else: st.error("Brak kolumny Email lub Surname.")

# ---------------------------------------------------------
# 7. ZAKŁADKA HUNTER (DDGS)
# ---------------------------------------------------------
with tab_hunter:
    st.header("🕵️ Email Hunter")
    
    if not HAS_DDGS:
        st.error("Brak biblioteki duckduckgo_search! Upewnij się, że jest w requirements.txt")
        
    hunter_file = st.file_uploader("Wgraj bazę brakujących", type=["xlsx", "xls"], key="h_file")
    strategy = st.radio("Strategia", ["Google-Style (Imię + Nazwisko + 'email')", "Instytucja (Nazwisko + Instytucja + 'email')"])
    
    if hunter_file and st.button("🚀 Uruchom Huntera", type="primary"):
        df = pd.read_excel(hunter_file)
        email_col = next((c for c in df.columns if 'mail' in c.lower()), 'Email')
        surname_col = next((c for c in df.columns if 'surname' in c.lower() or 'nazwisko' in c.lower()), 'Surname')
        name_col = next((c for c in df.columns if 'name' in c.lower() and 'surname' not in c.lower()), 'Name')
        aff_col = next((c for c in df.columns if 'country' in c.lower() or 'affil' in c.lower()), None)
        orcid_col = next((c for c in df.columns if 'orcid' in c.lower()), None)
        
        if email_col not in df.columns: df[email_col] = ""
        found_count = 0; total = len(df)
        
        progress_bar = st.progress(0)
        status_text = st.empty()
        
        for idx, row in df.iterrows():
            surname = str(row.get(surname_col, '')).strip()
            name = str(row.get(name_col, '')).strip()
            aff = str(row.get(aff_col, '')).strip()
            orcid_id = str(row.get(orcid_col, '')).strip()
            if 'orcid.org/' in orcid_id: orcid_id = orcid_id.split('/')[-1]
            
            if pd.isna(surname) or surname == "" or '@' in str(row.get(email_col, '')):
                progress_bar.progress((idx + 1) / total)
                continue

            status_text.text(f"🔍 Szukam: {name} {surname}...")
            email_found = ""; all_s = []

            # Szukanie po ORCID
            if orcid_id and len(orcid_id) >= 15:
                all_s.extend(get_emails_from_orcid(orcid_id))
                email_found = get_matched_email(surname, list(set(all_s)))

            # Szukanie w Internecie przez DDGS
            if not email_found and HAS_DDGS:
                time.sleep(1.5) # ZAPOBIEGA BLOKADZIE W CHMURZE
                queries = [f'"{name} {surname}" email'] if "Google" in strategy else [f'"{name} {surname}" {aff if aff and aff.lower()!="nan" else "university"} email']
                try:
                    with DDGS() as ddgs:
                        for q in queries:
                            if email_found: break
                            res = list(ddgs.text(q, max_results=3))
                            for r in res:
                                all_s.extend(extract_emails_from_text(r.get('body', '') + " " + r.get('title', '')))
                            email_found = get_matched_email(surname, list(set(all_s)))
                except Exception as e:
                    st.warning(f"Problem DDGS przy {surname}: {e}")

            if email_found:
                df.at[idx, email_col] = email_found
                found_count += 1
            progress_bar.progress((idx + 1) / total)
            
        status_text.text(f"✅ Zakończono! Znaleziono {found_count} nowych adresów.")
        output = io.BytesIO()
        with pd.ExcelWriter(output, engine='openpyxl') as writer: df.to_excel(writer, index=False)
        st.download_button("💾 Pobierz Uzupełniony Plik", data=output.getvalue(), file_name="Hunter_Results.xlsx")