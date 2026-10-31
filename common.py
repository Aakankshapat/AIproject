import time

import streamlit as st

import db

STATUS_LABEL = {"completed": "Completed", "in_progress": "In progress", "timed_out": "Timed out",
                "terminated": "Terminated (violations)"}


def metric(label, value, col=None):
    html = f'<div class="metric"><div class="metric-label">{label}</div><div class="metric-value">{value}</div></div>'
    (col or st).markdown(html, unsafe_allow_html=True)


def fmt_ts(ts):
    return time.strftime("%d %b %Y %H:%M", time.localtime(ts)) if ts else "—"


def pct(score, total):
    return (score / total * 100) if total else 0.0


def integrity(avg_focus, violations):
    """0-100 trust score: average focus minus 10 points per strike."""
    return max(0, round((avg_focus or 0) - 10 * (violations or 0)))


def short_name(full):
    parts = full.split()
    return parts[0] + (f" {parts[-1][0]}." if len(parts) > 1 else "")


def result_badge(passed):
    return ('<span class="badge">PASSED</span>' if passed else '<span class="badge bad">FAILED</span>')


def profile_page(user):
    st.markdown('<div class="eyebrow">Account</div><h1>Profile</h1>', unsafe_allow_html=True)
    st.write(f"**Name:** {user['full_name']}  \n**Username:** {user['username']}  \n**Role:** {user['role'].title()}")
    with st.form("pw"):
        st.subheader("Change password")
        old = st.text_input("Current password", type="password")
        new = st.text_input("New password (min 6)", type="password")
        if st.form_submit_button("Update password", type="primary"):
            if not db.check_pw(old, db.one("SELECT pw_hash FROM users WHERE id=?", (user["id"],))["pw_hash"]):
                st.error("Current password is incorrect.")
            else:
                try:
                    db.set_password(user["id"], new)
                    st.success("Password updated.")
                except ValueError as e:
                    st.error(str(e))
