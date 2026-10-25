/*
 * ESP32 Bluetooth game controller for the 5x5x5 LED cube.
 *
 * Sends short text tokens over Bluetooth Classic (SPP) to the Raspberry Pi,
 * where game_controller_bridge.py turns them into the cube server's game
 * commands. 6 direction buttons + Start + Select, and a 96x16 I2C OLED that
 * shows the selected game, the current score and the game's high score.
 * (Meteors fires with Z-; the bridge maps it.)
 *
 * Setup:
 *   1. Install libraries (Arduino Library Manager): "Adafruit SSD1306"
 *      (pulls in "Adafruit GFX Library").
 *   2. Flash this sketch (Arduino IDE: board "ESP32 Dev Module").
 *   3. On the Pi, pair with this device (it advertises as "CubeController"),
 *      then:  sudo rfcomm bind 0 <MAC> 1
 *   4. Run:  python game_controller_bridge.py          (live)
 *        or:  python game_controller_bridge.py --test   (just watch input)
 *
 * Wiring:
 *   Buttons: each button connects its GPIO to GND. Internal pull-ups are
 *   enabled, so an un-pressed button reads HIGH and a press reads LOW. No
 *   external resistors needed.
 *   OLED (SSD1306, 96x16, I2C address 0x3C): VCC -> 3V3, GND -> GND,
 *   SDA -> GPIO 21, SCL -> GPIO 22. OLED modules carry their own I2C pull-ups.
 *
 * Tokens sent (one per line):
 *   px nx py ny pz nz          direction buttons (axis +/-)
 *   start                      Start button  (preceded by a game:<name> resync)
 *   game:planes / game:snake / game:meteors
 *                              sent when Select cycles the selected game, and
 *                              whenever the Pi (re)connects
 *
 * Lines received from the bridge (one per line):
 *   disp:<game>:<score>:<highscore>:<ready|play|over|won>   ('-' = unknown)
 */

#include "BluetoothSerial.h"
#include <Wire.h>
#include <Adafruit_GFX.h>
#include <Adafruit_SSD1306.h>

#if !defined(CONFIG_BT_ENABLED) || !defined(CONFIG_BLUEDROID_ENABLED)
#error Bluetooth is not enabled! Enable it in the Arduino ESP32 board options.
#endif

BluetoothSerial SerialBT;

// ---- button -> GPIO pin map ----------------------------------------------
// These pins all support internal pull-ups and avoid the ESP32 strapping pins
// (0, 2, 5, 12, 15), the input-only pins (34-39), the flash pins (6-11), the
// USB-serial pins (1, 3) and the OLED's I2C pins (21, 22).
// Other free pins that work just as well: 16, 17, 18, 19, 23.
const int PIN_XP     = 23;  // X+
const int PIN_XN     = 18;  // X-
const int PIN_YP     = 32;  // Y+
const int PIN_YN     = 19;  // Y-
const int PIN_ZP     = 14;  // Z+
const int PIN_ZN     = 27;  // Z- (also Fire in Meteors)
const int PIN_START  = 17;  // Start
const int PIN_SELECT = 16;   // Select

// ---- OLED ----------------------------------------------------------------
const int PIN_OLED_SDA = 21;
const int PIN_OLED_SCL = 22;
const uint8_t OLED_ADDR = 0x3C;
const int OLED_W = 96;
const int OLED_H = 16;
const int CHAR_W = 6;       // built-in 5x7 font + 1px gap: 16 chars x 2 rows

Adafruit_SSD1306 oled(OLED_W, OLED_H, &Wire, -1);
bool oledOk = false;

struct Button {
  int pin;
  const char* token;       // "px".."nz", "start", or "select"
  int lastReading;         // last raw digitalRead
  bool stableHigh;         // debounced state (true = released)
  unsigned long lastChange;
};

Button buttons[] = {
  { PIN_XP,     "px",     HIGH, true, 0 },
  { PIN_XN,     "nx",     HIGH, true, 0 },
  { PIN_YP,     "py",     HIGH, true, 0 },
  { PIN_YN,     "ny",     HIGH, true, 0 },
  { PIN_ZP,     "pz",     HIGH, true, 0 },
  { PIN_ZN,     "nz",     HIGH, true, 0 },
  { PIN_START,  "start",  HIGH, true, 0 },
  { PIN_SELECT, "select", HIGH, true, 0 },
};
const int NUM_BUTTONS = sizeof(buttons) / sizeof(buttons[0]);

const unsigned long DEBOUNCE_MS = 30;

// ---- game selection (driven by the Select button) ------------------------
const char* GAMES[] = { "planes", "snake", "meteors" };
const char* GAME_TITLES[] = { "Space Planes", "3D Snake", "Meteors" };  // <= 12 chars
const int NUM_GAMES = sizeof(GAMES) / sizeof(GAMES[0]);
int selectedGame = 0;   // index into GAMES; "planes" matches the bridge default

