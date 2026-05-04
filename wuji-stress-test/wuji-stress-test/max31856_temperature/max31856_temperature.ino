/*
 * MAX31856 Temperature Reading for Arduino Uno R4 Minima (2 Sensors)
 * 
 * Wiring (all sensors share SPI bus, each has unique CS pin):
 *   MAX31856     Sensor 1    Sensor 2
 *   --------     --------    --------
 *   VIN       -> 5V          5V
 *   GND       -> GND         GND
 *   SCK       -> Pin 13      Pin 13
 *   SDO       -> Pin 12      Pin 12
 *   SDI       -> Pin 11      Pin 11
 *   CS        -> Pin 10      Pin 9
 * 
 * Required Library: Adafruit MAX31856
 *   Install via Arduino IDE: Sketch -> Include Library -> Manage Libraries
 *   Search for "Adafruit MAX31856" and install
 */

#include <Adafruit_MAX31856.h>

// Number of sensors
#define NUM_SENSORS 2

// CS pins for each sensor
const int CS_PINS[NUM_SENSORS] = {10, 9};

// Sensor objects
Adafruit_MAX31856 sensors[NUM_SENSORS] = {
  Adafruit_MAX31856(CS_PINS[0]),
  Adafruit_MAX31856(CS_PINS[1])
};

// Reading interval in milliseconds
const unsigned long READ_INTERVAL = 1000;
unsigned long lastReadTime = 0;

// Track which sensors initialized successfully
bool sensorOK[NUM_SENSORS] = {false, false};

void setup() {
  Serial.begin(115200);
  while (!Serial) delay(10);  // Wait for serial on USB-native boards
  
  Serial.println("MAX31856 Thermocouple Sensors (2x)");
  Serial.println("Initializing...");
  
  for (int i = 0; i < NUM_SENSORS; i++) {
    if (!sensors[i].begin()) {
      Serial.print("ERR:Sensor ");
      Serial.print(i + 1);
      Serial.println(" not found. Check wiring!");
      sensorOK[i] = false;
    } else {
      sensorOK[i] = true;
      // Set thermocouple type (default is K-type)
      sensors[i].setThermocoupleType(MAX31856_TCTYPE_K);
      Serial.print("Sensor ");
      Serial.print(i + 1);
      Serial.println(" initialized (Type K)");
    }
  }
  
  Serial.println("--------------------------");
}

void printFaults(int sensorNum, uint8_t fault) {
  Serial.print("FAULT:S");
  Serial.print(sensorNum);
  Serial.print(":");
  if (fault & MAX31856_FAULT_CJRANGE) Serial.print("CJRange ");
  if (fault & MAX31856_FAULT_TCRANGE) Serial.print("TCRange ");
  if (fault & MAX31856_FAULT_CJHIGH)  Serial.print("CJHigh ");
  if (fault & MAX31856_FAULT_CJLOW)   Serial.print("CJLow ");
  if (fault & MAX31856_FAULT_TCHIGH)  Serial.print("TCHigh ");
  if (fault & MAX31856_FAULT_TCLOW)   Serial.print("TCLow ");
  if (fault & MAX31856_FAULT_OVUV)    Serial.print("OVUV ");
  if (fault & MAX31856_FAULT_OPEN)    Serial.print("Open ");
  Serial.println();
}

void loop() {
  unsigned long currentTime = millis();
  
  if (currentTime - lastReadTime >= READ_INTERVAL) {
    lastReadTime = currentTime;
    
    // Output format: S1:temp1,S2:temp2
    // Also includes cold junction temps: S1:tc1:cj1,S2:tc2:cj2
    bool first = true;
    
    for (int i = 0; i < NUM_SENSORS; i++) {
      if (!sensorOK[i]) continue;
      
      // Check for faults
      uint8_t fault = sensors[i].readFault();
      if (fault) {
        printFaults(i + 1, fault);
        continue;
      }
      
      // Read temperatures
      float cjTemp = sensors[i].readCJTemperature();
      float tcTemp = sensors[i].readThermocoupleTemperature();
      
      if (!first) Serial.print(",");
      first = false;
      
      // Format: S1:tcTemp:cjTemp
      Serial.print("S");
      Serial.print(i + 1);
      Serial.print(":");
      Serial.print(tcTemp, 2);
      Serial.print(":");
      Serial.print(cjTemp, 2);
    }
    
    if (!first) Serial.println();
  }
}
