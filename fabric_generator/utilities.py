import csv
import re
from parser import parseList
import collections

# Default parameters (will be overwritten if defined in fabric between 'ParametersBegin' and 'ParametersEnd'
#Parameters = [ 'ConfigBitMode', 'FrameBitsPerRow' ]
ConfigBitMode = 'FlipFlopChain'
FrameBitsPerRow = 32
MaxFramesPerCol = 20
Package = 'use work.my_package.all;'
# time in ps - this is needed for simulation as a fabric configuration can result in loops crashing the simulator
GenerateDelayInSwitchMatrix = '100'
# 'custom': using our hard-coded MUX-4 and MUX-16; 'generic': using standard generic RTL code
MultiplexerStyle = 'custom'
# generate switch matrix select signals (index) which is useful to verify if bitstream matches bitstream
SwitchMatrixDebugSignals = True
SuperTileEnable = True		# enable SuperTile generation

src_dir = "./"

# TILE field aliases
direction = 0
source_name = 1
X_offset = 2
Y_offset = 3
destination_name = 4
wires = 5

# bitstream mapping aliases
frame_name = 0
frame_index = 1
bits_used_in_frame = 2
used_bits_mask = 3
ConfigBits_ranges = 4

# columns where VHDL file is specified
VHDL_file_position = 1
TileType_position = 1

# BEL prefix field (needed to allow multiple instantiations of the same BEL inside the same tile)
BEL_prefix = 2
# MISC
All_Directions = ['NORTH', 'EAST', 'SOUTH', 'WEST']
Opposite_Directions = {"NORTH": "SOUTH",
                       "EAST": "WEST", "SOUTH": "NORTH", "WEST": "EAST"}

# Given a fabric array description, return all uniq cell types


def GetCellTypes(list):
    # make the fabric flat
    flat_list = []
    for sublist in list:
        for item in sublist:
            flat_list.append(item)

    output = []
    for item in flat_list:
        if item not in output:
            output.append(item)

    # we use the keyword 'NULL' for padding tiles that we don't return
    if ('NULL' in output):
        output.remove('NULL')

    return output


# take a list and remove all items that contain a #    and remove empty lines
def RemoveComments(list):
    output = []

    for line in list:
        templine = []
        marker = False        # we use this marker to remember if we had an '#' element before
        for item in line:
            if item.startswith('#'):
                marker = True
            if not (item.startswith('#') or marker == True):
                # marker = True
                templine.append(item)
                if item == '':
                    templine.remove('')
        if templine != []:
            output.append(templine)
    return output


def GetFabric(list, filter='Fabric'):
    templist = []
    # output = []
    marker = False

    for sublist in list:
        if filter+'End' in sublist:            # was FabricEnd
            marker = False
        if marker == True:
            templist.append(sublist)
        # we place this conditional after the append such that the 'FabricBegin' will be kicked out
        if filter+'Begin' in sublist:        # was FabricBegin
            marker = True
    return RemoveComments(templist)


def GetTileFromFile(list, TileType):
    templist = []
    # output = []
    marker = False

    for sublist in list:
        if ('EndTILE' in sublist):
            marker = False
        if ('TILE' in sublist) and (TileType in sublist):
            marker = True
        if marker == True:
            templist.append(sublist)
        # we place this conditional after the append such that the 'FabricBegin' will be kicked out
        # if ('TILE' in sublist) and (type in sublist):
        # if ('TILE' in sublist) and (TileType in sublist):
            # marker = True
    return RemoveComments(templist)


def GetSuperTileFromFile(list):
    templist = []
    tempdict = {}
    superTile_type = ''
    marker = False
    for sublist in list:
        if 'SuperTILE' in sublist:
            marker = True
            superTile_type = sublist[1]
            continue
        elif 'EndSuperTILE' in sublist:
            marker = False
            tempdict[superTile_type] = RemoveComments(templist)
            templist = []
            continue
        if marker == True:
            templist.append(sublist)
    return tempdict


