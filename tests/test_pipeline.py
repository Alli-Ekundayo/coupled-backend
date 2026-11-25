import json, os, tempfile, time
os.environ["DATA_DIR"] = tempfile.mkdtemp()
os.environ["ROUTER_ENGINE"] = "mock"
os.environ["GEMINI_API_KEY"] = ""
os.environ["ANTHROPIC_API_KEY"] = ""
os.environ["NEXAR_ACCESS_TOKEN"] = ""
os.environ["DATABASE_URL"] = ""
os.environ["DATABASE_URL_UNPOOLED"] = ""
os.environ["NEON_AUTH_BASE_URL"] = ""
os.environ["NEON_AUTH_JWKS_URL"] = ""
os.environ["AUTH_REQUIRED"] = "false"
from fastapi.testclient import TestClient
from coupled_backend.api import app
from coupled_backend import sexpr as sx
from coupled_backend.kicad_io import parse_board


def test_end_to_end():
    with TestClient(app) as c:
        assert c.get("/health").json()["parts"] >= 10
        r = c.post("/api/v1/parts/search", json={"query": "3.3V low noise LDO", "filters": {"v_in_min": 5, "in_stock": True}}).json()
        assert r["results"][0]["mpn"] == "AP2112K-3.3TRG1"

        n = c.post("/api/v1/projects/demo/netlist", json={"prompt": "USB-C to 3.3V BME280 sensor board"}).json()
        assert n["erc_status"] == "PASS" and len(n["netlist"]["nets"]) == 7

        p = c.post("/api/v1/projects/demo/place", data={"netlist_id": "demo", "iterations": 2500}).json()
        assert p["overlaps"] == 0 and p["hpwl_mm"] > 0 and len(p["placements"]) == 9
        txt = c.get("/api/v1/projects/demo/export?format=kicad_pcb").text
        tree, comps, nets, outline = parse_board(txt)
        assert len(comps) == 9 and outline == (0.0, 0.0, 40.0, 30.0) and len(nets) == 7

        jid = c.post("/api/v1/projects/demo/route", json={"max_steps": 100}).json()["job_id"]
        evs = []
        with c.stream("GET", f"/api/v1/jobs/{jid}/stream") as s:
            for line in s.iter_lines():
                if line.startswith("data:"):
                    evs.append(json.loads(line[5:]))
        assert evs[-1]["type"] == "done", evs[-1]
        assert evs[-2]["connected_nets"] == evs[-2]["total_nets"] == 7
        routed = c.get("/api/v1/projects/demo/export?format=kicad_pcb").text
        assert "(segment" in routed and sx.parse(routed)
        assert c.get("/api/v1/projects/demo/export?format=svg").text.startswith("<svg")
        assert "LCSC" in c.get("/api/v1/projects/demo/export?format=bom").text


def test_scraped_board_replace_and_strip():
    with TestClient(app) as c:
        c.post("/api/v1/projects/s1/netlist", json={"prompt": "USB-C BME280"})
        c.post("/api/v1/projects/s1/place", data={"netlist_id": "s1", "iterations": 500})
        board = c.get("/api/v1/projects/s1/export?format=kicad_pcb").text
        # fake a "scraped" board: add copper that must be stripped
        dirty = board.rstrip().rstrip(")") + '\n(segment (start 1 1) (end 5 5) (width 0.25) (layer "F.Cu") (net 1) (uuid "x"))\n)\n'
        r = c.post("/api/v1/projects/s2/place", files={"board": ("b.kicad_pcb", dirty)}, data={"iterations": 1500}).json()
        assert r["mode"] == "scraped" and r["overlaps"] == 0
        out = c.get("/api/v1/projects/s2/export?format=kicad_pcb").text
        assert "(segment" not in out


def test_gemini_llm_json():
    from unittest.mock import MagicMock, patch
    from coupled_backend import llm, config

    with patch.object(config, "GEMINI_API_KEY", "mock-gemini-key"), \
         patch("google.genai.Client") as mock_client:
        mock_instance = mock_client.return_value
        mock_instance.models.generate_content.return_value = MagicMock(
            text='```json\n{"parts": [{"ref": "U1"}]}\n```'
        )
        assert llm.available()
        res = llm.complete_json("system instructions", "user prompt")
        assert res == {"parts": [{"ref": "U1"}]}


