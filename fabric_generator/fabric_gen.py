#!/bin/env python3
# Copyright 2021 University of Manchester
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# SPDX-License-Identifier: Apache-2.0


from curses.ascii import isdigit
import json
import re
import math
import os
import string
import numpy
import pickle
import csv
from typing import Dict, List, Tuple
import argparse
import logging


from fasm import *  # Remove this line if you do not have the fasm library installed and will not be generating a bitstream


from fabric_generator.file_parser import parseFabricCSV, parseMatrix, parseConfigMem, parseList
from fabric_generator.fabric import IO, Direction, Side, MultiplexerStyle, ConfigBitMode
from fabric_generator.fabric import Fabric, Tile, Port, SuperTile, ConfigMem, Wire
from fabric_generator.utilities import GetFabric, PrintCSV_FileInfo, genFabricObject
import fabric_generator.model_generation_vpr as vpr
import fabric_generator.model_generation_npnr as npnr
from fabric_generator.code_generation_VHDL import VHDLWriter
from fabric_generator.code_generation_Verilog import VerilogWriter
from fabric_generator.code_generator import codeGenerator

SWITCH_MATRIX_DEBUG_SIGNAL = True
logger = logging.getLogger(__name__)


class FabricGenerator:
    fabric: Fabric
    writer: codeGenerator

    def __init__(self, fabric: Fabric, writer: codeGenerator):
        self.fabric = fabric
        self.writer = writer

    def bootstrapSwitchMatrix(self, tile: Tile, outputDir: str):
        logger.info(
            f"Generate matrix csv for {tile.name} # filename: {outputDir}")
        with open(f"{outputDir}", "w") as f:
            writer = csv.writer(f)
            sourceName, destName = [], []
            # normal wire
            for i in tile.portsInfo:
                if i.wireDirection != Direction.JUMP:
                    input, output = i.expandPortInfo("AutoSwitchMatrix")
                    sourceName += input
                    destName += output
            # bel wire
            for b in tile.bels:
                for p in b.inputs:
                    sourceName.append(f"{p}")
                for p in b.outputs + b.externalOutput:
                    destName.append(f"{p}")

            # jump wire
            for i in tile.portsInfo:
                if i.wireDirection == Direction.JUMP:
                    input, output = i.expandPortInfo("AutoSwitchMatrix")
                    sourceName += input
                    destName += output
            sourceName = list(dict.fromkeys(sourceName))
            destName = list(dict.fromkeys(destName))
            writer.writerow([tile.name] + destName)
            for p in sourceName:
                writer.writerow([p] + [0] * len(destName))

    def list2CSV(self, InFileName, OutFileName):
        # this function is export a given list description into its equivalent CSV switch matrix description
        # format: source,destination (per line)
        # read CSV file into an array of strings

        logger.info(f"Adding {InFileName} to {OutFileName}")

        connectionPair = parseList(InFileName)

        with open(OutFileName, "r") as f:
            file = f.read()
            file = re.sub(r"#.*", "", file)
            file = file.split("\n")

        col = len(file[0].split(','))
        rows = len(file)

        # create a 0 zero matrix as initialization
        matrix = [[0 for _ in range(col)] for _ in range(rows)]

        # load the data from the original csv into the matrix
        for i in range(1, len(file)):
            for j in range(1, len(file[i].split(','))):
                value = file[i].split(',')[j]
                if value == "":
                    continue
                matrix[i-1][j-1] = int(value)

        # get source and destination list in the csv
        destination = file[0].strip("\n").split(',')[1:]
        source = [file[i].split(",")[0] for i in range(1, len(file))]

        # set the matrix value with the provided connection pair
        for (s, d) in connectionPair:
            try:
                s_index = source.index(s)
            except ValueError:
                logger.critical(
                    f"{s} is not in the source column of the matrix csv file")
                exit(-1)

            try:
                d_index = destination.index(d)
            except ValueError:
                logger.critical(
                    f"{d} is not in the destination row of the matrix csv file")
                exit(-1)

            if matrix[s_index][d_index] != 0:
                logger.warning(
                    f"connection ({s}, {d}) already exists in the original matrix")
            matrix[s_index][d_index] = 1

        # writing the matrix back to the given out file
        with open(OutFileName, "w") as f:
            f.write(file[0] + "\n")
            for i in range(len(source)):
                f.write(f"{source[i]},")
                for j in range(len(destination)):
                    f.write(str(matrix[i][j]))
                    if j != len(destination) - 1:
                        f.write(',')
                    else:
                        f.write(f",#,{matrix[i].count(1)}")
                f.write('\n')
            colCount = []
            for j in range(col):
                count = 0
                for i in range(rows):
                    if matrix[i][j] == 1:
                        count += 1
                # if j != col - 1:
                #     f.write(str(count) + ',')
                colCount.append(str(count))
            f.write(f"#,{','.join(colCount)}")

    def CSV2list(self, InFileName, OutFileName):
        # this function is export a given CSV switch matrix description into its equivalent list description
        # format: destination,source (per line)
        # read CSV file into an array of strings
        InFile = [i.strip('\n').split(',') for i in open(InFileName)]
        with open(OutFileName, "w") as f:
            # get the number of tiles in vertical direction
            rows = len(InFile)
            # get the number of tiles in horizontal direction
            cols = len(InFile[0])
            # top-left should be the name
            print('#', InFile[0][0], file=f)
            # switch matrix inputs
            inputs = []
            for item in InFile[0][1:]:
                inputs.append(item)
            # beginning from the second line, write out the list
            for line in InFile[1:]:
                for i in range(1, cols):
                    if line[i] != '0':
                        # it is [i-1] because the beginning of the line is the destination port
                        print(line[0]+','+inputs[i-1], file=f)
        return

    def generateConfigMemInit(self, file, globalConfigBitsCounter):
        # write configuration bits to frame mapping init file (e.g. 'LUT4AB_ConfigMem.init.csv')
        # this file can be modified and saved as 'LUT4AB_ConfigMem.csv' (without the '.init')
        bitsLeftToPackInFrames = globalConfigBitsCounter

        fieldName = ["frame_name", "frame_index", "bits_used_in_frame",
                     "used_bits_mask", "ConfigBits_ranges"]

        frameBitPerRow = self.fabric.frameBitsPerRow
        with open(file, "w") as f:
            writer = csv.writer(f)
            writer.writerow(fieldName)
            for k in range(self.fabric.maxFramesPerCol):
                entry = []
                # frame0, frame1, ...
                entry.append(f"frame{k}")
                # and the index (0, 1, 2, ...), in case we need
                entry.append(str(k))
                # size of the frame in bits
                if bitsLeftToPackInFrames >= frameBitPerRow:
                    entry.append(str(frameBitPerRow))
                    # generate a string encoding a '1' for each flop used
                    frameBitsMask = f"{2**frameBitPerRow-1:_b}"
                    entry.append(frameBitsMask)
                    entry.append(
                        f"{bitsLeftToPackInFrames-1}:{bitsLeftToPackInFrames-frameBitPerRow}")
                    bitsLeftToPackInFrames -= frameBitPerRow
                else:
                    entry.append(str(bitsLeftToPackInFrames))
                    # generate a string encoding a '1' for each flop used
                    # this will allow us to kick out flops in the middle (e.g. for alignment padding)
                    frameBitsMask = (2**frameBitPerRow-1) - \
                        (2**(frameBitPerRow-bitsLeftToPackInFrames)-1)
                    frameBitsMask = f"{frameBitsMask:0{frameBitPerRow+7}_b}"
                    entry.append(frameBitsMask)
                    if bitsLeftToPackInFrames > 0:
                        entry.append(f"{bitsLeftToPackInFrames-1}:0")
                    else:
                        entry.append("# NULL")
                    # will have to be 0 if already 0 or if we just allocate the last bits
                    bitsLeftToPackInFrames = 0
                # The mapping into frames is described as a list of index ranges applied to the ConfigBits vector
                # use '2' for a single bit; '5:0' for a downto range; multiple ranges can be specified in optional consecutive comma separated fields get concatenated)
                # default is counting top down

                # write the entry to the file
                writer.writerow(entry)

    def generateConfigMem(self, tile: Tile, configMemCsv: str):
        # we use a file to describe the exact configuration bits to frame mapping
        # the following command generates an init file with a simple enumerated default mapping (e.g. 'LUT4AB_ConfigMem.init.csv')
        # if we run this function again, but have such a file (without the .init), then that mapping will be used

        # test if we have a bitstream mapping file
        # if not, we will take the default, which was passed on from  GenerateConfigMemInit
        configMemList: List[ConfigMem] = []
        if os.path.exists(configMemCsv):
            configMemList = parseConfigMem(
                configMemCsv, self.fabric.maxFramesPerCol, self.fabric.frameBitsPerRow, tile.globalConfigBits)
        else:
            self.generateConfigMemInit(
                configMemCsv, tile.globalConfigBits)
            configMemList = parseConfigMem(
                configMemCsv, self.fabric.maxFramesPerCol, self.fabric.frameBitsPerRow, tile.globalConfigBits)

        # write entity
        entity = f"{tile.name}_ConfigMem"
        self.writer.addHeader(f"{tile.name}_ConfigMem")
        self.writer.addParameterStart(indentLevel=1)
        if self.fabric.maxFramesPerCol != 0:
            self.writer.addParameter("MaxFramesPerCol", "integer",
                                     self.fabric.maxFramesPerCol, indentLevel=2)
        if self.fabric.frameBitsPerRow != 0:
            self.writer.addParameter("FrameBitsPerRow", "integer",
                                     self.fabric.frameBitsPerRow, indentLevel=2)
        self.writer.addParameter("NoConfigBits", "integer",
                                 tile.globalConfigBits, end=True, indentLevel=2)
        self.writer.addParameterEnd(indentLevel=1)
        self.writer.addPortStart(indentLevel=1)
        # the port definitions are generic
        self.writer.addPortVector(
            "FrameData", IO.INPUT, "FrameBitsPerRow - 1", indentLevel=2)
        self.writer.addPortVector("FrameStrobe", IO.INPUT,
                                  "MaxFramesPerCol - 1", indentLevel=2)
        self.writer.addPortVector("ConfigBits", IO.OUTPUT,
                                  "NoConfigBits - 1", indentLevel=2)
        self.writer.addPortVector("ConfigBits_N", IO.OUTPUT,
                                  "NoConfigBits - 1", end=True, indentLevel=2)
        self.writer.addPortEnd(indentLevel=1)
        self.writer.addHeaderEnd(f"{tile.name}_ConfigMem")
        self.writer.addNewLine()
        # declare architecture
        self.writer.addDesignDescriptionStart(f"{tile.name}_ConfigMem")

        self.writer.addLogicStart()
        # instantiate latches for only the used frame bits
        for i in configMemList:
            if i.usedBitMask.count("1") > 0:
                self.writer.addConnectionVector(
                    i.frameName, f"{i.bitsUsedInFrame}-1")

        self.writer.addNewLine()
        self.writer.addNewLine()
        self.writer.addComment("instantiate frame latches", end="")
        for i in configMemList:
            counter = 0
            for k in range(self.fabric.frameBitsPerRow):
                if i.usedBitMask[k] == "1":
                    self.writer.addInstantiation(compName="LHQD1",
                                                 compInsName=f"Inst_{i.frameName}_bit{self.fabric.frameBitsPerRow-1-k}",
                                                 compPort=[
                                                     "D", "E", "Q", "QN"],
                                                 signal=[f"FrameData[{self.fabric.frameBitsPerRow-1-k}]",
                                                         f"FrameStrobe[{i.frameIndex}]",
                                                         f"ConfigBits[{i.configBitRanges[counter]}]",
                                                         f"ConfigBits[{i.configBitRanges[counter]}]"])
                    counter += 1

        self.writer.addLogicEnd()
        self.writer.addDesignDescriptionEnd()
        self.writer.writeToFile()

    def genTileSwitchMatrix(self, tile: Tile) -> None:
        # convert the matrix to a dictionary map and performs entry check
        connections: Dict[str, List[str]] = {}
        if tile.matrixDir.endswith(".csv"):
            connections = parseMatrix(tile.matrixDir, tile.name)
        elif tile.matrixDir.endswith(".list"):
            logger.info(f"{tile.name} matrix is a list file")
            logger.info(
                f"bootstrapping {tile.name} to matrix form and adding the list file to the matrix")
            matrixDir = tile.matrixDir.replace(".list", ".csv")
            self.bootstrapSwitchMatrix(tile, matrixDir)
            self.list2CSV(tile.matrixDir, matrixDir)
            logger.info(
                f"Update matrix directory to {matrixDir} for Fabric Tile Dictionary")
            tile.matrixDir = matrixDir
            connections = parseMatrix(tile.matrixDir, tile.name)
        elif tile.matrixDir.endswith(".v") or tile.matrixDir.endswith(".vhdl"):
            logger.info(
                f"A switch matrix file is provided in {tile.name}, will skip the matrix generation process")
            return

        noConfigBits = 0
        for i in connections:
            noConfigBits += len(connections[i]).bit_length()-1

        # we pass the NumberOfConfigBits as a comment in the beginning of the file.
        # This simplifies it to generate the configuration port only if needed later when building the fabric where we are only working with the VHDL files

        # VHDL header
        self.writer.addComment(
            f"NumberOfConfigBits: {noConfigBits}")
        self.writer.addHeader(f"{tile.name}_switch_matrix")
        self.writer.addParameterStart(indentLevel=1)
        self.writer.addParameter("NoConfigBits", "integer",
                                 noConfigBits, end=True, indentLevel=2)
        self.writer.addParameterEnd(indentLevel=1)
        self.writer.addPortStart(indentLevel=1)
        sourceName, destName = [], []
        # normal wire
        for i in tile.portsInfo:
            if i.wireDirection != Direction.JUMP:
                input, output = i.expandPortInfo("AutoSwitchMatrix")
                destName += input
                sourceName += output
        # bel wire
        for b in tile.bels:
            for p in list(dict.fromkeys(b.inputs)):
                destName.append(f"{p}")
            for p in list(dict.fromkeys(b.outputs)):
                sourceName.append(f"{p}")

        # jump wire
        for i in tile.portsInfo:
            if i.wireDirection == Direction.JUMP:
                input, output = i.expandPortInfo("AutoSwitchMatrix")
                destName += input
                sourceName += output
        sourceName = list(dict.fromkeys(sourceName))
        destName = list(dict.fromkeys(destName))

        for port in sourceName:
            if "GND" in port or "VCC" in port:
                continue
            self.writer.addPortScalar(port, IO.INPUT, indentLevel=2)
        for port in destName:
            if "GND" in port or "VCC" in port:
                continue
            self.writer.addPortScalar(port, IO.OUTPUT, indentLevel=2)
        self.writer.addComment("global", onNewLine=True)
        if noConfigBits > 0:
            if self.fabric.configBitMode == ConfigBitMode.FLIPFLOP_CHAIN:
                self.writer.addPortScalar("MODE", IO.INPUT, indentLevel=2)
                self.writer.addComment(
                    "global signal 1: configuration, 0: operation")
                self.writer.addPortScalar("CONFin", IO.INPUT, indentLevel=2)
                self.writer.addPortScalar("CONFout", IO.OUTPUT, indentLevel=2)
                self.writer.addPortScalar("CLK", IO.INPUT, indentLevel=2)
            if self.fabric.configBitMode == ConfigBitMode.FRAME_BASED:
                self.writer.addPortVector("ConfigBits", IO.INPUT,
                                          "NoConfigBits-1", indentLevel=2)
                self.writer.addPortVector("ConfigBits_N", IO.INPUT,
                                          "NoConfigBits-1", end=True, indentLevel=2)
        self.writer.addPortEnd()
        self.writer.addHeaderEnd(f"{tile.name}_switch_matrix")
        self.writer.addDesignDescriptionStart(f"{tile.name}_switch_matrix")

        # constant declaration
        # we may use the following in the switch matrix for providing '0' and '1' to a mux input:
        self.writer.addConstant("GND0", 0)
        self.writer.addConstant("GND", 0)
        self.writer.addConstant("VCC0", 1)
        self.writer.addConstant("VCC", 1)
        self.writer.addConstant("VDD0", 1)
        self.writer.addConstant("VDD", 1)
        self.writer.addNewLine()

        # signal declaration
        for portName in connections:
            self.writer.addConnectionVector(
                f"{portName}_input", f"{len(connections[portName])}-1")

        ### SwitchMatrixDebugSignals ### SwitchMatrixDebugSignals ###
        ### SwitchMatrixDebugSignals ### SwitchMatrixDebugSignals ###
        if SWITCH_MATRIX_DEBUG_SIGNAL:
            self.writer.addNewLine()
            for portName in connections:
                muxSize = len(connections[portName])
                if muxSize >= 2:
                    self.writer.addConnectionVector(
                        f"DEBUG_select_{portName}", f"{int(math.ceil(math.log2(muxSize)))}-1")
        ### SwitchMatrixDebugSignals ### SwitchMatrixDebugSignals ###
        ### SwitchMatrixDebugSignals ### SwitchMatrixDebugSignals ###
        self.writer.addComment(
            "The configuration bits (if any) are just a long shift register", onNewLine=True)
        self.writer.addComment(
            "This shift register is padded to an even number of flops/latches", onNewLine=True)

        # we are only generate configuration bits, if we really need configurations bits
        # for example in terminating switch matrices at the fabric borders, we may just change direction without any switching
        if noConfigBits > 0:
            if self.fabric.configBitMode == 'ff_chain':
                self.writer.addConnectionVector(
                    "ConfigBits", noConfigBits)
            if self.fabric.configBitMode == 'FlipFlopChain':
                # print('DEBUG DEBUG DEBUG DEBUG DEBUG DEBUG DEBUG DEBUG ConfigBitMode == FlipFlopChain')
                # we pad to an even number of bits: (int(math.ceil(ConfigBitCounter/2.0))*2)
                self.writer.addConnectionVector("ConfigBits", int(
                    math.ceil(noConfigBits/2.0))*2)
                self.writer.addConnectionVector("ConfigBitsInput", int(
                    math.ceil(noConfigBits/2.0))*2)

        # begin architecture
        self.writer.addLogicStart()

        # the configuration bits shift register
        # again, we add this only if needed
        # TODO Should ff_chain be the same as FlipFlopChain?
        if noConfigBits > 0:
            if self.fabric.configBitMode == 'ff_chain':
                self.writer.addShiftRegister(noConfigBits)
            elif self.fabric.configBitMode == ConfigBitMode.FLIPFLOP_CHAIN:
                self.writer.addFlipFlopChain(noConfigBits)
            elif self.fabric.configBitMode == ConfigBitMode.FRAME_BASED:
                pass
            else:
                raise ValueError(
                    f"{self.fabric.configBitMode} is not a valid ConfigBitMode")

        # the switch matrix implementation
        # we use the following variable to count the configuration bits of a long shift register which actually holds the switch matrix configuration
        configBitstreamPosition = 0
        for portName in connections:
            muxSize = len(connections[portName])
            self.writer.addComment(
                f"switch matrix multiplexer {portName} MUX-{muxSize}", onNewLine=True)
            if muxSize == 0:
                logger.warning(
                    f"Input port {portName} of switch matrix in Tile {tile.name} is not used")
                self.writer.addComment(
                    f"WARNING unused multiplexer MUX-{portName}", onNewLine=True)

            elif muxSize == 1:
                # just route through : can be used for auxiliary wires or diagonal routing (Manhattan, just go to a switch matrix when turning
                # can also be used to tap a wire. A double with a mid is nothing else as a single cascaded with another single where the second single has only one '1' to cascade from the first single
                if connections[portName][0] == '0':
                    self.writer.addAssignScalar(portName, 0)
                elif connections[portName][0] == '1':
                    self.writer.addAssignScalar(portName, 1)
                else:
                    self.writer.addAssignScalar(
                        portName, connections[portName][0])
                self.writer.addNewLine()
            elif muxSize >= 2:
                # this is the case for a configurable switch matrix multiplexer
                old_ConfigBitstreamPosition = configBitstreamPosition
                # math.ceil(math.log2(len(connections[k]))) tells us how many configuration bits a multiplexer takes
                configBitstreamPosition += len(
                    connections[portName]).bit_length()-1
                # the reversed() changes the direction that we iterate over the line list.
                # Changed it such that the left-most entry is located at the end of the concatenated vector for the multiplexing
                # This was done such that the index from left-to-right in the adjacency matrix corresponds with the multiplexer select input (index)
                numGnd = 0
                muxComponentName = ""
                if (self.fabric.multiplexerStyle == MultiplexerStyle.CUSTOM) and (muxSize == 2):
                    muxComponentName = 'my_mux2'
                elif (self.fabric.multiplexerStyle == MultiplexerStyle.CUSTOM) and (2 < muxSize <= 4):
                    muxComponentName = 'cus_mux41_buf'
                    numGnd = 4-muxSize
                elif (self.fabric.multiplexerStyle == MultiplexerStyle.CUSTOM) and (4 < muxSize <= 8):
                    muxComponentName = 'cus_mux81_buf'
                    numGnd = 8-muxSize
                elif (self.fabric.multiplexerStyle == MultiplexerStyle.CUSTOM) and (8 < muxSize <= 16):
                    muxComponentName = 'cus_mux161_buf'
                    numGnd = 16-muxSize

                portList, signalList = [], []
                start = 0
                for start in range(muxSize):
                    portList.append(f"A{start}")
                    signalList.append(f"{portName}_input[{start}]")

                for end in range(start, numGnd):
                    portList.append(f"A{end}")
                    signalList.append(f"GND0")

                if self.fabric.multiplexerStyle == MultiplexerStyle.CUSTOM:
                    if muxSize == 2:
                        portList.append(f"S")
                        signalList.append(
                            f"ConfigBits[{old_ConfigBitstreamPosition}+0]")
                    else:
                        for i in range(int(math.ceil(math.log2(muxSize)))):
                            portList.append(f"S{i}")
                            portList.append(f"S{i}N")
                            signalList.append(
                                f"ConfigBits[{old_ConfigBitstreamPosition}+{i}]")
                            signalList.append(
                                f"ConfigBits[{old_ConfigBitstreamPosition}+{i}]")

                portList.append("X")
                signalList.append(f"{portName}")

                if (self.fabric.multiplexerStyle == MultiplexerStyle.CUSTOM):
                    self.writer.addAssignScalar(
                        f"{portName}_input", connections[portName][::-1], int(self.fabric.generateDelayInSwitchMatrix))
                    self.writer.addInstantiation(compName=muxComponentName,
                                                 compInsName=f"inst_{muxComponentName}_{portName}",
                                                 compPort=portList,
                                                 signal=signalList)
                    if muxSize != 2 and muxSize != 4 and muxSize != 8 and muxSize != 16:
                        print(
                            f"HINT: creating a MUX-{str(muxSize)} for port {portName} using MUX-{muxSize} in switch matrix for tile {tile.name}")
                else:
                    # generic multiplexer
                    self.writer.addAssignScalar(
                        portName, f"{portName}_input[ConfigBits[{configBitstreamPosition-1}:{old_ConfigBitstreamPosition}]]")

        ### SwitchMatrixDebugSignals ### SwitchMatrixDebugSignals ###
        ### SwitchMatrixDebugSignals ### SwitchMatrixDebugSignals ###
        if SWITCH_MATRIX_DEBUG_SIGNAL:
            self.writer.addNewLine()
            configBitstreamPosition = 0
            for portName in connections:
                muxSize = len(connections[portName])
                if muxSize >= 2:
                    old_ConfigBitstreamPosition = configBitstreamPosition
                    configBitstreamPosition += int(
                        math.ceil(math.log2(muxSize)))
                    self.writer.addAssignVector(
                        f"DEBUG_select_{portName:<15}", "ConfigBits", configBitstreamPosition-1, old_ConfigBitstreamPosition)

        ### SwitchMatrixDebugSignals ### SwitchMatrixDebugSignals ###
        ### SwitchMatrixDebugSignals ### SwitchMatrixDebugSignals ###

        # just the final end of architecture

        self.writer.addDesignDescriptionEnd()
        self.writer.writeToFile()

    def generateTile(self, tile: Tile):
        allJumpWireList = []
        numberOfSwitchMatricesWithConfigPort = 0

        # We first check if we need a configuration port
        # Currently we assume that each primitive needs a configuration port
        # However, a switch matrix can have no switch matrix multiplexers
        # (e.g., when only bouncing back in border termination tiles)
        # we can detect this as each switch matrix file contains a comment -- NumberOfConfigBits
        # NumberOfConfigBits:0 tells us that the switch matrix does not have a config port
        # TODO: we don't do this and always create a configuration port for each tile. This may dangle the CLK and MODE ports hanging in the air, which will throw a warning

        # GenerateVHDL_Header(file, entity, NoConfigBits=str(GlobalConfigBitsCounter))
        self.writer.addHeader(f"{tile.name}")
        self.writer.addParameterStart(indentLevel=1)
        self.writer.addParameter("MaxFramesPerCol", "integer",
                                 self.fabric.maxFramesPerCol, indentLevel=2)
        self.writer.addParameter("FrameBitsPerRow", "integer",
                                 self.fabric.frameBitsPerRow, indentLevel=2)
        self.writer.addParameter("NoConfigBits", "integer",
                                 tile.globalConfigBits, end=True, indentLevel=2)
        self.writer.addParameterEnd(indentLevel=1)
        self.writer.addPortStart(indentLevel=1)

        # holder for each direction of port string
        portList = [tile.getNorthSidePorts(), tile.getEastSidePorts(),
                    tile.getWestSidePorts(), tile.getSouthSidePorts()]
        for l in portList:
            if not l:
                continue
            self.writer.addComment(l[0].sideOfTile, onNewLine=True)
            # destination port are input to the tile
            # source port are output of the tile
            for p in l:
                if p.sourceName != "NULL":
                    wireSize = (abs(p.xOffset)+abs(p.yOffset)) * p.wireCount-1
                    self.writer.addPortVector(p.name, p.inOut,
                                              wireSize, indentLevel=2)
                    self.writer.addComment(
                        str(p), indentLevel=2, onNewLine=False)

        # now we have to scan all BELs if they use external pins, because they have to be exported to the tile entity
        externalPorts = []
        for i in tile.bels:
            for p in i.externalInput:
                self.writer.addPortScalar(i.name, IO.INPUT, indentLevel=2)
            for p in i.externalOutput:
                self.writer.addPortScalar(i.name, IO.OUTPUT, indentLevel=2)
            externalPorts += i.externalInput
            externalPorts += i.externalOutput

        # if we found BELs with top-level IO ports, we just pass them through
        sharedExternalPorts = set()
        for i in tile.bels:
            sharedExternalPorts.update(i.sharedPort)

        self.writer.addComment("Tile IO ports from BELs",
                               onNewLine=True, indentLevel=1)

        if not tile.withUserCLK:
            self.writer.addPortScalar("UserCLKo", IO.OUTPUT, indentLevel=2)
        else:
            self.writer.addPortScalar("UserCLK", IO.INPUT, indentLevel=2)
            self.writer.addPortScalar("UserCLKo", IO.OUTPUT, indentLevel=2)

        if self.fabric.configBitMode == ConfigBitMode.FRAME_BASED:
            if tile.globalConfigBits > 0:
                self.writer.addPortVector(
                    "FrameData", IO.INPUT, "FrameBitsPerRow -1", indentLevel=2)
                self.writer.addComment("CONFIG_PORT", onNewLine=False, end="")
                self.writer.addPortVector("FrameData_O", IO.OUTPUT,
                                          "FrameBitsPerRow -1", indentLevel=2)
                self.writer.addPortVector("FrameStrobe", IO.INPUT,
                                          "MaxFramesPerCol -1", indentLevel=2)
                self.writer.addComment("CONFIG_PORT", onNewLine=False, end="")
                self.writer.addPortVector("FrameStrobe_O", IO.OUTPUT,
                                          "MaxFramesPerCol -1", end=True, indentLevel=2)
            else:
                self.writer.addPortVector("FrameStrobe", IO.INPUT,
                                          "MaxFramesPerCol -1", indentLevel=2)
                self.writer.addPortVector("FrameStrobe_O", IO.OUTPUT,
                                          "MaxFramesPerCol -1", end=True, indentLevel=2)

        elif self.fabric.configBitMode == ConfigBitMode.FLIPFLOP_CHAIN:
            self.writer.addPortScalar("MODE", IO.INPUT, indentLevel=2)
            self.writer.addPortScalar("CONFin", IO.INPUT, indentLevel=2)
            self.writer.addPortScalar("CONFout", IO.OUTPUT, indentLevel=2)
            self.writer.addPortScalar("CLK", IO.INPUT, end=True, indentLevel=2)

        self.writer.addComment("global", onNewLine=True, indentLevel=1)

        self.writer.addPortEnd()
        self.writer.addHeaderEnd(f"{tile.name}")
        self.writer.addDesignDescriptionStart(f"{tile.name}")

        # insert CLB, I/O (or whatever BEL) component declaration
        # specified in the fabric csv file after the 'BEL' key word
        # we use this list to check if we have seen a BEL description before so we only insert one component declaration
        BEL_VHDL_riles_processed = []
        for i in tile.bels:
            if i.src not in BEL_VHDL_riles_processed:
                BEL_VHDL_riles_processed.append(i.src)
                self.writer.addComponentDeclarationForFile(i.src)

        # insert switch matrix component declaration
        # specified in the fabric csv file after the 'MATRIX' key word
        if os.path.exists(tile.matrixDir):
            numberOfSwitchMatricesWithConfigPort += self.writer.addComponentDeclarationForFile(
                tile.matrixDir)
        else:
            raise ValueError(
                f"Could not find switch matrix definition for tile type {tile.name} in function GenerateTileVHDL")

        if self.fabric.configBitMode == ConfigBitMode.FRAME_BASED and tile.globalConfigBits > 0:
            if os.path.exists(f"{tile.name}_ConfigMem.vhdl"):
                self.writer.addComponentDeclarationForFile(
                    f"{tile.name}_ConfigMem.vhdl")

        # VHDL signal declarations
        self.writer.addComment("signal declarations", onNewLine=True)
        # BEL port wires
        self.writer.addComment("BEL ports (e.g., slices)", onNewLine=True)
        repeatDeclaration = set()
        for bel in tile.bels:
            for i in bel.inputs + bel.outputs:
                if f"{i}" not in repeatDeclaration:
                    self.writer.addConnectionScalar(i)
                    repeatDeclaration.add(f"{bel.prefix}{i}")

        # Jump wires
        self.writer.addComment("Jump wires", onNewLine=True)
        for p in tile.portsInfo:
            if p.wireDirection == Direction.JUMP:
                if p.sourceName != "NULL" and p.destinationName != "NULL" and p.inOut == IO.OUTPUT:
                    self.writer.addConnectionVector(p.name, f"{p.wireCount}-1")

                for k in range(p.wireCount):
                    allJumpWireList.append(f"{p.name}( {k} )")

        # internal configuration data signal to daisy-chain all BELs (if any and in the order they are listed in the fabric.csv)
        self.writer.addComment(
            "internal configuration data signal to daisy-chain all BELs (if any and in the order they are listed in the fabric.csv)", onNewLine=True)

        # the signal has to be number of BELs+2 bits wide (Bel_counter+1 downto 0)
        # we chain switch matrices only to the configuration port, if they really contain configuration bits
        # i.e. switch matrices have a config port which is indicated by "NumberOfConfigBits:0 is false"

        # The following conditional as intended to only generate the config_data signal if really anything is actually configured
        # however, we leave it and just use this signal as conf_data(0 downto 0) for simply touting through CONFin to CONFout
        # maybe even useful if we want to add a buffer here

        if tile.globalConfigBits > 0:
            self.writer.addConnectionVector("ConfigBits", "NoConfigBits-1", 0)
            self.writer.addConnectionVector(
                "ConfigBits_N", "NoConfigBits-1", 0)

            self.writer.addNewLine()
            self.writer.addConnectionVector(
                "FrameData_i", "FrameBitsPerRow-1", 0)
            self.writer.addConnectionVector(
                "FrameData_O_i", "FrameBitsPerRow-1", 0)
            self.writer.addAssignScalar("FrameData_O_i", "FrameData_i")
            self.writer.addNewLine()
            for i in range(self.fabric.frameBitsPerRow):
                self.writer.addInstantiation("my_buf",
                                             f"data_inbuf_{i}",
                                             ["A", "X"],
                                             [f"FrameData[{i}]", f"FrameData_i[{i}]"])
            for i in range(self.fabric.frameBitsPerRow):
                self.writer.addInstantiation("my_buf",
                                             f"data_outbuf_{i}",
                                             ["A", "X"],
                                             [f"FrameData_O_i[{i}]", f"FrameData_O[{i}]"])

        # strobe is always added even when config bits are 0
        self.writer.addNewLine()
        self.writer.addConnectionVector(
            "FrameStrobe_i", "MaxFramesPerCol-1", 0)
        self.writer.addConnectionVector(
            "FrameStrobe_O_i", "MaxFramesPerCol-1", 0)
        self.writer.addAssignScalar("FrameStrobe_O_i", "FrameStrobe_i")
        self.writer.addNewLine()
        for i in range(self.fabric.maxFramesPerCol):
            self.writer.addInstantiation("my_buf",
                                         f"strobe_inbuf_{i}",
                                         ["A", "X"],
                                         [f"FrameStrobe[{i}]", f"FrameStrobe_i[{i}]"])
        for i in range(self.fabric.maxFramesPerCol):
            self.writer.addInstantiation("my_buf",
                                         f"strobe_outbuf_{i}",
                                         ["A", "X"],
                                         [f"FrameStrobe_O_i[{i}]", f"FrameStrobe_O[{i}]"])

        added = set()
        for port in tile.portsInfo:
            span = abs(port.xOffset) + abs(port.yOffset)
            if (port.sourceName, port.destinationName) in added:
                continue
            if span >= 2 and port.sourceName != "NULL" and port.destinationName != "NULL":
                highBoundIndex = span*port.wireCount - 1
                self.writer.addConnectionVector(
                    f"{port.destinationName}_i", highBoundIndex)
                self.writer.addConnectionVector(
                    f"{port.sourceName}_i", highBoundIndex - port.wireCount)
                # using scalar assignment to connect the two vectors
                self.writer.addAssignScalar(
                    f"{port.destinationName}_i[{highBoundIndex}-{port.wireCount}:0]", f"{port.destinationName}_i[{highBoundIndex}:{port.wireCount}]")
                self.writer.addNewLine()
                for i in range(highBoundIndex - port.wireCount + 1):
                    self.writer.addInstantiation("my_buf",
                                                 f"{port.destinationName}_inbuf_{i}",
                                                 ["A", "X"],
                                                 [f"{port.destinationName}[{i+port.wireCount}]", f"{port.destinationName}_i[{i+port.wireCount}]"])
                for i in range(highBoundIndex - port.wireCount + 1):
                    self.writer.addInstantiation("my_buf",
                                                 f"{port.sourceName}_outbuf_{i}",
                                                 ["A", "X"],
                                                 [f"{port.sourceName}_i[{i}]",
                                                  f"{port.sourceName}[{i}]"])
                added.add((port.sourceName, port.destinationName))

        if tile.withUserCLK:
            self.writer.addInstantiation("clk_buf",
                                         f"inst_clk_buf",
                                         ["A", "X"],
                                         [f"UserCLK", f"UserCLKo"])

        self.writer.addNewLine()
        # top configuration data daisy chaining
        if self.fabric.configBitMode == ConfigBitMode.FLIPFLOP_CHAIN:
            self.writer.addComment(
                "top configuration data daisy chaining", onNewLine=True)
            self.writer.addAssignScalar("conf_data(conf_data'low)", "CONFin")
            self.writer.addComment(
                "conf_data'low=0 and CONFin is from tile entity")
            self.writer.addAssignScalar("conf_data(conf_data'high)", "CONFout")
            self.writer.addComment("CONFout is from tile entity")

        # the <entity>_ConfigMem module is only parametrized through generics, so we hard code its instantiation here
        if self.fabric.configBitMode == ConfigBitMode.FRAME_BASED and tile.globalConfigBits > 0:
            self.writer.addComment(
                "configuration storage latches", onNewLine=True)
            self.writer.addInstantiation(compName=f"{tile.name}_ConfigMem",
                                         compInsName=f"Inst_{tile.name}_ConfigMem",
                                         compPort=["FrameData",
                                                   "FrameStrobe", "ConfigBits", "ConfigBits_N"],
                                         signal=["FrameData", "FrameStrobe", "ConfigBits", "ConfigBits_N"])

        # BEL component instantiations
        self.writer.addComment("BEL component instantiations", onNewLine=True)
        belCounter = 0
        belConfigBitsCounter = 0
        for b in tile.bels:
            self.writer.addBELInstantiations(
                b, belConfigBitsCounter, self.fabric.configBitMode, belCounter)
            belConfigBitsCounter += b.configBit
            # for the next BEL (if any) for cascading configuration chain (this information is also needed for chaining the switch matrix)
            belCounter += 1

        # switch matrix component instantiation
        # important to know:
        # Each switch matrix entity is build up is a specific order:
        # 1.a) interconnect wire INPUTS (in the order defined by the fabric file,)
        # 2.a) BEL primitive INPUTS (in the order the BEL-VHDLs are listed in the fabric CSV)
        #      within each BEL, the order from the entity is maintained
        #      Note that INPUTS refers to the view of the switch matrix! Which corresponds to BEL outputs at the actual BEL
        # 3.a) JUMP wire INPUTS (in the order defined by the fabric file)
        # 1.b) interconnect wire OUTPUTS
        # 2.b) BEL primitive OUTPUTS
        #      Again: OUTPUTS refers to the view of the switch matrix which corresponds to BEL inputs at the actual BEL
        # 3.b) JUMP wire OUTPUTS
        # The switch matrix uses single bit ports (std_logic and not std_logic_vector)!!!

        switchMatrixConfigPort = 5
        portInputIndexed = []
        portOutputIndexed = []
        portTopInput = []
        portTopOutputs = []
        jumpWire = []
        sourceName, destName = [], []
        # normal wire
        for i in tile.portsInfo:
            if i.wireDirection != Direction.JUMP:
                input, output = i.expandPortInfo("AutoSwitchMatrix")
                sourceName += input
                destName += output
        # bel wire
        for b in tile.bels:
            for p in b.inputs:
                sourceName.append(f"{p}")
            for p in b.outputs:
                destName.append(f"{p}")

        # jump wire
        for i in tile.portsInfo:
            if i.wireDirection == Direction.JUMP:
                input, output = i.expandPortInfo("AutoSwitchMatrix")
                sourceName += input
                destName += output
        sourceName = list(dict.fromkeys(sourceName))
        destName = list(dict.fromkeys(destName))
        # get indexed version of the port of the tile
        for p in tile.portsInfo:
            if p.wireDirection != Direction.JUMP:
                input, output = p.expandPortInfo(
                    mode="AutoSwitchMatrixIndexed")
                portInputIndexed += input
                portOutputIndexed += output
                input, output = p.expandPortInfo(mode="AutoTopIndexed")
                portTopInput += input
                portTopOutputs += output
            else:
                input, output = p.expandPortInfo(
                    mode="AutoSwitchMatrixIndexed")
                input = [i for i in input if "GND" not in i and "VCC" not in i]
                jumpWire += input

        belOutputs = []
        belInputs = []
        # removing duplicates
        portOutputIndexed = list(dict.fromkeys(portOutputIndexed))
        portTopInput = list(dict.fromkeys(portTopInput))
        jumpWire = list(dict.fromkeys(jumpWire))
        for b in tile.bels:
            belOutputs += b.outputs
            belInputs += b.inputs

        portList = [i for i in (destName + sourceName)
                    if "GND" not in i and "VCC" not in i]
        signal = portOutputIndexed + belOutputs + \
            jumpWire + portTopInput + belInputs + jumpWire

        configBit = ""
        if self.fabric.configBitMode == ConfigBitMode.FLIPFLOP_CHAIN:
            portList.append("MODE")
            portList.append("CONFin")
            portList.append("CONFout")
            portList.append("CLK")
            signal.append("Mode")
            signal.append(f"conf_data[{belCounter}]")
            signal.append(f"conf_data[{belCounter+1}]")
            signal.append("CLK")
        if self.fabric.configBitMode == ConfigBitMode.FRAME_BASED:
            if tile.globalConfigBits > 0:
                portList.append("ConfigBits")
                portList.append("ConfigBits_N")
                signal.append(
                    f"ConfigBits[{tile.globalConfigBits}-1:{belConfigBitsCounter}]")
                signal.append(
                    f"ConfigBits_N[{tile.globalConfigBits}-1:{belConfigBitsCounter}]")

        self.writer.addInstantiation(compName=f"{tile.name}_switch_matrix",
                                     compInsName=f"Inst_{tile.name}_switch_matrix",
                                     compPort=portList,
                                     signal=signal)
        self.writer.addDesignDescriptionEnd()
        self.writer.writeToFile()

    def generateSuperTile(self, superTile: SuperTile):
        # GenerateVHDL_Header(file, entity, NoConfigBits=str(GlobalConfigBitsCounter))
        self.writer.addHeader(f"{superTile.name}")
        self.writer.addParameterStart(indentLevel=1)
        self.writer.addParameter("MaxFramesPerCol", "integer",
                                 self.fabric.maxFramesPerCol, indentLevel=2)
        self.writer.addParameter("FrameBitsPerRow", "integer",
                                 self.fabric.frameBitsPerRow, indentLevel=2)
        self.writer.addParameter("NoConfigBits", "integer", 0, indentLevel=2)
        self.writer.addParameterEnd(indentLevel=1)
        self.writer.addPortStart(indentLevel=1)

        portsAround = superTile.getPortsAroundTile()

        for k, v in portsAround.items():
            if not v:
                continue
            x, y = k
            for pList in v:
                self.writer.addComment(
                    f"Tile_X{x}Y{y}_{pList[0].wireDirection}", onNewLine=True, indentLevel=1)
                for p in pList:
                    if p.sourceName == "NULL" or p.destinationName == "NULL":
                        continue
                    wire = (abs(p.xOffset) + abs(p.yOffset)) * p.wireCount - 1
                    self.writer.addPortVector(
                        f"Tile_X{x}Y{y}_{p.name}", p.inOut, wire, indentLevel=2)
                    self.writer.addComment(str(p), onNewLine=False)

        # add tile external bel port
        self.writer.addComment("Tile IO ports from BELs",
                               onNewLine=True, indentLevel=1)
        for i in superTile.tiles:
            for b in i.bels:
                for p in b.externalInput:
                    self.writer.addPortScalar(p, IO.OUTPUT, indentLevel=2)
                for p in b.externalOutput:
                    self.writer.addPortScalar(p, IO.INPUT, indentLevel=2)
                for p in b.sharedPort:
                    self.writer.addPortScalar(p[0], p[1], indentLevel=2)

        # add config port
        if self.fabric.configBitMode == ConfigBitMode.FRAME_BASED:
            for y, row in enumerate(superTile.tileMap):
                for x, tile in enumerate(row):
                    if y - 1 < 0 or superTile.tileMap[y-1][x] == None:
                        self.writer.addPortVector(
                            f"Tile_X{x}Y{y}_FrameStrobe_O", IO.OUTPUT, "MaxFramePerCol-1", indentLevel=2)
                        self.writer.addComment("CONFIG_PORT", onNewLine=False)
                    if x + 1 >= len(superTile.tileMap[y]) or superTile.tileMap[y][x+1] == None:
                        self.writer.addPortVector(
                            f"Tile_X{x}Y{y}_FrameData_O", IO.OUTPUT, "FrameBitsPerRow-1", indentLevel=2)
                        self.writer.addComment("CONFIG_PORT", onNewLine=False)
                    if y + 1 >= len(superTile.tileMap) or superTile.tileMap[y+1][x] == None:
                        self.writer.addPortVector(
                            f"Tile_X{x}Y{y}_FrameStrobe", IO.INPUT, "MaxFramePerCol-1", indentLevel=2)
                        self.writer.addComment("CONFIG_PORT", onNewLine=False)
                    if x - 1 < 0 or superTile.tileMap[y][x-1] == None:
                        self.writer.addPortVector(
                            f"Tile_X{x}Y{y}_FrameData", IO.INPUT, "FrameBitsPerRow-1", indentLevel=2)
                        self.writer.addComment("CONFIG_PORT", onNewLine=False)

        self.writer.addPortEnd()
        self.writer.addHeaderEnd(f"{superTile.name}")
        self.writer.addDesignDescriptionStart(f"{superTile.name}")
        self.writer.addNewLine()

        BEL_VHDL_riles_processed = []
        if isinstance(self.writer, VHDLWriter):
            for t in superTile.tiles:
                # This is only relevant to VHDL code generation, will not affect Verilog code genration
                self.writer.addComponentDeclarationForFile(
                    f"{t.name}_tile.vhdl")

        # find all internal connections
        internalConnections = superTile.getInternalConnections()

        # declare internal connections
        self.writer.addComment("signal declarations", onNewLine=True)
        for i, x, y in internalConnections:
            if i:
                self.writer.addComment(
                    f"Tile_X{x}Y{y}_{i[0].wireDirection}", onNewLine=True)
                for p in i:
                    if p.inOut == IO.OUTPUT:
                        wire = (abs(p.xOffset) + abs(p.yOffset)) * \
                            p.wireCount - 1
                        self.writer.addConnectionVector(
                            f"Tile_X{x}Y{y}_{p.name}", wire, indentLevel=1)
                        self.writer.addComment(str(p), onNewLine=False)

        # declare internal connections for frameData and frameStrobe
        for y, row in enumerate(superTile.tileMap):
            for x, tile in enumerate(row):
                if 0 <= y - 1 < len(superTile.tileMap) and superTile.tileMap[y-1][x] != None:
                    self.writer.addConnectionVector(
                        f"Tile_X{x}Y{y}_FrameStrobe_O", "MaxFramePerCol-1", indentLevel=1)
                if 0 <= x - 1 < len(superTile.tileMap) and superTile.tileMap[y][x-1] != None:
                    self.writer.addConnectionVector(
                        f"Tile_X{x}Y{y}_FrameData_O", "FrameBitsPerRow-1", indentLevel=1)

        self.writer.addNewLine()
        # pair up the connection for tile instantiation

        for y, row in enumerate(superTile.tileMap):
            for x, tile in enumerate(row):
                northInput, southInput, eastInput, westInput = [], [], [], []
                outputSignalList = []
                if tile != None:
                    # north input connection
                    if 0 <= y + 1 < len(superTile.tileMap) and superTile.tileMap[y+1][x] != None:
                        for p in superTile.tileMap[y+1][x].getNorthPorts():
                            if p.sourceName != "NULL":
                                northInput.append(
                                    f"Tile_X{x}Y{y+1}_{p.sourceName}")
                    else:
                        for p in tile.getNorthPorts():
                            if p.destinationName != "NULL":
                                northInput.append(
                                    f"Tile_X{x}Y{y}_{p.destinationName}")

                    # east input connection
                    if 0 <= x - 1 < len(superTile.tileMap[0]) and superTile.tileMap[y][x-1] != None:
                        for p in superTile.tileMap[y][x-1].getEastPorts():
                            if p.sourceName != "NULL":
                                eastInput.append(
                                    f"Tile_X{x-1}Y{y}_{p.sourceName}")
                    else:
                        for p in tile.getEastPorts():
                            if p.destinationName != "NULL":
                                eastInput.append(
                                    f"Tile_X{x}Y{y}_{p.destinationName}")

                    # south input connection
                    if 0 <= y - 1 < len(superTile.tileMap) and superTile.tileMap[y-1][x] != None:
                        for p in superTile.tileMap[y-1][x].getSouthPorts():
                            if p.sourceName != "NULL":
                                southInput.append(
                                    f"Tile_X{x}Y{y-1}_{p.sourceName}")
                    else:
                        for p in tile.getSouthPorts():
                            if p.destinationName != "NULL":
                                southInput.append(
                                    f"Tile_X{x}Y{y}_{p.destinationName}")

                    # west input connection
                    if 0 <= x + 1 < len(superTile.tileMap[0]) and superTile.tileMap[y][x+1] != None:
                        for p in superTile.tileMap[y][x+1].getWestPorts():
                            if p.sourceName != "NULL":
                                westInput.append(
                                    f"Tile_X{x+1}Y{y}_{p.sourceName}")
                    else:
                        for p in tile.getWestPorts():
                            if p.destinationName != "NULL":
                                westInput.append(
                                    f"Tile_X{x}Y{y}_{p.destinationName}")

                    for p in tile.getNorthPorts() + tile.getEastPorts() + tile.getSouthPorts() + tile.getWestPorts():
                        if p.sourceName != "NULL":
                            outputSignalList.append(
                                f"Tile_X{x}Y{y}_{p.sourceName}")

                    combine = northInput + eastInput + southInput + westInput + outputSignalList

                    self.writer.addComment(
                        "tile IO port will get directly connected to top-level tile module", onNewLine=True, indentLevel=1)
                    for i in superTile.tiles:
                        for b in i.bels:
                            for p in b.externalInput:
                                combine.append(p)
                            for p in b.externalOutput:
                                combine.append(p)
                            for p in b.sharedPort:
                                combine.append(p[0])

                    if self.fabric.configBitMode == ConfigBitMode.FRAME_BASED:
                        # add connection for frameData and frameStrobe
                        if 0 <= x - 1 < len(superTile.tileMap[0]) - 1 and superTile.tileMap[y][x-1] != None:
                            combine.append(f"Tile_X{x-1}Y{y}_FrameData_O")
                        else:
                            combine.append(f"Tile_X{x}Y{y}_FrameData")

                        combine.append(f"Tile_X{x}Y{y}_FrameData_O")

                        if 0 <= y + 1 < len(superTile.tileMap) - 1 and superTile.tileMap[y+1][x] != None:
                            combine.append(
                                f"Tile_X{x}Y{y-1}_FrameStrobe_O")
                        else:
                            combine.append(f"Tile_X{x}Y{y}_FrameStrobe")

                        combine.append(f"Tile_X{x}Y{y}_FrameStrobe_O")

                    ports = []
                    # all the input port
                    ports += [i.destinationName for i in tile.getNorthPorts()
                              if i.destinationName != "NULL"]
                    ports += [i.destinationName for i in tile.getEastPorts()
                              if i.destinationName != "NULL"]
                    ports += [i.destinationName for i in tile.getSouthPorts()
                              if i.destinationName != "NULL"]
                    ports += [i.destinationName for i in tile.getWestPorts()
                              if i.destinationName != "NULL"]
                    # all the output port
                    ports += [i.sourceName for i in tile.getNorthPorts()
                              if i.sourceName != "NULL"]
                    ports += [i.sourceName for i in tile.getEastPorts()
                              if i.sourceName != "NULL"]
                    ports += [i.sourceName for i in tile.getSouthPorts()
                              if i.sourceName != "NULL"]
                    ports += [i.sourceName for i in tile.getWestPorts()
                              if i.sourceName != "NULL"]

                    for i in superTile.tiles:
                        for b in i.bels:
                            for p in b.externalInput:
                                ports.append(p)
                            for p in b.externalOutput:
                                ports.append(p)
                            for p in b.sharedPort:
                                ports.append(p[0])

                    if self.fabric.configBitMode == ConfigBitMode.FRAME_BASED:
                        ports += ["FrameData", "FrameData_O",
                                  "FrameStrobe", "FrameStrobe_O"]

                    ports = list(dict.fromkeys(ports))
                    combine = list(dict.fromkeys(combine))

                    self.writer.addInstantiation(compName=tile.name,
                                                 compInsName=f"Tile_X{x}Y{y}_{tile.name}",
                                                 compPort=ports,
                                                 signal=combine)
        self.writer.addDesignDescriptionEnd()
        self.writer.writeToFile()

    def generateFabric(self):
        # There are of course many possibilities for generating the fabric.
        # I decided to generate a flat description as it may allow for a little easier debugging.
        # For larger fabrics, this may be an issue, but not for now.
        # We only have wires between two adjacent tiles in North, East, South, West direction.
        # So we use the output ports to generate wires.

        # we first scan all tiles if those have IOs that have to go to top
        # the order of this scan is later maintained when instantiating the actual tiles
        # header
        fabricName = "eFPGA"
        self.writer.addHeader(fabricName)
        self.writer.addParameterStart(indentLevel=1)
        self.writer.addParameter("MaxFramesPerCol", "integer",
                                 self.fabric.maxFramesPerCol, indentLevel=2)
        self.writer.addParameter("FrameBitsPerRow", "integer",
                                 self.fabric.frameBitsPerRow, indentLevel=2)
        self.writer.addParameter("NoConfigBits", "integer",
                                 0, end=True, indentLevel=2)
        self.writer.addParameterEnd(indentLevel=1)
        self.writer.addPortStart(indentLevel=1)
        for y, row in enumerate(self.fabric.tile):
            for x, tile in enumerate(row):
                if tile != None:
                    for bel in tile.bels:
                        for i in bel.externalInput:
                            self.writer.addPortScalar(
                                f"Tile_X{x}Y{y}_{i}", IO.INPUT, indentLevel=2)
                            self.writer.addComment("EXTERNAL", onNewLine=False)
                        for i in bel.externalOutput:
                            self.writer.addPortScalar(
                                f"Tile_X{x}Y{y}_{i}", IO.OUTPUT, indentLevel=2)
                            self.writer.addComment("EXTERNAL", onNewLine=False)

        if self.fabric.configBitMode == ConfigBitMode.FRAME_BASED:
            self.writer.addPortVector(
                "FrameData", IO.INPUT, f"(FrameBitsPerRow{len(self.fabric.tile[0])})-1", indentLevel=2)
            self.writer.addComment("CONFIG_PORT", onNewLine=False)
            self.writer.addPortVector(
                "FrameData_O", IO.OUTPUT, f"(FrameBitsPerRow{len(self.fabric.tile[0])})-1", indentLevel=2)
            self.writer.addPortVector(
                "FrameStrobe", IO.INPUT, f"(MaxFramesPerCol*{len(self.fabric.tile)})", indentLevel=2)
            self.writer.addComment("CONFIG_PORT", onNewLine=False)
            self.writer.addPortVector(
                "FrameStrobe_O", IO.OUTPUT, f"(MaxFramesPerCol*{len(self.fabric.tile)})", end=True, indentLevel=2)

        self.writer.addPortScalar("UserCLK", IO.INPUT, indentLevel=2)

        self.writer.addPortEnd()
        self.writer.addHeaderEnd(fabricName)
        self.writer.addDesignDescriptionStart(fabricName)
        self.writer.addNewLine()

        # TODO refactor
        for t in self.fabric.tileDic:
            if isinstance(self.writer, VHDLWriter):
                self.writer.addComponentDeclarationForFile(f"{t}_tile.vhdl")

        # VHDL signal declarations
        self.writer.addComment("signal declarations", onNewLine=True, end="\n")

        for y, row in enumerate(self.fabric.tile):
            for x, tile in enumerate(row):
                if tile != None:
                    self.writer.addConnectionScalar(f"Tile_X{x}Y{y}_UserCLKo")

        self.writer.addComment("configuration signal declarations",
                               onNewLine=True, end="\n")

        if self.fabric.configBitMode == 'FlipFlopChain':
            tileCounter = 0
            for row in self.fabric.tile:
                for t in row:
                    if t != None:
                        tileCounter += 1
            self.writer.addConnectionVector("conf_data", tileCounter)

        if self.fabric.configBitMode == ConfigBitMode.FRAME_BASED:
            # FrameData       =>     Tile_Y3_FrameData,
            # FrameStrobe      =>     Tile_X1_FrameStrobe
            # MaxFramesPerCol : integer := 20;
            # FrameBitsPerRow : integer := 32;
            for y in range(1, len(self.fabric.tile)-1):
                self.writer.addConnectionVector(
                    f"Tile_Y{y}_FrameData", "FrameBitsPerRow -1")

            for x in range(len(self.fabric.tile[0])):
                self.writer.addConnectionVector(
                    f"Tile_X{x}_FrameStrobe", "MaxFramesPerCol - 1")

            for y in range(1, len(self.fabric.tile)-1):
                for x in range(len(self.fabric.tile[0])):
                    self.writer.addConnectionVector(
                        f"Tile_X{x}Y{y}_FrameData_O", "FrameBitsPerRow - 1")

            for y in range(1, len(self.fabric.tile)):
                for x in range(len(self.fabric.tile[0])):
                    self.writer.addConnectionVector(
                        f"Tile_X{x}Y{y}_FrameStrobe_O", "MaxFramesPerCol - 1")

        self.writer.addComment(
            "tile-to-tile signal declarations", onNewLine=True)
        for y, row in enumerate(self.fabric.tile):
            for x, tile in enumerate(row):
                if tile != None:
                    for p in tile.portsInfo:
                        wireLength = (abs(p.xOffset)+abs(p.yOffset)
                                      ) * p.wireCount-1
                        if p.sourceName == "NULL" or p.wireDirection == Direction.JUMP:
                            continue
                        self.writer.addConnectionVector(
                            f"Tile_X{x}Y{y}_{p.sourceName}", wireLength)
        self.writer.addNewLine()
        # VHDL architecture body
        self.writer.addLogicStart()

        # top configuration data daisy chaining
        # this is copy and paste from tile code generation (so we can modify this here without side effects
        if self.fabric.configBitMode == 'FlipFlopChain':
            self.writer.addComment(
                "configuration data daisy chaining", onNewLine=True)
            self.writer.addAssignScalar("conf_dat'low", "CONFin")
            self.writer.addComment(
                "conf_data'low=0 and CONFin is from tile entity")
            self.writer.addAssignScalar("CONFout", "conf_data'high")
            self.writer.addComment("CONFout is from tile entity")

        if self.fabric.configBitMode == ConfigBitMode.FRAME_BASED:
            for y in range(1, len(self.fabric.tile)-1):
                self.writer.addAssignVector(
                    f"Tile_Y{y}_FrameData", "FrameData", f"FrameBitsPerRow*({y}+1)", f"FrameBitsPerRow*{y}")
            for x in range(len(self.fabric.tile[0])):
                self.writer.addAssignVector(
                    f"Tile_X{x}_FrameStrobe", "FrameStrobe", f"MaxFramesPerCol*({x}+1) -1", f"MaxFramesPerCol*{x}")

        instantiatedPosition = []
        # VHDL tile instantiations
        for y, row in enumerate(self.fabric.tile):
            for x, tile in enumerate(row):
                tilePortList: List[str] = []
                tilePortsInfo: List[Tuple[List[Port], int, int]] = []
                outputSignalList = []
                tileLocationOffset: List[Tuple[int, int]] = []
                superTileLoc = []
                superTile = None
                if tile == None:
                    continue

                if (x, y) in instantiatedPosition:
                    continue

                # instantiate super tile when encountered
                # get all the ports of the tile. If is a super tile, we loop over the
                # tile map and find all the offset of the subtile, and all their related
                # ports.
                for k, v in self.fabric.superTileDic.items():
                    if tile.name in [i.name for i in v.tiles]:
                        if tile.name == "":
                            continue
                        superTile = self.fabric.superTileDic[k]
                        break
                if self.fabric.superTileEnable and superTile:
                    portsAround = superTile.getPortsAroundTile()
                    for (i, j) in list(portsAround.keys()):
                        tileLocationOffset.append((int(i), int(j)))
                        instantiatedPosition.append((x+int(i), y+int(j)))
                        superTileLoc.append((x+int(i), y+int(j)))

                    # all input port of the tile
                    for (i, j), around in portsAround.items():
                        for ports in around:
                            for port in ports:
                                if port.inOut == IO.INPUT and port.sourceName != "NULL" and port.destinationName != "NULL":
                                    tilePortList.append(
                                        f"Tile_X{i}Y{j}_{port.name}")

                    # all output port of the tile
                    for (i, j), around in portsAround.items():
                        for ports in around:
                            for port in ports:
                                if port.inOut == IO.OUTPUT and port.sourceName != "NULL" and port.destinationName != "NULL":
                                    tilePortList.append(
                                        f"Tile_X{i}Y{j}_{port.name}")

                    for t in superTile.bels:
                        for b in t.bels:
                            for p in b.externalInput:
                                tilePortList.append(p)
                            for p in b.externalOutput:
                                tilePortList.append(p)
                            for p in b.sharedPort:
                                if "UserCLK" not in p:
                                    tilePortList.append(p[0])

                    withUserCLK = False
                    for t in superTile.tiles:
                        withUserCLK |= t.withUserCLK
                    if withUserCLK:
                        tilePortList.append("UserCLK")
                    else:
                        tilePortList.append("UserCLK")
                        tilePortList.append("UserCLKo")

                    if self.fabric.configBitMode == ConfigBitMode.FRAME_BASED:
                        for j, row in enumerate(superTile.tileMap):
                            for i, _ in enumerate(row):
                                if superTile.tileMap[j][i] == None:
                                    continue
                                if j - 1 < 0 or superTile.tileMap[j-1][i] == None:
                                    tilePortList.append(
                                        f"Tile_X{i}Y{j}_FrameStrobe_O")
                                if i + 1 >= len(superTile.tileMap[0]) or superTile.tileMap[y][i+1] == None:
                                    tilePortList.append(
                                        f"Tile_X{i}Y{j}_FrameData_O")
                                if j + 1 >= len(superTile.tileMap) or superTile.tileMap[j+1][i] == None:
                                    tilePortList.append(
                                        f"Tile_X{i}Y{j}_FrameStrobe")
                                if i - 1 < 0 or superTile.tileMap[j][i-1] == None:
                                    tilePortList.append(
                                        f"Tile_X{i}Y{j}_FrameData")
                else:
                    instantiatedPosition.append((x, y))
                    tileLocationOffset.append((0, 0))
                    tilePortsInfo.append((tile.getNorthSidePorts(), 0, 0))
                    tilePortsInfo.append((tile.getEastSidePorts(), 0, 0))
                    tilePortsInfo.append((tile.getSouthSidePorts(), 0, 0))
                    tilePortsInfo.append((tile.getWestSidePorts(), 0, 0))

                    # all the input port of a single normal tile
                    for port, i, j in tilePortsInfo:
                        for p in port:
                            if p.name != "NULL" and p.inOut == IO.INPUT:
                                tilePortList.append(f"{p.name}")

                    # all the output port of a single normal tile
                    for port, i, j in tilePortsInfo:
                        for p in port:
                            if p.name != "NULL" and p.inOut == IO.OUTPUT:
                                tilePortList.append(f"{p.name}")

                    for b in tile.bels:
                        for p in b.externalInput:
                            tilePortList.append(p)
                        for p in b.externalOutput:
                            tilePortList.append(p)
                        for p in b.sharedPort:
                            if "UserCLK" not in p[0]:
                                tilePortList.append(p[0])

                    if superTile:
                        tilePortList.append("UserCLK")
                    else:
                        tilePortList.append("UserCLK")
                        tilePortList.append("UserCLKo")

                    if self.fabric.configBitMode == ConfigBitMode.FRAME_BASED:
                        # only add frame data port when there are config bits in the tile
                        if tile.globalConfigBits > 0:
                            tilePortList.append("FrameData")
                            tilePortList.append("FrameData_O")
                        tilePortList.append("FrameStrobe")
                        tilePortList.append("FrameStrobe_O")

                # remove all the duplicated edge tile
                tileLocationOffset = list(dict.fromkeys(tileLocationOffset))
                signal = []
                # use the offset to find all the related tile input, output signal
                # if is a normal tile then the offset is (0, 0)
                for i, j in tileLocationOffset:
                    # input connection from south side of the north tile
                    if 0 <= y - 1 < len(self.fabric.tile) and self.fabric.tile[y+j-1][x+i] != None and (x+i, y+j-1) not in superTileLoc:
                        for p in self.fabric.tile[y+j-1][x+i].getSouthSidePorts():
                            if p.inOut == IO.OUTPUT:
                                signal.append(
                                    f"Tile_X{x+i}Y{y+j-1}_{p.name}")
                    # input connection from west side of the east tile
                    if 0 <= x + 1 < len(self.fabric.tile[0]) and self.fabric.tile[y+j][x+i+1] != None and (x+i+1, y+j) not in superTileLoc:
                        for p in self.fabric.tile[y+j][x+i+1].getWestSidePorts():
                            if p.inOut == IO.OUTPUT:
                                signal.append(
                                    f"Tile_X{x+i+1}Y{y+j}_{p.name}")
                    # input connection from north side of the south tile
                    if 0 <= y + 1 < len(self.fabric.tile) and self.fabric.tile[y+j+1][x+i] != None and (x+i, y+j+1) not in superTileLoc:
                        for p in self.fabric.tile[y+j+1][x+i].getNorthSidePorts():
                            if p.inOut == IO.OUTPUT:
                                signal.append(
                                    f"Tile_X{x+i}Y{y+j+1}_{p.name}")

                    # input connection from east side of the west tile
                    if 0 <= x - 1 < len(self.fabric.tile[0]) and self.fabric.tile[y+j][x+i-1] != None and (x+i-1, y+j) not in superTileLoc:
                        for p in self.fabric.tile[y+j][x+i-1].getEastSidePorts():
                            if p.inOut == IO.OUTPUT:
                                signal.append(
                                    f"Tile_X{x+i-1}Y{y+j}_{p.name}")

                # output signal name is same as the output port name
                if superTile:
                    portsAround = superTile.getPortsAroundTile()
                    for (i, j), around in portsAround.items():
                        for ports in around:
                            for port in ports:
                                if port.inOut == IO.OUTPUT and port.name != "NULL":
                                    outputSignalList.append(
                                        f"Tile_X{x+int(i)}Y{y+int(j)}_{port.name}")
                else:
                    for ports, i, j in tilePortsInfo:
                        for p in ports:
                            if p.inOut == IO.OUTPUT and p.name != "NULL":
                                outputSignalList.append(
                                    f"Tile_X{x+i}Y{y+j}_{p.name}")

                # combine = northInput + eastInput + southInput + westInput + outputSignalList
                signal += outputSignalList

                self.writer.addNewLine()
                self.writer.addComment(
                    "tile IO port will get directly connected to top-level tile module", onNewLine=True, indentLevel=0)
                for (i, j) in tileLocationOffset:
                    for b in self.fabric.tile[y+j][x+i].bels:
                        for p in b.externalInput:
                            signal.append(f"Tile_X{x}Y{y}_{p}")
                        for p in b.externalOutput:
                            signal.append(f"Tile_X{x}Y{y}_{p}")
                        for p in b.sharedPort:
                            if "UserCLK" not in p[0]:
                                signal.append(f"{p[0]}")

                if not superTile:
                    # for userCLK
                    if y + 1 < self.fabric.numberOfRows:
                        signal.append(f"Tile_X{x}Y{y+1}_UserCLKo")
                    else:
                        signal.append(f"UserCLK")
                    # for userCLKo
                    signal.append(f"Tile_X{x}Y{y}_UserCLKo")
                else:
                    if y + 1 < self.fabric.numberOfRows:
                        signal.append(f"Tile_X{x}Y{y+1}_UserCLKo")
                    else:
                        signal.append(f"UserCLK")

                if self.fabric.configBitMode == ConfigBitMode.FRAME_BASED:
                    for (i, j) in tileLocationOffset:
                        if tile.globalConfigBits > 0 or superTile:
                            # frameData signal
                            if x == 0:
                                signal.append(f"Tile_Y{y}_FrameData")
                            elif (x+i-1, y+j) not in superTileLoc:
                                signal.append(
                                    f"Tile_X{x+i-1}Y{y+j}_FrameData_O")
                            # frameData_O signal
                            if x == len(self.fabric.tile[0]) - 1:
                                signal.append(f"Tile_X{x}Y{y}_FrameData_O")
                            elif (x+i-1, y+j) not in superTileLoc:
                                signal.append(f"Tile_X{x+i}Y{y+j}_FrameData_O")

                        # frameStrobe signal
                        if y == 0:
                            signal.append(f"Tile_X{x}Y{y+1}_FrameStrobe_O")
                        elif (x+i, y+j+1) not in superTileLoc:
                            signal.append(f"Tile_X{x+i}Y{y+j+1}_FrameStrobe_O")

                        # frameStrobe_O signal
                        if y == len(self.fabric.tile) - 1:
                            signal.append(f"Tile_X{x}_FrameStrobe")
                        elif (x+i, y+j+1) not in superTileLoc:
                            signal.append(f"Tile_X{x+i}Y{y+j}_FrameStrobe_O")

                name = ""
                if superTile:
                    name = superTile.name
                else:
                    name = tile.name

                tilePortList = list(dict.fromkeys(tilePortList))
                signal = list(dict.fromkeys(signal))
                self.writer.addInstantiation(compName=name,
                                             compInsName=f"Tile_X{x}Y{y}_{name}",
                                             compPort=tilePortList,
                                             signal=signal)
        self.writer.addDesignDescriptionEnd()
        self.writer.writeToFile()

    def generateTopWrapper(self):
        numberOfRows = self.fabric.numberOfRows - 2
        numberOfColumns = self.fabric.numberOfColumns - 2
        self.writer.addHeader(f"{self.fabric.name}")
        self.writer.addParameterStart(indentLevel=1)
        self.writer.addParameter("include_eFPGA", "integer", 1, indentLevel=2)
        self.writer.addParameter("NumberOfRows", "integer",
                                 numberOfRows, indentLevel=2)
        self.writer.addParameter("NumberOfCols", "integer",
                                 numberOfColumns, indentLevel=2)
        self.writer.addParameter("FrameBitsPerRow", "integer",
                                 self.fabric.frameBitsPerRow, indentLevel=2)
        self.writer.addParameter("MaxFramesPerCol", "integer",
                                 self.fabric.maxFramesPerCol, indentLevel=2)
        self.writer.addParameter("desync_flag", "integer",
                                 self.fabric.desync_flag, indentLevel=2)
        self.writer.addParameter("FrameSelectWidth", "integer",
                                 self.fabric.frameSelectWidth, indentLevel=2)
        self.writer.addParameter("RowSelectWidth", "integer",
                                 self.fabric.rowSelectWidth, indentLevel=2)
        self.writer.addParameterEnd(indentLevel=1)
        self.writer.addPortStart(indentLevel=1)
        self.writer.addPortVector("I_top", IO.INOUT, f"{numberOfRows*2}-1")
        self.writer.addPortVector("T_top", IO.INOUT, f"{numberOfRows*2}-1")
        self.writer.addPortVector("O_top", IO.INOUT, f"{numberOfRows*2}-1")
        self.writer.addPortVector(
            "A_config_C", IO.INOUT, f"{numberOfRows*4}-1")
        self.writer.addPortVector(
            "B_config_C", IO.INOUT, f"{numberOfRows*4}-1")
        self.writer.addPortScalar("CLK", IO.INPUT)
        self.writer.addPortScalar("SelfWriteStrobe", IO.INOUT)
        self.writer.addPortScalar("SelfWriteData", IO.INOUT)
        self.writer.addPortScalar("RX", IO.INOUT)
        self.writer.addPortScalar("ComActive", IO.INOUT)
        self.writer.addPortScalar("ReceiveLED", IO.INOUT)
        self.writer.addPortScalar("s_clk", IO.INOUT)
        self.writer.addPortScalar("s_data", IO.INOUT)
        self.writer.addPortEnd()
        self.writer.addHeaderEnd(f"{self.fabric.name}")
        self.writer.addDesignDescriptionStart(f"{self.fabric.name}")

        self.writer.addComment("BlockRAM ports", onNewLine=True)
        self.writer.addNewLine()
        self.writer.addConnectionVector("RAM2FAB_D", f"{numberOfRows*4*4}-1")
        self.writer.addConnectionVector("FAB2RAM_D", f"{numberOfRows*4*4}-1")
        self.writer.addConnectionVector("FAB2RAM_A", f"{numberOfRows*4*2}-1")
        self.writer.addConnectionVector("FAB2RAM_C", f"{numberOfRows*4}-1")
        self.writer.addConnectionVector(
            "Config_accessC", f"{numberOfRows*4}-1")

        self.writer.addNewLine()
        self.writer.addComment("Signal declarations", onNewLine=True)
        self.writer.addConnectionVector(
            "FrameRegister", "(NumberOfRows*FrameBitsPerRow)-1")
        self.writer.addConnectionVector(
            "FrameSelect", "(MaxFramesPerCol*NumberOfCols)-1")
        self.writer.addConnectionVector(
            "FrameData", "(FrameBitsPerRow*(NumberOfRows+2))-1")
        self.writer.addConnectionVector(
            "FrameAddressRegister", "FrameBitsPerRow-1")
        self.writer.addConnectionScalar("LongFrameStrobe")
        self.writer.addConnectionVector("LocalWriteData", 31)
        self.writer.addConnectionScalar("LocalWriteStrobe")
        self.writer.addConnectionVector("RowSelect", "RowSelectWidth-1")

        self.writer.addNewLine()
        self.writer.addInstantiation(compName="Config",
                                     compInsName="Config_inst",
                                     compPort=["CLK", "RX", "ComActive", "ReceiveLED",
                                               "SelfWriteStrobe", "s_clk",   "s_data", "SelfWriteData",
                                               "SelfWriteStrobe", "ConfigWriteData", "ConfigWriteStrobe", "FrameAddressRegister", "LongFrameStrobe", "RowSelect"],
                                     signal=["CLK", "RX", "ComActive", "ReceiveLED",
                                             "SelfWriteStrobe", "s_clk",   "s_data", "SelfWriteData",
                                             "SelfWriteStrobe", "ConfigWriteData", "ConfigWriteStrobe", "FrameAddressRegister", "LongFrameStrobe", "RowSelect"],
                                     paramPort=["RowSelectWidth", "FrameBitsPerRow",
                                                "NumberOfRows", "desync_flag"],
                                     paramSignal=["RowSelectWidth", "FrameBitsPerRow",
                                                  "NumberOfRows", "desync_flag"])
        self.writer.addNewLine()
        for row in range(numberOfRows):
            self.writer.addInstantiation(compName=f"Frame_data_Reg_{row}",
                                         compInsName=f"Frame_data_Reg_{row}_inst",
                                         compPort=["FrameData_I",
                                                   "FrameData_O", "RowSelect", "CLK"],
                                         signal=["LocalWriteData",
                                                 f"FrameRegister[{row}*FrameBitsPerRow+:FrameBitsPerRow]", "RowSelect",
                                                 "CLK"],
                                         paramPort=["FrameBitsPerRow",
                                                    "RowSelectWidth"],
                                         paramSignal=["FrameBitsPerRow", "RowSelectWidth"])
        self.writer.addNewLine()
        for col in range(numberOfColumns):
            self.writer.addInstantiation(compName=f"Frame_select_{col}",
                                         compInsName=f"inst_Frame_select_{col}",
                                         compPort=["FrameStrobe_I",
                                                   "FrameStrobe_O", "FrameSelect", "FrameStrobe"],
                                         signal=["FrameAddressRegister[MaxFramesPerCol-1:0]",
                                                 f"FrameSelect[{col}*MaxFramesPerCol +: MaxFramesPerCol]",
                                                 "FrameAddressRegister[FrameBitsPerRow-1:FrameBitsPerRow-(FrameSelectWidth)]",
                                                 "LongFrameStrobe"],
                                         paramPort=["MaxFramesPerCol",
                                                    "FrameSelectWidth"],
                                         paramSignal=["MaxFramesPerCol", "FrameSelectWidth"])

        self.writer.addNewLine()
        portList = []
        for i in range(1, numberOfRows+1):
            portList.append(f"Tile_X0Y{i}_A_I_top")
            portList.append(f"Tile_X0Y{i}_B_I_top")

        for i in range(1, numberOfRows+1):
            portList.append(f"Tile_X0Y{i}_A_T_top")
            portList.append(f"Tile_X0Y{i}_B_T_top")

        for i in range(1, numberOfRows+1):
            portList.append(f"Tile_X0Y{i}_A_O_top")
            portList.append(f"Tile_X0Y{i}_B_O_top")

        for i in range(1, numberOfRows+1):
            portList.append(f"Tile_X0Y{i}_A_config_C_bit0")
            portList.append(f"Tile_X0Y{i}_A_config_C_bit1")
            portList.append(f"Tile_X0Y{i}_A_config_C_bit2")
            portList.append(f"Tile_X0Y{i}_A_config_C_bit3")

        for i in range(1, numberOfRows+1):
            portList.append(f"Tile_X0Y{i}_B_config_C_bit0")
            portList.append(f"Tile_X0Y{i}_B_config_C_bit1")
            portList.append(f"Tile_X0Y{i}_B_config_C_bit2")
            portList.append(f"Tile_X0Y{i}_B_config_C_bit3")

        for i in range(1, numberOfRows+1):
            for j in range(4):
                for k in range(4):
                    portList.append(
                        f"Tile_X{numberOfColumns-1}Y{i}_RAM2FAB_D{j}_I{k}")

        for i in range(1, numberOfRows + 1):
            for j in range(4):
                for k in range(4):
                    portList.append(
                        f"Tile_X{numberOfColumns-1}Y{i}_RAM2FAB_D{j}_O{k}")

        for i in range(1, numberOfRows + 1):
            for j in range(2):
                for k in range(4):
                    portList.append(
                        f"Tile_X{numberOfColumns-1}Y{i}_RAM2FAB_A{j}_O{k}")

        for i in range(1, numberOfRows + 1):
            for j in range(4):
                portList.append(
                    f"Tile_X{numberOfColumns-1}Y{i}_RAM2FAB_C_O{j}")

        for i in range(1, numberOfRows+1):
            portList.append(
                f"Tile_X{numberOfColumns-1}Y{i}_Config_accessC_bit0")
            portList.append(
                f"Tile_X{numberOfColumns-1}Y{i}_Config_accessC_bit1")
            portList.append(
                f"Tile_X{numberOfColumns-1}Y{i}_Config_accessC_bit2")
            portList.append(
                f"Tile_X{numberOfColumns-1}Y{i}_Config_accessC_bit3")

        portList.append("UserCLK")
        portList.append("FrameData")
        portList.append("FrameStrobe")

        signal = []
        signal += [f"I_top[{i}]" for i in range(numberOfRows*2-1, -1, -1)]
        signal += [f"T_top[{i}]" for i in range(numberOfRows*2-1, -1, -1)]
        signal += [f"O_top[{i}]" for i in range(numberOfRows*2-1, -1, -1)]
        signal += [f"A_config_C[{i}]" for i in range(numberOfRows*4-1, -1, -1)]
        signal += [f"B_config_C[{i}]" for i in range(numberOfRows*4-1, -1, -1)]
        signal += [f"RAM2FAB_D[{i}]" for i in range(
            numberOfRows*4*4-1, -1, -1)]
        signal += [f"FAB2RAM_D[{i}]" for i in range(
            numberOfRows*4*4-1, -1, -1)]
        signal += [f"FAB2RAM_A[{i}]" for i in range(
            numberOfRows*2*4-1, -1, -1)]
        signal += [f"FAB2RAM_C[{i}]" for i in range(numberOfRows*4-1, -1, -1)]
        signal += [f"Config_accessC[{i}]" for i in range(
            numberOfRows*4-1, -1, -1)]
        signal.append("CLK")
        signal.append("FrameData")
        signal.append("FrameSelect")
        self.writer.addInstantiation(compName=self.fabric.name,
                                     compInsName=f"{self.fabric.name}_inst",
                                     compPort=portList,
                                     signal=signal)

        self.writer.addNewLine()
        portList = ["CLK", "rd_addr", "rd_data", "wr_addr",
                    "wr_data", "C0", "C1", "C2", "C3", "C4", "C5"]
        data_cap = int((numberOfRows*4*4)/self.fabric.numberOfBRAMs)
        addr_cap = int((numberOfRows*4*2)/self.fabric.numberOfBRAMs)
        config_cap = int((numberOfRows*4)/self.fabric.numberOfBRAMs)
        for i in range(self.fabric.numberOfBRAMs-1):
            signal = ["CLK",
                      f"FAB2RAM_A[{addr_cap*i+8-1}:{addr_cap*i}]",
                      f"RAM2FAB_D[{addr_cap*i+32-1}:{addr_cap*i}]",
                      f"FAB2RAM_A[{addr_cap *i+16-1}:{addr_cap*i+8}]",
                      f"FAB2RAM_D[{data_cap*i+32-1}:{data_cap*i}]",
                      f"FAB2RAM_C[{config_cap*i}]",
                      f"FAB2RAM_C[{config_cap*i+1}]",
                      f"FAB2RAM_C[{config_cap*i+2}]",
                      f"FAB2RAM_C[{config_cap*i+3}]",
                      f"FAB2RAM_C[{config_cap*i+4}]",
                      f"FAB2RAM_C[{config_cap*i+5}]"
                      ]
            self.writer.addInstantiation(compName="BlockRAM_1KB",
                                         compInsName=f"Inst_BlockRAM_{i}",
                                         compPort=portList,
                                         signal=signal)
        self.writer.addAssignScalar(
            "FrameData", ["32'h12345678", "FrameRegister", "32'h12345678"])
        self.writer.addDesignDescriptionEnd()
        self.writer.writeToFile()

    def generateBitsStreamSpec(self):
        specData = {"TileMap": {},
                    "TileSpecs": {},
                    "TileSpecs_No_Mask": {},
                    "FrameMap": {},
                    "FrameMapEncode": {},
                    "ArchSpecs": {"MaxFramesPerCol": self.fabric.maxFramesPerCol,
                                  "FrameBitsPerRow": self.fabric.frameBitsPerRow}}

        tileMap = {}
        for y, row in enumerate(self.fabric.tile):
            for x, tile in enumerate(row):
                if tile is not None:
                    tileMap[f"X{x}Y{y}"] = tile.name
                else:
                    tileMap[f"X{x}Y{y}"] = "NULL"

        specData["TileMap"] = tileMap
        configMemList: List[ConfigMem] = []
        for y, row in enumerate(self.fabric.tile):
            for x, tile in enumerate(row):
                if tile == None:
                    continue
                if os.path.exists(f"{tile.filePath}/{tile.name}_ConfigMem.csv"):
                    configMemList = parseConfigMem(
                        f"{tile.filePath}/{tile.name}_ConfigMem.csv", self.fabric.maxFramesPerCol, self.fabric.frameBitsPerRow, tile.globalConfigBits)
                elif tile.globalConfigBits > 0:
                    logger.error(
                        f"No ConfigMem csv file found for {tile.name} which have config bits")
                    exit(-1)

                encodeDict = [-1] * (self.fabric.maxFramesPerCol *
                                     self.fabric.frameBitsPerRow)
                maskDic = {}
                for cfm in configMemList:
                    maskDic[cfm.frameIndex] = cfm.usedBitMask
                    # matching the value in the configBitRanges with the reversedBitMask
                    # bit 0 in bit mask is the first value in the configBitRanges
                    for i, char in enumerate(cfm.usedBitMask):
                        if char == "1":
                            encodeDict[cfm.configBitRanges.pop(0)] = (
                                self.fabric.frameBitsPerRow - 1 - i) + self.fabric.frameBitsPerRow * cfm.frameIndex

                # filling the maskDic with the unused frames
                for i in range(self.fabric.maxFramesPerCol-len(configMemList)):
                    maskDic[len(configMemList)+i] = '0' * \
                        self.fabric.frameBitsPerRow

                specData["FrameMap"][tile.name] = maskDic
                if tile.globalConfigBits == 0:
                    logger.info(f"No config memory for X{x}Y{y}_{tile.name}.")
                    specData["FrameMap"][tile.name] = {}
                    specData["FrameMapEncode"][tile.name] = {}

                curBitOffset = 0
                curTileMap = {}
                curTileMapNoMask = {}

                for i, bel in enumerate(tile.bels):
                    for featureKey, keyDict in bel.belFeatureMap.items():
                        for entry in keyDict:
                            if isinstance(entry, int):
                                for v in keyDict[entry]:
                                    curTileMap[f"{string.ascii_uppercase[i]}.{featureKey}"] = {
                                        encodeDict[curBitOffset+v]: keyDict[entry][v]}
                                    curTileMapNoMask[f"{string.ascii_uppercase[i]}.{featureKey}"] = {
                                        encodeDict[curBitOffset+v]: keyDict[entry][v]}
                                curBitOffset += len(keyDict[entry])

                # All the generation will be working on the tile level with the tileDic
                # This is added to propagate the updated switch matrix to each of the tile in the fabric
                if tile.matrixDir.endswith(".list"):
                    tile.matrixDir = tile.matrixDir.replace(".list", ".csv")

                result = parseMatrix(
                    tile.matrixDir, tile.name)
                for source, sinkList in result.items():
                    controlWidth = 0
                    for i, sink in enumerate(reversed(sinkList)):
                        controlWidth = len(sinkList).bit_length()-1
                        controlValue = f"{len(sinkList) - 1 - i:0{controlWidth}b}"
                        pip = f"{sink}.{source}"
                        if len(sinkList) < 2:
                            curTileMap[pip] = {}
                            curTileMapNoMask[pip] = {}
                            continue

                        for c, curChar in enumerate(controlValue[::-1]):
                            if pip not in curTileMap.keys():
                                curTileMap[pip] = {}
                                curTileMapNoMask[pip] = {}

                            curTileMap[pip][encodeDict[curBitOffset+c]] = curChar
                            curTileMapNoMask[pip][encodeDict[curBitOffset+c]] = curChar

                    curBitOffset += controlWidth

                # And now we add empty config bit mappings for immutable connections (i.e. wires), as nextpnr sees these the same as normal pips
                for wire in tile.wireList:
                    curTileMap[f"{wire.source}.{wire.destination}"] = {}
                    curTileMapNoMask[f"{wire.source}.{wire.destination}"] = {}

                specData["TileSpecs"][f"X{x}Y{y}"] = curTileMap
                specData["TileSpecs_No_Mask"][f"X{x}Y{y}"] = curTileMapNoMask

        return specData


