#   Copyright (C) 2013 Canonical Ltd.
#
#   Author: Scott Moser <scott.moser@canonical.com>
#
#   Curtin is free software: you can redistribute it and/or modify it under
#   the terms of the GNU Affero General Public License as published by the
#   Free Software Foundation, either version 3 of the License, or (at your
#   option) any later version.
#
#   Curtin is distributed in the hope that it will be useful, but WITHOUT ANY
#   WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
#   FOR A PARTICULAR PURPOSE.  See the GNU Affero General Public License for
#   more details.
#
#   You should have received a copy of the GNU Affero General Public License
#   along with Curtin.  If not, see <http://www.gnu.org/licenses/>.

import errno
import os
import stat
import shlex
import tempfile

from curtin import util
from curtin.log import LOG


def get_dev_name_entry(devname):
    bname = devname.split('/dev/')[-1]
    return (bname, "/dev/" + bname)


def is_valid_device(devname):
    devent = get_dev_name_entry(devname)[1]
    try:
        return stat.S_ISBLK(os.stat(devent).st_mode)
    except OSError as e:
        if e.errno != errno.ENOENT:
            raise
    return False


def _lsblock_pairs_to_dict(lines, key="NAME", filter_func=None):
    ret = {}
    for line in lines.splitlines():
        toks = shlex.split(line)
        cur = {}
        for tok in toks:
            k, v = tok.split("=", 1)
            cur[k] = v
        cur['device_path'] = get_dev_name_entry(cur['NAME'])[1]
        if not filter_func or filter_func(cur):
            ret[cur[key]] = cur
    return ret


def _lsblock(args=None, filter_func=None):
    # lsblk  --help | sed -n '/Available/,/^$/p' |
    #     sed -e 1d -e '$d' -e 's,^[ ]\+,,' -e 's, .*,,' | sort
    keys = ['ALIGNMENT', 'DISC-ALN', 'DISC-GRAN', 'DISC-MAX', 'DISC-ZERO',
            'FSTYPE', 'GROUP', 'KNAME', 'LABEL', 'LOG-SEC', 'MAJ:MIN',
            'MIN-IO', 'MODE', 'MODEL', 'MOUNTPOINT', 'NAME', 'OPT-IO', 'OWNER',
            'PHY-SEC', 'RM', 'RO', 'ROTA', 'RQ-SIZE', 'SCHED', 'SIZE', 'STATE',
            'TYPE']
    if args is None:
        args = []
    args = [x.replace('!', '/') for x in args]

    # in order to avoid a very odd error with '-o' and all output fields above
    # we just drop one.  doesn't really matter which one.
    keys.remove('SCHED')
    basecmd = ['lsblk', '--noheadings', '--bytes', '--pairs',
               '--output=' + ','.join(keys)]
    (out, _err) = util.subp(basecmd + list(args), capture=True)
    out = out.replace('!', '/')
    return _lsblock_pairs_to_dict(out, filter_func=filter_func)


def get_unused_blockdev_info():
    # return a list of unused block devices. These are devices that
    # do not have anything mounted on them.

    # get a list of top level block devices, then iterate over it to get
    # devices dependent on those.  If the lsblk call for that specific
    # call has nothing 'MOUNTED", then this is an unused block device
    bdinfo = _lsblock(['--nodeps'])
    unused = {}
    for devname, data in bdinfo.items():
        cur = _lsblock([data['device_path']])
        mountpoints = [x for x in cur if cur[x].get('MOUNTPOINT')]
        if len(mountpoints) == 0:
            unused[devname] = data
    return unused


def get_devices_for_mp(mountpoint):
    # return a list of devices (full paths) used by the provided mountpoint
    bdinfo = _lsblock()
    found = set()
    for devname, data in bdinfo.items():
        if data['MOUNTPOINT'] == mountpoint:
            found.add(data['device_path'])

    if found:
        return list(found)

    # for some reason, on some systems, lsblk does not list mountpoint
    # for devices that are mounted.  This happens on /dev/vdc1 during a run
    # using tools/launch.
    with open("/proc/mounts", "r") as fp:
        for line in fp:
            try:
                (dev, mp, vfs, opts, freq, passno) = line.split(None, 5)
                if mp == mountpoint:
                    return [os.path.realpath(dev)]
            except ValueError:
                continue
    return []


def get_installable_blockdevs(include_removable=False, min_size=1024**3):
    good = []
    unused = get_unused_blockdev_info()
    for devname, data in unused.items():
        if not include_removable and data.get('RM') == "1":
            continue
        if data.get('RO') != "0" or data.get('TYPE') != "disk":
            continue
        if min_size is not None and int(data.get('SIZE', '0')) < min_size:
            continue
        good.append(devname)
    return good


def get_blockdev_for_partition(devpath):
    # convert an entry in /dev/ to parent disk and partition number
    # if devpath is a block device and not a partition, return (devpath, None)

    # input of /dev/vdb or /dev/disk/by-label/foo
    # rpath is hopefully a real-ish path in /dev (vda, sdb..)
    rpath = os.path.realpath(devpath)

    bname = os.path.basename(rpath)
    syspath = "/sys/class/block/%s" % bname

    if not os.path.exists(syspath):
        syspath2 = "/sys/class/block/cciss!%s" % bname
        if not os.path.exists(syspath2):
            raise ValueError("%s had no syspath (%s)" % (devpath, syspath))
        syspath = syspath2

    ptpath = os.path.join(syspath, "partition")
    if not os.path.exists(ptpath):
        return (rpath, None)

    ptnum = util.load_file(ptpath).rstrip()

    # for a partition, real syspath is something like:
    # /sys/devices/pci0000:00/0000:00:04.0/virtio1/block/vda/vda1
    rsyspath = os.path.realpath(syspath)
    disksyspath = os.path.dirname(rsyspath)

    diskmajmin = util.load_file(os.path.join(disksyspath, "dev")).rstrip()
    diskdevpath = os.path.realpath("/dev/block/%s" % diskmajmin)

    # diskdevpath has something like 253:0
    # and udev has put links in /dev/block/253:0 to the device name in /dev/
    return (diskdevpath, ptnum)