def PrintTileComponentPort(tile_description, entity, direction, file):
    print('\t-- ', direction, file=file)
    for line in tile_description:
        if line[0] == direction:
            print('\t\t', line[source_name],
                  '\t: out \tSTD_LOGIC_VECTOR( ', end='', file=file)
            print(((abs(int(line[X_offset]))+abs(int(line[Y_offset])))
                  * int(line[wires]))-1, end='', file=file)
            print(' downto 0 );', end='', file=file)
            print('\t -- wires: ', line[wires], file=file)
    for line in tile_description:
        if line[0] == direction:
            print('\t\t', line[destination_name],
                  '\t: in \tSTD_LOGIC_VECTOR( ', end='', file=file)
            print(((abs(int(line[X_offset]))+abs(int(line[Y_offset])))
                  * int(line[wires]))-1, end='', file=file)
            print(' downto 0 );', end='', file=file)
            print('\t -- wires: ', line[wires], file=file)
    return


def replace(string, substitutions):
    substrings = sorted(substitutions, key=len, reverse=True)
    regex = re.compile('|'.join(map(re.escape, substrings)))
    return regex.sub(lambda match: substitutions[match.group(0)], string)


def PrintComponentDeclarationForFile(VHDL_file_name, file):
    ConfigPortUsed = 0  # 1 means is used
    VHDLfile = [line.rstrip('\n')
                for line in open(f"{src_dir}/{VHDL_file_name}")]
    templist = []
    marker = False
    # for item in VHDLfile:
    # print(item)
    for line in VHDLfile:
        # NumberOfConfigBits:0 means no configuration port
        if re.search('NumberOfConfigBits', line, flags=re.IGNORECASE):
            # NumberOfConfigBits appears, so we may have a config port
            ConfigPortUsed = 1
            # but only if the following is not true
            if re.search('NumberOfConfigBits:0', line, flags=re.IGNORECASE):
                ConfigPortUsed = 0
        if marker == True:
            print(re.sub('entity', 'component', line,
                  flags=re.IGNORECASE), file=file)
        if re.search('^entity', line, flags=re.IGNORECASE):
            # print(str.replace('^entity', line))
            # re.sub('\$noun\$', 'the heck', 'What $noun$ is $verb$?')
            print(re.sub('entity', 'component', line,
                  flags=re.IGNORECASE), file=file)
            marker = True
        if re.search('^end ', line, flags=re.IGNORECASE):
            marker = False
    print('', file=file)
    return ConfigPortUsed


def GetComponentPortsFromFile(VHDL_file_name, filter='ALL', port='internal', BEL_Prefix=''):
    VHDLfile = [line.rstrip('\n')
                for line in open(f"{src_dir}/{VHDL_file_name}")]

    Inputs = []
    Outputs = []
    ExternalPorts = []
    marker = False
    FoundEntityMarker = False
    DoneMarker = False
    direction = ''
    for line in VHDLfile:
        # the order of the if-statements are important ;
        if re.search('^entity', line, flags=re.IGNORECASE):
            FoundEntityMarker = True

        # detect the direction from comments, like "--NORTH"
        # we need this to filter for a specific direction
        # this implies of course that this information is provided in the VHDL entity
        if re.search('NORTH', line, flags=re.IGNORECASE):
            direction = 'NORTH'
        if re.search('EAST', line, flags=re.IGNORECASE):
            direction = 'EAST'
        if re.search('SOUTH', line, flags=re.IGNORECASE):
            direction = 'SOUTH'
        if re.search('WEST', line, flags=re.IGNORECASE):
            direction = 'WEST'

        # all primitive pins that are connected to the switch matrix have to go before the GLOBAL label
        if re.search('-- global', line, flags=re.IGNORECASE):
            FoundEntityMarker = False
            marker = False
            DoneMarker = True

        if (marker == True) and (DoneMarker == False) and (direction == filter or filter == 'ALL'):
            # detect if the port has to be exported as EXTERNAL which is flagged by the comment
            if re.search('EXTERNAL', line):
                External = True
            else:
                External = False
            if re.search('CONFIG_PORT', line):
                Config = True
            else:
                Config = False
            # get rid of everything with and after the ';' that will also remove comments
            # substitutions = {';.*', '', '--.*', '', ',.*', ''}
            # tmp_line=(replace(line, substitutions))
            # tmp_line = (re.sub(';.*', '',(re.sub('--.*', '',line, flags=re.IGNORECASE)), flags=re.IGNORECASE))
            tmp_line = (re.sub(';.*', '', (re.sub('--.*', '', (re.sub(',.*', '', line,
                        flags=re.IGNORECASE)), flags=re.IGNORECASE)), flags=re.IGNORECASE))
            std_vector = ''
            if re.search('std_logic_vector', tmp_line, flags=re.IGNORECASE):
                std_vector = (re.sub('.*std_logic_vector', '',
                              tmp_line, flags=re.IGNORECASE))
            tmp_line = (re.sub('STD_LOGIC.*', '',
                        tmp_line, flags=re.IGNORECASE))

            substitutions = {" ": "", "\t": ""}
            tmp_line = (replace(tmp_line, substitutions))
            # at this point, we get clean port names, like
            # A0:in
            # A1:in
            # A2:in
            # The following is for internal fabric signal ports (e.g., a CLB/LUT)
            if (port == 'internal') and (External == False) and (Config == False):
                if re.search(':in', tmp_line, flags=re.IGNORECASE):
                    Inputs.append(
                        BEL_Prefix+(re.sub(':in.*', '', tmp_line, flags=re.IGNORECASE))+std_vector)
                if re.search(':out', tmp_line, flags=re.IGNORECASE):
                    Outputs.append(
                        BEL_Prefix+(re.sub(':out.*', '', tmp_line, flags=re.IGNORECASE))+std_vector)
            # The following is for ports that have to go all the way up to the top-level entity (e.g., from an I/O cell)
            if (port == 'external') and (External == True):
                # .lstrip() removes leading white spaces including ' ', '\t'
                ExternalPorts.append(BEL_Prefix+line.lstrip())

            # frame reconfiguration needs a port for writing in frame data
            if (port == 'frame_config') and (Config == True):
                # .lstrip() removes leading white spaces including ' ', '\t'
                ExternalPorts.append(BEL_Prefix+line.lstrip())

        if re.search('port', line, flags=re.IGNORECASE):
            marker = True
    if port == 'internal':             # default
        return Inputs, Outputs
    else:
        return ExternalPorts