#####################################################################################
# Main
#####################################################################################

if __name__ == "__main__":

    def CheckExt(choices):
        class Act(argparse.Action):
            def __call__(self, parser, namespace, fname, option_string=None):
                try:
                    ext = os.path.splitext(fname)[1][1:]
                    if ext not in choices:
                        option_string = '({})'.format(
                            option_string) if option_string else ''
                        parser.error("file doesn't end with one of {}{}".format(
                            choices, option_string))
                    else:
                        setattr(namespace, self.dest, fname)
                except:
                    for e in fname:
                        ext = os.path.splitext(e)[1][1:]
                        if ext not in choices:
                            option_string = '({})'.format(
                                option_string) if option_string else ''
                            parser.error("file doesn't end with one of {}{}".format(
                                choices, option_string))
                        else:
                            setattr(namespace, self.dest, fname)
        return Act

    steps = """
    Steps to use this script to produce an FPGA fabric:
        1) create/modify a fabric description (see fabric.csv as an example)

        2) create BEL primitives as VHDL code. 
        Use std_logic (not std_logic_vector) ports
        Follow the example in clb_slice_4xLUT4.vhdl
        Only one entity allowed in the file!
        If you use further components, they go into extra files.
        The file name and the entity should match.
        Ensure the top-level BEL VHDL-file is in your fabric description.

        3) run the script with the -GenTileSwitchMatrixCSV switch
        This will eventually overwrite all old switch matrix csv-files!!!

        4) Edit the switch matrix adjacency (the switch matrix csv-files). 

        5) run the script with the -GenTileSwitchMatrixVHDL switch
        This will generate switch matrix VHDL files

        6) run the script with the -GenTileHDL switch
        This will generate all tile VHDL files

        Note that the only manual VHDL code is implemented in 2) the rest is autogenerated!
    """

    parser = argparse.ArgumentParser(description=steps)

    parser.add_argument('-f', "--fabric_csv",
                        default="fabric.csv",
                        action=CheckExt({'csv'}),
                        help="Specifiy Fabric CSV file, if not specified will use the fabric.csv in the current directory")

    parser.add_argument('-s', "--src",
                        default=".",
                        dest="src_dir",
                        help="Specify the source directory of the fabric design")

    parser.add_argument('-o', "--out",
                        dest="out_dir",
                        help="Specify the output directory of the files. If not set will be default to be the same as source directory. This option do not affect -CSV2list and -AddList2CSV output directory")

    parser.add_argument('-GenTileSwitchMatrixCSV',
                        default=False,
                        action='store_true',
                        help='generate initial switch matrix template (has to be bootstrapped first)')

    parser.add_argument('-GenTileSwitchMatrixVHDL',
                        default=False,
                        action='store_true',
                        help='generate initial switch matrix VHDL code')

    parser.add_argument('-GenTileSwitchMatrixVerilog',
                        default=False,
                        action='store_true',
                        help='generate all tile VHDL descriptions')

    parser.add_argument('-GenTileConfigMemVHDL',
                        default=False,
                        action='store_true')

    parser.add_argument('-GenTileConfigMemVerilog',
                        default=False,
                        action='store_true')

    parser.add_argument('-GenTileVHDL',
                        default=False,
                        action='store_true')

    parser.add_argument('-GenTileVerilog',
                        default=False,
                        action='store_true')

    parser.add_argument('-GenFabricVHDL',
                        default=False,
                        action='store_true')

    parser.add_argument('-GenFabricVerilog',
                        default=False,
                        action='store_true')

    parser.add_argument('-GenFabricTopWrapperVHDL',
                        default=False,
                        action='store_true',
                        help='Generate the top wrapper for the fabric design with VHDL')

    parser.add_argument('-GenFabricTopWrapperVerilog',
                        default=False,
                        action='store_true',
                        help='Generate the top wrapper for the fabric design with Verilog')

    parser.add_argument('-run_all',
                        default=False,
                        action='store_true',
                        help="run -GenTileSwitchMatrixCSV, -GenTileSwitchMatrixVHDL, -GenTileVHDL in one go")

    parser.add_argument('-CSV2list',
                        nargs=2,
                        metavar=('in.csv', 'out.list'),
                        action=CheckExt({'csv', 'list'}),
                        help='translate a switch matrix adjacency matrix into a list (beg_port,end_port)')

    parser.add_argument('-AddList2CSV',
                        nargs=2,
                        metavar=('in.list', 'out.csv'),
                        action=CheckExt({'csv', 'list'}),
                        help='adds connctions from a list (beg_port,end_port) to a switch matrix adjacency matrix')

    parser.add_argument('-PrintCSV_FileInfo',
                        action=CheckExt({'csv'}),
                        help='prints input and oputput ports in csv switch matrix files')

    parser.add_argument('-GenNextpnrModel',
                        default=False,
                        action='store_true')

    parser.add_argument('-GenNextpnrModel_pair',
                        default=False,
                        action='store_true')

    parser.add_argument('-GenVPRModel',
                        action=CheckExt({'xml'}),
                        metavar=('custom_info.xml'))

    parser.add_argument('-debug', default=False,
                        action='store_true', help='debug mode')

    parser.add_argument('-GenBitstreamSpec',
                        metavar=("meta_data.txt"),
                        help='generates a bitstream spec for fasm parsing')

    args = parser.parse_args()

    src_dir = args.src_dir
    if args.out_dir:
        out_dir = args.out_dir
    else:
        out_dir = src_dir

    fabric = parseFabricCSV(args.fabric_csv)

    fabric.configBitMode = fabric.configBitMode
    fabric.frameBitsPerRow = fabric.frameBitsPerRow
    fabric.maxFramesPerCol = fabric.maxFramesPerCol
    PACKAGE = fabric.package
    # time in ps - this is needed for simulation as a fabric configuration can result in loops crashing the simulator
    GENERATE_DELAY_IN_SWITCH_MATRIX = fabric.generateDelayInSwitchMatrix
    # 'custom': using our hard-coded MUX-4 and MUX-16; 'generic': using standard generic RTL code
    MULTIPLEXER_STYLE = fabric.multiplexerStyle
    # generate switch matrix select signals (index) which is useful to verify if bitstream matches bitstream
    SWITCH_MATRIX_DEBUG_SIGNAL = True
    # enable SuperTile generation
    SUPER_TILE_ENABLE = fabric.superTileEnable

    src_dir = "./"

    writer = VerilogWriter("")

    # The original plan was to do something super generic where tiles can be arbitrarily defined.
    # However, that would have let into a heterogeneous/flat FPGA fabric as each tile may have different sets of wires to route.
    # If we say that a wire is defined by/in its source cell then that implies how many wires get routed through adjacent neighbouring tiles.
    # To keep things simple, we left this all out and the wiring between tiles is the same (for the first shot)

    if args.GenTileSwitchMatrixCSV or args.run_all:
        print('### Generate initial switch matrix template (has to be bootstrapped first)')
        fabricGen = FabricGenerator(fabric, writer)
        fabricGen.bootstrapSwitchMatrix()

    if args.GenTileSwitchMatrixVHDL or args.run_all:
        print('### Generate initial switch matrix VHDL code')
        for tile in fabric.tileDic:
            print(
                f'### generate VHDL for tile {tile} # filename: {out_dir}/{str(tile)}_switch_matrix.vhdl)')
            writer = VHDLWriter(f"{out_dir}/{str(tile)}_switch_matrix.vhdl")
            fabricGen = FabricGenerator(fabric, writer)
            fabricGen.genTileSwitchMatrix(fabric.tileDic[tile])

    if args.GenTileSwitchMatrixVerilog or args.run_all:
        print('### Generate initial switch matrix Verilog code')
        for tile in fabric.tileDic:
            print(
                f"### generate Verilog for tile {tile} # filename: {out_dir}/{str(tile)}_switch_matrix.v")
            writer = VerilogWriter(f"{out_dir}/{str(tile)}_switch_matrix.v")
            fabricGen = FabricGenerator(fabric, writer)
            fabricGen.genTileSwitchMatrix(
                fabric.tileDic[tile], f"{src_dir}/{str(tile)}_switch_matrix.csv")

    if args.GenTileConfigMemVHDL or args.run_all:
        print('### Generate all tile HDL descriptions')
        for tile in fabric.tileDic:
            print(
                f"### generate configuration bitstream storage VHDL for tile {tile} # filename: {out_dir}/{str(tile)}_ConfigMem.vhdl")
            writer = VHDLWriter(f"{out_dir}/{str(tile)}_ConfigMem.vhdl")
            fabricGen = FabricGenerator(fabric, writer)
            fabricGen.generateConfigMem(
                fabric.tileDic[tile], f"{src_dir}/{str(tile)}_ConfigMem.csv")

    if args.GenTileConfigMemVerilog or args.run_all:
        for tile in fabric.tileDic:
            print(
                f"### generate configuration bitstream storage Verilog for tile {tile} # filename: {out_dir}/{str(tile)}_ConfigMem.v')")
            writer = VerilogWriter(f"{out_dir}/{str(tile)}_ConfigMem.v")
            fabricGen = FabricGenerator(fabric, writer)
            fabricGen.generateConfigMem(
                fabric.tileDic[tile], f"{src_dir}/{str(tile)}_ConfigMem.csv")

    if args.GenTileVHDL or args.run_all:
        print('### Generate all tile HDL descriptions')
        for tile in fabric.tileDic:
            print(
                f"### generate VHDL for tile {tile} # filename:', {out_dir}/{str(tile)}_tile.vhdl")
            writer = VHDLWriter(f"{out_dir}/{str(tile)}_tile.vhdl")
            fabricGen = FabricGenerator(fabric, writer)
            fabricGen.generateTile(fabric.tileDic[tile])

        if SUPER_TILE_ENABLE:
            for superTile in fabric.superTileDic:
                print(
                    f"### generate VHDL for SuperTile {superTile} # filename: {out_dir}/{superTile}_tile.vhdl")
                writer = VHDLWriter(f"{out_dir}/{str(superTile)}_tile.vhdl")
                fabricGen = FabricGenerator(fabric, writer)
                fabricGen.generateSuperTile(
                    fabric.superTileDic[superTile])

    if args.GenTileVerilog or args.run_all:
        for tile in fabric.tileDic:
            print(
                f"### generate Verilog for tile {tile} # filename: {out_dir}/{str(tile)}_tile.v")
            writer = VerilogWriter(f"{out_dir}/{str(tile)}_tile.v")
            fabricGen = FabricGenerator(fabric, writer)
            fabricGen.generateTile(fabric.tileDic[tile])

        if SUPER_TILE_ENABLE:
            for superTile in fabric.superTileDic:
                print(
                    f"### generate Verilog for SuperTile {superTile} # filename: {out_dir}/{superTile}_tile.v")
                writer = VerilogWriter(f"{out_dir}/{str(superTile)}_tile.v")
                fabricGen = FabricGenerator(fabric, writer)
                fabricGen.generateSuperTile(fabric.superTileDic[superTile])

    if args.GenFabricVHDL or args.run_all:
        print('### Generate the Fabric VHDL descriptions ')
        writer = VHDLWriter(f"{out_dir}/fabric.vhdl")
        fabricGen = FabricGenerator(fabric, writer)
        fabricGen.generateFabric()

    if args.GenFabricVerilog or args.run_all:
        print('### Generate the Fabric Verilog descriptions')
        writer = VerilogWriter(f"{out_dir}/fabric.v")
        fabricGen = FabricGenerator(fabric, writer)
        fabricGen.generateFabric()

    if args.GenFabricTopWrapperVerilog or args.run_all:
        print("### Generate the Fabric Top Wrapper")
        writer = VerilogWriter(f"{out_dir}/{fabric.name}_top.v")
        fabricGen = FabricGenerator(fabric, writer)
        fabricGen.generateTopWrapper()

    if args.GenFabricTopWrapperVHDL or args.run_all:
        print("### Generate the Fabric Top Wrapper")
        writer = VHDLWriter(f"{out_dir}/{fabric.name}_top.vhdl")
        fabricGen = FabricGenerator(fabric, writer)
        fabricGen.generateTopWrapper()

    if args.CSV2list:
        InFileName, OutFileName = args.CSV2list
        fabricGen = FabricGenerator(fabric, writer)
        fabricGen.CSV2list(InFileName, OutFileName)

    if args.AddList2CSV:
        InFileName, OutFileName = args.AddList2CSV
        fabricGen = FabricGenerator(fabric, writer)
        fabricGen.list2CSV(InFileName, OutFileName)

    if args.PrintCSV_FileInfo:
        PrintCSV_FileInfo(args.PrintCSV_FileInfo)

    if args.GenNextpnrModel:
        # read fabric description as a csv file (Excel uses tabs '\t' instead of ',')
        print('### Read Fabric csv file ###')
        try:
            FabricFile = [i.strip('\n').split(',')
                          for i in open(args.fabric_csv)]
            # filter = 'Fabric' is default to get the definition between 'FabricBegin' and 'FabricEnd'
            fabric = GetFabric(FabricFile)
        except IOError:
            print("Could not open fabric.csv file")
            exit(-1)

        fabricObject = genFabricObject(fabric, FabricFile)
        pipFile = open("npnroutput/pips.txt", "w")
        belFile = open("npnroutput/bel.txt", "w")
        #pairFile = open("npnroutput/wirePairs.csv", "w")
        constraintFile = open("npnroutput/template.pcf", "w")

        npnrModel = npnr.genNextpnrModel(fabricObject, False)

        pipFile.write(npnrModel[0])
        belFile.write(npnrModel[1])
        constraintFile.write(npnrModel[2])

        pipFile.close()
        belFile.close()
        constraintFile.close()

        with open("npnroutput/template.v", "w") as templateFile:
            templateFile.write(npnr.genVerilogTemplate(fabricObject))

    if args.GenNextpnrModel_pair:
        # read fabric description as a csv file (Excel uses tabs '\t' instead of ',')
        print('### Read Fabric csv file ###')
        try:
            FabricFile = [i.strip('\n').split(',')
                          for i in open(args.fabric_csv)]
            # filter = 'Fabric' is default to get the definition between 'FabricBegin' and 'FabricEnd'
            fabric = GetFabric(FabricFile)
        except IOError:
            print("Could not open fabric.csv file")
            exit(-1)

        fabricObject = genFabricObject(fabric, FabricFile)
        pipFile = open("npnroutput/pips.txt", "w")
        belFile = open("npnroutput/bel.txt", "w")
        pairFile = open("npnroutput/wirePairs.csv", "w")
        constraintFile = open("npnroutput/template.pcf", "w")

        npnrModel = npnr.genNextpnrModel(fabricObject)

        pipFile.write(npnrModel[0])
        belFile.write(npnrModel[1])
        constraintFile.write(npnrModel[2])
        pairFile.write(npnrModel[3])

        pipFile.close()
        belFile.close()
        constraintFile.close()
        pairFile.close()

        with open("npnroutput/template.v", "w") as templateFile:
            templateFile.write(npnr.genVerilogTemplate(fabricObject))

    if args.GenVPRModel:
        # read fabric description as a csv file (Excel uses tabs '\t' instead of ',')
        print('### Read Fabric csv file ###')
        try:
            FabricFile = [i.strip('\n').split(',')
                          for i in open(args.fabric_csv)]
            # filter = 'Fabric' is default to get the definition between 'FabricBegin' and 'FabricEnd'
            fabric = GetFabric(FabricFile)
        except IOError:
            print("Could not open fabric.csv file")
            exit(-1)

        customXmlFilename = args.GenVPRModel
        fabricObject = genFabricObject(fabric, FabricFile)
        archFile = open(f"{out_dir}/architecture.xml", "w")
        rrFile = open(f"{out_dir}/routing_resources.xml", "w")

        archFile = open("vproutput/architecture.xml", "w")
        archXML = vpr.genVPRModelXML(fabricObject, customXmlFilename, False)
        archFile.write(archXML)
        archFile.close()

        rrFile = open("vproutput/routing_resources.xml", "w")
        rrGraphXML = vpr.genVPRModelRRGraph(fabricObject, False)
        rrFile.write(rrGraphXML)
        rrFile.close()

        if args.debug:
            with open("vproutput/template.v", "w") as templateFile:
                templateFile.write(npnr.genVerilogTemplate(fabricObject))

            with open("vproutput/fab_constraints.xml", "w") as constraintFile:
                constraintFile.write(vpr.genVPRModelConstraints(fabricObject))
            print(archXML)
            print(rrGraphXML)

    if args.GenBitstreamSpec:
        print('### Read Fabric csv file ###')
        fabric = parseFabricCSV(args.fabric_csv)
        fabricGen = FabricGenerator(fabric, writer)
        specObject = fabricGen.generateBitsStreamSpec()
        with open(args.GenBitstreamSpec, "wb") as outFile:
            pickle.dump(specObject, outFile)

        with open("spec.json", "w") as f:
            json.dump(specObject, f, indent=4)

        w = csv.writer(open(args.GenBitstreamSpec.replace("txt", "csv"), "w"))
        for key1 in specObject["TileSpecs"]:
            w.writerow([key1])
            for key2, val in specObject["TileSpecs"][key1].items():
                w.writerow([key2, val])
