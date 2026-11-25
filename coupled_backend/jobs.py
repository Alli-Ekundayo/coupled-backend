import asyncio, json, threading, traceback, uuid
from .engines import run_job


class JobHub:
    def __init__(self):
        self.events: dict[str, list[dict]] = {}
        self.done: dict[str, bool] = {}

    def start(self, board_path, checkpoint, max_steps, job_id=None, on_complete=None) -> str:
        jid = job_id or f"job_{uuid.uuid4().hex[:6]}"
        self.events[jid], self.done[jid] = [], False

        def work():
            try:
                res = run_job(board_path, checkpoint, max_steps, lambda e: self.events[jid].append(e))
                if on_complete is not None:
                    try:
                        on_complete(res)
                    except Exception:
                        pass
                self.events[jid].append({"type": "done", **res})
            except Exception as ex:  # surfaced to the client so the demo shows why, not a hang
                self.events[jid].append({"type": "error", "message": f"{type(ex).__name__}: {ex}", "trace": traceback.format_exc()[-800:]})
            self.done[jid] = True
        threading.Thread(target=work, daemon=True).start()
        return jid

    async def stream(self, jid: str):
        i = 0
        while True:
            evs = self.events.get(jid)
            if evs is None:
                yield sse({"type": "error", "message": "unknown job"}); return
            while i < len(evs):
                yield sse(evs[i]); i += 1
            if self.done.get(jid) and i >= len(evs):
                return
            await asyncio.sleep(0.05)


def sse(ev: dict) -> str:
    return f"event: {ev['type']}\ndata: {json.dumps(ev)}\n\n"
