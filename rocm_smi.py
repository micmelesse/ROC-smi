#!/usr/bin/env python

from __future__ import print_function

import os
import argparse
import re
import sys
import subprocess
import json
from subprocess import check_output
import glob
import time
import collections
import math
if hasattr(__builtins__, 'raw_input'):
    input = raw_input

# Version of the JSON output used to save clocks
JSON_VERSION = 1

# Set to 1 if an error occurs
RETCODE = 0

# To write to sysfs, we need to run this script as root. If the script is not run as
# root, re-launch it via execvp to give the script sudo privileges.
def relaunchAsSudo():
    if os.geteuid() != 0:
        os.execvp('sudo', ['sudo'] + sys.argv)

nodesprefix = '/sys/class/kfd/kfd/topology/nodes'
drmprefix = '/sys/class/drm'
hwmonprefix = '/sys/class/hwmon'
powerprefix = '/sys/kernel/debug/dri/'

headerSpacer = '='*20
logSpacer = headerSpacer * 4

valuePaths = {
    'id' : {'prefix' : drmprefix, 'filepath' : 'device', 'needsparse' : True},
    'vbios' : {'prefix' : drmprefix, 'filepath' : 'vbios_version', 'needsparse' : False},
    'perf' : {'prefix' : drmprefix, 'filepath' : 'power_dpm_force_performance_level', 'needsparse' : False},
    'sclk_od' : {'prefix' : drmprefix, 'filepath' : 'pp_sclk_od', 'needsparse' : False},
    'mclk_od' : {'prefix' : drmprefix, 'filepath' : 'pp_mclk_od', 'needsparse' : False},
    'sclk' : {'prefix' : drmprefix, 'filepath' : 'pp_dpm_sclk', 'needsparse' : False},
    'mclk' : {'prefix' : drmprefix, 'filepath' : 'pp_dpm_mclk', 'needsparse' : False},
    'profile' : {'prefix' : drmprefix, 'filepath' : 'pp_power_profile_mode', 'needsparse' : False},
    'fan' : {'prefix' : hwmonprefix, 'filepath' : 'pwm1', 'needsparse' : False},
    'fanmax' : {'prefix' : hwmonprefix, 'filepath' : 'pwm1_max', 'needsparse' : False},
    'fanmode' : {'prefix' : hwmonprefix, 'filepath' : 'pwm1_enable', 'needsparse' : False},
    'temp' : {'prefix' : hwmonprefix, 'filepath' : 'temp1_input', 'needsparse' : True},
    'power' : {'prefix' : powerprefix, 'filepath' : 'amdgpu_pm_info', 'needsparse' : True},
    'used_mem':{'prefix' : nodesprefix, 'filepath' : 'used_memory', 'needsparse' : True},
    'total_mem':{'prefix' : nodesprefix, 'filepath' : 'properties', 'needsparse' : True},
}


def getSysfsValue(device, key):
    """ Return the desired SysFS value for a specified device

    Parameters:
    device -- Device to return the desired value
    value -- SysFS value to return (defined in dict above)
    """
    global RETCODE
    pathDict = valuePaths[key]
    fileValue = ''
    filePath = os.path.join(pathDict['prefix'], device, 'device', pathDict['filepath'])

    if pathDict['prefix'] == hwmonprefix:
        # HW Monitor values have a different path structure
        if not getHwmonFromDevice(device):
            return None
        filePath = os.path.join(getHwmonFromDevice(device), pathDict['filepath'])
    if pathDict['prefix'] == powerprefix:
        # Power consumption is in debugfs and has a different path structure
        filePath = os.path.join(powerprefix, device[4:], 'amdgpu_pm_info')

    if pathDict['prefix'] == nodesprefix:
        for root, _, files in os.walk(nodesprefix):
            if "used_memory" in files:
                if key == "used_mem":
                    filePath = os.path.join(root, "used_memory")
                elif key == "total_mem":
                    filePath = os.path.join(root, "properties")

    if not os.path.isfile(filePath):
        return None

    with open(filePath, 'r') as fileContents:
        fileValue = fileContents.read().rstrip('\n')

    # Some sysfs files aren't a single line of text
    if pathDict['needsparse']:
        fileValue = parseSysfsValue(key, fileValue)

    if fileValue == '':
        printLog(device, 'WARNING: Empty SysFS value: ' + key)

    return fileValue


def parseSysfsValue(key, value):
    """ Parse the sysfs value string

    Parameters:
    value -- SysFS value to parse

    Some SysFS files aren't a single line/string, so we need to parse it
    to get the desired value
    """
    if key == 'used_mem':
        # convert to megabytes
        return int(math.ceil(float(value)/1024/1024))
    if key == 'total_mem':
        m = re.search("size_in_bytes (.*)", value)
        if m:
            # convert to megabytes
            return int(math.ceil(float(m.group(1))/1024/1024))
        else:
            return ''
    if key == 'id':
        # Strip the 0x prefix
        return value[2:]
    if key == 'temp':
        # Convert from millidegrees
        return int(value) / 1000
    if key == 'power':
        # amdgpu_pm_info has a bunch of info, we only want GPU power usage
        for line in value.splitlines():
            if 'average GPU' in line:
                return str.lstrip(line.replace(' (average GPU)', ''))

    return ''


def parseDeviceNumber(deviceNum):
    """ Parse the device number, returning the format of card#

    Parameters:
    deviceNum -- Device number to parse
    """
    return 'card' + str(deviceNum)


def parseDeviceName(deviceName):
    """ Parse the device name, which is of the format card#.

    Parameters:
    deviceName -- Device name to parse
    """
    return deviceName[4:]


def printLog(device, log):
    """ Print out to the SMI log.

    Parameters:
    device -- Device that the log will reference
    log -- String to print to the log
    """
    print('GPU[', parseDeviceName(device), '] \t\t: ', log, sep='')


