# Copyright (C) 2026 Analog Devices, Inc.
#
# SPDX short identifier: ADIBSD
"""Unified Tkinter GUI bundling all ADMT4000 example scripts.

Wraps basic_example.py, calibration_example.py, magnetic_reset_example.py,
tmc5240_basic_example.py and true_powerdown_example.py behind a single
application with a shared URI field and an output console.
"""
import csv
import math
import pprint
import threading
import tkinter as tk
from datetime import datetime
from time import sleep
from tkinter import messagebox, scrolledtext, ttk

import calibration_utils as cal_utils
import matplotlib.pyplot as plt
import numpy as np

from adi.admt4000 import admt4000
from adi.admt4000ard1z import admt4000ard1z
from adi.tmc5240 import tmc5240

FULL_TURN = 360  # degrees

DEFAULT_URI = "serial:COM10,230400,8n1"


################################################################################
# Shared helpers (mirrors magnetic_reset_example.py / calibration_example.py)
################################################################################
def read_faults(sensor, log):
    sensor.reg_write(0x06, 0x00)
    fault_reg = sensor.faults
    log("ADMT4000 Fault Register: 0x{:04X} (0b{:016b})".format(fault_reg, fault_reg))
    return fault_reg


def print_current_position(motor, sensor, log):
    log("\n--- Current Position ---")
    if motor is not None:
        log(
            "Current Motor Absolute Angle: {:.2f}° ({} microsteps)".format(
                motor.angular_position.processed, motor.angular_position.raw
            )
        )
    if sensor is not None:
        curr_angle = sensor.angle.processed
        curr_turns = sensor.turns.processed
        curr_abs_angle = curr_angle + int(curr_turns) * FULL_TURN
        log("Current ADMT4000 Absolute Angle:  {:.2f}°".format(curr_abs_angle))
        log("ADMT4000 Turn count: {:.2f}".format(curr_turns))
        log("ADMT4000 Angle: {:.2f}°".format(curr_angle))


def print_harmonic_errors(error_mag, harmonic_errors, scaled_harmonic_errors, log):
    log("1st harmonic")
    log("bin: 11")
    log("magnitude: {:.4f}".format(error_mag[11]))
    log("2nd harmonic")
    log("bin: 22")
    log("magnitude: {:.4f}".format(error_mag[22]))

    if not (
        error_mag[11] == max(error_mag[8:15]) and error_mag[22] == max(error_mag[19:26])
    ):
        log("Harmonics might not be the localized peak!!!")

    log("--------------------------------------------------------------")
    log("Harmonic Errors")
    log("Harmonic Errors (raw):")
    log(pprint.pformat(harmonic_errors))
    log("Scaled Harmonic Errors:")
    log(pprint.pformat(scaled_harmonic_errors))


################################################################################
# 1. Basic readout (basic_example.py)
################################################################################
def run_basic(uri, plot, log):
    dev = admt4000(uri=uri)
    try:
        angle_scale = dev.angle.scale
        temp_scale = dev.temp.scale
        temp_offset = dev.temp.offset

        curr_angle_raw = dev.angle.raw
        curr_angle = curr_angle_raw * angle_scale
        curr_temp_raw = dev.temp.raw
        curr_temp = (curr_temp_raw + temp_offset) * temp_scale / 1000
        log("ADMT4000 Current Readings")
        log("Turn count: " + str(dev.turns.raw))
        log("Angle: " + str(curr_angle_raw) + " (" + str(curr_angle) + " degrees)")
        log("Temperature: " + str(curr_temp) + " C")

        dev.rx_output_type = "SI"
        log(str(dev.rx()))
        dev.rx_buffer_size = 20
        dev.rx_enabled_channels = [0, 1]

        if plot:
            fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(10, 8))
            turns_data = []
            angle_data = []

            def animate(i, turns_data, angle_data):
                try:
                    admt_data = dev.rx()
                except Exception as e:  # noqa: BLE001
                    log(f"Error retrieving data: {e}")
                    return
                turns_data.extend(admt_data[0])
                angle_data.extend(admt_data[1])
                del turns_data[:-200]
                del angle_data[:-200]

                ax1.clear()
                ax1.set_ylim([-10, 60])
                ax1.plot(turns_data, label="Turns", linewidth=2.0, color="orange")
                ax1.text(0.02, 0.95, f"Turns: {turns_data[-1]}", transform=ax1.transAxes)
                ax1.set_title("Turns")
                ax1.set_ylabel("Value")
                ax1.legend(loc="upper right")

                ax2.clear()
                ax2.set_ylim([0, 400])
                ax2.plot(angle_data, label="Angle", linewidth=2.0)
                ax2.text(
                    0.02, 0.95, f"Angle: {angle_data[-1]:.4f} °", transform=ax2.transAxes
                )
                ax2.set_title("Angle")
                ax2.set_ylabel("Value")
                ax2.legend(loc="upper right")

                plt.subplots_adjust(hspace=0.4)

            import matplotlib.animation as animation

            ani = animation.FuncAnimation(
                fig, animate, fargs=(turns_data, angle_data), interval=100
            )
            plt.show()
            del ani
    finally:
        del dev


