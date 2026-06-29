#!/usr/bin/env python
'''A simple Wi-Fi connection optimizer based on the geolocation.'''

import binascii
import logging
import os
from pathlib import Path
import re
import subprocess
import sys
from threading import Event
from time import sleep
from typing import NoReturn

import numpy as np
import pandas as pd
import sdbus
try:
    # Base class of EVERY error sdbus can raise: mapped NetworkManager errors,
    # generic D-Bus transport errors (NoReply/Timeout/Disconnected/AccessDenied
    # /UnknownMethod) and unmapped error names. NetworkManagerBaseError alone
    # does NOT cover the transport/unmapped ones, which are exactly what is
    # raised on an NM restart or a wedged device. The import path differs
    # between sdbus releases.
    from sdbus import SdBusBaseError
except ImportError:  # pragma: no cover - depends on sdbus version
    from sdbus.dbus_exceptions import SdBusBaseError
from sdbus import (
    DbusInterfaceCommon,
    dbus_method,
    dbus_property,
)
from sdbus_block.networkmanager import (
    AccessPoint,
    ActiveConnection,
    DeviceState,
    NetworkConnectionSettings,
    NetworkDeviceWireless,
    NetworkManager,
    NetworkManagerSettings,
)
from sdbus_block.networkmanager.settings import (
    ConnectionProfile,
    WirelessSettings,
)

logger = logging.getLogger('wifi_optimizer')
profile_pattern = re.compile(
    r'^(/[0-9a-zA-Z-]+)+/[1-9][0-9]*-kiss-enable-[0-9a-zA-Z]+.nmconnection$'
)


def _halt() -> NoReturn:
    Event().wait(timeout=None)
    return sys.exit(0)


def _get_system_uuid() -> str:
    outputs = subprocess.check_output(
        args=['dmidecode', '-s', 'system-uuid'],
        shell=False,
        stdin=None,
        stderr=sys.stderr,
    )
    return outputs.decode('utf-8').strip()


def _find_device(
    nm: NetworkManager,
) -> tuple[
    str,
    NetworkConnectionSettings,
    ConnectionProfile,
    str,
    NetworkDeviceWireless,
]:
    logger.info('Inspect the primary connection')
    primary_connection = ActiveConnection(nm.primary_connection)
    connection = NetworkConnectionSettings(primary_connection.connection)
    profile = connection.get_profile()
    if profile.connection.uuid is None or \
        profile.connection.interface_name != 'master' or \
            profile.connection.connection_type != 'bond':
        logger.warning('Unsupported network configuration; sleeping...')
        return _halt()
    primary_connection_uuid = profile.connection.uuid

    logger.info('List all wifi interfaces')
    settings = NetworkManagerSettings()
    connection_paths = list(settings.connections)
    connections = [
        NetworkConnectionSettings(path)
        for path in connection_paths
    ]
    print([
        connection.filename
        for connection in connections
        if profile_pattern.match(connection.filename)
    ])
    profiles = [
        connection.get_profile()
        for connection in connections
        if profile_pattern.match(connection.filename)
    ]
    wifi_indices = [
        index
        for index, profile in enumerate(profiles)
        if profile.connection.interface_name is not None
        if profile.connection.connection_type == '802-11-wireless'
        if profile.connection.slave_type == 'bond'
        if profile.connection.master == primary_connection_uuid
        if profile.wireless is not None
        if profile.wireless.mode == 'infrastructure'
        if profile.wireless.ssid is not None
    ]
    connection_paths = [
        connection_paths[index]
        for index in wifi_indices
    ]
    connections = [
        connections[index]
        for index in wifi_indices
    ]
    profiles = [
        profiles[index]
        for index in wifi_indices
    ]
    device_paths = [
        nm.get_device_by_ip_iface(profile.connection.interface_name)
        for profile in profiles
        # already checked on `wifi_indices`
        if profile.connection.interface_name is not None
    ]
    devices = [
        NetworkDeviceWireless(device_path)
        for device_path in device_paths
    ]
    logger.info('Found wifi interfaces: %s', repr([
        profile.connection.interface_name
        for profile in profiles
    ]))

    logger.info('Find a best wifi interface')
    selected_index = 0  # Use the first device
    try:
        connection_path = connection_paths[selected_index]
        connection = connections[selected_index]
        profile = profiles[selected_index]
        device_path = device_paths[selected_index]
        device = devices[selected_index]
        logger.info('Selected interface: %s', device.interface)
    except IndexError:
        logger.info('No available wifi interfaces; sleeping...')
        return _halt()
    return connection_path, connection, profile, device_path, device