def doesDeviceExist(device):
    """ Check whether the specified device exists in sysfs.

    Parameters:
    device -- Device to check for existence
    """
    if os.path.exists(os.path.join(drmprefix, device)) == 0:
        return False
    return True


def getPid(name):
    return check_output(["pidof",name])


def confirmOverDrive(autoRespond):
    """ Print the warning for OverDrive functionality and prompt user to accept the terms.

    Parameters:
    autoRespond -- Response to automatically provide for all prompts
    """

    print('''
          ******WARNING******\n
          Operating your AMD GPU outside of official AMD specifications or outside of
          factory settings, including but not limited to the conducting of overclocking
          (including use of this overclocking software, even if such software has been
          directly or indirectly provided by AMD or otherwise affiliated in any way
          with AMD), may cause damage to your AMD GPU, system components and/or result
          in system failure, as well as cause other problems. DAMAGES CAUSED BY USE OF
          YOUR AMD GPU OUTSIDE OF OFFICIAL AMD SPECIFICATIONS OR OUTSIDE OF FACTORY
          SETTINGS ARE NOT COVERED UNDER ANY AMD PRODUCT WARRANTY AND MAY NOT BE COVERED
          BY YOUR BOARD OR SYSTEM MANUFACTURER'S WARRANTY. Please use this utility with caution.
          ''')
    if not autoRespond:
        user_input = input('Do you accept these terms? [y/N] ')
    else:
        user_input = autoRespond
    if user_input in ['Yes', 'yes', 'y', 'Y', 'YES']:
        return
    else:
        sys.exit('Confirmation not given. Exiting without setting OverDrive value')


def isPowerplayEnabled(device):
    """ Check if PowerPlay is enabled for a specified device.

    Parameters:
    device -- Device to check for PowerPlay enablement
    """
    if not doesDeviceExist(device) or os.path.isfile(os.path.join(drmprefix, device, 'device', 'power_dpm_force_performance_level')) == 0:
        return False
    return True


def getNumProfileArgs(device):
    """ Get the number of Power Profile fields for a specific device

    This varies per ASIC, so ensure that we get the right number of arguments

    Parameters:
    device -- Device to get the number of Power Profile fields
    """

    profile = getSysfsValue(device, 'profile')
    numHiddenFields = 0
    if not profile:
        return 0
    # Get the 1st line (column names)
    fields = profile.splitlines()[0]
    # SMU7 has 2 hidden fields for SclkProfileEnable and MclkProfileEnable
    if 'SCLK_UP_HYST' in fields:
        numHiddenFields = 2
    # Subtract 2 to remove NUM and MODE NAME, since they're not valid Profile fields
    return len(fields.split()) - 2 + numHiddenFields


def verifySetProfile(device, profile):
    """ Verify data from user to set as Power Profile.

    Ensure that we can set the profile, with Profiles being supported and
    the profile being passed in being valid

    Parameters:
    device -- Device to check for PowerPlay enablement
    """
    global RETCODE
    if not isPowerplayEnabled(device):
        printLog(device, 'PowerPlay not enabled, cannot specify profile.')
        RETCODE = 1
        return False

    # If it's 1 number, we're setting the level, not the Custom Profile
    if profile.isdigit():
        maxProfileLevel = getMaxLevel(device, 'profile')
        if int(profile) > maxProfileLevel:
            printLog(device, 'Cannot set profile to level ' + str(profile) + ', max level is ' + str(maxProfileLevel))
            return False
        return True
    # If we get a string, split it into elements to make it a list
    elif isinstance(profile, str):
        if profile == 'reset':
            printLog(device, 'Reset no longer accepted as a Power Profile')
            return False
        else:
            profileList = profile.strip().split(' ')
    elif isinstance(profile, collections.Iterable):
        profileList = profile
    else:
        printLog(device, 'Unsupported profile argument : ' + str(profile))
        return False
    numProfileArgs = getNumProfileArgs(device)
    if numProfileArgs == 0:
        printLog(device, 'Power Profiles not supported')
        return False
    if len(profileList) != numProfileArgs:
        printLog(device, 'Cannot set profile, must be 1 or ' + str(numProfileArgs) + ' values')
        RETCODE = 1
        return False

    return True


def writeProfileSysfs(device, value):
    if not verifySetProfile(device, value):
        return

    # Perf Level must be set to manual for a Power Profile to be specified
    # This is new compared to previous versions of the Power Profile
    setPerfLevel(device, 'manual')
    profilePath = os.path.join(drmprefix, device, 'device', 'pp_power_profile_mode')
    maxLevel = getMaxLevel(device, 'profile')
    # If it's a single number, then we're choosing the Power Profile, not setting CUSTOM
    if isinstance(value, str) and len(value) == 1:
        profileString = value
    # Otherwise, we're setting the CUSTOM profile
    elif isinstance(value, str) and len(value) > 1:
        # Prepend the Max Level of Profiles since that will always be the CUSTOM profile
        profileString = str(maxLevel) + value
    else:
        printLog(device, 'Invalid input argument' + value)
        return False
    if (writeToSysfs(profilePath, profileString)):
        return True
    return False


def writeToSysfs(fsFile, fsValue):
    """ Write to a sysfs file.

    Parameters:
    fsFile -- Path to the sysfs file to modify
    fsValue -- Value to write to the sysfs file
    """
    global RETCODE
    if not os.path.isfile(fsFile):
        print('Cannot write to sysfs file ' + fsFile + '. File does not exist', sep='')
        RETCODE = 1
        return False
    with open(fsFile, 'w') as fs:
        try:
            fs.write(fsValue + '\n') # Certain sysfs files require \n at the end
        except OSError:
            print('Unable to write to sysfs file' + fsFile)
            RETCODE = 1
            return False
    return True


def listDevices():
    """ Return a list of GPU devices."""
    devicelist = [device for device in os.listdir(drmprefix) if re.match(r'^card\d+$', device)]
    devicelist.sort()
    return devicelist


