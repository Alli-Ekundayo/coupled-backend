"""Stages 1-2: part planning, grounded netlist generation, deterministic ERC with LLM retry loop."""
from __future__ import annotations
import json
from . import config, llm
from .models import Part

PREFIX = {"Capacitors": "C", "Resistors": "R", "Connector": "J", "LED": "D", "Transistors": "Q"}


def ref_for(part: Part, used: dict[str, int]) -> str:
    p = PREFIX.get(part.category, "U")
    used[p] = used.get(p, 0) + 1
    return f"{p}{used[p]}"


# ---------- deterministic demo design (used when no LLM key): USB-C -> 3.3V LDO -> BME280 + I2C header ----------
DEMO_PLAN = [("J1", "TYPE-C-31-M-17"), ("U1", "AP2112K-3.3TRG1"), ("U2", "BME280"), ("C1", "CL10A106KP8NNNC"),
             ("C2", "CL10A106KP8NNNC"), ("C3", "CL10B104KB8NNNC"), ("R1", "0603WAF5101T5E"),
             ("R2", "0603WAF5101T5E"), ("J2", "PZ254V-11-04P")]
DEMO_WIRING = {  # (ref, pin NAME or number) -> net
    "VBUS": [("J1", "VBUS"), ("U1", "VIN"), ("U1", "EN"), ("C1", "1")],
    "GND": [("J1", "GND"), ("J1", "SHIELD"), ("U1", "GND"), ("C1", "2"), ("C2", "2"), ("C3", "2"), ("U2", "GND"),
            ("U2", "SDO"), ("R1", "2"), ("R2", "2"), ("J2", "2")],
    "3V3": [("U1", "VOUT"), ("C2", "1"), ("C3", "1"), ("U2", "VDD"), ("U2", "VDDIO"), ("U2", "CSB"), ("J2", "1")],
    "SDA": [("U2", "SDI"), ("J2", "3")], "SCL": [("U2", "SCK"), ("J2", "4")],
    "CC1": [("J1", "CC1"), ("R1", "1")], "CC2": [("J1", "CC2"), ("R2", "1")],
}
DEMO_CLASSES = {"VBUS": "power", "GND": "power", "3V3": "power", "SDA": "signal", "SCL": "signal", "CC1": "signal", "CC2": "signal"}
NC_NAMES = {"NC"}


def is_demo_prompt(prompt: str) -> bool:
    p = prompt.lower()
    return any(k in p for k in ("bme280", "usb-c", "usb c", "environment", "sensor"))


def plan_parts(prompt: str, db) -> list[tuple[str, Part]]:
    """-> [(ref, Part)]. LLM picks queries+filters, DB picks verified parts (the LLM never invents MPNs)."""
    plan = llm.complete_json(
        "You are an electronics architect. Decompose the board request into parts. Output "
        '{"parts":[{"ref":"U1","query":"functional description","filters":{"category":"","package":"","v_in_min":0,"value":""}}]}. '
        "Include passives (decoupling caps, pull-ups) and connectors. Target 5-15 routable nets on a 2-layer board "
        f"(<= 15 parts, strictly <= {config.MAX_NETS} nets).", prompt)
    if plan:
        out, seen = [], set()
        for it in plan["parts"]:
            f = {k: v for k, v in it.get("filters", {}).items() if v not in ("", 0, None)}
            f["in_stock"] = True
            hits = db.search(it["query"], f, k=1) or db.search(it["query"], {"in_stock": True}, k=1)
            if hits and it["ref"] not in seen:
                seen.add(it["ref"]); out.append((it["ref"], hits[0]))
        return out
    if not is_demo_prompt(prompt):
        raise ValueError("No GEMINI_API_KEY set: only the built-in USB-C/BME280 demo prompt works offline.")
    return [(r, db.get(m)) for r, m in DEMO_PLAN]


def _resolve(parts: dict[str, Part], ref: str, key: str) -> list[str]:
    pins = parts[ref].pins
    nums = [p.num for p in pins if p.name == key]
    return nums or [p.num for p in pins if p.num == key]


def demo_netlist(parts: dict[str, Part]) -> dict:
    nets = {n: [[r, num] for r, k in pins if r in parts for num in _resolve(parts, r, k)] for n, pins in DEMO_WIRING.items()}
    return {"nets": nets, "net_classes": dict(DEMO_CLASSES)}