################################################################################
# 2. Calibration (calibration_example.py)
################################################################################
def _run_motor_and_capture_angles(
    cal_kit, usteps_per_sample, samples_size, starting_angle, starting_turn, log
):
    data = []

    log("Moving motor to starting position...")
    cal_kit.rotate_absolute_angle = starting_angle + starting_turn * FULL_TURN
    read_faults(cal_kit.sensor, log)

    curr_abs_usteps = int(cal_kit.motor.angular_position.raw)
    log("Current position in microsteps: {}".format(curr_abs_usteps))
    for i in range(samples_size):
        curr_angle_raw = cal_kit.sensor.angle.raw
        curr_angle = curr_angle_raw * cal_kit.sensor.angle.scale
        curr_turns = cal_kit.sensor.turns.processed
        log("")
        log("-------------------------------------------")
        log("Sample #{}:".format(i))
        log("Current microsteps: {}".format(curr_abs_usteps))
        log("Turn count: {:.2f}".format(curr_turns))
        log("Angle:  {:.2f}° ({})".format(curr_angle, curr_angle_raw))
        curr_motor_angle = (
            cal_kit.motor.angular_position.raw
            % (
                360.0
                / (
                    cal_kit.motor.angular_position.scale
                    * cal_kit.motor.angular_position.calibscale
                )
            )
            * cal_kit.motor.angular_position.scale
            * cal_kit.motor.angular_position.calibscale
        )
        log("Current motor angle: {:.2f}°".format(curr_motor_angle))
        log(
            "Current Motor Abs Angle: {:.2f}°".format(
                cal_kit.motor.angular_position.processed
            )
        )
        log(
            "Current ADMT4000 Abs Angle: {:.2f}°".format(
                int(curr_turns) * 360 + curr_angle
            )
        )

        data.append((curr_angle, curr_motor_angle))
        cal_kit.rotate_relative_usteps = usteps_per_sample
        curr_abs_usteps = int(cal_kit.motor.angular_position.raw)
    return data