def listAmdHwMons():
    """Return a list of AMD HW Monitors."""
    hwmons = []

    for mon in os.listdir(hwmonprefix):
        tempname = os.path.join(hwmonprefix, mon, 'name')
        if os.path.isfile(tempname):
            with open(tempname, 'r') as tempmon:
                drivername = tempmon.read().rstrip('\n')
                if drivername in ['radeon', 'amdgpu']:
                    hwmons.append(os.path.join(hwmonprefix, mon))
    return hwmons


def getHwmonFromDevice(device):
    """ Return the corresponding HW Monitor for a specified GPU device.

    Parameters:
    device -- Device to return the corresponding HW Monitor
    """
    drmdev = os.path.realpath(os.path.join(drmprefix, device, 'device'))
    for hwmon in listAmdHwMons():
        if os.path.realpath(os.path.join(hwmon, 'device')) == drmdev:
            return hwmon
    return None


def getFanSpeed(device):
    """ Return the fan speed (%) for a specified device.

    Parameters:
    device -- Device to return the current fan speed
    """

    fanLevel = getSysfsValue(device, 'fan')
    fanMax = getSysfsValue(device, 'fanmax')
    if not fanLevel or not fanMax:
        return 0
    return round((float(fanLevel) / float(fanMax)) * 100, 2)


def getCurrentClock(device, clock, type):
    """ Return the current clock frequency for a specified device.

    Parameters:
    device -- Device to return the clock frequency
    clock -- [gpu|mem] Return either the GPU (gpu) or GPU Memory (mem) clock frequency
    type -- [freq|level] Return either the clock frequency (freq) or clock level (level)
    """
    currClk = ''

    currClocks = getSysfsValue(device, 'sclk')
    if clock == 'mem':
        currClocks = getSysfsValue(device, 'mclk')

    if not currClocks:
        return None
    # Since the current clock line is of the format 'X: #Mhz *', we either want the
    # first character for level, or the 3rd-to-2nd-last characters for speed
    for line in currClocks.splitlines():
        if re.match(r'.*\*$', line):
            if (type == 'freq'):
                currClk = line[3:-2]
            else:
                currClk = line[0]
            break
    return currClk


def getMaxLevel(device, type):
    """ Return the maximum level for a specified device.

    Parameters:
    device -- Device to return the maximum level
    type -- [gpu|mem|profile] Return either the maximum GPU (gpu) or GPU Memory (mem) level, or the highest numbered Power Profiles
    """
    global RETCODE
    if type not in ['gpu', 'mem', 'profile']:
        printLog(device, 'Invalid level type ' + type)
        RETCODE = 1
        return ''

    key = 'sclk'
    if type == 'mem':
        key = 'mclk'
    elif type == 'profile':
        key = 'profile'

    levels = getSysfsValue(device, key)
    if not levels:
        return 0
    # lstrip since there are leading spaces for this sysfs file, but no others
    if type == 'profile':
        levels = levels.splitlines()[-1].lstrip(' ')
    return int(levels.splitlines()[-1][0])


def findFile(prefix, file, device):
    """ Return the full file path given prefix

    Parameters:
    prefix -- directory that contains the file
    file -- name of the file
    """
    pcPaths = glob.glob(os.path.join(prefix, '*', file))
    for path in pcPaths:
        if isCorrectPowerDevice(os.path.dirname(path), device):
            return path
    return ''


def setPerfLevel(device, level):
    """ Set the PowerPlay Performance Level for a specified device.

    Parameters:
    device -- Device to modify the current PowerPlay Performance Level
    level -- PowerPlay Performance Level to set
    """
    global RETCODE
    validLevels = ['auto', 'low', 'high', 'manual']
    perfPath = os.path.join(drmprefix, device, 'device', 'power_dpm_force_performance_level')

    if level not in validLevels:
        print(device, 'Invalid Performance level:' + level)
        RETCODE = 1
        return False
    if not os.path.isfile(perfPath):
        return False
    writeToSysfs(perfPath, level)
    return True


def showId(deviceList):
    """ Display the device ID for a list of devices.

    Parameters:
    deviceList -- List of devices to return the device ID (can be a single-item list)
    """
    print(logSpacer)
    for device in deviceList:
        printLog(device, 'GPU ID: 0x' + getSysfsValue(device, 'id'))
    print(logSpacer)


def showVbiosVersion(deviceList):
    """ Display the VBIOS version for a list of devices.

    Parameters:
    deviceList -- List of devices to return the VBIOS version (can be a single-item list)
    """
    print(logSpacer)
    for device in deviceList:
        vbios = getSysfsValue(device, 'vbios')
        if vbios:
            printLog(device, 'VBIOS version: ' + vbios)
        else:
            printLog(device, 'Cannot get VBIOS version')
    print(logSpacer)


def showCurrentGpuClocks(deviceList):
    """ Display the current GPU clock frequencies for a list of devices.

    Parameters:
    deviceList -- List of devices to return the current clock frequencies (can be a single-item list)
    """
    global RETCODE
    print(logSpacer)
    for device in deviceList:
        if not isPowerplayEnabled(device):
            printLog(device, 'PowerPlay not enabled - Cannot display GPU clocks')
            continue
        gpuclk = getCurrentClock(device, 'gpu', 'freq')
        gpulevel = getCurrentClock(device, 'gpu', 'level')

        if gpuclk == '':
            printLog(device, 'Unable to determine current GPU clocks. Check dmesg or GPU temperature')
            RETCODE = 1
            return

        printLog(device, 'GPU Clock Level: ' + str(gpulevel) + ' (' + str(gpuclk) + ')')
    print(logSpacer)