def _find_bssids(
    device: NetworkDeviceWireless,
    ssid: bytes,
) -> tuple[list[str], dict[str, str]]:
    logger.debug('Rescan APs')
    device.request_scan(
        options={
            'ssids': ('aay', [ssid]),
        },
    )
    ap_paths = list(device.access_points)
    aps = [
        (path, AccessPoint(path))
        for path in ap_paths
    ]
    aps = [
        (path, ap)
        for path, ap in aps
        if ap.ssid == ssid
    ]
    max_bitrate = max(
        ap.max_bitrate
        for _, ap in aps
    )
    # Map the normalized BSSID to its AP object path so that activation
    # can pin the exact AP via `specific_object` (instead of letting
    # NetworkManager pick/generate one).
    bssid_to_ap_path = {
        ap.hw_address.upper(): path
        for path, ap in aps
    }
    bssids = [
        ap.hw_address
        for _, ap in aps
        if ap.max_bitrate == max_bitrate
    ]
    return bssids, bssid_to_ap_path


class _AccessPointSelector:
    def __init__(
        self,
        sources: pd.DataFrame,
        targets: pd.DataFrame,
    ) -> None:
        self._sources = sources
        self._targets = targets

        system_uuid = _get_system_uuid()
        index_source = self._sources['id'] == system_uuid
        if not index_source.any():  # type: ignore
            logger.warning('Unsupported node: %s', system_uuid)
            _halt()

        source = self._sources[index_source]
        logger.info('Node info\n%s', repr(source))
        self._x: float = source['x'].item()
        self._y: float = source['y'].item()

    def _fit_target(self, bssid: str) -> int | None:
        macs = [
            binascii.unhexlify(mac.replace(':', ''))  # type: ignore
            for mac in self._targets['id']  # type: ignore
        ]
        pattern = binascii.unhexlify(bssid.replace(':', ''))
        filtered = [
            index
            for index, mac in enumerate(macs)
            if pattern[1:-1] == mac[1:-1]
            if abs(pattern[0] - mac[0]) in [0, 8]
            if pattern[-1] - mac[-1] >= 0 and pattern[-1] - mac[-1] < 16
        ]
        if not filtered:
            return None
        if len(filtered) > 1:
            logger.warning(
                'Duplicated MAC addresses: %s; selecting the first one',
                repr(filtered),
            )
        return filtered[0]

    def find(self, bssids: list[str]) -> str | None:
        '''Find a best AP's BSSID.'''

        # Map to the target APs
        target_indices = [
            self._fit_target(bssid)
            for bssid in bssids
        ]
        targets = pd.DataFrame(self._targets.iloc[[
            index
            for index in target_indices
            if index is not None
        ]])
        targets = targets.reset_index(inplace=False, drop=True)

        # Concat the BSSIDs into the targets
        series_bssid = pd.Series([
            bssid
            for index, bssid in zip(target_indices, bssids)
            if index is not None
        ])
        targets.loc[:, ['bssid']] = series_bssid
        logger.debug('Available APs\n%s', repr(targets))

        # Find the nearest AP
        series_diff_x = (targets['x'] - self._x).abs()  # type: ignore
        series_diff_y = (targets['y'] - self._y).abs()  # type: ignore
        series_l2_dist = np.sqrt(
            series_diff_x ** 2 + series_diff_y ** 2  # type: ignore
        )
        if series_l2_dist.empty:
            return None
        nearest_ap_index = np.argmin(series_l2_dist).item()

        # Return the nearest AP's BSSID
        return targets['bssid'][nearest_ap_index]  # type: ignore


def _persist_profile(
    connection: NetworkConnectionSettings,
    profile: ConnectionProfile,
) -> None:
    '''Persist the profile to disk under its existing UUID.

    Writing with `save_to_disk=True` maps to Update2(flags=0x1 TO_DISK), which
    overwrites the single keyfile in place keeping the same UUID, never relies
    on volatile in-memory storage that NetworkManager can garbage-collect, and
    (because BLOCK_AUTOCONNECT is not set) also clears any autoconnect-blocked
    reason so a flapping link can re-arm itself.
    '''
    # Never echo back server-managed fields; sending `read-only`/a stale
    # `timestamp` back is at best ignored and at worst rejected/normalized.
    profile.connection.timestamp = None
    profile.connection.read_only = None
    connection.update_profile(profile, save_to_disk=True)


