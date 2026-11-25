"""Stage 4: routing engines. `run_job` streams events through `emit(dict)`.

- pcbworld : real LG-AI-Research/PCBWorld decoder-only RL policy (needs Linux x86_64 + compiled KiCad 9.0.8 C++ engine + checkpoints).
             Wired to the canonical PCBWorld API (`KiCadRLAgent.from_checkpoint`, `RLEnvConfig.from_checkpoint`,
             `resolve_eval_env`, `make_decoder_env`, `gather_dist`, `env.save_pcb`).
- astar    : built-in DRC-aware 2-layer grid A* + plated vias + targeted Pathfinder rip-up-and-reroute autorouter
             (F.Cu + B.Cu, exact pad/via/track/Edge.Cuts clearances; passes KiCad 9.0.9 DRC with 0 errors).
- mock     : legacy greedy MST + L-shaped traces (only used when ROUTER_ENGINE=mock explicitly).
"""
from __future__ import annotations

import math
import sys
import subprocess
import uuid
from pathlib import Path
from typing import Any, Mapping

from . import config, sexpr as sx
from .kicad_io import count_copper_layers, count_routable_nets, parse_board, render_svg, rotate


def _ensure_pcbworld_on_path() -> None:
    repo = config.PCBWORLD_REPO
    if repo.is_dir() and str(repo) not in sys.path:
        sys.path.insert(0, str(repo))


def check_board_limits(board_path: str, max_nets: int | None = None) -> dict[str, int]:
    """Validate that a .kicad_pcb respects the checkpoint's net-slot cap (default 64 nets)."""
    cap = config.MAX_NETS if max_nets is None else max_nets
    text = Path(board_path).read_text(encoding="utf-8", errors="ignore")
    tree, _comps, nets, _outline = parse_board(text)
    routable = count_routable_nets(nets)
    layers = count_copper_layers(tree)
    if routable > cap:
        raise ValueError(
            f"Board {Path(board_path).name} has {routable} routable nets, exceeding the {cap}-net "
            f"PCBWorld checkpoint slot cap. Keep demo boards to 5-15 nets on 2 layers."
        )
    return {"routable_nets": routable, "copper_layers": layers}


def pcbworld_status() -> dict[str, bool]:
    """Inspect whether PCBWorld Python package, Gymnasium, and compiled C++ kicad_rl_router are available."""
    _ensure_pcbworld_on_path()
    status = {}
    for mod in ("pcb_world", "gymnasium", "kicad_rl_router"):
        try:
            __import__(mod)
            status[mod] = True
        except ImportError:
            status[mod] = False
    return status


def active_router_name() -> str:
    """Return the routing engine that `run_job` will actually execute under current config/environment."""
    eng = config.ROUTER_ENGINE
    if eng == "mock":
        return "mock"
    if eng in ("auto", "pcbworld"):
        st = pcbworld_status()
        if st.get("pcb_world") and st.get("gymnasium") and st.get("kicad_rl_router"):
            return "pcbworld"
        if eng == "pcbworld":
            return "pcbworld"
    return "astar"


def run_job(board_path: str, checkpoint: str, max_steps: int, emit):
    eng = config.ROUTER_ENGINE
    if eng in ("auto", "pcbworld"):
        _ensure_pcbworld_on_path()
        try:
            import pcb_world  # noqa: F401
            import gymnasium  # noqa: F401
            import kicad_rl_router  # noqa: F401
            return _pcbworld_route(board_path, checkpoint, max_steps, emit)
        except ImportError:
            if eng == "pcbworld":
                raise
    if eng == "mock":
        return _mock_route(board_path, max_steps, emit)
    from .router import astar_route
    check_board_limits(board_path)
    return astar_route(board_path, max_steps, emit)


