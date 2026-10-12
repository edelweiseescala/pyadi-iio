# Copyright (C) 2026 Analog Devices, Inc.
#
# SPDX short identifier: ADIBSD
"""Local HTTP/WebSocket bridge exposing live ADMT4000 readings as JSON.

Wraps adi.admt4000 so non-Python clients (e.g. a Flutter dashboard on
Windows or Android) can read live angle/turns/temperature/fault data over
the network instead of linking against libiio directly.

Also exposes the 4 long-running action routines from admt4000_app.py
(calibration, magnetic reset, TMC5240 motor move, true power-down) as
background jobs: POST /actions/<name> starts a job and returns a job id,
GET /jobs/<id> polls status, WS /jobs/<id>/stream tails its log lines.
Only one job runs at a time, and the live /stream connection is paused
(device handle released) while a job owns the serial port, resuming once
the job finishes. Jobs cannot be cancelled once started -- none of the
underlying run_* functions support cooperative cancellation.

Usage:
    python admt4000_bridge.py --uri serial:COM10,230400,8n1 --host 0.0.0.0 --port 8765
"""
import argparse
import asyncio
import logging
import math
import threading
import uuid

import uvicorn
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from admt4000_app import (
    run_calibration,
    run_magnetic_reset,
    run_true_powerdown,
    run_tmc5240_basic,
)
from adi.admt4000 import admt4000

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("admt4000_bridge")

app = FastAPI(title="ADMT4000 Bridge")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class ConnectRequest(BaseModel):
    uri: str


class CalibrationRequest(BaseModel):
    uri: str = None
    use_factory_correction: bool = False
    no_re_run: bool = False
    no_calibrate: bool = False
    zero_correction: bool = True
    full_step_angle: float = 0.9
    samples_size: int = 256
    cycles: int = 11
    starting_turn: int = 12
    starting_angle: float = 0.0
    reversed_shaft: bool = False
    cw_steps: bool = False


class MagneticResetRequest(BaseModel):
    uri: str = None
    run_sweep: bool = True
    dc_dc_charge_time_ms: int = 300
    reset_duration_ms: int = 1
    disable_dc_dc_after_reset: bool = True
    starting_turn: int = 35
    reversed_shaft: bool = False
    full_step_angle: float = 0.9
    log_to_file: bool = False


class Tmc5240MoveRequest(BaseModel):
    uri: str = None
    direction: int = -1


class PowerdownRequest(BaseModel):
    uri: str = None
    reversed_shaft: bool = False
    cw_steps: bool = False


