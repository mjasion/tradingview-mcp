# Offsite Raspberry Pi Zero W egress proxy — SHELVED (ARMv6)

Idea: run a tinyproxy on an offsite Pi (managed by balenaCloud) so its
**residential** IP joins `PROXY_EXTRA_URLS` as an extra Yahoo egress. A 2nd
residential IP is high-value (Yahoo trusts residential >> datacenter).

## Why it's parked

The original **Pi Zero W is ARMv6** (BCM2835). Modern Go tunnel daemons ship
ARMv7 (`GOARM=7`) builds that crash with **"Illegal instruction"** on ARMv6 —
this hits *both* candidates:

- cloudflared — official `cloudflared-linux-arm` is ARMv7; only fragile
  third-party ARMv6 glibc builds work (won't run on Alpine/musl):
  https://github.com/cloudflare/cloudflared/issues/1162
- tailscale — same crash; single `arm` tarball, ARMv6 unconfirmed:
  https://github.com/tailscale/tailscale/issues/6778

tinyproxy itself is fine (pure C, Alpine armhf). The blocker is purely the
*transport* to reach an offsite/NAT'd device.

## Recommended approach when revisiting (ARMv6-safe)

Use only C tooling — no Go daemons:

1. **Image** (balena, ARMv6): `FROM balenalib/raspberry-pi-alpine` +
   `apk add --no-cache tinyproxy autossh openssh-client`.
2. **tinyproxy** on `:8888` (BasicAuth + `Allow` private ranges).
3. **autossh reverse tunnel** out to an Oracle node (public IP = hub), e.g.
   `autossh -M0 -N -R 127.0.0.1:18888:localhost:8888 tunnel@<oracle-ip>`
   (outbound-only → NAT-friendly; SSH key as a balena fleet variable).
4. On the Oracle node, `127.0.0.1:18888` now reaches the Pi's tinyproxy;
   expose it the same way as the node-local proxy and add to the MCP
   `PROXY_EXTRA_URLS`.

Deploy stays 100% remote via `balena push` — no direct device access needed.

Alternative: WireGuard with an Oracle node as hub (kernel module + `wg`/
`wg-quick`, both C). More setup, also ARMv6-safe.

(Pi Zero **2** W is ARMv8/64-bit and avoids all of this — there cloudflared/
tailscale official images just work.)
