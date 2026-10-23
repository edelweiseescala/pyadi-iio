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
Only one job runs at a time. Jobs cannot be cancelled once started --
none of the underlying run_* functions support cooperative cancellation.

The bridge holds exactly ONE admt4000ard1z connection for its entire
process lifetime and shares it between live reads and action jobs --
confirmed via direct hardware testing that opening a SECOND connection to
this device's serial port within the same process reliably fails
(sp_blocking_read_next timeout on the admt_evb control device's first
write, likely an Arduino-style DTR-reset-on-first-open quirk on the eval
board). A DeviceState-level lock serializes live reads and job device
access against each other since they now share one device instance.

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
from adi.admt4000ard1z import admt4000ard1z

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
    mode: str = None  # "buffered" | "polling"; omit to keep the current mode




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
    """Holds THE ONE device connection for the bridge's whole process
    lifetime; swappable only via /connect (e.g. switching COM ports), never
    torn down/recreated around an action job -- confirmed by direct hardware
    testing that a second connection to this device within one process can
    fail outright (see repro_reconnect_test.py). Live reads go through
    dev.sensor; action jobs reuse dev directly (see JobManager).

    Two mutually exclusive connection modes, selected by `mode` -- there is
    deliberately NO "auto" mode: both a throwaway probe connection and a
    sequential try-buffered-then-fall-back-to-polling approach were tried
    and both reliably hang/crash this hardware on the SECOND connection
    attempt within the same process, even after the first attempt's object
    has gone out of scope on exception (confirmed by direct testing; see
    repro_reconnect_test.py). The caller must know and pass the right mode
    for their backend/board up front.

    - "buffered": connects via admt4000ard1z (admt_evb + TMC5240 required in
      the IIO context) and reads live data with dev.sensor.rx(), matching
      the no-OS backend's buffer support. Action jobs (calibration, magnetic
      reset, motor move, power-down) are available in this mode.
    - "polling": connects via the plain admt4000 driver (no admt_evb/motor
      needed), for backends without buffer support -- e.g. the Zephyr
      admt4000 IIO driver, which only implements create/read_attr/write_attr
      (confirmed via iio_info: no open_buffer/readbuf hook, no admt_evb
      device, no faults attribute). Reads poll raw/processed attributes one
      sample at a time, exactly like basic_example_polling.py. Action jobs
      are unavailable in this mode (no admt_evb/motor device exists).

    Two distinct hardware issues were found by direct testing on this board:

    1. Once dev.sensor.rx() has been called (the live-stream's buffered
       read), all subsequent writes to the separate admt_evb control device
       (shutdown_pulse/coil_reset/power_enable -- used by magnetic_reset and
       true_powerdown) permanently fail with a low-level serial timeout, for
       the rest of this connection's lifetime. The reverse order is fine
       (control writes, then rx(), works). rx_started latches True the first
       time read() successfully calls rx(), and JobManager refuses to start
       a job that touches admt_evb (requires_control_device=True) once it's
       set. Calibration and the motor move only touch .sensor/.motor, which
       were never shown to break from rx() alone, so they stay available.

    2. Separately, a long/heavy calibration run was observed to sometimes
       leave the connection unable to complete whatever job runs next --
       not limited to admt_evb, also seen breaking a plain motor move and
       Live's rx() reads. This was intermittent (not reproduced in
       isolation every time) and never pinned to a specific cause.
       Auto-reconnecting after calibration was tried as a mitigation but
       reliably failed itself on this board (opening a second in-process
       connection is unreliable here -- see repro_reconnect_test.py), so
       it was removed; this issue has NO mitigation right now. If it
       happens, use /connect (best effort) or restart the bridge.

    Clearing rx_started (issue 1) needs a fresh connection too: restart the
    bridge, or use /connect (best effort -- see repro_reconnect_test.py).
    """

    def __init__(self, uri, mode="buffered"):
        self.uri = uri
        self.mode = mode
        self.dev = None
        self.error = None
        self.rx_started = False
        self._lock = threading.Lock()
        self.connect(uri, mode)

    def connect(self, uri, mode=None):
        mode = mode or self.mode
        with self._lock:
            if self.dev is not None:
                try:
                    del self.dev
                except Exception:  # noqa: BLE001
                    pass
                self.dev = None
            try:
                if mode == "polling":
                    dev = admt4000(uri=uri)
                else:
                    dev = admt4000ard1z(uri=uri, motor_direction=-1)
                    # Must be set before the first rx() call -- the rx buffer
                    # is lazily created on first use and these settings have
                    # no effect once that buffer exists (see adi/rx_tx.py).
                    dev.sensor.rx_output_type = "SI"
                    dev.sensor.rx_enabled_channels = [0, 1]  # turns, angle
                    # On libiio >= 1.0, iio.Stream pipelines
                    # rx_buffer_num_blocks buffers deep (default 4) before
                    # data reaches rx() -- for Live's small buffer size that
                    # was visible as ~500ms of lag behind real sensor motion
                    # (see basic_example.py for the same fix). No effect on
                    # libiio 0.X, which doesn't pipeline.
                    if hasattr(dev.sensor, "rx_buffer_num_blocks"):
                        dev.sensor.rx_buffer_num_blocks = 1
                self.dev = dev
                self.uri = uri
                self.mode = mode
                self.error = None
                self.rx_started = False
            except Exception as e:  # noqa: BLE001
                self.dev = None
                self.mode = mode
                self.error = str(e)
                logger.warning("Failed to connect to %s (mode=%s): %s", uri, mode, e)

    def read(self):
        with self._lock:
            if self.dev is None:
                raise RuntimeError(self.error or "Device not connected")

            if self.mode == "polling":
                return self._read_polling()
            return self._read_buffered()

    def _read_buffered(self):
        dev = self.dev.sensor

        # Buffered channel read, matching the data path basic_example.py's
        # --plot mode uses for the live angle trace -- NOT dev.angle.raw,
        # which is a different (unbuffered) attribute path with different
        # values/timing on this driver.
        turns_samples, angle_samples = dev.rx()
        self.rx_started = True
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

    def _read_polling(self):
        # One sample per channel directly, exactly like
        # basic_example_polling.py -- no buffer support required, for
        # backends like the Zephyr admt4000 IIO driver.
        dev = self.dev
        angle_deg = dev.angle.processed
        turns = dev.turns.processed
        # Unlike the no-OS buffered path's dev.temp scale (milli-degrees,
        # hence /1000 there), the Zephyr admt4000 device's temp scale is
        # already in whole degrees C -- confirmed via iio_info
        # (raw=35661760, scale=0.000001 -> processed=35.66 C directly).
        temp_c = dev.temp.processed

        try:
            faults = dev.faults
            faults_hex = "0x{:04X}".format(faults)
        except Exception:  # noqa: BLE001
            # The Zephyr admt4000 device doesn't expose a faults attribute.
            faults = None
            faults_hex = None

        abs_angle_deg = angle_deg + math.floor(turns) * 360

        return {
            "angle_deg": angle_deg,
            "turns": turns,
            "angle_samples": [angle_deg],
            "turns_samples": [turns],
            "temp_c": temp_c,
            "faults": faults,
            "faults_hex": faults_hex,
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

    def results_snapshot(self, after=0):
        with self._lock:
            return list(self.results[after:])

    def finish(self, error=None):
        with self._lock:
            self.status = "error" if error else "done"
            self.error = error


class JobManager:
    """Runs one admt4000_app.py run_* function at a time as a background job,
    reusing the bridge's single persistent device connection (never opens a
    second connection -- see DeviceState's docstring for why).
    """

    def __init__(self, device_state):
        self.device_state = device_state
        self._lock = threading.Lock()
        self.current = None
        self.jobs = {}

    def start(
        self,
        target,
        *args,
        pass_result_callback=False,
        requires_control_device=False,
    ):
        with self._lock:
            if self.device_state.mode == "polling":
                raise HTTPException(
                    status_code=501,
                    detail=(
                        "Action unavailable: bridge is in Zephyr polling mode"
                        " (no admt_evb/motor present on this device)."
                    ),
                )
            if self.current is not None and self.current.status == "running":
                raise HTTPException(
                    status_code=409, detail="Another action job is already running"
                )
            if requires_control_device and self.device_state.rx_started:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "Actions are unavailable: live monitoring has already run on"
                        " this connection, which permanently breaks control-device"
                        " writes (confirmed on this hardware). Use Reconnect or"
                        " restart the bridge to re-enable this action."
                    ),
                )
            job = Job(str(uuid.uuid4()))
            self.jobs[job.id] = job
            self.current = job

        def wrapper():
            # Hold the device lock for the job's entire duration -- live
            # reads (DeviceState.read()) and this job now access the SAME
            # device instance, so they must not interleave at the hardware
            # level. /stream's reads naturally block for this duration and
            # resume automatically once the job releases the lock.
            with self.device_state._lock:
                dev = self.device_state.dev
                if dev is None:
                    job.log(f"ERROR: {self.device_state.error or 'Device not connected'}")
                    job.finish(error=self.device_state.error or "Device not connected")
                    return
                try:
                    if pass_result_callback:
                        target(*args, job.log, job.add_result, dev=dev)
                    else:
                        target(*args, job.log, dev=dev)
                    job.finish()
                except Exception as e:  # noqa: BLE001
                    job.log(f"ERROR: {e}")
                    job.finish(error=str(e))

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
        "mode": state.mode,
        "connected": state.dev is not None,
        "error": state.error,
        "compatible_parts": state.dev.compatible_parts if state.dev else None,
        "rx_started": state.rx_started,
    }