def showCurrentClocks(deviceList):
    """ Display the current GPU and GPU Memory clock frequencies for a list of devices.

    Parameters:
    deviceList -- List of devices to return the current clock frequencies (can be a single-item list)
    """
    global RETCODE
    print(logSpacer)
    for device in deviceList:
        if not isPowerplayEnabled(device):
            printLog(device, 'PowerPlay not enabled - Cannot display clocks')
            continue
        gpuclk = getCurrentClock(device, 'gpu', 'freq')
        gpulevel = getCurrentClock(device, 'gpu', 'level')
        memclk = getCurrentClock(device, 'mem', 'freq')
        memlevel = getCurrentClock(device, 'mem', 'level')

        if gpuclk == '':
            printLog(device, 'Unable to determine current clocks. Check dmesg or GPU temperature')
            RETCODE = 1
            return

        printLog(device, 'GPU Clock Level: ' + str(gpulevel) + ' (' + str(gpuclk) + ')')
        printLog(device, 'GPU Memory Clock Level: ' + str(memlevel) + ' (' + str(memclk) + ')')
    print(logSpacer)


def showCurrentTemps(deviceList):
    """ Display the current temperature for a list of devices.

    Parameters:
    deviceList -- List of devices to return the current temperature (can be a single-item list)
    """
    tempfiles = []

    print(logSpacer)
    for device in deviceList:
        temp = getSysfsValue(device, 'temp')
        if not temp:
            printLog(device, 'Unable to display temperature')
            continue
        printLog(device, 'Temperature: ' + str(temp) + 'c')
    print(logSpacer)


def showCurrentFans(deviceList):
    """ Display the current fan speed for a list of devices.

    Parameters:
    deviceList -- List of devices to return the current fan speed (can be a single-item list)
    """
    print(logSpacer)
    for device in deviceList:
        fanlevel = getSysfsValue(device, 'fan')
        fanspeed = getFanSpeed(device)
        if not fanspeed or not fanlevel:
            printLog(device, 'Unable to determine current fan speed')
            continue
        printLog(device, 'Fan Level: ' + str(fanlevel) + ' (' + str(fanspeed) + ')%')
    print(logSpacer)


def showClocks(deviceList):
    """ Display current GPU and GPU Memory clock frequencies for a list of devices.

    Parameters:
    deviceList -- List of devices to display current clock frequencies (can be a single-item list)
    """
    print(logSpacer)
    for device in deviceList:
        devpath = os.path.join(drmprefix, device, 'device')
        sclkPath = os.path.join(devpath, 'pp_dpm_sclk')
        mclkPath = os.path.join(devpath, 'pp_dpm_mclk')
        if not isPowerplayEnabled(device):
            printLog(device, 'PowerPlay not enabled - Cannot display clocks')
            continue
        if not os.path.isfile(sclkPath) or not os.path.isfile(mclkPath):
            continue
        with open(sclkPath, 'r') as sclk:
            sclkLog = 'Supported GPU clock frequencies on GPU' + parseDeviceName(device) + '\n' + sclk.read()
        with open(mclkPath, 'r') as mclk:
            mclkLog = 'Supported GPU Memory clock frequencies on GPU' + parseDeviceName(device) + '\n' + mclk.read()
        for line in sclkLog.split('\n'):
            printLog(device, line)
        for line in mclkLog.split('\n'):
            printLog(device, line)
    print(logSpacer)


def showPerformanceLevel(deviceList):
    """ Display current PowerPlay Performance Level for a list of devices.

    Parameters:
    deviceList -- List of devices to display current PowerPlay Performance Level (can be a single-item list)
    """
    print(logSpacer)
    for device in deviceList:
        level = getSysfsValue(device, 'perf')
        if not level:
            printLog(device, 'Cannot get Performance Level: Performance Level not supported')
        else:
            printLog(device, 'Current PowerPlay Level: ' + level)
    print(logSpacer)


def showOverDrive(deviceList, type):
    """ Display current OverDrive level for a list of devices.

    Parameters:
    deviceList -- List of devices to display current OverDrive values (can be a single-item list)
    type -- Which OverDrive to display (gpu|mem)
    """

    print(logSpacer)
    for device in deviceList:
        if type == 'gpu':
            od = getSysfsValue(device, 'sclk_od')
            odStr = 'GPU'
        elif type == 'mem':
            od = getSysfsValue(device, 'mclk_od')
            odStr = 'GPU Memory'
        if not od or int(od) < 0:
            printLog(device, 'Cannot get ' + odStr + ' OverDrive value: OverDrive not supported')
        else:
            printLog(device, 'Current ' + odStr + ' OverDrive value: ' + str(od) + '%')
    print(logSpacer)


def showProfile(deviceList):
    """ Display available Power Profiles for a list of devices.

    Parameters:
    deviceList -- List of devices to display available Power Profile attributes (can be a single-item list)
    """
    global RETCODE
    print(logSpacer)
    for device in deviceList:
        if not isPowerplayEnabled(device):
            printLog(device, 'PowerPlay not enabled - Power Profiles not supported')
            continue
        profile = getSysfsValue(device, 'profile')
        if not profile:
            printLog(device, 'Unable to get Power Profiles')
            continue
        if len(profile) > 1:
            printLog(device, '\n' + profile)
        else:
            printLog(device, 'Invalid return value from Power Profile SysFS file')
    print(logSpacer)


def showPower(deviceList):
    """ Display current Power Consumption for a list of devices.

    Parameters:
    deviceList -- List of devices to display current Power Consumption (can be a single-item list)
    """
    print(logSpacer)
    try:
        getPid("atitool")
        print('WARNING: Please terminate ATItool to use this functionality')
    except subprocess.CalledProcessError:
        for device in deviceList:
            power = getSysfsValue(device, 'power')
            if not power:
                printLog(device, 'Cannot get GPU power Consumption: Average GPU Power not supported')
            else:
                printLog(device, 'Average GPU Power: ' + power)
    print(logSpacer)


