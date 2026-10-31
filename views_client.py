"""Client portal: dashboard, proctored test taking (3-strike rule), results & ranking."""
import html
import random
import time

import streamlit as st
from streamlit_webrtc import WebRtcMode, webrtc_streamer

import db
from common import STATUS_LABEL, fmt_ts, integrity, metric, pct, profile_page, result_badge, short_name
from focus_engine import FocusModel
from proctor import ProctorProcessor, ViolationDetector, aggregate


def head(eyebrow, title):
    st.markdown(f'<div class="eyebrow">{eyebrow}</div><h1>{title}</h1>', unsafe_allow_html=True)


@st.cache_resource(ttl=30)
def load_model():
    m = FocusModel.load()
    return m if m.trained else None


def available_tests(uid):
    return db.rows("""SELECT t.*, (SELECT COUNT(*) FROM questions q WHERE q.test_id=t.id) nq,
                 (SELECT COUNT(*) FROM attempts a WHERE a.test_id=t.id AND a.user_id=? AND a.status!='in_progress') n_done,
                 (SELECT COUNT(*) FROM attempts a WHERE a.test_id=t.id AND a.user_id=? AND a.status='in_progress') running
                 FROM tests t WHERE t.published=1 ORDER BY t.id""", (uid, uid))


def expire_stale(uid):
    """Close attempts whose time ran out while the client was away."""
    for a in db.rows("""SELECT a.id, a.started_at, t.duration_min FROM attempts a JOIN tests t ON t.id=a.test_id
                        WHERE a.user_id=? AND a.status='in_progress'""", (uid,)):
        if a["id"] != st.session_state.get("active_attempt") and time.time() > a["started_at"] + a["duration_min"] * 60 + 5:
            db.grade_attempt(a["id"], "timed_out")


def test_caption(t):
    return (f"{t['category']} · {t['difficulty']} · {t['nq']} questions · {t['duration_min']} min · pass {t['pass_pct']:g}%"
            + (f" · −{t['neg_mark']:.0%} per wrong answer" if t["neg_mark"] else "")
            + f" · {t['max_violations']} strikes" if t["proctored"] else
            f"{t['category']} · {t['difficulty']} · {t['nq']} questions · {t['duration_min']} min · no camera")


# ------------------------------------------------------------------ dashboard
def dashboard(user):
    head("Client portal", f"Welcome, {user['full_name'].split()[0]}")
    expire_stale(user["id"])
    done = db.rows("SELECT * FROM attempts WHERE user_id=? AND status!='in_progress'", (user["id"],))
    c = st.columns(4)
    metric("Tests taken", len(done), c[0])
    metric("Avg score", f"{sum(pct(a['score'], a['total']) for a in done) / len(done):.0f}%" if done else "—", c[1])
    metric("Avg focus", f"{sum(a['avg_focus'] for a in done) / len(done):.0f}%" if done else "—", c[2])
    metric("Strikes received", sum(a["violations"] for a in done), c[3])
    st.subheader("Available tests")
    tests = available_tests(user["id"])
    if not tests:
        st.info("No tests are published yet.")
    cols = st.columns(2, gap="medium")
    for i, t in enumerate(tests):
        with cols[i % 2], st.container(border=True):
            left = t["attempts_allowed"] - t["n_done"]
            badge = ('<span class="badge warn">In progress</span>' if t["running"] else
                     f'<span class="badge">{left} attempt{"s" if left != 1 else ""} left</span>' if left > 0 else
                     '<span class="badge bad">No attempts left</span>')
            st.markdown(f"**{t['title']}** {badge}", unsafe_allow_html=True)
            st.caption(t["description"] or "")
            st.caption(test_caption(t))
    st.info("Test ends automatically when the admin-defined strike limit is reached.")