def run_erc(netlist: dict, parts: dict[str, Part], max_nets: int | None = None) -> list[str]:
    errs, nets = [], netlist.get("nets", {})
    cap = config.MAX_NETS if max_nets is None else max_nets
    routable = sum(1 for pins in nets.values() if len(pins) >= 2)
    if routable > cap:
        errs.append(f"netlist has {routable} routable nets, exceeding the {cap}-net checkpoint cap")
    ptype = {(r, p.num): p.type for r, pt in parts.items() for p in pt.pins}
    where: dict[tuple, str] = {}
    for n, pins in nets.items():
        for r, num in pins:
            if (r, num) not in ptype:
                errs.append(f"{n}: ({r},{num}) does not exist in grounded parts")
            elif (r, num) in where and where[(r, num)] != n:
                errs.append(f"pin ({r},{num}) is on two nets: {where[(r, num)]} and {n}")
            where[(r, num)] = n
    gnd_nets = {n for n, pins in nets.items() if n.upper() in ("GND", "AGND", "DGND") or any(ptype.get((r, p)) == "ground" for r, p in pins)}
    for n, pins in nets.items():
        if n in gnd_nets and any(ptype.get((r, p)) == "power_out" for r, p in pins):
            errs.append(f"{n}: power_out pin shorted to ground net")
    caps = {r: [where.get((r, p.num)) for p in pt.pins] for r, pt in parts.items() if r.startswith("C")}
    for r, pt in parts.items():
        if not r.startswith("U"):
            continue
        pw = {where[(r, p.num)] for p in pt.pins if p.type == "power_in" and (r, p.num) in where}
        gn = [p for p in pt.pins if p.type == "ground" and (r, p.num) in where]
        if not any(p.type == "power_in" for p in pt.pins) or not pw:
            errs.append(f"{r}: no power pin assigned to a net")
        if not gn:
            errs.append(f"{r}: no ground pin assigned to a net")
        for s in pw:
            if not any(s in cn and any(x in gnd_nets for x in cn if x) for cn in caps.values()):
                errs.append(f"{r}: supply net {s} has no decoupling capacitor to ground")
    return errs


def generate_and_verify_netlist(prompt: str, parts: dict[str, Part], retries: int = 2):
    """-> (netlist, status, warnings). LLM wires pins; ERC failures are fed back verbatim up to `retries` times."""
    table = {
        r: {
            "mpn": p.mpn,
            "value": p.value,
            "category": p.category,
            "description": p.description[:100],
            "pins": [x.model_dump() for x in p.pins],
        }
        for r, p in parts.items()
    }
    sysmsg = ('Wire the given parts into a complete electrical netlist. Use ONLY the exact (ref, pin num) pairs in the table. Output '
              '{"nets":{"NET":[["U1","1"],...]},"net_classes":{"NET":"power|signal|diff_pair"}}. '
              "Rules: Every IC needs power+ground and a decoupling cap between each supply rail and GND. "
              "Never short a power_out pin to GND. Wire USB-C CC1/CC2 via 5.1k pull-downs to GND and connect SHIELD/GND pins to GND. "
              f"Target 5-15 routable nets on 2 layers (strictly <= {config.MAX_NETS} nets).")
    feedback, nl, errs = "", None, []
    for attempt in range(retries + 1):
        raw = None
        if llm.available():
            try:
                raw = llm.complete_json(sysmsg, f"Request: {prompt}\nParts: {json.dumps(table)}{feedback}")
            except Exception as exc:
                errs = [f"LLM call error: {exc}"]
                feedback = f"\nPrevious attempt raised an error ({exc}). Return a clean JSON netlist."
                continue
        if raw is None:
            nl = demo_netlist(parts)
        else:
            # Normalize refs & pin numbers to strings so integer pins like ["U1", 1] never fail ERC
            raw_nets = raw.get("nets") if isinstance(raw, dict) else {}
            norm_nets = {
                str(n): [[str(pair[0]), str(pair[1])] for pair in pins if isinstance(pair, (list, tuple)) and len(pair) >= 2]
                for n, pins in (raw_nets or {}).items()
            }
            nl = {"nets": norm_nets, "net_classes": dict((raw.get("net_classes") or {}) if isinstance(raw, dict) else {})}
        errs = run_erc(nl, parts)
        if not errs:
            return nl, "PASS", []
        if not llm.available():
            break
        feedback = "\nPrevious attempt failed ERC. Fix exactly these errors:\n" + "\n".join(errs)
    return nl or {"nets": {}, "net_classes": {}}, "FAIL", errs