def test_octopart_integration():
    from unittest.mock import MagicMock, patch
    from coupled_backend import octopart, config

    mock_resp_data = {
        "data": {
            "supSearch": {
                "results": [
                    {
                        "part": {
                            "mpn": "AP2112K-3.3TRG1",
                            "name": "Diodes AP2112",
                            "shortDescription": "3.3V LDO regulator SOT-23-5",
                            "manufacturer": {"name": "Diodes Inc."},
                            "category": {"name": "LDO Voltage Regulators"},
                            "totalAvail": 50000,
                            "medianPrice1000": {"price": 0.18, "currency": "USD"},
                            "octopartUrl": "https://octopart.com/part/diodes-inc/AP2112K-3.3TRG1",
                            "specs": [
                                {"attribute": {"shortname": "case_package"}, "displayValue": "SOT-23-5"},
                                {"attribute": {"shortname": "maxinputvoltage"}, "displayValue": "6 V"},
                            ],
                        }
                    }
                ]
            }
        }
    }

    with patch.object(config, "NEXAR_ACCESS_TOKEN", "mock-nexar-token"), \
         patch("httpx.Client.post") as mock_post:
        mock_post.return_value = MagicMock(status_code=200, json=lambda: mock_resp_data)
        assert octopart.available()
        parts = octopart.search_octopart("AP2112", limit=1)
        assert len(parts) == 1
        p = parts[0]
        assert p.mpn == "AP2112K-3.3TRG1"
        assert "SOT-23-5" in p.footprint_ref
        assert len(p.pins) == 5
        assert p.voltage_max == 6.0
        assert p.price == 0.18
        assert p.stock == 50000
        assert "octopart.com" in p.octopart_url