# ------------------------------------------------------------------ test taking
def take_test(user):
    expire_stale(user["id"])
    head("Client portal", "Take test")
    sett = db.get_settings()
    active = st.session_state.get("active_attempt")
    if active is None:  # resume an in-progress attempt after a browser refresh
        r = db.one("SELECT id FROM attempts WHERE user_id=? AND status='in_progress'", (user["id"],))
        if r:
            active = st.session_state.active_attempt = r["id"]
    att = db.one("SELECT a.*, t.title, t.duration_min, t.max_violations, t.proctored, t.shuffle, t.pass_pct "
                 "FROM attempts a JOIN tests t ON t.id=a.test_id WHERE a.id=?", (active,)) if active else None
    if att and att["status"] != "in_progress":  # ended remotely (admin) — show result
        for k in ("active_attempt", "det"):
            st.session_state.pop(k, None)
        st.session_state.last_result, att = att["id"], None

    if not att and st.session_state.get("last_result"):
        show_result(st.session_state.last_result, sett)
        if st.button("Done", type="primary"):
            st.session_state.pop("last_result")
            st.rerun()
        return

    left, right = st.columns([2, 1], gap="large")
    ctx = None
    with right:  # camera is rendered first and always in the same slot so it never restarts
        if (att and att["proctored"]) or not att:
            st.markdown('<div class="card"><b>Proctoring camera</b><br><span style="color:#758078;font-size:.85rem">'
                        'Frames are analysed in memory only — never saved or uploaded.</span></div>', unsafe_allow_html=True)
            model, sens = load_model(), float(sett["sensitivity"])
            ctx = webrtc_streamer(key="proctor", mode=WebRtcMode.SENDRECV,
                                  video_processor_factory=lambda: ProctorProcessor(model, sens),
                                  media_stream_constraints={"video": {"width": 640, "height": 480, "frameRate": 15}, "audio": False},
                                  async_processing=True)
    with left:
        if not att:
            lobby(user, ctx, sett)
        else:
            run_test(att, ctx, sett, right)


def lobby(user, ctx, sett):
    tests = [t for t in available_tests(user["id"]) if t["nq"] and not t["running"] and t["n_done"] < t["attempts_allowed"]]
    if not tests:
        return st.info("No tests available — you have used all attempts on every published test (see My Results).")
    t = st.selectbox("Choose a test", tests, key="lobby_test",
                     format_func=lambda t: f"{t['title']}  ·  {t['category']}  ·  {t['attempts_allowed'] - t['n_done']} attempt(s) left")
    st.markdown(t["description"] or "")
    st.caption(test_caption(t))
    if t["proctored"]:
        n = t["max_violations"]
        st.markdown(f"""<div class="card" style="border-color:#f5c6c6"><b>🚨 The {n}-strike rule</b><br>
Every violation is a strike. <b>At {n} strikes your test ends immediately</b> and is submitted with the answers saved so far.<br><br>
<b>What counts as a strike</b>
<ul style="margin:.3rem 0 0 1.1rem">
<li>No face visible for {float(sett['no_face_seconds']):g}+ seconds</li>
<li>More than one face in the frame</li>
<li>Focus score under {float(sett['low_focus_threshold']):g}% for {float(sett['low_focus_seconds']):g}+ seconds</li>
<li>Camera switched off during the test</li></ul></div>""", unsafe_allow_html=True)
    cam_ok = True
    if t["proctored"]:
        proc = ctx.video_processor if ctx and ctx.state.playing else None
        latest = proc.latest if proc else None
        cam_ok = bool(latest and latest.faces == 1)
        if not proc:
            st.warning("Click **START** on the camera panel to begin the camera check.")
        elif not cam_ok:
            st.warning("Camera on — make sure exactly one face is clearly visible. Checking…")
            time.sleep(1)
            st.rerun()
        else:
            st.success("Camera check passed.")
    agree = st.checkbox("I have read the rules and agree to be monitored by camera." if t["proctored"] else "I understand the rules.")
    if st.button("▶ Start test", type="primary", disabled=not (cam_ok and agree)):
        st.session_state.active_attempt = db.start_attempt(t["id"], user["id"])
        st.rerun()


