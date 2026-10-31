"""Admin portal: dashboard, tests & questions, clients, results/proctoring review, settings."""
import io
import time

import pandas as pd
import streamlit as st

import db
from common import STATUS_LABEL, fmt_ts, integrity, metric, pct, profile_page, result_badge
from focus_engine import FocusModel


DIFFICULTIES = ["Beginner", "Intermediate", "Advanced"]


def head(eyebrow, title):
    st.markdown(f'<div class="eyebrow">{eyebrow}</div><h1>{title}</h1>', unsafe_allow_html=True)


# ------------------------------------------------------------------ dashboard
def dashboard(user):
    head("Admin portal", "Overview")
    s = db.get_settings()
    k = db.one("""SELECT (SELECT COUNT(*) FROM users WHERE role='client') clients,
                         (SELECT COUNT(*) FROM tests) tests,
                         COUNT(*) attempts,
                         COALESCE(AVG(CASE WHEN total>0 THEN score*100.0/total END),0) avg_score,
                         COALESCE(AVG(CASE WHEN status!='in_progress' THEN avg_focus END),0) avg_focus,
                         COALESCE(SUM(status='in_progress'),0) live,
                         SUM(status!='in_progress' AND (avg_focus<? OR status='terminated')) flagged
                  FROM attempts""", (float(s["flag_integrity_below"]),))
    c = st.columns(7)
    for col, (label, val) in zip(c, [("Live now", k["live"]), ("Clients", k["clients"]), ("Tests", k["tests"]), ("Attempts", k["attempts"]),
                                     ("Avg score", f"{k['avg_score']:.0f}%"), ("Avg focus", f"{k['avg_focus']:.0f}%"),
                                     ("Flagged", int(k["flagged"] or 0))]):
        metric(label, val, col)
    st.write("")
    left, right = st.columns(2, gap="large")
    with left:
        st.subheader("Attempts per test")
        d = db.df("SELECT t.title, COUNT(a.id) attempts FROM tests t LEFT JOIN attempts a ON a.test_id=t.id GROUP BY t.id")
        if not d.empty:
            st.bar_chart(d.set_index("title"), color="#54d88b")
    with right:
        st.subheader("Score distribution")
        d = db.df("SELECT score*100.0/total p FROM attempts WHERE total>0 AND status!='in_progress'")
        if d.empty:
            st.caption("No completed attempts yet.")
        else:
            bins = pd.cut(d["p"], bins=[-1, 20, 40, 60, 80, 100], labels=["0-20", "21-40", "41-60", "61-80", "81-100"])
            cnt = bins.value_counts().sort_index()
            cnt.index = cnt.index.astype(str)
            st.bar_chart(cnt, color="#193b35")
    st.subheader("Latest activity")
    d = db.df("""SELECT u.full_name client, t.title test, a.status, a.score, a.total, a.avg_focus, a.violations, a.started_at
                 FROM attempts a JOIN users u ON u.id=a.user_id JOIN tests t ON t.id=a.test_id
                 ORDER BY a.started_at DESC LIMIT 8""")
    if d.empty:
        st.info("No attempts yet. Sign in as a client to take the demo test.")
    else:
        d["started"] = d.pop("started_at").map(fmt_ts)
        d["result"] = [f"{a:g}/{b:g}" for a, b in zip(d.pop("score"), d.pop("total"))]
        d["avg_focus"] = d["avg_focus"].round(0)
        st.dataframe(d, use_container_width=True, hide_index=True)