def test_pcbworld_policy_and_route():
    """Gap 1: verify _load_policy (path & dict blob) and _pcbworld_route against the real PCBWorld API."""
    import sys
    import types
    from pathlib import Path
    from unittest.mock import MagicMock, patch
    from coupled_backend import config, engines
    from coupled_backend.kicad_io import make_comp, write_unrouted_kicad_pcb
    from coupled_backend.seed import SEED

    # Build a small valid board file
    by_mpn = {p.mpn: p for p in SEED}
    c1 = make_comp("R1", "5.1k", by_mpn["0603WAF5101T5E"].footprint_ref)
    c2 = make_comp("R2", "4.7k", by_mpn["0603WAF4701T5E"].footprint_ref)
    pcb_txt = write_unrouted_kicad_pcb([c1, c2], {"R1": (10.0, 10.0, 0.0), "R2": (20.0, 10.0, 0.0)},
                                       {"SIG": [("R1", "1"), ("R2", "1")], "GND": [("R1", "2"), ("R2", "2")]},
                                       (0.0, 0.0, 30.0, 20.0))
    tmp = Path(tempfile.mkdtemp())
    board_path = tmp / "board.kicad_pcb"
    board_path.write_text(pcb_txt)
    ckpt_dir = tmp / "d2" / "ppo"
    ckpt_dir.mkdir(parents=True)
    ckpt_file = ckpt_dir / "policy_iter_100.pt"
    ckpt_file.write_bytes(b"mock-ckpt")

    # Build lightweight fake PCBWorld module hierarchy matching LGAI-Research/PCBWorld contracts
    fake_model = MagicMock()
    fake_model.policy_net_select = False
    fake_model.ckpt_sources = {"max_steps": "ckpt"}
    fake_model.load_state_dict.return_value = ([], [])

    class FakeDist:
        def to(self, dev):
            return self

    class FakeAgent:
        def __init__(self, model, device="cpu", deterministic=True):
            self.model = model
            self.device = device
            self.deterministic = deterministic
            self.ckpt_args = {"emit_drc_tokens": False, "max_steps": 50, "net_constraint_obs": False}
            self.train_iteration = 100

        @classmethod
        def from_checkpoint(cls, path, device="auto", *, strict=False, deterministic=True):
            assert Path(path) == ckpt_file
            return cls(fake_model, device="cpu", deterministic=deterministic)

        def act(self, obs_list, *, dist, deterministic=True):
            return ([[1, 2, 0]], [0.0])

    saved_paths = []
    closed = []

    class FakeWrapperEnv:
        def __init__(self):
            self._step = 0

        def reset(self, seed=0):
            return {"board_static": {}}, {"initial_unconnected": 2, "unrouted_count": 2}

        def step(self, action):
            self._step += 1
            done = self._step >= 2
            return ({"board_static": {}}, 1.0, done, False,
                    {"initial_unconnected": 2, "unrouted_count": 2 - self._step, "drc_violations": 0, "ratsnest_reduction": self._step / 2})

        def save_pcb(self, out_path):
            saved_paths.append(out_path)
            Path(out_path).write_text(pcb_txt)
            return out_path

        def close(self):
            closed.append(True)

    # Wire fake modules into sys.modules
    mod_agent = types.ModuleType("methods.rl_agent.policy.agent")
    mod_agent.KiCadRLAgent = FakeAgent
    mod_agent.gather_dist = lambda envs, idxs, *, policy_net_select, obs_list: FakeDist()

    mod_loader = types.ModuleType("methods.rl_agent.models.loader")
    mod_loader.pad_legacy_entity_type_rows = lambda sd, m: None

    mod_utils = types.ModuleType("methods.rl_agent.training.utils")
    mod_utils.auto_device = lambda d: "cpu"

    mod_ckpt = types.ModuleType("configs.loader.ckpt")
    mod_ckpt.load_ckpt_config = lambda blob, *, strict=False: ({"net_constraint_obs": False, "emit_drc_tokens": False, "max_steps": 50}, {})

    mod_schema = types.ModuleType("configs.loader.schema")
    mod_schema.RLPolicyConfig = MagicMock()
    mod_schema.RLPolicyConfig.from_checkpoint.return_value.build.return_value = fake_model
    mod_schema.RLEnvConfig = MagicMock()
    mod_schema.RLEnvConfig.from_checkpoint.return_value = MagicMock()

    mod_resolve = types.ModuleType("configs.loader.resolve")
    mod_resolve.Layer = lambda name, vals, where=None: (name, vals)

    mod_eval_rl = types.ModuleType("eval.rollout.rl")
    mod_eval_rl.resolve_eval_env = lambda base, **kw: MagicMock()
    mod_eval_rl.eval_env_config = lambda run: MagicMock()
    mod_eval_rl.check_policy_env = lambda model, cfg: None

    mod_tf = types.ModuleType("methods.rl_agent.rollout.transformer")
    mod_tf.max_net_slots_for_policy = lambda m: 64

    mod_fac = types.ModuleType("methods.rl_agent.wrappers.factory")
    mod_fac.make_decoder_env = lambda bp, cfg, *, seed, policy_net_select: FakeWrapperEnv()

    fake_mods = {
        "methods": types.ModuleType("methods"),
        "methods.rl_agent": types.ModuleType("methods.rl_agent"),
        "methods.rl_agent.policy": types.ModuleType("methods.rl_agent.policy"),
        "methods.rl_agent.policy.agent": mod_agent,
        "methods.rl_agent.models": types.ModuleType("methods.rl_agent.models"),
        "methods.rl_agent.models.loader": mod_loader,
        "methods.rl_agent.training": types.ModuleType("methods.rl_agent.training"),
        "methods.rl_agent.training.utils": mod_utils,
        "methods.rl_agent.rollout": types.ModuleType("methods.rl_agent.rollout"),
        "methods.rl_agent.rollout.transformer": mod_tf,
        "methods.rl_agent.wrappers": types.ModuleType("methods.rl_agent.wrappers"),
        "methods.rl_agent.wrappers.factory": mod_fac,
        "configs": types.ModuleType("configs"),
        "configs.loader": types.ModuleType("configs.loader"),
        "configs.loader.ckpt": mod_ckpt,
        "configs.loader.schema": mod_schema,
        "configs.loader.resolve": mod_resolve,
        "eval": types.ModuleType("eval"),
        "eval.rollout": types.ModuleType("eval.rollout"),
        "eval.rollout.rl": mod_eval_rl,
    }

    with patch.dict(sys.modules, fake_mods), patch.object(config, "CHECKPOINT_DIR", tmp):
        # 1. Test _load_policy with a raw dict blob
        blob_agent = engines._load_policy({"policy_state_dict": {}, "args": {"max_steps": 50}, "iteration": 42})
        assert blob_agent.train_iteration == 42
        fake_model.eval.assert_called()

        # 2. Test _pcbworld_route full episode + save_pcb + close
        events = []
        res = engines._pcbworld_route(str(board_path), "d2/ppo", max_steps=10, emit=events.append)
        assert res["engine"] == "pcbworld"
        assert len(events) == 2
        assert events[-1]["connected_nets"] == 2 and events[-1]["unrouted_count"] == 0
        assert len(saved_paths) == 1 and saved_paths[0].endswith("_routed.kicad_pcb")
        assert closed == [True]