def GetComponentPortsFromVerilog(Verilog_file_name, filter='ALL', port='internal', BEL_Prefix=''):
    Verilogfile = [line.rstrip('\n') for line in open(Verilog_file_name)]
    Inputs = []
    Outputs = []
    ExternalPorts = []
    marker = False
    FoundEntityMarker = False
    DoneMarker = False
    direction = ''
    for line in Verilogfile:
        # the order of the if-statements are important ;
        if re.search('^module', line, flags=re.IGNORECASE):
            FoundEntityMarker = True

        # detect the direction from comments, like "--NORTH"
        # we need this to filter for a specific direction
        # this implies of course that this information is provided in the VHDL entity
        if re.search('NORTH', line, flags=re.IGNORECASE):
            direction = 'NORTH'
        if re.search('EAST', line, flags=re.IGNORECASE):
            direction = 'EAST'
        if re.search('SOUTH', line, flags=re.IGNORECASE):
            direction = 'SOUTH'
        if re.search('WEST', line, flags=re.IGNORECASE):
            direction = 'WEST'

        # all primitive pins that are connected to the switch matrix have to go before the GLOBAL label
        if re.search('// global', line, flags=re.IGNORECASE):
            FoundEntityMarker = False
            marker = False
            DoneMarker = True

        if (marker == True) and (DoneMarker == False) and (direction == filter or filter == 'ALL'):
            # detect if the port has to be exported as EXTERNAL which is flagged by the comment
            if re.search('EXTERNAL', line):
                External = True
            else:
                External = False
            if re.search('CONFIG_PORT', line):
                Config = True
            else:
                Config = False
            # get rid of everything with and after the ';' that will also remove comments
            # substitutions = {';.*', '', '--.*', '', ',.*', ''}
            # tmp_line=(replace(line, substitutions))
            # tmp_line = (re.sub(';.*', '',(re.sub('--.*', '',line, flags=re.IGNORECASE)), flags=re.IGNORECASE))
            tmp_line = (re.sub(';.*', '', (re.sub('//.*', '', (re.sub(',.*', '', line,
                        flags=re.IGNORECASE)), flags=re.IGNORECASE)), flags=re.IGNORECASE))
            std_vector = ''
            if re.search('input', tmp_line, flags=re.IGNORECASE) or re.search('output', tmp_line, flags=re.IGNORECASE):
                std_vector = (re.sub('.*std_logic_vector', '',
                              tmp_line, flags=re.IGNORECASE))
            tmp_line = (re.sub('STD_LOGIC.*', '',
                        tmp_line, flags=re.IGNORECASE))

            substitutions = {" ": "", "\t": ""}
            tmp_line = (replace(tmp_line, substitutions))
            # at this point, we get clean port names, like
            # A0:in
            # A1:in
            # A2:in
            # The following is for internal fabric signal ports (e.g., a CLB/LUT)
            if (port == 'internal') and (External == False) and (Config == False):
                if re.search(':in', tmp_line, flags=re.IGNORECASE) and 'integer' not in tmp_line:
                    Inputs.append(
                        BEL_Prefix+(re.sub(':in.*', '', tmp_line, flags=re.IGNORECASE))+std_vector)
                if re.search(':out', tmp_line, flags=re.IGNORECASE):
                    Outputs.append(
                        BEL_Prefix+(re.sub(':out.*', '', tmp_line, flags=re.IGNORECASE))+std_vector)
            # The following is for ports that have to go all the way up to the top-level entity (e.g., from an I/O cell)
            if (port == 'external') and (External == True):
                # .lstrip() removes leading white spaces including ' ', '\t'
                ExternalPorts.append(BEL_Prefix+line.lstrip())

            # frame reconfiguration needs a port for writing in frame data
            if (port == 'frame_config') and (Config == True):
                # .lstrip() removes leading white spaces including ' ', '\t'
                ExternalPorts.append(BEL_Prefix+line.lstrip())

        if re.search('port', line, flags=re.IGNORECASE):
            marker = True
    if port == 'internal':             # default
        return Inputs, Outputs
    else:
        return ExternalPorts