# ------------------------------------------------------------------ tests
def tests_page(user):
    head("Admin portal", "Tests & questions")
    tests = db.rows("SELECT * FROM tests ORDER BY id DESC")
    tab_new, tab_edit = st.tabs(["➕ Create test", "✏️ Manage test & questions"])

    with tab_new, st.form("new_test", clear_on_submit=True):
        title = st.text_input("Title")
        desc = st.text_area("Description / instructions", height=80)
        c = st.columns(4)
        cat = c[0].text_input("Category", "General")
        diff = c[1].selectbox("Difficulty", DIFFICULTIES)
        dur = c[2].number_input("Duration (min)", 1, 600, 30)
        pass_pct = c[3].number_input("Pass mark (%)", 0, 100, 50)
        c = st.columns(4)
        maxv = c[0].number_input("Strikes allowed", 1, 20, 3, help="Test auto-ends when this many violations occur.")
        neg = c[1].number_input("Negative marking (%)", 0, 100, 0, help="Share of a question's marks lost per wrong answer.")
        att = c[2].number_input("Attempts allowed", 1, 10, 1)
        proct = c[3].toggle("Camera proctoring", True)
        if st.form_submit_button("Create test", type="primary"):
            if not title.strip():
                st.error("Title is required.")
            else:
                db.run("INSERT INTO tests(title,description,duration_min,pass_pct,max_violations,proctored,created_at,"
                       "category,difficulty,neg_mark,attempts_allowed) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                       (title.strip(), desc, dur, pass_pct, maxv, int(proct), time.time(), cat.strip() or "General",
                        diff, neg / 100, att))
                st.success("Test created (unpublished). Add questions in the next tab, then publish.")
                st.rerun()

    with tab_edit:
        if not tests:
            return st.info("Create your first test.")
        tid = st.selectbox("Test", [t["id"] for t in tests], format_func=lambda i: next(t["title"] for t in tests if t["id"] == i))
        t = next(t for t in tests if t["id"] == tid)
        with st.form(f"edit_{tid}"):
            c = st.columns([2, 3, 1.2, 1.2])
            title = c[0].text_input("Title", t["title"])
            desc = c[1].text_input("Description", t["description"])
            cat = c[2].text_input("Category", t["category"])
            diff = c[3].selectbox("Difficulty", DIFFICULTIES, index=DIFFICULTIES.index(t["difficulty"]) if t["difficulty"] in DIFFICULTIES else 0)
            c = st.columns(4)
            dur = c[0].number_input("Duration (min)", 1, 600, int(t["duration_min"]))
            pp = c[1].number_input("Pass mark (%)", 0, 100, int(t["pass_pct"]))
            mv = c[2].number_input("Strikes allowed", 1, 20, int(t["max_violations"]))
            neg = c[3].number_input("Negative marking (%)", 0, 100, int(round(t["neg_mark"] * 100)))
            c = st.columns(4)
            att = c[0].number_input("Attempts allowed", 1, 10, int(t["attempts_allowed"]))
            proct = c[1].toggle("Proctored", bool(t["proctored"]))
            shuf = c[2].toggle("Shuffle", bool(t["shuffle"]))
            pub = c[3].toggle("Published", bool(t["published"]))
            if st.form_submit_button("Save settings", type="primary"):
                db.run("UPDATE tests SET title=?,description=?,category=?,difficulty=?,duration_min=?,pass_pct=?,max_violations=?,"
                       "neg_mark=?,attempts_allowed=?,proctored=?,shuffle=?,published=? WHERE id=?",
                       (title, desc, cat, diff, dur, pp, mv, neg / 100, att, int(proct), int(shuf), int(pub), tid))
                st.success("Saved.")
                st.rerun()

        st.subheader("Questions")
        q = db.df("SELECT id,text,a,b,c,d,answer,marks FROM questions WHERE test_id=? ORDER BY id", (tid,))
        if q.empty:
            q = pd.DataFrame({"id": pd.Series(dtype="float"), "text": pd.Series(dtype="str"), "a": pd.Series(dtype="str"),
                              "b": pd.Series(dtype="str"), "c": pd.Series(dtype="str"), "d": pd.Series(dtype="str"),
                              "answer": pd.Series(dtype="str"), "marks": pd.Series(dtype="float")})
        edited = st.data_editor(
            q, num_rows="dynamic", use_container_width=True, hide_index=True, key=f"qe_{tid}",
            column_config={"id": None, "text": st.column_config.TextColumn("Question", width="large", required=True),
                           "a": "Option A", "b": "Option B", "c": "Option C", "d": "Option D",
                           "answer": st.column_config.SelectboxColumn("Correct", options=list("ABCD"), required=True),
                           "marks": st.column_config.NumberColumn("Marks", min_value=0, default=1)})
        b1, b2, b3 = st.columns([1, 1, 2])
        if b1.button("💾 Save questions", type="primary", use_container_width=True):
            _save_questions(tid, edited)
        up = b2.file_uploader("Import CSV", type="csv", label_visibility="collapsed")
        b3.download_button("Download CSV template", "text,a,b,c,d,answer,marks\n2+2?,3,4,5,6,B,1\n",
                           "questions_template.csv", "text/csv")
        if up and st.button("Append CSV questions"):
            try:
                d = pd.read_csv(up).fillna("")
                miss = {"text", "a", "b", "c", "d", "answer"} - set(d.columns)
                if miss:
                    raise ValueError(f"Missing columns: {', '.join(sorted(miss))}")
                if "marks" not in d:
                    d["marks"] = 1
                _save_questions(tid, d, append=True)
            except Exception as e:
                st.error(str(e))

        with st.expander("⚠️ Danger zone"):
            if st.button("Delete this test and all its attempts"):
                db.run("DELETE FROM tests WHERE id=?", (tid,))
                st.rerun()