class DeviceState:
    """Holds the current admt4000 connection; swappable via /connect."""

    def __init__(self, uri):
        self.uri = uri
        self.dev = None
        self.error = None
        self._lock = threading.Lock()
        self.connect(uri)

    def connect(self, uri):
        with self._lock:
            if self.dev is not None:
                try:
                    del self.dev
                except Exception:  # noqa: BLE001
                    pass
                self.dev = None
            try:
                dev = admt4000(uri=uri)
                # Must be set before the first rx() call -- the rx buffer is
                # lazily created on first use and these settings have no
                # effect once that buffer exists (see adi/rx_tx.py).
                dev.rx_output_type = "SI"
                dev.rx_enabled_channels = [0, 1]  # turns, angle
                self.dev = dev
                self.uri = uri
                self.error = None
            except Exception as e:  # noqa: BLE001
                self.dev = None
                self.error = str(e)
                logger.warning("Failed to connect to %s: %s", uri, e)

    def pause(self):
        """Release the device handle so another connection (a job) can use the port."""
        with self._lock:
            if self.dev is not None:
                try:
                    del self.dev
                except Exception:  # noqa: BLE001
                    pass
                self.dev = None
            self.error = "Paused for an action job"

    def resume(self):
        """Reconnect on the last known URI after a job releases the port."""
        self.connect(self.uri)

    def read(self):
        with self._lock:
            if self.dev is None:
                raise RuntimeError(self.error or "Device not connected")
            dev = self.dev

            # Buffered channel read, matching the data path basic_example.py's
            # --plot mode uses for the live angle trace -- NOT dev.angle.raw,
            # which is a different (unbuffered) attribute path with different
            # values/timing on this driver.
            turns_samples, angle_samples = dev.rx()
            angle_deg = angle_samples[-1]

            # Turn count for the stat tile and absolute-angle math comes from
            # the plain attribute (turns.processed), matching every other
            # example script's absolute-angle calculation (never the rx
            # buffer's turns channel). This value is fractional -- it
            # increments gradually through a revolution (e.g. 0.25 per
            # quarter turn), reaching a whole number only once a full 360
            # degree revolution completes -- so floor() is used below to
            # count only completed revolutions, matching
            # magnetic_reset_example.py's own
            # "math.floor(curr_turns) * FULL_TURN + curr_angle" pattern.
            turns = dev.turns.processed

            temp_raw = dev.temp.raw
            temp_c = (temp_raw + dev.temp.offset) * dev.temp.scale / 1000
            faults = dev.faults
            abs_angle_deg = angle_deg + math.floor(turns) * 360

            return {
                "angle_deg": angle_deg,
                "turns": turns,
                "angle_samples": _json_safe(angle_samples),
                "turns_samples": _json_safe(turns_samples),
                "temp_c": temp_c,
                "faults": faults,
                "faults_hex": "0x{:04X}".format(faults),
                "abs_angle_deg": abs_angle_deg,
            }


def _json_safe(value):
    """Recursively convert numpy arrays/scalars to plain JSON-serializable types."""
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if hasattr(value, "tolist"):  # numpy ndarray / scalar
        return value.tolist()
    return value


class Job:
    def __init__(self, job_id):
        self.id = job_id
        self.status = "running"  # running | done | error
        self.lines = []
        self.results = []
        self.error = None
        self._lock = threading.Lock()

    def log(self, message):
        with self._lock:
            self.lines.append(str(message))

    def add_result(self, data):
        with self._lock:
            self.results.append(_json_safe(data))

    def snapshot(self, after=0):
        with self._lock:
            return list(self.lines[after:])

    def finish(self, error=None):
        with self._lock:
            self.status = "error" if error else "done"
            self.error = error


class JobManager:
    """Runs one admt4000_app.py run_* function at a time as a background job.

    Pauses the bridge's own live-reading connection before a job starts
    (the job opens its own device handle on the same serial port) and
    resumes it once the job finishes.
    """

    def __init__(self, device_state):
        self.device_state = device_state
        self._lock = threading.Lock()
        self.current = None
        self.jobs = {}

    def start(self, target, *args, pass_result_callback=False):
        with self._lock:
            if self.current is not None and self.current.status == "running":
                raise HTTPException(
                    status_code=409, detail="Another action job is already running"
                )
            job = Job(str(uuid.uuid4()))
            self.jobs[job.id] = job
            self.current = job

        self.device_state.pause()

        def wrapper():
            try:
                if pass_result_callback:
                    target(*args, job.log, job.add_result)
                else:
                    target(*args, job.log)
                job.finish()
            except Exception as e:  # noqa: BLE001
                job.log(f"ERROR: {e}")
                job.finish(error=str(e))
            finally:
                self.device_state.resume()

        threading.Thread(target=wrapper, daemon=True).start()
        return job.id

    def get(self, job_id):
        job = self.jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Unknown job id")
        return job


state: DeviceState = None
jobs: JobManager = None


@app.get("/status")
def status():
    return {
        "uri": state.uri,
        "connected": state.dev is not None,
        "error": state.error,
        "compatible_parts": state.dev.compatible_parts if state.dev else None,
    }


@app.get("/reading")
def reading():
    try:
        return state.read()
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


@app.post("/connect")
def connect(req: ConnectRequest):
    state.connect(req.uri)
    return status()