def GetNoConfigBitsFromFile(VHDL_file_name):
    with open(VHDL_file_name, 'r') as f:
        file = f.read()
    result = re.search(
        r"NoConfigBits\s*:\s*integer\s*:=\s*(\w+)", file, flags=re.IGNORECASE)
    if result:
        try:
            return int(result.group(1))
        except ValueError:
            return 0


def GetComponentEntityNameFromFile(VHDL_file_name):
    VHDLfile = [line.rstrip('\n')
                for line in open(f"{src_dir}/{VHDL_file_name}")]
    for line in VHDLfile:
        # the order of the if-statements is important
        if re.search('^entity', line, flags=re.IGNORECASE):
            result = (re.sub(' ', '', (re.sub('entity', '', (re.sub(
                ' is.*', '', line, flags=re.IGNORECASE)), flags=re.IGNORECASE))))
    return result


def GetComponentEntityNameFromVerilog(Verilog_file_name):
    Verilogfile = [line.rstrip('\n') for line in open(
        f"{src_dir}/{Verilog_file_name}")]
    for line in Verilogfile:
        # the order of the if-statements is important
        if re.search('^module', line, flags=re.IGNORECASE):
            result = (re.sub(' ', '', (re.sub('module', '', (re.sub(
                ' (.*', '', line, flags=re.IGNORECASE)), flags=re.IGNORECASE))))
    return result