def _save_questions(tid, df, append=False):
    df = df.copy()
    df["answer"] = df["answer"].astype(str).str.upper().str.strip()
    bad = df[(df["text"].astype(str).str.strip() == "") | ~df["answer"].isin(list("ABCD"))]
    if len(bad):
        return st.error("Every question needs text and a correct answer (A–D).")
    keep = [] if append else [int(i) for i in df.get("id", pd.Series(dtype=float)).dropna()]
    if not append:
        if keep:
            db.run(f"DELETE FROM questions WHERE test_id=? AND id NOT IN ({','.join('?' * len(keep))})", (tid, *keep))
        else:
            db.run("DELETE FROM questions WHERE test_id=?", (tid,))
    for r in df.itertuples():
        vals = (r.text, r.a, r.b, r.c, r.d, r.answer, float(getattr(r, "marks", 1) or 0))
        rid = None if append else getattr(r, "id", None)
        if rid is not None and not pd.isna(rid):
            db.run("UPDATE questions SET text=?,a=?,b=?,c=?,d=?,answer=?,marks=? WHERE id=?", (*vals, int(rid)))
        else:
            db.run("INSERT INTO questions(test_id,text,a,b,c,d,answer,marks) VALUES(?,?,?,?,?,?,?,?)", (tid, *vals))
    st.success("Questions saved.")
    st.rerun()


# ------------------------------------------------------------------ clients
def clients_page(user):
    head("Admin portal", "Clients")
    d = db.df("""SELECT u.id, u.username, u.full_name, u.active, u.created_at,
                        COUNT(a.id) attempts, ROUND(COALESCE(AVG(a.avg_focus),0)) avg_focus
                 FROM users u LEFT JOIN attempts a ON a.user_id=u.id AND a.status!='in_progress'
                 WHERE u.role='client' GROUP BY u.id ORDER BY u.id DESC""")
    if not d.empty:
        show = d.assign(created=d["created_at"].map(fmt_ts), active=d["active"].astype(bool)).drop(columns="created_at")
        st.dataframe(show, use_container_width=True, hide_index=True, column_config={"id": None})
    c1, c2 = st.columns(2, gap="large")
    with c1, st.form("new_client", clear_on_submit=True):
        st.subheader("Add client")
        name, uname, pw = st.text_input("Full name"), st.text_input("Username"), st.text_input("Password", type="password")
        if st.form_submit_button("Create", type="primary"):
            try:
                db.create_user(uname, name, pw, "client")
                st.rerun()
            except ValueError as e:
                st.error(str(e))
    with c2, st.form("manage_client"):
        st.subheader("Manage client")
        if d.empty:
            st.caption("No clients yet.")
        else:
            uid = st.selectbox("Client", d["id"].tolist(), format_func=lambda i: d.set_index("id").loc[i, "username"])
            pw = st.text_input("New password (optional)", type="password")
            a, b, c = st.columns(3)
            if a.form_submit_button("Reset password"):
                try:
                    db.set_password(uid, pw)
                    st.success("Password reset.")
                except ValueError as e:
                    st.error(str(e))
            if b.form_submit_button("Enable/Disable"):
                db.run("UPDATE users SET active=1-active WHERE id=?", (uid,))
                st.rerun()
            if c.form_submit_button("Delete"):
                db.run("DELETE FROM users WHERE id=?", (uid,))
                st.rerun()


