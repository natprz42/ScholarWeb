import streamlit as st
import pandas as pd
import time
import re
import requests
import urllib.parse as urlparse
from urllib.parse import urlencode
import unicodedata
import io
from ddgs import DDGS

# --- KONFIGURACJA STRONY ---
st.set_page_config(page_title="ScholarHunt Cloud", page_icon="📚", layout="wide")

# --- FUNKCJE POMOCNICZE ---
def to_excel_buffer(df):
    """Zamienia DataFrame na dane Excela w pamięci RAM"""
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df.to_excel(writer, index=False)
    return output.getvalue()

def normalize_international(text):
    if not isinstance(text, str) or pd.isna(text): return ""
    nfkd_form = unicodedata.normalize('NFKD', text)
    result = "".join([c for c in nfkd_form if not unicodedata.combining(c)])
    for k, v in {'ł': 'l', 'Ł': 'l', 'ø': 'o', 'Ø': 'o', 'æ': 'ae', 'ß': 'ss', 'đ': 'd', '[at]': '@', '(at)': '@', ' [dot] ': '.'}.items(): 
        result = result.replace(k, v)
    return result.lower().strip()

def clean_author_name(n):
    n_clean = re.sub(r'\(\d+\)', '', str(n)).strip()
    n_clean = re.sub(r'\(.*?\)', '', n_clean).strip()
    if ',' in n_clean:
        parts = n_clean.split(',', 1)
        return parts[0].strip(), parts[1].strip()
    parts = n_clean.split()
    return (parts[-1], " ".join(parts[:-1])) if len(parts) > 1 else (n_clean, "")

def extract_emails_from_text(text):
    text = str(text).lower().replace(' [at] ', '@').replace('(at)', '@').replace(' [dot] ', '.')
    for junk in ['works.email', 'email:', 'e-mail:', 'mailto:', 'contact:', 'mail:', 'email']: text = text.replace(junk, ' ')
    found = re.findall(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.(?:com|org|edu|net|gov|mil|int|info|biz|eu|[a-zA-Z]{2})', text)
    return list(set([re.sub(r'^[^a-zA-Z0-9]+', '', e) for e in found if not e.endswith(('.png', '.jpg', '.jpeg', '.gif', '.css', '.js'))]))

def get_matched_email(surname, email_list):
    if not email_list: return ""
    sn_raw = normalize_international(surname)
    sn_clean = re.sub(r'[^a-z]', '', sn_raw)
    for em in email_list:
        em_str = str(em).lower().strip()
        if sn_raw in em_str: return em_str
        if "@" in em_str and sn_clean and sn_clean in re.sub(r'[^a-z]', '', em_str.split('@')[0]): return em_str
    return ""

def extract_universal_data(df, r):
    doi = str(r.get('DOI', r.get('DI', ''))).strip()
    title = str(r.get('Title', r.get('Article Title', r.get('TI', '')))).strip()
    raw_authors = str(r.get('Author Full Names', r.get('AF', '')))
    af = [x.strip() for x in raw_authors.split(';') if x.strip()] if raw_authors and raw_authors.lower() != 'nan' else [str(r.get('Authors', r.get('Name', '')))]
    raw_ems = str(r.get('Email Addresses', r.get('EM', '')))
    em_all = [x.strip().lower() for x in raw_ems.split(';') if "@" in x] if raw_ems and raw_ems.lower() != 'nan' else []
    return doi, title, af, em_all

def enforce_column_order(df, mode=""):
    base_order = ['Name', 'Surname', 'Email', 'ORCID', 'Country', 'Affiliation', 'DG Journal name', 'DG article title']
    if mode in ['merge', 'past']:
        for col in ['link', 'DG article DOI', 'DG article title']:
            if col in df.columns: df = df.drop(columns=[col])
        if mode == 'merge' and 'DG Journal name' in df.columns: df = df.drop(columns=['DG Journal name'])
    else: base_order.extend(['link', 'DG article DOI'])
    base_order.append('Article Title')
    for col in base_order:
        if col not in df.columns: df[col] = "" 
    return df

# --- INTERFEJS ---
st.title("📚 ScholarHunt Cloud")
tab_merge, tab_past, tab_hunter = st.tabs(["🗂️ Merge", "👥 Past Authors", "🕵️ Hunter"])

with tab_merge:
    m_files = st.file_uploader("Wgraj pliki", accept_multiple_files=True, key="m_f")
    m_oa = st.text_input("OpenAlex Link", key="m_oa")
    if st.button("🚀 RUN MERGE"):
        final_rows = []
        for f in m_files:
            df = pd.read_excel(f)
            for _, r in df.iterrows():
                doi, title, af, ems = extract_universal_data(df, r)
                for n in af:
                    sn, fn = clean_author_name(n)
                    final_rows.append({"Name": fn, "Surname": sn, "Email": get_matched_email(sn, ems), "Article Title": title})
        out_df = enforce_column_order(pd.DataFrame(final_rows), 'merge')
        st.download_button("💾 Pobierz Wynik XLSX", data=to_excel_buffer(out_df), file_name="Merge_Result.xlsx")

with tab_past:
    p_files = st.file_uploader("Wgraj pliki", accept_multiple_files=True, key="p_f")
    p_jrnl = st.text_input("DG Journal name:", key="p_j")
    if st.button("🚀 RUN SPLIT"):
        final_rows = []
        for f in p_files:
            df = pd.read_excel(f)
            for _, r in df.iterrows():
                doi, title, af, ems = extract_universal_data(df, r)
                for n in af:
                    sn, fn = clean_author_name(n)
                    final_rows.append({"Name": fn, "Surname": sn, "Email": get_matched_email(sn, ems), "Article Title": title, "DG Journal name": p_jrnl})
        out_df = enforce_column_order(pd.DataFrame(final_rows), 'past')
        st.download_button("💾 Pobierz CoAuthors XLSX", data=to_excel_buffer(out_df), file_name="CoAuthors.xlsx")

with tab_hunter:
    st.header("🕵️ Email Hunter")
    h_file = st.file_uploader("Wgraj bazę", type=["xlsx", "xls"], key="h_f")
    
    if h_file and st.button("🚀 Uruchom Huntera"):
        df = pd.read_excel(h_file)
        email_col = next((c for c in df.columns if 'mail' in c.lower()), 'Email')
        if email_col not in df.columns: df[email_col] = ""
        df[email_col] = df[email_col].astype(object)
        
        surname_col = next((c for c in df.columns if 'surname' in c.lower() or 'nazwisko' in c.lower()), 'Surname')
        name_col = next((c for c in df.columns if 'name' in c.lower() and 'surname' not in c.lower()), 'Name')
        
        progress = st.progress(0)
        for idx, row in df.iterrows():
            sn = str(row.get(surname_col, '')).strip()
            nm = str(row.get(name_col, '')).strip()
            
            if pd.isna(sn) or sn == "" or '@' in str(row.get(email_col, '')): continue
            
            try:
                with DDGS() as ddgs:
                    res = list(ddgs.text(f'"{nm} {sn}" email', max_results=3))
                    emails = []
                    for r in res: emails.extend(extract_emails_from_text(r.get('body', '') + " " + r.get('title', '')))
                    if emails: df.at[idx, email_col] = emails[0]
            except Exception as e: st.warning(f"Błąd przy {sn}: {e}")
            progress.progress((idx + 1) / len(df))
            
        st.download_button("💾 Pobierz Uzupełniony XLSX", data=to_excel_buffer(df), file_name="Hunter_Results.xlsx")