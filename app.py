"""ProctorPulse — proctored test portal """
import streamlit as st

import db

st.set_page_config(page_title="ProctorPulse", page_icon="◉", layout="wide", initial_sidebar_state="expanded")

CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;600;700&family=Space+Grotesk:wght@500;600;700&display=swap');
:root{--ink:#17211d;--muted:#758078;--line:#dfe6df;--navy:#193b35;--mint:#54d88b}
html,body,[class*="css"]{font-family:'DM Sans',sans-serif;color:var(--ink)}
.stApp{background:linear-gradient(140deg,#f6faf5 0%,#eff7f0 55%,#eaf4ee 100%)}
.block-container{max-width:1440px;padding:2rem 2.5rem 3rem}
h1,h2,h3{font-family:'Space Grotesk',sans-serif!important;letter-spacing:-.03em;color:var(--navy)}
.eyebrow{color:#3b9c68;font-size:.74rem;font-weight:700;letter-spacing:.14em;text-transform:uppercase}
.badge{display:inline-block;background:#e1f7e9;border:1px solid #c8ecd5;color:#26804f;border-radius:30px;padding:.3rem .7rem;font-size:.75rem;font-weight:700}
.badge.bad{background:#fde8e8;border-color:#f5c6c6;color:#b3261e}
.badge.warn{background:#fff3e0;border-color:#f5d9a8;color:#a15c00}
.card{background:rgba(255,255,255,.82);border:1px solid rgba(212,224,214,.95);border-radius:20px;padding:1.2rem;box-shadow:0 10px 28px rgba(29,67,46,.05);margin-bottom:1rem}
.q-card{background:#fff;border:1px solid var(--line);border-radius:16px;padding:1rem 1.2rem .4rem;margin-bottom:.8rem}
.q-title{font:600 1.02rem 'Space Grotesk';color:var(--navy)}
.metric{background:#fff;border:1px solid var(--line);border-radius:16px;padding:.9rem 1rem}
.metric-label{color:var(--muted);font-size:.72rem;font-weight:600;text-transform:uppercase;letter-spacing:.08em}
.metric-value{color:var(--navy);font:700 1.8rem 'Space Grotesk';margin-top:.15rem}
.timer{font:700 1.9rem 'Space Grotesk';color:var(--navy)}
.timer.low{color:#b3261e}
div[data-testid="stSidebar"]{background:#f1f7f1;border-right:1px solid #dce8dd}
div[data-testid="stMetric"]{background:#fff;border:1px solid var(--line);border-radius:15px;padding:.8rem 1rem}
.stButton>button,.stDownloadButton>button,div[data-testid="stFormSubmitButton"]>button{border-radius:11px;font-weight:700}
.stButton>button[kind="primary"],div[data-testid="stFormSubmitButton"]>button[kind="primary"]{background:var(--navy);border-color:var(--navy);color:#fff}
footer{visibility:hidden}
</style>
"""
st.markdown(CSS, unsafe_allow_html=True)


def landing():
    st.markdown('<div style="text-align:center;margin-top:1.5rem"><div class="eyebrow">AI-proctored assessments</div>'
                '<h1 style="font-size:3rem;margin:.2rem 0">◉ ProctorPulse</h1>'
                '<p style="color:#758078">Choose your portal. Each portal has its own sign-in.</p></div>', unsafe_allow_html=True)
    _, a, b, _ = st.columns([0.6, 1, 1, 0.6], gap="large")
    cards = [(a, "client", "🎓 Client portal", "Take proctored tests, see your scores, focus insights and ranking."),
             (b, "admin", "🛡️ Admin portal", "Manage tests and clients, watch live sessions, review violations and analytics.")]
    for col, key, title, text in cards:
        with col:
            st.markdown(f'<div class="card" style="min-height:150px"><h3 style="margin-top:0">{title}</h3>'
                        f'<p style="color:#758078">{text}</p></div>', unsafe_allow_html=True)
            if st.button(f"Enter {key} portal", key=f"go_{key}", type="primary", use_container_width=True):
                st.query_params["portal"] = key
                st.rerun()
    st.caption("Every proctoring violation counts as a strike. Administrators define the number of permitted strikes, and the test ends automatically once that limit is reached.")


def login_screen(portal):
    is_admin = portal == "admin"
    _, mid, _ = st.columns([1, 1.2, 1])
    with mid:
        st.markdown(f'<div class="eyebrow">{"Administrator access" if is_admin else "Candidate access"}</div>'
                    f'<h1 style="margin:.2rem 0 .3rem">{"🛡️ Admin sign in" if is_admin else "🎓 Client sign in"}</h1>',
                    unsafe_allow_html=True)
        if st.button("← Switch portal"):
            st.query_params.clear()
            st.rerun()
        settings = db.get_settings()
        can_signup = (not is_admin) and settings["allow_signup"] == "1"
        tabs = st.tabs(["Sign in", "Create account"] if can_signup else ["Sign in"])
        with tabs[0], st.form(f"login_{portal}"):
            u = st.text_input("Username")
            p = st.text_input("Password", type="password")
            if st.form_submit_button("Sign in", type="primary", use_container_width=True):
                wait = db.lock_remaining(u) if u else 0
                if wait:
                    st.error(f"Too many failed attempts. Try again in {wait}s.")
                else:
                    user = db.authenticate(u, p, portal) if u and p else None
                    if user:
                        st.session_state.user = user
                        st.rerun()
                    st.error(f"Invalid credentials for the {portal} portal.")
        if can_signup:
            with tabs[1], st.form("signup"):
                name = st.text_input("Full name")
                u = st.text_input("Choose a username")
                p = st.text_input("Choose a password (min 6)", type="password")
                if st.form_submit_button("Create account", use_container_width=True):
                    try:
                        db.create_user(u, name, p, "client")
                        st.session_state.user = db.authenticate(u, p, "client")
                        st.rerun()
                    except ValueError as e:
                        st.error(str(e))
        st.caption("Demo: admin / admin123" if is_admin else "Demo: client / client123")


def main():
    db.conn()  # init + migrate + seed once
    user = st.session_state.get("user")
    if not user:
        portal = st.query_params.get("portal")
        return login_screen(portal) if portal in ("admin", "client") else landing()

    from views_admin import PAGES as ADMIN_PAGES
    from views_client import PAGES as CLIENT_PAGES

    is_admin = user["role"] == "admin"
    if st.query_params.get("portal") != user["role"]:
        st.query_params["portal"] = user["role"]
    pages = ADMIN_PAGES if is_admin else CLIENT_PAGES
    locked = (not is_admin) and st.session_state.get("active_attempt")

    with st.sidebar:
        st.markdown("# ◉ ProctorPulse")
        st.markdown(f'<span class="badge">{"Admin portal" if is_admin else "Client portal"}</span>', unsafe_allow_html=True)
        st.caption(f"Signed in as **{user['full_name']}**")
        st.divider()
        if locked:
            st.warning("Test in progress — navigation is locked until you submit.")
            page = "Take Test"
        else:
            page = st.radio("Navigate", list(pages), label_visibility="collapsed")
        st.divider()
        if not locked and st.button("Sign out", use_container_width=True):
            for k in list(st.session_state):
                del st.session_state[k]
            st.rerun()

    pages[page](user)


main()