# ------------------------------------------------------------------ results
def results_page(user):
    head("Admin portal", "Results & proctoring")
    s = db.get_settings()
    thr = float(s["flag_integrity_below"])
    d = db.df("""SELECT a.id, u.full_name client, t.title test, t.pass_pct, a.status, a.score, a.total,
                        a.avg_focus, a.violations, a.started_at
                 FROM attempts a JOIN users u ON u.id=a.user_id JOIN tests t ON t.id=a.test_id
                 ORDER BY a.started_at DESC""")
    if d.empty:
        return st.info("No attempts yet.")
    d["pct"] = [pct(a, b) for a, b in zip(d["score"], d["total"])]
    d["flagged"] = (d["status"] != "in_progress") & ((d["avg_focus"] < thr) | (d["status"] == "terminated"))
    f = st.columns([2, 2, 1])
    tsel = f[0].multiselect("Test", sorted(d["test"].unique()))
    ssel = f[1].multiselect("Status", sorted(d["status"].unique()), format_func=lambda x: STATUS_LABEL.get(x, x))
    only_flag = f[2].toggle("Flagged only")
    if tsel:
        d = d[d["test"].isin(tsel)]
    if ssel:
        d = d[d["status"].isin(ssel)]
    if only_flag:
        d = d[d["flagged"]]
    view = d.assign(started=d["started_at"].map(fmt_ts), status=d["status"].map(lambda x: STATUS_LABEL.get(x, x)),
                    score_pct=d["pct"].round(1), avg_focus=d["avg_focus"].round(1),
                    integrity=[integrity(f, v) for f, v in zip(d["avg_focus"], d["violations"])],
                    passed=(d["pct"] >= d["pass_pct"]))[
        ["id", "client", "test", "started", "status", "score_pct", "passed", "avg_focus", "integrity", "violations", "flagged"]]
    st.dataframe(view, use_container_width=True, hide_index=True,
                 column_config={"avg_focus": st.column_config.ProgressColumn("Avg focus", min_value=0, max_value=100, format="%.0f"),
                                "score_pct": st.column_config.NumberColumn("Score %"),
                                "integrity": st.column_config.ProgressColumn("Integrity", min_value=0, max_value=100, format="%.0f")})
    st.download_button("⬇ Export CSV", view.to_csv(index=False), "results.csv", "text/csv")
    if view.empty:
        return

    st.divider()
    aid = st.selectbox("Inspect attempt", view["id"].tolist(),
                       format_func=lambda i: f"#{i} · {view.set_index('id').loc[i, 'client']} · {view.set_index('id').loc[i, 'test']}")
    row = d.set_index("id").loc[aid]
    passed = row["pct"] >= row["pass_pct"]
    c = st.columns(5)
    metric("Score", f"{row['score']:g}/{row['total']:g}", c[0])
    metric("Percent", f"{row['pct']:.0f}%", c[1])
    metric("Avg focus", f"{row['avg_focus']:.0f}%", c[2])
    metric("Strikes", f"{int(row['violations'])} · integrity {integrity(row['avg_focus'], row['violations'])}", c[3])
    flag_html = '<span class="badge bad">FLAGGED</span>' if row["flagged"] else ""
    c[4].markdown(f"<div style='padding-top:1.2rem'>{result_badge(passed)} {flag_html}</div>", unsafe_allow_html=True)

    t1, t2, t3 = st.tabs(["📈 Focus timeline", "🚨 Violations", "📝 Answers"])
    with t1:
        fl = db.df("SELECT ts, score, face, eyes, posture, faces FROM focus_logs WHERE attempt_id=? ORDER BY ts", (aid,))
        if fl.empty:
            st.caption("No camera data (test not proctored or camera was off).")
        else:
            fl["sec"] = (fl["ts"] - fl["ts"].iloc[0]).round(0)
            st.line_chart(fl.set_index("sec")[["score"]], color="#54d88b")
            st.line_chart(fl.set_index("sec")[["face", "eyes", "posture"]])
    with t2:
        v = db.df("SELECT ts, kind, detail FROM violations WHERE attempt_id=? ORDER BY ts", (aid,))
        if v.empty:
            st.success("No violations recorded.")
        else:
            v["time"] = v.pop("ts").map(lambda x: time.strftime("%H:%M:%S", time.localtime(x)))
            st.dataframe(v[["time", "kind", "detail"]], use_container_width=True, hide_index=True)
    with t3:
        a = db.df("""SELECT q.text question, a.choice given, q.answer correct_answer, a.correct
                     FROM answers a JOIN questions q ON q.id=a.question_id WHERE a.attempt_id=?""", (aid,))
        if a.empty:
            st.caption("No answers saved.")
        else:
            st.dataframe(a, use_container_width=True, hide_index=True)
    if st.button("🗑 Delete attempt (lets the client retake)"):
        db.run("DELETE FROM attempts WHERE id=?", (aid,))
        st.rerun()