def test_kicad_mod_footprint_inlining():
    """Gap 2: real .kicad_mod inlining (pads, rotations, oval drills, graphics) + procedural fallback."""
    from pathlib import Path
    from unittest.mock import patch
    from coupled_backend import config, libraries
    from coupled_backend.kicad_io import footprint_pads, make_comp, write_unrouted_kicad_pcb

    # USB-C 6P real footprint has A5/B5/A9/B9/A12/B12 + S1 oval thru-hole shield pads
    usbc = make_comp("J1", "USB-C 6P", "USBC-6P")
    pad_nums = {p.num for p in usbc.pads}
    assert {"A5", "B5", "A9", "B9", "A12", "B12", "S1"}.issubset(pad_nums)
    assert any(p.drill_y > 0 for p in usbc.pads if p.num == "S1")

    # BME280 LGA-8 pads carry 90-degree local rotation in Bosch .kicad_mod
    bme = make_comp("U2", "BME280", "LGA-8")
    assert len(bme.pads) == 8
    pcb = write_unrouted_kicad_pcb([usbc, bme], {"J1": (10.0, 10.0, 0.0), "U2": (25.0, 15.0, 90.0)},
                                   {"GND": [("J1", "A12"), ("U2", "1")]}, (0.0, 0.0, 40.0, 30.0))
    assert "(drill oval " in pcb
    assert "(roundrect_rratio " in pcb

    # Procedural fallback works even when KiCad footprint dir does not exist
    libraries.load_footprint.cache_clear()
    with patch.object(config, "KICAD_FOOTPRINTS_DIR", Path("/nonexistent/kicad/footprints")):
        pads = footprint_pads("SOT-23-5")
        assert len(pads) == 5
    libraries.load_footprint.cache_clear()


def test_jlc_csv_and_kicad_symbol_pin_enrichment():
    """Gap 3: KiCad .kicad_sym pin extraction (incl. extends) and JLC CSV pin/footprint enrichment."""
    from pathlib import Path
    from coupled_backend import config, ingest, libraries

    # Verify KiCad symbol enrichment when local KiCad symbols are installed
    if config.KICAD_SYMBOLS_DIR.is_dir():
        pins, fp = libraries.lookup_kicad_symbol("AP2112K-3.3TRG1")
        assert len(pins) == 5 and "SOT-23-5" in fp
        bme_pins, bme_fp = libraries.lookup_kicad_symbol("BME280")
        assert len(bme_pins) == 8 and "LGA-8" in bme_fp

    # Test JLC CSV ingestion attaches pins & footprints automatically
    csv_path = Path(tempfile.mkdtemp()) / "jlc.csv"
    csv_path.write_text(
        "LCSC Part,MFR.Part,Package,First Category,Description,Manufacturer,Price,Stock\n"
        "C51118,AP2112K-3.3TRG1,SOT-25-5,Voltage Regulators,3.3V 600mA LDO 6V,DIODES,0.12,20000\n"
        "C25804,RC0603FR-0710KL,0603,Resistors,10kΩ ±1% 75V 0603,YAGEO,0.002,500000\n"
        "C492403,PZ254V-11-04P,Through Hole,Connector,Pin Header 4 Position 2.54mm,XFCN,0.03,40000\n",
        encoding="utf-8",
    )
    parts = ingest.load_jlc_csv(str(csv_path), enrich=True)
    assert len(parts) == 3
    by_mpn = {p.mpn: p for p in parts}
    assert len(by_mpn["AP2112K-3.3TRG1"].pins) == 5 and by_mpn["AP2112K-3.3TRG1"].footprint_ref
    assert len(by_mpn["RC0603FR-0710KL"].pins) == 2 and "R_0603" in by_mpn["RC0603FR-0710KL"].footprint_ref
    assert len(by_mpn["PZ254V-11-04P"].pins) == 4 and "1x04" in by_mpn["PZ254V-11-04P"].footprint_ref