def showMemUsage(deviceList):
    """ Display current memoery Consumption for a list of devices.


    Parameters:
    deviceList -- List of devices to display current Power Consumption (can be a single-item list)
    """
    print(logSpacer)
    for device in deviceList:
        used_mem = getSysfsValue(device, 'used_mem')
        if not used_mem:
            printLog(
                device, 'Cannot get GPU memory usage: GPU used memory not supported')
        else:
            printLog(device, 'Average GPU used memory: ' +
                     str(used_mem) + "MB")
    print(logSpacer)


def showMemTotal(deviceList):
    """ Display current memoery Consumption for a list of devices.


    Parameters:
    deviceList -- List of devices to display current Power Consumption (can be a single-item list)
    """
    print(logSpacer)
    for device in deviceList:
        total_mem = getSysfsValue(device, 'total_mem')
        if not total_mem:
            printLog(
                device, 'Cannot get GPU total memory: GPU total memory not supported')
        else:
            printLog(device, 'GPU total memory: ' +
                     str(total_mem) + "MB")
    print(logSpacer)


def showAllConciseHw(deviceList):
    """ Display critical Hardware info for all devices in a concise format.

    Parameters:
    deviceList -- List of all devices
    """
    print(logSpacer)
    print(' GPU  DID    ECC        VBIOS')
    for device in deviceList:
        gpuid = getSysfsValue(device, 'id')

        # To support later
        ecc = 'N/A'

        vbios = getSysfsValue(device, 'vbios')

        print("  %-4s%-7s%-6s%-17s" % (device[4:], gpuid, ecc, vbios))


def showAllConcise(deviceList):
    """ Display critical info for all devices in a concise format.

    Parameters:
    deviceList -- List of all devices
    """
    print(logSpacer)
    print(' GPU  Temp    AvgPwr   SCLK     MCLK     UsedMem  TotalMem   Fan      Perf    SCLK OD    MCLK OD')
    for device in deviceList:

        temp = getSysfsValue(device, 'temp')
        if not temp:
            temp = 'N/A'
        else:
            temp = str(temp) + 'c'

        power = getSysfsValue(device, 'power')
        if not power:
            power = 'N/A'
        else:
            power = power[:-2] + 'W'

        sclk = getCurrentClock(device, 'gpu', 'freq')
        if not sclk:
            sclk = 'N/A'

        mclk = getCurrentClock(device, 'mem', 'freq')
        if not mclk:
            mclk = 'N/A'

        used_mem = getSysfsValue(device, 'used_mem')
        if not used_mem:
            used_mem = 'N/A'
        else:
            used_mem = str(used_mem) + 'MB'

        total_mem = getSysfsValue(device, 'total_mem')
        if not total_mem:
            total_mem = 'N/A'
        else:
            total_mem = str(total_mem) + 'MB'

        fan = str(getFanSpeed(device))
        if not fan:
            fan = 'N/A'
        else:
            fan = fan + '%'

        perf = getSysfsValue(device, 'perf')
        if not perf:
            perf = 'N/A'

        sclk_od = getSysfsValue(device, 'sclk_od')
        if not sclk_od or sclk_od == '-1':
            sclk_od = 'N/A'
        else:
            sclk_od = sclk_od + '%'

        mclk_od = getSysfsValue(device, 'mclk_od')
        if not mclk_od or mclk_od == '-1':
            mclk_od = 'N/A'
        else:
            mclk_od = mclk_od + '%'

        print("  %-4s%-8s%-9s%-9s%-9s%-9s%-11s%-9s%-10s%-11s%-9s" % (device[4:], temp,
                                                                    power, sclk, mclk, used_mem, total_mem, fan, perf, sclk_od, mclk_od))
    print(logSpacer)


def setPerformanceLevel(deviceList, level):
    """ Set the PowerPlay Performance Level for a list of devices.

    Parameters:
    deviceList -- List of devices to set the current PowerPlay Performance Level (can be a single-item list)
    level -- Specific PowerPlay Performance Level to set
    """
    print(logSpacer)
    for device in deviceList:
        if setPerfLevel(device, level):
            printLog(device, 'Successfully set current PowerPlay Level to ' + level)
        else:
            printLog(device, 'Unable to set current PowerPlay Level to ' + level)
    print(logSpacer)


def setClocks(deviceList, clktype, clk):
    """ Set clock frequency level for a list of devices.

    Parameters:
    deviceList -- List of devices to set the clock frequency (can be a single-item list)
    clktype -- [gpu|mem] Set the GPU (gpu) or GPU Memory (mem) clock frequency level
    clk -- Clock frequency level to set
    """
    global RETCODE
    if not clk:
        print('Invalid clock frequency')
        RETCODE = 1
        return
    value = ''.join(map(str, clk))
    try:
        int(value)
    except ValueError:
        print('Cannot set Clock level to value', value, ', non-integer characters are present!')
        RETCODE = 1
        return
    for device in deviceList:
        if not isPowerplayEnabled(device):
            printLog(device, 'PowerPlay not enabled - Cannot set clocks')
            RETCODE = 1
            continue
        devpath = os.path.join(drmprefix, device, 'device')
        if clktype == 'gpu':
            clkFile = os.path.join(devpath, 'pp_dpm_sclk')
        else:
            clkFile = os.path.join(devpath, 'pp_dpm_mclk')

        # GPU clocks can be set to multiple levels at the same time (of the format
        # 4 5 6 for levels 4, 5 and 6). Don't compare against the max level for gpu
        # clocks in this case
        if any(int(item) > getMaxLevel(device, clktype) for item in clk):
            printLog(device, 'Unable to set clock to unsupported Level - Max Level is ' + str(getMaxLevel(device, clktype)))
            RETCODE = 1
            continue
        setPerfLevel(device, 'manual')
        if writeToSysfs(clkFile, value):
            if clktype == 'gpu':
                printLog(device, 'Successfully set GPU Clock frequency mask to Level ' + value)
            else:
                printLog(device, 'Successfully set GPU Memory Clock frequency mask to Level ' + value)
        else:
            printLog(device, 'Unable to set ' + clktype + ' clock to Level ' + value)
            RETCODE = 1


