"""
TDS Automation - web app (Streamlit)
=====================================
Upload a BOQ (.xlsx) + cover-page template (.docx) and point the app at your
SharePoint TDS folder. For every BOQ line item it finds the best-matching TDS
(by reading the TDS contents), puts the cover page in front, highlights the
BOQ Description / Model keywords in the TDS, and gives you ONE zip to download.

Run locally:   streamlit run app.py
Deploy free:   see ONLINE_SETUP.md
"""

import io
import shutil
import tempfile
import zipfile
from pathlib import Path

import pandas as pd
import streamlit as st

import tds_automation as tool
import tds_matcher as matcher
import sharepoint_source as sp

st.set_page_config(page_title="TDS Automation", page_icon="📄", layout="wide")


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def secret(name, default=""):
    try:
        return st.secrets.get(name, default)
    except Exception:
        return default


def process_request(boq_bytes, cover_bytes, provider, highlight, progress_cb=None):
    """Runs the pipeline in a temp dir; returns (zip_path, summary, out_dir)."""
    work = Path(tempfile.mkdtemp(prefix="tds_job_"))
    boq_path = work / "BOQ.xlsx"
    cover_path = work / "Cover_Template.docx"
    boq_path.write_bytes(boq_bytes)
    cover_path.write_bytes(cover_bytes)
    out_dir = work / "Output"
    summary = tool.run(str(boq_path), str(cover_path), None, str(out_dir),
                       tds_provider=provider, progress_cb=progress_cb, highlight=highlight)
    zip_path = shutil.make_archive(str(work / "TDS_Output"), "zip", root_dir=str(out_dir))
    return zip_path, summary, out_dir


def zip_to_dir(zip_bytes):
    tmp = Path(tempfile.mkdtemp(prefix="tds_db_"))
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as z:
        z.extractall(tmp)
    return tmp


def clear_repo_cache():
    for k in ("sp_prov", "library", "repo_key", "library_key"):
        st.session_state.pop(k, None)


# --------------------------------------------------------------------------
# UI
# --------------------------------------------------------------------------

st.title("📄 TDS Automation")
st.caption("BOQ + cover page in → for every line item: cover page + best-matching TDS, "
           "keywords highlighted, all zipped.")

left, right = st.columns(2)

# ---- 1. repository ---------------------------------------------------------
with left:
    st.header("1 · TDS repository")
    source = st.radio("Source", ["SharePoint folder", "Upload a .zip of the repository"], horizontal=True)
    if source == "SharePoint folder":
        client_id = st.text_input("App (client) ID", value=secret("CLIENT_ID"))
        tenant_id = st.text_input("Tenant ID or domain", value=secret("TENANT_ID", "organizations"))
        folder_url = st.text_input("SharePoint TDS folder link", value=secret("TDS_FOLDER_URL"))
        if "token" not in st.session_state:
            if st.button("Sign in with Microsoft", disabled=not client_id):
                try:
                    msal_app, flow = sp.start_device_login(client_id, tenant_id or "organizations")
                    st.info(f"1) Open **{flow['verification_uri']}**  \n2) Enter code **{flow['user_code']}**  \n"
                            "3) Sign in with your work account, then come back here.")
                    with st.spinner("Waiting for you to finish signing in..."):
                        st.session_state["token"] = sp.finish_device_login(msal_app, flow)
                    st.rerun()
                except Exception as e:
                    st.error(str(e))
        else:
            st.success("Signed in to Microsoft ✔")
            if st.button("Sign out"):
                st.session_state.pop("token", None)
                clear_repo_cache()
                st.rerun()
    else:
        db_zip = st.file_uploader("Repository (.zip of PDFs, subfolders OK)", type=["zip"])

# ---- 2. inputs + options -----------------------------------------------------
with right:
    st.header("2 · Your files & options")
    boq_file = st.file_uploader("BOQ (.xlsx)", type=["xlsx"])
    cover_file = st.file_uploader("Cover page template (.docx) with {Column} placeholders", type=["docx"])
    mode = st.radio("How should TDS files be matched?",
                    ["AI best match (reads the TDS contents)", "Exact filename = Model no."],
                    help="AI mode ranks every TDS in the repository against each line's Description, "
                         "Make and Model and reports a confidence level. Exact mode is the original behaviour.")
    min_conf = "Medium"
    if mode.startswith("AI"):
        min_conf = st.select_slider("Use a match automatically when confidence is at least",
                                    options=["High", "Medium", "Low"], value="Medium",
                                    help="High = model number evidenced. Medium = also strong description match. "
                                         "Lower-confidence guesses are NOT used; they are listed for review.")
    highlight = st.checkbox("Highlight Description / Model keywords in the TDS", value=True)