def test_seed_parts_verified_lcsc_and_drc():
    """Gaps 4 & 5: all seed parts have verified LCSC IDs, real footprints, and pass DRC."""
    from coupled_backend.seed import SEED

    for p in SEED:
        assert p.lcsc_id.startswith("C") and p.lcsc_id[1:].isdigit(), f"missing/invalid LCSC ID on {p.mpn}"
        assert ":" in p.footprint_ref, f"expected real KiCad Library:Footprint ref on {p.mpn}"
        assert "DEMO" not in p.mpn

    with TestClient(app) as c:
        c.post("/api/v1/projects/drc_demo/netlist", json={"prompt": "USB-C to 3.3V BME280 sensor board"})
        c.post("/api/v1/projects/drc_demo/place", data={"netlist_id": "drc_demo", "iterations": 2000})
        bom = c.get("/api/v1/projects/drc_demo/export?format=bom").text
        assert "C283540" in bom and "C51118" in bom and "C92489" in bom and "C492403" in bom
        drc = c.get("/api/v1/projects/drc_demo/drc").json()
        assert drc["passed"] is True and drc["error_count"] == 0


def test_scraped_board_preserves_zones_and_nonrect_outline():
    """Gap 6: scraped board preserves GND pour zones (stripping stale filled_polygon) & respects L-shaped Edge.Cuts."""
    from coupled_backend.kicad_io import parse_board_outline
    from coupled_backend.placer import _point_in_polygon

    # Construct an L-shaped board: 40x40 minus the top-right 20x20 notch [(20..40), (0..20)]
    l_edges = [
        ((0, 0), (20, 0)), ((20, 0), (20, 20)), ((20, 20), (40, 20)),
        ((40, 20), (40, 40)), ((40, 40), (0, 40)), ((0, 40), (0, 0)),
    ]
    edge_sexpr = "\n".join(
        f'  (gr_line (start {a[0]} {a[1]}) (end {b[0]} {b[1]}) (stroke (width 0.1) (type solid)) (layer "Edge.Cuts") (uuid "e{i}"))'
        for i, (a, b) in enumerate(l_edges)
    )
    zone_sexpr = '''  (zone (net 1) (net_name "GND") (layer "B.Cu") (uuid "z1")
    (polygon (pts (xy 0 0) (xy 20 0) (xy 20 20) (xy 40 20) (xy 40 40) (xy 0 40)))
    (filled_polygon (layer "B.Cu") (pts (xy 1 1) (xy 19 1) (xy 19 19) (xy 1 19)))
  )'''
    fps = "\n".join(
        f'''  (footprint "Resistor_SMD:R_0603_1608Metric" (layer "F.Cu") (uuid "f{i}") (at {5 + i * 3} 30)
    (property "Reference" "R{i}" (at 0 -1.5 0) (layer "F.SilkS") (uuid "pr{i}") (effects (font (size 1 1) (thickness 0.15))))
    (property "Value" "10k" (at 0 1.5 0) (layer "F.Fab") (uuid "pv{i}") (effects (font (size 1 1) (thickness 0.15))))
    (pad "1" smd roundrect (at -0.825 0) (size 0.8 0.95) (layers "F.Cu" "F.Paste" "F.Mask") (net 1 "GND") (uuid "p1_{i}"))
    (pad "2" smd roundrect (at 0.825 0) (size 0.8 0.95) (layers "F.Cu" "F.Paste" "F.Mask") (net 2 "SIG") (uuid "p2_{i}"))
  )'''
        for i in range(1, 5)
    )
    scraped = f'''(kicad_pcb (version 20241229) (generator "test")
  (layers (0 "F.Cu" signal) (2 "B.Cu" signal) (25 "Edge.Cuts" user))
  (net 0 "") (net 1 "GND") (net 2 "SIG")
{fps}
{edge_sexpr}
{zone_sexpr}
  (segment (start 5 30) (end 10 30) (width 0.25) (layer "F.Cu") (net 1) (uuid "seg1"))
)'''

    with TestClient(app) as c:
        r = c.post("/api/v1/projects/lshape/place", files={"board": ("l.kicad_pcb", scraped)}, data={"iterations": 1500}).json()
        assert r["mode"] == "scraped" and r["overlaps"] == 0 and r["zones_preserved"] == 1
        out = c.get("/api/v1/projects/lshape/export?format=kicad_pcb").text
        assert "(segment" not in out
        assert "(filled_polygon" not in out
        assert "(zone" in out  # ground pour zone preserved!

        bo = parse_board_outline(sx.parse(out))
        assert bo is not None and len(bo.outer) == 6
        for pl in r["placements"]:
            assert _point_in_polygon(pl["x"], pl["y"], bo.outer), f"{pl} placed outside L-shaped Edge.Cuts polygon!"