# ------------------------------------------------------------------ live monitor
@st.fragment(run_every=3)
def _live_table():
    d = db.df("""SELECT a.id, u.full_name client, t.title test, t.max_violations mx, a.started_at,
                        t.duration_min dur,
                        (SELECT COUNT(*) FROM violations v WHERE v.attempt_id=a.id) strikes,
                        (SELECT score FROM focus_logs f WHERE f.attempt_id=a.id ORDER BY ts DESC LIMIT 1) focus,
                        (SELECT faces FROM focus_logs f WHERE f.attempt_id=a.id ORDER BY ts DESC LIMIT 1) faces,
                        (SELECT MAX(ts) FROM focus_logs f WHERE f.attempt_id=a.id) last_seen,
                        (SELECT COUNT(*) FROM answers an WHERE an.attempt_id=a.id) answered
                 FROM attempts a JOIN users u ON u.id=a.user_id JOIN tests t ON t.id=a.test_id
                 WHERE a.status='in_progress' ORDER BY a.started_at""")
    st.caption(f"Auto-refreshes every 3 seconds · {len(d)} live session(s)")
    if d.empty:
        return st.info("No one is taking a test right now.")
    now = time.time()
    d["time_left"] = [f"{max(0, int(s + m * 60 - now)) // 60:02d}:{max(0, int(s + m * 60 - now)) % 60:02d}" for s, m in zip(d["started_at"], d["dur"])]
    d["camera"] = [("🟢 live" if ls and now - ls < 6 else "🔴 off") for ls in d["last_seen"]]
    d["strikes"] = [f"{s}/{m}" for s, m in zip(d["strikes"], d["mx"])]
    st.dataframe(d[["id", "client", "test", "time_left", "camera", "focus", "strikes", "answered"]], use_container_width=True,
                 hide_index=True, column_config={"focus": st.column_config.ProgressColumn("Focus", min_value=0, max_value=100, format="%.0f")})
    c = st.columns([2, 1])
    aid = c[0].selectbox("Session", d["id"].tolist(), key="live_sel",
                         format_func=lambda i: f"#{i} · {d.set_index('id').loc[i, 'client']} · {d.set_index('id').loc[i, 'test']}")
    if c[1].button("⛔ End this test now", use_container_width=True):
        db.grade_attempt(aid, "terminated")
        st.toast("Session ended and graded.")
        st.rerun()


def live_page(user):
    head("Admin portal", "Live monitor")
    _live_table()


# ------------------------------------------------------------------ analytics
def analytics_page(user):
    head("Admin portal", "Analytics")
    tests = db.rows("SELECT id, title FROM tests ORDER BY id")
    if not tests:
        return st.info("No tests yet.")
    tid = st.selectbox("Test", [t["id"] for t in tests], format_func=lambda i: next(t["title"] for t in tests if t["id"] == i))
    k = db.one("""SELECT COUNT(*) n, COALESCE(AVG(score*100.0/NULLIF(total,0)),0) avg_pct, COALESCE(AVG(avg_focus),0) focus,
                         COALESCE(SUM(status='terminated'),0) term, COALESCE(SUM(violations),0) strikes
                  FROM attempts WHERE test_id=? AND status!='in_progress'""", (tid,))
    c = st.columns(5)
    for col, (l, v) in zip(c, [("Attempts", k["n"]), ("Avg score", f"{k['avg_pct']:.0f}%"), ("Avg focus", f"{k['focus']:.0f}%"),
                               ("Terminated", int(k["term"])), ("Total strikes", int(k["strikes"]))]):
        metric(l, v, col)
    st.write("")
    t1, t2, t3 = st.tabs(["🏆 Leaderboard", "❓ Question difficulty", "🚨 Violation types"])
    with t1:
        lb = db.leaderboard(tid, 10)
        if lb.empty:
            st.caption("No completed attempts yet.")
        else:
            lb.insert(0, "rank", range(1, len(lb) + 1))
            st.dataframe(lb, use_container_width=True, hide_index=True)
    with t2:
        q = db.df("""SELECT q.text question, COUNT(a.attempt_id) answered_by,
                            ROUND(100.0*SUM(a.correct)/NULLIF(COUNT(a.attempt_id),0),0) pct_correct
                     FROM questions q LEFT JOIN answers a ON a.question_id=q.id
                          AND a.attempt_id IN (SELECT id FROM attempts WHERE status!='in_progress')
                     WHERE q.test_id=? GROUP BY q.id ORDER BY pct_correct""", (tid,))
        st.caption("Lowest % correct first — these are your hardest (or most ambiguous) questions.")
        st.dataframe(q, use_container_width=True, hide_index=True,
                     column_config={"pct_correct": st.column_config.ProgressColumn("% correct", min_value=0, max_value=100, format="%.0f")})
    with t3:
        v = db.df("""SELECT kind, COUNT(*) n FROM violations WHERE attempt_id IN (SELECT id FROM attempts WHERE test_id=?)
                     GROUP BY kind ORDER BY n DESC""", (tid,))
        if v.empty:
            st.success("No violations recorded for this test.")
        else:
            st.bar_chart(v.set_index("kind"), color="#b3261e")