def GetTileComponentPorts(tile_description, mode='SwitchMatrix'):
    Inputs = []
    Outputs = []
    OpenIndex = ''
    CloseIndex = ''
    if re.search('Indexed', mode, flags=re.IGNORECASE):
        OpenIndex = '('
        CloseIndex = ')'
    for line in tile_description:
        if (line[direction] == 'NORTH') or (line[direction] == 'EAST') or (line[direction] == 'SOUTH') or (line[direction] == 'WEST'):
            # range (wires-1 downto 0) as connected to the switch matrix
            if mode in ['SwitchMatrix', 'SwitchMatrixIndexed']:
                ThisRange = int(line[wires])
            if mode in ['AutoSwitchMatrix', 'AutoSwitchMatrixIndexed']:
                if line[source_name] == 'NULL' or line[destination_name] == 'NULL':
                    # the following line connects all wires to the switch matrix in the case one port is NULL (typically termination)
                    ThisRange = (
                        abs(int(line[X_offset]))+abs(int(line[Y_offset]))) * int(line[wires])
                else:
                    # the following line connects all bottom wires to the switch matrix in the case begin and end ports are used
                    ThisRange = int(line[wires])
            # range ((wires*distance)-1 downto 0) as connected to the tile top
            if mode in ['all', 'allIndexed', 'Top', 'TopIndexed', 'AutoTop', 'AutoTopIndexed']:
                ThisRange = (
                    abs(int(line[X_offset]))+abs(int(line[Y_offset]))) * int(line[wires])
            # the following three lines are needed to get the top line[wires] that are actually the connection from a switch matrix to the routing fabric
            StartIndex = 0
            if mode in ['Top', 'TopIndexed']:
                StartIndex = (
                    (abs(int(line[X_offset]))+abs(int(line[Y_offset])))-1) * int(line[wires])
            if mode in ['AutoTop', 'AutoTopIndexed']:
                if line[source_name] == 'NULL' or line[destination_name] == 'NULL':
                    # in case one port is NULL, then the all the other port wires get connected to the switch matrix.
                    StartIndex = 0
                else:
                    # "normal" case as for the CLBs
                    StartIndex = (
                        (abs(int(line[X_offset]))+abs(int(line[Y_offset])))-1) * int(line[wires])
            for i in range(StartIndex, ThisRange):
                if line[destination_name] != 'NULL':
                    Inputs.append(line[destination_name] +
                                  OpenIndex+str(i)+CloseIndex)
                if line[source_name] != 'NULL':
                    Outputs.append(line[source_name] +
                                   OpenIndex+str(i)+CloseIndex)

    return Inputs, Outputs


def GetTileComponentPortsVectors(tile_description, mode):
    Inputs = []
    Outputs = []
    MaxIndex = 0
    for line in tile_description:
        if (line[direction] == 'NORTH') or (line[direction] == 'EAST') or (line[direction] == 'SOUTH') or (line[direction] == 'WEST'):
            # range (wires-1 downto 0) as connected to the switch matrix
            if mode in ['SwitchMatrix', 'SwitchMatrixIndexed']:
                MaxIndex = int(line[wires])
            # range ((wires*distance)-1 downto 0) as connected to the tile top
            if mode in ['all', 'allIndexed']:
                MaxIndex = (
                    abs(int(line[X_offset]))+abs(int(line[Y_offset]))) * int(line[wires])
            if line[destination_name] != 'NULL':
                Inputs.append(
                    str(line[destination_name]+'('+str(MaxIndex)+' downto 0)'))
            if line[source_name] != 'NULL':
                Outputs.append(
                    str(line[source_name]+'('+str(MaxIndex)+' downto 0)'))
    return Inputs, Outputs


def PrintCSV_FileInfo(CSV_FileName):
    CSVFile = [i.strip('\n').split(',') for i in open(CSV_FileName)]
    print('Tile: ', str(CSVFile[0][0]), '\n')

    # print('DEBUG:',CSVFile)

    print('\nInputs: \n')
    CSVFileRows = len(CSVFile)
    # for port in CSVFile[0][1:]:
    line = CSVFile[0]
    for k in range(1, len(line)):
        PortList = []
        PortCount = 0
        for j in range(1, len(CSVFile)):
            if CSVFile[j][k] != '0':
                PortList.append(CSVFile[j][0])
                PortCount += 1
        print(line[k], ' connects to ', PortCount, ' ports: ', PortList)

    print('\nOutputs: \n')
    for line in CSVFile[1:]:
        # we first count the number of multiplexer inputs
        mux_size = 0
        PortList = []
        # for port in line[1:]:
        # if port != '0':
        for k in range(1, len(line)):
            if line[k] != '0':
                mux_size += 1
                PortList.append(CSVFile[0][k])
        print(line[0], ',', str(mux_size), ', Source port list: ', PortList)
    return


def CSV2list(InFileName, OutFileName):
    # this function is export a given CSV switch matrix description into its equivalent list description
    # format: destination,source (per line)
    # read CSV file into an array of strings
    InFile = [i.strip('\n').split(',') for i in open(InFileName)]
    f = open(OutFileName, 'w')
    rows = len(InFile)      # get the number of tiles in vertical direction
    cols = len(InFile[0])    # get the number of tiles in horizontal direction
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
    f.close()
    return