def test_64_net_cap_enforced():
    """Gap 7: enforce PCBWorld 64-net checkpoint cap in ERC, check_board_limits, and /route API."""
    from coupled_backend.models import Part, Pin
    from coupled_backend.netlist import run_erc
    from coupled_backend.store import Project

    # 1. ERC rejects > 64 routable nets
    parts = {
        "J1": Part(mpn="HDR70", pins=[Pin(num=str(i), name=str(i), type="passive") for i in range(1, 71)]),
        "J2": Part(mpn="HDR70", pins=[Pin(num=str(i), name=str(i), type="passive") for i in range(1, 71)]),
    }
    big_nl = {"nets": {f"N{i}": [["J1", str(i)], ["J2", str(i)]] for i in range(1, 66)}}
    errs = run_erc(big_nl, parts)
    assert any("64-net" in e for e in errs)

    # 2. /route endpoint returns 422 on board with > 64 routable nets
    net_decls = "\n".join(f'  (net {i} "N{i}")' for i in range(1, 66))
    pads1 = "\n".join(f'    (pad "{i}" smd rect (at {i * 0.5} 0) (size 0.3 0.3) (layers "F.Cu") (net {i} "N{i}"))' for i in range(1, 66))
    pads2 = "\n".join(f'    (pad "{i}" smd rect (at {i * 0.5} 0) (size 0.3 0.3) (layers "F.Cu") (net {i} "N{i}"))' for i in range(1, 66))
    big_board = f'''(kicad_pcb (version 20241229) (generator "test")
  (layers (0 "F.Cu" signal) (2 "B.Cu" signal) (25 "Edge.Cuts" user))
  (net 0 "")
{net_decls}
  (footprint "test:A" (layer "F.Cu") (at 5 5) (property "Reference" "J1") (property "Value" "A")
{pads1}
  )
  (footprint "test:B" (layer "F.Cu") (at 5 15) (property "Reference" "J2") (property "Value" "B")
{pads2}
  )
)'''
    with TestClient(app) as c:
        prj = Project("too_many_nets")
        prj.path("board.kicad_pcb").write_text(big_board)
        resp = c.post("/api/v1/projects/too_many_nets/route", json={"max_steps": 10})
        assert resp.status_code == 422
        assert "64-net" in resp.json()["detail"]


def test_design_endpoint_end_to_end_and_schematic_exports():
    """One-shot POST /api/v1/design orchestrates requirements -> parts -> schematic -> place -> route -> DRC."""
    with TestClient(app) as c:
        resp = c.post("/api/v1/design", json={
            "prompt": "USB-C powered 3.3V BME280 environmental sensor board with I2C header",
            "project_id": "ai_bme280",
            "iterations": 1500,
            "max_steps": 100,
        })
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["project_id"] == "ai_bme280"
        assert data["erc_status"] == "PASS"
        assert data["placement_metrics"]["overlaps"] == 0
        assert data["drc"]["passed"] is True and data["drc"]["error_count"] == 0
        assert data["routing"]["engine"] == "mock"
        assert data["schematic_svg"].startswith("<svg")
        assert data["pcb_svg"].startswith("<svg")
        assert len(data["components"]) == 9
        assert data["review"]

        # Verify KiCad 9 schematic (.kicad_sch) and schematic SVG exports
        sch = c.get("/api/v1/projects/ai_bme280/export?format=kicad_sch").text
        assert "(kicad_sch" in sch and "(lib_symbols" in sch and sx.parse(sch)
        sch_svg = c.get("/api/v1/projects/ai_bme280/export?format=schematic_svg").text
        assert sch_svg.startswith("<svg")

        # Verify project listing and inspection endpoints
        plist = c.get("/api/v1/projects").json()["projects"]
        assert any(p["project_id"] == "ai_bme280" and p["has_schematic"] and p["has_board"] for p in plist)
        detail = c.get("/api/v1/projects/ai_bme280").json()
        assert detail["drc"]["passed"] is True and len(detail["chat_history"]) >= 2


def test_design_stream_sse():
    """POST /api/v1/design/stream emits live SSE stage & routing events."""
    with TestClient(app) as c:
        evs = []
        with c.stream("POST", "/api/v1/design/stream", json={
            "prompt": "USB-C BME280 sensor board",
            "project_id": "stream_bme",
            "iterations": 800,
            "max_steps": 80,
        }) as s:
            assert s.status_code == 200
            for line in s.iter_lines():
                if line.startswith("data:"):
                    evs.append(json.loads(line[5:]))
        stages = {e.get("stage") for e in evs if e.get("type") == "stage"}
        assert {"requirements", "components", "netlist", "schematic", "placement", "routing", "drc"}.issubset(stages)
        assert any(e.get("type") == "routing_step" for e in evs)
        assert evs[-1]["type"] == "done" and evs[-1]["project_id"] == "stream_bme"


