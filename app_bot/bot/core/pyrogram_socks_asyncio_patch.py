"""Pyrogram + PySocks: asyncio requires a non-blocking socket for loop.sock_connect (3.11+)."""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from typing import Tuple

import socks

from pyrogram.connection.transport.tcp import tcp as tcp_mod


async def _connect_via_proxy_patched(self: tcp_mod.TCP, destination: Tuple[str, int]) -> None:
    scheme = self.proxy.get('scheme')
    if scheme is None:
        raise ValueError('No scheme specified')

    proxy_type = tcp_mod.proxy_type_by_scheme.get(scheme.upper())
    if proxy_type is None:
        raise ValueError(f'Unknown proxy type {scheme}')

    hostname = self.proxy.get('hostname')
    port = self.proxy.get('port')
    username = self.proxy.get('username')
    password = self.proxy.get('password')

    try:
        ip_address = ipaddress.ip_address(hostname)
    except ValueError:
        is_proxy_ipv6 = False
    else:
        is_proxy_ipv6 = isinstance(ip_address, ipaddress.IPv6Address)

    proxy_family = socket.AF_INET6 if is_proxy_ipv6 else socket.AF_INET
    sock = socks.socksocket(proxy_family)

    sock.set_proxy(
        proxy_type=proxy_type,
        addr=hostname,
        port=port,
        username=username,
        password=password,
    )
    sock.setblocking(False)

    await self.loop.sock_connect(sock=sock, address=destination)

    self.reader, self.writer = await asyncio.open_connection(sock=sock)


def apply_pyrogram_socks_asyncio_patch() -> None:
    if getattr(tcp_mod.TCP, '_socks_asyncio_patch_applied', False):
        return
    tcp_mod.TCP._connect_via_proxy = _connect_via_proxy_patched
    tcp_mod.TCP._socks_asyncio_patch_applied = True
