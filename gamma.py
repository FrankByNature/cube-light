# -*- coding: utf-8 -*-
"""
Created on Sun Mar  8 17:01:04 2026

@author: TJFra
"""
Bcap = 78

for i in range(256):
    linear = (i / 255.0) ** 2.2  # gamma 2.2
    scaled = linear * Bcap
    integer = int(scaled)
    fraction = int((scaled - integer) * 256)
    #print(integer, fraction)
    print(f"0x{integer:02X}{fraction:02X}", end=", ")