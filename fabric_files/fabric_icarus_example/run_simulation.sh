#!/usr/bin/env bash
set -ex
DESIGN=counter
BITSTREAM=test_design/${DESIGN}.bin
VERILOG=../../fabric_generator/verilog_output
MAX_BITBYTES=16384

iverilog $VERILOG/* test_design/${DESIGN}.v fabulous_tb.v -s fab_tb -o fab_tb.vvp
python3 makehex.py $BITSTREAM $MAX_BITBYTES bitstream.hex
vvp fab_tb.vvp