def _activate(
    nm: NetworkManager,
    connection_path: str,
    device_path: str,
    ap_path: str,
) -> None:
    '''Re-activate the EXISTING profile, pinned to the device and target AP.

    Passing the real connection path plus an explicit device and AP object path
    guarantees NetworkManager re-associates THIS profile (same UUID) to the
    chosen BSSID and never has to pick/generate a device or fork a new
    connection. We intentionally do NOT use Device.Reapply: for an 802-11
    bssid change Reapply may return success without actually re-associating to
    the new AP, which would silently defeat the optimizer.
    '''
    nm.activate_connection(
        connection=connection_path,
        device=device_path,
        specific_object=ap_path,
    )


def _recover_device(
    nm: NetworkManager,
    connection: NetworkConnectionSettings,
    profile: ConnectionProfile,
    connection_path: str,
    device_path: str,
    device: NetworkDeviceWireless,
) -> None:
    '''Bring the slave back up after a link drop without an NM restart.

    Re-arm autoconnect by persisting TO_DISK (no BLOCK_AUTOCONNECT clears the
    blocked reason and resets the retry counter), then re-activate the existing
    profile explicitly. Never call Device.Disconnect (it blocks autoconnect)
    and never touch the bond master.
    '''
    try:
        # The device path can go stale (e.g. the bond/device was torn down),
        # in which case reading the property raises a D-Bus error, not just a
        # ValueError. Never let that crash the daemon.
        state = DeviceState(device.state)
    except ValueError:
        return
    except SdBusBaseError as error:
        logger.warning('Failed to read device state: %s', error)
        return
    # Only recover from terminal "down" states; leave in-progress
    # transitions (PREPARE/CONFIG/IP_CONFIG/NEED_AUTH/...) alone so we do
    # not fight an activation that NetworkManager is already driving.
    if state not in (
        DeviceState.UNAVAILABLE,
        DeviceState.DISCONNECTED,
        DeviceState.FAILED,
    ):
        return

    logger.info('Recovering wifi device from state: %s', state)
    try:
        # The profile still carries the last selected bssid, so re-activating
        # it re-pins to that AP (specific_object='/' adds no extra constraint).
        _persist_profile(connection, profile)
        nm.activate_connection(
            connection=connection_path,
            device=device_path,
            specific_object='/',
        )
    except SdBusBaseError as error:
        logger.warning('Recovery activation failed: %s', error)


WPA_SERVICE_NAME = 'fi.w1.wpa_supplicant1'
WPA_OBJECT_PATH = '/fi/w1/wpa_supplicant1'


class _WpaSupplicant(
    DbusInterfaceCommon,
    interface_name='fi.w1.wpa_supplicant1',
):
    '''Minimal proxy for the wpa_supplicant root object.'''

    @dbus_method(
        input_signature='s',
        result_signature='o',
        method_name='GetInterface',
    )
    def get_interface(self, ifname: str) -> str:
        raise NotImplementedError


class _WpaInterface(
    DbusInterfaceCommon,
    interface_name='fi.w1.wpa_supplicant1.Interface',
):
    '''Minimal proxy for a wpa_supplicant managed interface.'''

    @dbus_method(input_signature='s', method_name='Roam')
    def roam(self, addr: str) -> None:
        raise NotImplementedError

    @dbus_method(method_name='Reassociate')
    def reassociate(self) -> None:
        raise NotImplementedError

    @dbus_property('s', property_name='State')
    def state(self) -> str:
        raise NotImplementedError

    @dbus_property('o', property_name='CurrentBSS')
    def current_bss(self) -> str:
        raise NotImplementedError


class _WpaBSS(
    DbusInterfaceCommon,
    interface_name='fi.w1.wpa_supplicant1.BSS',
):
    '''Minimal proxy for a scanned BSS.'''

    @dbus_property('ay', property_name='BSSID')
    def bssid(self) -> bytes:
        raise NotImplementedError


def _normalize_mac(mac: str) -> str:
    return mac.replace(':', '').replace('-', '').upper()


def _normalize_mac_bytes(raw: bytes) -> str:
    return binascii.hexlify(bytes(raw)).decode('ascii').upper()


class _Backend:
    '''Strategy that applies the selected BSSID to the running radio.'''

    def startup(self) -> None:
        '''One-time preparation.'''

    def recover(self) -> None:
        '''Per-iteration step: bring the link back if it dropped.'''

    def apply(self, bssid: str | None, ap_path: str) -> bool:
        '''Pin (bssid) or release (None) the BSSID.

        Returns False only on a recoverable failure so the caller can retry.
        '''
        raise NotImplementedError

    def is_pinned(self, bssid: str) -> bool:
        '''Whether the radio is currently on the requested BSSID.'''
        raise NotImplementedError


