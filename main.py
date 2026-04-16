import cv2
import time
import mediapipe as mp
import numpy as np
from datetime import datetime
import requests
from concurrent.futures import ThreadPoolExecutor

# ── Firebase ──────────────────────────────────────────────────
DATABASE_URL = "https://smart-desk-monitor-4b9f2-default-rtdb.asia-southeast1.firebasedatabase.app"
fb_executor  = ThreadPoolExecutor(max_workers=10) # Increased workers to prevent queue bottlenecks

# Create a persistent session
session = requests.Session()

def fb_patch(path, data: dict):
    try:
        # Use session.patch instead of requests.patch
        session.patch(f"{DATABASE_URL}/{path}.json", json=data, timeout=3)
    except Exception as e:
        pass

def fb_async(data: dict):
    fb_executor.submit(fb_patch, "studyguard/student", data)

def fb_daily_async(today: str, data: dict):
    fb_executor.submit(fb_patch, f"studyguard/daily_logs/{today}", data)

def fetch_presence_async():
    global person_present, ultrasonic_absent_since
    try:
        # Use session.get instead of requests.get
        res = session.get(f"{DATABASE_URL}/studyguard/student/present.json", timeout=3)
        val = res.json()
        if val is not None:
            raw = bool(val)
            # Debounce: only mark absent after 10 seconds of no presence
            if not raw:
                if ultrasonic_absent_since is None:
                    ultrasonic_absent_since = time.time()
                elif time.time() - ultrasonic_absent_since >= 10.0:
                    person_present = False
            else:
                ultrasonic_absent_since = None
                person_present          = True
    except:
        pass

def load_today_previous_total() -> float:
    today = datetime.now().strftime("%Y-%m-%d")
    try:
        res = requests.get(f"{DATABASE_URL}/studyguard/daily_logs/{today}.json", timeout=3)
        val = res.json()
        if val and isinstance(val, dict) and "duration" in val:
            d = float(val["duration"])
            print(f"[Startup] Loaded today's earlier total: {d}s")
            return d
    except:
        pass
    return 0.0

# ── MediaPipe setup ───────────────────────────────────────────
mp_face = mp.solutions.face_mesh
mp_draw = mp.solutions.drawing_utils

face_mesh = mp_face.FaceMesh(
    static_image_mode=False,
    max_num_faces=1,
    refine_landmarks=True,
    min_detection_confidence=0.7,
    min_tracking_confidence=0.7
)

# ── EAR drowsiness landmarks ──────────────────────────────────
LEFT_EYE  = [33,  160, 158, 133, 153, 144]
RIGHT_EYE = [362, 385, 387, 263, 373, 380]
NOSE_TIP  = 1

EAR_THRESHOLD      = 0.22   # below = eye closed
DROWSY_SECS        = 3.0    # seconds of closed eyes before alert
HEAD_DROP_THRESH   = 0.72   # nose Y ratio — below this = head dropped forward

def eye_aspect_ratio(landmarks, eye_indices, w, h):
    pts = [np.array([landmarks[i].x * w, landmarks[i].y * h]) for i in eye_indices]
    A = np.linalg.norm(pts[1] - pts[5])
    B = np.linalg.norm(pts[2] - pts[4])
    C = np.linalg.norm(pts[0] - pts[3])
    return (A + B) / (2.0 * C + 1e-6)

def is_head_dropped(landmarks):
    return landmarks[NOSE_TIP].y > HEAD_DROP_THRESH

# ── Study time ────────────────────────────────────────────────
previous_daily_total = load_today_previous_total()
study_running        = False
study_start_time     = None
study_total_secs     = 0.0

def get_session_secs():
    if study_running and study_start_time:
        return study_total_secs + (time.time() - study_start_time)
    return study_total_secs

def get_daily_total_secs():
    return previous_daily_total + get_session_secs()