def run_test(att, ctx, sett, right):
    aid = att["id"]
    deadline = att["started_at"] + att["duration_min"] * 60
    qs = db.rows("SELECT * FROM questions WHERE test_id=? ORDER BY id", (att["test_id"],))
    if att["shuffle"]:
        random.Random(aid).shuffle(qs)  # stable order across reruns / resume
    saved = db.saved_answers(aid)

    def finish(status):
        db.grade_attempt(aid, status)
        for k in ("active_attempt", "det", "cam_off_since", "confirm_submit"):
            st.session_state.pop(k, None)
        st.session_state.last_result = aid
        st.rerun()

    if time.time() >= deadline:
        finish("timed_out")
    with right:
        if att["proctored"]:
            if st.session_state.get("det") is None:
                st.session_state.det = ViolationDetector(sett)
            monitor(ctx, aid, deadline, int(att["max_violations"]), finish)
        else:
            timer_only(deadline, aid, finish)

    st.subheader(att["title"])
    for i, q in enumerate(qs, 1):
        opts = {k: q[k.lower()] for k in "ABCD" if q[k.lower()]}
        st.markdown(f'<div class="q-card"><div class="q-title">Q{i}. {html.escape(q["text"])}</div></div>', unsafe_allow_html=True)
        st.radio(f"q{q['id']}", list(opts), index=list(opts).index(saved[q["id"]]) if saved.get(q["id"]) in opts else None,
                 format_func=lambda k, o=opts: f"{k}. {o[k]}", key=f"ans_{aid}_{q['id']}", label_visibility="collapsed",
                 on_change=lambda qid=q["id"]: db.save_answer(aid, qid, st.session_state[f"ans_{aid}_{qid}"]))
    answered = len(db.saved_answers(aid))
    st.progress(answered / max(1, len(qs)), text=f"{answered}/{len(qs)} answered")
    if st.button("Submit test", type="primary"):
        st.session_state.confirm_submit = True
    if st.session_state.get("confirm_submit"):
        st.warning(f"Submit now? {len(qs) - answered} question(s) unanswered. This cannot be undone.")
        if st.button("Yes, submit"):
            finish("completed")


def _timer_html(deadline):
    left = max(0, int(deadline - time.time()))
    cls = "timer low" if left < 60 else "timer"
    return f'<div class="card"><div class="metric-label">Time left</div><div class="{cls}">{left // 60:02d}:{left % 60:02d}</div></div>'


@st.fragment(run_every=1)
def timer_only(deadline, aid, finish):
    st.markdown(_timer_html(deadline), unsafe_allow_html=True)
    if time.time() >= deadline or db.one("SELECT status FROM attempts WHERE id=?", (aid,))["status"] != "in_progress":
        finish("timed_out")


@st.fragment(run_every=1)
def monitor(ctx, aid, deadline, max_viol, finish):
    """Once per second (questions are NOT re-rendered): store focus, detect strikes, enforce the limit."""
    st.markdown(_timer_html(deadline), unsafe_allow_html=True)
    now = time.time()
    if now >= deadline:
        finish("timed_out")
    det = st.session_state.det
    proc = ctx.video_processor if ctx else None
    events, samples = [], []
    if proc:
        st.session_state.pop("cam_off_since", None)
        samples = proc.drain()
    else:
        st.error("⚠️ Camera is off — press START on the camera panel or you will receive a strike.")
        since = st.session_state.setdefault("cam_off_since", now)
        if now - since >= det.no_face_s * 2 and now - det.last_fired.get("Camera off", float("-inf")) >= det.cooldown:
            det.last_fired["Camera off"] = now
            events.append((now, "Camera off", "Camera stopped during the test"))
    if samples:
        db.run("INSERT INTO focus_logs VALUES(?,?,?,?,?,?,?)", (aid, *aggregate(samples)))
        events += det.feed(samples)
    if events:
        db.many("INSERT INTO violations(attempt_id,ts,kind,detail) VALUES(?,?,?,?)", [(aid, *e) for e in events])

    s = db.one("""SELECT status, (SELECT COUNT(*) FROM violations WHERE attempt_id=a.id) nv,
                         (SELECT score FROM focus_logs WHERE attempt_id=a.id ORDER BY ts DESC LIMIT 1) score,
                         (SELECT faces FROM focus_logs WHERE attempt_id=a.id ORDER BY ts DESC LIMIT 1) faces
                  FROM attempts a WHERE id=?""", (aid,))
    if s["status"] != "in_progress":  # ended by an administrator
        finish(s["status"])
    nv, score = s["nv"], s["score"] or 0.0
    if events:
        st.toast(f"⚠️ Strike {min(nv, max_viol)}/{max_viol} — {events[-1][1]}: {events[-1][2]}", icon="🚨")

    dots = "".join('<span style="color:#b3261e">●</span>' if i < nv else '<span style="color:#cfd8d2">●</span>' for i in range(max_viol))
    c = st.columns(2)
    metric("Focus", f"{score:.0f}%", c[0])
    metric("Strikes", f"{nv}/{max_viol}", c[1])
    st.markdown(f'<div style="font-size:1.6rem;letter-spacing:.35rem;margin:.4rem 0">{dots}</div>', unsafe_allow_html=True)
    if nv == max_viol - 1:
        st.error("**Final warning** — one more strike ends your test.")
    elif nv:
        st.warning(f"Strike {nv} of {max_viol}. Stay in frame and keep looking at the screen.")
    st.progress(int(min(100, score)), text=_alert_text(score))
    if s["faces"] == 0:
        st.error("No face detected — look at the screen.")
    elif (s["faces"] or 0) > 1:
        st.error("Multiple faces detected.")
    if nv >= max_viol:
        finish("terminated")


