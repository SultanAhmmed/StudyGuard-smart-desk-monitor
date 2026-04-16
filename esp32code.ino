#include <WiFi.h>
#include <Firebase_ESP_Client.h>
#include "addons/TokenHelper.h"
#include "addons/RTDBHelper.h"

// ── WiFi credentials ──────────────────────────────────────────
#define WIFI_SSID      "Nothing Personal"
#define WIFI_PASSWORD  "12345678"

// ── Firebase credentials ──────────────────────────────────────
#define API_KEY        "AIzaSyCC_Nd8a27c5RqTk0TRmilPcb3sJyi0Pi8"
#define DATABASE_URL   "smart-desk-monitor-4b9f2-default-rtdb.asia-southeast1.firebasedatabase.app/"

// ── Pin definitions ───────────────────────────────────────────
#define TRIG_PIN       5
#define ECHO_PIN       18
#define LED_PIN        2
#define BUZZER_PIN     4

// ── Presence config ───────────────────────────────────────────
#define PRESENCE_DISTANCE_CM   10.0
#define ABSENT_DEBOUNCE_MS     10000

// ── Firebase objects ──────────────────────────────────────────
FirebaseData   fbdo;
FirebaseAuth   auth;
FirebaseConfig config;

// ── State ─────────────────────────────────────────────────────
bool          personPresent       = false;
bool          ledState            = false;
bool          buzzerOn            = false;
bool          firebaseReady       = false;
unsigned long lastSensorRead      = 0;
unsigned long lastFirebasePush    = 0;
unsigned long lastFirebaseRead    = 0;
unsigned long absentSince         = 0;
unsigned long lastWifiAttempt     = 0; // Added for non-blocking WiFi reconnect

// ── Ultrasonic ────────────────────────────────────────────────
float getDistance() {
    digitalWrite(TRIG_PIN, LOW);
    delayMicroseconds(2);
    digitalWrite(TRIG_PIN, HIGH);
    delayMicroseconds(10);
    digitalWrite(TRIG_PIN, LOW);
    long duration = pulseIn(ECHO_PIN, HIGH, 30000);
    if (duration == 0) return 999.0;
    return duration * 0.034 / 2.0;
}

// ── Debounced presence ────────────────────────────────────────
bool getDebouncedPresence(float dist) {
    bool rawPresent = (dist > 2.0 && dist < PRESENCE_DISTANCE_CM);
    if (rawPresent) {
        absentSince = 0;
        return true;
    } else {
        if (absentSince == 0) absentSince = millis();
        if (millis() - absentSince >= ABSENT_DEBOUNCE_MS) return false;
        return personPresent; 
    }
}

// ── Buzzer — 3 short beeps pattern ───────────────────────────
void handleBuzzer() {
    if (!buzzerOn) {
        digitalWrite(BUZZER_PIN, LOW);
        return;
    }
    unsigned long pos = millis() % 1800;
    if      (pos <  200) digitalWrite(BUZZER_PIN, HIGH); 
    else if (pos <  400) digitalWrite(BUZZER_PIN, LOW);
    else if (pos <  600) digitalWrite(BUZZER_PIN, HIGH); 
    else if (pos <  800) digitalWrite(BUZZER_PIN, LOW);
    else if (pos < 1000) digitalWrite(BUZZER_PIN, HIGH); 
    else                 digitalWrite(BUZZER_PIN, LOW);  
}

