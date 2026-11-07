from __future__ import annotations
import httpx

class SimClient:
    """HTTP client wrapping the simulator API."""

    def __init__(self, base_url: str, timeout: float = 60.0):
        self.base_url = base_url.rstrip("/")
        self.client = httpx.Client(base_url=self.base_url, timeout=timeout)

    def close(self):
        self.client.close()

    def health(self) -> dict:
        return self.client.get("/health").json()

    def sim_state(self) -> dict:
        return self.client.get("/sim/state").json()

    def advance(self, minutes: int) -> dict:
        r = self.client.post("/sim/advance", json={"minutes": minutes})
        if r.status_code == 409 and r.json().get("error") == "finished":
            return {"finished": True, "new_chunks": [], "sim_minute": r.json().get("detail", {}).get("sim_minute", -1)}
        r.raise_for_status()
        return r.json()

    def workflows(self) -> list[dict]:
        return self.client.get("/workflows").json()

    def workflow(self, wid: str) -> dict:
        return self.client.get(f"/workflows/{wid}").json()

    def get_config(self, wid: str) -> dict:
        return self.client.get(f"/workflows/{wid}/config").json()

    def list_chunks(self, kind: str = "events", after: int = -1) -> list[dict]:
        return self.client.get("/events/chunks", params={"kind": kind, "after": after}).json()

    def iter_chunk(self, n: int):
        """Streams chunk NDJSON lines one by one to avoid large memory allocations."""
        with self.client.stream("GET", f"/events/chunks/{n}") as r:
            r.raise_for_status()
            for line in r.iter_lines():
                if line.strip():
                    yield line

    def get_chunk_lines(self, n: int) -> list[str]:
        return list(self.iter_chunk(n))

    def request_approval(self, body: dict) -> dict:
        r = self.client.post("/approvals", json=body)
        return {"status_code": r.status_code, "data": r.json()}

    def get_approval(self, aid: str) -> dict:
        r = self.client.get(f"/approvals/{aid}")
        return {"status_code": r.status_code, "data": r.json()}

    def list_approvals(self, workflow_id: str | None = None, status: str | None = None) -> list[dict]:
        params = {}
        if workflow_id:
            params["workflow_id"] = workflow_id
        if status:
            params["status"] = status
        return self.client.get("/approvals", params=params).json()

    def apply_approval(self, wid: str, aid: str) -> dict:
        r = self.client.post(f"/workflows/{wid}/apply", json={"approval_id": aid})
        return {"status_code": r.status_code, "data": r.json()}

    def auto_apply(self, wid: str, body: dict) -> dict:
        r = self.client.put(f"/workflows/{wid}/config", json=body)
        return {"status_code": r.status_code, "data": r.json()}

    def suggest_fixes(self, wid: str, problem: str) -> dict:
        r = self.client.post("/llm/suggest-fixes", json={"workflow_id": wid, "problem": problem})
        return {"status_code": r.status_code, "data": r.json()}

    def run_validation(self, wid: str, prompt_version: str, n: int) -> dict:
        r = self.client.post("/validation/runs", json={"workflow_id": wid, "prompt_version": prompt_version, "n": n})
        return {"status_code": r.status_code, "data": r.json()}