def ExpandListPorts(port, PortList):
    # a leading '[' tells us that we have to expand the list
    if re.search('\[', port):
        if not re.search('\]', port):
            raise ValueError(
                '\nError in function ExpandListPorts: cannot find closing ]\n')
        # port.find gives us the first occurrence index in a string
        left_index = port.find('[')
        right_index = port.find(']')
        before_left_index = port[0:left_index]
        # right_index is the position of the ']' so we need everything after that
        after_right_index = port[(right_index+1):]
        ExpandList = []
        ExpandList = re.split('\|', port[left_index+1:right_index])
        for entry in ExpandList:
            ExpandListItem = (before_left_index+entry+after_right_index)
            ExpandListPorts(ExpandListItem, PortList)

    else:
        # print('DEBUG: else, just:',port)
        PortList.append(port)
    return


def list2CSV(InFileName, OutFileName):
    # this function is export a given list description into its equivalent CSV switch matrix description
    # format: source,destination (per line)
    # read CSV file into an array of strings

    print(f"### Adding {InFileName} to {OutFileName}")

    connectionPair = parseList(InFileName)

    with open(OutFileName, "r") as f:
        file = f.readlines()

    col = len(file[0].split(','))
    rows = len(file)

    # create a 0 zero matrix as initialization
    matrix = [[0 for _ in range(col)] for _ in range(rows)]

    # load the data from the original csv into the matrix
    for i in range(1, len(file)):
        for j in range(1, len(file[i].split(','))):
            matrix[i-1][j-1] = int(file[i].split(',')[j])

    # get source and destination list in the csv
    source = file[0].strip("\n").split(',')[1:]
    destination = [file[i].split(",")[0] for i in range(1, len(file))]

    # set the matrix value with the provided connection pair
    for (s, d) in connectionPair:
        try:
            s_index = source.index(s)
        except ValueError:
            print(f"{s} is not in the source column of the matrix csv file")
            print()
            exit(-1)

        try:
            d_index = destination.index(d)
        except ValueError:
            print(f"{d} is not in the destination row of the matrix csv file")
            print()
            exit(-1)

        if matrix[d_index][s_index] != 0:
            print(
                f"connection ({s}, {d}) already exists in the original matrix")
        matrix[d_index][s_index] = 1

    # writing the matrix back to the given out file
    with open(OutFileName, "w") as f:
        f.write(file[0])
        for i in range(len(destination)):
            f.write(f"{destination[i]},")
            for j in range(len(source)):
                f.write(str(matrix[i][j]))
                if j != len(source) - 1:
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
    print("\n")


def generateConfigMemInit(file, globalConfigBitsCounter):
    # write configuration bits to frame mapping init file (e.g. 'LUT4AB_ConfigMem.init.csv')
    # this file can be modified and saved as 'LUT4AB_ConfigMem.csv' (without the '.init')
    bitsLeftToPackInFrames = globalConfigBitsCounter

    fieldName = ["#frame_name", "frame_index", "bits_used_in_frame",
                 "used_bits_mask", "ConfigBits_ranges"]

    with open(file, "w") as f:
        writer = csv.writer(f)
        writer.writerow(fieldName)
        for k in range(int(MaxFramesPerCol)):
            entry = []
            # frame0, frame1, ...
            entry.append(f"frame{k}")
            # and the index (0, 1, 2, ...), in case we need
            entry.append(str(k))
            # size of the frame in bits
            if bitsLeftToPackInFrames >= FrameBitsPerRow:
                entry.append(str(FrameBitsPerRow))
                # generate a string encoding a '1' for each flop used
                frameBitsMask = f"{2**FrameBitsPerRow-1:_b}"
                entry.append(frameBitsMask)
                entry.append(
                    f"{bitsLeftToPackInFrames-1}:{bitsLeftToPackInFrames-FrameBitsPerRow}")
                bitsLeftToPackInFrames -= FrameBitsPerRow
            else:
                entry.append(str(bitsLeftToPackInFrames))
                # generate a string encoding a '1' for each flop used
                # this will allow us to kick out flops in the middle (e.g. for alignment padding)
                frameBitsMask = (2**FrameBitsPerRow-1) - \
                    (2**(FrameBitsPerRow-bitsLeftToPackInFrames)-1)
                frameBitsMask = f"{frameBitsMask:0{FrameBitsPerRow+7}_b}"
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


if __name__ == '__main__':
    print(GetComponentPortsFromFile("MULADD.vhdl", port="external"))
