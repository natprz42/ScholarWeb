import streamlit as st
import pandas as pd
import time, re, requests, io
import urllib.parse as urlparse
from urllib.parse import urlencode
from ddgs import DDGS
from openpyxl import Workbook

st.set_page_config(page_title="ScholarHunt Cloud", layout="wide")

# --- NARZĘDZIA (CoreLogicMixin) ---
def clean_author_name(n):
    n_clean = re.sub(r'\(\d+\)', '', str(n)).strip()
    if ',' in n_clean: parts = n_clean.split(',', 1); return parts[0].strip(), parts[1].strip()
    parts = n_clean.split()
    return (parts[-1], " ".join(parts[:-1])) if len(parts) > 1 else (n_clean, "")

def extract_emails_from_text(text):
    found = re.findall(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.(?:com|org|edu|net|gov|int|info|biz|eu|[a-zA-Z]{2})', str(text).lower())
    return list(set([e for e in found if not e.endswith(('.png', '.jpg', '.jpeg', '.gif'))]))

def get_matched_email(surname, email_list):
    for em in email_list:
        if str(surname).lower() in em.lower(): return em
    return ""

def enforce_column_order(df, mode=""):
    base_order = ['Name', 'Surname', 'Email', 'ORCID', 'Country', 'Affiliation', 'DG Journal name', 'DG article title']
    if mode not in ['merge', 'past']: base_order.extend(['link', 'DG article DOI'])
    base_order.append('Article Title')
    for col in base_order:
        if col not in df.columns: df[col] = ""
    return df[base_order + [c for c in df.columns if c not in base_order]]

# --- UI (GuiTabsMixin) ---
st.title("📚 ScholarHunt Cloud")
tabs = st.tabs(["🗂️ Merge", "👥 Past", "🔑 Keywords", "📜 Cited", "💬 Citing", "✅ Validation", "🕵️ Hunter"])

# --- ZAKŁADKA MERGE ---
with tabs[0]:
    st.header("🗂️ Merge Files")
    files = st.file_uploader("Wgraj pliki", accept_multiple_files=True, key="m_f")
    oa = st.text_input("OpenAlex Link")
    if st.button("RUN MERGE"):
        data = []
        for f in files:
            df = pd.read_excel(f)
            for _, r in df.iterrows():
                data.append({"Name": str(r.get('Name','')), "Surname": str(r.get('Surname','')), "Email": str(r.get('Email',''))})
        df = pd.DataFrame(data)
        out = io.BytesIO()
        with pd.ExcelWriter(out, engine='openpyxl') as w: df.to_excel(w, index=False)
        st.download_button("💾 Pobierz XLSX", out.getvalue(), "Merge.xlsx")

# --- ZAKŁADKA PAST AUTHORS ---
with tabs[1]:
    st.header("👥 Past Authors")
    files = st.file_uploader("Wgraj pliki", accept_multiple_files=True, key="p_f")
    jrnl = st.text_input("DG Journal name:", key="p_j")
    if st.button("RUN SPLIT"):
        data = []
        for f in files:
            df = pd.read_excel(f)
            for _, r in df.iterrows():
                data.append({"Name": str(r.get('Name','')), "Surname": str(r.get('Surname','')), "DG Journal name": jrnl})
        out = io.BytesIO()
        with pd.ExcelWriter(out, engine='openpyxl') as w: pd.DataFrame(data).to_excel(w, index=False)
        st.download_button("💾 Pobierz XLSX", out.getvalue(), "PastAuthors.xlsx")

# --- ZAKŁADKA KEYWORDS ---
with tabs[2]:
    st.header("🔑 Keywords")
    # Analogiczna logika...

# --- ZAKŁADKA CITED/CITING ---
with tabs[3]:
    st.header("📜 Cited / Citing")
    st.write("Użyj sekcji Auto-Pilot z DOI")
    doi = st.text_input("Wpisz DOI:")
    if st.button("Pobierz dane"):
        st.info("Tutaj wstawiamy logikę OpenAlex...")

# --- ZAKŁADKA HUNTER ---
with tabs[4]:
    st.header("✅ Validation")
    st.write("Walidacja email...")

with tabs[5]:
    st.header("🕵️ Email Hunter")
    h_file = st.file_uploader("Wgraj bazę", type=["xlsx", "xls"], key="h_f")
    if h_file and st.button("🚀 Start Hunter"):
        df = pd.read_excel(h_file)
        email_col = next((c for c in df.columns if 'mail' in c.lower()), 'Email')
        surname_col = next((c for c in df.columns if 'surname' in c.lower()), 'Surname')
        name_col = next((c for c in df.columns if 'name' in c.lower()), 'Name')
        
        prog = st.progress(0)
        for idx, row in df.iterrows():
            sn = str(row.get(surname_col, ''))
            nm = str(row.get(name_col, ''))
            try:
                with DDGS() as ddgs:
                    res = list(ddgs.text(f'"{nm} {sn}" email', max_results=3))
                    emails = []
                    for r in res: emails.extend(extract_emails_from_text(r.get('body', '')))
                    if emails: df.at[idx, email_col] = emails[0]
            except Exception as e: st.error(f"Błąd przy {sn}: {e}")
            prog.progress((idx+1)/len(df))
            
        out = io.BytesIO()
        with pd.ExcelWriter(out, engine='openpyxl') as w: df.to_excel(w, index=False)
        st.download_button("💾 Pobierz Wynik", out.getvalue(), "Hunter_Results.xlsx")