def format_time(secs):
    h = int(secs // 3600)
    m = int((secs % 3600) // 60)
    s = int(secs % 60)
    return f"{h:02d}:{m:02d}:{s:02d}"

def pause_study():
    global study_running, study_total_secs, study_start_time
    if study_running and study_start_time:
        study_total_secs += time.time() - study_start_time
        study_start_time  = None
    study_running = False

def resume_study():
    global study_running, study_start_time
    if not study_running:
        study_start_time = time.time()
        study_running    = True

# ── Focus score ───────────────────────────────────────────────
desk_session_start  = None
desk_total_secs     = 0.0

def get_desk_secs():
    if desk_session_start:
        return desk_total_secs + (time.time() - desk_session_start)
    return desk_total_secs

def calculate_focus_score():
    desk  = get_desk_secs()
    study = get_session_secs()
    if desk <= 0:
        return 0
    return min(100, int((study / desk) * 100))

# ── State ─────────────────────────────────────────────────────
led_state               = False
face_detected           = False
person_present          = False
ultrasonic_absent_since = None

drowsy_start            = None
drowsy_alert            = False
last_fb_push            = 0
last_fb_fetch           = 0
frame_count             = 0
reset_done_today        = False
cached_face_landmarks   = None

# ── Camera ────────────────────────────────────────────────────
cap = cv2.VideoCapture(0)
cap.set(cv2.CAP_PROP_FRAME_WIDTH,  640)
cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
cap.set(cv2.CAP_PROP_FPS, 30)

print("=" * 42)
print("  StudyGuard Started")
print(f"  Today's previous total : {format_time(previous_daily_total)}")
print("  Timer is FULLY AUTOMATIC")
print("  Q = Quit")
print("=" * 42)

last_pushed_state = {}
# ── Main loop ─────────────────────────────────────────────────
while True:
    ret, frame = cap.read()
    if not ret:
        break

    frame        = cv2.flip(frame, 1)
    h, w, _      = frame.shape
    frame_count += 1
    now          = time.time()
    drowsy_text  = ""

    rgb_full = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)

    # ── Fetch presence from Firebase every 2s (non-blocking) ──
    if now - last_fb_fetch >= 2:
        last_fb_fetch = now
        fb_executor.submit(fetch_presence_async)

    # ── FaceMesh every other frame for performance ─────────────
    if frame_count % 2 == 0:
        face_result           = face_mesh.process(rgb_full)
        cached_face_landmarks = face_result.multi_face_landmarks
        face_detected         = cached_face_landmarks is not None

        # ── Drowsiness detection ──────────────────────────────
        if face_detected:
            for face_lm in cached_face_landmarks:
                lms       = face_lm.landmark
                left_ear  = eye_aspect_ratio(lms, LEFT_EYE,  w, h)
                right_ear = eye_aspect_ratio(lms, RIGHT_EYE, w, h)
                avg_ear   = (left_ear + right_ear) / 2.0
                head_drop = is_head_dropped(lms)

                is_drowsy_signal = (avg_ear < EAR_THRESHOLD) or head_drop

                if is_drowsy_signal:
                    if drowsy_start is None:
                        drowsy_start = now
                    elapsed = now - drowsy_start

                    if elapsed >= DROWSY_SECS:
                        if not drowsy_alert:
                            drowsy_alert = True
                            fb_async({"buzzer": "ON"})
                            print("[Drowsy] ALERT triggered")
                        drowsy_text = "DROWSY ALERT! Wake up!"
                        pause_study()
                    else:
                        remaining   = DROWSY_SECS - elapsed
                        drowsy_text = f"Eyes closing... {remaining:.1f}s"
                else:
                    if drowsy_alert:
                        fb_async({"buzzer": "OFF"})
                        print("[Drowsy] Recovered")
                    drowsy_start = None
                    drowsy_alert = False
        else:
            if drowsy_alert:
                fb_async({"buzzer": "OFF"})
            drowsy_start = None
            drowsy_alert = False

    # ── Draw eye landmarks ────────────────────────────────────
    if cached_face_landmarks:
        for face_lm in cached_face_landmarks:
            for idx in LEFT_EYE + RIGHT_EYE:
                cx = int(face_lm.landmark[idx].x * w)
                cy = int(face_lm.landmark[idx].y * h)
                cv2.circle(frame, (cx, cy), 2, (0, 255, 255), -1)

    # ── Auto LED ──────────────────────────────────────────────
    both_present = face_detected and person_present

    if both_present and not led_state:
        led_state = True
        fb_async({"LED_Status": "ON"})
        print("[LED] Auto ON")

    if not both_present and led_state:
        led_state = False
        fb_async({"LED_Status": "OFF"})
        print("[LED] Auto OFF")

    # ── Desk time tracking ────────────────────────────────────
    if both_present:
        if desk_session_start is None:
            desk_session_start = now
    else:
        if desk_session_start is not None:
            desk_total_secs   += now - desk_session_start
            desk_session_start = None

    # ── Automatic study timer ─────────────────────────────────
    if both_present and not drowsy_alert:
        resume_study()
        status = "studying"
    elif not both_present:
        pause_study()
        status = "absent"
    elif drowsy_alert:
        pause_study()
        status = "drowsy"
    else:
        pause_study()
        status = "idle"

    # ── Midnight daily reset ──────────────────────────────────
    now_dt = datetime.now()
    if now_dt.hour == 0 and now_dt.minute == 0 and not reset_done_today:
        final_daily = int(get_daily_total_secs())
        today       = now_dt.strftime("%Y-%m-%d")
        final_time  = now_dt.strftime("%Y-%m-%d %H:%M:%S")
        focus       = calculate_focus_score()

        def push_final(t=today, s=final_daily, ft=final_time, f=focus):
            try:
                requests.put(
                    f"{DATABASE_URL}/studyguard/daily_logs/{t}.json",
                    json={"duration": s, "focus_score": f, "last_updated": ft},
                    timeout=3
                )
            except:
                pass
        fb_executor.submit(push_final)

        previous_daily_total = 0.0
        study_total_secs     = 0.0
        study_start_time     = None
        study_running        = False
        desk_total_secs      = 0.0
        desk_session_start   = None
        reset_done_today     = True
        print(f"[System] Midnight reset. Final: {final_daily}s | Focus: {focus}%")

    elif now_dt.hour != 0:
        reset_done_today = False

    # ── Optimized Firebase Push (Only on Change or every 10s) ──
    
    today  = now_dt.strftime("%Y-%m-%d")
    daily_total = int(get_daily_total_secs())
    focus_score = calculate_focus_score()

    # Create a dictionary of the current critical state
    current_state = {
        "status": status,
        "focus_score": focus_score,
        "LED_Status": "ON" if led_state else "OFF",
        "buzzer": "ON" if drowsy_alert else "OFF"
    }

    # Only push if the state CHANGED, or every 10 seconds (as a heartbeat for the timer)
    if current_state != last_pushed_state or (now - last_fb_push >= 10):
        last_fb_push = now
        last_pushed_state = current_state.copy() # Save state for next comparison
        
        # Add the constantly changing time data to the payload
        current_state["study_duration"] = daily_total
        current_state["last_updated"] = now_dt.strftime("%Y-%m-%d %H:%M:%S")

        # Push to database
        fb_async(current_state)
        fb_daily_async(today, {"duration": daily_total, "focus_score": focus_score})

    # ── UI overlay ────────────────────────────────────────────
    led_color    = (0, 255, 0) if led_state      else (0, 0, 255)
    status_color = (0, 255, 0) if status == "studying" else (0, 140, 255)
    face_color   = (0, 255, 0) if face_detected  else (0, 0, 255)
    pres_color   = (0, 255, 0) if person_present else (0, 0, 255)
    focus_score  = calculate_focus_score()
    focus_color  = (0, 255, 0) if focus_score >= 70 else \
                   (0, 165, 255) if focus_score >= 40 else (0, 0, 255)

    cv2.putText(frame, f"LED    : {'ON' if led_state else 'OFF'}", (10, 30),  cv2.FONT_HERSHEY_SIMPLEX, 0.75, led_color,    2)
    cv2.putText(frame, f"Status : {status.upper()}",             (10, 58),  cv2.FONT_HERSHEY_SIMPLEX, 0.75, status_color, 2)
    cv2.putText(frame, f"Today  : {format_time(get_daily_total_secs())}", (10, 86),  cv2.FONT_HERSHEY_SIMPLEX, 0.75, (255,255,255),2)
    cv2.putText(frame, f"Session: {format_time(get_session_secs())}",     (10, 114), cv2.FONT_HERSHEY_SIMPLEX, 0.75, (200,200,200),2)
    cv2.putText(frame, f"Focus  : {focus_score}%",               (10, 142), cv2.FONT_HERSHEY_SIMPLEX, 0.75, focus_color,  2)
    cv2.putText(frame, f"Face   : {'Yes' if face_detected else 'No'}",    (10, 170), cv2.FONT_HERSHEY_SIMPLEX, 0.75, face_color,   2)
    cv2.putText(frame, f"Person : {'Present' if person_present else 'Absent'}", (10, 198), cv2.FONT_HERSHEY_SIMPLEX, 0.75, pres_color,   2)

    if drowsy_text:
        col = (0, 0, 255) if "ALERT" in drowsy_text else (0, 165, 255)
        cv2.putText(frame, drowsy_text, (10, 230), cv2.FONT_HERSHEY_SIMPLEX, 0.8, col, 2)

    cv2.imshow("StudyGuard", frame)
    key = cv2.waitKey(1) & 0xFF
    if key == ord('q'):
        final_daily = int(get_daily_total_secs())
        today       = datetime.now().strftime("%Y-%m-%d")
        fb_executor.submit(fb_patch, f"studyguard/daily_logs/{today}", {"duration": final_daily, "focus_score": calculate_focus_score()})
        print(f"[Quit] Saved today's total: {final_daily}s")
        break

cap.release()
cv2.destroyAllWindows()
fb_executor.shutdown(wait=True) 