def run_calibration(
    uri,
    use_factory_correction,
    no_re_run,
    no_calibrate,
    zero_correction,
    full_step_angle,
    samples_size,
    cycles,
    starting_turn,
    starting_angle,
    reversed_shaft,
    cw_steps,
    plot,
    log,
    on_result=lambda data: None,
    dev=None,
):
    amax = 131072
    dmax = int(amax / 2)
    vmax = 127438 * 2

    do_re_run = not no_re_run
    calibrate = not no_calibrate
    direction = 1 if reversed_shaft else -1
    step_direction = -1 if cw_steps else 1

    owns_device = dev is None
    cal_setup = dev if dev is not None else admt4000ard1z(uri=uri, motor_direction=direction)
    try:
        # See run_magnetic_reset's matching comment: large multi-turn
        # rotations can exceed the serial connection's default read/write
        # timeout mid-move.
        cal_setup.ctx.set_timeout(30000)

        angle_scale = cal_setup.sensor.angle.scale
        temp_scale = cal_setup.sensor.temp.scale
        temp_offset = cal_setup.sensor.temp.offset

        curr_angle_raw = cal_setup.sensor.angle.raw
        curr_angle = curr_angle_raw * angle_scale
        curr_temp_raw = cal_setup.sensor.temp.raw
        curr_temp = (curr_temp_raw + temp_offset) * temp_scale / 1000
        curr_turns = cal_setup.sensor.turns.processed
        log("ADMT4000 Current Readings")
        log("Turn count: {:.2f}".format(curr_turns))
        log("Angle:  {:.2f}° ({})".format(curr_angle, curr_angle_raw))
        log("Absolute Angle:  {:.2f}°".format(curr_angle + int(curr_turns) * FULL_TURN))
        log("Temperature: {:.2f}°C".format(curr_temp))

        cal_setup.motor.amax = amax
        cal_setup.motor.vmax = vmax
        cal_setup.motor.dmax = dmax

        usteps_per_full_step = 1 / cal_setup.motor.angular_position.scale
        cal_setup.motor.angular_position.calibscale = full_step_angle

        full_turn_usteps = usteps_per_full_step * FULL_TURN / full_step_angle
        log(f"Full turn microsteps: {full_turn_usteps}")
        total_steps = cycles * FULL_TURN / full_step_angle
        log(f"Total steps: {total_steps}")
        total_usteps = total_steps * usteps_per_full_step
        log(f"Total microsteps: {total_usteps}")

        usteps_per_sample = step_direction * int(total_usteps / samples_size)
        log("Microsteps per sample: {}".format(usteps_per_sample))
        log(
            "Motor current position: {:.2f}° ({} usteps)".format(
                cal_setup.motor.angular_position.processed,
                cal_setup.motor.angular_position.raw,
            )
        )

        log("Aligning motor and sensor measurements...")
        cal_setup.motor_sensor_alignment = True
        log(
            "Motor current position: {:.2f}° ({} usteps)".format(
                cal_setup.motor.angular_position.processed,
                cal_setup.motor.angular_position.raw,
            )
        )

        if use_factory_correction:
            cal_setup.sensor.h8_corr_src = "factory"
        else:
            cal_setup.sensor.h8_corr_src = "user"

        if calibrate or zero_correction:
            log("Zeroing harmonic correction coefficients...")
            cal_setup.sensor.harmonics = [(0, 0), (0, 0), (0, 0), (0, 0)]

        cal_error_mag = cal_harmonic_errors = cal_scaled_harmonic_errors = None
        if calibrate:
            cal_setup.sensor.h8_corr_src = "user"
            data = _run_motor_and_capture_angles(
                cal_setup,
                usteps_per_sample,
                samples_size,
                starting_angle,
                starting_turn,
                log,
            )
            sensor_angles, motor_angles = zip(*data)
            with open("calibration_data.csv", mode="w", newline="") as file:
                writer = csv.writer(file)
                writer.writerow(["PANG (degrees)", "Ideal Angle"])
                for row in data:
                    writer.writerow(row)

            primary_angles = np.array(sensor_angles)
            ref_angles = np.array(motor_angles)
            (
                cal_scaled_harmonic_errors,
                cal_harmonic_errors,
                cal_angle_error,
                cal_error_mag,
            ) = cal_utils.harmonic_calibration(primary_angles, ref_angles)
            corrected_angle_error, corrected_error_mag = cal_utils.calculate_corrections(
                cal_angle_error, cal_harmonic_errors, ref_angles
            )

            print_harmonic_errors(
                cal_error_mag, cal_harmonic_errors, cal_scaled_harmonic_errors, log
            )

            log("Applying correction values to ADMT4000 registers...")
            harmonic_errors = [
                (cal_harmonic_errors[1]["magnitude"], cal_harmonic_errors[1]["phase"]),
                (cal_harmonic_errors[2]["magnitude"], cal_harmonic_errors[2]["phase"]),
                (cal_harmonic_errors[3]["magnitude"], cal_harmonic_errors[3]["phase"]),
                (cal_harmonic_errors[8]["magnitude"], cal_harmonic_errors[8]["phase"]),
            ]
            cal_setup.sensor.harmonics = harmonic_errors
            read_faults(cal_setup.sensor, log)

            on_result(
                {
                    "phase": "pre",
                    "primary_angles": primary_angles,
                    "angle_error": cal_angle_error,
                    "error_mag": cal_error_mag,
                    "corrected_angle_error": corrected_angle_error,
                    "corrected_error_mag": corrected_error_mag,
                }
            )

            if plot:
                cal_utils.print_cal_plots(
                    primary_angles,
                    cal_angle_error,
                    cal_error_mag,
                    corrected_angle_error,
                    corrected_error_mag,
                    plot_name="Pre-Calibration",
                    blocking=not do_re_run,
                )

        if do_re_run:
            data = _run_motor_and_capture_angles(
                cal_setup,
                usteps_per_sample,
                samples_size,
                starting_angle,
                starting_turn,
                log,
            )
            sensor_angles, motor_angles = zip(*data)
            primary_angles = np.array(sensor_angles)
            ref_angles = np.array(motor_angles)
            (
                scaled_harmonic_errors,
                harmonic_errors,
                angle_error,
                error_mag,
            ) = cal_utils.harmonic_calibration(primary_angles, ref_angles)
            print_harmonic_errors(error_mag, harmonic_errors, scaled_harmonic_errors, log)
            read_faults(cal_setup.sensor, log)

            on_result(
                {
                    "phase": "post",
                    "primary_angles": primary_angles,
                    "angle_error": angle_error,
                    "error_mag": error_mag,
                }
            )

            if plot:
                cal_utils.print_cal_plots(
                    primary_angles=primary_angles,
                    angle_error=angle_error,
                    error_mag=error_mag,
                    plot_name="Post-Calibration",
                    blocking=True,
                    show_computed=False,
                )

        if calibrate:
            log("\n\nResults Summary from the Calibration Run:")
            print_harmonic_errors(
                cal_error_mag, cal_harmonic_errors, cal_scaled_harmonic_errors, log
            )
            log(
                "Calibration coefficients applied to the device"
                " registers. You can power cycle the device to"
                " reset the coefficients to factory values or"
                " run again with zero-correction enabled to set"
                " the coefficients to zero."
            )
    finally:
        if owns_device:
            del cal_setup