def setClockOverDrive(deviceList, clktype, value, autoRespond):
    """ Set clock speed to OverDrive for a list of devices

    Parameters:
    deviceList -- List of devices to set to OverDrive
    type -- Clock type to set to OverDrive (currently only GPU and GPU Memory supported)
    value -- Percentage amount to set for OverDrive (0-20)
    autoRespond -- Response to automatically provide for all prompts
    """
    global RETCODE
    try:
        int(value)
    except ValueError:
        print('Cannot set OverDrive to value', value, ', it is not an integer!')
        RETCODE = 1
        return
    confirmOverDrive(autoRespond)

    for device in deviceList:
        if not isPowerplayEnabled(device):
            printLog(device, 'PowerPlay not enabled - Cannot set OverDrive')
            continue
        devpath = os.path.join(drmprefix, device, 'device')
        if clktype == 'gpu':
            odPath = os.path.join(devpath, 'pp_sclk_od')
            odStr = 'GPU'
        elif clktype == 'mem':
            odPath = os.path.join(devpath, 'pp_mclk_od')
            odStr = 'GPU Memory'
        else:
            printLog(device, 'Unsupported clock type ' + clktype + ' - Cannot set OverDrive')
            RETCODE = 1
            continue

        if int(value) < 0:
            printLog(device, 'Unable to set OverDrive less than 0%')
            RETCODE = 1
            return

        if int(value) > 20:
            printLog(device, 'Unable to set OverDrive greater than 20%. Changing to 20')
            value = '20'

        if (writeToSysfs(odPath, value)):
            printLog(device, 'Successfully set ' + odStr + ' OverDrive to ' + value + '%')
            setClocks([device], clktype, [getMaxLevel(device, clktype)])
        else:
            printLog(device, 'Unable to set OverDrive to ' + value + '%')


def resetFans(deviceList):
    """ Reset fans to driver control for a list of devices.

    Parameters:
    deviceList -- List of devices to set the fan speed (can be a single-item list)
    """
    for device in deviceList:
        if not isPowerplayEnabled(device):
            printLog(device, 'PowerPlay not enabled - Cannot reset fan speed')
            continue
        hwmon = getHwmonFromDevice(device)
        if not hwmon:
            printLog(device, 'No corresponding HW Monitor found')
            continue
        fanpath = os.path.join(hwmon, 'pwm1_enable')
        if writeToSysfs(fanpath, '2'):
            printLog(device, 'Successfully reset fan speed to driver control')
        else:
            printLog(device, 'Unable to reset fan speed to driver control')


def setFanSpeed(deviceList, fan):
    """ Set fan speed for a list of devices.

    Parameters:
    deviceList -- List of devices to set the fan speed (can be a single-item list)
    level -- Fan speed level to set (0-255)
    """
    global RETCODE
    for device in deviceList:
        if not isPowerplayEnabled(device):
            printLog(device, 'PowerPlay not enabled - Cannot set fan speed')
            RETCODE = 1
            continue
        hwmon = getHwmonFromDevice(device)
        if not hwmon:
            printLog(device, 'No corresponding HW Monitor found')
            RETCODE = 1
            continue
        fanpath = os.path.join(hwmon, 'pwm1')
        modepath = os.path.join(hwmon, 'pwm1_enable')
        maxfan = getSysfsValue(device, 'fanmax')
        if not maxfan:
            printLog(device, 'Cannot get max fan speed')
            RETCODE = 1
            continue
        if fan.endswith('%'):
            fanpct = int(fan[:-1])
            if fanpct > 100:
                printLog(device, 'Invalid fan value ' + fan)
                RETCODE = 1
                continue
            fan = str(int((fanpct * int(maxfan)) / 100))
        if int(fan) > int(maxfan):
            printLog(device, 'Unable to set fan speed to ' + fan + ' : Max Level = ' + maxfan)
            RETCODE = 1
            continue
        if getSysfsValue(device, 'fanmode') != '1':
            if writeToSysfs(modepath, '1'):
                printLog(device, 'Successfully set fan control to \'manual\'')
            else:
                printLog(device, 'Unable to set fan control to \'manual\'')
                continue
        if writeToSysfs(fanpath, str(fan)):
            printLog(device, 'Successfully set fan speed to Level ' + str(fan))
        else:
            printLog(device, 'Unable to set fan speed to Level ' + str(fan))


def setProfile(deviceList, profile):
    """ Set CUSTOM Power Profile values for a list of devices.

    Parameters:
    deviceList -- List of devices to specify the CUSTOM Power Profile for (can be a single-item list)
    profile -- The profile to set
    """
    for device in deviceList:
        if writeProfileSysfs(device, profile):
            printLog(device, 'Successfully set CUSTOM Power Profile values')
        else:
            printLog(device, 'Unable to set CUSTOM Power Profile values')


def resetProfile(deviceList):
    """ Reset profile for a list of a devices.

    Parameters:
    deviceList -- List of devices to reset the CUSTOM Power Profile for (can be a single-item list)
    """
    for device in deviceList:
        if not getSysfsValue(device, 'profile'):
            printLog(device, 'Unable to get current Power Profile')
            continue
        # Performance level must be set to manual for a reset of the profile to work
        setPerfLevel(device, 'manual')
        if not writeProfileSysfs(device, '0 ' * getNumProfileArgs(device)):
            printLog(device, 'Unable to reset CUSTOM Power Profile values')
        if writeProfileSysfs(device, '0'):
            printLog(device, 'Successfully reset Power Profile')
        else:
            printLog(device, 'Unable to reset Power Profile')
        setPerfLevel(device, 'auto')