def _load_policy(
    ckpt_or_blob: str | Path | Mapping[str, Any],
    *,
    device: Any = "auto",
    strict: bool = False,
    deterministic: bool = True,
):
    """Build and load a PCBWorld decoder-only RL policy into a `KiCadRLAgent`.

    Accepts either:
      - a checkpoint path (`str | Path`): delegates to `KiCadRLAgent.from_checkpoint`, or
      - an already-loaded checkpoint dict (`blob` containing `policy_state_dict` and `config`/`args`):
        resolves config via `configs.loader.ckpt.load_ckpt_config`, instantiates via
        `RLPolicyConfig.from_checkpoint`, pads legacy entity-type embeddings, loads weights, and
        wraps in `KiCadRLAgent`.
    """
    _ensure_pcbworld_on_path()
    from methods.rl_agent.policy.agent import KiCadRLAgent

    if isinstance(ckpt_or_blob, (str, Path)):
        return KiCadRLAgent.from_checkpoint(
            Path(ckpt_or_blob),
            device=device,
            strict=strict,
            deterministic=deterministic,
        )

    from configs.loader.ckpt import load_ckpt_config
    from configs.loader.schema import RLPolicyConfig
    from methods.rl_agent.models.loader import pad_legacy_entity_type_rows
    from methods.rl_agent.training.utils import auto_device

    dev = auto_device(device) if isinstance(device, str) else device
    ckpt_args, sources = load_ckpt_config(ckpt_or_blob, strict=strict)
    model = RLPolicyConfig.from_checkpoint(ckpt_args).build(dev)
    state_dict = dict(ckpt_or_blob["policy_state_dict"])
    pad_legacy_entity_type_rows(state_dict, model)
    missing, unexpected = model.load_state_dict(state_dict, strict=False)
    allowed_missing = {"prev_action", "drc"}
    hard_missing = [k for k in missing if not any(tag in k.lower() for tag in allowed_missing)]
    if hard_missing or unexpected:
        raise RuntimeError(
            f"PCBWorld checkpoint load mismatch: missing={hard_missing}, unexpected={unexpected}"
        )
    model.net_constraint_obs = bool(ckpt_args["net_constraint_obs"])
    model.ckpt_sources = sources
    model.eval()
    agent = KiCadRLAgent(model, device=dev, deterministic=deterministic)
    agent.ckpt_args = ckpt_args
    agent.train_iteration = int(ckpt_or_blob.get("iteration", -1))
    return agent


def _pcbworld_route(board_path: str, checkpoint: str, max_steps: int, emit):
    """Run LG-AI-Research/PCBWorld decoder-only RL policy on `board_path` and stream SSE step events."""
    _ensure_pcbworld_on_path()
    from configs.loader.resolve import Layer
    from configs.loader.schema import RLEnvConfig
    from eval.rollout.rl import check_policy_env, eval_env_config, resolve_eval_env
    from methods.rl_agent.policy.agent import gather_dist
    from methods.rl_agent.rollout.transformer import max_net_slots_for_policy
    from methods.rl_agent.wrappers.factory import make_decoder_env

    ckpt_dir = config.CHECKPOINT_DIR / checkpoint
    files = sorted(ckpt_dir.glob("*.pt")) + sorted(ckpt_dir.glob("*.pth"))
    if not files:
        raise FileNotFoundError(
            f"no checkpoint under {ckpt_dir}: run `hf download LG-AI-Research/PCBWorld --local-dir {config.CHECKPOINT_DIR}`"
        )
    ckpt_path = files[0]
    agent = _load_policy(ckpt_path, device="auto", strict=False, deterministic=True)

    # Enforce checkpoint net-slot cap (default 64 nets) before launching KiCad router
    max_slots = max_net_slots_for_policy(agent.model) or config.MAX_NETS
    limits = check_board_limits(board_path, max_nets=max_slots)

    # Resolve eval RLEnvConfig from checkpoint metadata (preserving arch & obs semantics)
    ckpt_args = agent.ckpt_args
    step_drc = bool(ckpt_args.get("emit_drc_tokens", False))
    base_env = RLEnvConfig.from_checkpoint(ckpt_args, int(max_steps))
    user_layers = [Layer("user", {"max_steps": int(max_steps)}, where={"max_steps": "coupled-backend"})]
    env_run = resolve_eval_env(
        base_env,
        step_drc=step_drc,
        where=str(ckpt_path),
        sources=getattr(agent.model, "ckpt_sources", None),
        user=user_layers,
    )
    env_config = eval_env_config(env_run)
    check_policy_env(agent.model, env_config)

    policy_net_select = bool(getattr(agent.model, "policy_net_select", False))
    env = make_decoder_env(board_path, env_config, seed=0, policy_net_select=policy_net_select)
    out = board_path.replace(".kicad_pcb", "_routed.kicad_pcb")
    info: dict[str, Any] = {}
    try:
        obs, info = env.reset(seed=0)
        step, done = 0, False
        while not done and step < max_steps:
            dist = gather_dist([env], [0], policy_net_select=policy_net_select, obs_list=[obs])
            acts, _logp = agent.act([obs], dist=dist.to(agent.device), deterministic=True)
            action = acts[0].cpu().numpy() if hasattr(acts, "cpu") else acts[0]
            obs, _reward, term, trunc, info = env.step(action)
            done = bool(term or trunc)
            step += 1
            init_unc = int(info.get("initial_unconnected") or limits["routable_nets"])
            unrouted = int(info.get("unrouted_count") or 0)
            emit({
                "type": "step",
                "step": step,
                "connected_nets": max(0, init_unc - unrouted),
                "total_nets": init_unc,
                "unrouted_count": unrouted,
                "drc_violations": info.get("drc_violations", 0),
                "ratsnest_reduction": info.get("ratsnest_reduction"),
                "engine": "pcbworld",
            })
        env.save_pcb(out)
    finally:
        try:
            env.close()
        except Exception:
            pass

    final_metrics = {k: v for k, v in info.items() if isinstance(v, (int, float, str, bool))}
    final_metrics.setdefault("routable_nets", limits["routable_nets"])
    final_metrics.setdefault("copper_layers", limits["copper_layers"])
    return {"output_path": out, "final_metrics": final_metrics, "engine": "pcbworld"}


