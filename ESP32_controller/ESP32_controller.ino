/*
 * ESP32 Bluetooth game controller for the 5x5x5 LED cube.
 *
 * Sends short text tokens over Bluetooth Classic (SPP) to the Raspberry Pi,
 * where game_controller_bridge.py turns them into the cube server's game
 * commands. No LCD yet - just 6 direction buttons + Fire + Start + Select.
 *
 * Setup:
 *   1. Flash this sketch (Arduino IDE: board "ESP32 Dev Module").
 *   2. On the Pi, pair with this device (it advertises as "CubeController"),
 *      then:  sudo rfcomm bind 0 <MAC> 1
 *   3. Run:  python game_controller_bridge.py          (live)
 *        or:  python game_controller_bridge.py --test   (just watch input)
 *
 * Wiring: each button connects its GPIO to GND. Internal pull-ups are enabled,
 * so an un-pressed button reads HIGH and a press reads LOW. No external
 * resistors needed.
 *
 * Tokens sent (one per line):
 *   px nx py ny pz nz          direction buttons (axis +/-)
 *   fire                       Fire button (used by Meteors; ignored elsewhere)
 *   start                      Start button  (preceded by a game:<name> resync)
 *   game:planes / game:snake / game:meteors
 *                              sent when Select cycles the selected game
 */

#include "BluetoothSerial.h"

#if !defined(CONFIG_BT_ENABLED) || !defined(CONFIG_BLUEDROID_ENABLED)
#error Bluetooth is not enabled! Enable it in the Arduino ESP32 board options.
#endif

BluetoothSerial SerialBT;

// ---- button -> GPIO pin map ----------------------------------------------
// These pins all support internal pull-ups and avoid the ESP32 strapping pins
// (0, 2, 12, 15) and the input-only pins (34-39). Adjust to taste.
const int PIN_XP     = 32;  // X+
const int PIN_XN     = 33;  // X-
const int PIN_YP     = 25;  // Y+
const int PIN_YN     = 26;  // Y-
const int PIN_ZP     = 27;  // Z+
const int PIN_ZN     = 14;  // Z-
const int PIN_FIRE   = 23;  // Fire (Meteors)
const int PIN_START  = 13;  // Start
const int PIN_SELECT = 4;   // Select

struct Button {
  int pin;
  const char* token;       // "px".."nz", "fire", "start", or "select"
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
  { PIN_FIRE,   "fire",   HIGH, true, 0 },
  { PIN_START,  "start",  HIGH, true, 0 },
  { PIN_SELECT, "select", HIGH, true, 0 },
};
const int NUM_BUTTONS = sizeof(buttons) / sizeof(buttons[0]);

const unsigned long DEBOUNCE_MS = 30;

// ---- game selection (driven by the Select button) ------------------------
const char* GAMES[] = { "planes", "snake", "meteors" };
const int NUM_GAMES = sizeof(GAMES) / sizeof(GAMES[0]);
int selectedGame = 0;   // index into GAMES; "planes" matches the bridge default

void sendLine(const char* s) {
  SerialBT.println(s);
  Serial.println(s);    // mirror to USB serial for debugging
}

void sendSelectedGame() {
  String msg = String("game:") + GAMES[selectedGame];
  sendLine(msg.c_str());
}

void onPress(const Button& b) {
  if (strcmp(b.token, "select") == 0) {
    selectedGame = (selectedGame + 1) % NUM_GAMES;
    sendSelectedGame();
  } else if (strcmp(b.token, "start") == 0) {
    // Resync the bridge to the current selection, then start it. This makes a
    // bridge that joined late (or restarted) always launch the right game.
    sendSelectedGame();
    sendLine("start");
  } else {
    sendLine(b.token);   // a direction or fire button
  }
}

void setup() {
  Serial.begin(115200);
  for (int i = 0; i < NUM_BUTTONS; i++) {
    pinMode(buttons[i].pin, INPUT_PULLUP);
  }
  SerialBT.begin("CubeController");   // Bluetooth SPP slave name
  Serial.println("CubeController ready - pair from the Pi.");
  sendSelectedGame();                 // announce default selection
}

void loop() {
  unsigned long now = millis();
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