def resetOverDrive(deviceList):
    """ Reset OverDrive to 0 if needed. We check first as setting OD requires sudo

    Parameters:
    deviceList -- List of devices to reset OverDrive (can be a single-item list)
    """
    for device in deviceList:
        devpath = os.path.join(drmprefix, device, 'device')
        odpath = os.path.join(devpath, 'pp_sclk_od')
        if not os.path.isfile(odpath):
            printLog(device, 'Unable to reset OverDrive; OverDrive not available')
            continue
        od = getSysfsValue(device, 'sclk_od')
        if not od or int(od) != 0:
            if not writeToSysfs(odpath, '0'):
                printLog(device, 'Unable to reset OverDrive')
                continue
        printLog(device, 'OverDrive set to 0')


def resetClocks(deviceList):
    """ Reset clocks to default

    Reset sclk and mclk to default values by setting performance level to auto, as well
    as setting OverDrive back to 0


    Parameters:
    deviceList -- List of devices to reset clocks (can be a single-item list)
    """
    for device in deviceList:
        resetOverDrive([device])
        setPerfLevel(device, 'auto')

        od = getSysfsValue(device, 'sclk_od')
        perf = getSysfsValue(device, 'perf')

        if not perf or not od or perf != 'auto' or od != '0':
            printLog(device, 'Unable to reset clocks')
        else:
            printLog(device, 'Successfully reset clocks')


def load(savefilepath, autoRespond):
    """ Load clock frequencies and fan speeds from a specified file.

    Parameters:
    savefilepath -- Path to a file with saved clock frequencies and fan speeds
    autoRespond -- Response to automatically provide for all prompts
    """
    gpuClocks = {}
    memClocks = {}
    fanSpeeds = {}
    profiles = {}

    if not os.path.isfile(savefilepath):
        print('No settings file found at ', savefilepath)
        sys.exit()
    with open(savefilepath, 'r') as savefile:
        jsonData = json.loads(savefile.read())
        for (device, values) in jsonData.items():
            if values['vJson'] != JSON_VERSION:
                print('Unable to load legacy clock file - file v' + str(values['vJson']) +
                      ' != current v' + str(JSON_VERSION))
                break
            setFanSpeed([device], values['fan'])
            setClockOverDrive([device], 'gpu', values['overdrivegpu'], autoRespond)
            setClockOverDrive([device], 'mem', values['overdrivegpumem'], autoRespond)
            setClocks([device], 'gpu', values['gpu'])
            setClocks([device], 'mem', values['mem'])
            #TODO: Get this working with the new Power Profile
            #setProfile([device], values['profile'].split())

            # Set Perf level last, since setting OverDrive sets the Performance level
            # it to manual, and Profiles only work when the Performance level is auto
            setPerfLevel(device, values['perflevel'])

            printLog(device, 'Successfully loaded values from ' + savefilepath)


def save(deviceList, savefilepath):
    """ Save clock frequencies and fan speeds for a list of devices to a specified file path.

    Parameters:
    deviceList -- List of devices to save the clock frequencies and fan speeds
    savefilepath -- Location to save the clock frequencies and fan speeds
    """
    perfLevels = {}
    gpuClocks = {}
    memClocks = {}
    fanSpeeds = {}
    overDriveGpu = {}
    overDriveGpuMem = {}
    profiles = {}
    jsonData = {}

    if os.path.isfile(savefilepath):
        print(savefilepath, 'already exists. Settings not saved')
        sys.exit()
    for device in deviceList:
        if not isPowerplayEnabled(device):
            printLog(device, 'PowerPlay not enabled - Cannot save clocks')
            continue
        perfLevels[device] = getSysfsValue(device, 'perf')
        gpuClocks[device] = getCurrentClock(device, 'gpu', 'level')
        memClocks[device] = getCurrentClock(device, 'mem', 'level')
        fanSpeeds[device] = getSysfsValue(device, 'fan')
        overDriveGpu[device] = getSysfsValue(device, 'sclk_od')
        overDriveGpuMem[device] = getSysfsValue(device, 'mclk_od')
        #TODO: Get this working with the new Power Profile stuff
        #profiles[device] = getSysfsValue(device, 'profile')
        profiles[device] = "0"
        jsonData[device] = {'vJson': JSON_VERSION, 'gpu': gpuClocks[device], 'mem': memClocks[device], 'fan': fanSpeeds[device], 'overdrivegpu': overDriveGpu[device], 'overdrivegpumem': overDriveGpuMem[device], 'profile': profiles[device], 'perflevel': perfLevels[device]}
        printLog(device, 'Current settings successfully saved to ' + savefilepath)
    with open(savefilepath, 'w') as savefile:
        json.dump(jsonData, savefile, ensure_ascii=True)