# ------------------------------------------------------------------ settings
def settings_page(user):
    head("Admin portal", "Settings & model")
    s = db.get_settings()
    with st.form("settings"):
        st.subheader("Proctoring rules")
        c = st.columns(3)
        sens = c[0].slider("Detection sensitivity", 0.1, 1.0, float(s["sensitivity"]), 0.05)
        low = c[1].slider("Low-focus threshold (%)", 5, 80, int(float(s["low_focus_threshold"])))
        lows = c[2].number_input("…for how many seconds", 2, 120, int(float(s["low_focus_seconds"])))
        c = st.columns(3)
        nof = c[0].number_input("No-face seconds", 2, 60, int(float(s["no_face_seconds"])))
        cool = c[1].number_input("Violation cooldown (s)", 3, 120, int(float(s["violation_cooldown"])))
        flag = c[2].slider("Flag attempts with avg focus below (%)", 10, 90, int(float(s["flag_integrity_below"])))
        c = st.columns(2)
        signup = c[0].toggle("Allow client self-registration", s["allow_signup"] == "1")
        show = c[1].toggle("Clients can review their answers", s["show_answers"] == "1")
        if st.form_submit_button("Save settings", type="primary"):
            for k, v in {"sensitivity": sens, "low_focus_threshold": low, "low_focus_seconds": lows, "no_face_seconds": nof,
                         "violation_cooldown": cool, "flag_integrity_below": flag, "allow_signup": int(signup),
                         "show_answers": int(show)}.items():
                db.set_setting(k, v)
            st.success("Saved. Applies to attempts started from now on.")

    with st.expander("➕ Create another admin account"):
        with st.form("new_admin", clear_on_submit=True):
            n, u, p = st.text_input("Full name"), st.text_input("Username"), st.text_input("Password (min 6)", type="password")
            if st.form_submit_button("Create admin"):
                try:
                    db.create_user(u, n, p, "admin")
                    st.success("Admin created — they sign in via the Admin portal.")
                except ValueError as e:
                    st.error(str(e))

    st.subheader("Local ML calibration")
    model = FocusModel.load()
    st.caption("Optional: upload one *focused* and one *distracted* photo to train a local logistic-regression model "
               "(same approach as FocusLab). Without it, the rule-based score is used. Nothing leaves this machine.")
    st.markdown(f'<span class="badge {"" if model.trained else "warn"}">{"Trained model active" if model.trained else "Rule-based scoring"}</span>',
                unsafe_allow_html=True)
    c = st.columns(2)
    f, dct = c[0].file_uploader("Focused example", type=["jpg", "jpeg", "png"]), c[1].file_uploader("Distracted example", type=["jpg", "jpeg", "png"])
    b = st.columns(2)
    if b[0].button("Train model", disabled=not (f and dct)):
        try:
            FocusModel.train(f.getvalue(), dct.getvalue())
            st.success("Model trained and activated.")
        except ValueError as e:
            st.error(str(e))
    if b[1].button("Reset to rule-based", disabled=not model.trained):
        FocusModel.delete()
        st.rerun()


PAGES = {"Dashboard": dashboard, "Live Monitor": live_page, "Tests": tests_page, "Clients": clients_page,
         "Results & Proctoring": results_page, "Analytics": analytics_page, "Settings": settings_page,
         "Profile": profile_page}
