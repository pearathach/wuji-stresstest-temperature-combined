"""
Temperature Reading Script for Adafruit MAX31856 + Arduino Uno R4 Minima (2 Sensors)
With WebSocket server for live data streaming.

Wiring (all sensors share SPI bus, each has unique CS pin):
  MAX31856     Sensor 1    Sensor 2
  --------     --------    --------
  VIN       -> 5V          5V
  GND       -> GND         GND
  SCK       -> Pin 13      Pin 13
  SDO       -> Pin 12      Pin 12
  SDI       -> Pin 11      Pin 11
  CS        -> Pin 10      Pin 9

Upload the accompanying Arduino sketch (max31856_temperature.ino) to the board first.

WebSocket server runs on ws://localhost:8765
Install dependencies: pip install pyserial websockets
"""

import serial
import time
import json
import asyncio
import webbrowser
import os
import serial.tools.list_ports
import websockets
from datetime import datetime

# Get directory where this script is located
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DASHBOARD_PATH = os.path.join(SCRIPT_DIR, "temperature_dashboard.html")

# Arduino Uno R4 Minima typically appears as /dev/ttyACM2 on Linux
PORT = "/dev/ttyACM1"
BAUD = 115200

# WebSocket server settings
WS_HOST = "0.0.0.0"
WS_PORT = 8765

# Connected WebSocket clients
connected_clients = set()


def find_arduino_port():
    """Attempt to auto-detect Arduino Uno R4 Minima port."""
    ports = serial.tools.list_ports.comports()
    for port in ports:
        # Arduino Uno R4 Minima uses Renesas RA4M1 chip
        if "Arduino" in port.description or "ACM" in port.device:
            return port.device
    return None


def parse_multi_sensor_line(line):
    """
    Parse multi-sensor data format: S1:tcTemp:cjTemp,S2:tcTemp:cjTemp
    Returns list of dicts for JSON serialization.
    """
    readings = []
    parts = line.split(",")
    for part in parts:
        try:
            # Format: S1:tcTemp:cjTemp
            sensor_data = part.split(":")
            if len(sensor_data) >= 3 and sensor_data[0].startswith("S"):
                sensor_num = int(sensor_data[0][1:])
                tc_temp = float(sensor_data[1])
                cj_temp = float(sensor_data[2])
                readings.append({
                    "sensor": sensor_num,
                    "thermocouple_c": round(tc_temp, 2),
                    "thermocouple_f": round(tc_temp * 9/5 + 32, 2),
                    "cold_junction_c": round(cj_temp, 2)
                })
        except (ValueError, IndexError):
            continue
    return readings


async def websocket_handler(websocket):
    """Handle new WebSocket connections."""
    connected_clients.add(websocket)
    client_addr = websocket.remote_address
    print(f"[WS] Client connected: {client_addr}")
    try:
        await websocket.wait_closed()
    finally:
        connected_clients.discard(websocket)
        print(f"[WS] Client disconnected: {client_addr}")


async def broadcast_data(data):
    """Broadcast data to all connected WebSocket clients."""
    if connected_clients:
        message = json.dumps(data)
        await asyncio.gather(
            *[client.send(message) for client in connected_clients],
            return_exceptions=True
        )


async def read_serial_data(ser):
    """Read data from Arduino serial port and broadcast via WebSocket."""
    loop = asyncio.get_event_loop()
    
    print("Connected to Arduino Uno R4 Minima. Reading MAX31856 temperatures (2 sensors)...\n")
    print("-" * 70)
    print(f"{'Sensor':<10} {'Thermocouple':>20} {'Cold Junction':>20}")
    print("-" * 70)
    
    while True:
        # Read serial in a thread to avoid blocking
        line = await loop.run_in_executor(
            None, 
            lambda: ser.readline().decode("utf-8", errors="replace").strip()
        )
        
        if not line:
            await asyncio.sleep(0.01)
            continue
        
        # Handle error messages from Arduino
        if line.startswith("ERR") or line.startswith("FAULT"):
            print(f"Sensor error: {line}")
            await broadcast_data({
                "type": "error",
                "message": line,
                "timestamp": datetime.now().isoformat()
            })
            continue
        
        # Handle initialization messages
        if line.startswith("MAX31856") or line.startswith("Init") or line.startswith("Sensor") or line.startswith("-"):
            print(f"[Arduino] {line}")
            continue
        
        # Parse multi-sensor temperature data
        readings = parse_multi_sensor_line(line)
        
        if readings:
            # Print to console
            for reading in readings:
                print(f"Sensor {reading['sensor']:<3} {reading['thermocouple_c']:>8.2f} °C ({reading['thermocouple_f']:>6.2f} °F)    {reading['cold_junction_c']:>8.2f} °C")
            print("-" * 70)
            
            # Broadcast via WebSocket
            await broadcast_data({
                "type": "temperature",
                "sensors": readings,
                "timestamp": datetime.now().isoformat()
            })


async def main():
    global PORT
    
    # Try auto-detection if default port doesn't exist
    try:
        ser = serial.Serial(PORT, BAUD, timeout=1)
    except serial.SerialException:
        detected_port = find_arduino_port()
        if detected_port:
            print(f"Auto-detected Arduino on {detected_port}")
            PORT = detected_port
            ser = serial.Serial(PORT, BAUD, timeout=1)
        else:
            raise SystemExit(
                f"Failed to open serial port {PORT}.\n"
                "Available ports: " + 
                ", ".join([p.device for p in serial.tools.list_ports.comports()])
            )
    
    time.sleep(2)  # Arduino reset delay
    
    # Start WebSocket server
    print(f"\n[WS] WebSocket server starting on ws://{WS_HOST}:{WS_PORT}")
    print("[WS] Connect clients to receive live temperature data\n")
    
    async with websockets.serve(websocket_handler, WS_HOST, WS_PORT):
        # Open dashboard in browser
        dashboard_url = f"file://{DASHBOARD_PATH}"
        print(f"[Browser] Opening dashboard: {dashboard_url}")
        webbrowser.open(dashboard_url)
        
        await read_serial_data(ser)


if __name__ == "__main__":
    asyncio.run(main())