def _alert_text(score):
    return ("Excellent focus" if score >= 80 else "Focus is good" if score >= 60
            else "Focus decreasing — refocus" if score >= 40 else "Low focus — look at the screen")


# ------------------------------------------------------------------ results
def show_result(aid, sett):
    a = db.one("SELECT a.*, t.title, t.pass_pct, t.max_violations FROM attempts a JOIN tests t ON t.id=a.test_id WHERE a.id=?", (aid,))
    p = pct(a["score"], a["total"])
    if a["status"] == "terminated":
        if a["violations"] >= a["max_violations"]:
            st.error(f"🚨 Your test was ended automatically after {a['violations']} strikes. "
                     "Answers saved up to that point were graded.")
        else:
            st.error("🚨 Your test was ended by an administrator. Answers saved up to that point were graded.")
    st.markdown(f"### {a['title']} — {result_badge(p >= a['pass_pct'])}", unsafe_allow_html=True)
    rank, n = db.rank_of(aid)
    c = st.columns(5)
    metric("Score", f"{a['score']:g}/{a['total']:g}", c[0])
    metric("Percent", f"{p:.0f}%", c[1])
    metric("Avg focus", f"{a['avg_focus']:.0f}%", c[2])
    metric("Integrity", f"{integrity(a['avg_focus'], a['violations'])}", c[3])
    metric("Rank", f"#{rank} of {n}", c[4])
    st.caption(f"Status: {STATUS_LABEL.get(a['status'], a['status'])} · finished {fmt_ts(a['finished_at'])} · "
               f"{a['violations']} strike(s)")
    v = db.df("SELECT ts, kind, detail FROM violations WHERE attempt_id=? ORDER BY ts", (aid,))
    if not v.empty:
        v["time"] = v.pop("ts").map(lambda x: time.strftime("%H:%M:%S", time.localtime(x)))
        with st.expander(f"Proctoring events ({len(v)})"):
            st.dataframe(v[["time", "kind", "detail"]], use_container_width=True, hide_index=True)
    lb = db.leaderboard(a["test_id"], 5)
    if not lb.empty:
        lb["client"] = lb["client"].map(short_name)
        with st.expander("🏆 Top performers on this test"):
            st.dataframe(lb.rename(columns={"best_pct": "best %"}), use_container_width=True, hide_index=True)
    if sett["show_answers"] == "1":
        r = db.df("""SELECT q.text question, a.choice your_answer, q.answer correct_answer, a.correct
                     FROM answers a JOIN questions q ON q.id=a.question_id WHERE a.attempt_id=?""", (aid,))
        with st.expander("Answer review"):
            st.dataframe(r, use_container_width=True, hide_index=True)


def my_results(user):
    head("Client portal", "My results")
    expire_stale(user["id"])
    d = db.df("""SELECT a.id, t.title, t.pass_pct, a.status, a.score, a.total, a.avg_focus, a.violations, a.finished_at
                 FROM attempts a JOIN tests t ON t.id=a.test_id WHERE a.user_id=? AND a.status!='in_progress'
                 ORDER BY a.id DESC""", (user["id"],))
    if d.empty:
        return st.info("You haven't completed any tests yet.")
    d["percent"] = [round(pct(a, b), 1) for a, b in zip(d["score"], d["total"])]
    d["result"] = ["Passed" if p >= m else "Failed" for p, m in zip(d["percent"], d["pass_pct"])]
    d["finished"] = d["finished_at"].map(fmt_ts)
    d["status"] = d["status"].map(lambda x: STATUS_LABEL.get(x, x))
    st.dataframe(d[["title", "finished", "status", "percent", "result", "avg_focus", "violations"]].round(1),
                 use_container_width=True, hide_index=True)
    aid = st.selectbox("Details", d["id"].tolist(), format_func=lambda i: f"#{i} · {d.set_index('id').loc[i, 'title']}")
    show_result(aid, db.get_settings())


PAGES = {"Dashboard": dashboard, "Take Test": take_test, "My Results": my_results, "Profile": profile_page}