# ---- 3. run --------------------------------------------------------------------
st.header("3 · Generate")
ready = bool(boq_file and cover_file)
if source == "SharePoint folder":
    ready = ready and "token" in st.session_state and bool(folder_url)
else:
    ready = ready and db_zip is not None

c1, c2 = st.columns([1, 4])
if c1.button("Re-read repository", help="Use after TDS files were added/changed in SharePoint."):
    clear_repo_cache()

if c2.button("Generate", type="primary", disabled=not ready):
    try:
        status, bar = st.empty(), st.progress(0.0)
        ai = mode.startswith("AI")

        # ---- build the TDS provider -------------------------------------------
        if source == "SharePoint folder":
            key = (folder_url, st.session_state["token"][:20])
            if st.session_state.get("repo_key") != key:
                status.write("Indexing the SharePoint folder...")
                st.session_state["sp_prov"] = sp.SharePointTDSProvider(
                    st.session_state["token"], folder_url, progress_cb=lambda m: status.write(m))
                st.session_state["repo_key"] = key
                st.session_state.pop("library", None)
            sp_prov = st.session_state["sp_prov"]
            status.write(f"Repository: {len(sp_prov.files)} PDFs, {sp_prov.total_mb:,.0f} MB.")
            if ai:
                if "library" not in st.session_state:
                    st.session_state["library"] = sp_prov.build_library(lambda m: status.write(m))
                provider = matcher.AIMatchProvider(st.session_state["library"], min_conf)
            else:
                provider = sp_prov
        else:
            dbkey = ("zip", db_zip.name, db_zip.size)
            if st.session_state.get("repo_key") != dbkey:
                status.write("Reading the uploaded repository...")
                root = zip_to_dir(db_zip.getvalue())
                st.session_state["library"] = matcher.TDSLibrary.from_dir(root, lambda m: status.write(m))
                st.session_state["local_prov"] = tool.LocalTDSProvider(root)
                st.session_state["repo_key"] = dbkey
            provider = (matcher.AIMatchProvider(st.session_state["library"], min_conf)
                        if ai else st.session_state["local_prov"])

        status.write(f"{provider.count} TDS PDFs available. Processing the BOQ...")

        def on_progress(done, total, msg):
            bar.progress(min(done / max(total, 1), 1.0))
            status.write(msg)

        zip_path, summary, out_dir = process_request(
            boq_file.getvalue(), cover_file.getvalue(), provider, highlight, on_progress)
        bar.progress(1.0)
        status.write("Done.")
        rep = Path(out_dir) / "match_report.csv"
        st.session_state["result"] = {
            "zip": zip_path, "summary": summary,
            "report": pd.read_csv(rep).fillna("") if rep.exists() else None,
            "missing": (Path(out_dir) / "missing_tds_log.csv").read_text(encoding="utf-8"),
        }
    except Exception as e:
        st.error(f"Something went wrong: {e}")

# ---- result ----------------------------------------------------------------------
res = st.session_state.get("result")
if res:
    s = res["summary"]
    st.success(f"{s['items']} line items processed · {s['items'] - s['missing']} got a TDS · "
               f"{s['missing']} cover-page only (no confident match)")
    with open(res["zip"], "rb") as f:
        st.download_button("⬇️ Download all TDS (.zip)", f, file_name="TDS_Output.zip",
                           mime="application/zip", type="primary")
    if res["report"] is not None:
        st.subheader("Match review")
        st.caption("Check the Medium rows and the 'Used = No' rows. 'Other Candidates' shows the runners-up. "
                   "The same table is inside the zip as match_report.csv.")
        cols = ["S.No", "Make", "Model no.", "Matched TDS", "Confidence", "Used", "Why", "Other Candidates"]
        st.dataframe(res["report"][[c for c in cols if c in res["report"].columns]],
                     use_container_width=True, hide_index=True)
    elif s["missing"]:
        with st.expander("Items with no TDS"):
            st.text(res["missing"])