def test_llm_full_design_and_conversational_chat():
    """Verify Gemini LLM connects to architecture, part selection, netlist/ERC, placement, review, and iterative /chat."""
    from unittest.mock import patch
    from coupled_backend import config

    # Scripted Gemini JSON responses for: (1) architecture, (2) netlist, (3) placement rules, (4) revised netlist after chat
    arch_json = {
        "board_name": "USB-C 3.3V Power & BME280",
        "summary": "USB-C 5V regulated to 3.3V via AP2112K feeding BME280 and I2C header.",
        "board_width_mm": 42.0,
        "board_height_mm": 32.0,
        "power_rails": ["VBUS", "3V3", "GND"],
        "interfaces": ["USB-C", "I2C"],
        "placement_notes": "Keep J1 and J2 on edges; cluster C1/C2 near U1 and C3 near U2.",
        "part_specs": [
            {"ref": "J1", "role": "USB-C power", "preferred_mpn": "TYPE-C-31-M-17", "query": "USB-C 6P", "filters": {}},
            {"ref": "U1", "role": "3.3V LDO", "preferred_mpn": "AP2112K-3.3TRG1", "query": "3.3V LDO", "filters": {}},
            {"ref": "C1", "role": "input cap", "preferred_mpn": "CL10A106KP8NNNC", "query": "10uF 0603", "filters": {}},
            {"ref": "C2", "role": "output cap", "preferred_mpn": "CL10B104KB8NNNC", "query": "100nF 0603", "filters": {}},
            {"ref": "R1", "role": "CC1 pull-down", "preferred_mpn": "0603WAF5101T5E", "query": "5.1k 0603", "filters": {}},
            {"ref": "R2", "role": "CC2 pull-down", "preferred_mpn": "0603WAF5101T5E", "query": "5.1k 0603", "filters": {}},
            {"ref": "J2", "role": "3.3V header", "preferred_mpn": "PZ254V-11-04P", "query": "1x4 header", "filters": {}},
        ],
    }
    netlist_v1 = {
        "nets": {
            "VBUS": [["J1", "A9"], ["J1", "B9"], ["U1", "1"], ["U1", "3"], ["C1", "1"]],
            "GND": [["J1", "A12"], ["J1", "B12"], ["J1", "S1"], ["U1", "2"], ["C1", "2"], ["C2", "2"], ["R1", "2"], ["R2", "2"], ["J2", "2"]],
            "3V3": [["U1", "5"], ["C2", "1"], ["J2", "1"]],
            "CC1": [["J1", "A5"], ["R1", "1"]],
            "CC2": [["J1", "B5"], ["R2", "1"]],
        },
        "net_classes": {"VBUS": "power", "GND": "power", "3V3": "power", "CC1": "signal", "CC2": "signal"},
    }
    netlist_v2 = {
        "nets": {
            **netlist_v1["nets"],
            "3V3": [["U1", "5"], ["C2", "1"], ["J2", "1"], ["R3", "1"]],
            "LED_A": [["R3", "2"], ["D1", "2"]],
            "GND": netlist_v1["nets"]["GND"] + [["D1", "1"]],
        },
        "net_classes": {**netlist_v1["net_classes"], "LED_A": "signal"},
    }
    place_rules = {"edge_components": ["J1", "J2"], "clusters": [["U1", "C1", "C2"]]}

    calls = iter([arch_json, netlist_v1, place_rules, netlist_v2, place_rules])

    chat_answers = iter([
        {"action": "answer_only", "reply": "U1 is an AP2112K-3.3TRG1 600mA low-noise LDO stepped down from USB-C VBUS."},
        {
            "action": "add_or_swap_parts",
            "reply": "Added green power LED D1 (KT-0603G) with current-limiting resistor R3 (4.7k) on 3V3.",
            "add_parts": [
                {"ref": "D1", "role": "power LED", "preferred_mpn": "KT-0603G", "query": "green 0603 LED", "filters": {}},
                {"ref": "R3", "role": "LED resistor", "preferred_mpn": "0603WAF4701T5E", "query": "4.7k 0603", "filters": {}},
            ],
            "remove_refs": [],
            "revised_prompt": "USB-C 3.3V power board with green power indicator LED D1 and resistor R3",
        },
    ])

    with patch.object(config, "GEMINI_API_KEY", "mock-key"), \
         patch("coupled_backend.llm.complete_json", side_effect=lambda *a, **kw: next(calls)), \
         patch("coupled_backend.llm.complete_text", return_value="Engineering review: all rails decoupled and DRC clean."), \
         patch("coupled_backend.llm.chat_json", side_effect=lambda *a, **kw: next(chat_answers)):
        with TestClient(app) as c:
            # 1. Design from natural language
            d = c.post("/api/v1/design", json={"prompt": "Build a USB-C 3.3V power supply board", "project_id": "llm_psu", "iterations": 1000}).json()
            assert d["erc_status"] == "PASS" and d["drc"]["passed"] is True
            assert len(d["components"]) == 7
            assert "Engineering review" in d["review"]

            # 2. Conversational Q&A (answer_only does not rebuild board)
            q = c.post("/api/v1/projects/llm_psu/chat", json={"message": "What regulator did you choose?"}).json()
            assert q["action"] == "answer_only" and "AP2112K" in q["reply"] and q["design"] is None

            # 3. Conversational iterative refinement (adds D1 + R3, rebuilds schematic, placement, routing, DRC)
            e = c.post("/api/v1/projects/llm_psu/chat", json={"message": "Add a green power LED on 3V3", "iterations": 1000}).json()
            assert e["action"] == "add_or_swap_parts"
            assert e["design"]["erc_status"] == "PASS" and e["design"]["drc"]["passed"] is True
            refs = {comp["ref"] for comp in e["design"]["components"]}
            assert {"D1", "R3"}.issubset(refs)
            assert "LED_A" in e["design"]["netlist"]["nets"]


