"""Seed parts with verified LCSC IDs, real KiCad library footprints, and datasheet pin tables.
All LCSC part numbers verified against LCSC product catalog."""
from .models import Part, Pin


def P(*rows):
    return [Pin(num=n, name=nm, type=t) for n, nm, t in rows]


def passive2():
    return P(("1", "1", "passive"), ("2", "2", "passive"))


SEED: list[Part] = [
    Part(
        mpn="TYPE-C-31-M-17", lcsc_id="C283540", category="Connector", package="USBC-6P", value="USB-C 6P",
        description="USB Type-C 6-pin power-only receptacle VBUS CC1 CC2 GND SHIELD SMD",
        manufacturer="Korean Hroparts Elec", voltage_max=20.0, stock=22625, price=0.20,
        footprint_ref="Connector_USB:USB_C_Receptacle_GCT_USB4125-xx-x_6P_TopMnt_Horizontal",
        pins=P(
            ("A5", "CC1", "bidir"), ("B5", "CC2", "bidir"),
            ("A9", "VBUS", "power_out"), ("B9", "VBUS", "power_out"),
            ("A12", "GND", "ground"), ("B12", "GND", "ground"),
            ("S1", "SHIELD", "ground"),
        ),
    ),
    Part(
        mpn="AP2112K-3.3TRG1", lcsc_id="C51118", category="Power Management", package="SOT-23-5", value="3.3V",
        description="LDO linear regulator 3.3V 600mA low noise low quiescent current fixed output",
        manufacturer="Diodes Inc", voltage_max=6.0, stock=20090, price=0.12,
        footprint_ref="Package_TO_SOT_SMD:SOT-23-5",
        pins=P(("1", "VIN", "power_in"), ("2", "GND", "ground"), ("3", "EN", "input"), ("4", "NC", "nc"), ("5", "VOUT", "power_out")),
    ),
    Part(
        mpn="BME280", lcsc_id="C92489", category="Sensors", package="LGA-8", value="BME280",
        description="digital humidity pressure temperature environmental sensor I2C SPI 3.3V",
        manufacturer="Bosch", voltage_max=3.6, stock=8000, price=3.10,
        footprint_ref="Package_LGA:Bosch_LGA-8_2.5x2.5mm_P0.65mm_ClockwisePinNumbering",
        pins=P(
            ("1", "GND", "ground"), ("2", "CSB", "input"), ("3", "SDI", "bidir"), ("4", "SCK", "input"),
            ("5", "SDO", "bidir"), ("6", "VDDIO", "power_in"), ("7", "GND", "ground"), ("8", "VDD", "power_in"),
        ),
    ),
    Part(
        mpn="CL10A106KP8NNNC", lcsc_id="C19702", category="Capacitors", package="0603", value="10uF",
        description="MLCC ceramic capacitor 10uF 10V X5R 0603 bulk decoupling",
        manufacturer="Samsung Electro-Mechanics", voltage_max=10.0, stock=338200, price=0.01,
        footprint_ref="Capacitor_SMD:C_0603_1608Metric", pins=passive2(),
    ),
    Part(
        mpn="CC0603KRX7R9BB104", lcsc_id="C14663", category="Capacitors", package="0603", value="100nF",
        description="MLCC ceramic capacitor 100nF 50V X7R 0603 decoupling bypass",
        manufacturer="YAGEO", voltage_max=50.0, stock=2000000, price=0.004,
        footprint_ref="Capacitor_SMD:C_0603_1608Metric", pins=passive2(),
    ),
    Part(
        mpn="CL10B104KB8NNNC", lcsc_id="C1591", category="Capacitors", package="0603", value="100nF",
        description="MLCC ceramic capacitor 100nF 50V X7R 0603 decoupling bypass high stock",
        manufacturer="Samsung Electro-Mechanics", voltage_max=50.0, stock=1976700, price=0.0064,
        footprint_ref="Capacitor_SMD:C_0603_1608Metric", pins=passive2(),
    ),
    Part(
        mpn="0603WAF5101T5E", lcsc_id="C23186", category="Resistors", package="0603", value="5.1k",
        description="resistor 5.1k ohm 1% 0603 USB-C CC pull-down",
        manufacturer="UNI-ROYAL", voltage_max=75.0, stock=12955100, price=0.002,
        footprint_ref="Resistor_SMD:R_0603_1608Metric", pins=passive2(),
    ),
    Part(
        mpn="0603WAF4701T5E", lcsc_id="C23162", category="Resistors", package="0603", value="4.7k",
        description="resistor 4.7k ohm 1% 0603 I2C pull-up",
        manufacturer="UNI-ROYAL", voltage_max=75.0, stock=6173400, price=0.002,
        footprint_ref="Resistor_SMD:R_0603_1608Metric", pins=passive2(),
    ),
    Part(
        mpn="PZ254V-11-04P", lcsc_id="C492403", category="Connector", package="HDR-1x04", value="1x4 header",
        description="pin header 1x4 2.54mm vertical through-hole breakout connector",
        manufacturer="XFCN", voltage_max=250.0, stock=49620, price=0.028,
        footprint_ref="Connector_PinHeader_2.54mm:PinHeader_1x04_P2.54mm_Vertical",
        pins=P(("1", "1", "passive"), ("2", "2", "passive"), ("3", "3", "passive"), ("4", "4", "passive")),
    ),
    Part(
        mpn="KT-0603G", lcsc_id="C12624", category="LED", package="0603", value="green",
        description="LED emerald green 525nm 0603 indicator",
        manufacturer="Hubei KENTO Elec", voltage_max=5.0, stock=187300, price=0.012,
        footprint_ref="LED_SMD:LED_0603_1608Metric",
        pins=P(("1", "K", "passive"), ("2", "A", "passive")),
    ),
    Part(
        mpn="2N7002", lcsc_id="C8545", category="Transistors", package="SOT-23", value="N-MOSFET 60V",
        description="N-channel MOSFET 60V 115mA logic level switch SOT-23",
        manufacturer="JSCJ", voltage_max=60.0, stock=5451600, price=0.016,
        footprint_ref="Package_TO_SOT_SMD:SOT-23",
        pins=P(("1", "G", "input"), ("2", "S", "passive"), ("3", "D", "passive")),
    ),
]
