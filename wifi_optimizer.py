#!/usr/bin/env python
'''A simple Wi-Fi connection optimizer based on the geolocation.'''

import binascii
import logging
import os
from pathlib import Path
import subprocess
import sys
from threading import Event
from time import sleep
from typing import NoReturn

import numpy as np
import pandas as pd
import sdbus
from sdbus.dbus_exceptions import DbusUnknownMethodError
from sdbus_block.networkmanager import (
    AccessPoint,
    ActiveConnection,
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
    profiles = [
        connection.get_profile()
        for connection in connections
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
    devices = [
        NetworkDeviceWireless(
            nm.get_device_by_ip_iface(profile.connection.interface_name)
        )
        for profile in profiles
        # already checked on `wifi_indices`
        if profile.connection.interface_name is not None
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
        device = devices[selected_index]
        logger.info('Selected interface: %s', device.interface)
    except IndexError:
        logger.info('No available wifi interfaces; sleeping...')
        return _halt()
    return connection_path, connection, profile, device


def _find_bssids(
    device: NetworkDeviceWireless,
    ssid: bytes,
) -> list[str]:
    logger.debug('Rescan APs')
    device.request_scan(
        options={
            'ssids': ('aay', [ssid]),
        },
    )
    aps = [
        AccessPoint(path)
        for path in device.access_points
    ]
    aps = [
        ap
        for ap in aps
        if ap.ssid == ssid
    ]
    max_bitrate = max(
        ap.max_bitrate
        for ap in aps
    )
    return [
        ap.hw_address
        for ap in aps
        if ap.max_bitrate == max_bitrate
    ]


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


def _main() -> None:
    nm = NetworkManager()
    connection_path, connection, profile, device = _find_device(nm)

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

    def _update_bssid(bssid: str | None) -> None:
        if bssid is not None:
            logger.debug('Switch BSSID to: %s', bssid)
            bssid_bytes = binascii.unhexlify(bssid.replace(':', ''))
            if wireless.bssid == bssid_bytes:
                return
            wireless.bssid = bssid_bytes
        else:
            logger.debug('Reset BSSID')
            if wireless.bssid is None:
                return
            wireless.bssid = None

        logger.debug('Update wifi profile')
        if not dry_run:
            connection.update_profile(profile, save_to_disk=False)
            nm.activate_connection(
                connection=connection_path,
            )

    last_bssid = None
    while True:
        bssids = _find_bssids(device, ssid)
        logger.debug('Detected BSSIDs: %s', repr(bssids))

        # Update BSSID
        bssid = selector.find(bssids)
        _update_bssid(bssid)

        # Skip if already changed
        if not one_shot or bssid is None or last_bssid is None:
            # Revert on failure
            if bssid is not None:
                active_connections = []
                for path in iter(nm.active_connections):
                    try:
                        active_connections.append(
                            ActiveConnection(path).connection
                        )
                    except DbusUnknownMethodError:
                        pass
                if connection_path in active_connections:
                    logger.debug('Succeeded switching BSSID')
                    last_bssid = bssid
                else:
                    _update_bssid(None)
                    last_bssid = None
        else:
            logger.debug('Keeped the original BSSID: %s', last_bssid)

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