################################################################################
# 3. Magnetic reset (magnetic_reset_example.py)
################################################################################
def run_magnetic_reset(
    uri,
    run_sweep,
    plot,
    dc_dc_charge_time_ms,
    reset_duration_ms,
    disable_dc_dc_after_reset,
    starting_turn,
    reversed_shaft,
    full_step_angle,
    log_to_file,
    log,
    on_result=lambda data: None,
    dev=None,
):
    reset_angle = 315.00
    amax = 131072
    dmax = int(amax / 2)
    vmax = 127438 * 2
    direction = 1 if reversed_shaft else -1

    fault_count = [0] * 16
    csv_filename = None
    if log_to_file:
        date = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        csv_filename = f"magnetic_reset_{date}.csv"
        log(f"Results will be saved to: {csv_filename}")

    owns_device = dev is None
    if dev is not None:
        # Per-request overrides: these are normally constructor kwargs read
        # later by the magnetic_reset setter, so when reusing an existing
        # device they must be applied directly instead.
        dev._disable_dc_dc_after_reset = disable_dc_dc_after_reset
        dev._dc_dc_charge_time_ms = dc_dc_charge_time_ms
        dev._reset_pulse_time_ms = reset_duration_ms
    else:
        dev = admt4000ard1z(
            uri=uri,
            motor_direction=direction,
            disable_dc_dc_after_reset=disable_dc_dc_after_reset,
            dc_dc_charge_time_ms=dc_dc_charge_time_ms,
            reset_pulse_time_ms=reset_duration_ms,
        )
    try:
        # Large multi-turn rotations (e.g. to a high starting_turn) can take
        # longer than the serial connection's default read/write timeout,
        # causing a transport-level timeout mid-move even though the motor
        # is still moving correctly. Raise it here rather than in the shared
        # context_manager base class, which every pyadi-iio driver uses.
        dev.ctx.set_timeout(30000)

        dev.motor.amax = amax
        dev.motor.vmax = vmax
        dev.motor.dmax = dmax

        try:
            board_id = dev.ctx._attrs["hw_mezzanine"]
        except KeyError:
            board_id = "EVAL-ADTM4000ARD1Z"

        if csv_filename:
            with open(csv_filename, "w") as f:
                f.write(f"BOARD ID, {board_id}\n")

        cnv_page = dev.sensor.reg_read(0x01)
        dev.sensor.reg_write(0x01, (cnv_page & 0x001F) | 0x02)
        gen_reg = dev.sensor.reg_read(0x10)
        log("ADMT4000 GENERAL Register: 0x{:04X}".format(gen_reg))
        dev.sensor.reg_write(0x01, cnv_page)

        print_current_position(motor=dev.motor, sensor=dev.sensor, log=log)

        dev.motor_sensor_alignment = True
        print_current_position(motor=dev.motor, sensor=dev.sensor, log=log)

        log(
            "\nRotating to turn = {} and angle = {}°...".format(
                starting_turn, reset_angle
            )
        )
        dev.rotate_absolute_angle = reset_angle + starting_turn * FULL_TURN
        print_current_position(motor=dev.motor, sensor=dev.sensor, log=log)

        dev.magnetic_reset = True
        sleep(2)
        print_current_position(motor=dev.motor, sensor=dev.sensor, log=log)
        dev.motor_sensor_alignment = True
        print_current_position(motor=dev.motor, sensor=dev.sensor, log=log)

        if not run_sweep:
            return

        log("\n\nStarting verification sweep...")
        quarter_turns = {"CCW": [], "CW": []}
        dev.motor_sensor_alignment = True

        sweep_filename = None
        if csv_filename:
            sweep_filename = csv_filename.replace(".csv", "_sweep.csv")
            with open(sweep_filename, "w") as f:
                f.write(
                    "Step, Direction, Expected Turn Count,"
                    " ADMT4000 Turn Count, ADMT4000 Angle (°),"
                    " ADMT4000 Abs Angle (°),"
                    " Motor ABSANGLE (°), Fault (0x)\n"
                )

        turns_list, angles_list, absolute_angles_list, fault_list = [], [], [], []

        for step_direction, sweep in [(-1, (45, 0)), (1, (0, 45))]:
            step_direction_str = "CCW" if step_direction == -1 else "CW"
            log(f"\n\nStarting verification sweep in {step_direction_str} direction...")
            start_angle = sweep[0] * FULL_TURN + 45.0
            log(
                f"Initial angle for this sweep: {start_angle:.2f}°"
                f" starting at turn {sweep[0]:.2f}"
            )

            quarter_turn = step_direction * FULL_TURN / 4
            print_current_position(motor=dev.motor, sensor=dev.sensor, log=log)
            dev.rotate_absolute_angle = start_angle
            print_current_position(motor=dev.motor, sensor=dev.sensor, log=log)
            new_angle = start_angle
            num_steps = abs((sweep[0] - sweep[1]) * 4)

            for i in range(num_steps + 1):
                log(f"\nStep {i}/{num_steps}:")
                sleep(0.1)
                curr_angle = dev.sensor.angle.processed
                curr_turns = dev.sensor.turns.processed
                motor_angle = dev.motor.angular_position.processed
                curr_abs_angle = math.floor(curr_turns) * FULL_TURN + curr_angle
                fault_reg = read_faults(dev.sensor, log)
                if fault_reg != 0:
                    for bit in range(16):
                        if fault_reg & (1 << bit):
                            fault_count[bit] += 1
                    log("  Warning: Fault detected after rotation!")
                log(
                    f"Step {i}"
                    f"(expected turn/absangle: {new_angle/360}/{new_angle}):"
                    f" ADMT4000 - Turns: {curr_turns:.2f},"
                    f" Angle: {curr_angle:.2f}°,"
                    f" Absolute: {curr_abs_angle:.2f}°,"
                    f" Motor Angle: {motor_angle:.2f}°,"
                    f" Fault: 0x{fault_reg:04X}"
                    f" (0b{fault_reg:016b})"
                )
                if sweep_filename:
                    with open(sweep_filename, "a") as f:
                        f.write(
                            f"{i}, {step_direction_str},"
                            f" {new_angle/360}, {curr_turns},"
                            f" {curr_angle}, {curr_abs_angle},"
                            f" {motor_angle}, {fault_reg:04x}\n"
                        )

                quarter_turns[step_direction_str].append(int(curr_turns * 4))
                turns_list.append(curr_turns)
                angles_list.append(curr_angle)
                absolute_angles_list.append(curr_abs_angle)
                fault_list.append(fault_reg)
                on_result(
                    {
                        "turns": curr_turns,
                        "angle": curr_angle,
                        "absolute_angle": curr_abs_angle,
                        "fault": fault_reg,
                    }
                )

                log(f"Rotating by {quarter_turn:.2f}°...")
                new_angle += quarter_turn
                dev.rotate_relative_angle = quarter_turn

        log("\nVerification sweep complete!")
        if any(count > 0 for count in fault_count):
            log("\n*** ERROR: Faults detected during verification! ***")
            log("Fault breakdown by bit:")
            for bit, count in enumerate(fault_count):
                if count > 0:
                    log(f"  Bit {bit}: {count} occurrences")

        test_pass = False
        qturn_ccw_start = quarter_turns["CCW"][0]
        for i, actual_qturn in enumerate(quarter_turns["CCW"]):
            expected_qturn = qturn_ccw_start - i
            if expected_qturn != actual_qturn:
                log(
                    f"*** ERROR: Mismatch in CCW quarter-turns at step {i}:"
                    f" expected {expected_qturn}, got {actual_qturn} ***"
                )
                test_pass = False
            else:
                test_pass = True

        qturn_cw_start = quarter_turns["CW"][0]
        for i, actual_qturn in enumerate(quarter_turns["CW"]):
            expected_qturn = qturn_cw_start + i
            if expected_qturn != actual_qturn:
                log(
                    f"*** ERROR: Mismatch in CW quarter-turns at step {i+1}:"
                    f" expected {expected_qturn}, got {actual_qturn} ***"
                )
                test_pass = False
            else:
                test_pass = test_pass and True

        if csv_filename:
            with open(csv_filename, "a") as f:
                f.write(f"CCW TURNS TEST, {test_pass}\n")
                f.write(
                    "CCW TURNS DATA, "
                    + " ".join(map(str, quarter_turns["CCW"]))
                    + "\n"
                )
                f.write(f"CW TURNS TEST, {test_pass}\n")
                f.write(
                    "CW TURNS DATA, " + " ".join(map(str, quarter_turns["CW"])) + "\n"
                )

        log("PASSED" if test_pass else "FAILED")

        if plot:
            colors_turns = ["red" if f != 0 else "blue" for f in fault_list]
            colors_angles = ["red" if f != 0 else "green" for f in fault_list]
            fig, (ax1, ax2, ax3) = plt.subplots(3, 1, figsize=(10, 8))
            ax1.scatter(range(len(turns_list)), turns_list, c=colors_turns, marker=".")
            ax1.set_title("Turn Count (Red = Fault)")
            ax2.scatter(
                range(len(angles_list)), angles_list, c=colors_angles, marker="o"
            )
            ax2.set_title("Angle (Red = Fault)")
            ax3.scatter(
                range(len(absolute_angles_list)),
                absolute_angles_list,
                c=colors_angles,
                marker=".",
            )
            ax3.set_title("Absolute Angle (Red = Fault)")
            fig.suptitle("PASSED" if test_pass else "FAILED")
            plt.tight_layout()
            plt.show()
    finally:
        if owns_device:
            del dev


