// Truth Social Flagpole — Arduino Uno firmware
//
// Drives:
//   * H-bridge flag motor on pins 9 (IN1) / 10 (IN2)
//   * HW-069 (TM1637) 4-digit 7-seg #1 on pins 2 (CLK) / 3 (DIO)  — flag percent
//   * HW-069 (TM1637) 4-digit 7-seg #2 on pins 4 (CLK) / 5 (DIO)  — countdown MM:SS
//
// Required library (install via Arduino IDE Library Manager):
//   "TM1637" by Avishay Orpaz   (header: TM1637Display.h)
//
// Serial protocol (9600 baud, newline-terminated, from Raspberry Pi 5):
//   U / D / UU / DD   jog motor (calibration)
//   L / H             mark low / high calibration endpoints
//   R                 reset calibration
//   S / ?             status / help
//   G<pct>            move motor to pct (0-100)
//   P<pct>            set percent display (Seg1)
//   T<secs>           start countdown on Seg2; -1 blanks it
//
// EEPROM layout is preserved from the previous firmware so existing
// calibration survives an upgrade.

#include <EEPROM.h>
#include <TM1637Display.h>

// ── Motor ─────────────────────────────────────────────────────────────────
#define MOTOR_IN1 9
#define MOTOR_IN2 10

// PWM speeds for positioning. Gravity assists downward motion,
// so DOWN runs at half speed to keep travel distances consistent.
// Changing these invalidates calibration — recalibrate after any change.
#define MOTOR_SPEED_UP   100
#define MOTOR_SPEED_DOWN 50

#define JOG_FINE_MS   100
#define JOG_COARSE_MS 1000

// ── 7-segment displays ────────────────────────────────────────────────────
#define SEG1_CLK 2
#define SEG1_DIO 3
#define SEG2_CLK 4
#define SEG2_DIO 5
#define SEG_BRIGHTNESS 4   // 0..7

TM1637Display seg1(SEG1_CLK, SEG1_DIO);
TM1637Display seg2(SEG2_CLK, SEG2_DIO);

const uint8_t SEG_DASHES[4] = {0x40, 0x40, 0x40, 0x40};
const uint8_t SEG_BLANK[4]  = {0x00, 0x00, 0x00, 0x00};

// ── EEPROM ────────────────────────────────────────────────────────────────
#define EEPROM_MAGIC_ADDR 0
#define EEPROM_RANGE_ADDR 1   // 4 bytes (long)
#define EEPROM_POS_ADDR   5   // 4 bytes (long)
#define EEPROM_MAGIC_VAL  0xCA

// ── State ─────────────────────────────────────────────────────────────────
long range_ms    = 0;
long position_ms = 0;
bool calibrated  = false;
bool low_set     = false;

int  pct_display    = -1;    // -1 => blank
long countdown_sec  = -1;    // -1 => blank, 0 => hold at 00:00
unsigned long last_tick_ms = 0;
bool colon_on = true;

// ── Motor primitives ──────────────────────────────────────────────────────
void stopMotor() {
  analogWrite(MOTOR_IN1, 0);
  analogWrite(MOTOR_IN2, 0);
  digitalWrite(MOTOR_IN1, LOW);
  digitalWrite(MOTOR_IN2, LOW);
}

void runMotorUp(long ms) {
  if (ms <= 0) return;
  stopMotor();
  delay(20);
  digitalWrite(MOTOR_IN1, LOW);
  analogWrite(MOTOR_IN2, MOTOR_SPEED_UP);
  delay(ms);
  stopMotor();
}

void runMotorDown(long ms) {
  if (ms <= 0) return;
  stopMotor();
  delay(20);
  analogWrite(MOTOR_IN1, MOTOR_SPEED_DOWN);
  digitalWrite(MOTOR_IN2, LOW);
  delay(ms);
  stopMotor();
}