class _NmBackend(_Backend):
    '''Apply the BSSID by editing and re-activating the NM profile.'''

    def __init__(
        self,
        nm: NetworkManager,
        connection: NetworkConnectionSettings,
        profile: ConnectionProfile,
        connection_path: str,
        device_path: str,
        device: NetworkDeviceWireless,
        dry_run: bool,
    ) -> None:
        self._nm = nm
        self._connection = connection
        self._profile = profile
        self._connection_path = connection_path
        self._device_path = device_path
        self._device = device
        self._dry_run = dry_run
        self._wireless: WirelessSettings = profile.wireless  # type: ignore

    def startup(self) -> None:
        # A single link flap must never permanently block autoconnect, and the
        # profile must live on disk (not volatile runtime storage) so nothing
        # gets garbage-collected.
        if self._dry_run:
            return
        self._profile.connection.autoconnect = True
        self._profile.connection.autoconnect_retries = 0
        try:
            _persist_profile(self._connection, self._profile)
        except SdBusBaseError as error:
            logger.warning('Initial profile persist failed: %s', error)

    def recover(self) -> None:
        if self._dry_run:
            return
        _recover_device(
            nm=self._nm,
            connection=self._connection,
            profile=self._profile,
            connection_path=self._connection_path,
            device_path=self._device_path,
            device=self._device,
        )

    def apply(self, bssid: str | None, ap_path: str) -> bool:
        if bssid is not None:
            logger.debug('Switch BSSID to: %s', bssid)
            bssid_bytes = binascii.unhexlify(bssid.replace(':', ''))
            if self._wireless.bssid == bssid_bytes:
                return True
            self._wireless.bssid = bssid_bytes
        else:
            logger.debug('Reset BSSID')
            if self._wireless.bssid is None:
                return True
            self._wireless.bssid = None

        if self._dry_run:
            return True
        try:
            # Persist to disk under the existing UUID (idempotent, no duplicate
            # rows, no volatile churn), then re-activate the device.
            _persist_profile(self._connection, self._profile)
            _activate(
                nm=self._nm,
                connection_path=self._connection_path,
                device_path=self._device_path,
                ap_path=ap_path,
            )
            return True
        except SdBusBaseError as error:
            # Never let a transient NM/D-Bus failure crash the daemon.
            logger.warning('Failed to apply BSSID change: %s', error)
            return False

    def is_pinned(self, bssid: str) -> bool:
        active_connections = []
        for path in iter(self._nm.active_connections):
            try:
                active_connections.append(
                    ActiveConnection(path).connection
                )
            except SdBusBaseError:
                # The active connection may vanish between listing and reading
                # it; skip it rather than crash.
                pass
        return self._connection_path in active_connections


class _WpaBackend(_Backend):
    '''Apply the BSSID by asking wpa_supplicant to roam, over D-Bus.

    NetworkManager connection profiles are never modified, so this sidesteps
    the duplicate-connection / bond-master-teardown / cannot-reactivate
    problems of the profile-editing path entirely. Requires a wpa_supplicant
    exposing the D-Bus ``Roam`` method (>= 2.10) on the system bus; NM already
    runs one for the managed Wi-Fi device. Roam is a one-shot move (not a
    persistent pin) and only works while associated and when the target BSSID
    is a known BSS of the current SSID, so we re-apply it every interval.
    '''

    # States in which the radio is associated or on its way there; we only
    # nudge a reassociation when it is none of these.
    _LIVE_STATES = (
        'associating', 'associated', 'authenticating',
        '4way_handshake', 'group_handshake', 'completed',
    )

    def __init__(self, ifname: str, dry_run: bool) -> None:
        self._dry_run = dry_run
        supplicant = _WpaSupplicant(WPA_SERVICE_NAME, WPA_OBJECT_PATH)
        try:
            iface_path = supplicant.get_interface(ifname)
        except SdBusBaseError as error:
            logger.warning(
                'wpa_supplicant has no interface %s (%s); sleeping...',
                ifname, error,
            )
            _halt()
        self._iface = _WpaInterface(WPA_SERVICE_NAME, iface_path)
        logger.info('Using wpa_supplicant interface: %s', iface_path)

    def recover(self) -> None:
        if self._dry_run:
            return
        try:
            state = self._iface.state
        except SdBusBaseError as error:
            logger.warning('Failed to read wpa_supplicant state: %s', error)
            return
        if state in self._LIVE_STATES:
            return
        logger.info('Reassociating wpa_supplicant (state: %s)', state)
        try:
            self._iface.reassociate()
        except SdBusBaseError as error:
            logger.warning('wpa_supplicant reassociate failed: %s', error)

    def apply(self, bssid: str | None, ap_path: str) -> bool:
        if bssid is None:
            # Nothing to pin; leave the radio where wpa_supplicant put it.
            return True
        if self._dry_run:
            return True
        try:
            if self._current_bssid() == _normalize_mac(bssid):
                return True  # already on the target AP
            logger.debug('Roam to BSSID: %s', bssid)
            self._iface.roam(bssid)
            return True
        except SdBusBaseError as error:
            logger.warning('wpa_supplicant Roam failed: %s', error)
            return False

    def is_pinned(self, bssid: str) -> bool:
        try:
            return self._current_bssid() == _normalize_mac(bssid)
        except SdBusBaseError:
            return False

    def _current_bssid(self) -> str | None:
        path = self._iface.current_bss
        if not path or path == '/':
            return None
        raw = _WpaBSS(WPA_SERVICE_NAME, path).bssid
        return _normalize_mac_bytes(raw)