################################################################################
# 4. TMC5240 basic motor move (tmc5240_basic_example.py)
################################################################################
def _wait_and_poll_position(dev, on_result, duration_s=2, poll_interval_s=0.1):
    """Sleep for duration_s total, reporting position every poll_interval_s
    so a live caller (e.g. the bridge) can chart motion during the wait
    instead of only before/after it."""
    for _ in range(int(duration_s / poll_interval_s)):
        on_result(dev.angular_position.processed)
        sleep(poll_interval_s)


def run_tmc5240_basic(uri, direction, log, on_result=lambda data: None, dev=None):
    amax = 131072
    dmax = int(amax / 2)
    vmax = 127438 * 2

    # admt4000ard1z.motor is a tmc5240 built from the same shared IIO
    # context, so reusing it here is a drop-in replacement for constructing
    # a standalone tmc5240 -- same attributes (amax/vmax/dmax/
    # angular_position) used throughout this function.
    owns_device = dev is None
    motor = dev.motor if dev is not None else tmc5240(uri=uri, direction=direction)
    dev = motor
    try:
        dev.amax = amax
        dev.vmax = vmax
        dev.dmax = dmax

        on_result(dev.angular_position.processed)
        log(
            "Current position: {:.4f}° ({:.4f}° wrapped) {} microsteps".format(
                dev.angular_position.processed,
                dev.angular_position.processed % 360,
                dev.angular_position.raw,
            )
        )
        target_angle = int(dev.angular_position.processed) + 120
        dev.angular_position.processed = target_angle
        log(
            "Target position: {:.4f}° {} microsteps".format(
                target_angle,
                target_angle
                / (dev.angular_position.scale * dev.angular_position.calibscale),
            )
        )
        _wait_and_poll_position(dev, on_result)
        log(
            "Current position: {:.4f}° ({:.4f}° wrapped) {} microsteps".format(
                dev.angular_position.processed,
                dev.angular_position.processed % 360,
                dev.angular_position.raw,
            )
        )

        move_actual = 0
        log("Set Current position to: {:.4f}°".format(move_actual))
        dev.angular_position.processed = move_actual
        _wait_and_poll_position(dev, on_result)
        log(
            "Current position: {:.4f}° ({:.4f}° wrapped) {} microsteps".format(
                dev.angular_position.processed,
                dev.angular_position.processed % 360,
                dev.angular_position.raw,
            )
        )

        target_usteps = int(dev.angular_position.raw) + 20000
        dev.angular_position.raw = target_usteps
        log(
            "Target position: {:.4f}° {} microsteps".format(
                target_usteps
                * dev.angular_position.scale
                * dev.angular_position.calibscale,
                target_usteps,
            )
        )
        _wait_and_poll_position(dev, on_result)
        log(
            "Current position: {:.4f}° ({:.4f}° wrapped) {} microsteps".format(
                dev.angular_position.processed,
                dev.angular_position.processed % 360,
                dev.angular_position.raw,
            )
        )

        target_usteps = int(dev.angular_position.raw) - 20000
        dev.angular_position.raw = target_usteps
        log(
            "Target position: {:.4f}° {} microsteps".format(
                target_usteps
                * dev.angular_position.scale
                * dev.angular_position.calibscale,
                target_usteps,
            )
        )
        _wait_and_poll_position(dev, on_result)
        log(
            "Current position: {:.4f}° ({:.4f}° wrapped) {} microsteps".format(
                dev.angular_position.processed,
                dev.angular_position.processed % 360,
                dev.angular_position.raw,
            )
        )
    finally:
        if owns_device:
            del dev