# ------------------------------------------------------------------ mock router
def _mock_route(board_path, max_steps, emit):
    check_board_limits(board_path)
    text = Path(board_path).read_text()
    tree, comps, nets, _ = parse_board(text)
    pos = {}
    for c in comps:
        for p in c.pads:
            dx, dy = rotate(p.x, p.y, c.rot)
            if p.net:
                pos.setdefault(p.net, []).append((c.x + dx, c.y + dy))
    ids = {str(n[2]): n[1] for n in sx.find_all(tree, "net") if len(n) >= 3}
    segs, step, connected = [], 0, 0
    total = len([n for n, p in pos.items() if len(p) > 1])
    for net, pts in sorted(pos.items(), key=lambda kv: len(kv[1])):
        if len(pts) < 2:
            continue
        done, rest = [pts[0]], pts[1:]
        while rest and step < max_steps:
            a, b = min(((a, b) for a in done for b in rest), key=lambda ab: abs(ab[0][0] - ab[1][0]) + abs(ab[0][1] - ab[1][1]))
            via = (b[0], a[1])
            for s, e in ((a, via), (via, b)):
                if math.dist(s, e) > 1e-6:
                    segs.append(f'  (segment (start {s[0]:.4f} {s[1]:.4f}) (end {e[0]:.4f} {e[1]:.4f}) (width 0.25) (layer "F.Cu") (net {ids.get(net, 0)}) (uuid "{uuid.uuid4()}"))')
            done.append(b); rest.remove(b); step += 1
            ev = {"type": "step", "step": step, "connected_nets": connected, "total_nets": total, "drc_violations": 0, "engine": "mock"}
            if step % 3 == 0:  # live trace preview
                ev["svg_snapshot"] = render_svg(text.rstrip().rstrip(")") + "\n" + "\n".join(segs) + "\n)\n")
            emit(ev)
        connected += 1
    body = text.rstrip().rstrip(")") + "\n" + "\n".join(segs) + "\n)\n"
    out = board_path.replace(".kicad_pcb", "_routed.kicad_pcb")
    Path(out).write_text(body)
    emit({"type": "step", "step": step, "connected_nets": connected, "total_nets": total, "drc_violations": 0, "engine": "mock", "svg_snapshot": render_svg(body)})
    return {"output_path": out, "final_metrics": {"connected_nets": connected, "total_nets": total, "drc_violations": "n/a (mock router)"}, "engine": "mock"}


# ------------------------------------------------------------------ Freerouting "finish remaining nets" fallback
def freerouting_finish(board_path: str) -> str:
    """Needs KiCad's python `pcbnew` + Java + freerouting.jar (worker image provides all three)."""
    dsn, ses, out = (board_path.replace(".kicad_pcb", s) for s in (".dsn", ".ses", "_fr.kicad_pcb"))
    code = f"import pcbnew;b=pcbnew.LoadBoard({board_path!r});pcbnew.ExportSpecctraDSN(b,{dsn!r})"
    subprocess.run(["python3", "-c", code], check=True)
    subprocess.run(["java", "-jar", config.FREEROUTING_JAR, "-de", dsn, "-do", ses, "-mp", "20"], check=True, timeout=300)
    code = f"import pcbnew;b=pcbnew.LoadBoard({board_path!r});pcbnew.ImportSpecctraSES(b,{ses!r});b.Save({out!r})"
    subprocess.run(["python3", "-c", code], check=True)
    return out