@app.websocket("/stream")
async def stream(websocket: WebSocket, interval_ms: int = 200):
    await websocket.accept()
    try:
        while True:
            try:
                payload = state.read()
            except Exception as e:  # noqa: BLE001
                payload = {"error": str(e)}
            await websocket.send_json(payload)
            await asyncio.sleep(interval_ms / 1000)
    except WebSocketDisconnect:
        logger.info("Client disconnected from /stream")


@app.post("/actions/calibration")
def action_calibration(req: CalibrationRequest):
    job_id = jobs.start(
        run_calibration,
        req.uri or state.uri,
        req.use_factory_correction,
        req.no_re_run,
        req.no_calibrate,
        req.zero_correction,
        req.full_step_angle,
        req.samples_size,
        req.cycles,
        req.starting_turn,
        req.starting_angle,
        req.reversed_shaft,
        req.cw_steps,
        False,  # plot: always disabled on the headless bridge
        pass_result_callback=True,
    )
    return {"job_id": job_id}


@app.post("/actions/magnetic_reset")
def action_magnetic_reset(req: MagneticResetRequest):
    job_id = jobs.start(
        run_magnetic_reset,
        req.uri or state.uri,
        req.run_sweep,
        False,  # plot: always disabled on the headless bridge
        req.dc_dc_charge_time_ms,
        req.reset_duration_ms,
        req.disable_dc_dc_after_reset,
        req.starting_turn,
        req.reversed_shaft,
        req.full_step_angle,
        req.log_to_file,
    )
    return {"job_id": job_id}


@app.post("/actions/tmc5240_move")
def action_tmc5240_move(req: Tmc5240MoveRequest):
    job_id = jobs.start(run_tmc5240_basic, req.uri or state.uri, req.direction)
    return {"job_id": job_id}


@app.post("/actions/powerdown")
def action_powerdown(req: PowerdownRequest):
    job_id = jobs.start(
        run_true_powerdown,
        req.uri or state.uri,
        req.reversed_shaft,
        req.cw_steps,
    )
    return {"job_id": job_id}


@app.get("/jobs/{job_id}")
def job_status(job_id: str):
    job = jobs.get(job_id)
    return {
        "job_id": job.id,
        "status": job.status,
        "error": job.error,
        "lines": job.snapshot(),
        "results": job.results,
    }


@app.websocket("/jobs/{job_id}/stream")
async def job_stream(websocket: WebSocket, job_id: str, interval_ms: int = 200):
    await websocket.accept()
    try:
        job = jobs.get(job_id)
    except HTTPException as e:
        await websocket.send_json({"error": e.detail})
        await websocket.close()
        return

    sent = 0
    try:
        while True:
            new_lines = job.snapshot(after=sent)
            for line in new_lines:
                await websocket.send_json({"line": line})
            sent += len(new_lines)
            if job.status != "running":
                await websocket.send_json(
                    {
                        "done": True,
                        "status": job.status,
                        "error": job.error,
                        "results": job.results,
                    }
                )
                break
            await asyncio.sleep(interval_ms / 1000)
    except WebSocketDisconnect:
        logger.info("Client disconnected from /jobs/%s/stream", job_id)


def main():
    global state, jobs

    parser = argparse.ArgumentParser(description="ADMT4000 bridge service")
    parser.add_argument(
        "--uri",
        type=str,
        default="serial:COM10,230400,8n1",
        help="IIO context URI. Use 'ip:<address>' for Ethernet"
        " or 'serial:<port>,<baud>,8n1' for USB serial.",
    )
    parser.add_argument(
        "--host",
        type=str,
        default="0.0.0.0",
        help="Host/interface to bind the bridge server to (default: 0.0.0.0,"
        " reachable from other devices on the LAN, e.g. an Android phone).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8765,
        help="Port to bind the bridge server to (default: 8765).",
    )
    args = parser.parse_args()

    state = DeviceState(args.uri)
    jobs = JobManager(state)
    if state.error:
        logger.warning(
            "Starting bridge without a device connection (%s)."
            " Use POST /connect to retry once hardware is available.",
            state.error,
        )

    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