################################################################################
# 5. True power-down (true_powerdown_example.py)
################################################################################
def run_true_powerdown(uri, reversed_shaft, cw_steps, log, dev=None):
    direction = 1 if reversed_shaft else -1
    step_direction = -1 if cw_steps else 1

    amax = 131072
    dmax = int(amax / 2)
    vmax = 127438 * 2

    starting_turn = 12
    starting_angle = 0
    target_starting_angle = starting_angle + starting_turn * FULL_TURN
    target_displacement = 1090

    owns_device = dev is None
    if dev is None:
        dev = admt4000ard1z(uri=uri, motor_direction=direction)
    try:
        # See run_magnetic_reset's matching comment: large multi-turn
        # rotations can exceed the serial connection's default read/write
        # timeout mid-move.
        dev.ctx.set_timeout(30000)

        dev.motor.amax = amax
        dev.motor.vmax = vmax
        dev.motor.dmax = dmax
        dev.motor_sensor_alignment = True
        log("Homing to a known position...")
        dev.rotate_absolute_angle = target_starting_angle
        print_current_position(motor=dev.motor, sensor=dev.sensor, log=log)

        log(
            "\n\nMoving Motor by Angle while ADMT4000 is powered ON:"
            f" {target_displacement:.2f}°"
        )
        dev.rotate_relative_angle = step_direction * target_displacement
        print_current_position(motor=dev.motor, sensor=dev.sensor, log=log)

        log("ADMT4000 Power OFF")
        dev.power_enable = 0

        log(
            "\n\nMoving Motor by Angle while powered down:"
            f" {target_displacement:.2f}°"
        )
        dev.rotate_relative_angle = step_direction * target_displacement
        print_current_position(motor=dev.motor, sensor=None, log=log)

        log("ADMT4000 Power ON")
        dev.power_enable = 1
        sleep(2)
        dev.sensor.reinitialize()
        print_current_position(motor=dev.motor, sensor=dev.sensor, log=log)
    finally:
        if owns_device:
            del dev