def get_pardevs_on_blockdevs(devs):
    # return a dict of partitions with their info that are on provided devs
    if devs is None:
        devs = []
    devs = [get_dev_name_entry(d)[1] for d in devs]
    found = _lsblock(devs)
    ret = {}
    for short in found:
        if found[short]['device_path'] not in devs:
            ret[short] = found[short]
    return ret


def stop_all_unused_multipath_devices():
    """
    Stop all unused multipath devices.
    """
    multipath = util.which('multipath')

    # Command multipath is not available only when multipath-tools package
    # is not installed. Nothing needs to be done in this case because system
    # doesn't create multipath devices without this package installed and we
    # have nothing to stop.
    if not multipath:
        return

    # Command multipath -F flushes all unused multipath device maps
    cmd = [multipath, '-F']
    try:
        util.subp(cmd)
    except util.ProcessExecutionError as e:
        LOG.warn("Failed to stop multipath devices: %s", e)


def detect_multipath():
    """
    Figure out if target machine has any multipath devices.
    """
    # Multipath-tools are not available in the ephemeral image.
    # This workaround detects multipath by looking for multiple
    # devices with the same scsi id (serial number).
    disk_ids = []
    bdinfo = _lsblock(['--nodeps'])
    for devname, data in bdinfo.items():
        # Command scsi_id returns error while running against cdrom devices.
        # To prevent getting unexpected errors for some other types of devices
        # we ignore everything except hard disks.
        if data['TYPE'] != 'disk':
            continue

        cmd = ['/lib/udev/scsi_id', '--replace-whitespace',
               '--whitelisted', '--device=%s' % data['device_path']]
        try:
            (out, err) = util.subp(cmd, capture=True)
            scsi_id = out.rstrip('\n')
            # ignore empty ids because they are meaningless
            if scsi_id:
                disk_ids.append(scsi_id)
        except util.ProcessExecutionError as e:
            LOG.warn("Failed to get disk id: %s", e)

    duplicates_found = (len(disk_ids) != len(set(disk_ids)))
    return duplicates_found


def get_root_device(dev, fpath="curtin"):
    """
    Get root partition for specified device, based on presence of /curtin.
    """
    partitions = get_pardevs_on_blockdevs(dev)
    target = None
    tmp_mount = tempfile.mkdtemp()
    for i in partitions:
        dev_path = partitions[i]['device_path']
        mp = None
        try:
            util.do_mount(dev_path, tmp_mount)
            mp = tmp_mount
            curtin_dir = os.path.join(tmp_mount, fpath)
            if os.path.isdir(curtin_dir):
                target = dev_path
                break
        except:
            pass
        finally:
            if mp:
                util.do_umount(mp)

    os.rmdir(tmp_mount)

    if target is None:
        raise ValueError("Could not find root device")
    return target


def get_disk_serial(path):
    """
    Get serial number of the disk or empty string if it can't be fetched.
    """
    (out, _err) = util.subp(["udevadm", "info", "--query=property", path],
                            capture=True)
    for line in out.splitlines():
        if "ID_SERIAL_SHORT" in line:
            return line.split('=')[-1]
    return ''


def get_disk_busid(name):
    """
    Get bus address of the disk. This address uniquely identifies the device.
    """
    bus_path = os.readlink('/sys/block/%s' % name)
    # /sys/block/vda -> ../devices/pci0000:00/0000:00:03.0/virtio1/block/vda
    start = '../devices/'
    end = '/block/%s' % name
    if not bus_path.startswith(start) or not bus_path.endswith(end):
        raise ValueError("malformed bus path from sysfs (%s)" % bus_path)
    return bus_path[len(start):-len(end)]


def get_volume_uuid(path):
    """
    Get uuid of disk with given path. This address uniquely identifies
    the device and remains consistant across reboots
    """
    (out, _err) = util.subp(["blkid", "-o", "export", path], capture=True)
    for line in out.splitlines():
        if "UUID" in line:
            return line.split('=')[-1]
    return ''


def get_mountpoints():
    """
    Returns a list of all mountpoints where filesystems are currently mounted.
    """
    info = _lsblock(filter_func=None)
    return list(i.get("MOUNTPOINT") for name, i in info.items() if
                i.get("MOUNTPOINT") is not None and
                i.get("MOUNTPOINT") != "")


def _filter_disks(block_device):
    return (block_device['TYPE'] == 'disk')


def get_disk_info():
    """
    Get information about disks (not partitions) available in the system.
    Together with information provided by lsblk, this function fetches
    disk serial number and unique bus path.
    """
    disks = _lsblock(filter_func=_filter_disks)
    for name in disks:
        disks[name]['BUSID'] = get_disk_busid(name)
    for name in disks:
        disks[name]['SERIAL'] = get_disk_serial(disks[name]['device_path'])
    return disks


def lookup_disk(serial=None, busid=None):
    """
    Search for a disk by its serial number and/or bus id.
    """
    ret = None
    for name, info in get_disk_info().items():
        if serial and info['SERIAL'] != serial:
            continue
        if busid and info['BUSID'] != busid:
            continue
        ret = info
        break
    return ret

# vi: ts=4 expandtab syntax=python