def _main() -> None:
    nm = NetworkManager()
    connection_path, connection, profile, device_path, device = \
        _find_device(nm)

    logger.info('Load geolocational informations')
    selector = _AccessPointSelector(
        sources=pd.read_csv(  # type: ignore
            Path(os.environ.get('SRC_FILE', 'sources.csv'))
        ),
        targets=pd.read_csv(  # type: ignore
            Path(os.environ.get('TGT_FILE', 'targets.csv'))
        ),
    )

    wireless: WirelessSettings = profile.wireless  # type: ignore

    ssid: bytes = wireless.ssid  # type: ignore
    ssid_str = ssid.decode('utf-8')
    logger.info('Find BSSIDs: %s', ssid_str)

    dry_run = os.environ.get('DRY_RUN', 'false') == 'true'
    interval_secs = float(os.environ.get('INTERVAL_SECS', '30'))
    one_shot = os.environ.get('ONE_SHOT', 'false') == 'true'
    backend_name = os.environ.get('BACKEND', 'nm').lower()

    if backend_name in ('wpa', 'wpa_supplicant', 'supplicant'):
        logger.info('Backend: wpa_supplicant (D-Bus)')
        backend: _Backend = _WpaBackend(device.interface, dry_run)
    else:
        logger.info('Backend: NetworkManager')
        backend = _NmBackend(
            nm=nm,
            connection=connection,
            profile=profile,
            connection_path=connection_path,
            device_path=device_path,
            device=device,
            dry_run=dry_run,
        )

    backend.startup()

    last_bssid = None
    while True:
        # If the link dropped, bring it back without an NM restart.
        backend.recover()

        try:
            bssids, bssid_to_ap_path = _find_bssids(device, ssid)
        except (SdBusBaseError, ValueError) as error:
            # SdBusBaseError: scan/AP enumeration failed (device down, bus
            # busy). ValueError: max() over zero matching APs. Either way,
            # back off and retry next interval instead of crashing.
            logger.warning('Failed to scan APs: %s', error)
            sleep(interval_secs)
            continue
        logger.debug('Detected BSSIDs: %s', repr(bssids))

        # Select and apply the best BSSID.
        bssid = selector.find(bssids)
        ap_path = bssid_to_ap_path.get(
            bssid.upper(), '/',
        ) if bssid is not None else '/'
        applied = backend.apply(bssid, ap_path)

        # Verify / revert (skipped while ONE_SHOT keeps a working pin).
        if not one_shot or bssid is None or last_bssid is None:
            if bssid is not None:
                if applied and backend.is_pinned(bssid):
                    logger.debug('Succeeded switching BSSID')
                    last_bssid = bssid
                else:
                    backend.apply(None, '/')
                    last_bssid = None
        else:
            logger.debug('Kept the original BSSID: %s', last_bssid)

        logger.debug('Waiting for %.02f seconds...', interval_secs)
        sleep(interval_secs)


if __name__ == '__main__':
    # Configure logger
    if os.environ.get('DEBUG', 'false') == 'true':
        level = logging.DEBUG  # pylint: disable=C0103
    else:
        level = logging.INFO  # pylint: disable=C0103
    logging.basicConfig(level=level)

    # Use system D-Bus
    sdbus.set_default_bus(sdbus.sd_bus_open_system())

    # Do optimize
    _main()