################################################################################
# GUI
################################################################################
class Admt4000App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("ADMT4000 Example Suite")
        self.geometry("820x720")

        self._build_connection_panel()
        self._build_notebook()
        self._build_console()

    # -- shared widgets ---------------------------------------------------
    def _build_connection_panel(self):
        frame = ttk.Frame(self, padding=8)
        frame.pack(fill=tk.X)
        ttk.Label(frame, text="IIO URI:").pack(side=tk.LEFT)
        self.uri_var = tk.StringVar(value=DEFAULT_URI)
        ttk.Entry(frame, textvariable=self.uri_var, width=40).pack(
            side=tk.LEFT, padx=6
        )

    def _build_console(self):
        frame = ttk.LabelFrame(self, text="Output", padding=4)
        frame.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)
        self.console = scrolledtext.ScrolledText(frame, height=16, state="disabled")
        self.console.pack(fill=tk.BOTH, expand=True)

    def log(self, message):
        def _append():
            self.console.configure(state="normal")
            self.console.insert(tk.END, str(message) + "\n")
            self.console.see(tk.END)
            self.console.configure(state="disabled")

        self.after(0, _append)

    # -- tabs ---------------------------------------------------------------
    def _build_notebook(self):
        notebook = ttk.Notebook(self)
        notebook.pack(fill=tk.X, padx=8, pady=(0, 8))

        self._build_basic_tab(notebook)
        self._build_calibration_tab(notebook)
        self._build_magnetic_reset_tab(notebook)
        self._build_tmc5240_tab(notebook)
        self._build_powerdown_tab(notebook)

    def _run_in_thread(self, button, target, *args):
        def wrapper():
            try:
                target(*args)
            except Exception as e:  # noqa: BLE001
                self.log(f"ERROR: {e}")
                self.after(0, lambda: messagebox.showerror("Error", str(e)))
            finally:
                self.after(0, lambda: button.configure(state="normal"))

        button.configure(state="disabled")
        threading.Thread(target=wrapper, daemon=True).start()

    # 1. Basic ---------------------------------------------------------------
    def _build_basic_tab(self, notebook):
        tab = ttk.Frame(notebook, padding=8)
        notebook.add(tab, text="Basic Readout")

        self.basic_plot_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            tab, text="Enable real-time plotting", variable=self.basic_plot_var
        ).pack(anchor=tk.W)

        btn = ttk.Button(tab, text="Run Basic Readout")
        btn.configure(
            command=lambda: self._run_in_thread(
                btn,
                run_basic,
                self.uri_var.get(),
                self.basic_plot_var.get(),
                self.log,
            )
        )
        btn.pack(anchor=tk.W, pady=6)

    # 2. Calibration -----------------------------------------------------
    def _build_calibration_tab(self, notebook):
        tab = ttk.Frame(notebook, padding=8)
        notebook.add(tab, text="Calibration")

        self.cal_use_factory = tk.BooleanVar(value=False)
        self.cal_no_re_run = tk.BooleanVar(value=False)
        self.cal_no_calibrate = tk.BooleanVar(value=False)
        self.cal_zero_correction = tk.BooleanVar(value=True)
        self.cal_reversed_shaft = tk.BooleanVar(value=False)
        self.cal_cw_steps = tk.BooleanVar(value=False)
        self.cal_plot = tk.BooleanVar(value=False)
        self.cal_full_step_angle = tk.StringVar(value="0.9")
        self.cal_samples_size = tk.StringVar(value="256")
        self.cal_cycles = tk.StringVar(value="11")
        self.cal_starting_turn = tk.StringVar(value="12")
        self.cal_starting_angle = tk.StringVar(value="0.0")

        checks = ttk.Frame(tab)
        checks.pack(fill=tk.X)
        ttk.Checkbutton(
            checks, text="Use factory correction", variable=self.cal_use_factory
        ).grid(row=0, column=0, sticky=tk.W)
        ttk.Checkbutton(
            checks, text="No re-run after calibration", variable=self.cal_no_re_run
        ).grid(row=0, column=1, sticky=tk.W)
        ttk.Checkbutton(
            checks, text="Skip calibration (no-calibrate)", variable=self.cal_no_calibrate
        ).grid(row=1, column=0, sticky=tk.W)
        ttk.Checkbutton(
            checks, text="Zero correction before running", variable=self.cal_zero_correction
        ).grid(row=1, column=1, sticky=tk.W)
        ttk.Checkbutton(
            checks, text="Reversed shaft", variable=self.cal_reversed_shaft
        ).grid(row=2, column=0, sticky=tk.W)
        ttk.Checkbutton(
            checks, text="CW steps", variable=self.cal_cw_steps
        ).grid(row=2, column=1, sticky=tk.W)
        ttk.Checkbutton(
            checks, text="Enable plotting", variable=self.cal_plot
        ).grid(row=3, column=0, sticky=tk.W)

        fields = ttk.Frame(tab)
        fields.pack(fill=tk.X, pady=6)
        for i, (label, var) in enumerate(
            [
                ("Full step angle (deg)", self.cal_full_step_angle),
                ("Samples size", self.cal_samples_size),
                ("Cycles", self.cal_cycles),
                ("Starting turn", self.cal_starting_turn),
                ("Starting angle (deg)", self.cal_starting_angle),
            ]
        ):
            ttk.Label(fields, text=label + ":").grid(row=i, column=0, sticky=tk.W)
            ttk.Entry(fields, textvariable=var, width=12).grid(
                row=i, column=1, sticky=tk.W, padx=4
            )

        btn = ttk.Button(tab, text="Run Calibration")
        btn.configure(
            command=lambda: self._run_in_thread(
                btn,
                run_calibration,
                self.uri_var.get(),
                self.cal_use_factory.get(),
                self.cal_no_re_run.get(),
                self.cal_no_calibrate.get(),
                self.cal_zero_correction.get(),
                float(self.cal_full_step_angle.get()),
                int(self.cal_samples_size.get()),
                int(self.cal_cycles.get()),
                int(self.cal_starting_turn.get()),
                float(self.cal_starting_angle.get()),
                self.cal_reversed_shaft.get(),
                self.cal_cw_steps.get(),
                self.cal_plot.get(),
                self.log,
            )
        )
        btn.pack(anchor=tk.W)

    # 3. Magnetic reset ----------------------------------------------------
    def _build_magnetic_reset_tab(self, notebook):
        tab = ttk.Frame(notebook, padding=8)
        notebook.add(tab, text="Magnetic Reset")

        self.mr_run_sweep = tk.BooleanVar(value=True)
        self.mr_plot = tk.BooleanVar(value=False)
        self.mr_keep_dc_dc_enabled = tk.BooleanVar(value=False)
        self.mr_reversed_shaft = tk.BooleanVar(value=False)
        self.mr_log_to_file = tk.BooleanVar(value=False)
        self.mr_dc_dc_charge_time = tk.StringVar(value="300")
        self.mr_reset_duration = tk.StringVar(value="1")
        self.mr_starting_turn = tk.StringVar(value="35")
        self.mr_full_step_angle = tk.StringVar(value="0.9")

        checks = ttk.Frame(tab)
        checks.pack(fill=tk.X)
        ttk.Checkbutton(
            checks, text="Run verification sweep", variable=self.mr_run_sweep
        ).grid(row=0, column=0, sticky=tk.W)
        ttk.Checkbutton(checks, text="Enable plotting", variable=self.mr_plot).grid(
            row=0, column=1, sticky=tk.W
        )
        ttk.Checkbutton(
            checks, text="Keep DC-DC enabled after reset", variable=self.mr_keep_dc_dc_enabled
        ).grid(row=1, column=0, sticky=tk.W)
        ttk.Checkbutton(
            checks, text="Reversed shaft", variable=self.mr_reversed_shaft
        ).grid(row=1, column=1, sticky=tk.W)
        ttk.Checkbutton(
            checks, text="Log results to file", variable=self.mr_log_to_file
        ).grid(row=2, column=0, sticky=tk.W)

        fields = ttk.Frame(tab)
        fields.pack(fill=tk.X, pady=6)
        for i, (label, var) in enumerate(
            [
                ("DC-DC charge time (ms)", self.mr_dc_dc_charge_time),
                ("Reset pulse duration (ms)", self.mr_reset_duration),
                ("Starting turn", self.mr_starting_turn),
                ("Full step angle (deg)", self.mr_full_step_angle),
            ]
        ):
            ttk.Label(fields, text=label + ":").grid(row=i, column=0, sticky=tk.W)
            ttk.Entry(fields, textvariable=var, width=12).grid(
                row=i, column=1, sticky=tk.W, padx=4
            )

        btn = ttk.Button(tab, text="Run Magnetic Reset Test")
        btn.configure(
            command=lambda: self._run_in_thread(
                btn,
                run_magnetic_reset,
                self.uri_var.get(),
                self.mr_run_sweep.get(),
                self.mr_plot.get(),
                int(self.mr_dc_dc_charge_time.get()),
                int(self.mr_reset_duration.get()),
                not self.mr_keep_dc_dc_enabled.get(),
                int(self.mr_starting_turn.get()),
                self.mr_reversed_shaft.get(),
                float(self.mr_full_step_angle.get()),
                self.mr_log_to_file.get(),
                self.log,
            )
        )
        btn.pack(anchor=tk.W)

    # 4. TMC5240 basic -----------------------------------------------------
    def _build_tmc5240_tab(self, notebook):
        tab = ttk.Frame(notebook, padding=8)
        notebook.add(tab, text="TMC5240 Motor Move")

        self.tmc_direction = tk.StringVar(value="-1")
        ttk.Label(tab, text="Direction (1=CW, -1=CCW):").pack(anchor=tk.W)
        ttk.Entry(tab, textvariable=self.tmc_direction, width=6).pack(anchor=tk.W)

        btn = ttk.Button(tab, text="Run Motor Move Demo")
        btn.configure(
            command=lambda: self._run_in_thread(
                btn,
                run_tmc5240_basic,
                self.uri_var.get(),
                int(self.tmc_direction.get()),
                self.log,
            )
        )
        btn.pack(anchor=tk.W, pady=6)

    # 5. True power-down -----------------------------------------------------
    def _build_powerdown_tab(self, notebook):
        tab = ttk.Frame(notebook, padding=8)
        notebook.add(tab, text="True Power-down")

        self.pd_reversed_shaft = tk.BooleanVar(value=False)
        self.pd_cw_steps = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            tab, text="Reversed shaft", variable=self.pd_reversed_shaft
        ).pack(anchor=tk.W)
        ttk.Checkbutton(tab, text="CW steps", variable=self.pd_cw_steps).pack(
            anchor=tk.W
        )

        btn = ttk.Button(tab, text="Run Power-down Test")
        btn.configure(
            command=lambda: self._run_in_thread(
                btn,
                run_true_powerdown,
                self.uri_var.get(),
                self.pd_reversed_shaft.get(),
                self.pd_cw_steps.get(),
                self.log,
            )
        )
        btn.pack(anchor=tk.W, pady=6)


if __name__ == "__main__":
    app = Admt4000App()
    app.mainloop()