// ── EEPROM helpers ────────────────────────────────────────────────────────
void saveCalibration() {
  EEPROM.update(EEPROM_MAGIC_ADDR, EEPROM_MAGIC_VAL);
  EEPROM.put(EEPROM_RANGE_ADDR, range_ms);
  EEPROM.put(EEPROM_POS_ADDR, position_ms);
}

void savePosition() {
  EEPROM.put(EEPROM_POS_ADDR, position_ms);
}

void loadCalibration() {
  if (EEPROM.read(EEPROM_MAGIC_ADDR) != EEPROM_MAGIC_VAL) return;
  EEPROM.get(EEPROM_RANGE_ADDR, range_ms);
  EEPROM.get(EEPROM_POS_ADDR, position_ms);
  if (range_ms > 0) {
    calibrated = true;
    low_set = true;
    Serial.print(F("CAL:loaded,range="));
    Serial.print(range_ms);
    Serial.print(F("ms,pos="));
    Serial.print(position_ms);
    Serial.print(F("ms,pct="));
    Serial.println(position_ms * 100 / range_ms);
  }
}

// ── Motor commands ────────────────────────────────────────────────────────
void jogUp(long step_ms) {
  if (calibrated && position_ms >= range_ms) {
    Serial.println(F("ERR:At upper limit"));
    return;
  }
  long actual = step_ms;
  if (calibrated) {
    actual = min(step_ms, range_ms - position_ms);
  }
  runMotorUp(actual);
  position_ms += actual;
  Serial.print(F("OK:Jog up "));
  Serial.print(actual);
  Serial.print(F("ms,pos="));
  Serial.println(position_ms);
}

void jogDown(long step_ms) {
  if (calibrated && position_ms <= 0) {
    Serial.println(F("ERR:At lower limit"));
    return;
  }
  long actual = step_ms;
  if (calibrated) {
    actual = min(step_ms, position_ms);
  }
  runMotorDown(actual);
  position_ms -= actual;
  Serial.print(F("OK:Jog down "));
  Serial.print(actual);
  Serial.print(F("ms,pos="));
  Serial.println(position_ms);
}

void setLow() {
  position_ms = 0;
  low_set = true;
  calibrated = false;
  Serial.println(F("OK:Low set. Jog up to high, press H."));
}

void setHigh() {
  if (!low_set) {
    Serial.println(F("ERR:Set low first (L)"));
    return;
  }
  if (position_ms <= 0) {
    Serial.println(F("ERR:Jog up from low first"));
    return;
  }
  range_ms = position_ms;
  calibrated = true;
  saveCalibration();
  Serial.print(F("OK:High set. Range="));
  Serial.print(range_ms);
  Serial.println(F("ms. Saved."));
}

void gotoPercent(int pct) {
  if (!calibrated) {
    Serial.println(F("ERR:Not calibrated"));
    return;
  }
  pct = constrain(pct, 0, 100);
  long target = (long)pct * range_ms / 100;
  long diff = target - position_ms;

  Serial.print(F("OK:Moving to "));
  Serial.print(pct);
  Serial.print(F("% (delta="));
  Serial.print(diff);
  Serial.println(F("ms)"));

  if (diff > 0) {
    runMotorUp(diff);
  } else if (diff < 0) {
    runMotorDown(-diff);
  }

  position_ms = target;
  savePosition();
  Serial.print(F("POS:"));
  Serial.println(pct);
}

void printStatus() {
  Serial.print(F("STATUS:cal="));
  Serial.print(calibrated ? F("yes") : F("no"));
  Serial.print(F(",range="));
  Serial.print(range_ms);
  Serial.print(F("ms,pos="));
  Serial.print(position_ms);
  Serial.print(F("ms,pct="));
  if (calibrated && range_ms > 0) {
    Serial.println(position_ms * 100 / range_ms);
  } else {
    Serial.println(F("N/A"));
  }
}

