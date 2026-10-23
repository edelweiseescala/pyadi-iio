# Copyright (C) 2026 Analog Devices, Inc.
#
# SPDX short identifier: ADIBSD
"""ADMT4000 basic example using polled (unbuffered) attribute reads.

Use this version on backends without buffered capture support, e.g. the
Zephyr admt4000 IIO driver, which only implements create/read_attr/write_attr
(no open_buffer/readbuf hook). See basic_example.py for the buffered
(dev.rx()) version intended for backends with buffer support, like no-OS.
"""
import argparse
import time
from typing import List

import matplotlib.animation as animation
import matplotlib.pyplot as plt

from adi.admt4000 import admt4000

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="ADMT4000 basic example (polling, no buffer support required)"
    )
    parser.add_argument(
        "--uri",
        type=str,
        default="serial:COM10,230400,8n1",
        help="IIO context URI. Use 'ip:<address>' for Ethernet"
        " or 'serial:<port>,<baud>,8n1' for USB serial.",
    )
    parser.add_argument(
        "--plot",
        action="store_true",
        help="Enable real-time plotting of angle and turns data.",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="Continuously poll and print readings to the console (no"
        " display/matplotlib required). Independent of --plot; combine both"
        " to also see the live chart.",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=0.5,
        help="Polling interval in seconds for --live (default: 0.5).",
    )
    args = parser.parse_args()

    iio_context = args.uri
    enable_plot = args.plot
    enable_live = args.live
    poll_interval = args.interval

    ################################################################################
    # Use this with MCUs running tinyiiod and make sure to use the correct com port.
    dev = admt4000(uri=iio_context)

    angle_scale = dev.angle.scale
    temp_scale = dev.temp.scale
    temp_offset = dev.temp.offset

    curr_angle_raw = dev.angle.raw
    curr_angle = (curr_angle_raw) * angle_scale
    curr_temp_raw = dev.temp.raw
    curr_temp = (curr_temp_raw + temp_offset) * temp_scale / 1000
    print("ADMT4000 Current Readings")
    print("Turn count: " + str(dev.turns.raw))
    print("Angle: " + str(curr_angle_raw) + " (" + str(curr_angle) + " degrees)")
    print("Temperature: " + str(curr_temp) + " C")

    if enable_live:
        print("\nPolling continuously (Ctrl+C to stop)...")
        try:
            while True:
                turn_count = dev.turns.processed
                angle_deg = dev.angle.processed
                # dev.temp.processed is already in whole degrees C on this
                # backend (confirmed via iio_info: scale=0.000001 against a
                # raw value in the tens of millions), unlike the top-level
                # curr_temp calculation above which divides by 1000 to match
                # the no-OS buffered path's milli-degree scale.
                temp_c = dev.temp.processed
                print(
                    f"Turns: {turn_count:.4f}  "
                    f"Angle: {angle_deg:.4f} deg  "
                    f"Temp: {temp_c:.2f} C"
                )
                time.sleep(poll_interval)
        except KeyboardInterrupt:
            print("\nStopped.")

    if enable_plot:
        # Create figure for plotting
        fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(10, 8))
        turns: List[int] = []
        angle: List[float] = []

        def animate(i, turns, angle):
            """Function called to animate in real time by polling raw attributes"""

            # Poll one sample per channel directly, no buffered capture
            try:
                turn_count = dev.turns.processed
                angle_deg = dev.angle.processed
            except Exception as e:
                print(f"Error retrieving data: {e}")
                return

            # Continuously adding samples to lists
            turns.append(turn_count)
            angle.append(angle_deg)

            # Limit data lists to 200 items
            turns = turns[-200:]
            angle = angle[-200:]

            # Draw x and y lists for first subplot
            ax1.clear()
            ax1.set_ylim([-10, 60])
            ax1.plot(turns, label="Turns", linewidth=2.0, color="orange")
            ax1.text(
                0.02, 0.95, f"Turns: {turns[-1]}", transform=ax1.transAxes,
            )
            ax1.set_title("Turns")
            ax1.set_ylabel("Value")
            ax1.legend(loc="upper right")

            # Draw x and y lists for second subplot
            ax2.clear()
            ax2.set_ylim([0, 400])
            ax2.plot(angle, label="Angle", linewidth=2.0)
            ax2.text(
                0.02, 0.95, f"Angle: {angle[-1]:.4f} °", transform=ax2.transAxes,
            )
            ax2.set_title("Angle")
            ax2.set_ylabel("Value")
            ax2.legend(loc="upper right")

            # Format plot
            plt.subplots_adjust(hspace=0.4)

        # Set up plot to call animate() function periodically
        ani = animation.FuncAnimation(fig, animate, fargs=(turns, angle), interval=100)
        plt.show()

    del dev
