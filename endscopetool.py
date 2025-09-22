#!/usr/bin/env python3
# Python implementation of the endscopetool (sic!) Android application used for the Vitcoco ear wax remover camera thingy.
# CC-0 / Public Domain
# (0) 2023 Raphael Wimmer
# v0.1.0
# reverse-engineered using a packet capture log - this means that I have no idea what all those magic numbers mean
# and whether there are further features that might be supported by the hardware
# usage: first connect to the 'softish-XXXX' wifi, then run this script. Check code for keyboard shortcuts.

import socket
import sys
import cv2
import numpy as np
import time
import collections
from PIL import Image
from io import BytesIO
from urllib.parse import parse_qs

def get_battery_level(query_string):
    """
    Extracts the battery level from a string like 'type=2001&data=23'.
    Returns an integer or None if not found or invalid.
    """
    try:
        params = parse_qs(query_string)
        return int(params["data"][0]) / 100
    except (KeyError, IndexError, ValueError):
        return None

def draw_battery(img, x, y, width, height, level):
    """
    Draw a battery icon at (x, y) with given width, height and charge level (0 to 1).
    """
    # Clamp level to [0, 1]
    level = max(0, min(float(level), 1.0))

    # Colors
    border_color = (255, 255, 255)
    fill_color = (0, 255, 0) if level > 0.3 else (0, 0, 255)  # Red if low battery

    # Draw battery outline
    cv2.rectangle(img, (x, y), (x + width, y + height), border_color, 2)

    # Draw battery tip
    tip_width = int(width * 0.08)
    tip_x = x + width
    tip_y = y + int(height * 0.3)
    tip_height = int(height * 0.4)
    cv2.rectangle(img, (tip_x, tip_y), (tip_x + tip_width, tip_y + tip_height), border_color, -1)

    # Fill battery level
    fill_width = int((width - 4) * level)
    cv2.rectangle(img, (x + 2, y + 2), (x + 2 + fill_width, y + height - 2), fill_color, -1)