void printHelp() {
  Serial.println(F("Commands:"));
  Serial.println(F("  U/D    Fine jog up/down"));
  Serial.println(F("  UU/DD  Coarse jog up/down"));
  Serial.println(F("  L/H    Mark low/high calibration endpoints"));
  Serial.println(F("  G<n>   Go to n% (0-100)"));
  Serial.println(F("  P<n>   Show n% on Seg1 (-1 blanks)"));
  Serial.println(F("  T<n>   Start countdown of n seconds on Seg2 (-1 blanks)"));
  Serial.println(F("  S      Status"));
  Serial.println(F("  R      Reset calibration"));
  Serial.println(F("  ?      Help"));
}

// ── 7-seg rendering ───────────────────────────────────────────────────────
void renderPercent() {
  if (pct_display < 0) {
    seg1.setSegments(SEG_DASHES);
  } else {
    // Right-aligned with no leading zeros.
    seg1.showNumberDec(pct_display, false);
  }
}

void renderCountdown() {
  if (countdown_sec < 0) {
    seg2.setSegments(SEG_DASHES);
    return;
  }
  long total = countdown_sec;
  if (total > 5999) total = 5999;  // clamp to 99:59
  int mm = total / 60;
  int ss = total % 60;
  int value = mm * 100 + ss;
  // Dot bit 0x40 on digit index 1 lights the colon between digits 2 and 3.
  uint8_t dots = colon_on ? 0x40 : 0x00;
  seg2.showNumberDecEx(value, dots, true);
}

void tickCountdown() {
  unsigned long now = millis();
  if (now - last_tick_ms < 1000) return;
  last_tick_ms += 1000;
  colon_on = !colon_on;
  if (countdown_sec > 0) countdown_sec--;
  renderCountdown();
}

// ── Setup / loop ──────────────────────────────────────────────────────────
void setup() {
  Serial.begin(9600);
  pinMode(MOTOR_IN1, OUTPUT);
  pinMode(MOTOR_IN2, OUTPUT);
  stopMotor();

  seg1.setBrightness(SEG_BRIGHTNESS);
  seg2.setBrightness(SEG_BRIGHTNESS);
  seg1.setSegments(SEG_BLANK);
  seg2.setSegments(SEG_BLANK);
  renderPercent();
  renderCountdown();

  loadCalibration();
  last_tick_ms = millis();
  Serial.println(F("READY"));
}

void loop() {
  tickCountdown();

  if (!Serial.available()) return;

  String input = Serial.readStringUntil('\n');
  input.trim();
  if (input.length() == 0) return;

  if      (input == "U")  jogUp(JOG_FINE_MS);
  else if (input == "D")  jogDown(JOG_FINE_MS);
  else if (input == "UU") jogUp(JOG_COARSE_MS);
  else if (input == "DD") jogDown(JOG_COARSE_MS);
  else if (input == "L")  setLow();
  else if (input == "H")  setHigh();
  else if (input == "S")  printStatus();
  else if (input == "?")  printHelp();
  else if (input == "R") {
    EEPROM.update(EEPROM_MAGIC_ADDR, 0);
    calibrated = false;
    range_ms = 0;
    position_ms = 0;
    low_set = false;
    Serial.println(F("OK:Calibration cleared"));
  }
  else if (input.startsWith("G") || input.startsWith("g")) {
    gotoPercent(input.substring(1).toInt());
  }
  else if (input.startsWith("P") || input.startsWith("p")) {
    int v = input.substring(1).toInt();
    pct_display = (v < 0) ? -1 : constrain(v, 0, 9999);
    renderPercent();
    Serial.print(F("OK:P="));
    Serial.println(pct_display);
  }
  else if (input.startsWith("T") || input.startsWith("t")) {
    long v = input.substring(1).toInt();
    countdown_sec = (v < 0) ? -1 : v;
    last_tick_ms = millis();
    colon_on = true;
    renderCountdown();
    Serial.print(F("OK:T="));
    Serial.println(countdown_sec);
  }
  else {
    Serial.println(F("ERR:Unknown. Send ? for help."));
  }
}