# Below is for when called as a script instead of when imported as a module
if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='AMD ROCm System Management Interface',
                                     formatter_class=lambda prog: argparse.HelpFormatter(prog, max_help_position=90, width=110))
    groupDev = parser.add_argument_group()
    groupDisplay = parser.add_argument_group()
    groupAction = parser.add_argument_group()
    groupFile = parser.add_mutually_exclusive_group()
    groupResponse = parser.add_argument_group()

    groupDev.add_argument('-d', '--device', help='Execute command on specified device', type=int)
    groupDisplay.add_argument('-i', '--showid', help='Show GPU ID', action='store_true')
    groupDisplay.add_argument('-v', '--showvbios', help='Show VBIOS version', action='store_true')
    groupDisplay.add_argument('--showhw', help='Show Hardware details', action='store_true')
    groupDisplay.add_argument('-t', '--showtemp', help='Show current temperature', action='store_true')
    groupDisplay.add_argument('-c', '--showclocks', help='Show current clock frequencies', action='store_true')
    groupDisplay.add_argument('-g', '--showgpuclocks', help='Show current GPU clock frequencies', action='store_true')
    groupDisplay.add_argument('-f', '--showfan', help='Show current fan speed', action='store_true')
    groupDisplay.add_argument('-p', '--showperflevel', help='Show current PowerPlay Performance Level', action='store_true')
    groupDisplay.add_argument('-P', '--showpower', help='Show current power consumption', action='store_true')
    groupDisplay.add_argument('-o', '--showoverdrive', help='Show current GPU Clock OverDrive level', action='store_true')
    groupDisplay.add_argument('-m', '--showmemoverdrive', help='Show current GPU Memory Clock OverDrive level', action='store_true')
    groupDisplay.add_argument('-mu', '--showmemusage', help='Show current GPU Memory usage', action='store_true')
    groupDisplay.add_argument('-mt', '--showmemtotal', help='Show total GPU Memory', action='store_true')
    groupDisplay.add_argument('-l', '--showprofile', help='Show Compute Profile attributes', action='store_true')
    groupDisplay.add_argument('-s', '--showclkfrq', help='Show supported GPU and Memory Clock', action='store_true')
    groupDisplay.add_argument('-a' ,'--showallinfo', help='Show Temperature, Fan and Clock values', action='store_true')

    groupAction.add_argument('-r', '--resetclocks', help='Reset sclk and mclk to default', action='store_true')
    groupAction.add_argument('--setsclk', help='Set GPU Clock Frequency Level(s) (requires manual Perf level)', type=int, metavar='LEVEL', nargs='+')
    groupAction.add_argument('--setmclk', help='Set GPU Memory Clock Frequency Level(s) (requires manual Perf level)', type=int, metavar='LEVEL', nargs='+')
    groupAction.add_argument('--resetfans', help='Reset fans to automatic (driver) control', action='store_true')
    groupAction.add_argument('--setfan', help='Set GPU Fan Speed (Level or %%)', metavar='LEVEL')
    groupAction.add_argument('--setperflevel', help='Set PowerPlay Performance Level', metavar='LEVEL')
    groupAction.add_argument('--setoverdrive', help='Set GPU OverDrive level (requires manual|high Perf level)', metavar='%')
    groupAction.add_argument('--setmemoverdrive', help='Set GPU Memory Overclock OverDrive level (requires manual|high Perf level)', metavar='%')
    groupAction.add_argument('--setprofile', help='Specify Power Profile level (#) or a quoted string of CUSTOM Profile attributes "# # # #..." (requires manual Perf level)')
    groupAction.add_argument('--resetprofile', help='Reset Power Profile back to default', action='store_true')

    groupFile.add_argument('--load', help='Load Clock, Fan, Performance and Profile settings from FILE', metavar='FILE')
    groupFile.add_argument('--save', help='Save Clock, Fan, Performance and Profile settings to FILE', metavar='FILE')

    groupResponse.add_argument('--autorespond', help='Response to automatically provide for all prompts (NOT RECOMMENDED)', metavar='RESPONSE')

    args = parser.parse_args()

    # If there is a single device specified, use that for all commands, otherwise use a
    # list of all available devices. Also use "is not None" as device 0 would
    # have args.device=0, and "if 0" returns false.
    if args.device is not None:
        device = parseDeviceNumber(args.device)
        if not doesDeviceExist(device):
            print('No such device ' + device)
            sys.exit()
        deviceList = [device]
    else:
        deviceList = listDevices()

    if args.showallinfo:
        args.showid = True
        args.showtemp = True
        args.showclocks = True
        args.showfan = True
        args.list = True
        args.showclkfrq = True
        args.showperflevel = True
        args.showoverdrive = True
        args.showprofile = True
        args.showpower = True

    if args.setsclk or args.setmclk or args.resetfans or args.setfan or args.setperflevel or args.load or \
       args.resetclocks or args.setprofile or args.resetprofile or args.setoverdrive or args.showpower or \
       len(sys.argv) == 1:
        relaunchAsSudo()

    # Header for the SMI
    print('\n\n', headerSpacer, '    ROCm System Management Interface    ', headerSpacer, sep='')

    if len(sys.argv) == 1:
        showAllConcise(deviceList)
    if args.showhw:
        showAllConciseHw(deviceList)
    if args.showid:
        showId(deviceList)
    if args.showvbios:
        showVbiosVersion(deviceList)
    if args.resetclocks:
        resetClocks(deviceList)
    if args.showtemp:
        showCurrentTemps(deviceList)
    if args.showclocks:
        showCurrentClocks(deviceList)
    if args.showgpuclocks:
        showCurrentGpuClocks(deviceList)
    if args.showfan:
        showCurrentFans(deviceList)
    if args.showperflevel:
        showPerformanceLevel(deviceList)
    if args.showoverdrive:
        showOverDrive(deviceList, 'gpu')
    if args.showmemoverdrive:
        showOverDrive(deviceList, 'mem')
    if args.showmemusage:
        showMemUsage(deviceList)
    if args.showmemtotal:
        showMemTotal(deviceList)
    if args.showprofile:
        showProfile(deviceList)
    if args.showpower:
        showPower(deviceList)
    if args.showclkfrq:
        showClocks(deviceList)
    if args.setsclk:
        setClocks(deviceList, 'gpu', args.setsclk)
    if args.setmclk:
        setClocks(deviceList, 'mem', args.setmclk)
    if args.resetfans:
        resetFans(deviceList)
    if args.setfan:
        setFanSpeed(deviceList, args.setfan)
    if args.setperflevel:
        setPerformanceLevel(deviceList, args.setperflevel)
    if args.setoverdrive:
        setClockOverDrive(deviceList, 'gpu', args.setoverdrive, args.autorespond)
    if args.setmemoverdrive:
        setClockOverDrive(deviceList, 'mem', args.setmemoverdrive, args.autorespond)
    if args.setprofile:
        setProfile(deviceList, args.setprofile)
    if args.resetprofile:
        resetProfile(deviceList)
    if args.load:
        load(args.load, args.autorespond)
    if args.save:
        save(deviceList, args.save)

    # If RETCODE isn't 0, inform the user
    if RETCODE:
        print('WARNING: One or more commands failed')

    # Footer for the SMI
    print(headerSpacer, '           End of ROCm SMI Log          ', headerSpacer, '\n', sep='')
    sys.exit(RETCODE)