// ── Setup ─────────────────────────────────────────────────────
void setup() {
    Serial.begin(115200);

    pinMode(TRIG_PIN,   OUTPUT);
    pinMode(ECHO_PIN,   INPUT);
    pinMode(LED_PIN,    OUTPUT);
    pinMode(BUZZER_PIN, OUTPUT);
    digitalWrite(LED_PIN,    LOW);
    digitalWrite(BUZZER_PIN, LOW);

    Serial.print("[WiFi] Connecting");

    digitalWrite(LED_PIN, HIGH);
    delay(2000);
    digitalWrite(LED_PIN, LOW);

    WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
    int attempts = 0;
    
    // Try to connect to WiFi for ~20 seconds
    while (WiFi.status() != WL_CONNECTED && attempts < 40) {
        delay(500);
        Serial.print(".");
        attempts++;
    }
    
    if (WiFi.status() == WL_CONNECTED) {
        Serial.println("\n[WiFi] Connected: " + WiFi.localIP().toString());
        
        // ONLY initialize Firebase if we have a WiFi connection
        config.api_key      = API_KEY;
        config.database_url = DATABASE_URL;
        config.token_status_callback = tokenStatusCallback;

        if (Firebase.signUp(&config, &auth, "", "")) {
            Serial.println("[Firebase] Auth OK");
            firebaseReady = true;
        } else {
            Serial.printf("[Firebase] Auth error: %s\n", config.signer.signupError.message.c_str());
        }

        Firebase.begin(&config, &auth);
        Firebase.reconnectWiFi(true);
        fbdo.setResponseSize(1024);
        
    } else {
        Serial.println("\n[WiFi] FAILED. Skipping Firebase setup.");
        // firebaseReady remains false, preventing the loop from spamming errors
    }

    Serial.println("[System] Ready — threshold: " + String(PRESENCE_DISTANCE_CM) + "cm");
}
// ── Loop ──────────────────────────────────────────────────────
void loop() {
    unsigned long now = millis();

    // FIXED: Buzzer runs FIRST and independently. It will not freeze anymore.
    handleBuzzer();

    // FIXED: Non-blocking WiFi reconnect instead of delay(1000)
    if (WiFi.status() != WL_CONNECTED) {
        if (now - lastWifiAttempt > 5000) {
            WiFi.reconnect();
            lastWifiAttempt = now;
        }
        return; // Prevents calling Firebase when offline, but loop continues for buzzer
    }

    if (!Firebase.ready()) return;

    // ── Read ultrasonic every 500ms ───────────────────────────
    if (now - lastSensorRead >= 500) {
        lastSensorRead = now;
        float dist         = getDistance();
        bool  newPresence  = getDebouncedPresence(dist);

        if (newPresence != personPresent) {
            personPresent = newPresence;
            Serial.printf("[Sensor] %.1fcm | %s\n", dist, personPresent ? "PRESENT" : "ABSENT");
        }
    }

    // ── Push presence to Firebase every 1s ───────────────────
    if (now - lastFirebasePush >= 1000) {
        lastFirebasePush = now;
        if (!Firebase.RTDB.setBool(&fbdo, "/studyguard/student/present", personPresent)) {
            Serial.printf("[Firebase] Push error: %s\n", fbdo.errorReason().c_str());
        }
    }

    // ── Read LED + buzzer from Firebase every 1s ──────────────
    if (now - lastFirebaseRead >= 1000) {
        lastFirebaseRead = now;

        // LED
        if (Firebase.RTDB.getString(&fbdo, "/studyguard/student/LED_Status")) {
            String cmd = fbdo.stringData();
            cmd.trim();
            bool newLed = cmd.equalsIgnoreCase("ON");
            if (newLed != ledState) {
                ledState = newLed;
                digitalWrite(LED_PIN, ledState ? HIGH : LOW);
                Serial.printf("[LED] %s\n", ledState ? "ON" : "OFF");
            }
        }

        // Buzzer
        if (Firebase.RTDB.getString(&fbdo, "/studyguard/student/buzzer")) {
            String bz = fbdo.stringData();
            bz.trim();
            bool newBuzzer = bz.equalsIgnoreCase("ON");
            if (newBuzzer != buzzerOn) {
                buzzerOn = newBuzzer;
                Serial.printf("[Buzzer] %s\n", buzzerOn ? "ON" : "OFF");
                if (!buzzerOn) digitalWrite(BUZZER_PIN, LOW);
            }
        }
    }
}