// ---- latest status from the bridge ---------------------------------------
String dispScore = "-";
String dispHigh = "-";
String dispStatus = "ready";
bool linked = false;     // is the Pi connected over Bluetooth?
String rxLine;           // partial line being received from the bridge

void sendLine(const char* s) {
  SerialBT.println(s);
  Serial.println(s);    // mirror to USB serial for debugging
}

void sendSelectedGame() {
  String msg = String("game:") + GAMES[selectedGame];
  sendLine(msg.c_str());
}

void printRight(const String& s, int y) {
  oled.setCursor(OLED_W - CHAR_W * s.length(), y);
  oled.print(s);
}

// Row 1: game name (+ OVER / WIN! tag).  Row 2: score and high score.
void drawDisplay() {
  if (!oledOk) return;
  oled.clearDisplay();
  oled.setTextSize(1);
  oled.setTextColor(SSD1306_WHITE);

  oled.setCursor(0, 0);
  oled.print(GAME_TITLES[selectedGame]);
  if (dispStatus == "over") printRight("OVER", 0);
  else if (dispStatus == "won") printRight("WIN!", 0);

  oled.setCursor(0, 8);
  if (!linked) {
    oled.print("Waiting for Pi");
  } else {
    oled.print(dispStatus == "ready" ? String("Ready") : "Sc " + dispScore);
    printRight("Hi " + dispHigh, 8);
  }
  oled.display();
}

void resetDisplayStatus() {
  dispScore = "-";
  dispHigh = "-";
  dispStatus = "ready";
}

// disp:<game>:<score>:<highscore>:<status>
void handleBridgeLine(const String& line) {
  if (!line.startsWith("disp:")) return;
  int a = line.indexOf(':', 5);
  int b = line.indexOf(':', a + 1);
  int c = line.indexOf(':', b + 1);
  if (a < 0 || b < 0 || c < 0) return;
  // Ignore status for a game that is no longer selected (sent before the
  // bridge saw our last Select press).
  if (line.substring(5, a) != GAMES[selectedGame]) return;
  dispScore = line.substring(a + 1, b);
  dispHigh = line.substring(b + 1, c);
  dispStatus = line.substring(c + 1);
  drawDisplay();
}

void readBridge() {
  while (SerialBT.available()) {
    char ch = SerialBT.read();
    if (ch == '\n') {
      handleBridgeLine(rxLine);
      rxLine = "";
    } else if (ch != '\r' && rxLine.length() < 64) {
      rxLine += ch;
    }
  }
}

void onPress(const Button& b) {
  if (strcmp(b.token, "select") == 0) {
    selectedGame = (selectedGame + 1) % NUM_GAMES;
    sendSelectedGame();
    resetDisplayStatus();   // until the bridge reports the new game's scores
    drawDisplay();
  } else if (strcmp(b.token, "start") == 0) {
    // Resync the bridge to the current selection, then start it. This makes a
    // bridge that joined late (or restarted) always launch the right game.
    sendSelectedGame();
    sendLine("start");
  } else {
    sendLine(b.token);   // a direction button
  }
}

void setup() {
  Serial.begin(115200);
  for (int i = 0; i < NUM_BUTTONS; i++) {
    pinMode(buttons[i].pin, INPUT_PULLUP);
  }

  Wire.begin(PIN_OLED_SDA, PIN_OLED_SCL);
  oledOk = oled.begin(SSD1306_SWITCHCAPVCC, OLED_ADDR);
  if (!oledOk) Serial.println("OLED not found - continuing without display.");
  drawDisplay();

  SerialBT.begin("CubeController");   // Bluetooth SPP slave name
  Serial.println("CubeController ready - pair from the Pi.");
}

void loop() {
  unsigned long now = millis();

  // When the Pi (re)connects, tell the bridge which game is selected.
  bool nowLinked = SerialBT.hasClient();
  if (nowLinked != linked) {
    linked = nowLinked;
    if (linked) sendSelectedGame();
    else resetDisplayStatus();
    drawDisplay();
  }

  readBridge();

  for (int i = 0; i < NUM_BUTTONS; i++) {
    Button& b = buttons[i];
    int reading = digitalRead(b.pin);

    if (reading != b.lastReading) {
      b.lastChange = now;
      b.lastReading = reading;
    }

    if ((now - b.lastChange) > DEBOUNCE_MS) {
      bool isHigh = (reading == HIGH);
      if (isHigh != b.stableHigh) {
        b.stableHigh = isHigh;
        if (!isHigh) {       // HIGH -> LOW transition = button pressed (active low)
          onPress(b);
        }
      }
    }
  }
  delay(5);
}