def test_expand_grouped_designators():
    """Defensive expansion when LLM groups refs like 'R1, R2' or 'C1-C3'."""
    from coupled_backend.designer import _expand_refs
    assert _expand_refs("R1, R2") == ["R1", "R2"]
    assert _expand_refs("C1-C3") == ["C1", "C2", "C3"]
    assert _expand_refs("U1") == ["U1"]


def test_headless_api_no_static_frontend():
    """Verify the backend runs purely headless with JSON discovery at / and no static frontend."""
    with TestClient(app) as c:
        r = c.get("/")
        assert r.status_code == 200
        info = r.json()
        assert info["service"] == "coupled-backend"
        assert info["mode"] == "headless-api"
        assert info["docs"] == "/docs"
        assert info["openapi"] == "/openapi.json"
        assert info["auth_config"] == "/api/auth/config"
        assert c.get("/static/index.html").status_code == 404
        assert c.get("/static/app.js").status_code == 404
        assert c.get("/auth/sign-in").status_code == 404
        assert c.get("/openapi.json").status_code == 200
        assert c.get("/api/auth/config").status_code == 200


def test_astar_2layer_router_drc_clean():
    """Verify the built-in DRC-aware 2-layer A* + via router routes all nets across F.Cu/B.Cu with 0 errors."""
    from pathlib import Path
    from coupled_backend import engines, export
    from coupled_backend.router import astar_route

    with TestClient(app) as c:
        c.post("/api/v1/projects/astar_demo/netlist", json={"prompt": "USB-C to 3.3V BME280 sensor board"})
        c.post("/api/v1/projects/astar_demo/place", data={"netlist_id": "astar_demo", "iterations": 1500})
        board_text = c.get("/api/v1/projects/astar_demo/export?format=kicad_pcb").text

    tmp = Path(tempfile.mkdtemp())
    bp = tmp / "board.kicad_pcb"
    bp.write_text(board_text, encoding="utf-8")

    events = []
    res = astar_route(str(bp), 250, events.append)
    assert res["engine"] == "astar"
    assert res["final_metrics"]["unrouted_connections"] == 0
    assert res["final_metrics"]["connected_nets"] == res["final_metrics"]["total_nets"] >= 6

    routed_path = Path(res["output_path"])
    routed_text = routed_path.read_text(encoding="utf-8")
    assert '(layer "F.Cu")' in routed_text
    assert "(segment " in routed_text
    assert sx.parse(routed_text)

    drc = export.run_drc(routed_path)
    assert drc["passed"] is True and drc["error_count"] == 0 and drc["unconnected_count"] == 0
    assert engines.active_router_name() in ("mock", "astar", "pcbworld")







