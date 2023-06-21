#!/usr/bin/env python3
# Python implementation of the endscopetool (sic!) Android application used for the Vitcoco ear wax remover camera thingy.
# CC-0 / Public Domain
# (0) 2023 Raphael Wimmer
# v0.01
# reverse-engineered using a packet capture log - this means that I have no idea what all those magic numbers mean
# and whether there are further features that might be supported by the hardware
# usage: first connect to the 'softish-XXXX' wifi, then run this script. Check code for keyboard shortcuts.

import socket
import sys
import cv2
import numpy as np
from PIL import Image
from io import BytesIO


buffer_size = 1500
target_ip = "192.168.1.1"
target_port_meta = 61502
source_port_meta = 50262

target_port_vid = 61503
source_port_vid = 51320

sock_meta = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock_meta.bind(('0.0.0.0', source_port_meta))

sock_vid = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock_vid.bind(('0.0.0.0', source_port_vid))
sock_vid.settimeout(5.0) 


try:
    # get system info
    data = "type=1002\x0a".encode()
    sock_meta.sendto(data, (target_ip, target_port_meta))
    reply, addr = sock_meta.recvfrom(buffer_size)
    received_data = reply.decode()

    print("Received data:", received_data)
    #print("Sender address:", addr)
    # type=2002&protocol=2&w=640&h=480&fps=20&ratio=4:3&angle=270&hardware=V1.1&company=vitcoco&id=a07b4c3092607cf29daaab607cf20000&firmware=1820220727&ssid=softish-31986&dn=Y8&bl=30

    # Battery?
    data = "type=1001\x0a".encode()
    sock_meta.sendto(data, (target_ip, target_port_meta))
    reply, addr = sock_meta.recvfrom(buffer_size)
    received_data = reply.decode()

    print("Received data (Battery level?):", received_data)
    #print("Sender address:", addr)

    # three times according to captured traffic
    data = "\x20\x36\x00\x02".encode()
    sock_vid.sendto(data, (target_ip, target_port_vid))
    sock_vid.sendto(data, (target_ip, target_port_vid))
    sock_vid.sendto(data, (target_ip, target_port_vid))

    # no idea what this command does
    data = "type=1003&value=100\x0a".encode()
    sock_meta.sendto(data, (target_ip, target_port_meta))
    reply, addr = sock_meta.recvfrom(buffer_size)
    received_data = reply.decode()

    print("Received data:", received_data)
    #print("Sender address:", addr)

    cv2.namedWindow("Video Stream", cv2.WINDOW_NORMAL)
    #cv2.setWindowProperty("Video Stream", cv2.WND_PROP_FULLSCREEN, cv2.WINDOW_FULLSCREEN)
    
    rotation_lock = False

    frame = 0
    part = 0
    pic_buf = bytearray()
    JPEG_HEADER = bytes.fromhex("FF D8 FF E0 00 10 4A 46 49 46")
    while True:
        # read video stream
        reply, addr = sock_vid.recvfrom(buffer_size)
        frame = reply[0]
        frame_end = reply[1]
        part = reply[2]
        part_end = reply[3]
        misc_data =  reply[4:8]
        if not rotation_lock:
            rotation = int.from_bytes(reply[4:6], "big")
        pic_data = reply[8:]
        if pic_data.find(JPEG_HEADER) > -1:
            if len(pic_buf) > 0:
                try:
                    image = Image.open(BytesIO(pic_buf))
                    image_np = np.array(image)
                    image_cv = cv2.cvtColor(image_np, cv2.COLOR_RGB2BGR)
                    num_rows, num_cols = image_cv.shape[:2]
                    mask = np.zeros((num_rows, num_cols), np.uint8)
                    cv2.circle(mask, (num_cols//2,num_rows//2), num_rows//2, 255, -1)
                    image_masked = cv2.bitwise_and(image_cv, image_cv, mask = mask)
                    rotation_matrix = cv2.getRotationMatrix2D((num_cols/2, num_rows/2), rotation + 90, 1)
                    image_rotated = cv2.warpAffine(image_masked, rotation_matrix, (num_cols, num_rows))
                    cv2.imshow('Video Stream', image_rotated)
                except OSError:
                    print("image corrupted")
            key = cv2.waitKey(1) & 0xFF
            if key == ord('1'):
                rotation_lock = True
                rotation = 0
            elif key == ord('2'):
                rotation_lock = True
                rotation = 90
            elif key == ord('3'):
                rotation_lock = True
                rotation = 180
            elif key == ord('4'):
                rotation_lock = True
                rotation = 270
            elif key == ord('r'):
                rotation_lock = False
            elif key == ord('q'):
                break
            elif key == ord('w'):
                fd = open("out.jpg", "wb")
                ret = fd.write(pic_buf)
                fd.close()
                print("Wrote " + str(ret) + " bytes to out.jpg")
            #print("new frame")
            pic_buf = bytearray()
        pic_buf += pic_data
        #print(frame, part, len(pic_buf))
        #print(misc_data[0], misc_data[1], misc_data[2], misc_data[3])
        #print(rotation)


finally:
    # stop stream
    data = "\x20\x37".encode()
    sock_vid.sendto(data, (target_ip, target_port_vid))
    # Close the socket
    sock_meta.close()
    sock_vid.close()