def absolute_frame_from_raw(raw_frame, latest_abs_frame):
    # Find the multiple of 256 that makes raw_frame closest to latest_abs_frame
    base = (latest_abs_frame // 256) * 256
    candidates = [base - 256 + raw_frame, base + raw_frame, base + 256 + raw_frame]
    # pick the candidate closest to latest_abs_frame
    abs_frame = min(candidates, key=lambda x: abs(x - latest_abs_frame))
    return abs_frame

buffer_size = 1500
target_ip = "192.168.1.1"
target_port_meta = 61502
source_port_meta = 50262

target_port_vid = 61503
source_port_vid = 51320

sock_meta = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock_meta.bind(("0.0.0.0", source_port_meta))

sock_vid = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock_vid.bind(("0.0.0.0", source_port_vid))
sock_vid.settimeout(5.0)
brightness = 100

try:
    # get system info
    data = "type=1002\x0a".encode()
    sock_meta.sendto(data, (target_ip, target_port_meta))
    reply, addr = sock_meta.recvfrom(buffer_size)
    received_data = reply.decode()

    print("Received data:", received_data)

    # Battery?
    data = "type=1001\x0a".encode()
    sock_meta.sendto(data, (target_ip, target_port_meta))
    reply, addr = sock_meta.recvfrom(buffer_size)
    received_data = reply.decode()

    print("Received data (Battery level?):", received_data)
    # print("Sender address:", addr)

    # three times according to captured traffic
    data = "\x20\x36\x00\x02".encode()
    sock_vid.sendto(data, (target_ip, target_port_vid))
    sock_vid.sendto(data, (target_ip, target_port_vid))
    sock_vid.sendto(data, (target_ip, target_port_vid))

    # set led brightness to 100%
    data = "type=1003&value=100\x0a".encode()
    # start with led off
    # data = "type=1003&value=0\x0a".encode()
    sock_meta.sendto(data, (target_ip, target_port_meta))
    reply, addr = sock_meta.recvfrom(buffer_size)
    # handle UnicodeDecodeError: 'utf-8' codec can't decode byte 0xaa in position 21: invalid start byte gracefully
    try:
        received_data = reply.decode()
        print("Received data:", received_data)
    except UnicodeDecodeError:
        print("UnicodeDecodeError, can be ignored")
    # print("Sender address:", addr)

    cv2.namedWindow("Video Stream", cv2.WINDOW_NORMAL)
    cv2.resizeWindow("Video Stream", 1280, 720)

    rotation_lock = False
    fullframe = False

    battery_level = 0
    latest_frame = 0
    raw_frame = 0
    frame = 0
    part = 0
    pic_buf = bytearray()
    JPEG_HEADER = bytes.fromhex("FF D8 FF E0 00 10 4A 46 49 46")
    keep_awake_time = time.time()

    # Store received parts per frame
    frames_dict = {}  # frame_number -> {part_number: pic_data}
    parts_dict = {} # number of parts required per frame

    while True:
        # read video stream
        reply, addr = sock_vid.recvfrom(buffer_size)
        raw_frame = reply[0]
        frame_end = reply[1]
        part = reply[2]
        part_end = reply[3]
        misc_data = reply[4:8]
        if not rotation_lock:
            rotation = int.from_bytes(reply[4:6], "big")
        pic_data = reply[8:]

        frame = absolute_frame_from_raw(raw_frame, frame)

        # store the part
        if frame not in frames_dict:
            frames_dict[frame] = {}
        frames_dict[frame][part] = pic_data

        # find number of frames required
        if frame_end == 1:
            parts_dict[frame] = part_end

        if frame in parts_dict:
            if parts_dict[frame] == len(frames_dict[frame]):
                pic_buf = b''.join(frames_dict[frame][i] for i in range(parts_dict[frame]))

                try:
                    image = Image.open(BytesIO(pic_buf))
                    image_np = np.array(image)
                    image_cv = cv2.cvtColor(image_np, cv2.COLOR_RGB2BGR)
                    num_rows, num_cols = image_cv.shape[:2]

                    if not fullframe:
                        # Case 1: Masked circle. The window will be a square of the SHORTER dimension.
                        square_size = min(num_rows, num_cols)

                        # Create a circular mask on the original image dimensions
                        mask = np.zeros((num_rows, num_cols), np.uint8)
                        cv2.circle(
                            mask,
                            (num_cols // 2, num_rows // 2),
                            square_size // 2,
                            255,
                            -1,
                        )
                        image_masked = cv2.bitwise_and(image_cv, image_cv, mask=mask)

                        # Get rotation matrix for the original image
                        rotation_matrix = cv2.getRotationMatrix2D(
                            (num_cols / 2, num_rows / 2), rotation + 90, 1
                        )
                        # Rotate the masked image within its original frame
                        image_rotated = cv2.warpAffine(
                            image_masked, rotation_matrix, (num_cols, num_rows)
                        )

                        # Crop the center square from the rotated image
                        center_x, center_y = num_cols // 2, num_rows // 2
                        half_size = square_size // 2
                        image_to_show = image_rotated[
                            center_y - half_size : center_y + half_size,
                            center_x - half_size : center_x + half_size,
                        ]

                    else:
                        # Case 2: Full frame, ensuring no corners are ever cropped.
                        # The window will be a square with side length equal to the image diagonal.

                        # Calculate the length of the image diagonal
                        diagonal = np.sqrt(num_cols**2 + num_rows**2)

                        # The new square size is the diagonal, rounded up to the nearest integer
                        square_size = int(np.ceil(diagonal))

                        # Get the rotation matrix centered on the original image
                        rotation_matrix = cv2.getRotationMatrix2D(
                            (num_cols / 2, num_rows / 2), rotation + 90, 1
                        )

                        # Adjust the matrix's translation component to center the image on the new, larger canvas
                        tx = (square_size - num_cols) / 2
                        ty = (square_size - num_rows) / 2
                        rotation_matrix[0, 2] += tx
                        rotation_matrix[1, 2] += ty

                        # Warp the original image onto the new square canvas
                        image_to_show = cv2.warpAffine(
                            image_cv, rotation_matrix, (square_size, square_size)
                        )

                    draw_battery(image_to_show, x=5, y=5, width=15, height=8, level=battery_level)
                    cv2.imshow("Video Stream", image_to_show)

                    #delete earlier frame data
                    frames_dict = {f: frames_dict[f] for f in frames_dict if f >= frame}
                    parts_dict = {f: parts_dict[f] for f in parts_dict if f >= frame}

                    if time.time() > keep_awake_time:
                        keep_awake_time = time.time() + 10
                        # Battery?
                        data = "type=1001\x0a".encode()
                        sock_meta.sendto(data, (target_ip, target_port_meta))
                        reply, addr = sock_meta.recvfrom(buffer_size)
                        battery_level = get_battery_level(reply.decode())

                except OSError:
                    print("image corrupted")
            key = cv2.waitKey(1) & 0xFF
            if key == ord("1"):
                rotation_lock = True
                rotation = 0
            elif key == ord("2"):
                rotation_lock = True
                rotation = 90
            elif key == ord("3"):
                rotation_lock = True
                rotation = 180
            elif key == ord("4"):
                rotation_lock = True
                rotation = 270
            elif key == ord("r"):
                rotation_lock = False
            elif key == ord("q"):
                break
            elif key == ord("w"):
                fd = open("out.jpg", "wb")
                ret = fd.write(pic_buf)
                fd.close()
                print("Wrote " + str(ret) + " bytes to out.jpg")
            elif key == ord("+"):
                if brightness < 100:
                    brightness += 10
                    data = ("type=1003&value=" + str(brightness) + "\x0a").encode()
                    print("Send data: ", data)
                    sock_meta.sendto(data, (target_ip, target_port_meta))
                    reply, addr = sock_meta.recvfrom(buffer_size)
                    received_data = reply.decode()
                    print("Received data:", received_data)
            elif key == ord("-"):
                if brightness > 0:
                    brightness -= 10
                    data = ("type=1003&value=" + str(brightness) + "\x0a").encode()
                    print("Send data: ", data)
                    sock_meta.sendto(data, (target_ip, target_port_meta))
                    reply, addr = sock_meta.recvfrom(buffer_size)
                    received_data = reply.decode()
                    print("Received data:", received_data)
            elif key == ord("f"):
                fullframe = not fullframe


finally:
    # stop stream
    data = "\x20\x37".encode()
    sock_vid.sendto(data, (target_ip, target_port_vid))
    # Close the socket
    sock_meta.close()
    sock_vid.close()