@app.get("/reading")
def reading():
    try:
        return state.read()
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


@app.post("/connect")
def connect(req: ConnectRequest):
    state.connect(req.uri, req.mode)
    return status()


@app.websocket("/stream")
async def stream(websocket: WebSocket, interval_ms: int = 200):
    await websocket.accept()
    try:
        while True:
            try:
                # state.read() blocks on real hardware I/O (dev.rx()) --
                # offload it to a thread so it doesn't block the asyncio
                # event loop. Without this, a slow/blocked read here could
                # starve other requests, including an /actions/* call
                # waiting to acquire DeviceState._lock for a job's duration.
                payload = await asyncio.to_thread(state.read)
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
        pass_result_callback=True,
        requires_control_device=True,
    )
    return {"job_id": job_id}


@app.post("/actions/tmc5240_move")
def action_tmc5240_move(req: Tmc5240MoveRequest):
    job_id = jobs.start(
        run_tmc5240_basic,
        req.uri or state.uri,
        req.direction,
        pass_result_callback=True,
    )
    return {"job_id": job_id}


@app.post("/actions/powerdown")
def action_powerdown(req: PowerdownRequest):
    job_id = jobs.start(
        run_true_powerdown,
        req.uri or state.uri,
        req.reversed_shaft,
        req.cw_steps,
        requires_control_device=True,
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
    results_sent = 0
    try:
        while True:
            new_lines = job.snapshot(after=sent)
            for line in new_lines:
                await websocket.send_json({"line": line})
            sent += len(new_lines)

            new_results = job.results_snapshot(after=results_sent)
            for result in new_results:
                await websocket.send_json({"result": result})
            results_sent += len(new_results)

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
    parser.add_argument(
        "--mode",
        type=str,
        choices=["buffered", "polling"],
        default="buffered",
        help="'buffered' (default) connects via admt4000ard1z and reads live"
        " data with dev.sensor.rx() -- requires admt_evb/TMC5240 in the IIO"
        " context (no-OS backend). 'polling' connects via the plain admt4000"
        " driver and polls raw attributes one sample at a time -- for"
        " backends without buffer support, e.g. the Zephyr admt4000 IIO"
        " driver. Action jobs (calibration, magnetic reset, motor move,"
        " power-down) are unavailable in polling mode. There is no 'auto'"
        " mode -- probing/trying one mode then falling back to the other"
        " both reliably hang this hardware on the second connection attempt"
        " within the same process (confirmed by direct testing), so you must"
        " know and pass the right mode for your board.",
    )
    args = parser.parse_args()

    state = DeviceState(args.uri, args.mode)
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
